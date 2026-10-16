/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PlayList.h"

#include "FileItem.h"
#include "PlayListFactory.h"
#include "ServiceBroker.h"
#include "filesystem/File.h"
#include "interfaces/AnnouncementManager.h"
#include "music/tags/MusicInfoTag.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/Variant.h"
#include "utils/log.h"

#include <algorithm>
#include <iostream>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <utility>
#include <vector>


using namespace MUSIC_INFO;
using namespace XFILE;
using namespace PLAYLIST;

CPlayList::CPlayList(Id id /* = PLAYLIST::TYPE_NONE */) : m_id(id)
{
  m_iPlayableItems = -1;
  m_bShuffled = false;
  m_bWasPlayed = false;
}

CPlayList::CPlayList(const CPlayList& playlist) : m_id(TYPE_NONE)
{
  std::unique_lock lock(playlist.m_critSection);
  m_id = playlist.m_id;
  m_strPlayListName = playlist.m_strPlayListName;
  m_strBasePath = playlist.m_strBasePath;
  m_iPlayableItems = playlist.m_iPlayableItems;
  m_bShuffled = playlist.m_bShuffled;
  m_bWasPlayed = playlist.m_bWasPlayed;
  m_vecItems = playlist.m_vecItems;
}

CPlayList& CPlayList::operator=(const CPlayList& playlist)
{
  if (this == &playlist)
    return *this;
  CPlayList snapshot(playlist);
  {
    std::unique_lock lock(m_critSection);
    m_id = snapshot.m_id;
    m_strPlayListName = std::move(snapshot.m_strPlayListName);
    m_strBasePath = std::move(snapshot.m_strBasePath);
    m_iPlayableItems = snapshot.m_iPlayableItems;
    m_bShuffled = snapshot.m_bShuffled;
    m_bWasPlayed = snapshot.m_bWasPlayed;
    m_vecItems.swap(snapshot.m_vecItems);
  }
  return *this;
}

void CPlayList::AnnounceRemove(int pos)
{
  Id id;
  {
    std::unique_lock lock(m_critSection);
    id = m_id;
  }
  if (id == TYPE_NONE)
    return;

  CVariant data;
  data["playlistid"] = id;
  data["position"] = pos;
  CServiceBroker::GetAnnouncementManager()->Announce(ANNOUNCEMENT::Playlist, "OnRemove", data);
}

void CPlayList::AnnounceClear()
{
  Id id;
  {
    std::unique_lock lock(m_critSection);
    id = m_id;
  }
  if (id == TYPE_NONE)
    return;

  CVariant data;
  data["playlistid"] = id;
  CServiceBroker::GetAnnouncementManager()->Announce(ANNOUNCEMENT::Playlist, "OnClear", data);
}

void CPlayList::AnnounceAdd(const std::shared_ptr<CFileItem>& item, int pos)
{
  Id id;
  {
    std::unique_lock lock(m_critSection);
    id = m_id;
  }
  if (id == TYPE_NONE)
    return;

  CVariant data;
  data["playlistid"] = id;
  data["position"] = pos;
  CServiceBroker::GetAnnouncementManager()->Announce(ANNOUNCEMENT::Playlist, "OnAdd", item, data);
}

int CPlayList::AddLocked(const std::shared_ptr<CFileItem>& item, int iPosition, int iOrder)
{
  const int iOldSize = m_vecItems.size();
  if (iPosition < 0 || iPosition >= iOldSize)
    iPosition = iOldSize;
  item->m_iprogramCount = (iOrder < 0 || iOrder >= iOldSize) ? iOldSize : iOrder;

  item->ClearProperty("unplayable");
  if (m_iPlayableItems < 0)
    m_iPlayableItems = 1;
  else
    m_iPlayableItems++;
  item->SetProperty("IsPlayable", true);

  m_vecItems.insert(m_vecItems.begin() + iPosition, item);
  if (iPosition < iOldSize && iOrder < iOldSize)
    IncrementOrder(iPosition + 1, iOrder);
  return iPosition;
}

void CPlayList::Add(const std::shared_ptr<CFileItem>& item)
{
  int position;
  {
    std::unique_lock lock(m_critSection);
    position = AddLocked(item, -1, -1);
  }
  AnnounceAdd(item, position);
}

