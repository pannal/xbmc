#!/usr/bin/env python3
"""Disc menu graphics: plain premultiply on SDR output, bilinear filtering.

Compiles the production OverlayRendererUtil build_rgba/convert_rgba (with the
real CDVDOverlayImage header), PremultiplyPlain, COverlay::PlainPremultiplyDiscMenu,
the Amlogic IsGuiOutputHdr and the texture filter choice from
COverlayTextureGLES::Render, under ASan/UBSan, and checks:
- blending a plain-premultiplied texel as the GLES overlay pass does
  (GL_ONE, GL_ONE_MINUS_SRC_ALPHA) matches the authored straight-alpha blend to
  1 LSB, for palette (HDMV IG) and ARGB (BD-J) graphics alike;
- the linear-light premultiply stays the default for everything else;
- disc menus keep GL_LINEAR at every subtitle zoom, subtitles keep their policy.
Does not claim GPU validation. Temporary outputs are removed.
"""
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    renderers = ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers'
    util = (renderers / 'OverlayRendererUtil.cpp').read_text()
    gles = (renderers / 'OverlayRendererGLES.cpp').read_text()
    renderer = (renderers / 'OverlayRenderer.cpp').read_text()
    context = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()

    # Both disc menu paths are prepared from the one decision (executed in
    # tools/test-overlay-resource-inputs.py), made where the texture is created.
    create = function(gles, 'std::shared_ptr<COverlay> COverlay::Create(const CDVDOverlayImage& o')
    assert 'PlainPremultiplyDiscMenu(o)' in create
    prepare = function(gles, 'COverlayTextureGLES::PreparedImage COverlayTextureGLES::PrepareImage(')
    argb = prepare[prepare.index('if (image.premultiplied && image.plainPmaMenu)'):prepare.index('else if (image.premultiplied)')]
    assert 'PremultiplyPlain(src[col])' in argb and 'o.linesize' in argb
    assert 'convert_rgba(o, image.premultiplied, rgba, !image.plainPmaMenu);' in prepare
    ctor = function(gles, 'COverlayTextureGLES::COverlayTextureGLES(const CDVDOverlayImage& o')
    assert 'm_plainPmaMenu = image.plainPmaMenu;' in ctor

    render = function(gles, 'void COverlayTextureGLES::Render(')
    begin = render.index('GLenum filter = GL_LINEAR;')
    choose = render[begin:render.index('glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER', begin)]

    source = PRELUDE
    source += '\n' + re.search(r'\nfloat SrgbToLinear\(.*?\n}\n', util, re.S)[0]
    source += re.search(r'\nint LinearToSrgb8\(.*?\n}\n', util, re.S)[0]
    source += function(util, 'static uint32_t build_rgba(int a, int r, int g, int b')
    # The header's declaration carries the default argument.
    header = (renderers / 'OverlayRendererUtil.h').read_text()
    source += '\n' + re.search(r'void convert_rgba\(const CDVDOverlayImage& o,.*?\);', header, re.S)[0]
    source += '\n' + function(util, 'void convert_rgba(const CDVDOverlayImage& o')
    source += '\n}\nusing namespace OVERLAY;\n'
    source += function(gles, 'uint32_t PremultiplyPlain(')
    source += '\n' + function(renderer, 'bool COverlay::PlainPremultiplyDiscMenu(')
    source += '\n' + function(context, 'bool CWinSystemAmlogicGLESContext::IsGuiOutputHdr(')
    source += '\nGLenum ChooseFilter(bool m_isBitmapOverlay, bool m_discMenuOverlay)\n{\n  ' + choose + \
              '  return filter;\n}\n'
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='disc-menu-edges-test-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#include <stdint.h>\n#define PIXEL_ASHIFT 24\n'
                                            '#define PIXEL_RSHIFT 16\n#define PIXEL_GSHIFT 8\n#define PIXEL_BSHIFT 0\n')
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-unused-function', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], check=True, capture_output=True, text=True)
    print(result.stdout, end='')
    print('Disc menu overlay edges: PASS (production premultiply/filter/output decision; ASan/UBSan)')


PRELUDE = r'''
#include <array>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <vector>
#include "PlatformDefs.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
typedef unsigned int GLenum;
constexpr GLenum GL_NEAREST=0x2600,GL_LINEAR=0x2601,GL_LINEAR_MIPMAP_LINEAR=0x2703;
struct Settings {int zoom=100;int GetInt(const char*){return zoom;}};
struct CSettings {static constexpr const char* SETTING_SUBTITLES_BITMAPZOOM="subtitles.bitmapzoom";};
struct SettingsComponent {Settings s;Settings* GetSettings(){return &s;}};
struct Window {bool guiHdr=false;bool IsGuiOutputHdr(){return guiHdr;}};
struct CServiceBroker {
  static SettingsComponent* GetSettingsComponent(){static SettingsComponent c;return &c;}
  static Window* GetWinSystem(){static Window w;return &w;}
};
static bool pqAtOpen=false,dvAtOpen=false;
bool aml_transfer_pq_at_open(){return pqAtOpen;}
bool aml_dv_core_at_open(){return dvAtOpen;}
enum DV_MODE{DV_MODE_ON=0,DV_MODE_ON_DEMAND=1,DV_MODE_OFF=2};static DV_MODE dvMode=DV_MODE_ON_DEMAND;
DV_MODE aml_dv_mode(){return dvMode;}
struct CWinSystemAmlogicGLESContext {bool IsGuiOutputHdr();};
struct COverlay {static bool PlainPremultiplyDiscMenu(const CDVDOverlayImage& o);};
namespace OVERLAY {
'''

