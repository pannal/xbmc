/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once
#include "DetectorPlayback.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <utility>

// CPU-only backing for the generic Home property. Consuming a signal at a
// submission boundary never acquires GUI locks or clears a newer publication.
namespace DETECTOR_ADMISSION
{
inline constexpr const char* PROPERTY = "p3i.detector.skip_next";
inline constexpr const char* ITEM_SKIP = "p3i.detector.request_skip";

class PositionDeadline
{
public:
  using Clock = std::chrono::steady_clock;
  void Start(std::chrono::milliseconds budget) { m_end = Clock::now() + budget; }
  void Clear() { m_end = Clock::time_point::max(); }
  bool Expired() const { return Clock::now() >= m_end; }

private:
  Clock::time_point m_end{Clock::time_point::max()};
};

struct PlaybackPolicy
{
  PlaybackPolicy(bool skip = false, int seconds = 5) : skip(skip), seconds(seconds) {}
  const bool skip;
  const int seconds;
  const uint64_t id{DETECTOR_PLAYBACK::NextIdentity()};
  std::atomic<bool> timedOut{false};
  std::atomic<bool> ended{false};
  bool Blocked() const { return skip || timedOut.load() || ended.load(); }
};

struct Signal
{
  std::mutex mutex;
  uint64_t generation{0};
  std::string value;
};

inline Signal& State()
{
  static Signal signal;
  return signal;
}

inline uint64_t Publish(std::string value)
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  ++signal.generation;
  signal.value = std::move(value);
  return signal.generation;
}

inline std::string Value()
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  return signal.value;
}

inline bool Cancel(uint64_t generation)
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  if (!generation || generation != signal.generation || signal.value != "1")
    return false;
  signal.value.clear();
  return true;
}

inline bool Capture()
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  if (signal.value != "1")
    return false;
  signal.value.clear();
  return true;
}

inline constexpr const char* PLAYLIST_SKIP = "p3i.detector.request_skip=1";
inline constexpr const char* PLAYLIST_KEEP = "p3i.detector.request_skip=0";
inline std::string CapturePlaylist(bool video)
{
  return video ? (Capture() ? PLAYLIST_SKIP : PLAYLIST_KEEP) : "";
}

template<class Item>
void CaptureItem(Item& item)
{
  // False is a captured decision too: queued/deferred opens must not consume
  // a flag published after this request. Known audio/game requests leave it.
  if ((!item.IsAudio() || item.IsVideo()) && !item.IsGame() &&
      !item.IsPicture() && !item.IsPlayList() && !item.IsSmartPlayList())
  {
    item.CaptureDetectorBinding();
    if (!item.HasProperty(ITEM_SKIP))
      item.SetProperty(ITEM_SKIP, Capture());
  }
}
} // namespace DETECTOR_ADMISSION
