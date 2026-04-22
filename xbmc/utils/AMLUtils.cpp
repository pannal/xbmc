/*
 *  Copyright (C) 2011-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <string>
#include <regex>
#include <chrono>
#include <vector>
#include <numeric>
#include <cmath>
#include <algorithm>
#include <mutex>

#include "AMLUtils.h"

#include "application/Application.h"
#include "application/ApplicationComponents.h"
#include "application/ApplicationPlayer.h"
#include "cores/DataCacheCore.h"
#include "utils/log.h"
#include "utils/JobManager.h"
#include "utils/StringUtils.h"
#include "windowing/GraphicContext.h"
#include "utils/RegExp.h"
#include "filesystem/SpecialProtocol.h"
#include "rendering/RenderSystem.h"
#include "settings/DisplaySettings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "ServiceBroker.h"

#include "settings/AdvancedSettings.h"
#include "HDR10PlusConvert.h"

#include "platform/linux/SysfsPath.h"
#include "threads/Thread.h"
#include "filesystem/File.h"

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
}

#include "linux/fb.h"
#include <sys/ioctl.h>
#include <amcodec/codec.h>

static bool vs10_conversion = false;
// Only true after aml_dv_set_vs10_mode() flips from VS10 HDR10 mapping to
// Bypass mid-playback; aml_kodi_reset_cd_cs() then restores IPT at close.
// Default must be false or the IPT restore fires spuriously on the first
// HDR10 close of the session (see aml_kodi_reset_cd_cs).
static bool vs10_conversion_reset_hdr10 = false;
static bool s_pm4kActive = false;
static CGUIWindow* s_pm4kHome = nullptr;

static std::shared_ptr<CSettings> settings()
{
  return CServiceBroker::GetSettingsComponent()->GetSettings();
}

// Cached DV mode — updated by aml_dv_on/aml_dv_off, avoids per-frame sysfs reads.
static unsigned int s_dvModeCached = DOLBY_VISION_OUTPUT_MODE_BYPASS;

// Tracks whether DV playback is active (between aml_dv_open/aml_dv_close).
// Used by CreateNewWindow to avoid restoring IPT during playback-start mode switches.
static bool s_dvPlaybackActive = false;

static void aml_dv_reset_osd_max()
{
  int max(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE_ON_LUMINANCE));
  aml_dv_set_osd_max(max);
}

static void aml_dv_toggle_frame(unsigned int mode)
{
  CSysfsPath dolby_vision_flags{"/sys/module/amdolby_vision/parameters/dolby_vision_flags"};
  if (dolby_vision_flags.Exists()) 
  {
    dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() | FLAG_TOGGLE_FRAME);
    CLog::Log(LOGINFO, "AMLUtils::{} - Toggle Frame - start - for mode [{}]", __FUNCTION__, aml_dv_output_mode_to_string(mode));
    std::chrono::time_point<std::chrono::system_clock> now(std::chrono::system_clock::now());
    while(true) { 
      if ((dolby_vision_flags.Get<unsigned int>().value() & FLAG_TOGGLE_FRAME) == 0) {
        CLog::Log(LOGINFO, "AMLUtils::{} - Toggle Frame - done - for mode [{}]", __FUNCTION__, aml_dv_output_mode_to_string(mode));
        break;
      }
      if ((std::chrono::system_clock::now() - now) >= std::chrono::milliseconds(3000)) {
        CLog::Log(LOGINFO, "AMLUtils::{} - Toggle Frame - wait time elapsed - for mode [{}]", __FUNCTION__, aml_dv_output_mode_to_string(mode));
        break;
      } 
      usleep(10000); // wait 10ms
    }
  }
}

static void aml_dv_wait_dv_std_vsif_packet()
{
  // Wait for DV Std vsif packet being sent on HDMI.
  CSysfsPath hdmi_pkt{"/sys/kernel/debug/amhdmitx/hdmi_pkt"};
  if (hdmi_pkt.Exists())
  {
    CLog::Log(LOGINFO, "AMLUtils::{} - DV VSIF Packet - start", __FUNCTION__);
    std::chrono::time_point<std::chrono::system_clock> now(std::chrono::system_clock::now());
    while(true) { 
      std::string valstr = hdmi_pkt.Get<std::string>().value();
      if (valstr.find("DV STD hdmitx_parsing_vsifpkt") != std::string::npos) {
        CLog::Log(LOGINFO, "AMLUtils::{} - DV VSIF Packet - done", __FUNCTION__);
        break;
      }
      if ((std::chrono::system_clock::now() - now) >= std::chrono::milliseconds(3000)) {
        CLog::Log(LOGINFO, "AMLUtils::{} - DV VSIF Packet - wait time elapsed", __FUNCTION__);
        break;
      } 
      usleep(10000); // wait 10ms
    }
  }
}

void aml_reset_audio_from_vs10_change()
{
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(1);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
}

void aml_dv_set_vs10_mode(unsigned int mode, StreamHdrType hdrType)
{
  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  if (dv_type == DV_TYPE_VS10_ONLY) return;

  CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  unsigned int existing_mode = dolby_vision_mode.Get<unsigned int>().value();
  if ((existing_mode != mode) && (mode == DOLBY_VISION_OUTPUT_MODE_BYPASS) && (hdrType == StreamHdrType::HDR_TYPE_HDR10))
    vs10_conversion_reset_hdr10 = true;
  else
    vs10_conversion_reset_hdr10 = false;

  if (mode != DOLBY_VISION_OUTPUT_MODE_BYPASS)
  {
    if ((existing_mode == mode) || ((mode == DOLBY_VISION_OUTPUT_MODE_IPT) && (hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)))
      vs10_conversion = false;
    else
      vs10_conversion = true;

    if ((existing_mode != DOLBY_VISION_OUTPUT_MODE_HDR10) &&
        (mode == DOLBY_VISION_OUTPUT_MODE_SDR10) && (hdrType == StreamHdrType::HDR_TYPE_HDR10))
      aml_dv_on(DOLBY_VISION_OUTPUT_MODE_HDR10);

    aml_dv_on(mode);
  }
  else if (aml_is_dv_enable()) // DV BYPASS, and it is on - then switch it off.
    aml_dv_off();

  aml_reset_audio_from_vs10_change();
}

void aml_dv_wait_video_off(int timeout)
{
  // Wait for dv_video_on to unset.
  CSysfsPath dv_video_on{"/sys/class/amdolby_vision/dv_video_on"};
  if (dv_video_on.Exists())
  {      
    CLog::Log(LOGINFO, "AMLUtils::{} - DV Video Off - start", __FUNCTION__);
    std::chrono::time_point<std::chrono::system_clock> now(std::chrono::system_clock::now());
    while(true) { 
      if (dv_video_on.Get<int>().value() == 0) {
        CLog::Log(LOGINFO, "AMLUtils::{} - DV Video Off - done", __FUNCTION__);
        break;
      }
      if ((std::chrono::system_clock::now() - now) >= std::chrono::seconds(timeout)) {
        CLog::Log(LOGINFO, "AMLUtils::{} - DV Video Off - wait time elapsed", __FUNCTION__);
        break;
      } 
      usleep(10000); // wait 10ms
    }
  }
}

int aml_blackout_policy(int new_blackout)
{
  CSysfsPath blackout_policy{"/sys/class/video/blackout_policy"};
  if (blackout_policy.Exists())
  {
    int existing_blackout = blackout_policy.Get<int>().value();
    blackout_policy.Set(new_blackout);
    return existing_blackout;
  }
  return 0;
}

static unsigned int aml_vs10_by_hdrtype(StreamHdrType hdrType, unsigned int bitDepth)
{
  unsigned int vs10_mode = DOLBY_VISION_OUTPUT_MODE_BYPASS;
  switch (hdrType) {
    case StreamHdrType::HDR_TYPE_NONE:
      if (bitDepth == 10)
        vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR10);
      else
        vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR8);
      break;
    case StreamHdrType::HDR_TYPE_HDR10:
      vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10);
      break;
    case StreamHdrType::HDR_TYPE_HDR10PLUS:
      vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10PLUS);
      break;
    case StreamHdrType::HDR_TYPE_HLG:
      vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDRHLG);
      break;
    case StreamHdrType::HDR_TYPE_DOLBYVISION:
      vs10_mode = aml_vs10_by_setting(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV);
      break;
  }

  if ((vs10_mode != DOLBY_VISION_OUTPUT_MODE_BYPASS) &&
       ((hdrType != StreamHdrType::HDR_TYPE_DOLBYVISION) ||
        ((hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION) && (vs10_mode == DOLBY_VISION_OUTPUT_MODE_SDR10)) ||
        ((hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION) && (vs10_mode == DOLBY_VISION_OUTPUT_MODE_HDR10))))
    vs10_conversion = true;
  else
    vs10_conversion = false;

  return vs10_mode;
}

static void aml_dv_trigger_update_resolution(StreamHdrType hdrType)
{
  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  appPlayer->TriggerUpdateResolutionHdr(hdrType);
}

int aml_get_cpufamily_id()
{
  static int aml_cpufamily_id = -1;
  if (aml_cpufamily_id == -1)
  {
    std::ifstream cpuinfo("/proc/cpuinfo");
    std::regex re(".*: (.*)$");

    for (std::string line; std::getline(cpuinfo, line);)
    {
      if (line.find("Serial") != std::string::npos)
      {
        std::smatch match;

        if (std::regex_match(line, match, re) && match.size() == 2)
        {
          std::ssub_match value = match[1];
          std::string cpu_family = value.str().substr(0, 2);
          aml_cpufamily_id = std::stoi(cpu_family, nullptr, 16);
          break;
        }
      }
    }
  }
  return aml_cpufamily_id;
}

bool aml_display_support_hdr_pq()
{
  bool support = false;
  CSysfsPath hdr_cap{"/sys/class/amhdmitx/amhdmitx0/hdr_cap"};
  if (hdr_cap.Exists())
  {
    std::string valstr = hdr_cap.Get<std::string>().value();
    support = (valstr.find("SMPTE ST 2084: 1") != std::string::npos);
  }
  return support;
}

bool aml_display_support_hdr_hlg()
{
  bool support = false;
  CSysfsPath hdr_cap{"/sys/class/amhdmitx/amhdmitx0/hdr_cap"};
  if (hdr_cap.Exists())
  {
    std::string valstr = hdr_cap.Get<std::string>().value();
    support = (valstr.find("Hybrid Log-Gamma: 1") != std::string::npos);
  }
  return support;
}

bool aml_display_support_hdr10plus()
{
  bool support = false;
  CSysfsPath hdr_cap{"/sys/class/amhdmitx/amhdmitx0/hdr_cap"};
  if (hdr_cap.Exists())
  {
    std::string valstr = hdr_cap.Get<std::string>().value();
    support = (valstr.find("HDR10Plus Supported: 1") != std::string::npos);
  }
  return support;
}

bool aml_display_support_dv_ll()
{
  int support_ll = 0;
  CRegExp regexp;
  regexp.RegComp("LL_YCbCr_422_12BIT");
  std::string valstr;
  CSysfsPath dv_cap{"/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap"};
  if (dv_cap.Exists())
  {
    valstr = dv_cap.Get<std::string>().value();
    support_ll = (regexp.RegFind(valstr) >= 0) ? 1 : 0;
  }

  return support_ll;
}

bool aml_display_support_dv_std()
{
  int support_std = 0;
  CRegExp regexp;
  regexp.RegComp("DV_RGB_444_8BIT");
  std::string valstr;
  CSysfsPath dv_cap{"/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap"};
  if (dv_cap.Exists())
  {
    valstr = dv_cap.Get<std::string>().value();
    support_std = (regexp.RegFind(valstr) >= 0) ? 1 : 0;
  }
  return support_std;
}

bool aml_display_support_dv()
{
  int support_dv = 0;
  CRegExp regexp;
  regexp.RegComp("The Rx don't support DolbyVision");
  std::string valstr;
  CSysfsPath dv_cap{"/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap"};
  if (dv_cap.Exists())
  {
    valstr = dv_cap.Get<std::string>().value();
    support_dv = (regexp.RegFind(valstr) >= 0) ? 0 : 1;
  }
  return support_dv;
}

bool aml_display_support_12bit()
{
  // Require 12-bit on at least one non-4:2:0 chroma.  420-only 12-bit is
  // too restricted: with no explicit CS in attr the kernel picks its own
  // chroma (typically 422/444 for HDR10), and landing on a CS the TV
  // doesn't support at 12-bit produces a broken signal or 8-bit fallback.
  CSysfsPath dc_cap{"/sys/class/amhdmitx/amhdmitx0/dc_cap"};
  if (!dc_cap.Exists())
    return false;
  std::string valstr = dc_cap.Get<std::string>().value();
  return (valstr.find("444,12bit") != std::string::npos) ||
         (valstr.find("422,12bit") != std::string::npos) ||
         (valstr.find("rgb,12bit")  != std::string::npos);
}

bool aml_display_support_3d()
{
  static int support_3d = -1;

  if (support_3d == -1)
  {
    CSysfsPath amhdmitx0_support_3d{"/sys/class/amhdmitx/amhdmitx0/support_3d"};
    if (amhdmitx0_support_3d.Exists())
      support_3d = amhdmitx0_support_3d.Get<int>().value();
    else
      support_3d = 0;

    CLog::Log(LOGDEBUG, "AMLUtils: display support 3D: {}", bool(!!support_3d));
  }

  return (support_3d == 1);
}

static bool aml_support_vcodec_profile(const char *regex)
{
  int profile = 0;
  CRegExp regexp;
  regexp.RegComp(regex);
  std::string valstr;
  CSysfsPath vcodec_profile{"/sys/class/amstream/vcodec_profile"};
  if (vcodec_profile.Exists())
  {
    valstr = vcodec_profile.Get<std::string>().value();
    profile = (regexp.RegFind(valstr) >= 0) ? 1 : 0;
  }

  return profile;
}

bool aml_support_hevc()
{
  static int has_hevc = -1;

  if (has_hevc == -1)
      has_hevc = aml_support_vcodec_profile("\\bhevc\\b:");

  return (has_hevc == 1);
}

bool aml_support_hevc_4k2k()
{
  static int has_hevc_4k2k = -1;

  if (has_hevc_4k2k == -1)
    has_hevc_4k2k = aml_support_vcodec_profile("\\bhevc\\b:(?!\\;).*(4k|8k)");

  return (has_hevc_4k2k == 1);
}

bool aml_support_hevc_8k4k()
{
  static int has_hevc_8k4k = -1;

  if (has_hevc_8k4k == -1)
    has_hevc_8k4k = aml_support_vcodec_profile("\\bhevc\\b:(?!\\;).*8k");

  return (has_hevc_8k4k == 1);
}

bool aml_support_hevc_10bit()
{
  static int has_hevc_10bit = -1;

  if (has_hevc_10bit == -1)
    has_hevc_10bit = aml_support_vcodec_profile("\\bhevc\\b:(?!\\;).*10bit");

  return (has_hevc_10bit == 1);
}

AML_SUPPORT_H264_4K2K aml_support_h264_4k2k()
{
  static AML_SUPPORT_H264_4K2K has_h264_4k2k = AML_SUPPORT_H264_4K2K_UNINIT;

  if (has_h264_4k2k == AML_SUPPORT_H264_4K2K_UNINIT)
  {
    has_h264_4k2k = AML_NO_H264_4K2K;

    if (aml_support_vcodec_profile("\\bh264\\b:4k"))
      has_h264_4k2k = AML_HAS_H264_4K2K_SAME_PROFILE;
    else if (aml_support_vcodec_profile("\\bh264_4k2k\\b:"))
      has_h264_4k2k = AML_HAS_H264_4K2K;
  }
  return has_h264_4k2k;
}

bool aml_support_vp9()
{
  static int has_vp9 = -1;

  if (has_vp9 == -1)
    has_vp9 = aml_support_vcodec_profile("\\bvp9\\b:(?!\\;).*compressed");

  return (has_vp9 == 1);
}

bool aml_support_av1()
{
  static int has_av1 = -1;

  if (has_av1 == -1)
    has_av1 = aml_support_vcodec_profile("\\bav1\\b:(?!\\;).*compressed");

  return (has_av1 == 1);
}

bool aml_support_dolby_vision()
{
  static int support_dv = -1;

  if (support_dv == -1)
  {
    CSysfsPath support_info{"/sys/class/amdolby_vision/support_info"};
    support_dv = 0;
    if (support_info.Exists())
    {
      support_dv = (int)((support_info.Get<int>().value() & 7) == 7);
      if (support_dv == 1) {
        CSysfsPath ko_info{"/sys/class/amdolby_vision/ko_info"};
        if (ko_info.Exists())
          CLog::Log(LOGDEBUG, "Amlogic Dolby Vision info: {}", ko_info.Get<std::string>().value().c_str());
      }
    }
  }

  return (support_dv == 1);
}

bool aml_dolby_vision_enabled()
{
  static int dv_enabled = -1;
  bool dv_user_enabled(aml_dv_mode() != DV_MODE_OFF);

  if (dv_enabled == -1)
    dv_enabled = (!!aml_support_dolby_vision());

  return ((dv_enabled && !!dv_user_enabled) == 1);
}

std::string aml_dv_output_mode_to_string(unsigned int mode)
{
  std::string mode_string = "Unknown";
  switch (mode) {
    case DOLBY_VISION_OUTPUT_MODE_IPT:
      mode_string = "0-IPT";
      break;
    case DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL:
      mode_string = "1-IPT Tunnel";
      break;
    case DOLBY_VISION_OUTPUT_MODE_HDR10:
      mode_string = "2-HDR10";
      break;
    case DOLBY_VISION_OUTPUT_MODE_SDR10:
      mode_string = "3-SDR10";
      break;
    case DOLBY_VISION_OUTPUT_MODE_BYPASS:
      mode_string = "5-Bypass";
      break;
  }
  return mode_string;
}

std::string aml_dv_mode_to_string(enum DV_MODE mode)
{
  std::string mode_string = "Unknown";
  switch (mode) {
    case DV_MODE::DV_MODE_ON:
      mode_string = "0-On";
      break;
    case DV_MODE::DV_MODE_ON_DEMAND:
      mode_string = "1-On Demand";
      break;
    case DV_MODE::DV_MODE_OFF:
      mode_string = "2-Off";
      break;
  }
  return mode_string;
}

std::string aml_dv_type_to_string(enum DV_TYPE type)
{
  std::string type_string = "Unknown";
  switch (type) {
    case DV_TYPE::DV_TYPE_DISPLAY_LED:
      type_string = "0-Display Led (DV-Std)";
      break;
    case DV_TYPE::DV_TYPE_PLAYER_LED_LLDV:
      type_string = "1-Player Led (DV-LL)";
      break;
    case DV_TYPE::DV_TYPE_PLAYER_LED_HDR:
      type_string = "2-Player Led (HDR)";
      break;
    case DV_TYPE::DV_TYPE_VS10_ONLY:
      type_string = "3-VS10 Only";
      break;
    case DV_TYPE::DV_TYPE_PLAYER_LED_HDR2:
      type_string = "4-Player Led (HDR2)";
      break;
  }
  return type_string;
}

void set_vsvdb_payload_ver(enum DV_TYPE dv_type, int max_lum_nits_value, int source_max_pq)
{
  int cs(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS));

  if (cs == 10 || 
      (dv_type == DV_TYPE_DISPLAY_LED) || 
      (max_lum_nits_value < 400) || 
      ((max_lum_nits_value > 6450) && (source_max_pq == 4095)))
  {
    CalculateVSVDBPayload_2();
  }
  else
  {
    CalculateVSVDBPayload();
  }
}

// Static flag for kernel-side 422 forcing during DV/HDR10+ playback
static bool aml_linux_force_422 = false;

unsigned int aml_dv_on(unsigned int mode)
{
  bool dv_level5_enabled(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5));
  bool dv_source_level_5(dv_level5_enabled && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5", dv_source_level_5);

  bool dv_source_level_5_osdst(dv_source_level_5 && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5_OSDST));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_osdst", dv_source_level_5_osdst);

  int dv_l5_subs_signal_mode = dv_source_level_5 ? settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS) : 0;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_subt", dv_l5_subs_signal_mode > 0);

  bool dv_detect_active_area(dv_level5_enabled && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detect_active_area", dv_detect_active_area);
  CLog::Log(LOGDEBUG, "AMLUtils::aml_dv_on - L5 detect: l5_enabled={} detect_setting={} → detect={}",
            dv_level5_enabled, dv_detect_active_area, dv_detect_active_area);

  unsigned int xbmc_dv_vsvdb_source_lum_limit_num = 0;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_source_lum_limit_num", xbmc_dv_vsvdb_source_lum_limit_num);

  xbmc_dv_cap::dv_ver_i = 0;
  aml_get_dv_cap();
  enum DV_COLORIMETRY colorimetry = DV_COLORIMETRY_AMLOGIC;
  if (xbmc_dv_cap::dv_ver_i == 2) colorimetry = DV_COLORIMETRY_REMOVE;
  CSysfsPath("/sys/module/hdmitx20/parameters/dovi_tv_led_bt2020", (colorimetry == DV_COLORIMETRY_BT2020NC) ? 'Y' : 'N');
  CSysfsPath("/sys/module/hdmitx20/parameters/dovi_tv_led_no_colorimetry", (colorimetry == DV_COLORIMETRY_REMOVE) ? 'Y' : 'N');

  DOVIStreamMetadata dovi_stream_metadata;
  dovi_stream_metadata = CServiceBroker::GetDataCacheCore().GetVideoDoViStreamMetadata();
  int source_max_pq = static_cast<int>(dovi_stream_metadata.source_max_pq);
  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  int max_lum_nits_value(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM));

  bool dv_type_vp_auto(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO));
  unsigned int dv_vp(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR));

  if (vs10_conversion || (dv_vp != 0) || (dv_type == DV_TYPE_DISPLAY_LED) || (max_lum_nits_value < max_pq_to_nits(source_max_pq)))
    dv_type_vp_auto = false;

  if (dv_type_vp_auto)
  {
    switch (dv_type)
    {
      case DV_TYPE_PLAYER_LED_HDR:
        dv_vp = 1;
        break;
      case DV_TYPE_PLAYER_LED_HDR2:
        dv_vp = 2;
        break;
      case DV_TYPE_PLAYER_LED_LLDV:
        dv_vp = 3;
        break;
      default:
        break;
    }
  }

  // FPS-based VP adjustment: high FPS content uses different VP modes
  if ((CServiceBroker::GetDataCacheCore().GetVideoFps() > 41.0f) && ((dv_vp == 4) || (dv_vp == 5)))
  {
    if (dv_vp == 4) dv_vp = 6;
    else if (dv_vp == 5) dv_vp = 7;
  }

  // During VS10 conversion, map VP mode to corresponding DV type and clear VP
  if (vs10_conversion && (dv_vp != 0))
  {
    switch (dv_vp)
    {
      case 1:
        dv_type = DV_TYPE_PLAYER_LED_HDR;
        break;
      case 2:
        dv_type = DV_TYPE_PLAYER_LED_HDR2;
        break;
      case 3:
      case 4:
      case 6:
        dv_type = DV_TYPE_PLAYER_LED_LLDV;
        break;
      default:
        break;
    }
    dv_vp = 0;
    vs10_conversion = false;
  }

  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp", dv_vp);

  // VP tone mapping level controls core bypass stages:
  //   > 1: CVM bypass in core1/core2
  //   > 2: CSC bypass in core1
  //   > 3: core3 forced to IPT 12-bit bypass (0x00)
  // P5: cap at 3 (IPT base layer, diag block handles HDR10 conversion)
  // P8: keep at 4 (YUV HDR10 base layer, needs core3 IPT bypass)
  // VP=5/7 (LLDV 444, FPS-adjusted only): cap at 2.
  unsigned int dv_vp_tm(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR_TM));
  dv_vp_tm = 4;
  CSysfsPath dvprofile{"/sys/module/amdolby_vision/parameters/xbmc_dv_profile"};
  if (dvprofile.Exists())
  {
    unsigned int dv_profile = dvprofile.Get<unsigned int>().value();
    if ((dv_vp != 0) && (dv_vp_tm > 3) && (dv_profile == 5)) dv_vp_tm = 3;
    if ((dv_vp != 0) && (dv_vp_tm > 2) && ((dv_vp == 5) || (dv_vp == 7))) dv_vp_tm = 2;
  }
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp_tm", dv_vp_tm);

  // Override DV type based on VP mode
  if (dv_vp > 2) dv_type = DV_TYPE_PLAYER_LED_LLDV;
  else if (dv_vp == 1) dv_type = DV_TYPE_PLAYER_LED_HDR;
  else if (dv_vp == 2) dv_type = DV_TYPE_PLAYER_LED_HDR2;

  // Tell kernel the DV type
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_type", static_cast<unsigned int>(dv_type));

  // Force CD/CS for all DV modes
  aml_kodi_set_cd_cs(1);

  // Enable VPP 12-bit precision preservation when the "12-bit Deep Color
  // pipeline" toggle is on AND the sink advertises 12-bit on a non-4:2:0
  // chroma.  The VPU_HDMI_FMT_CTRL bit 4 that xbmc_dv_dither also sets is a
  // no-op here: the HDMI output is already 12-bit so there is no 12→10 stage
  // to dither, and no noise reaches the wire (which is what avoids the
  // TV-side edge-enhancer oversharpening seen when dither was applied at
  // 10-bit output).
  bool dv_dither = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_PREFER_12BIT)
                && aml_display_support_12bit();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_dither", dv_dither);

  // For VS10 non-IPT output (HDR10, SDR), the DV module is active but the
  // kernel HDMI TX may still have a stale DV EOTF from a previous IPT mode.
  // Tell the kernel to skip DV tunnel overrides so normal colour params apply.
  bool dv_non_ipt = (mode >= DOLBY_VISION_OUTPUT_MODE_HDR10 && mode <= DOLBY_VISION_OUTPUT_MODE_SDR8);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_non_ipt", dv_non_ipt);

  // Enable HDR10 metadata injection for DV LL output (VP/HDR modes)
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll",
             (dv_type == DV_TYPE_PLAYER_LED_HDR || dv_type == DV_TYPE_PLAYER_LED_HDR2) ? 'Y' : 'N');
  unsigned int xbmc_dv_hdr10_for_dv_ll_inject_num = 0;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll_inject_num", xbmc_dv_hdr10_for_dv_ll_inject_num);

  bool dv_dolby_vsvdb_inject(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject", dv_dolby_vsvdb_inject);
  unsigned int xbmc_dv_vsvdb_inject_num = 0;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject_num", xbmc_dv_vsvdb_inject_num);

  set_vsvdb_payload_ver(dv_type, max_lum_nits_value, source_max_pq);

  std::string dv_dolby_vsvdb_payload(settings()->GetString(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_PAYLOAD));
  if ((dv_vp != 0) && (dv_vp_tm > 1))
    dv_dolby_vsvdb_payload = "27FE012E5699AA";
  else if ((dv_vp != 0) && (dv_vp_tm == 1))
    dv_dolby_vsvdb_payload = "27FE012D5699AA";
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_payload", dv_dolby_vsvdb_payload);

  // setup display led or player led
  CSysfsPath dolby_vision_flags{"/sys/module/amdolby_vision/parameters/dolby_vision_flags"};
  CSysfsPath dolby_vision_ll_policy{"/sys/module/amdolby_vision/parameters/dolby_vision_ll_policy"};

  if (dolby_vision_flags.Exists() && dolby_vision_ll_policy.Exists())
  {
    if (dv_type == DV_TYPE_DISPLAY_LED) // Display Led (DV-Std)
    {
      dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_RGB_OUTPUT));
      dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_DOVI_LL));
      dolby_vision_ll_policy.Set(DOLBY_VISION_LL_DISABLE);
    }
    else // Player Led (DV-LL and HDR) or VS10 Only.
    {
      if ((dv_vp == 5) || (dv_vp == 7))
      {
        dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() | FLAG_FORCE_DOVI_LL);
        dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() | FLAG_FORCE_RGB_OUTPUT);
        dolby_vision_ll_policy.Set(DOLBY_VISION_LL_RGB444);
      }
      else
      {
        dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_RGB_OUTPUT));
        dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() | FLAG_FORCE_DOVI_LL);
        dolby_vision_ll_policy.Set(DOLBY_VISION_LL_YUV422);
      }
    }

    // For SDR output, clear FLAG_FORCE_CVM so the kernel's skip_cvm_tbl can
    // bypass Color Volume Management for SDR→SDR (avoids DV compositor
    // over-processing that users perceive as sharpening/contrast artifacts).
    if (mode == DOLBY_VISION_OUTPUT_MODE_SDR10 || mode == DOLBY_VISION_OUTPUT_MODE_SDR8)
      dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_CVM));
    else
      dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() | FLAG_FORCE_CVM);
  }

  // switch mode to IPT Tunnel if IPT and type is DV_TYPE_DISPLAY_LED.
  if ((mode == DOLBY_VISION_OUTPUT_MODE_IPT) && (dv_type == DV_TYPE_DISPLAY_LED))
    mode = DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL;

  // change mode and enable.
  CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  unsigned int existing_mode = dolby_vision_mode.Get<unsigned int>().value();
  bool modeChange(existing_mode != mode);
  CLog::Log(LOGDEBUG, "AMLUtils::{} - mode change [{}], existing mode [{}], this mode [{}]", __FUNCTION__, modeChange, aml_dv_output_mode_to_string(existing_mode), aml_dv_output_mode_to_string(mode));
  if (modeChange) CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_mode", mode);
  s_dvModeCached = mode;
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_policy", DOLBY_VISION_FORCE_OUTPUT_MODE);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_enable", "Y");

  // Set OSD brightness for the current mode.
  // IMPORTANT: dolby_vision_graphic_max must be written LAST because it's the only
  // brightness param with kernel change detection (is_graphic_changed → force_set_lut).
  // Writing it last ensures dv_graphic_blend_test and dv_HDR10_graphics_max are already
  // correct when the kernel recalculates the LUT. Otherwise a vsync between the
  // graphic_max reset and the blend_test write causes a premature LUT update with
  // wrong values, and force_set_lut is consumed before the correct params are in place.
  //
  // VP modes always use the DV OSD Brightness slider (dolby_vision_graphic_max) — the
  // kernel scales the g_2_l degamma table by this value. The HDR10-specific slider is
  // only conditionally visible and not applicable to VP.
  if (dv_vp != 0)
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test", 0);
    aml_dv_set_osd_brightness(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_OSD_BRIGHTNESS));
  }
  else if (mode == DOLBY_VISION_OUTPUT_MODE_HDR10)
  {
    aml_dv_set_hdr10_osd_brightness(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10_OSD_BRIGHTNESS));
  }
  else if (mode != DOLBY_VISION_OUTPUT_MODE_SDR10 && mode != DOLBY_VISION_OUTPUT_MODE_SDR8)
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test", 0);
    aml_dv_set_osd_brightness(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_OSD_BRIGHTNESS));
  }
  else
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test", 0);
    CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max", 0);
  }

  if (modeChange) {
    aml_dv_toggle_frame(mode);

    // Re-trigger update resolution when mode IPT Tunnel and in Display Led (DV-Std).
    // Work around CD 12 bit issue for DV-Std shoule be CD 8 bit.
    // Wait for Dolby VSIF being output before trigging the update resolution so logic has correct input to work from.
    // The update resolution will cause the hdmi mode switch logic in the kernel to set the colour bit depth correctly in DV-Std.
    if ((mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL) && (dv_type == DV_TYPE_DISPLAY_LED))
      aml_dv_wait_dv_std_vsif_packet();

    if ((mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL) || (mode == DOLBY_VISION_OUTPUT_MODE_IPT)) {
      aml_dv_trigger_update_resolution(StreamHdrType::HDR_TYPE_DOLBYVISION); // Required for 60Hz VS10 > DV.
      aml_dv_display_auto_now();
    }
    else if (dv_non_ipt) {
      // Any transition into a VS10 non-IPT output mode needs the HDMI TX
      // re-evaluated.  Coming from IPT the attr is stale from the DV tunnel;
      // coming from Bypass (DV_MODE_ON_DEMAND between playbacks) the attr
      // still reflects the prior non-DV output so the kernel DV pipeline
      // activates while HDMI keeps sending the old signal -> colour
      // corruption (purple/green playback).  Write user's colour settings
      // and trigger mode re-evaluation in one atomic attr write (separate
      // writes don't work: "now" overwrites fmt_attr).
      int force_cs = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS);
      int limit_cd = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD);
      const std::string force_cs_str[] = { "rgb", "420", "422", "444" };
      const std::string limit_cd_str[] = { "8bit", "10bit", "12bit", "16bit" };
      std::string fmt_attr;
      if (force_cs > 0)
        fmt_attr += force_cs_str[force_cs - 1];
      if (limit_cd > 0) {
        if (!fmt_attr.empty()) fmt_attr += ",";
        fmt_attr += limit_cd_str[limit_cd - 1];
      } else if (settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_PREFER_12BIT)
                 && aml_display_support_12bit()) {
        // User left CD on Auto and requested the 12-bit Deep Color pipeline.
        // Writing "12bit" opts out of the kernel's Auto-depth loop and its
        // 4K60 4:4:4 bandwidth clamp to 8-bit; EDID-gated so we fall back to
        // plain Auto when the sink can't accept 12-bit on a non-420 chroma.
        if (!fmt_attr.empty()) fmt_attr += ",";
        fmt_attr += "12bit";
      }
      if (!fmt_attr.empty()) fmt_attr += ",";
      fmt_attr += "now";
      CSysfsPath("/sys/class/amhdmitx/amhdmitx0/attr", fmt_attr);
    }
  }

  return mode;
}

void aml_get_dv_cap()
{
  xbmc_dv_cap::edid_pnpid = "";
  CSysfsPath edid_dump{"/sys/class/amhdmitx/amhdmitx0/edid"};
  if (edid_dump.Exists())
  {
    std::string parsed = edid_dump.Get<std::string>().value();
    size_t mpos = parsed.find("Rx Manufacturer Name:");
    if (mpos != std::string::npos)
    {
      size_t lstart = parsed.find_first_not_of(" \t", mpos + 21);
      size_t lend = parsed.find('\n', lstart);
      if (lstart != std::string::npos && lend != std::string::npos && lend > lstart)
        xbmc_dv_cap::edid_pnpid = parsed.substr(lstart, lend - lstart);
    }
  }

  if (aml_display_support_dv())
  {
    CSysfsPath dv_cap{"/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap"};
    if (dv_cap.Exists())
    {
      try
      {
        std::string valstr = dv_cap.Get<std::string>().value();

        int pos = valstr.find(": V");
        xbmc_dv_cap::dv_ver_i = std::stoi(valstr.substr(pos+3, 1));

        pos = valstr.find("h: ");
        xbmc_dv_cap::dv_len_i = std::stoi(valstr.substr(pos+3, 2)) + 1;

        pos = valstr.find("B: ");
        xbmc_dv_cap::dv_vsvdb_s = valstr.substr(pos+3, xbmc_dv_cap::dv_len_i);

        pos = valstr.find("M: ");
        int pos2 = valstr.find("nti");
        xbmc_dv_cap::dv_max_v1_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Q: ");
        pos2 = valstr.find("pqi");
        xbmc_dv_cap::dv_max_v2_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Rx: ");
        pos2 = valstr.find("rxi");
        xbmc_dv_cap::dv_rx_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Ry: ");
        pos2 = valstr.find("ryi");
        xbmc_dv_cap::dv_ry_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Gx: ");
        pos2 = valstr.find("gxi");
        xbmc_dv_cap::dv_gx_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Gy: ");
        pos2 = valstr.find("gyi");
        xbmc_dv_cap::dv_gy_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("Bx: ");
        pos2 = valstr.find("bxi");
        xbmc_dv_cap::dv_bx_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));

        pos = valstr.find("By: ");
        pos2 = valstr.find("byi");
        xbmc_dv_cap::dv_by_i = std::stoi(valstr.substr(pos+3, pos2-pos-1));
      }
      catch (const std::exception& e)
      {
        CLog::Log(LOGERROR, "AMLUtils::{} - failed to parse dv_cap: {}", __FUNCTION__, e.what());
      }
    }
  }
}

void aml_dv_send_md_levels()
{
  DOVIStreamMetadata dovi_stream_metadata;
  dovi_stream_metadata = CServiceBroker::GetDataCacheCore().GetVideoDoViStreamMetadata();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_source_max_pq", dovi_stream_metadata.source_max_pq);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_source_min_pq", dovi_stream_metadata.source_min_pq);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_level_6_max_lum", dovi_stream_metadata.level6_max_lum);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_level_6_min_lum", dovi_stream_metadata.level6_min_lum);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_level_6_max_cll", dovi_stream_metadata.level6_max_cll);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_md_level_6_max_fall", dovi_stream_metadata.level6_max_fall);
}

void aml_dv_send_hdr10_data()
{
  HDRStaticMetadataInfo hdrStaticMetadataInfo;
  hdrStaticMetadataInfo = CServiceBroker::GetDataCacheCore().GetVideoHDRStaticMetadataInfo();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_max_lum", hdrStaticMetadataInfo.max_lum);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_min_lum", hdrStaticMetadataInfo.min_lum);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_max_cll", hdrStaticMetadataInfo.max_cll);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_max_fall", hdrStaticMetadataInfo.max_fall);
}

void aml_dv_send_el_type()
{
  DOVIStreamInfo dovi_stream_info;
  dovi_stream_info = CServiceBroker::GetDataCacheCore().GetVideoDoViStreamInfo();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_el_type", dovi_stream_info.dovi_el_type);
}

void aml_dv_send_profile(int dvprofile)
{
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_profile", static_cast<unsigned int>(dvprofile));
}

// aml_linux_force_422 moved before aml_dv_on()

static int s_lastSubtitles = -1;
static int s_lastOsd = -1;

void aml_dv_reset_l5_signals()
{
  s_lastSubtitles = -1;
  s_lastOsd = -1;
}

void aml_dv_off(bool skip_hdmi_update)
{
  aml_dv_detect_active_area_stop();

  // change mode and disable.
  CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  unsigned int existing_mode = dolby_vision_mode.Get<unsigned int>().value();
  bool modeChange(existing_mode != DOLBY_VISION_OUTPUT_MODE_BYPASS);

  CLog::Log(LOGDEBUG, "AMLUtils::{} - mode change [{}], existing mode [{}], this mode [{}]",
    __FUNCTION__, modeChange,
    aml_dv_output_mode_to_string(existing_mode),
    aml_dv_output_mode_to_string(DOLBY_VISION_OUTPUT_MODE_BYPASS));

  CSysfsPath dolby_vision_flags{"/sys/module/amdolby_vision/parameters/dolby_vision_flags"};
  CSysfsPath dolby_vision_ll_policy{"/sys/module/amdolby_vision/parameters/dolby_vision_ll_policy"};
  if (dolby_vision_flags.Exists() && dolby_vision_ll_policy.Exists())
  {
    dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_RGB_OUTPUT));
    dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~(FLAG_FORCE_DOVI_LL));
    dolby_vision_ll_policy.Set(DOLBY_VISION_LL_DISABLE);
  }

  CSysfsPath amdolby_vision_debug{"/sys/class/amdolby_vision/debug"};
  if (amdolby_vision_debug.Exists()) CSysfsPath("/sys/class/amdolby_vision/debug", "enable_fel 0");

  // First allow system to reset to follow source, then turn off DV.
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_policy", DOLBY_VISION_FOLLOW_SOURCE);
  if (modeChange) aml_dv_toggle_frame(DOLBY_VISION_OUTPUT_MODE_BYPASS);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_enable", "N");

  // Finally reset back to bypass for consistency.
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_policy", DOLBY_VISION_FORCE_OUTPUT_MODE);
  if (modeChange) CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_mode", DOLBY_VISION_OUTPUT_MODE_BYPASS);
  s_dvModeCached = DOLBY_VISION_OUTPUT_MODE_BYPASS;

  aml_linux_force_422 = false;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_aml_linux_force_422", aml_linux_force_422);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_non_ipt", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_dither", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp_tm", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_osdst", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_subt", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_subtitles", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd", 0);
  aml_dv_reset_l5_signals();

  // Trigger HDMI TX re-evaluation so kernel applies the new (non-DV) output
  // mode.  Skipped when the caller will immediately re-enable DV (e.g.
  // aml_dv_start restoring IPT for DV_MODE_ON) — the subsequent aml_dv_on
  // handles the HDMI update, and sending an intermediate SDR signal here
  // corrupts the HDMI TX color-space state on some displays.
  if (modeChange && !skip_hdmi_update)
  {
    aml_dv_display_auto_now();
    const RESOLUTION_INFO res_info = CDisplaySettings::GetInstance().GetResolutionInfo(CDisplaySettings::GetInstance().GetCurrentResolution());
    write_resolution_ini(res_info);
  }

  // Re-write the display mode to force VPP reconfiguration after DV is
  // disabled.  display_auto_now only triggers HDMI TX re-evaluation via
  // amhdmitx0/attr — the VPP pipeline (DAT_CONV, DOLBY_CTRL, HDMI_FMT_CTRL
  // registers) also needs resetting, which the display/mode re-write triggers.
  // Without this, stale VPP state (e.g. 12-bit dither passthrough) persists
  // into SDR output, causing color corruption.
  // Skipped with skip_hdmi_update (DV_MODE_ON) — the display mode re-write
  // may cascade into HDMI TX changes; the subsequent aml_dv_on + display
  // trigger in CreateNewWindow handles VPP reset for that path.
  if (modeChange && !skip_hdmi_update)
  {
    CSysfsPath display_mode{"/sys/class/display/mode"};
    if (display_mode.Exists())
      display_mode.Set(display_mode.Get<std::string>().value());
  }
}

unsigned int aml_dv_dolby_vision_mode()
{
  CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  return dolby_vision_mode.Get<unsigned int>().value();
}

void aml_dv_open(StreamHdrType hdrType, unsigned int bitDepth, AVColorPrimaries colorPrimaries)
{
  s_dvPlaybackActive = true;

  // Detect PM4K once at playback start for OSD visibility override.
  s_pm4kHome = CServiceBroker::GetGUI()->GetWindowManager().GetWindow(WINDOW_HOME);
  s_pm4kActive = s_pm4kHome && !s_pm4kHome->GetProperty("script.plex.is_active").asString().empty();

  enum DV_MODE dv_mode(aml_dv_mode());
  CLog::Log(LOGINFO, "AMLUtils::{} - Checking DV for DV mode: [{}], DV type: [{}]", __FUNCTION__, aml_dv_mode_to_string(dv_mode), aml_dv_type_to_string(aml_dv_type()));
  if (dv_mode == DV_MODE_ON || dv_mode == DV_MODE_ON_DEMAND) {

    // SDR BT.2020 content: bypass VS10 — the DV library assumes SDR is BT.709
    // and can't handle BT.2020 gamut correctly. Bypass preserves original signaling.
    if (hdrType == StreamHdrType::HDR_TYPE_NONE && colorPrimaries == AVCOL_PRI_BT2020)
    {
      CLog::Log(LOGINFO, "AMLUtils::{} - SDR BT.2020 detected, bypassing VS10 to preserve gamut", __FUNCTION__);
      if (aml_is_dv_enable())
        aml_dv_off();
      return;
    }

    unsigned int vs10_mode = aml_vs10_by_hdrtype(hdrType, bitDepth);    

    if (vs10_mode != DOLBY_VISION_OUTPUT_MODE_BYPASS) 
      vs10_mode = aml_dv_on(vs10_mode);
    else if (aml_is_dv_enable()) // DV BYPASS, and it is on - then switch it off.
      aml_dv_off(); 

    bool content_is_dv(hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION);
    CLog::Log(LOGINFO, "AMLUtils::{} - DV is [{}], requested with vs10 mode: [{}], set for: [{}]",  __FUNCTION__, aml_is_dv_enable(), aml_dv_output_mode_to_string(vs10_mode), content_is_dv ? "content" : "mapping");
  }
}

void aml_dv_close()
{
  s_dvPlaybackActive = false;
  s_pm4kActive = false;
  s_pm4kHome = nullptr;

  // DV_MODE_ON: leave DV enabled in its current output mode.  This avoids
  // costly HDMI mode-switch cycles (off->IPT->playback-mode) during live-TV
  // channel changes where a new aml_dv_open() follows immediately.
  // IPT is restored for the GUI by the Player.OnStop announcement handler
  // in CDolbyVisionAML when playback truly ends.
  if (aml_dv_mode() == DV_MODE_ON)
    return;

  const auto bypass_dv_mode_switch_gui = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_BYPASS);
  if (aml_is_dv_enable() && !bypass_dv_mode_switch_gui)
    aml_dv_off();
}

bool aml_dv_playback_active()
{
  return s_dvPlaybackActive;
}

void aml_dv_set_osd_max(int max)
{
  // Set the OSD DV graphic max.
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max", max);
}

void aml_dv_set_osd_brightness(int nits)
{
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max", nits);
}

void aml_dv_set_hdr10_osd_brightness(int nits)
{
  CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max", nits);
}

bool aml_is_dv_enable()
{
  CSysfsPath dolby_vision_enable{"/sys/module/amdolby_vision/parameters/dolby_vision_enable"};
  return (dolby_vision_enable.Exists() && StringUtils::EqualsNoCase(dolby_vision_enable.Get<std::string>().value(), "Y"));
}

void aml_dv_display_trigger()
{
  if (aml_is_dv_enable()) {
    CSysfsPath display_mode{"/sys/class/display/mode"};
    if (display_mode.Exists()) display_mode.Set(display_mode.Get<std::string>().value());
  }
}

void aml_hdr10plus_vsif_hold(bool hold)
{
  CSysfsPath p{"/sys/module/hdmitx20/parameters/hdr10plus_vsif_hold"};
  if (p.Exists())
    p.Set(hold ? 1 : 0);
}

void aml_dv_display_auto_now()
{
  // hdmi tx store attr "now" - will trigger set_disp_mode_auto. 
  CSysfsPath attr{"/sys/class/amhdmitx/amhdmitx0/attr"};
  if (attr.Exists()) attr.Set("now");
}

// Serializes aml_dv_start() and aml_dv_wait_for_pipeline() so EGL context
// recreation doesn't race with DV pipeline restoration.
static std::mutex s_dvStartMutex;

void aml_dv_start()
{
  std::lock_guard<std::mutex> lock(s_dvStartMutex);

  if (aml_is_dv_enable())
  {
    unsigned int mode = aml_dv_dolby_vision_mode();
    if (mode == DOLBY_VISION_OUTPUT_MODE_IPT || mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL)
      return;

    // The kernel needs the full Bypass toggle + disable cycle to set
    // mode_changed and CP_FLAG_CHANGE_ALL for proper register updates.
    // For DV_MODE_ON, skip the display_auto_now in aml_dv_off — it sends
    // an intermediate SDR signal that corrupts HDMI TX color-space on
    // some displays.  aml_dv_on(IPT) below handles its own HDMI update.
    aml_dv_off(/*skip_hdmi_update=*/aml_dv_mode() == DV_MODE_ON);
  }

  if (aml_dv_mode() == DV_MODE_ON) {
    aml_dv_reset_osd_max();
    aml_dv_on(DOLBY_VISION_OUTPUT_MODE_IPT);
  }
}

