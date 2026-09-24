/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DVDInputStreamBluray.h"

#include "DVDCodecs/Overlay/DVDOverlay.h"
#include "DVDCodecs/Overlay/DVDOverlayImage.h"
#include "DVDDemuxers/DemuxMVC.h"
#include "DVDInputStreamFile.h"
#include "IVideoPlayer.h"
#include "LangInfo.h"
#include "ServiceBroker.h"
#include "URL.h"
#include "dialogs/GUIDialogKaiToast.h"
#include "filesystem/BlurayCallback.h"
#include "filesystem/Directory.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "guilib/LocalizeStrings.h"
#include "settings/AdvancedSettings.h"
#include "settings/DiscSettings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/AMLUtils.h"
#include "utils/Geometry.h"
#include "utils/LangCodeExpander.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/XTimeUtils.h"
#include "utils/log.h"
#include "video/VideoInfoTag.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <memory>
#include <mutex>
#include <queue>
#include <thread>

#include <libbluray/bluray.h>
#include <libbluray/filesystem.h>
#include <libbluray/log_control.h>
#include <libbluray/mpls_data.h>

namespace
{
constexpr int64_t END_OF_TITLE_SPIN_TIMEOUT_MS = 5000;
// END_OF_TITLE and NONE may alternate indefinitely. Only real navigation/data
// progress or an explicit libbluray user-wait state restarts this deadline.
bool EndOfTitleReadStalled(uint32_t event,
                           int result,
                           bool waiting,
                           bool navigationProgress,
                           std::chrono::steady_clock::time_point now,
                           std::chrono::steady_clock::time_point& since)
{
  if (result > 0 || waiting || navigationProgress || event == BD_EVENT_IDLE ||
      event == BD_EVENT_STILL_TIME || event == BD_EVENT_STILL)
  {
    since = {};
    return false;
  }
  if (event == BD_EVENT_END_OF_TITLE && since == std::chrono::steady_clock::time_point{})
    since = now;
  return since != std::chrono::steady_clock::time_point{} &&
         now - since >= std::chrono::milliseconds(END_OF_TITLE_SPIN_TIMEOUT_MS);
}
} // namespace

#define LIBBLURAY_BYTESEEK 0
#define EMPTY_QUEUE(x) { while(!x.empty()) x.pop(); }

using namespace XFILE;

using namespace std::chrono_literals;

static int read_blocks(void* handle, void* buf, int lba, int num_blocks)
{
  auto blurayStream = reinterpret_cast<CDVDInputStreamBluray*>(handle);
  if (!blurayStream)
    return -1;
  return blurayStream->ReadBlocks(reinterpret_cast<uint8_t*>(buf), lba, num_blocks);
}

static void bluray_overlay_cb(void *this_gen, const BD_OVERLAY * ov)
{
  static_cast<CDVDInputStreamBluray*>(this_gen)->OverlayCallback(ov);
}

#ifdef HAVE_LIBBLURAY_BDJ
void  bluray_overlay_argb_cb(void *this_gen, const struct bd_argb_overlay_s * const ov)
{
  static_cast<CDVDInputStreamBluray*>(this_gen)->OverlayCallbackARGB(ov);
}
#endif

CDVDInputStreamBluray::CDVDInputStreamBluray(IVideoPlayer* player, const CFileItem& fileitem) :
  CDVDInputStream(DVDSTREAM_TYPE_BLURAY, fileitem), m_player(player)
{
  m_content = "video/x-mpegts";
  memset(&m_event, 0, sizeof(m_event));
#ifdef HAVE_LIBBLURAY_BDJ
  memset(&m_argb,  0, sizeof(m_argb));
#endif
}

CDVDInputStreamBluray::~CDVDInputStreamBluray()
{
  Close();
}

void CDVDInputStreamBluray::Abort()
{
  m_aborted = true;
  m_hold = HOLD_EXIT;
}

bool CDVDInputStreamBluray::IsEOF()
{
  return false;
}

BLURAY_TITLE_INFO* CDVDInputStreamBluray::GetTitleFromState(const std::string& xmlstate)
{
  BlurayState blurayState;
  if (!m_blurayStateSerializer.XMLToBlurayState(blurayState, xmlstate))
  {
    CLog::LogF(LOGWARNING, "Failed to deserialize Bluray state");
    return nullptr;
  }
  return bd_get_playlist_info(m_bd, blurayState.playlistId, 0);
}

BLURAY_TITLE_INFO* CDVDInputStreamBluray::GetTitleLongest() const
{
  int titles = bd_get_titles(m_bd, TITLES_RELEVANT, 0);

  BLURAY_TITLE_INFO* s = nullptr;
  for (int i = 0; i < titles; i++)
  {
    BLURAY_TITLE_INFO* t = bd_get_title_info(m_bd, i, 0);
    if (!t)
    {
      CLog::Log(LOGDEBUG, "get_main_title - unable to get title {}", i);
      continue;
    }
    if (!s || s->duration < t->duration)
      std::swap(s, t);

    if (t)
      bd_free_title_info(t);
  }
  return s;
}

BLURAY_TITLE_INFO* CDVDInputStreamBluray::GetTitleFile(const std::string& filename) const
{
  unsigned int playlist;
  if(sscanf(filename.c_str(), "%05u.mpls", &playlist) != 1)
  {
    CLog::Log(LOGERROR, "get_playlist_title - unsupported playlist file selected {}",
              CURL::GetRedacted(filename));
    return nullptr;
  }

  return bd_get_playlist_info(m_bd, playlist, 0);
}

bool CDVDInputStreamBluray::Open()
{
  m_aborted = false;

  if(m_player == nullptr)
    return false;

  std::string strPath(m_item.GetDynPath());
  std::string filename;
  std::string root;

  bool openStream = false;
  bool openDisc = false;
  bool resumable = true;

  // The item was selected via the simple menu
  if (URIUtils::IsProtocol(strPath, "bluray"))
  {
    CURL url(strPath);
    root = url.GetHostName();
    filename = URIUtils::GetFileName(url.GetFileName());

    // Check whether disc is AACS protected
    CURL url2(root);
    CFileItem item(url2, false);
    if (url2.IsProtocol("udf"))
      item.SetPath(url2.GetHostName());
    openDisc = item.IsProtectedBlurayDisc();

    // check for a menu call for an image file
    if (StringUtils::EqualsNoCase(filename, "menu"))
    {
      resumable = false;

      if (item.IsDiscImage())
      {
        if (!OpenStream(item))
          return false;

        openStream = true;
      }
    }
  }
  else if (m_item.IsDiscImage())
  {
    CURL url2("udf://");

    url2.SetHostName(m_item.GetPath());
    root = url2.Get();

    if (!OpenStream(m_item))
      return false;

    openStream = true;
  }
  else if (m_item.IsProtectedBlurayDisc())
  {
    openDisc = true;
  }
  else
  {
    strPath = URIUtils::GetDirectory(strPath);
    URIUtils::RemoveSlashAtEnd(strPath);

    if(URIUtils::GetFileName(strPath) == "PLAYLIST")
    {
      strPath = URIUtils::GetDirectory(strPath);
      URIUtils::RemoveSlashAtEnd(strPath);
    }

    if(URIUtils::GetFileName(strPath) == "BDMV")
    {
      strPath = URIUtils::GetDirectory(strPath);
      URIUtils::RemoveSlashAtEnd(strPath);
    }
    root = strPath;
    filename = URIUtils::GetFileName(m_item.GetDynPath());
  }

  // root should not have trailing slash
  URIUtils::RemoveSlashAtEnd(root);

  bd_set_debug_handler(CBlurayCallback::bluray_logger);

  m_bd = bd_init();

  if (!m_bd)
  {
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to initialize libbluray");
    return false;
  }

  SetupPlayerSettings();

  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - opening {}", CURL::GetRedacted(root));

  if (openStream)
  {
    if (!bd_open_stream(m_bd, this, read_blocks))
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to open {} in stream mode",
                CURL::GetRedacted(root));
      return false;
    }
  }
  else if (openDisc)
  {
    // This special case is required for opening original AACS protected Blu-ray discs. Otherwise
    // things like Bus Encryption might not be handled properly and playback will fail.
    m_rootPath = root;
    if (!bd_open_disc(m_bd, root.c_str(), nullptr))
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to open {} in disc mode",
                CURL::GetRedacted(root));
      return false;
    }
  }
  else
  {
    m_rootPath = root;
    if (!bd_open_files(m_bd, &m_rootPath, CBlurayCallback::dir_open, CBlurayCallback::file_open))
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to open {} in files mode",
                CURL::GetRedacted(root));
      return false;
    }
  }

  bd_get_event(m_bd, nullptr);

  m_root = root;
  const BLURAY_DISC_INFO *disc_info = bd_get_disc_info(m_bd);

  if (!disc_info)
  {
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - bd_get_disc_info() failed");
    return false;
  }

  ApplyUHDCapabilities();

  if (disc_info->bluray_detected)
  {
#if (BLURAY_VERSION > BLURAY_VERSION_CODE(1,0,0))
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - Disc name           : {}",
              disc_info->disc_name ? disc_info->disc_name : "");
#endif
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - First Play supported: {}",
              disc_info->first_play_supported);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - Top menu supported  : {}",
              disc_info->top_menu_supported);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - HDMV titles         : {}",
              disc_info->num_hdmv_titles);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - BD-J titles         : {}",
              disc_info->num_bdj_titles);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - BD-J handled        : {}",
              disc_info->bdj_handled);
    m_topMenuIsBdj =
        disc_info->top_menu ? disc_info->top_menu->bdj != 0 : disc_info->bdj_detected != 0;
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - UNSUPPORTED titles  : {}",
              disc_info->num_unsupported_titles);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - AACS detected       : {}",
              disc_info->aacs_detected);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - libaacs detected    : {}",
              disc_info->libaacs_detected);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - AACS handled        : {}",
              disc_info->aacs_handled);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - BD+ detected        : {}",
              disc_info->bdplus_detected);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - libbdplus detected  : {}",
              disc_info->libbdplus_detected);
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - BD+ handled         : {}",
              disc_info->bdplus_handled);
#if (BLURAY_VERSION >= BLURAY_VERSION_CODE(1,0,0))
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - no menus (libmmbd, or profile 6 bdj)  : {}",
              disc_info->no_menu_support);
