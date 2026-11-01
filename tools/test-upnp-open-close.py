#!/usr/bin/env python3
"""Execute the production CUPnPPlayer open/close methods against a recording renderer.

Extracts WaitOnEvent, PlayFile, OpenFile, CloseFile, the destructor, the controls,
OnExit and one pass of the worker loop from xbmc/network/upnp/UPnPPlayer.cpp and
splices them into a C++ prelude with stub Platinum/Neptune types. The fake renderer records every
command in order and answers each command inside the next busy-dialog wait, or
between two frames of the application. A hook runs inside that wait, as the
GUI/application message pump does, and closes the player or starts a newer player
on the same renderer. The application calls CloseFile once each frame until the
close completes. The Platinum stack, threads and the real GUI are not simulated.
Requires Python 3 and g++; runs with ASan/UBSan.
"""
import argparse
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>
using namespace std::chrono_literals;

std::string g_case;
#define CHECK(c) do { if (!(c)) { \
  std::fprintf(stderr, "Assertion failed: [%s] %s (line %d)\n", g_case.c_str(), #c, __LINE__); \
  std::abort(); } } while (0)

typedef int NPT_Result;
typedef unsigned NPT_UInt32;
typedef unsigned NPT_Cardinal;
#define NPT_SUCCESS 0
#define NPT_FAILURE (-1)
#define NPT_FAILED(r) ((r) != NPT_SUCCESS)
#define NPT_SUCCEEDED(r) ((r) == NPT_SUCCESS)
#define NPT_CHECK_LABEL(x, l) do { NPT_Result r_ = (x); if (r_ != NPT_SUCCESS) goto l; } while (0)
#define NPT_CHECK_LABEL_SEVERE(x, l) NPT_CHECK_LABEL(x, l)
#define NPT_CHECK_POINTER_LABEL_SEVERE(p, l) do { if (!(p)) goto l; } while (0)
#define NPT_SET_LOCAL_LOGGER(x)

struct NPT_String : std::string {
  NPT_String() = default;
  NPT_String(const char* s) : std::string(s) {}
  void Insert(const char* s, unsigned pos) { insert(pos, s); }
  void Append(const char* s) { append(s); }
  int Compare(const char* s) const { return compare(s); }
  void Replace(const char*, const char*) {}
  explicit operator const char*() const { return c_str(); }
};
template<class T> struct NPT_Reference {
  std::shared_ptr<T> p;
  NPT_Reference() = default;
  explicit NPT_Reference(T* t) : p(t) {}
  bool IsNull() const { return !p; }
  T& operator*() const { return *p; }
  T* operator->() const { return p.get(); }
};
struct PLT_ProtocolInfo {
  NPT_String s;
  PLT_ProtocolInfo() = default;
  PLT_ProtocolInfo(const NPT_String& v) : s(v) {}
  NPT_String GetContentType() const { return s; }
  NPT_String ToString() const { return s; }
};
struct PLT_MediaItemResource { NPT_String m_Uri; PLT_ProtocolInfo m_ProtocolInfo; };
struct ResourceList {
  std::vector<PLT_MediaItemResource> v;
  NPT_Cardinal GetItemCount() const { return v.size(); }
  PLT_MediaItemResource& operator[](size_t i) { return v[i]; }
};
struct PLT_MediaObject { ResourceList m_Resources; };
struct PLT_TransportInfo { NPT_String cur_transport_state, cur_transport_status, cur_speed; };
struct PLT_Didl {
  static NPT_Result ToDidl(PLT_MediaObject&, const char*, NPT_String&) { return NPT_SUCCESS; }
  static NPT_String FormatTimeStamp(NPT_UInt32) { return "00:00:00"; }
};
const char* didl_header = "<DIDL>";
const char* didl_footer = "</DIDL>";
enum EMediaControllerQuirks { EMEDIACONTROLLERQUIRKS_NONE = 0, EMEDIACONTROLLERQUIRKS_X_MKV = 1 };
struct CThumbLoader { virtual ~CThumbLoader() = default; };
struct CVideoThumbLoader : CThumbLoader {};
struct CMusicThumbLoader : CThumbLoader {};
const int UPnPPlayer = 1;
struct CUPnP {
  static void* GetServer() { return nullptr; }
  static void RegisterUserdata(void*);
  static void UnregisterUserdata(void*);
};
template<class F, class... A> NPT_Reference<PLT_MediaObject> BuildObject(F& file, A&&...) {
  auto* obj = new PLT_MediaObject();
  obj->m_Resources.v.resize(1);
  obj->m_Resources.v[0].m_Uri = file.GetPath().c_str();
  return NPT_Reference<PLT_MediaObject>(obj);
}

struct CFileItem {
  std::string path;
  bool video = true;
  explicit CFileItem(const char* p) : path(p) {}
  const std::string& GetPath() const { return path; }
  bool IsVideoDb() const { return false; }
  bool IsMusicDb() const { return false; }
  bool IsVideo() const { return video; }
  bool IsAudio() const { return !video; }
};
struct CPlayerOptions { double starttime = 0; };
struct IPlayerCallback {
  int started = 0, avstarted = 0, stopped = 0, ended = 0, paused = 0, resumed = 0;
  std::string events;  // the order of the events
  std::function<void()> hook;  // runs inside each event
  void Event(int& count, char event) {
    ++count;
    events += event;
    if (hook)
      hook();
  }
  void OnPlayBackStarted(const CFileItem&) { Event(started, 'S'); }
  void OnAVStarted(const CFileItem&) { Event(avstarted, 'A'); }
  void OnPlayBackStopped() { Event(stopped, 'T'); }
  void OnPlayBackEnded() { Event(ended, 'E'); }
  void OnPlayBackPaused() { Event(paused, 'P'); }
  void OnPlayBackResumed() { Event(resumed, 'R'); }
  int Total() const { return started + avstarted + stopped + ended; }
};
struct LoggerImpl {
  template<class... A> void error(const char*, A&&...) {}
  template<class... A> void debug(const char*, A&&...) {}
  template<class... A> void info(const char*, A&&...) {}
};
using Logger = std::shared_ptr<LoggerImpl>;
struct CDataCacheCore {
  float speed = 1.0f;
  static CDataCacheCore& GetInstance() { static CDataCacheCore c; return c; }
  // the production reset restores the default state of every player
  void Reset() { speed = 1.0f; }
  void SetPlayTimes(int64_t, int64_t, int64_t, int64_t) {}
  float GetSpeed() const { return speed; }
  void SetSpeed(float, float s) { speed = s; }
  void SeekFinished(int64_t) {}
};
struct CSettings {
  static constexpr const char* SETTING_SERVICES_UPNPPLAYERVOLUMESYNC = "services.upnpplayervolumesync";
  bool GetBool(const char*) const { return true; }
};
struct CSettingsComponent {
  std::shared_ptr<CSettings> settings = std::make_shared<CSettings>();
  std::shared_ptr<CSettings> GetSettings() { return settings; }
};
struct CServiceBroker {
  static CDataCacheCore& GetDataCacheCore() { return CDataCacheCore::GetInstance(); }
  static CSettingsComponent* GetSettingsComponent() { static CSettingsComponent c; return &c; }
};

