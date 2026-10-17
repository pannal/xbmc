/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyRoom.h"

#include "OmniphonyCache.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "guilib/LocalizeStrings.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <cstring>

namespace ActiveAE
{

namespace
{

//! The first bytes of every prepared room.
constexpr char ROOM_MAGIC[8] = {'O', 'M', 'N', 'I', 'R', 'O', 'O', 'M'};

//! The room's result for a helper's failure word.
COmniphonyRoom::Result FromReason(const std::string& reason)
{
  using Result = COmniphonyRoom::Result;
  if (reason == "unusable")
    return Result::Unusable;
  if (reason == "memory")
    return Result::TooLarge;
  if (reason == "input")
    return Result::ReadFailed;
  if (reason == "write")
    return Result::WriteFailed;
  if (reason == "unsupported" || reason == "engine")
    return Result::Unavailable;
  return Result::EngineFailed;
}

//! \brief Keep the room of \p name among the rooms in use.
void Kept(const std::string& name)
{
  COmniphonyCache::Touch(COmniphonyCache::File(name, ".room"));
  COmniphonyCache::Trim(".room");
}

} // unnamed namespace

COmniphonyRoom::Outcome COmniphonyRoom::Choose(const std::string& chosen)
{
  Outcome o;
  o.name = COmniphonyCache::Name(chosen);
  const std::string room = COmniphonyCache::File(o.name, ".room");
  COmniphonyCache::Folder();
  // Held to the end: a room of the same name prepared again for a stream
  // meanwhile is not put in place of this one - see PrepareAgain.
  const COmniphonyCache::Pick pick(room);

  // One pass over the file, which the engine reads whole from wherever Kodi
  // reads it: it says what the file is and its identity, and prepares it only
  // when the room stage takes it and the room kept under its name was not
  // prepared from the same file - that one is kept, and nothing is written.
  // Prepared beside the room and put in its place only once it is done, so
  // that a cancel, even one as the engine finishes, leaves the room kept
  // under the name as it was. The engine writes through a part of its own.
  const std::string next = room + ".next";
  const auto discard = [&next]
  {
    XFILE::CFile::Delete(next);
    XFILE::CFile::Delete(next + ".part");
  };
  discard();
  const std::unique_ptr<COmniphonyTool> preparer =
      Preparer(CSpecialProtocol::TranslatePath(next), chosen,
               XFILE::CFile::Exists(room) ? CSpecialProtocol::TranslatePath(room) : "");
  COmniphonyTool& tool = *preparer;
  switch (tool.Execute(true))
  {
    case COmniphonyTool::Status::Done:
      break;
    case COmniphonyTool::Status::Unavailable:
      o.result = Result::Unavailable;
      return o;
    case COmniphonyTool::Status::Failed:
      discard();
      o.result = Result::EngineFailed;
      return o;
    case COmniphonyTool::Status::Unreadable:
      o.result = Result::NotFound;
      return o;
    case COmniphonyTool::Status::Cancelled:
      discard();
      o.result = Result::Cancelled;
      return o;
  }

  // What the file holds, which the engine says whatever became of it once it
  // could read it.
  const bool notRoom = tool.Word() == "failed" && tool.Reason() == "notroom";
  if (tool.Word() == "prepared" || tool.Word() == "kept" || notRoom)
    o.described = OmniphonyParseSofaInfo(tool.Detail(), o.contents);
  if (notRoom)
  {
    o.result = Result::NotARoom;
    return o;
  }
  if (tool.Word() == "kept")
  {
    Kept(o.name);
    o.result = Result::Cached;
    CLog::Log(LOGINFO, "COmniphonyRoom: '{}' is already prepared as {}", chosen, room);
    return o;
  }
  if (tool.Word() != "prepared")
  {
    discard();
    o.detail = tool.Detail();
    o.result = FromReason(tool.Reason());
    CLog::Log(LOGWARNING, "COmniphonyRoom: '{}' not prepared: {} {}", chosen, tool.Reason(),
              tool.Detail());
    return o;
  }
  if (!pick.Promote(next))
  {
    o.result = Result::WriteFailed;
    return o;
  }
  Kept(o.name);
  o.result = Result::Ok;
  CLog::Log(LOGINFO, "COmniphonyRoom: '{}' prepared as {}: {}", chosen, room, tool.Detail());
  return o;
}

std::unique_ptr<COmniphonyTool> COmniphonyRoom::Preparer(const std::string& out,
                                                         const std::string& source,
                                                         const std::string& kept)
{
  std::vector<std::string> args{"--prepare-brir", COmniphonyTool::EnginePath(), out};
  if (!kept.empty())
    args.push_back(kept);
  return std::make_unique<COmniphonyTool>(std::move(args), source);
}

COmniphonyRoom::Again COmniphonyRoom::PrepareAgain(const std::string& chosen,
                                                   COmniphonyTool& tool,
                                                   uint64_t epoch)
{
  const std::string name = COmniphonyCache::Name(chosen);
  const std::string room = COmniphonyCache::File(name, ".room");
  const std::string again = COmniphonyCache::File(name, ".room.again");
  COmniphonyCache::Folder();
  if (tool.Execute(false) != COmniphonyTool::Status::Done || tool.Word() != "prepared")
  {
    XFILE::CFile::Delete(again);
    XFILE::CFile::Delete(again + ".part");
    CLog::Log(LOGWARNING, "COmniphonyRoom: the room of '{}' not prepared again: {} {}", chosen,
              tool.Reason(), tool.Detail());
    return Again::Failed;
  }
  switch (COmniphonyCache::PromoteUnlessPicked(epoch, again, room))
  {
    case COmniphonyCache::Placed::Picked:
      CLog::Log(LOGINFO,
                "COmniphonyRoom: the room of '{}' was picked in the settings meanwhile - the one "
                "prepared again is not used",
                chosen);
      return Again::Picked;
    case COmniphonyCache::Placed::Failed:
      return Again::Failed;
    case COmniphonyCache::Placed::Done:
      break;
  }
  Kept(name);
  CLog::Log(LOGINFO, "COmniphonyRoom: the room of '{}' prepared again: {}", chosen, tool.Detail());
  return Again::Prepared;
}

std::string COmniphonyRoom::AgainPath(const std::string& chosen)
{
  return COmniphonyCache::LocalFile(COmniphonyCache::Name(chosen), ".room.again");
}

std::string COmniphonyRoom::RoomPath(const std::string& chosen)
{
  return COmniphonyCache::LocalFile(COmniphonyCache::Name(chosen), ".room");
}

bool COmniphonyRoom::IsPrepared(const std::string& path)
{
  XFILE::CFile file;
  char magic[sizeof(ROOM_MAGIC)] = {};
  return file.Open(path) &&
         file.Read(magic, sizeof(magic)) == static_cast<ssize_t>(sizeof(magic)) &&
         std::memcmp(magic, ROOM_MAGIC, sizeof(magic)) == 0;
}

std::string COmniphonyRoom::Explain(const Outcome& o)
{
  const std::string room = COmniphonyCache::LocalFile(o.name, ".room");
  std::string text;
  switch (o.result)
  {
    case Result::Ok:
      text = StringUtils::Format(g_localizeStrings.Get(39356), room) + "[CR][CR]" +
             g_localizeStrings.Get(39368) + "[CR][CR]" + g_localizeStrings.Get(39377);
      break;
    case Result::Cached:
      text = g_localizeStrings.Get(39357);
      break;
    case Result::NotFound:
      text = g_localizeStrings.Get(39334);
      break;
    case Result::Unusable:
      text = StringUtils::Format(g_localizeStrings.Get(39336), o.detail);
      break;
    case Result::NotARoom:
      // An HRTF set is pointed to the other choice; a file neither stage
      // takes is said to be so by its description.
      if (o.contents.hrtf)
        text = g_localizeStrings.Get(39355);
      break;
    case Result::TooLarge:
      text = g_localizeStrings.Get(39358);
      break;
    case Result::ReadFailed:
      text = g_localizeStrings.Get(39359);
      break;
    case Result::WriteFailed:
      text = g_localizeStrings.Get(39360);
      break;
    case Result::Unavailable:
      text = g_localizeStrings.Get(39361);
      break;
    case Result::EngineFailed:
      text = g_localizeStrings.Get(39362);
      break;
    case Result::Cancelled:
      return {};
  }
  if (o.described)
    text += (text.empty() ? "" : "[CR][CR]") + OmniphonyDescribeSofaInfo(o.contents);
  // The engine's numbers say how far short the memory was, and its words what
  // failed; English, but more use than nothing to someone reading them out.
  if ((o.result == Result::TooLarge || o.result == Result::EngineFailed) && !o.detail.empty())
    text += "[CR][CR][I]" + o.detail + "[/I]";
  return text;
}

} // namespace ActiveAE
