/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"

// Main-owned window transaction. Nested CreateNewWindow/ResetRenderSystem calls
// share one admission decision. An active codec call makes this a pending retry,
// never a wait under graphics/resource locks or on our own capture permit.
class CAMLDisplayLifecycle
{
public:
  using Phase = CAMLSession::DisplayPhase;
  class Mutation
  {
  public:
    explicit Mutation(CAMLDisplayLifecycle& lifecycle) : m_lifecycle(lifecycle)
    {
      m_admitted = m_lifecycle.Begin();
    }
    Mutation(const Mutation&) = delete;
    Mutation& operator=(const Mutation&) = delete;
    ~Mutation()
    {
      if (m_admitted)
        m_lifecycle.End(m_result);
    }
    explicit operator bool() const { return m_admitted; }
    void Finish(Phase result) { m_result = result; }
  private:
    CAMLDisplayLifecycle& m_lifecycle;
    bool m_admitted{false};
    Phase m_result{Phase::FAILED};
  };

  bool Pending() const { return m_pending; }
  bool Ready() const { return m_ready; }
  void Resume()
  {
    // PresentRenderImpl is also called from render-system destruction. It must
    // not reopen admission in a nested mutation or after a failed bind/reset.
    if (!m_depth && m_waiting && CAMLSession::EndDisplay(m_request, Phase::READY))
    {
      m_waiting = false;
      m_ready = true;
    }
  }

private:
  bool Begin()
  {
    if (std::this_thread::get_id() != m_owner)
      return false;
    if (m_depth)
    {
      ++m_depth;
      return true;
    }
    m_ready = false;
    if (!m_pending)
      m_request = CAMLSession::FenceDisplay();
    if (!m_request)
      return false;
    m_pending = true;
    m_waiting = false;
    if (!CAMLSession::TryBeginDisplay(m_request))
      return false;
    m_pending = false;
    ++m_depth;
    return true;
  }
  void End(Phase result)
  {
    if (--m_depth)
      return;
    const bool completed = CAMLSession::EndDisplay(m_request, result);
    m_waiting = completed && result == Phase::WAITING_FOR_RESET;
    m_ready = completed && result == Phase::READY;
  }

  const std::thread::id m_owner{std::this_thread::get_id()};
  CAMLSession::DisplayRequest m_request;
  unsigned int m_depth{0};
  bool m_pending{false};
  bool m_waiting{false};
  bool m_ready{true};
};
