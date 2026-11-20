/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "TextureCache.h"

#include "ServiceBroker.h"
#include "TextureCacheJob.h"
#include "URL.h"
#include "commons/ilog.h"
#include "filesystem/File.h"
#include "filesystem/IFileTypes.h"
#include "guilib/Texture.h"
#include "profiles/ProfileManager.h"
#include "settings/SettingsComponent.h"
#include "utils/Crc32.h"
#include "utils/Job.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/log.h"

#include <chrono>
#include <exception>
#include <mutex>
#include <string.h>

using namespace XFILE;
using namespace std::chrono_literals;

CTextureCache::CTextureCache() : CJobQueue(false, 1, CJob::PRIORITY_LOW_PAUSABLE)
{
}

CTextureCache::~CTextureCache() = default;

void CTextureCache::Initialize()
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  if (!m_database.IsOpen())
    m_database.Open();
}

void CTextureCache::Deinitialize()
{
  CancelJobs();

  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  m_database.Close();
}

bool CTextureCache::IsCachedImage(const std::string &url) const
{
  if (url.empty())
    return false;

  if (!CURL::IsFullPath(url))
    return true;

  const std::shared_ptr<CProfileManager> profileManager = CServiceBroker::GetSettingsComponent()->GetProfileManager();

  return URIUtils::PathHasParent(url, "special://skin", true) ||
      URIUtils::PathHasParent(url, "special://temp", true) ||
      URIUtils::PathHasParent(url, "resource://", true) ||
      URIUtils::PathHasParent(url, "androidapp://", true)   ||
      URIUtils::PathHasParent(url, profileManager->GetThumbnailsFolder(), true);
}

bool CTextureCache::HasCachedImage(const std::string &url)
{
  CTextureDetails details;
  std::string cachedImage(GetCachedImage(url, details));
  return (!cachedImage.empty() && cachedImage != url);
}

std::string CTextureCache::GetCachedImage(const std::string &image, CTextureDetails &details, bool trackUsage)
{
  std::string url = CTextureUtils::UnwrapImageURL(image);
  if (url.empty())
    return "";
  if (IsCachedImage(url))
    return url;

  // lookup the item in the database
  if (GetCachedTexture(url, details))
  {
    if (details.file.empty())
      return {};

    if (trackUsage)
      IncrementUseCount(details);
    return GetCachedPath(details.file);
  }
  return "";
}

bool CTextureCache::CanCacheImageURL(const CURL &url)
{
  return url.GetUserName().empty() || url.GetUserName() == "music" ||
         url.GetUserName() == "video" || url.GetUserName() == "picturefolder" ||
         StringUtils::StartsWith(url.GetUserName(), "video_") ||
         StringUtils::StartsWith(url.GetUserName(), "pvr") ||
         StringUtils::StartsWith(url.GetUserName(), "epg");
}

std::string CTextureCache::CheckCachedImage(const std::string &url, bool &needsRecaching)
{
  CTextureDetails details;
  std::string path(GetCachedImage(url, details, true));
  needsRecaching = !details.hash.empty();
  if (!path.empty())
    return path;
  return "";
}

void CTextureCache::BackgroundCacheImage(const std::string &url)
{
  if (url.empty())
    return;

  CTextureDetails details;
  std::string path(GetCachedImage(url, details));
  if (!path.empty() && details.hash.empty())
    return; // image is already cached and doesn't need to be checked further

  path = CTextureUtils::UnwrapImageURL(url);
  if (path.empty())
    return;

  // needs (re)caching
  AddJob(new CTextureCacheJob(path, details.hash));
}

bool CTextureCache::StartCacheImage(const std::string& image, uint64_t* fill /* = nullptr */)
{
  std::unique_lock<CCriticalSection> lock(m_processingSection);
  std::set<std::string>::iterator i = m_processinglist.find(image);
  if (i == m_processinglist.end())
  {
    m_processinglist.insert(image);
    m_fillIDs[image] = ++m_lastFillID;
    if (fill)
      *fill = m_lastFillID;
    return true;
  }
  return false;
}

void CTextureCache::EndFill(const std::string& url, uint64_t fill)
{
  const auto current = m_fillIDs.find(url);
  if (fill == 0 || current == m_fillIDs.end() || current->second != fill)
    return;
  m_fillIDs.erase(current);
  m_processinglist.erase(url);
}

