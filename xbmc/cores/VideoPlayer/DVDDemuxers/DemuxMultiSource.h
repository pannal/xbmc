/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDDemux.h"
#include "DVDInputStreams/InputStreamMultiSource.h"

#include <chrono>
#include <map>
#include <queue>
#include <string>
#include <utility>
#include <vector>

typedef std::shared_ptr<CDVDDemux> DemuxPtr;

struct comparator{
  bool operator()(const std::pair<double, DemuxPtr>& x, const std::pair<double, DemuxPtr>& y) const
  {
    return x.first > y.first;
  }
};

typedef std::priority_queue<std::pair<double, DemuxPtr>, std::vector<std::pair<double, DemuxPtr>>, comparator> DemuxQueue;

class CDemuxMultiSource : public CDVDDemux
{

public:
  CDemuxMultiSource();
  ~CDemuxMultiSource() override;

  bool Open(const std::shared_ptr<CDVDInputStream>& pInput);

  // implementation of CDVDDemux
  void Abort() override;
  void EnableStream(int64_t demuxerId, int id, bool enable) override;
  void Flush() override;
  int GetNrOfStreams() const override;
  CDemuxStream* GetStream(int64_t demuxerId, int iStreamId) const override;
  std::vector<CDemuxStream*> GetStreams() const override;
  std::string GetStreamCodecName(int64_t demuxerId, int iStreamId) override;
  int GetStreamLength() override;
  DemuxPacket* Read() override;
  bool Reset() override;
  bool SeekTime(double time, bool backwards = false, double* startpts = NULL) override;

protected:
  CDemuxStream* GetStream(int iStreamId) const override { return nullptr; }

private:
  void Dispose();
  void SetMissingStreamDetails(const DemuxPtr& demuxer);
  void ClearScheduling();
  void LogRead(const DemuxPtr& demuxer, const DemuxPacket* packet, bool inputEof,
               std::chrono::steady_clock::time_point started);

  struct ReadProgress
  {
    uint64_t reads{0};
    uint64_t untimed{0};
    uint64_t nulls{0};
    double dts{0};
    double pts{0};
    int stream{-1};
    int bytes{0};
    bool packet{false};
    bool inputEof{false};
  };

  std::shared_ptr<InputStreamMultiStreams> m_pInput = NULL;
  std::map<DemuxPtr, InputStreamPtr> m_DemuxerToInputStreamMap;
  DemuxQueue m_demuxerQueue;
  // Retry sources rotate independently of media timestamps. Alternate the two
  // queues so persistent empty packets cannot monopolize or lose all read slots.
  std::queue<DemuxPtr> m_retryQueue;
  bool m_retryTurn{false};
  std::map<DemuxPtr, ReadProgress> m_readProgress;
  std::chrono::steady_clock::time_point m_nextProgressLog{};
  uint64_t m_scheduleGeneration{0};
  std::map<int64_t, DemuxPtr> m_demuxerMap;
};
