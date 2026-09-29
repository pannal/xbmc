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
    bool nativeFenced{false};
    std::thread::id owner;
    uint64_t ownerSerial{0};
    bool transferring{false};
    bool controlPending{false};
    bool controlActive{false};
    std::thread::id controller;
  };

public:
  struct NativeRequest
  {
    enum class Phase { PENDING, ACTIVE, COMPLETED, CANCELLED };
    explicit NativeRequest(std::shared_ptr<const void> session = {})
      : sessionIdentity(std::move(session)) {}
    const std::thread::id owner{std::this_thread::get_id()};
    const std::shared_ptr<const void> sessionIdentity;
    Phase phase{Phase::PENDING}; // registry lock
  };

  explicit CAMLSession(std::thread::id owner = std::this_thread::get_id())
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    m_state->owner = owner;
    m_state->controller = owner;
    m_state->displayFenced = s_displayPhase != DisplayPhase::READY;
    m_state->displayActive = s_displayPhase == DisplayPhase::MUTATING;
    m_state->nativeFenced = bool(s_nativeRequest);
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
      : m_state(std::move(other.m_state)), m_epoch(other.m_epoch), m_retirement(other.m_retirement), m_control(other.m_control)
    {
    }
    ~Permit()
    {
      if (m_state)
      {
        std::lock_guard<std::mutex> lock(m_state->mutex);
        --(m_retirement ? m_state->retiring : m_state->active);
        if (m_control)
          m_state->controlActive = false;
        m_state->idle.notify_all();
      }
    }
    explicit operator bool() const { return m_state != nullptr; }
    uint64_t Epoch() const { return m_epoch; }
    bool IsRetirement() const { return m_retirement; }
    bool IsControl() const { return m_control; }

  private:
    friend class CAMLSession;
    Permit(std::shared_ptr<State> state, uint64_t epoch, bool retirement)
      : m_state(std::move(state)), m_epoch(epoch), m_retirement(retirement)
    {
    }
    std::shared_ptr<State> m_state;
    uint64_t m_epoch{0};
    bool m_retirement{false};
    bool m_control{false};
  };

  struct OwnerTransfer
  {
    std::shared_ptr<const void> identity;
    uint64_t serial{0};
    std::thread::id from;
    std::thread::id to;
    explicit operator bool() const { return identity != nullptr; }
  };

  // Only the acknowledged owner can nominate its successor. Closing admission
  // is immediate; acceptance can wait for quiescence but never revokes a call.
  OwnerTransfer RequestOwner(std::thread::id successor)
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (m_state->owner != std::this_thread::get_id() || m_state->transferring ||
        successor == std::thread::id{} || successor == m_state->owner)
      return {};
    m_state->transferring = true;
    return {m_state, ++m_state->ownerSerial, m_state->owner, successor};
  }

  bool AcceptOwner(const OwnerTransfer& request, bool wait = false)
  {
    std::unique_lock<std::mutex> lock(m_state->mutex);
    if (request.to != std::this_thread::get_id())
      return false;
    if (wait)
      m_state->idle.wait(lock, [&] {
        return !MatchesTransfer(request) ||
               (!m_state->active && !m_state->retiring && !m_state->mutating);
      });
    if (!MatchesTransfer(request) || request.to != std::this_thread::get_id() ||
        m_state->active || m_state->retiring || m_state->mutating)
      return false;
    m_state->owner = request.to;
    m_state->transferring = false;
    m_state->idle.notify_all();
    return true;
  }

  // Ownership serials are separate from decoder epochs: reset may finish while
  // ownership is fenced. Transfer never reopens decoder admission and every
  // subsequent device call still needs the current decoder epoch. Only the
  // original owner can cancel; stale cancellation cannot undo acceptance.
  bool CancelOwner(const OwnerTransfer& request)
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    if (request.identity != m_state || request.serial != m_state->ownerSerial ||
        !m_state->transferring || request.from != m_state->owner ||
        request.from != std::this_thread::get_id())
      return false;
    m_state->transferring = false;
    m_state->idle.notify_all();
    return true;
  }

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
        !m_state->open || m_state->controlPending || m_state->controlActive || m_state->mutating || m_state->displayFenced || m_state->nativeFenced || epoch != m_state->epoch ||
        ((m_state->fenced || m_state->transferring) && !retirement))
      return {};
    ++(retirement ? m_state->retiring : m_state->active);
    return Permit(m_state, epoch, retirement);
  }

  // Main's explicitly separate control/read lease. It closes admission before
  // waiting for counted device operations, never changes the presentation owner,
  // and cannot coexist with presentation, retirement or decoder permits. No
  // native call or graphics operation executes while the state mutex is held.
  Permit AcquireControl(uint64_t epoch, bool wait = false)
  {
    std::unique_lock<std::mutex> lock(m_state->mutex);
    const auto available = [&] {
      return m_state->open && !m_state->fenced && !m_state->mutating &&
             !m_state->displayFenced && !m_state->nativeFenced &&
             !m_state->transferring && epoch == m_state->epoch;
    };
    if (std::this_thread::get_id() != m_state->controller ||
        m_state->controlPending || m_state->controlActive || !available())
      return {};
    m_state->controlPending = true;
    if (wait)
      m_state->idle.wait(lock, [&] {
        return !available() || (!m_state->active && !m_state->retiring);
      });
    m_state->controlPending = false;
    if (!available() || m_state->active || m_state->retiring)
      return {};
    m_state->controlActive = true;
    ++m_state->active;
    Permit permit(m_state, epoch, false);
    permit.m_control = true;
    return permit;
  }

  bool Matches(const Permit& permit, uint64_t epoch) const
  {
    return permit.m_state == m_state && permit.m_epoch == epoch;
  }

  Request Fence()
  {
    std::lock_guard<std::mutex> lock(m_state->mutex);
    m_state->fenced = true;
    m_state->idle.notify_all();
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

  bool BeginMutation(const Request& request,
                     const std::shared_ptr<NativeRequest>& native = {})
  {
    // Serialize the decision with pre-display admission, including registration
    // of a new codec while a display request is already fenced.
    std::lock_guard<std::mutex> registry(s_registryMutex);
    std::lock_guard<std::mutex> lock(m_state->mutex);
    // A codec mutation may nest only beneath its exact admitted outer token.
    // Other sessions/callers cannot use native fencing as permission to mutate.
    const bool ownsNative = native && native == s_nativeRequest &&
                            native->owner == std::this_thread::get_id() &&
                            native->sessionIdentity == m_state &&
                            native->phase == NativeRequest::Phase::ACTIVE;
    if (request.identity != m_state || request.serial != m_state->request || request.epoch != m_state->epoch ||
        m_state->active || m_state->retiring || m_state->mutating || m_state->displayActive ||
        (native && !ownsNative) || (m_state->nativeFenced && !ownsNative))
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
    m_state->idle.notify_all();
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
    if (!m_state->open || m_state->controlPending || m_state->controlActive || m_state->transferring || m_state->mutating || m_state->fenced || m_state->displayFenced || m_state->nativeFenced)
      return {};
    ++m_state->active;
    return Permit(m_state, m_state->epoch, false);
  }

  // A distinct outer native transaction. It never replaces a window's display
  // request or reopens a failed/unbound display. Admission precedes DV locks.
  static std::shared_ptr<NativeRequest> FenceNative(std::shared_ptr<const void> session = {})
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (s_nativeRequest)
      return {};
    s_nativeRequest = std::make_shared<NativeRequest>(std::move(session));
    ForSessions([](State& state) { state.nativeFenced = true; });
    return s_nativeRequest;
  }
  static bool TryBeginNative(const std::shared_ptr<NativeRequest>& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!request || request != s_nativeRequest || request->owner != std::this_thread::get_id() ||
        request->phase != NativeRequest::Phase::PENDING || s_displayPhase == DisplayPhase::MUTATING)
      return false;
    bool ready = true;
    ForSessions([&](State& state) { ready = ready && !state.active && !state.retiring && !state.mutating; });
    if (!ready)
      return false;
    request->phase = NativeRequest::Phase::ACTIVE;
    return true;
  }
  static bool EndNative(const std::shared_ptr<NativeRequest>& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!request || request != s_nativeRequest || request->owner != std::this_thread::get_id() ||
        request->phase != NativeRequest::Phase::ACTIVE)
      return false;
    request->phase = NativeRequest::Phase::COMPLETED;
    s_nativeRequest.reset();
    ForSessions([](State& state) { state.nativeFenced = false; state.idle.notify_all(); });
    return true;
  }
  static bool CancelNative(const std::shared_ptr<NativeRequest>& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    // Teardown may cancel queued work from main, but cannot revoke an active
    // transaction. The display gate will wait for its original owner to finish.
    if (!request || request != s_nativeRequest || request->phase != NativeRequest::Phase::PENDING)
      return false;
    request->phase = NativeRequest::Phase::CANCELLED;
    s_nativeRequest.reset();
    ForSessions([](State& state) { state.nativeFenced = false; state.idle.notify_all(); });
    return true;
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
    if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::PENDING ||
        (s_nativeRequest && s_nativeRequest->phase == NativeRequest::Phase::ACTIVE))
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

  // Explicit synchronous native work may nest only under this exact active
  // display transaction, on its owner. A queued unrelated native request stays
  // queued; this does not borrow or complete it.
  static bool BeginDisplayNative(const DisplayRequest& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::MUTATING)
      return false;
    ++s_displayNativeDepth;
    return true;
  }
  static bool EndDisplayNative(const DisplayRequest& request)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::MUTATING ||
        !s_displayNativeDepth)
      return false;
    --s_displayNativeDepth;
    return true;
  }

  // A failed bind/reset retains admission fencing, while ending the actual
  // mutation lets decoder reset/close invalidate its own old return obligations.
  static bool EndDisplay(const DisplayRequest& request, DisplayPhase phase)
  {
    std::lock_guard<std::mutex> registry(s_registryMutex);
    if (!MatchesDisplay(request) || s_displayNativeDepth ||
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
  bool MatchesTransfer(const OwnerTransfer& request) const
  {
    return request.identity == m_state && request.serial == m_state->ownerSerial &&
           m_state->transferring &&
           request.from == m_state->owner;
  }

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
  static inline unsigned int s_displayNativeDepth{0};
  static inline uint64_t s_displaySerial{0};
  static inline std::thread::id s_displayOwner;
  static inline std::shared_ptr<NativeRequest> s_nativeRequest;
  static inline bool s_cancelDisplayReady{true};
  static inline DisplayPhase s_displayPhase{DisplayPhase::READY};
  const std::shared_ptr<State> m_state{std::make_shared<State>()};
};