void aml_dv_wait_for_pipeline()
{
  // Block until any in-progress aml_dv_start() completes. Called from
  // CreateNewWindow to prevent EGL surface creation while the DV pipeline
  // is transitioning through Bypass mode (causes color corruption).
  std::lock_guard<std::mutex> lock(s_dvStartMutex);
}

void aml_dv_set_subtitles(bool visible)
{
  int val = visible ? 1 : 0;
  if (val != s_lastSubtitles)
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_subtitles", val);
    s_lastSubtitles = val;
  }
}

void aml_dv_set_xbmc_osd()
{
  auto &wm = CServiceBroker::GetGUI()->GetWindowManager();

  bool osd_active;
  if (s_pm4kActive && s_pm4kHome)
  {
    osd_active = s_pm4kHome->GetProperty("script.plex.osd_active").asString() == "1";
  }
  else
    osd_active = wm.HasVisibleDialog() ||
                 wm.IsWindowVisible(WINDOW_VIDEO_MENU) ||
                 CServiceBroker::GetDataCacheCore().GetAVChangeExtended();

  int val = osd_active ? 1 : 0;
  if (val != s_lastOsd)
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd", val);
    s_lastOsd = val;
  }
}

bool aml_dv_use_active_area()
{
  // s_dvModeCached avoids per-frame sysfs reads (updated by aml_dv_on/off).
  // Settings are read live so toggling between playbacks takes effect immediately.
  return s_dvModeCached == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL &&
         settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5) &&
         settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_RESTRICT_SUBS_ACTIVE_AREA);
}

