/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "interfaces/IAnnouncer.h"
#include "AMLDeferredWork.h"
#include "settings/lib/SettingCallbackRegistration.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"

#include <atomic>
#include <mutex>
#include <string>
#include <vector>
#include "settings/lib/ISettingCallback.h"

class CDolbyVisionAML : public ANNOUNCEMENT::IAnnouncer, // Application callback
                        public ISettingCallback          // Settings callback
{
public:
  CDolbyVisionAML();
  ~CDolbyVisionAML() override;
  bool ContinueAnnounce() override;
  bool Retire();

  // Setup
  bool Setup(CAMLSession::DisplayRequest display = {});

  // implementation of IAnnouncer
  void Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                const std::string& sender,
                const std::string& message,
                const CVariant& data) override;

  // implementation of ISettingCallback
  void OnSettingChanged(const std::shared_ptr<const CSetting>& setting) override;
private:
  void apply_tv_preset(int preset);
  void schedule_tv_preset_apply(int preset);
  void schedule_vsvdb_payload_apply();
  void schedule_native_setting_apply(const std::string& settingId);
  void apply_native_setting(const std::string& settingId);
  std::mutex m_nativeSettingsMutex;
  std::vector<std::string> m_nativeSettingsPending;
  bool m_nativeSettingsScheduled{false}; // guarded by m_nativeSettingsMutex
  CAMLDeferredWork m_deferredWork;
  std::shared_ptr<CSettingCallbackRegistration> m_settingsRetirement;
  std::atomic<bool> m_applying_tv_preset{false};
  std::atomic<bool> m_tv_preset_apply_scheduled{false};
  std::mutex m_tvPresetMutex;
  int m_tv_preset_pending{0}; // guarded by m_tvPresetMutex
  std::atomic<bool> m_vsvdb_apply_scheduled{false};
  std::atomic<bool> m_applying_vsvdb{false};
  enum class Pending { NONE, START, RESTORE };
  Pending m_pending{Pending::NONE}; // announcement owner only
  std::shared_ptr<CAMLSession::NativeRequest> m_nativeRequest; // atomic load/store
  std::atomic<bool> m_retiring{false};
  bool m_registered{false};
};