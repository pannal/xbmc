/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Omniphony/OmniphonyCache.h"

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include <gtest/gtest.h>

using ActiveAE::COmniphonyCache;

TEST(TestOmniphonyCache, KeepsAFileUnderItsOwnName)
{
  EXPECT_EQ(COmniphonyCache::Name("smb://nas/sofa/My Head.sofa"), "My Head");
  EXPECT_EQ(COmniphonyCache::Name("/storage/bbcrdlr_systemG.v2.sofa"), "bbcrdlr_systemG.v2");
  EXPECT_EQ(COmniphonyCache::Name("/storage/kemar"), "kemar");
}

TEST(TestOmniphonyCache, KnowsTheGridsOfAName)
{
  EXPECT_TRUE(COmniphonyCache::IsGridOf("kemar-48.grid", "kemar"));
  EXPECT_TRUE(COmniphonyCache::IsGridOf("kemar-44.grid", "kemar"));
  // Written through a part or a next, and still the name's.
  EXPECT_TRUE(COmniphonyCache::IsGridOf("kemar-96.grid.part", "kemar"));
  EXPECT_TRUE(COmniphonyCache::IsGridOf("kemar-48.grid.next", "kemar"));

  // Another set's, whose name only starts the same way.
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar-48-48.grid", "kemar"));
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar-large-48.grid", "kemar"));
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar2-48.grid", "kemar"));
  // Not a grid, or no rate.
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar.sofa", "kemar"));
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar-.grid", "kemar"));
  EXPECT_FALSE(COmniphonyCache::IsGridOf("kemar-48.room", "kemar"));
}

namespace
{

std::string Read(const std::filesystem::path& path)
{
  std::ifstream file(path);
  std::stringstream text;
  text << file.rdbuf();
  return text.str();
}

} // unnamed namespace

TEST(TestOmniphonyCache, PutsWhatWasMadeInPlaceOfWhatPlays)
{
  const auto dir = std::filesystem::temp_directory_path() / "omniphony-cache-test";
  std::filesystem::remove_all(dir);
  std::filesystem::create_directories(dir);
  const auto room = dir / "bbc.room";
  const auto next = dir / "bbc.room.next";

  std::ofstream(room) << "kept";
  std::ofstream(next) << "made";
  EXPECT_TRUE(COmniphonyCache::Promote(next.string(), room.string()));
  EXPECT_EQ(Read(room), "made");
  EXPECT_FALSE(std::filesystem::exists(next));

  // A failure - here, nothing was made - leaves the room that plays as it
  // was, never missing.
  EXPECT_FALSE(COmniphonyCache::Promote(next.string(), room.string()));
  EXPECT_EQ(Read(room), "made");

  std::filesystem::remove_all(dir);
}

TEST(TestOmniphonyCache, WhatIsMadeInTheBackgroundNeverReplacesAPick)
{
  const auto dir = std::filesystem::temp_directory_path() / "omniphony-pick-test";
  std::filesystem::remove_all(dir);
  std::filesystem::create_directories(dir);
  const std::string room = (dir / "shared.room").string();
  const std::string again = (dir / "shared.room.again").string();
  const std::string next = (dir / "shared.room.next").string();

  // Nothing picked meanwhile: what a stream prepared again takes the room's place.
  uint64_t epoch = COmniphonyCache::PickEpoch();
  std::ofstream(again) << "A";
  EXPECT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
            COmniphonyCache::Placed::Done);
  EXPECT_EQ(Read(room), "A");

  // Being picked when it is done: the pick's, and what was made is removed.
  epoch = COmniphonyCache::PickEpoch();
  {
    const COmniphonyCache::Pick pick(room);
    std::ofstream(again) << "A again";
    EXPECT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
              COmniphonyCache::Placed::Picked);
    EXPECT_FALSE(std::filesystem::exists(again));
    std::ofstream(next) << "B";
    EXPECT_TRUE(pick.Promote(next));
  }
  EXPECT_EQ(Read(room), "B");

  // Picked and done while it was made: still the pick's.
  epoch = COmniphonyCache::PickEpoch();
  {
    const COmniphonyCache::Pick pick(room);
  }
  std::ofstream(again) << "A again";
  EXPECT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
            COmniphonyCache::Placed::Picked);
  EXPECT_EQ(Read(room), "B");

  // A pick over before the stream read its settings is what the stream was
  // given: what it makes again takes its place.
  {
    const COmniphonyCache::Pick pick(room);
  }
  epoch = COmniphonyCache::PickEpoch();
  std::ofstream(again) << "B again";
  EXPECT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
            COmniphonyCache::Placed::Done);
  EXPECT_EQ(Read(room), "B again");

  // A pick of another name is no concern of this one.
  epoch = COmniphonyCache::PickEpoch();
  std::ofstream(again) << "A again";
  {
    const COmniphonyCache::Pick pick((dir / "other.room").string());
    EXPECT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
              COmniphonyCache::Placed::Done);
  }
  EXPECT_EQ(Read(room), "A again");

  std::filesystem::remove_all(dir);
}

TEST(TestOmniphonyCache, APickAfterTheBackgroundRoomIsInPlaceStillOwnsTheFile)
{
  const auto dir = std::filesystem::temp_directory_path() / "omniphony-adopt-test";
  std::filesystem::remove_all(dir);
  std::filesystem::create_directories(dir);
  const std::string room = (dir / "shared.room").string();
  const std::string again = (dir / "shared.room.again").string();

  // The stream read its settings, and the room it prepared again is in place.
  const uint64_t epoch = COmniphonyCache::PickEpoch();
  std::ofstream(again) << "A";
  ASSERT_EQ(COmniphonyCache::PromoteUnlessPicked(epoch, again, room),
            COmniphonyCache::Placed::Done);
  EXPECT_FALSE(COmniphonyCache::PickedSince(room, epoch));

  // Picked before the stream takes it up - a pause holds that back - and
  // whether being picked or picked and done, the file is the pick's.
  {
    const COmniphonyCache::Pick pick(room);
    EXPECT_TRUE(COmniphonyCache::PickedSince(room, epoch));
  }
  EXPECT_TRUE(COmniphonyCache::PickedSince(room, epoch));
  EXPECT_FALSE(COmniphonyCache::PickedSince((dir / "other.room").string(), epoch));

  // A stream that read its settings after the pick owns what it was given.
  EXPECT_FALSE(COmniphonyCache::PickedSince(room, COmniphonyCache::PickEpoch()));

  std::filesystem::remove_all(dir);
}