int aml_dv_l5_subs_signal_mode()
{
  if (s_dvModeCached != DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL ||
      !settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5))
    return 0;
  return settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS);
}

bool aml_dv_detect_active_area_enabled()
{
  return s_dvModeCached == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL &&
         settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5) &&
         settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA);
}

/* Cached detected values — written by background detection thread,
 * read by CalcOverlayActiveArea on the render thread. */
static std::atomic<bool> s_detectStable{false};
static std::atomic<int> s_detectState{DV_DETECT_FAILED};
static std::atomic<uint16_t> s_detectedTop{0};
static std::atomic<uint16_t> s_detectedBottom{0};
static std::atomic<uint16_t> s_detectedLeft{0};
static std::atomic<uint16_t> s_detectedRight{0};

bool aml_dv_detect_active_area_stable()
{
  return s_detectStable.load();
}

int aml_dv_detect_active_area_state()
{
  if (!aml_dv_detect_active_area_enabled())
    return DV_DETECT_INACTIVE;
  return s_detectState.load();
}

void aml_dv_detect_active_area_get(uint16_t& top, uint16_t& bottom, uint16_t& left, uint16_t& right)
{
  top = s_detectedTop.load();
  bottom = s_detectedBottom.load();
  left = s_detectedLeft.load();
  right = s_detectedRight.load();
}