#endif
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Open - 3D content exist    : {}", disc_info->content_exist_3D);
  }
  else
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - BluRay not detected");

  if (disc_info->aacs_detected && !disc_info->aacs_handled)
  {
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - Media stream scrambled/encrypted with AACS");
    m_player->OnDiscNavResult(nullptr, BD_EVENT_ENC_ERROR);
    return false;
  }

  if (disc_info->bdplus_detected && !disc_info->bdplus_handled)
  {
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - Media stream scrambled/encrypted with BD+");
    m_player->OnDiscNavResult(nullptr, BD_EVENT_ENC_ERROR);
    return false;
  }

  m_nTitles = bd_get_titles(m_bd, TITLES_RELEVANT, 0);
  int mode = CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(CSettings::SETTING_DISC_PLAYBACK);

  if (URIUtils::HasExtension(filename, ".mpls"))
  {
    m_navmode = false;
    ReplaceTitleInfo(GetTitleFile(filename));
  }
  else if (mode == BD_PLAYBACK_MAIN_TITLE)
  {
    m_navmode = false;
    ReplaceTitleInfo(GetTitleLongest());
  }
  else if (resumable && m_item.GetStartOffset() == STARTOFFSET_RESUME && m_item.IsResumable())
  {
    m_navmode = false;
    ReplaceTitleInfo(GetTitleFromState(m_item.GetVideoInfoTag()->GetResumePoint().playerState));
  }
  else
  {
    m_navmode = true;
    if (!disc_info->first_play_supported)
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - Can't play disc in HDMV navigation mode - First Play title not supported");
      m_navmode = false;
    }

    if (m_navmode && disc_info->num_unsupported_titles > 0) {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - Unsupported titles found - Some titles can't be played in navigation mode");
    }

    if(!m_navmode)
      ReplaceTitleInfo(GetTitleLongest());
  }

  if (m_navmode)
  {

    bd_register_overlay_proc (m_bd, this, bluray_overlay_cb);
#ifdef HAVE_LIBBLURAY_BDJ
    bd_register_argb_overlay_proc (m_bd, this, bluray_overlay_argb_cb, nullptr);
#endif

    if(bd_play(m_bd) <= 0)
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed play disk {}",
                CURL::GetRedacted(strPath));
      return false;
    }
    m_hold = HOLD_DATA;
  }
  else
  {
    uint32_t playlist;
    {
      std::lock_guard lock(m_clipTableMutex);
      if (!m_titleInfo)
      {
        CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to get title info");
        return false;
      }
      playlist = m_titleInfo->playlist;
    }

    if (!bd_select_playlist(m_bd, playlist))
    {
      CLog::Log(LOGERROR, "CDVDInputStreamBluray::Open - failed to select playlist {}", playlist);
      return false;
    }
  }

  // Process any events that occurred during opening
  while (bd_get_event(m_bd, &m_event))
    ProcessEvent();

  OpenNextStream();

  return true;
}

// close file and reset everything
// Keep all read/file callback context alive until libbluray and its Java threads stop.
void CDVDInputStreamBluray::Close()
{
  m_aborted = true;
  m_hold = HOLD_EXIT;
  m_navmode = false;
  CloseMVCDemux();
  if (m_bd)
  {
    bd_register_overlay_proc(m_bd, nullptr, nullptr);
#ifdef HAVE_LIBBLURAY_BDJ
    bd_register_argb_overlay_proc(m_bd, nullptr, nullptr, nullptr);
#endif
    bd_close(m_bd);
    m_bd = nullptr;
    OverlayClose();
  }
  ReplaceTitleInfo(nullptr);
  FreePrevTitleInfo();
  m_crossPlaylistPending = false;
  m_videoCompatBoundary = false;
  m_naturalChainBoundary = false;
  m_pstream.reset();
  m_rootPath.clear();
  m_currentTitleIsBdj = false;
  m_endOfTitleSpinStart = {};
  m_atTitleEnd = false;
  m_bdStillActive = false;
}

void CDVDInputStreamBluray::ReplaceTitleInfo(BLURAY_TITLE_INFO* incoming)
{
  BLURAY_TITLE_INFO* outgoing = nullptr;
  {
    std::lock_guard lock(m_clipTableMutex);
    outgoing = m_titleInfo;
    m_titleInfo = incoming;
    m_clip = nullptr;
    m_nMVCClip = nullptr;
    ++m_titleGeneration;
    EMPTY_QUEUE(m_clipQueue);
  }

  if (outgoing)
    bd_free_title_info(outgoing);
}

void CDVDInputStreamBluray::FreePrevTitleInfo()
{
  BLURAY_TITLE_INFO* outgoing;
  {
    std::lock_guard lock(m_clipTableMutex);
    outgoing = m_prevTitleInfo;
    m_prevTitleInfo = nullptr;
    m_prevClip = nullptr;
  }
  if (outgoing)
    bd_free_title_info(outgoing);
}

void CDVDInputStreamBluray::StashBoundaryClip()
{
  BLURAY_TITLE_INFO* outgoing;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (!m_titleInfo || !m_clip)
      return;
    outgoing = m_prevTitleInfo;
    m_prevTitleInfo = m_titleInfo;
    m_prevClip = m_clip;
    m_prevPlaylist = m_playlist;
    m_prevWasMVC = m_bMVCPlayback;
    m_prevFlipEyes = m_bFlipEyes;
    m_titleInfo = nullptr;
    m_clip = nullptr;
    m_nMVCClip = nullptr;
    ++m_titleGeneration;
    EMPTY_QUEUE(m_clipQueue);
  }
  if (outgoing)
    bd_free_title_info(outgoing);
}

void CDVDInputStreamBluray::UpdateSeamTimeOffset(uint64_t previousOut, uint64_t nextIn)
{
  const double step = (static_cast<double>(previousOut) - static_cast<double>(nextIn)) / 90000.0;
  std::lock_guard<std::mutex> lock(m_seamOffsetMutex);
  if (previousOut == nextIn)
    return;

  m_seamTimeOffsetPrev = m_seamTimeOffset;
  m_seamTimeOffset += step;
  m_seamGeneration++;
  CLog::Log(LOGDEBUG,
            "CDVDInputStreamBluray - seam offset step {:.3f}s applied, offset now {:.3f}s gen {}",
            step, m_seamTimeOffset, m_seamGeneration);
}

void CDVDInputStreamBluray::ResetSeamTimeOffset(const char* reason)
{
  std::lock_guard<std::mutex> lock(m_seamOffsetMutex);
  if (m_seamGeneration == 0 && m_seamTimeOffset == 0.0)
    return;

  m_seamTimeOffsetPrev = 0.0;
  m_seamTimeOffset = 0.0;
  m_seamGeneration++;
  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - seam offset reset ({}), gen {}", reason,
            m_seamGeneration);
}

bool CDVDInputStreamBluray::AreClipVideoStreamsCompatible(const BLURAY_CLIP_INFO* a,
                                                          const BLURAY_CLIP_INFO* b)
{
  if (!a || !b)
    return false;
  if (a->video_stream_count < 1 || a->video_stream_count != b->video_stream_count)
    return false;
  if (a->dv_stream_count != b->dv_stream_count || !a->video_streams || !b->video_streams ||
      (a->dv_stream_count && (!a->dv_streams || !b->dv_streams)) || a->ext_video_stream_count ||
      b->ext_video_stream_count)
    return false;
  for (uint8_t i = 0; i < a->video_stream_count; ++i)
  {
    if (a->video_streams[i].pid != b->video_streams[i].pid ||
        a->video_streams[i].coding_type != b->video_streams[i].coding_type ||
        a->video_streams[i].format != b->video_streams[i].format ||
        a->video_streams[i].rate != b->video_streams[i].rate ||
        a->video_streams[i].aspect != b->video_streams[i].aspect ||
        a->video_streams[i].color_space != b->video_streams[i].color_space ||
        a->video_streams[i].cr_flag != b->video_streams[i].cr_flag ||
        a->video_streams[i].dynamic_range_type != b->video_streams[i].dynamic_range_type ||
        a->video_streams[i].hdr_plus_flag != b->video_streams[i].hdr_plus_flag)
      return false;
  }
  for (uint8_t i = 0; i < a->dv_stream_count; ++i)
  {
    if (a->dv_streams[i].pid != b->dv_streams[i].pid)
      return false;
  }
  return true;
}

bool CDVDInputStreamBluray::AreClipPgStreamsEqual(const BLURAY_CLIP_INFO* a,
                                                  const BLURAY_CLIP_INFO* b)
{
  if (!a || !b)
    return false;
  if (a->pg_stream_count != b->pg_stream_count ||
      (a->pg_stream_count && (!a->pg_streams || !b->pg_streams)))
    return false;
  for (uint8_t i = 0; i < a->pg_stream_count; ++i)
  {
    if (a->pg_streams[i].pid != b->pg_streams[i].pid ||
        a->pg_streams[i].coding_type != b->pg_streams[i].coding_type ||
        a->pg_streams[i].char_code != b->pg_streams[i].char_code)
      return false;
  }
  return true;
}

bool CDVDInputStreamBluray::IsClipCodecCompatible(const BLURAY_CLIP_INFO* a,
                                                  const BLURAY_CLIP_INFO* b) const
{
  if (!AreClipVideoStreamsCompatible(a, b) || !AreClipPgStreamsEqual(a, b))
    return false;
  if (a->audio_stream_count != b->audio_stream_count ||
      (a->audio_stream_count && (!a->audio_streams || !b->audio_streams)))
    return false;
  for (uint8_t i = 0; i < a->audio_stream_count; ++i)
  {
    if (a->audio_streams[i].pid != b->audio_streams[i].pid ||
        a->audio_streams[i].coding_type != b->audio_streams[i].coding_type ||
        a->audio_streams[i].format != b->audio_streams[i].format ||
        a->audio_streams[i].rate != b->audio_streams[i].rate)
      return false;
  }
  return true;
}

