/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cmath>

namespace OVERLAY
{
// Glyph positions are integer libass pixels, normalized by the raster dimensions
// before upload. Prove that the complete uploaded matrix and current viewport
// map that grid to framebuffer pixel boundaries. Do not approximate a fractional
// translation or a nearly unit scale as an aligned mapping.
inline bool IsGlyphPixelAligned(const float* matrix,
                                const int* viewport,
                                float rasterWidth,
                                float rasterHeight)
{
  if (!matrix || !viewport)
    return false;

  // Bound integer factors so the products of float matrix values and integers
  // below are exact in double. Unusual or invalid dimensions retain filtering.
  constexpr double maxDimension = 65536.0;
  constexpr double maxOrigin = 65536.0;
  const double width = static_cast<double>(rasterWidth);
  const double height = static_cast<double>(rasterHeight);
  if (!std::isfinite(width) || !std::isfinite(height) || width <= 0.0 || height <= 0.0 ||
      width > maxDimension || height > maxDimension || width != std::trunc(width) ||
      height != std::trunc(height) || viewport[2] <= 0 || viewport[3] <= 0 ||
      viewport[2] > maxDimension || viewport[3] > maxDimension ||
      std::abs(static_cast<double>(viewport[0])) > maxDimension ||
      std::abs(static_cast<double>(viewport[1])) > maxDimension)
    return false;

  for (int i = 0; i < 16; ++i)
    if (!std::isfinite(matrix[i]))
      return false;

  // On the glyph z=0 plane, clip-W must be constant and positive. A perspective
  // camera may satisfy this condition; perspective across the plane may not.
  // Keep the relevant GPU arithmetic in its normal finite domain as well.
  if (matrix[3] != 0.0f || matrix[7] != 0.0f || matrix[15] < 1.0f / 65536.0f ||
      matrix[15] > 65536.0f || matrix[1] != 0.0f || matrix[4] != 0.0f)
    return false;

  const double w = static_cast<double>(matrix[15]);
  const double viewportWidth = static_cast<double>(viewport[2]);
  const double viewportHeight = static_cast<double>(viewport[3]);
  if (std::abs(static_cast<double>(matrix[0])) * viewportWidth != 2.0 * width * w ||
      std::abs(static_cast<double>(matrix[5])) * viewportHeight != 2.0 * height * w)
    return false;

  const auto alignedOrigin = [w](float translation, int offset, int size)
  {
    const double t = static_cast<double>(translation);
    const double origin =
        (t / w + 1.0) * static_cast<double>(size) * 0.5 + static_cast<double>(offset);
    if (!std::isfinite(origin) || std::abs(origin) > maxOrigin)
      return false;
    const double integerOrigin = std::round(origin);
    // Check the rational identity, rather than a rounded division or epsilon.
    return t * static_cast<double>(size) ==
           (2.0 * (integerOrigin - static_cast<double>(offset)) - static_cast<double>(size)) * w;
  };
  return alignedOrigin(matrix[12], viewport[0], viewport[2]) &&
         alignedOrigin(matrix[13], viewport[1], viewport[3]);
}

inline bool IsGlyphVertexOnRasterGrid(float x, float y, float rasterWidth, float rasterHeight)
{
  // Only accommodate conversion of integer glyph vertices to normalized float.
  // The transform itself must already have passed the exact proof above.
  constexpr double maxConversionError = 1.0 / 4096.0;
  const double rasterX = static_cast<double>(x) * static_cast<double>(rasterWidth);
  const double rasterY = static_cast<double>(y) * static_cast<double>(rasterHeight);
  return std::isfinite(rasterX) && std::isfinite(rasterY) && std::abs(rasterX) <= 65536.0 &&
         std::abs(rasterY) <= 65536.0 &&
         std::abs(rasterX - std::round(rasterX)) <= maxConversionError &&
         std::abs(rasterY - std::round(rasterY)) <= maxConversionError;
}
} // namespace OVERLAY
