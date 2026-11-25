/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Utils/AEStreamInfo.h"

#include <algorithm>
#include <cstdint>
#include <memory>
#include <vector>

#include <gtest/gtest.h>

namespace
{
void FixTailCRC(std::vector<uint8_t>& frame)
{
  uint16_t crc = 0;
  for (unsigned int byte = 2; byte < frame.size() - 2; ++byte)
  {
    crc ^= static_cast<uint16_t>(frame[byte]) << 8;
    for (unsigned int bit = 0; bit < 8; ++bit)
      crc = static_cast<uint16_t>((crc << 1) ^ (crc & 0x8000 ? 0x8005 : 0));
  }
  frame[frame.size() - 2] = static_cast<uint8_t>(crc >> 8);
  frame[frame.size() - 1] = static_cast<uint8_t>(crc);
}

// Header-only synthetic frames test classification and rewriting, not Atmos decoding.
std::vector<uint8_t> EAC3Frame(bool joc,
                               unsigned int streamType = 0,
                               unsigned int additionalBytes = 2,
                               unsigned int substream = 0,
                               bool finalDependent = true)
{
  std::vector<uint8_t> frame(16, 0);
  unsigned int pos = 0;
  const auto write = [&frame, &pos](unsigned int value, unsigned int width)
  {
    for (unsigned int bit = 0; bit < width; ++bit, ++pos)
      frame[pos / 8] |= ((value >> (width - bit - 1)) & 1) << (7 - pos % 8);
  };
  write(0x0b77, 16);
  write(streamType, 2);
  write(substream, 3);
  write(7, 11); // 16-byte frame
  write(0, 2);
  write(3, 2);
  write(7, 3);
  write(1, 1);
  write(16, 5);
  write(20, 5);
  const bool compre = streamType == 1 && finalDependent;
  write(compre, 1);
  if (compre)
    write(0, 8);
  if (streamType == 1)
    write(0, 1);
  write(0, 1);
  write(0, 1);
  write(1, 1);
  write(additionalBytes - 1, 6);
  write(joc ? 1 : 0, 8);
  write(joc ? 16 : 0, 8);

  FixTailCRC(frame);
  return frame;
}

std::vector<uint8_t> AC3CoreFrame()
{
  std::vector<uint8_t> frame(128, 0);
  frame[0] = 0x0b;
  frame[1] = 0x77;
  // CRC1 for bytes [2,80); the all-zero second region already has CRC2 zero.
  frame[2] = 0xf2;
  frame[3] = 0xaa;
  frame[5] = 8 << 3;
  frame[6] = 0xe1;
  frame[7] = 20 << 3;
  return frame;
}

std::vector<uint8_t> ParseFirstPacket(CAEStreamParser& parser, std::vector<uint8_t> packet,
                                    const std::vector<uint8_t>& lookahead = EAC3Frame(false))
{
  packet.insert(packet.end(), lookahead.begin(), lookahead.end());
  uint8_t* output = nullptr;
  unsigned int size = 0;
  parser.AddData(packet.data(), packet.size(), &output, &size);
  std::unique_ptr<uint8_t[]> owner(output);
  return output ? std::vector<uint8_t>(output, output + size) : std::vector<uint8_t>{};
}

unsigned int Dialnorm(const std::vector<uint8_t>& packet)
{
  return ((packet[5] & 7) << 2) | (packet[6] >> 6);
}
} // namespace

TEST(TestAEStreamParser, UntypedJocIsPreservedBeforeDialnormRewrite)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto original = EAC3Frame(true);
  EXPECT_EQ(original, ParseFirstPacket(parser, original));
  EXPECT_EQ(-11, parser.GetStreamInfo().m_dialNormApplied);
}

TEST(TestAEStreamParser, ExplicitAtmosOverrideRetainsExistingRewriteBehavior)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  parser.SetDefeatEAC3AtmosDialNorm(true);
  const auto original = EAC3Frame(true);
  const auto output = ParseFirstPacket(parser, original);
  ASSERT_EQ(original.size(), output.size());
  EXPECT_NE(original, output);
  EXPECT_EQ(31u, Dialnorm(output));
  EXPECT_EQ(0, parser.GetStreamInfo().m_dialNormApplied);
}

