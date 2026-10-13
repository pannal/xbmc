/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ActiveAreaCache.h"

#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "utils/Digest.h"

#include <sstream>
#include <sys/stat.h>

namespace ACTIVE_AREA_CACHE
{
namespace
{
// Bump when detection policy or the meaning of stored geometry changes.
constexpr int CACHE_VERSION = 2;
constexpr auto CACHE_DIRECTORY = "special://profile/active-area-cache/";

std::string Key(const Identity& identity)
{
  // Source URLs may contain credentials. Persist only a digest of the key.
  const auto key = identity.path + '\n' + std::to_string(identity.streamIndex) + '\n' +
                   std::to_string(identity.codec) + '\n' + std::to_string(identity.width) + '\n' +
                   std::to_string(identity.height) + '\n' + std::to_string(identity.nativeDV);
  using KODI::UTILITY::CDigest;
  return std::string(CACHE_DIRECTORY) + CDigest::Calculate(CDigest::Type::SHA256, key) + ".dat";
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
} // namespace

bool Identity::operator==(const Identity& other) const
{
  return path == other.path && size == other.size && modified == other.modified &&
         created == other.created && device == other.device && inode == other.inode &&
         streamIndex == other.streamIndex && codec == other.codec && width == other.width &&
         height == other.height && nativeDV == other.nativeDV;
}

bool ReadIdentity(Identity& identity)
{
  if (identity.path.empty() || identity.streamIndex < 0 || identity.codec <= 0 ||
      identity.width < 64 || identity.height < 64 || identity.width > 65535 ||
      identity.height > 65535)
    return false;
  struct __stat64 info{};
  if (XFILE::CFile::Stat(identity.path, &info) != 0 || (info.st_mode & S_IFDIR) ||
      info.st_size <= 0 || info.st_mtime <= 0)
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
  XFILE::CFile file;
  if (!file.Open(Key(identity)))
    return false;
  const auto length = file.GetLength();
  if (length <= 0 || length > 512)
    return false;
  std::string data(static_cast<size_t>(length), '\0');
  if (file.Read(data.data(), data.size()) != length)
    return false;
  std::istringstream input(data);
  int version;
  Identity stored = identity;
  Record candidate;
  if (!(input >> version >> stored.size >> stored.modified >> stored.created >> stored.device >>
        stored.inode >> candidate.state >> candidate.hasStrict >> candidate.hasSubtitle >>
        candidate.highConfidence) ||
      version != CACHE_VERSION)
    return false;
  for (auto& offset : candidate.strict)
    if (!(input >> offset))
      return false;
  for (auto& offset : candidate.subtitle)
    if (!(input >> offset))
      return false;
  input >> std::ws;
  if (!input.eof() || !(stored == identity) || !Valid(identity, candidate))
    return false;
  record = candidate;
  return true;
}

bool Store(const Identity& identity, const Record& record)
{
  Identity current = identity;
  if (!Valid(identity, record) || !ReadIdentity(current) || !(current == identity) ||
      !XFILE::CDirectory::Create(CACHE_DIRECTORY))
    return false;
  std::ostringstream output;
  output << CACHE_VERSION << ' ' << identity.size << ' ' << identity.modified << ' '
         << identity.created << ' ' << identity.device << ' ' << identity.inode << ' '
         << record.state << ' ' << record.hasStrict << ' ' << record.hasSubtitle << ' '
         << record.highConfidence;
  for (const auto offset : record.strict)
    output << ' ' << offset;
  for (const auto offset : record.subtitle)
    output << ' ' << offset;
  output << '\n';
  const auto data = output.str();
  const auto path = Key(identity);
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