void DeliverPending();
struct CCriticalSection : std::recursive_mutex {};
struct CEvent {
  bool signaled = false;
  void Set() { signaled = true; }
  void Reset() { signaled = false; }
  bool Wait(std::chrono::milliseconds d) {
    if (!signaled && d.count() > 0)
      DeliverPending();
    bool s = signaled;
    signaled = false;
    return s;
  }
};
namespace XbmcThreads {
template<class T = std::chrono::milliseconds> struct EndTime {
  EndTime() = default;
  template<class D> EndTime(D d) : initial(std::chrono::duration_cast<std::chrono::milliseconds>(d)) {}
  void Set(std::chrono::milliseconds d) { initial = d; polls = 0; }
  std::chrono::milliseconds GetInitialTimeoutValue() const { return initial; }
  bool IsTimePast() { return initial.count() == 0 || ++polls > 50; }
  std::chrono::milliseconds initial{0};
  int polls = 0;
};
}

// A busy-dialog wait runs the hook (the message pump), then the renderer answers.
int g_waits = 0;
std::function<void(int)> g_waitHook;
// The busy dialog waits until the event is set; a wait that no pending answer and
// no other wake can end never ends.
bool PendingEmpty();
struct CGUIDialogBusy {
  static bool WaitOnEvent(CEvent& event) {
    ++g_waits;
    auto hook = g_waitHook;
    if (hook)
      hook(g_waits);
    while (!event.Wait(0ms)) {
      CHECK(!PendingEmpty());  // the busy dialog would wait forever
      DeliverPending();
    }
    return true;
  }
};

namespace UPNP {
class CUPnPPlayerController;
struct PLT_DeviceDataReference {
  CUPnPPlayerController* owner = nullptr;
  void* AsPointer() { return nullptr; }
};
// The Platinum delegate: the controller answers a command to the userdata of the
// command.
struct PLT_MediaControllerDelegate {
  virtual ~PLT_MediaControllerDelegate() = default;
  virtual void OnSetAVTransportURIResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnSetNextAVTransportURIResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnPlayResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnStopResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnPauseResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnSeekResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnSetVolumeResult(NPT_Result, PLT_DeviceDataReference&, void*) {}
  virtual void OnGetTransportInfoResult(NPT_Result, PLT_DeviceDataReference&, PLT_TransportInfo*,
                                        void*) {}
};
EMediaControllerQuirks GetMediaControllerQuirks(void*) { return EMEDIACONTROLLERQUIRKS_NONE; }

struct PLT_MediaController {
  std::vector<std::string> log;
  struct Pending {
    void* userdata;
    int delay;
    std::function<void(PLT_MediaControllerDelegate&)> answer;
  };
  std::vector<Pending> pending;
  std::function<void(const std::string&)> cmdHook;
  std::string sendFails;  // the controller does not send the next command of this name
  void* failedUserdata = nullptr;  // the userdata of the command that was not sent
  NPT_Result Send(PLT_DeviceDataReference& device, void* userdata, const char* cmd,
                  std::function<void(PLT_MediaControllerDelegate&)> answer, int delay = 0);
  NPT_Result FindBestResource(PLT_DeviceDataReference&, PLT_MediaObject&, NPT_Cardinal& i) {
    i = 0;
    return NPT_SUCCESS;
  }
  NPT_Result GetTransportInfo(PLT_DeviceDataReference&, NPT_UInt32, void* userdata);
  NPT_Result Stop(PLT_DeviceDataReference&, NPT_UInt32, void* userdata);
  NPT_Result SetAVTransportURI(PLT_DeviceDataReference&, NPT_UInt32, const NPT_String&,
                               const NPT_String&, void* userdata);
  NPT_Result SetNextAVTransportURI(PLT_DeviceDataReference& d, NPT_UInt32, const char*, const char*,
                                   void* userdata) {
    return Send(d, userdata, "SetNextAVTransportURI", nullptr);
  }
  NPT_Result Play(PLT_DeviceDataReference&, NPT_UInt32, const char*, void* userdata);
  // A control: the answer comes after the delivery rounds of controlDelay
  NPT_Result Control(PLT_DeviceDataReference& d, void* userdata, const char* cmd,
                     void (PLT_MediaControllerDelegate::*result)(NPT_Result,
                                                                 PLT_DeviceDataReference&, void*));
  NPT_Result Seek(PLT_DeviceDataReference& d, NPT_UInt32, const char*, const NPT_String&,
                  void* userdata) {
    return Control(d, userdata, "Seek", &PLT_MediaControllerDelegate::OnSeekResult);
  }
  NPT_Result Pause(PLT_DeviceDataReference& d, NPT_UInt32, void* userdata) {
    return Control(d, userdata, "Pause", &PLT_MediaControllerDelegate::OnPauseResult);
  }
  NPT_Result SetVolume(PLT_DeviceDataReference& d, NPT_UInt32, const char*, int, void* userdata) {
    return Control(d, userdata, "SetVolume", &PLT_MediaControllerDelegate::OnSetVolumeResult);
  }
  NPT_Result GetPositionInfo(PLT_DeviceDataReference& d, NPT_UInt32, void* userdata) {
    return Send(d, userdata, "GetPositionInfo", nullptr);
  }
  NPT_Result GetMediaInfo(PLT_DeviceDataReference& d, NPT_UInt32, void* userdata) {
    return Send(d, userdata, "GetMediaInfo", nullptr);
  }
};

// The renderer of one player. The first GetTransportInfo after a stop reports
// firstState, later ones PLAYING.
class CUPnPPlayerController : public PLT_MediaControllerDelegate {
public:
  CUPnPPlayerController(PLT_MediaController* c, const char* n) : m_control(c), name(n) {
    m_device.owner = this;
  }
  NPT_String GetTransportState() const { return m_trainfo.cur_transport_state; }
  NPT_String GetTransportStatus() const { return m_trainfo.cur_transport_status; }
  void SetTransportInfo(const PLT_TransportInfo& info) { m_trainfo = info; }
  // The worker polls the renderer with the controller as the userdata
  void OnGetTransportInfoResult(NPT_Result res, PLT_DeviceDataReference&, PLT_TransportInfo* info,
                                void*) override {
    if (NPT_FAILED(res))
      m_trainfo.cur_transport_state = "STOPPED";
    else
      m_trainfo = *info;
  }
  bool pollDue = false;  // the worker polls the renderer
  void UpdatePositionInfo() {
    if (!std::exchange(pollDue, false))
      return;
    m_control->GetTransportInfo(m_device, m_instance, this);
    m_control->GetPositionInfo(m_device, m_instance, this);
  }
  PLT_MediaController* m_control;
  PLT_DeviceDataReference m_device;
  NPT_UInt32 m_instance = 0;
  PLT_TransportInfo m_trainfo;  // the state that the worker polls
  std::string name;
  std::string firstState = "STOPPED";
  int infoCalls = 0;
  int stopRejects = 0;  // the next Stop commands that the renderer rejects
  int stopDelay = 0;    // the delivery rounds that the answer of the next Stop waits
  int uriRejects = 0;   // the next SetAVTransportURI commands that the renderer rejects
  int transitioning = 0;  // the later GetTransportInfo answers that report TRANSITIONING
  std::string uri;      // the URI of the last SetAVTransportURI
  std::string nextState;  // the state of the next GetTransportInfo answer, once
  bool playUnanswered = false;  // the renderer never answers Play
  int controlDelay = 0;  // the delivery rounds that the answer of a control waits
  bool controlUnanswered = false;  // the renderer never answers a control
  bool stopUnanswered = false;  // the renderer never answers Stop
};

NPT_Result PLT_MediaController::Send(PLT_DeviceDataReference& device, void* userdata,
                                     const char* cmd,
                                     std::function<void(PLT_MediaControllerDelegate&)> answer,
                                     int delay) {
  log.push_back(device.owner->name + ":" + cmd);
  if (sendFails == cmd) {
    sendFails.clear();
    failedUserdata = userdata;
    return NPT_FAILURE;
  }
  if (answer)
    pending.push_back({userdata, delay, answer});
  if (cmdHook) {
    auto hook = cmdHook;
    hook(log.back());
  }
  return NPT_SUCCESS;
}
NPT_Result PLT_MediaController::GetTransportInfo(PLT_DeviceDataReference& device, NPT_UInt32,
                                                 void* userdata) {
  auto* r = device.owner;
  PLT_TransportInfo info;
  info.cur_transport_state = !r->nextState.empty() ? std::exchange(r->nextState, "").c_str()
                             : r->infoCalls++ == 0 ? r->firstState.c_str()
                             : r->transitioning > 0 ? (--r->transitioning, "TRANSITIONING")
                                                    : "PLAYING";
  info.cur_transport_status = "OK";
  return Send(device, userdata, "GetTransportInfo", [device, info](PLT_MediaControllerDelegate& d) {
    auto dev = device;
    auto i = info;
    d.OnGetTransportInfoResult(NPT_SUCCESS, dev, &i, nullptr);
  });
}
NPT_Result PLT_MediaController::Stop(PLT_DeviceDataReference& device, NPT_UInt32, void* userdata) {
  auto* r = device.owner;
  // the renderer decides when the command arrives
  const NPT_Result result = r->stopRejects > 0 ? (--r->stopRejects, NPT_FAILURE) : NPT_SUCCESS;
  if (result == NPT_SUCCESS)
    r->infoCalls = 0;
  const int delay = std::exchange(r->stopDelay, 0);
  if (r->stopUnanswered)
    return Send(device, userdata, "Stop", nullptr);
  return Send(device, userdata, "Stop", [device, result](PLT_MediaControllerDelegate& d) {
    auto dev = device;
    d.OnStopResult(result, dev, nullptr);
  }, delay);
}
NPT_Result PLT_MediaController::SetAVTransportURI(PLT_DeviceDataReference& device, NPT_UInt32,
                                                  const NPT_String& uri, const NPT_String&,
                                                  void* userdata) {
  auto* r = device.owner;
  const NPT_Result result = r->uriRejects > 0 ? (--r->uriRejects, NPT_FAILURE) : NPT_SUCCESS;
  if (result == NPT_SUCCESS)
    r->uri = uri;
  return Send(device, userdata, "SetAVTransportURI", [device, result](PLT_MediaControllerDelegate& d) {
    auto dev = device;
    d.OnSetAVTransportURIResult(result, dev, nullptr);
  });
}
NPT_Result PLT_MediaController::Play(PLT_DeviceDataReference& device, NPT_UInt32, const char*,
                                     void* userdata) {
  if (device.owner->playUnanswered)
    return Send(device, userdata, "Play", nullptr);
  return Send(device, userdata, "Play", [device](PLT_MediaControllerDelegate& d) {
    auto dev = device;
    d.OnPlayResult(NPT_SUCCESS, dev, nullptr);
  });
}

NPT_Result PLT_MediaController::Control(
    PLT_DeviceDataReference& device, void* userdata, const char* cmd,
    void (PLT_MediaControllerDelegate::*result)(NPT_Result, PLT_DeviceDataReference&, void*)) {
  auto* r = device.owner;
  if (r->controlUnanswered)
    return Send(device, userdata, cmd, nullptr);
  return Send(device, userdata, cmd, [device, result](PLT_MediaControllerDelegate& d) {
    auto dev = device;
    (d.*result)(NPT_SUCCESS, dev, nullptr);
  }, r->controlDelay);
}

PLT_MediaController g_ctl;
std::vector<void*> g_userdata;  // the Platinum userdata registry
std::vector<void*> g_unregistered;  // every userdata that left the registry
bool Registered(void* userdata) {
  return std::find(g_userdata.begin(), g_userdata.end(), userdata) != g_userdata.end();
}

class CUPnPCommand;
class CUPnPPlayer {
public:
  CUPnPPlayer(IPlayerCallback& cb, const char* name)
    : m_control(&g_ctl),
      m_delegate(std::make_unique<CUPnPPlayerController>(&g_ctl, name)),
      m_callback(cb),
      m_logger(std::make_shared<LoggerImpl>()) {
    CUPnP::RegisterUserdata(m_delegate.get());
  }
  ~CUPnPPlayer();
  bool OpenFile(const CFileItem& file, const CPlayerOptions& options);
  bool CloseFile(bool reopen = false);
  int PlayFile(const CFileItem& file, const CPlayerOptions& options, XbmcThreads::EndTime<>& timeout,
               uint64_t open);
  void CancelOpen();
  int WaitOnEvent(CUPnPCommand& command, XbmcThreads::EndTime<>& timeout, uint64_t open);
  std::shared_ptr<CUPnPCommand> SendCommand(const std::function<int(void* userdata)>& send);
  void Pause();
  void SeekTime(int64_t ms);
  void SetSpeed(float speed);
  void SetVolume(float volume);
  void OnExit();
  bool IsRunning() const { return running; }
  void Create() { ++creates; running = true; }
  void StopThread(bool) { running = false; }
  bool IsPaused() const;
  int64_t GetTime() { return 0; }
  int64_t GetTotalTime() { return 0; }
  // One pass of the worker loop of CUPnPPlayer::Process.
  void WorkerStep();

