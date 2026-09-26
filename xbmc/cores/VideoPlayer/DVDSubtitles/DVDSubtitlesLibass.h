/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDSubtitlesLibassRenderResult.h"
#include "SubtitlesStyle.h"
#include "threads/CriticalSection.h"
#include "utils/ColorUtils.h"

#include <memory>

#include <ass/ass.h>
#include <ass/ass_types.h>

/** Wrapper for Libass **/

static constexpr int ASS_NO_ID = -1;

enum ASSSubType
{
  NATIVE = 0,
  ADAPTED
};

class CDVDSubtitlesLibass
{
public:
  CDVDSubtitlesLibass();
  ~CDVDSubtitlesLibass();

  /*!
  * \brief Configure libass. This method groups any configurations
  * that might change throughout the lifecycle of libass (e.g. fonts)
  */
  void Configure();

  std::shared_ptr<const CLibassRenderResult> RenderImage(
      double pts,
      KODI::SUBTITLES::STYLE::renderOpts opts,
      bool updateStyle,
      const std::shared_ptr<struct KODI::SUBTITLES::STYLE::style>& subStyle);

  ASS_Event* GetEvents();

  /*!
  * \brief Get the number of events (subtitle entries) in the ASS track
  * \return The number of events in the ASS track
  */
  int GetNrOfEvents() const;

  /*!
  * \brief Decode Header of ASS/SSA, needed to properly decode
  * demux packets with DecodeDemuxPkt
  * \return True if success, false if error
  */
  bool DecodeHeader(char* data, int size);

  /*!
  * \brief Decode ASS/SSA demux packet (depends from DecodeHeader)
  * \return True if success, false if error
  */
  bool DecodeDemuxPkt(const char* data, int size, double start, double duration);

  /*!
  * \brief Create a new ASS track based on an SSA buffer
  * \return True if success, false if error
  */
  bool CreateTrack(char* buf, size_t size);

  /*!
  * \brief Flush buffered events
  */
  void FlushEvents();

  /*!
  * \brief Invalidate the cached render result, e.g. on seek/flush. The render
  * cache is keyed by pts and is not dropped by a timeline jump, so without this
  * RenderImage() can keep returning a stale image for the old position until a
  * re-demuxed event happens to invalidate it.
  */
  void FlushRenderCache();

  /*!
  * \brief Get PlayResY value
  * \return The PlayResY value of current track
  */
  int GetPlayResY();

  /*!
  * \brief Check if currently an event is active
  * \param pts as double
  * \return True when there is any event active
  */
  bool EventActive(double pts);

protected:
  /*!
  * \brief Create a new empty ASS track
  * \return True if success, false if error
  */
  bool CreateTrack();

  /*!
  * \brief Create a new empty ASS style
  * \return True if success, false if error
  */
  bool CreateStyle();

  /*!
  * \brief Specify whether the subtitles are
  * native (loaded from ASS/SSA file or stream)
  * or adapted (converted from other types e.g. SubRip)
  */
  void SetSubtitleType(ASSSubType type) { m_subtitleType = type; }

  /*!
  * \brief Add an ASS event to show a subtitle on a specified time
  * \param text The subtitle text
  * \param startTime The PTS start time of the Event
  * \param stopTime The PTS stop time of the Event
  * \return Return the Event ID, otherwise ASS_NO_ID if fails
  */
  int AddEvent(const char* text, double startTime, double stopTime);

  /*!
  * \brief Add an ASS event to show a subtitle on a specified time
  * \param text The subtitle text
  * \param startTime The PTS start time of the Event
  * \param stopTime The PTS stop time of the Event
  * \param opts Subtitle options
  * \return Return the Event ID, otherwise ASS_NO_ID if fails
  */
  int AddEvent(const char* text,
               double startTime,
               double stopTime,
               KODI::SUBTITLES::STYLE::subtitleOpts* opts);

  /*!
  * \brief Append text to the specified event
  */
  void AppendTextToEvent(int eventId, const char* text);

  /*!
  * \brief Delete old events only if the total number of events reaches the threshold
  * \param nEvents The number of events to delete
  * \param threshold Start deleting only when the number of events is reached
  * \return The updated ID of the last Event, otherwise ASS_NO_ID if error or no events
  */
  int DeleteEvents(int nEvents, int threshold);

  /*!
  * \brief Change the stop time of an Event with the specified time
  * \param eventId The ASS Event ID
  * \param stopTime The PTS stop time
  */
  void ChangeEventStopTime(int eventId, double stopTime);

  friend class CSubtitlesAdapter;


private:
  void ConfigureAssOverride(const std::shared_ptr<struct KODI::SUBTITLES::STYLE::style>& subStyle,
                            ASS_Style* style);
  void ApplyStyle(const std::shared_ptr<struct KODI::SUBTITLES::STYLE::style>& subStyle,
                  KODI::SUBTITLES::STYLE::renderOpts opts);

  /*!
  * \brief Whether an event's appearance changes over time (animation tags or
  * scroll/banner effects), meaning it must be re-rendered every frame.
  */
  bool IsDynamicEvent(const ASS_Event* assEvent) const;

  /*!
  * \brief Recompute the time interval [m_cacheValidFrom, m_cacheValidUntil)
  * over which the rendered output is provably constant, based on the active
  * event set at the given pts (ms). Disables the cache if any active event is
  * dynamic. Must be called with m_section held, right after a real render.
  */
  void UpdateRenderCache(int64_t ptsMs);

  /*!
  * \brief Invalidate the static-overlay render cache (call whenever events
  * are added/removed/modified). Must be called with m_section held.
  */
  void InvalidateRenderCache();

  ASS_Library* m_library = nullptr;
  ASS_Track* m_track = nullptr;
  ASS_Renderer* m_renderer = nullptr;
  mutable CCriticalSection m_section;
  ASSSubType m_subtitleType{NATIVE};

  // Static-overlay render cache: lets RenderImage skip the (potentially very
  // expensive) ass_render_frame() call on frames whose output is provably
  // identical to the previous one. Without this, heavy static signs
  // (typesetting with hundreds of \p drawing events) re-rasterize on every
  // video frame on the render thread, starving video buffer recycling and
  // collapsing playback to a few fps.
  std::shared_ptr<const CLibassRenderResult> m_lastResult;
  KODI::SUBTITLES::STYLE::renderOpts m_lastOpts{};
  bool m_renderCacheValid{false};
  int64_t m_cacheValidFrom{0};
  int64_t m_cacheValidUntil{0};

  // current default style ID of the ASS track
  int m_currentDefaultStyleId{ASS_NO_ID};

  // default allocated style ID for the kodi user configured subtitle style
  int m_defaultKodiStyleId{ASS_NO_ID};
  std::string m_defaultFontFamilyName;
};
