/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "ICodec.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodec.h"
#include "cores/VideoPlayer/DVDDemuxers/DVDDemux.h"
#include "cores/VideoPlayer/DVDInputStreams/DVDInputStream.h"

#include <cstdint>
#include <memory>
#include <vector>

namespace ActiveAE
{
  class IAEResample;
};

class VideoPlayerCodec : public ICodec
{
public:
  VideoPlayerCodec();
  ~VideoPlayerCodec() override;

  bool Init(const CFileItem &file, unsigned int filecache) override;
  bool Seek(int64_t iSeekTime) override;
  int ReadPCM(uint8_t* pBuffer, size_t size, size_t* actualsize) override;
  int ReadRaw(uint8_t **pBuffer, int *bufferSize) override;
  bool CanInit() override;
  bool CanSeek() override;

  void DeInit();
  AEAudioFormat GetFormat();
  void SetContentType(const std::string &strContent);

  bool NeedConvert(AEDataFormat fmt);
  void SetPassthroughStreamType(CAEStreamInfo::DataType streamType);

private:
  CAEStreamInfo::DataType GetPassthroughStreamType(AVCodecID codecId, int samplerate, int profile);
  int FetchFrame();
  bool Reformat();

  CDVDDemux* m_pDemuxer{nullptr};
  std::shared_ptr<CDVDInputStream> m_pInputStream;
  std::unique_ptr<CDVDAudioCodec> m_pAudioCodec;

  std::string m_strContentType;
  std::string m_strFileName;
  int m_nAudioStream{-1};
  size_t m_nDecodedLen{0};
  //! A packet the codec did not take, to be offered again - see ReadPCM.
  DemuxPacket* m_pRefusedPacket{nullptr};
  //! The codec has been told the demuxer is dry - see ReadPCM.
  bool m_drained{false};

  bool m_bInited{false};
  bool m_bCanSeek{false};

  std::unique_ptr<ActiveAE::IAEResample> m_pResampler;
  DVDAudioFrame m_audioFrame{};
  int m_planes{0};
  bool m_needConvert{false};
  AEAudioFormat m_srcFormat{};
  int m_channels{0};

  //! Brings a frame whose format has changed since Init() back to the one
  //! PAPlayer was given - see Reformat().
  std::unique_ptr<ActiveAE::IAEResample> m_pReformatter;
  AEAudioFormat m_reformatFrom{};
  std::vector<uint8_t> m_reformatted;
  //! The frame being read out is in m_reformatted, already in m_format.
  bool m_frameReformatted{false};

  std::unique_ptr<CProcessInfo> m_processInfo;
};

