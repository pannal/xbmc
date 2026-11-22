/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "guilib/TextureManager.h"
#include "threads/CriticalSection.h"
#include "threads/PerformanceCores.h"
#include "utils/Job.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

class CTexture;

// A worker claims STARTED before loading. The manager claims DROPPED before
// cancelling a queued job on a cache hit or the last release without caching.
enum class DownloadStart
{
  QUEUED,
  STARTED,
  DROPPED
};

/*!
 \ingroup textures,jobs
 \brief Image loader job class

 Used by the CGUILargeTextureManager to perform asynchronous loading of textures.

 \sa CGUILargeTextureManager and CJob
 */
class CImageLoader : public CJob
{
public:
  /*!
   \param download false for a decode job, which leaves an internet image that is not
          cached to a download job (see m_needsDownload); true for that download job.
   */
  CImageLoader(const std::string& path,
               bool useCache,
               std::shared_ptr<std::atomic_bool> cancelled = {},
               std::shared_ptr<PERFORMANCE_CORES::CDecodePlacement> placement = {},
               bool download = false);
  ~CImageLoader() override;

  /*!
   \brief Work function that loads in a particular image.
   */
  bool DoWork() override;

  bool          m_use_cache; ///< Whether or not to use any caching with this image
  std::string    m_path; ///< path of image to load
  std::unique_ptr<CTexture> m_texture; ///< Texture object to load the image into \sa CTexture.
  const bool m_download;
  // Set by a decode job that failed only because the image must be downloaded,
  // with the URL the texture cache stores it under (empty without the cache).
  bool m_needsDownload{false};
  std::string m_cacheURL;
  // Decode jobs: the manager's count of texture cache reports, read before the cache check.
  std::shared_ptr<const std::atomic<uint64_t>> m_cachedGeneration;
  uint64_t m_checkedGeneration{0};
  // Download jobs: see DownloadStart.
  std::shared_ptr<std::atomic<DownloadStart>> m_start;

private:
  std::shared_ptr<std::atomic_bool> m_cancelled;
  std::shared_ptr<PERFORMANCE_CORES::CDecodePlacement> m_placement;
};

/*!
 \ingroup textures
 \brief Background texture loading manager

 Used to load textures for the user interface asynchronously, allowing fluid framerates
 while background loading textures.

 \sa IJobCallback, CGUITexture
 */
class CGUILargeTextureManager : public IJobCallback
{
public:
  CGUILargeTextureManager();
  ~CGUILargeTextureManager() override;

  /*!
   \brief Callback from CImageLoader on completion of a loaded image

   Transfers texture information from the loading job to our allocated texture list.

   \sa CImageLoader, IJobCallback
   */
  void OnJobComplete(unsigned int jobID, bool success, CJob *job) override;

  /*!
   \brief Request a texture to be loaded in the background.

   Loaded textures are reference counted, hence this call may immediately return with the texture
   object filled if the texture has been previously loaded, else will return with an empty texture
   object if it is being loaded.

   \param path path of the image to load.
   \param texture texture object to hold the resulting texture
   \param orientation orientation of resulting texture
   \param firstRequest true if this is the first time we are requesting this texture
   \param background the consumer is a background request; a new one waits while the GUI moves
   \param direct the consumer is neither a background request nor a selection image, such as a
                 list row; only counted when this is the first request
   \return true if the image exists, else false.
   \sa CGUITextureArray and CGUITexture
   */
  bool GetImage(const std::string& path,
                CTextureArray& texture,
                bool firstRequest,
                bool useCache = true,
                bool background = false,
                bool direct = false);

  // True if a direct consumer, such as a list row, holds a reference to this
  // image and was processed in this or the previous frame. A consumer in a
  // window that is not processed, such as the home screen kept in memory behind
  // the active window, does not count.
  bool HasDirectConsumer(const std::string& path);
  // A direct consumer of this image is processed in this frame.
  void DirectConsumerActive(const std::string& path);

  // Containers and group lists report each frame in which their scroller runs,
  // and controls each frame in which a non-repeating animation runs.
  // ProcessDeferred is called once after all windows/dialogs have been processed
  // for that frame.
  void SetScrolling();
  void ProcessDeferred();

  // True if background images wait for motion and the GUI moved in this or the
  // previous frame. Controls processed before the moving one see it a frame late.
  bool IsBackgroundMotion() const;

