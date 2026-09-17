/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/StreamUtils.h"

#include <gtest/gtest.h>

extern "C"
{
#include <libavcodec/codec_id.h>
#include <libavcodec/defs.h>
}

TEST(TestStreamUtils, General)
{
  EXPECT_EQ(0, StreamUtils::GetCodecPriority(""));
  EXPECT_EQ(1, StreamUtils::GetCodecPriority("ac3"));
  EXPECT_EQ(2, StreamUtils::GetCodecPriority("dca"));
  EXPECT_EQ(3, StreamUtils::GetCodecPriority("eac3"));
  EXPECT_EQ(4, StreamUtils::GetCodecPriority("eac3_ddp_atmos"));
  EXPECT_EQ(5, StreamUtils::GetCodecPriority("dtshd_hra"));
  EXPECT_EQ(6, StreamUtils::GetCodecPriority("dtshd_ma"));
  EXPECT_EQ(7, StreamUtils::GetCodecPriority("truehd"));
  EXPECT_EQ(8, StreamUtils::GetCodecPriority("flac"));
  EXPECT_EQ(9, StreamUtils::GetCodecPriority("dtshd_ma_x"));
  EXPECT_EQ(10, StreamUtils::GetCodecPriority("dtshd_ma_x_imax"));
  EXPECT_EQ(11, StreamUtils::GetCodecPriority("truehd_atmos"));
}

TEST(TestStreamUtils, CountsTheObjectsTheDtsxSyncwordDeclares)
{
  // The syncword's low nibble is the type-241 element's declaration count less
  // one, and it is that nibble which arrives in the level. So D0 declares one
  // object and D4 five, on either name DTS:X goes by.
  EXPECT_EQ(1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X_IMAX, 0));
  EXPECT_EQ(2, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 1));
  EXPECT_EQ(3, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 2));
  EXPECT_EQ(4, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 3));
  EXPECT_EQ(5, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 4));

  // The 0x02000850 form carries no such nibble and leaves the level where it
  // started, which is what empties the label rather than showing a zero. So does
  // an ffmpeg that never reports this at all.
  EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, AV_LEVEL_UNKNOWN));
  EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X_IMAX, AV_LEVEL_UNKNOWN));
  EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 5));

  // A level is only ever read as a count when the profile says DTS:X. Auro-3D
  // will report its layout in the same field, and every other codec is free to
  // report whatever it likes there.
  for (const int profile :
       {AV_PROFILE_DTS_HD_MA, AV_PROFILE_DTS_HD_HRA, AV_PROFILE_DTS_EXPRESS})
  {
    EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(profile, 2)) << profile;
    EXPECT_FALSE(StreamUtils::IsDTSXProfile(profile)) << profile;
  }

  // Both names are DTS:X, which is what says a stream has a bed with heights
  // over it even on the release that declares no objects at all.
  EXPECT_TRUE(StreamUtils::IsDTSXProfile(AV_PROFILE_DTS_HD_MA_X));
  EXPECT_TRUE(StreamUtils::IsDTSXProfile(AV_PROFILE_DTS_HD_MA_X_IMAX));

  // Naming is untouched by any of it: one profile per codec, as before.
  EXPECT_EQ("dtshd_ma_x", StreamUtils::GetCodecName(AV_CODEC_ID_DTS, AV_PROFILE_DTS_HD_MA_X));
  EXPECT_EQ("dtshd_ma_x_imax",
            StreamUtils::GetCodecName(AV_CODEC_ID_DTS, AV_PROFILE_DTS_HD_MA_X_IMAX));
}

TEST(TestStreamUtils, ReadsTheDtsxHeightsFromTheLevelAndNotFromTheCount)
{
  constexpr int SETS_READ = 0x10;
  constexpr int HEIGHTS = 0x20;

  // The bits above the nibble are the element's own word on its channel sets,
  // and they leave the count exactly where it was.
  EXPECT_EQ(4, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 3 | SETS_READ | HEIGHTS));
  EXPECT_EQ(1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X_IMAX, SETS_READ));
  EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 5 | SETS_READ | HEIGHTS));
  EXPECT_EQ(-1, StreamUtils::GetDTSXObjectCount(AV_PROFILE_DTS_HD_MA_X, 0x40 | 3));

  // One object over 7.1 with the height quartet, and one over 5.1 with none:
  // the same D0, told apart only by the byte ffmpeg read.
  EXPECT_EQ(4, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X_IMAX, SETS_READ | HEIGHTS));
  EXPECT_EQ(0, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X_IMAX, SETS_READ));
  EXPECT_EQ(4, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X, 4 | SETS_READ | HEIGHTS));

  // An element whose byte was not read says nothing about heights, and an
  // ffmpeg that reports only the nibble is exactly that case.
  for (int nibble = 0; nibble <= 4; ++nibble)
    EXPECT_EQ(-1, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X, nibble)) << nibble;
  EXPECT_EQ(-1, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X, 2 | HEIGHTS));
  EXPECT_EQ(-1, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X, 5 | SETS_READ | HEIGHTS));

  // No element at all is the 0x02000850 form, and its four heights stand.
  EXPECT_EQ(4, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X, AV_LEVEL_UNKNOWN));
  EXPECT_EQ(4, StreamUtils::GetDTSXHeightCount(AV_PROFILE_DTS_HD_MA_X_IMAX, AV_LEVEL_UNKNOWN));

  // And none of it is read off a stream that is not DTS:X.
  for (const int profile : {AV_PROFILE_DTS_HD_MA, AV_PROFILE_DTS_HD_HRA, AV_PROFILE_DTS_EXPRESS})
  {
    EXPECT_EQ(-1, StreamUtils::GetDTSXHeightCount(profile, SETS_READ | HEIGHTS)) << profile;
    EXPECT_EQ(-1, StreamUtils::GetDTSXHeightCount(profile, AV_LEVEL_UNKNOWN)) << profile;
  }
}
