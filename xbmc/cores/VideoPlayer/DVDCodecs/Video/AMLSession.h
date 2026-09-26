/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>

// Device admission is independent of allocation lifetime and successful-open
// generation. The serialized decoder owner mutates; presentation/returns obtain
// counted permits before claiming a buffer. No mutex survives a device call.
class CAMLSession
{
  struct State
  {
    std::mutex mutex;
    std::condition_variable idle;
    uint64_t epoch{1};
    uint64_t request{0};
    unsigned int active{0};
    unsigned int retiring{0};
    bool open{false};
    bool fenced{true};
    bool mutating{false};
  };

public:
  explicit CAMLSession(std::thread::id owner = std::this_thread::get_id()) : m_owner(owner) {}

  class Permit
  {
  public:
    Permit() = default;
    Permit(const Permit&) = delete;
    Permit& operator=(const Permit&) = delete;
    Permit(Permit&& other) noexcept
      : m_state(std::move(other.m_state)), m_epoch(other.m_epoch), m_retirement(other.m_retirement)
    {
    }
    ~Permit()
    {
      if (m_state)
      {
        std::lock_guard<std::mutex> lock(m_state->mutex);
        --(m_retirement ? m_state->retiring : m_state->active);
        m_state->idle.notify_all();
      }
    }
    explicit operator bool() const { return m_state != nullptr; }
    uint64_t Epoch() const { return m_epoch; }
    bool IsRetirement() const { return m_retirement; }

  private:
    friend class CAMLSession;
    Permit(std::shared_ptr<State> state, uint64_t epoch, bool retirement)
      : m_state(std::move(state)), m_epoch(epoch), m_retirement(retirement)
    {
    }
    std::shared_ptr<State> m_state;
    uint64_t m_epoch{0};
    bool m_retirement{false};
  };

  struct Request
  {
    std::shared_ptr<const void> identity;
    uint64_t serial{0};
    uint64_t epoch{0};
  };

  uint64_t Epoch() const
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return m_state->epoch;
  }

  Permit Acquire(uint64_t epoch, bool retirement = false)
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if ((!retirement && std::this_thread::get_id() != m_owner) ||
        !m_state->open || m_state->mutating || epoch != m_state->epoch ||
        (m_state->fenced && !retirement))
      return {};
    ++(retirement ? m_state->retiring : m_state->active);
    return Permit(m_state, epoch, retirement);
  }

  bool Matches(const Permit& permit, uint64_t epoch) const
  {
    return permit.m_state == m_state && permit.m_epoch == epoch;
  }

  Request Fence()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    m_state->fenced = true;
    return {m_state, ++m_state->request, m_state->epoch};
  }

  // A failed/expired wait is only a pending observation. It does not reopen
  // admission, revoke permits, or authorize mutation/deletion/another owner.
  bool Wait(const Request& request, std::chrono::milliseconds timeout)
  {
    std::unique_lock<std::mutex> lock(m_state->mutex);
    return m_state->idle.wait_for(lock, timeout, [&] {
      return request.identity == m_state && request.serial == m_state->request && request.epoch == m_state->epoch &&
             m_state->active == 0 && m_state->retiring == 0;
    });
  }

  bool BeginMutation(const Request& request)
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (request.identity != m_state || request.serial != m_state->request || request.epoch != m_state->epoch ||
        m_state->active || m_state->retiring || m_state->mutating)
      return false;
    m_state->mutating = true;
    // Mutation now owns invalidation of outstanding old device indices. Late
    // returns may retire memory, but cannot QBUF into the next decoder epoch.
    ++m_state->epoch;
    return true;
  }

  bool Complete(const Request& request, bool open)
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (request.identity != m_state || request.serial != m_state->request || !m_state->mutating ||
        request.epoch + 1 != m_state->epoch)
      return false;
    m_state->mutating = false;
    m_state->open = open;
    m_state->fenced = !open;
    return true;
  }

private:
  const std::thread::id m_owner;
  const std::shared_ptr<State> m_state{std::make_shared<State>()};
};
