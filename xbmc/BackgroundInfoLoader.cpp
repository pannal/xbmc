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
#include <utility>

CBackgroundInfoLoader::CBackgroundInfoLoader() = default;

CBackgroundInfoLoader::~CBackgroundInfoLoader()
{
  StopThread();
}

void CBackgroundInfoLoader::Reset()
{
  m_pVecItems = nullptr;
  m_vecItems.clear();
  m_bIsLoading = false;
  {
    // Nothing more will be done: no one waits for it.
    std::unique_lock<CCriticalSection> lock(m_shownLock);
    m_shownRequest.reset();
    m_shownDone = m_shownRequested;
  }
  m_shownBatchDone.Set();
}

std::optional<size_t> CBackgroundInfoLoader::TakeShownRequest()
{
  CFileItemPtr item;
  {
    std::unique_lock<CCriticalSection> lock(m_shownLock);
    if (!m_shownRequest)
      return std::nullopt;
    item = std::move(m_shownRequest);
    m_shownRequest.reset();
    m_shownTaken = m_shownRequested;
  }
  const auto it = std::find(m_vecItems.begin(), m_vecItems.end(), item);
  if (it != m_vecItems.end())
    return static_cast<size_t>(it - m_vecItems.begin());
  ShownBatchDone(); // not one of these items: nothing to wait for
  return std::nullopt;
}

bool CBackgroundInfoLoader::ShownRequestPending()
{
  std::unique_lock<CCriticalSection> lock(m_shownLock);
  return m_shownRequest != nullptr;
}

void CBackgroundInfoLoader::ShownBatchDone()
{
  {
    std::unique_lock<CCriticalSection> lock(m_shownLock);
    m_shownDone = m_shownTaken;
  }
  m_shownBatchDone.Set();
}

void CBackgroundInfoLoader::Prioritize(const CFileItemPtr& item)
{
  std::unique_lock<CCriticalSection> lock(m_shownLock);
  if (!m_bIsLoading || !item)
    return;
  m_shownRequest = item;
  m_shownRequested++;
}

bool CBackgroundInfoLoader::WaitForShownBatch(std::chrono::milliseconds timeout)
{
  const auto end = std::chrono::steady_clock::now() + timeout;
  while (true)
  {
    {
      std::unique_lock<CCriticalSection> lock(m_shownLock);
      if (m_shownDone == m_shownRequested)
        return true;
      // ShownBatchDone() and Reset() set it after their update under the lock
      m_shownBatchDone.Reset();
    }
    const auto now = std::chrono::steady_clock::now();
    if (now >= end ||
        !m_shownBatchDone.Wait(std::chrono::duration_cast<std::chrono::milliseconds>(end - now)))
    {
      std::unique_lock<CCriticalSection> lock(m_shownLock);
      return m_shownDone == m_shownRequested;
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
      std::vector<bool> batched(count);
      std::vector<bool> cached(count);
      std::vector<bool> looked(count);
      const auto aborted = [this]
      { return (m_pProgressCallback && m_pProgressCallback->Abort()) || m_bStop; };

      // The batch from index; items done in another batch are passed again, a loader skips them.
      const auto batch = [&](size_t index)
      {
        const size_t end = std::min(index + LOADER_BATCH_SIZE, count);
        try
        {
          OnLoaderBatch(m_vecItems.begin() + index, m_vecItems.begin() + end);
        }
        catch (...)
        {
          CLog::Log(LOGERROR, "CBackgroundInfoLoader::OnLoaderBatch - Unhandled exception");
        }
        std::fill(batched.begin() + index, batched.begin() + end, true);
      };
      // Stage 1: All "fast" stuff we have already cached
      const auto loadCached = [&](size_t index)
      {
        if (cached[index])
          return;
        cached[index] = true;
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
        if (looked[index])
          return;
        looked[index] = true;
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
      // The items shown go through both stages before the rest of the list.
      const auto loadShown = [&]()
      {
        const std::optional<size_t> first = TakeShownRequest();
        if (!first || aborted())
          return; // when stopped, Reset() ends the request
        const size_t end = std::min(*first + LOADER_BATCH_SIZE, count);
        if (!std::all_of(batched.begin() + *first, batched.begin() + end,
                         [](bool done) { return done; }))
          batch(*first);
        ShownBatchDone();
        // Items shown now come before those shown before: a newer request ends this one's
        // stages, whose items then follow in order.
        for (size_t index = *first; index < end && !aborted() && !ShownRequestPending(); index++)
          loadCached(index);
        for (size_t index = *first; index < end && !aborted() && !ShownRequestPending(); index++)
          loadLookup(index);
      };

      for (size_t index = 0; index < count && !aborted(); index++)
      {
        loadShown();
        if (aborted())
          break;
        if (!batched[index])
          batch(index);
        if (aborted())
          break;
        loadCached(index);
      }
      for (size_t index = 0; index < count && !aborted(); index++)
      {
        loadShown();
        if (aborted())
          break;
        loadLookup(index);
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
  Start(items, 0, false);
}

void CBackgroundInfoLoader::LoadShownFirst(CFileItemList& items, int firstItem)
{
  Start(items, firstItem, true);
}

void CBackgroundInfoLoader::Start(CFileItemList& items, int firstItem, bool shownFirst)
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
  if (shownFirst)
  {
    std::unique_lock<CCriticalSection> shownLock(m_shownLock);
    m_shownRequest = m_vecItems.front();
    m_shownRequested++;
  }

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

