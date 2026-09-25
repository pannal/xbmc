/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecOmniphony.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyPcmStream.h"
#include "utils/log.h"

extern "C"
{
#include <libavutil/channel_layout.h>
}

#include <cstdint>
#include <string>
#include <vector>

#include <gtest/gtest.h>

// The bed strings are the ones the helper packs from the engine's labels:
// comma separated, no spaces (see describe_bed in omniphony-helper.c).

TEST(TestOmniphonySpatialBed, NamesABedCarryingObjectsByItsLayout)
{
  // DTS:X D4: 7.1 bed, the fixed height quartet, five objects on top. With
  // objects to report the bed is context, so it is written the compact way.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Lb,Rb,Tfl,Tfr,Tbl,Tbr", 5),
            "7.1.4 + 5 Objects");
}

TEST(TestOmniphonySpatialBed, NamesAHeightBedCarryingNoObjects)
{
  // A presentation that places a height quartet and nothing above it still has
  // something to say, which is the whole reason this is not gated on objects.
  // Here the heights are the news, so they are spelled out rather than folded
  // into a third number.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Tfl,Tfr,Tbl,Tbr", 0),
            "5.1 + 4 Heights");
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Tfl,Tfr,Tbl,Tbr", -1),
            "5.1 + 4 Heights");
}

TEST(TestOmniphonySpatialBed, NamesAFlatBedCarryingObjects)
{
  // No heights, so no third number - but a floor is still a layout, and saying
  // it beats listing the six labels it is made of.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs", 11), "5.1 + 11 Objects");
}

TEST(TestOmniphonySpatialBed, SingularsAreSpelledSingular)
{
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Tfc", 1), "5.1.1 + 1 Object");
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Tfc", 0), "5.1 + 1 Height");
}

TEST(TestOmniphonySpatialBed, CountsEveryOverheadPositionAsAHeight)
{
  // The eight top-channel labels, and the second
  // LFE, which belongs to the floor's point-something rather than above it.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,LFE2,Ls,Rs,Tfl,Tfr,Tsl,Tsr,Tbl,Tbr,Tfc,Tc", 2),
            "5.2.8 + 2 Objects");
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,LFE2,Ls,Rs,Tfl,Tfr,Tsl,Tsr,Tbl,Tbr,Tfc,Tc", 0),
            "5.2 + 8 Heights");
}

TEST(TestOmniphonySpatialBed, CountsUpstreamAuroHeightsAboveTheFloor)
{
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Lh,Rh,Ch,Lhs,Rhs,Tc", 0),
            "5.1 + 6 Heights");
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs,Lh,Rh,Ch,Lhs,Rhs,Tc", 2),
            "5.1.6 + 2 Objects");
}

TEST(TestOmniphonySpatialBed, AFloorlessBedIsLeftToTheLabelList)
{
  // "0.1" is not a layout anyone writes, and there is nothing overhead to
  // spell out instead, so this returns empty and the caller keeps
  // "LFE + 15 Objects" - the sentence that already reads correctly there.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("LFE", 15), "");
  EXPECT_EQ(OmniphonyDescribeSpatialBed("", 15), "");
}

TEST(TestOmniphonySpatialBed, SaysNothingAboutAPlainBedWithNoObjects)
{
  // Neither heights nor objects: Player.Process(audiochannels) already says
  // this, and the row exists to add to that rather than repeat it.
  EXPECT_EQ(OmniphonyDescribeSpatialBed("L,R,C,LFE,Ls,Rs", 0), "");
}

TEST(TestOmniphonySpatialBed, NamesHeightsWithNoFloorUnderThem)
{
  // Nothing produces this today; it is here so that if something ever does,
  // the row says what arrived rather than "0.0.2 + ...".
  EXPECT_EQ(OmniphonyDescribeSpatialBed("Tfl,Tfr", 3), "2 Heights + 3 Objects");
}

TEST(TestOmniphonySpatialBed, ToleratesSpacingAndEmptyFields)
{
  // The helper packs without spaces, but nothing downstream should depend on
  // that to avoid miscounting a channel.
  EXPECT_EQ(OmniphonyDescribeSpatialBed(" L , R , C , LFE , Ls , Rs , Tfl , Tfr ,", 0),
            "5.1 + 2 Heights");
  EXPECT_EQ(OmniphonyDescribeSpatialBed(" L , R , C , LFE , Ls , Rs , Tfl , Tfr ,", 4),
            "5.1.2 + 4 Objects");
}

namespace
{
// What InputDescription hands OmniphonyDescribeSpatialBed on the PCM path: the
// labels the bridge is sent, in Kodi's spelling, for a layout ffmpeg decoded.
std::string DescribePcm(uint64_t mask, int channels)
{
  std::vector<uint8_t> labels;
  EXPECT_TRUE(OmniphonyPcmChannelLabels(mask, channels, labels));
  return OmniphonyDescribeSpatialBed(OmniphonyPcmDescribe(labels), 0);
}
} // namespace

