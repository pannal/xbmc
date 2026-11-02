// SPDX-License-Identifier: GPL-2.0-or-later
// Deterministic decoder, GL completion and job-admission boundaries.

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <exception>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
static thread_local int heldLocks = 0;
static std::atomic<unsigned> checks{0}, cases{0};
static void Check(bool ok, const char* text)
{
  ++checks;
  if (!ok)
    throw std::runtime_error(text);
}
struct CCriticalSection
{
  std::recursive_mutex mutex;
  void lock()
  {
    mutex.lock();
    ++heldLocks;
  }
  void unlock()
  {
    --heldLocks;
    mutex.unlock();
  }
};
constexpr int LOGDEBUG = 0, LOGERROR = 1;
struct CLog
{
  template<class... T>
  static void Log(int, const char*, T&&...)
  {
  }
};
struct Barrier
{
  std::mutex mutex;
  std::condition_variable cv;
  bool entered = false, released = false;
  void Wait()
  {
    std::unique_lock<std::mutex> l(mutex);
    entered = true;
    cv.notify_all();
    Check(cv.wait_for(l, 3s, [&] { return released; }), "fixture barrier release timeout");
  }
  void Await()
  {
    std::unique_lock<std::mutex> l(mutex);
    Check(cv.wait_for(l, 3s, [&] { return entered; }), "fixture barrier entry timeout");
  }
  void Release()
  {
    std::lock_guard<std::mutex> l(mutex);
    released = true;
    cv.notify_all();
  }
};
struct State
{
  bool cacheHit = true, useCache = true, needsChecking = false, async = true, directFails = false,
       cacheFails = false, emptyPath = false;
  bool uploadUnsupported = false, throwUpload = false;
  int blockDecode = 0;
  bool blockUpload = false, blockAfterUpload = false;
  std::atomic<int> decodes{0}, uploads{0}, refreshes{0}, destroyed{0}, gpuDestroyedWithLock{0},
      leases{0}, releases{0};
  // Background delay setting, on so that deferral is exercised; the product default is off.
  bool delayBackgrounds = true;
  Barrier decodeBarrier, uploadBarrier;
  void Decode(int n)
  {
    if (n == blockDecode)
      decodeBarrier.Wait();
  }
};
static thread_local State* currentState = nullptr;
class CTexture
{
public:
  State* state;
  bool uploaded = false;
  explicit CTexture(State* s) : state(s) {}
  ~CTexture()
  {
    ++state->destroyed;
    if (uploaded && heldLocks)
      ++state->gpuDestroyedWithLock;
  }
  void SetBackgroundTexture(bool) {}
  unsigned GetWidth() const
  {
    Check(state->leases == 0, "metadata read after upload lease release");
    return 64;
  }
  unsigned GetHeight() const
  {
    Check(state->leases == 0, "metadata read after upload lease release");
    return 32;
  }
  void LoadToGPUAsync(const std::atomic_bool* cancelled = nullptr)
  {
    Check(heldLocks == 0, "upload work outside manager/job locks");
    if (cancelled && cancelled->load())
      return;
    if (state->uploadUnsupported)
      return;
    ++state->leases;
    if (state->blockUpload)
      state->uploadBarrier.Wait();
    if (cancelled && cancelled->load())
    {
      --state->leases;
      ++state->releases;
      return;
    }
    ++state->uploads;
    uploaded = true;
    if (state->blockAfterUpload)
      state->uploadBarrier.Wait();
    --state->leases;
    ++state->releases;
    if (state->throwUpload)
      throw std::runtime_error("upload fixture partial failure");
  }
  static std::unique_ptr<CTexture> LoadFromFile(const std::string&, int, int)
  {
    auto& s = *currentState;
    int n = ++s.decodes;
    s.Decode(n);
    return s.directFails ? nullptr : std::make_unique<CTexture>(&s);
  }
};
struct CTextureArray
{
  std::vector<std::shared_ptr<CTexture>> m_textures;
  unsigned m_width = 0, m_height = 0;
  std::size_t size() const { return m_textures.size(); }
  void Set(std::shared_ptr<CTexture> texture, unsigned w, unsigned h)
  {
    m_textures.clear();
    m_width = w;
    m_height = h;
    m_textures.push_back(std::move(texture));
  }
  void Set(std::unique_ptr<CTexture> texture, unsigned w, unsigned h)
  {
    Set(std::shared_ptr<CTexture>(std::move(texture)), w, h);
  }
  void Free()
  {
    m_textures.clear();
    m_width = m_height = 0;
  }
};
class CJob;
struct IJobCallback
{
  virtual ~IJobCallback() = default;
  virtual void OnJobComplete(unsigned, bool, CJob*) = 0;
  virtual void OnJobProgress(unsigned, unsigned, unsigned, const CJob*) {}
};
class CJobManager;
class CJob
{
  friend class CJobManager;
  CJobManager* m_callback = nullptr;

public:
  enum PRIORITY
  {
    PRIORITY_LOW_PAUSABLE = 0,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_DEDICATED
  };
  virtual ~CJob() = default;
  virtual bool DoWork() = 0;
  virtual const char* GetType() const { return "FixtureJob"; }
  virtual bool ShouldCancel(unsigned, unsigned) const;
};
struct CWorkItem
{
  CJob* m_job;
  unsigned m_id;
  IJobCallback* m_callback;
  bool operator==(unsigned id) const { return m_id == id; }
  bool operator==(const CJob* j) const { return m_job == j; }
  void FreeJob() { delete m_job; }
  void Cancel() { m_callback = nullptr; }
};
class CJobManager
{
public:
  using JobQueue = std::deque<CWorkItem>;
  using Processing = std::vector<CWorkItem>;
  mutable CCriticalSection m_section;
  JobQueue m_jobQueue[5];
  Processing m_processing;
  unsigned next = 1;
  bool reject = false;
  int added[5]{};
  unsigned AddJob(CJob* job, IJobCallback* callback, CJob::PRIORITY priority)
  {
    std::unique_lock<CCriticalSection> l(m_section);
    ++added[priority];
    if (reject)
    {
      delete job;
      return 0;
    }
    unsigned id = next++;
    m_jobQueue[priority].push_back({job, id, callback});
    return id;
  }
  CJob* Start(unsigned id)
  {
    std::unique_lock<CCriticalSection> l(m_section);
    for (auto& q : m_jobQueue)
    {
      auto i = std::find(q.begin(), q.end(), id);
      if (i != q.end())
      {
        auto item = *i;
        q.erase(i);
        m_processing.push_back(item);
        item.m_job->m_callback = this;
        return item.m_job;
      }
    }
    throw std::runtime_error("fixture queued job missing");
  }
  unsigned First() const
  {
    for (const auto& q : m_jobQueue)
      if (!q.empty())
        return q.front().m_id;
    return 0;
  }
  unsigned Queued() const
  {
    unsigned n = 0;
    for (const auto& q : m_jobQueue)
      n += q.size();
    return n;
  }
  bool m_pauseJobs{false};
  CJob* PopJob();
  unsigned GetMaxWorkers(CJob::PRIORITY);
  void CancelJob(unsigned);
  bool OnJobProgress(unsigned, unsigned, const CJob*) const;
  void OnJobComplete(bool, CJob*);
  ~CJobManager()
  {
    for (auto& q : m_jobQueue)
      for (auto item : q)
        item.FreeJob();
    for (auto item : m_processing)
      item.FreeJob();
  }
};
static CJobManager* activeJobs = nullptr;
class CGUILargeTextureManager;
struct FakeTextureManager
{
  std::string GetTexturePath(const std::string& p) { return currentState->emptyPath ? "" : p; }
};
struct FakeGUI
{
  FakeTextureManager manager;
  FakeTextureManager& GetTextureManager() { return manager; }
};
struct FakeCache
{
  std::string CheckCachedImage(const std::string& p, bool& checking)
  {
    checking = currentState->needsChecking;
    return currentState->cacheHit ? p : "";
  }
  void BackgroundCacheImage(const std::string&) { ++currentState->refreshes; }
  void CacheImage(const std::string&, std::unique_ptr<CTexture>* out)
  {
    auto& s = *currentState;
    int n = ++s.decodes;
    s.Decode(n);
    if (!s.cacheFails)
      *out = std::make_unique<CTexture>(&s);
  }
};
struct FakeGraphics
{
  int GetWidth() const { return 3840; }
  int GetHeight() const { return 2160; }
};
struct FakeWindow
{
  FakeGraphics gfx;
  FakeGraphics& GetGfxContext() { return gfx; }
};
struct FakeAdvanced
{
  bool m_guiAsyncTextureUpload = true;
};
struct CSettings
{
  static constexpr auto SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES =
      "lookandfeel.delaybackgroundimages";
  static constexpr auto SETTING_LOOKANDFEEL_DELAYSELECTIONIMAGES =
      "lookandfeel.delayselectionimages";
  bool GetBool(const std::string& id) const
  {
    return id == SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES && currentState->delayBackgrounds;
  }
};
struct FakeSettings
{
  FakeAdvanced* GetAdvancedSettings()
  {
    static thread_local FakeAdvanced a;
    a.m_guiAsyncTextureUpload = currentState->async;
    return &a;
  }
  CSettings* GetSettings()
  {
    static CSettings s;
    return &s;
  }
};
struct CServiceBroker
{
  static FakeGUI* GetGUI()
  {
    static FakeGUI g;
    return &g;
  }
  static FakeCache* GetTextureCache()
  {
    static FakeCache c;
    return &c;
  }
  static FakeWindow* GetWinSystem()
  {
    static FakeWindow w;
    return &w;
  }
  static FakeSettings* GetSettingsComponent()
  {
    static FakeSettings s;
    return &s;
  }
  static CJobManager* GetJobManager() { return activeJobs; }
};
static unsigned frameTime = 0;
struct CTimeUtils
{
  static unsigned GetFrameTime() { return frameTime; }
};