TEST(TestAEStreamParser, DisablingOverrideProtectsPreviouslyObservedJoc)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  parser.SetDefeatEAC3AtmosDialNorm(true);
  const auto first = ParseFirstPacket(parser, EAC3Frame(true));
  ASSERT_EQ(16u, first.size());
  EXPECT_EQ(31u, Dialnorm(first));
  parser.Reset();
  parser.SetDefeatEAC3AtmosDialNorm(false);
  const auto plain = EAC3Frame(false);
  EXPECT_EQ(plain, ParseFirstPacket(parser, plain));
}

TEST(TestAEStreamParser, ExplicitAtmosOverrideIncludesTypedAc3Core)
{
  CAEStreamParser parser;
  parser.SetEAC3Track(true);
  parser.SetEAC3JOC(true);
  parser.SetDefeatAC3DialNorm(true);
  parser.SetDefeatEAC3AtmosDialNorm(true);
  const auto original = AC3CoreFrame();
  const auto output = ParseFirstPacket(parser, original, original);
  ASSERT_EQ(original.size(), output.size());
  EXPECT_EQ(31, output[7] >> 3);
  parser.Reset();
  parser.SetDefeatEAC3AtmosDialNorm(false);
  auto preserved = ParseFirstPacket(parser, original, original);
  // The retained AC3 sync reader can need another input feed after Reset.
  if (preserved.empty())
    preserved = ParseFirstPacket(parser, original, original);
  EXPECT_EQ(original, preserved);
}

TEST(TestAEStreamParser, NonJocEac3StillDefeatsDialnorm)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto output = ParseFirstPacket(parser, EAC3Frame(false));
  ASSERT_EQ(16u, output.size());
  EXPECT_EQ(31u, Dialnorm(output));
  EXPECT_EQ(-11, parser.GetStreamInfo().m_dialNormOriginal);
  EXPECT_EQ(0, parser.GetStreamInfo().m_dialNormApplied);
}

TEST(TestAEStreamParser, DemuxTypedAtmosIsPreservedWithoutAnInBandSignal)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  parser.SetEAC3JOC(true);
  const auto original = EAC3Frame(false);
  EXPECT_EQ(original, ParseFirstPacket(parser, original));
}

TEST(TestAEStreamParser, JocInDependentFrameProtectsTheWholePacket)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  auto packet = EAC3Frame(false);
  const auto dependent = EAC3Frame(true, 1);
  packet.insert(packet.end(), dependent.begin(), dependent.end());
  EXPECT_EQ(packet, ParseFirstPacket(parser, packet));
}

TEST(TestAEStreamParser, UnclassifiedAdditionalBsiIsNotRewritten)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto original = EAC3Frame(true, 0, 64);
  EXPECT_EQ(original, ParseFirstPacket(parser, original));
}

TEST(TestAEStreamParser, JocClassificationSurvivesSeekButNotANewTrackHint)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto joc = EAC3Frame(true);
  ASSERT_EQ(joc, ParseFirstPacket(parser, joc));
  parser.Reset();
  const auto plain = EAC3Frame(false);
  EXPECT_EQ(plain, ParseFirstPacket(parser, plain));
  parser.Reset();
  parser.SetEAC3JOC(false);
  const auto output = ParseFirstPacket(parser, plain);
  ASSERT_EQ(16u, output.size());
  EXPECT_EQ(31u, Dialnorm(output));
}

TEST(TestAEStreamParser, JocObservedWithDefeatOffRemainsProtectedWhenDefeatIsEnabled)
{
  CAEStreamParser parser;
  ASSERT_EQ(16u, ParseFirstPacket(parser, EAC3Frame(true)).size());
  parser.Reset();
  parser.SetDefeatAC3DialNorm(true);
  const auto plain = EAC3Frame(false);
  EXPECT_EQ(plain, ParseFirstPacket(parser, plain));
}

TEST(TestAEStreamParser, LateJocIsProtectedAfterTheObjectScanBudget)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto plain = EAC3Frame(false);
  const auto joc = EAC3Frame(true);
  std::vector<uint8_t> stream;
  for (unsigned int packet = 0; packet < 65; ++packet)
    stream.insert(stream.end(), plain.begin(), plain.end());
  stream.insert(stream.end(), joc.begin(), joc.end());
  auto output = ParseFirstPacket(parser, stream);
  for (unsigned int packet = 0; packet < 65; ++packet)
    output = ParseFirstPacket(parser, {});
  EXPECT_EQ(joc, output);
}

