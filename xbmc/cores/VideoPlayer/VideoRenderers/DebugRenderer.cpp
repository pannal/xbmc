/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DebugRenderer.h"

#include "ServiceBroker.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayLibass.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "settings/SettingsComponent.h"
#include "settings/SubtitlesSettings.h"
#include "utils/log.h"
#include "windowing/GraphicContext.h"

#include <utility>

using namespace OVERLAY;

CDebugRenderer::CDebugRenderer()
{
}

CDebugRenderer::~CDebugRenderer()
{
  Dispose();
}

void CDebugRenderer::Initialize()
{
  if (m_isInitialized)
    return;

  m_adapter = new CSubtitlesAdapter();

  m_isInitialized = m_adapter->Initialize();
  if (!m_isInitialized)
  {
    CLog::Log(LOGERROR, "{} - Failed to configure OSD info debug renderer", __FUNCTION__);
    delete m_adapter;
    m_adapter = nullptr;
    return;
  }

  // We create only a single overlay with a fixed PTS for each rendered frame
  m_overlay = m_adapter->CreateOverlay();
  m_overlayRenderer.AddOverlay(m_overlay, 1000000., 0);
}

void CDebugRenderer::Dispose()
{
  m_cachedLines.clear();
  m_nextInfoUpdate = {};
  m_isInitialized = false;
  m_overlayRenderer.Flush();
  m_overlay.reset();
  if (m_adapter)
  {
    delete m_adapter;
    m_adapter = nullptr;
  }
}

void CDebugRenderer::SetInfo(DEBUG_INFO_PLAYER& info)
{
  SetInfo({info.audio, info.video, info.player, info.vsync});
}

void CDebugRenderer::SetInfo(DEBUG_INFO_VIDEO& video, DEBUG_INFO_RENDER& render)
{
  SetInfo({video.videoSource, video.metaPrim, video.metaLight, video.shader, video.render,
           render.renderFlags, render.videoOutput});
}

void CDebugRenderer::SetInfo(std::vector<std::string> lines)
{
  if (!m_isInitialized)
    return;
  const auto now = std::chrono::steady_clock::now();
  if (now < m_nextInfoUpdate)
    return;
  m_nextInfoUpdate = now + std::chrono::milliseconds(100);
  if (lines == m_cachedLines)
    return;

  // Keep the fixed ASS event/PTS scheme and drawing cadence. Only replacing
  // events is limited; player diagnostic queries retain their sampling cadence.
  m_cachedLines.clear();
  m_adapter->FlushSubtitles();
  bool added = true;
  for (auto line : lines) // PostProcess mutates text; cache the original values.
    if (!line.empty() && m_adapter->AddSubtitle(line, 0., 5000000.) == NO_SUBTITLE_ID)
      added = false;
  if (added)
    m_cachedLines = std::move(lines);
}

void CDebugRenderer::Render(CRect& src, CRect& dst, CRect& view)
{
  if (!m_isInitialized)
    return;

  m_overlayRenderer.SetVideoRect(src, dst, view);
  m_overlayRenderer.Render(0);
}

void CDebugRenderer::Flush()
{
  m_cachedLines.clear();
  m_nextInfoUpdate = {};
  if (!m_isInitialized)
    return;

  m_adapter->FlushSubtitles();
}

CDebugRenderer::CRenderer::CRenderer() : OVERLAY::CRenderer()
{
}

void CDebugRenderer::CRenderer::Render(int idx, float depth)
{
  std::vector<SElement>& list = m_buffers[idx];
  for (std::vector<SElement>::iterator it = list.begin(); it != list.end(); ++it)
  {
    if (it->overlay_dvd)
    {
      auto ovAss = std::static_pointer_cast<const CDVDOverlayLibass>(it->overlay_dvd);
      if (!ovAss || !ovAss->GetLibassHandler())
        continue;

      bool updateStyle = !m_debugOverlayStyle;
      if (updateStyle)
        CreateSubtitlesStyle();

      std::shared_ptr<COverlay> o =
          ConvertLibass(*ovAss, it->pts, updateStyle, m_debugOverlayStyle);

      if (o)
        OVERLAY::CRenderer::Render(o);
    }
  }
  ReleaseUnused();
}

void CDebugRenderer::CRenderer::CreateSubtitlesStyle()
{
  KODI::SUBTITLES::STYLE::style style{};
  style.fontName = KODI::SUBTITLES::FONT_DEFAULT_FAMILYNAME;
  style.fontSize = 20.0;
  style.marginVertical = 12;
  m_debugOverlayStyle = std::make_shared<const KODI::SUBTITLES::STYLE::style>(std::move(style));
}

void CDebugRenderer::CRenderer::ResetSubtitlePosition()
{
  // The base implementation pushes the computed default position through
  // CApplicationPlayer, which rewrites the per-video setting and the main
  // subtitle renderer's position — clobbering a manually moved subtitle
  // position as soon as the debug OSD is rendered (m_subtitlePosResInfo
  // starts unset, so the first render always lands here). This instance
  // only needs its own tracking state synced: m_subtitleAlign is always
  // the member default (LoadSettings is never called) and the player
  // callback never routed back here, so m_subtitlePosition stays 0.
  m_saveSubtitlePosition = false;
  m_subtitleVerticalMargin = static_cast<int>(
      static_cast<float>(m_rv.Height()) / 100 *
      CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->GetVerticalMarginPerc());
  m_subtitlePosResInfo = static_cast<int>(m_rv.Height());
}
