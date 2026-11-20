/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "FileCache.h"

#include "CircularCache.h"
#include "ServiceBroker.h"
#include "URL.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "threads/SystemClock.h"
#include "threads/Thread.h"
#include "utils/log.h"

#include <mutex>

#if !defined(TARGET_WINDOWS)
#include "platform/posix/ConvUtils.h"
#endif

#include <algorithm>
#include <cassert>
#include <cerrno>
#include <limits>
#include <inttypes.h>
#include <memory>

#ifdef TARGET_POSIX
#include "platform/posix/ConvUtils.h"
#endif

using namespace XFILE;

class CWriteRate
{
public:
  CWriteRate() : m_stamp(std::chrono::steady_clock::now()), m_time(std::chrono::milliseconds(0))
  {
    m_pos   = 0;
    m_size = 0;
  }

  void Reset(int64_t pos, bool bResetAll = true)
  {
    m_stamp = std::chrono::steady_clock::now();
    m_pos   = pos;

    if (bResetAll)
    {
      m_size  = 0;
      m_time = std::chrono::milliseconds(0);
    }
  }

  uint32_t Rate(int64_t pos, uint32_t time_bias = 0)
  {
    auto ts = std::chrono::steady_clock::now();

    m_size += (pos - m_pos);
    m_time += std::chrono::duration_cast<std::chrono::milliseconds>(ts - m_stamp);
    m_pos = pos;
    m_stamp = ts;

    if (m_time == std::chrono::milliseconds(0))
      return 0;

    return static_cast<uint32_t>(1000 * (m_size / (m_time.count() + time_bias)));
  }

private:
  std::chrono::time_point<std::chrono::steady_clock> m_stamp;
  int64_t  m_pos;
  std::chrono::milliseconds m_time;
  int64_t  m_size;
};

namespace
{
int64_t SeekTarget(int64_t base, int64_t offset)
{
  if (base < 0 || offset < -base || offset > std::numeric_limits<int64_t>::max() - base)
    return -1;
  return base + offset;
}
} // namespace

CFileCache::CFileCache(const unsigned int flags)
  : CThread("FileCache"), m_fileSize(0), m_flags(flags)
{
}

CFileCache::~CFileCache()
{
  Close();
}

IFile *CFileCache::GetFileImp()
{
  return m_source.GetImplementation();
}

