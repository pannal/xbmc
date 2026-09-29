/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "ISettingCallback.h"

#include <memory>
#include <mutex>
#include <utility>

// Copies of a dispatch list retain this record, not an unrevocable raw pointer.
// Revocation never waits: owners poll Drained() outside callback-needed locks
// before destruction. Legacy UnregisterCallback still does not drain active calls.
class CSettingCallbackRegistration
{
  struct State
  {
    explicit State(ISettingCallback* target) : callback(target) {}
    ISettingCallback* const callback;
    std::mutex mutex;
    bool revoked{false};
    unsigned int active{0};
  };

public:
  explicit CSettingCallbackRegistration(ISettingCallback* callback)
    : m_state(std::make_shared<State>(callback)) {}

  class Lease
  {
  public:
    Lease() = default;
    explicit Lease(std::shared_ptr<State> state) : m_state(std::move(state)) {}
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;
    Lease(Lease&& other) noexcept : m_state(std::move(other.m_state)) {}
    ~Lease()
    {
      if (m_state)
      {
        std::lock_guard<std::mutex> lock(m_state->mutex);
        --m_state->active;
      }
    }
    explicit operator bool() const { return bool(m_state); }
    ISettingCallback* operator->() const { return m_state->callback; }
  private:
    std::shared_ptr<State> m_state;
  };

  Lease Acquire()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (m_state->revoked)
      return {};
    ++m_state->active;
    return Lease(m_state);
  }
  void Revoke()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    m_state->revoked = true;
  }
  bool Drained() const
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return m_state->revoked && m_state->active == 0;
  }
private:
  const std::shared_ptr<State> m_state;
};
