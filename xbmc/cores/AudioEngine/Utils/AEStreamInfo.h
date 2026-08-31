/*
 *  Copyright (C) 2010-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "AEPackIEC61937.h"
#include "AEChannelInfo.h"
#include <stdint.h>

/* ffmpeg re-defines this, so undef it to squash the warning */
#undef restrict

extern "C" {
#include <libavutil/crc.h>
}

class CAEStreamInfo
{
public:
  double GetDuration() const;
  bool operator==(const CAEStreamInfo& info) const;

  enum DataType
  {
    STREAM_TYPE_NULL,
    STREAM_TYPE_AC3,
    STREAM_TYPE_DTS_512,
    STREAM_TYPE_DTS_1024,
    STREAM_TYPE_DTS_2048,
    STREAM_TYPE_DTSHD,
    STREAM_TYPE_DTSHD_CORE,
    STREAM_TYPE_EAC3,
    STREAM_TYPE_MLP,
    STREAM_TYPE_TRUEHD,
    STREAM_TYPE_DTSHD_MA
  };
  DataType m_type = STREAM_TYPE_NULL;
  unsigned int m_sampleRate = 0;
  unsigned int m_bitDepth = 0;
  unsigned int m_channels = 0;
  bool m_dataIsLE = true;
  unsigned int m_dtsPeriod = 0;
  unsigned int m_repeat = 0;
  unsigned int m_ac3FrameSize = 0;
  unsigned int m_dtsSamplesPerFrame = 0;
  int m_dialNorm = 0; // Dialog Normalization in dB (TrueHD Atmos 16ch)
  bool m_hasAtmos = false; // TrueHD Atmos (16-channel presentation)
  // Encoded elements in the 16-channel presentation: bed/static channels plus
  // dynamic objects, from 16ch_channel_count. Not an object count on its own -
  // program_assignment() below is what separates the two.
  unsigned int m_atmosChannels = 0;
  // Dynamic objects only, from program_assignment(): the presentation's element
  // total less its bed (static) and ISF objects. Set for TrueHD out of the major
  // sync and for E-AC-3 JOC out of the dependent substream's object metadata, so
  // it means the same thing whichever codec is playing. -1 when the stream did
  // not declare it or the block could not be parsed, which is not the same as 0
  // (a bed-only Atmos program that genuinely carries no dynamic objects).
  int m_atmosObjects = -1;
};

class CAEStreamParser
{
public:

  CAEStreamParser();
  ~CAEStreamParser() = default;

  int AddData(uint8_t *data, unsigned int size, uint8_t **buffer = NULL, unsigned int *bufferSize = 0);

  //! \brief Restrict which codec families DetectType() may sync to after a
  //! total sync loss. A demuxer-typed track never legitimately changes codec
  //! family mid-stream, but a corrupt region can contain a foreign syncword
  //! (AC3's is only two bytes) and without the restriction the parser locks
  //! onto it, "detecting" a bogus format and tearing the audio stream down.
  enum class SyncFamily
  {
    Any,
    AC3, // AC3 and E-AC3 (shared syncword)
    DTS,
    TrueHD
  };
  void SetSyncFamily(SyncFamily family) { m_syncFamily = family; }

  //! \brief Number of times sync was lost mid-stream (i.e. not through
  //! Reset()). A corrupt stream region eats bytes before the parser re-locks,
  //! which physically shortens the audio while every timestamp still looks
  //! correct - the player uses this to notice that it happened at all.
  unsigned int GetSyncLostCount() const { return m_syncLostCount; }