bool CFileCache::Open(const CURL& url)
{
  Close();

  std::unique_lock<CCriticalSection> lock(m_sync);

  m_sourcePath = url.GetRedacted();

  CLog::Log(LOGDEBUG, "CFileCache::{} - <{}> opening", __FUNCTION__, m_sourcePath);

  // Opening the source file.
  // The READ_NO_CACHE and READ_NO_BUFFER flags are required to avoid create other intances of
  // FileCache or StreamBuffer since CFile::Open is called again in loop
  if (!m_source.Open(url.Get(), READ_NO_CACHE | READ_TRUNCATED | READ_NO_BUFFER))
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> failed to open", __FUNCTION__, m_sourcePath);
    Close();
    return false;
  }

  const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  if (!settings)
    return false;

  const unsigned int cacheMemSize =
      settings->GetInt(CSettings::SETTING_FILECACHE_MEMORYSIZE) * 1024 * 1024;

  m_source.IoControl(IOCTRL_SET_CACHE, this);

  bool retry = false;
  m_source.IoControl(IOCTRL_SET_RETRY, &retry); // We already handle retrying ourselves

  // check if source can seek
  m_seekPossible = m_source.IoControl(IOCTRL_SEEK_POSSIBLE, NULL);

  // Determine the best chunk size we can use
  m_chunkSize = CFile::DetermineChunkSize(m_source.GetChunkSize(),
                                          settings->GetInt(CSettings::SETTING_FILECACHE_CHUNKSIZE));
  CLog::Log(LOGDEBUG,
            "CFileCache::{} - <{}> source chunk size is {}, setting cache chunk size to {}",
            __FUNCTION__, m_sourcePath, m_source.GetChunkSize(), m_chunkSize);

  m_fileSize = m_source.GetLength();

  if (!m_pCache)
  {
    if (cacheMemSize == 0)
    {
      // Use cache on disk
      m_pCache = std::make_unique<CSimpleFileCache>();
      m_forwardCacheSize = 0;
      m_maxForward = m_fileSize;
    }
    else
    {
      size_t cacheSize;
      if (m_fileSize > 0 && m_fileSize < cacheMemSize && !(m_flags & READ_AUDIO_VIDEO))
      {
        // Cap cache size by filesize, but not for audio/video files as those may grow.
        // We don't need to take into account READ_MULTI_STREAM here as that's only used for audio/video
        cacheSize = m_fileSize;

        // Cap chunk size by cache size
        if (m_chunkSize > cacheSize)
          m_chunkSize = cacheSize;
      }
      else
      {
        cacheSize = cacheMemSize;

        // NOTE: READ_MULTI_STREAM is only used with READ_AUDIO_VIDEO
        if (m_flags & READ_MULTI_STREAM)
        {
          // READ_MULTI_STREAM requires double buffering, so use half the amount of memory for each buffer
          cacheSize /= 2;
        }

        // Make sure cache can at least hold 2 chunks
        if (cacheSize < m_chunkSize * 2)
          cacheSize = m_chunkSize * 2;
      }

      if (m_flags & READ_MULTI_STREAM)
        CLog::Log(LOGDEBUG, "CFileCache::{} - <{}> using double memory cache each sized {} bytes",
                  __FUNCTION__, m_sourcePath, cacheSize);
      else
        CLog::Log(LOGDEBUG, "CFileCache::{} - <{}> using single memory cache sized {} bytes",
                  __FUNCTION__, m_sourcePath, cacheSize);

      const size_t back = cacheSize / 4;
      const size_t front = cacheSize - back;

      m_pCache = std::make_unique<CCircularCache>(front, back);
      m_forwardCacheSize = front;
      m_maxForward = m_forwardCacheSize;
    }

    if (m_flags & READ_MULTI_STREAM)
    {
      // If READ_MULTI_STREAM flag is set: Double buffering is required
      m_pCache = std::make_unique<CDoubleCache>(m_pCache.release());
    }
  }

  // open cache strategy
  if (!m_pCache || m_pCache->Open() != CACHE_RC_OK)
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> failed to open cache", __FUNCTION__, m_sourcePath);
    Close();
    return false;
  }

  m_pCache->ClearEndOfInput();
  m_readError = false;
  m_readPos = 0;
  m_writePos = 0;
  m_writeRate = 1024 * 1024;
  m_writeRateActual = 0;
  m_writeRateLowSpeed = 0;
  m_bFilling = true;
  m_seekEvent.Reset();
  m_seekEnded.Reset();

  // A source read that waits for the network ends when the cache thread is told to stop, so
  // StopThread() does not wait for the network timeout of a stalled source.
  std::function<bool()> stopRequested = [this] { return m_bStop.load(); };
  m_source.IoControl(IOCTRL_READ_INTERRUPT, &stopRequested);

  CThread::Create(false);

  return true;
}