void CDVDInputStreamBluray::ProcessEvent() {

  int pid = -1, ret;
  switch (m_event.event) {

   /* errors */

  case BD_EVENT_ERROR:
    switch (m_event.param)
    {
    case BD_ERROR_HDMV:
    case BD_ERROR_BDJ:
      m_player->OnDiscNavResult(nullptr, BD_EVENT_MENU_ERROR);
      break;
    default:
      break;
    }
    CLog::Log(LOGERROR, "BD_EVENT_ERROR: Fatal error. Playback can't be continued.");
    m_hold = HOLD_ERROR;
    break;

  case BD_EVENT_READ_ERROR:
    CLog::Log(LOGERROR, "CDVDInputStreamBluray - BD_EVENT_READ_ERROR");
    break;

  case BD_EVENT_ENCRYPTED:
    CLog::Log(LOGERROR, "BD_EVENT_ENCRYPTED");
    switch (m_event.param)
    {
    case BD_ERROR_AACS:
      CLog::Log(LOGERROR, "BD_ERROR_AACS");
      break;
    case BD_ERROR_BDPLUS:
      CLog::Log(LOGERROR, "BD_ERROR_BDPLUS");
      break;
    default:
      break;
    }
    m_hold = HOLD_ERROR;
    m_player->OnDiscNavResult(nullptr, BD_EVENT_ENC_ERROR);
    break;

  /* playback control */

  case BD_EVENT_SEEK:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_SEEK");
    ResetSeamTimeOffset("seek");

    //m_player->OnDVDNavResult(nullptr, 1);
    //bd_read_skip_still(m_bd);
    //m_hold = HOLD_HELD;
    break;

  case BD_EVENT_STILL_TIME:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_STILL_TIME {}", m_event.param);
    pid = m_event.param;
    m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_STILL_TIME);
    m_hold = HOLD_STILL;
    break;

  case BD_EVENT_STILL:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_STILL {}", m_event.param);

    pid = m_event.param;

    if (pid == 1)
    {
      m_bdStillActive = true;
      m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_STILL);
    }
    else if (pid == 0 && m_bdStillActive)
    {
      m_bdStillActive = false;
      m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_STILL);
    }
    break;

  case BD_EVENT_DISCONTINUITY:
    if (m_seamlessPlayItem)
    {
      m_seamlessPlayItem = false;
      CLog::Log(
          LOGDEBUG,
          "CDVDInputStreamBluray - BD_EVENT_DISCONTINUITY suppressed after seamless playitem");
      m_hold = HOLD_NONE;
      break;
    }
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_DISCONTINUITY");
    m_player->OnDiscNavResult(&m_event.param, BD_EVENT_DISCONTINUITY);
    m_hold = HOLD_NONE;
    break;

    /* playback position */

  case BD_EVENT_ANGLE:
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_ANGLE {}", m_event.param);
    bool angleReannounce;
    {
      std::lock_guard lock(m_clipTableMutex);
      angleReannounce = m_event.param == m_angle && m_titleInfo;
    }
    m_angle = m_event.param;

    if (!angleReannounce && m_playlist <= MAX_PLAYLIST_ID)
    {
      ReplaceTitleInfo(bd_get_playlist_info(m_bd, m_playlist, m_angle));
    }
    break;
  }

  case BD_EVENT_END_OF_TITLE:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_END_OF_TITLE {}", m_event.param);
    break;

  case BD_EVENT_TITLE:
  {

    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_TITLE {}", m_event.param);

    const BLURAY_DISC_INFO* disc_info = bd_get_disc_info(m_bd);
    if (!disc_info)
    {
      m_hold = HOLD_ERROR;
      break;
    }

    ApplyUHDCapabilities();

    m_isInMainMenu = false;

    const BLURAY_TITLE* title = nullptr;
    if (m_event.param == BLURAY_TITLE_TOP_MENU)
    {
      title = disc_info->top_menu;
      m_isInMainMenu = true;
    }
    else if (m_event.param == BLURAY_TITLE_FIRST_PLAY)
      title = disc_info->first_play;
    else if (disc_info->titles && m_event.param <= disc_info->num_titles)
      title = disc_info->titles[m_event.param];
    else
      title = nullptr;

    m_currentTitleIsBdj = title && title->bdj != 0;
    m_titleNumber = m_event.param;
    break;
  }
  case BD_EVENT_PLAYLIST:
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_PLAYLIST {}", m_event.param);
    if (m_menuRestorePlaylist <= MAX_PLAYLIST_ID)
    {
      if (m_event.param == m_menuRestorePlaylist && !m_menu)
      {
        m_menu = true;
        CLog::Log(LOGDEBUG, "BD_EVENT_PLAYLIST {} menu playlist restarted, restoring menu state",
                  m_event.param);
      }
      m_menuRestorePlaylist = MAX_PLAYLIST_ID + 1;
    }

    if (m_overlayCloseDeferred && m_event.param != m_playlist)
      OverlayClose();

    bool playlistReannounce;
    {
      std::lock_guard lock(m_clipTableMutex);
      playlistReannounce = m_event.param == m_playlist && m_titleInfo;
    }
    if (playlistReannounce)
    {
      CLog::Log(LOGDEBUG,
                "CDVDInputStreamBluray - BD_EVENT_PLAYLIST {} re-announced, keeping title info",
                m_event.param);
      break;
    }
    if (!m_crossPlaylistPending)
      ResetSeamTimeOffset("playlist");
    m_playlist = m_event.param;
    ProcessItem(m_playlist);
    break;
  }

  case BD_EVENT_PLAYITEM:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_PLAYITEM {}", m_event.param);

    {
      std::lock_guard lock(m_clipTableMutex);
      m_clip = (m_titleInfo && m_event.param < m_titleInfo->clip_count)
                   ? &m_titleInfo->clips[m_event.param]
                   : nullptr;
    }
    uint64_t clip_start, clip_in, bytepos;
    ret = bd_get_clip_infos(m_bd, m_event.param, &clip_start, &clip_in, &bytepos, nullptr);
    if (ret)
      m_clipStartTime = clip_start / 90;
    break;

  case BD_EVENT_CHAPTER:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_CHAPTER {}", m_event.param);
    break;

    /* stream selection */

  case BD_EVENT_AUDIO_STREAM:
    pid = -1;
    {
      std::lock_guard lock(m_clipTableMutex);
      if (m_titleInfo && m_clip &&
          static_cast<uint32_t>(m_clip->audio_stream_count) > (m_event.param - 1))
        pid = m_clip->audio_streams[m_event.param - 1].pid;
    }
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_AUDIO_STREAM {} {}", m_event.param, pid);
    m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_AUDIO_STREAM);
    break;

  case BD_EVENT_PG_TEXTST:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_PG_TEXTST {}", m_event.param);
    pid = m_event.param;
    m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_PG_TEXTST);
    break;

  case BD_EVENT_PG_TEXTST_STREAM:
    pid = -1;
    {
      std::lock_guard lock(m_clipTableMutex);
      if (m_titleInfo && m_clip &&
          static_cast<uint32_t>(m_clip->pg_stream_count) > (m_event.param - 1))
        pid = m_clip->pg_streams[m_event.param - 1].pid;
    }
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_PG_TEXTST_STREAM {}, {}", m_event.param,
              pid);
    m_player->OnDiscNavResult(static_cast<void*>(&pid), BD_EVENT_PG_TEXTST_STREAM);
    break;

  case BD_EVENT_MENU:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_MENU {}", m_event.param);
    m_menu = (m_event.param != 0);
    m_menuRestorePlaylist = MAX_PLAYLIST_ID + 1;
    if (!m_menu)
      m_isInMainMenu = false;
    m_player->OnDiscNavResult(&m_event.param, BD_EVENT_MENU);
    break;

  case BD_EVENT_IDLE:
    KODI::TIME::Sleep(100ms);
    break;

  case BD_EVENT_SOUND_EFFECT:
    // Optional menu sound effects are not part of the navigation port.
    break;

  case BD_EVENT_IG_STREAM:
  case BD_EVENT_SECONDARY_AUDIO:
  case BD_EVENT_SECONDARY_AUDIO_STREAM:
  case BD_EVENT_SECONDARY_VIDEO:
  case BD_EVENT_SECONDARY_VIDEO_SIZE:
  case BD_EVENT_SECONDARY_VIDEO_STREAM:
  case BD_EVENT_PLAYMARK:
  case BD_EVENT_KEY_INTEREST_TABLE:
  case BD_EVENT_PIP_PG_TEXTST:
  case BD_EVENT_PIP_PG_TEXTST_STREAM:
    break;

  case BD_EVENT_POPUP:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_POPUP {}", m_event.param);
    m_popupAvailable = (m_event.param != 0);
    break;

  case BD_EVENT_STEREOSCOPIC_STATUS:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_STEREOSCOPIC_STATUS {}", m_event.param);
    break;

  case BD_EVENT_UO_MASK_CHANGED:
    m_uoMask = m_event.param;
    CLog::Log(LOGDEBUG,
              "CDVDInputStreamBluray - BD_EVENT_UO_MASK_CHANGED 0x{:x} (menu_call={} "
              "time_search={} chapter_search={})",
              m_event.param, (m_event.param & BLURAY_UO_MENU_CALL) != 0,
              (m_event.param & BLURAY_UO_TIME_SEARCH_MASK) != 0,
              (m_event.param & BLURAY_UO_CHAPTER_SEARCH) != 0);
    break;

  case BD_EVENT_PLAYLIST_STOP:
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - BD_EVENT_PLAYLIST_STOP: flush buffers");
    m_menuRestorePlaylist = (m_menu && m_hasMenuOverlay) ? m_playlist : MAX_PLAYLIST_ID + 1;
    m_menu = false;
    ReplaceTitleInfo(nullptr);
    if (m_hasMenuOverlay)
    {
      m_overlayCloseDeferred = true;
    }
    else
      OverlayClose();
    m_player->OnDiscNavResult(nullptr, BD_EVENT_PLAYLIST_STOP);
    break;
  case BD_EVENT_NONE:
    break;

  default:
    CLog::Log(LOGWARNING, "unhandled libbluray event {} [param {}]", m_event.event, m_event.param);
    break;
  }

  /* event has been consumed */
  m_event.event = BD_EVENT_NONE;

  if (m_bMVCPlayback)
  {
    bool queued = false;
    {
      std::lock_guard lock(m_clipTableMutex);
      if (m_clip && m_titleInfo && m_clip >= m_titleInfo->clips &&
          m_clip < m_titleInfo->clips + m_titleInfo->clip_count && m_nMVCClip != m_clip &&
          (m_clipQueue.empty() || m_clip != m_titleInfo->clips + m_clipQueue.front()))
      {
        m_clipQueue.push(m_clip - m_titleInfo->clips);
        queued = true;
      }
    }

    if (queued && m_pMVCDemux == nullptr)
      OpenNextStream();
  }
}

void CDVDInputStreamBluray::DisableExtention()
{
  CloseMVCDemux();
  m_bMVCDisabled = true;
  m_bMVCPlayback = false;
}

