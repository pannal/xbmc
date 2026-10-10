/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "BlurayDiscSession.h"

#include "DVDInputStreamBluray.h"
#include "DVDInputStreamFile.h"
#include "filesystem/File.h"
#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <limits>
#include <system_error>
#include <thread>

namespace
{
class CBlurayCloseQueue
{
public:
  ~CBlurayCloseQueue()
  {
    // Explicit application shutdown normally joined us with services alive.
    std::unique_lock lock(m_mutex);
    m_accepting = false;
    m_stopping = true;
    m_changed.notify_all();
    lock.unlock();
    if (m_worker.joinable())
      m_worker.join();
  }

  bool Initialize()
  {
    std::lock_guard lock(m_mutex);
    if (m_leased || m_active || m_pending != nullptr || m_worker.joinable())
      return false;
    m_accepting = true;
    m_stopping = false;
    return true;
  }

  bool Acquire(const std::atomic_bool& cancelled)
  {
    std::unique_lock lock(m_mutex);
    while (m_accepting && !cancelled && (m_leased || m_active || m_pending != nullptr))
      m_changed.wait_for(lock, std::chrono::milliseconds(50));
    if (!m_accepting || cancelled)
      return false;
    m_leased = true;
    return true;
  }

  void Release()
  {
    std::lock_guard lock(m_mutex);
    m_leased = false;
    m_changed.notify_all();
  }

  bool Submit(const std::shared_ptr<CBlurayDiscSession>& session)
  {
    std::lock_guard lock(m_mutex);
    if (!m_accepting)
      return false;
    if (!m_worker.joinable())
    {
      try
      {
        m_worker = std::thread(&CBlurayCloseQueue::Worker, this);
      }
      catch (const std::exception&)
      {
        return false;
      }
    }
    m_pending = session;
    m_changed.notify_one();
    return true;
  }

  bool Shutdown()
  {
    std::unique_lock lock(m_mutex);
    m_accepting = false;
    m_stopping = true;
    m_changed.notify_all();
    if (m_leased || m_active || m_pending != nullptr)
      return false;
    lock.unlock();
    if (m_worker.joinable())
      m_worker.join();
    return true;
  }

private:
  void Worker()
  {
    for (;;)
    {
      std::shared_ptr<CBlurayDiscSession> session;
      {
        std::unique_lock lock(m_mutex);
        m_changed.wait(lock, [this] { return m_stopping || m_pending != nullptr; });
        if (!m_pending)
          return;
        session = std::move(m_pending);
        m_active = true;
      }
      session->CloseNative();
      session.reset(); // Callback/file destructors never run under queue locks.
      {
        std::lock_guard lock(m_mutex);
        m_active = false;
        m_changed.notify_all();
      }
    }
  }

  std::mutex m_mutex;
  std::condition_variable m_changed;
  std::shared_ptr<CBlurayDiscSession> m_pending;
  std::thread m_worker;
  bool m_accepting{true};
  bool m_stopping{false};
  bool m_leased{false};
  bool m_active{false};
};

CBlurayCloseQueue& CloseQueue()
{
  static CBlurayCloseQueue queue;
  return queue;
}
} // namespace

CBlurayDiscSession::CBlurayDiscSession(CDVDInputStreamBluray* owner, uint64_t diagnostic)
  : m_owner(owner), m_diagnostic(diagnostic)
{
}

CBlurayDiscSession::~CBlurayDiscSession()
{
  CloseNative();
}

BLURAY* CBlurayDiscSession::OpenNative()
{
  if (!CloseQueue().Acquire(m_cancelled))
    return nullptr;
  m_admitted = true;
  m_bd = bd_init();
  return m_bd;
}

