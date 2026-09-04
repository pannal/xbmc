/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Utils/AEStreamInfo.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyTimeline.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"

#include <gtest/gtest.h>

/*
 * Offsets below are bytes into the stream, as the codec counts them, and every
 * unit is 100 bytes long. Durations and timestamps are microseconds.
 */

namespace
{
constexpr uint64_t UNIT = 100;

// Feeds `count` units of `us` each, one packet per unit, the packets stamped
// from `pts` on as a continuous source would stamp them. Returns the pts of the
// packet after the last.
double FeedContinuous(
    COmniphonyTimeline& timeline, uint64_t& offset, double pts, int count, double us)
{
  for (int i = 0; i < count; ++i)
  {
    timeline.Mark(offset, pts);
    timeline.Feed(offset, us);
    offset += UNIT;
    pts += us;
  }
  return pts;
}

CAEStreamInfo Info(CAEStreamInfo::DataType type, unsigned int rate)
{
  CAEStreamInfo info;
  info.m_type = type;
  info.m_sampleRate = rate;
  return info;
}
} // namespace

TEST(TestOmniphonyTimeline, ContinuousSourceNeedsNoCorrection)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  FeedContinuous(timeline, offset, 1000000.0, 500, 20000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, GapMovesTheOutputWhereItFalls)
{
  // Two 20 ms blocks stamped 1.000 s and 1.270 s: the second belongs 250 ms
  // later than the engine's count puts it, and from the second block on.
  COmniphonyTimeline timeline;
  timeline.Mark(0, 1000000.0);
  timeline.Feed(0, 20000.0);
  timeline.Mark(UNIT, 1270000.0);
  timeline.Feed(UNIT, 20000.0);

  EXPECT_EQ(timeline.Take(0.0), 0.0);
  EXPECT_EQ(timeline.Take(19999.0), 0.0);
  EXPECT_DOUBLE_EQ(timeline.Take(20000.0), 250000.0);
  // Taken once only.
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, LaterGapsAreMeasuredFromTheLastOneFollowed)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  double pts = FeedContinuous(timeline, offset, 0.0, 10, 32000.0);
  pts = FeedContinuous(timeline, offset, pts + 100000.0, 10, 32000.0);
  FeedContinuous(timeline, offset, pts + 60000.0, 10, 32000.0);

  EXPECT_DOUBLE_EQ(timeline.Take(10 * 32000.0), 100000.0);
  EXPECT_DOUBLE_EQ(timeline.Take(20 * 32000.0), 60000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, TimestampRoundingIsNotAGap)
{
  // Matroska keeps whole milliseconds, so a 32 ms unit at an odd start reads
  // up to a millisecond either side of where it is.
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  for (int i = 0; i < 300; ++i, offset += UNIT)
  {
    const double exact = 1000333.0 + i * 32000.0;
    timeline.Mark(offset, (i % 3 == 0) ? exact + 999.0 : exact - 999.0);
    timeline.Feed(offset, 32000.0);
  }
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, SmallGapsAddUpUntilTheyCount)
{
  // Three 8 ms holes: none is a gap alone, the three together are.
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  double pts = FeedContinuous(timeline, offset, 0.0, 5, 10000.0);
  pts = FeedContinuous(timeline, offset, pts + 8000.0, 5, 10000.0);
  pts = FeedContinuous(timeline, offset, pts + 8000.0, 5, 10000.0);
  FeedContinuous(timeline, offset, pts + 8000.0, 5, 10000.0);

  EXPECT_EQ(timeline.Take(15 * 10000.0 - 1.0), 0.0);
  EXPECT_DOUBLE_EQ(timeline.Take(15 * 10000.0), 24000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, OverlapMovesTheOutputBack)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  const double pts = FeedContinuous(timeline, offset, 0.0, 5, 32000.0);
  FeedContinuous(timeline, offset, pts - 64000.0, 5, 32000.0);
  EXPECT_DOUBLE_EQ(timeline.Take(1e12), -64000.0);
}

