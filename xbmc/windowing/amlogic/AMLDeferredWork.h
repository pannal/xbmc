/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <chrono>
#include <condition_variable>
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
    unsigned int outstanding{0};
  };
public:
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
        if (run)
          action();
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