void CFileCache::Process()
{
  if (!m_pCache)
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> sanity failed. no cache strategy", __FUNCTION__,
              m_sourcePath);
    return;
  }

  // create our read buffer
  std::unique_ptr<char[]> buffer(new char[m_chunkSize]);
  if (buffer == nullptr)
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> failed to allocate read buffer", __FUNCTION__,
              m_sourcePath);
    return;
  }

  const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  if (!settings)
    return;

  float readFactor = settings->GetInt(CSettings::SETTING_FILECACHE_READFACTOR) / 100.0f;

  const bool useAdaptativeReadFactor = (readFactor < 1.0f);

  CWriteRate limiter;
  CWriteRate average;
  auto nextRateLog = std::chrono::steady_clock::time_point::min();
  uint32_t lastLoggedBaseRate = 0;

  bool inputEnded = false;
  while (!m_bStop)
  {
    // At EOF or after a terminal error, only a new seek can resume the source.
    if (inputEnded && (!m_seekEvent.Wait() || m_bStop))
      break;

    // Update filesize
    m_fileSize = m_source.GetLength();

    // check for seek events
    if (inputEnded || m_seekEvent.Wait(0ms))
    {
      if (m_bStop)
        break;
      int64_t length = m_fileSize;
      int64_t target = m_seekPos;
      int64_t sourcePos = -1;
      if (m_seekFromEnd)
      {
        // The reported size can be stale, especially after parking at EOF.
        sourcePos = m_source.Seek(0, SEEK_END);
        target = SeekTarget(sourcePos, m_seekPos);
        if (sourcePos >= 0)
        {
          length = sourcePos;
          m_fileSize = length;
        }
      }

      // Read ahead for tags near a known end. Beyond the last length snapshot,
      // seek to the requested position and let the source decide.
      int64_t start = target;
      if (length > 0 && target <= length)
        start = std::min(target, std::max<int64_t>(0, length - m_chunkSize));
      const int64_t cacheMaxPos = start >= 0 ? m_pCache->CachedDataEndPosIfSeekTo(start) : -1;
      if (cacheMaxPos >= 0 && sourcePos != cacheMaxPos)
        sourcePos = m_source.Seek(cacheMaxPos, SEEK_SET);
      if (target < 0 || sourcePos != cacheMaxPos)
      {
        CLog::Log(LOGERROR, "CFileCache::{} - <{}> error {} seeking to {}. Seek returned {}",
                  __FUNCTION__, m_sourcePath, GetLastError(), cacheMaxPos, sourcePos);
        m_seekPossible = m_source.IoControl(IOCTRL_SEEK_POSSIBLE, nullptr);
        // The source may have moved even on failure. Keep the old cached bytes
        // readable, but never append from an unconfirmed source position.
        m_readError = true;
        m_pCache->EndOfInput();
        inputEnded = true;
        m_nSeekResult = -1;
        m_seekEnded.Set();
        continue;
      }

      const bool bCompleteReset = m_pCache->Reset(start);
      m_pCache->ClearEndOfInput();
      m_readError = false;
      inputEnded = false;
      m_readPos = start;
      m_writePos = m_pCache->CachedDataEndPos();
      assert(m_writePos == cacheMaxPos);
      average.Reset(m_writePos, bCompleteReset); // Can only recalculate new average from scratch after a full reset (empty cache)
      limiter.Reset(m_writePos);
      m_nSeekResult = target;
      if (bCompleteReset)
      {
        CLog::Log(LOGDEBUG,
                  "CFileCache::{} - <{}> cache completely reset for seek to position {}",
                  __FUNCTION__, m_sourcePath, start);
        m_bFilling = true;
        m_writeRateLowSpeed = 0;
      }

      m_seekEnded.Set();
    }

    // variable read factor based on cache level
    if (useAdaptativeReadFactor)
    {
      // cache level [0.0 - 1.0]
      // An unbounded disk cache has no fill fraction until the source reports a length.
      const double level = m_maxForward > 0 ? static_cast<double>(m_writePos - m_readPos) / m_maxForward
                                            : 0.0;
      readFactor = static_cast<float>(level * -2.5 + 4.0); // read factor [4.0x - 1.5x]
    }

    if (CServiceBroker::GetLogging().IsLogLevelLogged(LOGDEBUG))
    {
      const auto now = std::chrono::steady_clock::now();
      const uint32_t baseRate = m_writeRate;
      if (now >= nextRateLog || baseRate != lastLoggedBaseRate)
      {
        const auto forward = m_writePos - m_readPos;
        const float targetRate = baseRate * readFactor;
        const char* throttleState = baseRate == 0          ? "disabled"
                                    : forward < targetRate ? "bypassed (low forward buffer)"
                                                           : "enabled";
        CLog::Log(LOGDEBUG,
                  "CFileCache::Process - base rate {} B/s, read factor {:.2f}x ({}), "
                  "throttle target {:.0f} B/s ({:.2f} Mbit/s), forward {} bytes "
                  "(nominal capacity {} bytes), average cache write rate {} B/s, throttle {}",
                  baseRate, readFactor, useAdaptativeReadFactor ? "adaptive" : "fixed", targetRate,
                  static_cast<double>(targetRate) * 8.0 / 1000000.0, forward, m_maxForward,
                  m_writeRateActual, throttleState);
        nextRateLog = now + 5s;
        lastLoggedBaseRate = baseRate;
      }
    }

    while (m_writeRate)
    {
      if (m_writePos - m_readPos < m_writeRate * readFactor)
      {
        limiter.Reset(m_writePos);
        break;
      }

      if (limiter.Rate(m_writePos) < m_writeRate * readFactor)
        break;

      if (m_seekEvent.Wait(m_processWait))
      {
        if (!m_bStop)
          m_seekEvent.Set();
        break;
      }
    }

    const int64_t maxWrite = m_pCache->GetMaxWriteSize(m_chunkSize);
    int64_t maxSourceRead = m_chunkSize;
    // Shorten the final chunk, but probe the source at the reported end: it may have grown.
    if (m_fileSize > m_writePos)
      maxSourceRead = std::min(maxSourceRead, m_fileSize - m_writePos);

    /* Only read from source if there's enough write space in the cache
     * else we may keep disposing data and seeking back on (slow) source
     */
    if (maxWrite < maxSourceRead)
    {
      // Wait until sufficient cache write space is available
      m_pCache->m_space.Wait(5ms);
      continue;
    }

    ssize_t iRead = 0;
    if (maxSourceRead > 0)
      iRead = m_source.Read(buffer.get(), maxSourceRead);
    if (iRead <= 0)
    {
      // A read interrupted by StopThread() is no end of file and no source error.
      if (m_bStop)
        break;

      // Check for actual EOF and retry as long as we still have data in our cache
      if (m_writePos < m_fileSize && m_pCache->WaitForData(0, 0ms) > 0)
      {
        CLog::Log(LOGWARNING, "CFileCache::{} - <{}> source read returned {}! Will retry",
                  __FUNCTION__, m_sourcePath, iRead);

        // Wait a bit:
        if (m_seekEvent.Wait(2000ms))
        {
          if (!m_bStop)
            m_seekEvent.Set(); // hack so that later we realize seek is needed
        }

        // and retry:
        continue; // while (!m_bStop)
      }
      else
      {
        if (iRead < 0)
          CLog::Log(LOGERROR,
                    "{} - <{}> source read failed with {}!", __FUNCTION__, m_sourcePath, iRead);
        else if (m_fileSize <= 0)
          CLog::Log(LOGDEBUG,
                    "CFileCache::{} - <{}> source read didn't return any data! Hit eof(?)",
                    __FUNCTION__, m_sourcePath);
        else if (m_writePos < m_fileSize)
          CLog::Log(LOGERROR,
                    "CFileCache::{} - <{}> source read didn't return any data before eof!",
                    __FUNCTION__, m_sourcePath);
        else
          CLog::Log(LOGDEBUG, "CFileCache::{} - <{}> source read hit eof", __FUNCTION__,
                    m_sourcePath);

        m_readError = (iRead < 0);
        m_pCache->EndOfInput();
        inputEnded = true;
        continue;
      }
    }

    int iTotalWrite = 0;
    while (!m_bStop && (iTotalWrite < iRead))
    {
      int iWrite = 0;
      iWrite = m_pCache->WriteToCache(buffer.get() + iTotalWrite, iRead - iTotalWrite);

      // write should always work. all handling of buffering and errors should be
      // done inside the cache strategy. only if unrecoverable error happened, WriteToCache would return error and we break.
      if (iWrite < 0)
      {
        CLog::Log(LOGERROR, "CFileCache::{} - <{}> error writing to cache", __FUNCTION__,
                  m_sourcePath);
        m_readError = true;
        m_bStop = true;
        break;
      }
      else if (iWrite == 0)
      {
        m_pCache->m_space.Wait(5ms);
      }

      iTotalWrite += iWrite;

      // check if seek was asked. otherwise if cache is full we'll freeze.
      if (m_seekEvent.Wait(0ms))
      {
        if (!m_bStop)
          m_seekEvent.Set(); // make sure we get the seek event later.
        break;
      }
    }

    m_writePos += iTotalWrite;

    // under estimate write rate by a second, to
    // avoid uncertainty at start of caching
    m_writeRateActual = average.Rate(m_writePos, 1000);

   /* NOTE: We can only reliably test for low speed condition, when the cache is *really*
    * filling. This is because as soon as it's full the average-
    * rate will become approximately the current-rate which can flag false
    * low read-rate conditions.
    */
    if (m_bFilling && m_forwardCacheSize != 0)
    {
      const int64_t forward = m_pCache->WaitForData(0, 0ms);
      if (forward + m_chunkSize >= m_forwardCacheSize)
      {
        // The one-second bias is useful for buffering, but can make a fast
        // refill after a seek look slower than playback. Classify the source
        // using the measured rate while the cache is still filling.
        const uint32_t rate = average.Rate(m_writePos);
        if (rate < m_writeRate)
          m_writeRateLowSpeed = rate;

        m_bFilling = false;
      }
    }
  }
}

