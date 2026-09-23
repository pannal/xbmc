/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDVideoCodec.h"
#include "DVDStreamInfo.h"
#include "settings/lib/ISettingCallback.h"
#include "threads/CriticalSection.h"
#include "cores/VideoPlayer/Buffers/VideoBuffer.h"
#include "utils/BitstreamConverter.h"

#include <set>
#include <atomic>
#include <memory>

class CAMLCodec;
struct mpeg2_sequence;
struct h264_sequence;
class CBitstreamParser;
class CBitstreamConverter;

class CDVDVideoCodecAmlogic;

typedef std::tuple<uint8_t*, uint32_t, bool, double> DLDemuxPacket; // data, size, isEL, pts

class CAMLVideoBuffer : public CVideoBuffer
{
public:
  CAMLVideoBuffer(int id) : CVideoBuffer(id) {};
  void Set(CDVDVideoCodecAmlogic *codec, std::shared_ptr<CAMLCodec> amlcodec, int omxPts, int amlDuration, uint32_t bufferIndex, uint64_t generation)
  {
    m_codec = codec;
    m_amlCodec = amlcodec;
    m_omxPts = omxPts;
    m_amlDuration = amlDuration;
    m_bufferIndex = bufferIndex;
    m_presentationGeneration = generation;
  }

  CDVDVideoCodecAmlogic* m_codec;
  std::shared_ptr<CAMLCodec> m_amlCodec;
  int m_omxPts, m_amlDuration;
  uint32_t m_bufferIndex;
  uint64_t m_presentationGeneration = 0;
};

class CAMLVideoBufferPool : public IVideoBufferPool
{
public:
  virtual ~CAMLVideoBufferPool();

  virtual CVideoBuffer* Get() override;
  virtual void Return(int id) override;

private:
  CCriticalSection m_criticalSection;;
  std::vector<CAMLVideoBuffer*> m_videoBuffers;
  std::vector<int> m_freeBuffers;
};

class CSetting;

class CDVDVideoCodecAmlogic : public CDVDVideoCodec, public ISettingCallback
{
public:
  CDVDVideoCodecAmlogic(CProcessInfo &processInfo);
  virtual ~CDVDVideoCodecAmlogic();

  static std::unique_ptr<CDVDVideoCodec> Create(CProcessInfo& processInfo);
  static bool Register();

  // Required overrides
  virtual bool Open(CDVDStreamInfo &hints, CDVDCodecOptions &options) override;
  virtual bool AddData(const DemuxPacket &packet) override;
  virtual void Reset() override;
  virtual VCReturn GetPicture(VideoPicture* pVideoPicture) override;
  virtual void SetSpeed(int iSpeed) override;
  virtual void SetCodecControl(int flags) override;
  virtual void Abort() override;
  virtual const char* GetName(void) override { return (const char*)m_pFormatName; }
  virtual bool SupportsExtention() { return true; }

  // ISettingCallback
  void OnSettingChanged(const std::shared_ptr<const CSetting>& setting) override;

protected:
  void            Close(void);
  void            FrameRateTracking(uint8_t *pData, int iSize, double dts, double pts);
  //void            RemoveInfo(CDVDAmlogicInfo* info);

  std::shared_ptr<CAMLCodec> m_Codec;

  const char     *m_pFormatName;
  VideoPicture m_videobuffer;
  bool            m_opened;
  int             m_codecControlFlags;
  CDVDStreamInfo  m_hints;
  double          m_framerate;
  int             m_video_rate;
  float           m_aspect_ratio;
  mpeg2_sequence *m_mpeg2_sequence;
  double          m_mpeg2_sequence_pts;
  h264_sequence  *m_h264_sequence;
  double          m_h264_sequence_pts;
  bool            m_has_keyframe;

  CBitstreamParser *m_bitparser;
  std::unique_ptr<CBitstreamConverter> m_bitstream;
private:
  void UpdateAppendCMv40SettingCache();
  void UpdateStripCMv40SettingCache();
  void UpdateLevel5OverrideSettingCache();
  void ApplyDynamicDoViSettings();

  std::shared_ptr<CAMLVideoBufferPool> m_videoBufferPool;
  static std::atomic<bool> m_InstanceGuard;

  std::list<DLDemuxPacket> m_packages;
  int m_el_starvation_count = 0;

  std::atomic<int> m_appendCMv40ModeSetting{static_cast<int>(DOVICMv40Mode::CMV40_NONE)};
  DOVICMv40Mode m_appendCMv40ModeApplied{DOVICMv40Mode::CMV40_NONE};
  std::atomic<bool> m_stripCMv40Setting{false};
  bool m_stripCMv40Applied{false};
  // Smart CMv4.0 bypass inputs (cached from settings; only used when
  // m_appendCMv40ModeSetting == CMV40_SMART). Display peak nits come from the
  // EDID VSVDB/HGIG max-luminance setting; threshold is percent headroom.
  // Atomic like the mode/strip caches above: written on the settings thread
  // (UpdateAppendCMv40SettingCache via OnSettingChanged), read on the decode
  // thread (ApplyDynamicDoViSettings / Open). They are published *before* the
  // gating m_appendCMv40ModeSetting store so a reader that observes CMV40_SMART
  // also observes coherent nits/threshold.
  std::atomic<int> m_smartDisplayNits{0};
  std::atomic<int> m_smartThresholdPct{20};
  // L5 active-area override. Active is tracked separately from values so
  // "0,0,0,0" (a legitimate override meaning "treat the stream as having no
  // bars") is distinguished from "no override set" (empty string). Values
  // packed: [63:48]=top [47:32]=bottom [31:16]=left [15:0]=right. Two atomics
  // is racy across reads, but the worst case is one frame of mixed state
  // (invisible) — and the alternative of compressing both into one uint64
  // would require sub-16-bit offsets.
  std::atomic<bool> m_level5OverrideActiveSetting{false};
  std::atomic<uint64_t> m_level5OverrideValuesSetting{0};
  bool m_level5OverrideActiveApplied{false};
  uint64_t m_level5OverrideValuesApplied{0};
  bool m_settingsCallbackRegistered{false};
};
