/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DemuxMultiSource.h"

#include "DVDDemuxUtils.h"
#include "DVDFactoryDemuxer.h"
#include "DVDInputStreams/DVDInputStream.h"
#include "ServiceBroker.h"
#include "Util.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "utils/log.h"

#include <cmath>

namespace
{
bool HasTimestamp(double time)
{
  return time != DVD_NOPTS_VALUE && std::isfinite(time);
}
} // namespace

CDemuxMultiSource::CDemuxMultiSource() = default;

CDemuxMultiSource::~CDemuxMultiSource()
{
  Dispose();
}

void CDemuxMultiSource::Abort()
{
  for (auto& iter : m_demuxerMap)
    iter.second->Abort();
}

void CDemuxMultiSource::Dispose()
{
  ClearScheduling();

  m_demuxerMap.clear();
  m_DemuxerToInputStreamMap.clear();
  m_pInput = NULL;

}

void CDemuxMultiSource::ClearScheduling()
{
  m_demuxerQueue = DemuxQueue{};
  m_retryQueue = {};
  m_retryTurn = false;
  m_readProgress.clear();
  m_nextProgressLog = {};
  ++m_scheduleGeneration;
}

void CDemuxMultiSource::EnableStream(int64_t demuxerId, int id, bool enable)
{
  auto iter = m_demuxerMap.find(demuxerId);
  if (iter != m_demuxerMap.end())
  {
    DemuxPtr demuxer = iter->second;
    demuxer->EnableStream(demuxerId, id, enable);
  }
}

void CDemuxMultiSource::Flush()
{
  for (auto& iter : m_demuxerMap)
    iter.second->Flush();
}

int CDemuxMultiSource::GetNrOfStreams() const
{
  int streamsCount = 0;
  for (auto& iter : m_demuxerMap)
    streamsCount += iter.second->GetNrOfStreams();

  return streamsCount;
}

CDemuxStream* CDemuxMultiSource::GetStream(int64_t demuxerId, int iStreamId) const
{
  auto iter = m_demuxerMap.find(demuxerId);
  if (iter != m_demuxerMap.end())
  {
    return iter->second->GetStream(demuxerId, iStreamId);
  }
  else
    return NULL;
}

std::vector<CDemuxStream*> CDemuxMultiSource::GetStreams() const
{
  std::vector<CDemuxStream*> streams;

  for (auto& iter : m_demuxerMap)
  {
    for (auto& stream : iter.second->GetStreams())
    {
      streams.push_back(stream);
    }
  }
  return streams;
}

std::string CDemuxMultiSource::GetStreamCodecName(int64_t demuxerId, int iStreamId)
{
  auto iter = m_demuxerMap.find(demuxerId);
  if (iter != m_demuxerMap.end())
  {
    return iter->second->GetStreamCodecName(demuxerId, iStreamId);
  }
  else
    return "";
};

int CDemuxMultiSource::GetStreamLength()
{
  int length = 0;
  for (auto& iter : m_demuxerMap)
  {
    length = std::max(length, iter.second->GetStreamLength());
  }

  return length;
}

bool CDemuxMultiSource::Open(const std::shared_ptr<CDVDInputStream>& pInput)
{
  if (!pInput)
    return false;

  Dispose();
  m_pInput = std::dynamic_pointer_cast<InputStreamMultiStreams>(pInput);

  if (!m_pInput)
    return false;

  auto iter = m_pInput->m_InputStreams.begin();
  while (iter != m_pInput->m_InputStreams.end())
  {
    DemuxPtr demuxer = DemuxPtr(CDVDFactoryDemuxer::CreateDemuxer((*iter)));
    if (!demuxer)
    {
      iter = m_pInput->m_InputStreams.erase(iter);
    }
    else
    {
      SetMissingStreamDetails(demuxer);

      m_demuxerMap[demuxer->GetDemuxerId()] = demuxer;
      m_DemuxerToInputStreamMap[demuxer] = *iter;
      m_demuxerQueue.emplace(-1.0, demuxer);
      ++iter;
    }
  }
  return !m_demuxerMap.empty();
}

bool CDemuxMultiSource::Reset()
{
  ClearScheduling();
  bool ret = true;
  for (auto& iter : m_demuxerMap)
  {
    if (!iter.second->Reset())
      ret = false;
    else
      m_retryQueue.push(iter.second);
  }
  return ret;
}

DemuxPacket* CDemuxMultiSource::Read()
{
  if (m_demuxerQueue.empty() && m_retryQueue.empty())
    return NULL;

  const bool retry = !m_retryQueue.empty() && (m_retryTurn || m_demuxerQueue.empty());
  const double priority = retry ? DVD_NOPTS_VALUE : m_demuxerQueue.top().first;
  DemuxPtr currentDemuxer;
  if (retry)
  {
    currentDemuxer = m_retryQueue.front();
    m_retryQueue.pop();
  }
  else
  {
    currentDemuxer = m_demuxerQueue.top().second;
    m_demuxerQueue.pop();
  }
  m_retryTurn = !retry;

  if (!currentDemuxer)
    return NULL;

  const bool log = CServiceBroker::GetLogging().IsLogLevelLogged(LOGDEBUG);
  const auto started = log ? std::chrono::steady_clock::now()
                           : std::chrono::steady_clock::time_point{};
  if (log && started >= m_nextProgressLog)
    CLog::Log(LOGDEBUG,
              "DemuxMultiSource: read-enter generation={} source={} queue={} priority={}",
              m_scheduleGeneration, currentDemuxer->GetDemuxerId(), retry ? "retry" : "timestamp",
              priority);
  bool inputEof = false;
  DemuxPacket* packet = currentDemuxer->Read();
  if (packet)
  {
    if (HasTimestamp(packet->dts))
      m_demuxerQueue.emplace(packet->dts, currentDemuxer);
    else if (HasTimestamp(packet->pts))
      m_demuxerQueue.emplace(packet->pts, currentDemuxer);
    else
      m_retryQueue.push(currentDemuxer);
  }
  else
  {
    auto input = m_DemuxerToInputStreamMap.find(currentDemuxer);
    if (input != m_DemuxerToInputStreamMap.end())
    {
      inputEof = input->second->IsEOF();
      if (inputEof)
      {
        CLog::Log(LOGDEBUG, "DemuxMultiSource: source={} input EOF, removed from scheduling",
                  currentDemuxer->GetDemuxerId());
      }
      else
        m_retryQueue.push(currentDemuxer);
    }
  }

  if (log)
    LogRead(currentDemuxer, packet, inputEof, started);
  return packet;
}

