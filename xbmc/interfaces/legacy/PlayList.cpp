/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PlayList.h"

#include "AddonUtils.h"
#include "PlayListPlayer.h"
#include "ServiceBroker.h"
#include "playlists/PlayListFactory.h"
#include "utils/URIUtils.h"

namespace XBMCAddon
{
  namespace xbmc
  {
    //! @todo need a means to check for a valid construction
    //!  either by throwing an exception or by an "isValid" check
    PlayList::PlayList(int playList) :
      iPlayList(playList), pPlayList(NULL)
    {
      // we do not create our own playlist, just using the ones from playlistplayer
      if (iPlayList != PLAYLIST::TYPE_MUSIC && iPlayList != PLAYLIST::TYPE_VIDEO)
        throw PlayListException("PlayList does not exist");

      pPlayList = &CServiceBroker::GetPlaylistPlayer().GetPlaylist(playList);
      iPlayList = playList;
    }

    PlayList::~PlayList() = default;

    void PlayList::add(const String& url, XBMCAddon::xbmcgui::ListItem* listitem, int index)
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      CFileItemList items;

      if (listitem != nullptr)
      {
        // The optional ListItem may already belong to the playlist.
        listitem->item->SetPath(url);
        items.Add(listitem->item);
      }
      else
      {
        CFileItemPtr item(new CFileItem(url, false));
        item->SetLabel(url);
        items.Add(item);
      }

      pPlayList->Insert(items, index);
    }

    bool PlayList::load(const char* cFileName)
    {
      CFileItem item(cFileName);
      item.SetPath(cFileName);

      if (!item.IsPlayList())
        throw PlayListException("Not a valid playlist");

      std::unique_ptr<PLAYLIST::CPlayList> playlist(PLAYLIST::CPlayListFactory::Create(item));
      if (!playlist)
        return true;

      // Parse the file before acquiring GUI ownership.
      if (!playlist->Load(item.GetPath()))
        return false;

      XBMCAddonUtils::GuiLock lock(languageHook, false);
      CServiceBroker::GetPlaylistPlayer().ClearPlaylist(iPlayList);
      for (int i = 0; i < playlist->size(); ++i)
      {
        CFileItemPtr playListItem = (*playlist)[i];
        if (playListItem->GetLabel().empty())
          playListItem->SetLabel(URIUtils::GetFileName(playListItem->GetPath()));
        pPlayList->Add(playListItem);
      }
      return true;
    }

    void PlayList::remove(const char* filename)
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      pPlayList->Remove(filename);
    }

    void PlayList::clear()
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      pPlayList->Clear();
    }

    int PlayList::size()
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      return pPlayList->size();
    }

    void PlayList::shuffle()
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      pPlayList->Shuffle();
    }

    void PlayList::unshuffle()
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      pPlayList->UnShuffle();
    }

    int PlayList::getposition()
    {
      XBMCAddonUtils::GuiLock lock(languageHook, false);
      return CServiceBroker::GetPlaylistPlayer().GetCurrentItemIdx();
    }

    XBMCAddon::xbmcgui::ListItem* PlayList::operator [](long i)
    {
      CFileItemPtr item;
      {
        XBMCAddonUtils::GuiLock lock(languageHook, false);
        const int playlistSize = pPlayList->size();
        long position = i;
        if (position < 0)
          position += playlistSize;
        if (position < 0 || position >= playlistSize)
          throw PlayListException("array out of bound");
        item = (*pPlayList)[position];
      }
      return new XBMCAddon::xbmcgui::ListItem(item);
    }
  }
}

