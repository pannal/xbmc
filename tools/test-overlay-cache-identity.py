#!/usr/bin/env python3
"""Production cache/conversion checks with fake GPU uploads and libass rasterizer.

Exercises the complete Convert and ConvertLibass methods, cache retirement,
and actual SPU highlight replacement with real producer overlay classes.
Exercises owned ASS output and per-consumer result identity with a fake rasterizer.
Does not claim frozen bitmap producers, real rasterization or GPU validation. Run with Python 3 and g++; temporary outputs are removed.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    root = ROOT / 'xbmc/cores/VideoPlayer'
    renderer = (root / 'VideoRenderers/OverlayRenderer.cpp').read_text()
    header = (root / 'VideoRenderers/OverlayRenderer.h').read_text()
    libass_header = (root / 'DVDCodecs/Overlay/DVDOverlayLibass.h').read_text()
    container = (root / 'DVDOverlayContainer.cpp').read_text()
    # No renderer-generated identity is written to, or consulted on, a producer.
    for name in ['VideoRenderers/OverlayRenderer.cpp', 'VideoRenderers/OverlayRenderer.h',
                 'DVDCodecs/Overlay/DVDOverlay.h', 'DVDCodecs/Overlay/DVDOverlayImage.h',
                 'DVDOverlayContainer.cpp']:
        assert 'm_textureid' not in (root / name).read_text()
    begin = header.index('using TextureCache =')
    cache = header[begin:header.index(';', begin) + 1]
    source = (PRELUDE.replace('@CACHE@', cache)
              .replace('@ELEMENT@', function(header, 'struct SElement') + ';')
              .replace('@LIBASS_OVERLAY@', function(libass_header, 'class CDVDOverlayLibass') + ';'))
    for signature in ['void CRenderer::SetOverlays(', 'void CRenderer::Release(std::vector',
                      'void CRenderer::Release(int', 'void CRenderer::ReleaseCache()',
                      'void CRenderer::ReleaseUnused(', 'void CRenderer::Flush()',
                      'void CRenderer::Reset()', 'std::shared_ptr<COverlay> CRenderer::ConvertLibass(',
                      'std::shared_ptr<COverlay> CRenderer::Convert(']:
        source += '\n' + function(renderer, signature)
    libass = (root / 'DVDSubtitles/DVDSubtitlesLibass.cpp').read_text()
    source += '\n' + function(libass, 'CLibassRenderResult::CLibassRenderResult(')
    source += '\nCLibassRenderResult::~CLibassRenderResult() = default;\n'
    source += function(renderer, 'std::shared_ptr<COverlay> COverlay::Create(const CLibassRenderResult&')
    source += '\n' + function(container, 'void CDVDOverlayContainer::UpdateOverlayInfo(')
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='overlay-cache-test-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                        '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Overlay cache identity: PASS (production conversion/cache/COW; ASan/UBSan; fake GPU/libass)')


PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstring>
#include <map>
#include <limits>
#include <stdexcept>
#include "cores/VideoPlayer/DVDSubtitles/DVDSubtitlesLibassRenderResult.h"
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlaySpu.h"
#include "cores/VideoPlayer/DVDSubtitles/SubtitlesStyle.h"
using CCriticalSection=std::recursive_mutex;
constexpr int NUM_BUFFERS=5;
const auto ownerThread=std::this_thread::get_id();
namespace KODI::SUBTITLES {
enum class Align{MANUAL,BOTTOM_OUTSIDE,BOTTOM_INSIDE,TOP_INSIDE};
enum class HorizontalAlign{LEFT,CENTER,RIGHT};
}
using namespace KODI;
struct CRect {float w=1920,h=1080;float Width() const{return w;}float Height() const{return h;}};
struct RESOLUTION_INFO {int iSubtitles=1000;float fPixelRatio=1;struct{int top=0;}Overscan;};
struct Gfx {RESOLUTION_INFO info;RESOLUTION_INFO GetResInfo(){return info;}int GetVideoResolution(){return 0;}
  void SetResInfo(int,const RESOLUTION_INFO& r){info=r;}};
struct Window {bool active=false;Gfx gfx;bool IsMenuCompositeActive(){return active;}Gfx& GetGfxContext(){return gfx;}};
struct CServiceBroker {static Window* GetWinSystem(){static Window w;return &w;}};
struct ass_image {int w=1,h=1,stride=1;unsigned char* bitmap=nullptr;
  uint32_t color=0;int dst_x=0,dst_y=0;ASS_Image* next=nullptr;int type=0;};
struct CDVDSubtitlesLibass {
  struct {unsigned char value=0;} image;bool visible=true;int changes=2,calls=0;bool sawStyle=false;
  std::shared_ptr<const CLibassRenderResult> result;
  SUBTITLES::STYLE::renderOpts opts{};double lastPts=0;
  int GetPlayResY(){return 720;}
  std::shared_ptr<const CLibassRenderResult> RenderImage(double pts,SUBTITLES::STYLE::renderOpts o,bool update,
                        const std::shared_ptr<SUBTITLES::STYLE::style>&){
    assert(std::this_thread::get_id()==ownerThread);++calls;lastPts=pts;opts=o;sawStyle=update;
    if(!visible){result.reset();return nullptr;}
    if(update||changes||!result){ASS_Image node;node.bitmap=&image.value;result=std::make_shared<const CLibassRenderResult>(&node);}
    return result;
  }
};
@LIBASS_OVERLAY@
struct CDVDDemuxSPU{};
struct CDVDInputStreamNavigator {
  int value=0;bool changed=true;void CheckButtons(){}
  bool GetCurrentButtonInfo(CDVDOverlaySpu& o,CDVDDemuxSPU*,int){
    if(changed){o.highlight_color[0][0]=value;o.crop_i_x_start=value;}return changed;
  }
};
struct CDVDOverlayContainer:CCriticalSection{
  VecOverlays m_overlays;
  void UpdateOverlayInfo(const std::shared_ptr<CDVDInputStreamNavigator>&,CDVDDemuxSPU*,int);
};
namespace OVERLAY {
struct COverlay {
  std::weak_ptr<const CLibassRenderResult> m_libassResult;
  static int created,destroyed;uint32_t value=0;bool m_rawPqMenu=false,m_discMenuOverlay=false;
  ~COverlay(){assert(std::this_thread::get_id()==ownerThread);++destroyed;}
  static std::shared_ptr<COverlay> New(uint32_t value){
    assert(std::this_thread::get_id()==ownerThread);++created;auto p=std::make_shared<COverlay>();p->value=value;return p;
  }
  static std::shared_ptr<COverlay> Create(const CDVDOverlayImage& o,CRect&){
    uint32_t value=0;
    if(!o.palette.empty())value=o.palette[0];else if(o.pixels.size()>=4)memcpy(&value,o.pixels.data(),4);
    auto p=New(value);p->m_rawPqMenu=o.m_isPqMenuGraphics&&CServiceBroker::GetWinSystem()->active;return p;
  }
  static std::shared_ptr<COverlay> Create(const CDVDOverlaySpu& o){return New(o.highlight_color[0][0]);}
  static std::shared_ptr<COverlay> Create(ASS_Image* o,float,float){return New(o->bitmap[0]);}
  static std::shared_ptr<COverlay> Create(const CLibassRenderResult&,float,float);
};
int COverlay::created=0,COverlay::destroyed=0;
class CRenderer {
public:
  @ELEMENT@
  using OverlayBatch=std::vector<SElement>;
  @CACHE@
  TextureCache m_textureCache;CCriticalSection m_section;OverlayBatch m_buffers[NUM_BUFFERS];
  CRect m_rs,m_rd,m_rv;std::string m_stereomode;
  SUBTITLES::Align m_subtitleAlign=SUBTITLES::Align::BOTTOM_INSIDE;
  SUBTITLES::HorizontalAlign m_subtitleHorizontalAlign=SUBTITLES::HorizontalAlign::CENTER;
  int m_subtitlePosition=900,m_subtitlePosResInfo=1000,m_subtitleVerticalMargin=10;
  int m_activeAreaTopOffset=0,m_activeAreaBottomOffset=0;bool m_activeAreaApplyUserPos=false;
  enum{POSRESINFO_SAVE_CHANGES=-2};
  bool m_isSettingsChanged=false;int styleLoads=0;
  std::shared_ptr<SUBTITLES::STYLE::style> m_overlayStyle;
  void ResetSubtitlePosition(){m_subtitlePosResInfo=1000;}
  void LoadSettings(){++styleLoads;}
  void CreateSubtitlesStyle(){m_overlayStyle=std::make_shared<SUBTITLES::STYLE::style>();}
  void SetOverlays(OverlayBatch,int);void Release(int);void Release(std::vector<SElement>&);
  void ReleaseCache();void ReleaseUnused(const OverlayBatch& selected={});void Flush();void Reset();
  std::shared_ptr<COverlay> Convert(CDVDOverlay&,double);
  std::shared_ptr<COverlay> ConvertLibass(CDVDOverlayLibass&,double,bool,const std::shared_ptr<SUBTITLES::STYLE::style>&);
};
}
using namespace OVERLAY;
'''

TESTS = r'''
std::shared_ptr<CDVDOverlayImage> image(bool bdj,uint32_t color){
  auto p=std::make_shared<CDVDOverlayImage>();p->SetDiscMenuOverlay(true);p->m_isPqMenuGraphics=true;p->m_isHdrPq=true;
  p->m_menuVisible=(color>>24)!=0;p->width=p->height=1;
  if(bdj){p->pixels.resize(4);memcpy(p->pixels.data(),&color,4);}else{p->pixels={0};p->palette={color};p->pqMenuPalette={color};}
  return p;
}
int main(){
  for(bool bdj:{false,true}){
    CRenderer a,b;auto p=image(bdj,0xff102030);auto* bytes=p->pixels.data();auto* palette=p->palette.data();
    auto first=a.Convert(*p,1);int uploads=COverlay::created;
    for(int i=0;i<30;++i)assert(a.Convert(*p,i)==first);
    assert(COverlay::created==uploads&&p->pixels.data()==bytes&&p->palette.data()==palette);
    auto other=b.Convert(*p,1);assert(other!=first&&a.Convert(*p,2)==first);
    // Cache identity is per-renderer, and converting elsewhere cannot disturb it.
    b.Flush();assert(a.Convert(*p,3)==first);
    auto replacement=std::static_pointer_cast<CDVDOverlayImage>(p->Clone());
    if(bdj){uint32_t color=0xffabcdef;memcpy(replacement->pixels.data(),&color,4);}else replacement->palette[0]=0xffabcdef;
    auto second=a.Convert(*replacement,4);assert(second!=first&&second->value==0xffabcdef&&first->value==0xff102030);
    assert(a.Convert(*p,5)==first);
    // PQ raw-route conversion still invalidates only the appropriate texture.
    auto* window=CServiceBroker::GetWinSystem();window->active=true;auto raw=a.Convert(*p,6);
    assert(raw!=first&&raw->m_rawPqMenu&&a.Convert(*p,7)==raw);
    window->active=false;auto converted=a.Convert(*p,8);assert(converted!=raw&&!converted->m_rawPqMenu);
    auto transparent=image(bdj,0);assert(a.Convert(*transparent,9)->value==0);
    // Clear/hide releases slot membership, while the retained selection keeps
    // only its content reachable until main drops it. No producer copies needed.
    a.SetOverlays({{0,p}},0);CRenderer::OverlayBatch selected={{0,p}};a.SetOverlays({},0);
    a.ReleaseUnused(selected);assert(a.m_textureCache.size()==1&&a.m_textureCache.count(p));
    selected.clear();a.ReleaseUnused();assert(a.m_textureCache.empty());
  }
  // Actual container highlight callback replaces shared SPU content. Base Clone
  // aliases, so this test relies on the explicit copy constructor used by COW.
  {CRenderer r;CDVDOverlayContainer container;auto p=std::make_shared<CDVDOverlaySpu>();
   p->bForced=true;p->highlight_color[0][0]=10;p->result[0]=77;container.m_overlays={p};
   assert(p->Clone()==p);auto old=r.Convert(*p,0);auto nav=std::make_shared<CDVDInputStreamNavigator>();nav->value=20;
   container.UpdateOverlayInfo(nav,nullptr,0);auto next=std::static_pointer_cast<CDVDOverlaySpu>(container.m_overlays[0]);
   assert(next!=p&&next->highlight_color[0][0]==20&&next->result[0]==77&&p->highlight_color[0][0]==10);
   auto prepared=r.Convert(*next,1);assert(prepared!=old&&prepared->value==20&&old->value==10);
   nav->value=30;container.UpdateOverlayInfo(nav,nullptr,0);assert(next->highlight_color[0][0]==20&&prepared->value==20);
   nav->changed=false;container.UpdateOverlayInfo(nav,nullptr,0);assert(r.Convert(*container.m_overlays[0],2)->value==30);}
  // Run complete production libass conversion with controlled change/output
  // signals: timing, animation/style and empty output retain their policy.
  {CRenderer r;auto handler=std::make_shared<CDVDSubtitlesLibass>();
   auto p=std::make_shared<CDVDOverlayLibass>(handler,DVDOVERLAY_TYPE_SSA);handler->image.value=1;
   auto first=r.Convert(*p,100);assert(handler->lastPts==100&&handler->sawStyle&&r.styleLoads==1);
   handler->changes=0;auto same=r.Convert(*p,101);assert(same==first&&!handler->sawStyle);
   handler->changes=2;handler->image.value=2;auto animated=r.Convert(*p,102);assert(animated!=first&&animated->value==2);
   handler->changes=0;r.m_isSettingsChanged=true;handler->image.value=3;
   auto styled=r.Convert(*p,103);assert(styled!=animated&&handler->sawStyle&&r.styleLoads==2);
   r.m_activeAreaTopOffset=100;r.m_activeAreaBottomOffset=120;r.m_activeAreaApplyUserPos=true;
   r.Convert(*p,104);assert(handler->opts.marginsMode==SUBTITLES::STYLE::MarginsMode::INSIDE_ACTIVE_AREA);
   assert(handler->opts.activeAreaTopMargin==100&&handler->opts.activeAreaBottomMargin==120&&handler->opts.activeAreaApplyUserPos);
   handler->visible=false;assert(!r.Convert(*p,105));handler->visible=true;handler->changes=2;
   assert(r.Convert(*p,106));r.Flush();handler->changes=0;
   auto afterFlush=r.Convert(*p,107);assert(afterFlush&&afterFlush!=styled);
   // DebugRenderer calls ConvertLibass directly with its own style and cache.
   CRenderer debug;auto debugStyle=std::make_shared<SUBTITLES::STYLE::style>();
   auto debugImage=debug.ConvertLibass(*p,108,true,debugStyle);handler->changes=0;
   assert(debug.ConvertLibass(*p,109,false,debugStyle)==debugImage);
   // Another consumer changed the handler output. Its subsequent changes=0
   // must not validate the old texture in r.
   auto refreshed=r.Convert(*p,110);assert(refreshed!=afterFlush&&r.Convert(*p,111)==refreshed);
   handler->image.value=7;handler->changes=2;auto changed=debug.ConvertLibass(*p,112,false,debugStyle);
   handler->changes=0;auto caughtUp=r.Convert(*p,113);
   assert(caughtUp!=refreshed&&caughtUp->value==7&&changed->value==7&&refreshed->value==3);
   assert(r.Convert(*p,114)==caughtUp);
   std::weak_ptr<const CLibassRenderResult> weak=handler->result;handler->result.reset();
   assert(weak.expired());auto rebuilt=r.Convert(*p,115);assert(rebuilt!=caughtUp);
   // Prepared owned bytes do not follow later producer mutation.
   auto held=handler->result;handler->image.value=8;
   assert(COverlay::Create(*held,1920,1080)->value==7);}
  // Strong cache keys prevent address recycling until main cache retirement.
  {CRenderer r;auto p=image(false,0xff123456);std::weak_ptr<CDVDOverlay> weak=p;r.Convert(*p,0);p.reset();
   assert(!weak.expired());int before=COverlay::destroyed;r.ReleaseUnused();
   assert(weak.expired()&&COverlay::destroyed==before+1);}
  // Scope boundary: uploaded output is independent, but producer payloads have
  // NOT been frozen. After flush, the altered producer data is still consumed.
  {CRenderer r;auto p=image(false,0xff123456);auto prepared=r.Convert(*p,0);p->palette[0]=0xffaabbcc;
   assert(prepared->value==0xff123456&&r.Convert(*p,1)==prepared);
   r.Flush();assert(r.Convert(*p,2)->value==0xffaabbcc);}
}
'''

if __name__ == '__main__':
    main()