void CPlayList::Add(const CPlayList& playlist)
{
  InsertItems(playlist.GetItems(), -1);
}

void CPlayList::Add(const CFileItemList& items)
{
  Insert(items, -1);
}

void CPlayList::InsertItems(const std::vector<std::shared_ptr<CFileItem>>& items, int position)
{
  std::vector<int> positions;
  positions.reserve(items.size());
  {
    std::unique_lock lock(m_critSection);
    const bool append = position < 0 || position >= static_cast<int>(m_vecItems.size());
    for (const auto& item : items)
    {
      positions.push_back(AddLocked(item, append ? -1 : position, append ? -1 : position));
      if (!append)
        ++position;
    }
  }
  for (size_t i = 0; i < items.size(); ++i)
    AnnounceAdd(items[i], positions[i]);
}

void CPlayList::Insert(const CPlayList& playlist, int position)
{
  InsertItems(playlist.GetItems(), position);
}

void CPlayList::Insert(const CFileItemList& items, int position)
{
  std::vector<CFileItemPtr> entries;
  entries.reserve(items.Size());
  for (int i = 0; i < items.Size(); ++i)
    entries.push_back(items[i]);
  InsertItems(entries, position);
}

void CPlayList::Insert(const std::shared_ptr<CFileItem>& item, int position)
{
  {
    std::unique_lock lock(m_critSection);
    position = AddLocked(item, position, position);
  }
  AnnounceAdd(item, position);
}

void CPlayList::DecrementOrder(int iOrder)
{
  if (iOrder < 0) return;

  // it was the last item so do nothing
  if (iOrder == static_cast<int>(m_vecItems.size())) return;

  // fix all items with an order greater than the removed iOrder
  ivecItems it;
  it = m_vecItems.begin();
  while (it != m_vecItems.end())
  {
    CFileItemPtr item = *it;
    if (item->m_iprogramCount > iOrder)
    {
      //CLog::Log(LOGDEBUG,"{} fixing item at order {}", __FUNCTION__, item->m_iprogramCount);
      item->m_iprogramCount--;
    }
    ++it;
  }
}

void CPlayList::IncrementOrder(int iPosition, int iOrder)
{
  if (iOrder < 0) return;

  // fix all items with an order equal or greater to the added iOrder at iPos
  ivecItems it;
  it = m_vecItems.begin() + iPosition;
  while (it != m_vecItems.end())
  {
    CFileItemPtr item = *it;
    if (item->m_iprogramCount >= iOrder)
    {
      //CLog::Log(LOGDEBUG,"{} fixing item at order {}", __FUNCTION__, item->m_iprogramCount);
      item->m_iprogramCount++;
    }
    ++it;
  }
}

void CPlayList::Clear()
{
  std::vector<CFileItemPtr> removed;
  {
    std::unique_lock lock(m_critSection);
    removed.swap(m_vecItems);
    m_strPlayListName.clear();
    m_iPlayableItems = -1;
    m_bWasPlayed = false;
  }
  if (!removed.empty())
    AnnounceClear();
}

int CPlayList::size() const
{
  std::unique_lock lock(m_critSection);
  return m_vecItems.size();
}

std::shared_ptr<CFileItem> CPlayList::TryGet(int index) const
{
  return TryGet(index, false);
}

std::shared_ptr<CFileItem> CPlayList::TryGet(long index, bool relativeToEnd) const
{
  std::unique_lock lock(m_critSection);
  if (relativeToEnd && index < 0)
    index += static_cast<long>(m_vecItems.size());
  if (index < 0 || static_cast<size_t>(index) >= m_vecItems.size())
    return {};
  return m_vecItems[index];
}

std::vector<std::shared_ptr<CFileItem>> CPlayList::GetItems() const
{
  std::unique_lock lock(m_critSection);
  return m_vecItems;
}

const std::shared_ptr<CFileItem> CPlayList::operator[](int index) const
{
  return TryGet(index);
}

std::shared_ptr<CFileItem> CPlayList::operator[](int index)
{
  return TryGet(index);
}