/* Common aspect ratios × 1000 for snapping */
static const uint32_t s_commonAR[] = {
  1333, 1370, 1667, 1778, 1850, 1896, 2000, 2200, 2350, 2390, 2400, 2550, 2760
};

/* Cancel flag — set by stop(), checked by ffmpeg interrupt callback and
 * between seek positions.  Allows clean abort of slow network I/O. */
static std::atomic<bool> s_detectCancel{false};

static int detect_interrupt_cb(void *opaque)
{
  return s_detectCancel.load() ? 1 : 0;
}

/* AVIO callbacks for reading through Kodi's VFS (handles nfs://, smb://, etc.) */
static int detect_avio_read(void *opaque, uint8_t *buf, int size)
{
  auto* file = static_cast<XFILE::CFile*>(opaque);
  int ret = file->Read(buf, size);
  return (ret == 0) ? AVERROR_EOF : ret;
}

static int64_t detect_avio_seek(void *opaque, int64_t pos, int whence)
{
  auto* file = static_cast<XFILE::CFile*>(opaque);
  if (whence == AVSEEK_SIZE)
    return file->GetLength();
  return file->Seek(pos, whence & ~AVSEEK_FORCE);
}

static bool detect_throttle_enabled()
{
  return settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_THROTTLE);
}

/* Sleep in small increments, checking cancel flag.  Returns true if cancelled. */
static bool detect_sleep_ms(int ms)
{
  const int step = 100;
  for (int elapsed = 0; elapsed < ms; elapsed += step)
  {
    if (s_detectCancel.load())
      return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(std::min(step, ms - elapsed)));
  }
  return s_detectCancel.load();
}

