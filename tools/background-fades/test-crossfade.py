#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compare the production single-pass crossfade with the production two-pass
texture drawing using software GLES.

Both paths run the production CGUITextureGLES methods with the production
shaders, assembled as CGLESShader assembles them. The reference draws the
outgoing and then the incoming image through Draw/BeginDraw/End; the single
pass uses DrawCrossfade. Pixels are read back here only.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SHADERS = ROOT / 'system/shaders/GLES/2.0'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"', lambda m: ' ' * len(m.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def shader(name, prefix, fragment):
    # CShader::LoadSource inserts the prefix after the #version line and
    # CGLESShader inserts gui_colour.frag before the first "void main".
    source = (SHADERS / name).read_text()
    version = source.find('#version')
    pos = source.find('\n', version) + 1 if version >= 0 else 0
    source = source[:pos] + prefix + source[pos:]
    if fragment:
        location = source.index('void main')
        source = source[:location] + (SHADERS / 'gui_colour.frag').read_text() + source[location:]
    return source


def cstring(text):
    return 'R"GLSL(' + text + ')GLSL"'


PRELUDE = r'''
#include <EGL/egl.h>
#include <GLES2/gl2.h>
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
namespace UTILS { namespace COLOR { using Color = uint32_t; } }
namespace KODI { namespace UTILS { namespace GL {
enum class ColorChannel { A, R, G, B };
uint8_t GetChannelFromARGB(const ColorChannel colorChannel, const uint32_t argb);
} } }
@CHANNEL@
struct CRect { float x1{0}, y1{0}, x2{0}, y2{0}; };
@PACKED@
typedef std::vector<PackedVertex> PackedVertices;
enum class ShaderMethodGLES { SM_TEXTURE, SM_TEXTURE_NOBLEND_GUI, SM_MULTI, SM_MULTI_BLENDCOLOR, SM_CROSSFADE };
static unsigned draws = 0;
#define glDrawElements(mode, count, type, indices) (++draws, glDrawElements(mode, count, type, indices))
struct Program { GLuint id{0}; };
struct CRenderSystemGLES
{
  std::map<ShaderMethodGLES, Program> programs;
  GLuint current{0};
  float width{1}, height{1};
  void EnableGUIShader(ShaderMethodGLES method)
  {
    current = programs.at(method).id;
    glUseProgram(current);
    // As CGLESShader::OnEnabled: pixel coordinates to clip space.
    const GLfloat projection[16]{2 / width, 0, 0, 0, 0, -2 / height, 0, 0, 0, 0, 1, 0, -1, 1, 0, 1};
    const GLfloat identity[16]{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
    glUniformMatrix4fv(glGetUniformLocation(current, "m_proj"), 1, GL_FALSE, projection);
    glUniformMatrix4fv(glGetUniformLocation(current, "m_model"), 1, GL_FALSE, identity);
    glUniform1f(glGetUniformLocation(current, "m_guiTuning"), 1.0f);
    glUniform1f(glGetUniformLocation(current, "m_guiPeak"), 0.9f);
    glUniform1f(glGetUniformLocation(current, "m_guiSaturation"), 0.7f);
    glUniform1f(glGetUniformLocation(current, "m_sdrPeak"), 0.75f);
  }
  void DisableGUIShader() { glUseProgram(0); current = 0; }
  bool HasGUIShader(ShaderMethodGLES method) const { return programs.count(method); }
  GLint GUIShaderGetPos() { return glGetAttribLocation(current, "m_attrpos"); }
  GLint GUIShaderGetCol() { return glGetAttribLocation(current, "m_attrcol"); }
  GLint GUIShaderGetCoord0() { return glGetAttribLocation(current, "m_attrcord0"); }
  GLint GUIShaderGetCoord1() { return glGetAttribLocation(current, "m_attrcord1"); }
  GLint GUIShaderGetUniCol() { return glGetUniformLocation(current, "m_unicol"); }
  GLint GUIShaderGetDepth() { return glGetUniformLocation(current, "m_depth"); }
};
static bool limitedColor = false;
struct WinSystem { bool UseLimitedColor() { return limitedColor; } };
struct CServiceBroker { static WinSystem* GetWinSystem() { static WinSystem w; return &w; } };
struct CTexture
{
  GLuint id{0};
  bool alpha{true};
  void LoadToGPU() {}
  void BindToUnit(unsigned int unit) { glActiveTexture(GL_TEXTURE0 + unit); glBindTexture(GL_TEXTURE_2D, id); }
  bool HasAlpha() const { return alpha; }
};
struct CTextureArray
{
  std::vector<std::shared_ptr<CTexture>> m_textures;
  size_t size() const { return m_textures.size(); }
};
struct Info { int orientation{0}; };
class CGUITextureGLES
{
public:
  CTextureArray m_texture, m_diffuse;
  unsigned int m_currentFrame{0};
  Info m_info;
  UTILS::COLOR::Color m_pendingColor{0};
  std::array<GLubyte, 4> m_col;
  PackedVertices m_packedVertices;
  std::vector<GLushort> m_idx;
  CRenderSystemGLES* m_renderSystem{nullptr};
  float m_depth{0.5f};
  void BeginDraw(UTILS::COLOR::Color color);
  void End();
  void Draw(float* x, float* y, float* z, const CRect& texture, const CRect& diffuse, int orientation);
  void DrawCrossfade(const float* x, const float* y, const float* z, CTexture* texture,
                     const CRect& coords, int orientation, UTILS::COLOR::Color color,
                     CTexture* outgoing, const CRect& outgoingCoords, int outgoingOrientation,
                     UTILS::COLOR::Color outgoingColor, unsigned char groupAlpha);
};
'''

