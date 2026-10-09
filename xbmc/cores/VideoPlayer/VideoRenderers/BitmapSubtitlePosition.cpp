/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "BitmapSubtitlePosition.h"

#include <algorithm>
#include <cmath>
#include <limits>

using namespace OVERLAY;

namespace
{
bool Valid(const CRect& r)
{
  return std::isfinite(r.x1) && std::isfinite(r.y1) && std::isfinite(r.x2) && std::isfinite(r.y2) &&
         r.Width() > 0.0f && r.Height() > 0.0f;
}

float FitShift(float first, float last, float low, float high, float shift)
{
  if (last - first > high - low)
    return (low + high - first - last) * 0.5f;
  return std::clamp(shift, low - first, high - last);
}
} // namespace

CRect OVERLAY::GetActivePictureArea(const CRect& source,
                                    const CRect& destination,
                                    const CRect& view,
                                    const CRect& codedFrame,
                                    const CRect& inFrameArea)
{
  if (!Valid(source) || !Valid(destination) || !Valid(view))
    return {};
  CRect visible = destination;
  visible.Intersect(view);
  if (!Valid(visible) || !Valid(codedFrame) || !Valid(inFrameArea) ||
      inFrameArea.x1 < codedFrame.x1 || inFrameArea.y1 < codedFrame.y1 ||
      inFrameArea.x2 > codedFrame.x2 || inFrameArea.y2 > codedFrame.y2)
    return visible;
  CRect crop = inFrameArea;
  crop.Intersect(source);
  if (!Valid(crop))
    return visible;
  const float scaleX = destination.Width() / source.Width();
  const float scaleY = destination.Height() / source.Height();
  CRect area(destination.x1 + (crop.x1 - source.x1) * scaleX,
             destination.y1 + (crop.y1 - source.y1) * scaleY,
             destination.x1 + (crop.x2 - source.x1) * scaleX,
             destination.y1 + (crop.y2 - source.y1) * scaleY);
  area.Intersect(visible);
  return Valid(area) ? area : visible;
}

CRect OVERLAY::GetBitmapSubtitleArea(const CRect& view,
                                     const CRect& video,
                                     const CRect& active,
                                     float aspect)
{
  CRect area = Valid(active) ? active : video;
  area.Intersect(view);
  if (std::isfinite(aspect) && aspect > 0.0f && Valid(video))
  {
    const float height = std::min(video.Height(), video.Width() / aspect);
    const float center = (video.y1 + video.y2) * 0.5f;
    area.Intersect(CRect(video.x1, center - height * 0.5f, video.x2, center + height * 0.5f));
  }
  return area;
}

