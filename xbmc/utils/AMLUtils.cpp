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
#include <atomic>
#include <mutex>
#include <thread>

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
#include "guilib/LocalizeStrings.h"
#include "dialogs/GUIDialogKaiToast.h"
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

// Last canonical /sys/class/display/mode value we wrote.
//
// aml_set_display_resolution does a "null then target" sequence (line ~2884
// writes "null" deliberately to force the kernel display driver to drop the
// current mode, then immediately writes the new target). Other code paths
// (aml_dv_off line ~1281, aml_dv_display_trigger ~1385) round-trip the
// current value through sysfs to nudge the driver — Get-then-Set the same
// string. If those round-trips happen to read during the brief intermediate
// "null" window of aml_set_display_resolution, they write "null" BACK,
// re-asserting the modeless state that aml_set_display_resolution was about
// to recover from. The display engine then sits modeless: vsync stalls,
// decoder backpressures, video frozen / audio continuing — the "vlix bug"
// (BACK→replay on same-resolution DV content). See test1 trace at 21:02:59:
// mode goes null at .162 (Kodi intermediate), recovers to 2160p24hz at .285,
// then null again at .536 — this second null is the round-trip site catching
// the brief window between .162 and .285 in a different thread.
//
// To make "stuck at null" structurally impossible: track the last-known-good
// mode that was actually written, and have a single helper that round-trips
// through sysfs but recovers via s_lastDisplayMode if the read came back as
// "null". The intermediate-null write inside aml_set_display_resolution
// itself is preserved (it's the legitimate kernel-drop step), but the value
// of s_lastDisplayMode is updated only when we write a real target — so the
// helper always has a non-null value to fall back to once the system has
// ever set a real mode.
static std::mutex s_lastDisplayModeMutex;
static std::string s_lastDisplayMode;

// Serializes ALL mutations of the DV-core sysfs state across the threads that
// touch it: the VideoPlayer codec thread (aml_dv_open/aml_dv_close), the
// announcement thread (aml_dv_start via Player.OnStop / OnWake) and the
// windowing/render thread (aml_dv_display_trigger after a resolution switch).
// Without this, a back-to-back DV->DV switch can interleave one path's
// aml_dv_off() Bypass cycle into another path's aml_dv_on() register sequence,
// leaving the core enabled with inconsistent matrices (gray/green corrupted
// output). Recursive because aml_dv_start()/aml_dv_open() legitimately nest
// aml_dv_off()/aml_dv_on(). Lock ordering is always s_dvStartMutex ->
// s_dvCoreMutex (only aml_dv_start takes both, in that order), never the
// reverse, so the two cannot deadlock.
//
// Lock-ordering rule for everything inside a CDVCoreGuard scope: NEVER call
// GUI/windowmanager APIs while holding the DV-core lock. The render thread
// takes the gfx context lock first (SetVideoResolution -> CreateNewWindow ->
// aml_dv_display_trigger -> DV-core lock); a holder of the DV-core lock that
// then needs the gfx lock (GetWindow/GetProperty) closes an ABBA cycle and
// freezes the whole UI (vlix Back->replay picture freeze, 2026-06-12).
static std::recursive_timed_mutex s_dvCoreMutex;
// Tag of the current outermost holder, for the blocked-acquire diagnostic.
// Diagnostic-only (relaxed): a racing read may see a just-released holder.
static std::atomic<const char*> s_dvCoreHolder{nullptr};

// RAII guard for s_dvCoreMutex with convoy diagnostics. A DV off/on cycle
// advances kernel-side per-vsync, so a queue of serialized DV ops can hold the
// core lock for whole frames — long enough to stall the render thread's
// SetFullScreen path behind a codec-thread close/reopen (FEL black-screen
// class). Log when acquiring had to wait on another thread (>50ms) and when a
// critical section held the lock long (>100ms, outermost acquisition only —
// nested recursive re-entries are free and would double-count).
class CDVCoreGuard
{
public:
  explicit CDVCoreGuard(const char* tag) : m_tag(tag)
  {
    const auto t0 = std::chrono::steady_clock::now();
    // Poll instead of a bare lock(): the convoy/held warnings below only fire
    // after acquire/release, which a deadlock never reaches — a lock-order
    // inversion used to freeze silently. A blocked acquire now identifies
    // itself and the holder in the log every 5s while it waits.
    while (!s_dvCoreMutex.try_lock_for(std::chrono::seconds(5)))
    {
      const auto waitS = std::chrono::duration_cast<std::chrono::seconds>(
          std::chrono::steady_clock::now() - t0).count();
      const char* holder = s_dvCoreHolder.load(std::memory_order_relaxed);
      CLog::Log(LOGERROR,
                "AMLUtils::{} - blocked {}s waiting for DV-core lock held by {} - "
                "possible lock-order deadlock",
                m_tag, waitS, holder ? holder : "<unknown>");
    }
    const auto waitMs = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t0).count();
    if (waitMs > 50)
      CLog::Log(LOGWARNING, "AMLUtils::{} - waited {}ms for DV-core lock (convoy)",
                m_tag, waitMs);
    m_prevHolder = s_dvCoreHolder.exchange(m_tag, std::memory_order_relaxed);
    m_outermost = (s_lockDepth++ == 0);
    if (m_outermost)
      m_acquired = std::chrono::steady_clock::now();
  }
  ~CDVCoreGuard()
  {
    --s_lockDepth;
    if (m_outermost)
    {
      const auto heldMs = std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::steady_clock::now() - m_acquired).count();
      if (heldMs > 100)
        CLog::Log(LOGWARNING, "AMLUtils::{} - held DV-core lock for {}ms", m_tag, heldMs);
    }
    s_dvCoreHolder.store(m_prevHolder, std::memory_order_relaxed);
    s_dvCoreMutex.unlock();
  }
  CDVCoreGuard(const CDVCoreGuard&) = delete;
  CDVCoreGuard& operator=(const CDVCoreGuard&) = delete;
private:
  static thread_local int s_lockDepth;
  const char* m_tag;
  const char* m_prevHolder{nullptr};
  bool m_outermost{false};
  std::chrono::steady_clock::time_point m_acquired{};
};
thread_local int CDVCoreGuard::s_lockDepth = 0;

// Forward-declared: defined below near aml_dv_display_trigger, called from
// aml_dv_off (above) too. Round-trips display/mode through sysfs but recovers
// to last-known-good if the read returned "null".
static void aml_display_mode_round_trip(const char* fn);

// Diagnostic: dump full DV/HDMI kernel state and our cached state to debug log.
// Forward-declared so it's callable from set_vs10_mode (defined before the
// helper's body, which sits next to aml_dv_off where all statics are in scope).
// Externally visible (declared in AMLUtils.h) so CVideoSyncAML can trigger a
// snapshot when it detects a stall on FBIO_WAITFORVSYNC_64.
void aml_dv_dump_state(const char* tag);

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
        // Timed out without the kernel consuming the toggle request. Happens
        // when the consume path in amdolby_vision.c:7256/7325 can't run (no
        // frames yet → new_dovi_setting.video_width/height stay 0). Leaving
        // FLAG_TOGGLE_FRAME asserted lets the stuck request bleed into
        // subsequent code paths, where the kernel keeps treating it as
        // pending — manifests as FBIO_WAITFORVSYNC_64 returning stale
        // timestamps and the testers' "audio works, picture frozen / HDMI
        // requires power-cycle" symptom. Force-clear here so the
        // user-space request doesn't dangle.
        dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~FLAG_TOGGLE_FRAME);
        CLog::Log(LOGWARNING, "AMLUtils::{} - Toggle Frame - force-cleared stuck FLAG_TOGGLE_FRAME after timeout", __FUNCTION__);
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

void aml_set_audio_ddr_urgent(bool enable)
{
  // The audio DMA (FRDDR) shares the system DMC "DEVICE" port (bit 7). Under heavy
  // 4K/Dolby Vision decode the super-urgent display reads can saturate DDR at peak
  // frames and starve the small audio FIFO for a few hundred microseconds, producing
  // a rare split-second HDMI audio dropout. Lift the DEVICE port to super-urgent so
  // audio's tiny requests are served in the top tier alongside the display — audio is
  // ~3 MB/s and cannot starve the display, which pulls hundreds of MB/s.
  CSysfsPath urgent{"/sys/class/aml_ddr/urgent"};
  if (!urgent.Exists())
    return;

  // format: "<port_bitmask_hex> <level>"; bit 7 = DEVICE port. 4 = super-urgent;
  // 0 clears bits[18:16] back to the firmware default (honour the request's own
  // urgency) — i.e. the original un-prioritised state. NOT 1, which would force
  // non-urgent and never return the register to its native 0.
  urgent.Set(std::string(enable ? "80 4" : "80 0"));
}

