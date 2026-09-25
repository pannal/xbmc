/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyPcmStream.h"

extern "C"
{
#include <libavcodec/codec_id.h>
#include <libavutil/channel_layout.h>
}

#include <cmath>
#include <utility>

#include <gtest/gtest.h>

/*
 * These bytes are the renderer's ABI. The parser on the other side lives in
 * omniphony-renderer/pcm_bridge/src/header.rs, in a different language and a
 * different process, so nothing but a test holds the two spellings together.
 * The label values below are RChannelLabel's discriminants in bridge_api.
 */

namespace
{
constexpr uint8_t L = 0;
constexpr uint8_t R = 1;
constexpr uint8_t C = 2;
constexpr uint8_t LFE = 3;
constexpr uint8_t LS = 4; // side left
constexpr uint8_t RS = 5; // side right
constexpr uint8_t TFL = 6;
constexpr uint8_t TFR = 7;
constexpr uint8_t TBL = 10;
constexpr uint8_t TBR = 11;
constexpr uint8_t LB = 14; // back left
constexpr uint8_t RB = 15; // back right

std::vector<uint8_t> LabelsOf(uint64_t mask, int channels)
{
  std::vector<uint8_t> labels;
  EXPECT_TRUE(OmniphonyPcmChannelLabels(mask, channels, labels));
  return labels;
}
} // namespace

TEST(TestOmniphonyPcmStream, OriginalCodecSelectsThePlacementFamily)
{
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_AC3), "ac3");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_EAC3), "eac3");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_TRUEHD), "truehd");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_DTS), "dts");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_AAC), "pcm");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_FLAC), "pcm");
  EXPECT_STREQ(OmniphonyPcmCodecId(AV_CODEC_ID_PCM_S16LE), "pcm");
}

TEST(TestOmniphonyPcmStream, LabelsStereo)
{
  EXPECT_EQ(LabelsOf(AV_CH_LAYOUT_STEREO, 2), (std::vector<uint8_t>{L, R}));
}

TEST(TestOmniphonyPcmStream, LabelsMonoIsCentre)
{
  EXPECT_EQ(LabelsOf(AV_CH_LAYOUT_MONO, 1), (std::vector<uint8_t>{C}));
}

//! The distinction a bare channel count cannot express, and the reason this
//! wire format carries labels at all.
TEST(TestOmniphonyPcmStream, SideAndBackFivePointOneDiffer)
{
  const auto side = LabelsOf(AV_CH_LAYOUT_5POINT1, 6);
  const auto back = LabelsOf(AV_CH_LAYOUT_5POINT1_BACK, 6);

  EXPECT_EQ(side, (std::vector<uint8_t>{L, R, C, LFE, LS, RS}));
  EXPECT_EQ(back, (std::vector<uint8_t>{L, R, C, LFE, LB, RB}));
  EXPECT_NE(side, back);
}

//! Ascending mask-bit order is interleave order, which puts the back pair
//! (bits 4 and 5) before the side pair (bits 9 and 10).
TEST(TestOmniphonyPcmStream, SevenPointOneIsBackThenSide)
{
  EXPECT_EQ(LabelsOf(AV_CH_LAYOUT_7POINT1, 8),
            (std::vector<uint8_t>{L, R, C, LFE, LB, RB, LS, RS}));
}

TEST(TestOmniphonyPcmStream, SevenPointOnePointFourCarriesHeight)
{
  EXPECT_EQ(LabelsOf(AV_CH_LAYOUT_7POINT1POINT4_BACK, 12),
            (std::vector<uint8_t>{L, R, C, LFE, LB, RB, LS, RS, TFL, TFR, TBL, TBR}));
}

TEST(TestOmniphonyPcmStream, RejectsACountThatDisagreesWithTheMask)
{
  std::vector<uint8_t> labels;
  // Six set bits, eight channels claimed: the mask is not this stream's.
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_5POINT1, 8, labels));
  EXPECT_TRUE(labels.empty());
}

TEST(TestOmniphonyPcmStream, RejectsAPositionTheRendererCannotPlace)
{
  std::vector<uint8_t> labels;
  // TOP_BACK_CENTER has no counterpart, so the whole layout is refused rather
  // than the channel being placed approximately.
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_STEREO | AV_CH_TOP_BACK_CENTER, 3, labels));
  // 22.2 carries that channel and the BOTTOM_* family too.
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_22POINT2, 24, labels));
}

TEST(TestOmniphonyPcmStream, RejectsCountsOutOfRange)
{
  std::vector<uint8_t> labels;
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_STEREO, 0, labels));
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_STEREO, -1, labels));
  EXPECT_FALSE(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_STEREO, OMNI_PCM_MAX_CHANNELS + 1, labels));
}

