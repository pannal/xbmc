/*
 *      Initial code sponsored by: Voddler Inc (voddler.com)
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OverlayRendererGLES.h"

#include "LinuxRendererGLES.h"
#include "OverlayRenderer.h"
#include "OverlayRendererUtil.h"
#include "RenderManager.h"
#include "ServiceBroker.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySSA.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySpu.h"
#include "rendering/MatrixGL.h"
#include "rendering/gles/RenderSystemGLES.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/GLUtils.h"
#include "utils/MathUtils.h"
#include "utils/log.h"
#include "windowing/WinSystem.h"

#include <cmath>
#include <utility>

// GLES2.0 cant do CLAMP, but can do CLAMP_TO_EDGE.
#define GL_CLAMP GL_CLAMP_TO_EDGE

#define USE_PREMULTIPLIED_ALPHA 1

using namespace OVERLAY;

static void LoadTexture(GLenum target,
                        GLsizei width,
                        GLsizei height,
                        GLsizei stride,
                        GLfloat* u,
                        GLfloat* v,
                        bool alpha,
                        const GLvoid* pixels)
{
  int width2 = width;
  int height2 = height;
  char* pixelVector = NULL;
  const GLvoid* pixelData = pixels;

  GLenum internalFormat = alpha ? GL_ALPHA : GL_RGBA;
  GLenum externalFormat = alpha ? GL_ALPHA : GL_RGBA;

  int bytesPerPixel = KODI::UTILS::GL::glFormatElementByteCount(externalFormat);

  bool bgraSupported = false;
  CRenderSystemGLES* renderSystem =
      dynamic_cast<CRenderSystemGLES*>(CServiceBroker::GetRenderSystem());

  if (!alpha)
  {
    if (renderSystem->IsExtSupported("GL_EXT_texture_format_BGRA8888") ||
        renderSystem->IsExtSupported("GL_IMG_texture_format_BGRA8888"))
    {
      bgraSupported = true;
      internalFormat = externalFormat = GL_BGRA_EXT;
    }
    else if (renderSystem->IsExtSupported("GL_APPLE_texture_format_BGRA8888"))
    {
      // Apple's implementation does not conform to spec. Instead, they require
      // differing format/internalformat, more like GL.
      bgraSupported = true;
      externalFormat = GL_BGRA_EXT;
    }
  }

  int bytesPerLine = bytesPerPixel * width;

  if (!alpha && !bgraSupported)
  {
    pixelVector = (char*)malloc(bytesPerLine * height);

    const char* src = (const char*)pixels;
    char* dst = pixelVector;
    for (int y = 0; y < height; ++y)
    {
      src = (const char*)pixels + y * stride;
      dst = pixelVector + y * bytesPerLine;

      for (GLsizei i = 0; i < width; i++, src += 4, dst += 4)
      {
        dst[0] = src[2];
        dst[1] = src[1];
        dst[2] = src[0];
        dst[3] = src[3];
      }
    }

    pixelData = pixelVector;
    stride = width;
  }
  /** OpenGL ES does not support strided texture input. Make a copy without stride **/
  else if (stride != bytesPerLine)
  {
    pixelVector = (char*)malloc(bytesPerLine * height);

    const char* src = (const char*)pixels;
    char* dst = pixelVector;
    for (int y = 0; y < height; ++y)
    {
      memcpy(dst, src, bytesPerLine);
      src += stride;
      dst += bytesPerLine;
    }

    pixelData = pixelVector;
    stride = bytesPerLine;
  }

  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

  glTexImage2D(target, 0, internalFormat, width2, height2, 0, externalFormat, GL_UNSIGNED_BYTE,
               NULL);

  glTexSubImage2D(target, 0, 0, 0, width, height, externalFormat, GL_UNSIGNED_BYTE, pixelData);

  if (height < height2)
    glTexSubImage2D(target, 0, 0, height, width, 1, externalFormat, GL_UNSIGNED_BYTE,
                    (const unsigned char*)pixelData + stride * (height - 1));

  if (width < width2)
    glTexSubImage2D(target, 0, width, 0, 1, height, externalFormat, GL_UNSIGNED_BYTE,
                    (const unsigned char*)pixelData + bytesPerPixel * (width - 1));

  free(pixelVector);

  *u = (GLfloat)width / width2;
  *v = (GLfloat)height / height2;
}

