/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PlaylistDirectory.h"

#include "FileItem.h"
#include "PlayListPlayer.h"
#include "ServiceBroker.h"
#include "URL.h"
#include "playlists/PlayList.h"

using namespace XFILE;

CPlaylistDirectory::CPlaylistDirectory() = default;

CPlaylistDirectory::~CPlaylistDirectory() = default;

bool CPlaylistDirectory::GetDirectory(const CURL& url, CFileItemList &items)
{
  PLAYLIST::Id playlistId = PLAYLIST::TYPE_NONE;
  if (url.IsProtocol("playlistmusic"))
    playlistId = PLAYLIST::TYPE_MUSIC;
  else if (url.IsProtocol("playlistvideo"))
    playlistId = PLAYLIST::TYPE_VIDEO;

  if (playlistId == PLAYLIST::TYPE_NONE)
    return false;

  const PLAYLIST::CPlayList& playlist = CServiceBroker::GetPlaylistPlayer().GetPlaylist(playlistId);
  const auto entries = playlist.GetItems();
  items.Reserve(entries.size());

  for (size_t i = 0; i < entries.size(); ++i)
  {
    const CFileItemPtr& item = entries[i];
    item->SetProperty("playlistposition", static_cast<int>(i));
    item->SetProperty("playlisttype", playlistId);
    //item->m_iprogramCount = i; // the programCount is set as items are added!
    items.Add(item);
  }

  return true;
}
