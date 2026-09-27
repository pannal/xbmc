/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDCodecs/Overlay/DVDOverlay.h"
#include "threads/CriticalSection.h"

#include <memory>

class CDVDInputStreamNavigator;
class CDVDDemuxSPU;

class CDVDOverlayContainer : public CCriticalSection
{
public:
  virtual ~CDVDOverlayContainer();

  /*!
  * \brief Adds an overlay into the container by processing the existing overlay collection first
  *
  * \details Processes the overlay collection whenever a new overlay is added. Useful to change
  * the overlay's PTS values of previously added overlays if the collection itself is sequential. This
  * is, for example, the case of ASS subtitles in which a single call to ass_render_frame generates all
  * the subtitle images on a single call even if two subtitles exist at the same time frame. Other cases
  * might exist where an overlay shouldn't be added to the collection if completely contained in another
  * overlay.
  *
  * \param pPicture pointer to the overlay to be evaluated and possibly added to the collection
  */
  void ProcessAndAddOverlayIfValid(const std::shared_ptr<CDVDOverlay>& pPicture);

  VecOverlays* GetOverlays(); // get the first overlay in this fifo
  bool ContainsOverlayType(DVDOverlayType type);

  void Clear(); // clear the fifo and delete all overlays

  /*
   * \brief Flush the overlays.
   */
  void Flush();

  void CleanUp(double pts); // validates all overlays against current pts

  /*!
   * \brief The disc menu composition to show with the picture at pts
   *
   * \details The newest composition that is due. Compositions it supersedes are
   * removed. A composition is due when it is untimed, when its start time has
   * been reached, or when its start time is further ahead than read-ahead can
   * explain.
   */
  std::shared_ptr<CDVDOverlay> GetDueDiscMenu(double pts);
  static bool IsDiscMenuDue(const CDVDOverlay& overlay, double pts);
  //! Start of the newest disc menu composition: DVD_NOPTS_VALUE once shown or when none.
  double GetNewestDiscMenuStart();
  size_t GetSize();

  void UpdateOverlayInfo(const std::shared_ptr<CDVDInputStreamNavigator>& pStream,
                         CDVDDemuxSPU* pSpu,
                         int iAction);

private:
  VecOverlays::iterator Remove(VecOverlays::iterator itOverlay); // removes a specific overlay

  VecOverlays m_overlays;
};