void CPlayList::Shuffle(int iPosition)
{
  std::mt19937 random{std::random_device{}()};
  std::unique_lock lock(m_critSection);
  if (m_vecItems.empty())
  {
    m_bShuffled = true;
    return;
  }
  if (iPosition >= static_cast<int>(m_vecItems.size()))
    return;
  if (iPosition < 0)
    iPosition = 0;

  std::shuffle(m_vecItems.begin() + iPosition, m_vecItems.end(), random);
  m_bShuffled = true;
  lock.unlock();
  CLog::Log(LOGDEBUG, "{} shuffling at pos:{}", __FUNCTION__, iPosition);
}

struct SSortPlayListItem
{
  static bool PlaylistSort(const CFileItemPtr &left, const CFileItemPtr &right)
  {
    return (left->m_iprogramCount < right->m_iprogramCount);
  }
};

void CPlayList::UnShuffle()
{
  std::unique_lock lock(m_critSection);
  std::sort(m_vecItems.begin(), m_vecItems.end(), SSortPlayListItem::PlaylistSort);
  // the list is now unshuffled!
  m_bShuffled = false;
}

std::string CPlayList::GetName() const
{
  std::unique_lock lock(m_critSection);
  return m_strPlayListName;
}

bool CPlayList::IsShuffled() const
{
  std::unique_lock lock(m_critSection);
  return m_bShuffled;
}

void CPlayList::SetPlayed(bool bPlayed)
{
  std::unique_lock lock(m_critSection);
  m_bWasPlayed = true;
}

bool CPlayList::WasPlayed() const
{
  std::unique_lock lock(m_critSection);
  return m_bWasPlayed;
}

int CPlayList::GetPlayable() const
{
  std::unique_lock lock(m_critSection);
  return m_iPlayableItems;
}

void CPlayList::Remove(const std::string& strFileName)
{
  std::vector<CFileItemPtr> removed;
  std::vector<int> positions;
  {
    std::unique_lock lock(m_critSection);
    removed.reserve(m_vecItems.size());
    positions.reserve(m_vecItems.size());
    int order = -1;
    int position = 0;
    auto entry = m_vecItems.begin();
    while (entry != m_vecItems.end())
    {
      if ((*entry)->GetPath() == strFileName)
      {
        order = (*entry)->m_iprogramCount;
        removed.push_back(std::move(*entry));
        entry = m_vecItems.erase(entry);
        positions.push_back(position);
      }
      else
      {
        ++position;
        ++entry;
      }
    }
    DecrementOrder(order);
  }
  for (int position : positions)
    AnnounceRemove(position);
}

int CPlayList::FindOrder(int iOrder) const
{
  std::unique_lock lock(m_critSection);
  for (size_t i = 0; i < m_vecItems.size(); i++)
  {
    if (m_vecItems[i]->m_iprogramCount == iOrder)
      return i;
  }
  return -1;
}

// remove item from playlist by position
void CPlayList::Remove(int position)
{
  CFileItemPtr removed;
  {
    std::unique_lock lock(m_critSection);
    int order = -1;
    if (position >= 0 && position < static_cast<int>(m_vecItems.size()))
    {
      order = m_vecItems[position]->m_iprogramCount;
      removed = std::move(m_vecItems[position]);
      m_vecItems.erase(m_vecItems.begin() + position);
    }
    DecrementOrder(order);
  }
  AnnounceRemove(position);
}

int CPlayList::RemoveDVDItems()
{
  std::vector<std::string> filenames;
  for (const auto& item : GetItems())
  {
    if (item->IsCDDA() || item->IsOnDVD())
      filenames.push_back(item->GetPath());
  }
  for (const auto& filename : filenames)
    Remove(filename);
  return filenames.size();
}

bool CPlayList::Swap(int position1, int position2)
{
  std::unique_lock lock(m_critSection);
  if (
    (position1 < 0) ||
    (position2 < 0) ||
    (position1 >= static_cast<int>(m_vecItems.size())) ||
    (position2 >= static_cast<int>(m_vecItems.size()))
  )
  {
    return false;
  }

  if (!m_bShuffled)
  {
    // swap the ordinals before swapping the items!
    //CLog::Log(LOGDEBUG,"PLAYLIST swapping items at orders ({}, {})",m_vecItems[position1]->m_iprogramCount,m_vecItems[position2]->m_iprogramCount);
    std::swap(m_vecItems[position1]->m_iprogramCount, m_vecItems[position2]->m_iprogramCount);
  }

  // swap the items
  std::swap(m_vecItems[position1], m_vecItems[position2]);
  return true;
}

