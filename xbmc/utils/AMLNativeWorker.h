/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "windowing/amlogic/AMLNativeTransaction.h"

#include <atomic>
#include <condition_variable>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>

// One joinable participant with an exact run identity. The worker never takes
// the handle mutex. Native owners may cancel/join a pending participant without
// waiting on admission they themselves hold. Retire closes startup permanently.
class CAMLNativeWorker
{
public:
  class Run
  {
  public:
    explicit Run(std::shared_ptr<std::atomic<bool>> superseded) : m_superseded(std::move(superseded)) {}
    bool Cancelled() const
    {
      return m_cancelled || (m_superseded && m_superseded->load());
    }
    bool WaitFor(std::chrono::milliseconds delay) const
    {
      std::unique_lock<std::mutex> lock(m_mutex);
      m_changed.wait_for(lock, delay, [&] { return Cancelled(); });
      return !Cancelled();
    }
    bool Admit(CAMLNativeTransaction& native) const
    {
      while (!Cancelled())
      {
        if (native.TryBegin())
          return !Cancelled();
        if (!WaitFor(std::chrono::milliseconds(10)))
          break;
      }
      return false;
    }
  private:
    friend class CAMLNativeWorker;
    void Cancel()
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      m_cancelled = true;
      m_changed.notify_all();
    }
    const std::shared_ptr<std::atomic<bool>> m_superseded;
    std::atomic<bool> m_cancelled{false};
    std::atomic<bool> m_done{false};
    std::exception_ptr m_failure;
    mutable std::mutex m_mutex;
    mutable std::condition_variable m_changed;
  };

  bool Start(std::function<void(const Run&)> action,
             std::shared_ptr<std::atomic<bool>> superseded = {})
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    StopLocked();
    if (m_closed)
      return false;
    auto run = std::make_shared<Run>(std::move(superseded));
    std::atomic_store(&m_run, run);
    try
    {
      m_thread = std::thread([run, action = std::move(action)]() mutable {
        try
        {
          if (!run->Cancelled())
            action(*run);
        }
        catch (...)
        {
          run->m_failure = std::current_exception();
        }
        action = {}; // release captures/admission before retirement acknowledgment
        run->m_done = true;
      });
    }
    catch (...)
    {
      run->m_failure = std::current_exception();
      run->m_done = true;
      throw;
    }
    // A concurrent Retire may have cancelled the previous run before this one
    // was published. Close that startup race without reopening cancellation.
    if (m_closed)
      run->Cancel();
    return true;
  }
  void Stop()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    StopLocked();
  }
  bool Retire()
  {
    m_closed = true;
    if (auto run = std::atomic_load(&m_run))
      run->Cancel();
    std::unique_lock<std::mutex> lock(m_mutex, std::try_to_lock);
    if (!lock.owns_lock())
      return false;
    auto run = std::atomic_load(&m_run);
    if (run && !run->m_done)
      return false;
    StopLocked();
    return true;
  }
  bool Closed() const { return m_closed; }
  std::exception_ptr Failure()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_failure;
  }
private:
  void StopLocked()
  {
    auto run = std::atomic_load(&m_run);
    if (run)
      run->Cancel();
    if (m_thread.joinable())
      m_thread.join();
    if (run && run->m_failure)
      m_failure = run->m_failure;
  }
  std::mutex m_mutex;
  std::thread m_thread;
  std::shared_ptr<Run> m_run; // atomic load/store; identity never reused
  std::atomic<bool> m_closed{false};
  std::exception_ptr m_failure;
};
