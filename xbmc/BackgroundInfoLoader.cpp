/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "BackgroundInfoLoader.h"

#include "FileItem.h"
#include "URL.h"
#include "threads/Thread.h"
#include "utils/log.h"

#include <algorithm>
#include <mutex>
#include <numeric>
#include <unordered_map>
#include <utility>

CBackgroundInfoLoader::CBackgroundInfoLoader() = default;

CBackgroundInfoLoader::~CBackgroundInfoLoader()
{
  StopThread();
}

void CBackgroundInfoLoader::Reset()
{
  {
    std::unique_lock<CCriticalSection> lock(m_viewportLock);
    m_awaitingViewport = false;
    m_completedBatchItems = 0;
    m_inFlightBatchNeeds.clear();
    m_waitingFor.clear();
    m_newViewport.clear();
    m_viewportChanged = false;
    m_shownBatchDone.Set();
  }
  m_pVecItems = nullptr;
  m_vecItems.clear();
  m_bIsLoading = false;
}

bool CBackgroundInfoLoader::PrioritizeAndWait(const std::vector<CFileItemPtr>& shown,
                                             std::chrono::milliseconds timeout)
{
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  {
    std::unique_lock<CCriticalSection> lock(m_viewportLock);
    if (!m_awaitingViewport)
      return true;

    m_awaitingViewport = false;
    const auto waitForItem = [&](size_t index)
    {
      const auto& item = m_vecItems[index];
      if (!item || index < m_completedBatchItems)
        return;
      // The worker snapshots the active batch before changing any shared items.
      // Later items are untouched until we release the lock.
      const size_t offset = index - m_completedBatchItems;
      if (offset < m_inFlightBatchNeeds.size() ? m_inFlightBatchNeeds[offset]
                                               : NeedsLoaderBatch(*item))
        m_waitingFor.insert(item.get());
    };

    if (shown.size() <= std::min(m_shownItems, m_vecItems.size()) &&
        std::equal(shown.begin(), shown.end(), m_vecItems.begin()))
    {
      if (shown.size() > m_completedBatchItems)
      {
        // Handle repeated references by their earliest occurrence, as for a rebound list.
        std::unordered_set<const CFileItem*> seen;
        for (size_t i = 0; i < shown.size(); ++i)
          if (seen.insert(shown[i].get()).second)
            waitForItem(i);
      }
    }
    else
    {
      // A rebound list may contain items outside this load. Match only the visible
      // identities and pass their worker indices on in the GUI's current order.
      std::unordered_map<const CFileItem*, size_t> positions;
      for (const auto& item : shown)
        if (item)
          positions.emplace(item.get(), m_vecItems.size());
      size_t missing = positions.size();
      for (size_t i = 0; i < m_vecItems.size() && missing > 0; ++i)
      {
        const auto it = positions.find(m_vecItems[i].get());
        if (it != positions.end() && it->second == m_vecItems.size())
        {
          it->second = i;
          --missing;
        }
      }
      for (const auto& item : shown)
      {
        const auto it = positions.find(item.get());
        if (it != positions.end() && it->second < m_vecItems.size())
        {
          m_newViewport.push_back(it->second);
          waitForItem(it->second);
        }
      }
      m_viewportChanged = !m_newViewport.empty();
    }

    if (m_waitingFor.empty())
      return true;
    m_shownBatchDone.Reset();
  }
  const auto now = std::chrono::steady_clock::now();
  return now < deadline ? m_shownBatchDone.Wait(deadline - now) : m_shownBatchDone.Signaled();
}

void CBackgroundInfoLoader::ProcessBatch(const std::vector<CFileItemPtr>& items)
{
  if (m_shownItems > 0)
  {
    std::unique_lock<CCriticalSection> lock(m_viewportLock);
    if (m_awaitingViewport)
    {
      for (const auto& item : items)
        m_inFlightBatchNeeds.push_back(item && NeedsLoaderBatch(*item));
    }
  }

  try
  {
    OnLoaderBatch(items.begin(), items.end());
  }
  catch (...)
  {
    CLog::Log(LOGERROR, "CBackgroundInfoLoader::OnLoaderBatch - Unhandled exception");
  }

  if (m_shownItems > 0)
  {
    std::unique_lock<CCriticalSection> lock(m_viewportLock);
    m_inFlightBatchNeeds.clear();
    if (m_awaitingViewport)
      m_completedBatchItems += items.size();
    else if (!m_waitingFor.empty())
    {
      for (const auto& item : items)
        m_waitingFor.erase(item.get());
      if (m_waitingFor.empty())
        m_shownBatchDone.Set();
    }
  }
}