bool CBlurayDiscSession::OpenStream(CFileItem& item, CBlurayIsoCache::Config config)
{
  m_stream = std::make_unique<CDVDInputStreamFile>(item, XFILE::READ_TRUNCATED | XFILE::READ_BITRATE |
                                                         XFILE::READ_CHUNKED | XFILE::READ_NO_CACHE);
  if (m_cancelled || !m_stream->Open())
    return false;
  auto cache = std::make_shared<CBlurayIsoCache>(m_stream->GetLength(),
      [this](int64_t offset, uint8_t* buffer, size_t bytes) { return ReadAt(offset, buffer, bytes); }, config);
  std::atomic_store(&m_cache, cache);
  if (m_cancelled)
    cache->Cancel();
  return !m_cancelled;
}

int64_t CBlurayDiscSession::ReadAt(int64_t offset, uint8_t* buffer, size_t bytes)
{
  if (m_cancelled || bytes > static_cast<size_t>(std::numeric_limits<int>::max()))
    return -1;
  std::lock_guard lock(m_ioMutex);
  if (m_cancelled || !m_stream || m_stream->Seek(offset, SEEK_SET) != offset)
    return -1;
  const int count = m_stream->Read(buffer, static_cast<int>(bytes));
  return m_cancelled ? -1 : count;
}

int CBlurayDiscSession::ReadBlocks(uint8_t* buffer, int lba, int blocks)
{
  if (m_cancelled)
    return -1;
  const auto cache = std::atomic_load(&m_cache);
  return cache ? cache->ReadBlocks(buffer, lba, blocks) : -1;
}

void CBlurayDiscSession::ResetAccessPattern()
{
  if (const auto cache = std::atomic_load(&m_cache))
    cache->ResetAccessPattern();
}

void CBlurayDiscSession::Cancel()
{
  m_cancelled = true;
  if (const auto cache = std::atomic_load(&m_cache))
    cache->Cancel();
}

void CBlurayDiscSession::DetachOwner()
{
  std::lock_guard lock(m_overlayMutex);
  m_owner = nullptr;
}

void CBlurayDiscSession::Overlay(const BD_OVERLAY* overlay)
{
  std::lock_guard lock(m_overlayMutex);
  if (m_owner)
    m_owner->OverlayCallback(overlay);
}

#ifdef HAVE_LIBBLURAY_BDJ
void CBlurayDiscSession::OverlayARGB(const bd_argb_overlay_s* overlay)
{
  std::lock_guard lock(m_overlayMutex);
  if (m_owner)
    m_owner->OverlayCallbackARGB(overlay);
}
#endif

void CBlurayDiscSession::CloseNative()
{
  Cancel();
  DetachOwner();
  if (const auto cache = std::atomic_load(&m_cache))
    cache->Stop();
  if (m_bd)
  {
    CLog::Log(LOGINFO, "p3i-transition t_us={} disc={} close=native-begin",
              PLAYBACK_DIAGNOSTICS::NowUs(), m_diagnostic);
    // Owner gate is unlocked before native callbacks/Java locks are entered.
    bd_register_overlay_proc(m_bd, nullptr, nullptr);
#ifdef HAVE_LIBBLURAY_BDJ
    bd_register_argb_overlay_proc(m_bd, nullptr, nullptr, nullptr);
#endif
    bd_close(m_bd);
    m_bd = nullptr;
    CLog::Log(LOGINFO, "p3i-transition t_us={} disc={} close=native-returned",
              PLAYBACK_DIAGNOSTICS::NowUs(), m_diagnostic);
  }
  std::atomic_store(&m_cache, std::shared_ptr<CBlurayIsoCache>{});
  {
    // A foreground callback can still be returning from a canceled read.
    std::lock_guard lock(m_ioMutex);
    m_stream.reset();
  }
  root.clear();
  if (m_admitted)
  {
    m_admitted = false;
    CloseQueue().Release();
  }
}

void CBlurayDiscSession::Retire(std::shared_ptr<CBlurayDiscSession> session)
{
  // An aborted admission/failed bd_init has no Java session. It must not
  // replace the single leased session already pending in the close queue.
  if (session && (!session->m_bd || !CloseQueue().Submit(session)))
    session->CloseNative();
}

bool CBlurayDiscSession::Initialize()
{
  return CloseQueue().Initialize();
}

bool CBlurayDiscSession::Shutdown()
{
  return CloseQueue().Shutdown();
}