//! Byte for byte, because the reader is a parser in another process.
TEST(TestOmniphonyPcmStream, HeaderBytes)
{
  const auto header =
      OmniphonyPcmHeader(LabelsOf(AV_CH_LAYOUT_STEREO, 2), 48000, OmniphonyPcmEncoding::Float32);

  const std::vector<uint8_t> expected{
      'O',  'P',  'C',  'M', // magic
      0x01, 0x00, // version 1, little endian
      0x02, 0x00, // two channels
      0x80, 0xBB, 0x00, 0x00, // 48000 Hz
      0x01, // Float32
      0x00, // reserved
      L,    R, // labels
  };
  EXPECT_EQ(header, expected);
  EXPECT_EQ(header.size(), OMNI_PCM_FIXED_HEADER_LEN + 2);
}

TEST(TestOmniphonyPcmStream, HeaderCarriesTheEncodingItWasAskedFor)
{
  const auto labels = LabelsOf(AV_CH_LAYOUT_STEREO, 2);
  EXPECT_EQ(OmniphonyPcmHeader(labels, 48000, OmniphonyPcmEncoding::I32Scaled24)[12], 0);
  EXPECT_EQ(OmniphonyPcmHeader(labels, 48000, OmniphonyPcmEncoding::Float32)[12], 1);
}

TEST(TestOmniphonyPcmStream, HeaderRejectsWhatTheBridgeWould)
{
  const auto labels = LabelsOf(AV_CH_LAYOUT_STEREO, 2);
  EXPECT_TRUE(OmniphonyPcmHeader({}, 48000, OmniphonyPcmEncoding::Float32).empty());
  EXPECT_TRUE(OmniphonyPcmHeader(labels, 0, OmniphonyPcmEncoding::Float32).empty());
  EXPECT_TRUE(OmniphonyPcmHeader(std::vector<uint8_t>(OMNI_PCM_MAX_CHANNELS + 1, L), 48000,
                                 OmniphonyPcmEncoding::Float32)
                  .empty());
}

//! A 44.1 kHz source is resampled to 48 kHz before it reaches the bridge, but
//! the header still has to be able to say any rate: nothing here assumes one.
TEST(TestOmniphonyPcmStream, HeaderEncodesTheRateLittleEndian)
{
  const auto header =
      OmniphonyPcmHeader(LabelsOf(AV_CH_LAYOUT_MONO, 1), 44100, OmniphonyPcmEncoding::Float32);
  EXPECT_EQ(header[8], 0x44);
  EXPECT_EQ(header[9], 0xAC);
  EXPECT_EQ(header[10], 0x00);
  EXPECT_EQ(header[11], 0x00);
}

/*
 * The on-screen channel list, built from the layouts a listener actually meets.
 * Every one of these is described from the labels the renderer is sent, in the
 * order it is sent them, so what the row says and what the bridge was handed
 * cannot drift apart.
 */
TEST(TestOmniphonyPcmStream, DescribesTheCommonLayouts)
{
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_MONO, 1)), "Mono");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_STEREO, 2)), "FL, FR");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_2POINT1, 3)), "FL, FR, LFE");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_QUAD, 4)), "FL, FR, BL, BR");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_5POINT0, 5)), "FL, FR, FC, SL, SR");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_5POINT1, 6)), "FL, FR, FC, LFE, SL, SR");
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_7POINT1, 8)),
            "FL, FR, FC, LFE, BL, BR, SL, SR");
}

/*!
 * 6.1 in interleave order, which is not the order it is usually written.
 *
 * BACK_CENTER is bit 8 and the side pair bits 9 and 10, so the centre back
 * arrives before the sides rather than after them. Written out as its own case
 * because it is the one layout where the order a listener expects and the order
 * the samples arrive in disagree - and the samples win, since this row exists
 * to say what the renderer was handed.
 */
TEST(TestOmniphonyPcmStream, DescribesSixPointOneInInterleaveOrder)
{
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_6POINT1, 7)), "FL, FR, FC, LFE, BC, SL, SR");
}

//! Height layouts, where the count alone would say nothing useful at all.
TEST(TestOmniphonyPcmStream, DescribesHeightLayouts)
{
  EXPECT_EQ(OmniphonyPcmDescribe(LabelsOf(AV_CH_LAYOUT_7POINT1POINT4_BACK, 12)),
            "FL, FR, FC, LFE, BL, BR, SL, SR, TFL, TFR, TBL, TBR");
}

TEST(TestOmniphonyPcmStream, DescribesNothingItCannotName)
{
  EXPECT_TRUE(OmniphonyPcmDescribe({}).empty());
  // 24 is Object and 255 is Unknown; neither is a speaker and neither can reach
  // here from OmniphonyPcmChannelLabels, which refuses both.
  EXPECT_TRUE(OmniphonyPcmDescribe({L, R, 24}).empty());
  EXPECT_TRUE(OmniphonyPcmDescribe({L, R, 255}).empty());
}

/*
 * The level match. Both terms are derived from the layout rather than measured
 * per stream, so what they come to for each layout is worth pinning down: this
 * is the whole of the arithmetic that decides whether "-3 dB" sounds the same
 * on an album as on a film.
 */
