/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "GLESShader.h"
#include "TextureResources.h"
#include "rendering/RenderSystem.h"
#include "utils/ColorUtils.h"
#include "utils/Map.h"

#include <array>
#include <map>

#include <fmt/format.h>

#include "system_gl.h"

enum class ShaderMethodGLES
{
  SM_DEFAULT,
  SM_TEXTURE,
  SM_MULTI,
  SM_FONTS,
  SM_FONTS_SHADER_CLIP,
  SM_FONTS_SUBTITLE,
  SM_TEXTURE_NOBLEND,
  SM_TEXTURE_NOBLEND_PQ_TO_SDR,
  SM_MULTI_BLENDCOLOR,
  SM_TEXTURE_RGBA,
  SM_TEXTURE_RGBA_OES,
  SM_TEXTURE_RGBA_BLENDCOLOR,
  SM_TEXTURE_RGBA_BOB,
  SM_TEXTURE_RGBA_BOB_OES,
  SM_TEXTURE_NOALPHA,
  SM_MAX
};

template<>
struct fmt::formatter<ShaderMethodGLES> : fmt::formatter<std::string_view>
{
  template<typename FormatContext>
  constexpr auto format(const ShaderMethodGLES& shaderMethod, FormatContext& ctx)
  {
    const auto it = ShaderMethodGLESMap.find(shaderMethod);
    if (it == ShaderMethodGLESMap.cend())
      throw std::range_error("no string mapping found for shader method");

    return fmt::formatter<string_view>::format(it->second, ctx);
  }

private:
  static constexpr auto ShaderMethodGLESMap = make_map<ShaderMethodGLES, std::string_view>({
      {ShaderMethodGLES::SM_DEFAULT, "default"},
      {ShaderMethodGLES::SM_TEXTURE, "texture"},
      {ShaderMethodGLES::SM_MULTI, "multi"},
      {ShaderMethodGLES::SM_FONTS, "fonts"},
      {ShaderMethodGLES::SM_FONTS_SHADER_CLIP, "fonts with vertex shader based clipping"},
      {ShaderMethodGLES::SM_FONTS_SUBTITLE, "fonts for subtitles (linear-light PMA)"},
      {ShaderMethodGLES::SM_TEXTURE_NOBLEND, "texture no blending"},
      {ShaderMethodGLES::SM_TEXTURE_NOBLEND_PQ_TO_SDR, "texture no blending (PQ to SDR)"},
      {ShaderMethodGLES::SM_MULTI_BLENDCOLOR, "multi blend colour"},
      {ShaderMethodGLES::SM_TEXTURE_RGBA, "texure rgba"},
      {ShaderMethodGLES::SM_TEXTURE_RGBA_OES, "texture rgba OES"},
      {ShaderMethodGLES::SM_TEXTURE_RGBA_BLENDCOLOR, "texture rgba blend colour"},
      {ShaderMethodGLES::SM_TEXTURE_RGBA_BOB, "texture rgba bob"},
      {ShaderMethodGLES::SM_TEXTURE_RGBA_BOB_OES, "texture rgba bob OES"},
      {ShaderMethodGLES::SM_TEXTURE_NOALPHA, "texture no alpha"},
  });

  static_assert(static_cast<size_t>(ShaderMethodGLES::SM_MAX) == ShaderMethodGLESMap.size(),
                "ShaderMethodGLESMap doesn't match the size of ShaderMethodGLES, did you forget to "
                "add/remove a mapping?");
};

class CRenderSystemGLES : public CRenderSystemBase
{
public:
  CRenderSystemGLES();
  ~CRenderSystemGLES() override = default;

  bool InitRenderSystem() override;
  bool DestroyRenderSystem() override;
  bool ResetRenderSystem(int width, int height) override;

  bool CanRender() const override;
  std::shared_ptr<CGLESTextureResources> GetTextureResources() const { return m_textureResources; }
  bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>& resources) const;
  void DrainTextureResources();
  void CloseTextureResources();

  bool BeginRender() override;
  bool EndRender() override;
  void PresentRender(bool rendered, bool videoLayer) override;
  void InvalidateColorBuffer() override;
  bool ClearBuffers(UTILS::COLOR::Color color) override;
  bool IsExtSupported(const char* extension) const override;

  void SetVSync(bool vsync);
  void ResetVSync() { m_bVsyncInit = false; }

  void SetViewPort(const CRect& viewPort) override;
  void GetViewPort(CRect& viewPort) override;

  bool ScissorsCanEffectClipping() override;
  CRect ClipRectToScissorRect(const CRect &rect) override;
  void SetScissors(const CRect& rect) override;
  void ResetScissors() override;

  void SetDepthCulling(DEPTH_CULLING culling) override;

  void CaptureStateBlock() override;
  void ApplyStateBlock() override;

  void SetCameraPosition(const CPoint &camera, int screenWidth, int screenHeight, float stereoFactor = 0.0f) override;

  bool SupportsStereo(RENDER_STEREO_MODE mode) const override;

  void Project(float &x, float &y, float &z) override;

  std::string GetShaderPath(const std::string &filename) override { return "GLES/2.0/"; }

  void InitialiseShaders();
  void ReleaseShaders(bool abandon = false);
  void EnableGUIShader(ShaderMethodGLES method);
  void DisableGUIShader();

  GLint GUIShaderGetPos();
  GLint GUIShaderGetCol();
  GLint GUIShaderGetCoord0();
  GLint GUIShaderGetCoord1();
  GLint GUIShaderGetUniCol();
  GLint GUIShaderGetCoord0Matrix();
  GLint GUIShaderGetField();
  GLint GUIShaderGetStep();
  GLint GUIShaderGetContrast();
  GLint GUIShaderGetBrightness();
  GLint GUIShaderGetModel();
  GLint GUIShaderGetMatrix();
  GLint GUIShaderGetClip();
  GLint GUIShaderGetCoordStep();
  GLint GUIShaderGetDepth();
  GLint GUIShaderGetPma();
  GLint GUIShaderGetSdrBrightness();
  GLint GUIShaderGetSdrSaturation();
  GLint GUIShaderGetSubtitlePeak();
  GLint GUIShaderGetGuiTuning();

protected:
  virtual bool IsPrimaryContextCurrent() const { return true; }
  std::shared_ptr<CGLESTextureResources> m_textureResources;

  virtual void SetVSyncImpl(bool enable) = 0;
  virtual void PresentRenderImpl(bool rendered) = 0;
  void CalculateMaxTexturesize();

  bool m_bVsyncInit{false};
  int m_width;
  int m_height;

  std::string m_RenderExtensions;

  using ShaderSet = std::map<ShaderMethodGLES, std::unique_ptr<CGLESShader>>;
  static void ReleaseShaderSet(ShaderSet& shaders, bool abandon);

  ShaderSet m_pShader;
  std::pair<float, float> m_guiColour{1.0f, 1.0f};
  bool m_guiColourEnabled{false};
  // Inactive variants share the current EGL context; surface changes retain them.
  std::array<ShaderSet, 8> m_shaderVariants;
  ShaderMethodGLES m_method = ShaderMethodGLES::SM_DEFAULT;

  GLint      m_viewPort[4];
};