int CDVDInputStreamBluray::Read(uint8_t* buf, int buf_size)
{
  if (m_aborted || !m_bd || !buf || buf_size <= 0)
    return -1;
  int result = 0;
  struct InReadGuard
  {
    explicit InReadGuard(std::atomic<std::thread::id>& flag) : m_flag(flag)
    {
      m_flag.store(std::this_thread::get_id(), std::memory_order_relaxed);
    }
    ~InReadGuard() { m_flag.store(std::thread::id(), std::memory_order_relaxed); }
    std::atomic<std::thread::id>& m_flag;
  };
  const InReadGuard inReadGuard(m_readingThread);
  if (m_repostMenuOverlay.load(std::memory_order_relaxed))
  {
    m_repostMenuOverlay = false;
    if (m_menu && m_hasOverlay)
    {
      CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Read - reposting retained menu overlay "
                          "composition after stream reopen");
      OverlayFlush(-1);
    }
  }
  DeliverParkedOverlayIfDue();
  m_dispTimeBeforeRead = static_cast<int>((bd_tell_time(m_bd) / 90));

  if(m_navmode)
  {
    do {

      DeliverParkedOverlayIfDue();

      if (m_hold == HOLD_HELD)
         return 0;

      if (m_aborted || m_hold == HOLD_ERROR || m_hold == HOLD_EXIT)
        return -1;

      result = bd_read_ext (m_bd, buf, buf_size, &m_event);
      if (m_aborted)
        return -1;
      m_lastReadEvent = m_event.event;

      if(result < 0)
      {
        m_hold = HOLD_ERROR;
        return result;
      }

      if (m_event.event == BD_EVENT_END_OF_TITLE && result == 0)
        m_atTitleEnd = true;
      bool navigationProgress =
          (m_event.event == BD_EVENT_TITLE && m_event.param != m_titleNumber) ||
          (m_event.event == BD_EVENT_PLAYLIST && m_event.param != m_playlist) ||
          (m_event.event == BD_EVENT_ANGLE && m_event.param != m_angle) ||
          (m_event.event == BD_EVENT_SEEK && !m_wrapSeekExempt);
      if (m_event.event == BD_EVENT_PLAYITEM)
      {
        std::lock_guard lock(m_clipTableMutex);
        navigationProgress = m_titleInfo && m_event.param < m_titleInfo->clip_count &&
                             m_clip != &m_titleInfo->clips[m_event.param];
      }
      if (EndOfTitleReadStalled(m_event.event, result, m_bdStillActive || m_hold == HOLD_STILL,
                                navigationProgress, std::chrono::steady_clock::now(),
                                m_endOfTitleSpinStart))
      {
        CLog::Log(LOGWARNING, "Blu-ray navigation made no progress after end of title for {}ms",
                  END_OF_TITLE_SPIN_TIMEOUT_MS);
        m_hold = HOLD_ERROR;
        return -1;
      }

      /* Check for holding events */
      switch(m_event.event) {
        case BD_EVENT_SEEK:
          if (m_wrapSeekExempt)
          {
            m_wrapSeekExempt = false;
            m_event.event = BD_EVENT_NONE;
            CLog::Log(LOGDEBUG, "BD_EVENT_SEEK consumed by same-playlist loop-wrap continuation");
            break;
          }
          if (m_hold != HOLD_DATA)
          {
            m_hold = HOLD_HELD;
            return result;
          }
          break;

        case BD_EVENT_TITLE:
          if (m_atTitleEnd)
            break;
          if (m_hold != HOLD_DATA)
          {
            StashBoundaryClip();
            m_hold = HOLD_HELD;
            return result;
          }
          break;

        case BD_EVENT_ANGLE:
          if (m_atTitleEnd && m_event.param == m_angle)
            break;
          if (m_hold != HOLD_DATA)
          {
            m_hold = HOLD_HELD;
            return result;
          }
          break;

        case BD_EVENT_PLAYLIST:
        {
          bool hasClip;
          bool hasVideo;
          {
            std::lock_guard lock(m_clipTableMutex);
            hasClip = m_titleInfo && m_clip;
            hasVideo = hasClip && m_clip->video_stream_count >= 1;
          }
          if (!m_bMVCPlayback && m_atTitleEnd && m_event.param == m_playlist && hasClip)
          {
            CLog::Log(
                LOGDEBUG,
                "BD_EVENT_PLAYLIST {} same-playlist loop wrap at title end, seamless continuation",
                m_event.param);
            m_wrapSeekExempt = true;
            ProcessEvent();
            m_event.event = BD_EVENT_NONE;
            break;
          }
          if (m_hold != HOLD_DATA)
          {
            if (!m_bMVCPlayback && m_atTitleEnd && m_event.param != m_playlist && hasVideo)
            {
              CLog::Log(LOGDEBUG, "BD_EVENT_PLAYLIST {} cross-playlist candidate from playlist {}",
                        m_event.param, m_playlist);
              StashBoundaryClip();
              m_crossPlaylistPending = true;
              ProcessEvent();
              m_event.event = BD_EVENT_NONE;
              break;
            }
            StashBoundaryClip();
            m_hold = HOLD_HELD;
            return result;
          }
          break;
        }

        case BD_EVENT_PLAYITEM:
          if(m_hold != HOLD_DATA)
          {
            std::unique_lock clipLock(m_clipTableMutex);
            const bool pending = m_crossPlaylistPending;
            m_crossPlaylistPending = false;
            const BLURAY_CLIP_INFO* cur = pending ? m_prevClip : m_clip;
            const char* tdReason = nullptr;
            const BLURAY_CLIP_INFO* nextClip =
                (m_titleInfo && m_event.param < m_titleInfo->clip_count)
                    ? &m_titleInfo->clips[m_event.param]
                    : nullptr;
            if (!m_bMVCPlayback && !pending && cur && nextClip == cur)
            {
              CLog::Log(LOGDEBUG, "BD_EVENT_PLAYITEM {} same-clip re-entry, seamless continuation",
                        m_event.param);
              const uint64_t previousOut = cur->out_time;
              const uint64_t nextIn = nextClip->in_time;
              clipLock.unlock();
              // Duplicate PLAYITEM notifications are not loop boundaries. Consume
              // the end marker once so repeated notifications cannot add it twice.
              if (m_atTitleEnd.exchange(false))
              {
                UpdateSeamTimeOffset(previousOut, nextIn);
                m_seamlessPlayItem = true;
              }
              ProcessEvent();
              m_event.event = BD_EVENT_NONE;
              break;
            }
            if (m_bMVCPlayback || (pending && m_prevWasMVC))
              tdReason = "mvc_reopen";
            else if (!m_titleInfo)
              tdReason = "no_titleInfo";
            else if (!cur)
              tdReason = pending ? "xpl_no_prev_clip" : "no_current_clip";
            else if (!nextClip)
              tdReason = "clip_oob";
            else if (!pending && cur->audio_stream_count < 1)
              tdReason = "current_no_audio";
            else if (!pending && cur->video_stream_count < 1)
              tdReason = "current_no_video";
            else if (!pending && nextClip->audio_stream_count < 1)
              tdReason = "next_no_audio";
            else if (!pending && nextClip->video_stream_count < 1)
              tdReason = "next_no_video";
            else if (pending && !AreClipVideoStreamsCompatible(cur, nextClip))
              tdReason = "xpl_video_changed";
            else if (pending && cur->audio_stream_count != nextClip->audio_stream_count)
              tdReason = "xpl_audio_set_changed";
            else if (pending && !AreClipPgStreamsEqual(cur, nextClip))
              tdReason = "xpl_pg_changed";
            else if (pending && m_prevWasMVC != m_bMVCPlayback)
              tdReason = "xpl_mvc_changed";
            else if (pending && m_bMVCPlayback && m_prevFlipEyes != m_bFlipEyes)
              tdReason = "xpl_mvc_eyes_changed";
            else if (!IsClipCodecCompatible(cur, nextClip))
              tdReason = "codec_changed";
            if (tdReason == nullptr)
            {
              CLog::Log(LOGDEBUG,
                        "BD_EVENT_PLAYITEM {} {} continuation (videoPid=0x{:x} videoType=0x{:x}, "
                        "audioPid=0x{:x} audioType=0x{:x})",
                        m_event.param, pending ? "cross-playlist seamless" : "seamless",
                        cur->video_streams[0].pid, cur->video_streams[0].coding_type,
                        cur->audio_stream_count ? cur->audio_streams[0].pid : 0,
                        cur->audio_stream_count ? cur->audio_streams[0].coding_type : 0);
              const uint64_t previousOut = cur->out_time;
              const uint64_t nextIn = nextClip->in_time;
              clipLock.unlock();
              UpdateSeamTimeOffset(previousOut, nextIn);
              m_atTitleEnd = false;
              m_seamlessPlayItem = true;
              ProcessEvent();
              m_event.event = BD_EVENT_NONE;
              if (pending)
              {
                m_wrapSeekExempt = true;
                FreePrevTitleInfo();
              }
              break;
            }
            const bool contentChain =
                cur && nextClip && cur->audio_stream_count >= 1 && cur->video_stream_count >= 1 &&
                nextClip->audio_stream_count >= 1 && nextClip->video_stream_count >= 1;
            if (cur && nextClip && !m_bMVCPlayback && !(pending && m_prevWasMVC))
              m_videoCompatBoundary = AreClipVideoStreamsCompatible(cur, nextClip);
            clipLock.unlock();
            if (pending)
              FreePrevTitleInfo();
            CLog::Log(LOGDEBUG, "BD_EVENT_PLAYITEM {} teardown: {}{}", m_event.param, tdReason,
                      m_videoCompatBoundary ? " (video-compatible boundary)" : "");
            m_seamlessPlayItem = false;
            m_naturalChainBoundary = contentChain && !m_bMVCPlayback && !(pending && m_prevWasMVC);
            m_hold = HOLD_HELD;
            return result;
          }
          break;

        case BD_EVENT_STILL_TIME:
          if(m_hold == HOLD_STILL)
            m_event.event = 0; /* Consume duplicate still event */
          else
            m_hold = HOLD_HELD;
          return result;

        default:
          break;
      }

      if(result > 0)
      {
        m_hold = HOLD_NONE;
        m_atTitleEnd = false;
        m_wrapSeekExempt = false;
        m_videoCompatBoundary = false;
        m_naturalChainBoundary = false;
        if (m_crossPlaylistPending)
        {
          CLog::Log(LOGWARNING,
                    "CDVDInputStreamBluray::Read - data before BD_EVENT_PLAYITEM resolved a "
                    "cross-playlist candidate, dropping it");
          m_crossPlaylistPending = false;
          FreePrevTitleInfo();
        }
      }

      ProcessEvent();

    } while(result == 0);

  }
  else
  {
    result = bd_read(m_bd, buf, buf_size);
    while (bd_get_event(m_bd, &m_event))
      ProcessEvent();
  }
  return result;
}

int CDVDInputStreamBluray::ReadBlocks(uint8_t* buf, int lba, int num_blocks)
{
  if (m_aborted || !buf || lba < 0 || num_blocks <= 0 ||
      num_blocks > std::numeric_limits<int>::max() / 2048)
    return -1;
  std::lock_guard lock(m_readBlocksLock);
  const int64_t offset = static_cast<int64_t>(lba) * 2048;
  if (!m_pstream || m_pstream->Seek(offset, SEEK_SET) != offset)
    return -1;
  const int size = num_blocks * 2048;
  int total = 0;
  while (total < size)
  {
    if (m_aborted)
      return -1;
    const int count = m_pstream->Read(buf + total, size - total);
    if (count < 0 || count > size - total)
      return -1;
    if (count == 0)
      break;
    total += count;
  }
  return total / 2048;
}