  PLT_MediaController* m_control;
  std::unique_ptr<CUPnPPlayerController> m_delegate;
  IPlayerCallback& m_callback;
  std::atomic<bool> m_started{false};
  std::atomic<bool> m_stopremote{false};
  std::atomic<bool> m_closed{false};
  std::atomic<uint64_t> m_openSequence{0};
  CCriticalSection m_waitSection;
  std::vector<CUPnPCommand*> m_waiting;
  CCriticalSection m_itemSection;
  std::vector<std::shared_ptr<CUPnPCommand>> m_commands;
  XbmcThreads::EndTime<> m_commandTimeout;
  std::shared_ptr<CUPnPCommand> m_stop;
  XbmcThreads::EndTime<> m_stopTimeout;
  bool m_hasVideo{false};
  bool m_hasAudio{false};
  XbmcThreads::EndTime<> m_updateTimer;
  Logger m_logger;
  int creates = 0;
  bool running = false;
  // destroyed after the destructor body, before the other members: the worker
  // must not run with destroyed members
  struct ThreadCheck {
    CUPnPPlayer* player;
    ~ThreadCheck() { CHECK(!player->running); }
  } m_threadCheck{this};
};
}  // namespace UPNP

void CUPnP::RegisterUserdata(void* userdata) { UPNP::g_userdata.push_back(userdata); }
void CUPnP::UnregisterUserdata(void* userdata) {
  UPNP::g_unregistered.push_back(userdata);
  auto& v = UPNP::g_userdata;
  v.erase(std::remove(v.begin(), v.end(), userdata), v.end());
}
bool PendingEmpty() { return UPNP::g_ctl.pending.empty(); }
// One delivery round. Platinum drops the answer for a userdata that is not
// registered.
void DeliverPending() {
  auto v = std::move(UPNP::g_ctl.pending);
  UPNP::g_ctl.pending.clear();
  for (auto& e : v) {
    if (e.delay > 0) {
      --e.delay;
      UPNP::g_ctl.pending.push_back(e);
      continue;
    }
    auto& r = UPNP::g_userdata;
    if (std::find(r.begin(), r.end(), e.userdata) != r.end())
      e.answer(*static_cast<UPNP::PLT_MediaControllerDelegate*>(e.userdata));
  }
}
using namespace UPNP;

