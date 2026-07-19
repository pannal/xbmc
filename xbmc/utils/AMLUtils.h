/*
 *  Copyright (C) 2011-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "windowing/Resolution.h"
#include "utils/StreamDetails.h"

extern "C" {
#include <libavutil/pixfmt.h>
}

#include <string>
#include <vector>

enum AML_DEVICE_TYPE
{
  AML_DEVICE_TYPE_UNINIT   = -2,
  AML_DEVICE_TYPE_UNKNOWN  = -1,
  AML_DEVICE_TYPE_M1,
  AML_DEVICE_TYPE_M3,
  AML_DEVICE_TYPE_M6,
  AML_DEVICE_TYPE_M8,   // S802
  AML_DEVICE_TYPE_M8B,  // S805
  AML_DEVICE_TYPE_M8M2  // S812
};

enum AML_DISPLAY_AXIS_PARAM
{
  AML_DISPLAY_AXIS_PARAM_X = 0,
  AML_DISPLAY_AXIS_PARAM_Y,
  AML_DISPLAY_AXIS_PARAM_WIDTH,
  AML_DISPLAY_AXIS_PARAM_HEIGHT
};

enum AML_SUPPORT_H264_4K2K
{
  AML_SUPPORT_H264_4K2K_UNINIT = -1,
  AML_NO_H264_4K2K,
  AML_HAS_H264_4K2K,
  AML_HAS_H264_4K2K_SAME_PROFILE
};

enum DV_MODE : int
{
  DV_MODE_ON = 0,
  DV_MODE_ON_DEMAND,
  DV_MODE_OFF
};

enum DV_TYPE : int
{
  DV_TYPE_DISPLAY_LED = 0,
  DV_TYPE_PLAYER_LED_LLDV,
  DV_TYPE_PLAYER_LED_HDR,
  DV_TYPE_VS10_ONLY,
  DV_TYPE_PLAYER_LED_HDR2
};

enum DV_COLORIMETRY : int
{
  DV_COLORIMETRY_AMLOGIC = 0,
  DV_COLORIMETRY_BT2020NC,
  DV_COLORIMETRY_REMOVE
};

#define AML_GXBB    0x1F
#define AML_GXL     0x21
#define AML_GXM     0x22
#define AML_G12A    0x28
#define AML_G12B    0x29
#define AML_SM1     0x2B
#define AML_SC2     0x32

#define FLAG_FORCE_CVM          (unsigned int)(0x0001)
#define FLAG_FORCE_DOVI_LL      (unsigned int)(0x4000)
#define FLAG_FORCE_RGB_OUTPUT   (unsigned int)(0x8000)
#define FLAG_TOGGLE_FRAME       (unsigned int)(0x80000000)

#define DOLBY_VISION_LL_DISABLE (unsigned int)(0)
#define DOLBY_VISION_LL_YUV422  (unsigned int)(1)
#define DOLBY_VISION_LL_RGB444  (unsigned int)(2)

#define DOLBY_VISION_FOLLOW_SOURCE     (unsigned int)(1)
#define DOLBY_VISION_FORCE_OUTPUT_MODE (unsigned int)(2)

#define DOLBY_VISION_OUTPUT_MODE_IPT        (unsigned int)(0)
#define DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL (unsigned int)(1)
#define DOLBY_VISION_OUTPUT_MODE_HDR10      (unsigned int)(2)
#define DOLBY_VISION_OUTPUT_MODE_SDR10      (unsigned int)(3)
#define DOLBY_VISION_OUTPUT_MODE_SDR8       (unsigned int)(4)
#define DOLBY_VISION_OUTPUT_MODE_BYPASS     (unsigned int)(5)

int  aml_get_cpufamily_id();
bool aml_display_is_widescreen();
bool aml_display_support_hdr_pq();
bool aml_display_support_hdr_hlg();
bool aml_display_support_hdr10plus();
bool aml_display_support_dv();
bool aml_display_support_dv_ll();
bool aml_display_support_dv_std();
bool aml_display_support_3d();
bool aml_display_support_12bit(int force_cs = 0);
bool aml_support_hevc();
bool aml_support_hevc_4k2k();
bool aml_support_hevc_8k4k();
bool aml_support_hevc_10bit();
AML_SUPPORT_H264_4K2K aml_support_h264_4k2k();
bool aml_support_vp9();
bool aml_support_av1();
bool aml_support_dolby_vision();
bool aml_dolby_vision_enabled();
std::string aml_dv_output_mode_to_string(unsigned int mode);
std::string aml_dv_mode_to_string(enum DV_MODE mode);
std::string aml_dv_type_to_string(enum DV_TYPE type);
void aml_dv_set_vs10_mode(unsigned int mode, StreamHdrType hdrType);
void aml_dv_wait_video_off(int timeout);
int aml_blackout_policy(int new_blackout);
unsigned int aml_dv_on(unsigned int mode, bool force_hdmi = false);
void aml_dv_off(bool skip_hdmi_update = false);
// Push the L5-related sysfs flags from current Kodi settings. Called from
// aml_dv_on() at playback start, and from CDolbyVisionAML::OnSettingChanged
// so per-folder override.ini writes that mutate L5 settings take effect mid-
// playback instead of waiting for the next aml_dv_on().
void aml_dv_apply_l5_sysfs();
// Diagnostic: snapshot full DV/HDMI/fb0 kernel state + Kodi cached state to
// debug log. Already called at every DV state transition (dv_on, dv_off,
// dv_open, dv_close, set_vs10_mode, xbmc_osd). Exposed so CVideoSyncAML can
// also trigger one when it detects a vsync ioctl stall — that captures
// post-transition drift the existing transition snapshots can't see.
void aml_dv_dump_state(const char* tag);

// Lightweight HDMI-TX link-state watchdog for the GUI/menu path. The richer
// aml_dv_dump_state() only fires on DV transitions, and the vsync-stall
// snapshot only runs while the RefClock playback thread is alive — so a sink
// that drops sync while sitting in the menus (no playback) leaves no trace in
// the log. Call this from the per-frame present hook: it self-throttles to
// ~1Hz and only logs when the link state changes (LOGWARNING on a degrade,
// LOGINFO otherwise) — quiet when stable, loud the moment the link drops.
void aml_hdmi_link_probe(const char* ctx);

void aml_get_dv_cap();
struct xbmc_dv_cap
{
  static inline int dv_ver_i = 0;
  static inline int dv_len_i = 0;
  static inline int dv_max_v1_i = 0;
  static inline int dv_max_v2_i = 0;
  static inline int dv_rx_i = 0;
  static inline int dv_ry_i = 0;
  static inline int dv_gx_i = 0;
  static inline int dv_gy_i = 0;
  static inline int dv_bx_i = 0;
  static inline int dv_by_i = 0;
  static inline std::string dv_vsvdb_s = "";
  static inline std::string edid_pnpid = "";
};
unsigned int aml_dv_dolby_vision_mode();
// swDecoded: true when the caller is the software/GLES render path (no AML hardware
// video layer / VD1). Such streams cannot use VS10 conversion — see aml_dv_open().
void aml_dv_open(StreamHdrType hdrType, unsigned int bitDepth, AVColorPrimaries colorPrimaries = AVCOL_PRI_UNSPECIFIED, bool swDecoded = false);
void aml_dv_close();
bool aml_dv_playback_active();
void aml_dv_set_osd_max(int max);
void aml_dv_set_osd_brightness(int nits);
void aml_dv_set_hdr10_osd_brightness(int nits);
int  aml_dv_sdr_boost_param();
void aml_dv_set_sdr_source_max_nits(int value);
bool aml_is_dv_enable();
void aml_dv_display_trigger();
void aml_hdr10plus_vsif_hold(bool hold);
void aml_dv_display_auto_now();
void aml_dv_start();
// Restore the GUI IPT output mode after playback truly ended. All guard
// checks (DV_MODE_ON, dv enabled, NOT playback-active, not already IPT) run
// inside the DV-core critical section so the decision and the off->on cycle
// are atomic vs a concurrent aml_dv_open() — a queued restore that lost the
// race to a new playback no-ops instead of cycling over the live stream.
// Returns true when a restore cycle actually ran. reason = log tag.
bool aml_dv_restore_gui_ipt(const char* reason);
void aml_dv_wait_for_pipeline();
void aml_dv_set_subtitles(bool visible);
void aml_dv_set_xbmc_osd();
bool aml_dv_use_active_area();
int aml_dv_l5_subs_signal_mode();
bool aml_dv_detect_active_area_enabled();
bool aml_dv_detect_active_area_stable();
void aml_dv_detect_active_area_get(uint16_t& top, uint16_t& bottom, uint16_t& left, uint16_t& right);
enum DvDetectState { DV_DETECT_FAILED = 0, DV_DETECT_SKIP_NON16X9 = 1,
                     DV_DETECT_SKIP_IMAX = 2, DV_DETECT_OK = 3,
                     DV_DETECT_SKIPPED = 4, DV_DETECT_INACTIVE = 5,
                     DV_DETECT_RUNNING = 6 };
int aml_dv_detect_active_area_state();
void aml_dv_detect_set_file(const std::string& path);
void aml_dv_detect_active_area_start();
void aml_dv_detect_active_area_stop();
// True when coreelec.amlogic.dolbyvision.level5.override is set to ANY value
// (including "0,0,0,0" — a legitimate override meaning "treat the stream as
// having no bars / full active frame"). Empty / unparseable = inactive.
// Used to short-circuit the active-area detect path and to gate the kernel-
// side xbmc_force_l5_override flag.
bool aml_dv_l5_override_active();

// Push the L5 active-area override to the amdolby_vision kernel module so
// the TV sees the override values in the outgoing RPU. Writes the four
// xbmc_detected_l5_* params plus the xbmc_force_l5_override flag (which
// makes the kernel substitute regardless of source RPU L5 state). Called
// from aml_dv_on() at playback start and from CDolbyVisionAML::OnSettingChanged
// when the override key changes mid-playback.
void aml_dv_apply_l5_override_sysfs();

// Stash the current stream's coded geometry + native-DV flag for the
// auto-letterbox L5 path (coreelec.amlogic.dolbyvision.l5.auto.letterbox).
// Called at codec Open(); pass nativeDV=false for non-DV streams to clear.
void aml_dv_set_active_area_geometry(int width, int height, bool nativeDV);

// True when the auto-letterbox L5 path is reacting to the current stream:
// native DV, L5 + auto-letterbox enabled, no manual L5 override set, and the
// coded frame is non-16:9 (cropped). Gates the kernel substitution master and
// feeds synthesised offsets into aml_dv_apply_l5_override_sysfs().
bool aml_dv_auto_letterbox_active();

// Fetch the synthesised auto-letterbox L5 offsets for the current stream.
// Returns false (outputs 0) when the path is not reacting (same gating as
// aml_dv_auto_letterbox_active()). For surfacing the values in player info.
bool aml_dv_auto_letterbox_get(uint16_t& top, uint16_t& bottom,
                               uint16_t& left, uint16_t& right);
enum DV_MODE aml_dv_mode();
enum DV_TYPE aml_dv_type();
unsigned int aml_vs10_by_setting(const std::string setting);
void aml_dv_send_md_levels();
void aml_dv_send_hdr10_data();
void aml_dv_send_el_type();
void aml_dv_send_profile(int dvprofile);
void set_vsvdb_payload_ver(enum DV_TYPE dv_type, int max_lum_nits_value, int source_max_pq);
void CalculateVSVDBPayload();
void CalculateVSVDBPayload_2();
void aml_dv_enable_fel();
void aml_hevc_nal_skip_policy(const int value);
void aml_set_transfer_pq(StreamHdrType hdrType, unsigned int bitDepth);
bool aml_has_frac_rate_policy();
void aml_video_mute(bool mute);
void aml_set_audio_passthrough(bool passthrough);
void aml_set_3d_video_mode(unsigned int mode, bool framepacking_support, int view_mode);
bool aml_mode_to_resolution(const char *mode, RESOLUTION_INFO *res);
bool aml_get_native_resolution(RESOLUTION_INFO *res);
bool aml_set_native_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name, const int stereo_mode, bool force_mode_switch);
bool aml_probe_resolutions(std::vector<RESOLUTION_INFO> &resolutions);
bool aml_set_display_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name, bool force_mode_switch);
bool aml_display_mode_changing(const RESOLUTION_INFO &res);
void aml_handle_scale(const RESOLUTION_INFO &res);
void aml_handle_display_stereo_mode(const int stereo_mode);
void aml_enable_freeScale(const RESOLUTION_INFO &res);
void aml_disable_freeScale();
void aml_set_framebuffer_resolution(const RESOLUTION_INFO &res, std::string framebuffer_name);
void aml_set_framebuffer_resolution(unsigned int width, unsigned int height, std::string framebuffer_name);
bool aml_read_reg(const std::string &reg, uint32_t &reg_val);
bool aml_has_capability_ignore_alpha();
bool aml_set_reg_ignore_alpha();
bool aml_unset_reg_ignore_alpha();
std::string aml_video_fps_info();
std::string aml_video_fps_drop();
unsigned int aml_dv_video_processor_mode();
void aml_toogle_video_freerun_mode();
void aml_set_audio_ddr_urgent(bool enable);
void aml_reset_audio_from_vs10_change();
void aml_reset_audio_from_player_open();
void aml_reset_audio_from_player_pause();
void aml_reset_audio_from_window_home();
void aml_reset_audio_from_play_from_beginning();
void aml_reset_audio_from_play_from_resume();
void aml_kodi_set_cd_cs(int cd_cs_type);
void aml_kodi_reset_cd_cs();