std::vector<BitmapSubtitlePlacement> OVERLAY::PlaceBitmapSubtitles(
    const std::vector<BitmapSubtitleRegion>& regions,
    const CRect& view,
    const CRect& active,
    const CRect& limit,
    BitmapSubtitlePosition position,
    float offsetPercent,
    float marginPercent,
    bool restrictToActiveArea,
    float zoom)
{
  std::vector<BitmapSubtitlePlacement> result(regions.size());
  if (!Valid(view) || !Valid(active) || !Valid(limit) ||
      (position == BitmapSubtitlePosition::ORIGINAL && !restrictToActiveArea))
    return result;

  auto candidate = [&view](const BitmapSubtitleRegion& region)
  {
    const float center = (region.authored.y1 + region.authored.y2) * 0.5f;
    return region.eligible && Valid(region.authored) && Valid(region.scaled) &&
           region.authored.Height() < view.Height() * 0.15f && center >= (view.y1 + view.y2) * 0.5f;
  };
  float anchor = view.y1;
  bool found = false;
  for (const auto& region : regions)
  {
    if (candidate(region))
    {
      anchor = std::max(anchor, (region.authored.y1 + region.authored.y2) * 0.5f);
      found = true;
    }
  }
  if (!found)
    return result;

  CRect block;
  float authorBottom = std::numeric_limits<float>::lowest();
  float authorTop = std::numeric_limits<float>::max();
  CRect reference = view;
  bool first = true;
  for (size_t i = 0; i < regions.size(); ++i)
  {
    const auto& region = regions[i];
    if (candidate(region) &&
        (region.authored.y1 + region.authored.y2) * 0.5f >= anchor - view.Height() * 0.20f)
    {
      result[i].selected = true;
      if (first)
      {
        block = region.scaled;
        reference = Valid(region.reference) ? region.reference : view;
        first = false;
      }
      else
        block.Union(region.scaled);
      authorBottom = std::max(authorBottom, region.authored.y2);
      authorTop = std::min(authorTop, region.authored.y1);
    }
  }

  // Zoom the whole authored block about its bottom/center, including the gaps.
  // Per-region zoom was computed by the existing renderer; retain those sizes
  // but correct each region's origin to the common pivot before displacement.
  CRect authoredBlock;
  bool firstAuthored = true;
  for (size_t i = 0; i < regions.size(); ++i)
    if (result[i].selected)
    {
      if (firstAuthored)
      {
        authoredBlock = regions[i].authored;
        firstAuthored = false;
      }
      else
        authoredBlock.Union(regions[i].authored);
    }
  const float centerX = (authoredBlock.x1 + authoredBlock.x2) * 0.5f;
  std::vector<CRect> scaled(regions.size());
  bool firstScaled = true;
  for (size_t i = 0; i < regions.size(); ++i)
    if (result[i].selected)
    {
      const auto& a = regions[i].authored;
      scaled[i] = CRect(
          centerX + (a.x1 - centerX) * zoom, authoredBlock.y2 + (a.y1 - authoredBlock.y2) * zoom,
          centerX + (a.x2 - centerX) * zoom, authoredBlock.y2 + (a.y2 - authoredBlock.y2) * zoom);
      if (firstScaled)
      {
        block = scaled[i];
        firstScaled = false;
      }
      else
        block.Union(scaled[i]);
    }

  const float margin = view.Height() * std::clamp(marginPercent, 0.0f, 20.0f) / 100.0f;
  const float bottomPadding = std::max(margin, std::max(0.0f, reference.y2 - authorBottom) /
                                                   reference.Height() * active.Height());
  const float topPadding = std::max(margin, std::max(0.0f, authorTop - reference.y1) /
                                                reference.Height() * active.Height());
  float shift = 0.0f;
  switch (position)
  {
    case BitmapSubtitlePosition::BOTTOM_PICTURE:
      shift = active.y2 - bottomPadding - block.y2;
      break;
    case BitmapSubtitlePosition::TOP_PICTURE:
      // Bottom-authored dialogue has no meaningful top authoring margin.
      shift = active.y1 + std::max(margin, bottomPadding) - block.y1;
      break;
    case BitmapSubtitlePosition::BOTTOM_SCREEN:
      shift = view.y2 - margin - block.y2;
      break;
    case BitmapSubtitlePosition::TOP_SCREEN:
      shift = view.y1 + margin - block.y1;
      break;
    case BitmapSubtitlePosition::MANUAL:
      shift = -offsetPercent * view.Height() / 100.0f;
      break;
    case BitmapSubtitlePosition::ORIGINAL:
      if (block.y2 > active.y2)
        shift = active.y2 - bottomPadding - block.y2;
      else if (block.y1 < active.y1)
        shift = active.y1 + topPadding - block.y1;
      break;
  }
  const float boundedMargin =
      std::min(margin, std::max(0.0f, (limit.Height() - block.Height()) * 0.5f));
  shift = FitShift(block.y1, block.y2, limit.y1 + boundedMargin, limit.y2 - boundedMargin, shift);
  const float horizontal =
      restrictToActiveArea ? FitShift(block.x1, block.x2, limit.x1, limit.x2, 0.0f) : 0.0f;
  for (size_t i = 0; i < result.size(); ++i)
  {
    if (result[i].selected)
    {
      result[i].offset = shift + scaled[i].y1 - regions[i].scaled.y1;
      result[i].horizontalOffset = horizontal + scaled[i].x1 - regions[i].scaled.x1;
    }
  }
  return result;
}
