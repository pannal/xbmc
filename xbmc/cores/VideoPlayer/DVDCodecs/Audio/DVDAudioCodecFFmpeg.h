/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDAudioCodec.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>
#include <libavutil/downmix_info.h>
}

class CProcessInfo;

class CDVDAudioCodecFFmpeg : public CDVDAudioCodec
{
public:
  explicit CDVDAudioCodecFFmpeg(CProcessInfo &processInfo);
  ~CDVDAudioCodecFFmpeg() override;
  bool Open(CDVDStreamInfo &hints,
                    CDVDCodecOptions &options) override;
  void Dispose() override;
  bool AddData(const DemuxPacket &packet) override;
  void GetData(DVDAudioFrame &frame) override;
  void Reset() override;
  AEAudioFormat GetFormat() override { return m_format; }
  std::string GetName() override { return m_codecName; }
  enum AVMatrixEncoding GetMatrixEncoding() override;
  enum AVAudioServiceType GetAudioServiceType() override;
  int GetProfile() override;

protected:
  // Virtual so a subclass can serve a frame it has already received rather
  // than pull the next one off the decoder. GetData(DVDAudioFrame&) formats
  // whatever this returns, so overriding it is enough to keep that path right.
  virtual int GetData(uint8_t** dst);
  // (Re)apply the configured dynamic range compression to the open codec.
  // Must be called after every avcodec_flush_buffers(): the AC3/E-AC3
  // decoder's flush memsets drc_scale (it lives past frame_type in
  // AC3DecodeContext), silently disabling DRC after a seek.
  void ApplyDrcScale();
  enum AEDataFormat GetDataFormat();
  int GetSampleRate();
  int GetChannels();
  CAEChannelInfo GetChannelMap();
  int GetBitRate() override;
  void BuildChannelMap();

  AEAudioFormat m_format;
  AVCodecContext* m_pCodecContext;
  enum AVSampleFormat m_iSampleFormat = AV_SAMPLE_FMT_NONE;
  CAEChannelInfo m_channelLayout;
  enum AVMatrixEncoding m_matrixEncoding = AV_MATRIX_ENCODING_NONE;
  AVFrame* m_pFrame;
  AVDownmixInfo m_downmixInfo;
  bool m_hasDownmix = false;
  bool m_eof;
  int m_channels;
  uint64_t m_layout;
  std::string m_codecName;
  uint64_t m_hint_layout;
};

