/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "BlurayIsoCache.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <new>
#include <system_error>

CBlurayIsoCache::CBlurayIsoCache(int64_t sourceLength, ReadCallback read, Config config)
  : m_length(sourceLength), m_read(std::move(read)), m_config(config)
{
  m_config.pageSize = std::clamp<size_t>(m_config.pageSize, BLOCK_SIZE, 1024 * 1024);
  m_config.pageSize -= m_config.pageSize % BLOCK_SIZE;
  m_config.maxBytes = std::clamp<size_t>(m_config.maxBytes, m_config.pageSize, 1024 * 1024 * 1024);
  m_maxPages = m_config.maxBytes / m_config.pageSize;
  m_config.forwardPrefetchPages = std::min({m_config.forwardPrefetchPages, size_t{16}, m_maxPages - 1});
  m_config.enabled = m_config.enabled && m_length > 0 && static_cast<bool>(m_read);
  if (m_config.enabled && m_config.forwardPrefetchPages)
  {
    try
    {
      m_worker = std::thread(&CBlurayIsoCache::Worker, this);
    }
    catch (const std::system_error&)
    {
      m_config.forwardPrefetchPages = 0; // Demand reads remain usable.
    }
  }
}

CBlurayIsoCache::~CBlurayIsoCache()
{
  Stop();
}

void CBlurayIsoCache::Cancel()
{
  m_cancelled = true;
  m_changed.notify_all();
}

void CBlurayIsoCache::Stop()
{
  Cancel();
  if (m_worker.joinable())
    m_worker.join();
  std::lock_guard lock(m_mutex);
  m_pages.clear();
  m_lru.clear();
  m_remaining = 0;
}

void CBlurayIsoCache::ResetAccessPattern()
{
  std::lock_guard lock(m_mutex);
  ++m_generation;
  m_remaining = 0;
  m_lastEnd = -1;
}

int64_t CBlurayIsoCache::ReadAt(int64_t offset, uint8_t* buffer, size_t bytes)
{
  size_t total = 0;
  while (total < bytes)
  {
    if (m_cancelled || !m_read)
      return -1;
    const int64_t count = m_read(offset + static_cast<int64_t>(total), buffer + total, bytes - total);
    if (count < 0 || static_cast<uint64_t>(count) > bytes - total)
      return -1;
    if (!count)
      break;
    total += static_cast<size_t>(count);
  }
  return m_cancelled ? -1 : static_cast<int64_t>(total);
}

CBlurayIsoCache::PagePtr CBlurayIsoCache::GetPage(int64_t index, uint64_t generation)
{
  {
    std::lock_guard lock(m_mutex);
    if (m_cancelled || generation != m_generation)
      return {};
    auto found = m_pages.find(index);
    if (found != m_pages.end())
    {
      m_lru.splice(m_lru.begin(), m_lru, found->second.lru);
      return found->second.page;
    }
  }
  // One page load at a time avoids duplicate demand/prefetch allocation. Never
  // hold the metadata mutex across backend I/O: cancellation/reset must progress.
  std::lock_guard load(m_loadMutex);
  {
    std::lock_guard lock(m_mutex);
    if (m_cancelled || generation != m_generation)
      return {};
    auto found = m_pages.find(index);
    if (found != m_pages.end())
    {
      m_lru.splice(m_lru.begin(), m_lru, found->second.lru);
      return found->second.page;
    }
  }
  const int64_t offset = index * static_cast<int64_t>(m_config.pageSize);
  const size_t size = static_cast<size_t>(std::min<int64_t>(m_config.pageSize, m_length - offset));
  PagePtr page;
  try
  {
    page = std::make_shared<Page>(size);
  }
  catch (const std::bad_alloc&)
  {
    return {}; // Optional caching must not prevent a direct demand read.
  }
  const int64_t count = ReadAt(offset, page->data(), size);
  if (count < 0)
    return {};
  page->resize(static_cast<size_t>(count));
  std::lock_guard lock(m_mutex);
  if (m_cancelled)
    return {};
  // A short page is a contiguous prefix, not permission to skip its suffix.
  // Do not retain transient short reads as permanent EOF in the page cache.
  if (page->size() == size && generation == m_generation)
  {
    try
    {
      m_lru.push_front(index);
    }
    catch (const std::bad_alloc&)
    {
      return page;
    }
    try
    {
      m_pages.emplace(index, Slot{page, m_lru.begin()});
    }
    catch (const std::bad_alloc&)
    {
      m_lru.pop_front();
      return page;
    }
    while (m_pages.size() > m_maxPages)
    {
      m_pages.erase(m_lru.back());
      m_lru.pop_back();
    }
  }
  return page;
}