namespace
{
// Disc menu graphics on the composite's raw PQ route: premultiply plainly; the
// composite passes the premultiplied PQ values through and the display
// pipeline blends them, so opaque pixels reach it exactly as authored.
uint32_t PremultiplyPlain(uint32_t c)
{
  const uint32_t a = (c >> PIXEL_ASHIFT) & 0xff;
  const uint32_t r = (((c >> PIXEL_RSHIFT) & 0xff) * a + 127) / 255;
  const uint32_t g = (((c >> PIXEL_GSHIFT) & 0xff) * a + 127) / 255;
  const uint32_t b = (((c >> PIXEL_BSHIFT) & 0xff) * a + 127) / 255;
  return a << PIXEL_ASHIFT | r << PIXEL_RSHIFT | g << PIXEL_GSHIFT | b << PIXEL_BSHIFT;
}
} // namespace

std::shared_ptr<COverlay> COverlay::Create(const CDVDOverlayImage& o, CRect& rSource)
{
  return std::make_shared<COverlayTextureGLES>(o, rSource);
}

COverlayTextureGLES::COverlayTextureGLES(const CDVDOverlayImage& o, CRect& rSource)
{
  glGenTextures(1, &m_texture);
  glBindTexture(GL_TEXTURE_2D, m_texture);

  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);

  m_rawPqMenu = o.m_isPqMenuGraphics && CServiceBroker::GetWinSystem()->IsMenuCompositeActive();

  if (m_rawPqMenu)
  {
    m_pma = true;
    const size_t count = static_cast<size_t>(o.width) * o.height;
    std::vector<uint32_t> rgba(count);
    if (o.palette.empty())
    {
      for (int row = 0; row < o.height; row++)
      {
        const uint32_t* src = reinterpret_cast<const uint32_t*>(o.pixels.data() + row * o.linesize);
        for (int col = 0; col < o.width; col++)
          rgba[row * o.width + col] = PremultiplyPlain(src[col]);
      }
    }
    else
    {
      const std::vector<uint32_t>& pal = o.pqMenuPalette.empty() ? o.palette : o.pqMenuPalette;
      uint32_t palette[256] = {};
      for (size_t i = 0; i < pal.size() && i < 256; i++)
        palette[i] = PremultiplyPlain(pal[i]);
      for (int row = 0; row < o.height; row++)
        for (int col = 0; col < o.width; col++)
          rgba[row * o.width + col] = palette[o.pixels[row * o.linesize + col]];
    }
    LoadTexture(GL_TEXTURE_2D, o.width, o.height, o.width * 4, &m_u, &m_v, false, rgba.data());
  }
  else if (o.palette.empty())
  {
    m_pma = !!USE_PREMULTIPLIED_ALPHA;
    if (m_pma)
    {
      // Premultiply alpha in linear light so bilinear/mipmap filtering
      // preserves correct edge colors and semi-transparent edges don't appear
      // dark under HDR/PQ output.
      const size_t count = static_cast<size_t>(o.width) * o.height;
      std::vector<uint32_t> pma(count);
      const uint32_t* src = reinterpret_cast<const uint32_t*>(o.pixels.data());
      for (size_t i = 0; i < count; i++)
      {
        const uint32_t a = (src[i] >> PIXEL_ASHIFT) & 0xff;
        const float af = a / 255.0f;
        const int rp = OVERLAY::LinearToSrgb8(
            OVERLAY::SrgbToLinear((src[i] >> PIXEL_RSHIFT) & 0xff) * af);
        const int gp = OVERLAY::LinearToSrgb8(
            OVERLAY::SrgbToLinear((src[i] >> PIXEL_GSHIFT) & 0xff) * af);
        const int bp = OVERLAY::LinearToSrgb8(
            OVERLAY::SrgbToLinear((src[i] >> PIXEL_BSHIFT) & 0xff) * af);
        pma[i] = (a << PIXEL_ASHIFT) | (rp << PIXEL_RSHIFT) | (gp << PIXEL_GSHIFT) |
                 (bp << PIXEL_BSHIFT);
      }
      LoadTexture(GL_TEXTURE_2D, o.width, o.height, o.width * 4, &m_u, &m_v, false, pma.data());
    }
    else
    {
      const uint32_t* rgba = reinterpret_cast<const uint32_t*>(o.pixels.data());
      LoadTexture(GL_TEXTURE_2D, o.width, o.height, o.linesize, &m_u, &m_v, false, rgba);
    }
  }
  else
  {
    std::vector<uint32_t> rgba(o.width * o.height);
    m_pma = !!USE_PREMULTIPLIED_ALPHA;
    convert_rgba(o, m_pma, rgba);
    LoadTexture(GL_TEXTURE_2D, o.width, o.height, o.width * 4, &m_u, &m_v, false, rgba.data());
  }

  m_isHdrPqAuthored = o.m_isHdrPq && !m_rawPqMenu;
  m_isBitmapOverlay = true;

  glGenerateMipmap(GL_TEXTURE_2D);
  glBindTexture(GL_TEXTURE_2D, 0);

  if (o.source_width > 0 && o.source_height > 0)
  {
    m_pos = POSITION_RELATIVE;
    m_x = (0.5f * o.width + o.x) / o.source_width;
    m_y = (0.5f * o.height + o.y) / o.source_height;

    const float subRatio{static_cast<float>(o.source_width) / o.source_height};
    const float vidRatio{rSource.Width() / rSource.Height()};

    // We always consider aligning 4/3 subtitles to the video,
    // for example SD DVB subtitles (4/3) must be stretched on fullhd video

    if (std::fabs(subRatio - vidRatio) < 0.001f || IsSquareResolution(subRatio))
    {
      m_align = ALIGN_VIDEO;
      m_width = static_cast<float>(o.width) / o.source_width;
      m_height = static_cast<float>(o.height) / o.source_height;
    }
    else
    {
      // We should have a re-encoded/cropped (removed black bars) video source.
      // Then we cannot align to video otherwise the subtitles will be deformed
      // better align to screen by keeping the aspect-ratio.
      m_align = ALIGN_SCREEN_AR;
      m_width = static_cast<float>(o.width);
      m_height = static_cast<float>(o.height);
      m_source_width = static_cast<float>(o.source_width);
      m_source_height = static_cast<float>(o.source_height);
    }
  }
  else
  {
    m_align = ALIGN_VIDEO;
    m_pos = POSITION_ABSOLUTE;
    m_x = static_cast<float>(o.x);
    m_y = static_cast<float>(o.y);
    m_width = static_cast<float>(o.width);
    m_height = static_cast<float>(o.height);
  }
}

