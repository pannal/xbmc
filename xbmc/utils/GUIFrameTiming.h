/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <cstdint>

#ifdef HAS_LIBAMCODEC
#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <time.h>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

// Opt-in observation only. Durations are inclusive, main-thread wall/CPU time;
// neither these regions nor nested/modal invocations may be added as disjoint work.
namespace GUI_FRAME_TIMING
{
enum class Region : uint32_t
{
  Process,
  FrameMove,
  Render,
  WindowProcess,
  AssignDepth,
  PublishBounds,
  RenderPass,
  EGLSwap,
  Count
};
enum class Counter : uint32_t
{
  FontLookup,
  FontHit,
  FontMiss,
  Count
};
constexpr uint32_t REGION_COUNT = static_cast<uint32_t>(Region::Count);
constexpr uint32_t COUNTER_COUNT = static_cast<uint32_t>(Counter::Count);
constexpr uint32_t CAPACITY = 32768;
enum Flag : uint32_t
{
  Full = 1,
  ClockFailure = 2,
  Unbalanced = 4,
  NestedFrame = 8,
  NestedRegion = 16
};
struct Header
{
  char magic[8];
  uint32_t version, headerBytes, recordBytes, capacity;
  uint32_t regionCount, counterCount, pid, tid;
  uint32_t completed, flags;
  uint64_t reserved[2];
};
struct Duration
{
  uint64_t calls, wallNs, cpuNs;
};
struct Record
{
  uint64_t frame, beginWallNs, beginCpuNs, endWallNs, endCpuNs;
  uint32_t flags, nestedFrames;
  Duration regions[REGION_COUNT];
  uint64_t counters[COUNTER_COUNT];
};
static_assert(sizeof(Header) == 64 && sizeof(Duration) == 24 && sizeof(Record) == 264);
enum class InitResult
{
  Disabled,
  Started,
  Rejected,
  Failed
};

#ifdef HAS_LIBAMCODEC
namespace detail
{
struct PreserveErrno
{
  const int saved = errno;
  ~PreserveErrno() { errno = saved; }
};
inline std::atomic<bool> enabled{false};
inline std::atomic_flag initialized = ATOMIC_FLAG_INIT;
inline thread_local bool owner = false;
inline Header* header = nullptr;
inline Record* records = nullptr;
inline Record* current = nullptr;
inline uint32_t frameDepth = 0;
inline uint32_t regionDepth[REGION_COUNT]{};
constexpr size_t FILE_BYTES = sizeof(Header) + sizeof(Record) * CAPACITY;
inline bool Active()
{
  return enabled.load(std::memory_order_relaxed) && owner;
}
inline void Fail(uint32_t flag)
{
  header->flags |= flag;
  if (current)
    current->flags |= flag;
  current = nullptr;
  enabled.store(false, std::memory_order_relaxed);
}
inline bool Now(uint64_t& wall, uint64_t& cpu)
{
  timespec w{}, c{};
  if (clock_gettime(CLOCK_MONOTONIC, &w) != 0 || clock_gettime(CLOCK_THREAD_CPUTIME_ID, &c) != 0)
  {
    Fail(ClockFailure);
    return false;
  }
  wall = static_cast<uint64_t>(w.tv_sec) * 1000000000 + static_cast<uint64_t>(w.tv_nsec);
  cpu = static_cast<uint64_t>(c.tv_sec) * 1000000000 + static_cast<uint64_t>(c.tv_nsec);
  return true;
}
inline InitResult Initialize()
{
  const PreserveErrno saved;
  if (initialized.test_and_set(std::memory_order_relaxed))
    return InitResult::Rejected;
  const char* path = std::getenv("P3I_GUI_TIMING_PATH");
  if (!path || !*path)
    return InitResult::Disabled;
  if (*path != '/')
    return InitResult::Failed;
  const int fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0)
    return InitResult::Failed;
  // Reserve backing storage before touching the mapping, including on sparse
  // filesystems. Unsupported allocation fails closed; never overwrite a file.
  if (fchmod(fd, 0600) != 0 || posix_fallocate(fd, 0, FILE_BYTES) != 0)
  {
    close(fd);
    return InitResult::Failed;
  }
  void* mapping = mmap(nullptr, FILE_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  if (mapping == MAP_FAILED)
    return InitResult::Failed;
  // Pre-touch the bounded storage at startup. Capture performs no explicit file
  // I/O, allocations or synchronization with GPU/scanout; kernel writeback remains.
  std::memset(mapping, 0, FILE_BYTES);
  header = static_cast<Header*>(mapping);
  records = reinterpret_cast<Record*>(header + 1);
  std::memcpy(header->magic, "P3IGUI1", 8);
  header->version = 1;
  header->headerBytes = sizeof(Header);
  header->recordBytes = sizeof(Record);
  header->capacity = CAPACITY;
  header->regionCount = REGION_COUNT;
  header->counterCount = COUNTER_COUNT;
  header->pid = static_cast<uint32_t>(getpid());
  header->tid = static_cast<uint32_t>(syscall(SYS_gettid));
  owner = true;
  enabled.store(true, std::memory_order_relaxed);
  return InitResult::Started;
}
inline void Shutdown()
{
  if (!owner)
    return;
  const PreserveErrno saved;
  if (current)
    Fail(Unbalanced);
  enabled.store(false, std::memory_order_relaxed);
  owner = false;
  munmap(header, FILE_BYTES);
  header = nullptr;
  records = current = nullptr;
}
} // namespace detail
#endif

// Construct once from CApplication::Run. No lazy initialization from workers.
class Session
{
public:
#ifdef HAS_LIBAMCODEC
  Session() : m_result(detail::Initialize()) {}
  ~Session()
  {
    if (m_result == InitResult::Started)
      detail::Shutdown();
  }
  InitResult Result() const { return m_result; }

private:
  InitResult m_result;
#else
  Session() = default;
  ~Session() {}
  InitResult Result() const { return InitResult::Disabled; }
#endif
  Session(const Session&) = delete;
  Session& operator=(const Session&) = delete;
};

class Frame
{
public:
  Frame()
  {
#ifdef HAS_LIBAMCODEC
    if (!detail::Active())
      return;
    const detail::PreserveErrno saved;
    if (detail::current)
    {
      ++detail::frameDepth;
      ++detail::current->nestedFrames;
      detail::current->flags |= NestedFrame;
      m_active = true;
      return;
    }
    if (detail::header->completed >= CAPACITY)
    {
      detail::Fail(Full);
      return;
    }
    auto* record = &detail::records[detail::header->completed];
    if (!detail::Now(record->beginWallNs, record->beginCpuNs))
      return;
    record->frame = static_cast<uint64_t>(detail::header->completed) + 1;
    detail::current = record;
    detail::frameDepth = 1;
    m_active = true;
#endif
  }
  ~Frame()
  {
#ifdef HAS_LIBAMCODEC
    if (!m_active || !detail::Active() || !detail::current)
      return;
    const detail::PreserveErrno saved;
    if (detail::frameDepth > 1)
    {
      --detail::frameDepth;
      return;
    }
    for (auto depth : detail::regionDepth)
      if (depth != 0)
      {
        detail::Fail(Unbalanced);
        return;
      }
    auto* record = detail::current;
    if (!detail::Now(record->endWallNs, record->endCpuNs))
      return;
    if (record->endWallNs < record->beginWallNs || record->endCpuNs < record->beginCpuNs)
    {
      detail::Fail(ClockFailure);
      return;
    }
    detail::current = nullptr;
    detail::frameDepth = 0;
    // Aligned 32-bit publication is lock-free on the target. The completed
    // prefix is immutable; unfinished records must never be interpreted.
    static_assert(__atomic_always_lock_free(sizeof(uint32_t), nullptr));
    const uint32_t count = detail::header->completed + 1;
    __atomic_store_n(&detail::header->completed, count, __ATOMIC_RELEASE);
    if (count == CAPACITY)
    {
      detail::header->flags |= Full;
      detail::enabled.store(false, std::memory_order_relaxed);
    }
#endif
  }
  Frame(const Frame&) = delete;
  Frame& operator=(const Frame&) = delete;

private:
#ifdef HAS_LIBAMCODEC
  bool m_active = false;
#endif
};

class Scope
{
public:
  explicit Scope(Region region)
  {
#ifdef HAS_LIBAMCODEC
    if (!detail::Active() || !detail::current)
      return;
    const detail::PreserveErrno saved;
    m_region = static_cast<uint32_t>(region);
    if (m_region >= REGION_COUNT || !detail::Now(m_wall, m_cpu))
      return;
    m_record = detail::current;
    ++m_record->regions[m_region].calls;
    if (detail::regionDepth[m_region]++ != 0)
      m_record->flags |= NestedRegion;
#else
    (void)region;
#endif
  }
  ~Scope()
  {
#ifdef HAS_LIBAMCODEC
    if (!m_record || !detail::Active() || detail::current != m_record)
      return;
    const detail::PreserveErrno saved;
    uint64_t wall = 0, cpu = 0;
    if (!detail::Now(wall, cpu))
      return;
    if (wall < m_wall || cpu < m_cpu)
    {
      detail::Fail(ClockFailure);
      return;
    }
    auto& duration = m_record->regions[m_region];
    duration.wallNs += wall - m_wall;
    duration.cpuNs += cpu - m_cpu;
    --detail::regionDepth[m_region];
#endif
  }
  Scope(const Scope&) = delete;
  Scope& operator=(const Scope&) = delete;

private:
#ifdef HAS_LIBAMCODEC
  Record* m_record = nullptr;
  uint32_t m_region = 0;
  uint64_t m_wall = 0, m_cpu = 0;
#endif
};

inline void Count(Counter counter)
{
#ifdef HAS_LIBAMCODEC
  if (detail::Active() && detail::current && static_cast<uint32_t>(counter) < COUNTER_COUNT)
    ++detail::current->counters[static_cast<uint32_t>(counter)];
#else
  (void)counter;
#endif
}
} // namespace GUI_FRAME_TIMING