@PRODUCTION@

// ---- scenarios ----
const std::vector<std::string> NORMAL = {"GetTransportInfo", "SetAVTransportURI", "Play",
                                         "GetTransportInfo", "GetPositionInfo", "GetMediaInfo"};
CFileItem Item(const char* path = "http://server/a.mkv") { return CFileItem(path); }
void Begin(const std::string& name) {
  g_case = name;
  g_ctl.log.clear();
  g_ctl.pending.clear();
  g_ctl.cmdHook = nullptr;
  g_waitHook = nullptr;
  g_waits = 0;
}
std::vector<std::string> Of(const char* who, std::vector<std::string> v) {
  for (auto& s : v)
    s = std::string(who) + ":" + s;
  return v;
}
std::vector<std::string> Only(const char* who) {
  std::vector<std::string> v;
  const std::string prefix = std::string(who) + ":";
  for (auto& s : g_ctl.log)
    if (s.compare(0, prefix.size(), prefix) == 0)
      v.push_back(s);
  return v;
}
int Count(const std::string& entry) {
  int n = 0;
  for (auto& s : g_ctl.log)
    n += s == entry;
  return n;
}
void ExpectLog(const std::vector<std::string>& want) {
  if (g_ctl.log != want) {
    std::fprintf(stderr, "log mismatch, got:");
    for (auto& s : g_ctl.log)
      std::fprintf(stderr, " %s", s.c_str());
    std::fprintf(stderr, "\n");
    CHECK(g_ctl.log == want);
  }
}
std::vector<std::string> Head(std::vector<std::string> v, size_t n) {
  v.resize(n);
  return v;
}
// The application calls CloseFile once each frame until the close completes; the
// renderer answers between two frames. Returns the number of frames.
int Close(CUPnPPlayer& p) {
  int frames = 1;
  while (!p.CloseFile()) {
    CHECK(frames < 100);  // the close never completes
    DeliverPending();
    ++frames;
  }
  return frames;
}

void normal_open_close() {
  Begin("normal open then close");
  auto want = Of("P", NORMAL);
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    ExpectLog(want);
    CHECK(cb.started == 1 && cb.avstarted == 1 && p.creates == 1 && p.m_started);
    // the item starts with the last state of the open
    CHECK(p.m_delegate->GetTransportState() == "PLAYING");
    Close(p);
    want.push_back("P:Stop");
    ExpectLog(want);
    CHECK(cb.stopped == 1 && cb.ended == 0);
  }
  ExpectLog(want);  // the destructor must not stop a second time
  CHECK(cb.stopped == 1);
}

void close_twice() {
  Begin("close twice stops once");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    Close(p);
    CHECK(p.CloseFile());
  }
  CHECK(Count("P:Stop") == 1 && cb.stopped == 1);
}

// The close does not wait for the renderer: the close sends Stop once and
// completes after the answer, in a later frame.
void close_without_wait() {
  Begin("close without a wait for the renderer");
  IPlayerCallback cb;
  CUPnPPlayer p(cb, "P");
  CHECK(p.OpenFile(Item(), CPlayerOptions()));
  CHECK(!p.CloseFile());
  CHECK(!p.CloseFile());
  CHECK(Count("P:Stop") == 1 && !g_ctl.pending.empty() && p.running);
  DeliverPending();
  CHECK(p.CloseFile());
  CHECK(Count("P:Stop") == 1 && cb.stopped == 1 && !p.running);
}

void close_inside_wait() {
  for (size_t n = 1; n <= 4; ++n) {
    Begin("close inside wait " + std::to_string(n));
    IPlayerCallback cb;
    auto want = Of("P", Head(NORMAL, n));
    if (n >= 3)
      want.push_back("P:Stop");  // Play was sent: the renderer is stopped once
    {
      CUPnPPlayer p(cb, "P");
      g_waitHook = [&](int w) {
        if (w != int(n))
          return;
        g_waitHook = nullptr;
        CHECK(p.CloseFile() == (n < 2));  // the close waits for the answer of the URI and of Play
      };
      CHECK(!p.OpenFile(Item(), CPlayerOptions()));
      CHECK(cb.Total() == 0 && p.creates == 0 && !p.running);
      Close(p);
      ExpectLog(want);
    }
    ExpectLog(want);  // the destructor sends nothing
  }
}

void close_inside_wait_renderer_playing() {
  const std::vector<std::string> flow = {"GetTransportInfo", "Stop", "SetAVTransportURI", "Play"};
  for (size_t n = 2; n <= 4; n += 2) {
    Begin("renderer playing, close inside wait " + std::to_string(n));
    IPlayerCallback cb;
    auto want = Of("P", Head(flow, n));
    if (n >= 3)
      want.push_back("P:Stop");
    {
      CUPnPPlayer p(cb, "P");
      p.m_delegate->firstState = "PLAYING";
      g_waitHook = [&](int w) {
        if (w != int(n))
          return;
        g_waitHook = nullptr;
        p.CloseFile();
      };
      CHECK(!p.OpenFile(Item(), CPlayerOptions()));
      CHECK(cb.Total() == 0 && p.creates == 0);
      Close(p);
      ExpectLog(want);
    }
    ExpectLog(want);
  }
}

// The application replaces a remote player: the newer player opens after the
// close of the older player completes.
void newer_player_on_same_renderer() {
  for (size_t n : {size_t(1), size_t(3)}) {
    Begin("newer player starts inside wait " + std::to_string(n));
    IPlayerCallback cbA, cbB;
    auto a = std::make_unique<CUPnPPlayer>(cbA, "A");
    auto b = std::make_unique<CUPnPPlayer>(cbB, "B");
    g_waitHook = [&](int w) {
      if (w != int(n))
        return;
      g_waitHook = nullptr;
      Close(*a);
      CHECK(b->OpenFile(Item(), CPlayerOptions()));
    };
    CHECK(!a->OpenFile(Item(), CPlayerOptions()));
    CHECK(cbA.Total() == 0 && a->creates == 0);
    CHECK(cbB.started == 1 && cbB.avstarted == 1 && b->creates == 1);
    auto wantA = Of("A", Head(NORMAL, n));
    if (n >= 3)
      wantA.push_back("A:Stop");
    CHECK(Only("A") == wantA);
    CHECK(Only("B") == Of("B", NORMAL));
    a.reset();
    CHECK(Only("A") == wantA);  // the old destructor must not stop the newer playback
    CHECK(Count("B:Stop") == 0);
    b.reset();
    CHECK(Count("B:Stop") == 1);
  }
}