static uint8_t  clamp(double v)
{
  return (v) > 255.0 ? 255 : ((v) < 0.0 ? 0 : static_cast<uint32_t>((v + 0.5)));
}

static uint32_t build_rgba(const BD_PG_PALETTE_ENTRY &e)
{
  double r = 1.164 * (e.Y - 16)                        + 1.596 * (e.Cr - 128);
  double g = 1.164 * (e.Y - 16) - 0.391 * (e.Cb - 128) - 0.813 * (e.Cr - 128);
  double b = 1.164 * (e.Y - 16) + 2.018 * (e.Cb - 128);
  return static_cast<uint32_t>(e.T)      << PIXEL_ASHIFT
       | static_cast<uint32_t>(clamp(r)) << PIXEL_RSHIFT
       | static_cast<uint32_t>(clamp(g)) << PIXEL_GSHIFT
       | static_cast<uint32_t>(clamp(b)) << PIXEL_BSHIFT;
}

void CDVDInputStreamBluray::OverlayClose(bool deferrable, int closingPlane)
{
#if(BD_OVERLAY_INTERFACE_VERSION >= 2)
  std::lock_guard lock(m_overlayLock);
  if (deferrable && m_atTitleEnd && m_hasMenuOverlay && IsInMenu())
  {
    m_overlayCloseDeferred = true;

    return;
  }
  m_overlayCloseDeferred = false;
  m_pendingOverlayGroup.reset();

  if (closingPlane >= 0 && closingPlane < 2)
  {
    OverlayInit(m_planes[closingPlane], 0, 0);
    OverlayFlush(-1);
    return;
  }
  for (SPlane& plane : m_planes)
    OverlayInit(plane, 0, 0);
  auto group = std::make_shared<CDVDOverlayGroup>();
  group->bForced = true;
  group->SetDiscMenuOverlay(true);
  std::shared_ptr<CDVDOverlay> composition = group;
  m_player->OnDiscNavResult(static_cast<void*>(&composition), BD_EVENT_MENU_OVERLAY);
  m_hasOverlay = false;
  m_hasMenuOverlay = false;
#endif
}

void CDVDInputStreamBluray::OverlayInit(SPlane& plane, int w, int h)
{
#if(BD_OVERLAY_INTERFACE_VERSION >= 2)
  plane.o.clear();
  plane.w = w;
  plane.h = h;
#endif
}

void CDVDInputStreamBluray::OverlayClear(SPlane& plane, int x, int y, int w, int h)
{
#if(BD_OVERLAY_INTERFACE_VERSION >= 2)
  CRectInt ovr(x
          , y
          , x + w
          , y + h);

  /* fixup existing overlays */
  for (auto it = plane.o.begin(); it != plane.o.end();)
  {
    CRectInt old((*it)->x
            , (*it)->y
            , (*it)->x + (*it)->width
            , (*it)->y + (*it)->height);

    std::vector<CRectInt> rem = old.SubtractRect(ovr);

    /* if no overlap we are done */
    if(rem.size() == 1 && !(rem[0] != old))
    {
      ++it;
      continue;
    }

    SOverlays add;
    for (auto itr = rem.begin(); itr != rem.end(); ++itr)
    {
      auto overlay =
          std::make_shared<CDVDOverlayImage>(*(*it), itr->x1, itr->y1, itr->Width(), itr->Height());
      add.push_back(overlay);
    }

    it = plane.o.erase(it);
    plane.o.insert(it, add.begin(), add.end());
  }
#endif
}

void CDVDInputStreamBluray::DeliverParkedOverlayIfDue()
{
  const bool eventsDrained = m_lastReadEvent == BD_EVENT_NONE || m_lastReadEvent == BD_EVENT_IDLE;
  if ((IsNaturalChainBoundaryInFlight() || !eventsDrained) && m_hold != HOLD_STILL)
    return;

  std::lock_guard lock(m_overlayLock);
  if (!m_pendingOverlayGroup)
    return;

  std::shared_ptr<CDVDOverlay> pending;
  pending.swap(m_pendingOverlayGroup);
  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::Read - delivering parked menu overlay composition");
  m_player->OnDiscNavResult(static_cast<void*>(&pending), BD_EVENT_MENU_OVERLAY);
}

void CDVDInputStreamBluray::OverlayFlush(int64_t pts)
{
#if(BD_OVERLAY_INTERFACE_VERSION >= 2)
  std::lock_guard lock(m_overlayLock);
  auto group = std::make_shared<CDVDOverlayGroup>();
  group->bForced       = true;
  group->iPTSStartTime = static_cast<double>(pts);
  group->iPTSStopTime  = 0;
  group->SetDiscMenuOverlay(true);
  group->SetOverlayContainerFlushable(false);

  size_t subOverlayCount = 0;
  size_t menuOverlayCount = 0;
  for (size_t i = 0; i < sizeof(m_planes) / sizeof(m_planes[0]); ++i)
  {
    SPlane& plane = m_planes[i];
    for (auto it = plane.o.begin(); it != plane.o.end(); ++it)
    {
      group->m_overlays.push_back(*it);
    }
    subOverlayCount += plane.o.size();
    if (i == BD_OVERLAY_IG)
      menuOverlayCount = plane.o.size();
  }
  m_hasMenuOverlay = menuOverlayCount > 0;
  if (menuOverlayCount > 0)
    m_overlayCloseDeferred = false;

  if (m_readingThread.load(std::memory_order_relaxed) == std::this_thread::get_id())
  {
    m_pendingOverlayGroup = group;
    CLog::Log(LOGDEBUG, "OverlayFlush parked during demux read, boundaryInFlight={}",
              IsNaturalChainBoundaryInFlight());
  }
  else
  {
    m_pendingOverlayGroup.reset();
    std::shared_ptr<CDVDOverlay> composition = group;
    m_player->OnDiscNavResult(static_cast<void*>(&composition), BD_EVENT_MENU_OVERLAY);
  }
  m_hasOverlay = subOverlayCount != 0;
#endif
}

void CDVDInputStreamBluray::OverlayCallback(const BD_OVERLAY * const ov)
{
#if(BD_OVERLAY_INTERFACE_VERSION >= 2)
  std::lock_guard lock(m_overlayLock);
  if(ov == nullptr || ov->cmd == BD_OVERLAY_CLOSE)
  {
    if (!ov || ov->plane <= 1)
      OverlayClose(ov && ov->plane == BD_OVERLAY_IG, ov ? ov->plane : -1);
    return;
  }

  if (ov->plane > 1)
  {
    CLog::Log(LOGWARNING, "CDVDInputStreamBluray - Ignoring overlay with multiple planes");
    return;
  }

  SPlane& plane(m_planes[ov->plane]);
  if (ov->cmd == BD_OVERLAY_DRAW && !ov->palette_update_flag &&
      (ov->w == 0 || ov->h == 0 || ov->x > plane.w || ov->y > plane.h || ov->w > plane.w - ov->x ||
       ov->h > plane.h - ov->y))
  {
    CLog::Log(LOGWARNING, "Blu-ray: invalid overlay rectangle");
    return;
  }

  if (ov->cmd == BD_OVERLAY_CLEAR)
  {
    plane.o.clear();
    return;
  }

  if (ov->cmd == BD_OVERLAY_INIT)
  {
    OverlayInit(plane, ov->w, ov->h);
    return;
  }

  if (ov->cmd == BD_OVERLAY_HIDE)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - overlay HIDE plane {}", ov->plane);
    plane.o.clear();
    OverlayFlush(ov->pts);
    return;
  }

  if (ov->cmd == BD_OVERLAY_DRAW && ov->palette_update_flag)
  {
    if (ov->palette)
    {
      std::vector<uint32_t> pal(256);
      for (unsigned i = 0; i < 256; i++)
        pal[i] = build_rgba(ov->palette[i]);
      for (SOverlay& o : plane.o)
      {
        if (o->palette.empty())
          continue;
        SOverlay copy = std::make_shared<CDVDOverlayImage>(*o, o->x, o->y, o->width, o->height);
        copy->palette = pal;
        o = copy;
      }
      CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - palette-only update plane {} ({} overlays)",
                ov->plane, plane.o.size());
    }
    return;
  }

  if (ov->cmd == BD_OVERLAY_WIPE)
    OverlayClear(plane, ov->x, ov->y, ov->w, ov->h);

  /* uncompress and draw bitmap */
  if (ov->img && ov->cmd == BD_OVERLAY_DRAW)
  {
    if (!ov->palette)
    {
      CLog::Log(LOGWARNING, "Blu-ray: indexed overlay has no palette");
      return;
    }
    auto overlay = std::make_shared<CDVDOverlayImage>();
    overlay->SetDiscMenuOverlay(ov->plane == BD_OVERLAY_IG);

    if (ov->palette)
    {
      overlay->palette.resize(256);

      for(unsigned i = 0; i < 256; i++)
        overlay->palette[i] = build_rgba(ov->palette[i]);
    }
    else
      overlay->palette.clear();

    const BD_PG_RLE_ELEM *rlep = ov->img;
    const size_t bytes = static_cast<size_t>(ov->w) * ov->h;
    if (bytes > static_cast<size_t>(std::numeric_limits<int>::max()))
      return;
    overlay->pixels.resize(bytes);

    size_t lastEol = 0;
    for (size_t i = 0; i < bytes; ++rlep)
    {
      if (rlep->len == 0)
      {
        if (rlep->color != 0 || i == lastEol || i % ov->w != 0)
        {
          CLog::Log(LOGWARNING, "Blu-ray: invalid overlay RLE line marker");
          return;
        }
        lastEol = i;
        continue;
      }
      if (rlep->len > bytes - i || rlep->color > 255)
      {
        CLog::Log(LOGWARNING, "Blu-ray: invalid overlay RLE run");
        return;
      }
      memset(overlay->pixels.data() + i, rlep->color, rlep->len);
      i += rlep->len;
    }

    overlay->linesize = ov->w;
    overlay->x = ov->x;
    overlay->y = ov->y;
    overlay->height = ov->h;
    overlay->width = ov->w;
    overlay->source_height = plane.h;
    overlay->source_width = plane.w;

    OverlayClear(plane, ov->x, ov->y, ov->w, ov->h);
    plane.o.push_back(overlay);
  }

  if (ov->cmd == BD_OVERLAY_FLUSH)
    OverlayFlush(ov->pts);
#endif
}