int CBlurayIsoCache::ReadBlocks(uint8_t* buffer, int lba, int numBlocks)
{
  if (m_cancelled || !buffer || lba < 0 || numBlocks <= 0 ||
      numBlocks > static_cast<int>(std::numeric_limits<int>::max() / BLOCK_SIZE))
    return -1;
  const int64_t offset = static_cast<int64_t>(lba) * BLOCK_SIZE;
  size_t bytes = static_cast<size_t>(numBlocks) * BLOCK_SIZE;
  if (m_length > 0)
  {
    if (offset >= m_length)
      return 0;
    bytes = static_cast<size_t>(std::min<int64_t>(bytes, m_length - offset));
  }
  if (!m_config.enabled)
  {
    const int64_t count = ReadAt(offset, buffer, bytes);
    return count < 0 ? -1 : static_cast<int>(count / BLOCK_SIZE);
  }
  uint64_t generation;
  {
    std::lock_guard lock(m_mutex);
    generation = m_generation;
  }
  size_t copied = 0;
  while (copied < bytes)
  {
    if (m_cancelled)
      return -1;
    const int64_t position = offset + static_cast<int64_t>(copied);
    const int64_t index = position / static_cast<int64_t>(m_config.pageSize);
    const size_t within = static_cast<size_t>(position % m_config.pageSize);
    const auto page = GetPage(index, generation);
    if (!page || within >= page->size())
    {
      const int64_t count = ReadAt(position, buffer + copied, bytes - copied);
      if (count < 0)
        return -1;
      copied += static_cast<size_t>(count);
      break;
    }
    const size_t chunk = std::min(bytes - copied, page->size() - within);
    std::memcpy(buffer + copied, page->data() + within, chunk);
    copied += chunk;
    if (within + chunk < m_config.pageSize && copied < bytes)
    {
      // Resume at the exact missing byte, never at the next page's start.
      const int64_t count = ReadAt(offset + static_cast<int64_t>(copied), buffer + copied, bytes - copied);
      if (count < 0)
        return -1;
      copied += static_cast<size_t>(count);
      break;
    }
  }
  if (copied == bytes)
    Prefetch(offset / static_cast<int64_t>(m_config.pageSize),
             (offset + static_cast<int64_t>(copied) - 1) / static_cast<int64_t>(m_config.pageSize), generation);
  return m_cancelled ? -1 : static_cast<int>(copied / BLOCK_SIZE);
}

void CBlurayIsoCache::Prefetch(int64_t first, int64_t last, uint64_t generation)
{
  std::lock_guard lock(m_mutex);
  if (m_cancelled || generation != m_generation)
    return;
  const bool sequential = m_lastEnd < 0 || (first >= m_lastEnd && first <= m_lastEnd + 1);
  if (!sequential)
    ++m_generation; // Supersede an older, still-loading prefetch window.
  m_lastEnd = last;
  m_nextPrefetch = last + 1;
  m_remaining = sequential ? m_config.forwardPrefetchPages : 0;
  m_changed.notify_one();
}

void CBlurayIsoCache::Worker()
{
  for (;;)
  {
    int64_t index;
    uint64_t generation;
    {
      std::unique_lock lock(m_mutex);
      m_changed.wait(lock, [this] { return m_cancelled || m_remaining; });
      if (m_cancelled)
        return;
      index = m_nextPrefetch++;
      --m_remaining;
      generation = m_generation;
    }
    if (index <= (m_length - 1) / static_cast<int64_t>(m_config.pageSize))
      GetPage(index, generation);
  }
}