std::shared_ptr<COverlay> COverlay::Create(const CDVDOverlaySpu& o)
{
  return std::make_shared<COverlayTextureGLES>(o);
}

COverlayTextureGLES::COverlayTextureGLES(const CDVDOverlaySpu& o)
{
  int min_x, max_x, min_y, max_y;
  std::vector<uint32_t> rgba(o.width * o.height);

  convert_rgba(o, USE_PREMULTIPLIED_ALPHA, min_x, max_x, min_y, max_y, rgba);

  glGenTextures(1, &m_texture);
  glBindTexture(GL_TEXTURE_2D, m_texture);

  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);

  LoadTexture(GL_TEXTURE_2D, max_x - min_x, max_y - min_y, o.width * 4, &m_u, &m_v, false,
              rgba.data() + min_x + min_y * o.width);

  glGenerateMipmap(GL_TEXTURE_2D);
  glBindTexture(GL_TEXTURE_2D, 0);

  m_align = ALIGN_VIDEO;
  m_pos = POSITION_ABSOLUTE;
  m_x = static_cast<float>(min_x + o.x);
  m_y = static_cast<float>(min_y + o.y);
  m_width = static_cast<float>(max_x - min_x);
  m_height = static_cast<float>(max_y - min_y);
  m_pma = !!USE_PREMULTIPLIED_ALPHA;
  m_isBitmapOverlay = true;
}