TEST(TestAEStreamParser, TypedAtmosAc3CoreIsPreserved)
{
  CAEStreamParser parser;
  parser.SetEAC3JOC(true);
  parser.SetDefeatAC3DialNorm(true);
  const auto original = AC3CoreFrame();
  EXPECT_EQ(original, ParseFirstPacket(parser, original));
  EXPECT_EQ(-11, parser.GetStreamInfo().m_dialNormApplied);
}

TEST(TestAEStreamParser, OrdinaryAc3StillDefeatsDialnorm)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  const auto output = ParseFirstPacket(parser, AC3CoreFrame());
  ASSERT_EQ(128u, output.size());
  EXPECT_EQ(31, output[7] >> 3);
  EXPECT_EQ(0, parser.GetStreamInfo().m_dialNormApplied);
}

TEST(TestAEStreamParser, UntypedEac3TrackPreservesCoreBeforeALateJocDependent)
{
  CAEStreamParser parser;
  parser.SetEAC3Track(true);
  parser.SetDefeatAC3DialNorm(true);
  const auto core = AC3CoreFrame();
  EXPECT_EQ(core, ParseFirstPacket(parser, core, core));
  const auto dependent = EAC3Frame(true, 1);
  auto complete = core;
  complete.insert(complete.end(), dependent.begin(), dependent.end());
  EXPECT_EQ(complete, ParseFirstPacket(parser, dependent));
}

TEST(TestAEStreamParser, ParsedJocProtectsSubsequentAc3CoreAfterSeek)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  ASSERT_EQ(EAC3Frame(true), ParseFirstPacket(parser, EAC3Frame(true)));
  parser.Reset();
  const auto original = AC3CoreFrame();
  EXPECT_EQ(original, ParseFirstPacket(parser, original));
}

TEST(TestAEStreamParser, ClassifiesLastDependentBeforeEmittingFragmentedPresentation)
{
  for (const bool joc : {false, true})
  {
    CAEStreamParser parser;
    parser.SetEAC3Track(true);
    parser.SetDefeatAC3DialNorm(true);
    auto packet = EAC3Frame(false);
    const auto first = EAC3Frame(false, 1, 2, 0, false);
    const auto last = EAC3Frame(joc, 1, 2, 1);
    packet.insert(packet.end(), first.begin(), first.end());
    packet.insert(packet.end(), last.begin(), last.end());

    uint8_t* output = nullptr;
    unsigned int size = 0;
    for (size_t i = 0; i < packet.size(); ++i)
    {
      EXPECT_EQ(1, parser.AddData(&packet[i], 1, &output, &size));
      if (i + 1 < packet.size())
      {
        EXPECT_EQ(0u, size);
      }
    }
    std::unique_ptr<uint8_t[]> owner(output);
    ASSERT_EQ(packet.size(), size);
    const std::vector<uint8_t> result(output, output + size);
    EXPECT_EQ(joc ? 20u : 31u, Dialnorm(result));
    if (joc)
    {
      EXPECT_EQ(packet, result);
    }
    EXPECT_TRUE(std::equal(first.begin(), first.end(), result.begin() + 16));
    EXPECT_TRUE(std::equal(last.begin(), last.end(), result.begin() + 32));
  }
}

TEST(TestAEStreamParser, MissingFinalDependentPreservesPrimaryAndKeepsNextIndependent)
{
  CAEStreamParser parser;
  parser.SetDefeatAC3DialNorm(true);
  auto packet = EAC3Frame(false);
  const auto dependent = EAC3Frame(false, 1, 2, 0, false);
  packet.insert(packet.end(), dependent.begin(), dependent.end());
  EXPECT_EQ(packet, ParseFirstPacket(parser, packet));
  const auto next = ParseFirstPacket(parser, EAC3Frame(false));
  ASSERT_EQ(16u, next.size());
  EXPECT_EQ(31u, Dialnorm(next));
}

TEST(TestAEStreamParser, MalformedFirstDependentPreservesPrimary)
{
  const auto primary = EAC3Frame(false);
  auto wrongID = EAC3Frame(false, 1, 2, 1);
  auto wrongRate = EAC3Frame(false, 1);
  wrongRate[4] |= 0x40; // 44.1 kHz dependent on a 48 kHz primary.
  FixTailCRC(wrongRate);
  for (const auto& dependent : {wrongID, wrongRate})
  {
    CAEStreamParser parser;
    parser.SetDefeatAC3DialNorm(true);
    auto packet = primary;
    packet.insert(packet.end(), dependent.begin(), dependent.end());
    EXPECT_EQ(packet, ParseFirstPacket(parser, packet));
  }
}