TESTS = r'''
static int ch(uint32_t c,int shift){return (c>>shift)&0xff;}
// GL_ONE, GL_ONE_MINUS_SRC_ALPHA in 8-bit, texel over an opaque background.
static int blendPma(int texel,int a,int bg){return (int)std::lround(texel+bg*(1.0-a/255.0));}
// The authored result: straight alpha, blended as drawn.
static int blendStraight(int c,int a,int bg){return (int)std::lround(c*(a/255.0)+bg*(1.0-a/255.0));}

static uint32_t palettePixel(uint32_t argb,bool linearLight){
  CDVDOverlayImage o;o.width=o.height=1;o.linesize=1;o.pixels={0};o.palette={argb};
  std::vector<uint32_t> out(1);OVERLAY::convert_rgba(o,true,out,linearLight);return out[0];
}

int main(){
  // Plain premultiply: palette (IG) and ARGB (BD-J) agree exactly and blend as
  // authored to 1 LSB over every background.
  int worstPlain=0,worstLinear=0;
  for(int a=0;a<256;++a)
    for(int c=0;c<256;++c){
      const uint32_t argb=(uint32_t)a<<24|(uint32_t)c<<16|(uint32_t)(255-c)<<8|(uint32_t)(c/2);
      const uint32_t plain=palettePixel(argb,false);
      assert(plain==PremultiplyPlain(argb));
      assert(ch(plain,24)==a);
      const uint32_t linear=palettePixel(argb,true);
      assert(ch(linear,24)==a);
      for(int bg:{0,16,64,128,235,255}){
        worstPlain=std::max(worstPlain,std::abs(blendPma(ch(plain,16),a,bg)-blendStraight(c,a,bg)));
        worstLinear=std::max(worstLinear,std::abs(blendPma(ch(linear,16),a,bg)-blendStraight(c,a,bg)));
      }
    }
  assert(worstPlain<=1);
  // The linear-light factor is what brightens the edges: white at 25% over black.
  const int plainEdge=ch(palettePixel(0x40ffffff,false),16),linearEdge=ch(palettePixel(0x40ffffff,true),16);
  assert(plainEdge==64&&linearEdge>=130);
  std::printf("white 25%% edge over black: authored 64, plain %d, linear-light %d; worst blend error plain %d, linear-light %d LSB\n",
              plainEdge,linearEdge,worstPlain,worstLinear);
  // Default argument keeps the linear-light premultiply for every other caller.
  {CDVDOverlayImage o;o.width=o.height=1;o.linesize=1;o.pixels={0};o.palette={0x40ffffffu};
   std::vector<uint32_t> out(1);OVERLAY::convert_rgba(o,true,out);assert(out[0]==palettePixel(0x40ffffff,true));}
  // Unpremultiplied conversion is untouched by the flag.
  assert(palettePixel(0x40ffffff,false)!=0x40ffffffu);
  {CDVDOverlayImage o;o.width=o.height=1;o.linesize=1;o.pixels={0};o.palette={0x40ffffffu};
   std::vector<uint32_t> out(1);OVERLAY::convert_rgba(o,false,out,false);assert(out[0]==0x40ffffffu);}

  // Which overlays are premultiplied plainly.
  auto* window=CServiceBroker::GetWinSystem();
  CDVDOverlayImage menu,subtitle;menu.SetDiscMenuOverlay(true);subtitle.SetDiscMenuOverlay(false);
  window->guiHdr=false;assert(COverlay::PlainPremultiplyDiscMenu(menu)&&!COverlay::PlainPremultiplyDiscMenu(subtitle));
  window->guiHdr=true;assert(!COverlay::PlainPremultiplyDiscMenu(menu)&&!COverlay::PlainPremultiplyDiscMenu(subtitle));
  // PQ-tagged menu graphics: plain on any output (the PQ-to-SDR shader divides by alpha); PQ subtitles untouched.
  menu.m_isHdrPq=subtitle.m_isHdrPq=true;
  for(bool hdr:{false,true}){window->guiHdr=hdr;assert(COverlay::PlainPremultiplyDiscMenu(menu)&&!COverlay::PlainPremultiplyDiscMenu(subtitle));}

  // The Amlogic GUI output: HDR when the last open set the PQ GUI transfer or engaged the DV core.
  CWinSystemAmlogicGLESContext ctx;
  // DV mode On keeps the GUI in DV before the first decoder open.
  for(DV_MODE m:{DV_MODE_ON,DV_MODE_ON_DEMAND,DV_MODE_OFF})for(bool pq:{false,true})for(bool dv:{false,true}){
    dvMode=m;pqAtOpen=pq;dvAtOpen=dv;assert(ctx.IsGuiOutputHdr()==(pq||dv||m==DV_MODE_ON));}

  // Filtering: disc menus bilinear at every zoom; subtitles keep the zoom policy.
  auto& zoom=CServiceBroker::GetSettingsComponent()->GetSettings()->zoom;
  for(int z:{50,100,150}){zoom=z;assert(ChooseFilter(true,true)==GL_LINEAR);assert(ChooseFilter(false,false)==GL_LINEAR);}
  zoom=100;assert(ChooseFilter(true,false)==GL_NEAREST);
  zoom=50;assert(ChooseFilter(true,false)==GL_LINEAR_MIPMAP_LINEAR);
  zoom=150;assert(ChooseFilter(true,false)==GL_LINEAR);
  return 0;
}
'''

if __name__ == '__main__':
    main()
