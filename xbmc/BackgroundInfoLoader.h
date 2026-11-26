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

#include <chrono>
#include <memory>
#include <optional>
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
   The batch from \p firstItem (LOADER_BATCH_SIZE items, or fewer at the list's end) goes through
   both stages first. The other items follow from there, wrapping around to the list's start.
   \param items the items to load
   \param firstItem the first of the items shown
   */
  void LoadShownFirst(CFileItemList& items, int firstItem);

  /*! \brief Have the running loader do the batch from \p item next, through both stages, then go on
   where it was. Does not wait for the loader, which may be in a slow query.
   \param item the first of the items shown now, one of the items being loaded
   */
  void Prioritize(const CFileItemPtr& item);

  /*! \brief Wait until the loader has done OnLoaderBatch() for the batch of the last
   LoadShownFirst() or Prioritize(), or has ended.
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

  volatile bool m_bIsLoading{false};
  volatile bool m_bStop{true};
  CThread* m_thread{nullptr};

  IBackgroundLoaderObserver* m_pObserver{nullptr};
  IProgressCallback* m_pProgressCallback{nullptr};

private:
  void Start(CFileItemList& items, int firstItem, bool shownFirst);
  void Reset();
  std::optional<size_t> TakeShownRequest();
  bool ShownRequestPending();
  void ShownBatchDone();

  CCriticalSection m_shownLock; //!< for the members below
  CFileItemPtr m_shownRequest; //!< the first item shown, for the loader to do next
  unsigned int m_shownRequested{0}; //!< requests made by LoadShownFirst() and Prioritize()
  unsigned int m_shownTaken{0}; //!< the request the loader took last
  unsigned int m_shownDone{0}; //!< the last request whose batch is done, or all when stopped
  CEvent m_shownBatchDone{true};
};

