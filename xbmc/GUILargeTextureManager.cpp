/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "GUILargeTextureManager.h"

#include "ServiceBroker.h"
#include "TextureCache.h"
#include "commons/ilog.h"
#include "guilib/GUIComponent.h"
#include "guilib/Texture.h"
#include "settings/AdvancedSettings.h"
#include "settings/SettingsComponent.h"
#include "utils/JobManager.h"
#include "utils/TimeUtils.h"
#include "utils/log.h"
#include "windowing/GraphicContext.h"
#include "windowing/WinSystem.h"

#include <cassert>
#include <chrono>
#include <exception>
#include <mutex>

CImageLoader::CImageLoader(const std::string& path,
                         bool useCache,
                         std::shared_ptr<std::atomic_bool> cancelled)
  : m_path(path), m_texture(nullptr), m_cancelled(std::move(cancelled))
{
  m_use_cache = useCache;
}

CImageLoader::~CImageLoader() = default;

namespace
{
class CTextureUploadJob : public CJob
{
public:
  CTextureUploadJob(std::shared_ptr<CTexture> texture, std::shared_ptr<std::atomic_bool> cancelled)
    : m_texture(std::move(texture)), m_cancelled(std::move(cancelled))
  {
  }

  bool DoWork() override
  {
    // Never call the job manager while holding the upload context lease.
    if (ShouldCancel(0, 0) || m_cancelled->load(std::memory_order_acquire))
      return false;
    m_texture->LoadToGPUAsync(m_cancelled.get());
    return !m_cancelled->load(std::memory_order_acquire);
  }

private:
  // In particular, failed uploads retain ownership until job deletion outside
  // the callback and job-manager locks (GL deletion takes the graphics lock).
  std::shared_ptr<CTexture> m_texture;
  std::shared_ptr<std::atomic_bool> m_cancelled;
};
} // namespace

bool CImageLoader::DoWork()
{
  if (ShouldCancel(0, 0))
    return false;

  bool needsChecking = false;
  std::string loadPath;

  std::string texturePath = CServiceBroker::GetGUI()->GetTextureManager().GetTexturePath(m_path);
  if (texturePath.empty())
    return false;

  if (m_use_cache)
    loadPath = CServiceBroker::GetTextureCache()->CheckCachedImage(texturePath, needsChecking);
  else
    loadPath = texturePath;

  if (!loadPath.empty())
  {
    // direct route - load the image
    auto start = std::chrono::steady_clock::now();
    m_texture =
        CTexture::LoadFromFile(loadPath, CServiceBroker::GetWinSystem()->GetGfxContext().GetWidth(),
                               CServiceBroker::GetWinSystem()->GetGfxContext().GetHeight());

    auto end = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start);

    if (duration.count() > 100)
      CLog::Log(LOGDEBUG, "{} - took {} ms to load {}", __FUNCTION__, duration.count(), loadPath);

    if (ShouldCancel(0, 0))
      return false;

    if (m_texture)
    {
      if (needsChecking)
        CServiceBroker::GetTextureCache()->BackgroundCacheImage(texturePath);

      return true;
    }

    // Fallthrough on failure:
    CLog::Log(LOGERROR, "{} - Direct texture file loading failed for {}", __FUNCTION__, loadPath);
  }

  if (!m_use_cache)
    return false; // We're done

  // not in our texture cache or it failed to load from it, so try and load directly and then cache the result
  CServiceBroker::GetTextureCache()->CacheImage(texturePath, &m_texture);

  if (!m_texture)
    return false;

  if (ShouldCancel(0, 0))
    return false;

  return true;
}

CGUILargeTextureManager::CLargeTexture::CLargeTexture(const std::string &path):
  m_path(path)
{
  m_refCount = 1;
  m_timeToDelete = 0;
}

CGUILargeTextureManager::CLargeTexture::~CLargeTexture()
{
  assert(m_refCount == 0);
  // The job retains this token if it is still waiting for the upload context.
  m_cancelled->store(true, std::memory_order_release);
  m_texture.Free();
}

void CGUILargeTextureManager::CLargeTexture::AddRef()
{
  m_refCount++;
}

bool CGUILargeTextureManager::CLargeTexture::DecrRef(bool deleteImmediately)
{
  assert(m_refCount);
  m_refCount--;
  if (m_refCount == 0)
  {
    if (deleteImmediately)
      delete this;
    else
      m_timeToDelete = CTimeUtils::GetFrameTime() + TIME_TO_DELETE;
    return true;
  }
  return false;
}

bool CGUILargeTextureManager::CLargeTexture::DeleteIfRequired(bool deleteImmediately)
{
  if (m_refCount == 0 && (deleteImmediately || m_timeToDelete < CTimeUtils::GetFrameTime()))
  {
    delete this;
    return true;
  }
  return false;
}

