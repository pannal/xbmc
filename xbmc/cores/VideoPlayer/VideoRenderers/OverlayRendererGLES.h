/*
 *      Initial code sponsored by: Voddler Inc (voddler.com)
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "OverlayRenderer.h"
#include "rendering/RenderResource.h"
#include "rendering/gles/TextureResources.h"

#include "system_gl.h"

class CDVDOverlay;
class CDVDOverlayImage;
class CDVDOverlaySpu;
class CDVDOverlaySSA;

namespace OVERLAY
{

class COverlayTextureGLES : public COverlay
{
public:
  /*! \brief Create the overlay for rendering
     *  \param o The overlay image
     *  \param rSource The video source rect size
     */
  struct PreparedImage
  {
    RenderTargetToken target;
    bool rawPqMenu{false};
    bool plainPmaMenu{false};
    bool premultiplied{false};
    int stride{0};
    std::vector<uint32_t> pixels;
  };
  // CPU-only work over immutable producer content and explicit route input.
  static PreparedImage PrepareImage(const CDVDOverlayImage& o,
                                    bool rawPqMenu,
                                    RenderTargetToken target,
                                    bool plainPmaMenu = false);
  COverlayTextureGLES(const CDVDOverlayImage& o, CRect& rSource, PreparedImage image);
  explicit COverlayTextureGLES(const CDVDOverlaySpu& o);
  ~COverlayTextureGLES() override;

  void Render(SRenderState& state) override;
  bool IsValid() const override;

  std::shared_ptr<CGLESTextureResources> m_textureResources;
  GLuint m_texture = 0;
  float m_u;
  float m_v;
  bool m_pma; /*< is alpha in texture premultiplied in the values */
  bool m_isHdrPqAuthored{false};
  bool m_isSdrSubtitle{false};
};

class COverlayGlyphGLES : public COverlay
{
public:
  COverlayGlyphGLES(ASS_Image* images, float width, float height);

  ~COverlayGlyphGLES() override;

  void Render(SRenderState& state) override;
  bool IsValid() const override;

  struct VERTEX
  {
    GLfloat u, v;
    GLubyte r, g, b, a;
    GLfloat x, y, z;
  };

private:
  // A single subtitle frame can produce more glyph bitmap data than one GPU
  // texture can hold (heavy typesetting signs). The glyphs are split across
  // one or more atlas pages, each within GL_MAX_TEXTURE_SIZE, drawn in order
  // to preserve libass' blending. Most subtitles use a single page.
  struct Page
  {
    GLuint texture{0};
    std::vector<VERTEX> vertex;
  };

  std::shared_ptr<CGLESTextureResources> m_textureResources;
  std::vector<Page> m_pages;
  bool m_uploadFailed{false};
};

} // namespace OVERLAY