// A refused Stop completes the close once: the renderer decides to keep playing.
// The close does not stop the renderer again in each frame.
void refused_stop() {
  Begin("refused stop after an open");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    p.m_delegate->stopRejects = 1;
    CHECK(Close(p) == 2);
    CHECK(p.CloseFile());
    CHECK(cb.stopped == 1 && !p.running);
  }
  CHECK(Count("P:Stop") == 1);

  // The answer of Play arrives during the close: the close sends the Stop after
  // the answer, the renderer refuses the Stop, and the close completes once.
  Begin("late answer of Play during the stop of a close");
  {
    CUPnPPlayer p(cb, "P");
    g_waitHook = [&](int w) {
      if (w != 3)  // the wait for the answer to Play
        return;
      g_waitHook = nullptr;
      p.m_delegate->stopRejects = 1;
      p.m_delegate->stopDelay = 1;
      CHECK(!p.CloseFile());
    };
    CHECK(!p.OpenFile(Item(), CPlayerOptions()));
    CHECK(Count("P:Stop") == 0);
    DeliverPending();  // the answer of Play
    CHECK(!p.CloseFile() && Count("P:Stop") == 1);
    DeliverPending();
    DeliverPending();  // the answer of Stop
    CHECK(p.CloseFile());
  }
  CHECK(Count("P:Stop") == 1);
}

// A renderer that never answers Stop does not keep the close: the close
// completes at the deadline.
void unanswered_stop() {
  Begin("renderer never answers Stop");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    p.m_delegate->stopUnanswered = true;
    CHECK(Close(p) > 2);
    CHECK(cb.stopped == 1 && !p.running);
  }
  CHECK(Count("P:Stop") == 1);
}

// The worker and the close report one end of the item between them.
void one_end_notification() {
  Begin("the worker sees the stop of the close");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    CHECK(!p.CloseFile());
    DeliverPending();
    // the worker polls the stopped renderer before the close completes
    p.m_delegate->pollDue = true;
    p.WorkerStep();
    DeliverPending();
    CHECK(p.m_delegate->GetTransportState() == "STOPPED");
    p.WorkerStep();
    CHECK(p.CloseFile());
    CHECK(cb.stopped == 1 && cb.ended == 0);
  }
  Begin("the item ends before the close");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    p.m_delegate->nextState = "STOPPED";
    p.m_delegate->pollDue = true;
    p.WorkerStep();
    DeliverPending();
    p.WorkerStep();
    CHECK(cb.ended == 1);
    p.WorkerStep();
    Close(p);
    CHECK(cb.ended == 1 && cb.stopped == 0);
  }
}

// The destructor of a player in a close stops the worker before the members end.
void destroy_during_close() {
  Begin("destroy a player in a close");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    CHECK(!p.CloseFile());
  }
  CHECK(Count("P:Stop") == 1);
  while (!g_ctl.pending.empty())
    DeliverPending();  // the answer reaches the command, not the destroyed player
}

// The wait for PLAYING reads the answer of the command that the wait sent, not
// the state that the worker polled.
void transitioning_renderer() {
  Begin("renderer reports TRANSITIONING before PLAYING");
  IPlayerCallback cb;
  CUPnPPlayer p(cb, "P");
  p.m_delegate->transitioning = 1;
  p.m_delegate->m_trainfo.cur_transport_state = "STOPPED";
  p.m_delegate->m_trainfo.cur_transport_status = "ERROR_OCCURED";
  CHECK(p.OpenFile(Item(), CPlayerOptions()));
  CHECK(cb.started == 1);
  ExpectLog(Of("P", {"GetTransportInfo", "SetAVTransportURI", "Play", "GetTransportInfo",
                     "GetTransportInfo", "GetPositionInfo", "GetMediaInfo"}));
}

// A command lives until the renderer answers the command, also after the wait
// for the answer ended: the address of the command stays registered, so no
// newer command takes the address while the answer can arrive.
void abandoned_command() {
  Begin("command abandoned by a close");
  IPlayerCallback cb;
  CUPnPPlayer p(cb, "P");
  void* abandoned = nullptr;
  g_waitHook = [&](int w) {
    if (w != 2)  // the wait for the answer to SetAVTransportURI
      return;
    g_waitHook = nullptr;
    g_ctl.pending.back().delay = 2;
    abandoned = g_ctl.pending.back().userdata;
    CHECK(!p.CloseFile());  // the close waits for the answer
  };
  CHECK(!p.OpenFile(Item(), CPlayerOptions()));
  CHECK(abandoned && Registered(abandoned));
  CHECK(!g_ctl.pending.empty());  // the answer has yet to arrive
  while (!g_ctl.pending.empty())
    DeliverPending();
  CHECK(Registered(abandoned) && p.CloseFile());
  // a newer command frees the answered command
  CUPnPPlayer q(cb, "Q");
  CHECK(q.OpenFile(Item(), CPlayerOptions()));
  CHECK(!Registered(abandoned));
}

// A command that the controller did not send has no answer to come: a newer
// command frees the command.
void failed_send() {
  for (const char* command : {"GetTransportInfo", "Stop"}) {
    Begin(std::string("controller does not send ") + command);
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    g_ctl.failedUserdata = nullptr;
    if (std::string(command) == "Stop") {
      CHECK(p.OpenFile(Item(), CPlayerOptions()));
      g_ctl.sendFails = command;
      CHECK(p.CloseFile());  // nothing to wait for
      CHECK(cb.stopped == 1);
    } else {
      g_ctl.sendFails = command;
      CHECK(!p.OpenFile(Item(), CPlayerOptions()));
    }
    void* const failed = g_ctl.failedUserdata;
    CHECK(failed && Registered(failed));
    g_unregistered.clear();
    CUPnPPlayer q(cb, "Q");
    CHECK(q.OpenFile(Item(), CPlayerOptions()));
    CHECK(std::find(g_unregistered.begin(), g_unregistered.end(), failed) != g_unregistered.end());
  }
}

// A close ends the wait of an open, also when the renderer never answers the
// command of the wait.
void close_unanswered_wait() {
  Begin("close during a wait without an answer");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    p.m_delegate->playUnanswered = true;
    g_waitHook = [&](int w) {
      if (w != 3)  // the wait for the answer to Play
        return;
      g_waitHook = nullptr;
      CHECK(!p.CloseFile());
    };
    CHECK(!p.OpenFile(Item(), CPlayerOptions()));
    CHECK(cb.Total() == 0 && p.creates == 0);
    Close(p);
  }
  auto want = Of("P", Head(NORMAL, 3));
  want.push_back("P:Stop");
  ExpectLog(want);
}

void close_during_last_command() {
  // The last command of an open with a start time is Seek, without a wait after
  // the command: a close on another thread after the last wait ends the open.
  Begin("close while the last command is sent");
  IPlayerCallback cb;
  {
    CUPnPPlayer p(cb, "P");
    g_ctl.cmdHook = [&](const std::string& c) {
      if (c == "P:Seek") {
        g_ctl.cmdHook = nullptr;
        CHECK(!p.CloseFile());
      }
    };
    CPlayerOptions options;
    options.starttime = 10;
    CHECK(!p.OpenFile(Item(), options));
    CHECK(cb.Total() == 0 && p.creates == 0 && !p.running);
    Close(p);
  }
  auto want = Of("P", Head(NORMAL, 4));
  want.push_back("P:Seek");
  want.push_back("P:Stop");
  ExpectLog(want);
}

