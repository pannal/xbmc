/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>

// Home-property publication is the only GUI boundary. Playback and scanner
// threads read this CPU-only state and never acquire a window/graphics lock.
namespace PM4K_STARTUP
{
inline constexpr const char* ITEM_TOKEN = "p3i.pm4k.startup_token";
using Clock = std::chrono::steady_clock;
inline constexpr auto MAX_WAIT = std::chrono::seconds(60);

struct Signal
{
  std::mutex mutex;
  uint64_t generation{0};
  uint64_t consumed{0};
  bool initializing{false};
  Clock::time_point published{};
};

inline Signal& State()
{
  static Signal signal;
  return signal;
}

inline void Publish(bool initializing, Clock::time_point now = Clock::now())
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  signal.initializing = initializing;
  if (initializing)
  {
    // A new startup may overwrite an orphaned '1' from an aborted playback.
    ++signal.generation;
    signal.published = now;
  }
}

inline uint64_t Capture(const std::string& addon, Clock::time_point now = Clock::now())
{
  if (addon != "script.plexmod")
    return 0;
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  return signal.initializing && now - signal.published < MAX_WAIT ? signal.generation : 0;
}

inline uint64_t Claim(uint64_t token, Clock::time_point now = Clock::now())
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  if (!token || token != signal.generation || token <= signal.consumed ||
      !signal.initializing || now - signal.published >= MAX_WAIT)
    return 0;
  signal.consumed = token;
  return token;
}

inline bool Pending(uint64_t token, Clock::time_point now = Clock::now())
{
  auto& signal = State();
  std::lock_guard<std::mutex> lock(signal.mutex);
  // Expiry only abandons an orphaned signal. Current video sync is still
  // required by the player before it can establish the detector hold.
  return token && token == signal.generation && signal.initializing &&
         now - signal.published < MAX_WAIT;
}
} // namespace PM4K_STARTUP