TEST(TestOmniphonySpatialBed, SaysNothingAboutAPlainPcmLayout)
{
  // A 5.1 or a 7.1 decoded here is already said by VideoPlayer.AudioChannels,
  // which counts the source rather than the stereo render, so the row stays
  // empty for a skin to fall back to its own layout - as the same soundtrack
  // does on the bitstream path.
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_MONO, 1), "");
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_STEREO, 2), "");
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_5POINT1, 6), "");
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_7POINT1, 8), "");
}

TEST(TestOmniphonySpatialBed, NamesAPcmLayoutsHeightsTheWayABedsAre)
{
  // Kodi spells an overhead position "TFL" where the engine writes "Tfl".
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_7POINT1POINT4_BACK, 12), "7.1 + 4 Heights");
  EXPECT_EQ(DescribePcm(AV_CH_LAYOUT_5POINT1 | AV_CH_TOP_FRONT_LEFT | AV_CH_TOP_FRONT_RIGHT, 8),
            "5.1 + 2 Heights");
}

// The decoded source labels exported by ABI 9. These are the values after the
// helper has unpacked source_label= and turned underscores back into spaces.

TEST(TestOmniphonySourceLabel, NamesAnAuroCarrierByItsLayout)
{
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + Auro-3D 9.1"), "Auro 9.1");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + Auro-3D 11.1"), "Auro 11.1");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + Auro-3D 13.1"), "Auro 13.1");
}

TEST(TestOmniphonySourceLabel, NamesDtsxHeightsFromEitherCarrier)
{
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1.4"),
            "7.1 + 4 Heights");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD HRA + DTS:X 5.1.1"),
            "5.1 + 1 Height");
}

TEST(TestOmniphonySourceLabel, NamesDtsxObjectsFromTheUpstreamLabel)
{
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1.4+4"),
            "7.1.4 + 4 Objects");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD HRA + DTS:X 5.1+1"),
            "5.1 + 1 Object");

  // D0 can render three contributions from one authored object. This label
  // names those contributions; the demuxer's audio.object.count remains one.
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1.4+3"),
            "7.1.4 + 3 Objects");
}

TEST(TestOmniphonySourceLabel, LeavesOtherAndMalformedLabelsToTheBed)
{
  EXPECT_EQ(OmniphonyDescribeSourceLabel(""), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("Dolby TrueHD + Dolby Atmos"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("Auro 11.1"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + Auro-3D 11.1 extra"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1.4+0"), "");
  EXPECT_EQ(OmniphonyDescribeSourceLabel("DTS-HD MA + DTS:X 7.1.4 extra"), "");
}

// Lines the helper's stderr carries: the engine's env_logger layout, which the
// bridges' diagnostics reach through the engine's host log sink, and whatever
// else a process can print there.

TEST(TestOmniphonyHelperLog, KeepsTheEnginesWarningsAndErrors)
{
  EXPECT_EQ(LOGERROR, OmniphonyHelperLogLevel(
                          "[2026-09-23T10:00:00Z ERROR orender_engine] bridge decode error: x"));
  EXPECT_EQ(LOGWARNING,
            OmniphonyHelperLogLevel("[2026-09-23T10:00:00Z WARN  harletty-bridge::diag] "
                                    "dts: extension waveforms unavailable"));
}

TEST(TestOmniphonyHelperLog, SendsRoutineEngineLinesToTheDebugLog)
{
  EXPECT_EQ(LOGDEBUG,
            OmniphonyHelperLogLevel("[2026-09-23T10:00:00Z INFO  orender_engine] engine ready"));
  EXPECT_EQ(LOGDEBUG, OmniphonyHelperLogLevel("[2026-09-23T10:00:00Z DEBUG sys] x"));
  EXPECT_EQ(LOGDEBUG, OmniphonyHelperLogLevel("[2026-09-23T10:00:00Z TRACE sys] x"));
}

TEST(TestOmniphonyHelperLog, TreatsAnythingElseAsUnexpected)
{
  // A panic, or a bridge printing because no host sink was installed.
  EXPECT_EQ(LOGWARNING, OmniphonyHelperLogLevel("thread 'main' panicked at src/lib.rs:1:1"));
  EXPECT_EQ(LOGWARNING, OmniphonyHelperLogLevel("[harletty] INFO but not the engine's layout"));
  EXPECT_EQ(LOGWARNING, OmniphonyHelperLogLevel("[2026-09-23T10:00:00Z]"));
  EXPECT_EQ(LOGWARNING, OmniphonyHelperLogLevel(""));
}
