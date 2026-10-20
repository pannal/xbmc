/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * See LICENSES/README.md for more information.
 */
#pragma once

#include <algorithm>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>

// Requests may arrive on input, player, and RPC threads. Only the application
// thread applies them and commits the backend result. Never hold this mutex
// while calling a backend or a window/resource lifecycle operation.
class CDPMSControl
{
public:
  struct State
  {
    bool active{false};
    bool manual{false};
  };
  struct Request : State
  {
    uint64_t serial;
    bool explicitRequest{false};
    bool powerOff{false};
  };

  State GetState() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_state;
  }

  bool Sleep()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_state.active || m_wakePending || m_inFlight || !m_requests.empty())
      return false;
    m_requests.push_back({{true, false}, ++m_serial});
    return true;
  }

  void ToggleManual()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    // Explicit input supersedes idle intent that has not reached the backend.
    // Preserve in-flight work and the order of already accepted manual toggles.
    m_requests.erase(std::remove_if(m_requests.begin(), m_requests.end(),
                                   [](const Request& r) { return !r.explicitRequest; }),
                     m_requests.end());
    const State desired = Desired();
    m_requests.push_back({{!desired.active, !desired.active}, ++m_serial, true});
    m_wakePending = (m_inFlight && !m_inFlight->active) ||
        std::any_of(m_requests.begin(), m_requests.end(),
                    [](const Request& r) { return !r.active; });
  }

  // The return value means consume ordinary input, not that wake completed.
  bool Wake(bool powerOff = false)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    // Playback callbacks while already awake must leave idle timing alone.
    // Pending sleep still needs cancellation, including an in-flight entry.
    if (!m_state.active && !m_inFlight && m_requests.empty())
      return false;
    const State desired = Desired();
    if ((m_state.active && m_state.manual) || (desired.active && desired.manual))
      return false;
    const bool consume = m_state.active || (m_inFlight && m_inFlight->active);
    // Coalescing another wake must not undo a pending power action's CEC
    // suppression. In-flight requests keep their own, separate context.
    powerOff |= std::any_of(m_requests.begin(), m_requests.end(),
                           [](const Request& r) {
                             return !r.explicitRequest && !r.active && r.powerOff;
                           });
    m_requests.erase(std::remove_if(m_requests.begin(), m_requests.end(),
                                   [](const Request& r) { return !r.explicitRequest; }),
                     m_requests.end());
    m_requests.push_back({{false, false}, ++m_serial, false, powerOff});
    m_wakePending = true;
    return consume;
  }

  std::optional<Request> Begin()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_requests.empty())
      return {};
    m_inFlight = m_requests.front();
    m_requests.pop_front();
    return m_inFlight;
  }

  void Defer(const Request& request)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    // A newer wake cancels an automatic entry that has not touched hardware.
    if (request.explicitRequest || !request.active || !m_wakePending)
      m_requests.push_front(request);
    m_inFlight.reset();
  }

  void Complete(const Request& request, bool success)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (success)
      m_state = {request.active, request.manual};
    if (!request.active)
      m_wakePending = m_state.active ||
          std::any_of(m_requests.begin(), m_requests.end(),
                      [](const Request& r) { return !r.active; });
    // A failed wake retains committed sleep and its automatic-sleep barrier,
    // but a failed lifecycle release while already awake must not strand it.
    // A later input can retry; FrameMove must not spin on I/O failure.
    m_inFlight.reset();
  }

  void ClearRequests()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_requests.clear();
    m_inFlight.reset();
    m_wakePending = true;
  }

  bool HasRequests() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_inFlight || !m_requests.empty();
  }

private:
  State Desired() const
  {
    if (!m_requests.empty())
      return m_requests.back();
    if (m_inFlight)
      return *m_inFlight;
    return m_state;
  }

  mutable std::mutex m_mutex;
  State m_state;
  std::deque<Request> m_requests;
  std::optional<Request> m_inFlight;
  uint64_t m_serial{0};
  bool m_wakePending{false};
};
