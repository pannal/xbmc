/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "windowing/amlogic/AMLNativeTransaction.h"

#include <chrono>
#include <condition_variable>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>

// Preserve the existing deferred apply-thread owner while making queued/running
// work visible to window retirement. Cancellation wakes sleepers; admitted work
// finishes before Drained. No wait or native effect runs under the state mutex.
class CAMLDeferredWork
{
  struct State
  {
    std::mutex mutex;
    std::condition_variable changed;
    bool closed{false};
    std::exception_ptr failure;
    unsigned int outstanding{0};
  };
public:
  // The delayed owner acquires its own admission before fresh reads/effects.
  // Cancellation interrupts admission retries as well as the initial delay.
  bool ScheduleNative(std::chrono::milliseconds delay, std::function<void()> action)
  {
    auto state = m_state;
    return Schedule(delay, [state, action = std::move(action)]() mutable {
      CAMLNativeTransaction native;
      std::unique_lock<std::mutex> lock(state->mutex);
      while (!state->closed)
      {
        lock.unlock();
        const bool admitted = native.TryBegin();
        lock.lock();
        if (state->closed)
          return;
        if (admitted)
        {
          lock.unlock();
          action();
          return;
        }
        state->changed.wait_for(lock, std::chrono::milliseconds(10), [&] { return state->closed; });
      }
    });
  }

  std::exception_ptr Failure() const
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return m_state->failure;
  }

  bool Schedule(std::chrono::milliseconds delay, std::function<void()> action)
  {
    auto state = m_state;
    {
      std::lock_guard<std::mutex> lock(state->mutex);
      if (state->closed)
        return false;
      ++state->outstanding;
    }
    try
    {
      std::thread([state, delay, action = std::move(action)]() mutable {
        std::unique_lock<std::mutex> lock(state->mutex);
        state->changed.wait_for(lock, delay, [&] { return state->closed; });
        const bool run = !state->closed;
        lock.unlock();
        try
        {
          if (run)
            action();
        }
        catch (...)
        {
          // A detached failure must still release admission/captures and drain.
          // Keep the failure observable to the owner at retirement.
          std::lock_guard<std::mutex> failureLock(state->mutex);
          state->failure = std::current_exception();
        }
        // Destroy captures before acknowledging retirement.
        action = {};
        lock.lock();
        --state->outstanding;
      }).detach();
    }
    catch (...)
    {
      std::lock_guard<std::mutex> lock(state->mutex);
      --state->outstanding;
      throw;
    }
    return true;
  }
  bool Cancel()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    m_state->closed = true;
    m_state->changed.notify_all();
    return m_state->outstanding == 0;
  }
private:
  const std::shared_ptr<State> m_state{std::make_shared<State>()};
};