TESTS = r'''
static GLuint Compile(GLenum type, const char* source)
{
  const GLuint shader = glCreateShader(type);
  glShaderSource(shader, 1, &source, nullptr);
  glCompileShader(shader);
  GLint ok = 0;
  glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
  if (!ok)
  {
    char log[2048];
    glGetShaderInfoLog(shader, sizeof(log), nullptr, log);
    std::fprintf(stderr, "shader compile failed: %s\n", log);
    std::exit(3);
  }
  return shader;
}
static GLuint Link(const char* vertex, const char* fragment)
{
  const GLuint program = glCreateProgram();
  glAttachShader(program, Compile(GL_VERTEX_SHADER, vertex));
  glAttachShader(program, Compile(GL_FRAGMENT_SHADER, fragment));
  glLinkProgram(program);
  GLint ok = 0;
  glGetProgramiv(program, GL_LINK_STATUS, &ok);
  if (!ok)
    std::exit(4);
  // As CGLESShader::OnCompiledAndLinked.
  glUseProgram(program);
  glUniform1i(glGetUniformLocation(program, "m_samp0"), 0);
  glUniform1i(glGetUniformLocation(program, "m_samp1"), 1);
  glUniform4f(glGetUniformLocation(program, "m_unicol"), 1, 1, 1, 1);
  const GLfloat identity[16]{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
  glUniformMatrix4fv(glGetUniformLocation(program, "m_coord0Matrix"), 1, GL_FALSE, identity);
  glUseProgram(0);
  return program;
}
static std::shared_ptr<CTexture> Image(int width, int height, int seed, bool opaque = false)
{
  std::vector<uint8_t> pixels(width * height * 4);
  for (int y = 0; y < height; ++y)
    for (int x = 0; x < width; ++x)
    {
      uint8_t* p = &pixels[(y * width + x) * 4];
      p[0] = static_cast<uint8_t>((x * 37 + seed * 11) % 256);
      p[1] = static_cast<uint8_t>((y * 53 + seed * 29) % 256);
      p[2] = static_cast<uint8_t>(((x + y) * 19 + seed * 7) % 256);
      p[3] = opaque ? 255 : static_cast<uint8_t>(255 - ((x * 23 + y * 31 + seed) % 200));
    }
  auto texture = std::make_shared<CTexture>();
  glGenTextures(1, &texture->id);
  glBindTexture(GL_TEXTURE_2D, texture->id);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
  texture->alpha = !opaque;
  return texture;
}
static constexpr int SIZE = 48;
static bool floatTarget = false;
static float Half(uint16_t h)
{
  const int exponent = (h >> 10) & 31, mantissa = h & 1023;
  const float value = exponent ? std::ldexp(1.0f + mantissa / 1024.0f, exponent - 15)
                               : std::ldexp(mantissa / 1024.0f, -14);
  return (h & 0x8000) ? -value : value;
}
static std::vector<float> Read()
{
  std::vector<float> pixels(SIZE * SIZE * 4);
  if (!floatTarget)
  {
    std::vector<uint8_t> bytes(pixels.size());
    glReadPixels(0, 0, SIZE, SIZE, GL_RGBA, GL_UNSIGNED_BYTE, bytes.data());
    std::transform(bytes.begin(), bytes.end(), pixels.begin(), [](uint8_t b) { return b / 255.0f; });
    return pixels;
  }
  GLint format = 0, type = 0;
  glGetIntegerv(GL_IMPLEMENTATION_COLOR_READ_FORMAT, &format);
  glGetIntegerv(GL_IMPLEMENTATION_COLOR_READ_TYPE, &type);
  if (format != GL_RGBA)
  {
    std::fprintf(stderr, "unsupported float read format 0x%x type 0x%x\n", format, type);
    std::exit(6);
  }
  if (type == GL_FLOAT)
    glReadPixels(0, 0, SIZE, SIZE, GL_RGBA, GL_FLOAT, pixels.data());
  else if (type == 0x8D61 || type == 0x140B) // GL_HALF_FLOAT_OES or GL_HALF_FLOAT
  {
    std::vector<uint16_t> halves(pixels.size());
    glReadPixels(0, 0, SIZE, SIZE, GL_RGBA, type, halves.data());
    std::transform(halves.begin(), halves.end(), pixels.begin(), Half);
  }
  else
  {
    std::fprintf(stderr, "unsupported float read type 0x%x\n", type);
    std::exit(6);
  }
  return pixels;
}
struct Case
{
  uint32_t color, outgoingColor;
  float clear[4];
  CRect coords, outgoingCoords;
  int orientation, outgoingOrientation;
};
static float MaxDifference(CRenderSystemGLES& system, CGUITextureGLES& incoming,
                         CGUITextureGLES& outgoing, const Case& c, unsigned* separateDraws,
                         unsigned* singleDraws)
{
  float x[4]{4, 44, 44, 4}, y[4]{3, 3, 45, 45}, z[4]{0, 0, 0, 0};
  const CRect none;
  glClearColor(c.clear[0], c.clear[1], c.clear[2], c.clear[3]);
  // Reference: the ordinary path draws the outgoing and then the incoming image.
  glClear(GL_COLOR_BUFFER_BIT);
  unsigned before = draws;
  for (auto* image : {&outgoing, &incoming})
  {
    const bool in = image == &incoming;
    image->m_pendingColor = in ? c.color : c.outgoingColor;
    image->m_packedVertices.clear();
    image->Draw(x, y, z, in ? c.coords : c.outgoingCoords, none, in ? c.orientation : c.outgoingOrientation);
    image->End();
  }
  *separateDraws = draws - before;
  const auto reference = Read();
  glClear(GL_COLOR_BUFFER_BIT);
  before = draws;
  incoming.DrawCrossfade(x, y, z, incoming.m_texture.m_textures[0].get(), c.coords, c.orientation,
                         c.color, outgoing.m_texture.m_textures[0].get(), c.outgoingCoords,
                         c.outgoingOrientation, c.outgoingColor, 0xFF);
  *singleDraws = draws - before;
  const auto single = Read();
  float worst = 0;
  for (size_t i = 0; i < reference.size(); ++i)
    worst = std::max(worst, std::fabs(reference[i] - single[i]));
  (void)system;
  return worst;
}
// An opaque pair below a fading parent: the pair as composed at full opacity,
// mixed with what is behind it by the parent's opacity, in one draw.
static float GroupDifference(CGUITextureGLES& incoming, CGUITextureGLES& outgoing, const Case& c,
                             unsigned char group, bool* blended)
{
  float x[4]{4, 44, 44, 4}, y[4]{3, 3, 45, 45}, z[4]{0, 0, 0, 0};
  glClearColor(c.clear[0], c.clear[1], c.clear[2], c.clear[3]);
  glClear(GL_COLOR_BUFFER_BIT);
  const auto behind = Read();
  const auto draw = [&](unsigned char alpha)
  {
    glClear(GL_COLOR_BUFFER_BIT);
    incoming.DrawCrossfade(x, y, z, incoming.m_texture.m_textures[0].get(), c.coords, c.orientation,
                           c.color, outgoing.m_texture.m_textures[0].get(), c.outgoingCoords,
                           c.outgoingOrientation, c.outgoingColor, alpha);
    return Read();
  };
  const auto full = draw(0xFF);
  const auto single = draw(group);
  *blended = glIsEnabled(GL_BLEND);
  const float g = group / 255.0f;
  float worst = 0;
  for (size_t i = 0; i < single.size(); ++i)
    worst = std::max(worst, std::fabs(g * full[i] + (1 - g) * behind[i] - single[i]));
  return worst;
}
int main(int argc, char** argv)
{
  EGLDisplay display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
  if (display == EGL_NO_DISPLAY || !eglInitialize(display, nullptr, nullptr) || !eglBindAPI(EGL_OPENGL_ES_API))
    return 5;
  const EGLint configAttributes[]{EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
                                  EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
  EGLConfig config{};
  EGLint count = 0;
  if (!eglChooseConfig(display, configAttributes, &config, 1, &count) || !count)
    return 5;
  const EGLint contextAttributes[]{EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
  EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttributes);
  const EGLint surfaceAttributes[]{EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
  EGLSurface surface = eglCreatePbufferSurface(display, config, surfaceAttributes);
  if (context == EGL_NO_CONTEXT || surface == EGL_NO_SURFACE || !eglMakeCurrent(display, surface, surface, context))
    return 5;

  // A half-float target isolates the arithmetic; RGBA8 is the GUI's framebuffer format
  // and adds the fixed-point rounding of each blend.
  GLuint targets[2]{}, fbos[2]{};
  glGenTextures(2, targets);
  glGenFramebuffers(2, fbos);
  for (int i = 0; i < 2; ++i)
  {
    glBindTexture(GL_TEXTURE_2D, targets[i]);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, SIZE, SIZE, 0, GL_RGBA,
                 i ? 0x8D61 /* GL_HALF_FLOAT_OES */ : GL_UNSIGNED_BYTE, nullptr);
    glBindFramebuffer(GL_FRAMEBUFFER, fbos[i]);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, targets[i], 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
      return 5;
  }
  glViewport(0, 0, SIZE, SIZE);
  glDisable(GL_DEPTH_TEST);

  CRenderSystemGLES system;
  system.width = system.height = SIZE;
  CGUITextureGLES incoming, outgoing;
  incoming.m_renderSystem = outgoing.m_renderSystem = &system;
  const auto translucentIncoming = Image(16, 16, 1), translucentOutgoing = Image(9, 13, 2);
  const auto opaqueIncoming = Image(16, 16, 1, true), opaqueOutgoing = Image(9, 13, 2, true);
  incoming.m_texture.m_textures = {translucentIncoming};
  outgoing.m_texture.m_textures = {translucentOutgoing};

  unsigned checks = 0;
  float worst[2]{};
  const char* variants[]{@VARIANTS@};
  const size_t variantCount = sizeof(variants) / sizeof(variants[0]) / 3;
  for (size_t v = 0; v < variantCount; ++v)
  {
    system.programs.clear();
    system.programs[ShaderMethodGLES::SM_TEXTURE].id = Link(variants[v * 3], variants[v * 3 + 1]);
    system.programs[ShaderMethodGLES::SM_CROSSFADE].id = Link(variants[v * 3], variants[v * 3 + 2]);
    for (int target = 0; target < 2; ++target)
    for (bool limited : {false, true})
    {
      floatTarget = target == 1;
      glBindFramebuffer(GL_FRAMEBUFFER, fbos[target]);
      const float tolerance = floatTarget ? 0.002f : 3.5f / 255.0f;
      limitedColor = limited;
      const uint32_t colors[]{0x37FFFFFF, 0x80FFFFFF, 0x10FFFFFF, 0xF0C08040, 0x00FFFFFF, 0xFE204060};
      const float clears[][4]{{0, 0, 0, 0}, {0.2f, 0.4f, 0.6f, 1}, {0.9f, 0.1f, 0.3f, 0.5f}};
      const CRect coordinates[]{{0, 0, 1, 1}, {0.1f, 0.2f, 0.9f, 0.7f}, {0.3f, 0, 0.8f, 1}};
      for (uint32_t color : colors)
        for (uint32_t outgoingColor : colors)
          for (const auto& clear : clears)
            for (int set = 0; set < 3; ++set)
              for (int orientation : {0, 4})
              {
                Case c{color, outgoingColor, {clear[0], clear[1], clear[2], clear[3]},
                       coordinates[set], coordinates[(set + 1) % 3], orientation, set == 2 ? 4 : 0};
                unsigned separate = 0, single = 0;
                const float difference = MaxDifference(system, incoming, outgoing, c, &separate, &single);
                ++checks;
                worst[target] = std::max(worst[target], difference);
                if (separate != 2 || single != 1)
                {
                  std::fprintf(stderr, "FAIL draw count separate=%u single=%u\n", separate, single);
                  return 1;
                }
                if (difference > tolerance)
                {
                  std::fprintf(stderr, "FAIL pixel difference %.4f variant=%zu target=%s limited=%d color=%08x outgoing=%08x orientation=%d\n",
                               difference, v, floatTarget ? "half" : "rgba8", limited, color, outgoingColor, orientation);
                  return 1;
                }
              }
      // Opaque images under an opaque outgoing color dissolve: the single pass
      // matches drawing the opaque outgoing and then the incoming image, and
      // covers every pixel without blending.
      incoming.m_texture.m_textures = {opaqueIncoming};
      outgoing.m_texture.m_textures = {opaqueOutgoing};
      for (uint32_t color : colors)
        for (uint32_t outgoingColor : {0xFFC08040u, 0xFF204060u})
          for (const auto& clear : clears)
          {
            Case c{color, outgoingColor, {clear[0], clear[1], clear[2], clear[3]},
                   coordinates[0], coordinates[1], 0, 4};
            unsigned separate = 0, single = 0;
            const float difference = MaxDifference(system, incoming, outgoing, c, &separate, &single);
            ++checks;
            worst[target] = std::max(worst[target], difference);
            if (glIsEnabled(GL_BLEND))
            {
              std::fprintf(stderr, "FAIL opaque pair blended color=%08x outgoing=%08x\n", color, outgoingColor);
              return 1;
            }
            if (separate != 2 || single != 1 || difference > tolerance)
            {
              std::fprintf(stderr, "FAIL pixel difference %.4f opaque pair variant=%zu target=%s limited=%d color=%08x outgoing=%08x\n",
                           difference, v, floatTarget ? "half" : "rgba8", limited, color, outgoingColor);
              return 1;
            }
          }
      // Below a fading parent the pair is mixed as a whole, at every opacity.
      for (uint32_t color : colors)
        for (const auto& clear : clears)
          for (unsigned char group : {254, 200, 128, 37, 1, 0})
          {
            Case c{color, 0xFFC08040u, {clear[0], clear[1], clear[2], clear[3]},
                   coordinates[0], coordinates[1], 0, 4};
            bool blended = false;
            const float difference = GroupDifference(incoming, outgoing, c, group, &blended);
            ++checks;
            worst[target] = std::max(worst[target], difference);
            if (!blended || difference > tolerance + (floatTarget ? 0.0f : 1.0f / 255.0f))
            {
              std::fprintf(stderr, "FAIL pixel difference %.4f group pair variant=%zu target=%s limited=%d color=%08x group=%u blended=%d\n",
                           difference, v, floatTarget ? "half" : "rgba8", limited, color, group, blended);
              return 1;
            }
          }
      incoming.m_texture.m_textures = {translucentIncoming};
      outgoing.m_texture.m_textures = {translucentOutgoing};
    }
  }
  if (glGetError() != GL_NO_ERROR)
  {
    std::fprintf(stderr, "FAIL GL error\n");
    return 1;
  }
  std::printf("PASS %u crossfade comparisons, %zu shader variants: single pass matches two passes "
              "within %.5f (half float) and %.1f/255 (RGBA8), one draw instead of two\n",
              checks, variantCount, worst[1], worst[0] * 255);
  (void)argc;
  (void)argv;
  return 0;
}
'''