TEST(TestOmniphonyPcmStream, LevelMatchIsNeutralAtTheReferenceLayout)
{
  // 5.1 is the layout the default was chosen against, so it must correct by
  // nothing at all - that is what lets every profile in the field keep its
  // number.
  const auto m = OmniphonyPcmLevelMatch(AV_CH_LAYOUT_5POINT1, 6);
  EXPECT_EQ(m.summingDb, 0.0);
  EXPECT_EQ(m.downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
}

/*!
 * A layout Kodi would not fold is corrected as though it were the reference.
 *
 * Not because Kodi normalises it - Kodi leaves it alone - but because the
 * alternative was measured by ear and rejected: inheriting Kodi's own gap put
 * a stereo album 14 dB above a 5.1 film at one setting, and mono 17 above it.
 */
TEST(TestOmniphonyPcmStream, LevelMatchGivesAPassThroughTheReferenceCorrection)
{
  for (const auto& [layout, channels] :
       {std::pair{AV_CH_LAYOUT_STEREO, 2}, std::pair{AV_CH_LAYOUT_MONO, 1},
        // 2.1 is the case a channel count gets wrong: three
        // channels, so a count says "multichannel", but the only
        // thing beyond the front pair is the LFE, dropped rather
        // than summed. Nothing reaches the front pair that was
        // not already there.
        std::pair{AV_CH_LAYOUT_2POINT1, 3}})
  {
    const auto m = OmniphonyPcmLevelMatch(layout, channels);
    EXPECT_EQ(m.downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
    EXPECT_EQ(m.summingDb, 0.0);
  }

  // A centre or a surround is summed, and then it is a real fold - which takes
  // the same attenuation by the other branch, and its own width with it.
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_SURROUND, 3).downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_QUAD, 4).downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
}

//! The summing term attenuates layouts wider than the reference and never
//! boosts a narrower one. Rounded, because the point is the size.
TEST(TestOmniphonyPcmStream, LevelMatchTracksTheSourceWidth)
{
  auto round2 = [](double v) { return std::round(v * 100.0) / 100.0; };
  EXPECT_EQ(round2(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_7POINT1, 8).summingDb), -1.25);
  EXPECT_EQ(round2(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_7POINT1POINT4_BACK, 12).summingDb), -3.01);

  // Narrower than the reference and still folding - the boost the ratio alone
  // would give these is what would make one setting mean two loudnesses.
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_SURROUND, 3).summingDb, 0.0);
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_QUAD, 4).summingDb, 0.0);
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_MONO, 1).summingDb, 0.0);
  EXPECT_EQ(OmniphonyPcmLevelMatch(AV_CH_LAYOUT_STEREO, 2).summingDb, 0.0);
}

/*!
 * The whole point, in one assertion: one setting, one loudness.
 *
 * Every layout is corrected to within the reference's own attenuation or below
 * it, so no stream can come out louder than 5.1 does at the same setting. The
 * wide layouts are allowed to sit below it, which is the measured half of the
 * summing term doing its job.
 */
TEST(TestOmniphonyPcmStream, NoLayoutIsLouderThanTheReference)
{
  const auto ref = OmniphonyPcmLevelMatch(AV_CH_LAYOUT_5POINT1, 6);
  const double refGain = -ref.downmixDb + ref.summingDb;

  const std::pair<uint64_t, int> layouts[] = {
      {AV_CH_LAYOUT_MONO, 1},    {AV_CH_LAYOUT_STEREO, 2},
      {AV_CH_LAYOUT_2POINT1, 3}, {AV_CH_LAYOUT_SURROUND, 3},
      {AV_CH_LAYOUT_QUAD, 4},    {AV_CH_LAYOUT_5POINT1, 6},
      {AV_CH_LAYOUT_7POINT1, 8}, {AV_CH_LAYOUT_7POINT1POINT4_BACK, 12},
  };
  for (const auto& [layout, channels] : layouts)
  {
    const auto m = OmniphonyPcmLevelMatch(layout, channels);
    const double gain = -m.downmixDb + m.summingDb;
    EXPECT_LE(gain, refGain) << "channels=" << channels;
  }
}

/*!
 * An unknown layout has to behave as 5.1 always has.
 *
 * A demuxer that reports no channel count leaves nothing to correct from, and
 * the reference answer is the one that changes nothing for anybody: it is what
 * the object path gets, and what this path got before the terms existed.
 */
TEST(TestOmniphonyPcmStream, LevelMatchFallsBackToTheReferenceWhenNothingIsKnown)
{
  const auto m = OmniphonyPcmLevelMatch(0, 0);
  EXPECT_EQ(m.summingDb, 0.0);
  EXPECT_EQ(m.downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);

  // A count with no mask is still worth something: it cannot say what is being
  // folded, only that something must be.
  EXPECT_EQ(OmniphonyPcmLevelMatch(0, 6).downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
  // A count of two is a pass-through, and takes the reference correction like
  // any other - see LevelMatchGivesAPassThroughTheReferenceCorrection.
  EXPECT_EQ(OmniphonyPcmLevelMatch(0, 2).downmixDb, OMNI_NORMALIZED_DOWNMIX_DB);
}
