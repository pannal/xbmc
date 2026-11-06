/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "PerformanceCores.h"

#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"

#include <atomic>
#include <cerrno>
#include <fstream>
#include <mutex>
#include <string>

#if defined(TARGET_LINUX)
#include <sched.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
#include "ServiceBroker.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"

namespace
{
std::mutex s_playbackThreadsMutex;
std::set<pid_t> s_playbackThreads;
struct PlaybackThread
{
  const pid_t tid = static_cast<pid_t>(syscall(SYS_gettid));
  PlaybackThread()
  {
    std::lock_guard<std::mutex> lock(s_playbackThreadsMutex);
    s_playbackThreads.insert(tid);
  }
  ~PlaybackThread()
  {
    std::lock_guard<std::mutex> lock(s_playbackThreadsMutex);
    s_playbackThreads.erase(tid);
  }
};
bool ReadCpus(pid_t tid, std::set<int>& cpus)
{
  cpu_set_t mask;
  CPU_ZERO(&mask);
  if (sched_getaffinity(tid, sizeof(mask), &mask) != 0)
    return false;
  for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
    if (CPU_ISSET(cpu, &mask))
      cpus.insert(cpu);
  return true;
}
std::string CpuList(const std::set<int>& cpus)
{
  std::string text;
  for (int cpu : cpus)
    text += (text.empty() ? "" : ",") + std::to_string(cpu);
  return text;
}
bool SetCpus(const std::set<int>& cpus)
{
  cpu_set_t mask;
  CPU_ZERO(&mask);
  for (int cpu : cpus)
    CPU_SET(cpu, &mask);
  return sched_setaffinity(0, sizeof(mask), &mask) == 0;
}

// Pinned render thread and Kodi's CPUs from just before it was pinned. The
// main thread's live mask then no longer describes the CPUs Kodi may use.
const char* s_cpuRoot = "/sys/devices/system/cpu";
std::atomic<pid_t> s_renderThread{0};
std::mutex s_renderMutex;
std::set<int> s_processCpus;
std::set<int> ProcessCpus()
{
  std::lock_guard<std::mutex> lock(s_renderMutex);
  return s_processCpus;
}
uint64_t MaxFrequency(int cpu)
{
  std::ifstream file(std::string(s_cpuRoot) + "/cpu" + std::to_string(cpu) +
                     "/cpufreq/cpuinfo_max_freq");
  uint64_t value = 0;
  return file >> value ? value : 0;
}
bool SetCpusOf(pid_t tid, const std::set<int>& cpus)
{
  cpu_set_t mask;
  CPU_ZERO(&mask);
  for (int cpu : cpus)
    CPU_SET(cpu, &mask);
  return sched_setaffinity(tid, sizeof(mask), &mask) == 0;
}
// Maximum frequencies do not change at runtime; read each CPU once.
std::mutex s_frequencyMutex;
std::map<int, uint64_t> s_frequencies;
std::map<int, uint64_t> Frequencies(const std::set<int>& cpus)
{
  std::lock_guard<std::mutex> lock(s_frequencyMutex);
  std::map<int, uint64_t> result;
  for (int cpu : cpus)
  {
    auto it = s_frequencies.find(cpu);
    if (it == s_frequencies.end())
      it = s_frequencies.emplace(cpu, MaxFrequency(cpu)).first;
    result.insert(*it);
  }
  return result;
}
std::atomic_bool s_decodeAnnounced{false};
// The worker's own CPUs while a decode restricts it to the slow cluster.
thread_local std::set<int> t_decodeRestore;
} // namespace
#endif

namespace PERFORMANCE_CORES
{
int CurrentCpu()
{
#if defined(TARGET_LINUX)
  return sched_getcpu();
#else
  return -1;
#endif
}

bool ApplyDetectionCurrentThread()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (!CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(
          CSettings::SETTING_SUBTITLES_ISOLATEACTIVEAREA))
    return true;
  std::set<int> available;
  std::set<int> playback = Requested(); // reserve intended masks during thread startup
  std::set<pid_t> threads;
  {
    std::lock_guard<std::mutex> lock(s_playbackThreadsMutex);
    threads = s_playbackThreads;
  }
  // The main thread's mask supplies Kodi's available CPUs, unless it is the
  // pinned render thread: then use Kodi's CPUs from just before pinning. Also
  // read live masks of registered playback threads, including changes made
  // outside Kodi.
  bool readable;
  if (s_renderThread.load() == getpid())
  {
    available = ProcessCpus();
    readable = !available.empty();
  }
  else
    readable = ReadCpus(getpid(), available);
  for (pid_t tid : threads)
    if (!ReadCpus(tid, playback) && errno != ESRCH)
      readable = false;
  const auto requested = DetectionRequested(available, playback);
  if (!readable || requested.empty())
  {
    CLog::Log(LOGERROR, "p3i-affinity role=active-area-detection no separate CPUs; "
                       "available={} playback={} readable={}",
              CpuList(available), CpuList(playback), readable);
    return false;
  }
  int setError = 0, readError = 0;
  const auto result = ApplyFor(requested, [&](const std::set<int>& cpus) {
    cpu_set_t mask;
    CPU_ZERO(&mask);
    for (int cpu : cpus)
      CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask) == 0)
      return true;
    setError = errno;
    return false;
  }, [&](std::set<int>& effective) {
    if (ReadCpus(0, effective))
      return true;
    readError = errno;
    return false;
  });
  bool complete = result.setSucceeded && result.readSucceeded && !result.effective.empty();
  for (int cpu : result.effective)
    complete = complete && requested.count(cpu) != 0 && playback.count(cpu) == 0;
  CLog::Log(complete ? LOGINFO : LOGERROR,
            "p3i-affinity role=active-area-detection tid={} requested={} effective={} "
            "complete={} set_error={} read_error={}",
            syscall(SYS_gettid), CpuList(requested),
            result.readSucceeded ? CpuList(result.effective) : "unavailable", complete,
            setError, readError);
  return complete;