static void DetectActiveAreaFromFile(const std::string& filePath)
{
  AVFormatContext* fmtCtx = nullptr;
  AVCodecContext* codecCtx = nullptr;
  AVFrame* frame = nullptr;
  AVPacket pkt;
  AVIOContext* avioCtx = nullptr;
  XFILE::CFile file;
  uint8_t* avioBuf = nullptr;
  const int bufSize = 32768;
  int videoIdx = -1;
  uint16_t detTop = 0, detBottom = 0, detLeft = 0, detRight = 0;
  const bool throttle = detect_throttle_enabled();
  if (throttle)
    CLog::Log(LOGDEBUG, "DetectActiveArea: I/O throttle enabled, delaying 3s");

  /* BDMV/DVD: the file path is a bluray:// or dvd:// playlist URL that
   * our AVIO wrapper can't resolve — the VFS handler needs the full Kodi
   * playback context.  Skip early instead of failing at avformat_open. */
  if (StringUtils::StartsWithNoCase(filePath, "bluray://") ||
      StringUtils::StartsWithNoCase(filePath, "dvd://"))
  {
    CLog::Log(LOGINFO, "DetectActiveArea: disc source ({}) — skipping",
              filePath.substr(0, filePath.find("//")+2));
    s_detectState.store(DV_DETECT_SKIPPED);
    goto cleanup;
  }

  /* Throttle: let playback establish its I/O pipeline before we start
   * competing for network bandwidth. */
  if (throttle && detect_sleep_ms(3000))
    goto cleanup;

  /* Open through Kodi VFS — supports nfs://, smb://, local paths, etc. */
  if (!file.Open(filePath, XFILE::READ_NO_CACHE))
  {
    CLog::Log(LOGWARNING, "DetectActiveArea: failed to open {}", filePath);
    goto cleanup;
  }

  avioBuf = static_cast<uint8_t*>(av_malloc(bufSize));
  if (!avioBuf)
    goto cleanup;

  avioCtx = avio_alloc_context(avioBuf, bufSize, 0, &file,
                               detect_avio_read, nullptr, detect_avio_seek);
  if (!avioCtx)
  {
    av_free(avioBuf);
    goto cleanup;
  }

  fmtCtx = avformat_alloc_context();
  if (!fmtCtx)
  {
    avio_context_free(&avioCtx);
    goto cleanup;
  }
  fmtCtx->pb = avioCtx;
  fmtCtx->interrupt_callback.callback = detect_interrupt_cb;
  fmtCtx->interrupt_callback.opaque = nullptr;

  if (s_detectCancel.load())
    goto cleanup;

  if (avformat_open_input(&fmtCtx, filePath.c_str(), nullptr, nullptr) < 0)
  {
    CLog::Log(LOGWARNING, "DetectActiveArea: failed to open input {}", filePath);
    goto cleanup;
  }

  /* MPEG-TS (m2ts/ts) has no reliable seek index — byte-position estimation
   * often produces stale frames (decoder returns same frame repeatedly).
   * Skip rather than waste I/O on unreliable seeks. */
  if (fmtCtx->iformat && fmtCtx->iformat->name &&
      (strstr(fmtCtx->iformat->name, "mpegts") ||
       strstr(fmtCtx->iformat->name, "m2ts")))
  {
    CLog::Log(LOGINFO, "DetectActiveArea: MPEG-TS container ({}) — skipping, "
              "seek unreliable", fmtCtx->iformat->name);
    s_detectState.store(DV_DETECT_SKIPPED);
    goto cleanup;
  }

  if (avformat_find_stream_info(fmtCtx, nullptr) < 0)
  {
    CLog::Log(LOGWARNING, "DetectActiveArea: failed to find stream info");
    goto cleanup;
  }

  for (unsigned i = 0; i < fmtCtx->nb_streams; i++)
  {
    if (fmtCtx->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_VIDEO)
    {
      videoIdx = i;
      break;
    }
  }
  if (videoIdx < 0)
  {
    CLog::Log(LOGWARNING, "DetectActiveArea: no video stream found");
    goto cleanup;
  }

  {
    const AVCodec* codec = avcodec_find_decoder(fmtCtx->streams[videoIdx]->codecpar->codec_id);
    if (!codec)
    {
      CLog::Log(LOGWARNING, "DetectActiveArea: no decoder for codec id {}",
                fmtCtx->streams[videoIdx]->codecpar->codec_id);
      goto cleanup;
    }

    codecCtx = avcodec_alloc_context3(codec);
    if (!codecCtx)
      goto cleanup;

    avcodec_parameters_to_context(codecCtx, fmtCtx->streams[videoIdx]->codecpar);
    /* Use 2 threads for software decode — fast enough, doesn't starve playback */
    codecCtx->thread_count = 2;
    /* Only decode keyframes — we need one frame per seek position, not a
     * full GOP. Dramatically reduces decode time for P7 FEL content where
     * the decoder otherwise processes entire reference frame chains. */
    codecCtx->skip_frame = AVDISCARD_NONKEY;
    codecCtx->skip_loop_filter = AVDISCARD_ALL;

    if (avcodec_open2(codecCtx, codec, nullptr) < 0)
    {
      CLog::Log(LOGWARNING, "DetectActiveArea: failed to open codec");
      goto cleanup;
    }
  }

  CLog::Log(LOGDEBUG, "DetectActiveArea: opened {}x{} codec id {} for scanning",
            codecCtx->width, codecCtx->height, codecCtx->codec_id);

  frame = av_frame_alloc();
  if (!frame)
    goto cleanup;

  av_init_packet(&pkt);

  /* Pre-cropped content: if encoded resolution is significantly non-16:9,
   * there are no bars to detect — the resolution IS the active area.
   * Subtitle restriction uses displayLB (frame vs display difference). */
  {
    uint32_t refH = (uint32_t)codecCtx->width * 9 / 16;
    uint32_t refW = (uint32_t)codecCtx->height * 16 / 9;
    int tbGap = ((int)refH > codecCtx->height) ? ((int)refH - codecCtx->height) / 2 : 0;
    int lrGap = ((int)refW > codecCtx->width) ? ((int)refW - codecCtx->width) / 2 : 0;
    if (tbGap > 20 || lrGap > 20)
    {
      CLog::Log(LOGINFO, "DetectActiveArea: pre-cropped {}x{} (implied T/B={} L/R={}) — "
                "no bars to scan, subtitle restriction uses display letterbox",
                codecCtx->width, codecCtx->height, tbGap, lrGap);
      s_detectState.store(DV_DETECT_SKIP_NON16X9);
      goto cleanup;
    }
  }

  /* Sample at 7 spread positions for robustness against fades, title cards,
   * or dark scenes. If a frame doesn't have enough contrast for reliable
   * border detection, retry at +1% offsets to stay in the same scene.
   * All samples are collected (no early exit) to detect variable AR content. */
  {
    const int seekPercents[] = {0, 15, 30, 45, 60, 75, 88};
    const int numSeeks = 7;
    const int maxRetries = 8;
    const uint32_t minContrast = 10; /* minimum border-vs-content difference for detection */
    uint16_t samples_top[7] = {}, samples_bottom[7] = {};
    uint16_t samples_left[7] = {}, samples_right[7] = {};
    int validSamples = 0;
    int lastWidth = 0, lastHeight = 0;
    const int agreeTolerance = 5; /* pixels */
    /* Stale-frame detection: catches broken-seek cases (format-specific
     * quirks beyond the MPEG-TS skip) where the decoder returns the same
     * frame repeatedly.  If we see identical pixel values across several
     * seek positions, abort — it's not producing fresh data. */
    uint32_t staleRow0 = UINT32_MAX, staleMid = UINT32_MAX, staleLast = UINT32_MAX;
    int staleCount = 0;
    const int maxStale = 5;
    bool staleAbort = false;

    auto pickBest = [](uint16_t* v, int n) -> uint16_t {
      uint16_t best = v[0];
      int bestCount = 0;
      for (int i = 0; i < n; i++) {
        int count = 0;
        for (int j = 0; j < n; j++)
          if (v[j] == v[i]) count++;
        if (count > bestCount) { bestCount = count; best = v[i]; }
      }
      if (bestCount >= 2) return best;
      std::sort(v, v + n);
      return v[n / 2];
    };

    auto countSupport = [&agreeTolerance](uint16_t* v, int n, uint16_t picked) -> int {
      int support = 0;
      for (int i = 0; i < n; i++)
        if (std::abs((int)v[i] - (int)picked) <= agreeTolerance)
          support++;
      return support;
    };

    for (int s = 0; s < numSeeks && validSamples < numSeeks; s++)
    {
      if (s_detectCancel.load())
        break;

      /* Throttle: yield between seek positions to let playback I/O breathe */
      if (throttle && s > 0 && detect_sleep_ms(500))
        break;

      bool usable = false;

      for (int retry = 0; retry <= maxRetries && !usable; retry++)
      {
        int seekPct = seekPercents[s] + retry;
        if (seekPct > 90) break;

        if (fmtCtx->duration > 0)
        {
          avcodec_flush_buffers(codecCtx);
          av_seek_frame(fmtCtx, -1,
                        fmtCtx->duration * seekPct / 100,
                        AVSEEK_FLAG_BACKWARD);
        }
        else if (s > 0 || retry > 0)
          break; /* unseekable — one attempt only */

        /* Decode one frame. Count only video packets toward the limit —
         * multi-stream remuxes (DV P7 with 12+ streams) can have very few
         * video packets per 100 total. Also drain the decoder properly:
         * it may need several packets before producing a frame. */
        bool gotFrame = false;
        for (int vidPkts = 0; vidPkts < 50 && !gotFrame; )
        {
          if (av_read_frame(fmtCtx, &pkt) < 0)
            break;
          if (pkt.stream_index != videoIdx)
          {
            av_packet_unref(&pkt);
            continue;
          }
          vidPkts++;
          avcodec_send_packet(codecCtx, &pkt);
          av_packet_unref(&pkt);
          while (avcodec_receive_frame(codecCtx, frame) == 0)
          {
            gotFrame = true;
            break;
          }
        }

        /* Flush: the decoder may hold a decoded frame waiting for the next
         * packet (dual-RPU DV, B-frame reordering). Send NULL to drain. */
        if (!gotFrame)
        {
          avcodec_send_packet(codecCtx, nullptr);
          if (avcodec_receive_frame(codecCtx, frame) == 0)
            gotFrame = true;
        }

        if (!gotFrame || !frame->data[0] || frame->width < 64 || frame->height < 64)
          continue;

        lastWidth = frame->width;
        lastHeight = frame->height;
        const int stride = frame->linesize[0];
        const uint8_t* yData = frame->data[0];
        const bool isP010 = (frame->format == AV_PIX_FMT_P010LE ||
                             frame->format == AV_PIX_FMT_P010BE);
        const bool is10bit = isP010 ||
                             frame->format == AV_PIX_FMT_YUV420P10LE ||
                             frame->format == AV_PIX_FMT_YUV420P10BE;
        const int shift = isP010 ? 8 : (is10bit ? 2 : 0);
        const int sampleW = std::min(64, lastWidth / 2);
        const int sampleStartX = lastWidth / 2 - sampleW / 2;

        auto getY = [&](int row, int col) -> uint32_t {
          if (is10bit)
            return reinterpret_cast<const uint16_t*>(yData + row * stride)[col] >> shift;
          return yData[row * stride + col];
        };

        uint32_t row0Y = getY(0, lastWidth / 2);
        uint32_t midY = getY(lastHeight / 2, lastWidth / 2);
        uint32_t lastY = getY(lastHeight - 1, lastWidth / 2);
        uint32_t borderY = std::min(row0Y, lastY);
        uint32_t vContrast = (midY > borderY) ? (midY - borderY) : 0;

        /* Horizontal contrast: for pillarbox content (e.g. 4:3 in 16:9),
         * vertical contrast may be low but L/R edge-vs-center differs. */
        uint32_t col0Y = getY(lastHeight / 2, 0);
        uint32_t colLastY = getY(lastHeight / 2, lastWidth - 1);
        uint32_t colBorderY = std::min(col0Y, colLastY);
        uint32_t hContrast = (midY > colBorderY) ? (midY - colBorderY) : 0;
        uint32_t contrast = std::max(vContrast, hContrast);

        CLog::Log(LOGDEBUG, "DetectActiveArea: sample at {}%: {}x{} fmt={} shift={} "
                  "row0={} mid={} last={} contrast={} (v={} h={})",
                  seekPct, lastWidth, lastHeight, frame->format, shift,
                  row0Y, midY, lastY, contrast, vContrast, hContrast);

        /* Stale-frame check: if the decoder keeps returning the exact same
         * pixel values, seeks aren't working.  Abort rather than waste I/O. */
        if (row0Y == staleRow0 && midY == staleMid && lastY == staleLast)
        {
          if (++staleCount >= maxStale)
          {
            CLog::Log(LOGWARNING, "DetectActiveArea: decoder returning identical "
                      "frames ({} times) — seeks not working, aborting",
                      staleCount);
            staleAbort = true;
            break;
          }
        }
        else
        {
          staleCount = 0;
          staleRow0 = row0Y; staleMid = midY; staleLast = lastY;
        }

        if (contrast < minContrast)
        {
          CLog::Log(LOGDEBUG, "DetectActiveArea: insufficient contrast at {}% ({}), retrying",
                    seekPct, contrast);
          continue;
        }
        usable = true;
      }

      if (staleAbort)
      {
        s_detectState.store(DV_DETECT_FAILED);
        goto cleanup;
      }

      if (!usable)
        continue;

      /* Re-derive frame accessors from the decoded frame (still valid) */
      const int stride = frame->linesize[0];
      const uint8_t* yData = frame->data[0];
      const bool isP010 = (frame->format == AV_PIX_FMT_P010LE ||
                           frame->format == AV_PIX_FMT_P010BE);
      const bool is10bit = isP010 ||
                           frame->format == AV_PIX_FMT_YUV420P10LE ||
                           frame->format == AV_PIX_FMT_YUV420P10BE;
      const int shift = isP010 ? 8 : (is10bit ? 2 : 0);
      const int sampleW = std::min(64, lastWidth / 2);
      const int sampleStartX = lastWidth / 2 - sampleW / 2;

      auto getY = [&](int row, int col) -> uint32_t {
        if (is10bit)
          return reinterpret_cast<const uint16_t*>(yData + row * stride)[col] >> shift;
        return yData[row * stride + col];
      };

      /* Adaptive scan threshold: midpoint between border and content levels.
       * Handles dark DV content (Foundation: border=16, content=28 → threshold=22)
       * where a fixed threshold of 32 would miss the transition entirely. */
      uint32_t borderAvg = 0, contentAvg = 0;
      for (int i = 0; i < sampleW; i++) borderAvg += getY(0, sampleStartX + i);
      for (int i = 0; i < sampleW; i++) contentAvg += getY(lastHeight / 2, sampleStartX + i);
      borderAvg /= sampleW;
      contentAvg /= sampleW;
      uint32_t scanThreshold = (borderAvg + contentAvg) / 2;

      uint16_t sTop = 0, sBottom = 0, sLeft = 0, sRight = 0;

      for (int row = 0; row < lastHeight / 2; row++)
      {
        uint32_t sum = 0;
        for (int i = 0; i < sampleW; i++) sum += getY(row, sampleStartX + i);
        if (sum / sampleW > scanThreshold) { sTop = static_cast<uint16_t>(row); break; }
      }
      for (int row = lastHeight - 1; row >= lastHeight / 2; row--)
      {
        uint32_t sum = 0;
        for (int i = 0; i < sampleW; i++) sum += getY(row, sampleStartX + i);
        if (sum / sampleW > scanThreshold) { sBottom = static_cast<uint16_t>(lastHeight - 1 - row); break; }
      }
      /* Skip L/R scan when frame is already wider than 16:9 — pillarbox
       * is impossible on a 16:9 display, and dark edges cause false positives. */
      if (lastWidth * 1000 / lastHeight < 1778)
      {
        const int sampleH = std::min(64, lastHeight / 2);
        const int sampleStartY = lastHeight / 2 - sampleH / 2;

        /* Separate L/R threshold from column-based sampling — the row-based
         * scanThreshold is calibrated for letterbox (horizontal border brightness)
         * and can be completely wrong for pillarbox. Justice League: row-based
         * threshold ≈30 missed 480px bars because bar/content were both dark
         * horizontally but had clear vertical contrast at the L/R edges. */
        uint32_t lrBorderAvg = 0, lrContentAvg = 0;
        for (int i = 0; i < sampleH; i++) lrBorderAvg += getY(sampleStartY + i, 0);
        for (int i = 0; i < sampleH; i++) lrContentAvg += getY(sampleStartY + i, lastWidth / 2);
        lrBorderAvg /= sampleH;
        lrContentAvg /= sampleH;
        uint32_t lrThreshold = (lrBorderAvg + lrContentAvg) / 2;

        for (int col = 0; col < lastWidth / 2; col++)
        {
          uint32_t sum = 0;
          for (int i = 0; i < sampleH; i++) sum += getY(sampleStartY + i, col);
          if (sum / sampleH > lrThreshold) { sLeft = static_cast<uint16_t>(col); break; }
        }
        for (int col = lastWidth - 1; col >= lastWidth / 2; col--)
        {
          uint32_t sum = 0;
          for (int i = 0; i < sampleH; i++) sum += getY(sampleStartY + i, col);
          if (sum / sampleH > lrThreshold) { sRight = static_cast<uint16_t>(lastWidth - 1 - col); break; }
        }
      }

      samples_top[validSamples] = sTop;
      samples_bottom[validSamples] = sBottom;
      samples_left[validSamples] = sLeft;
      samples_right[validSamples] = sRight;
      validSamples++;

      CLog::Log(LOGDEBUG, "DetectActiveArea: sample {}: T={} B={} L={} R={}",
                validSamples, sTop, sBottom, sLeft, sRight);

      /* No early exit — always collect all available samples.
       * Variable AR content (IMAX + scope) needs every sample to detect
       * the full-frame outlier that prevents a false scope crop. */

      /* Check if source L5 appeared during our scan — by now the RPU parser
       * has had time to process frames from the current playback (no stale
       * data risk unlike an upfront check).  Abort to avoid wasted I/O. */
      {
        auto srcMeta = CServiceBroker::GetDataCacheCore().GetVideoDoViFrameMetadata();
        if (srcMeta.has_level5_metadata &&
            (srcMeta.level5_active_area_top_offset || srcMeta.level5_active_area_bottom_offset ||
             srcMeta.level5_active_area_left_offset || srcMeta.level5_active_area_right_offset))
        {
          CLog::Log(LOGINFO, "DetectActiveArea: source L5 appeared (T={} B={}) — aborting scan",
                    srcMeta.level5_active_area_top_offset, srcMeta.level5_active_area_bottom_offset);
          s_detectState.store(DV_DETECT_SKIPPED);
          s_detectStable.store(true);
          goto cleanup;
        }
      }
    }

    if (validSamples == 0)
    {
      CLog::Log(LOGWARNING, "DetectActiveArea: no usable frames decoded from {}x{} "
                "(all {} positions had insufficient contrast or decode failures)",
                codecCtx->width, codecCtx->height, numSeeks);
      goto cleanup;
    }

    /* Require enough usable samples for confident detection. If too many
     * positions failed (dark DV content), the unanalysed portions may
     * contain fullscreen scenes we can't see — e.g. Foundation's dark
     * intro is fullscreen but too dark for contrast-based analysis. */
    {
      const int minUsable = numSeeks - 1; /* at most 1 failed position */
      if (validSamples < minUsable)
      {
        CLog::Log(LOGINFO, "DetectActiveArea: insufficient coverage ({}/{} positions usable, "
                  "need {}) — skipping to avoid uncertain detection",
                  validSamples, numSeeks, minUsable);
        s_detectState.store(DV_DETECT_SKIPPED);
        goto cleanup;
      }
    }

    detTop = pickBest(samples_top, validSamples);
    detBottom = pickBest(samples_bottom, validSamples);

    /* T/B consensus: require majority of valid samples.
     * If one side has consensus but the other doesn't, use the consistent
     * side for both — letterbox borders are symmetric by definition.
     * If T and B independently pick the same value with 2+ support each,
     * accept it — the corroboration is strong evidence even without
     * individual majority (common on dark DV content).
     * If neither has consensus, likely IMAX hybrid — bail. */
    {
      int required = (validSamples + 1) / 2; /* majority */
      if (required < 2) required = 2;
      int topSupport = countSupport(samples_top, validSamples, detTop);
      int botSupport = countSupport(samples_bottom, validSamples, detBottom);
      bool topOk = topSupport >= required;
      bool botOk = botSupport >= required;

      /* Corroboration: T and B independently agree → accept with 2+ each */
      bool corroborated = !topOk && !botOk &&
                          topSupport >= 2 && botSupport >= 2 &&
                          std::abs((int)detTop - (int)detBottom) <= agreeTolerance;

      if (corroborated)
      {
        /* Use the side with more support, or B if equal */
        uint16_t agreed = (topSupport >= botSupport) ? detTop : detBottom;
        CLog::Log(LOGDEBUG, "DetectActiveArea: T/B corroborated at {} (T={}/{} B={}/{} support)",
                  agreed, detTop, topSupport, detBottom, botSupport);
        detTop = detBottom = agreed;
      }
      else if (!topOk && !botOk && validSamples >= 3)
      {
        CLog::Log(LOGINFO, "DetectActiveArea: no T/B consensus (T={}/{} B={}/{} of {} needed) — skipping",
                  detTop, topSupport, detBottom, botSupport, required);
        s_detectState.store(DV_DETECT_SKIPPED);
        goto cleanup;
      }
      else if (topOk && !botOk)
      {
        CLog::Log(LOGDEBUG, "DetectActiveArea: B inconsistent ({} support), using T={} for both",
                  botSupport, detTop);
        detBottom = detTop;
      }
      else if (botOk && !topOk)
      {
        CLog::Log(LOGDEBUG, "DetectActiveArea: T inconsistent ({} support), using B={} for both",
                  topSupport, detBottom);
        detTop = detBottom;
      }
    }

    /* Variable AR check: if consensus found significant bars but any sample
     * had no bars at all, the movie likely has IMAX/open-matte scenes mixed
     * with scope. Reject to avoid cropping full-frame scenes.
     * Threshold: 2.5% of frame height (~54px on 2160p). */
    if (detTop > 0 || detBottom > 0)
    {
      uint16_t minSignificant = static_cast<uint16_t>(lastHeight / 40);
      if (detTop >= minSignificant || detBottom >= minSignificant)
      {
        for (int i = 0; i < validSamples; i++)
        {
          /* Check if either side has no bar — on a real scope frame, row 0
           * is always bar level (Y≈16), so T is always well above 0.
           * T=0 means row 0 is content (fullscreen).  B can still show a
           * large false value from dark content below the scan threshold,
           * so require only one side to indicate no bar. */
          if (samples_top[i] <= agreeTolerance || samples_bottom[i] <= agreeTolerance)
          {
            CLog::Log(LOGINFO, "DetectActiveArea: variable AR (sample {} T={} B={}, "
                      "consensus T={} B={}) — skipping to avoid IMAX crop",
                      i + 1, samples_top[i], samples_bottom[i], detTop, detBottom);
            s_detectState.store(DV_DETECT_SKIP_IMAX);
            goto cleanup;
          }
        }
      }
    }

    detLeft = pickBest(samples_left, validSamples);
    detRight = pickBest(samples_right, validSamples);

    /* L/R: require majority support AND symmetric.
     * Symmetry is the primary false-positive guard for pillarbox — real
     * pillarbox is always symmetric, dark-edge artifacts are not. */
    {
      int lrRequired = (validSamples + 1) / 2;
      if (lrRequired < 2) lrRequired = 2;
      int leftSupport = countSupport(samples_left, validSamples, detLeft);
      int rightSupport = countSupport(samples_right, validSamples, detRight);
      if (leftSupport < lrRequired || rightSupport < lrRequired ||
          (detLeft && detRight &&
           std::abs((int)detLeft - (int)detRight) > (int)std::max(detLeft, detRight) / 10))
      {
        CLog::Log(LOGDEBUG, "DetectActiveArea: L/R rejected (support {}/{} of {} needed, "
                  "L={} R={}) — ignoring",
                  leftSupport, rightSupport, lrRequired, detLeft, detRight);
        detLeft = detRight = 0;
      }
    }

    /* Validate and snap to common AR */
    if (detTop || detBottom || detLeft || detRight)
    {
      const uint32_t activeW = lastWidth - detLeft - detRight;
      const uint32_t activeH = lastHeight - detTop - detBottom;

      if (activeW > 0 && activeH > 0)
      {
        const uint32_t arX1000 = (activeW * 1000) / activeH;
        const uint32_t frameAR = (lastWidth * 1000) / lastHeight;

        if (arX1000 >= 1200 && arX1000 <= 2900)
        {
          uint32_t bestAR = arX1000, bestDist = UINT32_MAX;
          for (auto ar : s_commonAR)
          {
            uint32_t dist = (arX1000 > ar) ? (arX1000 - ar) : (ar - arX1000);
            if (dist < bestDist) { bestDist = dist; bestAR = ar; }
          }
          if (bestDist * 100 > arX1000 * 5)
            bestAR = arX1000;

          if (bestAR >= frameAR)
          {
            uint32_t snapH = (lastWidth * 1000 + bestAR / 2) / bestAR;
            if (snapH > (uint32_t)lastHeight) snapH = lastHeight;
            uint16_t tb = static_cast<uint16_t>((lastHeight - snapH) / 2);
            detTop = tb; detBottom = tb; detLeft = 0; detRight = 0;
          }
          else
          {
            uint32_t snapW = (lastHeight * bestAR + 500) / 1000;
            if (snapW > (uint32_t)lastWidth) snapW = lastWidth;
            uint16_t lr = static_cast<uint16_t>((lastWidth - snapW) / 2);
            detLeft = lr; detRight = lr; detTop = 0; detBottom = 0;
          }

          CLog::Log(LOGINFO, "DetectActiveArea: {}x{} → T={} B={} L={} R={} "
                    "(AR={}.{:03d} snap={}.{:03d}, {} samples)",
                    lastWidth, lastHeight, detTop, detBottom, detLeft, detRight,
                    arX1000 / 1000, arX1000 % 1000, bestAR / 1000, bestAR % 1000,
                    validSamples);
        }
        else
          detTop = detBottom = detLeft = detRight = 0;
      }
    }
  }

  /* Check if source already provides non-zero L5 — by now the RPU will have
   * been parsed and DataCacheCore populated. Don't override valid source L5. */
  {
    auto srcMeta = CServiceBroker::GetDataCacheCore().GetVideoDoViFrameMetadata();
    if (srcMeta.has_level5_metadata &&
        (srcMeta.level5_active_area_top_offset || srcMeta.level5_active_area_bottom_offset ||
         srcMeta.level5_active_area_left_offset || srcMeta.level5_active_area_right_offset))
    {
      CLog::Log(LOGDEBUG, "DetectActiveArea: source has L5 (T={} B={}) — skipping injection",
                srcMeta.level5_active_area_top_offset, srcMeta.level5_active_area_bottom_offset);
      s_detectState.store(DV_DETECT_SKIPPED);
      s_detectStable.store(true);
      goto cleanup;
    }
  }

  /* Publish results */
  s_detectedTop.store(detTop);
  s_detectedBottom.store(detBottom);
  s_detectedLeft.store(detLeft);
  s_detectedRight.store(detRight);

  /* Write to kernel for L5 injection */
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_top", detTop);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_bottom", detBottom);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_left", detLeft);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_right", detRight);

  s_detectState.store(DV_DETECT_OK);
  s_detectStable.store(true);

  if (detTop || detBottom || detLeft || detRight)
    CLog::Log(LOGINFO, "DetectActiveArea: kernel L5 updated");
  else
    CLog::Log(LOGDEBUG, "DetectActiveArea: no borders found");