void CFileCache::OnExit()
{
  m_bStop = true;

  // make sure cache is set to mark end of file (read may be waiting).
  if (m_pCache)
    m_pCache->EndOfInput();

  // just in case someone's waiting...
  m_seekEnded.Set();
}

bool CFileCache::Exists(const CURL& url)
{
  return CFile::Exists(url.Get());
}

int CFileCache::Stat(const CURL& url, struct __stat64* buffer)
{
  return CFile::Stat(url.Get(), buffer);
}

ssize_t CFileCache::Read(void* lpBuf, size_t uiBufSize)
{
  std::unique_lock<CCriticalSection> lock(m_sync);
  if (!m_pCache)
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> sanity failed. no cache strategy!", __FUNCTION__,
              m_sourcePath);
    return -1;
  }
  int64_t iRc;

  if (uiBufSize > SSIZE_MAX)
    uiBufSize = SSIZE_MAX;

  if (uiBufSize == 0)
    return 0;

retry:
  // attempt to read
  iRc = m_pCache->ReadFromCache((char *)lpBuf, uiBufSize);
  if (iRc > 0)
  {
    m_readPos += iRc;
    return (int)iRc;
  }

  if (iRc == CACHE_RC_WOULD_BLOCK)
  {
    // Keep the read/parser in progress, but let an opted-in reader pause its
    // playback clock when downstream queues run dry during a blocked read.
    XbmcThreads::EndTime<> deadline{10s};
    do
    {
      if (m_readInterrupt && m_readInterrupt())
        return -1;
      const auto wait = m_readWaitCallback || m_readInterrupt
                            ? std::min(deadline.GetTimeLeft(), 100ms)
                            : deadline.GetTimeLeft();
      if (wait == 0ms)
      {
        iRc = CACHE_RC_TIMEOUT;
        break;
      }
      iRc = m_pCache->WaitForData(1, wait);
      if (iRc != CACHE_RC_TIMEOUT || deadline.IsTimePast())
        break;
      if (m_readWaitCallback)
      {
        try
        {
          m_readWaitCallback();
        }
        catch (...)
        {
          // An observer failure must not turn an in-progress read into an
          // input error (and hence a demux flush/end). Continue the same read.
          m_readWaitCallback = {};
          CLog::Log(LOGERROR, "CFileCache::Read - read wait callback failed");
        }
      }
    } while (!deadline.IsTimePast());
    // A wait can observe end of input just as the writer publishes its final bytes.
    // Read the cache itself before deciding how the stream ended.
    if (iRc > 0 || (iRc == 0 && m_pCache->IsEndOfInput()))
      goto retry;
  }

  if (iRc == CACHE_RC_TIMEOUT)
  {
    CLog::Log(LOGWARNING, "CFileCache::{} - <{}> timeout waiting for data", __FUNCTION__,
              m_sourcePath);
    return -1;
  }

  if (iRc == 0)
  {
    if (m_readError)
    {
      SetLastError(EIO);
      return -1;
    }
    return 0;
  }

  // unknown error code
  CLog::Log(LOGERROR, "CFileCache::{} - <{}> cache strategy returned unknown error code {}",
            __FUNCTION__, m_sourcePath, (int)iRc);
  return -1;
}