#else
  return true;
#endif
}

void ApplyRenderThread()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  const pid_t tid = static_cast<pid_t>(syscall(SYS_gettid));
  if (s_renderThread.load() == tid)
    return;
  std::set<int> available;
  if (!ReadCpus(0, available))
  {
    CLog::Log(LOGERROR, "p3i-affinity role=gui-render tid={} unchanged; cannot read CPUs error={}",
              tid, errno);
    return;
  }
  std::map<int, uint64_t> frequency;
  std::string frequencies;
  for (int cpu : available)
  {
    frequency[cpu] = MaxFrequency(cpu);
    frequencies += (frequencies.empty() ? "" : ",") + std::to_string(cpu) + ":" +
                   std::to_string(frequency[cpu]);
  }
  if (!FastClusterRequested(available, frequency))
  {
    CLog::Log(LOGINFO, "p3i-affinity role=gui-render tid={} unchanged; requested=2,3,4,5 "
                       "is not the fast cluster of available={} max_khz={}",
              tid, CpuList(available), frequencies);
    return;
  }
  {
    // Publish the CPUs before the mask changes, so a detector never reads a
    // pinned main thread without them.
    std::lock_guard<std::mutex> lock(s_renderMutex);
    s_processCpus = available;
  }
  s_renderThread.store(tid);
  const int setError = SetCpus(Requested()) ? 0 : errno;
  std::set<int> effective;
  const bool readable = ReadCpus(0, effective);
  const bool complete = !setError && readable && effective == Requested();
  if (!complete)
  {
    // Never leave a partial or unknown narrowing in place.
    SetCpus(available);
    s_renderThread.store(0);
  }
  CLog::Log(complete ? LOGINFO : LOGERROR,
            "p3i-affinity role=gui-render tid={} requested=2,3,4,5 effective={} available={} "
            "max_khz={} complete={} set_error={}",
            tid, readable ? CpuList(effective) : "unavailable", CpuList(available), frequencies,
            complete, setError);
#endif
}

void MaintainRenderThread()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  // Only the render thread calls this, so the counters need no locking.
  static unsigned int calls = 0;
  static bool waiting = false;
  if ((++calls & 255) != 0)
    return;
  const pid_t tid = static_cast<pid_t>(syscall(SYS_gettid));
  std::set<int> current;
  if (s_renderThread.load() != tid || !ReadCpus(0, current) || current == Requested())
    return;
  // The kernel limits the mask to online CPUs. While part of the cluster is
  // still offline, the thread gets the online part, and a later check widens
  // it again; only the complete cluster is logged as restored.
  const int setError = SetCpus(Requested()) ? 0 : errno;
  std::set<int> effective;
  const bool readable = !setError && ReadCpus(0, effective);
  bool restored = readable && !effective.empty();
  for (int cpu : effective)
    restored = restored && Requested().count(cpu) != 0;
  if (restored && effective == Requested())
  {
    CLog::Log(LOGINFO, "p3i-affinity role=gui-render tid={} restored requested=2,3,4,5 from={}",
              tid, CpuList(current));
    waiting = false;
  }
  else if (!restored && !waiting)
  {
    // Typically EINVAL while the whole fast cluster is offline. Retry later
    // without logging every attempt.
    CLog::Log(LOGINFO, "p3i-affinity role=gui-render tid={} waiting for requested=2,3,4,5 "
                       "current={} set_error={}",
              tid, CpuList(current), setError);
    waiting = true;
  }
#endif
}