void aml_dv_set_vs10_mode(unsigned int mode, StreamHdrType hdrType)
{
  aml_dv_dump_state("vs10_change/pre");
  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  if (dv_type == DV_TYPE_VS10_ONLY) return;

  // VS10 conversion is a hardware video-layer (VD1) feature: the DV core maps the
  // YUV that the amcodec decoder places on VD1. Software-decoded streams (codecs
  // the AML decoder declines, e.g. small MPEG-4/XVID) are composited by GLES
  // straight into the GUI/EGL plane and never reach VD1. Engaging the DV core
  // (IPT/HDR10/SDR10 tunnel) for them makes the sink de-tunnel plain SDR pixels as
  // Dolby Vision -> magenta/yellow chroma corruption. Refuse the conversion modes
  // for SW decode; Bypass (turn DV off) stays allowed so the toggle can still undo.
  if (mode != DOLBY_VISION_OUTPUT_MODE_BYPASS &&
      !CServiceBroker::GetDataCacheCore().IsVideoHwDecoder())
  {
    CLog::Log(LOGINFO, "AMLUtils::{} - refusing VS10 mode [{}] for software-decoded video (no hardware video layer)",
              __FUNCTION__, aml_dv_output_mode_to_string(mode));
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Info,
                                          g_localizeStrings.Get(60344),
                                          g_localizeStrings.Get(60345));
    return;
  }

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
  aml_dv_dump_state("vs10_change/post");
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
    // INFO on purpose: blackout_policy=1 is what blanks the video plane on a
    // decoder close — the timestamp of this write vs the black-screen onset is
    // a primary diagnostic for the FEL black-with-audio reports.
    CLog::Log(LOGINFO, "AMLUtils::{} - blackout_policy {} -> {}",
              __FUNCTION__, existing_blackout, new_blackout);
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

bool aml_display_is_widescreen()
{
  bool is_widescreen = true;
  CSysfsPath edid{"/sys/class/amhdmitx/amhdmitx0/edid"};

  if (edid.Exists())
  {
    // Match on the "size(mm):" suffix only: our 4.9 kernel misspells the label
    // as "Physcial size(mm):" (hdmi_tx_edid.c, hdmitx_edid_dump), so searching
    // for the correct spelling never hits and the helper silently reports every
    // display as widescreen. The suffix matches either spelling, so this keeps
    // working if the kernel is ever corrected.
    auto valstr = edid.Get<std::string>();
    size_t pos = valstr ? valstr->find("size(mm):") : std::string::npos;
    if (pos != std::string::npos)
    {
      int width_mm = 0, height_mm = 0;
      if (sscanf(valstr->c_str() + pos, "size(mm): %d x %d", &width_mm, &height_mm) == 2 &&
          width_mm > 0 && height_mm > 0)
      {
          float ratio = static_cast<float>(width_mm) / height_mm;
          // 16:9 range (with some tolerance)
          is_widescreen = (ratio > 1.65f) ? 1 : 0;
          CLog::Log(LOGDEBUG, "AMLUtils: display {} wide screen ({}x{}mm)",
            is_widescreen ? "is" : "is not", width_mm, height_mm);
      }
    }
  }

  return is_widescreen;
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

bool aml_display_support_12bit(int force_cs)
{
  // CS-aware EDID gate for the 12-bit Deep Color toggle.  force_cs uses the
  // same indexing as SETTING_COREELEC_AMLOGIC_FORCE_CS: 0=Auto, 1=rgb,
  // 2=420, 3=422, 4=444.  When the user has an explicit chroma we verify
  // 12-bit is listed for *that* chroma — otherwise injecting "<cs>,12bit"
  // would produce a broken signal on TVs that only support 12-bit on a
  // different chroma.  When on Auto we inject "422,12bit" ourselves, so
  // require 422,12bit support; per HDMI spec a TV that supports 4:2:2 at
  // all always lists 422,12bit (kernel's dc_cap show: hdmi_tx_main.c:3502).
  CSysfsPath dc_cap{"/sys/class/amhdmitx/amhdmitx0/dc_cap"};
  if (!dc_cap.Exists())
    return false;
  std::string valstr = dc_cap.Get<std::string>().value();
  switch (force_cs)
  {
    case 1: return valstr.find("rgb,12bit") != std::string::npos;
    case 2: return valstr.find("420,12bit") != std::string::npos;
    case 3: return valstr.find("422,12bit") != std::string::npos;
    case 4: return valstr.find("444,12bit") != std::string::npos;
    default: return valstr.find("422,12bit") != std::string::npos;
  }
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

void aml_dv_apply_l5_sysfs()
{
  bool dv_level5_enabled(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5));
  bool dv_source_level_5(dv_level5_enabled && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5", dv_source_level_5);

  bool dv_source_level_5_osdst(dv_source_level_5 && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5_OSDST));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_osdst", dv_source_level_5_osdst);

  int dv_l5_subs_signal_mode = dv_source_level_5 ? settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS) : 0;
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_subt", dv_l5_subs_signal_mode > 0);

  /* xbmc_detect_active_area is the kernel-side master enable for the L5
   * substitution path. The user's auto-detect setting is one source of
   * substitution values; service.p3i.override is another. Either one
   * needs the master enable on. */
  // Auto-letterbox stands on its own — it enables the substitution master
  // regardless of the L5 setting (the detect/override sources still require it).
  bool dv_detect_active_area =
      (dv_level5_enabled &&
       (settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA) ||
        aml_dv_l5_override_active())) ||
      aml_dv_auto_letterbox_active();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detect_active_area", dv_detect_active_area);
  CLog::Log(LOGDEBUG, "AMLUtils::aml_dv_apply_l5_sysfs - l5_enabled={} src_l5={} osdst={} subt_mode={} detect={}",
            dv_level5_enabled, dv_source_level_5, dv_source_level_5_osdst,
            dv_l5_subs_signal_mode, dv_detect_active_area);
}

unsigned int aml_dv_on(unsigned int mode, bool force_hdmi)
{
  CDVCoreGuard dvlock(__FUNCTION__);
  aml_dv_apply_l5_sysfs();
  aml_dv_apply_l5_override_sysfs();
  /* No-op unless the auto-letterbox path is actually driving the override. */
  aml_dv_auto_letterbox_watch_start();

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

  // For VS10 non-IPT output (HDR10, SDR), the DV module is active but the
  // kernel HDMI TX may still have a stale DV EOTF from a previous IPT mode.
  // Tell the kernel to skip DV tunnel overrides so normal colour params apply.
  bool dv_non_ipt = (mode >= DOLBY_VISION_OUTPUT_MODE_HDR10 && mode <= DOLBY_VISION_OUTPUT_MODE_SDR8);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_non_ipt", dv_non_ipt);

  // Enable VPP 12-bit precision preservation when the "12-bit Deep Color
  // pipeline" toggle is on.  For DV tunnel modes (IPT, IPT_TUNNEL) we skip
  // the EDID 12-bit wire check: amdolby_vision.c:2570-2573 gates
  // VPU_HDMI_FMT_CTRL bit 4 off for those modes, so xbmc_dv_deep_color only
  // affects VPP internal precision (DAT_CONV + DOLBY_CTRL), never the wire.
  // DV-Std tunnels through RGB 4:4:4 8-bit regardless; DV-LL is already
  // 422,12bit on the wire — in both cases VPP 12-bit preservation is a
  // strict quality win and doesn't depend on the sink advertising wire
  // 12-bit.  For VS10 non-IPT the HDMI TX dither bit does apply, so we
  // keep the EDID 12-bit gate there to avoid dither noise on a 10-bit wire.
  bool dv_deep_color = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_PREFER_12BIT)
                    && (!dv_non_ipt
                        || aml_display_support_12bit(
                             settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS)));
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_deep_color", dv_deep_color);

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

  // Refresh the SDR brightness boost param on every DV engage regardless of
  // mode/VP — the kernel only applies it for SDR output, but writing it
  // unconditionally prevents a stale value when the SDR branch below is
  // skipped (e.g. VP enabled while playing HDR10->SDR).
  aml_dv_set_sdr_source_max_nits(aml_dv_sdr_boost_param());
  aml_dv_set_sdr_keep_ext(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_PER_FRAME_METADATA));
  aml_dv_set_target_min_lum(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_TARGET_MIN_LUM));

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

  // force_hdmi: re-run the HDMI re-assertion (toggle_frame + attr/eotf) for the
  // CURRENT output mode even when the mode is unchanged. Used by
  // aml_dv_display_trigger() after a resolution switch to re-establish the
  // HDR/VS10 signaling at the final mode — the dolby_vision_mode sysfs is NOT
  // rewritten (no real mode change), only the HDMI output is re-asserted.
  if (modeChange || force_hdmi) {
    aml_dv_toggle_frame(mode);

    // Re-trigger update resolution when mode IPT Tunnel and in Display Led (DV-Std).
    // Work around CD 12 bit issue for DV-Std shoule be CD 8 bit.
    // Wait for Dolby VSIF being output before trigging the update resolution so logic has correct input to work from.
    // The update resolution will cause the hdmi mode switch logic in the kernel to set the colour bit depth correctly in DV-Std.
    if ((mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL) && (dv_type == DV_TYPE_DISPLAY_LED))
      aml_dv_wait_dv_std_vsif_packet();

    if ((mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL) || (mode == DOLBY_VISION_OUTPUT_MODE_IPT)) {
      // Skip on a forced re-assert: we're already at the final resolution, so
      // the update is redundant — and re-triggering it from the post-switch
      // hook (aml_dv_display_trigger) would re-enter CreateNewWindow and loop.
      if (!force_hdmi)
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
      // Mirror the native path's 12-bit Deep Color logic (see
      // DisplaySettings::write_resolution_ini): toggle forces 12-bit on the
      // depth axis; injects 4:2:2 on chroma only when force_cs is Auto.
      // Rarely fires here because aml_kodi_set_cd_cs(1) for Player-LED VS10
      // already forced force_cs/limit_cd before this branch runs — covers the
      // edge cases (e.g. DV→SDR outside Player-LED) for consistency.
      int force_cs = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_FORCE_CS);
      int limit_cd = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_LIMIT_CD);
      const std::string force_cs_str[] = { "rgb", "420", "422", "444" };
      const std::string limit_cd_str[] = { "8bit", "10bit", "12bit", "16bit" };
      const bool deep_color = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_PREFER_12BIT)
                           && aml_display_support_12bit(force_cs);
      std::string fmt_attr;
      if (force_cs > 0)
        fmt_attr += force_cs_str[force_cs - 1];
      else if (deep_color)
        fmt_attr += "422";
      if (deep_color) {
        if (!fmt_attr.empty()) fmt_attr += ",";
        fmt_attr += "12bit";
      } else if (limit_cd > 0) {
        if (!fmt_attr.empty()) fmt_attr += ",";
        fmt_attr += limit_cd_str[limit_cd - 1];
      }
      if (!fmt_attr.empty()) fmt_attr += ",";
      fmt_attr += "now";
      CSysfsPath("/sys/class/amhdmitx/amhdmitx0/attr", fmt_attr);
    }
  }

  aml_dv_dump_state("dv_on/post");
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