std::string CTextureCache::CacheImage(const std::string& image,
                                      std::unique_ptr<CTexture>* texture /*= nullptr*/,
                                      CTextureDetails* details /*= nullptr*/,
                                      const std::function<bool()>& interrupt /*= {}*/)
{
  std::string url = CTextureUtils::UnwrapImageURL(image);
  if (url.empty())
    return "";

  // A waiter fetches the image itself after the request it waited for stopped on that
  // request's interrupt, at most this many times.
  constexpr int MAX_REFETCHES = 2;
  for (int attempt = 0;; ++attempt)
  {
    std::unique_lock<CCriticalSection> lock(m_processingSection);
    if (m_processinglist.find(url) == m_processinglist.end())
    {
      m_processinglist.insert(url);
      const uint64_t fill = m_fillIDs[url] = ++m_lastFillID;
      lock.unlock();
      if (attempt > 0)
      {
        // Another caller of the stopped fill may have fetched the image since this one
        // looked. Owning the fill, nothing else can cache it while this looks again.
        CTextureDetails tempDetails;
        std::string cachedpath = GetCachedImage(url, details ? *details : tempDetails, true);
        if (!cachedpath.empty())
        {
          {
            std::unique_lock<CCriticalSection> lock(m_processingSection);
            EndFill(url, fill);
          }
          m_completeEvent.Set();
          if (texture)
            *texture = CTexture::LoadFromFile(cachedpath, 0, 0);
          return cachedpath;
        }
      }
      // cache the texture directly
      CTextureCacheJob job(url);
      job.m_readInterrupt = interrupt;
      job.m_fill = fill;
      bool success = job.CacheTexture(texture);
      OnCachingComplete(success, &job, !success && interrupt && interrupt());
      if (success && details)
        *details = job.m_details;
      return success ? GetCachedPath(job.m_details.file) : "";
    }
    // Waiters count in under this lock, while the fill they wait for runs.
    const auto running = m_fillIDs.find(url);
    const uint64_t fill = running != m_fillIDs.end() ? running->second : 0;
    ++m_waiting[fill];
    lock.unlock();

    // wait for currently processing job to end.
    bool stopped = false;
    while (true)
    {
      if (interrupt && interrupt())
      {
        stopped = true;
        break;
      }
      m_completeEvent.Wait(interrupt ? 50ms : 1000ms);
      {
        // The fill it counted in for ended, also if a later one already runs.
        std::unique_lock<CCriticalSection> lock(m_processingSection);
        const auto current = m_fillIDs.find(url);
        if (current == m_fillIDs.end() || current->second != fill)
          break;
      }
    }
    bool interrupted = false;
    {
      std::unique_lock<CCriticalSection> lock(m_processingSection);
      interrupted = m_interrupted.find(fill) != m_interrupted.end();
      if (--m_waiting[fill] == 0)
      {
        m_waiting.erase(fill);
        m_interrupted.erase(fill);
      }
    }
    if (stopped)
      return "";

    CTextureDetails tempDetails;
    CTextureDetails* found = details ? details : &tempDetails;

    std::string cachedpath = GetCachedImage(url, *found, true);
    if (!cachedpath.empty())
    {
      if (texture)
        *texture = CTexture::LoadFromFile(cachedpath, 0, 0);
      return cachedpath;
    }

    // A request that failed on its own is not repeated. One whose caller gave up, such as a
    // GUI download stopped for another image, says nothing about the image: fetch it here.
    if (!interrupted || attempt >= MAX_REFETCHES)
    {
      CLog::Log(LOGDEBUG, "CTextureCache::{} - Return NULL texture because cache is not ready",
                __FUNCTION__);
      return "";
    }
  }
}

void CTextureCache::SetCachedObserver(std::function<void(const std::string& url)> observer)
{
  std::unique_lock<CCriticalSection> lock(m_observerSection);
  m_cachedObserver = std::move(observer);
}

bool CTextureCache::CacheImage(const std::string &image, CTextureDetails &details)
{
  std::string path = GetCachedImage(image, details);
  if (path.empty()) // not cached
    path = CacheImage(image, NULL, &details);

  return !path.empty();
}

void CTextureCache::ClearCachedImage(const std::string& image, bool deleteSource /*= false */)
{
  //! @todo This can be removed when the texture cache covers everything.
  const std::string url = CTextureUtils::UnwrapImageURL(image);
  std::string path = deleteSource ? url : "";
  std::string cachedFile;
  if (ClearCachedTexture(url, cachedFile))
    path = GetCachedPath(cachedFile);
  if (CFile::Exists(path))
    CFile::Delete(path);
  path = URIUtils::ReplaceExtension(path, ".dds");
  if (CFile::Exists(path))
    CFile::Delete(path);
}

