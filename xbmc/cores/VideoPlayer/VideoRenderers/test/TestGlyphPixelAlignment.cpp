/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

// Standalone regression check:
// c++ -std=c++17 -Ixbmc this-file.cpp -o /tmp/test-glyph-alignment
#include "cores/VideoPlayer/VideoRenderers/GlyphPixelAlignment.h"

#include <array>
#include <iostream>
#include <limits>

namespace
{
int checks = 0;
int failures = 0;
void Check(bool actual, bool expected, const char* description)
{
  ++checks;
  if (actual != expected)
  {
    ++failures;
    std::cerr << "FAIL: " << description << '\n';
  }
}

constexpr std::array<float, 16> UNIT{2, 0, 0, 0, 0, -2, 0, 0, 0, 0, 1, 0, -1, 1, 0, 1};
constexpr std::array<int, 4> VIEWPORT{0, 0, 1920, 1080};
} // namespace

int main()
{
  using OVERLAY::IsGlyphPixelAligned;
  const auto eligible = [](const std::array<float, 16>& m, const std::array<int, 4>& v = VIEWPORT)
  { return IsGlyphPixelAligned(m.data(), v.data(), 1920.0f, 1080.0f); };
  Check(eligible(UNIT), true, "full-size orthographic mapping");
  Check(IsGlyphPixelAligned(nullptr, VIEWPORT.data(), 1920.0f, 1080.0f), false, "missing matrix");
  Check(IsGlyphPixelAligned(UNIT.data(), nullptr, 1920.0f, 1080.0f), false, "missing viewport");
  auto camera = UNIT;
  for (float& v : camera)
    v *= 1080.0f;
  camera[11] = -1.0f;
  Check(eligible(camera), true, "perspective camera with constant glyph-plane W");
  auto viewport = VIEWPORT;
  viewport[0] = 19;
  viewport[1] = -7;
  Check(eligible(UNIT, viewport), true, "integer viewport origin");
  viewport[2] = 3840;
  viewport[3] = 2160;
  Check(eligible(UNIT, viewport), false, "viewport introduces two-times scale");
  auto matrix = UNIT;
  matrix[0] = 1.0f;
  matrix[5] = -1.0f;
  Check(eligible(matrix, viewport), true, "model compensates viewport scale");
  viewport = VIEWPORT;
  matrix = UNIT;
  matrix[12] = -0.75f;
  Check(eligible(matrix), true, "integer 240-pixel translation");
  matrix[12] = -1.0f + 0.25f / 960.0f;
  Check(eligible(matrix), false, "quarter-pixel translation");
  matrix[12] = std::nextafter(-1.0f, 0.0f);
  Check(eligible(matrix), false, "smallest representable fractional translation");
  matrix = UNIT;
  matrix[0] = std::nextafter(2.0f, 3.0f);
  Check(eligible(matrix), false, "nearly unit scaling must not pass");
  matrix[0] = 2.5f;
  Check(eligible(matrix), false, "1.25-times scaling");
  matrix = UNIT;
  matrix[0] = -2.0f;
  Check(eligible(matrix), true, "axis inversion preserves pixel grid");
  matrix[1] = 0.125f;
  Check(eligible(matrix), false, "shear");
  matrix = UNIT;
  matrix[0] = 0.0f;
  matrix[5] = 0.0f;
  matrix[1] = 2.0f;
  matrix[4] = -2.0f;
  Check(eligible(matrix), false, "axis-swap rotation is conservatively unproven");
  matrix = UNIT;
  matrix[3] = 0.001f;
  Check(eligible(matrix), false, "perspective varies across glyph plane");
  matrix = UNIT;
  matrix[15] = 0.0f;
  Check(eligible(matrix), false, "zero clip W");
  matrix[15] = -1.0f;
  Check(eligible(matrix), false, "negative clip W");
  matrix = UNIT;
  for (float& v : matrix)
    v *= std::numeric_limits<float>::denorm_min();
  Check(eligible(matrix), false, "subnormal clip arithmetic must stay linear");
  matrix = UNIT;
  for (float& v : matrix)
    v *= 131072.0f;
  Check(eligible(matrix), false, "extreme homogeneous scale is unproven");
  for (int i = 0; i < 16; ++i)
  {
    matrix = UNIT;
    matrix[i] = std::numeric_limits<float>::quiet_NaN();
    Check(eligible(matrix), false, "nonfinite matrix coefficient");
  }
  Check(IsGlyphPixelAligned(UNIT.data(), VIEWPORT.data(), 0.0f, 1080.0f), false,
        "zero raster width");
  Check(IsGlyphPixelAligned(UNIT.data(), VIEWPORT.data(), 1920.5f, 1080.0f), false,
        "fractional raster dimensions");
  viewport[2] = 0;
  Check(eligible(UNIT, viewport), false, "empty viewport");
  viewport = VIEWPORT;
  viewport[2] = 65537;
  Check(eligible(UNIT, viewport), false, "viewport outside exact-product bounds");
  viewport = VIEWPORT;
  viewport[0] = 65537;
  Check(eligible(UNIT, viewport), false, "viewport origin outside proof bounds");
  for (int i = -100; i <= 2000; ++i)
    Check(OVERLAY::IsGlyphVertexOnRasterGrid(i / 1920.0f, 1031.0f / 1080.0f, 1920.0f, 1080.0f),
          true, "integer glyph normalization including outside-canvas vertices");
  Check(OVERLAY::IsGlyphVertexOnRasterGrid(0.25f / 1920.0f, 0.0f, 1920.0f, 1080.0f), false,
        "fractional glyph vertex");
  Check(OVERLAY::IsGlyphVertexOnRasterGrid(std::numeric_limits<float>::infinity(), 0.0f, 1920.0f,
                                           1080.0f),
        false, "invalid glyph vertex");
  // The same cached glyph must follow the current mapping, in both directions.
  for (int i = 0; i < 100; ++i)
  {
    matrix = UNIT;
    Check(eligible(matrix), true, "linear-to-nearest transition");
    matrix[12] += 0.25f;
    Check(eligible(matrix), true, "integer position remains nearest");
    matrix[12] += 0.125f / 960.0f;
    Check(eligible(matrix), false, "nearest-to-linear transition");
    matrix = UNIT;
    matrix[5] *= 0.75f;
    Check(eligible(matrix), false, "scaled draw stays linear");
  }
  std::cout << checks << " checks; " << failures << " failures\n";
  return failures ? 1 : 0;
}
