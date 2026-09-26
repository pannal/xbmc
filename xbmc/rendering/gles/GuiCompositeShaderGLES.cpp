/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "GuiCompositeShaderGLES.h"

#include "utils/log.h"

extern "C"
{
#include <libavutil/pixfmt.h>
}

#include <algorithm>
#include <cmath>

namespace
{
// ST2084 (PQ) constants
constexpr float ST2084_m1 = 0.1593017578125f; // 2610/16384
constexpr float ST2084_m2 = 78.84375f; // 2523/4096 * 128
constexpr float ST2084_c1 = 0.8359375f; // 3424/4096
constexpr float ST2084_c2 = 18.8515625f; // 2413/4096 * 32
constexpr float ST2084_c3 = 18.6875f; // 2392/4096 * 32

float ForwardPQ(float L)
{
  float Lm1 = std::pow(L, ST2084_m1);
  return std::pow((ST2084_c1 + ST2084_c2 * Lm1) / (1.0f + ST2084_c3 * Lm1), ST2084_m2);
}

// The Amlogic OSD SDR->HDR stage decodes the GUI with a pure 2.2 power
// (eo_y_lut_sdr), and dolby_core2_set's VP curve is the same power; matching
// it keeps Kodi's own controls over a disc menu as they look without one.
constexpr float GUI_GAMMA = 2.2f;

} // namespace

CGuiCompositeShaderGLES::CGuiCompositeShaderGLES(const std::string& prefix)
{
  VertexShader()->LoadSource("gles_gui_composite.vert", prefix);
  PixelShader()->LoadSource("gles_gui_composite.frag", prefix);
}

CGuiCompositeShaderGLES::~CGuiCompositeShaderGLES()
{
  if (m_lutDegammaTexId)
    glDeleteTextures(1, &m_lutDegammaTexId);
  if (m_lutTFTexId)
    glDeleteTextures(1, &m_lutTFTexId);
}

void CGuiCompositeShaderGLES::OnCompiledAndLinked()
{
  m_hPos = glGetAttribLocation(ProgramHandle(), "a_pos");
  m_hTex = glGetAttribLocation(ProgramHandle(), "a_tex");
  m_hSamp = glGetUniformLocation(ProgramHandle(), "u_samp");
  m_hLutDegamma = glGetUniformLocation(ProgramHandle(), "u_lutDegamma");
  m_hLutTF = glGetUniformLocation(ProgramHandle(), "u_lutTF");
  m_hProj = glGetUniformLocation(ProgramHandle(), "u_proj");
  m_hHdr = glGetUniformLocation(ProgramHandle(), "u_hdr");
  m_hHasHdr = glGetUniformLocation(ProgramHandle(), "u_hasHdr");
  glUseProgram(ProgramHandle());
  glUniform1i(m_hSamp, 0);
  glUniform1i(m_hLutDegamma, 1);
  glUniform1i(m_hLutTF, 2);
  glUniform1i(m_hHdr, 3);
  glUseProgram(0);
}

bool CGuiCompositeShaderGLES::OnEnabled()
{
  if (m_proj)
    glUniformMatrix4fv(m_hProj, 1, GL_FALSE, m_proj);

  glActiveTexture(GL_TEXTURE1);
  glBindTexture(GL_TEXTURE_2D, m_lutDegammaTexId);
  glActiveTexture(GL_TEXTURE2);
  glBindTexture(GL_TEXTURE_2D, m_lutTFTexId);
  glUniform1f(m_hHasHdr, m_hdrTexId ? 1.0f : 0.0f);
  if (m_hdrTexId)
  {
    glActiveTexture(GL_TEXTURE3);
    glBindTexture(GL_TEXTURE_2D, m_hdrTexId);
  }
  glActiveTexture(GL_TEXTURE0);

  return true;
}

