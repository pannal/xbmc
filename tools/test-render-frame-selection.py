#!/usr/bin/env python3
"""Exercise synchronous frame selection with extracted production methods.

Uses real overlay image types and production queue/selection, reporting, menu
pass, capture and field-render methods. Platform/GPU/clock services are stubs;
geometry/style placement and lifecycle wiring are checked structurally. Includes immutable published bitmap inputs; this is not libass, full application
or device verification.
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
    rm = (path / 'RenderManager.cpp').read_text()
    rh = (path / 'RenderManager.h').read_text()
    ov = (path / 'OverlayRenderer.cpp').read_text()
    oh = (path / 'OverlayRenderer.h').read_text()
    render = function(rm, 'void CRenderManager::Render(bool')
    assert 'm_presentsource' not in render and 'm_Queue[' not in render
    # Preserve late geometry and main composition order. Full platform Render
    # is not emulated by this fixture; the menu block below executes verbatim.
    points = ['m_pRenderer->Update();', 'm_pRenderer->GetVideoRect(',
              'CalcOverlayActiveArea(', 'm_overlays.SetVideoRect(',
              'winSystem->RequestMenuComposite(', 'winSystem->BeginMenuOverlayRender()',
              'm_overlays.RenderPqMenu(frame->overlays)', 'winSystem->EndMenuOverlayRender()',
              'm_overlays.Render(frame->overlays)']
    positions = [render.index(p) for p in points]
    assert positions == sorted(positions)
    for signature in ['bool CRenderManager::Configure()', 'void CRenderManager::PreInit()',
                      'void CRenderManager::UnInit()', 'bool CRenderManager::Flush(']:
        method = function(rm, signature)
        assert method.index('lock2(m_presentlock)') < method.index('ClearFrameSelection();')
    assert 'ClearFrameSelection' not in function(rm, 'void CRenderManager::DiscardBuffer()')
    frame_move = function(rm, 'void CRenderManager::FrameMove()')
    assert frame_move.index('ClearFrameSelection();') < frame_move.index('UpdateResolution();')
    if 'aml_dv_engage_stale_deferred_disc();' in frame_move:
        assert frame_move.index('UpdateResolution();') < frame_move.index('aml_dv_engage_stale_deferred_disc();')
    # The only full PrepareNextRender change is the ownership guard; retain all
    # scheduler formulas and tests by extracting its complete production body.
    selection = function(rh, 'struct FrameSelection\n') + ';'
    element = function(oh, 'struct SElement') + ';'
    source = (PRELUDE.replace('@ELEMENT@', element).replace('@SELECTION@', selection)
              .replace('@PASS@', function(rh, 'struct VideoRenderPass') + ';')
              .replace('@DRAW@', function(rh, 'struct PreparedVideoDraw') + ';'))
    source += function(ov, 'bool IsPqMenuImage(')
    for name in ['SetOverlays', 'Release(int', 'Release(std::vector', 'ReleaseUnused',
                 'Render(const OverlayBatch&', 'RenderPqMenu', 'HasPqMenuOverlay',
                 'HasOverlay', 'HasDiscMenuOverlay', 'HasTextOverlay', 'HasImageOverlay']:
        prefix = 'bool' if name.startswith('Has') else 'void'
        source += '\n' + function(ov, prefix + ' CRenderer::' + name)
    source += '\n' + function(ov, 'CRenderer::OverlayBatch CRenderer::GetOverlays(')
    for name in ['SelectFrame', 'ClearFrameSelection', 'FrameMove', 'ProcessPresentationQueue',
                 'RetireBuffer', 'PrepareNextRender', 'DiscardBuffer', 'UpdateGuiPresentationState',
                 'RenderCapture', 'SubmitVideoDraw']:
        source += '\n' + function(rm, 'void CRenderManager::' + name + '(')
    source += '\n' + function(rm, 'CRenderManager::PreparedVideoDraw CRenderManager::PrepareVideoDraw(')
    for name in ['IsGuiLayer', 'IsPresenting']:
        source += '\n' + function(rm, 'bool CRenderManager::' + name + '(')
    start = render.index('    CWinSystemBase* winSystem =')
    end = render.index('    m_overlays.Render(frame->overlays);', start)
    menu_block = render[start:end + len('    m_overlays.Render(frame->overlays);')]
    source += '\nvoid CRenderManager::DrawMenus() {const auto frame=m_frameSelection;\n' + menu_block + '\n}\n'
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='frame-selection-test-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                        '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Frame selection: PASS (production methods, ASan/UBSan; platform/GPU stubs)')


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <vector>
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
using DWORD=unsigned int;
constexpr int NUM_BUFFERS=5,LOGERROR=1,LOGDEBUG=2,LOGAVTIMING=3,CAPTURESTATE_FAILED=1;
constexpr DWORD RENDER_FLAG_BOT=1,RENDER_FLAG_TOP=2,RENDER_FLAG_FIELD0=4,RENDER_FLAG_FIELD1=8,RENDER_FLAG_NOOSD=16;
constexpr double DVD_TIME_BASE=1000000;
double DVD_MSEC_TO_TIME(double v){return v*1000;}
// Optional merged disc-hold callback; policy is covered by test-dv-disc-hold.py.
void aml_dv_engage_stale_deferred_disc(){}
struct CLog {template<class... T> static void Log(T&&...){} template<class... T> static void LogFC(T&&...) {}};
namespace XbmcThreads {template<class = void> struct EndTime {bool past=false;void Set(std::chrono::milliseconds){past=false;}bool IsTimePast(){return past;}};}
struct Event {void notifyAll(){}};
struct Gfx {float GetFPS(){return 50.0f;}};
struct CWinSystemBase {
  Gfx gfx;bool active=false,pending=false;int begin=0,end=0;std::vector<bool> requests;
  Gfx& GetGfxContext(){return gfx;}
  bool IsMenuCompositeActive(){return active;}
  bool IsMenuCompositePending(){return pending;}
  void RequestMenuComposite(bool b){requests.push_back(b);}
  bool BeginMenuOverlayRender(){++begin;return active;}
  void EndMenuOverlayRender(){++end;}
};
struct CServiceBroker {static CWinSystemBase* GetWinSystem(){static CWinSystemBase w;return &w;}};
bool discVisible=false,subtitleSignal=false;int signalMode=0;
void aml_set_disc_menu_visible(bool b){discVisible=b;}
int aml_dv_l5_subs_signal_mode(){return signalMode;}
void aml_dv_set_subtitles(bool b){subtitleSignal=b;}
struct Clock {double pts=0;double GetClock(){return pts;}double GetClockSpeed(){return 1;}void SetVsyncAdjust(double){}};
struct Cache {void SetRenderPts(double){}};
struct Port {bool gui=false;void UpdateGuiRender(bool b){gui=b;}};
struct Player {int GetSubtitleCount(){return 1;}};
struct CRenderCapture {int state=0;void SetState(int s){state=s;}};
struct Renderer {
  bool gui=false,need=false,captureOk=true;int captureSource=-1;
  std::vector<int> released;struct Call{int source,past;bool clear;DWORD flags,alpha;};std::vector<Call> calls;
  bool IsGuiLayer(){return gui;}
  bool NeedBuffer(int){return need;}
  void ReleaseBuffer(int i){released.push_back(i);}
  bool RenderCapture(int i,CRenderCapture*){captureSource=i;return captureOk;}
  void RenderUpdate(int i,int p,bool c,DWORD f,DWORD a){calls.push_back({i,p,c,f,a});}
};
namespace OVERLAY {
struct COverlay {const CDVDOverlay* value=nullptr;};
class CRenderer {
public:
  @ELEMENT@
  using OverlayBatch=std::vector<SElement>;
  CCriticalSection m_section;OverlayBatch m_buffers[NUM_BUFFERS];
  std::map<std::shared_ptr<const CDVDOverlay>,std::shared_ptr<COverlay>,
           std::owner_less<std::shared_ptr<const CDVDOverlay>>> m_textureCache;
  std::vector<const CDVDOverlay*> drawn;std::vector<double> evaluated;
  void SetOverlays(OverlayBatch,int);OverlayBatch GetOverlays(int);
  void Release(int);void Release(std::vector<SElement>&);void ReleaseUnused(const OverlayBatch& selected={});
  bool HasOverlay(const OverlayBatch&);bool HasPqMenuOverlay(const OverlayBatch&);
  bool HasDiscMenuOverlay(const OverlayBatch&);bool HasTextOverlay(const OverlayBatch&);bool HasImageOverlay(const OverlayBatch&);
  void Render(const OverlayBatch&);void RenderPqMenu(const OverlayBatch&);
  std::shared_ptr<COverlay> Convert(const CDVDOverlay& o,double pts) {
    evaluated.push_back(pts);auto r=std::make_shared<COverlay>();r->value=&o;return r;
  }
  void Render(std::shared_ptr<COverlay> o){drawn.push_back(o->value);}
};
}
using namespace OVERLAY;
struct CRenderManager {
  enum EPRESENTSTEP{PRESENT_IDLE,PRESENT_FLIP,PRESENT_FRAME,PRESENT_FRAME2,PRESENT_READY};
  enum EFIELDSYNC{FS_NONE,FS_TOP,FS_BOT};
  enum EPRESENTMETHOD{PRESENT_METHOD_SINGLE,PRESENT_METHOD_BLEND,PRESENT_METHOD_BOB};
  enum ERENDERSTATE{STATE_UNCONFIGURED,STATE_CONFIGURING,STATE_CONFIGURED};
  struct SPresent{double pts=0;EFIELDSYNC presentfield=FS_NONE;EPRESENTMETHOD presentmethod=PRESENT_METHOD_SINGLE;};
  SPresent m_Queue[NUM_BUFFERS];
  @SELECTION@
  @PASS@
  @DRAW@
  std::shared_ptr<const FrameSelection> m_frameSelection;
  CCriticalSection m_statelock,m_presentlock;
  Renderer renderer;Renderer* m_pRenderer=&renderer;OVERLAY::CRenderer m_overlays;
  Port port;Port* m_playerPort=&port;Player player;Player* m_appPlayer=&player;
  std::deque<int> m_queued,m_discard,m_free;Clock m_dvdClock;Cache m_dataCacheCore;
  double m_displayLatency=0,m_latencyTweak=0,m_audioLatencyTweak=0,m_videoDelay=0,m_presentpts=0;
  struct {bool m_enabled=false;double m_error=0,m_syncOffset=0;int m_errCount=0;} m_clockSync;
  bool m_presentstarted=false,m_showVideo=true,m_forceNext=false,m_bRenderGUI=true,m_renderedOverlay=false,m_renderDebug=false;
  std::atomic<bool> m_subtitleEnabled{true};
  int m_presentsource=0,m_presentsourcePast=-1,m_lateframes=0,m_QueueSkip=0;
  EPRESENTSTEP m_presentstep=PRESENT_IDLE;ERENDERSTATE m_renderState=STATE_CONFIGURED;
  Event m_presentevent;XbmcThreads::EndTime<> m_presentTimer,m_debugTimer;
  std::function<void()> resolutionHook;
  void UpdateResolution(){if(resolutionHook)resolutionHook();}
  bool Configure(){return false;}void FrameWait(std::chrono::milliseconds){}void CheckEnableClockSync(){}
  void ManageCaptures(){}bool IsConfigured(){return m_renderState==STATE_CONFIGURED;}
  void InvalidateReservations(){} // reservation semantics exercised by the 2A fixture
  void SelectFrame();void ClearFrameSelection();void FrameMove();void ProcessPresentationQueue();void RetireBuffer(int);
  void PrepareNextRender();void DiscardBuffer();void UpdateGuiPresentationState(bool);void RenderCapture(CRenderCapture*);
  bool IsGuiLayer();bool IsPresenting();void DrawMenus();
  static PreparedVideoDraw PrepareVideoDraw(const FrameSelection&,EPRESENTSTEP,bool,DWORD,DWORD);
  void SubmitVideoDraw(const PreparedVideoDraw&);
};
'''

TESTS = r'''
std::shared_ptr<CDVDOverlayImage> menu(bool bdj,uint32_t color) {
  auto p=std::make_shared<CDVDOverlayImage>();p->SetDiscMenuOverlay(true);
  p->m_isPqMenuGraphics=true;p->m_isHdrPq=true;p->width=p->height=1;
  if(bdj){p->pixels.resize(4);memcpy(p->pixels.data(),&color,4);}else{p->palette={color};p->pixels={0};p->pqMenuPalette={color};}
  p->m_menuVisible=p->HasVisiblePixels();return p;
}
void queue(CRenderManager& r,int i,double pts){r.m_Queue[i].pts=pts;r.m_queued.push_back(i);r.m_presentstep=CRenderManager::PRESENT_READY;}
int main(){
  // Initial early selection must never invent a past lease for placeholder 0.
  for(int first:{0,1}){
    CRenderManager r;queue(r,first,10000);r.FrameMove();
    assert(r.m_presentstarted&&r.m_frameSelection->source==first&&r.m_frameSelection->past==-1);
    assert(r.m_presentpts==0); // existing half-frame early PTS adjustment
    queue(r,2,100000);r.FrameMove();
    assert(r.m_frameSelection->source==first&&r.renderer.released.empty());
    assert(std::find(r.m_free.begin(),r.m_free.end(),first)==r.m_free.end());
    // Normal early pairing still keeps a real current/past pair.
    r.m_dvdClock.pts=90000;r.m_presentstep=CRenderManager::PRESENT_READY;r.FrameMove();
    assert(r.m_frameSelection->source==2&&r.m_frameSelection->past==first);
    queue(r,3,200000);r.FrameMove();
    assert(r.m_frameSelection->source==2&&r.m_frameSelection->past==-1);
    assert(r.renderer.released==std::vector<int>{first});
  }
  // Identity/metadata and both field passes remain stable even if live queue
  // members are changed; BOB still uses the current per-call step.
  {CRenderManager r;r.m_presentsource=2;r.m_presentsourcePast=1;
   r.m_Queue[2]={321,CRenderManager::FS_TOP,CRenderManager::PRESENT_METHOD_BOB};r.SelectFrame();
   r.m_presentsource=4;r.m_presentsourcePast=3;r.m_Queue[2].presentfield=CRenderManager::FS_BOT;
   const auto& f=*r.m_frameSelection;r.m_presentstep=CRenderManager::PRESENT_FRAME;r.SubmitVideoDraw(r.PrepareVideoDraw(f,r.m_presentstep,true,0,255));
   r.m_presentstep=CRenderManager::PRESENT_FRAME2;r.SubmitVideoDraw(r.PrepareVideoDraw(f,r.m_presentstep,false,0,255));
   assert(r.renderer.calls[0].source==2&&r.renderer.calls[0].past==1);
   assert(r.renderer.calls[0].flags==(RENDER_FLAG_TOP|RENDER_FLAG_FIELD0));
   assert(r.renderer.calls[1].flags==(RENDER_FLAG_BOT|RENDER_FLAG_FIELD1));
   auto other=f;other.present.presentmethod=CRenderManager::PRESENT_METHOD_SINGLE;
   r.SubmitVideoDraw(r.PrepareVideoDraw(other,r.m_presentstep,false,0,255));assert(r.renderer.calls.back().flags==RENDER_FLAG_TOP);
   other.present.presentmethod=CRenderManager::PRESENT_METHOD_BLEND;
   r.SubmitVideoDraw(r.PrepareVideoDraw(other,r.m_presentstep,true,0,255));assert(r.renderer.calls.back().alpha==127);
   CRenderCapture capture;r.RenderCapture(&capture);assert(r.renderer.captureSource==2);
   r.ClearFrameSelection();r.RenderCapture(&capture);assert(r.renderer.captureSource==4);
   r.renderer.captureOk=false;r.RenderCapture(&capture);assert(capture.state==CAPTURESTATE_FAILED);}
  for(bool bdj:{false,true})for(bool gui:{false,true}){
    CRenderManager r;auto* w=CServiceBroker::GetWinSystem();*w={};w->active=!gui;r.renderer.gui=gui;
    auto old=menu(bdj,0xff123456);auto text=std::make_shared<CDVDOverlay>(DVDOVERLAY_TYPE_TEXT);
    r.m_overlays.SetOverlays({{7,old},{8,text}},0);r.SelectFrame();r.UpdateGuiPresentationState(false);
    assert(discVisible&&r.port.gui&&w->requests.back()==!gui);
    // A new palette/canvas list and a later clear cannot change this selection.
    auto changed=menu(bdj,0xffabcdef);r.m_overlays.SetOverlays({{9,changed}},0);
    r.DrawMenus();assert(w->requests.back()==!gui);
    assert(r.m_overlays.drawn==std::vector<const CDVDOverlay*>({old->GetPublishedRenderContent().get(),text->GetPublishedRenderContent().get()}));
    assert(r.m_overlays.evaluated==std::vector<double>({7,8}));
    r.m_overlays.drawn.clear();r.m_overlays.evaluated.clear();
    r.m_overlays.SetOverlays({},0);r.DrawMenus();assert(r.m_overlays.drawn.front()==old->GetPublishedRenderContent().get());
    // Expiration works without any draw/swap completion callback, and occurs
    // before display updates. Paused selection is reacquired each FrameMove.
    std::weak_ptr<const CRenderManager::FrameSelection> previous=r.m_frameSelection;
    r.resolutionHook=[&]{assert(previous.expired()&&!r.m_frameSelection);};
    r.FrameMove();assert(!discVisible&&!w->requests.back());r.resolutionHook={};
    auto transparent=menu(bdj,0);r.m_overlays.SetOverlays({{10,transparent}},0);r.FrameMove();
    assert(discVisible&&!w->requests.back()); // transparent PQ canvas must not engage composite
    r.m_overlays.SetOverlays({{11,changed}},0);r.FrameMove();assert(discVisible);
    auto held=r.m_frameSelection;r.DiscardBuffer();assert(r.m_frameSelection==held);
    r.m_bRenderGUI=false;r.FrameMove();assert(r.m_frameSelection->overlays[0].overlay_dvd==changed->GetPublishedRenderContent());
    r.m_overlays.SetOverlays({},0);r.FrameMove();assert(!discVisible);
  }
  // Cache eviction accounts for a detached selected batch, without moving
  // cache destruction into CPU selection destruction or producer callbacks.
  {CRenderManager r;auto image=menu(false,0xff112233);
   r.m_overlays.SetOverlays({{0,image}},0);r.SelectFrame();r.m_overlays.Release(0);
   r.m_overlays.m_textureCache[image->GetPublishedRenderContent()]=std::make_shared<COverlay>();
   r.m_overlays.ReleaseUnused(r.m_frameSelection->overlays);assert(r.m_overlays.m_textureCache.count(image->GetPublishedRenderContent()));
   r.ClearFrameSelection();r.m_overlays.ReleaseUnused();assert(r.m_overlays.m_textureCache.empty());}
  // Selected bitmap contents are independent of later producer mutation;
  // libass track/style evaluation remains at its established render point.
  {CRenderManager r;auto image=menu(false,0xff112233);r.m_overlays.SetOverlays({{0,image}},0);r.SelectFrame();
   image->palette[0]=0xff445566;
   assert(std::static_pointer_cast<const CDVDOverlayImage>(r.m_frameSelection->overlays[0].overlay_dvd)->palette[0]==0xff112233);
   image->PublishRenderContent();r.m_overlays.SetOverlays({{0,image}},0);
   r.FrameMove();assert(std::static_pointer_cast<const CDVDOverlayImage>(r.m_frameSelection->overlays[0].overlay_dvd)->palette[0]==0xff445566);}
  // No render callback on an unconfigured/configure-failure frame can leave the
  // previous selection alive. Real configure/flush/teardown wiring is checked above.
  for(auto state:{CRenderManager::STATE_UNCONFIGURED,CRenderManager::STATE_CONFIGURING}){
    CRenderManager r;r.SelectFrame();std::weak_ptr<const CRenderManager::FrameSelection> old=r.m_frameSelection;
    r.m_renderState=state;r.FrameMove();assert(old.expired()&&!r.m_frameSelection);
  }
}
'''

if __name__ == '__main__':
    main()
