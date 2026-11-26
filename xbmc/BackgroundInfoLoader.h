/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "IProgressCallback.h"
#include "threads/CriticalSection.h"
#include "threads/Event.h"
#include "threads/IRunnable.h"

#include <atomic>
#include <chrono>
#include <memory>
#include <vector>

class CFileItem; typedef std::shared_ptr<CFileItem> CFileItemPtr;
class CFileItemList;
class CThread;

class IBackgroundLoaderObserver
{
public:
  virtual ~IBackgroundLoaderObserver() = default;
  virtual void OnItemLoaded(CFileItem* pItem) = 0;
};

class CBackgroundInfoLoader : public IRunnable
{
public:
  CBackgroundInfoLoader();
  ~CBackgroundInfoLoader() override;

  void Load(CFileItemList& items);

  /*! \brief Start loading the items in a background thread, the shown items first.
   The shown items go through both stages first, in batches of up to LOADER_BATCH_SIZE.
   The remaining items follow in list order, wrapping at the end.
   \param items the items to load
   \param firstItem the first shown item
   \param shownItems number of shown items; the first batch is always prioritized in full
   */
  void LoadShownFirst(CFileItemList& items, int firstItem, size_t shownItems = LOADER_BATCH_SIZE);

  /*! \brief Wait for the shown items to finish OnLoaderBatch(), or for the loader to end.
   \param timeout how long to wait at most
   \return true if it has
   */
  bool WaitForShownBatch(std::chrono::milliseconds timeout);

  /*! \brief Items per OnLoaderBatch(). Stopping the loader waits for a running batch. */
  static constexpr size_t LOADER_BATCH_SIZE = 100;

  bool IsLoading();
  void Run() override;
  void SetObserver(IBackgroundLoaderObserver* pObserver);
  void SetProgressCallback(IProgressCallback* pCallback);
  virtual bool LoadItem(CFileItem* pItem) { return false; }
  virtual bool LoadItemCached(CFileItem* pItem) { return false; }
  virtual bool LoadItemLookup(CFileItem* pItem) { return false; }

  void StopThread(); // will actually stop the loader thread.
  void StopAsync();  // will ask loader to stop as soon as possible, but not block

protected:
  virtual void OnLoaderStart() {}
  /*! \brief Called on the loader thread before the first stage of each batch of up to
   LOADER_BATCH_SIZE items, once per item. Lets a loader do work for a batch with one query,
   while the items loaded first are not held up by the rest of the list.
   */
  virtual void OnLoaderBatch(std::vector<CFileItemPtr>::const_iterator begin,
                             std::vector<CFileItemPtr>::const_iterator end)
  {
  }
  virtual void OnLoaderFinish() {}

  CFileItemList* m_pVecItems{nullptr};
  std::vector<CFileItemPtr> m_vecItems; // FileItemList would delete the items and we only want to keep a reference.
  CCriticalSection m_lock;

  std::atomic<bool> m_bIsLoading{false};
  std::atomic<bool> m_bStop{true};
  CThread* m_thread{nullptr};

  IBackgroundLoaderObserver* m_pObserver{nullptr};
  IProgressCallback* m_pProgressCallback{nullptr};

private:
  void Start(CFileItemList& items, int firstItem, size_t shownItems);
  void Reset();
  size_t m_shownItems{0};
  CEvent m_shownBatchDone{true, true};
};