void CDemuxMultiSource::LogRead(const DemuxPtr& demuxer, const DemuxPacket* packet, bool inputEof,
                              std::chrono::steady_clock::time_point started)
{
  auto& progress = m_readProgress[demuxer];
  ++progress.reads;
  const bool untimed = packet && !HasTimestamp(packet->dts) && !HasTimestamp(packet->pts);
  if (untimed)
    ++progress.untimed;
  if (!packet)
    ++progress.nulls;
  progress.dts = packet ? packet->dts : DVD_NOPTS_VALUE;
  progress.pts = packet ? packet->pts : DVD_NOPTS_VALUE;
  progress.stream = packet ? packet->iStreamId : -1;
  progress.bytes = packet ? packet->iSize : 0;
  progress.packet = packet != nullptr;
  progress.inputEof = inputEof;

  const auto now = std::chrono::steady_clock::now();
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - started).count();
  const uint64_t anomalies = progress.untimed + progress.nulls;
  // First eight anomalies, then powers of two; never log every retry forever.
  if (((untimed || !packet) && (anomalies <= 8 || (anomalies & (anomalies - 1)) == 0)) ||
      (elapsed >= 250 && now >= m_nextProgressLog))
  {
    CLog::Log(LOGDEBUG,
              "DemuxMultiSource: generation={} source={} read={} packet={} stream={} bytes={} dts={} pts={} "
              "dts_valid={} pts_valid={} next={} input_eof={} read_ms={}",
              m_scheduleGeneration, demuxer->GetDemuxerId(), progress.reads, packet != nullptr,
              packet ? packet->iStreamId : -1, packet ? packet->iSize : 0,
              packet ? packet->dts : DVD_NOPTS_VALUE, packet ? packet->pts : DVD_NOPTS_VALUE,
              packet && HasTimestamp(packet->dts), packet && HasTimestamp(packet->pts),
              inputEof ? "removed" : (untimed || !packet ? "retry" : "timestamp"), inputEof,
              elapsed);
  }
  if (now >= m_nextProgressLog)
  {
    m_nextProgressLog = now + std::chrono::seconds(5);
    for (const auto& entry : m_demuxerMap)
    {
      bool video = false;
      bool audio = false;
      for (const auto* stream : entry.second->GetStreams())
      {
        video |= stream->type == STREAM_VIDEO;
        audio |= stream->type == STREAM_AUDIO;
      }
      const auto& state = m_readProgress[entry.second];
      CLog::Log(LOGDEBUG,
                "DemuxMultiSource: progress generation={} source={} video={} audio={} reads={} untimed={} "
                "nulls={} packet={} stream={} bytes={} dts={} pts={} input_eof={} "
                "timed_queue={} retry_queue={}",
                m_scheduleGeneration, entry.first, video, audio, state.reads, state.untimed, state.nulls,
                state.packet, state.stream, state.bytes, state.dts, state.pts, state.inputEof,
                m_demuxerQueue.size(), m_retryQueue.size());
    }
  }
}

bool CDemuxMultiSource::SeekTime(double time, bool backwards, double* startpts)
{
  ClearScheduling();
  bool ret = false;
  for (auto& iter : m_demuxerMap)
  {
    double sourceStart = DVD_NOPTS_VALUE;
    if (iter.second->SeekTime(time, false, &sourceStart))
    {
      if (HasTimestamp(sourceStart))
        m_demuxerQueue.emplace(sourceStart, iter.second);
      else
        m_retryQueue.push(iter.second);
      if (startpts)
        *startpts = sourceStart;
      CLog::Log(LOGDEBUG, "{} - starting demuxer from: {:f}", __FUNCTION__, time);
      ret = true;
    }
    else
    {
      CLog::Log(LOGDEBUG, "{} - failed to start demuxing from: {:f}", __FUNCTION__, time);
    }
  }
  return ret;
}

void CDemuxMultiSource::SetMissingStreamDetails(const DemuxPtr& demuxer)
{
  std::string baseFileName = m_pInput->GetFileName();
  std::string fileName = demuxer->GetFileName();
  for (auto& stream : demuxer->GetStreams())
  {
    ExternalStreamInfo info = CUtil::GetExternalStreamDetailsFromFilename(baseFileName, fileName);

    if (stream->flags == StreamFlags::FLAG_NONE)
    {
      stream->flags = static_cast<StreamFlags>(info.flag);
    }
    if (stream->language.empty())
    {
      stream->language = info.language;
    }
  }
}