// Snapshot DV/HDMI kernel state + our cached state to one debug line.
// Called at every state-transition site so multi-playback traces can be diffed.
void aml_dv_dump_state(const char* tag)
{
  auto rd = [](const char* path) -> std::string {
    CSysfsPath p{path};
    if (!p.Exists()) return "-";
    auto v = p.Get<std::string>();
    if (!v.has_value()) return "?";
    std::string s = std::move(v.value());
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' '))
      s.pop_back();
    return s;
  };

  CLog::Log(LOGDEBUG,
    "AMLUtils::aml_dv_dump_state [{}] "
    "k: mode={} en={} pol={} fl={} ll={} "
    "vp={} vp_tm={} type={} prof={} non_ipt={} deep_c={} f422={} "
    "hdr10_ll={} hdr10_ll_inj_n={} vsvdb_inj={} vsvdb_inj_n={} vsvdb=[{}] "
    "tvled_bt2020={} tvled_no_col={} "
    "gmax={} blend={} xosd={} subs={} attr=[{}] hdmi_cfg=[{}] | "
    "tx: hpd={} rxsense={} rhpd={} used={} disp_mode={} sink_type={} | "
    "geom: fb_win=[{}] fb_fs=[{}] fb_fs_en={} vid_axis=[{}] vid_dis={} | "
    "l5: meta5={} l5_osdst={} l5_subt={} detect={} ovr_t={} ovr_b={} ovr_l={} ovr_r={} ovr_force={} ovr_add={} | "
    "c: lastOsd={} lastSubs={} dvMode={} f422={} vs10conv={} dvActive={}",
    tag,
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_mode"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_enable"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_policy"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_flags"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_ll_policy"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_vp"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_vp_tm"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_type"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_profile"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_non_ipt"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_deep_color"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_aml_linux_force_422"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll_inject_num"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject_num"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_payload"),
    rd("/sys/module/hdmitx20/parameters/dovi_tv_led_bt2020"),
    rd("/sys/module/hdmitx20/parameters/dovi_tv_led_no_colorimetry"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max"),
    rd("/sys/module/amdolby_vision/parameters/dv_graphic_blend_test"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd"),
    rd("/sys/module/amdolby_vision/parameters/dolby_vision_subtitles"),
    rd("/sys/class/amhdmitx/amhdmitx0/attr"),
    rd("/sys/class/amhdmitx/amhdmitx0/config"),
    // HDMI TX link-state — useful for diagnosing the "audio works, video frozen"
    // class. hpd_state / rhpd_state flag sink disconnect; rxsense_state shows
    // whether the sink's R-term is sensed back (link up); hdmi_used / disp_mode
    // / sink_type catch cases where the kernel thinks HDMI isn't active anymore.
    rd("/sys/class/amhdmitx/amhdmitx0/hpd_state"),
    rd("/sys/class/amhdmitx/amhdmitx0/rxsense_state"),
    rd("/sys/class/amhdmitx/amhdmitx0/rhpd_state"),
    rd("/sys/class/amhdmitx/amhdmitx0/hdmi_used"),
    rd("/sys/class/amhdmitx/amhdmitx0/disp_mode"),
    rd("/sys/class/amhdmitx/amhdmitx0/sink_type"),
    rd("/sys/class/graphics/fb0/window_axis"),
    rd("/sys/class/graphics/fb0/free_scale_axis"),
    rd("/sys/class/graphics/fb0/free_scale"),
    rd("/sys/class/video/axis"),
    rd("/sys/class/video/disable_video"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_osdst"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_meta_level_5_subt"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_detect_active_area"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_override_l5_top"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_override_l5_bottom"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_override_l5_left"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_override_l5_right"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_force_l5_override"),
    rd("/sys/module/amdolby_vision/parameters/xbmc_l5_override_additive"),
    s_lastOsd, s_lastSubtitles, s_dvModeCached,
    aml_linux_force_422 ? 1 : 0, vs10_conversion ? 1 : 0,
    s_dvPlaybackActive ? 1 : 0);
}

void aml_hdmi_link_probe(const char* ctx)
{
  // Runs from the GUI present hook (every frame, in menus and playback alike),
  // so keep it cheap: throttle to ~1Hz and bail before touching sysfs.
  static std::mutex s_probeMutex;
  std::lock_guard<std::mutex> lock(s_probeMutex);

  static std::chrono::steady_clock::time_point s_lastCheck{};
  auto now = std::chrono::steady_clock::now();
  if (s_lastCheck.time_since_epoch().count() != 0 &&
      now - s_lastCheck < std::chrono::seconds(1))
    return;
  s_lastCheck = now;

  auto rd = [](const char* path) -> std::string {
    CSysfsPath p{path};
    if (!p.Exists()) return "-";
    auto v = p.Get<std::string>();
    if (!v.has_value()) return "?";
    std::string s = std::move(v.value());
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' '))
      s.pop_back();
    return s;
  };

  const std::string hpd = rd("/sys/class/amhdmitx/amhdmitx0/hpd_state");
  const std::string rxsense = rd("/sys/class/amhdmitx/amhdmitx0/rxsense_state");
  const std::string rhpd = rd("/sys/class/amhdmitx/amhdmitx0/rhpd_state");
  const std::string used = rd("/sys/class/amhdmitx/amhdmitx0/hdmi_used");
  const std::string disp_mode = rd("/sys/class/amhdmitx/amhdmitx0/disp_mode");
  const std::string sink_type = rd("/sys/class/amhdmitx/amhdmitx0/sink_type");

  std::string state = StringUtils::Format(
    "hpd={} rxsense={} rhpd={} used={} disp_mode={} sink_type={}",
    hpd, rxsense, rhpd, used, disp_mode, sink_type);

  // Only log when something actually changes — quiet on a stable link.
  static std::string s_lastState;
  static bool s_haveBaseline = false;
  if (s_haveBaseline && state == s_lastState)
    return;

  // "Degraded" = the sink stopped asserting hot-plug / R-term, or the kernel
  // lost the active display mode: the signature of a blue/no-signal sink.
  const bool degraded =
      hpd == "0" || rhpd == "0" || rxsense == "0" ||
      disp_mode.empty() || disp_mode == "null" || disp_mode == "-" || disp_mode == "?";

  CLog::Log(degraded ? LOGWARNING : LOGINFO,
    "AMLUtils::aml_hdmi_link_probe [{}] {}{}",
    ctx, s_haveBaseline ? "link changed -> " : "baseline ", state);

  s_lastState = std::move(state);
  s_haveBaseline = true;
}

