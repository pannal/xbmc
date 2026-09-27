/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DVDOverlayContainer.h"

#include "DVDCodecs/Overlay/DVDOverlay.h"
#include "DVDInputStreams/DVDInputStreamNavigator.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "utils/log.h"

#include <memory>
#include <mutex>

CDVDOverlayContainer::~CDVDOverlayContainer()
{
  Clear();
}

void CDVDOverlayContainer::ProcessAndAddOverlayIfValid(const std::shared_ptr<CDVDOverlay>& pOverlay)
{
  if (!pOverlay)
    return;
  std::unique_lock<CCriticalSection> lock(*this);

  // Freeze content before publishing it to picture selection. Timing remains
  // container-owned; unchanged group children retain their published versions.
  pOverlay->PublishRenderContent();

  // Menu compositions and subtitles have independent lifetimes. A menu redraw
  // must neither expire subtitles nor be replaced by a forced subtitle.
  if (pOverlay->IsDiscMenuOverlay())
  {
    // An untimed composition is the menu's current state and replaces every
    // other. A timed one waits for its picture (GetDueDiscMenu): the
    // composition on screen stays until then.
    if (pOverlay->iPTSStartTime == DVD_NOPTS_VALUE)
      m_overlays.erase(std::remove_if(m_overlays.begin(), m_overlays.end(), [](const auto& overlay)
                                      { return overlay->IsDiscMenuOverlay(); }),
                       m_overlays.end());
    m_overlays.emplace_back(pOverlay);
    return;
  }

  // markup any non ending overlays, to finish
  // when this new one starts, there can be
  // multiple overlays queued at same start
  // point so only stop them when we get a
  // new startpoint
  for (int i = m_overlays.size(); i > 0 && pOverlay->iPTSStartTime >= 0;)
  {
    i--;
    if (m_overlays[i]->IsDiscMenuOverlay())
      continue;
    if(m_overlays[i]->iPTSStopTime)
    {
      if(!m_overlays[i]->replace)
        break;
      if(m_overlays[i]->iPTSStopTime <= pOverlay->iPTSStartTime)
        break;
    }

    if (m_overlays[i]->iPTSStartTime != pOverlay->iPTSStartTime)
      m_overlays[i]->iPTSStopTime = pOverlay->iPTSStartTime;
  }

  m_overlays.emplace_back(pOverlay);
}

VecOverlays* CDVDOverlayContainer::GetOverlays()
{
  return &m_overlays;
}

VecOverlays::iterator CDVDOverlayContainer::Remove(VecOverlays::iterator itOverlay)
{
  std::unique_lock<CCriticalSection> lock(*this);
  return m_overlays.erase(itOverlay);
}

void CDVDOverlayContainer::CleanUp(double pts)
{
  std::unique_lock<CCriticalSection> lock(*this);

  auto it = m_overlays.begin();
  while (it != m_overlays.end())
  {
    const std::shared_ptr<CDVDOverlay>& pOverlay = *it;

    if (pOverlay->IsDiscMenuOverlay())
    {
      ++it;
      continue;
    }

    // never delete forced overlays, they are used in menu's
    // clear takes care of removing them
    // also if stoptime = 0, it means the next subtitles will use its starttime as the stoptime
    // which means we cannot delete overlays with stoptime 0
    if (!pOverlay->bForced && pOverlay->iPTSStopTime <= pts && pOverlay->iPTSStopTime != 0)
    {
      //CLog::Log(LOGDEBUG,"CDVDOverlay::CleanUp, removing {}", (int)(pts / 1000));
      //CLog::Log(LOGDEBUG,"CDVDOverlay::CleanUp, remove, start : {}, stop : {}", (int)(pOverlay->iPTSStartTime / 1000), (int)(pOverlay->iPTSStopTime / 1000));
      it = Remove(it);
      continue;
    }
    else if (pOverlay->bForced)
    {
      //Check for newer replacements
      auto it2 = it;
      bool bNewer = false;
      while (!bNewer && ++it2 != m_overlays.end())
      {
        const std::shared_ptr<CDVDOverlay>& pOverlay2 = *it2;
        // There can be multiple overlays queued at same start point.
        // Skip them to find a new start point.
        if (!pOverlay2->IsDiscMenuOverlay() && pOverlay2->bForced &&
            pOverlay2->iPTSStartTime <= pts && pOverlay->iPTSStartTime != pOverlay2->iPTSStartTime)
          bNewer = true;
      }

      if (bNewer)
      {
        it = Remove(it);
        continue;
      }
    }
    ++it;
  }

}

bool CDVDOverlayContainer::IsDiscMenuDue(const CDVDOverlay& overlay, double pts)
{
  // Longer than disc read-ahead (16 s demux queues plus decoder depth): the
  // start time is not on this picture's clock, so it must not hide the menu.
  constexpr double MAX_LEAD = 30.0 * DVD_TIME_BASE;

  const double start = overlay.iPTSStartTime;
  return start == DVD_NOPTS_VALUE || pts == DVD_NOPTS_VALUE || start <= pts ||
         start - pts > MAX_LEAD;
}

