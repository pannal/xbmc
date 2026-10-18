/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "FileItem.h"
#include "playlists/PlayList.h"

#include <atomic>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

using namespace PLAYLIST;

namespace
{
std::shared_ptr<CFileItem> MakeItem(const std::string& name)
{
  return std::make_shared<CFileItem>("/music/" + name + ".mp3", false);
}

std::unique_ptr<CFileItemList> MakeItems(const std::vector<std::string>& names)
{
  auto items = std::make_unique<CFileItemList>();
  for (const auto& name : names)
    items->Add(MakeItem(name));
  return items;
}

// TYPE_NONE keeps the playlist from sending announcements.
CPlayList MakePlaylist(const std::vector<std::string>& names)
{
  CPlayList playlist;
  playlist.Add(*MakeItems(names));
  return playlist;
}

std::vector<std::string> Paths(const CPlayList& playlist)
{
  std::vector<std::string> paths;
  for (const auto& item : playlist.GetItems())
    paths.push_back(item->GetPath());
  return paths;
}

std::vector<std::string> ExpectedPaths(const std::vector<std::string>& names)
{
  std::vector<std::string> paths;
  for (const auto& name : names)
    paths.push_back("/music/" + name + ".mp3");
  return paths;
}

std::vector<int> Ordinals(const CPlayList& playlist)
{
  std::vector<int> ordinals;
  for (const auto& item : playlist.GetItems())
    ordinals.push_back(item->m_iprogramCount);
  return ordinals;
}
} // namespace

TEST(TestPlayList, TryGetReturnsNullOutsideTheBounds)
{
  CPlayList empty;
  EXPECT_EQ(nullptr, empty.TryGet(0));
  EXPECT_EQ(nullptr, empty.TryGet(-1L, true));

  const CPlayList playlist = MakePlaylist({"a", "b", "c"});
  EXPECT_EQ(nullptr, playlist.TryGet(-1));
  EXPECT_EQ(nullptr, playlist.TryGet(3));
  EXPECT_EQ(nullptr, playlist[3]);
  ASSERT_NE(nullptr, playlist.TryGet(2));
  EXPECT_EQ("/music/c.mp3", playlist.TryGet(2)->GetPath());
}

TEST(TestPlayList, TryGetCountsNegativeIndexesFromTheEnd)
{
  const CPlayList playlist = MakePlaylist({"a", "b", "c"});
  ASSERT_NE(nullptr, playlist.TryGet(-1L, true));
  EXPECT_EQ("/music/c.mp3", playlist.TryGet(-1L, true)->GetPath());
  ASSERT_NE(nullptr, playlist.TryGet(-3L, true));
  EXPECT_EQ("/music/a.mp3", playlist.TryGet(-3L, true)->GetPath());
  EXPECT_EQ(nullptr, playlist.TryGet(-4L, true));
  EXPECT_EQ(nullptr, playlist.TryGet(3L, true));
}

TEST(TestPlayList, SnapshotKeepsItemsAliveAfterTheQueueChanges)
{
  CPlayList playlist = MakePlaylist({"a", "b"});
  const auto snapshot = playlist.GetItems();
  const std::weak_ptr<CFileItem> first = snapshot[0];

  playlist.Clear();
  playlist.Add(MakeItem("c"));

  EXPECT_EQ(1, playlist.size());
  ASSERT_EQ(2u, snapshot.size());
  EXPECT_FALSE(first.expired());
  EXPECT_EQ("/music/a.mp3", snapshot[0]->GetPath());
  EXPECT_EQ("/music/b.mp3", snapshot[1]->GetPath());
}

TEST(TestPlayList, BatchInsertKeepsTheOrderAndRenumbersLaterItems)
{
  CPlayList playlist = MakePlaylist({"x", "y", "z"});
  playlist.Insert(*MakeItems({"a", "b", "c"}), 1);

  EXPECT_EQ(ExpectedPaths({"x", "a", "b", "c", "y", "z"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1, 2, 3, 4, 5}), Ordinals(playlist));

  playlist.UnShuffle();
  EXPECT_EQ(ExpectedPaths({"x", "a", "b", "c", "y", "z"}), Paths(playlist));
}

TEST(TestPlayList, PlaylistInsertMatchesItemListInsert)
{
  CPlayList playlist = MakePlaylist({"x", "y"});
  playlist.Insert(MakePlaylist({"a", "b"}), 0);

  EXPECT_EQ(ExpectedPaths({"a", "b", "x", "y"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1, 2, 3}), Ordinals(playlist));
}

TEST(TestPlayList, InsertPastTheEndAppends)
{
  CPlayList playlist = MakePlaylist({"x"});
  playlist.Insert(*MakeItems({"a", "b"}), 7);
  playlist.Add(MakePlaylist({"c"}));

  EXPECT_EQ(ExpectedPaths({"x", "a", "b", "c"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1, 2, 3}), Ordinals(playlist));
}

