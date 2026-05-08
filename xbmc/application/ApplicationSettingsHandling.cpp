/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ApplicationSettingsHandling.h"

#include "ServiceBroker.h"
#include "addons/AddonManager.h"
#include "addons/addoninfo/AddonType.h"
#include "addons/gui/GUIDialogAddonSettings.h"
#include "application/ApplicationComponents.h"
#include "application/ApplicationPlayer.h"
#include "application/ApplicationPowerHandling.h"
#include "application/ApplicationSkinHandling.h"
#include "application/ApplicationVolumeHandling.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "messaging/ApplicationMessenger.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "settings/lib/Setting.h"
#include "settings/lib/SettingsManager.h"
#include "utils/XMLUtils.h"
#if defined(TARGET_DARWIN_OSX)
#include "utils/StringUtils.h"
#endif

namespace
{
constexpr const char* AUDIO_RESTORE_PASSTHROUGH_TAG = "restorepassthroughonaml";
constexpr const char* AUDIO_AML_CHANNELS_TAG = "amlchannels";
constexpr const char* AML_PASSTHROUGH_DEVICE =
    "ALSA:hdmi:CARD=AMLAUGESOUND,DEV=0|AML-AUGESOUND";
constexpr int STEREO_CHANNELS_SETTING = 1; // AE_CH_LAYOUT_2_0

bool IsPlaying(const std::string& condition,
               const std::string& value,
               const SettingConstPtr& setting,
               void* data)
{
  return data ? static_cast<CApplicationPlayer*>(data)->IsPlaying() : false;
}
} // namespace

void CApplicationSettingsHandling::RegisterSettings()
{
  const std::shared_ptr<CSettings> settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  CSettingsManager* settingsMgr = settings->GetSettingsManager();

  settingsMgr->RegisterSettingsHandler(this);

  settingsMgr->RegisterCallback(this, {CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH,
                                       CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE,
                                       CSettings::SETTING_AUDIOOUTPUT_CHANNELS,
                                       CSettings::SETTING_LOOKANDFEEL_SKIN,
                                       CSettings::SETTING_LOOKANDFEEL_SKINSETTINGS,
                                       CSettings::SETTING_LOOKANDFEEL_FONT,
                                       CSettings::SETTING_LOOKANDFEEL_SKINTHEME,
                                       CSettings::SETTING_LOOKANDFEEL_SKINCOLORS,
                                       CSettings::SETTING_LOOKANDFEEL_SKINZOOM,
                                       CSettings::SETTING_MUSICPLAYER_REPLAYGAINPREAMP,
                                       CSettings::SETTING_MUSICPLAYER_REPLAYGAINNOGAINPREAMP,
                                       CSettings::SETTING_MUSICPLAYER_REPLAYGAINTYPE,
                                       CSettings::SETTING_MUSICPLAYER_REPLAYGAINAVOIDCLIPPING,
                                       CSettings::SETTING_SCRAPERS_MUSICVIDEOSDEFAULT,
                                       CSettings::SETTING_SCREENSAVER_MODE,
                                       CSettings::SETTING_SCREENSAVER_PREVIEW,
                                       CSettings::SETTING_SCREENSAVER_SETTINGS,
                                       CSettings::SETTING_AUDIOCDS_SETTINGS,
                                       CSettings::SETTING_VIDEOSCREEN_GUICALIBRATION,
                                       CSettings::SETTING_VIDEOSCREEN_TESTPATTERN,
                                       CSettings::SETTING_VIDEOPLAYER_USEAMCODEC,
                                       CSettings::SETTING_VIDEOPLAYER_USEMEDIACODEC,
                                       CSettings::SETTING_VIDEOPLAYER_USEMEDIACODECSURFACE,
                                       CSettings::SETTING_AUDIOOUTPUT_VOLUMESTEPS,
                                       CSettings::SETTING_SOURCE_VIDEOS,
                                       CSettings::SETTING_SOURCE_MUSIC,
                                       CSettings::SETTING_SOURCE_PICTURES,
                                       CSettings::SETTING_VIDEOSCREEN_FAKEFULLSCREEN});

  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  if (!appPlayer)
    return;

  settingsMgr->RegisterCallback(
      &appPlayer->GetSeekHandler(),
      {CSettings::SETTING_VIDEOPLAYER_SEEKDELAY, CSettings::SETTING_VIDEOPLAYER_SEEKSTEPS,
       CSettings::SETTING_MUSICPLAYER_SEEKDELAY, CSettings::SETTING_MUSICPLAYER_SEEKSTEPS});

  settingsMgr->AddDynamicCondition("isplaying", IsPlaying, appPlayer.get());

  settings->RegisterSubSettings(this);
}

