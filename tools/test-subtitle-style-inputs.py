#!/usr/bin/env python3
"""Run production style builders/conversion/options with stub settings/player/GPU/libass.

Retains real overlay/style types and extracts production state transitions. This
checks synchronous publication and evaluation, not service or worker thread safety.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-overlay-cache-identity.py'))
function = fixture['function']


def main():
    root = ROOT / 'xbmc/cores/VideoPlayer'
    renderer = (root / 'VideoRenderers/OverlayRenderer.cpp').read_text()
    header = (root / 'VideoRenderers/OverlayRenderer.h').read_text()
    debug = (root / 'VideoRenderers/DebugRenderer.cpp').read_text()
    debug_header = (root / 'VideoRenderers/DebugRenderer.h').read_text()
    for text, member in [(header, 'm_overlayStyle'), (debug_header, 'm_debugOverlayStyle')]:
        assert 'std::shared_ptr<const KODI::SUBTITLES::STYLE::style> ' + member in text
    prelude = fixture['PRELUDE']
    prelude = prelude.replace('#include <algorithm>', '#include <algorithm>\n#include <functional>\n#include <cmath>')
    prelude = prelude.replace('TOP_INSIDE};', 'TOP_INSIDE,TOP_OUTSIDE};')
    begin = prelude.index('struct CRect ')
    end = prelude.index('struct ass_image', begin)
    prelude = prelude[:begin] + SERVICES + prelude[end:]
    prelude = prelude.replace('int GetPlayResY(){return 720;}', 'int playResReads=0;int GetPlayResY(){++playResReads;return 720;}\n  std::shared_ptr<const SUBTITLES::STYLE::style> consumed;')
    prelude = prelude.replace('std::shared_ptr<const SUBTITLES::STYLE::style>){', 'std::shared_ptr<const SUBTITLES::STYLE::style> style){\n    consumed=std::move(style);')
    prelude = prelude.replace('enum{POSRESINFO_SAVE_CHANGES=-2};', 'enum{POSRESINFO_SAVE_CHANGES=-2,POSRESINFO_UNSET=-1};\n  bool m_saveSubtitlePosition=false;')
    prelude = prelude.replace('void ResetSubtitlePosition(){m_subtitlePosResInfo=1000;}', 'virtual void ResetSubtitlePosition();')
    prelude = prelude.replace('void LoadSettings(){++styleLoads;}', 'void LoadSettings();')
    prelude = prelude.replace('void CreateSubtitlesStyle(){m_overlayStyle=std::make_shared<const SUBTITLES::STYLE::style>();}', DECLARATIONS)
    begin = header.index('using TextureCache =')
    prelude = (prelude.replace('@CACHE@', header[begin:header.index(';', begin) + 1])
               .replace('@ELEMENT@', function(header, 'struct SElement') + ';')
               .replace('@LIBASS_OVERLAY@', function((root / 'DVDCodecs/Overlay/DVDOverlayLibass.h').read_text(), 'class CDVDOverlayLibass') + ';'))
    code = prelude + DEBUG
    for signature in ['void CRenderer::CreateSubtitlesStyle(', 'void CRenderer::LoadSettings(',
                      'void CRenderer::ResetSubtitlePosition(', 'void CRenderer::SetSubtitleVerticalPosition(',
                      'void CRenderer::SetVideoRect(', 'void CRenderer::OnViewChange(',
                      'void CRenderer::SetActiveAreaOffsets(', 'void CRenderer::SetStereoMode(',
                      'void CRenderer::Notify(', 'void CRenderer::ReleaseUnused(',
                      'std::shared_ptr<COverlay> CRenderer::ConvertLibass(',
                      'std::shared_ptr<COverlay> CRenderer::Convert(']:
        code += '\n' + function(renderer, signature)
    for name in ['CreateSubtitlesStyle(', 'ResetSubtitlePosition(', 'Render(']:
        code += '\n' + function(debug, 'void CDebugRenderer::CRenderer::' + name)
    code += '\n' + function((root / 'DVDSubtitles/DVDSubtitlesLibass.cpp').read_text(), 'CLibassRenderResult::CLibassRenderResult(')
    code += '\nCLibassRenderResult::~CLibassRenderResult() = default;\n'
    code += function(renderer, 'std::shared_ptr<COverlay> COverlay::Create(const CLibassRenderResult&') + TESTS
    with tempfile.TemporaryDirectory(prefix='subtitle-style-inputs-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Subtitle style inputs: PASS (production builders/conversion/options/debug; stub services/GPU/libass; ASan/UBSan)')


SERVICES = r'''
#include "utils/Geometry.h"
namespace KODI::SUBTITLES {
constexpr const char* FONT_DEFAULT_FAMILYNAME="DEFAULT";
enum class FontStyle{NORMAL,BOLD,ITALIC,BOLD_ITALIC};
enum class BackgroundType{NONE,SHADOW,BOX,SQUAREBOX};
enum class OverrideStyles{DISABLED,POSITIONS,STYLES,STYLES_POSITIONS};
}
struct Settings {
  std::function<void()> getter;
  std::string name="Example";int size=28;float margin=5;
  SUBTITLES::FontStyle font=SUBTITLES::FontStyle::NORMAL;
  SUBTITLES::BackgroundType background=SUBTITLES::BackgroundType::NONE;
  SUBTITLES::OverrideStyles overrides=SUBTITLES::OverrideStyles::DISABLED;
  SUBTITLES::Align align=SUBTITLES::Align::BOTTOM_OUTSIDE;
  SUBTITLES::HorizontalAlign horizontal=SUBTITLES::HorizontalAlign::CENTER;
  bool overrideAss=false,overrideFont=false;
  std::string GetFontName(){if(getter)getter();return name;}
  int GetFontSize(){return size;} auto GetFontStyle(){return font;}
  int GetFontColor(){return 0xff123456;}int GetBorderSize(){return 12;}
  int GetBorderColor(){return 0xff234567;}int GetFontOpacity(){return 90;}
  auto GetBackgroundType(){return background;}int GetBackgroundColor(){return 0xff345678;}
  int GetBackgroundOpacity(){return 40;}int GetShadowColor(){return 0xff456789;}
  int GetShadowOpacity(){return 50;}int GetShadowSize(){return 6;}
  auto GetAlignment(){return align;}auto GetHorizontalAlignment(){return horizontal;}
  bool IsOverrideAss(){return overrideAss;}bool IsOverrideFonts(){return overrideFont;}
  auto GetOverrideStyles(){return overrides;}float GetVerticalMarginPerc(){return margin;}
  int GetBlurSize(){if(getter)getter();return 7;}
};
struct SettingsComponent{Settings settings;Settings* GetSubtitlesSettings(){return &settings;}};
struct RESOLUTION_INFO {int iSubtitles=1000,iHeight=1080;float fPixelRatio=1;struct{int top=0;}Overscan;};
struct Gfx {RESOLUTION_INFO info;int reads=0,writes=0;std::function<void()> onRead;
  RESOLUTION_INFO GetResInfo(){++reads;if(onRead)onRead();return info;}
  int GetVideoResolution(){return 3;}
  void SetResInfo(int resolution,const RESOLUTION_INFO& r){assert(resolution==3);++writes;info=r;}};
struct Window{bool active=false;bool IsMenuCompositeActive(){return active;}Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct CApplicationPlayer {int calls=0;std::function<void(int)> callback;
  void SetSubtitleVerticalPosition(int pos,bool save){assert(!save);++calls;if(callback)callback(pos);}};
struct Components {CApplicationPlayer player;template<class T>T* GetComponent(){return &player;}};
struct CServiceBroker {
  static Window* GetWinSystem(){static Window w;return &w;}
  static SettingsComponent* GetSettingsComponent(){static SettingsComponent s;return &s;}
  static Components& GetAppComponents(){static Components c;return c;}
};
struct Observable{};
enum ObservableMessage{ObservableMessageSettingsChanged,ObservableMessagePositionChanged};
'''
DECLARATIONS = r'''
  void CreateSubtitlesStyle();void Notify(const Observable&,ObservableMessage);
  void SetVideoRect(CRect&,CRect&,CRect&);void OnViewChange();void SetStereoMode(const std::string&);
  void SetActiveAreaOffsets(int,int,bool);void SetSubtitleVerticalPosition(int,bool);
  int draws=0;void Render(const std::shared_ptr<COverlay>&){++draws;}
'''
DEBUG = r'''
class CDebugRenderer {public:class CRenderer:public OVERLAY::CRenderer {public:
  std::shared_ptr<const SUBTITLES::STYLE::style> m_debugOverlayStyle;
  void CreateSubtitlesStyle();void ResetSubtitlePosition() override;void Render(int,float);
};};
class TextOverlay:public CDVDOverlayLibass {public:
  explicit TextOverlay(const std::shared_ptr<CDVDSubtitlesLibass>& h):CDVDOverlayLibass(h,DVDOVERLAY_TYPE_TEXT){}
  void SetTextAlignEnabled(bool value)override{m_enableTextAlign=value;}
};
'''
TESTS = r'''
int main(){
  namespace S=SUBTITLES::STYLE;
  auto& settings=CServiceBroker::GetSettingsComponent()->settings;
  auto& gfx=CServiceBroker::GetWinSystem()->gfx;
  auto& player=CServiceBroker::GetAppComponents().player;
  CRenderer r;r.m_rs=r.m_rd=r.m_rv=CRect(0,0,1920,1080);
  player.callback=[&](int pos){r.SetSubtitleVerticalPosition(pos,false);};
  auto h=std::make_shared<CDVDSubtitlesLibass>();h->changes=0;
  auto o=std::make_shared<TextOverlay>(h);
  auto first=r.Convert(*o,123);auto retained=r.m_overlayStyle;
  assert(first&&h->sawStyle&&h->lastPts==123&&retained->fontSize==28);
  assert(retained->fontName=="Example"&&retained->marginVertical==54&&retained->blur==7);
  assert(retained->fontColor==0xff123456&&retained->fontBorderSize==12&&retained->fontBorderColor==0xff234567);
  assert(retained->fontOpacity==90&&retained->backgroundColor==0xff345678&&retained->backgroundOpacity==40);
  assert(retained->shadowColor==0xff456789&&retained->shadowOpacity==50&&retained->shadowSize==6);
  assert(r.Convert(*o,124)==first&&!h->sawStyle&&r.m_overlayStyle==retained);
  // Getter observations catch publishing an incomplete object or mutating the old one.
  settings.name="Changed";settings.size=44;settings.overrideAss=true;settings.overrideFont=true;
  int observations=0;settings.getter=[&]{++observations;assert(r.m_overlayStyle==retained&&retained->fontSize==28);};
  r.Notify({},ObservableMessageSettingsChanged);r.Convert(*o,125);
  settings.getter={};assert(observations==2&&h->sawStyle&&r.m_overlayStyle!=retained);
  assert(retained->fontName=="Example"&&r.m_overlayStyle->fontName=="Changed");
  // Exhaust the setting enums; production builder maps each without stale fields.
  for(int font=0;font<4;++font)for(int background=0;background<4;++background)
  for(int overrides=0;overrides<4;++overrides)for(bool top:{false,true}){
    settings.font=static_cast<SUBTITLES::FontStyle>(font);
    settings.background=static_cast<SUBTITLES::BackgroundType>(background);
    settings.overrides=static_cast<SUBTITLES::OverrideStyles>(overrides);
    settings.align=top?SUBTITLES::Align::TOP_OUTSIDE:SUBTITLES::Align::BOTTOM_OUTSIDE;
    r.CreateSubtitlesStyle();auto s=r.m_overlayStyle;
    assert(s->fontStyle==static_cast<S::FontStyle>(font)&&s->borderStyle==static_cast<S::BorderType>(background));
    assert(s->assOverrideStyles==static_cast<S::OverrideStyles>(overrides)&&s->assOverrideFont);
    assert(s->alignment==(top?S::FontAlign::TOP_CENTER:S::FontAlign::SUB_CENTER));
  }
  settings.overrideAss=false;r.CreateSubtitlesStyle();
  assert(!r.m_overlayStyle->assOverrideFont&&r.m_overlayStyle->assOverrideStyles==S::OverrideStyles::DISABLED);
  settings.align=SUBTITLES::Align::MANUAL;r.Notify({},ObservableMessageSettingsChanged);r.Convert(*o,1);
  auto current=r.m_overlayStyle;assert(h->sawStyle&&r.m_subtitlePosition==946);
  r.SetSubtitleVerticalPosition(800,true);const int writes=gfx.writes;r.Convert(*o,2);
  assert(gfx.writes==writes+1&&gfx.info.iSubtitles==854&&r.m_subtitlePosResInfo==854&&!h->sawStyle);
  const int calls=player.calls;gfx.info.iSubtitles=900;r.Notify({},ObservableMessagePositionChanged);r.Convert(*o,3);
  assert(player.calls==calls+1&&r.m_subtitlePosition==846&&r.m_overlayStyle==current&&!h->sawStyle);
  CRect source(0,0,1280,720),dest(0,0,1800,1000),view=r.m_rv;
  r.SetVideoRect(source,dest,view);r.Convert(*o,4);assert(r.m_overlayStyle==current&&!h->sawStyle);
  assert(h->opts.sourceWidth==1280&&h->opts.videoHeight==1000);
  view=CRect(0,0,1600,900);r.SetVideoRect(source,dest,view);r.Convert(*o,5);
  assert(r.m_overlayStyle!=current&&h->sawStyle&&h->opts.frameWidth==1600);current=r.m_overlayStyle;
  r.SetActiveAreaOffsets(80,90,true);r.Convert(*o,6);assert(h->sawStyle&&r.m_overlayStyle!=current);current=r.m_overlayStyle;
  r.SetActiveAreaOffsets(80,90,true);r.Convert(*o,7);assert(!h->sawStyle&&r.m_overlayStyle==current);
  // Exercise complete option preparation across alignment/forced/stereo/L5/horizontal paths.
  for(auto align:{SUBTITLES::Align::MANUAL,SUBTITLES::Align::BOTTOM_OUTSIDE,SUBTITLES::Align::BOTTOM_INSIDE,
                 SUBTITLES::Align::TOP_INSIDE,SUBTITLES::Align::TOP_OUTSIDE})
  for(bool forced:{false,true})for(bool halfOu:{false,true})for(bool l5:{false,true})
  for(bool textAlign:{false,true})for(auto horizontal:{SUBTITLES::HorizontalAlign::LEFT,SUBTITLES::HorizontalAlign::CENTER,SUBTITLES::HorizontalAlign::RIGHT}){
    r.m_subtitleAlign=align;r.m_subtitleHorizontalAlign=horizontal;
    r.m_rs=CRect(0,0,1920,1080);r.m_rd=CRect(0,0,1800,900);r.m_rv=CRect(0,0,1920,1080);
    r.m_subtitlePosResInfo=gfx.info.iSubtitles;r.m_subtitlePosition=800;r.m_subtitleVerticalMargin=54;
    gfx.info.fPixelRatio=1.25f;gfx.info.Overscan.top=20;
    r.SetStereoMode(halfOu?"top_bottom":"");r.SetActiveAreaOffsets(l5?80:0,l5?90:0,l5);
    o->SetForcedMargins(forced);o->SetTextAlignEnabled(textAlign);
    int reads=h->playResReads;r.ConvertLibass(*o,9,false,current);auto opts=h->opts;
    assert(opts.m_par==1.25f&&opts.frameHeight==1080&&opts.sourceHeight==1080&&opts.videoHeight==900);
    const bool manual=!forced&&align==SUBTITLES::Align::MANUAL;
    assert(h->playResReads==reads+(manual?1:0));
    auto mode=forced?S::MarginsMode::DISABLED:
      (align==SUBTITLES::Align::TOP_INSIDE||align==SUBTITLES::Align::BOTTOM_INSIDE)?S::MarginsMode::INSIDE_VIDEO:S::MarginsMode::DEFAULT;
    double expected=0;
    if(manual){double height=halfOu?2160:1080;expected=100-780/(height-current->marginVertical/720.0*height)*100;}
    else if(!forced&&align==SUBTITLES::Align::BOTTOM_OUTSIDE)expected=100-834/1080.0*100;
    if(l5){mode=S::MarginsMode::INSIDE_ACTIVE_AREA;expected=0;}
    assert(opts.marginsMode==mode&&std::fabs(opts.position-expected)<1e-10);
    assert(opts.activeAreaTopMargin==(l5?80:0)&&opts.activeAreaBottomMargin==(l5?90:0)&&opts.activeAreaApplyUserPos==l5);
    assert(opts.horizontalAlignment==(textAlign?static_cast<S::HorizontalAlign>(static_cast<int>(horizontal)+1):S::HorizontalAlign::DISABLED));
  }
  // Calibration/service callback may replace the member after request entry.
  // The request must retain the earlier immutable version through margin math/raster.
  r.m_overlayStyle=current;r.m_subtitleAlign=SUBTITLES::Align::MANUAL;r.SetActiveAreaOffsets(0,0,false);
  o->SetForcedMargins(false);r.SetStereoMode("");
  auto changed=S::style{};changed.marginVertical=200;auto replacement=std::make_shared<const S::style>(changed);
  gfx.onRead=[&]{r.m_overlayStyle=replacement;};
  r.ConvertLibass(*o,10,false,r.m_overlayStyle);gfx.onRead={};
  assert(h->consumed==current&&r.m_overlayStyle==replacement);
  assert(std::fabs(h->opts.position-(100-780/(1080-current->marginVertical/720.0*1080)*100))<1e-10);
  // Debug remains a separate style/consumer and never resets the player's position.
  CDebugRenderer::CRenderer debug;debug.m_rs=debug.m_rd=debug.m_rv=CRect(0,0,1920,1080);
  debug.m_subtitlePosResInfo=-1;debug.m_subtitlePosition=0;
  debug.m_buffers[0].push_back({11,o});int before=player.calls;
  debug.Render(0,0);auto heldDebug=debug.m_debugOverlayStyle;
  assert(player.calls==before&&h->consumed==heldDebug&&h->sawStyle&&debug.draws==1);
  assert(heldDebug->fontName=="DEFAULT"&&heldDebug->fontSize==20&&heldDebug->marginVertical==12);
  debug.Render(0,0);assert(!h->sawStyle&&debug.m_debugOverlayStyle==heldDebug&&player.calls==before);
  debug.CreateSubtitlesStyle();assert(debug.m_debugOverlayStyle!=heldDebug&&heldDebug->fontSize==20);
  player.callback={};
}
'''

if __name__ == '__main__':
    main()
