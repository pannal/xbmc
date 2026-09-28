/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OverlayRendererUtil.h"

#include "ServiceBroker.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySSA.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySpu.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "windowing/GraphicContext.h"

#include <array>
#include <cmath>

namespace OVERLAY
{

float SrgbToLinear(int v)
{
  static const auto lut = []() {
    std::array<float, 256> t{};
    for (int i = 0; i < 256; i++)
      t[i] = std::pow(i / 255.0f, 2.2f);
    return t;
  }();
  return lut[v & 0xff];
}

int LinearToSrgb8(float v)
{
  if (v <= 0.0f)
    return 0;
  if (v >= 1.0f)
    return 255;
  return static_cast<int>(std::pow(v, 1.0f / 2.2f) * 255.0f + 0.5f);
}

// Premultiply alpha in linear light so semi-transparent edges don't appear dark
// on HDR displays (where gamma-space PMA gets mapped to very low PQ luminance).
// Also improves SDR correctness for anti-aliased overlay edges.
static uint32_t build_rgba(int a, int r, int g, int b, bool mergealpha, bool linearLight = true)
{
  if (mergealpha && !linearLight)
  {
    return a << PIXEL_ASHIFT | ((r * a + 127) / 255) << PIXEL_RSHIFT |
           ((g * a + 127) / 255) << PIXEL_GSHIFT | ((b * a + 127) / 255) << PIXEL_BSHIFT;
  }
  if (mergealpha)
  {
    const float af = a / 255.0f;
    const int rp = LinearToSrgb8(SrgbToLinear(r) * af);
    const int gp = LinearToSrgb8(SrgbToLinear(g) * af);
    const int bp = LinearToSrgb8(SrgbToLinear(b) * af);
    return a << PIXEL_ASHIFT | rp << PIXEL_RSHIFT | gp << PIXEL_GSHIFT | bp << PIXEL_BSHIFT;
  }
  else
    return a << PIXEL_ASHIFT
         | r << PIXEL_RSHIFT
         | g << PIXEL_GSHIFT
         | b << PIXEL_BSHIFT;
}

#define clamp(x) (x) > 255.0 ? 255 : ((x) < 0.0 ? 0 : (int)(x + 0.5))
static uint32_t build_rgba(const int yuv[3], int alpha, bool mergealpha)
{
  int    a = alpha + ( (alpha << 4) & 0xff );
  double r = 1.164 * (yuv[0] - 16)                          + 1.596 * (yuv[2] - 128);
  double g = 1.164 * (yuv[0] - 16) - 0.391 * (yuv[1] - 128) - 0.813 * (yuv[2] - 128);
  double b = 1.164 * (yuv[0] - 16) + 2.018 * (yuv[1] - 128);
  return build_rgba(a, clamp(r), clamp(g), clamp(b), mergealpha);
}
#undef clamp

void convert_rgba(const CDVDOverlayImage& o,
                  bool mergealpha,
                  std::vector<uint32_t>& rgba,
                  bool linearLight)
{
  uint32_t palette[256] = {};
  for (size_t i = 0; i < o.palette.size(); i++)
    palette[i] = build_rgba(
        (o.palette[i] >> PIXEL_ASHIFT) & 0xff, (o.palette[i] >> PIXEL_RSHIFT) & 0xff,
        (o.palette[i] >> PIXEL_GSHIFT) & 0xff, (o.palette[i] >> PIXEL_BSHIFT) & 0xff, mergealpha,
        linearLight);

  for (int row = 0; row < o.height; row++)
    for (int col = 0; col < o.width; col++)
      rgba[row * o.width + col] = palette[o.pixels[row * o.linesize + col]];
}

void convert_rgba(const CDVDOverlaySpu& o,
                  bool mergealpha,
                  int& min_x,
                  int& max_x,
                  int& min_y,
                  int& max_y,
                  std::vector<uint32_t>& rgba)
{
  uint32_t palette[8];
  for (int i = 0; i < 4; i++)
  {
    palette[i] = build_rgba(o.color[i], o.alpha[i], mergealpha);
    palette[i + 4] = build_rgba(o.highlight_color[i], o.highlight_alpha[i], mergealpha);
  }

  uint32_t  color;
  uint32_t* trg;
  uint16_t* src;

  int len, idx, draw;

  int btn_x_start = 0
    , btn_x_end   = 0
    , btn_y_start = 0
    , btn_y_end   = 0;

  if (o.bForced)
  {
    btn_x_start = o.crop_i_x_start - o.x;
    btn_x_end = o.crop_i_x_end - o.x;
    btn_y_start = o.crop_i_y_start - o.y;
    btn_y_end = o.crop_i_y_end - o.y;
  }

  min_x = o.width;
  max_x = 0;
  min_y = o.height;
  max_y = 0;

  trg = rgba.data();
  src = (uint16_t*)o.result;

  for (int y = 0; y < o.height; y++)
  {
    for (int x = 0; x < o.width; x += len)
    {
      /* Get the RLE part, then draw the line */
      idx = *src & 0x3;
      len = *src++ >> 2;

      while( len > 0 )
      {
        draw  = len;
        color = palette[idx];

        if (y >= btn_y_start && y <= btn_y_end)
        {
          if     ( x <  btn_x_start && x + len >= btn_x_start) // starts outside
            draw = btn_x_start - x;
          else if( x >= btn_x_start && x       <= btn_x_end)   // starts inside
          {
            color = palette[idx + 4];
            draw  = btn_x_end - x + 1;
          }
        }
        /* make sure we are not requested to draw to far */
        /* that part will be taken care of in next pass */
        if( draw > len )
          draw = len;

        /* calculate cropping */
        if(color & 0xff000000)
        {
          if(x < min_x)
            min_x = x;
          if(y < min_y)
            min_y = y;
          if(x + draw > max_x)
            max_x = x + draw;
          if(y + 1    > max_y)
            max_y = y + 1;
        }

        for(int i = 0; i < draw; i++)
          trg[x + i] = color;

        len -= draw;
        x   += draw;
      }
    }
    trg += o.width;
  }

  /* if nothing visible, just output a dummy pixel */
  if(max_x <= min_x
  || max_y <= min_y)
  {
    max_y = max_x = 1;
    min_y = min_x = 0;
  }
}

bool convert_quad(ASS_Image* images, SQuads& quads, int max_x)
{
  ASS_Image* img;
  int count = 0;

  if (!images)
    return false;

  // first calculate how many glyph we have and the total x length

  for(img = images; img; img = img->next)
  {
    // fully transparent or width or height is 0 -> not displayed
    if((img->color & 0xff) == 0xff || img->w == 0 || img->h == 0)
      continue;

    quads.size_x += img->w + 1;
    count++;
  }

  if (count == 0)
    return false;

  if (quads.size_x > max_x)
    quads.size_x = max_x;

  int curr_x = 0;
  int curr_y = 0;

  // calculate the y size of the texture

  for(img = images; img; img = img->next)
  {
    if((img->color & 0xff) == 0xff || img->w == 0 || img->h == 0)
      continue;

    // check if we need to split to new line
    if (curr_x + img->w >= quads.size_x)
    {
      quads.size_y += curr_y + 1;
      curr_x = 0;
      curr_y = 0;
    }

    curr_x += img->w + 1;

    if (img->h > curr_y)
      curr_y = img->h;
  }

  quads.size_y += curr_y + 1;

  // allocate space for the glyph positions and texturedata
  quads.quad.resize(count);
  quads.texture.resize(quads.size_x * quads.size_y);

  SQuad* v = quads.quad.data();
  uint8_t* data = quads.texture.data();

  int y = 0;

  curr_x = 0;
  curr_y = 0;

  for (img = images; img; img = img->next)
  {
    if ((img->color & 0xff) == 0xff || img->w == 0 || img->h == 0)
      continue;

    unsigned int color = img->color;
    unsigned int alpha = (color & 0xff);

    if (curr_x + img->w >= quads.size_x)
    {
      curr_y += y + 1;
      curr_x = 0;
      y = 0;
      data = quads.texture.data() + curr_y * quads.size_x;
    }

    unsigned int r = ((color >> 24) & 0xff);
    unsigned int g = ((color >> 16) & 0xff);
    unsigned int b = ((color >> 8 ) & 0xff);

    v->a = 255 - alpha;
    v->r = r;
    v->g = g;
    v->b = b;

    v->u = curr_x;
    v->v = curr_y;

    v->x = img->dst_x;
    v->y = img->dst_y;

    v->w = img->w;
    v->h = img->h;

    v++;

    for (int i = 0; i < img->h; i++)
      memcpy(data + quads.size_x * i, img->bitmap + img->stride * i, img->w);

    if (img->h > y)
      y = img->h;

    curr_x += img->w + 1;
    data   += img->w + 1;
  }
  return true;
}

bool convert_quads(ASS_Image* images, std::vector<SQuads>& pages, int maxTextureSize)
{
  pages.clear();

  if (!images || maxTextureSize <= 0)
    return false;

  // Phase 1: plan placements. Shelf-pack the visible bitmaps in list order,
  // wrapping to a new shelf when a row is full and to a new page when a shelf
  // would exceed the texture height. Keeping list order means earlier images
  // always land on the same or an earlier page, so drawing the pages in order
  // reproduces libass' painter's-algorithm blending exactly.
  struct Placement
  {
    ASS_Image* img;
    int page;
    int x;
    int y;
  };
  std::vector<Placement> placements;
  std::vector<int> pageWidth;
  std::vector<int> pageHeight;

  int page = 0;
  int curX = 0; // pen x within the active shelf
  int shelfY = 0; // top y of the active shelf
  int shelfH = 0; // height of the active shelf
  int usedW = 0; // widest row used on the active page

  auto finalizePage = [&]() {
    pageWidth.push_back(usedW);
    pageHeight.push_back(shelfY + shelfH + 1);
  };

  for (ASS_Image* img = images; img; img = img->next)
  {
    // fully transparent or width or height is 0 -> not displayed
    if ((img->color & 0xff) == 0xff || img->w == 0 || img->h == 0)
      continue;

    // A single bitmap larger than a full texture cannot be represented; skip
    // it rather than corrupting an entire page.
    if (img->w + 1 > maxTextureSize || img->h + 1 > maxTextureSize)
      continue;

    // Wrap to a new shelf if the bitmap does not fit in the current row.
    if (curX + img->w + 1 > maxTextureSize)
    {
      shelfY += shelfH + 1;
      curX = 0;
      shelfH = 0;
    }

    // Start a new page if the shelf would not fit vertically.
    if (shelfY + img->h + 1 > maxTextureSize)
    {
      finalizePage();
      page++;
      curX = 0;
      shelfY = 0;
      shelfH = 0;
      usedW = 0;
    }

    placements.push_back({img, page, curX, shelfY});

    curX += img->w + 1;
    if (img->h > shelfH)
      shelfH = img->h;
    if (curX > usedW)
      usedW = curX;
  }

  if (placements.empty())
    return false;

  finalizePage(); // finalize the last active page

  // Phase 2: allocate the page atlases and blit the bitmaps + emit quads.
  pages.resize(page + 1);
  for (size_t p = 0; p < pages.size(); p++)
  {
    pages[p].size_x = pageWidth[p];
    pages[p].size_y = pageHeight[p];
    pages[p].texture.assign(static_cast<size_t>(pageWidth[p]) * pageHeight[p], 0);
  }

  for (const Placement& pl : placements)
  {
    SQuads& quads = pages[pl.page];
    const unsigned int color = pl.img->color;
    const unsigned int alpha = (color & 0xff);

    SQuad quad;
    quad.a = 255 - alpha;
    quad.r = (color >> 24) & 0xff;
    quad.g = (color >> 16) & 0xff;
    quad.b = (color >> 8) & 0xff;
    quad.u = pl.x;
    quad.v = pl.y;
    quad.x = pl.img->dst_x;
    quad.y = pl.img->dst_y;
    quad.w = pl.img->w;
    quad.h = pl.img->h;
    quads.quad.push_back(quad);

    uint8_t* data = quads.texture.data() + static_cast<size_t>(pl.y) * quads.size_x + pl.x;
    for (int i = 0; i < pl.img->h; i++)
      memcpy(data + static_cast<size_t>(quads.size_x) * i, pl.img->bitmap + pl.img->stride * i,
             pl.img->w);
  }

  return true;
}

int GetStereoscopicDepth(bool isPgs, int subtitleDepth)
{
  RENDER_STEREO_MODE stereoMode = CServiceBroker::GetWinSystem()->GetGfxContext().GetStereoMode();

  if (stereoMode == RENDER_STEREO_MODE_MONO || stereoMode == RENDER_STEREO_MODE_OFF)
  {
    // 2D display, so there's no subtitle depth
    return 0;
  }

  // get configured depth
  int depth = CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(CSettings::SETTING_SUBTITLES_STEREOSCOPICDEPTH);

  // in case of MVC playback and PGS subtitles, use the subtitle depth info additionally to the configured one
  if(stereoMode == RENDER_STEREO_MODE_HARDWAREBASED && isPgs)
  {
    depth += subtitleDepth;
  }

  // correct depth according to the current left/right eye view
  return depth * (CServiceBroker::GetWinSystem()->GetGfxContext().GetStereoView() == RENDER_STEREO_VIEW_LEFT ? 1 : -1);
}

}