GLuint CGuiCompositeShaderGLES::CreateLUTTexture(const std::vector<float>& data)
{
  while (glGetError() != GL_NO_ERROR)
  {
  }

  GLuint texId;
  glGenTextures(1, &texId);
  glBindTexture(GL_TEXTURE_2D, texId);

  // Prefer GL_R16F (GLES 3.0 core) over GL_LUMINANCE + GL_FLOAT (GLES 2.0).
  // The GLES 3.0 spec tightens format validation for unsized internal formats,
  // and some drivers (e.g. V3D on RPi5) silently reject GL_LUMINANCE + GL_FLOAT
  // despite advertising OES_texture_float. GL_R16F avoids this by using a sized
  // format with well-defined behavior. Half-float precision is sufficient for
  // a 1024-entry LUT.
  bool uploaded = false;
  glTexImage2D(GL_TEXTURE_2D, 0, GL_R16F, data.size(), 1, 0, GL_RED, GL_FLOAT, data.data());
  if (glGetError() == GL_NO_ERROR)
  {
    uploaded = true;
  }
  else
  {
    while (glGetError() != GL_NO_ERROR)
    {
    }
    glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, data.size(), 1, 0, GL_LUMINANCE, GL_FLOAT,
                 data.data());
    if (glGetError() == GL_NO_ERROR)
      uploaded = true;
    else
      CLog::Log(LOGERROR,
                "CGuiCompositeShaderGLES::CreateLUTTexture - failed to create {} entry "
                "LUT texture (GL_R16F and GL_LUMINANCE+GL_FLOAT both failed)",
                data.size());
  }

  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glBindTexture(GL_TEXTURE_2D, 0);

  if (!uploaded)
  {
    glDeleteTextures(1, &texId);
    return 0;
  }
  return texId;
}

std::vector<float> CGuiCompositeShaderGLES::GenerateDegammaLUT()
{
  std::vector<float> lut(LUT_SIZE);
  for (int i = 0; i < LUT_SIZE; i++)
  {
    float x = static_cast<float>(i) / (LUT_SIZE - 1);
    lut[i] = std::pow(x, GUI_GAMMA);
  }
  return lut;
}

std::vector<float> CGuiCompositeShaderGLES::GeneratePQLUT(float sdrPeak)
{
  // PQ is display-referred (absolute luminance). sdrPeak is in PQ-normalized
  // units (nits / 10000), e.g. 300 nits = 0.03. The LUT is indexed by the
  // gamma-encoded value (the shader re-encodes after the gamut matrix): PQ is
  // steep near black, where a linear-light index has too few entries.
  std::vector<float> lut(LUT_SIZE);
  for (int i = 0; i < LUT_SIZE; i++)
  {
    float x = static_cast<float>(i) / (LUT_SIZE - 1);
    lut[i] = ForwardPQ(std::pow(x, GUI_GAMMA) * sdrPeak);
  }
  return lut;
}

bool CGuiCompositeShaderGLES::CreateLUTs(int colorTransfer)
{
  // Build into locals and only commit on success. Deleting the live textures up
  // front would leave the shader sampling destroyed/zero texture names on any
  // failure - the GUI composites to solid black, and a caller that retries (a
  // live SetSdrPeak change) would thrash glDeleteTextures/glTexImage2D every
  // frame. Failure must be a no-op so the previous LUTs keep working.
  GLuint degamma = CreateLUTTexture(GenerateDegammaLUT());
  if (!degamma)
  {
    CLog::Log(LOGERROR, "CGuiCompositeShaderGLES::CreateLUTs - failed to create degamma LUT");
    return false;
  }

  if (colorTransfer != AVCOL_TRC_SMPTE2084)
  {
    CLog::Log(LOGERROR, "CGuiCompositeShaderGLES::CreateLUTs - unsupported transfer function {}",
              colorTransfer);
    glDeleteTextures(1, &degamma);
    return false;
  }

  GLuint tf = CreateLUTTexture(GeneratePQLUT(m_sdrPeak));
  if (!tf)
  {
    CLog::Log(LOGERROR, "CGuiCompositeShaderGLES::CreateLUTs - failed to create PQ LUT");
    glDeleteTextures(1, &degamma);
    return false;
  }
  CLog::Log(LOGDEBUG,
            "CGuiCompositeShaderGLES::CreateLUTs - created PQ LUT ({} entries, {:.0f} nits)",
            LUT_SIZE, m_sdrPeak * 10000.0f);

  if (m_lutDegammaTexId)
    glDeleteTextures(1, &m_lutDegammaTexId);
  if (m_lutTFTexId)
    glDeleteTextures(1, &m_lutTFTexId);

  m_lutDegammaTexId = degamma;
  m_lutTFTexId = tf;
  return true;
}