cleanup:
  /* Any exit path that didn't set a specific state leaves RUNNING — fall
   * back to FAILED so the skin doesn't show a perpetual spinner. */
  if (s_detectState.load() == DV_DETECT_RUNNING)
    s_detectState.store(DV_DETECT_FAILED);

  if (frame)
    av_frame_free(&frame);
  if (codecCtx)
    avcodec_free_context(&codecCtx);
  if (fmtCtx)
    avformat_close_input(&fmtCtx);
  if (avioCtx)
    avio_context_free(&avioCtx);
}

static std::thread s_detectThread;
static std::string s_detectFilePath;

void aml_dv_detect_set_file(const std::string& path)
{
  s_detectFilePath = path;
}

void aml_dv_detect_active_area_start()
{
  /* Reset state */
  s_detectCancel.store(false);
  s_detectStable.store(false);
  s_detectState.store(DV_DETECT_FAILED);
  s_detectedTop.store(0);
  s_detectedBottom.store(0);
  s_detectedLeft.store(0);
  s_detectedRight.store(0);

  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_top", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_bottom", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_left", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_right", 0);

  /* Use stored path from VideoPlayer::OpenFile — g_application.CurrentFile()
   * is not yet set when aml_dv_on runs from the codec thread. */
  std::string filePath = s_detectFilePath;
  if (filePath.empty())
    filePath = g_application.CurrentFile(); /* fallback */
  CLog::Log(LOGDEBUG, "DetectActiveArea: start — stored='{}' fallback='{}' → '{}'",
            s_detectFilePath.empty() ? "(empty)" : "(set)",
            g_application.CurrentFile().empty() ? "(empty)" : "(set)",
            filePath.empty() ? "(empty)" : CURL::GetRedacted(filePath));
  if (filePath.empty())
  {
    CLog::Log(LOGWARNING, "DetectActiveArea: no file path available");
    return;
  }

  /* Join previous detection thread if still running — cancel it first
   * so we don't block on a slow network read. */
  bool wasJoinable = s_detectThread.joinable();
  if (wasJoinable)
  {
    s_detectCancel.store(true);
    s_detectThread.join();
    s_detectCancel.store(false);
  }
  CLog::Log(LOGDEBUG, "DetectActiveArea: thread join={}, spawning", wasJoinable);
  s_detectState.store(DV_DETECT_RUNNING);

  s_detectThread = std::thread([filePath]() {
    DetectActiveAreaFromFile(filePath);
  });
}

void aml_dv_detect_active_area_stop()
{
  s_detectCancel.store(true);
  s_detectStable.store(false);
  s_detectState.store(DV_DETECT_FAILED);
  if (s_detectThread.joinable())
    s_detectThread.join();

  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_top", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_bottom", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_left", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_right", 0);
}

enum DV_MODE aml_dv_mode()
{
  return static_cast<DV_MODE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE));
}

enum DV_TYPE aml_dv_type()
{
  return static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE));
}

unsigned int aml_vs10_by_setting(const std::string setting)
{
  return static_cast<unsigned int>(settings()->GetInt(setting));
}

void aml_dv_enable_fel() 
{
  CSysfsPath("/sys/class/amdolby_vision/debug", "enable_fel 1");  
}

void aml_hevc_nal_skip_policy(const int value) 
{
  CSysfsPath("/sys/module/amvdec_h265/parameters/nal_skip_policy", value);  
}

void aml_set_transfer_pq(StreamHdrType hdrType, unsigned int bitDepth) {

  // Configure GUI/OSD for HDR PQ when display is in HDR PQ mode
  bool hdr_display(CServiceBroker::GetWinSystem()->IsHDRDisplay() || aml_display_support_dv());
  bool dv_on(aml_dv_mode() != DV_MODE_OFF);
  bool hdr(false);

  if (hdr_display) // Only relevant with an hdr_display 
  {
    // TODO: any need to test display supports each hdr content (inc fallback) specifically?
    hdr = (hdrType != StreamHdrType::HDR_TYPE_NONE);

    // When VP is active, the DV compositor handles OSD tone mapping
    // via dolby_vision_graphic_max. Skip GLES PQ scaling for VP modes.
    // For VS10 (non-VP), enable PQ scaling so the HDR PQ slider works.
    if (dv_on) {
      unsigned int dv_vp = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR);
      unsigned int vs10_mode = aml_vs10_by_hdrtype(hdrType, bitDepth);
      hdr = (((vs10_mode == DOLBY_VISION_OUTPUT_MODE_BYPASS) && hdr) ||
              (vs10_mode <= DOLBY_VISION_OUTPUT_MODE_HDR10)) && (dv_vp == 0);
    }
  }

  CLog::Log(LOGINFO, "AMLUtils::{} - {}DV support, {}, HDR type is {}, transfer PQ is {}",
          __FUNCTION__,
          aml_support_dolby_vision() ? "" : "no ",
          dv_on ? "enabled" : "disabled",
          CStreamDetails::HdrTypeToString(hdrType),
          hdr ? "set" : "not set");

  CServiceBroker::GetWinSystem()->GetGfxContext().SetTransferPQ(hdr);
}

bool aml_has_frac_rate_policy()
{
  static int has_frac_rate_policy = -1;

  if (has_frac_rate_policy == -1)
  {
    CSysfsPath amhdmitx0_frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    has_frac_rate_policy = static_cast<int>(amhdmitx0_frac_rate_policy.Exists());
  }

  return (has_frac_rate_policy == 1);
}

void aml_video_mute(bool mute)
{
  static int _mute = -1;

  if (_mute == -1 || (_mute != !!mute))
  {
    _mute = !!mute;
    CSysfsPath("/sys/class/amhdmitx/amhdmitx0/vid_mute", _mute);
    CLog::Log(LOGDEBUG, "AMLUtils::{} - {} video", __FUNCTION__, mute ? "mute" : "unmute");
  }
}

void aml_set_audio_passthrough(bool passthrough)
{
  CSysfsPath("/sys/class/audiodsp/digital_raw", (passthrough ? 2 : 0));
}

void aml_set_3d_video_mode(unsigned int mode, bool framepacking_support, int view_mode)
{
  int fd;
  if ((fd = open("/dev/amvideo", O_RDWR)) >= 0)
  {
    if (ioctl(fd, AMSTREAM_IOC_SET_3D_TYPE, mode) != 0)
      CLog::Log(LOGERROR, "AMLUtils::{} - unable to set 3D video mode 0x%x", __FUNCTION__, mode);
    close(fd);

    CSysfsPath("/sys/module/amvideo/parameters/framepacking_support", framepacking_support ? 1 : 0);
    CSysfsPath("/sys/module/amvdec_h264mvc/parameters/view_mode", view_mode);
  }
}

