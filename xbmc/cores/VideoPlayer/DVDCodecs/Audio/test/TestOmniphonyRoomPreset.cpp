/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyRoomPreset.h"

#include <algorithm>
#include <cmath>
#include <string>

#include <gtest/gtest.h>

/*
 * The engine resizes every room axis on its own: it floors each one at
 * 2 x (distance + 0.35 m) and clamps each one at 20 m. A room it has to resize
 * comes out with axes of the same length - towards a cube when it grows them,
 * two 20 m walls when it clamps them - and equal axes reflect a centre speaker
 * at one and the same delay, a comb filter that makes dialogue sound like it
 * comes out of a tin can. What these tests pin is the room Kodi asks for, so
 * that the engine never has to resize it.
 */

namespace
{
double EngineFloor(double distance)
{
  return 2.0 * (distance + 0.35);
}

constexpr double ENGINE_CEILING_M = 20.0;

constexpr int SMALL = 1;
constexpr int MEDIUM = 2;
constexpr int LARGE = 3;
} // namespace

TEST(TestOmniphonyRoomPreset, OffHasNoRoomAndNoReverb)
{
  EXPECT_FALSE(OmniphonyRoomFor(0, 2.0).enabled);
  const std::string yaml = OmniphonyRoomYaml(0, 2.0, 20);
  EXPECT_NE(std::string::npos, yaml.find("    reflections:\n      enabled: false\n"));
  EXPECT_NE(std::string::npos, yaml.find("    reverb:\n      enabled: false\n"));
}

TEST(TestOmniphonyRoomPreset, PresetsKeepTheirTunedSizeAtOneMetre)
{
  const OmniphonyRoom medium = OmniphonyRoomFor(MEDIUM, 1.0);
  EXPECT_DOUBLE_EQ(4.0, medium.width);
  EXPECT_DOUBLE_EQ(5.0, medium.depth);
  EXPECT_DOUBLE_EQ(2.7, medium.height);

  const OmniphonyRoom large = OmniphonyRoomFor(LARGE, 1.0);
  EXPECT_DOUBLE_EQ(6.0, large.width);
  EXPECT_DOUBLE_EQ(8.0, large.depth);
  EXPECT_DOUBLE_EQ(3.2, large.height);
}

TEST(TestOmniphonyRoomPreset, PresetsScaleWithTheSpeakerDistance)
{
  const OmniphonyRoom medium = OmniphonyRoomFor(MEDIUM, 2.0);
  EXPECT_DOUBLE_EQ(8.0, medium.width);
  EXPECT_DOUBLE_EQ(10.0, medium.depth);
  EXPECT_DOUBLE_EQ(5.4, medium.height);
}

TEST(TestOmniphonyRoomPreset, NoAxisIsLeftForTheEngineToResize)
{
  for (int preset : {SMALL, MEDIUM, LARGE})
  {
    const OmniphonyRoom tuned = OmniphonyRoomFor(preset, 1.0);
    for (double distance = 1.0; distance <= 6.0; distance += 0.25)
    {
      const OmniphonyRoom r = OmniphonyRoomFor(preset, distance);
      SCOPED_TRACE(testing::Message() << "preset " << preset << " at " << distance << " m");
      ASSERT_TRUE(r.enabled);
      for (double axis : {r.width, r.depth, r.height})
      {
        EXPECT_GE(axis, EngineFloor(distance) - 1e-9);
        EXPECT_LE(axis, ENGINE_CEILING_M + 1e-9);
      }
      // No two walls the same distance apart, so no two reflections coincide.
      EXPECT_GE(std::abs(r.width - r.depth), 0.5);
      EXPECT_GE(std::abs(r.width - r.height), 0.5);
      EXPECT_GE(std::abs(r.depth - r.height), 0.5);
      // Grown as a whole while it fits, so the preset keeps its proportions.
      if (std::max({r.width, r.depth, r.height}) < ENGINE_CEILING_M - 1e-9)
      {
        EXPECT_NEAR(tuned.width / tuned.height, r.width / r.height, 1e-9);
        EXPECT_NEAR(tuned.depth / tuned.height, r.depth / r.height, 1e-9);
      }
    }
  }
}

TEST(TestOmniphonyRoomPreset, ALargeRoomFarAwayStopsAtTheEngineCeiling)
{
  // 4.5 m would scale the large room's 8 m depth to 36 m; it stops at 20 m and
  // the ceiling comes up to the floor for the scene instead.
  const OmniphonyRoom large = OmniphonyRoomFor(LARGE, 4.5);
  EXPECT_DOUBLE_EQ(15.0, large.width);
  EXPECT_DOUBLE_EQ(20.0, large.depth);
  EXPECT_DOUBLE_EQ(EngineFloor(4.5), large.height);
}

TEST(TestOmniphonyRoomPreset, TheSmallRoomGrowsJustEnoughToHoldTheScene)
{
  // Its 2.4 m ceiling is below the 2.7 m floor even at 1 m.
  const OmniphonyRoom small = OmniphonyRoomFor(SMALL, 1.0);
  EXPECT_DOUBLE_EQ(2.7, small.height);
  EXPECT_DOUBLE_EQ(3.0 * 2.7 / 2.4, small.width);
}

TEST(TestOmniphonyRoomPreset, AnUnknownPresetIsTheMediumRoom)
{
  EXPECT_DOUBLE_EQ(OmniphonyRoomFor(MEDIUM, 2.0).width, OmniphonyRoomFor(7, 2.0).width);
  EXPECT_DOUBLE_EQ(OmniphonyRoomFor(MEDIUM, 2.0).width, OmniphonyRoomFor(-1, 2.0).width);
}

TEST(TestOmniphonyRoomPreset, TheYamlDescribesAFurnishedRoom)
{
  const std::string yaml = OmniphonyRoomYaml(MEDIUM, 2.0, 20);
  EXPECT_EQ("    reflections:\n"
            "      enabled: true\n"
            "      room_width_m: 8.00\n"
            "      room_depth_m: 10.00\n"
            "      room_height_m: 5.40\n"
            "      level: 0.5\n"
            "      wall_cutoff_hz: 4000\n"
            "    reverb:\n"
            "      enabled: true\n"
            "      level: 0.20\n"
            "      rt60_s: 0.35\n"
            "      predelay_ms: 20\n",
            yaml);
}

TEST(TestOmniphonyRoomPreset, NoReverbLeavesTheRoom)
{
  const std::string yaml = OmniphonyRoomYaml(MEDIUM, 2.0, 0);
  EXPECT_NE(std::string::npos, yaml.find("    reflections:\n      enabled: true\n"));
  EXPECT_NE(std::string::npos, yaml.find("    reverb:\n      enabled: false\n"));
}
