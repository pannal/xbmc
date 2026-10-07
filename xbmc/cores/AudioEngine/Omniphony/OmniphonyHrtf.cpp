/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyHrtf.h"

#include "FileItem.h"
#include "OmniphonyCache.h"
#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "guilib/LocalizeStrings.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/log.h"

namespace ActiveAE
{

namespace
{

//! The grid of \p name for streams at \p rate Hz, as a Kodi path.
std::string GridFor(const std::string& name, unsigned int rate)
{
  return COmniphonyCache::File(name, "-" + std::to_string(rate / 1000) + ".grid");
}

/*!
 * \brief What the engine says the file at \p path is, as a result: Ok for
 * an HRTF set. Describing a set hands it over whole and reads its shape and
 * identity; a few MB at most.
 */
COmniphonyHrtf::Result Check(const std::string& path, bool interactive, COmniphonyHrtf::Outcome& o)
{
  using Result = COmniphonyHrtf::Result;
  std::string failure;
  switch (COmniphonyTool::Describe(path, interactive, o.contents, failure))
  {
    case COmniphonyTool::Status::Cancelled:
      return Result::Cancelled;
    case COmniphonyTool::Status::Unreadable:
      return Result::NotFound;
    case COmniphonyTool::Status::Unavailable:
      return Result::Unavailable;
    case COmniphonyTool::Status::Failed:
      return Result::EngineFailed;
    case COmniphonyTool::Status::Done:
      break;
  }
  // The helper's words for why it could not describe the file - see its
  // --describe. An engine missing or too old is Unavailable; one that
  // failed on the file says why in contents.reason.
  if (failure == "unusable")
    return Result::Unusable;
  if (failure == "memory")
    return Result::TooLarge;
  if (failure == "input")
    return Result::ReadFailed;
  if (failure == "internal")
    return Result::EngineFailed;
  if (!failure.empty())
    return Result::Unavailable;
  o.described = true;
  if (o.contents.hrtf)
    return Result::Ok;
  return o.contents.room ? Result::RoomResponse : Result::WrongConvention;
}

//! The engine's HRTF_MAX_FILE_BYTES: 2^24 stored values of 8 bytes, plus
//! 16 MB for the rest of the file. No larger file is an HRTF set it reads.
constexpr int64_t HRTF_MAX_FILE_BYTES = (int64_t{8} << 24) + (int64_t{16} << 20);

//! \p o, refused by Check: logged, with the engine's words where it gave any.
COmniphonyHrtf::Outcome Refused(const std::string& chosen, COmniphonyHrtf::Outcome& o)
{
  using Result = COmniphonyHrtf::Result;
  o.detail = o.result == Result::Unusable || o.result == Result::EngineFailed ||
                     o.result == Result::TooLarge
                 ? o.contents.reason
                 : std::string();
  CLog::Log(LOGWARNING, "COmniphonyHrtf: '{}' refused as an HRTF set ({})", chosen,
            static_cast<int>(o.result));
  return o;
}

//! What became of building a set's grid when it was chosen.
enum class Grid
{
  Built,
  //! Not now - no helper, an engine without the call, a grid it could not
  //! write - and the first film builds it.
  Later,
  //! The engine cannot load the set: it said so, or stopped on it. A film
  //! would do no better, so the set is not kept.
  Refused,
  Cancelled,
};

/*!
 * \brief Have the engine build the GRID_RATE grid of the set at \p sofa into
 * \p grid (a Kodi path) now, behind a busy dialog, so that the first film
 * plays the set from its start rather than after the seconds its grid takes.
 * Nothing is built when \p grid already holds it. \p why takes the engine's
 * words when it refuses the set.
 */
Grid BuildGrid(const std::string& sofa, const std::string& grid, std::string& why)
{
  COmniphonyTool tool({"--prepare-hrtf", COmniphonyTool::EnginePath(),
                       CSpecialProtocol::TranslatePath(sofa), CSpecialProtocol::TranslatePath(grid),
                       std::to_string(COmniphonyHrtf::GRID_RATE), "1"},
                      {});
  const COmniphonyTool::Status status = tool.Execute(true);
  if (status == COmniphonyTool::Status::Done && tool.Word() == "prepared")
  {
    CLog::Log(LOGINFO, "COmniphonyHrtf: HRIR grid {}: {}", grid, tool.Detail());
    return Grid::Built;
  }
  // A killed engine leaves its part behind; it writes nothing in place.
  XFILE::CFile::Delete(grid + ".part");
  if (status == COmniphonyTool::Status::Cancelled)
    return Grid::Cancelled;
  if (status == COmniphonyTool::Status::Failed ||
      (status == COmniphonyTool::Status::Done && tool.Reason() == "unusable"))
  {
    why = tool.Detail();
    CLog::Log(LOGWARNING, "COmniphonyHrtf: the engine cannot load {} as an HRTF set ({})", sofa,
              why.empty() ? "it stopped without an answer" : why);
    return Grid::Refused;
  }
  CLog::Log(LOGWARNING,
            "COmniphonyHrtf: HRIR grid {} not built now ({} {}), the first film "
            "builds it",
            grid, tool.Reason(), tool.Detail());
  return Grid::Later;
}

/*!
 * \brief Every grid of \p name but \p keep: the grids of a set no longer
 * kept under that name, which no stream could use.
 */
void DiscardGrids(const std::string& name, const std::string& keep)
{
  CFileItemList items;
  XFILE::CDirectory::GetDirectory(COmniphonyCache::Folder(), items, "",
                                  XFILE::DIR_FLAG_BYPASS_CACHE | XFILE::DIR_FLAG_NO_FILE_DIRS);
  for (const auto& item : items)
  {
    const std::string file = URIUtils::GetFileName(item->GetPath());
    if (COmniphonyCache::IsGridOf(file, name) && item->GetPath() != keep)
      XFILE::CFile::Delete(item->GetPath());
  }
}

} // unnamed namespace

COmniphonyHrtf::Outcome COmniphonyHrtf::Choose(const std::string& chosen)
{
  Outcome o;
  o.name = COmniphonyCache::Name(chosen);
  const std::string sofa = COmniphonyCache::File(o.name, ".sofa");
  const std::string part = sofa + ".part";
  const std::string grid = GridFor(o.name, GRID_RATE);
  const std::string next = grid + ".next";
  COmniphonyCache::Folder();

  // A file larger than any HRTF set the engine reads (its
  // HRTF_MAX_FILE_BYTES, 144 MB) is checked where it is rather than copied:
  // it can only be refused, and a measured room chosen here is named for
  // what it is without first filling the cache with it.
  struct __stat64 st;
  if (XFILE::CFile::Stat(chosen, &st) == 0 && st.st_size > HRTF_MAX_FILE_BYTES)
  {
    o.result = Check(chosen, true, o);
    if (o.result != Result::Ok)
      return Refused(chosen, o);
  }

  // Copied first and checked as the copy: a file on a share is read once,
  // and what is kept is what was checked.
  XFILE::CFile::Delete(part);
  if (!XFILE::CFile::Copy(chosen, part))
  {
    XFILE::CFile::Delete(part);
    o.result = XFILE::CFile::Exists(chosen) ? Result::CopyFailed : Result::NotFound;
    return o;
  }
  o.result = Check(part, true, o);
  if (o.result != Result::Ok)
  {
    XFILE::CFile::Delete(part);
    return Refused(chosen, o);
  }

  // The same file chosen again: what is kept is already it. Its grid is
  // built only if it has gone since.
  Outcome kept;
  if (XFILE::CFile::Exists(sofa) && Check(sofa, false, kept) == Result::Ok &&
      !kept.contents.source.empty() && kept.contents.source == o.contents.source)
  {
    XFILE::CFile::Delete(part);
    o.result = Result::Cached;
    if (XFILE::CFile::Exists(grid, false))
      o.gridKept = true;
    else
      switch (BuildGrid(sofa, grid, o.detail))
      {
        case Grid::Built:
          o.gridKept = true;
          break;
        case Grid::Refused:
          o.result = Result::EngineFailed;
          return o;
        case Grid::Later:
        case Grid::Cancelled:
          break;
      }
    COmniphonyCache::Touch(sofa);
    CLog::Log(LOGINFO, "COmniphonyHrtf: '{}' is already kept as {}", chosen, sofa);
    return o;
  }

  // Built before the copy replaces anything, so that a cancel leaves the set
  // and grids kept under the name as they were.
  XFILE::CFile::Delete(next);
  const Grid built = BuildGrid(part, next, o.detail);
  if (built == Grid::Cancelled || built == Grid::Refused)
  {
    XFILE::CFile::Delete(part);
    XFILE::CFile::Delete(next);
    o.result = built == Grid::Cancelled ? Result::Cancelled : Result::EngineFailed;
    return o;
  }
  if (!COmniphonyCache::Promote(part, sofa))
  {
    XFILE::CFile::Delete(next);
    o.result = Result::CopyFailed;
    return o;
  }
  // The grids of what was kept under the name before go; the one just built
  // takes the place of its rate's.
  DiscardGrids(o.name, built == Grid::Built ? next : std::string());
  o.gridKept = built == Grid::Built && COmniphonyCache::Promote(next, grid);
  COmniphonyCache::Touch(sofa);
  COmniphonyCache::Trim(".sofa");
  CLog::Log(LOGINFO, "COmniphonyHrtf: '{}' kept as {}", chosen, sofa);
  return o;
}

std::string COmniphonyHrtf::ForStream(const std::string& chosen, Copy& copy)
{
  copy = Copy::Kept;
  if (chosen.empty())
    return {};
  const std::string name = COmniphonyCache::Name(chosen);
  const std::string sofa = COmniphonyCache::File(name, ".sofa");
  if (!XFILE::CFile::Exists(sofa))
  {
    // Gone from the cache since it was chosen: copied again if the file is
    // still there and still a set. No dialog - a stream is opening - and no
    // grid: the engine builds it as the film starts.
    COmniphonyCache::Folder();
    const std::string part = sofa + ".part";
    Outcome o;
    XFILE::CFile::Delete(part);
    if (!XFILE::CFile::Copy(chosen, part) || Check(part, false, o) != Result::Ok ||
        !COmniphonyCache::Promote(part, sofa))
    {
      XFILE::CFile::Delete(part);
      copy = XFILE::CFile::Exists(chosen) ? Copy::Refused : Copy::Gone;
      CLog::Log(LOGWARNING, "COmniphonyHrtf: '{}' cannot be kept again - built-in set", chosen);
      return {};
    }
    copy = Copy::Again;
    COmniphonyCache::Trim(".sofa");
    CLog::Log(LOGINFO, "COmniphonyHrtf: '{}' kept again as {}", chosen, sofa);
  }
  COmniphonyCache::Touch(sofa);
  return CSpecialProtocol::TranslatePath(sofa);
}

std::string COmniphonyHrtf::GridPath(const std::string& chosen)
{
  return COmniphonyCache::LocalFile(COmniphonyCache::Name(chosen), "-{khz}.grid");
}

std::string COmniphonyHrtf::Explain(const Outcome& o)
{
  const std::string sofa = COmniphonyCache::LocalFile(o.name, ".sofa");
  std::string text;
  switch (o.result)
  {
    case Result::Ok:
      text = StringUtils::Format(g_localizeStrings.Get(39333), sofa);
      break;
    case Result::Cached:
      text = g_localizeStrings.Get(39372);
      break;
    case Result::NotFound:
      text = g_localizeStrings.Get(39334);
      break;
    case Result::Unusable:
      text = StringUtils::Format(g_localizeStrings.Get(39336), o.detail);
      break;
    case Result::WrongConvention:
      text = g_localizeStrings.Get(39339);
      break;
    case Result::RoomResponse:
      text = g_localizeStrings.Get(39354);
      break;
    case Result::CopyFailed:
      text = g_localizeStrings.Get(39340);
      break;
    case Result::TooLarge:
      // The engine's numbers say how far short the memory was.
      text = g_localizeStrings.Get(39358);
      if (!o.detail.empty())
        text += "[CR][CR][I]" + o.detail + "[/I]";
      break;
    case Result::ReadFailed:
      text = g_localizeStrings.Get(39359);
      break;
    case Result::Unavailable:
      text = g_localizeStrings.Get(39361);
      break;
    case Result::EngineFailed:
      // The engine's words say why; English, but more use than nothing.
      text = g_localizeStrings.Get(39376);
      if (!o.detail.empty())
        text += "[CR][CR][I]" + o.detail + "[/I]";
      break;
    case Result::Cancelled:
      return {};
  }
  if ((o.result == Result::Ok || o.result == Result::Cached) && o.gridKept)
    text += "[CR][CR]" + g_localizeStrings.Get(39371);
  if (o.described)
    text += "[CR][CR]" + OmniphonyDescribeSofaInfo(o.contents);
  return text;
}

} // namespace ActiveAE