void CGUILargeTextureManager::CLargeTexture::SetTexture(std::shared_ptr<CTexture> texture)
{
  assert(!m_texture.size());
  if (texture)
  {
    const auto width = texture->GetWidth();
    const auto height = texture->GetHeight();
    m_texture.Set(std::move(texture), width, height);
  }
}

CGUILargeTextureManager::CGUILargeTextureManager() = default;

CGUILargeTextureManager::~CGUILargeTextureManager() = default;

void CGUILargeTextureManager::CleanupUnusedImages(bool immediately)
{
  std::unique_lock<CCriticalSection> lock(m_listSection);
  // check for items to remove from allocated list, and remove
  listIterator it = m_allocated.begin();
  while (it != m_allocated.end())
  {
    CLargeTexture *image = *it;
    // Save path before potential deletion to avoid use-after-free
    const std::string path = image->GetPath();
    const unsigned int uploadJobID = image->m_uploadJobID;
    if (image->DeleteIfRequired(immediately))
    {
      CancelUpload(uploadJobID);
      m_allocatedLookup.erase(path);
      it = m_allocated.erase(it);
    }
    else
      ++it;
  }
}

// if available, increment reference count, and return the image.
// else, add to the queue list if appropriate.
bool CGUILargeTextureManager::GetImage(const std::string& path,
                                      CTextureArray& texture,
                                      bool firstRequest,
                                      const bool useCache,
                                      bool deferWhileScrolling)
{
  std::unique_lock<CCriticalSection> lock(m_listSection);

  // O(1) lookup in allocated textures
  auto it = m_allocatedLookup.find(path);
  if (it != m_allocatedLookup.end())
  {
    CLargeTexture *image = it->second;
    if (firstRequest)
      image->AddRef();
    texture = image->GetTexture();
    return texture.size() > 0 || image->m_pendingTexture != nullptr;
  }

  if (firstRequest)
    QueueImage(path, useCache, deferWhileScrolling);

  return true;
}

void CGUILargeTextureManager::RequestImageUpload(const std::string& path)
{
  std::unique_lock<CCriticalSection> lock(m_listSection);
  auto allocated = m_allocatedLookup.find(path);
  if (allocated != m_allocatedLookup.end())
  {
    allocated->second->m_gpuDemand = true;
    QueueUpload(allocated->second);
  }
  else
  {
    auto queued = m_queuedLookup.find(path);
    if (queued != m_queuedLookup.end())
      queued->second->m_gpuDemand = true;
  }
}

void CGUILargeTextureManager::QueueUpload(CLargeTexture* image)
{
  if (!image->m_gpuDemand || !image->m_pendingTexture || image->m_uploadJobID != 0)
    return;

  // A visible upload can run even when normal-priority decoders fill their pool.
  image->m_uploadJobID = CServiceBroker::GetJobManager()->AddJob(
      new CTextureUploadJob(image->m_pendingTexture, image->m_cancelled), this, CJob::PRIORITY_HIGH);
  if (image->m_uploadJobID == 0)
  {
    // Rejected jobs have never uploaded. Preserve the existing main-context
    // fallback, and retain the entry's reference during synchronous job deletion.
    image->SetTexture(std::move(image->m_pendingTexture));
  }
}

void CGUILargeTextureManager::CancelUpload(unsigned int jobID)
{
  if (jobID != 0)
  {
    CServiceBroker::GetJobManager()->CancelJob(jobID);
  }
}

void CGUILargeTextureManager::ReleaseImage(const std::string &path, bool immediately)
{
  std::unique_lock<CCriticalSection> lock(m_listSection);

  // O(1) lookup in allocated textures
  auto allocIt = m_allocatedLookup.find(path);
  if (allocIt != m_allocatedLookup.end())
  {
    CLargeTexture *image = allocIt->second;
    const unsigned int uploadJobID = image->m_uploadJobID;
    if (image->DecrRef(immediately) && immediately)
    {
      CancelUpload(uploadJobID);
      m_allocatedLookup.erase(allocIt);
      // Also remove from vector
      for (listIterator it = m_allocated.begin(); it != m_allocated.end(); ++it)
      {
        if (*it == image)
        {
          m_allocated.erase(it);
          break;
        }
      }
    }
    return;
  }

  // O(1) lookup in queued textures
  auto queueIt = m_queuedLookup.find(path);
  if (queueIt != m_queuedLookup.end())
  {
    CLargeTexture *image = queueIt->second;
    if (image->DecrRef(true))
    {
      // Find and cancel the job, remove from queue vector
      for (queueIterator it = m_queued.begin(); it != m_queued.end(); ++it)
      {
        if (it->second == image)
        {
          if (it->first != 0)
            CServiceBroker::GetJobManager()->CancelJob(it->first);
          m_queued.erase(it);
          break;
        }
      }
      m_queuedLookup.erase(queueIt);
    }
  }
}

