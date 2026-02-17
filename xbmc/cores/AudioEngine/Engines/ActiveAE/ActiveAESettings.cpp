/*
 *  Copyright (C) 2010-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */


#include "ActiveAESettings.h"

#include "ServiceBroker.h"
#include "cores/AudioEngine/Engines/ActiveAE/ActiveAE.h"
#include "cores/AudioEngine/Interfaces/AE.h"
#include "guilib/LocalizeStrings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "settings/lib/SettingDefinitions.h"
#include "settings/lib/SettingsManager.h"
#include "utils/log.h"
#include "utils/StringUtils.h"

#include <mutex>

namespace ActiveAE
{

CActiveAESettings* CActiveAESettings::m_instance = nullptr;

CActiveAESettings::CActiveAESettings(CActiveAE &ae) : m_audioEngine(ae)
{
  const std::shared_ptr<CSettings> settings = CServiceBroker::GetSettingsComponent()->GetSettings();

  std::unique_lock<CCriticalSection> lock(m_cs);
  m_instance = this;

  std::set<std::string> settingSet;
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_CONFIG);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_SAMPLERATE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_CHANNELS);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_PROCESSQUALITY);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_ATEMPOTHRESHOLD);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_GUISOUNDMODE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_STEREOUPMIX);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_AC3PASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_AC3TRANSCODE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_EAC3PASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_DTSPASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_TRUEHDPASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_DTSHDPASSTHROUGH);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_AUDIODEVICE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_PASSTHROUGHDEVICE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_BTCODEC);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_BTVOLUMEBOOST);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_STREAMSILENCE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_STREAMNOISE);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_MIXSUBLEVEL);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_MAINTAINORIGINALVOLUME);
  settingSet.insert(CSettings::SETTING_AUDIOOUTPUT_DTSHDCOREFALLBACK);
  settings->GetSettingsManager()->RegisterCallback(this, settingSet);

  settings->GetSettingsManager()->RegisterSettingOptionsFiller("aequalitylevels", SettingOptionsAudioQualityLevelsFiller);
  settings->GetSettingsManager()->RegisterSettingOptionsFiller("audiodevices", SettingOptionsAudioDevicesFiller);
  settings->GetSettingsManager()->RegisterSettingOptionsFiller("audiodevicespassthrough", SettingOptionsAudioDevicesPassthroughFiller);
  settings->GetSettingsManager()->RegisterSettingOptionsFiller("audiostreamsilence", SettingOptionsAudioStreamsilenceFiller);
  settings->GetSettingsManager()->RegisterSettingOptionsFiller("bluetoothcodecs", SettingOptionsBluetoothCodecsFiller);
}

CActiveAESettings::~CActiveAESettings()
{
  const std::shared_ptr<CSettings> settings = CServiceBroker::GetSettingsComponent()->GetSettings();

  std::unique_lock<CCriticalSection> lock(m_cs);
  settings->GetSettingsManager()->UnregisterSettingOptionsFiller("aequalitylevels");
  settings->GetSettingsManager()->UnregisterSettingOptionsFiller("audiodevices");
  settings->GetSettingsManager()->UnregisterSettingOptionsFiller("audiodevicespassthrough");
  settings->GetSettingsManager()->UnregisterSettingOptionsFiller("audiostreamsilence");
  settings->GetSettingsManager()->UnregisterSettingOptionsFiller("bluetoothcodecs");
  settings->GetSettingsManager()->UnregisterCallback(this);
  m_instance = nullptr;
}

void CActiveAESettings::OnSettingChanged(const std::shared_ptr<const CSetting>& setting)
{
  std::unique_lock<CCriticalSection> lock(m_cs);

  // Handle Bluetooth codec changes
  if (setting->GetId() == CSettings::SETTING_AUDIOOUTPUT_BTCODEC)
  {
    std::string codec = std::static_pointer_cast<const CSettingString>(setting)->GetValue();

    // Map codec name to PulseAudio profile
    std::string profile;
    if (codec == "ldac")
      profile = "a2dp_sink_ldac";
    else if (codec == "aptx_hd")
      profile = "a2dp_sink_aptx_hd";
    else if (codec == "aptx")
      profile = "a2dp_sink_aptx";
    else if (codec == "aac")
      profile = "a2dp_sink_aac";
    else if (codec == "sbc")
      profile = "a2dp_sink_sbc";

    if (!profile.empty())
    {
      // Find Bluetooth card and switch profile
      FILE* pipe = popen("pactl list cards short 2>/dev/null | grep bluez", "r");
      if (pipe)
      {
        char buffer[256];
        if (fgets(buffer, sizeof(buffer), pipe) != nullptr)
        {
          std::string line(buffer);
          size_t tabPos = line.find('\t');
          if (tabPos != std::string::npos)
          {
            std::string cardName = line.substr(tabPos + 1);
            cardName = cardName.substr(0, cardName.find('\t'));

            // Switch codec
            std::string cmd = "pactl set-card-profile " + cardName + " " + profile + " 2>/dev/null";
            system(cmd.c_str());
          }
        }
        pclose(pipe);
      }
    }
  }

  m_instance->m_audioEngine.OnSettingsChange();
}