void aml_dv_off(bool skip_hdmi_update)
{
  CDVCoreGuard dvlock(__FUNCTION__);
  aml_dv_detect_active_area_stop();
  aml_dv_auto_letterbox_watch_stop();

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
    // FLAG_TOGGLE_FRAME (0x80000000) is a pending-toggle request the kernel
    // sets via dolby_vision_set_toggle_flag(1) and clears once the toggle is
    // consumed in the per-frame processing loop (amdolby_vision.c:7256/7325).
    // If new_dovi_setting.video_width/height stay 0 because frames never
    // started flowing during the prior DV session (typical of stop-then-
    // re-play with HDR10+→DV / VS10 conversions), the consume path never
    // fires and the flag survives across dv_off into the next dv_on. That
    // stuck flag then blocks the next playback's frame-toggle, manifests
    // as FBIO_WAITFORVSYNC_64 returning stale timestamps, and produces the
    // "audio works, picture frozen / HDMI requires power-cycle" symptom
    // reported by multiple testers (see kodi.log signature: fl=2147500037
    // and fl=2147483653 = 0x80000005). Force-clear here so each playback
    // starts with a clean state machine.
    dolby_vision_flags.Set(dolby_vision_flags.Get<unsigned int>().value() & ~FLAG_TOGGLE_FRAME);
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
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_deep_color", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vp_tm", 0);
  // Clear DV-LL HDR10/VSVDB InfoFrame injection flags. aml_dv_on sets these
  // for Player-LED HDR/HDR2 output; without unwinding them, the kernel keeps
  // these module-level enables hot after DV is otherwise off (verified via
  // dv_dump_state across the Ted→Novocaine live-swap repro). Does not by
  // itself flush the HDMI TX's own emit-state, but unblocks that as a next
  // step and removes one source of contradictory signaling.
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll", 'N');
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_for_dv_ll_inject_num", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject", false);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_inject_num", 0);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_vsvdb_payload", std::string{});
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
    aml_display_mode_round_trip(__FUNCTION__);
  }

  aml_dv_dump_state("dv_off/post");
}

unsigned int aml_dv_dolby_vision_mode()
{
  CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  return dolby_vision_mode.Get<unsigned int>().value();
}

void aml_dv_open(StreamHdrType hdrType, unsigned int bitDepth, AVColorPrimaries colorPrimaries, bool swDecoded)
{
  // Detect PM4K once at playback start for OSD visibility override.
  // MUST stay above the CDVCoreGuard: GetWindow()/GetProperty() take the gfx
  // context lock / window critsec, which the main thread holds while it takes
  // the DV-core lock (SetVideoResolution -> CreateNewWindow ->
  // aml_dv_display_trigger). Doing this under the guard is an ABBA deadlock —
  // the vlix Back->replay picture freeze.
  CGUIWindow* pm4kHome = CServiceBroker::GetGUI()->GetWindowManager().GetWindow(WINDOW_HOME);
  const bool pm4kActive =
      pm4kHome && !pm4kHome->GetProperty("script.plex.is_active").asString().empty();

  CDVCoreGuard dvlock(__FUNCTION__);
  aml_dv_dump_state("dv_open/pre");
  s_dvPlaybackActive = true;
  s_pm4kHome = pm4kHome;
  s_pm4kActive = pm4kActive;

  enum DV_MODE dv_mode(aml_dv_mode());
  CLog::Log(LOGINFO, "AMLUtils::{} - Checking DV for DV mode: [{}], DV type: [{}]", __FUNCTION__, aml_dv_mode_to_string(dv_mode), aml_dv_type_to_string(aml_dv_type()));
  if (dv_mode == DV_MODE_ON || dv_mode == DV_MODE_ON_DEMAND) {

    // Software-decoded content has no AML hardware video layer (VD1): SW frames are
    // composited by GLES into the GUI plane and never reach VD1, which is what the DV
    // core converts. Engaging any VS10 conversion (IPT/HDR10/SDR10) would make the sink
    // de-tunnel plain SDR pixels as Dolby Vision -> magenta/yellow corruption. Force
    // Bypass (DV off) regardless of the VS10 SDR/HDR mapping setting. swDecoded comes
    // straight from the call site (CLinuxRendererGLES = SW/GLES path; AMLCodec = VD1),
    // so it is authoritative even when a background HW title leaves the global decoder
    // flag set. The live VS10 toggle is guarded separately in aml_dv_set_vs10_mode().
    if (swDecoded)
    {
      CLog::Log(LOGINFO, "AMLUtils::{} - software-decoded video, forcing VS10 Bypass (no hardware video layer)", __FUNCTION__);
      if (aml_is_dv_enable())
        aml_dv_off();
      aml_dv_dump_state("dv_open/post(sw_decode_bypass)");
      return;
    }

    // SDR BT.2020 content: bypass VS10 — the DV library assumes SDR is BT.709
    // and can't handle BT.2020 gamut correctly. Bypass preserves original signaling.
    if (hdrType == StreamHdrType::HDR_TYPE_NONE && colorPrimaries == AVCOL_PRI_BT2020)
    {
      CLog::Log(LOGINFO, "AMLUtils::{} - SDR BT.2020 detected, bypassing VS10 to preserve gamut", __FUNCTION__);
      if (aml_is_dv_enable())
        aml_dv_off();
      aml_dv_dump_state("dv_open/post(sdr_bt2020_bypass)");
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
  aml_dv_dump_state("dv_open/post");
}

void aml_dv_close()
{
  CDVCoreGuard dvlock(__FUNCTION__);
  aml_dv_dump_state("dv_close/pre");
  s_dvPlaybackActive = false;
  s_pm4kActive = false;
  s_pm4kHome = nullptr;

  // DV_MODE_ON: leave DV enabled in its current output mode.  This avoids
  // costly HDMI mode-switch cycles (off->IPT->playback-mode) during live-TV
  // channel changes where a new aml_dv_open() follows immediately.
  // IPT is restored for the GUI by the Player.OnStop announcement handler
  // in CDolbyVisionAML when playback truly ends.
  if (aml_dv_mode() == DV_MODE_ON)
  {
    aml_dv_dump_state("dv_close/post(dv_mode_on_skip)");
    return;
  }

  // On Demand mode with active SDR output state:
  // Avoid shutting down the DV pipeline on playback stop when the core is
  // currently running in an SDR-ish output mode, preventing unnecessary
  // HDMI mode switches/blinks when returning to the SDR GUI.
  if (aml_dv_mode() == DV_MODE_ON_DEMAND && aml_is_dv_enable())
  {
    // Ensure this optimization is opt-in via a setting to protect VS10-only / non-DV setups
    bool keep_sdr_state = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_KEEP_SDR_STATE);

    int current_dv_mode = aml_dv_dolby_vision_mode();
    if (keep_sdr_state && (current_dv_mode == DOLBY_VISION_OUTPUT_MODE_SDR10 || current_dv_mode == DOLBY_VISION_OUTPUT_MODE_BYPASS))
    {
      aml_dv_dump_state("dv_close/post(dv_mode_on_demand_sdr_skip)");
      return;
    }
  }

  if (aml_is_dv_enable())
    aml_dv_off();
  aml_dv_dump_state("dv_close/post");
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

int aml_dv_sdr_boost_param()
{
  // Resolve the SDR brightness boost settings to the kernel param value:
  // Off -> 0, Auto -> 1 (kernel caps at the content's MaxCLL), Manual ->
  // the assumed source peak in nits from the sub-slider.
  int mode = settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_BOOST);
  if (mode == 2)
    return settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_SRC_MAX_NITS);
  return mode;
}

void aml_dv_set_sdr_keep_ext(bool keep)
{
  // VS10 DV->SDR: keep the source's L1/L2 extension metadata so the DV
  // library does per-shot dynamic display management with the colorist's
  // SDR trims, instead of the static tone curve the avdvplus R8 strip
  // leaves behind.
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_sdr_keep_ext", keep ? 1 : 0);
}

void aml_dv_set_sdr_source_max_nits(int value)
{
  // VS10 ->SDR brightness boost: cap the source's declared peak luminance
  // (DV source_max_pq / HDR10 mastering peak) kernel-side before the DV
  // library tone-maps, so it compresses less and lifts the SDR output.
  // Content brighter than the cap clips. 0 = off, 1 = auto (content
  // MaxCLL), >= 100 = manual nits. This is the working lever for dim VS10
  // SDR output - the *->SDR column of dolby_vision_target_lum_max is
  // ignored by the DV library (proven on-device, 10 vs 4000 identical).
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_dv_sdr_src_max_nits", value);
}

