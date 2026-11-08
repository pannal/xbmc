/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <algorithm>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
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
// The requested CPUs form a faster cluster only when Kodi may use all of them
// and each reports a higher maximum frequency than every other available CPU.
// Missing frequencies or a uniform cluster leave placement to the scheduler.
inline bool FastClusterRequested(const std::set<int>& available,
                                 const std::map<int, uint64_t>& maxFrequency,
                                 const std::set<int>& requested = Requested())
{
  uint64_t slowestRequested = UINT64_MAX, fastestOther = 0;
  bool other = false;
  for (int cpu : available)
  {
    const auto it = maxFrequency.find(cpu);
    if (it == maxFrequency.end() || it->second == 0)
      return false;
    if (requested.count(cpu))
      slowestRequested = std::min(slowestRequested, it->second);
    else
    {
      other = true;
      fastestOther = std::max(fastestOther, it->second);
    }
  }
  for (int cpu : requested)
    if (!available.count(cpu))
      return false;
  return other && slowestRequested > fastestOther;
}
void ApplyCurrentThread(const char* role, uint64_t identity);
// Optional AML detection isolation excludes playback masks from available CPUs.
// Decoder threads inherit the resulting mask. False means decoding must not start.
bool ApplyDetectionCurrentThread();
// Keep the GUI render (main) thread on the fast cluster. The scheduler parks
// it on a slow core while it waits between key presses, so the next frame's
// item, text and render work misses its refresh there.
void ApplyRenderThread();
// Called by the render thread every main-loop iteration; every 256th call
// checks its mask. Taking the fast cluster offline, as suspend does, moves
// the thread to the slow CPUs and the kernel keeps it there afterwards.
// Restore the fast cluster once it is back online.
void MaintainRenderThread();
// Placement of one image decode. Decoding on a job worker competes with the
// render thread for the fast cluster. A decode that nothing draws yet, such as
// fanart behind its fade or a look-ahead poster, runs on the slow cluster.
// When a foreground control tries to draw the image, the decode returns to
// the worker's own CPUs at once, also while it is running.
class CDecodePlacement
{
public:
  // A foreground consumer is waiting to draw this image. Permanent.
  void NeededNow();

  // Held by the decoding worker for the duration of the decode.
  class Scope
  {
  public:
    explicit Scope(const std::shared_ptr<CDecodePlacement>& placement);
    ~Scope();
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;

  private:
    std::shared_ptr<CDecodePlacement> m_placement;
  };

private:
  std::mutex m_mutex;
  bool m_neededNow{false};
  int m_worker{0}; // thread restricted to the slow cluster, 0 if none
  std::set<int> m_restore;
};
// True on the pinned render thread. Threads it starts would inherit its mask.
bool InheritsRenderAffinity();
// CPUs that a thread created now by the calling thread takes before it runs,
// or none to keep the mask it inherits. A child of the pinned render thread
// gets Kodi's CPUs back, and a child of a worker restricted to the slow
// cluster for a decode gets that worker's own CPUs.
std::set<int> ChildAffinity();
// Called first on such a child, with its creator's ChildAffinity().
void RestoreChildAffinity(const std::set<int>& cpus);
int CurrentCpu();
} // namespace PERFORMANCE_CORES
