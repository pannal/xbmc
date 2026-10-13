/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <cstdint>
#include <set>

namespace PERFORMANCE_CORES
{
// Dedicated S922XJ build contract: four A73 CPUs, no runtime topology policy.
inline std::set<int> Requested() { return {2, 3, 4, 5}; }
inline std::set<int> DetectionRequested(const std::set<int>& available,
                                      const std::set<int>& playback)
{
  std::set<int> requested;
  for (int cpu : available)
    if (playback.count(cpu) == 0)
      requested.insert(cpu);
  return requested;
}
struct Result
{
  bool setSucceeded{false};
  bool readSucceeded{false};
  std::set<int> effective;
  bool Complete(const std::set<int>& requested = Requested()) const
  {
    return setSucceeded && readSucceeded && effective == requested;
  }
};
// Tests exercise the same syscall/outcome boundary, including silent OS narrowing.
template<class SetAffinity, class GetAffinity>
Result ApplyFor(const std::set<int>& requested, SetAffinity setAffinity, GetAffinity getAffinity)
{
  Result result;
  result.setSucceeded = setAffinity(requested);
  result.readSucceeded = getAffinity(result.effective);
  return result;
}
template<class SetAffinity, class GetAffinity>
Result Apply(SetAffinity setAffinity, GetAffinity getAffinity)
{
  return ApplyFor(Requested(), setAffinity, getAffinity);
}
void ApplyCurrentThread(const char* role, uint64_t identity);
// Optional AML detection isolation excludes playback masks from available CPUs.
// Decoder threads inherit the resulting mask. False means decoding must not start.
bool ApplyDetectionCurrentThread();
int CurrentCpu();
} // namespace PERFORMANCE_CORES