void CPlayList::SetUnPlayable(int iItem)
{
  std::unique_lock lock(m_critSection);
  if (iItem < 0 || iItem >= static_cast<int>(m_vecItems.size()))
  {
    lock.unlock();
    CLog::Log(LOGWARNING, "Attempt to set unplayable index {}", iItem);
    return;
  }

  CFileItemPtr item = m_vecItems[iItem];
  if (!item->GetProperty("unplayable").asBoolean())
  {
    item->SetProperty("unplayable", true);
    m_iPlayableItems--;
  }
}


bool CPlayList::Load(const std::string& strFileName)
{
  Clear();
  const std::string basePath = URIUtils::GetDirectory(strFileName);
  {
    std::unique_lock lock(m_critSection);
    m_strBasePath = basePath;
  }

  CFileStream file;
  if (!file.Open(strFileName))
    return false;

  if (file.GetLength() > 1024*1024)
  {
    CLog::Log(LOGWARNING, "{} - File is larger than 1 MB, most likely not a playlist",
              __FUNCTION__);
    return false;
  }

  return LoadData(file);
}

bool CPlayList::LoadData(std::istream &stream)
{
  // try to read as a string
  std::ostringstream ostr;
  ostr << stream.rdbuf();
  return LoadData(ostr.str());
}

bool CPlayList::LoadData(const std::string& strData)
{
  return false;
}


bool CPlayList::Expand(int position)
{
  const CFileItemPtr item = TryGet(position);
  if (!item)
    return false;
  std::unique_ptr<CPlayList> playlist (CPlayListFactory::Create(*item.get()));
  if (playlist == nullptr)
    return false;

  std::string path = item->GetDynPath();

  if (!playlist->Load(path))
    return false;

  // remove any item that points back to itself
  for (int i = 0;i<playlist->size();i++)
  {
    if (StringUtils::EqualsNoCase((*playlist)[i]->GetPath(), path))
    {
      playlist->Remove(i);
      i--;
    }
  }

  // @todo
  // never change original path (id) of a file item
  for (int i = 0;i<playlist->size();i++)
  {
    (*playlist)[i]->SetDynPath((*playlist)[i]->GetPath());
    (*playlist)[i]->SetPath(item->GetDynPath());
    (*playlist)[i]->SetStartOffset(item->GetStartOffset());
  }

  if (playlist->size() <= 0)
    return false;

  const auto entries = playlist->GetItems();
  std::vector<int> positions;
  positions.reserve(entries.size());
  {
    std::unique_lock lock(m_critSection);
    if (position < 0 || position >= static_cast<int>(m_vecItems.size()) ||
        m_vecItems[position] != item)
      return false;
    const int order = item->m_iprogramCount;
    m_vecItems.erase(m_vecItems.begin() + position);
    DecrementOrder(order);
    for (const auto& entry : entries)
    {
      const int next = position + positions.size();
      positions.push_back(AddLocked(entry, next, next));
    }
  }
  AnnounceRemove(position);
  for (size_t i = 0; i < entries.size(); ++i)
    AnnounceAdd(entries[i], positions[i]);
  return true;
}

void CPlayList::UpdateItem(const CFileItem *item)
{
  if (!item) return;

  // Assignment can release GUI resources. Retain the shared item outside the leaf lock.
  for (const auto& playlistItem : GetItems())
  {
    if (playlistItem->IsSamePath(item))
    {
      std::string temp = playlistItem->GetPath(); // save path, it may have been altered
      *playlistItem = *item;
      playlistItem->SetPath(temp);
      break;
    }
  }
}

const std::string& CPlayList::ResolveURL(const std::shared_ptr<CFileItem>& item) const
{
  if (item->IsMusicDb() && item->HasMusicInfoTag())
    return item->GetMusicInfoTag()->GetURL();
  else
    return item->GetDynPath();
}
