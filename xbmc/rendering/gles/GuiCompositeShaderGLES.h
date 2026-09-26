/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "guilib/Shader.h"

#include <string>
#include <vector>

class CGuiCompositeShaderGLES : public Shaders::CGLSLShaderProgram
{
public:
  explicit CGuiCompositeShaderGLES(const std::string& prefix);
  ~CGuiCompositeShaderGLES() override;

  void SetProjection(const GLfloat* proj) { m_proj = proj; }

  // How the hardware decodes the SDR GUI on the active route: a BT.1886 curve
  // (a pure power when blackLift is 0) with white at `white` (PQ-normalized,
  // nits / 10000) and black at blackLift * white. inputScale is the factor
  // Kodi's per-primitive path applies to GUI code values before that decode.
  // Takes effect on the next CreateLUTs.
  struct GuiTransfer
  {
    float gamma = 2.2f;
    float blackLift = 0.0f;
    float inputScale = 1.0f;
    float white = 203.0f / 10000.0f;
    bool operator==(const GuiTransfer& o) const
    {
      return gamma == o.gamma && blackLift == o.blackLift && inputScale == o.inputScale &&
             white == o.white;
    }
  };
  void SetGuiTransfer(const GuiTransfer& transfer) { m_transfer = transfer; }

  bool CreateLUTs(int colorTransfer);

  // Texture of already-PQ disc menu graphics (premultiplied), output as-is
  // under the transformed GUI; 0 when there are none. Takes effect on the
  // next Enable.
  void SetHdrTexture(GLuint texId) { m_hdrTexId = texId; }

  GLint GetPosLoc() { return m_hPos; }
  GLint GetTexLoc() { return m_hTex; }

protected:
  void OnCompiledAndLinked() override;
  bool OnEnabled() override;

private:
  // One entry per RGBA8 input value; increase to match GUI bit depth.
  static constexpr int LUT_SIZE = 256;

  GLuint CreateLUTTexture(const std::vector<float>& data);
  static std::vector<float> GenerateDegammaLUT(const GuiTransfer& transfer);
  static std::vector<float> GeneratePQLUT(float white);

  const GLfloat* m_proj{nullptr};
  GuiTransfer m_transfer;

  GLuint m_lutDegammaTexId{0};
  GLuint m_lutTFTexId{0};
  GLuint m_hdrTexId{0};

  GLint m_hPos{-1};
  GLint m_hTex{-1};
  GLint m_hSamp{-1};
  GLint m_hLutDegamma{-1};
  GLint m_hLutTF{-1};
  GLint m_hProj{-1};
  GLint m_hHdr{-1};
  GLint m_hHasHdr{-1};
};
