/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Utils/AEStreamInfo.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecOmniphony.h"

#include <cstdint>
#include <vector>

#include <gtest/gtest.h>

/*
 * A restart part-way through a film (ReopenAs, when a room has been prepared
 * again) hands the parser the middle of a stream. TrueHD between two major
 * syncs is arbitrary bytes to anything but a TrueHD decoder, and AC-3's
 * syncword is two of them: a parser looking for any family takes it for a
 * frame, misses, and counts the stream as having lost sync - which
 * CVideoPlayerAudio answers with a seek a second forward.
 */

namespace
{
using Family = CAEStreamParser::SyncFamily;

//! Bytes from the middle of a TrueHD stream, as far as a parser can tell:
//! no major sync, and the AC-3 syncword where the data happens to hold it,
//! followed by what no AC-3 header holds (a bitstream id of 31).
std::vector<uint8_t> MidStream()
{
  std::vector<uint8_t> data(4096);
  for (size_t at : {100, 1000, 2500})
  {
    data[at] = 0x0b;
    data[at + 1] = 0x77;
    data[at + 5] = 0xf8;
  }
  return data;
}

unsigned int SyncLostAfterMidStream(Family family)
{
  CAEStreamParser parser;
  parser.SetSyncFamily(family);
  // As ReopenAs leaves it: reset, with the family set at open kept.
  parser.Reset();
  std::vector<uint8_t> data = MidStream();
  uint8_t* buffer = nullptr;
  unsigned int bufferSize = 0;
  unsigned int offset = 0;
  while (offset < data.size())
  {
    const int used =
        parser.AddData(data.data() + offset, data.size() - offset, &buffer, &bufferSize);
    if (used <= 0)
      break;
    offset += used;
  }
  return parser.GetSyncLostCount();
}
} // namespace

TEST(TestOmniphonySyncFamily, EachBridgeCodecHoldsItsOwnFamily)
{
  EXPECT_EQ(Family::AC3, OmniphonySyncFamily("eac3"));
  EXPECT_EQ(Family::TrueHD, OmniphonySyncFamily("truehd"));
  EXPECT_EQ(Family::DTS, OmniphonySyncFamily("dts"));
}

TEST(TestOmniphonySyncFamily, ThePcmPathAndAnythingElseAreAnyFamily)
{
  EXPECT_EQ(Family::Any, OmniphonySyncFamily(nullptr));
  EXPECT_EQ(Family::Any, OmniphonySyncFamily("aac"));
}

TEST(TestOmniphonySyncFamily, TrueHDResyncingMidStreamCountsNoLostSync)
{
  // What the codec did before: a lost sync per AC-3 syncword in the data.
  EXPECT_GT(SyncLostAfterMidStream(Family::Any), 0u);
  EXPECT_EQ(0u, SyncLostAfterMidStream(OmniphonySyncFamily("truehd")));
}
