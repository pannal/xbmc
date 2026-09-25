/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyTimeline.h"

#include "cores/AudioEngine/Utils/AEStreamInfo.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"

#include <cmath>

/*
 * Apart from the codec for the reason OmniphonyRateCheck.cpp is: a test can
 * link it without a helper process behind it.
 */

void COmniphonyTimeline::Reset()
{
  m_marks.clear();
  m_corrections.clear();
  m_position = 0.0;
  m_basePts = 0.0;
  m_basePosition = 0.0;
  m_following = true;
  m_based = false;
}

void COmniphonyTimeline::Mark(uint64_t offset, double pts)
{
  if (pts == DVD_NOPTS_VALUE || !m_following)
    return;

  // Only a stream the parser cannot frame leaves these unclaimed, and there is
  // no unit to follow in one anyway.
  if (m_marks.size() >= 256)
    m_marks.pop_front();
  m_marks.push_back({offset, pts});
}

void COmniphonyTimeline::Feed(uint64_t offset, double us)
{
  // A packet's timestamp belongs to the first unit starting at or after its
  // first byte. Several can be waiting for the same unit only when packets
  // ended before a unit began in them; the latest of those is the nearest.
  double pts = DVD_NOPTS_VALUE;
  while (!m_marks.empty() && m_marks.front().offset <= offset)
  {
    pts = m_marks.front().pts;
    m_marks.pop_front();
  }
  if (pts != DVD_NOPTS_VALUE)
    Stamp(pts);

  if (us > 0.0)
    m_position += us;
  else
    m_following = false;
}

void COmniphonyTimeline::Stamp(double pts)
{
  if (!m_following)
    return;

  // The first timestamp is the one the codec anchors the engine's count to, so
  // it is where this starts measuring from as well.
  if (!m_based)
  {
    m_based = true;
    m_basePts = pts;
    m_basePosition = m_position;
    return;
  }

  const double delta = pts - (m_basePts + (m_position - m_basePosition));
  if (std::abs(delta) < OMNI_TIMELINE_GAP_US)
    return;

  // Measured from here on, so a correction is never counted twice.
  m_corrections.push_back({m_position, delta});
  m_basePts = pts;
  m_basePosition = m_position;
}

double COmniphonyTimeline::Take(double at)
{
  double shift = 0.0;
  while (!m_corrections.empty() && m_corrections.front().at <= at)
  {
    shift += m_corrections.front().delta;
    m_corrections.pop_front();
  }
  return shift;
}

namespace
{
// A DTS core frame is a whole number of 32-sample blocks. The parser keeps the
// length only folded into m_dtsPeriod, the IEC 61937 repetition period, and
// rounds while it does, so it is recovered to the nearest block.
double DtsCoreSamples(double period, double scale)
{
  return std::round(period / scale / 32.0) * 32.0;
}
} // namespace

double OmniphonyAccessUnitUs(const CAEStreamInfo& info)
{
  if (info.m_sampleRate == 0)
    return 0.0;

  const double rate = static_cast<double>(info.m_sampleRate);
  double samples = 0.0;
  switch (info.m_type)
  {
    case CAEStreamInfo::STREAM_TYPE_TRUEHD:
      // 40 samples at the base rate of the family: an access unit is 1/1200 s
      // at 48, 96 and 192 kHz alike.
      return 40.0 * 1000000.0 / (info.m_sampleRate % 44100 == 0 ? 44100.0 : 48000.0);

    case CAEStreamInfo::STREAM_TYPE_AC3:
      samples = 1536.0;
      break;

    case CAEStreamInfo::STREAM_TYPE_EAC3:
      // 256 samples a block; the parser keeps 6 / blocks, and has already
      // folded any dependent substream into this unit.
      if (info.m_repeat == 0)
        return 0.0;
      samples = 1536.0 / info.m_repeat;
      break;

    case CAEStreamInfo::STREAM_TYPE_DTSHD_MA:
      // 192 kHz x 4 (8 channels) x core samples / core rate.
      samples = DtsCoreSamples(info.m_dtsPeriod * rate, 192000.0 * 4.0);
      break;

    case CAEStreamInfo::STREAM_TYPE_DTSHD:
      // 192 kHz x 1 (2 channels) x core samples / core rate.
      samples = DtsCoreSamples(info.m_dtsPeriod * rate, 192000.0);
      break;

    case CAEStreamInfo::STREAM_TYPE_DTS_512:
    case CAEStreamInfo::STREAM_TYPE_DTS_1024:
    case CAEStreamInfo::STREAM_TYPE_DTS_2048:
    case CAEStreamInfo::STREAM_TYPE_DTSHD_CORE:
      // The core samples themselves.
      samples = DtsCoreSamples(info.m_dtsPeriod, 1.0);
      break;

    default:
      return 0.0;
  }

  return samples * 1000000.0 / rate;
}
