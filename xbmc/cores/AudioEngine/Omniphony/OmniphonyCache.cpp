/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyCache.h"

#include "FileItem.h"
#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "threads/CriticalSection.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <map>
#include <mutex>
#include <utility>
#include <vector>

#if defined(TARGET_POSIX)
#include <utime.h>
#endif

namespace ActiveAE
{

namespace
{

//! The profile's own, not the master profile's: each profile keeps what its
//! settings chose. For the master profile the two are the same folder.
constexpr const char* CACHE_DIR = "special://profile/omniphony/cache/";

//! What a stem names once `.part`, `.next` or `.again`, which the engine and
//! this cache write files through, is taken off it.
std::string Stem(std::string file)
{
  for (const char* tail : {".part", ".next", ".again"})
    if (StringUtils::EndsWith(file, tail))
      file.erase(file.size() - std::char_traits<char>::length(tail));
  return file;
}

//! The picks of each kept file - see COmniphonyCache::Pick: the epoch its
//! latest began or ended at, and how many are under way. Every beginning and
//! end moves the epoch, which is shared by all files so that a stream can take
//! it before it knows which files its settings name.
struct PickState
{
  uint64_t last{0};
  unsigned int open{0};
};

CCriticalSection g_picksLock;
std::map<std::string, PickState> g_picks;
uint64_t g_pickEpoch{0};

} // unnamed namespace

std::string COmniphonyCache::Folder()
{
  if (!XFILE::CDirectory::Exists(CACHE_DIR) && !XFILE::CDirectory::Create(CACHE_DIR))
    CLog::Log(LOGERROR, "COmniphonyCache: could not create {}", CACHE_DIR);
  return CACHE_DIR;
}

std::string COmniphonyCache::Name(const std::string& chosen)
{
  std::string name = URIUtils::GetFileName(chosen);
  const size_t dot = name.rfind('.');
  if (dot != std::string::npos && dot > 0)
    name.erase(dot);
  return name;
}

std::string COmniphonyCache::File(const std::string& name, const std::string& suffix)
{
  return std::string(CACHE_DIR) + name + suffix;
}

std::string COmniphonyCache::LocalFile(const std::string& name, const std::string& suffix)
{
  return CSpecialProtocol::TranslatePath(File(name, suffix));
}

void COmniphonyCache::Touch(const std::string& path)
{
#if defined(TARGET_POSIX)
  utime(CSpecialProtocol::TranslatePath(path).c_str(), nullptr);
#endif
}

bool COmniphonyCache::IsGridOf(const std::string& file, const std::string& name)
{
  const std::string stem = Stem(file);
  const std::string head = name + "-";
  if (!StringUtils::StartsWith(stem, head) || !StringUtils::EndsWith(stem, ".grid"))
    return false;
  const std::string rate = stem.substr(head.size(), stem.size() - head.size() - 5);
  return !rate.empty() &&
         std::all_of(rate.begin(), rate.end(), [](char c) { return StringUtils::isasciidigit(c); });
}

void COmniphonyCache::Forget(const std::string& name, const std::string& main)
{
  CFileItemList items;
  XFILE::CDirectory::GetDirectory(CACHE_DIR, items, "",
                                  XFILE::DIR_FLAG_BYPASS_CACHE | XFILE::DIR_FLAG_NO_FILE_DIRS);
  for (const auto& item : items)
  {
    const std::string file = URIUtils::GetFileName(item->GetPath());
    const std::string stem = Stem(file);
    const bool ours = stem == name + main || (main == ".sofa" && IsGridOf(file, name));
    if (ours)
      XFILE::CFile::Delete(item->GetPath());
  }
  CLog::Log(LOGINFO, "COmniphonyCache: removed {}{} and what was kept with it", name, main);
}

void COmniphonyCache::Trim(const std::string& main)
{
  // Listed afresh: the engine writes into this folder, not through Kodi.
  CFileItemList items;
  XFILE::CDirectory::GetDirectory(CACHE_DIR, items, "",
                                  XFILE::DIR_FLAG_BYPASS_CACHE | XFILE::DIR_FLAG_NO_FILE_DIRS);
  std::vector<std::pair<int64_t, std::string>> entries;
  for (const auto& item : items)
  {
    const std::string file = URIUtils::GetFileName(item->GetPath());
    if (!StringUtils::EndsWith(file, main))
      continue;
    struct __stat64 st = {};
    XFILE::CFile::Stat(item->GetPath(), &st);
    // To the nanosecond where there is one, so that two choices within a
    // second are still told apart.
#if defined(TARGET_LINUX)
    const int64_t used = static_cast<int64_t>(st.st_mtim.tv_sec) * 1000000000 + st.st_mtim.tv_nsec;
#else
    const int64_t used = static_cast<int64_t>(st.st_mtime) * 1000000000;
#endif
    entries.emplace_back(used, file.substr(0, file.size() - main.size()));
  }
  std::sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) { return a > b; });
  for (size_t i = KEEP; i < entries.size(); ++i)
    Forget(entries[i].second, main);
}

bool COmniphonyCache::Promote(const std::string& made, const std::string& file)
{
  if (XFILE::CFile::Rename(made, file))
    return true;
  CLog::Log(LOGWARNING, "COmniphonyCache: could not replace {} with {}", file, made);
  XFILE::CFile::Delete(made);
  return false;
}

COmniphonyCache::Pick::Pick(std::string file) : m_file(std::move(file))
{
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  PickState& state = g_picks[m_file];
  state.last = ++g_pickEpoch;
  ++state.open;
}

COmniphonyCache::Pick::~Pick()
{
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  PickState& state = g_picks[m_file];
  state.last = ++g_pickEpoch;
  --state.open;
}

bool COmniphonyCache::Pick::Promote(const std::string& made) const
{
  // Under the lock, so that nothing made in the background lands between the
  // check in PromoteUnlessPicked and its rename.
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  return COmniphonyCache::Promote(made, m_file);
}

uint64_t COmniphonyCache::PickEpoch()
{
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  return g_pickEpoch;
}

bool COmniphonyCache::PickedSince(const std::string& file, uint64_t epoch)
{
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  const PickState& state = g_picks[file];
  return state.open > 0 || state.last > epoch;
}

COmniphonyCache::Placed COmniphonyCache::PromoteUnlessPicked(uint64_t epoch,
                                                             const std::string& made,
                                                             const std::string& file)
{
  std::unique_lock<CCriticalSection> lock(g_picksLock);
  if (PickedSince(file, epoch))
  {
    XFILE::CFile::Delete(made);
    CLog::Log(LOGINFO, "COmniphonyCache: {} was picked meanwhile - {} not put in its place", file,
              made);
    return Placed::Picked;
  }
  return Promote(made, file) ? Placed::Done : Placed::Failed;
}

} // namespace ActiveAE
