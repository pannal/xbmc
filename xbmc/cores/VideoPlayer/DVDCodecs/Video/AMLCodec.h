/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDVideoCodec.h"
#include "cores/VideoPlayer/DVDStreamInfo.h"
#include "cores/IPlayer.h"
#include "windowing/Resolution.h"
#include "rendering/RenderSystem.h"
#include "utils/BitstreamConverter.h"
#include "utils/Geometry.h"

#include <array>
#include <deque>
#include <atomic>
#include <mutex>

typedef struct am_private_t am_private_t;

class DllLibAmCodec;

class PosixFile;
typedef std::shared_ptr<PosixFile> PosixFilePtr;

class CProcessInfo;

struct vpp_pq_ctrl_s {
	unsigned int length;
	union {
		void *ptr;/*point to pq_ctrl_s*/
		long long ptr_length;
	};
};

struct pq_ctrl_s {
	unsigned char sharpness0_en;
	unsigned char sharpness1_en;
	unsigned char dnlp_en;
	unsigned char cm_en;
	unsigned char vadj1_en;
	unsigned char vd1_ctrst_en;
	unsigned char vadj2_en;
	unsigned char post_ctrst_en;
	unsigned char wb_en;
	unsigned char gamma_en;
	unsigned char lc_en;
	unsigned char black_ext_en;
	unsigned char chroma_cor_en;
	unsigned char reserved;
};

#define _VE_CM  'C'
#define AMVECM_IOC_S_PQ_CTRL  _IOW(_VE_CM, 0x69, struct vpp_pq_ctrl_s)
#define AMVECM_IOC_G_PQ_CTRL  _IOR(_VE_CM, 0x6a, struct vpp_pq_ctrl_s)

class CAMLCodec
{
public:
  CAMLCodec(CProcessInfo &processInfo, CDVDStreamInfo &hints);
  virtual ~CAMLCodec();

  bool          OpenDecoder();
  void          CloseDecoder();
  void          Reset();
  void          Abort();

  bool          AddData(uint8_t *pData, size_t size, double dts, double pts);
  CDVDVideoCodec::VCReturn GetPicture(VideoPicture& videoPicture);

  void          SetSpeed(int speed);
  void          SetDrain(bool drain){m_drain = drain; if (drain) m_tp_drain_start = std::chrono::system_clock::now();};
  void          SetStreamEOF(bool eof){m_stream_eof = eof;};
  void          SetVideoRect(const CRect &SrcRect, const CRect &DestRect, uint64_t generation);
  void          SetVideoRate(int videoRate);
  int           GetOMXPts() const { return static_cast<int>(m_cur_pts); }
  double        GetPts() const { return static_cast<double>(m_cur_pts); }
  uint32_t      GetBufferIndex() const { return m_bufferIndex; };
  static float  OMXPtsToSeconds(int omxpts);
  static int    OMXDurationToNs(int duration);
  int           GetAmlDuration() const;
  int           ReleaseFrame(const uint32_t index, uint64_t generation, bool bDrop = false);
  uint64_t      GetPresentationGeneration();

  static int    PollFrame();
  static void   SetPollDevice(int device);

private:
  void          ShowMainVideo(const bool show);
  // Hide video output across a decode (re)start (startup / seek flush) until the
  // first valid frame, masking the brief green flash. coreelec.amlogic.video.restart.mute.
  void          HoldVideo(bool hold);
  // Whether the hold should engage for this stream: the master toggle above,
  // optionally narrowed to Dolby Vision streams (…restart.mute.dvonly). Gating
  // the hold also gates the seek-edge settle inside HoldVideo.
  bool          VideoRestartHoldWanted() const;
  bool          OpenAmlVideo(const CDVDStreamInfo &hints);
  void          CloseAmlVideo();
  std::string   GetVfmMap(const std::string &name);
  void          SetVfmMap(const std::string &name, const std::string &map);
  float         GetBufferLevel();
  float         GetBufferLevel(int new_chunk, int &data_len, int &free_len);
  int           DequeueBuffer();
  unsigned int  GetDecoderVideoRate();
  std::string   GetHDRStaticMetadata();

  std::string   intToFourCCString(unsigned int value);
  std::string   GetDoViCodecFourCC(unsigned int codec_tag);
  void          SetProcessInfoVideoDetails();