void aml_dv_set_target_min_lum(int value)
{
  // Target display min luminance for the VS10 tone curve, 0.0001 nit
  // units. Amlogic's stock 50 (0.005 nits) floors PQ output black at
  // 10-bit code ~77 — visible black crush below code ~80 (github #83).
  // Default 1 (0.0001 nits) keeps the floor at PQ code ~65, one step
  // above reference black.
  CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_target_min", value);
}

bool aml_is_dv_enable()
{
  CSysfsPath dolby_vision_enable{"/sys/module/amdolby_vision/parameters/dolby_vision_enable"};
  return (dolby_vision_enable.Exists() && StringUtils::EqualsNoCase(dolby_vision_enable.Get<std::string>().value(), "Y"));
}

// Round-trip /sys/class/display/mode through sysfs to nudge the kernel
// display driver (re-applying the current mode re-asserts driver state).
// CRITICAL: if the read returns "null" — we landed in the brief window
// between aml_set_display_resolution's intermediate "null" write and its
// target write — never write "null" BACK. That re-assertion is what
// locks the display engine modeless and produces the vlix-class freeze.
// Recover via s_lastDisplayMode (the last canonical target we wrote)
// instead. If we have nothing to recover to (first call before any
// target was ever written), skip the write entirely.
static void aml_display_mode_round_trip(const char* fn)
{
  CSysfsPath display_mode{"/sys/class/display/mode"};
  if (!display_mode.Exists()) return;
  const std::string cur = display_mode.Get<std::string>().value_or("");
  if (cur != "null" && !cur.empty())
  {
    display_mode.Set(cur);
    return;
  }
  std::lock_guard<std::mutex> lk(s_lastDisplayModeMutex);
  if (!s_lastDisplayMode.empty() && s_lastDisplayMode != "null")
  {
    CLog::Log(LOGWARNING,
              "AMLUtils::{} - display/mode read as '{}' during round-trip, "
              "recovering to last-known [{}] instead of re-asserting null",
              fn, cur.empty() ? "(empty)" : cur, s_lastDisplayMode);
    display_mode.Set(s_lastDisplayMode);
  }
  else
  {
    CLog::Log(LOGWARNING,
              "AMLUtils::{} - display/mode read as '{}' during round-trip, "
              "no last-known mode to recover to; skipping write",
              fn, cur.empty() ? "(empty)" : cur);
  }
}

void aml_dv_display_trigger()
{
  CDVCoreGuard dvlock(__FUNCTION__);
  if (aml_is_dv_enable()) {
    aml_display_mode_round_trip(__FUNCTION__);

    // Opt-in (Player-LED projectors): re-assert the VS10/HDR output AFTER the
    // resolution switch has landed. At playback start aml_dv_on() configures
    // the HDR10/VS10 output while still at the GUI resolution; the panel then
    // switches to the playback resolution and nothing re-applies the DV config
    // at the final mode, leaving the HDR signaling (AVI/DRM/VSIF) inconsistent.
    // Some projectors with auto dynamic-range detection then flip-flop SDR<->HDR
    // continuously. Re-asserting here reproduces what a manual VS10 mode toggle
    // does (which is the known on-device workaround). Covers the Player-LED
    // output modes that get configured at the GUI resolution: IPT (DoVi) and
    // HDR10/SDR10/SDR8 (VS10 conversion). Excludes native-DV tunnel
    // (IPT_TUNNEL = Display-LED) — a DV TV handles DV signaling robustly and
    // doesn't exhibit the projector auto-DR flip, so it neither needs nor
    // should get an extra DV re-assert. (The setting is also only visible for
    // Player-LED, so this is belt-and-suspenders for a Player-LED->Display-LED
    // switch with the value left enabled.)
    if (settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_REASSERT_AFTER_MODESWITCH)) {
      unsigned int mode = aml_dv_dolby_vision_mode();
      if (mode != DOLBY_VISION_OUTPUT_MODE_BYPASS &&
          mode != DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL)
      {
        CLog::Log(LOGDEBUG, "AMLUtils::{} - reassert-after-modeswitch: re-asserting VS10/HDR output [{}] at final resolution",
                  __FUNCTION__, aml_dv_output_mode_to_string(mode));
        aml_dv_on(mode, /*force_hdmi=*/true);
      }
    }
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

// Body of aml_dv_start(). Caller must hold BOTH s_dvStartMutex and
// s_dvCoreMutex (ordering: start -> core, never the reverse).
static void _dv_start_locked()
{
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

void aml_dv_start()
{
  std::lock_guard<std::mutex> lock(s_dvStartMutex);
  // Ordering: s_dvStartMutex -> s_dvCoreMutex (never the reverse). The nested
  // aml_dv_off()/aml_dv_on() inside re-acquire s_dvCoreMutex recursively.
  CDVCoreGuard dvlock(__FUNCTION__);
  _dv_start_locked();
}

bool aml_dv_restore_gui_ipt(const char* reason)
{
  std::lock_guard<std::mutex> lock(s_dvStartMutex);
  CDVCoreGuard dvlock(__FUNCTION__);

  // Re-validate UNDER the DV-core lock. The callers (Player.OnStop
  // announcement handler, CWinSystemAmlogicGLESContext::SetFullScreen) used
  // to evaluate these conditions unlocked and then call aml_dv_start(): a
  // concurrent aml_dv_open() winning the race between check and cycle
  // re-created the mid-playback off->Bypass->on corruption (gray/green /
  // black output) through a narrower window than the one 123ad70121 closed,
  // and a restore queued behind a codec-thread close/reopen executed stale
  // instead of no-oping. Holding the lock across check+cycle makes the
  // decision and the action atomic: a restore that lost the race to a new
  // playback sees playback-active under the lock and skips.
  if (aml_dv_mode() != DV_MODE_ON || !aml_is_dv_enable() || aml_dv_playback_active())
  {
    CLog::Log(LOGDEBUG,
              "AMLUtils::{} - [{}] skip: DV state changed while queued "
              "(mode/enable/playback-active)",
              __FUNCTION__, reason);
    return false;
  }

  unsigned int mode = aml_dv_dolby_vision_mode();
  if (mode == DOLBY_VISION_OUTPUT_MODE_IPT || mode == DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL)
    return false; // already in a GUI-suitable mode — nothing to restore

  CLog::Log(LOGINFO, "AMLUtils::{} - [{}] restoring GUI IPT (from mode {})",
            __FUNCTION__, reason, aml_dv_output_mode_to_string(mode));
  _dv_start_locked();
  return true;
}

void aml_dv_wait_for_pipeline()
{
  // Block until any in-progress aml_dv_start() completes. Called from
  // CreateNewWindow to prevent EGL surface creation while the DV pipeline
  // is transitioning through Bypass mode (causes color corruption).
  const auto t0 = std::chrono::steady_clock::now();
  std::lock_guard<std::mutex> lock(s_dvStartMutex);
  const auto waitMs = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - t0).count();
  if (waitMs > 50)
    CLog::Log(LOGWARNING,
              "AMLUtils::{} - blocked {}ms behind an in-flight DV start/restore",
              __FUNCTION__, waitMs);
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
  {
    // Codec logos are not dialogs or the video menu, so a skin flags them via
    // skin.codeclogosonscreen while they are on screen. A skin that does so also
    // sets skin.codeclogososd once (at Home load) to opt out of the blunt
    // AVChangeExtended fallback, which otherwise zeroes L5 for the whole av.change
    // window on every DV start regardless of logos. Skins that don't set it keep
    // the old behavior — backward compatible.
    CGUIWindow* home = wm.GetWindow(WINDOW_HOME);
    const bool codecLogosOnScreen =
        home && home->GetProperty("skin.codeclogosonscreen").asString() == "1";
    const bool skinHandlesOsd =
        home && home->GetProperty("skin.codeclogososd").asString() == "1";
    osd_active = wm.HasVisibleDialog() ||
                 wm.IsWindowVisible(WINDOW_VIDEO_MENU) ||
                 codecLogosOnScreen ||
                 (!skinHandlesOsd && CServiceBroker::GetDataCacheCore().GetAVChangeExtended());
  }

  int val = osd_active ? 1 : 0;
  if (val != s_lastOsd)
  {
    CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd", val);
    s_lastOsd = val;
    aml_dv_dump_state(val ? "xbmc_osd/on" : "xbmc_osd/off");
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
         settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA) &&
         !aml_dv_l5_override_active();
}

/* Parse the override setting into (active, top, bottom, left, right).
 * "active" means the user has set a value — including "0,0,0,0", which
 * is a legitimate override meaning "treat the stream as having no bars".
 * Empty / unparseable = inactive (use whatever the stream's L5 says). */
static bool _l5_override_parse(uint16_t& top, uint16_t& bottom,
                               uint16_t& left, uint16_t& right)
{
  const std::string s =
      settings()->GetString(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_OVERRIDE);
  if (s.empty()) return false;
  unsigned int t = 0, b = 0, l = 0, r = 0;
  if (std::sscanf(s.c_str(), "%u,%u,%u,%u", &t, &b, &l, &r) != 4) return false;
  if (t > 0xFFFF || b > 0xFFFF || l > 0xFFFF || r > 0xFFFF) return false;
  top    = static_cast<uint16_t>(t);
  bottom = static_cast<uint16_t>(b);
  left   = static_cast<uint16_t>(l);
  right  = static_cast<uint16_t>(r);
  return true;
}

bool aml_dv_l5_override_active()
{
  uint16_t t = 0, b = 0, l = 0, r = 0;
  return _l5_override_parse(t, b, l, r);
}

/* ---- Auto-letterbox L5 for cropped (non-16:9) content ---------------------
 * When a Dolby Vision encode has its black bars cropped off (e.g. 3840x1600),
 * the player pads it back to a 16:9 output and a DV positive-lift trim would
 * raise those added bars to grey. We synthesise the L5 active-area offsets from
 * the coded aspect (matching R9 / the TV's internal player) and push them
 * through the override channel (xbmc_override_l5_* + force) so the DV core masks
 * the bars black. Pure geometry — no file scan, the detector is never engaged. */
static std::atomic<int> s_autoLbWidth{0};
static std::atomic<int> s_autoLbHeight{0};
static std::atomic<bool> s_autoLbNativeDV{false};
/* Cleared by the plausibility watch below when the stream's source L5 turns out
 * to already account for the crop — adding our gap on top would double the bars,
 * so the override goes back to replacing rather than composing. */
static std::atomic<bool> s_autoLbAdditive{true};

void aml_dv_set_active_area_geometry(int width, int height, bool nativeDV)
{
  s_autoLbWidth.store(width);
  s_autoLbHeight.store(height);
  s_autoLbNativeDV.store(nativeDV);
  /* New stream — the previous one's verdict must not carry over. */
  s_autoLbAdditive.store(true);
}

/* Compute the synthesised L5 offsets for the current stream. Returns false (and
 * leaves outputs 0) when the auto-letterbox path should not react: not native
 * DV, the feature or L5 disabled, a manual override is set (it wins), unknown
 * geometry, or the coded frame is ~16:9 (nothing to mask). */
static bool _auto_letterbox_geometry(uint16_t& top, uint16_t& bottom,
                                     uint16_t& left, uint16_t& right)
{
  top = bottom = left = right = 0;
  if (!s_autoLbNativeDV.load())
    return false;
  /* Stands on its own — intentionally NOT gated on the L5 master setting. */
  if (!settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_L5_AUTO_LETTERBOX))
    return false;
  if (aml_dv_l5_override_active()) /* manual per-folder override wins */
    return false;

  const int w = s_autoLbWidth.load();
  const int h = s_autoLbHeight.load();
  if (w <= 0 || h <= 0)
    return false;

  /* Gap needed to reach 16:9 at the coded width/height; one pair is non-zero.
   * E.g. 3840x1600 -> refH=2160, tbGap=(2160-1600)/2=280 (matches R9). */
  const int refH = w * 9 / 16;   /* implied 16:9 height for this width */
  const int refW = h * 16 / 9;   /* implied 16:9 width for this height */
  const int tbGap = (refH > h) ? (refH - h) / 2 : 0;
  const int lrGap = (refW > w) ? (refW - w) / 2 : 0;
  if (tbGap <= 20 && lrGap <= 20)
    return false; /* ~16:9 — not cropped, nothing to do */

  top = bottom = static_cast<uint16_t>(std::min(tbGap, 0xFFFF));
  left = right = static_cast<uint16_t>(std::min(lrGap, 0xFFFF));
  return true;
}

