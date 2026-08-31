/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyHrtf.h"

#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "guilib/LocalizeStrings.h"
#include "utils/log.h"

#include <algorithm>
#include <cstring>
#include <vector>

namespace ActiveAE
{

namespace
{

constexpr const char* HRTF_DIR = "special://masterprofile/omniphony/";
constexpr const char* HRTF_FILE = "special://masterprofile/omniphony/hrtf.sofa";
//! Written first, promoted only once it has been checked.
constexpr const char* HRTF_TEMP = "special://masterprofile/omniphony/hrtf.sofa.part";
//! Where the staged copy came from - see StageIfChanged.
constexpr const char* HRTF_SOURCE = "special://masterprofile/omniphony/hrtf.source";

//! Every SOFA file is an HDF5 container, and every HDF5 file starts with this.
constexpr unsigned char HDF5_SIGNATURE[8] = {0x89, 'H', 'D', 'F', '\r', '\n', 0x1a, '\n'};

/*!
 * Smallest plausible set of impulse responses. A real one is measured at
 * hundreds of directions; the smallest the engine's own test corpus carries is
 * about 20 kB. Anything under this is a truncated download or the wrong file
 * with the right extension.
 */
constexpr int64_t MIN_SIZE = 8 * 1024;

//! Read in pieces so a large file never has to be resident.
constexpr size_t CHUNK = 256 * 1024;

/*!
 * \brief Whether \a needle appears anywhere in the file.
 *
 * Both markers this looks for are stored as plain text: one is a dataset name
 * in the object header, the other an attribute value. Searching for them is
 * not a substitute for parsing HDF5, and does not pretend to be - it answers
 * the two questions that separate a usable HRIR file from every other file
 * with a .sofa extension, which is what the engine's reader will not tell us.
 * Chunks overlap by the needle length so a match cannot fall down the seam.
 */
bool Contains(XFILE::CFile& file, const char* needle)
{
  const size_t len = std::strlen(needle);
  if (len == 0 || len >= CHUNK)
    return false;

  if (file.Seek(0, SEEK_SET) < 0)
    return false;

  std::vector<char> buffer(CHUNK);
  size_t carry = 0;

  while (true)
  {
    const ssize_t got = file.Read(buffer.data() + carry, CHUNK - carry);
    if (got <= 0)
      return false;

    const size_t have = carry + static_cast<size_t>(got);
    if (std::search(buffer.begin(), buffer.begin() + have, needle, needle + len) !=
        buffer.begin() + have)
      return true;

    // Keep the last len-1 bytes: a marker may straddle two reads.
    carry = std::min(have, len - 1);
    std::memmove(buffer.data(), buffer.data() + have - carry, carry);
  }
}

} // unnamed namespace

COmniphonyHrtf::Result COmniphonyHrtf::Validate(const std::string& path)
{
  XFILE::CFile file;
  if (!file.Open(path))
    return Result::NotFound;

  if (file.GetLength() < MIN_SIZE)
    return Result::TooSmall;

  // The signature, then the superblock revision immediately after it. The
  // engine's reader handles revisions 0 to 3; 4 and later are a newer HDF5
  // than it knows, and it refuses them outright.
  unsigned char header[9] = {};
  if (file.Read(header, sizeof(header)) != static_cast<ssize_t>(sizeof(header)))
    return Result::NotSofa;
  if (std::memcmp(header, HDF5_SIGNATURE, sizeof(HDF5_SIGNATURE)) != 0)
    return Result::NotSofa;
  if (header[8] > 3)
    return Result::Unreadable;

  if (!Contains(file, "Data.IR"))
    return Result::NoImpulseResponses;
  if (!Contains(file, "SimpleFreeFieldHRIR"))
    return Result::WrongConvention;

  return Result::Ok;
}

COmniphonyHrtf::Result COmniphonyHrtf::Stage(const std::string& path)
{
  if (path.empty())
  {
    Clear();
    return Result::Ok;
  }

  if (!XFILE::CDirectory::Exists(HRTF_DIR) && !XFILE::CDirectory::Create(HRTF_DIR))
  {
    CLog::Log(LOGERROR, "Omniphony: could not create {}", HRTF_DIR);
    return Result::CopyFailed;
  }

  // Copy first, check second. The chosen file may be on a share, and reading
  // it twice - once to check, once to copy - would read it twice over the
  // network and leave a window where it could change in between.
  XFILE::CFile::Delete(HRTF_TEMP);
  if (!XFILE::CFile::Copy(path, HRTF_TEMP))
  {
    CLog::Log(LOGERROR, "Omniphony: could not copy '{}' into the profile", path);
    return Result::CopyFailed;
  }

  const Result result = Validate(HRTF_TEMP);
  if (result != Result::Ok)
  {
    XFILE::CFile::Delete(HRTF_TEMP);
    CLog::Log(LOGWARNING, "Omniphony: '{}' refused as an HRTF: {}", path, static_cast<int>(result));
    return result;
  }

  // Rename over the top rather than deleting first: on this platform that is
  // one atomic replace, so there is no instant at which the listener has no
  // HRTF at all, and a rename that fails leaves the previous file untouched.
  // Only if the replace is refused - a filesystem that will not rename onto an
  // existing name - is the older, lossy order worth trying.
  if (!XFILE::CFile::Rename(HRTF_TEMP, HRTF_FILE))
  {
    XFILE::CFile::Delete(HRTF_FILE);
    if (!XFILE::CFile::Rename(HRTF_TEMP, HRTF_FILE))
    {
      XFILE::CFile::Delete(HRTF_TEMP);
      CLog::Log(LOGERROR, "Omniphony: could not put the checked HRTF in place");
      return Result::CopyFailed;
    }
  }

  CLog::Log(LOGINFO, "Omniphony: using the personal HRTF copied from '{}'", path);
  return Result::Ok;
}

COmniphonyHrtf::Result COmniphonyHrtf::StageIfChanged(const std::string& path)
{
  // What was staged last time, and from where. Both have to still hold: the
  // note without the file means the copy was removed underneath us.
  std::string was;
  if (IsStaged())
  {
    XFILE::CFile note;
    if (note.Open(HRTF_SOURCE))
    {
      char buf[1024] = {};
      const ssize_t got = note.Read(buf, sizeof(buf) - 1);
      if (got > 0)
        was.assign(buf, static_cast<size_t>(got));
    }
  }

  if (was == path)
    return Result::Ok;

  const Result result = Stage(path);
  if (result != Result::Ok)
    return result;

  XFILE::CFile note;
  if (path.empty())
  {
    XFILE::CFile::Delete(HRTF_SOURCE);
  }
  else if (note.OpenForWrite(HRTF_SOURCE, true))
  {
    note.Write(path.data(), path.size());
    note.Close();
  }
  return Result::Ok;
}

void COmniphonyHrtf::Clear()
{
  XFILE::CFile::Delete(HRTF_TEMP);
  XFILE::CFile::Delete(HRTF_SOURCE);
  if (XFILE::CFile::Exists(HRTF_FILE))
  {
    XFILE::CFile::Delete(HRTF_FILE);
    CLog::Log(LOGINFO, "Omniphony: personal HRTF discarded, using the built-in set");
  }
}

std::string COmniphonyHrtf::StagedPath()
{
  if (!XFILE::CFile::Exists(HRTF_FILE))
    return {};
  return CSpecialProtocol::TranslatePath(HRTF_FILE);
}

bool COmniphonyHrtf::IsStaged()
{
  return XFILE::CFile::Exists(HRTF_FILE);
}

std::string COmniphonyHrtf::Explain(Result result)
{
  switch (result)
  {
    case Result::Ok:
      return g_localizeStrings.Get(39333);
    case Result::NotFound:
      return g_localizeStrings.Get(39334);
    case Result::TooSmall:
      return g_localizeStrings.Get(39335);
    case Result::NotSofa:
      return g_localizeStrings.Get(39336);
    case Result::Unreadable:
      return g_localizeStrings.Get(39337);
    case Result::NoImpulseResponses:
      return g_localizeStrings.Get(39338);
    case Result::WrongConvention:
      return g_localizeStrings.Get(39339);
    case Result::CopyFailed:
      return g_localizeStrings.Get(39340);
  }
  return {};
}

} // namespace ActiveAE