void CApplicationSettingsHandling::UnregisterSettings()
{
  const std::shared_ptr<CSettings> settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  CSettingsManager* settingsMgr = settings->GetSettingsManager();
  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  if (!appPlayer)
    return;

  settings->UnregisterSubSettings(this);
  settingsMgr->RemoveDynamicCondition("isplaying");
  settingsMgr->UnregisterCallback(&appPlayer->GetSeekHandler());
  settingsMgr->UnregisterCallback(this);
  settingsMgr->UnregisterSettingsHandler(this);
}

void CApplicationSettingsHandling::OnSettingChanged(const std::shared_ptr<const CSetting>& setting)
{
  if (!setting)
    return;

  auto& components = CServiceBroker::GetAppComponents();
  const auto appSkin = components.GetComponent<CApplicationSkinHandling>();
  if (appSkin->OnSettingChanged(*setting))
    return;

  const auto appVolume = components.GetComponent<CApplicationVolumeHandling>();
  if (appVolume->OnSettingChanged(*setting))
    return;

  const auto appPower = components.GetComponent<CApplicationPowerHandling>();
  if (appPower->OnSettingChanged(*setting))
    return;

  const std::string& settingId = setting->GetId();

  if (settingId == CSettings::SETTING_VIDEOSCREEN_FAKEFULLSCREEN)
  {
    if (CServiceBroker::GetWinSystem()->GetGfxContext().IsFullScreenRoot())
      CServiceBroker::GetWinSystem()->GetGfxContext().SetVideoResolution(
          CServiceBroker::GetWinSystem()->GetGfxContext().GetVideoResolution(), true);
  }
  else if (settingId == CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH)
  {
    if (m_ignoreNextPassthroughChange)
      m_ignoreNextPassthroughChange = false;
    else
    {
      const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
      if (settings != nullptr &&
          CApplicationVolumeHandling::IsAmlAudioDevice(
              settings->GetString(CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE)))
      {
        m_restorePassthroughOnAml =
            std::static_pointer_cast<const CSettingBool>(setting)->GetValue();
      }
    }

    CServiceBroker::GetAppMessenger()->PostMsg(TMSG_MEDIA_RESTART);
  }
  else if (settingId == CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE)
  {
    const auto audioDevice = std::static_pointer_cast<const CSettingString>(setting)->GetValue();
    appVolume->SetAudioDevice(audioDevice);

    const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
    if (settings != nullptr)
    {
      const bool passthroughEnabled = settings->GetBool(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH);
      const bool isAmlAudioDevice = CApplicationVolumeHandling::IsAmlAudioDevice(audioDevice);

      if (!isAmlAudioDevice)
      {
        bool saveSettings = false;
        const int channels = settings->GetInt(CSettings::SETTING_AUDIOOUTPUT_CHANNELS);
        if (channels != STEREO_CHANNELS_SETTING)
        {
          m_amlChannels = channels;
          m_hasAmlChannels = true;
          settings->SetInt(CSettings::SETTING_AUDIOOUTPUT_CHANNELS, STEREO_CHANNELS_SETTING);
          saveSettings = true;
        }

        if (passthroughEnabled)
        {
          m_restorePassthroughOnAml = true;
          m_ignoreNextPassthroughChange = true;
          settings->SetBool(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH, false);
          saveSettings = true;
        }

        if (saveSettings)
          settings->Save();
      }
      else
      {
        if (m_hasAmlChannels &&
            settings->GetInt(CSettings::SETTING_AUDIOOUTPUT_CHANNELS) != m_amlChannels)
        {
          settings->SetInt(CSettings::SETTING_AUDIOOUTPUT_CHANNELS, m_amlChannels);
        }

        if (m_restorePassthroughOnAml)
        {
          if (settings->GetString(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGHDEVICE) !=
              AML_PASSTHROUGH_DEVICE)
          {
            settings->SetString(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGHDEVICE,
                                AML_PASSTHROUGH_DEVICE);
          }

          if (!passthroughEnabled)
          {
            m_ignoreNextPassthroughChange = true;
            settings->SetBool(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH, true);
          }
        }

        settings->Save();
      }
    }
  }
  else if (settingId == CSettings::SETTING_AUDIOOUTPUT_CHANNELS)
  {
    const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
    if (settings != nullptr &&
        CApplicationVolumeHandling::IsAmlAudioDevice(
            settings->GetString(CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE)))
    {
      m_amlChannels = std::static_pointer_cast<const CSettingInt>(setting)->GetValue();
      m_hasAmlChannels = true;
    }
  }
}