TEST(TestOmniphonyTimeline, TimestampWaitsForItsOwnUnit)
{
  // The parser holds a unit back until it has seen the next one's header, so
  // packet n is marked while unit n-1 is the one being fed. Matched by arrival
  // this is one unit out on every packet; matched by offset it is exact.
  COmniphonyTimeline timeline;
  double pts = 0.0;
  timeline.Mark(0, pts);
  for (int n = 1; n < 100; ++n)
  {
    pts += 32000.0;
    timeline.Mark(n * UNIT, pts);
    timeline.Feed((n - 1) * UNIT, 32000.0);
  }
  timeline.Feed(99 * UNIT, 32000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, UnitStartingInsideAPacketTakesItsTimestamp)
{
  // A packet cut mid-unit: its timestamp describes the first unit that starts
  // in it, not the one it opens in the middle of.
  COmniphonyTimeline timeline;
  timeline.Mark(0, 0.0);
  timeline.Feed(0, 32000.0);
  timeline.Mark(UNIT + 40, 32000.0 + 300000.0); // cut 40 bytes into unit 1
  timeline.Feed(UNIT, 32000.0); // unit 1: started before the packet
  timeline.Feed(2 * UNIT, 32000.0); // unit 2: the first to start in it

  EXPECT_EQ(timeline.Take(2 * 32000.0 - 1.0), 0.0);
  // Unit 2 is at 64 ms on the count and was stamped 332 ms: 268 ms late.
  EXPECT_DOUBLE_EQ(timeline.Take(2 * 32000.0), 268000.0);
}

TEST(TestOmniphonyTimeline, NoTimestampIsNoInformation)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  const double pts = FeedContinuous(timeline, offset, 0.0, 5, 32000.0);
  for (int i = 0; i < 5; ++i, offset += UNIT)
  {
    timeline.Mark(offset, DVD_NOPTS_VALUE);
    timeline.Feed(offset, 32000.0);
  }
  FeedContinuous(timeline, offset, pts + 5 * 32000.0, 5, 32000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, UnknownLengthStopsFollowingUntilReset)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  double pts = FeedContinuous(timeline, offset, 0.0, 5, 32000.0);
  timeline.Mark(offset, pts);
  timeline.Feed(offset, 0.0);
  offset += UNIT;
  FeedContinuous(timeline, offset, pts + 500000.0, 5, 32000.0);
  EXPECT_EQ(timeline.Take(1e12), 0.0);

  timeline.Reset();
  offset = 0;
  pts = FeedContinuous(timeline, offset, 0.0, 5, 32000.0);
  FeedContinuous(timeline, offset, pts + 100000.0, 5, 32000.0);
  EXPECT_DOUBLE_EQ(timeline.Take(1e12), 100000.0);
}

TEST(TestOmniphonyTimeline, ResetForgetsCorrectionsNotYetTaken)
{
  COmniphonyTimeline timeline;
  uint64_t offset = 0;
  const double pts = FeedContinuous(timeline, offset, 0.0, 5, 32000.0);
  FeedContinuous(timeline, offset, pts + 100000.0, 5, 32000.0);
  timeline.Reset();
  EXPECT_EQ(timeline.Take(1e12), 0.0);
}

TEST(TestOmniphonyTimeline, AccessUnitDurationsComeFromTheStream)
{
  // TrueHD: 40 samples at the family's base rate, whatever the stream's rate.
  const auto truehd = [](unsigned int rate)
  { return OmniphonyAccessUnitUs(Info(CAEStreamInfo::STREAM_TYPE_TRUEHD, rate)); };
  EXPECT_NEAR(truehd(48000), 833.333, 0.001);
  EXPECT_NEAR(truehd(192000), 833.333, 0.001);
  EXPECT_NEAR(truehd(44100), 907.029, 0.001);
  EXPECT_NEAR(truehd(88200), 907.029, 0.001);

  EXPECT_DOUBLE_EQ(OmniphonyAccessUnitUs(Info(CAEStreamInfo::STREAM_TYPE_AC3, 48000)), 32000.0);

  // E-AC-3: the parser keeps 6 / blocks.
  CAEStreamInfo eac3 = Info(CAEStreamInfo::STREAM_TYPE_EAC3, 48000);
  eac3.m_repeat = 1;
  EXPECT_DOUBLE_EQ(OmniphonyAccessUnitUs(eac3), 32000.0);
  eac3.m_repeat = 6;
  EXPECT_NEAR(OmniphonyAccessUnitUs(eac3), 5333.333, 0.001);
  eac3.m_repeat = 0;
  EXPECT_EQ(OmniphonyAccessUnitUs(eac3), 0.0);

  // DTS: the core's samples, recovered from the period the parser computes
  // for each type - see CAEStreamParser::SyncDTS.
  CAEStreamInfo core = Info(CAEStreamInfo::STREAM_TYPE_DTS_512, 48000);
  core.m_dtsPeriod = 512;
  EXPECT_NEAR(OmniphonyAccessUnitUs(core), 10666.667, 0.001);

  CAEStreamInfo ma = Info(CAEStreamInfo::STREAM_TYPE_DTSHD_MA, 48000);
  ma.m_dtsPeriod = 192000 * 4 * 512 / 48000;
  EXPECT_NEAR(OmniphonyAccessUnitUs(ma), 10666.667, 0.001);
  ma.m_sampleRate = 44100;
  ma.m_dtsPeriod = 192000 * 4 * 512 / 44100; // truncated, as the parser does
  EXPECT_NEAR(OmniphonyAccessUnitUs(ma), 11609.977, 0.001);

  CAEStreamInfo hra = Info(CAEStreamInfo::STREAM_TYPE_DTSHD, 48000);
  hra.m_dtsPeriod = 192000 * 1 * 1024 / 48000;
  EXPECT_NEAR(OmniphonyAccessUnitUs(hra), 21333.333, 0.001);

  // Nothing to go on.
  EXPECT_EQ(OmniphonyAccessUnitUs(Info(CAEStreamInfo::STREAM_TYPE_AC3, 0)), 0.0);
  EXPECT_EQ(OmniphonyAccessUnitUs(Info(CAEStreamInfo::STREAM_TYPE_NULL, 48000)), 0.0);
}
