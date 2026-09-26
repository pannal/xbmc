#!/usr/bin/env python3
"""Exercise production geometry capture/calculation and synchronous submission.

Compiles actual geometry records/methods and ordinary/PQ pass loops. Settings,
window services, conversion and GPU submission are stubs. Checks value ownership,
conditional sampling, placement, skipped draws and return/unwind resource release;
this is not application-attempt, context-validity or GPU-completion verification.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    path = ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers'
    header = (path / 'OverlayRenderer.h').read_text()
    renderer = (path / 'OverlayRenderer.cpp').read_text()
    overlay = function(header, 'class COverlay\n')
    # Use production enums and placement fields, without platform factories.
    fields = overlay[overlay.index('    enum EType'):overlay.index('  protected:')]
    source = (PRELUDE.replace('@STATE@', function(header, 'struct SRenderState') + ';')
              .replace('@FIELDS@', fields)
              .replace('@GEOMETRY@', function(header, 'struct SRenderGeometry') + ';')
              .replace('@ELEMENT@', function(header, 'struct SElement') + ';'))
    for signature in ['bool IsPqMenuImage(',
                      'CRenderer::SRenderGeometry CRenderer::PrepareRenderGeometry(',
                      'SRenderState CRenderer::CalculateRenderState(',
                      'void CRenderer::Render(std::shared_ptr<COverlay>',
                      'void CRenderer::Render(const OverlayBatch&',
                      'void CRenderer::RenderPqMenu(']:
        source += '\n' + function(renderer, signature)
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='overlay-geometry-test-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Overlay geometry: PASS (production capture/calculation/pass/submission; ASan/UBSan; GPU/services stubbed)')


PRELUDE = r'''
#include <algorithm>
#include <cassert>
#include <cmath>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>
#include "utils/Geometry.h"
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
using CCriticalSection = std::recursive_mutex;
const auto ownerThread = std::this_thread::get_id();
struct CLibassRenderResult;
@STATE@
struct Services {
  int calibrationReads=0,depthReads=0,zoomReads=0,zoom=100,depth=0;
  int lastDepth=-1;bool lastPgs=true;
  bool failZoom=false;
  struct Info {int iSubtitles=1000;struct {int top=20;} Overscan;} info;
  Info GetResInfo(){++calibrationReads;return info;}
  Services& GetGfxContext(){return *this;}
  Services* GetSettings(){return this;}
  int GetInt(int){++zoomReads;if(failZoom)throw std::runtime_error("settings");return zoom;}
  bool active=false,pending=false;
  bool IsMenuCompositeActive(){return active;}
  bool IsMenuCompositePending(){return pending;}
} services;
using RESOLUTION_INFO=Services::Info;
using CWinSystemBase=Services;
struct CServiceBroker {
  static Services* GetWinSystem(){return &services;}
  static Services* GetSettingsComponent(){return &services;}
};
struct CSettings {static constexpr int SETTING_SUBTITLES_BITMAPZOOM=1;};
int GetStereoscopicDepth(bool pgs,int depth){
  ++services.depthReads;services.lastDepth=depth;services.lastPgs=pgs;return services.depth;
}
struct COverlay {
@FIELDS@
  int* destroyed=nullptr;
  std::function<void(SRenderState&)> draw;
  virtual ~COverlay(){assert(std::this_thread::get_id()==ownerThread);if(destroyed)++*destroyed;}
  virtual void Render(SRenderState& state){
    assert(std::this_thread::get_id()==ownerThread);if(draw)draw(state);
  }
};
struct CRenderer {
@GEOMETRY@
@ELEMENT@
  using OverlayBatch=std::vector<SElement>;
  CCriticalSection m_section;
  CRect m_rs{0,0,1920,1080},m_rd{0,0,1920,1080},m_rv{0,0,1920,1080};
  int m_activeAreaTopOffset=0,m_activeAreaBottomOffset=0;
  std::shared_ptr<COverlay> converted;
  int conversions=0,releases=0;
  std::shared_ptr<COverlay> Convert(const CDVDOverlay&,double){++conversions;return converted;}
  void ReleaseUnused(const OverlayBatch&){++releases;}
  SRenderGeometry PrepareRenderGeometry(const COverlay&) const;
  static SRenderState CalculateRenderState(const SRenderGeometry&);
  void Render(std::shared_ptr<COverlay> overlay);
  void Render(const OverlayBatch&);
  void RenderPqMenu(const OverlayBatch&);
};
void expect(SRenderState s,float x,float y,float w,float h){
  assert(std::abs(s.x-x)<0.002f);assert(std::abs(s.y-y)<0.002f);
  assert(std::abs(s.width-w)<0.002f);assert(std::abs(s.height-h)<0.002f);
}
'''

TESTS = r'''
int main(){
  CRenderer r;
  auto o=std::make_shared<COverlay>();
  o->m_pos=COverlay::POSITION_RELATIVE;o->m_align=COverlay::ALIGN_VIDEO;
  o->m_x=0.5f;o->m_y=0.9f;o->m_width=0.25f;o->m_height=0.1f;
  o->m_isBitmapOverlay=true;
  r.m_rd={100,200,1060,740};r.m_rv={10,20,1930,1100};
  services.zoom=150;services.depth=-4;
  const auto retained=r.PrepareRenderGeometry(*o);
  expect(r.CalculateRenderState(retained),576,672.5f,360,81);
  assert(services.calibrationReads==0&&services.depthReads==1&&services.zoomReads==1);
  // Effective depth still comes from the converted resource's existing defaults.
  assert(!services.lastPgs&&services.lastDepth==0);
  // No producer, renderer, settings or service storage is borrowed by geometry.
  o->m_x=900;o->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;
  o->m_align=COverlay::ALIGN_SCREEN;o->m_discMenuOverlay=true;
  r.m_rs={0,0,10,10};r.m_rd={0,0,1,1};r.m_rv={0,0,2,2};
  r.m_activeAreaTopOffset=1000;r.m_activeAreaBottomOffset=1000;
  services.zoom=20;services.depth=999;services.info.iSubtitles=99;
  expect(r.CalculateRenderState(retained),576,672.5f,360,81);
  assert(services.calibrationReads==0&&services.depthReads==1&&services.zoomReads==1);

  auto g=retained;
  g.position=COverlay::POSITION_ABSOLUTE;
  g.state={200,800,400,100};g.bitmapZoom=0.5f;
  expect(r.CalculateRenderState(g),246,625,100,25);
  g.position=COverlay::POSITION_ABSOLUTE_SCREEN;
  expect(r.CalculateRenderState(g),296,850,200,50);
  g.bitmapZoom=1;g.stereoDepth=0;g.bitmap=false;
  g.position=COverlay::POSITION_RELATIVE;g.alignment=COverlay::ALIGN_SCREEN;
  g.state={0.5f,0.5f,0.25f,0.1f};
  expect(r.CalculateRenderState(g),970,560,480,108);
  g.alignment=COverlay::ALIGN_SCREEN_AR;g.sourceWidth=1280;g.sourceHeight=720;
  g.state={0.5f,0.5f,400,100};
  expect(r.CalculateRenderState(g),970,560,600,150);
  g.sourceWidth=0;g.sourceHeight=0;
  expect(r.CalculateRenderState(g),970,560,400,100);

  services={};r.m_rs={0,0,1920,1080};r.m_rd=r.m_rs;r.m_rv={10,20,1930,1100};
  r.m_activeAreaTopOffset=0;r.m_activeAreaBottomOffset=0;
  o->m_pos=COverlay::POSITION_ABSOLUTE;o->m_align=COverlay::ALIGN_SUBTITLE;
  o->m_x=-20;o->m_y=-10;o->m_width=400;o->m_height=50;
  o->m_discMenuOverlay=false;o->m_isBitmapOverlay=false;
  const auto calibrated=r.PrepareRenderGeometry(*o);
  expect(r.CalculateRenderState(calibrated),950,990,400,50);
  services.info.iSubtitles=700;
  expect(r.CalculateRenderState(calibrated),950,990,400,50);
  expect(r.CalculateRenderState(r.PrepareRenderGeometry(*o)),950,690,400,50);
  assert(services.calibrationReads==2&&services.zoomReads==0);
  o->m_pos=COverlay::POSITION_RELATIVE;o->m_x=0;o->m_y=0;
  o->m_width=0.5f;o->m_height=0.1f;
  expect(r.CalculateRenderState(r.PrepareRenderGeometry(*o)),970,700,960,108);
  o->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;
  r.PrepareRenderGeometry(*o);assert(services.calibrationReads==3);

  // Active-area placement: view-anchored vs video-anchored, top/bottom,
  // center vs top-left coordinates, and invalid/empty active area.
  g=retained;g.view={0,0,1920,1080};g.destination={0,0,1920,1080};
  g.position=COverlay::POSITION_ABSOLUTE;g.alignment=COverlay::ALIGN_VIDEO;
  g.state={100,980,400,80};g.bitmapZoom=1;g.stereoDepth=0;
  g.activeAreaTop=100;g.activeAreaBottom=100;
  expect(r.CalculateRenderState(g),100,883.7037f,400,80);
  g.state.y=10;expect(r.CalculateRenderState(g),100,108.14815f,400,80);
  g.position=COverlay::POSITION_RELATIVE;g.alignment=COverlay::ALIGN_SCREEN_AR;
  g.sourceWidth=1920;g.sourceHeight=1080;g.state={0.5f,0.95f,400,80};
  expect(r.CalculateRenderState(g),960,928.5926f,400,80);
  g.state.y=0.02f;expect(r.CalculateRenderState(g),960,140,400,80);
  g.activeAreaTop=600;g.activeAreaBottom=600;
  expect(r.CalculateRenderState(g),960,21.6f,400,80);
  g.discMenu=true;g.bitmapZoom=2;g.stereoDepth=50;
  expect(r.CalculateRenderState(g),960,21.6f,400,80);

  // Positive active-area offsets and overlay metadata are copied as well.
  o->m_pos=COverlay::POSITION_ABSOLUTE;o->m_align=COverlay::ALIGN_VIDEO;
  o->m_isBitmapOverlay=true;o->m_discMenuOverlay=false;
  o->m_x=100;o->m_y=980;o->m_width=400;o->m_height=80;
  r.m_rv=r.m_rs;r.m_activeAreaTopOffset=100;r.m_activeAreaBottomOffset=100;
  const auto active=r.PrepareRenderGeometry(*o);
  r.m_activeAreaTopOffset=0;r.m_activeAreaBottomOffset=0;
  expect(r.CalculateRenderState(active),100,883.7037f,400,80);

  o->m_source_width=1280;o->m_source_height=720;
  o->m_pgsSubtitle=true;o->m_3dSubtitleDepth=7;services.depth=9;
  r.m_activeAreaTopOffset=21;r.m_activeAreaBottomOffset=34;
  const auto metadata=r.PrepareRenderGeometry(*o);
  assert(services.lastPgs&&services.lastDepth==7);
  assert(metadata.sourceWidth==1280&&metadata.sourceHeight==720&&metadata.stereoDepth==9);
  assert(metadata.activeAreaTop==21&&metadata.activeAreaBottom==34);
  o->m_source_width=0;o->m_source_height=0;o->m_pgsSubtitle=false;o->m_3dSubtitleDepth=0;
  r.m_activeAreaTopOffset=0;r.m_activeAreaBottomOffset=0;services.depth=0;
  assert(metadata.sourceWidth==1280&&metadata.sourceHeight==720&&metadata.stereoDepth==9);
  assert(metadata.activeAreaTop==21&&metadata.activeAreaBottom==34);

  // Each synchronous call retains its conversion through backend submission,
  // even if the caller/cache drops its last reference from inside the backend.
  int destroyed=0,draws=0;
  std::shared_ptr<COverlay> resource=std::make_shared<COverlay>();
  resource->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;resource->m_align=COverlay::ALIGN_SCREEN;
  resource->destroyed=&destroyed;
  std::weak_ptr<COverlay> weak=resource;
  resource->draw=[&](SRenderState&){++draws;resource.reset();assert(!weak.expired());assert(destroyed==0);};
  r.Render(resource);assert(weak.expired()&&destroyed==1&&draws==1);
  resource=std::make_shared<COverlay>();resource->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;
  resource->m_align=COverlay::ALIGN_SCREEN;resource->destroyed=&destroyed;
  resource->draw=[&](SRenderState&){++draws;throw std::runtime_error("draw failed");};
  weak=resource;
  try{r.Render(std::move(resource));assert(false);}catch(const std::runtime_error&){}
  assert(weak.expired()&&destroyed==2&&draws==2);
  // Failure during preparation also releases the lexical owner without drawing.
  resource=std::make_shared<COverlay>();resource->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;
  resource->m_align=COverlay::ALIGN_SCREEN;resource->m_isBitmapOverlay=true;
  resource->destroyed=&destroyed;services.failZoom=true;weak=resource;
  try{r.Render(std::move(resource));assert(false);}catch(const std::runtime_error&){}
  assert(weak.expired()&&destroyed==3&&draws==2);services.failZoom=false;
  // A backend no-op has the same CPU lifetime, without claiming GPU success.
  resource=std::make_shared<COverlay>();resource->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;
  resource->m_align=COverlay::ALIGN_SCREEN;resource->destroyed=&destroyed;weak=resource;
  r.Render(std::move(resource));assert(weak.expired()&&destroyed==4);

  auto menu=std::make_shared<CDVDOverlayImage>();menu->m_isPqMenuGraphics=true;
  CRenderer::OverlayBatch batch{{1.0,menu}};
  services={};r.converted.reset();r.Render(batch);r.RenderPqMenu(batch);
  assert(r.conversions==2&&services.depthReads==0&&services.zoomReads==0);
  r.converted=std::make_shared<COverlay>();
  r.converted->m_pos=COverlay::POSITION_ABSOLUTE_SCREEN;r.converted->m_align=COverlay::ALIGN_SCREEN;
  r.converted->m_isBitmapOverlay=true;r.converted->m_discMenuOverlay=true;
  r.converted->draw=[&](SRenderState&){++draws;};
  for(bool pending : {false,true}){
    services.active=!pending;services.pending=pending;
    int before=r.conversions;r.Render(batch);assert(r.conversions==before);
    r.RenderPqMenu(batch);assert(r.conversions==before+1);
  }
  assert(draws==4&&services.depthReads==0&&services.zoomReads==0);
  services.active=false;services.pending=false;r.Render(batch);assert(draws==5);
}
'''

if __name__ == '__main__':
    main()