int64_t CFileCache::Seek(int64_t iFilePosition, int iWhence)
{
  std::unique_lock<CCriticalSection> lock(m_sync);

  if (!m_pCache)
  {
    CLog::Log(LOGERROR, "CFileCache::{} - <{}> sanity failed. no cache strategy!", __FUNCTION__,
              m_sourcePath);
    return -1;
  }

  const bool seekFromEnd = iWhence == SEEK_END && m_seekPossible;
  int64_t target = iFilePosition;
  if (!seekFromEnd)
  {
    if (iWhence == SEEK_END)
    {
      const int64_t fileSize = m_fileSize;
      target = fileSize > 0 ? SeekTarget(fileSize, iFilePosition) : -1;
    }
    else if (iWhence == SEEK_CUR)
    {
      if (iFilePosition == 0)
        return m_readPos;
      target = SeekTarget(m_readPos, iFilePosition);
    }
    else if (iWhence != SEEK_SET)
      return -1;

    if (target < 0)
      return -1;
    // A seek at the end of an exhausted cache can restart a growing source.
    const bool restart = m_seekPossible && m_pCache->IsEndOfInput() &&
                         target == m_pCache->CachedDataEndPos();
    if (!restart && (target == m_readPos || m_pCache->Seek(target) == target))
    {
      m_readPos = target;
      return target;
    }
  }
  if (!m_seekPossible || m_bStop)
    return -1;

  m_seekPos = target;
  m_seekFromEnd = seekFromEnd;
  m_nSeekResult = -1;
  m_seekEnded.Reset();
  m_seekEvent.Set();
  while (!m_seekEnded.Wait(100ms))
  {
    if (!CThread::IsRunning())
      return -1;
  }
  if (m_bStop || m_nSeekResult < 0)
    return -1;

  target = m_nSeekResult;
  if (m_readPos < target)
  {
    const auto remaining = static_cast<uint32_t>(target - m_readPos);
    if (m_pCache->WaitForData(remaining, 10s) < remaining || m_pCache->Seek(target) != target)
      return -1;
  }
  m_readPos = target;
  return target;
}

