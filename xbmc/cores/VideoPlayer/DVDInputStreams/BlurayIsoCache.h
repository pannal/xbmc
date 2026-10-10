/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>

// An ISO-only cache. The positioned callback must serialize its seek/read pair
// and its owner must outlive this cache, including its prefetch worker.
class CBlurayIsoCache
{
public:
  static constexpr size_t BLOCK_SIZE = 2048;
  struct Config
  {
    bool enabled{true};
    size_t pageSize{256 * 1024};
    size_t maxBytes{64 * 1024 * 1024}; // Retained pages; in-flight readers can hold extra pages.
    size_t forwardPrefetchPages{1};
  };
  using ReadCallback = std::function<int64_t(int64_t, uint8_t*, size_t)>;

  CBlurayIsoCache(int64_t sourceLength, ReadCallback read, Config config);
  ~CBlurayIsoCache();
  int ReadBlocks(uint8_t* buffer, int lba, int numBlocks);
  void ResetAccessPattern();
  // Cancellation is nonblocking. Stop/destruction joins prefetch I/O completion.
  // The owner must also retain foreground callers; backend reads have no timeout.
  void Cancel();
  void Stop();

private:
  using Page = std::vector<uint8_t>;
  using PagePtr = std::shared_ptr<Page>;
  struct Slot
  {
    PagePtr page;
    std::list<int64_t>::iterator lru;
  };
  int64_t ReadAt(int64_t offset, uint8_t* buffer, size_t bytes);
  PagePtr GetPage(int64_t index, uint64_t generation);
  void Prefetch(int64_t first, int64_t last, uint64_t generation);
  void Worker();

  const int64_t m_length;
  ReadCallback m_read;
  Config m_config;
  size_t m_maxPages;
  std::atomic_bool m_cancelled{false};
  std::mutex m_loadMutex;
  std::mutex m_mutex;
  std::condition_variable m_changed;
  std::unordered_map<int64_t, Slot> m_pages;
  std::list<int64_t> m_lru;
  uint64_t m_generation{0};
  int64_t m_lastEnd{-1};
  int64_t m_nextPrefetch{0};
  size_t m_remaining{0};
  std::thread m_worker;
};