TEST(TestPlayList, RemoveAndSwapKeepOrdinalsConsistent)
{
  CPlayList playlist = MakePlaylist({"a", "b", "c", "d"});
  playlist.Remove(1);
  EXPECT_EQ(ExpectedPaths({"a", "c", "d"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1, 2}), Ordinals(playlist));

  playlist.Remove("/music/a.mp3");
  EXPECT_EQ(ExpectedPaths({"c", "d"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1}), Ordinals(playlist));

  EXPECT_TRUE(playlist.Swap(0, 1));
  EXPECT_FALSE(playlist.Swap(0, 2));
  EXPECT_EQ(ExpectedPaths({"d", "c"}), Paths(playlist));
  EXPECT_EQ((std::vector<int>{0, 1}), Ordinals(playlist));
}

TEST(TestPlayList, UnShuffleRestoresTheInsertionOrder)
{
  std::vector<std::string> names;
  for (int i = 0; i < 64; ++i)
    names.push_back(std::to_string(i));
  CPlayList playlist = MakePlaylist(names);

  playlist.Shuffle(2);
  EXPECT_TRUE(playlist.IsShuffled());
  ASSERT_EQ(64, playlist.size());
  EXPECT_EQ("/music/0.mp3", playlist[0]->GetPath());
  EXPECT_EQ("/music/1.mp3", playlist[1]->GetPath());

  playlist.UnShuffle();
  EXPECT_FALSE(playlist.IsShuffled());
  EXPECT_EQ(ExpectedPaths(names), Paths(playlist));
}

TEST(TestPlayList, CopyAndAssignmentTakeAnIndependentSnapshot)
{
  CPlayList source = MakePlaylist({"a", "b"});
  source.Shuffle();
  source.SetUnPlayable(0);

  CPlayList copy(source);
  CPlayList assigned = MakePlaylist({"z"});
  assigned = source;
  const CPlayList& self = assigned;
  assigned = self;

  for (const CPlayList* playlist : {&copy, &assigned})
  {
    EXPECT_EQ(2, playlist->size());
    EXPECT_TRUE(playlist->IsShuffled());
    EXPECT_EQ(1, playlist->GetPlayable());
  }

  source.Clear();
  EXPECT_EQ(2, copy.size());
  EXPECT_EQ(2, assigned.size());
}

TEST(TestPlayList, SetUnPlayableIgnoresInvalidIndexes)
{
  CPlayList playlist = MakePlaylist({"a", "b"});
  EXPECT_EQ(2, playlist.GetPlayable());

  playlist.SetUnPlayable(-1);
  playlist.SetUnPlayable(2);
  EXPECT_EQ(2, playlist.GetPlayable());

  playlist.SetUnPlayable(1);
  playlist.SetUnPlayable(1);
  EXPECT_EQ(1, playlist.GetPlayable());
}

TEST(TestPlayList, ConcurrentReplacementKeepsReadsValid)
{
  constexpr int ITEMS = 50;
  constexpr int REPLACEMENTS = 2000;
  std::vector<std::string> names;
  for (int i = 0; i < ITEMS; ++i)
    names.push_back(std::to_string(i));

  CPlayList playlist = MakePlaylist(names);
  std::atomic<bool> done{false};
  std::atomic<int> partialSnapshots{0};
  std::atomic<int> invalidReads{0};

  // A batch insert publishes all items under one lock, so no reader sees part of a batch.
  std::vector<std::thread> readers;
  for (int reader = 0; reader < 3; ++reader)
  {
    readers.emplace_back(
        [&]
        {
          while (!done.load())
          {
            const auto snapshot = playlist.GetItems();
            if (!snapshot.empty() && snapshot.size() != ITEMS)
              ++partialSnapshots;
            for (const auto& item : snapshot)
              if (!item || item->GetPath().empty())
                ++invalidReads;

            const auto last = playlist.TryGet(-1L, true);
            if (last && last->GetPath() != "/music/" + std::to_string(ITEMS - 1) + ".mp3")
              ++invalidReads;

            const CPlayList copy(playlist);
            if (copy.size() != 0 && copy.size() != ITEMS)
              ++partialSnapshots;
          }
        });
  }

  for (int i = 0; i < REPLACEMENTS; ++i)
  {
    playlist.Clear();
    playlist.Add(*MakeItems(names));
  }
  done = true;
  for (auto& reader : readers)
    reader.join();

  EXPECT_EQ(0, partialSnapshots.load());
  EXPECT_EQ(0, invalidReads.load());
  EXPECT_EQ(ExpectedPaths(names), Paths(playlist));
}