void aml_probe_hdmi_audio()
{
  // Audio {format, channel, freq, cce}
  // {1, 7, 7f, 7}
  // {7, 5, 1e, 0}
  // {2, 5, 7, 0}
  // {11, 7, 7e, 1}
  // {10, 7, 6, 0}
  // {12, 7, 7e, 0}

  int fd = open("/sys/class/amhdmitx/amhdmitx0/edid", O_RDONLY);
  if (fd >= 0)
  {
    char valstr[1024] = {0};

    read(fd, valstr, sizeof(valstr) - 1);
    valstr[strlen(valstr)] = '\0';
    close(fd);

    std::vector<std::string> probe_str = StringUtils::Split(valstr, "\n");

    for (std::vector<std::string>::const_iterator i = probe_str.begin(); i != probe_str.end(); ++i)
    {
      if (i->find("Audio") == std::string::npos)
      {
        for (std::vector<std::string>::const_iterator j = i + 1; j != probe_str.end(); ++j)
        {
          if      (j->find("{1,")  != std::string::npos)
            printf(" PCM found {1,\n");
          else if (j->find("{2,")  != std::string::npos)
            printf(" AC3 found {2,\n");
          else if (j->find("{3,")  != std::string::npos)
            printf(" MPEG1 found {3,\n");
          else if (j->find("{4,")  != std::string::npos)
            printf(" MP3 found {4,\n");
          else if (j->find("{5,")  != std::string::npos)
            printf(" MPEG2 found {5,\n");
          else if (j->find("{6,")  != std::string::npos)
            printf(" AAC found {6,\n");
          else if (j->find("{7,")  != std::string::npos)
            printf(" DTS found {7,\n");
          else if (j->find("{8,")  != std::string::npos)
            printf(" ATRAC found {8,\n");
          else if (j->find("{9,")  != std::string::npos)
            printf(" One_Bit_Audio found {9,\n");
          else if (j->find("{10,") != std::string::npos)
            printf(" Dolby found {10,\n");
          else if (j->find("{11,") != std::string::npos)
            printf(" DTS_HD found {11,\n");
          else if (j->find("{12,") != std::string::npos)
            printf(" MAT found {12,\n");
          else if (j->find("{13,") != std::string::npos)
            printf(" ATRAC found {13,\n");
          else if (j->find("{14,") != std::string::npos)
            printf(" WMA found {14,\n");
          else
            break;
        }
        break;
      }
    }
  }
}

int aml_axis_value(AML_DISPLAY_AXIS_PARAM param)
{
  std::string axis;
  int value[8];

  CSysfsPath display_axis{"/sys/class/display/axis"};
  if (display_axis.Exists())
    axis = display_axis.Get<std::string>().value();

  sscanf(axis.c_str(), "%d %d %d %d %d %d %d %d", &value[0], &value[1], &value[2], &value[3], &value[4], &value[5], &value[6], &value[7]);

  return value[param];
}

bool aml_mode_to_resolution(const char *mode, RESOLUTION_INFO *res)
{
  if (!res)
    return false;

  res->iWidth = 0;
  res->iHeight= 0;

  if(!mode)
    return false;

  const bool nativeGui = CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING);
  std::string fromMode = mode;
  StringUtils::Trim(fromMode);
  // strips, for example, 720p* to 720p
  // the * indicate the 'native' mode of the display
  if (StringUtils::EndsWith(fromMode, "*"))
    fromMode.erase(fromMode.size() - 1);

  if (StringUtils::EqualsNoCase(fromMode, "panel"))
  {
    res->iWidth = aml_axis_value(AML_DISPLAY_AXIS_PARAM_WIDTH);
    res->iHeight= aml_axis_value(AML_DISPLAY_AXIS_PARAM_HEIGHT);
    res->iScreenWidth = aml_axis_value(AML_DISPLAY_AXIS_PARAM_WIDTH);
    res->iScreenHeight= aml_axis_value(AML_DISPLAY_AXIS_PARAM_HEIGHT);
    res->fRefreshRate = 60;
    res->dwFlags = D3DPRESENTFLAG_PROGRESSIVE;
  }
  else if (StringUtils::EqualsNoCase(fromMode, "4k2ksmpte") || StringUtils::EqualsNoCase(fromMode, "smpte24hz"))
  {
    res->iWidth = nativeGui ? 4096 : 1920;
    res->iHeight= nativeGui ? 2160 : 1080;
    res->iScreenWidth = 4096;
    res->iScreenHeight= 2160;
    res->fRefreshRate = 24;
    res->dwFlags = D3DPRESENTFLAG_PROGRESSIVE;
  }
  else
  {
    int width = 0, height = 0, rrate = 60;
    char smode[2] = { 0 };

    if (sscanf(fromMode.c_str(), "%dx%dp%dhz", &width, &height, &rrate) == 3)
    {
      *smode = 'p';
    }
    else if (sscanf(fromMode.c_str(), "%d%1[ip]%dhz", &height, smode, &rrate) >= 2)
    {
      switch (height)
      {
        case 480:
        case 576:
          width = 720;
          break;
        case 720:
          width = 1280;
          break;
        case 1080:
          width = 1920;
          break;
        case 2160:
          width = 3840;
          break;
      }
    }
    else if (sscanf(fromMode.c_str(), "%dcvbs", &height) == 1)
    {
      width = 720;
      *smode = 'i';
      rrate = (height == 576) ? 50 : 60;
    }
    else if (sscanf(fromMode.c_str(), "4k2k%d", &rrate) == 1)
    {
      width = 3840;
      height = 2160;
      *smode = 'p';
    }
    else
    {
      return false;
    }

    res->iWidth = nativeGui ? width : std::min(width, 1920);
    res->iHeight= nativeGui ? height : std::min(height, 1080);
    res->iScreenWidth = width;
    res->iScreenHeight = height;
    res->dwFlags = (*smode == 'p') ? D3DPRESENTFLAG_PROGRESSIVE : D3DPRESENTFLAG_INTERLACED;

    switch (rrate)
    {
      case 23:
      case 29:
      case 59:
        res->fRefreshRate = (float)((rrate + 1)/1.001f);
        break;
      default:
        res->fRefreshRate = (float)rrate;
        break;
    }
  }

  res->bFullScreen   = true;
  res->iSubtitles    = (int)(0.965 * res->iHeight);
  res->fPixelRatio   = 1.0f;
  res->strId         = fromMode;
  res->strMode       = StringUtils::Format("{:d}x{:d} @ {:.2f}{} - Full Screen", res->iScreenWidth, res->iScreenHeight, res->fRefreshRate,
    res->dwFlags & D3DPRESENTFLAG_INTERLACED ? "i" : "");

  if (fromMode.find("FramePacking") != std::string::npos)
  {
    res->iBlanking = res->iScreenHeight == 1080 ? 45 : 30;
    res->dwFlags |= D3DPRESENTFLAG_MODE3DFP;
  }

  if (fromMode.find("TopBottom") != std::string::npos)
    res->dwFlags |= D3DPRESENTFLAG_MODE3DTB;

  if (fromMode.find("SidebySide") != std::string::npos)
    res->dwFlags |= D3DPRESENTFLAG_MODE3DSBS;

  return res->iWidth > 0 && res->iHeight> 0;
}

bool aml_get_native_resolution(RESOLUTION_INFO *res)
{
  std::string mode;
  CSysfsPath display_mode{"/sys/class/display/mode"};
  if (display_mode.Exists())
    mode = display_mode.Get<std::string>().value();
  bool result = aml_mode_to_resolution(mode.c_str(), res);

  if (aml_has_frac_rate_policy())
  {
    int fractional_rate = 0;
    CSysfsPath frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    if (frac_rate_policy.Exists())
      fractional_rate = frac_rate_policy.Get<int>().value();
    if (fractional_rate == 1)
      res->fRefreshRate /= 1.001f;
  }

  return result;
}

bool aml_set_native_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name,
  const int stereo_mode, bool force_mode_switch)
{
  bool result = false;

  aml_handle_display_stereo_mode(stereo_mode);
  result = aml_set_display_resolution(res, framebuffer_name, force_mode_switch);
  if (stereo_mode != RENDER_STEREO_MODE_OFF)
    CSysfsPath("/sys/class/amhdmitx/amhdmitx0/phy", 1);


  aml_handle_scale(res);

  return result;
}

bool aml_probe_resolutions(std::vector<RESOLUTION_INFO> &resolutions)
{
  std::string valstr, addstr;

  CSysfsPath user_dcapfile{CSpecialProtocol::TranslatePath("special://home/userdata/disp_cap")};

  if (!user_dcapfile.Exists())
  {
    CSysfsPath dcapfile{"/sys/class/amhdmitx/amhdmitx0/disp_cap"};
    if (dcapfile.Exists())
      valstr = dcapfile.Get<std::string>().value();
    else
      return false;

    CSysfsPath vesa{"/flash/vesa.enable"};
    if (vesa.Exists())
    {
      CSysfsPath vesa_cap{"/sys/class/amhdmitx/amhdmitx0/vesa_cap"};
      if (vesa_cap.Exists())
      {
        addstr = vesa_cap.Get<std::string>().value();
        valstr += "\n" + addstr;
      }
    }

    CSysfsPath custom_mode{"/sys/class/amhdmitx/amhdmitx0/custom_mode"};
    if (custom_mode.Exists())
    {
      addstr = custom_mode.Get<std::string>().value();
      valstr += "\n" + addstr;
    }

    CSysfsPath user_daddfile{CSpecialProtocol::TranslatePath("special://home/userdata/disp_add")};
    if (user_daddfile.Exists())
    {
      addstr = user_daddfile.Get<std::string>().value();
      valstr += "\n" + addstr;
    }
  }
  else
    valstr = user_dcapfile.Get<std::string>().value();

  if (aml_display_support_3d())
  {
    CSysfsPath user_dcapfile_3d{CSpecialProtocol::TranslatePath("special://home/userdata/disp_cap_3d")};
    if (!user_dcapfile_3d.Exists())
    {
      CSysfsPath dcapfile3d{"/sys/class/amhdmitx/amhdmitx0/disp_cap_3d"};
      if (dcapfile3d.Exists())
      {
        addstr = dcapfile3d.Get<std::string>().value();
        valstr += "\n" + addstr;
      }
    }
    else
      valstr = user_dcapfile_3d.Get<std::string>().value();
  }


  std::vector<std::string> probe_str = StringUtils::Split(valstr, "\n");

  resolutions.clear();
  RESOLUTION_INFO res;
  for (std::vector<std::string>::const_iterator i = probe_str.begin(); i != probe_str.end(); ++i)
  {
    if (((StringUtils::StartsWith(i->c_str(), "4k2k")) && (aml_support_h264_4k2k() > AML_NO_H264_4K2K)) || !(StringUtils::StartsWith(i->c_str(), "4k2k")))
    {
      if (aml_mode_to_resolution(i->c_str(), &res))
        resolutions.push_back(res);

      if (aml_has_frac_rate_policy())
      {
        // Add fractional frame rates: 23.976, 29.97 and 59.94 Hz
        switch ((int)res.fRefreshRate)
        {
          case 24:
          case 30:
          case 60:
            res.fRefreshRate /= 1.001f;
            res.strMode       = StringUtils::Format("{:d}x{:d} @ {:.2f}{} - Full Screen", res.iScreenWidth, res.iScreenHeight, res.fRefreshRate,
              res.dwFlags & D3DPRESENTFLAG_INTERLACED ? "i" : "");
            resolutions.push_back(res);
            break;
        }
      }
    }
  }
  return resolutions.size() > 0;
}

bool aml_set_display_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name,
  bool force_mode_switch)
{
  std::string mode = res.strId.c_str();
  std::string cur_mode;
  std::string custom_mode;
  std::vector<std::string> _mode = StringUtils::Split(mode, ' ');
  std::string mode_options;

  if (_mode.size() > 1)
  {
    mode = _mode[0];
    unsigned int i = 1;
    while(i < (_mode.size() - 1))
    {
      if (i > 1)
        mode_options.append(" ");
      mode_options.append(_mode[i]);
      i++;
    }
    CLog::Log(LOGDEBUG, "{}: try to set mode: {} ({})", __FUNCTION__, mode.c_str(), mode_options.c_str());
  }
  else
    CLog::Log(LOGDEBUG, "{}: try to set mode: {}", __FUNCTION__, mode.c_str());

  CSysfsPath display_mode{"/sys/class/display/mode"};
  if (display_mode.Exists())
    cur_mode = display_mode.Get<std::string>().value();

  CSysfsPath amhdmitx0_custom_mode{"/sys/class/amhdmitx/amhdmitx0/custom_mode"};
  if (amhdmitx0_custom_mode.Exists())
    custom_mode = amhdmitx0_custom_mode.Get<std::string>().value();

  if (custom_mode == mode)
  {
    mode = "custombuilt";
  }

  if (aml_has_frac_rate_policy())
  {
    int cur_fractional_rate;
    int fractional_rate = (res.fRefreshRate == floor(res.fRefreshRate)) ? 0 : 1;
    CSysfsPath amhdmitx0_frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    if (amhdmitx0_frac_rate_policy.Exists())
      cur_fractional_rate = amhdmitx0_frac_rate_policy.Get<int>().value();

    if ((cur_fractional_rate != fractional_rate) || force_mode_switch)
    {
      cur_mode = "null";
      if (display_mode.Exists())
        display_mode.Set(cur_mode);
      if (amhdmitx0_frac_rate_policy.Exists())
        amhdmitx0_frac_rate_policy.Set(fractional_rate);
    }
  }

  if (cur_mode != mode)
  {
    if (display_mode.Exists())
      display_mode.Set(mode);
  }

  aml_set_framebuffer_resolution(res, framebuffer_name);

  return true;
}

void aml_handle_scale(const RESOLUTION_INFO &res)
{
  if (res.iScreenWidth > res.iWidth && res.iScreenHeight > res.iHeight)
    aml_enable_freeScale(res);
  else
    aml_disable_freeScale();
}

void aml_handle_display_stereo_mode(const int stereo_mode)
{
  static int kernel_stereo_mode = -1;

  if (kernel_stereo_mode == -1)
  {
    CSysfsPath _kernel_stereo_mode{"/sys/class/amhdmitx/amhdmitx0/stereo_mode"};
    if (_kernel_stereo_mode.Exists())
      kernel_stereo_mode = _kernel_stereo_mode.Get<int>().value();
  }

  if (kernel_stereo_mode != stereo_mode)
  {
    std::string command = "3doff";
    switch (stereo_mode)
    {
      case RENDER_STEREO_MODE_SPLIT_VERTICAL:
        command = "3dlr";
        break;
      case RENDER_STEREO_MODE_SPLIT_HORIZONTAL:
        command = "3dtb";
        break;
      case RENDER_STEREO_MODE_HARDWAREBASED:
        command = "3dfp";
        break;
      default:
        // nothing - command is already initialised to "3doff"
        break;
    }

    CLog::Log(LOGDEBUG, "AMLUtils::{} setting new mode: {}", __FUNCTION__, command);
    CSysfsPath("/sys/class/amhdmitx/amhdmitx0/config", command);
    kernel_stereo_mode = stereo_mode;
  }
}

