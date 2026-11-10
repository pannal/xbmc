#!/usr/bin/env python3
"""Exercise the production video control with a model of the GUI depth buffer.

Checks layered backgrounds, foreground pixels, clipping and the single-pass
clear path. Services and pixel composition are fixtures. This is host evidence,
not a GL driver or device playback test.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

FIXTURE = r'''
#include "utils/Geometry.h"
#include "utils/TransformMatrix.h"
#include <cassert>
#include <cstdio>
#include <stack>
#include <type_traits>
#include <vector>

enum RENDER_ORDER
{
  RENDER_ORDER_ALL_BACK_TO_FRONT,
  RENDER_ORDER_FRONT_TO_BACK,
  RENDER_ORDER_BACK_TO_FRONT
};

struct Pixel
{
  float x, y, depth;
  unsigned int color;
};

struct CGraphicContext
{
  struct Transform
  {
    TransformMatrix matrix;
    float scaleX{1.0f}, scaleY{1.0f};
  };
  Transform m_finalTransform;
  std::stack<Transform> m_transforms;
  uint32_t m_layer{64};
  RENDER_ORDER order{RENDER_ORDER_BACK_TO_FRONT};
  CRect scissors{0, 0, 8, 8};
  std::vector<Pixel> pixels;
  unsigned int quadCalls{0}, clearCalls{0}, viewCalls{0};

  void SetTransform(const TransformMatrix&, float, float);
  void RemoveTransform();
  float GetNormalizedDepth(uint32_t);
  float GetTransformDepth(int32_t offset = 0);
  RENDER_ORDER GetRenderOrder() const { return order; }
  CRect GetScissors() const { return scissors; }
  void SetScissors(const CRect& rect) { scissors = rect; }
  void SetViewWindow(float, float, float, float) { ++viewCalls; }
  unsigned int MergeAlpha(unsigned int color) const { return color; }

  static bool Contains(const CRect& rect, const Pixel& pixel)
  {
    return pixel.x >= rect.x1 && pixel.x < rect.x2 &&
           pixel.y >= rect.y1 && pixel.y < rect.y2;
  }

  void Clear(unsigned int color)
  {
    ++clearCalls;
    for (auto& pixel : pixels)
      if (Contains(scissors, pixel))
        pixel.color = color;
  }
};

struct CApplicationPlayer
{
  bool playing{true}, hardware{true}, paused{false};
  unsigned int renderCalls{0}, renderAlpha{0};
  bool IsRenderingVideo() const { return playing; }
  bool IsRenderingVideoLayer() const { return hardware; }
  bool IsPausedPlayback() const { return paused; }
  void Render(bool, unsigned int alpha) { ++renderCalls; renderAlpha = alpha; }
};

struct CApplicationPowerHandling
{
  bool screensaver{false};
  unsigned int resets{0};
  bool IsPythonScreenSaverActive() const { return screensaver; }
  void ResetScreenSaver() { ++resets; }
};

struct Components
{
  CApplicationPlayer player;
  CApplicationPowerHandling power;
  template<typename T> T* GetComponent()
  {
    if constexpr (std::is_same_v<T, CApplicationPlayer>)
      return &player;
    else
      return &power;
  }
};

static CGraphicContext graphics;
static Components components;
struct Window
{
  CGraphicContext& GetGfxContext() { return graphics; }
};
struct CServiceBroker
{
  static Window* GetWinSystem() { static Window window; return &window; }
  static Components& GetAppComponents() { return components; }
};

struct CGUITexture
{
  static void DrawQuad(const CRect& rect, unsigned int color, void*, void*,
                       float depth, bool blending)
  {
    ++graphics.quadCalls;
    for (auto& pixel : graphics.pixels)
      if (CGraphicContext::Contains(rect, pixel) &&
          CGraphicContext::Contains(graphics.scissors, pixel) && depth >= pixel.depth)
      {
        // The video opening replaces RGBA. Blending a zero-alpha color cannot open it.
        if (!blending || color >> 24 == 255)
          pixel.color = color;
      }
  }
};

struct CGUIControl
{
  unsigned int baseCalls{0};
  void Render() { ++baseCalls; }
};
struct CGUIVideoControl : CGUIControl
{
  float m_posX{0}, m_posY{0}, m_width{8}, m_height{8};
  TransformMatrix m_cachedTransform;
  CRect GetRenderRegion() const
  {
    return CRect(m_posX, m_posY, m_posX + m_width, m_posY + m_height);
  }
  void Render();
};

@METHODS@

static void Reset()
{
  graphics = CGraphicContext{};
  components = Components{};
}

int main()
{
  const CRect viewports[] = {CRect(2, 2, 6, 6), CRect(0, 0, 8, 8)};
  const CRect clips[] = {CRect(0, 0, 8, 8), CRect(3, 3, 7, 7), CRect(0, 0, 1, 1)};
  for (const auto& viewport : viewports)
    for (const auto& clip : clips)
      for (auto order : {RENDER_ORDER_ALL_BACK_TO_FRONT, RENDER_ORDER_BACK_TO_FRONT})
        for (uint32_t layer : {8U, 24U, 40U})
        {
          Reset();
          graphics.order = order;
          graphics.scissors = clip;
          CGUIVideoControl control;
          control.m_posX = viewport.x1;
          control.m_posY = viewport.y1;
          control.m_width = viewport.Width();
          control.m_height = viewport.Height();
          control.m_cachedTransform.depth = layer;
          graphics.m_finalTransform.matrix = control.m_cachedTransform;
          const float controlDepth = graphics.GetNormalizedDepth(layer);

          for (int y = 0; y < 8; ++y)
            for (int x = 0; x < 8; ++x)
            {
              // In the dual pass, foreground controls have already drawn too.
              const bool foreground = x == 4 && y == 4;
              float depth = controlDepth - 0.1875f;
              depth = std::max(depth, controlDepth - 0.125f);
              depth = std::max(depth, controlDepth - 0.0625f);
              if (foreground && order == RENDER_ORDER_BACK_TO_FRONT)
                depth = controlDepth + 0.125f;
              const bool drawnForeground = foreground && order == RENDER_ORDER_BACK_TO_FRONT;
              graphics.pixels.push_back({x + 0.5f, y + 0.5f, depth,
                                        drawnForeground ? 0xffff0000U : 0xff000000U});
            }
          const auto before = graphics.pixels;
          control.Render();

          for (size_t i = 0; i < before.size(); ++i)
          {
            const bool visible = CGraphicContext::Contains(viewport, before[i]) &&
                                 CGraphicContext::Contains(clip, before[i]);
            const bool foreground = before[i].color == 0xffff0000U;
            const bool cleared = visible &&
                (order == RENDER_ORDER_ALL_BACK_TO_FRONT || !foreground);
            if (graphics.pixels[i].color != (cleared ? 0U : before[i].color))
            {
              std::fputs("FAIL video opening or foreground preservation\n", stderr);
              return 1;
            }
          }
          assert(graphics.scissors == clip);
          assert(graphics.m_transforms.empty());
          assert(graphics.m_finalTransform.matrix.depth == layer);
          assert(control.baseCalls == 1 && graphics.viewCalls == 1);
          assert(components.power.resets == 1 && components.player.renderCalls == 0);
          assert(graphics.quadCalls == (order == RENDER_ORDER_BACK_TO_FRONT ? 1U : 0U));
          assert(graphics.clearCalls == (order == RENDER_ORDER_ALL_BACK_TO_FRONT ? 1U : 0U));
        }

  Reset();
  graphics.order = RENDER_ORDER_FRONT_TO_BACK;
  CGUIVideoControl control;
  control.Render();
  assert(control.baseCalls == 0 && graphics.viewCalls == 0 && graphics.quadCalls == 0);

  Reset();
  components.player.hardware = false;
  control.Render();
  assert(components.player.renderCalls == 1 && components.player.renderAlpha == 255);
  assert(graphics.quadCalls == 0 && graphics.clearCalls == 0);

  Reset();
  components.player.playing = false;
  control.Render();
  assert(graphics.viewCalls == 0 && components.player.renderCalls == 0);
  std::puts("PASS layered video openings, foreground controls, clipping and render modes");
}
'''


def main():
    render = function((ROOT / 'xbmc/guilib/GUIVideoControl.cpp').read_text(),
                      'void CGUIVideoControl::Render()')
    graphics = (ROOT / 'xbmc/windowing/GraphicContext.cpp').read_text()
    methods = '\n'.join(function(graphics, signature) for signature in (
        'void CGraphicContext::SetTransform(const TransformMatrix &matrix, float scaleX, float scaleY)',
        'void CGraphicContext::RemoveTransform()',
        'float CGraphicContext::GetNormalizedDepth(uint32_t depth)',
        'float CGraphicContext::GetTransformDepth(int32_t depthOffset)',
    ))
    depth = 'CServiceBroker::GetWinSystem()->GetGfxContext().GetNormalizedDepth(m_cachedTransform.depth)'
    assert render.count(depth) == 1
    variants = {
        'production': render,
        'old-depth': render.replace(depth, '-1.0f'),
        'identity-transform-depth': render.replace(depth,
            'CServiceBroker::GetWinSystem()->GetGfxContext().GetTransformDepth()'),
        'always-front': render.replace(depth, '1.0f'),
    }
    with tempfile.TemporaryDirectory(prefix='gui-video-depth-') as temporary:
        out = Path(temporary)
        for name, body in variants.items():
            source = out / (name + '.cpp')
            binary = out / name
            source.write_text(FIXTURE.replace('@METHODS@', methods + '\n' + body))
            subprocess.run([
                os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                '-I' + str(ROOT / 'xbmc'), str(source), '-o', str(binary),
            ], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True,
                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            if name == 'production':
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout.strip())
            else:
                assert result.returncode == 1 and 'FAIL video opening' in result.stderr, result.stderr
                print('Rejected regression variant', name)


if __name__ == '__main__':
    main()