std::shared_ptr<COverlay> COverlay::Create(ASS_Image* images, float width, float height)
{
  return std::make_shared<COverlayGlyphGLES>(images, width, height);
}

COverlayGlyphGLES::COverlayGlyphGLES(ASS_Image* images, float width, float height)
{
  m_width = 1.0;
  m_height = 1.0;
  m_align = ALIGN_SCREEN;
  m_pos = POSITION_RELATIVE;
  m_x = 0.0f;
  m_y = 0.0f;

  // Split the glyphs into atlas pages no larger than the GPU can hold. A
  // single texture would silently fail to upload for heavy signs whose total
  // bitmap area exceeds GL_MAX_TEXTURE_SIZE, rendering them as white blocks.
  GLint maxTextureSize = 2048;
  glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxTextureSize);
  if (maxTextureSize <= 0)
    maxTextureSize = 2048;

  std::vector<SQuads> pages;
  if (!convert_quads(images, pages, maxTextureSize))
    return;

  const float scale_x = 1.0f / width;
  const float scale_y = 1.0f / height;

  m_pages.reserve(pages.size());

  for (SQuads& quads : pages)
  {
    Page page;
    glGenTextures(1, &page.texture);
    glBindTexture(GL_TEXTURE_2D, page.texture);

    float u = 0.0f;
    float v = 0.0f;
    LoadTexture(GL_TEXTURE_2D, quads.size_x, quads.size_y, quads.size_x, &u, &v, true,
                quads.texture.data());

    const float scale_u = u / quads.size_x;
    const float scale_v = v / quads.size_y;

    page.vertex.resize(quads.quad.size() * 4);

    VERTEX* vt = page.vertex.data();
    SQuad* vs = quads.quad.data();

    for (size_t i = 0; i < quads.quad.size(); i++)
    {
      for (int s = 0; s < 4; s++)
      {
        vt[s].a = vs->a;
        vt[s].r = vs->r;
        vt[s].g = vs->g;
        vt[s].b = vs->b;

        vt[s].x = scale_x;
        vt[s].y = scale_y;
        vt[s].z = 0.0f;
        vt[s].u = scale_u;
        vt[s].v = scale_v;
      }

      vt[0].x *= vs->x;
      vt[0].u *= vs->u;
      vt[0].y *= vs->y;
      vt[0].v *= vs->v;

      vt[1].x *= vs->x;
      vt[1].u *= vs->u;
      vt[1].y *= vs->y + vs->h;
      vt[1].v *= vs->v + vs->h;

      vt[2].x *= vs->x + vs->w;
      vt[2].u *= vs->u + vs->w;
      vt[2].y *= vs->y;
      vt[2].v *= vs->v;

      vt[3].x *= vs->x + vs->w;
      vt[3].u *= vs->u + vs->w;
      vt[3].y *= vs->y + vs->h;
      vt[3].v *= vs->v + vs->h;

      vs += 1;
      vt += 4;
    }

    glBindTexture(GL_TEXTURE_2D, 0);
    m_pages.push_back(std::move(page));
  }
}

COverlayGlyphGLES::~COverlayGlyphGLES()
{
  for (Page& page : m_pages)
    glDeleteTextures(1, &page.texture);
}

