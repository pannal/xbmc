/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ApplicationVolumeHandling.h"

#include "ServiceBroker.h"
#include "application/ApplicationComponents.h"
#include "application/ApplicationPlayer.h"
#include "cores/AudioEngine/Interfaces/AE.h"
#include "dialogs/GUIDialogVolumeBar.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "input/actions/ActionIDs.h"
#include "interfaces/AnnouncementManager.h"
#include "messaging/ApplicationMessenger.h"
#include "peripherals/Peripherals.h"
#include "settings/Settings.h"
#include "settings/lib/Setting.h"
#include "utils/Variant.h"
#include "utils/XMLUtils.h"

#include <tinyxml.h>
#include <cstdlib>
#include <string>

namespace
{
constexpr const char* AUDIO_DEVICE_VOLUME_NODE = "devicevolume";
constexpr const char* AUDIO_DEVICE_VOLUME_NAME = "name";
constexpr const char* AUDIO_DEVICE_VOLUME_VALUE = "value";
} // namespace

bool CApplicationVolumeHandling::IsAmlAudioDevice(const std::string& device)
{
  return device.find("AUGESOUND") != std::string::npos;
}

float CApplicationVolumeHandling::GetVolumePercent() const
{
  // converts the hardware volume to a percentage
  return m_volumeLevel * 100.0f;
}

float CApplicationVolumeHandling::GetVolumeRatio() const
{
  return m_volumeLevel;
}

void CApplicationVolumeHandling::SetHardwareVolume(float hardwareVolume)
{
  m_volumeLevel = std::clamp(hardwareVolume, VOLUME_MINIMUM, VOLUME_MAXIMUM);

  IAE* ae = CServiceBroker::GetActiveAE();
  if (ae)
    ae->SetVolume(m_volumeLevel);
}

void CApplicationVolumeHandling::StoreVolumeForCurrentDevice()
{
  if (!m_audioDevice.empty() && !IsAmlAudioDevice(m_audioDevice))
  {
    m_userVolumeLevel = m_volumeLevel;
    m_deviceVolumes[m_audioDevice] = m_userVolumeLevel;
  }
}

void CApplicationVolumeHandling::VolumeChanged()
{
  CVariant data(CVariant::VariantTypeObject);
  data["volume"] = static_cast<int>(std::lroundf(GetVolumePercent()));
  data["muted"] = m_muted;
  const auto announcementMgr = CServiceBroker::GetAnnouncementManager();
  announcementMgr->Announce(ANNOUNCEMENT::Application, "OnVolumeChanged", data);

  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  // if player has volume control, set it.
  if (appPlayer)
  {
    appPlayer->SetVolume(m_volumeLevel);
    appPlayer->SetMute(m_muted);
  }
}

void CApplicationVolumeHandling::ShowVolumeBar(const CAction* action)
{
  const auto& wm = CServiceBroker::GetGUI()->GetWindowManager();
  auto* volumeBar = wm.GetWindow<CGUIDialogVolumeBar>(WINDOW_DIALOG_VOLUME_BAR);
  if (volumeBar != nullptr && volumeBar->IsVolumeBarEnabled())
  {
    volumeBar->Open();
    if (action)
      volumeBar->OnAction(*action);
  }
}

bool CApplicationVolumeHandling::IsMuted() const
{
  if (CServiceBroker::GetPeripherals().IsMuted())
    return true;
  IAE* ae = CServiceBroker::GetActiveAE();
  if (ae)
    return ae->IsMuted();
  return true;
}

void CApplicationVolumeHandling::ToggleMute(void)
{
  if (m_muted)
    UnMute();
  else
    Mute();
}

void CApplicationVolumeHandling::SetMute(bool mute)
{
  if (m_muted != mute)
  {
    ToggleMute();
    m_muted = mute;
  }
}

void CApplicationVolumeHandling::Mute()
{
  if (CServiceBroker::GetPeripherals().Mute())
    return;

  IAE* ae = CServiceBroker::GetActiveAE();
  if (ae)
    ae->SetMute(true);
  m_muted = true;
  VolumeChanged();
}

void CApplicationVolumeHandling::UnMute()
{
  if (CServiceBroker::GetPeripherals().UnMute())
    return;

  IAE* ae = CServiceBroker::GetActiveAE();
  if (ae)
    ae->SetMute(false);
  m_muted = false;
  VolumeChanged();
}

void CApplicationVolumeHandling::SetVolume(float iValue, bool isPercentage)
{
  float hardwareVolume = iValue;

  if (isPercentage)
    hardwareVolume /= 100.0f;

  if (IsAmlAudioDevice(m_audioDevice) &&
      CServiceBroker::GetPeripherals().IsCECVolumeControlActive())
  {
    SetHardwareVolume(VOLUME_MAXIMUM);
    VolumeChanged();
    return;
  }

  SetHardwareVolume(hardwareVolume);
  m_userVolumeLevel = m_volumeLevel;
  StoreVolumeForCurrentDevice();
  VolumeChanged();
}

