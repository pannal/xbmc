#!/usr/bin/env python3
"""Exercise production ApplyStyle/ConfigureAssOverride with recording libass stubs.

Uses the real style/options definitions. ASS structs, API calls, handler bootstrap,
logging and color conversion are fixtures; no libass rasterization or GUI/device
behavior is claimed. Both pre-0.17.4 and current blur-override branches are tested.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    source = (ROOT / 'xbmc/cores/VideoPlayer/DVDSubtitles/DVDSubtitlesLibass.cpp').read_text()
    code = PRELUDE
    for signature in ['void CDVDSubtitlesLibass::ApplyStyle(',
                      'void CDVDSubtitlesLibass::ConfigureAssOverride(']:
        code += '\n' + function(source, signature)
    code += TESTS
    with tempfile.TemporaryDirectory(prefix='libass-style-test-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(code)
        for version in ['0x01701000', '0x01704000']:
            executable = out / version
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                            '-Werror', '-Werror=double-promotion',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-DLIBASS_VERSION=' + version, '-I', str(ROOT / 'xbmc'),
                            str(out / 'test.cpp'), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
    print('Libass style application: PASS (production helpers, recording API stubs; '
          'native/adapted, overrides, margins/alignment, old/current API; ASan/UBSan)')


PRELUDE = r'''
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include "cores/VideoPlayer/DVDSubtitles/SubtitlesStyle.h"
using namespace KODI::SUBTITLES::STYLE;
using namespace UTILS;
namespace KODI::SUBTITLES { constexpr const char* FONT_DEFAULT_FAMILYNAME = "DEFAULT"; }
constexpr int LOGDEBUG=0, LOGERROR=1, LOGWARNING=2;
struct CLog { template<typename... T> static void Log(T...) {}
              template<typename... T> static void LogF(T...) {} };
constexpr int ASS_BORDER_STYLE_OUTLINE=1, ASS_BORDER_STYLE_BOX=3, ASS_BORDER_STYLE_SQUARE_BOX=4;
constexpr int VALIGN_TOP=4, VALIGN_CENTER=8, VALIGN_SUB=0;
constexpr int HALIGN_LEFT=1, HALIGN_CENTER=2, HALIGN_RIGHT=3;
constexpr int ASS_OVERRIDE_DEFAULT=0, ASS_OVERRIDE_BIT_FONT_SIZE_FIELDS=1<<2,
  ASS_OVERRIDE_BIT_FONT_NAME=1<<3, ASS_OVERRIDE_BIT_COLORS=1<<4,
  ASS_OVERRIDE_BIT_ATTRIBUTES=1<<5, ASS_OVERRIDE_BIT_BORDER=1<<6,
  ASS_OVERRIDE_BIT_ALIGNMENT=1<<7, ASS_OVERRIDE_BIT_MARGINS=1<<8,
  ASS_OVERRIDE_BIT_BLUR=1<<11;
// Sentinel conversion exposes which input color/opacity each production branch
// passed; this deliberately does not validate Kodi's color conversion math.
COLOR::Color ConvColor(COLOR::Color color, int opacity=100)
{ return color ^ static_cast<COLOR::Color>(opacity); }
struct ASS_Style {
  char* Name=nullptr; char* FontName=nullptr;
  double FontSize=0, ScaleX=0, ScaleY=0, Spacing=0, Outline=0, Shadow=0, Blur=0;
  int Bold=0, Italic=0, BorderStyle=0, MarginL=0, MarginR=0, MarginV=0, Alignment=0;
  COLOR::Color PrimaryColour=0, SecondaryColour=0, OutlineColour=0, BackColour=0;
};
struct ASS_Track { ASS_Style styles[2]{}; int default_style=1, PlayResX=1920, PlayResY=1080; };
struct ASS_Renderer {
  ASS_Style selected{}; int flags=-1, selectCalls=0, enableCalls=0, spacingCalls=0;
  double lineSpacing=-1;
  ~ASS_Renderer() { free(selected.Name); free(selected.FontName); }
};
void ass_set_line_spacing(ASS_Renderer* renderer, double value)
{ ++renderer->spacingCalls; renderer->lineSpacing=value; }
void ass_set_selective_style_override(ASS_Renderer* renderer, ASS_Style* value)
{
  ++renderer->selectCalls;
  free(renderer->selected.Name); free(renderer->selected.FontName);
  // Record the temporary native style and retain its allocations for fixture
  // cleanup. This is not a claim about libass's allocation/ownership contract.
  renderer->selected=*value;
}
void ass_set_selective_style_override_enabled(ASS_Renderer* renderer, int flags)
{ ++renderer->enableCalls; renderer->flags=flags; }
constexpr int ADAPTED=0, NATIVE=1;
class CDVDSubtitlesLibass {
public:
  ASS_Track track{}; ASS_Renderer renderer{};
  ASS_Track* m_track=&track; ASS_Renderer* m_renderer=&renderer;
  int m_subtitleType=ADAPTED, m_currentDefaultStyleId=-1, m_defaultKodiStyleId=0;
  std::string m_defaultFontFamilyName="Resolved default";
  ~CDVDSubtitlesLibass() {
    for (auto& value : track.styles) { free(value.Name); free(value.FontName); }
  }
  void ApplyStyle(const std::shared_ptr<const style>&, renderOpts);
  void ConfigureAssOverride(const std::shared_ptr<const style>&, ASS_Style*);
};
void near(double actual,double expected) { assert(std::abs(actual-expected)<0.00001); }
'''

TESTS = r'''
int main()
{
  style input{};
  input.fontName=KODI::SUBTITLES::FONT_DEFAULT_FAMILYNAME;
  input.fontSize=40; input.fontStyle=FontStyle::BOLD_ITALIC;
  input.fontColor=COLOR::RED; input.fontOpacity=70;
  input.fontBorderSize=20; input.fontBorderColor=COLOR::BLUE;
  input.shadowSize=30; input.shadowColor=COLOR::GREEN; input.shadowOpacity=60;
  input.backgroundColor=COLOR::YELLOW; input.backgroundOpacity=50;
  input.blur=40; input.alignment=FontAlign::SUB_CENTER; input.marginVertical=80;
  auto publish=[&] { return std::make_shared<const style>(input); };
  renderOpts opts{}; opts.frameWidth=1920; opts.frameHeight=1080;
  CDVDSubtitlesLibass adapted;
  const auto original=publish();
  adapted.ApplyStyle(original,opts);
  const auto& result=adapted.track.styles[0];
  assert(adapted.m_currentDefaultStyleId==0);
  assert(std::string(result.Name)=="KodiDefault");
  assert(std::string(result.FontName)=="Resolved default");
  near(result.FontSize,60); near(result.ScaleX,1); near(result.ScaleY,1);
  assert(result.Bold==-1 && result.Italic==-1);
  assert(result.PrimaryColour==ConvColor(COLOR::RED,70));
  assert(result.SecondaryColour==ConvColor(COLOR::BLACK));
  assert(result.OutlineColour==ConvColor(COLOR::BLUE,70));
  assert(result.BackColour==ConvColor(COLOR::GREEN,60));
  near(result.Outline,3); near(result.Shadow,4.5); near(result.Blur,4);
  assert(result.MarginL==30 && result.MarginR==30 && result.MarginV==120);
  assert(result.Alignment==(VALIGN_SUB|HALIGN_CENTER));
  assert(adapted.renderer.selectCalls==0 && adapted.renderer.enableCalls==0);

  // Replacing a published const style neither mutates nor consumes its predecessor.
  input.fontName="Custom face"; input.fontSize=24;
  const auto replacement=publish();
  adapted.ApplyStyle(replacement,opts);
  assert(original->fontName=="DEFAULT" && original->fontSize==40);
  assert(std::string(result.FontName)=="Custom face"); near(result.FontSize,36);
  for (const auto font : {FontStyle::NORMAL, FontStyle::BOLD, FontStyle::ITALIC}) {
    input.fontStyle=font; adapted.ApplyStyle(publish(),opts);
    assert(result.Bold==(font==FontStyle::BOLD ? -1 : 0));
    assert(result.Italic==(font==FontStyle::ITALIC ? -1 : 0));
  }
  input.borderStyle=BorderType::OUTLINE_NO_SHADOW;
  adapted.ApplyStyle(publish(),opts); near(result.Shadow,0);
  assert(result.BackColour==ConvColor(COLOR::NONE,0));
  input.borderStyle=BorderType::BOX;
  adapted.ApplyStyle(publish(),opts);
  assert(result.BorderStyle==3); near(result.Outline,6); near(adapted.renderer.lineSpacing,12);
  assert(result.OutlineColour==ConvColor(COLOR::YELLOW,50));
  input.borderStyle=BorderType::SQUARE_BOX;
  adapted.ApplyStyle(publish(),opts);
  assert(result.BorderStyle==4); near(result.Shadow,6); near(adapted.renderer.lineSpacing,0);
  assert(result.BackColour==ConvColor(COLOR::YELLOW,50));

  // All nine style alignments, with each optional horizontal override.
  for(int alignment=0; alignment<9; ++alignment) {
    input.alignment=static_cast<FontAlign>(alignment);
    for(int horizontal=0; horizontal<4; ++horizontal) {
      opts.horizontalAlignment=static_cast<HorizontalAlign>(horizontal);
      adapted.ApplyStyle(publish(),opts);
      const int vertical=alignment<3 ? VALIGN_TOP : alignment<6 ? VALIGN_CENTER : VALIGN_SUB;
      assert(result.Alignment==(vertical|(horizontal ? horizontal : alignment%3+1)));
      assert(result.MarginL==(horizontal ? 318 : 30));
      assert(result.MarginR==result.MarginL);
    }
  }
  opts.marginsMode=MarginsMode::DISABLED;
  adapted.ApplyStyle(publish(),opts);
  assert(result.MarginL==0 && result.MarginR==0 && result.MarginV==0);
  opts.marginsMode=MarginsMode::INSIDE_ACTIVE_AREA;
  opts.activeAreaTopMargin=100; opts.activeAreaBottomMargin=200;
  for (const auto alignment : {FontAlign::TOP_CENTER,FontAlign::SUB_CENTER}) {
    input.alignment=alignment;
    for(bool user : {false,true}) {
      opts.activeAreaApplyUserPos=user; adapted.ApplyStyle(publish(),opts);
      assert(result.MarginV==(alignment==FontAlign::TOP_CENTER ? 100 : 200)+(user ? 86 : 0));
      assert(result.MarginL==0 && result.MarginR==0);
    }
  }
  opts.frameHeight=0;
  adapted.ApplyStyle(publish(),opts); assert(result.MarginV==120);
  opts.frameHeight=1080; opts.marginsMode=MarginsMode::DEFAULT;
  opts.horizontalAlignment=HorizontalAlign::DISABLED;

  // Native content retains its embedded style; only selective override state changes.
  CDVDSubtitlesLibass native; native.m_subtitleType=NATIVE;
  native.track.styles[1].FontSize=123; native.track.styles[1].MarginV=456;
  for(int mode=0; mode<4; ++mode) {
    for(bool font : {false,true}) {
      input.assOverrideStyles=static_cast<OverrideStyles>(mode); input.assOverrideFont=font;
      const int previousCalls=native.renderer.selectCalls;
      native.ApplyStyle(publish(),opts);
      assert(native.m_currentDefaultStyleId==1);
      assert(native.track.styles[1].FontSize==123 && native.track.styles[1].MarginV==456);
      int flags=mode==1 ? (ASS_OVERRIDE_BIT_ALIGNMENT|ASS_OVERRIDE_BIT_MARGINS) :
        mode>=2 ? (ASS_OVERRIDE_BIT_COLORS|ASS_OVERRIDE_BIT_ATTRIBUTES|
                   ASS_OVERRIDE_BIT_BORDER|ASS_OVERRIDE_BIT_MARGINS) : ASS_OVERRIDE_DEFAULT;
      if(mode==3) flags|=ASS_OVERRIDE_BIT_ALIGNMENT;
#if LIBASS_VERSION >= 0x01704000
      if(mode>=2) flags|=ASS_OVERRIDE_BIT_BLUR;
#endif
      if(font) flags|=ASS_OVERRIDE_BIT_FONT_SIZE_FIELDS|ASS_OVERRIDE_BIT_FONT_NAME;
      assert(native.renderer.flags==flags);
      assert(native.renderer.selectCalls==previousCalls+((mode || font) ? 1 : 0));
      if(mode || font) {
        near(native.renderer.selected.FontSize,(mode>=2 || font) ? 9.6 : 36);
        assert(native.renderer.selected.MarginV==120);
      }
    }
  }
  input.assOverrideStyles=OverrideStyles::DISABLED; input.assOverrideFont=false;
  const int previousCalls=native.renderer.selectCalls;
  native.ApplyStyle(publish(),opts);
  assert(native.renderer.flags==ASS_OVERRIDE_DEFAULT);
  assert(native.renderer.selectCalls==previousCalls);
  const int enableCalls=native.renderer.enableCalls;
  native.ApplyStyle(nullptr,opts); native.ConfigureAssOverride(nullptr,nullptr);
  assert(native.renderer.enableCalls==enableCalls);

  // Missing PlayResY fallback retains both default 288 and X-derived 4:3 scaling.
  adapted.track.PlayResY=0; adapted.track.PlayResX=0;
  adapted.ApplyStyle(publish(),opts); near(result.FontSize,9.6); assert(result.MarginV==32);
  adapted.track.PlayResX=640;
  adapted.ApplyStyle(publish(),opts); near(result.FontSize,16); assert(result.MarginV==53);
}
'''

if __name__ == '__main__':
    main()