void attach() {
  Begin("attach to playing renderer");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    p.m_delegate->firstState = "PLAYING";
    CHECK(p.OpenFile(Item(""), CPlayerOptions()));
    CHECK(cb.started == 1 && cb.avstarted == 1 && p.creates == 1);
    ExpectLog(Of("P", {"GetTransportInfo", "GetPositionInfo", "GetMediaInfo"}));
    Close(p);
    CHECK(Count("P:Stop") == 1);
  }
  CHECK(Count("P:Stop") == 1);

  Begin("attach to stopped renderer");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(!p.OpenFile(Item(""), CPlayerOptions()));
    CHECK(cb.Total() == 0 && p.creates == 0);
  }
  ExpectLog(Of("P", {"GetTransportInfo"}));

  Begin("attach, close inside wait");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    p.m_delegate->firstState = "PLAYING";
    g_waitHook = [&](int) {
      g_waitHook = nullptr;
      CHECK(p.CloseFile());
    };
    CHECK(!p.OpenFile(Item(""), CPlayerOptions()));
    CHECK(cb.Total() == 0 && p.creates == 0);
  }
  ExpectLog(Of("P", {"GetTransportInfo"}));
}

// The close ends the item: a control after the start of the close sends the
// renderer nothing and reports no event. A resume after the Stop would start the
// stopped renderer again. A control after the end of the item does the same.
void control_after_close() {
  for (bool ended : {false, true}) {
    Begin(ended ? "control after the end of the item" : "control after the close starts");
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    if (ended) {
      p.m_delegate->nextState = "STOPPED";
      p.m_delegate->pollDue = true;
      p.WorkerStep();
      DeliverPending();
      p.WorkerStep();
      CHECK(cb.ended == 1);
    } else {
      CHECK(!p.CloseFile());
      CHECK(Count("P:Stop") == 1);
    }
    const auto sent = g_ctl.log.size();
    p.m_delegate->m_trainfo.cur_transport_state = "PAUSED_PLAYBACK";
    p.SetSpeed(1.0f);
    p.Pause();
    p.m_delegate->m_trainfo.cur_transport_state = "PLAYING";
    p.Pause();
    p.SeekTime(42000);
    p.SetVolume(0.5f);
    CHECK(g_ctl.log.size() == sent && cb.paused == 0 && cb.resumed == 0);
    Close(p);
    CHECK(Count("P:Play") == 1 && Count("P:Stop") == 1);
  }
}

// The renderer can receive the commands in any order: the close stops the
// renderer after the answers of the sent controls. No control arrives after the
// Stop, or reaches the item of a newer player on the renderer.
void control_before_close() {
  Begin("controls sent before the close");
  IPlayerCallback cbA;
  auto a = std::make_unique<CUPnPPlayer>(cbA, "A");
  CHECK(a->OpenFile(Item(), CPlayerOptions()));
  a->m_delegate->controlDelay = 2;
  a->m_delegate->m_trainfo.cur_transport_state = "PAUSED_PLAYBACK";
  a->SetSpeed(1.0f);
  a->m_delegate->m_trainfo.cur_transport_state = "PLAYING";
  a->Pause();
  a->SeekTime(42000);
  a->SetVolume(0.5f);
  CHECK(Count("A:Play") == 2 && Count("A:Pause") == 1 && Count("A:Seek") == 1 &&
        Count("A:SetVolume") == 1);
  // the Stop goes after the answers of the controls: only the answer of the Stop is due
  g_ctl.cmdHook = [](const std::string& c) {
    if (c == "A:Stop")
      CHECK(g_ctl.pending.size() == 1);
  };
  CHECK(!a->CloseFile());
  CHECK(Count("A:Stop") == 0 && cbA.stopped == 1);
  CHECK(Close(*a) > 3 && Count("A:Stop") == 1);
  g_ctl.cmdHook = nullptr;
  CHECK(cbA.events == "SARPT");
}

// The commands of an open change the renderer too: a close during the open
// completes after the answers of the sent commands. A late Play would start the
// renderer after the Stop of the close; a late Stop or URI would reach the item
// of the newer player. After the close starts, each command goes out with only
// the answer of that command to come.
void open_commands_before_close() {
  const std::vector<std::string> flow = {"GetTransportInfo", "Stop", "SetAVTransportURI", "Play"};
  for (size_t n = 2; n <= 4; ++n) {
    Begin("delayed answer of " + flow[n - 1] + ", close and newer player");
    IPlayerCallback cbA, cbB;
    auto a = std::make_unique<CUPnPPlayer>(cbA, "A");
    auto b = std::make_unique<CUPnPPlayer>(cbB, "B");
    a->m_delegate->firstState = "PLAYING";
    void* delayed = nullptr;
    bool closing = false;
    g_ctl.cmdHook = [&](const std::string& c) {
      if (closing)
        CHECK(std::none_of(g_ctl.pending.begin(), g_ctl.pending.end(),
                           [&](const auto& e) { return e.userdata == delayed; }));
      else if (c == "A:" + flow[n - 1] && !delayed) {
        g_ctl.pending.back().delay = 3;
        delayed = g_ctl.pending.back().userdata;
      }
    };
    g_waitHook = [&](int w) {
      if (w != int(n))
        return;
      g_waitHook = nullptr;
      closing = true;
      Close(*a);
      CHECK(b->OpenFile(Item(), CPlayerOptions()));
    };
    CHECK(!a->OpenFile(Item(), CPlayerOptions()));
    g_ctl.cmdHook = nullptr;
    CHECK(delayed && cbA.Total() == 0 && cbB.started == 1);
    CHECK(Count("A:Stop") == (n >= 4 ? 2 : 1));
  }
}

// A renderer that never answers a control does not keep the close: the close
// stops the renderer at the deadline of the control. The destructor cannot wait,
// and stops the renderer at once.
void unanswered_control() {
  Begin("renderer never answers a control");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    p.m_delegate->controlUnanswered = true;
    p.SeekTime(42000);
    CHECK(!p.CloseFile() && Count("P:Stop") == 0);
    CHECK(Close(p) > 2 && Count("P:Stop") == 1 && cb.stopped == 1);
  }
  Begin("destroy a player that waits for a control");
  {
    IPlayerCallback cb;
    CUPnPPlayer p(cb, "P");
    CHECK(p.OpenFile(Item(), CPlayerOptions()));
    p.m_delegate->controlUnanswered = true;
    p.SeekTime(42000);
  }
  CHECK(Count("P:Stop") == 1);
}

// The start, the controls, the events and the end of the item hold the item
// section, also the close: a close on another thread cannot end the item between
// the check of a control and the send, or between the check of an event and the
// event.
bool SectionFree(CUPnPPlayer& p) {
  bool free = false;
  std::thread([&] {
    free = p.m_itemSection.try_lock();
    if (free)
      p.m_itemSection.unlock();
  }).join();
  return free;
}
void item_section() {
  Begin("the item section covers the item");
  IPlayerCallback cb;
  auto p = std::make_unique<CUPnPPlayer>(cb, "P");
  int held = 0;
  auto check = [&] {
    CHECK(!SectionFree(*p));
    ++held;
  };
  cb.hook = check;
  CHECK(p->OpenFile(Item(), CPlayerOptions()));
  CHECK(held == 2);  // the start
  g_ctl.cmdHook = [&](const std::string&) { check(); };
  p->m_delegate->m_trainfo.cur_transport_state = "PAUSED_PLAYBACK";
  p->SetSpeed(1.0f);
  p->m_delegate->m_trainfo.cur_transport_state = "PLAYING";
  p->Pause();
  p->SeekTime(42000);
  p->SetVolume(0.5f);
  CHECK(held == 8);  // four controls and two events
  g_ctl.cmdHook = nullptr;
  // the worker sees the renderer pause and resume, then stop
  CDataCacheCore::GetInstance().speed = 1.0f;
  p->m_delegate->m_trainfo.cur_transport_state = "PAUSED_PLAYBACK";
  p->WorkerStep();
  p->m_delegate->m_trainfo.cur_transport_state = "PLAYING";
  p->WorkerStep();
  p->m_delegate->m_trainfo.cur_transport_state = "STOPPED";
  p->WorkerStep();
  CHECK(held == 11 && cb.events == "SARPPRE");
  Close(*p);
  // the close and the exit of the worker
  p = std::make_unique<CUPnPPlayer>(cb, "Q");
  CHECK(p->OpenFile(Item(), CPlayerOptions()));
  CHECK(!p->CloseFile());
  CHECK(held == 14);
  Close(*p);
  p = std::make_unique<CUPnPPlayer>(cb, "R");
  CHECK(p->OpenFile(Item(), CPlayerOptions()));
  p->OnExit();
  CHECK(held == 17 && cb.events == "SARPPRESATSAE");
  Close(*p);
}