void CApplicationVolumeHandling::SetAudioDevice(const std::string& device)
{
  if (device == m_audioDevice)
    return;

  StoreVolumeForCurrentDevice();
  m_audioDevice = device;

  if (IsAmlAudioDevice(device) && CServiceBroker::GetPeripherals().IsCECVolumeControlActive())
  {
    IAE* ae = CServiceBroker::GetActiveAE();
    if (ae)
      ae->SetMute(false);
    m_muted = false;
    SetHardwareVolume(VOLUME_MAXIMUM);
    VolumeChanged();
    CServiceBroker::GetAppMessenger()->PostMsg(TMSG_VOLUME_SHOW, ACTION_VOLUME_UP);
    return;
  }

  const auto it = m_deviceVolumes.find(device);
  SetHardwareVolume(it != m_deviceVolumes.end() ? it->second : m_userVolumeLevel);
  m_userVolumeLevel = m_volumeLevel;
  VolumeChanged();
  CServiceBroker::GetAppMessenger()->PostMsg(TMSG_VOLUME_SHOW, ACTION_VOLUME_DOWN);
}

void CApplicationVolumeHandling::CacheReplayGainSettings(const CSettings& settings)
{
  // initialize m_replayGainSettings
  m_replayGainSettings.iType = settings.GetInt(CSettings::SETTING_MUSICPLAYER_REPLAYGAINTYPE);
  m_replayGainSettings.iPreAmp = settings.GetInt(CSettings::SETTING_MUSICPLAYER_REPLAYGAINPREAMP);
  m_replayGainSettings.iNoGainPreAmp =
      settings.GetInt(CSettings::SETTING_MUSICPLAYER_REPLAYGAINNOGAINPREAMP);
  m_replayGainSettings.bAvoidClipping =
      settings.GetBool(CSettings::SETTING_MUSICPLAYER_REPLAYGAINAVOIDCLIPPING);
}

bool CApplicationVolumeHandling::Load(const TiXmlNode* settings)
{
  if (!settings)
    return false;

  const TiXmlElement* audioElement = settings->FirstChildElement("audio");
  if (audioElement)
  {
    XMLUtils::GetBoolean(audioElement, "mute", m_muted);
    if (!XMLUtils::GetFloat(audioElement, "fvolumelevel", m_volumeLevel, VOLUME_MINIMUM,
                            VOLUME_MAXIMUM))
      m_volumeLevel = VOLUME_MAXIMUM;
    m_userVolumeLevel = m_volumeLevel;

    const TiXmlElement* deviceVolumesElement = audioElement->FirstChildElement("devicevolumes");
    if (deviceVolumesElement)
    {
      for (const TiXmlElement* deviceElement = deviceVolumesElement->FirstChildElement(AUDIO_DEVICE_VOLUME_NODE);
           deviceElement != nullptr;
           deviceElement = deviceElement->NextSiblingElement(AUDIO_DEVICE_VOLUME_NODE))
      {
        const char* name = deviceElement->Attribute(AUDIO_DEVICE_VOLUME_NAME);
        if (name == nullptr || IsAmlAudioDevice(name))
          continue;

        const char* value = deviceElement->Attribute(AUDIO_DEVICE_VOLUME_VALUE);
        if (value == nullptr)
          continue;

        char* end = nullptr;
        const float volume = strtof(value, &end);
        if (end != value && volume >= VOLUME_MINIMUM && volume <= VOLUME_MAXIMUM)
          m_deviceVolumes[name] = volume;
      }
    }
  }

  return true;
}

bool CApplicationVolumeHandling::Save(TiXmlNode* settings) const
{
  if (!settings)
    return false;

  TiXmlElement volumeNode("audio");
  TiXmlNode* audioNode = settings->InsertEndChild(volumeNode);
  if (!audioNode)
    return false;

  XMLUtils::SetBoolean(audioNode, "mute", m_muted);
  XMLUtils::SetFloat(audioNode, "fvolumelevel", m_userVolumeLevel);

  if (!m_deviceVolumes.empty())
  {
    TiXmlElement deviceVolumesNode("devicevolumes");
    TiXmlNode* deviceVolumesElement = audioNode->InsertEndChild(deviceVolumesNode);
    if (!deviceVolumesElement)
      return false;

    for (const auto& [device, volume] : m_deviceVolumes)
    {
      if (IsAmlAudioDevice(device))
        continue;

      TiXmlElement deviceNode(AUDIO_DEVICE_VOLUME_NODE);
      deviceNode.SetAttribute(AUDIO_DEVICE_VOLUME_NAME, device.c_str());
      const std::string volumeText = std::to_string(volume);
      deviceNode.SetAttribute(AUDIO_DEVICE_VOLUME_VALUE, volumeText.c_str());
      deviceVolumesElement->InsertEndChild(deviceNode);
    }
  }

  return true;
}

bool CApplicationVolumeHandling::OnSettingChanged(const CSetting& setting)
{
  const std::string& settingId = setting.GetId();

  if (StringUtils::EqualsNoCase(settingId, CSettings::SETTING_MUSICPLAYER_REPLAYGAINTYPE))
    m_replayGainSettings.iType = static_cast<const CSettingInt&>(setting).GetValue();
  else if (StringUtils::EqualsNoCase(settingId, CSettings::SETTING_MUSICPLAYER_REPLAYGAINPREAMP))
    m_replayGainSettings.iPreAmp = static_cast<const CSettingInt&>(setting).GetValue();
  else if (StringUtils::EqualsNoCase(settingId,
                                     CSettings::SETTING_MUSICPLAYER_REPLAYGAINNOGAINPREAMP))
    m_replayGainSettings.iNoGainPreAmp = static_cast<const CSettingInt&>(setting).GetValue();
  else if (StringUtils::EqualsNoCase(settingId,
                                     CSettings::SETTING_MUSICPLAYER_REPLAYGAINAVOIDCLIPPING))
    m_replayGainSettings.bAvoidClipping = static_cast<const CSettingBool&>(setting).GetValue();
  else
    return false;

  return true;
}
