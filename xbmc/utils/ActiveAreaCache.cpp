/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ActiveAreaCache.h"

#include "FileItem.h"
#include "URL.h"
#include "filesystem/CurlFile.h"
#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "utils/Digest.h"
#include "utils/URIUtils.h"

#include <algorithm>
#include <mutex>
#include <sstream>
#include <sys/stat.h>

namespace ACTIVE_AREA_CACHE
{
namespace
{
// Bump when detection policy or the meaning of stored geometry changes.
constexpr int CACHE_VERSION = 4;
constexpr auto CACHE_DIRECTORY = "special://profile/active-area-cache/";
std::mutex maintenanceMutex;

bool ReadVersion(const std::string& path, int& version)
{
  XFILE::CFile file;
  if (!file.Open(path))
    return false;
  char buffer[32];
  const auto length = file.Read(buffer, sizeof(buffer));
  if (length <= 0)
    return false;
  std::istringstream input(std::string(buffer, static_cast<size_t>(length)));
  return static_cast<bool>(input >> version);
}

void CleanObsoleteEntries(const std::string& directory)
{
  std::lock_guard<std::mutex> lock(maintenanceMutex);
  const std::string marker = directory + "version";
  int version;
  // Resolve the active profile on each call; a process-wide once flag would
  // miss profile changes. A current marker avoids enumerating its cache again.
  if (ReadVersion(marker, version) && version == CACHE_VERSION)
    return;
  if (!XFILE::CDirectory::Create(directory))
    return;
  CFileItemList items;
  if (!XFILE::CDirectory::GetDirectory(directory, items, ".dat",
                                       XFILE::DIR_FLAG_NO_FILE_DIRS | XFILE::DIR_FLAG_NO_FILE_INFO |
                                           XFILE::DIR_FLAG_BYPASS_CACHE))
    return;
  bool complete = true;
  for (const auto& item : items.GetList())
  {
    const auto name = URIUtils::GetFileName(item->GetPath());
    const auto hex = [](unsigned char c) {
      return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    };
    const bool legacy = name.size() == 68 && name.substr(64) == ".dat" &&
                        std::all_of(name.begin(), name.begin() + 64, hex);
    const bool current = name.size() == 133 && name[64] == '-' && name.substr(129) == ".dat" &&
                         std::all_of(name.begin(), name.begin() + 64, hex) &&
                         std::all_of(name.begin() + 65, name.begin() + 129, hex);
    if (item->m_bIsFolder || (!legacy && !current))
      continue;
    if ((!ReadVersion(item->GetPath(), version) || version != CACHE_VERSION) &&
        !XFILE::CFile::Delete(item->GetPath()))
      complete = false;
  }
  if (!complete)
    return; // Leave the old marker so a later cache access retries cleanup.

  const auto temporary = marker + ".tmp";
  const auto data = std::to_string(CACHE_VERSION) + '\n';
  XFILE::CFile file;
  if (!file.OpenForWrite(temporary, true))
    return;
  const bool written = file.Write(data.data(), data.size()) == static_cast<ssize_t>(data.size());
  file.Close();
  if (!written || !XFILE::CFile::Rename(temporary, marker))
    XFILE::CFile::Delete(temporary);
}

std::string StablePath(const std::string& path)
{
  const CURL url(path);
  if (!url.IsProtocol("http") && !url.IsProtocol("https"))
    return path;
  // Only Plex original-media parts have proven non-content session fields.
  auto file = url.GetFileName();
  if (!file.empty() && file.front() == '/')
    file.erase(0, 1);
  const std::string prefix = "library/parts/";
  if (file.compare(0, prefix.size(), prefix) != 0)
    return path;
  size_t part = prefix.size();
  for (int component = 0; component < 2; ++component)
  {
    const auto end = file.find('/', part);
    if (end == std::string::npos || end == part ||
        !std::all_of(file.begin() + part, file.begin() + end,
                     [](unsigned char c) { return c >= '0' && c <= '9'; }))
      return path;
    part = end + 1;
  }
  const auto query = path.find('?');
  const auto suffix = path.find_first_of("#|");
  if (query == std::string::npos || (suffix != std::string::npos && suffix < query))
    return path;
  const auto end = suffix == std::string::npos ? path.size() : suffix;
  std::string options;
  bool retained = false;
  for (size_t start = query + 1; start <= end;)
  {
    auto next = path.find('&', start);
    if (next == std::string::npos || next > end)
      next = end;
    const auto option = path.substr(start, next - start);
    const auto name = CURL::Decode(option.substr(0, option.find('=')));
    if (name != "X-Plex-Session-Id" && name != "X-Plex-Session-Identifier")
    {
      if (retained)
        options += '&';
      options += option;
      retained = true;
    }
    if (next == end)
      break;
    start = next + 1;
  }
  // Do not round-trip options through a map: duplicate parameters, escaping,
  // order, authorization and protocol/header options must remain distinct.
  return path.substr(0, query) + (retained ? "?" + options : "") + path.substr(end);
}

bool StrongETag(const std::string& tag)
{
  return tag.size() >= 2 && tag.front() == '"' && tag.back() == '"' &&
         std::all_of(tag.begin() + 1, tag.end() - 1, [](unsigned char c) {
           return c == 0x21 || (c >= 0x23 && c != 0x7f);
         });
}

std::string SourceKey(const std::string& path)
{
  using KODI::UTILITY::CDigest;
  return CDigest::Calculate(CDigest::Type::SHA256, StablePath(path));
}

std::string Key(const Identity& identity, const std::string& directory)
{
  // Source URLs may contain credentials. Persist only a digest of the key.
  const auto key = StablePath(identity.path) + '\n' + std::to_string(identity.streamIndex) + '\n' +
                   std::to_string(identity.codec) + '\n' + std::to_string(identity.width) + '\n' +
                   std::to_string(identity.height) + '\n' + std::to_string(identity.nativeDV);
  using KODI::UTILITY::CDigest;
  return directory + SourceKey(identity.path) + "-" +
         CDigest::Calculate(CDigest::Type::SHA256, key) + ".dat";
}

bool Valid(const Identity& identity, const Record& record)
{
  const auto validOffsets = [&identity](const auto& offsets) {
    return offsets[0] + offsets[1] < identity.height &&
           offsets[2] + offsets[3] < identity.width;
  };
  return (record.hasStrict ? record.state == 3
                           : (record.state == 1 || record.state == 2 || record.state == 4)) &&
         (record.hasStrict || record.hasSubtitle) &&
         (!record.highConfidence || record.hasStrict) &&
         (!record.hasStrict || validOffsets(record.strict)) &&
         (!record.hasSubtitle || validOffsets(record.subtitle));
}
bool ReadEntry(const std::string& path, Identity& stored, Record& candidate)
{
  XFILE::CFile file;
  if (!file.Open(path))
    return false;
  const auto length = file.GetLength();
  if (length <= 0 || length > 512)
    return false;
  std::string data(static_cast<size_t>(length), '\0');
  if (file.Read(data.data(), data.size()) != length)
    return false;
  std::istringstream input(data);
  int version;
  std::string sourceKey;
  if (!(input >> version >> sourceKey >> stored.streamIndex >> stored.codec >> stored.width >>
        stored.height >> stored.nativeDV >> candidate.uniqueStream >> stored.size >> stored.modified >>
        stored.created >> stored.device >> stored.inode >> stored.validator >> candidate.state >>
        candidate.hasStrict >> candidate.hasSubtitle >> candidate.highConfidence) ||
      version != CACHE_VERSION || sourceKey != SourceKey(stored.path) ||
      stored.streamIndex < 0 || stored.codec <= 0 || stored.width < 64 || stored.height < 64 ||
      stored.width > 65535 || stored.height > 65535)
    return false;
  if (stored.validator == "-")
    stored.validator.clear();
  for (auto& offset : candidate.strict)
    if (!(input >> offset))
      return false;
  for (auto& offset : candidate.subtitle)
    if (!(input >> offset))
      return false;
  input >> std::ws;
  return input.eof() && Valid(stored, candidate);
}

} // namespace

bool Identity::operator==(const Identity& other) const
{
  return path == other.path && size == other.size && modified == other.modified &&
         created == other.created && device == other.device && inode == other.inode &&
         validator == other.validator && streamIndex == other.streamIndex &&
         codec == other.codec && width == other.width &&
         height == other.height && nativeDV == other.nativeDV;
}

bool ReadIdentity(Identity& identity)
{
  if (identity.path.empty() || identity.streamIndex < 0 || identity.codec <= 0 ||
      identity.width < 64 || identity.height < 64 || identity.width > 65535 ||
      identity.height > 65535)
    return false;
  return ReadContentIdentity(identity);
}

bool ReadContentIdentity(Identity& identity)
{
  if (identity.path.empty())
    return false;
  struct __stat64 info{};
  identity.validator.clear();
  const CURL url(identity.path);
  int status;
  if (url.IsProtocol("http") || url.IsProtocol("https"))
  {
    XFILE::CCurlFile file;
    status = file.Stat(url, &info);
    const auto tag = file.GetHttpHeader().GetValue("etag");
    if (status == 0 && StrongETag(tag))
      identity.validator = KODI::UTILITY::CDigest::Calculate(
          KODI::UTILITY::CDigest::Type::SHA256, tag);
  }
  else
    status = XFILE::CFile::Stat(identity.path, &info);
  if (status != 0 || (info.st_mode & S_IFDIR) || info.st_size <= 0 ||
      (info.st_mtime <= 0 && identity.validator.empty()))
    return false;
  identity.size = info.st_size;
  identity.modified = info.st_mtime;
  identity.created = info.st_ctime;
  identity.device = info.st_dev;
  identity.inode = info.st_ino;
  return true;
}

bool Load(const Identity& identity, Record& record)
{
  // Freeze profile resolution for the entire operation. Directory results and
  // later deletes/renames must not move to another profile during a switch.
  const auto directory = CSpecialProtocol::TranslatePath(CACHE_DIRECTORY);
  if (directory.empty())
    return false;
  CleanObsoleteEntries(directory);
  Identity stored = identity;
  Record candidate;
  if (!ReadEntry(Key(identity, directory), stored, candidate) || !(stored == identity))
    return false;
  record = candidate;
  return true;
}

bool Find(const std::string& path, int streamIndex, Identity& identity, Record& record)
{
  const auto directory = CSpecialProtocol::TranslatePath(CACHE_DIRECTORY);
  if (directory.empty())
    return false;
  CleanObsoleteEntries(directory);
  CFileItemList items;
  if (!XFILE::CDirectory::GetDirectory(directory, items, ".dat",
          XFILE::DIR_FLAG_NO_FILE_DIRS | XFILE::DIR_FLAG_NO_FILE_INFO | XFILE::DIR_FLAG_BYPASS_CACHE))
    return false;
  bool found = false;
  const auto prefix = SourceKey(path) + "-";
  size_t examined = 0;
  for (const auto& item : items.GetList())
  {
    if (item->m_bIsFolder || URIUtils::GetFileName(item->GetPath()).compare(0, prefix.size(), prefix) != 0)
      continue;
    if (++examined > 4096)
      break; // bound per-source corrupt/duplicate records, not unrelated media
    Identity stored;
    stored.path = path;
    Record candidate;
    if (!ReadEntry(item->GetPath(), stored, candidate) ||
        (streamIndex >= 0 ? stored.streamIndex != streamIndex : !candidate.uniqueStream))
      continue;
    Identity current = stored;
    if (!ReadIdentity(current) || !(current == stored))
      continue;
    // Prefer geometry produced outside native metadata policy when both exist.
    if (!found || (identity.nativeDV && !stored.nativeDV))
    {
      identity = std::move(stored);
      record = candidate;
      found = true;
    }
  }
  return found;
}

bool Store(const Identity& identity, const Record& record)
{
  Identity current = identity;
  if (!Valid(identity, record) || !ReadIdentity(current) || !(current == identity))
    return false;
  const auto directory = CSpecialProtocol::TranslatePath(CACHE_DIRECTORY);
  if (directory.empty() || !XFILE::CDirectory::Create(directory))
    return false;
  CleanObsoleteEntries(directory);
  std::ostringstream output;
  output << CACHE_VERSION << ' ' << SourceKey(identity.path) << ' ' << identity.streamIndex << ' '
         << identity.codec << ' ' << identity.width << ' ' << identity.height << ' '
         << identity.nativeDV << ' ' << record.uniqueStream << ' ' << identity.size << ' ' << identity.modified << ' '
         << identity.created << ' ' << identity.device << ' ' << identity.inode << ' '
         << (identity.validator.empty() ? "-" : identity.validator) << ' '
         << record.state << ' ' << record.hasStrict << ' ' << record.hasSubtitle << ' '
         << record.highConfidence;
  for (const auto offset : record.strict)
    output << ' ' << offset;
  for (const auto offset : record.subtitle)
    output << ' ' << offset;
  output << '\n';
  const auto data = output.str();
  const auto path = Key(identity, directory);
  const auto temporary = path + ".tmp";
  XFILE::CFile file;
  if (!file.OpenForWrite(temporary, true))
    return false;
  const bool written = file.Write(data.data(), data.size()) == static_cast<ssize_t>(data.size());
  file.Close();
  if (!written || !XFILE::CFile::Rename(temporary, path))
  {
    XFILE::CFile::Delete(temporary);
    return false;
  }
  return true;
}
} // namespace ACTIVE_AREA_CACHE