// queue the image, and start the background loader if necessary
void CGUILargeTextureManager::QueueImage(const std::string& path, bool useCache, bool defer)
{
  if (path.empty())
    return;

  std::unique_lock<CCriticalSection> lock(m_listSection);

  // O(1) lookup to check if already queued
  auto it = m_queuedLookup.find(path);
  if (it != m_queuedLookup.end())
  {
    it->second->AddRef();
    if (!defer)
    {
      // A foreground consumer must not inherit another consumer's background
      // deferral. Preserve the original request's cache policy and token.
      for (auto queued = m_queued.begin(); queued != m_queued.end(); ++queued)
      {
        if (queued->second == it->second)
        {
          if (queued->first == 0)
            StartQueuedImage(queued);
          break;
        }
      }
    }
    return; // already queued
  }

  // queue the item
  CLargeTexture *image = new CLargeTexture(path);
  image->m_asyncUpload =
      CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_guiAsyncTextureUpload;
  image->m_useCache = useCache;
  image->m_deferredSince = CTimeUtils::GetFrameTime();
  // Register before admission; a callback cannot enter until this lock releases.
  m_queued.emplace_back(0, image);
  m_queuedLookup[path] = image;
  if (defer)
    m_hasDeferred = true;
  else
    StartQueuedImage(m_queued.end() - 1);
}

CGUILargeTextureManager::queueIterator CGUILargeTextureManager::StartQueuedImage(queueIterator it)
{
  CLargeTexture* image = it->second;
  it->first = CServiceBroker::GetJobManager()->AddJob(
      new CImageLoader(image->GetPath(), image->m_useCache, image->GetCancellationToken()), this,
      CJob::PRIORITY_NORMAL);
  if (it->first == 0)
  {
    // A stopped job manager cannot complete this request. Expose a load failure
    // instead of leaving a permanently queued image.
    m_allocated.push_back(image);
    m_allocatedLookup[image->GetPath()] = image;
    m_queuedLookup.erase(image->GetPath());
    return m_queued.erase(it);
  }
  return ++it;
}

void CGUILargeTextureManager::SetScrolling()
{
  m_scrolling = true;
}

void CGUILargeTextureManager::ProcessDeferred()
{
  const bool scrolling = m_scrolling.exchange(false);
  std::unique_lock<CCriticalSection> lock(m_listSection);
  if (!m_hasDeferred)
    return;

  const unsigned int now = CTimeUtils::GetFrameTime();
  bool deferred = false;
  for (auto it = m_queued.begin(); it != m_queued.end();)
  {
    if (it->first != 0)
      ++it;
    else if (scrolling && now - it->second->m_deferredSince < MAX_DEFER_TIME)
    {
      deferred = true;
      ++it;
    }
    else
      it = StartQueuedImage(it);
  }
  m_hasDeferred = deferred;
}

void CGUILargeTextureManager::OnJobComplete(unsigned int jobID, bool success, CJob *job)
{
  // see if we still have this job id
  std::unique_lock<CCriticalSection> lock(m_listSection);
  // Entries and their job IDs are protected by m_listSection. Searching here
  // avoids allocating tracking storage after a job has already been admitted.
  // A copied callback after release cannot match a replacement path's new ID.
  for (CLargeTexture* image : m_allocated)
  {
    if (image->m_uploadJobID == 0 || image->m_uploadJobID != jobID)
      continue;
    image->m_uploadJobID = 0;
    if (success)
      image->SetTexture(std::move(image->m_pendingTexture));
    else
      image->m_pendingTexture.reset(); // The job still owns any partially uploaded texture.
    return;
  }
  for (queueIterator it = m_queued.begin(); it != m_queued.end(); ++it)
  {
    if (it->first == jobID)
    { // found our job
      CImageLoader *loader = static_cast<CImageLoader*>(job);
      CLargeTexture *image = it->second;
      if (success)
      {
        if (image->m_asyncUpload)
          image->m_pendingTexture = std::move(loader->m_texture);
        else
          image->SetTexture(std::move(loader->m_texture));
      }
      // Failed jobs retain their texture until job deletion outside this lock.
      // Its GL deletion queue takes the graphics lock; rendering takes that
      // lock before m_listSection. Successful moves already empty the loader.

      // Move from queued to allocated - update both containers and lookup maps
      const std::string& path = image->GetPath();
      m_queuedLookup.erase(path);
      m_queued.erase(it);
      m_allocated.push_back(image);
      m_allocatedLookup[path] = image;
      QueueUpload(image);
      return;
    }
  }
}