void aml_enable_freeScale(const RESOLUTION_INFO &res)
{
  char fsaxis_str[256] = {0};
  sprintf(fsaxis_str, "0 0 %d %d", res.iWidth-1, res.iHeight-1);
  char waxis_str[256] = {0};
  sprintf(waxis_str, "0 0 %d %d", res.iScreenWidth-1, res.iScreenHeight-1);

  CSysfsPath("/sys/class/graphics/fb0/free_scale", 0);
  CSysfsPath("/sys/class/graphics/fb0/free_scale_axis", fsaxis_str);
  CSysfsPath("/sys/class/graphics/fb0/window_axis", waxis_str);
  CSysfsPath("/sys/class/graphics/fb0/free_scale", 0x10001);
}

void aml_disable_freeScale()
{
  // turn off frame buffer freescale
  CSysfsPath("/sys/class/graphics/fb0/free_scale", 0);
  CSysfsPath("/sys/class/graphics/fb1/free_scale", 0);
}

void aml_set_framebuffer_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name)
{
  aml_set_framebuffer_resolution(res.iWidth, res.iHeight, framebuffer_name);
}

void aml_set_framebuffer_resolution(unsigned int width, unsigned int height, std::string framebuffer_name)
{
  int fd0;
  std::string framebuffer = "/dev/" + framebuffer_name;

  if ((fd0 = open(framebuffer.c_str(), O_RDWR)) >= 0)
  {
    struct fb_var_screeninfo vinfo;
    if (ioctl(fd0, FBIOGET_VSCREENINFO, &vinfo) == 0)
    {
      if (width != vinfo.xres || height != vinfo.yres)
      {
        vinfo.xres = width;
        vinfo.yres = height;
        vinfo.xres_virtual = width;
        vinfo.yres_virtual = height * 2;
        vinfo.bits_per_pixel = 32;
        vinfo.activate = FB_ACTIVATE_ALL;
        ioctl(fd0, FBIOPUT_VSCREENINFO, &vinfo);
      }
    }
    close(fd0);
  }
}

bool aml_read_reg(const std::string &reg, uint32_t &reg_val)
{
  CSysfsPath paddr{"/sys/kernel/debug/aml_reg/paddr"};
  if (paddr.Exists())
  {
    paddr.Set(reg);
    std::string val = paddr.Get<std::string>().value();

    CRegExp regexp;
    regexp.RegComp("\\[0x(?<reg>.+)\\][\\s]+=[\\s]+(?<val>.+)");
    if (regexp.RegFind(val) == 0)
    {
      std::string match;
      if (regexp.GetNamedSubPattern("reg", match))
      {
        if (match == reg)
        {
          if (regexp.GetNamedSubPattern("val", match))
          {
            try
            {
              reg_val = std::stoul(match, 0, 16);
              return true;
            }
            catch (...) {}
          }
        }
      }
    }
  }
  return false;
}

bool aml_has_capability_ignore_alpha()
{
  // 4.9 seg faults on access to /sys/kernel/debug/aml_reg/paddr and since we are CE it's always AML
  return true;
}

bool aml_set_reg_ignore_alpha()
{
  if (aml_has_capability_ignore_alpha())
  {
    CSysfsPath fb0_debug{"/sys/class/graphics/fb0/debug"};
    if (fb0_debug.Exists())
    {
      fb0_debug.Set("write 0x1a2d 0x7fc0");
      return true;
    }
  }
  return false;
}

bool aml_unset_reg_ignore_alpha()
{
  if (aml_has_capability_ignore_alpha())
  {
    CSysfsPath fb0_debug{"/sys/class/graphics/fb0/debug"};
    if (fb0_debug.Exists())
    {
      fb0_debug.Set("write 0x1a2d 0x3fc0");
      return true;
    }
  }
  return false;
}

struct FpsData {
  unsigned int input_fps;
  unsigned int output_fps;
  std::chrono::steady_clock::time_point timestamp;
};

struct FpsInfo {
  unsigned int avg_input_fps;
  unsigned int avg_output_fps;
  unsigned int avg_drop_fps;
};

struct FormattedFpsInfo {
  std::string basic_info;
  std::string drop_info;
};

FpsInfo gather_fps_data() {

  static std::vector<FpsData> fps_history;
  static const std::chrono::seconds HISTORY_DURATION(1);

  CSysfsPath fps_info{"/sys/class/video/fps_info"};
  if (fps_info.Exists()) {

    std::string input = fps_info.Get<std::string>().value();
    unsigned int input_fps, output_fps;
    std::istringstream iss(input);

    if ((iss.ignore(std::numeric_limits<std::streamsize>::max(), ':') && iss >> std::hex >> input_fps) &&
        (iss.ignore(std::numeric_limits<std::streamsize>::max(), ':') && iss >> std::hex >> output_fps)) {
        
      // Add new entry
      auto now = std::chrono::steady_clock::now();
      fps_history.push_back({input_fps, output_fps, now});

      // Remove old entries
      fps_history.erase(
        std::remove_if(
            fps_history.begin(), fps_history.end(), 
            [&now](const FpsData& data) {
              return (now - data.timestamp) > HISTORY_DURATION;
            }
          ), fps_history.end()
      );

      // Calculate averages
      double avg_input_fps = 0;
      double avg_output_fps = 0;
      double avg_drop_fps = 0;

      unsigned int valid_count = 0;

      for (const auto& data : fps_history) {
        avg_input_fps += data.input_fps;
        avg_output_fps += data.output_fps;
        valid_count++;
      }

      if (valid_count > 0) {
        avg_input_fps /= valid_count;
        avg_output_fps /= valid_count;
        avg_drop_fps = avg_input_fps - avg_output_fps;

        return {
          static_cast<unsigned int>(avg_input_fps + 0.5),
          static_cast<unsigned int>(avg_output_fps + 0.5),
          static_cast<unsigned int>(avg_drop_fps + 0.5)
        };
      }
    }
  }

  return {0, 0, 0};
}

FormattedFpsInfo format_fps_info() {

  FpsInfo info = gather_fps_data();

  // Format basic info
  static int rotation_index = 0;
  const char rotation_chars[] = {'|', '/', '-', '\\'};

  static std::chrono::steady_clock::time_point last_update = std::chrono::steady_clock::now();
  const std::chrono::milliseconds UPDATE_INTERVAL(100);

  std::ostringstream basic_info;
  basic_info << std::fixed << std::setprecision(0) << std::setfill('0')
              << std::setw(3) << info.avg_input_fps << " - "
              << std::setw(3) << info.avg_output_fps << " - "
              << std::setw(3) << info.avg_drop_fps;

  auto now = std::chrono::steady_clock::now();
  if ((now - last_update) >= UPDATE_INTERVAL) {
    rotation_index = (rotation_index + 1) % 4;
    last_update = now;
  }

  basic_info << " " << rotation_chars[rotation_index];

  // Format drop info
  static unsigned int lowest_avg_output_fps = 0;
  static std::chrono::steady_clock::time_point last_drop_time;
  const std::chrono::seconds HOLD_PERIOD(3);
  static std::string drop_info = "";

  if (info.avg_output_fps < info.avg_input_fps) {
      if (lowest_avg_output_fps == 0 || info.avg_output_fps < lowest_avg_output_fps) {
          lowest_avg_output_fps = info.avg_output_fps;
          last_drop_time = now;
      } else if (now - last_drop_time >= HOLD_PERIOD) {
          lowest_avg_output_fps = info.avg_output_fps;
          last_drop_time = now;
      }
      drop_info = std::to_string(lowest_avg_output_fps);
  } else {
      if (lowest_avg_output_fps != 0 && now - last_drop_time >= HOLD_PERIOD) {
          lowest_avg_output_fps = 0;
          drop_info = "";
      }
  }

  return {basic_info.str(), drop_info};
}

std::string aml_video_fps_info() {
  return format_fps_info().basic_info;
}

std::string aml_video_fps_drop() {
  return format_fps_info().drop_info;
}

unsigned int aml_dv_video_processor_mode()
{
  CSysfsPath dv_vp{"/sys/module/amdolby_vision/parameters/xbmc_dv_vp"};
  if (dv_vp.Exists())
    return dv_vp.Get<unsigned int>().value_or(0);
  return 0;
}

void aml_toogle_video_freerun_mode() 
{
  CSysfsPath freerun_mode{"/sys/class/video/freerun_mode"};
  if (freerun_mode.Exists()) {
    freerun_mode.Set(0);
    // Schedule back to 1 in 1 sec.
    CServiceBroker::GetJobManager()->Submit([freerun_mode]() mutable {
      usleep(1000 * 1000);
      freerun_mode.Set(1);
    });
  }
}

void aml_dv_hdr10plus_conversion (bool hdr10plus_conversion) {
  // Kernel-side CMv4.0 injection via xbmc_dv_hdr10plus_conv is no longer
  // needed: the RPU writer includes L3/L9/L11/L254 directly, matching
  // avdvplus R9 which also does not use this flag.
}

void aml_reset_audio_from_player_open()
{
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
  if (aml_get_cpufamily_id() == AML_G12B)
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(2);
  else
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(3);
}

void aml_reset_audio_from_player_pause()
{
  if (aml_get_cpufamily_id() == AML_G12B)
  {
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(1);
  }
}

void aml_reset_audio_from_window_home()
{
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(1);
}

void aml_reset_audio_from_play_from_beginning()
{
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
  if (aml_get_cpufamily_id() == AML_G12B)
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(2);
  else
    CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(3);
}

void aml_reset_audio_from_play_from_resume()
{
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSync(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetResetSeek(true);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetLastResetTime(0.0);
  CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->SetAlgoForReset(1);
}

// CD/CS (Color Depth/Color Space) management for DV/HDR10+ playback (avdvplus R6)
// Type 1: DV processing setup - force YUV422 and adjust CS/CD limits
// Type 2: HDR10+ processing - limit color depth/space
void aml_kodi_set_cd_cs(int cd_cs_type)
{
  auto advSettings = CServiceBroker::GetSettingsComponent()->GetAdvancedSettings();

  switch (cd_cs_type)
  {
    case 1: // DV video processor setup
    {
      enum DV_TYPE dv_type = static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE));
      unsigned int dv_vp(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR));

      // For player-led modes or VP override, force YUV422 and set CS/CD limits
      if ((dv_vp == 2) || (dv_vp == 3) ||
          ((dv_vp == 0) && (dv_type == DV_TYPE_PLAYER_LED_HDR2)) ||
          ((dv_vp == 0) && (dv_type == DV_TYPE_PLAYER_LED_LLDV)))
      {
        if (!advSettings->GetForceCS())
        {
          // Save current values before changing
          advSettings->SetForceCS(true);
          advSettings->SetForceCSPrevVal(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS));
          advSettings->SetLimitCDPrevVal(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD));
        }
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS, 3); // 422
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD, 3); // 16bit (no limit)
        const RESOLUTION_INFO res_info = CDisplaySettings::GetInstance().GetResolutionInfo(CDisplaySettings::GetInstance().GetCurrentResolution());
        write_resolution_ini(res_info);

        // Enable kernel-side 422 forcing for lower frame rates
        if (CServiceBroker::GetDataCacheCore().GetVideoFps() < 41.0f)
          aml_linux_force_422 = true;
      }
      else
      {
        aml_linux_force_422 = false;
      }

      // Set kernel parameter for 422 forcing
      CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_aml_linux_force_422", aml_linux_force_422);
      break;
    }

    case 2: // HDR10+ processing
    {
      StreamHdrType hdrType = CServiceBroker::GetDataCacheCore().GetVideoHdrType();

      if (hdrType == StreamHdrType::HDR_TYPE_HDR10PLUS && !advSettings->GetLimitCD())
      {
        // Save current values before changing
        advSettings->SetLimitCD(true);
        advSettings->SetLimitCDPrevVal(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD));
        advSettings->SetForceCSPrevVal(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS));

        // Limit color depth and force color space for HDR10+ compatibility
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD, 3); // 16bit
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS, 3); // 422
        const RESOLUTION_INFO res_info = CDisplaySettings::GetInstance().GetResolutionInfo(CDisplaySettings::GetInstance().GetCurrentResolution());
        write_resolution_ini(res_info);
      }
      break;
    }

    default:
      break;
  }
}

void aml_kodi_reset_cd_cs()
{
  auto advSettings = CServiceBroker::GetSettingsComponent()->GetAdvancedSettings();

  // Restore previous CD/CS settings if they were changed
  if (advSettings->GetLimitCD() || advSettings->GetForceCS())
  {
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD, advSettings->GetLimitCDPrevVal());
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS, advSettings->GetForceCSPrevVal());

    advSettings->SetLimitCD(false);
    advSettings->SetForceCS(false);
    advSettings->SetLimitCDPrevVal(0);
    advSettings->SetForceCSPrevVal(0);
  }

  // Reset kernel-side 422 forcing
  aml_linux_force_422 = false;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_aml_linux_force_422", aml_linux_force_422);

  // DV_MODE_ON: IPT is the idle state, so restore it here when user toggled
  // VS10 HDR10 mapping to Bypass mid-playback.  (The Player.OnStop handler
  // would restore IPT asynchronously anyway; doing it synchronously avoids
  // a visible transient.)
  // DV_MODE_ON_DEMAND: idle state is Bypass -- the aml_dv_close() that
  // follows this reset already takes us there, so restoring to IPT here
  // would cause a pointless HDR10 -> IPT Tunnel -> Bypass cycle with a
  // 3s DV VSIF wait that never arrives (the screen corruption seen on
  // exit of HDR10 playback after DV content).
  if (CServiceBroker::GetDataCacheCore().GetVideoHdrType() == StreamHdrType::HDR_TYPE_HDR10 &&
      vs10_conversion_reset_hdr10 &&
      aml_dv_mode() == DV_MODE_ON)
  {
    aml_dv_on(DOLBY_VISION_OUTPUT_MODE_IPT);
  }
  vs10_conversion_reset_hdr10 = false;
}