void CActiveAESettings::SettingOptionsAudioDevicesFiller(const SettingConstPtr& setting,
                                                         std::vector<StringSettingOption>& list,
                                                         std::string& current,
                                                         void* data)
{
  SettingOptionsAudioDevicesFillerGeneral(setting, list, current, false);
}

void CActiveAESettings::SettingOptionsAudioDevicesPassthroughFiller(
    const SettingConstPtr& setting,
    std::vector<StringSettingOption>& list,
    std::string& current,
    void* data)
{
  SettingOptionsAudioDevicesFillerGeneral(setting, list, current, true);
}

void CActiveAESettings::SettingOptionsAudioQualityLevelsFiller(
    const SettingConstPtr& setting,
    std::vector<IntegerSettingOption>& list,
    int& current,
    void* data)
{
  std::unique_lock<CCriticalSection> lock(m_instance->m_cs);

  if (m_instance->m_audioEngine.SupportsQualityLevel(AE_QUALITY_LOW))
    list.emplace_back(g_localizeStrings.Get(13506), AE_QUALITY_LOW);
  if (m_instance->m_audioEngine.SupportsQualityLevel(AE_QUALITY_MID))
    list.emplace_back(g_localizeStrings.Get(13507), AE_QUALITY_MID);
  if (m_instance->m_audioEngine.SupportsQualityLevel(AE_QUALITY_HIGH))
    list.emplace_back(g_localizeStrings.Get(13508), AE_QUALITY_HIGH);
  if (m_instance->m_audioEngine.SupportsQualityLevel(AE_QUALITY_REALLYHIGH))
    list.emplace_back(g_localizeStrings.Get(13509), AE_QUALITY_REALLYHIGH);
  if (m_instance->m_audioEngine.SupportsQualityLevel(AE_QUALITY_GPU))
    list.emplace_back(g_localizeStrings.Get(38010), AE_QUALITY_GPU);
}

void CActiveAESettings::SettingOptionsAudioStreamsilenceFiller(
    const SettingConstPtr& setting,
    std::vector<IntegerSettingOption>& list,
    int& current,
    void* data)
{
  std::unique_lock<CCriticalSection> lock(m_instance->m_cs);

  list.emplace_back(g_localizeStrings.Get(20422),
                    XbmcThreads::EndTime<std::chrono::minutes>::Max().count());
  list.emplace_back(g_localizeStrings.Get(13551), 0);

  if (m_instance->m_audioEngine.SupportsSilenceTimeout())
  {
    list.emplace_back(StringUtils::Format(g_localizeStrings.Get(13554), 1), 1);
    for (int i = 2; i <= 10; i++)
    {
      list.emplace_back(StringUtils::Format(g_localizeStrings.Get(13555), i), i);
    }
  }
}

bool CActiveAESettings::IsSettingVisible(const std::string& condition,
                                         const std::string& value,
                                         const SettingConstPtr& setting,
                                         void* data)
{
  if (setting == NULL || value.empty())
    return false;

  std::unique_lock<CCriticalSection> lock(m_instance->m_cs);
  if (!m_instance)
    return false;

  return m_instance->m_audioEngine.IsSettingVisible(value);
}

void CActiveAESettings::SettingOptionsAudioDevicesFillerGeneral(
    const SettingConstPtr& setting,
    std::vector<StringSettingOption>& list,
    std::string& current,
    bool passthrough)
{
  current = std::static_pointer_cast<const CSettingString>(setting)->GetValue();
  std::string firstDevice;

  std::unique_lock<CCriticalSection> lock(m_instance->m_cs);

  bool foundValue = false;
  AEDeviceList sinkList;
  m_instance->m_audioEngine.EnumerateOutputDevices(sinkList, passthrough);
  if (sinkList.empty())
    list.emplace_back("Error - no devices found", "error");
  else
  {
    for (AEDeviceList::const_iterator sink = sinkList.begin(); sink != sinkList.end(); ++sink)
    {
      if (sink == sinkList.begin())
        firstDevice = sink->second;

      list.emplace_back(sink->first, sink->second);

      if (StringUtils::EqualsNoCase(current, sink->second))
        foundValue = true;
    }
  }

  if (!foundValue)
    current = firstDevice;
}