// A renderer can disappear before the creation of the player: the player has no
// renderer. The open fails, also the attach to a playing renderer, and the close
// completes at once, so a newer item and a shutdown do not wait for the player.
void missing_renderer() {
  for (const char* path : {"http://server/a.mkv", ""}) {
    Begin("a player without a renderer");
    IPlayerCallback cb;
    {
      CUPnPPlayer p(cb, "P");
      CUPnP::UnregisterUserdata(p.m_delegate.get());
      p.m_delegate.reset();
      CHECK(!p.OpenFile(Item(path), CPlayerOptions()));
      CHECK(Close(p) == 1);
      ExpectLog({});
    }
    CHECK(cb.started == 0 && cb.stopped == 0 && cb.ended == 0);
  }
}

// The application retires a player after the close completes and creates a newer
// player. A delayed destructor of the retired player, for example after the open
// of the retired player returns, keeps the cache of the newer player.
void destroy_after_close() {
  for (bool renderer : {true, false}) {
    Begin("the destructor of a closed player keeps the cache");
    IPlayerCallback cb;
    {
      CUPnPPlayer p(cb, "P");
      if (renderer)
        CHECK(p.OpenFile(Item(), CPlayerOptions()));
      else {
        CUPnP::UnregisterUserdata(p.m_delegate.get());
        p.m_delegate.reset();
      }
      Close(p);
      CDataCacheCore::GetInstance().speed = 0.0f;  // the newer player pauses
      CHECK(p.CloseFile());
    }
    CHECK(CDataCacheCore::GetInstance().speed == 0.0f);
    CDataCacheCore::GetInstance().speed = 1.0f;
  }
}

#define CASES(X) X(missing_renderer) X(destroy_after_close) X(normal_open_close) X(close_twice) X(close_without_wait) X(close_inside_wait) \
  X(close_inside_wait_renderer_playing) X(newer_player_on_same_renderer) X(refused_stop) \
  X(unanswered_stop) X(one_end_notification) X(destroy_during_close) X(transitioning_renderer) \
  X(abandoned_command) X(failed_send) X(close_unanswered_wait) X(close_during_last_command) \
  X(attach) X(control_after_close) X(control_before_close) X(open_commands_before_close) \
  X(unanswered_control) X(item_section)
