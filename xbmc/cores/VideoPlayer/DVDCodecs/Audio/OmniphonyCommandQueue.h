/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <vector>

/*!
 * \brief Commands framed for the helper that have not been written to it yet.
 *
 * The bytes are kept in one piece, so the pump thread writes as much as the pipe
 * will take in a single call - a TrueHD stream is twelve hundred commands a
 * second, and one write each would cost the pump more than the audio does.
 * Alongside them is a record of where each command starts and ends, which is
 * what a seek needs: the helper works through its input strictly in order, so
 * a reset queued behind seconds of audio is answered only once all of it has
 * been decoded and rendered, and every block of that is thrown away on arrival.
 * With the boundaries known, the audio nobody will hear can be taken back
 * before it is sent instead.
 *
 * A command the pump has started writing is never taken back: the helper has
 * part of it, and a command cut short would frame everything after it wrong.
 */
class COmniphonyCommandQueue
{
public:
  enum class Kind
  {
    Audio, //!< input to render: stale once a reset is queued behind it
    Reset, //!< a reset: pointless once another is queued behind it
    Control //!< anything else, which is kept whatever follows it
  };

  /*!
   * \brief Queue one command, its header and payload, as it is to be written.
   *
   * \p us is how much audio it carries, in microseconds - 0 where there is none
   * or it cannot be told, which leaves it out of Us() but not out of Bytes().
   */
  void Push(Kind kind,
            const uint8_t* header,
            size_t headerLen,
            const uint8_t* payload,
            size_t payloadLen,
            double us);

  //! \brief The bytes not yet written, oldest first, Bytes() of them.
  const uint8_t* Data() const { return m_bytes.data() + m_sent; }
  size_t Bytes() const { return m_bytes.size() - m_sent; }

  //! \brief Audio in the commands not yet written in full, in microseconds.
  double Us() const { return m_us; }

  //! \brief \p n of the bytes Data() offered have been written.
  void Written(size_t n);

  /*!
   * \brief Take back every command not yet started that a reset about to be
   * queued makes pointless: all of the audio, and the resets it supersedes.
   *
   * Control commands stay, in order. Returns how many resets were taken back,
   * which the caller has counted as sent and must now count as answered.
   */
  unsigned int DropStale();

  void Clear();

private:
  struct Command
  {
    uint64_t start; //!< offset in the stream of everything ever queued
    uint64_t end;
    Kind kind;
    double us;
  };

  std::vector<uint8_t> m_bytes;
  size_t m_sent{0}; //!< how much of m_bytes has been written
  uint64_t m_base{0}; //!< the stream offset of m_bytes[0]
  std::deque<Command> m_commands; //!< not yet written in full, oldest first
  double m_us{0.0};
};