  // True if window images following the selection wait like background images.
  static bool DelaysSelectionImages();

  // Admit GPU work for an existing request. This does not acquire another reference.
  // A background consumer's demand waits while the image is held.
  void RequestImageUpload(const std::string& path,
                          bool background = false,
                          bool prefetch = false,
                          std::optional<unsigned int> requestFrame = std::nullopt);

  /*!
   \brief Request a texture to be unloaded.

   When textures are finished with, this function should be called.  This decrements the texture's
   reference count, and schedules it to be unloaded once the reference count reaches zero.  If the
   texture is still queued for loading, or is in the process of loading, the image load is cancelled.

   \param path path of the image to release.
   \param immediately if set true the image is immediately unloaded once its reference count reaches zero
                      rather than being unloaded after a delay.
   \param direct the reference was acquired by a direct consumer, see GetImage.
   */
  void ReleaseImage(const std::string& path, bool immediately = false, bool direct = false);

  /*!
   \brief Cleanup images that are no longer in use.

   Loaded textures are reference counted, and upon reaching reference count 0 through ReleaseImage()
   they are flagged as unused with the current time.  After a delay they may be unloaded, hence
   CleanupUnusedImages() should be called periodically to ensure this occurs.

   \param immediately set to true to cleanup images regardless of whether the delay has passed
   */
  void CleanupUnusedImages(bool immediately = false);

private:
  class CLargeTexture
  {
  public:
    explicit CLargeTexture(const std::string &path);
    virtual ~CLargeTexture();

    void AddRef();
    bool DecrRef(bool deleteImmediately);
    bool DeleteIfRequired(bool deleteImmediately = false);
    void SetTexture(std::shared_ptr<CTexture> texture);

    const std::string& GetPath() const { return m_path; }
    const CTextureArray& GetTexture() const { return m_texture; }
    const std::shared_ptr<std::atomic_bool>& GetCancellationToken() const { return m_cancelled; }

  private:
    friend class CGUILargeTextureManager;
    static const unsigned int TIME_TO_DELETE = 2000;

    unsigned int m_refCount;
    std::string m_path;
    CTextureArray m_texture;
    unsigned int m_timeToDelete;
    std::shared_ptr<std::atomic_bool> m_cancelled{std::make_shared<std::atomic_bool>(false)};
    // Decodes nothing draws yet run on the slow cluster; see CDecodePlacement.
    std::shared_ptr<PERFORMANCE_CORES::CDecodePlacement> m_decodePlacement{
        std::make_shared<PERFORMANCE_CORES::CDecodePlacement>()};
    // Keep decoded data private until the upload job has released its context.
    std::shared_ptr<CTexture> m_pendingTexture;
    bool m_asyncUpload{false};
    bool m_gpuDemand{false};
    unsigned int m_uploadJobID{0};
    bool m_uploadQueued{false};
    // A screen/draw request was made, rather than only speculative prefetch.
    bool m_uploadVisible{false};
    // Per GUI clock tick: nearby=1, background=2, on-screen foreground=3.
    unsigned int m_uploadPriority{0};
    unsigned int m_lastUploadRequest{0};
    bool m_useCache{true};
    unsigned int m_deferredSince{0};
    // A background consumer asked for this image, whether or not a foreground one did.
    bool m_background{false};
    bool m_backgroundTexture{false};
    // Not handed to background consumers, nor uploaded unless a foreground
    // consumer demands it, until motion stops or the request times out.
    bool m_held{false};
    // A foreground consumer requested its upload.
    bool m_foregroundDemand{false};
    // References held by direct consumers, such as list rows.
    unsigned int m_directRefs{0};
    // The frame in which a direct consumer last requested or processed it.
    unsigned int m_directFrame{0};
    // An internet image that is not cached waits for, or has, a download job.
    // Its queue entry then has no decode job to start.
    enum class Download
    {
      NONE,
      WAITING,
      RUNNING
    };
    Download m_downloadState{Download::NONE};
    // The texture cache's URL of a waiting download's image (see OnImageCached).
    std::string m_cacheURL;
  };

