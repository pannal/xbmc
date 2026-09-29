/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "windowing/amlogic/AMLNativeTransaction.h"

#include <memory>
#include <mutex>
#include <optional>
#include <utility>

// The original player's message loop owns Queue/Continue/BeginStream/EndStream.
// Producers only capture/invalidate the published immutable stream identity.
// Pending work never resolves a current application player or a newer stream.
template<class Stream>
class CPlayerNativeAction
{
public:
  struct Intent
  {
    int action{0};
    std::shared_ptr<const Stream> stream;
  };
  Intent Capture(int action) const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return {action, m_stream};
  }
  void Invalidate()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_stream.reset();
  }
  void BeginStream(Stream stream)
  {
    Cancel();
    std::lock_guard<std::mutex> lock(m_mutex);
    m_stream = std::make_shared<const Stream>(std::move(stream));
  }
  void EndStream()
  {
    Invalidate();
    Cancel();
  }
  void Queue(Intent intent)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (!intent.stream || intent.stream != m_stream)
      return;
    Cancel(); // newest unstarted action supersedes the previous request/token
    m_pending = std::move(intent);
  }
  void Suspend() { m_native.reset(); } // retain intent, release its fence before lifecycle work
  void Cancel()
  {
    Suspend();
    m_pending.reset();
  }
  template<class Apply> bool Continue(Apply apply)
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    if (!m_pending)
      return true;
    if (m_pending->stream != m_stream)
    {
      Cancel();
      return true;
    }
    if (!m_native)
      m_native = std::make_unique<CAMLNativeTransaction>();
    if (!m_native->TryBegin())
      return false;
    // Consume before effects. Exceptions or nested lifecycle callbacks cannot
    // replay this request; the local admission and immutable inputs finish here.
    auto intent = std::move(*m_pending);
    m_pending.reset();
    auto native = std::move(m_native);
    lock.unlock(); // no producer/stream lock across effects or callbacks
    apply(intent);
    return true;
  }
private:
  mutable std::mutex m_mutex; // invalidation and admission decision are atomic together
  std::shared_ptr<const Stream> m_stream;
  std::optional<Intent> m_pending; // original message-loop owner only
  std::unique_ptr<CAMLNativeTransaction> m_native;
};