bool aml_dv_auto_letterbox_active()
{
  uint16_t t = 0, b = 0, l = 0, r = 0;
  return _auto_letterbox_geometry(t, b, l, r);
}

bool aml_dv_auto_letterbox_get(uint16_t& top, uint16_t& bottom,
                               uint16_t& left, uint16_t& right)
{
  return _auto_letterbox_geometry(top, bottom, left, right);
}

/* ---- Auto-letterbox plausibility watch ------------------------------------
 * Additive composition assumes the two L5 channels describe *different* bars:
 * ours the player-added padding, the RPU's the per-scene bars baked into the
 * picture. Some cropped encodes break that assumption — their source L5 was
 * authored against the uncropped frame, so it already is the crop. Adding our
 * gap on top doubles it and eats real picture (3840x1600 carrying source
 * 280/280 composes to 560, leaving a 3.69:1 active area — wider than anything
 * ever shot). Watch the per-frame source L5 for a while and drop back to a
 * plain replacing override when the two channels are clearly describing the
 * same bars: the geometry gap is derived from the actual coded frame, so it
 * stays trustworthy even though the file's own L5 no longer is.
 *
 * Two tests, because neither is complete on its own:
 *   - implausible aspect: read as coded-frame offsets (what the spec says they
 *     are), the source L5 implies an active area outside the aspect band the
 *     pixel detector already trusts (s_commonAR tops out at 2.76:1). Skipped
 *     when the coded frame is itself outside that band, where the test would
 *     fire on every frame's real bars;
 *   - equality: the source offsets match the gap we synthesised. Needed for
 *     small gaps, where doubling stays plausible (3840x2024, gap 68, source 68
 *     -> 2.03:1, a perfectly real aspect).
 * A sample matching neither test resets the run, so the equality trip needs the
 * condition to hold across scene changes — variable-L5 content whose in-picture
 * bars merely coincide with the gap for one scene therefore never trips. An
 * impossible aspect acts on ~1 s, since nothing but a corrupt RPU can produce
 * one and waiting only prolongs the over-crop. The error is also
 * biased on purpose: dropping composition wrongly leaves in-picture bars
 * lift-grey (cosmetic), while composing wrongly crops picture. */
static constexpr uint32_t AUTO_LB_AR_MAX = 2900; /* x1000, matches the detector band */
static constexpr uint32_t AUTO_LB_AR_MIN = 1200;
static constexpr int AUTO_LB_POLL_MS = 500;
static constexpr int AUTO_LB_MAX_POLLS = 120; /* give up after ~60 s */
static constexpr int AUTO_LB_TRIP_SAMPLES = 6; /* ~3 s of agreement before acting */
static constexpr int AUTO_LB_IMPOSSIBLE_SAMPLES = 2; /* ~1 s when the value can't be real */

static std::atomic<bool> s_autoLbWatchCancel{false};
static std::thread s_autoLbWatchThread;
static std::mutex s_autoLbWatchMutex;

/* One sample of the current frame's source L5. Fills src[] (T,B,L,R) and reason,
 * and returns how strongly this sample says the offsets cannot be describing
 * bars inside the coded picture:
 *   AUTO_LB_SAMPLE_OK       - plausible, composition stands;
 *   AUTO_LB_SAMPLE_SUSPECT  - the offsets match our gap. Real content can do
 *                             this by coincidence, so it needs corroboration;
 *   AUTO_LB_SAMPLE_IMPOSSIBLE - the offsets describe an aspect no frame has.
 *                             Only a corrupt RPU produces this transiently. */
enum
{
  AUTO_LB_SAMPLE_OK = 0,
  AUTO_LB_SAMPLE_SUSPECT = 1,
  AUTO_LB_SAMPLE_IMPOSSIBLE = 2,
};