void COverlayGlyphGLES::Render(SRenderState& state)
{
  if (m_pages.empty())
    return;

  glEnable(GL_BLEND);
  // Subtitle glyph shader outputs premultiplied alpha in linear light;
  // pair it with PMA blending to avoid dark-fringe edges.
  glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

  glMatrixModview.Push();
  glMatrixModview->Translatef(state.x, state.y, 0.0f);
  glMatrixModview->Scalef(state.width, state.height, 1.0f);
  glMatrixModview.Load();

  CRenderSystemGLES* renderSystem =
      dynamic_cast<CRenderSystemGLES*>(CServiceBroker::GetRenderSystem());
  renderSystem->EnableGUIShader(ShaderMethodGLES::SM_FONTS_SUBTITLE);
  GLint posLoc = renderSystem->GUIShaderGetPos();
  GLint colLoc = renderSystem->GUIShaderGetCol();
  GLint tex0Loc = renderSystem->GUIShaderGetCoord0();
  GLint depthLoc = renderSystem->GUIShaderGetDepth();
  GLint matrixUniformLoc = renderSystem->GUIShaderGetMatrix();

  CMatrixGL matrix = glMatrixProject.Get();
  matrix.MultMatrixf(glMatrixModview.Get());
  glUniformMatrix4fv(matrixUniformLoc, 1, GL_FALSE, matrix);

  glUniform1f(depthLoc, -1.0f);

  // Draw the atlas pages in order to preserve the blend order.
  for (const Page& page : m_pages)
  {
    if (page.texture == 0 || page.vertex.empty())
      continue;

    glBindTexture(GL_TEXTURE_2D, page.texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);

    // stack object until VBOs will be used
    std::vector<VERTEX> vecVertices(6 * page.vertex.size() / 4);
    VERTEX* vertices = vecVertices.data();

    for (size_t i = 0; i < page.vertex.size(); i += 4)
    {
      *vertices++ = page.vertex[i];
      *vertices++ = page.vertex[i + 1];
      *vertices++ = page.vertex[i + 2];

      *vertices++ = page.vertex[i + 1];
      *vertices++ = page.vertex[i + 3];
      *vertices++ = page.vertex[i + 2];
    }

    vertices = vecVertices.data();

    glVertexAttribPointer(posLoc, 3, GL_FLOAT, GL_FALSE, sizeof(VERTEX),
                          (char*)vertices + offsetof(VERTEX, x));
    glVertexAttribPointer(colLoc, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(VERTEX),
                          (char*)vertices + offsetof(VERTEX, r));
    glVertexAttribPointer(tex0Loc, 2, GL_FLOAT, GL_FALSE, sizeof(VERTEX),
                          (char*)vertices + offsetof(VERTEX, u));

    glEnableVertexAttribArray(posLoc);
    glEnableVertexAttribArray(colLoc);
    glEnableVertexAttribArray(tex0Loc);

    glDrawArrays(GL_TRIANGLES, 0, vecVertices.size());

    glDisableVertexAttribArray(posLoc);
    glDisableVertexAttribArray(colLoc);
    glDisableVertexAttribArray(tex0Loc);
  }

  renderSystem->DisableGUIShader();

  glMatrixModview.PopLoad();

  glDisable(GL_BLEND);

  glBindTexture(GL_TEXTURE_2D, 0);
}

COverlayTextureGLES::~COverlayTextureGLES()
{
  glDeleteTextures(1, &m_texture);
}