int main(int argc, char** argv) {
#define ENTRY(name) {#name, name},
  const std::vector<std::pair<const char*, void (*)()>> cases = {CASES(ENTRY)};
  if (argc > 1) {
    for (auto& c : cases)
      if (std::string(c.first) == argv[1]) {
        c.second();
        return 0;
      }
    return 2;
  }
  for (auto& c : cases)
    c.second();
  std::puts("ok");
}
'''

# Each control removes one new check from the production text. A rig that still
# passes would not prove the check.
CONTROLS = [
    ('a close without a renderer never completes',
     '  if (!m_delegate)\n  {\n    CServiceBroker::GetDataCacheCore().Reset();\n    m_closed = true;\n    return true;\n  }\n',
     '  if (!m_delegate)\n    return false;\n'),
    ('a repeated close resets the cache of a newer player', '  if (m_closed)\n    return true;\n', ''),
    ('a completed close without a renderer is not recorded',
     '    CServiceBroker::GetDataCacheCore().Reset();\n    m_closed = true;\n    return true;\n  }\n',
     '    CServiceBroker::GetDataCacheCore().Reset();\n    return true;\n  }\n'),
    ('a completed close is not recorded',
     '  StopThread(true);\n  CServiceBroker::GetDataCacheCore().Reset();\n  m_closed = true;\n',
     '  StopThread(true);\n  CServiceBroker::GetDataCacheCore().Reset();\n'),
    ('the open without a renderer uses the renderer',
     '  // the renderer disappeared before the creation of the player\n  NPT_CHECK_POINTER_LABEL_SEVERE(m_delegate, failed);\n', ''),
    ('wait does not check the open sequence',
     'if (open != m_openSequence)\n  {\n    m_logger->debug', 'if (false)\n  {\n    m_logger->debug'),
    ('Play does not mark the renderer as started',
     '    m_stopremote = true;\n    NPT_CHECK_LABEL_SEVERE(WaitOnEvent', '    NPT_CHECK_LABEL_SEVERE(WaitOnEvent'),
    ('OpenFile does not check the open sequence before starting',
     'if (open != m_openSequence)\n      goto failed;', 'if (false)\n      goto failed;'),
    ('CloseFile does not end the open',
     '  CancelOpen();\n  // a player without a renderer', '  // a player without a renderer'),
    ('a close does not end the waits of the open',
     '    command->m_event.Set();\n', '    (void)command;\n'),
    ('all commands share one answer',
     'CEvent m_event;\n  NPT_Result m_status = NPT_FAILURE;',
     'static inline CEvent m_event;\n  static inline NPT_Result m_status = NPT_FAILURE;'),
    ('a command is freed before the answer arrives',
     'return commands->emplace_back(std::make_shared<CUPnPCommand>());',
     'return std::make_shared<CUPnPCommand>();'),
    ('a command that was not sent is never freed',
     '    command.m_answered = true;\n', '    (void)command;\n'),
    ('the open does not set the state of the renderer',
     '    m_delegate->SetTransportInfo(info->m_trainfo);\n\n    const NPT_String& transportState',
     '\n    const NPT_String& transportState'),
    ('the close stops the renderer in each frame',
     'if (m_stopremote.exchange(false))', 'if (m_stopremote)'),
    ('the close completes before the answer of Stop',
     '    if (!m_stop->m_answered && !m_stopTimeout.IsTimePast())\n      return false;\n', ''),
    ('the close has no deadline',
     'if (!m_stop->m_answered && !m_stopTimeout.IsTimePast())', 'if (!m_stop->m_answered)'),
    ('the close does not end the item',
     'if (m_started.exchange(false))\n      m_callback.OnPlayBackStopped();',
     'if (m_started)\n      m_callback.OnPlayBackStopped();'),
    ('the worker reports the end twice',
     '        if (m_started.exchange(false))\n        {\n          m_logger->info(',
     '        if (m_started)\n        {\n          m_logger->info('),
    ('the destructor does not stop the worker',
     '  CloseFile();\n  // the close can wait for the renderer: the worker uses the members\n  StopThread(true);',
     '  CloseFile();'),
    ('a resume after the close reaches the renderer',
     '  if (!m_started)\n    return;\n  if (IsPaused() && speed == 1.0f)', '  if (IsPaused() && speed == 1.0f)'),
    ('a pause after the close reaches the renderer',
     '  if (!m_started)\n    return;\n  if (IsPaused())\n', '  if (IsPaused())\n'),
    ('a seek after the close reaches the renderer',
     '  if (!m_started)\n    return;\n  if (!SendCommand(', '  if (!SendCommand('),
    ('a volume change after the close reaches the renderer',
     '    if (!m_started)\n      return;\n    NPT_CHECK_POINTER_LABEL_SEVERE', '    NPT_CHECK_POINTER_LABEL_SEVERE'),
    ('the close stops the renderer before the answers of the commands',
     '      if (!m_commandTimeout.IsTimePast())\n        return false;\n', ''),
    ('the close waits for an unanswered command without a deadline',
     'if (!m_commandTimeout.IsTimePast())', 'if (true)'),
    ('the close does not wait for a command',
     '  m_commands.emplace_back(command);\n', ''),
    ('a command has no deadline', '  m_commandTimeout.Set(10s);\n', ''),
    ('the open sends Play without the close waiting for the answer',
     '    const auto play = SendCommand([this](void* command) {\n      return m_control->Play(',
     '    const auto play = NewCommand();\n    Sent(*play, m_control->Play(m_delegate->m_device, m_delegate->m_instance, "1", play.get()));\n    if (false) SendCommand([this](void* command) {\n      return m_control->Play('),
    ('the destructor waits for the commands',
     '    std::unique_lock<CCriticalSection> lock(m_itemSection);\n    m_commands.clear();\n  }\n  CloseFile();',
     '  }\n  CloseFile();'),
    ('the close ends the item outside the item section',
     '    std::unique_lock<CCriticalSection> lock(m_itemSection);\n    // the close ends the item', '    // the close ends the item'),
    ('the start of the item is outside the item section',
     '    std::unique_lock<CCriticalSection> lock(m_itemSection);\n    if (open != m_openSequence)', '    if (open != m_openSequence)'),
    ('a resume is outside the item section',
     'void CUPnPPlayer::SetSpeed(float speed)\n{\n  std::unique_lock<CCriticalSection> lock(m_itemSection);\n',
     'void CUPnPPlayer::SetSpeed(float speed)\n{\n'),
    ('a pause event of the worker is outside the item section',
     '      {\n        std::unique_lock<CCriticalSection> lock(m_itemSection);\n        if (m_started && paused)',
     '      {\n        if (m_started && paused)'),
    ('the end of the item by the worker is outside the item section',
     '        std::unique_lock<CCriticalSection> lock(m_itemSection);\n        if (m_started.exchange(false))',
     '        if (m_started.exchange(false))'),
    ('the exit of the worker is outside the item section',
     'void CUPnPPlayer::OnExit()\n{\n  std::unique_lock<CCriticalSection> lock(m_itemSection);\n',
     'void CUPnPPlayer::OnExit()\n{\n'),
]


def production():
    source = (ROOT / 'xbmc/network/upnp/UPnPPlayer.cpp').read_text()
    header = (ROOT / 'xbmc/network/upnp/UPnPPlayer.h').read_text()
    command = source.index('class CUPnPCommand : public PLT_MediaControllerDelegate')
    command = source[command:source.index('\n};\n', command) + 4]
    command += function(source, 'static std::shared_ptr<CUPnPCommand> NewCommand()')
    command += function(source, 'static NPT_Result Sent(')
    signatures = ['void CUPnPPlayer::CancelOpen()', 'int CUPnPPlayer::WaitOnEvent(', 'std::shared_ptr<CUPnPCommand> CUPnPPlayer::SendCommand(',
                  'int CUPnPPlayer::PlayFile(', 'bool CUPnPPlayer::OpenFile(', 'bool CUPnPPlayer::CloseFile(',
                  'CUPnPPlayer::~CUPnPPlayer()', 'void CUPnPPlayer::Pause()', 'void CUPnPPlayer::SeekTime(',
                  'void CUPnPPlayer::SetSpeed(', 'void CUPnPPlayer::SetVolume(', 'bool CUPnPPlayer::IsPaused() const',
                  'void CUPnPPlayer::OnExit()']
    body = '\n'.join(function(source, signature) for signature in signatures)
    # The worker: one pass of the Process loop.
    process = function(source, 'void CUPnPPlayer::Process()')
    poll = '    m_delegate->UpdatePositionInfo();\n'
    assert poll in process
    worker = poll + function(process, '    if (m_started)')
    body += '\nvoid CUPnPPlayer::WorkerStep()\n{\n' + worker + '\n}\n'
    code = PRELUDE.replace('@PRODUCTION@', 'namespace UPNP {\n' + command + body + '\n}  // namespace UPNP')
    return code, header


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code, header = production()
    run(code)
    # The header must declare the state the production methods use.
    assert 'std::atomic<uint64_t> m_openSequence' in header, 'm_openSequence must be atomic (CloseFile runs on another thread)'
    assert 'std::atomic<bool> m_stopremote' in header, 'm_stopremote must be atomic'
    assert 'std::atomic<bool> m_closed' in header, 'm_closed must be atomic (the worker calls CloseFile)'
    assert 'std::atomic<bool> m_started' in header, 'm_started must be atomic (the worker thread reads the flag)'
    # Platinum calls the router (CMediaController); the router forwards an answer
    # to a registered userdata. Every answer of a command needs a forwarder.
    source = (ROOT / 'xbmc/network/upnp/UPnPPlayer.cpp').read_text()
    command = source[source.index('class CUPnPCommand'):]
    command = command[:command.index('\n};\n')]
    router = (ROOT / 'xbmc/network/upnp/UPnP.cpp').read_text()
    router = router[router.index('class CMediaController'):]
    for name in sorted(set(re.findall(r'void (On\w+Result)\(', command))):
        assert 'void ' + name + '(' in router, 'the router does not forward ' + name
        forward = router[router.index('void ' + name + '('):]
        forward = forward[:forward.index('\n  }\n')]
        assert 'CHECK_USERDATA_RETURN(userdata);' in forward and '->' + name + '(' in forward, \
            'the router does not forward ' + name
    assert 'int WaitOnEvent(CUPnPCommand& command, XbmcThreads::EndTime<>& timeout, uint64_t open);' in header
    if args.negative_controls:
        for label, before, after in CONTROLS:
            assert before in code, label
            run(code.replace(before, after, 1), True, label)
    print('UPnP open/close: PASS (production methods; ASan/UBSan; recording renderer)')


CASES = re.findall(r'X\((\w+)\)', PRELUDE[PRELUDE.index('#define CASES(X)'):
                                             PRELUDE.index('int main(')])


def run(code, expect_failure=False, label=''):
    with tempfile.TemporaryDirectory(prefix='upnp-open-close-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        failures = {}
        for name in CASES:
            result = subprocess.run([str(out / 'test'), name], capture_output=True, text=True,
                                    timeout=20)
            if result.returncode != 0:
                # a check of the case or a sanitizer report
                assert any(sign in result.stderr for sign in ('Assertion', 'runtime error:', 'AddressSanitizer')), \
                    result.stderr
                failures[name] = result.stderr.strip().splitlines()[-1]
        if expect_failure:
            assert failures, 'negative control passed: ' + label
            print('Negative control rejected at runtime:', label)
        else:
            print('case results:', 'PASS' if not failures else 'FAIL')
        for name, message in failures.items():
            print('  FAIL', name, '->', message)
        assert expect_failure or not failures


if __name__ == '__main__':
    main()