VARIANT_DEFINES = ['', '#define KODI_LIMITED_RANGE 1\n', '#define KODI_GUI_COLOUR 1\n',
                   '#define KODI_TRANSFER_PQ 1\n',
                   '#define KODI_LIMITED_RANGE 1\n#define KODI_TRANSFER_PQ 1\n#define KODI_GUI_COLOUR 1\n']


def build(sources, crossfade_shader):
    texture = sources['xbmc/guilib/GUITextureGLES.cpp']
    header = sources['xbmc/guilib/GUITextureGLES.h']
    glutils = sources['xbmc/utils/GLUtils.cpp']
    methods = [function(texture, s) for s in ('void CGUITextureGLES::BeginDraw(', 'void CGUITextureGLES::End()',
                                               'void CGUITextureGLES::Draw(', 'void CGUITextureGLES::DrawCrossfade(')]
    packed = re.search(r'struct PackedVertex\n\{[^}]*\};', header).group(0)
    channel = function(glutils, 'uint8_t KODI::UTILS::GL::GetChannelFromARGB(')
    variants = []
    for defines in VARIANT_DEFINES:
        variants += [cstring(shader('gles_shader.vert', defines, False)),
                     cstring(shader('gles_shader_texture.frag', defines, True)),
                     cstring(crossfade_shader(defines))]
    return (PRELUDE.replace('@CHANNEL@', channel).replace('@PACKED@', packed) + '\n'.join(methods) +
            TESTS.replace('@VARIANTS@', ', '.join(variants)))


