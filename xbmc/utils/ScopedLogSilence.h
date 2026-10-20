/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

// Optional operations accepting arbitrary credential-bearing inputs must not
// expose them through lower-layer diagnostic messages. This scope affects only
// CLog on its calling thread and restores the prior state during unwinding.
class CScopedLogSilence
{
public:
  CScopedLogSilence() { ++Depth(); }
  ~CScopedLogSilence() { --Depth(); }
  CScopedLogSilence(const CScopedLogSilence&) = delete;
  CScopedLogSilence& operator=(const CScopedLogSilence&) = delete;
  static bool Active() { return Depth() != 0; }

private:
  static unsigned int& Depth()
  {
    static thread_local unsigned int depth = 0;
    return depth;
  }
};
