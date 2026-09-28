/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <stdint.h>
#include <stdlib.h>
#include <vector>

class CDVDOverlayImage;
class CDVDOverlaySpu;
class CDVDOverlaySSA;
typedef struct ass_image ASS_Image;

namespace OVERLAY
{

struct SQuad
{
  int u, v;
  unsigned char r, g, b, a;
  int x, y;
  int w, h;
};

struct SQuads
{
  int size_x{0};
  int size_y{0};
  std::vector<uint8_t> texture;
  std::vector<SQuad> quad;
};

// Linear-light alpha premultiplication for overlay pixels.
// Premultiplying gamma-encoded values directly produces edges that are
// too dark, especially pronounced when the OSD is displayed in HDR/PQ mode.
// These helpers perform the multiplication in linear space (gamma 2.2).
float SrgbToLinear(int v);
int LinearToSrgb8(float v);

// linearLight false: plain premultiply, for an overlay blended as drawn (see
// COverlayTextureGLES).
void convert_rgba(const CDVDOverlayImage& o,
                  bool mergealpha,
                  std::vector<uint32_t>& rgba,
                  bool linearLight = true);
void convert_rgba(const CDVDOverlaySpu& o,
                  bool mergealpha,
                  int& min_x,
                  int& max_x,
                  int& min_y,
                  int& max_y,
                  std::vector<uint32_t>& rgba);
bool convert_quad(ASS_Image* images, SQuads& quads, int max_x);

/*!
 * \brief Like convert_quad, but splits the glyph bitmaps across as many atlas
 * pages as needed so that no page exceeds maxTextureSize in either dimension.
 * Required for heavy typesetting signs whose combined bitmap area would
 * overflow a single GPU texture. Images are packed in list order, so the
 * painter's-algorithm blend order is preserved within and across pages
 * (page 0 must be drawn before page 1, etc.).
 * \return True if at least one visible glyph was packed.
 */
bool convert_quads(ASS_Image* images, std::vector<SQuads>& pages, int maxTextureSize);
int GetStereoscopicDepth(bool isPgs, int subtitleDepth);

} // namespace OVERLAY
