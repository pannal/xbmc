/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include "utils/Geometry.h"

#include <vector>

namespace OVERLAY
{
enum class BitmapSubtitlePosition
{
  ORIGINAL = 0,
  BOTTOM_PICTURE,
  TOP_PICTURE,
  BOTTOM_SCREEN,
  TOP_SCREEN,
  MANUAL,
};

struct BitmapSubtitleRegion
{
  CRect authored;
  CRect scaled;
  CRect reference;
  bool eligible{false};
};

struct BitmapSubtitlePlacement
{
  bool selected{false};
  float offset{0.0f};
  float horizontalOffset{0.0f};
};

// Map a valid coded-frame active rectangle through crop, scale and view clipping.
// Missing/invalid refinements fall back to the visible renderer video rectangle.
CRect GetActivePictureArea(const CRect& source,
                           const CRect& destination,
                           const CRect& view,
                           const CRect& codedFrame,
                           const CRect& inFrameArea);

// An aspect override only refines the known picture; it never enlarges it.
CRect GetBitmapSubtitleArea(const CRect& view,
                            const CRect& video,
                            const CRect& active,
                            float aspect);

// Select the lowest cluster of small, lower-half dialogue regions before zoom.
// One displacement preserves split dialogue. Confinement wins over screen/manual
// modes; oversized blocks keep their size/layout and are centered on the limit.
std::vector<BitmapSubtitlePlacement> PlaceBitmapSubtitles(
    const std::vector<BitmapSubtitleRegion>& regions,
    const CRect& view,
    const CRect& active,
    const CRect& limit,
    BitmapSubtitlePosition position,
    float offsetPercent,
    float marginPercent,
    bool restrictToActiveArea,
    float zoom = 1.0f);
} // namespace OVERLAY