bool CTextureCache::ClearCachedImage(int id)
{
  std::string cachedFile;
  if (ClearCachedTexture(id, cachedFile))
  {
    cachedFile = GetCachedPath(cachedFile);
    if (CFile::Exists(cachedFile))
      CFile::Delete(cachedFile);
    cachedFile = URIUtils::ReplaceExtension(cachedFile, ".dds");
    if (CFile::Exists(cachedFile))
      CFile::Delete(cachedFile);
    return true;
  }
  return false;
}

bool CTextureCache::GetCachedTexture(const std::string &url, CTextureDetails &details)
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  return m_database.GetCachedTexture(url, details);
}

bool CTextureCache::AddCachedTexture(const std::string &url, const CTextureDetails &details)
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  return m_database.AddCachedTexture(url, details);
}

void CTextureCache::IncrementUseCount(const CTextureDetails &details)
{
  static const size_t count_before_update = 100;
  std::unique_lock<CCriticalSection> lock(m_useCountSection);
  m_useCounts.reserve(count_before_update);
  m_useCounts.push_back(details);
  if (m_useCounts.size() >= count_before_update)
  {
    AddJob(new CTextureUseCountJob(m_useCounts));
    m_useCounts.clear();
  }
}

bool CTextureCache::SetCachedTextureValid(const std::string &url, bool updateable)
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  return m_database.SetCachedTextureValid(url, updateable);
}

bool CTextureCache::ClearCachedTexture(const std::string &url, std::string &cachedURL)
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  return m_database.ClearCachedTexture(url, cachedURL);
}

bool CTextureCache::ClearCachedTexture(int id, std::string &cachedURL)
{
  std::unique_lock<CCriticalSection> lock(m_databaseSection);
  return m_database.ClearCachedTexture(id, cachedURL);
}

std::string CTextureCache::GetCacheFile(const std::string &url)
{
  auto crc = Crc32::ComputeFromLowerCase(url);
  std::string hex = StringUtils::Format("{:08x}", crc);
  std::string hash = StringUtils::Format("{}/{}", hex[0], hex.c_str());
  return hash;
}

std::string CTextureCache::GetCachedPath(const std::string &file)
{
  const std::shared_ptr<CProfileManager> profileManager = CServiceBroker::GetSettingsComponent()->GetProfileManager();

  return URIUtils::AddFileToFolder(profileManager->GetThumbnailsFolder(), file);
}

void CTextureCache::OnCachingComplete(bool success,
                                      CTextureCacheJob* job,
                                      bool interrupted /* = false */)
{
  if (success)
  {
    if (job->m_details.hashRevalidated)
      SetCachedTextureValid(job->m_url, job->m_details.updateable);
    else
      AddCachedTexture(job->m_url, job->m_details);
  }

  { // remove from our processing list
    std::unique_lock<CCriticalSection> lock(m_processingSection);
    EndFill(job->m_url, job->m_fill);
    // Waiters count in before they wait, under this lock, so none can miss this.
    if (interrupted && job->m_fill != 0 && m_waiting.find(job->m_fill) != m_waiting.end())
      m_interrupted.insert(job->m_fill);
  }

  m_completeEvent.Set();

  if (success)
  {
    std::function<void(const std::string&)> observer;
    {
      std::unique_lock<CCriticalSection> lock(m_observerSection);
      observer = m_cachedObserver;
    }
    if (observer)
      observer(job->m_url);
  }
}

void CTextureCache::OnJobComplete(unsigned int jobID, bool success, CJob *job)
{
  // A CTextureCacheJob ends its fill itself (OnCachingComplete).
  return CJobQueue::OnJobComplete(jobID, success, job);
}

bool CTextureCache::Export(const std::string &image, const std::string &destination, bool overwrite)
{
  CTextureDetails details;
  std::string cachedImage(GetCachedImage(image, details));
  if (!cachedImage.empty())
  {
    std::string dest = destination + URIUtils::GetExtension(cachedImage);
    if (overwrite || !CFile::Exists(dest))
    {
      if (CFile::Copy(cachedImage, dest))
        return true;
      CLog::Log(LOGERROR, "{} failed exporting '{}' to '{}'", __FUNCTION__, cachedImage, dest);
    }
  }
  return false;
}

bool CTextureCache::Export(const std::string &image, const std::string &destination)
{
  CTextureDetails details;
  std::string cachedImage(GetCachedImage(image, details));
  if (!cachedImage.empty())
  {
    if (CFile::Copy(cachedImage, destination))
      return true;
    CLog::Log(LOGERROR, "{} failed exporting '{}' to '{}'", __FUNCTION__, cachedImage, destination);
  }
  return false;
}