void CDecodePlacement::NeededNow()
{
  std::lock_guard<std::mutex> lock(m_mutex);
  m_neededNow = true;
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (m_worker == 0)
    return;
  // Widen the running decode from the consumer's thread; the worker restores
  // nothing afterwards. On failure it stays recorded, so a later call retries
  // and the worker restores its CPUs itself when the decode returns.
  if (!SetCpusOf(m_worker, m_restore))
  {
    CLog::Log(LOGERROR, "p3i-affinity role=image-decode tid={} widen={} failed error={}",
              m_worker, CpuList(m_restore), errno);
    return;
  }
  m_worker = 0;
#endif
}

CDecodePlacement::Scope::Scope(const std::shared_ptr<CDecodePlacement>& placement)
  : m_placement(placement)
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (!m_placement)
    return;
  std::lock_guard<std::mutex> lock(m_placement->m_mutex);
  std::set<int> current;
  if (m_placement->m_neededNow || !ReadCpus(0, current) ||
      !FastClusterRequested(current, Frequencies(current)))
    return;
  std::set<int> slow;
  for (int cpu : current)
    if (!Requested().count(cpu))
      slow.insert(cpu);
  if (!SetCpus(slow))
    return;
  m_placement->m_worker = static_cast<pid_t>(syscall(SYS_gettid));
  m_placement->m_restore = current;
  t_decodeRestore = current;
  if (!s_decodeAnnounced.exchange(true))
    CLog::Log(LOGINFO, "p3i-affinity role=image-decode slow={} until a foreground control draws "
                       "the image; restore={}",
              CpuList(slow), CpuList(current));
#endif
}

CDecodePlacement::Scope::~Scope()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (!m_placement)
    return;
  std::lock_guard<std::mutex> lock(m_placement->m_mutex);
  // Threads this worker creates from now on keep its mask again.
  t_decodeRestore.clear();
  if (m_placement->m_worker != static_cast<pid_t>(syscall(SYS_gettid)))
    return; // never restricted, or already widened by NeededNow
  m_placement->m_worker = 0;
  if (!SetCpus(m_placement->m_restore))
  {
    try
    {
      CLog::Log(LOGERROR, "p3i-affinity role=image-decode restore={} failed error={}",
                CpuList(m_placement->m_restore), errno);
    }
    catch (...)
    {
    }
  }
#endif
}

bool InheritsRenderAffinity()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  const pid_t render = s_renderThread.load();
  return render != 0 && render == static_cast<pid_t>(syscall(SYS_gettid));
#else
  return false;
#endif
}

std::set<int> ChildAffinity()
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (InheritsRenderAffinity())
    return ProcessCpus();
  // Widened by NeededNow meanwhile, the worker's mask already equals these.
  return t_decodeRestore;
#else
  return {};
#endif
}

void RestoreChildAffinity([[maybe_unused]] const std::set<int>& cpus)
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  if (!cpus.empty() && !SetCpus(cpus))
    CLog::Log(LOGERROR, "p3i-affinity role=child tid={} restore={} failed error={}",
              syscall(SYS_gettid), CpuList(cpus), errno);
#endif
}

void ApplyCurrentThread(const char* role, uint64_t identity)
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
  thread_local PlaybackThread registration;
  int setError = 0, readError = 0;
  const auto result = Apply([&](const std::set<int>& requested) {
    cpu_set_t mask;
    CPU_ZERO(&mask);
    for (int cpu : requested)
      CPU_SET(cpu, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask) == 0)
      return true;
    setError = errno;
    return false;
  }, [&](std::set<int>& effective) {
    cpu_set_t mask;
    CPU_ZERO(&mask);
    if (sched_getaffinity(0, sizeof(mask), &mask) != 0)
    {
      readError = errno;
      return false;
    }
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
      if (CPU_ISSET(cpu, &mask))
        effective.insert(cpu);
    return true;
  });
  std::string effective;
  for (int cpu : result.effective)
    effective += (effective.empty() ? "" : ",") + std::to_string(cpu);
  sched_param param{};
  const int paramResult = sched_getparam(0, &param);
  errno = 0;
  const int nice = getpriority(PRIO_PROCESS, 0);
  const int niceError = errno;
  CLog::Log(result.Complete() ? LOGINFO : LOGERROR,
            "p3i-affinity t_us={} role={} id={} tid={} requested=2,3,4,5 effective={} "
            "complete={} set_error={} read_error={} policy={} priority={} nice={} nice_error={} cpu={}",
            PLAYBACK_DIAGNOSTICS::NowUs(), role, identity, syscall(SYS_gettid),
            result.readSucceeded ? effective : "unavailable", result.Complete(), setError, readError,
            sched_getscheduler(0), paramResult == 0 ? param.sched_priority : -1,
            nice, niceError, CurrentCpu());
#else
  // No affinity change outside the dedicated Linux AML target.
  CLog::Log(LOGDEBUG, "p3i-affinity t_us={} role={} id={} non-target unchanged",
            PLAYBACK_DIAGNOSTICS::NowUs(), role, identity);
#endif
}
} // namespace PERFORMANCE_CORES
