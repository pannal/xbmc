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