  DllLibAmCodec   *m_dll;
  bool             m_opened;
  // Buffered frames can outlive CloseDecoder. Drain their presentation calls
  // before teardown, then reject them even if this codec is reopened by a reset.
  // Never hold this mutex across decoder/DV teardown or graphics-lock acquisition.
  std::mutex       m_presentationMutex;
  bool             m_presentationActive = false;
  uint64_t         m_presentationGeneration = 0;
  bool             m_drain = false;
  bool             m_stream_eof = false;
  // True between a codec_reset and the next AddData: nothing is drainable in
  // that state, see the m_drain branch in GetPicture.
  bool             m_no_data_since_reset = false;
  // Green-flash mask state (coreelec.amlogic.video.restart.mute): the whole video
  // output is blanked (aml_video_mute, VENC black) across a decode (re)start until
  // the first valid frame, then released.
  bool             m_videoHoldActive = false;
  int              m_videoHoldTimeoutMs = 3000;
  std::chrono::time_point<std::chrono::system_clock> m_videoHoldStart;
  am_private_t    *am_private;

  int              m_speed;
  uint64_t         m_cur_pts;
  uint64_t         m_last_pts;
  uint64_t         m_prev_last_pts; // pts before m_last_pts (excursion detection)

  // Decoded frames queued between DequeueBuffer and GetPicture, handed over
  // in the order the decoder delivered them.
  struct DecodedFrame
  {
    uint64_t pts;
    uint32_t index;
  };
  std::deque<DecodedFrame> m_reorderQueue;
  // Consecutive timeline-rebuild steps outside the true-reorder band; a run
  // long enough means the timeline really moved and the chain re-anchors.
  size_t m_repairExcursionRun = 0;
  // Replace the decoder's timestamps with a clean chain at the nominal rate
  // (coreelec.amlogic.vc1_repair_timestamps) - they are not trustworthy, see
  // OpenDecoder.
  bool m_repairTimestamps = false;
  // Recently output (display-order) pts, for corrupt-splice detection: a pts
  // that steps backwards onto a value already output is a broken splice
  // (duplicate GOP / out-of-place keyframe), not a legal reorder.
  std::array<uint64_t, 8> m_outPtsRing{};
  size_t           m_outPtsRingPos = 0;
  size_t           m_outPtsRingCount = 0;
  uint32_t         m_bufferIndex;

  CRect            m_dst_rect;
  CRect            m_display_rect;

  int              m_view_mode = -1;
  RENDER_STEREO_MODE m_guiStereoMode = RENDER_STEREO_MODE_OFF;
  RENDER_STEREO_VIEW m_guiStereoView = RENDER_STEREO_VIEW_OFF;
  RESOLUTION       m_video_res = RES_INVALID;

  static const unsigned int STATE_PREFILLED  = 1;
  static const unsigned int STATE_HASPTS     = 2;

  unsigned int m_state;

  PosixFilePtr     m_amlVideoFile;
  std::string      m_defaultVfmMap;
  std::string      m_dvblpathVfmMap;

  static std::atomic_flag  m_pollSync;
  static int m_pollDevice;
  static double m_ttd;

  CDVDStreamInfo  &m_hints;         // Reference as values can change.
  CProcessInfo    &m_processInfo;
  CDataCacheCore  &m_dataCacheCore;

  int m_decoder_timeout;
  int m_decoder_drain_timeout;
  bool m_decoder_bypass_buffer_ready;
  float m_decoder_buffer;
  float m_decoder_stream_buffer;
  float m_decoder_minimum_buffer;
  float m_decoder_minimum_stream_buffer;

  std::chrono::time_point<std::chrono::system_clock> m_tp_last_frame;
  std::chrono::time_point<std::chrono::system_clock> m_tp_drain_start;

  // Hard (non-EAGAIN) codec-write failure escalation, see AddData: the packet
  // is retried by the player, the decoder reset periodically, and only after a
  // bounded window is the packet given up so a permanently dead codec cannot
  // stall playback forever.
  bool             m_wrFailActive = false;
  std::chrono::time_point<std::chrono::steady_clock> m_tpWrFailStart;
  std::chrono::time_point<std::chrono::steady_clock> m_tpWrFailLastReset;

  bool            m_buffer_level_ready;
  float           m_minimum_buffer_level;
  bool            m_dvOpened = false;
  std::atomic_bool m_abort{false};
};