void CApplicationSettingsHandling::OnSettingAction(const std::shared_ptr<const CSetting>& setting)
{
  if (!setting)
    return;

  auto& components = CServiceBroker::GetAppComponents();
  const auto appPower = components.GetComponent<CApplicationPowerHandling>();
  if (appPower->OnSettingAction(*setting))
    return;

  const std::string& settingId = setting->GetId();
  if (settingId == CSettings::SETTING_LOOKANDFEEL_SKINSETTINGS)
    CServiceBroker::GetGUI()->GetWindowManager().ActivateWindow(WINDOW_SKIN_SETTINGS);
  else if (settingId == CSettings::SETTING_AUDIOCDS_SETTINGS)
  {
    ADDON::AddonPtr addon;
    if (CServiceBroker::GetAddonMgr().GetAddon(
            CServiceBroker::GetSettingsComponent()->GetSettings()->GetString(
                CSettings::SETTING_AUDIOCDS_ENCODER),
            addon, ADDON::AddonType::AUDIOENCODER, ADDON::OnlyEnabled::CHOICE_YES))
      CGUIDialogAddonSettings::ShowForAddon(addon);
  }
  else if (settingId == CSettings::SETTING_VIDEOSCREEN_GUICALIBRATION)
    CServiceBroker::GetGUI()->GetWindowManager().ActivateWindow(WINDOW_SCREEN_CALIBRATION);
  else if (settingId == CSettings::SETTING_SOURCE_VIDEOS)
  {
    std::vector<std::string> params{"library://video/files.xml", "return"};
    CServiceBroker::GetGUI()->GetWindowManager().ActivateWindow(WINDOW_VIDEO_NAV, params);
  }
  else if (settingId == CSettings::SETTING_SOURCE_MUSIC)
  {
    std::vector<std::string> params{"library://music/files.xml", "return"};
    CServiceBroker::GetGUI()->GetWindowManager().ActivateWindow(WINDOW_MUSIC_NAV, params);
  }
  else if (settingId == CSettings::SETTING_SOURCE_PICTURES)
    CServiceBroker::GetGUI()->GetWindowManager().ActivateWindow(WINDOW_PICTURES);
}

bool CApplicationSettingsHandling::OnSettingUpdate(const std::shared_ptr<CSetting>& setting,
                                                   const char* oldSettingId,
                                                   const TiXmlNode* oldSettingNode)
{
  if (!setting)
    return false;

#if defined(TARGET_DARWIN_OSX)
  if (setting->GetId() == CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE)
  {
    std::shared_ptr<CSettingString> audioDevice = std::static_pointer_cast<CSettingString>(setting);
    // Gotham and older didn't enumerate audio devices per stream on osx
    // add stream0 per default which should be ok for all old settings.
    if (!StringUtils::EqualsNoCase(audioDevice->GetValue(), "DARWINOSX:default") &&
        StringUtils::FindWords(audioDevice->GetValue().c_str(), ":stream") == std::string::npos)
    {
      std::string newSetting = audioDevice->GetValue();
      newSetting += ":stream0";
      return audioDevice->SetValue(newSetting);
    }
  }
#endif

  return false;
}

bool CApplicationSettingsHandling::Load(const TiXmlNode* settings)
{
  auto& components = CServiceBroker::GetAppComponents();
  const auto appVolume = components.GetComponent<CApplicationVolumeHandling>();
  const bool loaded = appVolume->Load(settings);

  if (settings != nullptr)
  {
    const TiXmlElement* audioElement = settings->FirstChildElement("audio");
    if (audioElement != nullptr)
    {
      XMLUtils::GetBoolean(audioElement, AUDIO_RESTORE_PASSTHROUGH_TAG, m_restorePassthroughOnAml);
      m_hasAmlChannels = XMLUtils::GetInt(audioElement, AUDIO_AML_CHANNELS_TAG, m_amlChannels);
    }
  }

  return loaded;
}

bool CApplicationSettingsHandling::Save(TiXmlNode* settings) const
{
  const auto& components = CServiceBroker::GetAppComponents();
  const auto appVolume = components.GetComponent<CApplicationVolumeHandling>();
  if (!appVolume->Save(settings))
    return false;

  if (settings == nullptr)
    return false;

  TiXmlElement* audioElement = settings->FirstChildElement("audio");
  if (audioElement == nullptr)
    return false;

  XMLUtils::SetBoolean(audioElement, AUDIO_RESTORE_PASSTHROUGH_TAG, m_restorePassthroughOnAml);
  if (m_hasAmlChannels)
    XMLUtils::SetInt(audioElement, AUDIO_AML_CHANNELS_TAG, m_amlChannels);
  return true;
}