  static bool DeferBackgrounds();
  void QueueImage(const std::string& path,
                  bool useCache = true,
                  bool defer = false,
                  bool backgroundTexture = false,
                  bool direct = false);
  void QueueUpload(CLargeTexture* image);
  void StartNextUpload(); // m_listSection held; at most one job owns the upload context
  void RemoveUpload(CLargeTexture* image);
  void StartNextDownload(); // m_listSection held

  // Downloads run in their own jobs, at most MAX_DOWNLOADS at once and at low
  // priority, so that a slow server never holds every worker that decodes
  // cached images: the job manager starts no normal job while four jobs of any
  // priority run. A download whose image is released keeps filling the disk
  // cache, and a new request for that image takes it over, until an image
  // waits for its slot; then its read is interrupted. One without the cache is
  // stopped at once. Its callback stays, so the slot returns when the job ends.
  static constexpr std::size_t MAX_DOWNLOADS = 2;
  struct CDownload
  {
    unsigned int jobID{0};
    std::string path;
    std::string cacheURL;
    bool useCache{true};
    // The job's interrupt: set to free the slot for a waiting image, or when the
    // last consumer of a download without the cache leaves.
    std::shared_ptr<std::atomic_bool> stop{std::make_shared<std::atomic_bool>(false)};
    std::shared_ptr<std::atomic<DownloadStart>> start{
        std::make_shared<std::atomic<DownloadStart>>(DownloadStart::QUEUED)};
    // The job's; a request that takes the download over adopts it.
    std::shared_ptr<PERFORMANCE_CORES::CDecodePlacement> placement;
    // False once its image is released.
    bool wanted{true};
  };
  std::vector<CLargeTexture*> m_downloadQueue;
  std::vector<CDownload> m_downloadJobs;

  // An image that any request caches, such as a scan or a python add-on reading
  // image://, is decoded from the cache instead of waiting for a download: a
  // waiting one at once, one whose download has not started yet by dropping it,
  // one a decode job leaves just after its cache check by the reports since
  // that check. The texture cache reports through a link that outlives us.
  void OnImageCached(const std::string& url);
  void WatchCachedImages(); // m_listSection held
  bool CachedSince(uint64_t generation, const std::string& url) const; // m_listSection held
  struct CCachedLink
  {
    CCriticalSection section;
    CGUILargeTextureManager* manager{nullptr};
  };
  std::shared_ptr<CCachedLink> m_cachedLink; // under m_listSection; set with the first image
  // The texture cache's reports: their count (raised under m_listSection, read by
  // decode jobs) and the latest ones (under m_listSection).
  std::shared_ptr<std::atomic<uint64_t>> m_cachedGeneration{
      std::make_shared<std::atomic<uint64_t>>(0)};
  std::deque<std::pair<uint64_t, std::string>> m_recentCached;
  static constexpr std::size_t MAX_RECENT_CACHED = 64;

  std::vector<CLargeTexture*> m_uploadQueue;
  unsigned int m_activeUploadJobID{0};
  // Updated on the GUI thread under m_listSection. Workers never read the
  // unsynchronized global GUI animation clock when ranking queued images.
  unsigned int m_uploadFrame{0};
  static constexpr int MAX_PREFETCH_UPLOADS = 8;
  static constexpr unsigned int PREFETCH_MAX_AGE = 250;

  std::vector< std::pair<unsigned int, CLargeTexture *> > m_queued;
  std::vector<CLargeTexture *> m_allocated;
  typedef std::vector<CLargeTexture *>::iterator listIterator;
  typedef std::vector< std::pair<unsigned int, CLargeTexture *> >::iterator queueIterator;
  queueIterator StartQueuedImage(queueIterator image);

  static constexpr unsigned int MAX_DEFER_TIME = 3000;
  // A zero queued job ID is a deferred decoder, never a rejected job. The hint
  // also covers held images, and may remain true after release/promotion until
  // the next ProcessDeferred scan.
  bool m_hasDeferred{false};
  // Frames completed by ProcessDeferred, under m_listSection.
  unsigned int m_processFrame{0};
  std::atomic_bool m_scrolling{false};
  std::atomic_bool m_moved{false}; // the previous frame reported motion

  // O(1) lookup maps for path-based searches
  std::unordered_map<std::string, CLargeTexture*> m_allocatedLookup;
  std::unordered_map<std::string, CLargeTexture*> m_queuedLookup;

  CCriticalSection m_listSection;
};

