/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

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
    bool displayFenced{false};
    bool displayActive{false};
    std::thread::id owner;
  };

public:
  explicit CAMLSession(std::thread::id owner = std::this_thread::get_id())
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    m_state->owner = owner;
    m_state->displayFenced = s_displayPhase != DisplayPhase::READY;
    m_state->displayActive = s_displayPhase == DisplayPhase::MUTATING;
    PruneSessions();
    s_sessions.emplace_back(m_state);
  }

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
    if ((!retirement && std::this_thread::get_id() != m_state->owner) ||
        !m_state->open || m_state->mutating || m_state->displayFenced || epoch != m_state->epoch ||
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
    // Serialize the decision with pre-display admission, including registration
    // of a new codec while a display request is already fenced.
    std::lock_guard<std::mutex> registry(s_registryMutex);
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (request.identity != m_state || request.serial != m_state->request || request.epoch != m_state->epoch ||
        m_state->active || m_state->retiring || m_state->mutating || m_state->displayActive)
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

  // Rejection during an ordinary display transition is temporary. Only actual
  // decoder mutation/close owns invalidation of these driver indices.
  bool IsInvalidated(uint64_t epoch) const
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return !m_state->open || epoch != m_state->epoch || m_state->mutating;
  }

  bool DisplayBlocked() const
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    return m_state->displayFenced;
  }

  Permit AcquireDecoder()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (!m_state->open || m_state->mutating || m_state->fenced || m_state->displayFenced)
      return {};
    ++m_state->active;
    return Permit(m_state, m_state->epoch, false);
  }

  enum class DisplayPhase { READY, PENDING, MUTATING, WAITING_FOR_RESET, FAILED };
  struct DisplayRequest
  {
    uint64_t serial{0};
    std::thread::id owner;
    explicit operator bool() const { return serial != 0; }
  };

  static DisplayRequest FenceDisplay()
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (s_displayPhase == DisplayPhase::MUTATING)
      return {};
    if (s_displayPhase != DisplayPhase::PENDING)
      s_cancelDisplayReady = s_displayPhase == DisplayPhase::READY;
    s_displayPhase = DisplayPhase::PENDING;
    s_displayOwner = std::this_thread::get_id();
    ++s_displaySerial;
    ForSessions([](State& state) { state.displayFenced = true; });
    return {s_displaySerial, s_displayOwner};
  }

  static bool TryBeginDisplay(const DisplayRequest& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::PENDING)
      return false;
    bool ready = true;
    ForSessions([&](State& state) {
      ready = ready && !state.active && !state.retiring && !state.mutating;
    });
    if (!ready)
      return false;
    ForSessions([](State& state) { state.displayActive = true; });
    s_displayPhase = DisplayPhase::MUTATING;
    return true;
  }

  // A failed bind/reset retains admission fencing, while ending the actual
  // mutation lets decoder reset/close invalidate its own old return obligations.
  static bool EndDisplay(const DisplayRequest& request, DisplayPhase phase)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) ||
        (s_displayPhase != DisplayPhase::MUTATING &&
         s_displayPhase != DisplayPhase::WAITING_FOR_RESET) ||
        (phase != DisplayPhase::READY && phase != DisplayPhase::WAITING_FOR_RESET &&
         phase != DisplayPhase::FAILED))
      return false;
    s_displayPhase = phase;
    ForSessions([&](State& state) {
      state.displayActive = false;
      state.displayFenced = phase != DisplayPhase::READY;
      state.idle.notify_all();
    });
    return true;
  }

  static bool CancelDisplay(const DisplayRequest& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::PENDING)
      return false;
    s_displayPhase = s_cancelDisplayReady ? DisplayPhase::READY : DisplayPhase::FAILED;
    ForSessions([](State& state) { state.displayFenced = !s_cancelDisplayReady; });
    return true;
  }

private:
  static bool MatchesDisplay(const DisplayRequest& request)
  {
    return request.serial != 0 && request.serial == s_displaySerial &&
           request.owner == s_displayOwner && request.owner == std::this_thread::get_id();
  }

  static void PruneSessions()
  {
    s_sessions.erase(std::remove_if(s_sessions.begin(), s_sessions.end(),
                                   [](const auto& state) { return state.expired(); }),
                     s_sessions.end());
  }

  template<class Action> static void ForSessions(Action action)
  {
    PruneSessions();
    for (auto& weak : s_sessions)
    {
      if (auto state = weak.lock())
      {
        std::lock_guard<std::mutex> lock(state->mutex);
        action(*state);
      }
    }
  }

  static inline std::mutex s_registryMutex;
  static inline std::vector<std::weak_ptr<State>> s_sessions;
  static inline uint64_t s_displaySerial{0};
  static inline std::thread::id s_displayOwner;
  static inline bool s_cancelDisplayReady{true};
  static inline DisplayPhase s_displayPhase{DisplayPhase::READY};
  const std::shared_ptr<State> m_state{std::make_shared<State>()};
};
