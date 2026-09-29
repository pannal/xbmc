/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

// A bounded mailbox belonging to one original renderer owner. The application
// pumps it on main even before that player has registered a render loop. No
// request resolves through whichever player happens to be current at dispatch.
class CRenderLifecycle
{
public:
  enum class Status { PENDING, EXECUTING, COMPLETED, FAILED, CANCELLED };
  struct Request
  {
    const uint64_t session;
    const uint64_t serial;
    std::atomic<Status> status{Status::PENDING};
    std::function<std::optional<bool>()> execute;
    std::mutex mutex;
    std::condition_variable changed;

    Request(uint64_t generation, uint64_t id, std::function<std::optional<bool>()> action)
      : session(generation), serial(id), execute(std::move(action)) {}
    bool Wait(std::chrono::milliseconds timeout)
    {
      std::unique_lock<std::mutex> lock(mutex);
      changed.wait_for(lock, timeout, [&] {
        return status != Status::PENDING && status != Status::EXECUTING;
      });
      return status == Status::COMPLETED;
    }
    void Finish(Status result)
    {
      std::lock_guard<std::mutex> lock(mutex);
      execute = {};
      status = result;
      changed.notify_all();
    }
  };

  explicit CRenderLifecycle(std::thread::id owner = std::this_thread::get_id()) : m_owner(owner) {}

  static std::shared_ptr<CRenderLifecycle> Create(std::thread::id owner = std::this_thread::get_id())
  {
    auto mailbox = std::make_shared<CRenderLifecycle>(owner);
    std::lock_guard<std::mutex> lock(s_mutex);
    s_mailboxes.emplace_back(mailbox);
    return mailbox;
  }

  std::shared_ptr<Request> Submit(std::function<std::optional<bool>()> action)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_closed || m_requests.size() + (m_executing ? 1 : 0) >= 4)
      return {};
    auto request = std::make_shared<Request>(m_session, ++m_serial, std::move(action));
    m_requests.push_back(request);
    return request;
  }

  // Main-only executor. Never hold the mailbox lock while entering renderer,
  // GL, driver or application code. Timeout leaves this exact request pinned.
  void Process()
  {
    if (std::this_thread::get_id() != m_owner)
      return;
    for (unsigned int dispatched = 0; dispatched < 4; ++dispatched)
    {
      std::shared_ptr<Request> request;
      {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_executing || m_requests.empty() || m_closed)
          return;
        request = m_requests.front();
        m_requests.pop_front();
        m_executing = true;
        request->status = Status::EXECUTING;
      }
      try
      {
        const auto complete = request->execute();
        std::lock_guard<std::mutex> lock(m_mutex);
        m_executing = false;
        if (!complete)
        {
          // Retain this exact continuation ahead of dependent mutations. Retry
          // on a later main pump, never spin while admission needs other owners.
          request->status = Status::PENDING;
          m_requests.push_front(request);
          m_idle.notify_all();
          return;
        }
        request->Finish(*complete ? Status::COMPLETED : Status::FAILED);
        m_idle.notify_all();
      }
      catch (...)
      {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_executing = false;
        request->Finish(Status::FAILED);
        m_idle.notify_all();
        throw;
      }
    }
  }

  void AdvanceSession()
  {
    if (std::this_thread::get_id() != m_owner)
      return;
    std::lock_guard<std::mutex> lock(m_mutex);
    ++m_session;
    for (const auto& request : m_requests)
      request->Finish(Status::CANCELLED);
    m_requests.clear();
  }

  // CPU-only retirement after the target's main-owned UnInit acknowledgment.
  // Fence queued work, then retain the target through an active callback. A
  // late registry entry cannot call into the old player after this returns.
  bool Close()
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    if (m_executing && std::this_thread::get_id() == m_owner)
      return false; // An executor cannot retire its own active target.
    m_closed = true;
    m_idle.wait(lock, [&] { return !m_executing; });
    ++m_session;
    for (const auto& request : m_requests)
      request->Finish(Status::CANCELLED);
    m_requests.clear();
    return true;
  }

  static void ProcessAll()
  {
    std::vector<std::shared_ptr<CRenderLifecycle>> pending;
    {
      std::lock_guard<std::mutex> lock(s_mutex);
      for (auto it = s_mailboxes.begin(); it != s_mailboxes.end();)
      {
        if (auto mailbox = it->lock())
        {
          pending.push_back(std::move(mailbox));
          ++it;
        }
        else
          it = s_mailboxes.erase(it);
      }
    }
    for (const auto& mailbox : pending)
      mailbox->Process();
  }

private:
  const std::thread::id m_owner;
  std::mutex m_mutex;
  std::condition_variable m_idle;
  std::deque<std::shared_ptr<Request>> m_requests;
  uint64_t m_session{1};
  uint64_t m_serial{0};
  bool m_closed{false};
  bool m_executing{false};
  static inline std::mutex s_mutex;
  static inline std::vector<std::weak_ptr<CRenderLifecycle>> s_mailboxes;
};
