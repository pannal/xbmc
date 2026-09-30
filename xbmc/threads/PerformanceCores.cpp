/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "PerformanceCores.h"

#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"

#include <cerrno>
#include <string>

#if defined(TARGET_LINUX)
#include <sched.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
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

void ApplyCurrentThread(const char* role, uint64_t identity)
{
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
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
