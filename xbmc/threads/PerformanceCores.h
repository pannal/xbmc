/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <cstdint>
#include <set>

namespace PERFORMANCE_CORES
{
// Dedicated S922XJ build contract: four A73 CPUs, no runtime topology policy.
inline std::set<int> Requested() { return {2, 3, 4, 5}; }
struct Result
{
  bool setSucceeded{false};
  bool readSucceeded{false};
  std::set<int> effective;
  bool Complete() const { return setSucceeded && readSucceeded && effective == Requested(); }
};
// Tests exercise the same syscall/outcome boundary, including silent OS narrowing.
template<class SetAffinity, class GetAffinity>
Result Apply(SetAffinity setAffinity, GetAffinity getAffinity)
{
  Result result;
  result.setSucceeded = setAffinity(Requested());
  result.readSucceeded = getAffinity(result.effective);
  return result;
}
void ApplyCurrentThread(const char* role, uint64_t identity);
int CurrentCpu();
} // namespace PERFORMANCE_CORES