void CActiveAESettings::SettingOptionsBluetoothCodecsFiller(
    const SettingConstPtr& setting,
    std::vector<StringSettingOption>& list,
    std::string& current,
    void* data)
{
  current = std::static_pointer_cast<const CSettingString>(setting)->GetValue();

  std::unique_lock<CCriticalSection> lock(m_instance->m_cs);

  CLog::Log(LOGINFO, "CActiveAESettings: Bluetooth codec filler called");

  // Query PulseAudio for available Bluetooth codecs
  FILE* pipe = popen("pactl list cards 2>/dev/null | grep -A 50 'bluez_card' | grep 'a2dp_sink' | grep 'available: yes'", "r");
  if (!pipe)
  {
    CLog::Log(LOGWARNING, "CActiveAESettings: Failed to open pipe for pactl");
    list.emplace_back("SBC (Default)", "sbc");
    current = "sbc";
    return;
  }

  // First pass: detect which codecs are available
  bool hasLDAC = false;
  bool hasAptXHD = false;
  bool hasAptX = false;
  bool hasAAC = false;
  bool hasSBC = false;

  char buffer[256];
  while (fgets(buffer, sizeof(buffer), pipe) != nullptr)
  {
    std::string line(buffer);
    CLog::Log(LOGINFO, "CActiveAESettings: Parsing codec line: {}", line);

    if (line.find("a2dp_sink_ldac") != std::string::npos)
      hasLDAC = true;
    else if (line.find("a2dp_sink_aptx_hd") != std::string::npos)
      hasAptXHD = true;
    else if (line.find("a2dp_sink_aptx") != std::string::npos && line.find("aptx_hd") == std::string::npos)
      hasAptX = true;
    else if (line.find("a2dp_sink_aac") != std::string::npos)
      hasAAC = true;
    else if (line.find("a2dp_sink_sbc") != std::string::npos)
      hasSBC = true;
  }
  pclose(pipe);

  // Second pass: add codecs to list in priority order (highest quality first)
  std::string firstCodec;
  bool foundAny = false;

  if (hasLDAC)
  {
    list.emplace_back("LDAC (High Quality)", "ldac");
    if (!foundAny) firstCodec = "ldac";
    foundAny = true;
    CLog::Log(LOGINFO, "CActiveAESettings: Added LDAC codec");
  }
  if (hasAptXHD)
  {
    list.emplace_back("aptX HD", "aptx_hd");
    if (!foundAny) firstCodec = "aptx_hd";
    foundAny = true;
    CLog::Log(LOGINFO, "CActiveAESettings: Added aptX HD codec");
  }
  if (hasAptX)
  {
    list.emplace_back("aptX", "aptx");
    if (!foundAny) firstCodec = "aptx";
    foundAny = true;
    CLog::Log(LOGINFO, "CActiveAESettings: Added aptX codec");
  }
  if (hasAAC)
  {
    list.emplace_back("AAC", "aac");
    if (!foundAny) firstCodec = "aac";
    foundAny = true;
    CLog::Log(LOGINFO, "CActiveAESettings: Added AAC codec");
  }
  if (hasSBC)
  {
    list.emplace_back("SBC (Baseline)", "sbc");
    if (!foundAny) firstCodec = "sbc";
    foundAny = true;
    CLog::Log(LOGINFO, "CActiveAESettings: Added SBC codec");
  }

  // Always add SBC as fallback if nothing found
  if (!foundAny)
  {
    CLog::Log(LOGWARNING, "CActiveAESettings: No codecs found, using SBC fallback");
    list.emplace_back("SBC (Default)", "sbc");
    current = "sbc";
  }
  else if (current.empty() || current == "Default")
  {
    current = firstCodec;
    CLog::Log(LOGINFO, "CActiveAESettings: Setting default codec to {}", current);
  }
  else if (current == "sbc" && firstCodec != "sbc")
  {
    // Auto-upgrade from SBC to best available codec
    current = firstCodec;
    CLog::Log(LOGINFO, "CActiveAESettings: Upgrading from SBC to {}", current);
  }

  CLog::Log(LOGINFO, "CActiveAESettings: Bluetooth codec filler completed, {} codecs available", list.size());
}
}
