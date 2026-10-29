/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <utility>

// One sysfs reference for all Kodi restart holds. Exact owners prevent a late
// decoder release from unmuting its replacement. The writer must not throw.
class CAMLVideoMute
{
public:
  using Owner = std::shared_ptr<const void>;
  using Clock = std::chrono::steady_clock;
  explicit CAMLVideoMute(std::function<void(bool)> writer) : m_writer(std::move(writer)) {}

  Owner Acquire()
  {
    auto owner = std::make_shared<const char>(0);
    std::lock_guard<std::mutex> lock(m_mutex);
    m_owners.emplace(owner, ++m_generation);
    m_handoff = 0;
    Apply();
    return owner;
  }

  void Release(const Owner& owner)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_owners.erase(owner);
    Apply();
  }

  // Called after native decoder teardown finishes. Keep black until a GUI frame
  // rendered after this handoff is accepted, another playback starts, or expiry.
  void Handoff(const Owner& owner, Clock::time_point deadline)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto it = m_owners.find(owner);
    if (it == m_owners.end())
      return;
    const bool newest = it->second == m_generation;
    m_owners.erase(it);
    if (newest && m_owners.empty())
    {
      m_deadline = deadline;
      m_handoff = ++m_generation;
    }
    Apply();
  }

  // Unmasked playback/shutdown supersedes pending handoffs, including a late
  // close that has not published its handoff yet. Active owners stay intact.
  void CancelHandoff()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    ++m_generation;
    m_handoff = 0;
    Apply();
  }

  uint64_t HandoffGeneration() const { return m_handoff.load(); }

  void Expire(Clock::time_point now)
  {
    if (!m_handoff.load())
      return;
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_handoff.load() && now >= m_deadline)
    {
      m_handoff = 0;
      Apply();
    }
  }

  void Presented(uint64_t generation, bool accepted, bool ready, bool fullscreenVideo)
  {
    if (!generation || !accepted || !ready || fullscreenVideo)
      return;
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_handoff.load() == generation)
    {
      m_handoff = 0;
      Apply();
    }
  }

private:
  void Apply()
  {
    const bool mute = !m_owners.empty() || m_handoff.load() != 0;
    if (mute != m_requested)
    {
      m_requested = mute;
      m_writer(mute);
    }
  }
  std::mutex m_mutex;
  std::map<Owner, uint64_t, std::owner_less<Owner>> m_owners;
  uint64_t m_generation{0};
  std::atomic<uint64_t> m_handoff{0};
  Clock::time_point m_deadline{};
  bool m_requested{false};
  std::function<void(bool)> m_writer;
};
