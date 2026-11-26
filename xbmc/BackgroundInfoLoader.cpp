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
  m_shownBatchDone.Set();
}

bool CBackgroundInfoLoader::WaitForShownBatch(std::chrono::milliseconds timeout)
{
  return m_shownBatchDone.Wait(timeout);
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

      const auto batch = [&](size_t begin, size_t end)
      {
        try
        {
          OnLoaderBatch(m_vecItems.begin() + begin, m_vecItems.begin() + end);
        }
        catch (...)
        {
          CLog::Log(LOGERROR, "CBackgroundInfoLoader::OnLoaderBatch - Unhandled exception");
        }
      };
      // Stage 1: All "fast" stuff we have already cached
      const auto loadCached = [&](size_t index)
      {
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
      // Fill all artwork in the viewport before waking the GUI, even when it spans two batches.
      const size_t shownEnd = std::min(m_shownItems, count);
      if (shownEnd > 0)
      {
        for (size_t begin = 0; begin < shownEnd && !aborted(); begin += LOADER_BATCH_SIZE)
          batch(begin, std::min(begin + LOADER_BATCH_SIZE, shownEnd));
        m_shownBatchDone.Set();
        for (size_t index = 0; index < shownEnd && !aborted(); ++index)
          loadCached(index);
        for (size_t index = 0; index < shownEnd && !aborted(); ++index)
          loadLookup(index);
      }

      // Ordinary loads and the rest of a visible-first load keep the two list-wide passes.
      for (size_t begin = shownEnd; begin < count && !aborted(); begin += LOADER_BATCH_SIZE)
      {
        const size_t end = std::min(begin + LOADER_BATCH_SIZE, count);
        batch(begin, end);
        for (size_t index = begin; index < end && !aborted(); ++index)
          loadCached(index);
      }
      for (size_t index = shownEnd; index < count && !aborted(); ++index)
        loadLookup(index);
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
  if (shownItems > 0)
    m_shownBatchDone.Reset();

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