void COverlayTextureGLES::Render(SRenderState& state)
{
  glEnable(GL_BLEND);

  glBindTexture(GL_TEXTURE_2D, m_texture);
  if (m_pma)
    glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
  else
    glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  GLenum filter = GL_LINEAR;
  if (m_isBitmapOverlay)
  {
    int zoom = CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(
        CSettings::SETTING_SUBTITLES_BITMAPZOOM);
    if (zoom < 100)
      filter = GL_LINEAR_MIPMAP_LINEAR;
    else if (zoom == 100)
      filter = GL_NEAREST;
  }
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filter == GL_NEAREST ? GL_NEAREST : GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filter);

  CRect rd;
  if (m_pos == POSITION_RELATIVE)
  {
    float top = state.y - state.height * 0.5f;
    float bottom = state.y + state.height * 0.5f;
    float left = state.x - state.width * 0.5f;
    float right = state.x + state.width * 0.5f;

    rd.SetRect(left, top, right, bottom);
  }
  else
  {
    float top = state.y;
    float bottom = state.y + state.height;
    float left = state.x;
    float right = state.x + state.width;

    rd.SetRect(left, top, right, bottom);
  }

  CRenderSystemGLES* renderSystem =
      dynamic_cast<CRenderSystemGLES*>(CServiceBroker::GetRenderSystem());
  renderSystem->EnableGUIShader(m_isHdrPqAuthored
                                    ? ShaderMethodGLES::SM_TEXTURE_NOBLEND_PQ_TO_SDR
                                    : ShaderMethodGLES::SM_TEXTURE_NOBLEND);

  if (m_isHdrPqAuthored)
  {
    auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
    float brightness = static_cast<float>(
        settings->GetInt(CSettings::SETTING_SUBTITLES_PGSHDRTOSDR_BRIGHTNESS));
    float refNits = 20300.0f / std::max(brightness, 10.0f);
    float saturation = static_cast<float>(
        settings->GetInt(CSettings::SETTING_SUBTITLES_PGSHDRTOSDR_SATURATION)) / 100.0f;
    float tonemap = settings->GetBool(CSettings::SETTING_SUBTITLES_PGSHDRTOSDR_TONEMAP) ? 1.0f : 0.0f;
    float mode = static_cast<float>(
        settings->GetInt(CSettings::SETTING_SUBTITLES_PGSHDRTOSDR_MODE));

    GLint prog = 0;
    glGetIntegerv(GL_CURRENT_PROGRAM, &prog);
    glUniform1f(glGetUniformLocation(prog, "m_pqRefNits"), refNits);
    glUniform1f(glGetUniformLocation(prog, "m_pqSaturation"), saturation);
    glUniform1f(glGetUniformLocation(prog, "m_pqTonemap"), tonemap);
    glUniform1f(glGetUniformLocation(prog, "m_pqMode"), mode);
  }

  GLint posLoc = renderSystem->GUIShaderGetPos();
  GLint tex0Loc = renderSystem->GUIShaderGetCoord0();
  GLint depthLoc = renderSystem->GUIShaderGetDepth();

  // Tell the shader this texture is premultiplied so its limited-range
  // conversion scales the +16/255 offset by alpha; without this, transparent
  // PMA texels (alpha=0, rgb=0) would emit (0.063, 0.063, 0.063, 0) which the
  // PMA blend would write straight into dst, leaking a constant brightening
  // across the bitmap quad. CGLESShader::OnEnabled resets this to 0.0 every
  // time a shader is bound, so non-PMA consumers stay on straight-alpha math.
  if (m_pma)
    glUniform1f(renderSystem->GUIShaderGetPma(), 1.0f);

  GLfloat ver[4][2];
  GLfloat tex[4][2];
  GLubyte idx[4] = {0, 1, 3, 2}; //determines order of triangle strip

  glVertexAttribPointer(posLoc, 2, GL_FLOAT, 0, 0, ver);
  glVertexAttribPointer(tex0Loc, 2, GL_FLOAT, 0, 0, tex);

  glEnableVertexAttribArray(posLoc);
  glEnableVertexAttribArray(tex0Loc);

  glUniform1f(depthLoc, 1.0f);
  // Setup vertex position values
  ver[0][0] = ver[3][0] = rd.x1;
  ver[0][1] = ver[1][1] = rd.y1;
  ver[1][0] = ver[2][0] = rd.x2;
  ver[2][1] = ver[3][1] = rd.y2;

  // Setup texture coordinates
  tex[0][0] = tex[0][1] = tex[1][1] = tex[3][0] = 0.0f;
  tex[1][0] = tex[2][0] = m_u;
  tex[2][1] = tex[3][1] = m_v;

  glDrawElements(GL_TRIANGLE_STRIP, 4, GL_UNSIGNED_BYTE, idx);

  glDisableVertexAttribArray(posLoc);
  glDisableVertexAttribArray(tex0Loc);

  renderSystem->DisableGUIShader();

  glDisable(GL_BLEND);

  glBindTexture(GL_TEXTURE_2D, 0);
}