static int _auto_letterbox_duplicate_sample(uint16_t gapTop, uint16_t gapBottom,
                                            uint16_t gapLeft, uint16_t gapRight,
                                            int w, int h, uint16_t src[4],
                                            const char*& reason)
{
  auto meta = CServiceBroker::GetDataCacheCore().GetVideoDoViFrameMetadata();
  src[0] = meta.level5_active_area_top_offset;
  src[1] = meta.level5_active_area_bottom_offset;
  src[2] = meta.level5_active_area_left_offset;
  src[3] = meta.level5_active_area_right_offset;

  if (!meta.has_level5_metadata)
    return AUTO_LB_SAMPLE_OK;
  if (!(src[0] || src[1] || src[2] || src[3]))
    return AUTO_LB_SAMPLE_OK; /* no source bars — nothing can be doubled */

  /* Aspect implied by reading the offsets against the coded picture. The
   * padding cancels out, so this is the same number the sink would see:
   * out_h - 2*gap - top - bottom == h - top - bottom. */
  const int activeW = w - static_cast<int>(src[2]) - static_cast<int>(src[3]);
  const int activeH = h - static_cast<int>(src[0]) - static_cast<int>(src[1]);
  if (activeW <= 0 || activeH <= 0)
  {
    reason = "source L5 leaves no active area in the coded picture";
    return AUTO_LB_SAMPLE_IMPOSSIBLE;
  }

  const uint32_t impliedAR = (static_cast<uint32_t>(activeW) * 1000) / activeH;
  const uint32_t codedAR = (static_cast<uint32_t>(w) * 1000) / h;
  /* Bars only ever push the implied aspect away from the coded one, so this
   * test discriminates nothing once the coded frame is itself outside the band
   * — an extreme encode's real bars would read as "impossible" on every frame.
   * Those files are left to the equality test below. */
  if (codedAR <= AUTO_LB_AR_MAX && impliedAR > AUTO_LB_AR_MAX)
  {
    reason = "source L5 implies an impossibly wide active area";
    return AUTO_LB_SAMPLE_IMPOSSIBLE;
  }
  if (codedAR >= AUTO_LB_AR_MIN && impliedAR < AUTO_LB_AR_MIN)
  {
    reason = "source L5 implies an impossibly narrow active area";
    return AUTO_LB_SAMPLE_IMPOSSIBLE;
  }

  /* Within 2% of the gap (min 4px) counts as "the same bars". */
  auto sameAs = [](uint16_t a, uint16_t b) {
    const int tol = std::max(4, static_cast<int>(b) * 2 / 100);
    return std::abs(static_cast<int>(a) - static_cast<int>(b)) <= tol;
  };
  if ((gapTop || gapBottom) && sameAs(src[0], gapTop) && sameAs(src[1], gapBottom))
  {
    reason = "source L5 equals the synthesised top/bottom gap";
    return AUTO_LB_SAMPLE_SUSPECT;
  }
  if ((gapLeft || gapRight) && sameAs(src[2], gapLeft) && sameAs(src[3], gapRight))
  {
    reason = "source L5 equals the synthesised left/right gap";
    return AUTO_LB_SAMPLE_SUSPECT;
  }
  return AUTO_LB_SAMPLE_OK;
}

static void _auto_letterbox_watch_run(int w, int h)
{
  uint16_t gapTop = 0, gapBottom = 0, gapLeft = 0, gapRight = 0;
  if (!_auto_letterbox_geometry(gapTop, gapBottom, gapLeft, gapRight))
    return;

  int agree = 0;
  for (int poll = 0; poll < AUTO_LB_MAX_POLLS; ++poll)
  {
    for (int slice = 0; slice < AUTO_LB_POLL_MS / 100; ++slice)
    {
      if (s_autoLbWatchCancel.load())
        return;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (s_autoLbWatchCancel.load())
      return;
    /* Settings / override can change mid-playback — stop once we're not the
     * ones driving the override any more. */
    if (!aml_dv_auto_letterbox_active())
      return;

    uint16_t src[4] = {0, 0, 0, 0};
    const char* reason = nullptr;
    const int verdict = _auto_letterbox_duplicate_sample(gapTop, gapBottom,
                                                         gapLeft, gapRight,
                                                         w, h, src, reason);
    if (verdict == AUTO_LB_SAMPLE_OK)
    {
      agree = 0;
      continue;
    }
    /* An impossible aspect needs only enough samples to rule out a corrupt
     * RPU; a merely suspicious one has to survive scene changes. */
    const int needed = (verdict == AUTO_LB_SAMPLE_IMPOSSIBLE) ? AUTO_LB_IMPOSSIBLE_SAMPLES
                                                              : AUTO_LB_TRIP_SAMPLES;
    if (++agree < needed)
      continue;

    CLog::Log(LOGINFO, "AMLUtils::auto-letterbox - {} ({}x{}, source T={} B={} L={} R={} vs "
              "gap T={} B={} L={} R={}) — dropping additive composition, the geometry "
              "gap now replaces source L5",
              reason, w, h, src[0], src[1], src[2], src[3],
              gapTop, gapBottom, gapLeft, gapRight);
    s_autoLbAdditive.store(false);
    aml_dv_apply_l5_override_sysfs();
    return;
  }
}

/* Caller must hold s_autoLbWatchMutex. */
static void _auto_letterbox_watch_stop_locked()
{
  s_autoLbWatchCancel.store(true);
  if (s_autoLbWatchThread.joinable())
    s_autoLbWatchThread.join();
  s_autoLbWatchCancel.store(false);
}

void aml_dv_auto_letterbox_watch_start()
{
  std::unique_lock<std::mutex> lock(s_autoLbWatchMutex);
  _auto_letterbox_watch_stop_locked();
  if (!aml_dv_auto_letterbox_active())
    return;

  const int w = s_autoLbWidth.load();
  const int h = s_autoLbHeight.load();
  s_autoLbWatchThread = std::thread([w, h]() { _auto_letterbox_watch_run(w, h); });
}

void aml_dv_auto_letterbox_watch_stop()
{
  std::unique_lock<std::mutex> lock(s_autoLbWatchMutex);
  _auto_letterbox_watch_stop_locked();
}

bool aml_dv_auto_letterbox_additive()
{
  return s_autoLbAdditive.load();
}

void aml_dv_apply_l5_override_sysfs()
{
  uint16_t top = 0, bottom = 0, left = 0, right = 0;
  bool active = _l5_override_parse(top, bottom, left, right);

  /* No manual override set — fall back to the auto-letterbox geometry for
   * cropped content. Manual always wins (checked inside the helper too). */
  bool autoLb = false;
  if (!active)
  {
    autoLb = _auto_letterbox_geometry(top, bottom, left, right);
    active = autoLb;
  }

  // xbmc_override_l5_* is the override namespace (kernel commit 61aaaed51c52),
  // separate from xbmc_detected_l5_* which is owned by the detect thread.
  // No collision with aml_dv_detect_active_area_stop() zeroing detect paths.
  // Auto-letterbox values are ADDITIVE: the kernel adds them to each frame's
  // source L5 so variable in-picture bars compose with the player-added
  // padding (e.g. 3840x2024 with per-scene RPU L5 0/208 -> emits 68/276).
  // Unless the plausibility watch found the source L5 already carrying the
  // crop, in which case composition would double the bars and we replace.
  // A manual override stays absolute — the user dictates the final value.
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_override_l5_top",    top);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_override_l5_bottom", bottom);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_override_l5_left",   left);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_override_l5_right",  right);
  const bool additive = autoLb && s_autoLbAdditive.load();
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_l5_override_additive", additive);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_force_l5_override",  active);

  CLog::Log(LOGDEBUG, "AMLUtils::aml_dv_apply_l5_override_sysfs - active={} auto={} additive={} t={} b={} l={} r={}",
            active, autoLb, additive, top, bottom, left, right);
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

/* Mid-read cache guard.  The between-seek wait (detect_wait_for_cache) can't
 * interrupt a single in-flight read: on a slow device one far-offset keyframe
 * read can take tens of seconds and drain a shallow buffer to underrun, stalling
 * playback before the next seek is even reached.  So while a scan is reading,
 * the ffmpeg interrupt callback watches the player's buffer and aborts the read
 * the moment it falls below this level — detection becomes best-effort and never
 * starves playback on hardware too slow to scan concurrently. */
static constexpr int kDetectCacheCriticalPct = 40;
static std::atomic<bool> s_detectThrottleActive{false}; /* scan reading w/ throttle on */
static std::atomic<bool> s_detectCacheStarved{false};   /* interrupt fired on low cache */
static std::atomic<int64_t> s_detectLastCacheCheckMs{0};

static int64_t detect_steady_ms()
{
  return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}

