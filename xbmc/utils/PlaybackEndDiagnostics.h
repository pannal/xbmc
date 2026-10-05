/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "utils/PlaybackDiagnostics.h"

#include <array>
#include <cstdint>
#include <deque>
#include <string>
#include <utility>

// Main-thread observations only. Producers never log or read native state. The
// application drains outside Render/Flip and still pumps when rendering is off.
namespace PLAYBACK_DIAGNOSTICS
{
class EndDisplayTrace
{
public:
  static constexpr uint64_t WINDOW_US = 5000000;
  static constexpr unsigned DETAIL_LIMIT = 32;
  static constexpr unsigned QUEUE_LIMIT = 64;
  struct Entry
  {
    uint64_t at, trace, display;
    const char* kind;
    std::string detail;
  };
  bool Active(uint64_t now) const { return Enabled() && m_active && now < m_deadline; }
  void Cancel()
  {
    m_active = false;
    m_queue.clear();
    m_lost = 0;
  }
  uint64_t Id() const { return m_id; }
  uint64_t Display() const { return m_display; }
  void Generation(uint64_t value)
  {
    if (value != m_display)
    {
      m_generationDraw = m_generationSwap = false;
      m_display = value;
    }
  }
  void Begin(uint64_t now, const char* reason)
  {
    if (!Enabled())
    {
      Cancel();
      return;
    }
    Finish(now, "superseded");
    ++m_id;
    m_active = true;
    m_deadline = now + WINDOW_US;
    m_details = m_milestones = 0;
    m_generationDraw = m_generationSwap = false;
    m_lastDrawDisplay = m_lastSwapDisplay = 0;
    m_suppressed = m_frames = m_draws = m_swaps = m_failed = 0;
    m_firstDraw = m_firstSwap = m_attempts = m_swapFailures = 0;
    m_lastEglError = 0;
    m_lastFrame = {};
    m_haveFrame = m_havePresent = m_startSnapshot = m_successSnapshot = false;
    Push(now, "begin", reason);
  }
  template<class Describe>
  void Record(uint64_t now, const char* kind, Describe describe, bool milestone = false)
  {
    if (!Active(now))
      return;
    if (milestone ? m_milestones++ < 8 : m_details++ < DETAIL_LIMIT)
      Push(now, kind, describe());
    else
      ++m_suppressed;
  }
  void SwapResult(uint64_t now, bool attempted, bool success, int error)
  {
    if (!Active(now))
      return;
    m_attempts += attempted;
    m_swapFailures += attempted && !success;
    if (attempted && !success)
      m_lastEglError = error;
  }
  bool PresentChanged(uint64_t now, bool rendered, int result, bool ready, bool delay)
  {
    if (!Active(now))
      return false;
    const std::array<int64_t, 5> state{static_cast<int64_t>(m_display), rendered, result, ready, delay};
    if (m_havePresent && state == m_lastPresent)
    {
      ++m_suppressed;
      return false;
    }
    m_lastPresent = state;
    m_havePresent = true;
    return true;
  }
  // One observation per main loop, including render-disabled/rejected frames.
  // Enum values are reported with their definitions in the delivery notes.
  template<class Describe>
  void Frame(uint64_t now, uint64_t drawDisplay, int status, bool draw, int present, int window, bool fullscreen,
             bool swapped, bool failed, Describe describe)
  {
    if (!Active(now))
      return;
    ++m_frames;
    m_draws += draw;
    m_swaps += swapped;
    m_failed += failed;
    // Reserve bounded milestone records independently of noisy state changes,
    // so a late first draw/swap is still attributable to its display generation.
    if (draw && (m_lastDrawDisplay != drawDisplay || (swapped && m_lastSwapDisplay != drawDisplay)))
    {
      if (m_milestones++ < 8)
        Push(now, swapped ? "first-generation-gui-swap" : "first-generation-draw", describe(), drawDisplay);
      else
        ++m_suppressed;
      m_lastDrawDisplay = drawDisplay;
      if (swapped)
        m_lastSwapDisplay = drawDisplay;
    }
    // A cancelled draw to an old target is still observed, but cannot prove
    // that the replacement display received GUI commands.
    if (draw && drawDisplay == m_display)
    {
      m_generationDraw = true;
      m_generationSwap |= swapped;
    }
    if (draw && !m_firstDraw)
      m_firstDraw = now;
    if (draw && swapped && !m_firstSwap)
      m_firstSwap = now;
    const std::array<int64_t, 7> frame{static_cast<int64_t>(m_display), status, draw,
                                       present, window, fullscreen, static_cast<int64_t>(drawDisplay)};
    if (!m_haveFrame || frame != m_lastFrame)
      Record(now, "frame", describe);
    else
      ++m_suppressed;
    m_haveFrame = true;
    m_lastFrame = frame;
  }
  // At most three read-only snapshots per trace, never a retry loop. A late
  // pump records timeout even if neither Render nor Present ever succeeded.
  const char* SnapshotReason(uint64_t now)
  {
    if (!Enabled() || !m_active)
      return nullptr;
    if (now >= m_deadline)
      return m_generationSwap ? nullptr : "timeout-no-gui-swap";
    if (!m_startSnapshot)
    {
      m_startSnapshot = true;
      if (m_firstSwap)
      {
        m_successSnapshot = true;
        return "first-pump-gui-swap";
      }
      return "first-pump";
    }
    if (m_firstSwap && !m_successSnapshot)
    {
      m_successSnapshot = true;
      return "first-gui-swap";
    }
    return nullptr;
  }
  void Expire(uint64_t now)
  {
    if (m_active && now >= m_deadline)
      Finish(now, "timeout");
  }
  void Finish(uint64_t now, const char* reason)
  {
    if (!m_active)
      return;
    Push(now, "summary", std::string(reason) + " frames=" + std::to_string(m_frames) +
         " draws=" + std::to_string(m_draws) + " swaps=" + std::to_string(m_swaps) +
         " failed=" + std::to_string(m_failed) + " first_draw_us=" + std::to_string(m_firstDraw) +
         " first_gui_swap_us=" + std::to_string(m_firstSwap) +
         " last_display_draw=" + std::to_string(m_generationDraw) +
         " last_display_gui_swap=" + std::to_string(m_generationSwap) +
         " swap_attempts=" + std::to_string(m_attempts) +
         " swap_failures=" + std::to_string(m_swapFailures) +
         " last_egl_error=" + std::to_string(m_lastEglError) +
         " suppressed=" + std::to_string(m_suppressed));
    m_active = false;
  }
  template<class Emit>
  void Drain(Emit emit)
  {
    for (const auto& entry : m_queue)
      emit(entry);
    m_queue.clear();
  }
  uint64_t TakeLost() { return std::exchange(m_lost, 0); }
private:
  void Push(uint64_t now, const char* kind, std::string detail)
  {
    Push(now, kind, std::move(detail), m_display);
  }
  void Push(uint64_t now, const char* kind, std::string detail, uint64_t display)
  {
    if (m_queue.size() == QUEUE_LIMIT)
    {
      m_queue.pop_front();
      ++m_lost;
    }
    m_queue.push_back({now, m_id, display, kind, std::move(detail)});
  }
  bool m_active{false}, m_haveFrame{false}, m_havePresent{false};
  bool m_startSnapshot{false}, m_successSnapshot{false};
  std::array<int64_t, 5> m_lastPresent{};
  uint64_t m_id{0}, m_display{0}, m_deadline{0}, m_lost{0};
  uint64_t m_suppressed{0}, m_frames{0}, m_draws{0}, m_swaps{0}, m_failed{0};
  uint64_t m_firstDraw{0}, m_firstSwap{0};
  bool m_generationDraw{false}, m_generationSwap{false};
  uint64_t m_attempts{0}, m_swapFailures{0};
  int m_lastEglError{0};
  unsigned m_details{0}, m_milestones{0};
  uint64_t m_lastDrawDisplay{0}, m_lastSwapDisplay{0};
  std::array<int64_t, 7> m_lastFrame{};
  std::deque<Entry> m_queue;
};
inline EndDisplayTrace endDisplay;
} // namespace PLAYBACK_DIAGNOSTICS