void CFileCache::Close()
{
  StopThread();

  std::unique_lock<CCriticalSection> lock(m_sync);
  if (m_pCache)
    m_pCache->Close();

  m_source.Close();
}

int64_t CFileCache::GetPosition()
{
  return m_readPos;
}

int64_t CFileCache::GetLength()
{
  return m_fileSize;
}

void CFileCache::StopThread(bool bWait /*= true*/)
{
  m_bStop = true;
  //Process could be waiting for seekEvent
  m_seekEvent.Set();
  CThread::StopThread(bWait);
}

const std::string CFileCache::GetProperty(XFILE::FileProperty type, const std::string &name) const
{
  if (!m_source.GetImplementation())
    return IFile::GetProperty(type, name);

  return m_source.GetImplementation()->GetProperty(type, name);
}

int CFileCache::IoControl(EIoControl request, void* param)
{
  if (request == IOCTRL_CACHE_SET_WAIT_CALLBACK)
  {
    std::unique_lock<CCriticalSection> lock(m_sync);
    m_readWaitCallback = *static_cast<std::function<void()>*>(param);
    return 0;
  }

  if (request == IOCTRL_READ_INTERRUPT)
  {
    std::unique_lock<CCriticalSection> lock(m_sync);
    m_readInterrupt = param ? *static_cast<std::function<bool()>*>(param) : std::function<bool()>{};
    return 0;
  }

  if (request == IOCTRL_CACHE_STATUS)
  {
    SCacheStatus* status = (SCacheStatus*)param;
    status->maxforward = m_maxForward;
    status->forward = m_pCache->WaitForData(0, 0ms);
    status->maxrate = m_writeRate;
    status->currate = m_writeRateActual;
    status->lowrate = m_writeRateLowSpeed;
    m_writeRateLowSpeed = 0; // Reset low speed condition
    return 0;
  }

  if (request == IOCTRL_CACHE_SETRATE)
  {
    m_writeRate = *static_cast<uint32_t*>(param);

    const double mBits = m_writeRate / 1024.0 / 1024.0 * 8.0; // Mbit/s

    // calculates wait time inversely proportional to the bitrate
    // and limited between 30 - 100 ms
    const int wait = std::clamp(static_cast<int>(110.0 - mBits), 30, 100);

    m_processWait = std::chrono::milliseconds(wait);

    CLog::Log(LOGDEBUG,
              "CFileCache::IoControl - setting base rate to {} B/s ({:.2f} Mbit/s), "
              "before read factor, with processWait of {} ms",
              m_writeRate, m_writeRate * 8.0 / 1000000.0, wait);
    return 0;
  }

  if (request == IOCTRL_SEEK_POSSIBLE)
    return m_seekPossible;

  return -1;
}