#ifdef HAVE_LIBBLURAY_BDJ
void CDVDInputStreamBluray::OverlayCallbackARGB(const struct bd_argb_overlay_s * const ov)
{
  std::lock_guard lock(m_overlayLock);
  if(ov == nullptr || ov->cmd == BD_ARGB_OVERLAY_CLOSE)
  {
    if (!ov || ov->plane <= 1)
      OverlayClose(false, ov ? ov->plane : -1);
    return;
  }

  if (ov->plane > 1)
  {
    CLog::Log(LOGWARNING, "CDVDInputStreamBluray - Ignoring overlay with multiple planes");
    return;
  }

  SPlane& plane(m_planes[ov->plane]);
  if (ov->cmd == BD_ARGB_OVERLAY_DRAW &&
      (ov->w == 0 || ov->h == 0 || ov->x > plane.w || ov->y > plane.h || ov->w > plane.w - ov->x ||
       ov->h > plane.h - ov->y))
  {
    CLog::Log(LOGWARNING, "Blu-ray: invalid overlay rectangle");
    return;
  }

  if (ov->cmd == BD_ARGB_OVERLAY_INIT)
  {
    OverlayInit(plane, ov->w, ov->h);
    return;
  }

  if (ov->cmd == BD_ARGB_OVERLAY_DRAW &&
      (ov->stride < ov->w ||
       static_cast<size_t>(ov->stride) > std::numeric_limits<size_t>::max() / 4 / ov->h ||
       static_cast<size_t>(ov->w) * ov->h > std::numeric_limits<int>::max() / 4))
    return;

  /* uncompress and draw bitmap */
  if (ov->argb && ov->cmd == BD_ARGB_OVERLAY_DRAW)
  {
    auto overlay = std::make_shared<CDVDOverlayImage>();
    overlay->SetDiscMenuOverlay(true);

    overlay->palette.clear();
    // The source may point inside a larger canvas. Only the dirty rectangle
    // is readable on its last row; copying stride * height would overread it.
    const size_t rowBytes = static_cast<size_t>(ov->w) * sizeof(uint32_t);
    overlay->pixels.resize(rowBytes * ov->h);
    for (size_t row = 0; row < ov->h; ++row)
      memcpy(overlay->pixels.data() + row * rowBytes, ov->argb + row * ov->stride, rowBytes);

    overlay->linesize = static_cast<int>(rowBytes);
    overlay->x = ov->x;
    overlay->y = ov->y;
    overlay->height = ov->h;
    overlay->width = ov->w;
    overlay->source_height = plane.h;
    overlay->source_width = plane.w;

    OverlayClear(plane, ov->x, ov->y, ov->w, ov->h);
    plane.o.push_back(overlay);
  }

  if(ov->cmd == BD_ARGB_OVERLAY_FLUSH)
    OverlayFlush(ov->pts);
}
#endif


int CDVDInputStreamBluray::GetTotalTime()
{
  std::lock_guard lock(m_clipTableMutex);
  if(m_titleInfo)
    return static_cast<int>(m_titleInfo->duration / 90);
  else
    return 0;
}

int CDVDInputStreamBluray::GetTime()
{
  return m_dispTimeBeforeRead;
}

bool CDVDInputStreamBluray::PosTime(int ms)
{
  if (m_navmode && (m_uoMask.load() & BLURAY_UO_TIME_SEARCH_MASK))
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::PosTime - time search masked by disc UO");
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Warning, "Blu-ray",
                                          g_localizeStrings.Get(69241));
    return false;
  }

  m_seamlessPlayItem = false;
  if (!m_bd || ms < 0 || bd_seek_time(m_bd, static_cast<uint64_t>(ms) * 90) < 0)
    return false;

  {
    std::lock_guard lock(m_clipTableMutex);
    EMPTY_QUEUE(m_clipQueue);
  }
  while (bd_get_event(m_bd, &m_event))
    ProcessEvent();

  if (m_bMVCPlayback)
  {
    OpenNextStream();
    SeekMVCDemux(ms - m_clipStartTime);
  }
  return true;
}

int CDVDInputStreamBluray::GetChapterCount()
{
  std::lock_guard lock(m_clipTableMutex);
  if(m_titleInfo)
    return static_cast<int>(m_titleInfo->chapter_count);
  else
    return 0;
}

int CDVDInputStreamBluray::GetChapter()
{
  if (!m_bd || GetChapterCount() == 0)
    return 0;
  return static_cast<int>(bd_get_current_chapter(m_bd) + 1);
}

bool CDVDInputStreamBluray::SeekChapter(int ch)
{
  m_seamlessPlayItem = false;
  if (!m_bd || ch <= 0 || ch > GetChapterCount() ||
      (m_navmode && (m_uoMask.load() & BLURAY_UO_CHAPTER_SEARCH)) ||
      bd_seek_chapter(m_bd, ch - 1) < 0)
    return false;

  {
    std::lock_guard lock(m_clipTableMutex);
    EMPTY_QUEUE(m_clipQueue);
  }
  while (bd_get_event(m_bd, &m_event))
    ProcessEvent();

  if (m_bMVCPlayback)
  {
    OpenNextStream();
    SeekMVCDemux(GetChapterPos(ch) * 1000 - m_clipStartTime);
  }
  return true;
}

int64_t CDVDInputStreamBluray::GetChapterPos(int ch)
{
  if (ch == -1 || ch > GetChapterCount())
    ch = GetChapter();
  std::lock_guard lock(m_clipTableMutex);
  if (!m_titleInfo || !m_titleInfo->chapters || ch <= 0 ||
      static_cast<uint32_t>(ch) > m_titleInfo->chapter_count)
    return 0;
  return m_titleInfo->chapters[ch - 1].start / 90000;
}

int64_t CDVDInputStreamBluray::Seek(int64_t offset, int whence)
{
#if LIBBLURAY_BYTESEEK
  if(whence == SEEK_POSSIBLE)
    return 1;
  else if(whence == SEEK_CUR)
  {
    if(offset == 0)
      return bd_tell(m_bd);
    else
      offset += bd_tell(m_bd);
  }
  else if(whence == SEEK_END)
    offset += bd_get_title_size(m_bd);
  else if(whence != SEEK_SET)
    return -1;

  int64_t pos = bd_seek(m_bd, offset);
  if(pos < 0)
  {
    CLog::Log(LOGERROR, "CDVDInputStreamBluray::Seek - seek to {}, failed with {}", offset, pos);
    return -1;
  }

  if(pos != offset)
    CLog::Log(LOGWARNING, "CDVDInputStreamBluray::Seek - seek to {}, ended at {}", offset, pos);

  return offset;
#else
  if(whence == SEEK_POSSIBLE)
    return 0;
  return -1;
#endif
}

int64_t CDVDInputStreamBluray::GetLength()
{
  return static_cast<int64_t>(bd_get_title_size(m_bd));
}

static bool find_stream(int pid, BLURAY_STREAM_INFO *info, int count, std::string &language)
{
  int i=0;
  for(;i<count;i++,info++)
  {
    if(info->pid == static_cast<uint16_t>(pid))
      break;
  }
  if(i==count)
    return false;
  language = reinterpret_cast<char*>(info->lang);
  return true;
}

void CDVDInputStreamBluray::GetStreamInfo(int pid, std::string& language) const
{
  std::lock_guard lock(m_clipTableMutex);
  if(!m_titleInfo || !m_clip)
    return;

  if (pid == HDMV_PID_VIDEO || pid == HDMV_PID_VIDEO_EL)
    find_stream(pid, m_clip->video_streams, m_clip->video_stream_count, language);
  else if (HDMV_PID_AUDIO_FIRST <= pid && pid <= HDMV_PID_AUDIO_LAST)
    find_stream(pid, m_clip->audio_streams, m_clip->audio_stream_count, language);
  else if (HDMV_PID_PG_FIRST <= pid && pid <= HDMV_PID_PG_LAST)
    find_stream(pid, m_clip->pg_streams, m_clip->pg_stream_count, language);
  else if (HDMV_PID_PG_HDR_FIRST <= pid && pid <= HDMV_PID_PG_HDR_LAST)
    find_stream(pid, m_clip->pg_streams, m_clip->pg_stream_count, language);
  else if (HDMV_PID_IG_FIRST <= pid && pid <= HDMV_PID_IG_LAST)
    find_stream(pid, m_clip->ig_streams, m_clip->ig_stream_count, language);
  else
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::GetStreamInfo - unhandled pid {}", pid);
}

CDVDInputStream::ENextStream CDVDInputStreamBluray::NextStream()
{
  if(!m_navmode || m_hold == HOLD_EXIT || m_hold == HOLD_ERROR)
    return NEXTSTREAM_NONE;

  /* process any current event */
  ProcessEvent();

  /* process all queued up events */
  while(bd_get_event(m_bd, &m_event))
    ProcessEvent();

  if(m_hold == HOLD_STILL)
    return NEXTSTREAM_RETRY;

  m_crossPlaylistPending = false;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (m_prevClip && m_clip && m_playlist != m_prevPlaylist && !m_bMVCPlayback && !m_prevWasMVC)
    {
      m_videoCompatBoundary = AreClipVideoStreamsCompatible(m_prevClip, m_clip);
      CLog::Log(
          LOGDEBUG,
          "CDVDInputStreamBluray::NextStream - boundary playlist {} to {} video-compatible: {}",
          m_prevPlaylist, m_playlist, m_videoCompatBoundary);
    }
  }
  FreePrevTitleInfo();

  m_hold = HOLD_DATA;
  m_discontinuityFlush = true;
  return NEXTSTREAM_OPEN;
}

void CDVDInputStreamBluray::UserInput(bd_vk_key_e vk)
{
  if(m_bd == nullptr || !m_navmode)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::UserInput - key {} skipped (bd={} navmode={})",
              static_cast<int>(vk), m_bd != nullptr, m_navmode.load());
    return;
  }

  int ret = bd_user_input(m_bd, -1, vk);
  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::UserInput - key {} → bd_user_input ret={}",
            static_cast<int>(vk), ret);
  if (ret < 0)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::UserInput - user input failed");
  }
  else
  {
    /* process all queued up events */
    while (bd_get_event(m_bd, &m_event))
      ProcessEvent();
  }
}

bool CDVDInputStreamBluray::MouseMove(const CPoint& point) const
{
  if (m_bd == nullptr || !m_navmode)
    return false;

  // Disable mouse selection for BD-J menus, since it's not implemented in libbluray as of version 1.0.2
  if (m_currentTitleIsBdj)
    return false;

  if (bd_mouse_select(m_bd, -1, static_cast<uint16_t>(point.x), static_cast<uint16_t>(point.y)) < 0)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::MouseMove - mouse select failed");
    return false;
  }

  return true;
}

