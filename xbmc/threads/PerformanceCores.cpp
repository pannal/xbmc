/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "PerformanceCores.h"

#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"

#include <cerrno>
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
  // The main thread's mask supplies Kodi's available CPUs. Also read live masks
  // of registered playback threads, including changes made outside Kodi.
  bool readable = ReadCpus(getpid(), available);
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