double CDVDOverlayContainer::GetNewestDiscMenuStart()
{
  std::unique_lock<CCriticalSection> lock(*this);
  const auto newest = std::find_if(m_overlays.rbegin(), m_overlays.rend(), [](const auto& overlay)
                                   { return overlay->IsDiscMenuOverlay(); });
  return newest == m_overlays.rend() ? DVD_NOPTS_VALUE : (*newest)->iPTSStartTime;
}

std::shared_ptr<CDVDOverlay> CDVDOverlayContainer::GetDueDiscMenu(double pts)
{
  std::unique_lock<CCriticalSection> lock(*this);

  auto due = m_overlays.end();
  for (auto it = m_overlays.begin(); it != m_overlays.end(); ++it)
  {
    if ((*it)->IsDiscMenuOverlay() && IsDiscMenuDue(**it, pts))
      due = it;
  }
  if (due == m_overlays.end())
    return nullptr;

  // Older compositions, shown or pending, are superseded once a newer one is due.
  due = m_overlays.erase(std::remove_if(m_overlays.begin(), due, [](const auto& overlay)
                                        { return overlay->IsDiscMenuOverlay(); }),
                         due);

  // Once shown, a composition is the menu's current state until replaced.
  CDVDOverlay& shown = **due;
  if (shown.iPTSStartTime != DVD_NOPTS_VALUE)
  {
    CLog::Log(LOGDEBUG, "CDVDOverlayContainer - disc menu composition due {:.3f} shown at {:.3f}",
              shown.iPTSStartTime / DVD_TIME_BASE,
              pts == DVD_NOPTS_VALUE ? -1.0 : pts / DVD_TIME_BASE);
    shown.iPTSStartTime = DVD_NOPTS_VALUE;
  }
  return *due;
}

void CDVDOverlayContainer::Flush()
{
  std::unique_lock<CCriticalSection> lock(*this);

  // Flush only the overlays marked as flushable
  m_overlays.erase(std::remove_if(m_overlays.begin(), m_overlays.end(),
                                  [](const std::shared_ptr<CDVDOverlay>& ov) {
                                    return ov->IsOverlayContainerFlushable();
                                  }),
                   m_overlays.end());
}

void CDVDOverlayContainer::Clear()
{
  std::unique_lock<CCriticalSection> lock(*this);
  m_overlays.clear();
}

size_t CDVDOverlayContainer::GetSize()
{
  std::unique_lock<CCriticalSection> lock(*this);
  // Menu pages waiting for their picture count as the one menu composition.
  const auto menus = std::count_if(m_overlays.begin(), m_overlays.end(),
                                   [](const auto& overlay) { return overlay->IsDiscMenuOverlay(); });
  return m_overlays.size() - menus + std::min<size_t>(menus, 1);
}

bool CDVDOverlayContainer::ContainsOverlayType(DVDOverlayType type)
{
  bool result = false;

  std::unique_lock<CCriticalSection> lock(*this);

  auto it = m_overlays.begin();
  while (!result && it != m_overlays.end())
  {
    if ((*it)->IsOverlayType(type)) result = true;
    ++it;
  }

  return result;
}

/*
 * iAction should be LIBDVDNAV_BUTTON_NORMAL or LIBDVDNAV_BUTTON_CLICKED
 */
void CDVDOverlayContainer::UpdateOverlayInfo(
    const std::shared_ptr<CDVDInputStreamNavigator>& pStream, CDVDDemuxSPU* pSpu, int iAction)
{
  std::unique_lock<CCriticalSection> lock(*this);

  pStream->CheckButtons();

  //Update any forced overlays.
  for(VecOverlays::iterator it = m_overlays.begin(); it != m_overlays.end(); ++it )
  {
    if ((*it)->IsOverlayType(DVDOVERLAY_TYPE_SPU))
    {
      auto pOverlaySpu = std::static_pointer_cast<CDVDOverlaySpu>(*it);

      // make sure its a forced (menu) overlay
      // set menu spu color and alpha data if there is a valid menu overlay
      if (pOverlaySpu->bForced)
      {
        if (pOverlaySpu.use_count() > 1)
        {
          pOverlaySpu = std::make_shared<CDVDOverlaySpu>(*pOverlaySpu);
          (*it) = pOverlaySpu;
        }

        // A changed highlight belongs to the replacement object's content
        // identity. Renderer cache state is never written into this overlay.
        pStream->GetCurrentButtonInfo(*pOverlaySpu, pSpu, iAction);
        pOverlaySpu->PublishRenderContent();

      }
    }
  }
}