bool CDVDInputStreamBluray::MouseClick(const CPoint& point) const
{
  if (m_bd == nullptr || !m_navmode)
    return false;

  // Disable mouse selection for BD-J menus, since it's not implemented in libbluray as of version 1.0.2
  if (m_currentTitleIsBdj)
    return false;

  if (bd_mouse_select(m_bd, -1, static_cast<uint16_t>(point.x), static_cast<uint16_t>(point.y)) < 0)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::MouseClick - mouse select failed");
    return false;
  }

  if (bd_user_input(m_bd, -1, BD_VK_MOUSE_ACTIVATE) >= 0)
    return true;

  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::MouseClick - mouse click (user input) failed");
  return false;
}

bool CDVDInputStreamBluray::OnColorKey(int key)
{
  if (m_bd == nullptr || !m_navmode)
    return false;

  bd_vk_key_e vk;
  switch (key)
  {
    case 0:
      vk = BD_VK_RED;
      break;
    case 1:
      vk = BD_VK_GREEN;
      break;
    case 2:
      vk = BD_VL_YELLOW;
      break;
    case 3:
      vk = BD_VK_BLUE;
      break;
    default:
      return false;
  }
  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnColorKey - key {}", key);
  return bd_user_input(m_bd, -1, vk) >= 0;
}

bool CDVDInputStreamBluray::OnMenu(MenuCall call)
{
  if(m_bd == nullptr || !m_navmode)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnMenu - navigation mode not enabled");
    return false;
  }

  auto uoBlocked = [this]() -> bool
  {
    if (!(m_uoMask.load() & BLURAY_UO_MENU_CALL))
      return false;
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnMenu - menu call masked by disc UO");
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Warning, "Blu-ray",
                                          g_localizeStrings.Get(69240));
    return true;
  };

  const bool bdjMenuAllowed =
      !m_topMenuIsBdj || CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(
                             CSettings::SETTING_DISC_ALLOW_BDJ_TOP_MENU);
  const bool popupUoMasked = (m_uoMask.load() & BLURAY_UO_POPUP_ON_MASK) != 0;

  auto popupBlockedUo = [this]()
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnMenu - popup masked by disc UO");
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Warning, "Blu-ray",
                                          g_localizeStrings.Get(69240));
  };

  auto tryTop = [this]() -> bool
  {
    if (bd_user_input(m_bd, -1, BD_VK_ROOT_MENU) >= 0)
      return true;
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnMenu - root key failed, trying bd_menu_call");
    return bd_menu_call(m_bd, -1) > 0;
  };

  auto blockedBdjMenu = [this]()
  {
    CLog::Log(LOGDEBUG,
              "CDVDInputStreamBluray::OnMenu - BD-J disc menu blocked by disc.allowbdjtopmenu");
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Warning, "Blu-ray",
                                          g_localizeStrings.Get(69244));
  };

  switch (call)
  {
    case MenuCall::Popup:
      if (!bdjMenuAllowed)
      {
        blockedBdjMenu();
        return false;
      }
      if (popupUoMasked)
      {
        popupBlockedUo();
        return false;
      }
      return bd_user_input(m_bd, -1, BD_VK_POPUP) >= 0;
    case MenuCall::Top:
      if (uoBlocked())
        return false;
      if (!bdjMenuAllowed)
      {
        blockedBdjMenu();
        return false;
      }
      return tryTop();
    case MenuCall::Auto:
    default:
      if (bdjMenuAllowed && !popupUoMasked && bd_user_input(m_bd, -1, BD_VK_POPUP) >= 0)
        return true;
      CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OnMenu - popup unavailable, trying root");
      if (uoBlocked())
        return false;
      if (!bdjMenuAllowed)
      {
        blockedBdjMenu();
        return false;
      }
      return tryTop();
  }
}

bool CDVDInputStreamBluray::IsInMenu()
{
  return m_navmode &&
         (m_menu || (m_hasMenuOverlay && (m_uoMask.load() & BLURAY_UO_TIME_SEARCH_MASK)));
}

namespace
{
constexpr uint64_t MENU_DOMAIN_MAX_PLAYLIST_DURATION = 600ULL * 90000ULL;
}

bool CDVDInputStreamBluray::PlaylistWithinMenuDurationBound() const
{
  std::lock_guard lock(m_clipTableMutex);
  return m_titleInfo && m_titleInfo->duration <= MENU_DOMAIN_MAX_PLAYLIST_DURATION;
}

bool CDVDInputStreamBluray::TitleCarriesAlwaysOnMenuComposition() const
{
  std::lock_guard lock(m_clipTableMutex);
  if (!m_titleInfo)
    return false;
  if (m_titleInfo->duration > MENU_DOMAIN_MAX_PLAYLIST_DURATION && !m_hasMenuOverlay)
    return false;
  for (uint32_t i = 0; i < m_titleInfo->clip_count; ++i)
  {
    if (m_titleInfo->clips[i].ig_stream_count > 0)
      return true;
  }
  return false;
}

bool CDVDInputStreamBluray::IsMenuDomainSegment() const
{
  bool result;
  const char* reason;
  if (!m_navmode)
  {
    result = false;
    reason = "no_navmode";
  }
  else if (m_menu)
  {
    result = true;
    reason = "menu_flag";
  }
  else if (m_hasMenuOverlay && (m_uoMask.load() & BLURAY_UO_TIME_SEARCH_MASK) != 0)
  {
    result = true;
    reason = "menu_graphics";
  }
  else if (m_popupAvailable)
  {
    result = false;
    reason = "popup_ig_over_content";
  }
  else if (TitleCarriesAlwaysOnMenuComposition())
  {
    result = true;
    reason = "stn_ig";
  }
  else if (const uint32_t titleNumber = m_titleNumber.load();
           (titleNumber == BLURAY_TITLE_TOP_MENU || titleNumber == BLURAY_TITLE_FIRST_PLAY) &&
           PlaylistWithinMenuDurationBound())
  {
    result = true;
    reason = "menu_title";
  }
  else
  {
    result = false;
    reason = "no_match";
  }
  const int now = result ? 1 : 0;
  if (m_lastMenuDomainLogged.exchange(now) != now)
    CLog::Log(LOGDEBUG, "IsMenuDomainSegment -> {} via {}", result, reason);
  return result;
}

bool CDVDInputStreamBluray::ConsumeDiscontinuityFlush()
{
  return m_discontinuityFlush.exchange(false);
}

void CDVDInputStreamBluray::SkipStill()
{
  if(m_bd == nullptr || !m_navmode)
    return;

  if ( m_hold == HOLD_STILL)
  {
    m_hold = HOLD_HELD;
    bd_read_skip_still(m_bd);

    /* process all queued up events */
    while (bd_get_event(m_bd, &m_event))
      ProcessEvent();
  }
}

bool CDVDInputStreamBluray::CanSeek()
{
  if (m_navmode && (m_uoMask.load() & BLURAY_UO_TIME_SEARCH_MASK))
    return false;
  return !IsInMenu() || !m_isInMainMenu;
}

MenuType CDVDInputStreamBluray::GetSupportedMenuType()
{
  if (m_navmode)
  {
    return MenuType::NATIVE;
  }
  return MenuType::NONE;
}

bool CDVDInputStreamBluray::ProcessItem(int playitem)
{
  ReplaceTitleInfo(bd_get_playlist_info(m_bd, playitem, m_angle));

  if (!m_bMVCDisabled)
  {
    m_bMVCPlayback = false;
    m_nMVCSubPathIndex = 0;
    bool hasTitleInfo;
    uint8_t mvcBaseViewRFlag = 0;
    {
      std::lock_guard lock(m_clipTableMutex);
      hasTitleInfo = m_titleInfo != nullptr;
      if (m_titleInfo)
        mvcBaseViewRFlag = m_titleInfo->mvc_base_view_r_flag;
    }
    if (!hasTitleInfo)
    {
      CLog::Log(LOGWARNING, "CDVDInputStreamBluray::ProcessItem - no title info for playlist {}",
                playitem);
      CloseMVCDemux();
      return false;
    }

    MPLS_PL * mpls = bd_get_title_mpls(m_bd);
    if (mpls)
    {
      for (int i = 0; i < mpls->ext_sub_count; i++)
      {
        if (mpls->ext_sub_path[i].type == 8
          && mpls->ext_sub_path[i].sub_playitem_count == mpls->list_count)
        {
          CLog::Log(LOGDEBUG, "CDVDInputStreamBluray - Enabling BD3D MVC demuxing");
          CLog::Log(LOGDEBUG, "MVC_Base_view_R_flag: {}", mvcBaseViewRFlag);
          m_bMVCPlayback = true;
          m_nMVCSubPathIndex = i;
          m_bFlipEyes = mvcBaseViewRFlag != 0;
          break;
        }
      }
    }
  }
  CloseMVCDemux();
  return true;
}

int CDVDInputStreamBluray::Get3dSubtitlePlane(uint16_t pid) const
{
  if (!m_bMVCDisabled)
  {
    MPLS_PL *mpls = bd_get_title_mpls(m_bd);
    if (mpls)
    {
      for (int i = 0; i < mpls->list_count; i++)
      {
        for (int s = 0; s < mpls->play_item[i].stn.num_pg; s++)
        {
          if (mpls->play_item[i].stn.pg[s].pid == pid && mpls->play_item[i].stn.pg[s].ss_offset_sequence_id != 0xff)
            return mpls->play_item[i].stn.pg[s].ss_offset_sequence_id;
        }
      }
    }
  }

  return 0;
}

bool CDVDInputStreamBluray::OpenNextStream()
{
  int clip = 0;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (m_clipQueue.empty())
      return false;

    clip = m_clipQueue.front();
    m_clipQueue.pop();
  }

  auto pMVCDemux = dynamic_cast<CDemuxMVC*>(m_pMVCDemux);
  if (!pMVCDemux) {
    // either it's not a CDemuxMVC or it's 2D playback
    CloseMVCDemux();
    return OpenMVCDemux(clip);
  }

  // save start time for the next clip
  int64_t start_time = pMVCDemux->GetStartTime();

  CloseMVCDemux();

  bool res = OpenMVCDemux(clip);
  if (res) {
    auto nextDemux = dynamic_cast<CDemuxMVC*>(m_pMVCDemux);
    if (nextDemux) {
      // set start time for next clip
      auto menu = dynamic_cast<CDVDInputStream::IMenus*>(this);
      nextDemux->SetStartTime(start_time, menu->GetSupportedMenuType());
    }
  }

  return res;
}

