/*
 *  Copyright (C) 2010-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "cores/FFmpeg.h"

#include <optional>
#include <stdint.h>
#include <vector>

#include "ServiceBroker.h"
#include "cores/DataCacheCore.h"
#include "cores/VideoPlayer/DVDStreamInfo.h"
#include "cores/VideoPlayer/Process/ProcessInfo.h"

#include "HevcSei.h"
#include "HDR10Plus.h"
#include "HDR10PlusConvert.h"

extern "C" {
#include <libavutil/avutil.h>
#include <libavformat/avformat.h>
#include <libavfilter/avfilter.h>
#include <libavcodec/avcodec.h>
}

typedef struct
{
  const uint8_t *data;
  const uint8_t *end;
  int head;
  uint64_t cache;
} nal_bitstream;

typedef struct mpeg2_sequence
{
  uint32_t  width;
  uint32_t  height;
  uint32_t  fps_rate;
  uint32_t  fps_scale;
  float     ratio;
  uint32_t  ratio_info;
} mpeg2_sequence;

typedef struct h264_sequence
{
  uint32_t  width;
  uint32_t  height;
  float     ratio;
  uint32_t  ratio_info;
} h264_sequence;

typedef struct
{
  int profile_idc;
  int level_idc;
  int sps_id;

  int chroma_format_idc;
  int separate_colour_plane_flag;
  int bit_depth_luma_minus8;
  int bit_depth_chroma_minus8;
  int qpprime_y_zero_transform_bypass_flag;
  int seq_scaling_matrix_present_flag;

  int log2_max_frame_num_minus4;
  int pic_order_cnt_type;
  int log2_max_pic_order_cnt_lsb_minus4;

  int max_num_ref_frames;
  int gaps_in_frame_num_value_allowed_flag;
  int pic_width_in_mbs_minus1;
  int pic_height_in_map_units_minus1;

  int frame_mbs_only_flag;
  int mb_adaptive_frame_field_flag;

  int direct_8x8_inference_flag;

  int frame_cropping_flag;
  int frame_crop_left_offset;
  int frame_crop_right_offset;
  int frame_crop_top_offset;
  int frame_crop_bottom_offset;
} sps_info_struct;

enum DOVIMode : int
{
  MODE_NONE = 0,
  MODE_TOMEL,
  MODE_TO81
};

enum DOVICMv40Mode : int
{
  CMV40_NONE = 0,
  CMV40_NO_L2,
  CMV40_ALWAYS,
  CMV40_SMART, // per-frame L1 peak versus display peak plus headroom
  CMV40_SOURCE_AUTO = 4, // stream source peak versus display or selected peak
};

class CBitstreamParser
{
public:
  CBitstreamParser();
  ~CBitstreamParser() = default;

  static bool Open() { return true; }
  static void Close();
  static bool CanStartDecode(const uint8_t *buf, int buf_size);
};

class CBitstreamConverter
{
public:
  CBitstreamConverter(CDVDStreamInfo& hints);
  ~CBitstreamConverter();

  bool              Open(bool to_annexb);
  void              Close(void);
  bool              NeedConvert(void) const { return m_convert_bitstream; }
  bool              Convert(uint8_t *pData, int iSize, double pts);
  bool              Convert(uint8_t *pData_bl, int iSize_bl, uint8_t *pData_el, int iSize_el, double pts);
  uint8_t*          GetConvertBuffer(void) const;
  int               GetConvertSize() const;
  uint8_t*          GetExtraData();
  const uint8_t*    GetExtraData() const;
  int               GetExtraSize() const;
  void              ResetStartDecode(void);
  bool              CanStartDecode() const;
  void              SetConvertDovi(enum DOVIMode value) {
                      if (m_convert_dovi != value) InvalidateDoViCache();
                      m_convert_dovi = value;
                    }
  void              SetAppendCMv40(enum DOVICMv40Mode value) {
                      if (m_append_cmv40 != value) InvalidateDoViCache();
                      m_append_cmv40 = value;
                      m_smart_last_effective = CMV40_SMART;
                    }
                    // Display peak serves Smart and source Auto. Percent
                    // headroom is consulted only by Smart's per-frame policy.
  void              SetSmartBypassDisplayNits(int nits) {
                      if (m_smart_display_nits != nits) InvalidateDoViCache();
                      m_smart_display_nits = nits;
                    }
  void              SetSmartBypassThresholdPct(int pct) {
                      if (m_smart_threshold_pct != pct) InvalidateDoViCache();
                      m_smart_threshold_pct = pct;
                    }
                    // Source Auto: 0 uses display peak; 1..4 use
                    // 1000, 2000, 4000 and 10000 nits respectively.
  void              SetCMv40AutoTrigger(int trigger) {
                      if (m_cmv40_auto_trigger != trigger) InvalidateDoViCache();
                      m_cmv40_auto_trigger = trigger;
                    }
                    // Strip CMv4.0 ext blocks -> clean CMv2.9 (for old DV TVs
                    // that fail to fall back). Mutually exclusive with append;
                    // strip wins (see ProcessDoViRpu).
  void              SetStripCMv40(bool value) {
                      if (m_strip_cmv40 != value) InvalidateDoViCache();
                      m_strip_cmv40 = value;
                    }
                    // L5 active-area override pushed from
                    // CDVDVideoCodecAmlogic when the per-folder override.ini
                    // sets coreelec.amlogic.dolbyvision.level5.override.
                    // All-zero values mean "no override" (a stream with truly
                    // zero offsets has nothing to override anyway). Substituted
                    // into doviFrame/StreamMetadata during RPU parse so the
                    // render thread's overlay-active-area calc and downstream
                    // consumers see the override values.
                    void SetLevel5Override(bool active, uint16_t top, uint16_t bottom,
                                           uint16_t left, uint16_t right) {
                      const bool changed = (m_l5_override_active != active) ||
                                           (m_l5_override_top != top) ||
                                           (m_l5_override_bottom != bottom) ||
                                           (m_l5_override_left != left) ||
                                           (m_l5_override_right != right);
                      if (changed) InvalidateDoViCache();
                      m_l5_override_active = active;
                      m_l5_override_top = top;
                      m_l5_override_bottom = bottom;
                      m_l5_override_left = left;
                      m_l5_override_right = right;
                    }
  void              SetConvertHdr10Plus(bool value) { m_convert_Hdr10Plus = value; }
  void              SetPreferCovertHdr10Plus(bool value) { m_prefer_Hdr10Plus_conversion = value; }
  void              SetConvertHdr10PlusPeakBrightnessSource(enum PeakBrightnessSource value) { m_convert_Hdr10Plus_peak_brightness_source = value; };
  void              SetDualPriorityHdr10Plus(bool value) { m_dual_priority_Hdr10Plus = value; }
  void              SetRemoveDovi(bool value) { m_removeDovi = value; }
  void              SetRemoveHdr10Plus(bool value) { m_removeHdr10Plus = value; }

  static bool       mpeg2_sequence_header(const uint8_t *data, const uint32_t size, mpeg2_sequence *sequence);
  static bool       h264_sequence_header(const uint8_t *data, const uint32_t size, h264_sequence *sequence);

protected:
  void InvalidateDoViCache() {
    m_cached_dovi_rpu_in_nal.clear();
    m_cached_dovi_rpu_out_nal.clear();
  }

  static int        avc_parse_nal_units(AVIOContext *pb, const uint8_t *buf_in, int size);
  static int        avc_parse_nal_units_buf(const uint8_t *buf_in, uint8_t **buf, int *size);
  int               isom_write_avcc(AVIOContext *pb, const uint8_t *data, int len);
  // bitstream to bytestream (Annex B) conversion support.
  bool              IsIDR(uint8_t unit_type);
  bool              IsSlice(uint8_t unit_type);
  bool              BitstreamConvertInitAVC(void *in_extradata, int in_extrasize);
  bool              BitstreamConvertInitHEVC(void *in_extradata, int in_extrasize);
  bool              BitstreamConvert(uint8_t* pData, int iSize, uint8_t **poutbuf, int *poutbuf_size, double pts);
  static void       BitstreamAllocAndCopy(uint8_t** poutbuf,
                                         int* poutbuf_size,
                                         const uint8_t* sps_pps,
                                         uint32_t sps_pps_size,
                                         const uint8_t* in,
                                         uint32_t in_size,
                                         uint8_t nal_type);
  static void       BitstreamAllocAndCopy(uint8_t** poutbuf,
                                         uint32_t* poutbuf_size,
                                         const uint8_t* in,
                                         uint32_t in_size,
                                         uint8_t nal_type);

  void ApplyMasteringDisplayColourVolume(const MasteringDisplayColourVolume& metadata, bool& update);
  void ApplyContentLightLevel(const ContentLightLevel& metadata, bool& update);
  void UpdateHdrStaticMetadata();
  
  void AddDoViRpuNaluWrap(const Hdr10PlusMetadata& meta, uint8_t **poutbuf, uint32_t& poutbuf_size, double pts);
  void AddDoViRpuNalu(const Hdr10PlusMetadata& meta, uint8_t **poutbuf, int *poutbuf_size, double pts);

  void ProcessSeiPrefixWrap(uint8_t *buf, int32_t nal_size, uint8_t **poutbuf, uint32_t& poutbuf_size, Hdr10PlusMetadata& meta, bool& convert_hdr10plus_meta);
  void ProcessSeiPrefix(uint8_t *buf, int32_t nal_size, uint8_t **poutbuf, int *poutbuf_size, Hdr10PlusMetadata& meta, bool& convert_hdr10plus_meta);
  
  void ProcessDoViRpuWrap(uint8_t *buf, int32_t nal_size, uint8_t **poutbuf, uint32_t& poutbuf_size, double pts);
  void ProcessDoViRpu(uint8_t *buf, int32_t nal_size, uint8_t **poutbuf, int *poutbuf_size, double pts);
  
  typedef struct omx_bitstream_ctx {
      uint8_t  length_size;
      uint8_t  first_idr;
      uint8_t  idr_sps_pps_seen;
      uint8_t *sps_pps_data;
      uint32_t size;
  } omx_bitstream_ctx;

  uint8_t          *m_convertBuffer;
  int               m_convertSize;
  uint8_t          *m_inputBuffer;
  int               m_inputSize;

  uint32_t          m_sps_pps_size;
  omx_bitstream_ctx m_sps_pps_context;
  bool              m_convert_bitstream;
  bool              m_to_annexb;
  bool              m_combine;

  FFmpegExtraData   m_extraData;
  bool              m_convert_3byteTo4byteNALSize;
  bool              m_convert_bytestream;
  AVCodecID         m_codec;
  CDVDStreamInfo&   m_hints;
  CDataCacheCore&   m_dataCacheCore;
  StreamHdrType     m_intial_hdrType;
  bool              m_start_decode;
  enum DOVIMode     m_convert_dovi;
  bool              m_removeDovi;
  bool              m_removeHdr10Plus;
  bool              m_convert_Hdr10Plus;
  bool              m_prefer_Hdr10Plus_conversion;
  bool              m_dual_priority_Hdr10Plus;
  Hdr10PlusMetadata m_lastHdr10PlusMeta{};
  bool              m_lastHdr10PlusMetaValid{false};
  enum PeakBrightnessSource m_convert_Hdr10Plus_peak_brightness_source;
  bool              m_first_frame;
  enum DOVICMv40Mode m_append_cmv40;
  int               m_smart_display_nits{0};
  int               m_smart_threshold_pct{20};
  int               m_cmv40_auto_trigger{0};
  DOVICMv40Mode     m_smart_last_effective{CMV40_SMART};
  bool              m_strip_cmv40{false};
  bool              m_l5_override_active{false};
  uint16_t          m_l5_override_top{0};
  uint16_t          m_l5_override_bottom{0};
  uint16_t          m_l5_override_left{0};
  uint16_t          m_l5_override_right{0};
  HDRStaticMetadataInfo m_hdrStaticMetadataInfo;
  std::vector<uint8_t> m_cached_dovi_rpu_in_nal;
  std::vector<uint8_t> m_cached_dovi_rpu_out_nal;
  DOVIFrameMetadata m_cached_dovi_frame_metadata{};
  bool              m_is_dt_dl = false;
};

void aml_dv_send_md_levels();
void aml_dv_send_hdr10_data();
void aml_dv_send_el_type();
void aml_dv_send_profile(int dvprofile);
void aml_kodi_set_cd_cs(int cd_cs_type);
