/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "interfaces/IAnnouncer.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"

#include <atomic>
#include "settings/lib/ISettingCallback.h"

class CDolbyVisionAML : public ANNOUNCEMENT::IAnnouncer, // Application callback
                        public ISettingCallback          // Settings callback
{
public:
  CDolbyVisionAML();
  ~CDolbyVisionAML() override;
  bool ContinueAnnounce() override;
  void Retire();

  // Setup
  bool Setup();

  // implementation of IAnnouncer
  void Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                const std::string& sender,
                const std::string& message,
                const CVariant& data) override;

  // implementation of ISettingCallback
  void OnSettingChanged(const std::shared_ptr<const CSetting>& setting) override;
private:
  enum class Pending { NONE, START, RESTORE };
  Pending m_pending{Pending::NONE}; // announcement owner only
  std::shared_ptr<CAMLSession::NativeRequest> m_nativeRequest; // atomic load/store
  std::atomic<bool> m_retiring{false};
  bool m_registered{false};
};