def run(source, directory, name):
    cpp = directory / (name + '.cpp')
    cpp.write_text(source)
    binary = directory / name
    command = ['c++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
               '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(cpp)]
    if os.environ.get('GLES_HEADERS'):
        command += ['-I' + os.environ['GLES_HEADERS']]
    command += ['-Wl,--no-as-needed', '-l:libEGL.so.1', '-l:libGLESv2.so.2', '-o', str(binary)]
    subprocess.run(command, check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True,
                          env={**os.environ, 'EGL_PLATFORM': 'surfaceless', 'LIBGL_ALWAYS_SOFTWARE': '1',
                               'ASAN_OPTIONS': 'detect_leaks=0'})


def main():
    paths = ['xbmc/guilib/GUITextureGLES.cpp', 'xbmc/guilib/GUITextureGLES.h', 'xbmc/utils/GLUtils.cpp']
    sources = {p: (ROOT / p).read_text() for p in paths}
    production = lambda defines: shader('gles_shader_crossfade.frag', defines, True)
    with tempfile.TemporaryDirectory(prefix='gui-crossfade-') as temporary:
        directory = Path(temporary)
        result = run(build(sources, production), directory, 'production')
        if result.returncode != 0:
            sys.exit('production failed (exit ' + str(result.returncode) + '): ' + result.stdout + result.stderr)
        print(result.stdout.strip())
        # Semantic controls: each must break the comparison.
        def shader_edit(old, new):
            def make(defines):
                text = production(defines)
                assert text.count(old) == 1, old
                return text.replace(old, new)
            return make
        def source_edit(old, new):
            edited = dict(sources)
            body = function(edited['xbmc/guilib/GUITextureGLES.cpp'], 'void CGUITextureGLES::DrawCrossfade(')
            assert body.count(old) == 1, old
            edited['xbmc/guilib/GUITextureGLES.cpp'] = edited['xbmc/guilib/GUITextureGLES.cpp'].replace(body, body.replace(old, new))
            return edited
        controls = [
            ('outgoing-not-covered', sources, shader_edit('float under = outgoing.a * (1.0 - incoming.a);', 'float under = outgoing.a;'), 'pixel difference'),
            ('straight-alpha-blend', source_edit('glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA', 'glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA'), production, 'pixel difference'),
            ('outgoing-unadjusted', sources, shader_edit('  outgoing.rgb *= (235.0 - 16.0) / 255.0;\n  outgoing.rgb += 16.0 / 255.0;\n', ''), 'pixel difference'),
            ('orientation-ignored', source_edit('const bool swap = orientation & 4;', 'const bool swap = false;'), production, 'pixel difference'),
            ('opaque-pair-blended', source_edit('if (outgoingRGBA[3] >= 1.0f && !texture->HasAlpha() && !outgoing->HasAlpha())', 'if (false)'), production, 'opaque pair blended'),
            ('group-alpha-ignored', source_edit('if (groupAlpha != 0xFF)', 'if (false)'), production, 'group pair'),
            ('group-alpha-composes-layers', source_edit('glBlendFuncSeparate(GL_CONSTANT_ALPHA, GL_ONE_MINUS_CONSTANT_ALPHA, GL_CONSTANT_ALPHA,', 'glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE_MINUS_DST_ALPHA,'), production, 'group pair'),
        ]
        for name, edited, crossfade, witness in controls:
            result = run(build(edited, crossfade), directory, name)
            if result.returncode != 1 or witness not in result.stderr:
                sys.exit('control survived: ' + name + ' ' + str(result.returncode) + result.stderr)
            print('PASS semantic control ' + name)
        print(len(controls), 'semantic controls rejected')


if __name__ == '__main__':
    main()