static int detect_interrupt_cb(void *opaque)
{
  if (s_detectCancel.load())
    return 1;
  /* Abort the in-flight read if playback's buffer goes critical.  Rate-limited:
   * the interrupt callback is polled in tight I/O loops, and the cache query
   * takes a (brief) player lock. */
  if (s_detectThrottleActive.load())
  {
    const int64_t now = detect_steady_ms();
    if (now - s_detectLastCacheCheckMs.load() >= 150)
    {
      s_detectLastCacheCheckMs.store(now);
      auto& components = CServiceBroker::GetAppComponents();
      const auto appPlayer = components.GetComponent<CApplicationPlayer>();
      if (appPlayer && appPlayer->IsPlaying() &&
          appPlayer->GetCacheLevel() < kDetectCacheCriticalPct)
      {
        s_detectCacheStarved.store(true);
        return 1;
      }
    }
  }
  return 0;
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

/* Playback-cache thresholds for the cache-aware detect throttle (percent). */
static constexpr int kDetectCacheTargetPct = 80; /* only seek when the buffer is this full */
static constexpr int kDetectCacheFloorPct  = 50; /* still below this after waiting = too slow */

/* Cache-aware throttle: block until the player's buffer reaches targetPct so
 * our competing reads don't starve playback.  Returns the cache level reached
 * [0..100], or -1 if cancelled.  Caps at maxWaitMs so a marginal source can't
 * hang detection forever.
 *
 * The old fixed inter-seek sleep was tuned for *network* bandwidth contention
 * and does nothing when the bottleneck is a slow/shared local device thrashed
 * by two concurrent seeking readers (e.g. a USB-2.0 enclosure): a single
 * far-offset keyframe read can take 5-10s and drain a shallow cache to empty,
 * producing a multi-second playback stall.  Gating each seek on the actual
 * playback cache level lets the device refill the player's buffer between
 * samples instead. */
static int detect_wait_for_cache(int targetPct, int maxWaitMs)
{
  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  const int step = 100;
  int waited = 0;
  for (;;)
  {
    if (s_detectCancel.load())
      return -1;
    /* No active playback to protect (stopped / never started) — proceed. */
    if (!appPlayer || !appPlayer->IsPlaying())
      return 100;
    const int level = appPlayer->GetCacheLevel();
    if (level >= targetPct || waited >= maxWaitMs)
      return level;
    std::this_thread::sleep_for(std::chrono::milliseconds(step));
    waited += step;
  }
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
    CLog::Log(LOGDEBUG, "DetectActiveArea: cache-aware I/O throttle enabled");

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

  /* Cache-aware throttle: wait for playback to build a safety margin before we
   * start competing for the device.  If the cache can't even reach the floor
   * within the cap, the device is too slow to scan without starving playback —
   * skip rather than cause a stall. */
  if (throttle)
  {
    const int lvl = detect_wait_for_cache(kDetectCacheTargetPct, 12000);
    if (lvl < 0)
      goto cleanup; /* cancelled */
    if (lvl < kDetectCacheFloorPct)
    {
      CLog::Log(LOGINFO, "DetectActiveArea: playback cache stuck at {}% after wait — "
                "device too slow to scan without starving playback, skipping", lvl);
      s_detectState.store(DV_DETECT_SKIPPED);
      goto cleanup;
    }
    CLog::Log(LOGDEBUG, "DetectActiveArea: throttle — playback cache at {}%, starting scan", lvl);
  }

  /* Arm the mid-read cache guard for everything from here on (open, find-stream,
   * and the per-seek reads) so detect_interrupt_cb can abort an in-flight read
   * that drains playback's buffer. */
  s_detectThrottleActive.store(throttle);

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
    goto cleanup;
  fmtCtx->pb = avioCtx;
  /* avformat_open_input sets this itself when pb is pre-supplied, but the
   * cancel check below can reach avformat_close_input on a never-opened
   * context — without the flag that path frees our custom pb (and hands the
   * stack CFile to ffurl_close).  Set it explicitly so we own avioCtx on
   * every exit path. */
  fmtCtx->flags |= AVFMT_FLAG_CUSTOM_IO;
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
      if (s_detectCancel.load() || s_detectCacheStarved.load())
        break;

      /* Cache-aware throttle: before each seek, wait for the player buffer to
       * refill so our reads don't drain it below empty.  Replaces the old fixed
       * 500ms yield, which was far shorter than a single far-offset keyframe
       * read and so never prevented starvation on slow local devices. */
      if (throttle && s > 0 && detect_wait_for_cache(kDetectCacheTargetPct, 8000) < 0)
        break; /* cancelled */

      bool usable = false;

      for (int retry = 0; retry <= maxRetries && !usable; retry++)
      {
        /* detect_interrupt_cb aborts an in-flight read on critical cache; stop
         * retrying immediately rather than re-seek and thrash the device more. */
        if (s_detectCacheStarved.load())
          break;
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

    if (s_detectCacheStarved.load())
    {
      CLog::Log(LOGINFO, "DetectActiveArea: aborted mid-scan — playback buffer went "
                "critical; device too slow to scan concurrently, skipping");
      s_detectState.store(DV_DETECT_SKIPPED);
      goto cleanup;
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
  s_detectThrottleActive.store(false); /* disarm the mid-read guard */

  /* A read aborted because playback's buffer went critical (e.g. during open or
   * find-stream-info) lands here with state still RUNNING — classify as skipped
   * (device too slow), not a hard failure. */
  if (s_detectCacheStarved.load() && s_detectState.load() == DV_DETECT_RUNNING)
    s_detectState.store(DV_DETECT_SKIPPED);

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
  {
    /* AVFMT_FLAG_CUSTOM_IO: ffmpeg never frees a caller-supplied pb, so this
     * is the sole owner.  Free via avioCtx->buffer, not avioBuf — ffmpeg may
     * have reallocated the buffer during probing. */
    av_freep(&avioCtx->buffer);
    avio_context_free(&avioCtx);
  }
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
  s_detectThrottleActive.store(false);
  s_detectCacheStarved.store(false);
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
  // SD CEA modes carry non-square pixels. CEA-861 declares the full 720-wide
  // raster as exactly 4:3 or 16:9, so the ratio is the signalled display
  // aspect over the raster aspect:
  //   720x480  4:3 -> 8:9     720x480 16:9 -> 32:27
  //   720x576  4:3 -> 16:15   720x576 16:9 -> 64:45
  // Leaving these at 1.0 makes the renderer letterbox 16:9 content into ~400
  // lines, which the display then stretches back out. HD/UHD modes and the
  // VESA/DMT modes (640x480, 800x600, 1024x768, 1280x1024) are square-pixel
  // by definition and stay at 1.0.
  res->fPixelRatio = 1.0f;
  if (res->iScreenWidth == 720 && (res->iScreenHeight == 480 || res->iScreenHeight == 576))
  {
    // CVBS is analogue 4:3 and drives a composite TV, not the HDMI sink, so
    // its ratio must never be taken from the sink's EDID. A "_4x3" mode name
    // selects the 4:3 VIC explicitly. Everything else takes the 16:9 VIC the
    // driver maps plain SD mode names to (hdmi_tx_edid.c dispmode_vic_tab),
    // unless the sink reports itself as a non-widescreen display.
    const bool is4x3 = fromMode.find("cvbs") != std::string::npos ||
                       fromMode.find("_4x3") != std::string::npos ||
                       !aml_display_is_widescreen();
    res->fPixelRatio = (is4x3 ? (4.0f / 3.0f) : (16.0f / 9.0f)) *
                       res->iScreenHeight / res->iScreenWidth;
  }
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

bool aml_display_mode_changing(const RESOLUTION_INFO &res)
{
  // Mirror aml_set_display_resolution()'s mode-string derivation so we can
  // tell up-front whether it would actually write to /sys/class/display/mode
  // (or flip frac_rate_policy). Used to skip the post-mode-switch reset
  // delay when the HDMI mode isn't really changing (e.g. DV pipeline
  // transitions on live-TV channel zaps with GUI already at the target rate).
  std::string mode = res.strId.c_str();
  std::vector<std::string> _mode = StringUtils::Split(mode, ' ');
  if (_mode.size() > 1)
    mode = _mode[0];

  CSysfsPath amhdmitx0_custom_mode{"/sys/class/amhdmitx/amhdmitx0/custom_mode"};
  if (amhdmitx0_custom_mode.Exists())
  {
    std::string custom_mode = amhdmitx0_custom_mode.Get<std::string>().value();
    if (custom_mode == mode)
      mode = "custombuilt";
  }

  CSysfsPath display_mode{"/sys/class/display/mode"};
  if (!display_mode.Exists())
    return true;

  std::string cur_mode = display_mode.Get<std::string>().value();
  if (cur_mode != mode)
    return true;

  if (aml_has_frac_rate_policy())
  {
    int fractional_rate = (res.fRefreshRate == floor(res.fRefreshRate)) ? 0 : 1;
    CSysfsPath frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    if (frac_rate_policy.Exists())
    {
      int cur_fractional_rate = frac_rate_policy.Get<int>().value();
      if (cur_fractional_rate != fractional_rate)
        return true;
    }
  }

  return false;
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

  // Record last canonical mode actually written so aml_display_mode_round_trip
  // has a non-null value to recover to if a subsequent read lands on the
  // intermediate "null" window (line ~2949). Skip recording "null" itself —
  // that's the wedge state we want to recover FROM, not TO.
  if (mode != "null" && !mode.empty())
  {
    std::lock_guard<std::mutex> lk(s_lastDisplayModeMutex);
    s_lastDisplayMode = mode;
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