void CBackgroundInfoLoader::Run()
{
  try
  {
    if (!m_vecItems.empty())
    {
      OnLoaderStart();

      const size_t count = m_vecItems.size();
      const auto aborted = [this]
      { return (m_pProgressCallback && m_pProgressCallback->Abort()) || m_bStop; };

      struct ItemProgress
      {
        bool batched{false};
        bool cached{false};
        bool lookedUp{false};
      };
      std::vector<ItemProgress> progress(count);
      std::vector<CFileItemPtr> batch;
      batch.reserve(LOADER_BATCH_SIZE);
      const auto loadBatch = [&](const std::vector<size_t>& order, size_t first, size_t end)
      {
        batch.clear();
        for (size_t i = first; i < end; ++i)
        {
          const size_t index = order[i];
          if (!std::exchange(progress[index].batched, true))
            batch.push_back(m_vecItems[index]);
        }
        if (!batch.empty())
          ProcessBatch(batch);
      };
      // Stage 1: All "fast" stuff we have already cached
      const auto loadCached = [&](size_t index)
      {
        if (std::exchange(progress[index].cached, true))
          return;
        CFileItem* pItem = m_vecItems[index].get();
        try
        {
          if (LoadItemCached(pItem) && m_pObserver)
            m_pObserver->OnItemLoaded(pItem);
        }
        catch (...)
        {
          CLog::Log(LOGERROR,
                    "CBackgroundInfoLoader::LoadItemCached - Unhandled exception for item {}",
                    CURL::GetRedacted(pItem->GetPath()));
        }
      };
      // Stage 2: All "slow" stuff that we need to lookup
      const auto loadLookup = [&](size_t index)
      {
        if (std::exchange(progress[index].lookedUp, true))
          return;
        CFileItem* pItem = m_vecItems[index].get();
        try
        {
          if (LoadItemLookup(pItem) && m_pObserver)
            m_pObserver->OnItemLoaded(pItem);
        }
        catch (...)
        {
          CLog::Log(LOGERROR,
                    "CBackgroundInfoLoader::LoadItemLookup - Unhandled exception for item {}",
                    CURL::GetRedacted(pItem->GetPath()));
        }
      };
      std::vector<size_t> order(count);
      std::iota(order.begin(), order.end(), size_t{0});
      size_t shownEnd = std::min(m_shownItems, count);
      const auto interrupted = [&] { return aborted() || m_viewportChanged; };
      for (;;)
      {
        if (m_viewportChanged)
        {
          // Only the first frame can redirect the worker. Match by identity in case the GUI
          // rebound or resorted its list. Then continue all unfinished work in its prior order.
          std::vector<size_t> shown;
          {
            std::unique_lock<CCriticalSection> lock(m_viewportLock);
            shown.swap(m_newViewport);
            m_viewportChanged = false;
          }
          order.clear();
          std::vector<bool> selected(count, false);
          for (const size_t index : shown)
          {
            if (!selected[index])
            {
              selected[index] = true;
              order.push_back(index);
            }
          }
          shownEnd = order.size();
          for (size_t i = 0; i < count; ++i)
            if (!selected[i])
              order.push_back(i);
        }

        for (size_t i = 0; i < shownEnd && !interrupted(); i += LOADER_BATCH_SIZE)
          loadBatch(order, i, std::min(i + LOADER_BATCH_SIZE, shownEnd));
        for (size_t i = 0; i < shownEnd && !interrupted(); ++i)
          loadCached(order[i]);
        for (size_t i = 0; i < shownEnd && !interrupted(); ++i)
          loadLookup(order[i]);

        // Ordinary loads and the remainder keep the two list-wide passes.
        for (size_t i = shownEnd; i < count && !interrupted(); i += LOADER_BATCH_SIZE)
        {
          const size_t end = std::min(i + LOADER_BATCH_SIZE, count);
          loadBatch(order, i, end);
          for (size_t j = i; j < end && !interrupted(); ++j)
            loadCached(order[j]);
        }
        for (size_t i = shownEnd; i < count && !interrupted(); ++i)
          loadLookup(order[i]);

        if (aborted() || !m_viewportChanged)
          break;
      }
    }

    OnLoaderFinish();
  }
  catch (...)
  {
    CLog::Log(LOGERROR, "{} - Unhandled exception", __FUNCTION__);
  }

  Reset();
}

void CBackgroundInfoLoader::Load(CFileItemList& items)
{
  Start(items, 0, 0);
}

void CBackgroundInfoLoader::LoadShownFirst(CFileItemList& items, int firstItem, size_t shownItems)
{
  Start(items, firstItem, std::max(LOADER_BATCH_SIZE, shownItems));
}

void CBackgroundInfoLoader::Start(CFileItemList& items, int firstItem, size_t shownItems)
{
  StopThread();

  if (items.IsEmpty())
    return;

  std::unique_lock<CCriticalSection> lock(m_lock);

  const int count = items.Size();
  const int first = (firstItem > 0 && firstItem < count) ? firstItem : 0;
  for (int nItem = 0; nItem < count; nItem++)
    m_vecItems.push_back(items[(first + nItem) % count]);

  m_pVecItems = &items;
  m_bStop = false;
  m_bIsLoading = true;
  m_shownItems = shownItems;
  m_awaitingViewport = shownItems > 0;

  m_thread = new CThread(this, "BackgroundLoader");
  m_thread->Create();
  m_thread->SetPriority(ThreadPriority::BELOW_NORMAL);
}

void CBackgroundInfoLoader::StopAsync()
{
  m_bStop = true;
}


void CBackgroundInfoLoader::StopThread()
{
  StopAsync();

  if (m_thread)
  {
    m_thread->StopThread();
    delete m_thread;
    m_thread = NULL;
  }
  Reset();
}

bool CBackgroundInfoLoader::IsLoading()
{
  return m_bIsLoading;
}

void CBackgroundInfoLoader::SetObserver(IBackgroundLoaderObserver* pObserver)
{
  m_pObserver = pObserver;
}

void CBackgroundInfoLoader::SetProgressCallback(IProgressCallback* pCallback)
{
  m_pProgressCallback = pCallback;
}

