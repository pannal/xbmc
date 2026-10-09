/*
 *      Initial code sponsored by: Voddler Inc (voddler.com)
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OverlayRenderer.h"

#include "BitmapSubtitlePosition.h"
#include "OverlayRendererUtil.h"
#include "ServiceBroker.h"
#include "application/ApplicationComponents.h"
#include "application/ApplicationPlayer.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlay.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayLibass.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySpu.h"
#include "settings/DisplaySettings.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "windowing/GraphicContext.h"
#include "windowing/WinSystem.h"

#include <algorithm>
#include <mutex>
#include <utility>

using namespace KODI;
using namespace OVERLAY;

std::shared_ptr<COverlay> COverlay::Create(const CLibassRenderResult& result,
                                         float width,
                                         float height)
{
  // The legacy factories read and consume this owned chain synchronously.
  return Create(result.m_images.get(), width, height);
}

COverlay::COverlay()
{
  m_x = 0.0f;
  m_y = 0.0f;
  m_width = 0.0f;
  m_height = 0.0f;
  m_type = TYPE_NONE;
  m_align = ALIGN_SCREEN;
  m_pos = POSITION_RELATIVE;
}

COverlay::~COverlay() = default;

bool COverlay::PlainPremultiplyDiscMenu(const CDVDOverlayImage& o)
{
  // PQ-tagged graphics go through the PQ-to-SDR shader, whose un-premultiply
  // divides by alpha: exact only for a plain premultiply, on any output.
  return o.IsDiscMenuOverlay() &&
         (o.m_isHdrPq || !CServiceBroker::GetWinSystem()->IsGuiOutputHdr());
}

CRenderer::CRenderer()
{
  CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->RegisterObserver(this);
}

CRenderer::~CRenderer()
{
  CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->UnregisterObserver(this);
  Flush();
}

void CRenderer::AddOverlay(std::shared_ptr<CDVDOverlay> o, double pts, int index)
{
  std::unique_lock<CCriticalSection> lock(m_section);

  m_buffers[index].emplace_back(pts, o);
}

void CRenderer::SetOverlays(OverlayBatch overlays, int index)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  m_buffers[index] = std::move(overlays);
}

CRenderer::OverlayBatch CRenderer::GetOverlays(int index)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  if (index < 0 || index >= NUM_BUFFERS)
    return {};
  return m_buffers[index];
}

void CRenderer::Release(std::vector<SElement>& list)
{
  list.clear();
}

void CRenderer::UnInit()
{
  if (m_saveSubtitlePosition)
  {
    m_saveSubtitlePosition = false;
    CDisplaySettings::GetInstance().UpdateCalibrations();
    CServiceBroker::GetSettingsComponent()->GetSettings()->Save();
  }

  CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->EndBitmapPosition();
  Flush();
}

void CRenderer::Flush()
{
  std::unique_lock<CCriticalSection> lock(m_section);

  for(std::vector<SElement>& buffer : m_buffers)
    Release(buffer);

  ReleaseCache();
  Reset();
}

void CRenderer::Reset()
{
  m_subtitlePosition = 0;
  m_subtitlePosResInfo = -1;
  m_activePicture = {};
  m_restrictToActivePicture = false;
  m_activeAreaTopOffset = 0;
  m_activeAreaBottomOffset = 0;
  m_activeAreaApplyUserPos = false;
}

void CRenderer::Release(int idx)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  Release(m_buffers[idx]);
}

void CRenderer::ReleaseCache()
{
  m_textureCache.clear();
}

void CRenderer::ReleaseUnused(const OverlayBatch& selected)
{
  for (auto it = m_textureCache.begin(); it != m_textureCache.end(); )
  {
    // A retained selection can outlive its slot's CPU list. Keep its cache
    // entries reachable until this synchronous draw has finished as well.
    bool found = std::any_of(selected.begin(), selected.end(), [&it](const SElement& e) {
      return e.overlay_dvd == it->first;
    });
    for (auto& buffer : m_buffers)
    {
      for (auto& dvdoverlay : buffer)
      {
        if (dvdoverlay.overlay_dvd == it->first)
        {
          found = true;
          break;
        }
      }
      if (found)
        break;
    }
    if (!found)
    {
      it = m_textureCache.erase(it);
    }
    else
      ++it;
  }
}

namespace
{
bool IsPqMenuImage(const std::shared_ptr<const CDVDOverlay>& o)
{
  return o && o->IsOverlayType(DVDOVERLAY_TYPE_IMAGE) &&
         std::static_pointer_cast<const CDVDOverlayImage>(o)->m_isPqMenuGraphics;
}
} // namespace

void CRenderer::Render(int idx, float depth)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  Render(m_buffers[idx]);
}

void CRenderer::Render(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  RenderPrepared(PrepareRenderItems(overlays), overlays);
}

void CRenderer::RenderPrepared(const PreparedOverlays& items, const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  CWinSystemBase* winSystem = CServiceBroker::GetWinSystem();
  const bool menuComposite =
      winSystem->IsMenuCompositeActive() || winSystem->IsMenuCompositePending();
  for (const auto& item : items)
  {
    if (menuComposite && item.overlay->m_rawPqMenu)
      continue;
    SRenderState state = item.state;
    item.overlay->Render(state);
  }
  ReleaseUnused(overlays);
}

CRenderer::PreparedOverlays CRenderer::PrepareRenderItems(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  const bool hasBitmap = std::any_of(overlays.begin(), overlays.end(), [](const SElement& e) {
    return e.overlay_dvd && !e.overlay_dvd->IsDiscMenuOverlay() && !IsPqMenuImage(e.overlay_dvd) &&
           (e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_IMAGE) ||
            e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_SPU));
  });
  const auto position = static_cast<BitmapSubtitlePosition>(
      hasBitmap ? settings->GetInt(CSettings::SETTING_SUBTITLES_BITMAPPOSITION) : 0);
  const float zoom = hasBitmap ? static_cast<float>(settings->GetInt(CSettings::SETTING_SUBTITLES_BITMAPZOOM)) / 100.0f : 1.0f;
  const float aspect = hasBitmap && (position == BitmapSubtitlePosition::BOTTOM_PICTURE ||
      position == BitmapSubtitlePosition::TOP_PICTURE) ? static_cast<float>(settings->GetInt(CSettings::SETTING_SUBTITLES_BITMAPASPECT)) / 100.0f : 0.0f;
  const float margin = hasBitmap ? static_cast<float>(settings->GetNumber(CSettings::SETTING_SUBTITLES_BITMAPMARGIN)) : 0.0f;
  const float offset = hasBitmap ? CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->GetBitmapOffset() : 0.0f;
#if HAS_GLES >= 2
  const float sdrBrightness = hasBitmap ? static_cast<float>(settings->GetInt(
      CSettings::SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS)) / 100.0f : 1.0f;
  const float sdrSaturation = hasBitmap ? static_cast<float>(settings->GetInt(
      CSettings::SETTING_SUBTITLES_BITMAPSDRSATURATION)) / 100.0f : 1.0f;
#endif
  const CRect active = GetBitmapSubtitleArea(m_rv, m_rd, m_activePicture, aspect);
  const CRect limit = m_restrictToActivePicture ? m_activePicture : m_rv;
  const bool stereo = (!m_stereomode.empty() && m_stereomode != "mono") ||
      CServiceBroker::GetWinSystem()->GetGfxContext().GetStereoMode() != RENDER_STEREO_MODE_OFF;
  PreparedOverlays items;
  std::vector<BitmapSubtitleRegion> regions;
  std::vector<SRenderGeometry> geometries;
  auto bounds = [](const COverlay& overlay, const SRenderState& state)
  {
    const bool centered = overlay.m_pos == COverlay::POSITION_RELATIVE;
    const float left = state.x - (centered ? state.width * 0.5f : 0.0f);
    const float top = state.y - (centered ? state.height * 0.5f : 0.0f);
    return CRect(left, top, left + state.width, top + state.height);
  };
  const auto* winSystem = CServiceBroker::GetWinSystem();
  const bool menuComposite = winSystem->IsMenuCompositeActive() || winSystem->IsMenuCompositePending();
  for (const auto& element : overlays)
  {
    if (!element.overlay_dvd || (menuComposite && IsPqMenuImage(element.overlay_dvd)))
      continue;
    auto overlay = Convert(*element.overlay_dvd, element.pts);
    if (!overlay)
      continue;
    auto geometry = PrepareRenderGeometry(*overlay, zoom);
    geometries.push_back(geometry);
    geometry.activeAreaTop = geometry.activeAreaBottom = 0;
    geometry.bitmapZoom = 1.0f;
    const CRect authored = bounds(*overlay, CalculateRenderState(geometry));
    geometry.bitmapZoom = zoom;
    SRenderState state = CalculateRenderState(geometry);
    regions.push_back({authored, bounds(*overlay, state),
        overlay->m_align == COverlay::ALIGN_SCREEN_AR ? m_rv : m_rd,
        overlay->m_canPosition && !overlay->m_discMenuOverlay && !stereo});
    items.push_back({std::move(overlay), state, {}});
  }
  const auto placements = PlaceBitmapSubtitles(regions, m_rv, active, limit, position,
                                               offset, margin, m_restrictToActivePicture, zoom);
  for (size_t i = 0; i < items.size(); ++i)
  {
    auto& item = items[i];
    if (placements[i].selected)
    {
      item.state.y += placements[i].offset;
      item.state.x += placements[i].horizontalOffset;
    }
    else
      item.state = CalculateRenderState(geometries[i]);
#if HAS_GLES >= 2
    // A retained cue uses current colour settings, captured once for this batch.
    // The texture backend independently checks source and menu eligibility.
    item.state.sdrBrightness = sdrBrightness;
    item.state.sdrSaturation = sdrSaturation;
#endif
    item.bounds = bounds(*item.overlay, item.state);
  }
  return items;
}

void CRenderer::RenderPqMenu(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);

  for (auto& e : overlays)
  {
    if (!IsPqMenuImage(e.overlay_dvd))
      continue;
    std::shared_ptr<COverlay> o = Convert(*(e.overlay_dvd), e.pts);
    if (o)
      Render(o);
  }
}

bool CRenderer::HasPqMenuOverlay(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  for (const auto& e : overlays)
  {
    // Only visible menu graphics keep the composite engaged.
    if (IsPqMenuImage(e.overlay_dvd) &&
        std::static_pointer_cast<const CDVDOverlayImage>(e.overlay_dvd)->m_menuVisible)
      return true;
  }
  return false;
}

CRenderer::SRenderGeometry CRenderer::PrepareRenderGeometry(const COverlay& overlay, float bitmapZoom) const
{
  SRenderGeometry geometry{{overlay.m_x, overlay.m_y, overlay.m_width, overlay.m_height},
                           overlay.m_pos,
                           overlay.m_align,
                           overlay.m_source_width,
                           overlay.m_source_height,
                           overlay.m_isBitmapOverlay,
                           overlay.m_discMenuOverlay,
                           m_rs,
                           m_rd,
                           m_rv,
                           m_activeAreaTopOffset,
                           m_activeAreaBottomOffset};

  // Preserve the conditional service reads of the original draw path. In
  // particular, disc menus do not acquire subtitle depth or bitmap placement.
  if ((geometry.position == COverlay::POSITION_RELATIVE ||
       geometry.position == COverlay::POSITION_ABSOLUTE) &&
      geometry.alignment == COverlay::ALIGN_SUBTITLE)
  {
    const RESOLUTION_INFO resInfo =
        CServiceBroker::GetWinSystem()->GetGfxContext().GetResInfo();
    geometry.subtitleBaseline = resInfo.iSubtitles - resInfo.Overscan.top;
  }
  if (!geometry.discMenu)
    geometry.stereoDepth = GetStereoscopicDepth(overlay.m_pgsSubtitle, overlay.m_3dSubtitleDepth);
  if (geometry.bitmap && !geometry.discMenu)
  {
    geometry.bitmapZoom = bitmapZoom >= 0.0f ? bitmapZoom : static_cast<float>(
                              CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(
                                  CSettings::SETTING_SUBTITLES_BITMAPZOOM)) /
                          100.0f;
  }
  return geometry;
}

void CRenderer::Render(std::shared_ptr<COverlay> overlay)
{
  const SRenderGeometry geometry = PrepareRenderGeometry(*overlay);
  SRenderState state = CalculateRenderState(geometry);
  overlay->Render(state);
}

SRenderState CRenderer::CalculateRenderState(const SRenderGeometry& geometry)
{
  SRenderState state = geometry.state;
  COverlay::EPosition pos = geometry.position;
  COverlay::EAlign align = geometry.alignment;

  if (pos == COverlay::POSITION_RELATIVE)
  {
    float scale_x = 1.0;
    float scale_y = 1.0;
    float scale_w = 1.0;
    float scale_h = 1.0;

    if (align == COverlay::ALIGN_SCREEN || align == COverlay::ALIGN_SUBTITLE)
    {
      scale_x = geometry.view.Width();
      scale_y = geometry.view.Height();
      scale_w = scale_x;
      scale_h = scale_y;
    }
    else if (align == COverlay::ALIGN_SCREEN_AR)
    {
      // Align to screen by keeping aspect ratio to fit into the screen area
      float source_width =
          geometry.sourceWidth > 0 ? geometry.sourceWidth : geometry.source.Width();
      float source_height =
          geometry.sourceHeight > 0 ? geometry.sourceHeight : geometry.source.Height();
      float ratio =
          std::min<float>(geometry.view.Width() / source_width, geometry.view.Height() / source_height);
      scale_x = geometry.view.Width();
      scale_y = geometry.view.Height();
      scale_w = ratio;
      scale_h = ratio;
    }
    else if (align == COverlay::ALIGN_VIDEO)
    {
      scale_x = geometry.source.Width();
      scale_y = geometry.source.Height();
      scale_w = scale_x;
      scale_h = scale_y;
    }

    state.x *= scale_x;
    state.y *= scale_y;
    state.width *= scale_w;
    state.height *= scale_h;

    pos = COverlay::POSITION_ABSOLUTE;
  }

  if (pos == COverlay::POSITION_ABSOLUTE)
  {
    if (align == COverlay::ALIGN_SCREEN || align == COverlay::ALIGN_SCREEN_AR ||
        align == COverlay::ALIGN_SUBTITLE)
    {
      if (align == COverlay::ALIGN_SUBTITLE)
      {
        state.x += geometry.view.x1 + geometry.view.Width() * 0.5f;
        state.y += geometry.view.y1 + geometry.subtitleBaseline;
      }
      else
      {
        state.x += geometry.view.x1;
        state.y += geometry.view.y1;
      }
    }
    else if (align == COverlay::ALIGN_VIDEO)
    {
      float scale_x = geometry.destination.Width() / geometry.source.Width();
      float scale_y = geometry.destination.Height() / geometry.source.Height();

      state.x *= scale_x;
      state.y *= scale_y;
      state.width *= scale_x;
      state.height *= scale_y;

      state.x += geometry.destination.x1;
      state.y += geometry.destination.y1;
    }
  }

  if (!geometry.discMenu)
    state.x += geometry.stereoDepth;

  if (geometry.bitmap && !geometry.discMenu)
  {
    const float zoom = geometry.bitmapZoom;
    if (zoom != 1.0f)
    {
      if (geometry.position == COverlay::POSITION_RELATIVE)
      {
        // x/y are center-based; shift center down by half the height difference
        // so the scaled subtitle visually centers within its old bounding box
        state.y += state.height * (1.0f - zoom) * 0.5f;
        state.width *= zoom;
        state.height *= zoom;
      }
      else
      {
        // x/y are top-left; keep horizontal center, shift down by height difference
        float cx = state.x + state.width * 0.5f;
        state.y += state.height * (1.0f - zoom);
        state.width *= zoom;
        state.height *= zoom;
        state.x = cx - state.width * 0.5f;
      }
    }
  }

  // DV L5 active area: clamp image-based subtitles to the active content area.
  // The offsets are bar heights measured from the view edges — player-added
  // bars on cropped encodes and/or in-frame L5 bars scaled to the display.
  // Screen-anchored subs (ALIGN_SCREEN_AR: canvas AR != video AR, e.g. 16:9
  // PGS on a cropped 2.4:1 encode) are authored into those bars, so they need
  // moving just like video-anchored ones — L5 masking would swallow them.
  // For POSITION_RELATIVE subs, state.y is the center; for others it's the top edge.
  if (geometry.bitmap && !geometry.discMenu &&
      (geometry.activeAreaTop > 0 || geometry.activeAreaBottom > 0))
  {
    float activeTop = geometry.view.y1 + static_cast<float>(geometry.activeAreaTop);
    float activeBottom = geometry.view.y2 - static_cast<float>(geometry.activeAreaBottom);
    float activeHeight = activeBottom - activeTop;
    // Reference rect the sub was positioned against: the view for
    // screen-anchored subs, the video rect for video-anchored ones.
    const CRect& ref =
        (geometry.alignment == COverlay::ALIGN_SCREEN_AR) ? geometry.view : geometry.destination;
    float refHeight = ref.Height();

    bool centerBased = (geometry.position == COverlay::POSITION_RELATIVE);
    float halfH = centerBased ? state.height * 0.5f : 0.0f;
    float subTop = state.y - halfH;
    float subBottom = state.y + (centerBased ? halfH : state.height);

    // Infer the authoring margin from the sub's distance to the reference
    // edge and scale it proportionally to the active area height. This preserves
    // the original padding intent when clamping subs into the active area.
    if (refHeight > 0.0f && activeHeight > 0.0f)
    {
      if (subBottom > activeBottom)
      {
        float bottomGap = std::max(0.0f, ref.y2 - subBottom);
        float padding = (bottomGap / refHeight) * activeHeight;
        state.y = activeBottom - padding - (centerBased ? halfH : state.height);
      }
      if (subTop < activeTop)
      {
        float topGap = std::max(0.0f, subTop - ref.y1);
        float padding = (topGap / refHeight) * activeHeight;
        state.y = activeTop + padding + halfH;
      }
    }
  }

  return state;
}

bool CRenderer::HasOverlay(const OverlayBatch& overlays)
{
  bool hasOverlay = false;

  std::unique_lock<CCriticalSection> lock(m_section);

  for (auto it = overlays.begin(); it != overlays.end(); ++it)
  {
    if (it->overlay_dvd)
    {
      hasOverlay = true;
      break;
    }
  }
  return hasOverlay;
}

bool CRenderer::HasTextOverlay(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);

  for (const auto& e : overlays)
  {
    if (e.overlay_dvd &&
        (e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_TEXT) ||
         e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_SSA)))
      return true;
  }
  return false;
}

bool CRenderer::HasImageOverlay(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);

  for (const auto& e : overlays)
  {
    if (e.overlay_dvd && !e.overlay_dvd->IsDiscMenuOverlay() &&
        e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_IMAGE))
      return true;
  }
  return false;
}

bool CRenderer::HasDiscMenuOverlay(const OverlayBatch& overlays)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  for (const auto& e : overlays)
  {
    if (e.overlay_dvd && e.overlay_dvd->IsDiscMenuOverlay() &&
        e.overlay_dvd->IsOverlayType(DVDOVERLAY_TYPE_IMAGE))
      return true;
  }
  return false;
}

bool CRenderer::HasImageSubOutsideActiveArea(const PreparedOverlays& items, const CRect& active)
{
  if (active.IsEmpty())
    return false;
  for (const auto& item : items)
  {
    if (!item.overlay->m_isBitmapOverlay || item.overlay->m_discMenuOverlay)
      continue;
    if (item.bounds.x1 < active.x1 || item.bounds.x2 > active.x2 ||
        item.bounds.y1 < active.y1 || item.bounds.y2 > active.y2)
      return true;
  }
  return false;
}

void CRenderer::SetActivePicture(const CRect& area, bool restrictToArea, bool applyUserPos)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  m_activePicture = area;
  m_restrictToActivePicture = restrictToArea && !area.IsEmpty();
  SetActiveAreaOffsets(m_restrictToActivePicture ? std::max(0, static_cast<int>(area.y1 - m_rv.y1)) : 0,
      m_restrictToActivePicture ? std::max(0, static_cast<int>(m_rv.y2 - area.y2)) : 0, applyUserPos);
}

void CRenderer::SetVideoRect(CRect &source, CRect &dest, CRect &view)
{
  if (m_rv != view) // Screen resolution is changed
  {
    m_rv = view;
    OnViewChange();
  }
  m_rs = source;
  m_rd = dest;
}

void CRenderer::OnViewChange()
{
  m_isSettingsChanged = true;
}

void CRenderer::SetStereoMode(const std::string &stereomode)
{
  m_stereomode = stereomode;
}

void CRenderer::SetActiveAreaOffsets(int topPixels, int bottomPixels, bool applyUserPos)
{
  if (m_activeAreaTopOffset != topPixels || m_activeAreaBottomOffset != bottomPixels ||
      m_activeAreaApplyUserPos != applyUserPos)
    m_isSettingsChanged = true;
  m_activeAreaTopOffset = topPixels;
  m_activeAreaBottomOffset = bottomPixels;
  m_activeAreaApplyUserPos = applyUserPos;
}

void CRenderer::SetSubtitleVerticalPosition(const int value, bool save)
{
  std::unique_lock<CCriticalSection> lock(m_section);
  m_subtitlePosition = value;

  if (save && m_subtitleAlign == SUBTITLES::Align::MANUAL)
  {
    m_subtitlePosResInfo = POSRESINFO_SAVE_CHANGES;
    // We save the value to XML file settings when playback is stopped
    // to avoid saving to disk too many times
    m_saveSubtitlePosition = true;
  }
}

void CRenderer::ResetSubtitlePosition()
{
  // In the 'pos' var the vertical margin has been substracted because
  // we need to know the actual text baseline position on screen
  int pos{0};
  m_saveSubtitlePosition = false;
  RESOLUTION_INFO resInfo = CServiceBroker::GetWinSystem()->GetGfxContext().GetResInfo();

  if (m_subtitleAlign == SUBTITLES::Align::MANUAL)
  {
    // The position must be fixed to match the subtitle calibration bar
    m_subtitleVerticalMargin = static_cast<int>(
        static_cast<float>(resInfo.iHeight) / 100 *
        CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->GetVerticalMarginPerc());

    m_subtitlePosResInfo = resInfo.iSubtitles;
    pos = resInfo.iSubtitles - m_subtitleVerticalMargin;
  }
  else
  {
    // The position must be relative to the screen frame
    m_subtitleVerticalMargin = static_cast<int>(
        static_cast<float>(m_rv.Height()) / 100 *
        CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()->GetVerticalMarginPerc());

    m_subtitlePosResInfo = static_cast<int>(m_rv.Height());
    pos = static_cast<int>(m_rv.Height()) - m_subtitleVerticalMargin + resInfo.Overscan.top;
  }

  // Update player value (and callback to CRenderer::SetSubtitleVerticalPosition)
  auto& components = CServiceBroker::GetAppComponents();
  const auto appPlayer = components.GetComponent<CApplicationPlayer>();
  appPlayer->SetSubtitleVerticalPosition(pos, false);
}

void CRenderer::CreateSubtitlesStyle()
{
  SUBTITLES::STYLE::style style{};
  const auto settings{CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()};

  style.fontName = settings->GetFontName();
  style.fontSize = static_cast<double>(settings->GetFontSize());

  SUBTITLES::FontStyle fontStyle = settings->GetFontStyle();
  if (fontStyle == SUBTITLES::FontStyle::BOLD_ITALIC)
    style.fontStyle = SUBTITLES::STYLE::FontStyle::BOLD_ITALIC;
  else if (fontStyle == SUBTITLES::FontStyle::BOLD)
    style.fontStyle = SUBTITLES::STYLE::FontStyle::BOLD;
  else if (fontStyle == SUBTITLES::FontStyle::ITALIC)
    style.fontStyle = SUBTITLES::STYLE::FontStyle::ITALIC;

  style.fontColor = settings->GetFontColor();
  style.fontBorderSize = settings->GetBorderSize();
  style.fontBorderColor = settings->GetBorderColor();
  style.fontOpacity = settings->GetFontOpacity();

  SUBTITLES::BackgroundType backgroundType = settings->GetBackgroundType();
  if (backgroundType == SUBTITLES::BackgroundType::NONE)
    style.borderStyle = SUBTITLES::STYLE::BorderType::OUTLINE_NO_SHADOW;
  else if (backgroundType == SUBTITLES::BackgroundType::SHADOW)
    style.borderStyle = SUBTITLES::STYLE::BorderType::OUTLINE;
  else if (backgroundType == SUBTITLES::BackgroundType::BOX)
    style.borderStyle = SUBTITLES::STYLE::BorderType::BOX;
  else if (backgroundType == SUBTITLES::BackgroundType::SQUAREBOX)
    style.borderStyle = SUBTITLES::STYLE::BorderType::SQUARE_BOX;

  style.backgroundColor = settings->GetBackgroundColor();
  style.backgroundOpacity = settings->GetBackgroundOpacity();

  style.shadowColor = settings->GetShadowColor();
  style.shadowOpacity = settings->GetShadowOpacity();
  style.shadowSize = settings->GetShadowSize();

  SUBTITLES::Align subAlign = settings->GetAlignment();
  if (subAlign == SUBTITLES::Align::TOP_INSIDE || subAlign == SUBTITLES::Align::TOP_OUTSIDE)
    style.alignment = SUBTITLES::STYLE::FontAlign::TOP_CENTER;
  else
    style.alignment = SUBTITLES::STYLE::FontAlign::SUB_CENTER;

  if (settings->IsOverrideAss())
  {
    style.assOverrideFont = settings->IsOverrideFonts();

    SUBTITLES::OverrideStyles overrideStyles = settings->GetOverrideStyles();
    if (overrideStyles == SUBTITLES::OverrideStyles::POSITIONS)
      style.assOverrideStyles = SUBTITLES::STYLE::OverrideStyles::POSITIONS;
    else if (overrideStyles == SUBTITLES::OverrideStyles::STYLES)
      style.assOverrideStyles = SUBTITLES::STYLE::OverrideStyles::STYLES;
    else if (overrideStyles == SUBTITLES::OverrideStyles::STYLES_POSITIONS)
      style.assOverrideStyles = SUBTITLES::STYLE::OverrideStyles::STYLES_POSITIONS;
    else
      style.assOverrideStyles = SUBTITLES::STYLE::OverrideStyles::DISABLED;
  }

  // Changing vertical margin while in playback causes side effects when you
  // rewind the video, displaying the previous text position (test Libass 15.2)
  // for now vertical margin setting will be disabled during playback
  style.marginVertical =
      static_cast<int>(SUBTITLES::STYLE::VIEWPORT_HEIGHT / 100 *
                       static_cast<double>(settings->GetVerticalMarginPerc()));

  style.blur = settings->GetBlurSize();

  // Publish only a complete const object; no mutable shared alias escapes.
  m_overlayStyle = std::make_shared<const SUBTITLES::STYLE::style>(std::move(style));
}

std::shared_ptr<COverlay> CRenderer::ConvertLibass(
    const CDVDOverlayLibass& o,
    double pts,
    bool updateStyle,
    std::shared_ptr<const SUBTITLES::STYLE::style> overlayStyle)
{
  SUBTITLES::STYLE::renderOpts rOpts;
  // Local copy — L5 active area may override alignment per-overlay
  // without corrupting the persistent member for subsequent frames.
  auto subtitleAlign = m_subtitleAlign;

  // libass render in a target area which named as frame. the frame size may bigger than video size,
  // and including margins between video to frame edge. libass allow to render subtitles into the margins.
  // this has been used to show subtitles in the top or bottom "black bar" between video to frame border.
  rOpts.sourceWidth = m_rs.Width();
  rOpts.sourceHeight = m_rs.Height();
  rOpts.videoWidth = m_rd.Width();
  rOpts.videoHeight = m_rd.Height();
  rOpts.frameWidth = m_rv.Width();
  rOpts.frameHeight = m_rv.Height();

  // Set position of subtitles based on video calibration settings
  RESOLUTION_INFO resInfo = CServiceBroker::GetWinSystem()->GetGfxContext().GetResInfo();
  // Keep track of subtitle position value change,
  // can be changed by GUI Calibration or by window mode/resolution change or
  // by user manual change (e.g. keyboard shortcut)
  if (m_subtitlePosResInfo != resInfo.iSubtitles)
  {
    if (m_subtitlePosResInfo == POSRESINFO_SAVE_CHANGES)
    {
      // m_subtitlePosition has been changed
      // and has been requested to save the value to resInfo
      resInfo.iSubtitles = m_subtitlePosition + m_subtitleVerticalMargin;
      CServiceBroker::GetWinSystem()->GetGfxContext().SetResInfo(
          CServiceBroker::GetWinSystem()->GetGfxContext().GetVideoResolution(), resInfo);
      m_subtitlePosResInfo = m_subtitlePosition + m_subtitleVerticalMargin;
    }
    else
      ResetSubtitlePosition();
  }

  rOpts.m_par = resInfo.fPixelRatio;

  // rOpts.position and margins (set to style) can invalidate the text
  // positions to subtitles type that make use of margins to position text on
  // the screen (e.g. ASS/WebVTT) then we allow to set them when position
  // override setting is enabled only
  if (o.IsForcedMargins())
  {
    rOpts.marginsMode = SUBTITLES::STYLE::MarginsMode::DISABLED;
  }
  else if (subtitleAlign == SUBTITLES::Align::MANUAL)
  {
    // When vertical margins are used Libass apply a displacement in percentage
    // of the height available to line position, this displacement causes
    // problems with subtitle calibration bar on Video Calibration window,
    // so when you moving the subtitle bar of the GUI the text will no longer
    // match the bar, this calculation compensates for the displacement.
    // Note also that the displacement compensation will cause a different
    // default position of the text, different from the other alignment positions
    double posPx = static_cast<double>(m_subtitlePosition - resInfo.Overscan.top);

    double frameHeight = static_cast<double>(rOpts.frameHeight);

    if (m_stereomode == "top_bottom" || m_stereomode == "bottom_top")
    {
      // only half-ou video, ou video don't need to correct frame height
      if (rOpts.sourceWidth / rOpts.sourceHeight > 1.2f)
        frameHeight *= 2.0;
    }

    int assPlayResY = o.GetLibassHandler()->GetPlayResY();
    double assVertMargin = static_cast<double>(overlayStyle->marginVertical) *
                           (static_cast<double>(assPlayResY) / 720);

    double vertMarginScaled = assVertMargin / assPlayResY * frameHeight;
    double pos = posPx / (frameHeight - vertMarginScaled);

    rOpts.position = 100 - pos * 100;
  }
  else if (subtitleAlign == SUBTITLES::Align::BOTTOM_OUTSIDE)
  {
    // To keep consistent the position of text as other alignment positions
    // we avoid apply the displacement compensation
    double posPx =
        static_cast<double>(m_subtitlePosition + m_subtitleVerticalMargin - resInfo.Overscan.top);
    rOpts.position = 100 - posPx / static_cast<double>(rOpts.frameHeight) * 100;
  }
  else if (subtitleAlign == SUBTITLES::Align::BOTTOM_INSIDE ||
           subtitleAlign == SUBTITLES::Align::TOP_INSIDE)
  {
    rOpts.marginsMode = SUBTITLES::STYLE::MarginsMode::INSIDE_VIDEO;
  }

  // Set the horizontal text alignment (currently used to improve readability on CC subtitles only)
  // This setting influence style->alignment property
  if (o.IsTextAlignEnabled())
  {
    if (m_subtitleHorizontalAlign == SUBTITLES::HorizontalAlign::LEFT)
      rOpts.horizontalAlignment = SUBTITLES::STYLE::HorizontalAlign::LEFT;
    else if (m_subtitleHorizontalAlign == SUBTITLES::HorizontalAlign::RIGHT)
      rOpts.horizontalAlignment = SUBTITLES::STYLE::HorizontalAlign::RIGHT;
    else
      rOpts.horizontalAlignment = SUBTITLES::STYLE::HorizontalAlign::CENTER;
  }

  // DV L5 active area: restrict subtitles to the active content area.
  // Uses style MarginV to push subs inside the L5 boundaries, keeping the
  // full rendering canvas intact (no font/border/shadow distortion).
  if (m_activeAreaTopOffset > 0 || m_activeAreaBottomOffset > 0)
  {
    rOpts.marginsMode = SUBTITLES::STYLE::MarginsMode::INSIDE_ACTIVE_AREA;
    rOpts.activeAreaTopMargin = m_activeAreaTopOffset;
    rOpts.activeAreaBottomMargin = m_activeAreaBottomOffset;
    rOpts.activeAreaApplyUserPos = m_activeAreaApplyUserPos;
    rOpts.position = 0;
  }

  const auto result = o.GetLibassHandler()->RenderImage(pts, rOpts, updateStyle, overlayStyle);

  // If no images not execute the renderer
  if (!result)
    return nullptr;

  const auto content = o.shared_from_this();
  const auto it = m_textureCache.find(content);
  if (it != m_textureCache.end() && it->second && it->second->IsValid() &&
      it->second->m_libassResult.lock() == result)
    return it->second;

  std::shared_ptr<COverlay> overlay = COverlay::Create(*result, rOpts.frameWidth, rOpts.frameHeight);
  if (overlay)
    overlay->m_libassResult = result;

  m_textureCache[content] = overlay;
  return overlay;
}

std::shared_ptr<COverlay> CRenderer::Convert(const CDVDOverlay& o, double pts)
{
  std::shared_ptr<COverlay> r = NULL;

  if (o.IsOverlayType(DVDOVERLAY_TYPE_TEXT) || o.IsOverlayType(DVDOVERLAY_TYPE_SSA))
  {
    const CDVDOverlayLibass& ovAss = static_cast<const CDVDOverlayLibass&>(o);
    if (!ovAss.GetLibassHandler())
      return nullptr;
    bool updateStyle = !m_overlayStyle || m_isSettingsChanged;
    if (updateStyle)
    {
      m_isSettingsChanged = false;
      LoadSettings();
      CreateSubtitlesStyle();
    }

    r = ConvertLibass(ovAss, pts, updateStyle, m_overlayStyle);

    if (!r)
      return nullptr;
  }
  else
  {
    const auto it = m_textureCache.find(o.shared_from_this());
    if (it != m_textureCache.end())
      r = it->second;
  }

  if (r && !r->IsValid())
    r.reset();

  // A PQ menu texture is built for one route; rebuild it when the disc menu
  // composite turns on or off.
  if (r && o.IsOverlayType(DVDOVERLAY_TYPE_IMAGE) &&
      static_cast<const CDVDOverlayImage&>(o).m_isPqMenuGraphics &&
      r->m_rawPqMenu != CServiceBroker::GetWinSystem()->IsMenuCompositeActive())
    r = nullptr;

  // Likewise disc menu graphics when the output changes between SDR and HDR.
  const bool plainPmaMenu =
      o.IsOverlayType(DVDOVERLAY_TYPE_IMAGE) &&
      COverlay::PlainPremultiplyDiscMenu(static_cast<const CDVDOverlayImage&>(o));
  if (r && r->m_plainPmaMenu != plainPmaMenu)
    r = nullptr;

  if (r)
  {
    r->m_discMenuOverlay = o.IsDiscMenuOverlay();
    return r;
  }

  if (o.IsOverlayType(DVDOVERLAY_TYPE_IMAGE))
  {
    const auto& image = static_cast<const CDVDOverlayImage&>(o);
    r = COverlay::Create(image, m_rs);
    if (r)
      r->m_canPosition = image.m_canPosition;
  }
  else if (o.IsOverlayType(DVDOVERLAY_TYPE_SPU))
    r = COverlay::Create(static_cast<const CDVDOverlaySpu&>(o));

  if (r)
  {
    r->m_discMenuOverlay = o.IsDiscMenuOverlay();
    // Only the GLES texture premultiplies by it; the flag keys the cache.
    r->m_plainPmaMenu = plainPmaMenu;
  }

  m_textureCache[o.shared_from_this()] = r;

  return r;
}

void CRenderer::Notify(const Observable& obs, const ObservableMessage msg)
{
  switch (msg)
  {
    case ObservableMessageSettingsChanged:
    {
      m_isSettingsChanged = true;
      break;
    }
    case ObservableMessagePositionChanged:
    {
      std::unique_lock<CCriticalSection> lock(m_section);
      m_subtitlePosResInfo = POSRESINFO_UNSET;
      break;
    }
    default:
      break;
  }
}

void CRenderer::LoadSettings()
{
  const auto settings{CServiceBroker::GetSettingsComponent()->GetSubtitlesSettings()};
  m_subtitleHorizontalAlign = settings->GetHorizontalAlignment();
  m_subtitleAlign = settings->GetAlignment();
  ResetSubtitlePosition();
}
