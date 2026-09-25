/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>
#include <deque>

class CAEStreamInfo;

/*!
 * \brief How far a source timestamp may stray before it counts as a gap, in
 * microseconds.
 *
 * Below this the source is taken to be continuous and its timestamps are left
 * to the engine's sample count, which is exact. Timestamps carry rounding - a
 * Matroska one is whole milliseconds - so a threshold of zero would chase that
 * rounding. Above it a gap, or an overlap, is followed. Small gaps are not lost
 * either: they are measured against the last timestamp followed rather than
 * the previous one, so they add up until together they cross this.
 */
constexpr double OMNI_TIMELINE_GAP_US = 20000.0;

/*!
 * \brief Keeps the source's gaps in the timestamps of the audio rendered from it.
 *
 * The helper protocol carries no timestamps, and the engine stamps what it
 * renders with a count of the samples it has produced since its last reset. The
 * codec pins that count to the demuxer's timeline once, at the first timestamp,
 * which is exact for as long as the source is continuous. A gap in the source -
 * packets lost in a broadcast, a hole in a recording - is not seen by the
 * count: the audio after it would carry timestamps as early as the gap is long,
 * for the rest of the stream.
 *
 * So the input is followed on the engine's clock. Each access unit fed to the
 * helper advances a position by the time it lasts, and each timestamp is
 * compared with where the last one followed puts it. A disagreement beyond
 * OMNI_TIMELINE_GAP_US is recorded as a correction at that position, and the
 * output picks it up when the engine's count reaches it - after everything
 * already in flight has gone out at the timestamps it was given.
 *
 * A timestamp is matched to its access unit by where both sit in the byte
 * stream, not by the order they arrive in. The parser holds a unit back until
 * it has seen the start of the next one, so the unit fed while a packet is
 * being added is usually the previous packet's; counted by arrival, every
 * timestamp would land one unit early.
 *
 * Positions are microseconds since the engine was last reset, the unit the
 * engine stamps its output in. Offsets are bytes into the stream since the
 * parser was last reset, which is always together with this.
 */
class COmniphonyTimeline
{
public:
  //! The engine's count and the parser start again: forget everything.
  void Reset();

  /*!
   * \brief The packet starting \p offset bytes into the stream carries \p pts.
   *
   * It belongs to the first access unit that starts at or after that byte.
   * NOPTS is ignored.
   */
  void Mark(uint64_t offset, double pts);

  /*!
   * \brief The access unit starting \p offset bytes into the stream, lasting
   * \p us microseconds, has just been fed.
   *
   * A length of zero or less means it is not known. Gaps are then no longer
   * followed until the next Reset(), because a position that has stopped
   * advancing with the audio would turn every later timestamp into a gap.
   */
  void Feed(uint64_t offset, double us);

  /*!
   * \brief The corrections due by the engine position \p at, summed.
   *
   * Those returned are consumed, so each is applied once, by the block that
   * reaches it first.
   */
  double Take(double at);

private:
  void Stamp(double pts);

  struct Pending
  {
    uint64_t offset;
    double pts;
  };

  struct Correction
  {
    double at;
    double delta;
  };

  std::deque<Pending> m_marks;
  std::deque<Correction> m_corrections;
  double m_position{0.0};
  double m_basePts{0.0};
  double m_basePosition{0.0};
  bool m_following{true};
  bool m_based{false};
};

/*!
 * \brief How long the access unit the parser has just produced lasts, in
 * microseconds, or 0 when that cannot be told.
 *
 * From the stream's own header rather than CAEStreamInfo::GetDuration(), which
 * answers for passthrough packing: a TrueHD unit is 1/1200 of a second, not
 * the 20 ms of the MAT frame it packs into, and a DTS-HD MA frame is timed by
 * its core, not by the samples its extension counts at another rate.
 */
double OmniphonyAccessUnitUs(const CAEStreamInfo& info);
