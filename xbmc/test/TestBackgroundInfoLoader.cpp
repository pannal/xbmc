/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "BackgroundInfoLoader.h"
#include "FileItem.h"

#include <chrono>
#include <memory>
#include <vector>

#include <gtest/gtest.h>

using namespace std::chrono_literals;

namespace
{
class TestBackgroundInfoLoader : public ::testing::Test, public CBackgroundInfoLoader
{
protected:
  void SetUp() override
  {
    for (int i = 0; i < 250; ++i)
      m_items.Add(std::make_shared<CFileItem>());

    LoadShownFirst(m_items, 100);
    ASSERT_TRUE(m_firstLookup.Wait(5s));
  }

  void TearDown() override
  {
    m_resume.Set();
    StopThread();
  }

  bool LoadItemCached(CFileItem*) override
  {
    ++m_cached;
    return false;
  }

  bool LoadItemLookup(CFileItem* item) override
  {
    m_lookups.push_back(item);
    if (m_lookups.size() == 1)
    {
      m_firstLookup.Set();
      m_resume.Wait();
    }
    if (item == m_items[101].get())
      m_cachedBeforeItem101 = m_cached;
    return false;
  }

  void OnLoaderFinish() override { m_finished.Set(); }

  CFileItemList m_items;
  CEvent m_firstLookup{true};
  CEvent m_resume{true};
  CEvent m_finished{true};
  std::vector<CFileItem*> m_lookups;
  int m_cached{0};
  int m_cachedBeforeItem101{0};
};

TEST_F(TestBackgroundInfoLoader, CompletedAndForeignItemsDoNotWaitForAnotherLookup)
{
  // Item 101 already received its bulk lookup. A replacement item belongs to a different list.
  EXPECT_TRUE(PrioritizeAndWait({m_items[101], std::make_shared<CFileItem>()}, 0ms));
}

TEST_F(TestBackgroundInfoLoader, RedirectAcrossInitialStartCompletesVisibleLookupsFirst)
{
  // The first 100 cached items start at 100. The new viewport straddles that starting point.
  EXPECT_FALSE(PrioritizeAndWait({m_items[98], m_items[99], m_items[100], m_items[101]}, 0ms));
  m_resume.Set();
  ASSERT_TRUE(m_finished.Wait(5s));

  ASSERT_EQ(m_lookups.size(), static_cast<size_t>(m_items.Size()));
  EXPECT_EQ(m_lookups[0], m_items[100].get());
  EXPECT_EQ(m_lookups[1], m_items[98].get());
  EXPECT_EQ(m_lookups[2], m_items[99].get());
  EXPECT_EQ(m_lookups[3], m_items[101].get());
  EXPECT_LT(m_cachedBeforeItem101, m_items.Size());
}
} // namespace