  void SetCoreOnly(bool value) { m_coreOnly = value; }
  void SetDefeatTrueHDDialNorm(bool value) { m_defeatTrueHDDialNorm = value; }
  void SetDefeatAC3DialNorm(bool value) { m_defeatAC3DialNorm = value; }
  void SetDefeatDTSDialNorm(bool value) { m_defeatDTSDialNorm = value; }
  //! \brief Tell the parser the demuxer typed this E-AC-3 track as DD+ Atmos.
  //! Only a hint: the object scan is bounded on its own, so a stream this is
  //! never set for still gets read, it just spends its budget doing so.
  void SetEAC3JOC(bool value) { m_eac3IsJOC = value; }
  unsigned int IsValid() const { return m_hasSync; }
  unsigned int GetSampleRate() const { return m_info.m_sampleRate; }
  unsigned int GetChannels() const { return m_info.m_channels; }
  unsigned int GetFrameSize() const { return m_fsize; }
  // unsigned int GetDTSBlocks() const { return m_dtsBlocks; }
  unsigned int GetDTSPeriod() const { return m_info.m_dtsPeriod; }
  unsigned int GetEAC3BlocksDiv() const { return m_info.m_repeat; }
  enum CAEStreamInfo::DataType GetDataType() const { return m_info.m_type; }
  int GetDialNorm() const { return m_info.m_dialNorm; }
  bool HasAtmos() const { return m_info.m_hasAtmos; }
  unsigned int GetAtmosChannels() const { return m_info.m_atmosChannels; }
  int GetAtmosObjects() const { return m_info.m_atmosObjects; }
  bool IsLittleEndian() const { return m_info.m_dataIsLE; }
  unsigned int GetBufferSize() const { return m_bufferSize; }
  CAEStreamInfo& GetStreamInfo() { return m_info; }
  void Reset();

private:
  uint8_t m_buffer[MAX_IEC61937_PACKET];
  unsigned int m_bufferSize = 0;
  unsigned int m_skipBytes = 0;

  typedef unsigned int (CAEStreamParser::*ParseFunc)(uint8_t *data, unsigned int size);

  CAEStreamInfo m_info;
  SyncFamily m_syncFamily = SyncFamily::Any;
  unsigned int m_syncLostCount = 0;
  bool m_coreOnly = false;
  bool m_defeatTrueHDDialNorm = false;
  bool m_defeatAC3DialNorm = false;
  bool m_defeatDTSDialNorm = false;
  /* E-AC-3 JOC object metadata is looked for in dependent substream frames and
     latched: once found, or once the budget is spent, the scan stops for the
     rest of the stream. A seek re-arms both through Reset(). */
  bool m_eac3IsJOC = false;
  bool m_eac3ObjectsLatched = false;
  unsigned int m_eac3ScanAttempts = 0;
  /* How long to keep looking. The larger budget applies when the demuxer typed
     the track as Atmos, because then the metadata is known to be in there
     somewhere and giving up early would report nothing for a stream that has
     something. Where it did not, a short budget is enough to be sure. */
  static constexpr unsigned int EAC3_OBJECT_SCANS_UNTYPED = 64;
  static constexpr unsigned int EAC3_OBJECT_SCANS_TYPED_ATMOS = 512;
  unsigned int EAC3ObjectScanBudget() const
  {
    return m_eac3IsJOC ? EAC3_OBJECT_SCANS_TYPED_ATMOS : EAC3_OBJECT_SCANS_UNTYPED;
  }
  unsigned int m_needBytes = 0;
  ParseFunc m_syncFunc;
  bool m_hasSync = false;

  unsigned int m_coreSize = 0;         /* core size for dtsHD */
  unsigned int m_dtsBlocks = 0;
  unsigned int m_fsize = 0;
  int m_substreams = 0;       /* used for TrueHD  */
  AVCRC m_crcTrueHD[1024];  /* TrueHD crc table */

  /* TrueHD major sync fields we care about, snapshotted before any patching so
     mid-stream changes (which are invisible today) can be logged */
  struct TrueHDMajorSyncFields
  {
    uint8_t ratebits;
    uint8_t numSubstreams;
    uint8_t substreamInfo;
    uint8_t control2ch;
    uint8_t control6ch;
    uint8_t control8ch;
    uint8_t dialNorm2ch;
    uint8_t dialNorm6ch;
    uint8_t dialNorm8ch;
    uint8_t extPresent;
    uint8_t extLength;
    uint8_t dialNorm16ch;
    uint8_t mixLevel16ch;
    uint8_t channelCount16ch;
  };
  TrueHDMajorSyncFields m_thdFields{};
  bool m_thdFieldsValid = false;
  unsigned int m_thdFieldChanges = 0;

  void GetPacket(uint8_t **buffer, unsigned int *bufferSize);
  void DefeatAC3DialNorm(uint8_t* data, unsigned int size);
  void DefeatDTSDialNorm(uint8_t* data, unsigned int size);
  unsigned int DetectType(uint8_t *data, unsigned int size);
  bool TrySyncAC3(uint8_t *data, unsigned int size, bool resyncing, bool wantEAC3dependent);
  unsigned int SyncAC3(uint8_t *data, unsigned int size);
  unsigned int SyncDTS(uint8_t *data, unsigned int size);
  unsigned int SyncTrueHD(uint8_t *data, unsigned int size);

  static unsigned int GetTrueHDChannels(const uint16_t chanmap);
};