bool CDVDInputStreamBluray::OpenMVCDemux(int playItem)
{
  uint64_t titleGeneration;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (!m_titleInfo || playItem < 0 || static_cast<uint32_t>(playItem) >= m_titleInfo->clip_count)
      return false;
    titleGeneration = m_titleGeneration;
  }
  MPLS_PL *pl = bd_get_title_mpls(m_bd);
  if (!pl)
    return false;

  if (m_nMVCSubPathIndex < 0 || m_nMVCSubPathIndex >= pl->ext_sub_count)
  {
    CLog::Log(
        LOGWARNING,
        "CDVDInputStreamBluray::OpenMVCDemux - subpath index {} out of range ({} ext subpaths)",
        m_nMVCSubPathIndex, pl->ext_sub_count);
    return false;
  }

  const int subItems = static_cast<int>(pl->ext_sub_path[m_nMVCSubPathIndex].sub_playitem_count);
  if (playItem < 0 || playItem >= subItems)
  {
    CLog::Log(
        LOGWARNING,
        "CDVDInputStreamBluray::OpenMVCDemux - playitem {} out of range (subpath {} has {} sub "
        "playitems)",
        playItem, m_nMVCSubPathIndex, subItems);
    return false;
  }

  std::string strFileName;
  strFileName.append(m_root);
  strFileName.append("/BDMV/STREAM/");
  strFileName.append(pl->ext_sub_path[m_nMVCSubPathIndex].sub_play_item[playItem].clip->clip_id);
  strFileName.append(".m2ts");

  CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::OpenMVCDemuxer(): Opening MVC extension stream at {}", strFileName);

  CFileItem fileitem(CURL(strFileName), false);
  m_pMVCInput = new CDVDInputStreamFile(fileitem, 0);

  // Try to open the MVC stream
  if (!m_pMVCInput->Open())
  {
    CloseMVCDemux();
    m_bMVCPlayback = false;
    return false;
  }

  if (m_pMVCDemux)
    delete m_pMVCDemux;

  auto pMVCDemux = new CDemuxMVC;
  m_pMVCDemux = pMVCDemux;

  if (!pMVCDemux->Open(m_pMVCInput))
  {
    CloseMVCDemux();
    m_bMVCPlayback = false;
    return false;
  }

  {
    std::lock_guard lock(m_clipTableMutex);
    if (m_titleInfo && titleGeneration == m_titleGeneration &&
        static_cast<uint32_t>(playItem) < m_titleInfo->clip_count)
      m_nMVCClip = m_titleInfo->clips + playItem;
    else
      CLog::Log(LOGWARNING,
                "CDVDInputStreamBluray::OpenMVCDemux - clip table changed during open, "
                "playitem {} not tracked (clips={})",
                playItem, m_titleInfo ? m_titleInfo->clip_count : 0u);
  }
  return true;
}

bool CDVDInputStreamBluray::CloseMVCDemux()
{
  if (m_pMVCDemux)
  {
    delete m_pMVCDemux;
    m_pMVCDemux = nullptr;
  }

  delete m_pMVCInput;
  m_pMVCInput = nullptr;
  {
    std::lock_guard lock(m_clipTableMutex);
    m_nMVCClip = nullptr;
  }
  return true;
}

void CDVDInputStreamBluray::SeekMVCDemux(int64_t time)
{
  if (m_bMVCPlayback && m_pMVCDemux)
    m_pMVCDemux->SeekTime(time, time < GetTime());
}

void CDVDInputStreamBluray::SetupPlayerSettings() const
{
  int region = CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(CSettings::SETTING_BLURAY_PLAYERREGION);
  if ( region != BLURAY_REGION_A
    && region != BLURAY_REGION_B
    && region != BLURAY_REGION_C)
  {
    CLog::Log(LOGWARNING, "CDVDInputStreamBluray::Open - Blu-ray region must be set in setting, assuming region A");
    region = BLURAY_REGION_A;
  }
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_REGION_CODE, static_cast<uint32_t>(region));
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_PARENTAL, 99);
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_3D_CAP,
                        aml_display_support_3d() ? 0xffffffff : 0);
#if (BLURAY_VERSION >= BLURAY_VERSION_CODE(1, 0, 2))
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_PLAYER_PROFILE, BLURAY_PLAYER_PROFILE_6_v3_1);
  ApplyUHDCapabilities();
#else
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_PLAYER_PROFILE, BLURAY_PLAYER_PROFILE_5_v2_4);
#endif

  std::string langCode;
  g_LangCodeExpander.ConvertToISO6392T(g_langInfo.GetDVDAudioLanguage(), langCode);
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_SETTING_AUDIO_LANG, langCode.c_str());

  g_LangCodeExpander.ConvertToISO6392T(g_langInfo.GetDVDSubtitleLanguage(), langCode);
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_SETTING_PG_LANG, langCode.c_str());

  g_LangCodeExpander.ConvertToISO6392T(g_langInfo.GetDVDMenuLanguage(), langCode);
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_SETTING_MENU_LANG, langCode.c_str());

  g_LangCodeExpander.ConvertToISO6391(g_langInfo.GetRegionLocale(), langCode);
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_SETTING_COUNTRY_CODE, langCode.c_str());

#ifdef HAVE_LIBBLURAY_BDJ
  std::string cacheDir = CSpecialProtocol::TranslatePath("special://userdata/cache/bluray/cache");
  std::string persistentDir = CSpecialProtocol::TranslatePath("special://userdata/cache/bluray/persistent");
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_PERSISTENT_ROOT, persistentDir.c_str());
  bd_set_player_setting_str(m_bd, BLURAY_PLAYER_CACHE_ROOT, cacheDir.c_str());
#endif
}

void CDVDInputStreamBluray::ApplyUHDCapabilities() const
{
#if (BLURAY_VERSION >= BLURAY_VERSION_CODE(1, 0, 2))
  const bool dvChain = (aml_dv_mode() != DV_MODE_OFF) && aml_display_support_dv();
  uint32_t uhdCap = 0x01;
  uint32_t uhdDisplayCap = aml_display_support_hdr_pq() ? 0x02 : 0;
  if (dvChain)
    uhdCap |= 0x02;
  if (aml_display_support_hdr_hlg())
    uhdCap |= 0x04;
  if (aml_display_support_hdr10plus())
    uhdCap |= 0x20;
  if (aml_display_support_dv())
    uhdDisplayCap |= 0x04;
  if (aml_display_support_hdr_hlg())
    uhdDisplayCap |= 0x08;
  if (aml_display_support_hdr10plus())
    uhdDisplayCap |= 0x10;
  uint32_t hdrPreference;
  if (dvChain)
    hdrPreference = 0x02;
  else if (aml_display_support_hdr10plus())
    hdrPreference = 0x20;
  else
    hdrPreference = 0x01;
  CLog::Log(LOGINFO,
            "CDVDInputStreamBluray: UHD capability PSRs: UHD_CAP 0x{:02x} UHD_DISPLAY_CAP 0x{:02x} "
            "HDR_PREFERENCE 0x{:02x}",
            uhdCap, uhdDisplayCap, hdrPreference);
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_UHD_CAP, uhdCap);
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_UHD_DISPLAY_CAP, uhdDisplayCap);
  bd_set_player_setting(m_bd, BLURAY_PLAYER_SETTING_HDR_PREFERENCE, hdrPreference);
#endif
}

bool CDVDInputStreamBluray::OpenStream(CFileItem &item)
{
  m_pstream = std::make_unique<CDVDInputStreamFile>(item, READ_TRUNCATED | READ_BITRATE |
                                                              READ_CHUNKED | READ_NO_CACHE);

  if (!m_pstream->Open())
  {
    CLog::Log(LOGERROR, "Error opening image file {}", CURL::GetRedacted(item.GetPath()));
    Close();
    return false;
  }

  return true;
}

bool CDVDInputStreamBluray::GetState(std::string& xmlstate)
{
  std::lock_guard lock(m_clipTableMutex);
  if (!m_bd || !m_titleInfo)
  {
    return false;
  }

  BlurayState blurayState;
  blurayState.playlistId = m_titleInfo->playlist;

  if (!m_blurayStateSerializer.BlurayStateToXML(xmlstate, blurayState))
  {
    CLog::LogF(LOGWARNING, "Failed to serialize Bluray state");
    return false;
  }

  return true;
}

bool CDVDInputStreamBluray::SetState(const std::string& xmlstate)
{
  if (!m_bd)
    return false;

  BlurayState blurayState;
  if (!m_blurayStateSerializer.XMLToBlurayState(blurayState, xmlstate))
  {
    CLog::LogF(LOGWARNING, "Failed to deserialize Bluray state");
    return false;
  }

  ReplaceTitleInfo(bd_get_playlist_info(m_bd, blurayState.playlistId, 0));
  uint32_t playlist = 0;
  uint32_t idx = 0;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (!m_titleInfo)
      return false;

    playlist = m_titleInfo->playlist;
    idx = m_titleInfo->idx;
  }

  if (!bd_select_playlist(m_bd, playlist))
  {
    CLog::Log(LOGERROR, "failed to select playlist {}", idx);
    return false;
  }

  return true;
}

static int find_stream_number(int pid, BLURAY_STREAM_INFO* info, int count)
{
  for (int i = 0; i < count; i++, info++)
  {
    if (info->pid == static_cast<uint16_t>(pid))
      return i + 1;
  }
  return -1;
}

bool CDVDInputStreamBluray::SetActiveAudioStream(int pid)
{
  if (!m_bd || !m_navmode)
    return false;
  int streamNumber = -1;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (m_clip)
      streamNumber = find_stream_number(pid, m_clip->audio_streams, m_clip->audio_stream_count);
  }
  if (streamNumber < 0)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::SetActiveAudioStream - pid {} not found", pid);
    return false;
  }

  // Keep libbluray's on-disc menu system (HDMV/BD-J) in sync with the audio
  // stream selected through Kodi's UI/hotkeys, as documented for
  // bd_select_stream(). Without this, the elementary stream libbluray
  // multiplexes into the output - in particular for sub-path audio - is not
  // updated, and the previously selected track keeps playing.
  bd_select_stream(m_bd, BLURAY_AUDIO_STREAM, static_cast<uint32_t>(streamNumber), 1);
  return true;
}

bool CDVDInputStreamBluray::SetActiveSubtitleStream(int pid)
{
  if (!m_bd || !m_navmode)
    return false;
  int streamNumber = -1;
  {
    std::lock_guard lock(m_clipTableMutex);
    if (m_clip)
      streamNumber = find_stream_number(pid, m_clip->pg_streams, m_clip->pg_stream_count);
  }
  if (streamNumber < 0)
  {
    CLog::Log(LOGDEBUG, "CDVDInputStreamBluray::SetActiveSubtitleStream - pid {} not found", pid);
    return false;
  }

  // See SetActiveAudioStream() - the same applies to PG/TextST subtitle
  // streams, which for many discs are carried in a sub-path and therefore
  // only get spliced into the output once libbluray is told to select them.
  bd_select_stream(m_bd, BLURAY_PG_TEXTST_STREAM, static_cast<uint32_t>(streamNumber), 1);
  return true;
}
