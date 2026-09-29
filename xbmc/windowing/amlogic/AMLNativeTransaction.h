/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"

// Retained on the original caller. Pending retries never wait under native or
// settings locks. Display nesting requires an explicit exact parent receipt;
// no ambient same-thread native token may be borrowed.
class CAMLNativeTransaction
{
public:
  explicit CAMLNativeTransaction(CAMLSession::DisplayRequest display = {}) : m_display(display) {}
  CAMLNativeTransaction(const CAMLNativeTransaction&) = delete;
  CAMLNativeTransaction& operator=(const CAMLNativeTransaction&) = delete;
  ~CAMLNativeTransaction()
  {
    if (m_display)
    {
      if (m_active)
        CAMLSession::EndDisplayNative(m_display);
    }
    else if (m_active)
      CAMLSession::EndNative(m_request);
    else
      CAMLSession::CancelNative(m_request);
  }

  bool TryBegin()
  {
    if (std::this_thread::get_id() != m_owner)
      return false;
    if (m_active)
      return true;
    if (m_display)
      return m_active = CAMLSession::BeginDisplayNative(m_display);
    if (!m_request)
      m_request = CAMLSession::FenceNative();
    return m_active = CAMLSession::TryBeginNative(m_request);
  }

private:
  const std::thread::id m_owner{std::this_thread::get_id()};
  const CAMLSession::DisplayRequest m_display;
  std::shared_ptr<CAMLSession::NativeRequest> m_request;
  bool m_active{false};
};
