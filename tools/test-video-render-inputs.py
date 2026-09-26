#!/usr/bin/env python3
"""Run complete production Render and AML submission/capture methods with stubs.

Covers per-call fields/progression, GUI/video gates, late GUI rectangle values,
and AML rectangle capture/release/generation/poll ordering. Services, renderer,
L5 math, codec and screenshot/GPU effects are stand-ins, not hardware evidence.
--compare REV also checks call traces against accepted production at REV.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def source_at(relative, revision):
    if revision:
        return subprocess.check_output(['git', 'show', f'{revision}:{relative}'], cwd=ROOT, text=True)
    return (ROOT / relative).read_text()


def harness(revision=None):
    directory = 'xbmc/cores/VideoPlayer/VideoRenderers/'
    manager = source_at(directory + 'RenderManager.cpp', revision)
    header = source_at(directory + 'RenderManager.h', revision)
    aml = source_at(directory + 'HwDecRender/RendererAML.cpp', revision)
    aml_header = source_at(directory + 'HwDecRender/RendererAML.h', revision)
    prepared = 'struct PreparedVideoDraw' in header
    source = PRELUDE.replace('@PREPARED@', '1' if prepared else '0')
    for name in ['EPRESENTSTEP', 'EPRESENTMETHOD', 'ERENDERSTATE']:
        source = source.replace('@' + name + '@', function(header, 'enum ' + name) + ';')
    source = source.replace('@FRAME@', function(header, 'struct FrameSelection\n') + ';')
    source = source.replace('@PASS@', function(header, 'struct VideoRenderPass') + ';' if prepared else '')
    source = source.replace('@DRAW@', function(header, 'struct PreparedVideoDraw') + ';' if prepared else '')
    source = source.replace('@GEOMETRY@', function(aml_header, 'struct PreparedVideoGeometry') + ';' if prepared else '')
    for signature in ['void CRenderManager::Render(bool', 'void CRenderManager::RenderCapture(']:
        source += '\n' + function(manager, signature)
    if prepared:
        source += '\n' + function(manager, 'CRenderManager::PreparedVideoDraw CRenderManager::PrepareVideoDraw(')
        source += '\n' + function(manager, 'void CRenderManager::SubmitVideoDraw(')
    else:
        for name in ['PresentSingle', 'PresentFields', 'PresentBlend']:
            source += '\n' + function(manager, 'void CRenderManager::' + name + '(')
    for signature in ['void CRendererAML::RenderUpdate(', 'void CRendererAML::CommitVideoLayer(',
                      'void CRendererAML::PollVideoLayer()', 'bool CRendererAML::RenderCapture(']:
        source += '\n' + function(aml, signature)
    source += '\n' + function(aml, ('CRendererAML::PreparedVideoGeometry' if prepared else 'void') + ' CRendererAML::PrepareVideoLayer()')
    return source + TESTS


def run(revision=None):
    with tempfile.TemporaryDirectory(prefix='video-inputs-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(harness(revision))
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        return subprocess.check_output([str(out / 'test')], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare', metavar='REV')
    args = parser.parse_args()
    actual = run()
    if args.compare:
        baseline = run(args.compare)
        assert actual == baseline, 'Production call traces differ from ' + args.compare
        print('Differential video/AML call traces match:', args.compare)
    print('Video render inputs: PASS (complete Render and AML methods; ASan/UBSan; backend/services stubbed)')


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <chrono>
#include <deque>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include "utils/Geometry.h"
#include "cores/VideoPlayer/VideoRenderers/RenderFlags.h"
using namespace std::chrono_literals;
#define PREPARED @PREPARED@
using DWORD=unsigned int;
using CCriticalSection=std::recursive_mutex;
std::vector<std::string> trace;
void event(const std::string& s){trace.push_back(s);}
void dump(){for(const auto& s:trace)std::cout<<s<<'|';std::cout<<'\n';trace.clear();}
std::string rect(const CRect& r){return std::to_string(static_cast<int>(r.x1))+","+std::to_string(static_cast<int>(r.y1))+","+std::to_string(static_cast<int>(r.x2))+","+std::to_string(static_cast<int>(r.y2));}
bool same(const CRect& a,const CRect& b){return rect(a)==rect(b);}
constexpr int NUM_BUFFERS=16,CAPTURESTATE_FAILED=1;
enum EFIELDSYNC{FS_NONE,FS_TOP,FS_BOT};
enum class StreamHdrType{HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
struct RESOLUTION_INFO{int iSubtitles=1000,iHeight=1080;struct{int top=0;}Overscan;};
struct Gfx{RESOLUTION_INFO GetResInfo(){event("calibration");return {};}};
struct Window{
  Gfx gfx;bool active=false;Gfx& GetGfxContext(){return gfx;}
  void RequestMenuComposite(bool b){event(b?"menu-request":"menu-clear");}
  bool BeginMenuOverlayRender(){event("menu-begin");return active;}
  void EndMenuOverlayRender(){event("menu-end");}
  struct Info{std::string renderFlags,videoOutput;};Info GetDebugInfo(){return {};}
};
using CWinSystemBase=Window;
struct Data{
  struct Metadata{bool has_level5_metadata=false;uint16_t level5_active_area_top_offset=0,level5_active_area_bottom_offset=0;};
  Metadata GetVideoDoViFrameMetadata(){return {};}
};
struct Settings{Settings* GetSubtitlesSettings(){return this;}float GetVerticalMarginPerc(){return 5.0f;}};
struct CServiceBroker{
  static Window* GetWinSystem(){static Window w;return &w;}
  static Data& GetDataCacheCore(){static Data d;return d;}
  static Settings* GetSettingsComponent(){static Settings s;return &s;}
};
struct CSingleExit{explicit CSingleExit(Gfx&){event("gfx-exit");}~CSingleExit(){event("gfx-restore");}};
bool restrictActive=false;int signalMode=0;
bool aml_dv_use_active_area(){event("active-setting");return restrictActive;}
int aml_dv_l5_subs_signal_mode(){event("signal-setting");return signalMode;}
bool aml_dv_auto_letterbox_get(uint16_t&,uint16_t&,uint16_t&,uint16_t&){return false;}
bool aml_dv_auto_letterbox_additive(){return false;}
bool aml_dv_detect_active_area_enabled(){return false;}
bool aml_dv_detect_active_area_stable(){return false;}
void aml_dv_detect_active_area_get(uint16_t&,uint16_t&,uint16_t&,uint16_t&){}
void aml_dv_set_subtitles(bool b){event(b?"signal-on":"signal-off");}
struct DEBUG_INFO_PLAYER{std::string audio,video,player,vsync;};
struct DEBUG_INFO_VIDEO{std::string ignored;};
using DEBUG_INFO_RENDER=Window::Info;
struct StringUtils{template<class... T>static std::string Format(T&&...){return "debug";}};
double DVD_TIME_TO_MSEC(double v){return v/1000.0;}
struct Clock{bool GetClockInfo(int&,double&,double&){return false;}};
struct Port{void GetDebugInfo(std::string&,std::string&,std::string&) {}};
struct Debug{
  template<class... T>void SetInfo(T&&...){}
  void Render(CRect& s,CRect& d,CRect& v){event("debug:"+rect(s)+":"+rect(d)+":"+rect(v));}
};
struct Timer{void Set(std::chrono::milliseconds){}};
struct Event{void notifyAll(){event("notify");}};
struct CRenderCapture{
  int state=0;unsigned int width=640,height=360;unsigned char pixel=0;
  void SetState(int s){state=s;event("capture-failed");}
  void BeginRender(){event("capture-begin");}void EndRender(){event("capture-end");}
  unsigned int GetWidth(){return width;}unsigned int GetHeight(){return height;}
  unsigned char* GetRenderBuffer(){return &pixel;}
};
struct Renderer{
  bool gui=false,captureOk=true;int revision=0;std::function<void()> drawHook;
  CRect source{0,0,1920,1080},dest{0,0,1920,1080},view{0,0,1920,1080};
  bool IsGuiLayer(){event(gui?"gui-layer":"video-layer");return gui;}
  void Update(){event("update");dest={float(++revision),20,1000,600};}
  void GetVideoRect(CRect& s,CRect& d,CRect& v){event("get-rect");s=source;d=dest;v=view;}
  void RenderUpdate(int sourceIndex,int past,bool clear,unsigned flags,unsigned alpha){
    event("draw:"+std::to_string(sourceIndex)+":"+std::to_string(past)+":"+std::to_string(clear)+":"+std::to_string(flags)+":"+std::to_string(alpha));
    if(drawHook)drawHook();
  }
  DEBUG_INFO_VIDEO GetDebugInfo(int){return {};}
  bool RenderCapture(int index,CRenderCapture*){event("capture-source:"+std::to_string(index));return captureOk;}
};
namespace OVERLAY{struct CRenderer{
  using OverlayBatch=std::vector<int>;
  std::function<void()> overlayHook;
  bool HasOverlay(const OverlayBatch&){event("has-overlay");return true;}
  bool HasTextOverlay(const OverlayBatch&){return true;}
  bool HasImageOverlay(const OverlayBatch&){return true;}
  bool HasImageSubOutsideActiveArea(const OverlayBatch&,int,int){return false;}
  bool HasPqMenuOverlay(const OverlayBatch&){return true;}
  void SetVideoRect(CRect& s,CRect& d,CRect& v){event("overlay-rect:"+rect(s)+":"+rect(d)+":"+rect(v));}
  void RenderPqMenu(const OverlayBatch&){event("pq-menu");}
  void Render(const OverlayBatch&){event("overlay");if(overlayHook)overlayHook();}
};}
struct CRenderManager{
  @EPRESENTSTEP@
  @EPRESENTMETHOD@
  @ERENDERSTATE@
  struct SPresent{double pts;EFIELDSYNC presentfield;EPRESENTMETHOD presentmethod;};
  @FRAME@
  @PASS@
  @DRAW@
  CCriticalSection m_statelock,m_presentlock;
  bool m_presentstarted=true,m_renderedOverlay=false,m_renderDebug=false,m_renderDebugVideo=false;
  ERENDERSTATE m_renderState=STATE_CONFIGURED;
  std::shared_ptr<const FrameSelection> m_frameSelection;
  Renderer renderer;Renderer* m_pRenderer=&renderer;
  EPRESENTSTEP m_presentstep=PRESENT_FRAME;
  int m_presentsource=7;std::deque<int> m_queued;
  OVERLAY::CRenderer m_overlays;Debug m_debugRenderer;Timer m_debugTimer;Event m_presentevent;
  struct{StreamHdrType hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;}m_picture;
  struct{double m_syncOffset=0;}m_clockSync;
  double m_displayLatency=0,m_audioLatencyTweak=0,m_latencyTweak=0;
  Clock m_dvdClock;Port port;Port* m_playerPort=&port;
  void CalcOverlayActiveArea(CRect& s,CRect& d,CRect& v,bool use){event("active-rect:"+rect(s)+":"+rect(d)+":"+rect(v)+":"+std::to_string(use));}
  void Render(bool,DWORD,DWORD,bool);void RenderCapture(CRenderCapture*);
#if PREPARED
  static PreparedVideoDraw PrepareVideoDraw(const FrameSelection&,EPRESENTSTEP,bool,DWORD,DWORD);
  void SubmitVideoDraw(const PreparedVideoDraw&);
#else
  void PresentSingle(const FrameSelection&,bool,DWORD,DWORD);
  void PresentFields(const FrameSelection&,bool,DWORD,DWORD);
  void PresentBlend(const FrameSelection&,bool,DWORD,DWORD);
#endif
};
struct CAMLCodec{
  std::function<void()> releaseHook;
  static inline std::function<void()> pollHook;
  int ReleaseFrame(int index,uint64_t generation){event("release:"+std::to_string(index)+":"+std::to_string(generation));if(releaseHook)releaseHook();return 0;}
  void SetVideoRect(const CRect& s,const CRect& d,uint64_t generation){event("aml-rect:"+rect(s)+":"+rect(d)+":"+std::to_string(generation));}
  static void PollFrame(){event("poll");if(pollHook)pollHook();}
};
struct CVideoBuffer{virtual ~CVideoBuffer()=default;};
struct CAMLVideoBuffer:CVideoBuffer{CAMLCodec* m_amlCodec=nullptr;int m_omxPts=0,m_bufferIndex=0;uint64_t m_presentationGeneration=0;};
struct CScreenshotAML{
  static bool CaptureVideoFrame(unsigned char* pixel,unsigned width,unsigned height){event("screenshot:"+std::to_string(width)+":"+std::to_string(height));*pixel=42;return true;}
};
struct CRendererAML{
  @GEOMETRY@
  struct{CVideoBuffer* videoBuffer=nullptr;}m_buffers[NUM_BUFFERS];
  int m_prevVPts=-1,revision=0;
  CRect m_sourceRect,m_destRect;
  void ManageRenderArea(){event("prepare");m_sourceRect={0,0,1920,1080};m_destRect={float(++revision),20,1000,600};}
  void RenderUpdate(int,int,bool,unsigned,unsigned);
#if PREPARED
  PreparedVideoGeometry PrepareVideoLayer();void CommitVideoLayer(int,const PreparedVideoGeometry&);
#else
  void PrepareVideoLayer();void CommitVideoLayer(int);
#endif
  void PollVideoLayer();bool RenderCapture(int,CRenderCapture*);
};
'''

TESTS = r'''
int main(){
  // Full production Render: every method/field/step, flags/odd alpha, GUI and
  // hardware passes, queued readiness, and repeated calls on one selection.
  unsigned cases=0;
  for(auto method:{CRenderManager::PRESENT_METHOD_SINGLE,CRenderManager::PRESENT_METHOD_BOB,CRenderManager::PRESENT_METHOD_BLEND})
  for(auto field:{FS_NONE,FS_TOP,FS_BOT})
  for(auto step:{CRenderManager::PRESENT_IDLE,CRenderManager::PRESENT_FLIP,CRenderManager::PRESENT_FRAME,CRenderManager::PRESENT_FRAME2,CRenderManager::PRESENT_READY})
  for(bool gui:{false,true})for(bool guiLayer:{false,true})for(bool clear:{false,true})
  for(unsigned alpha:{0u,1u,127u,255u})for(bool queued:{false,true})
  for(unsigned flags:{0u,static_cast<unsigned>(RENDER_FLAG_NOOSD)}){
    CRenderManager r;r.renderer.gui=guiLayer;r.m_presentstep=step;
    r.m_frameSelection=std::make_shared<const CRenderManager::FrameSelection>(CRenderManager::FrameSelection{2,1,{123,field,method},{}});
    if(queued)r.m_queued.push_back(4);
    CServiceBroker::GetWinSystem()->active=true;
    for(int repeated=0;repeated<3;++repeated){
      r.Render(clear,flags,alpha,gui);
      event("step:"+std::to_string(r.m_presentstep));
    }
    dump();++cases;
  }
  assert(cases==5760);
  {CRenderManager r;
   r.m_frameSelection=std::make_shared<const CRenderManager::FrameSelection>(CRenderManager::FrameSelection{2,1,{0,FS_TOP,CRenderManager::PRESENT_METHOD_BOB},{}});
   r.Render(true,0,255,false);
   assert(std::find(trace.begin(),trace.end(),"draw:2:1:1:"+std::to_string(RENDER_FLAG_TOP|RENDER_FLAG_FIELD0)+":255")!=trace.end());
   assert(r.m_presentstep==CRenderManager::PRESENT_FRAME2);dump();
   r.Render(false,0,255,false);
   assert(std::find(trace.begin(),trace.end(),"draw:2:1:0:"+std::to_string(RENDER_FLAG_BOT|RENDER_FLAG_FIELD1)+":255")!=trace.end());
   assert(r.m_presentstep==CRenderManager::PRESENT_IDLE);dump();}

  for(int reason=0;reason<3;++reason){
    CRenderManager r;
    r.m_frameSelection=std::make_shared<const CRenderManager::FrameSelection>(CRenderManager::FrameSelection{2,-1,{0,FS_TOP,CRenderManager::PRESENT_METHOD_BOB},{}});
    if(reason==0)r.m_presentstarted=false;
    if(reason==1)r.m_renderState=CRenderManager::STATE_UNCONFIGURED;
    if(reason==2)r.m_frameSelection.reset();
    r.Render(true,0,255,true);
    assert((trace==std::vector<std::string>{"gfx-exit","gfx-restore"}));
    assert(r.m_presentstep==CRenderManager::PRESENT_FRAME);dump();
  }
  // GUI updates occur after backend presentation, before rect/L5/overlay/debug.
  for(bool guiLayer:{false,true}){
    CRenderManager r;r.renderer.gui=guiLayer;r.m_renderDebug=true;
    r.renderer.view={100,200,1100,800}; // windowed viewport
    r.m_frameSelection=std::make_shared<const CRenderManager::FrameSelection>(CRenderManager::FrameSelection{2,1,{0,FS_NONE,CRenderManager::PRESENT_METHOD_SINGLE},{}});
    r.renderer.drawHook=[&]{r.renderer.dest={30,40,900,500};};
    signalMode=2;restrictActive=true;
    for(int i=0;i<2;++i){r.Render(false,0,255,true);dump();}
    // Completion reads current live step, not the step used for drawing.
    r.m_overlays.overlayHook=[&]{r.m_presentstep=CRenderManager::PRESENT_FRAME2;};
    r.Render(false,0,255,true);assert(r.m_presentstep==CRenderManager::PRESENT_IDLE);dump();
    r.renderer.drawHook=[] {throw std::runtime_error("backend");};
    r.m_presentstep=CRenderManager::PRESENT_FRAME;
    try{r.Render(true,0,255,false);assert(guiLayer);}catch(const std::runtime_error&){}
    assert(r.m_presentstep==CRenderManager::PRESENT_FRAME);
    assert(std::find(trace.begin(),trace.end(),"notify")==trace.end());dump();
  }
  // Capture uses selected source or current fallback, never a queued slot,
  // without stepping fields or preparing display geometry.
  {CRenderManager r;CRenderCapture c;r.m_queued.push_back(9);
   r.RenderCapture(&c);assert(trace==std::vector<std::string>{"capture-source:7"});dump();
   r.m_frameSelection=std::make_shared<const CRenderManager::FrameSelection>(CRenderManager::FrameSelection{2,1,{0,FS_TOP,CRenderManager::PRESENT_METHOD_BOB},{}});
   r.RenderCapture(&c);assert(trace==std::vector<std::string>{"capture-source:2"});dump();
   r.renderer.captureOk=false;r.RenderCapture(&c);assert(c.state==CAPTURESTATE_FAILED);dump();
   assert(r.m_presentstep==CRenderManager::PRESENT_FRAME);}
  // Real AML methods with a fake codec. Null, processed, fresh and duplicate
  // PTS still prepare and poll; generation, release and marking order persist.
  {CRendererAML r;CAMLCodec codec;CAMLVideoBuffer buffer;
   r.RenderUpdate(0,-1,true,0,255);assert((trace==std::vector<std::string>{"prepare","poll"}));dump();
   r.m_buffers[0].videoBuffer=&buffer;r.RenderUpdate(0,-1,false,0,1);dump();
   buffer.m_amlCodec=&codec;buffer.m_omxPts=123;buffer.m_bufferIndex=8;buffer.m_presentationGeneration=99;
   CAMLCodec::pollHook=[&]{assert(!buffer.m_amlCodec&&r.m_prevVPts==123);};
   r.RenderUpdate(0,-1,false,0,255);
   assert((trace==std::vector<std::string>{"prepare","release:8:99","aml-rect:0,0,1920,1080:3,20,1000,600:99","poll"}));dump();
   CAMLCodec::pollHook={};buffer.m_amlCodec=&codec;buffer.m_presentationGeneration=100;
   r.RenderUpdate(0,-1,false,0,255);assert(buffer.m_amlCodec==&codec);assert((trace==std::vector<std::string>{"prepare","poll"}));dump();
   buffer.m_omxPts=124;r.RenderUpdate(0,-1,false,0,255);assert(!buffer.m_amlCodec);dump();
   CRenderCapture c;const auto before=r.m_destRect;const int previous=r.m_prevVPts;
   assert(r.RenderCapture(9,&c));assert(c.pixel==42&&same(before,r.m_destRect)&&previous==r.m_prevVPts);
   assert((trace==std::vector<std::string>{"capture-begin","capture-end","screenshot:640:360"}));dump();}
#if PREPARED
  // Prepared pass values remain independent of the selection and current step.
  {CRenderManager r;CRenderManager::FrameSelection f{2,1,{0,FS_BOT,CRenderManager::PRESENT_METHOD_BLEND},{}};
   const auto draw=r.PrepareVideoDraw(f,CRenderManager::PRESENT_FRAME,true,0,255);
   f.source=9;f.past=8;f.present.presentfield=FS_TOP;r.m_presentstep=CRenderManager::PRESENT_FRAME2;
   r.SubmitVideoDraw(draw);
   assert((trace==std::vector<std::string>{"draw:2:1:1:"+std::to_string(RENDER_FLAG_BOT|RENDER_FLAG_NOOSD)+":255","draw:2:1:0:"+std::to_string(RENDER_FLAG_TOP)+":127"}));trace.clear();}
  // The rectangle values survive a member mutation between prepare and commit,
  // including one inside ReleaseFrame. No codec ownership is inferred.
  {CRendererAML r;CAMLCodec codec;CAMLVideoBuffer buffer;
   r.m_buffers[0].videoBuffer=&buffer;buffer.m_amlCodec=&codec;buffer.m_omxPts=9;
   buffer.m_bufferIndex=3;buffer.m_presentationGeneration=77;
   const auto geometry=r.PrepareVideoLayer();r.m_sourceRect={5,6,7,8};r.m_destRect={9,10,11,12};
   codec.releaseHook=[&]{r.m_sourceRect={20,30,40,50};r.m_destRect={60,70,80,90};};
   r.CommitVideoLayer(0,geometry);
   assert((trace==std::vector<std::string>{"prepare","release:3:77","aml-rect:0,0,1920,1080:1,20,1000,600:77"}));trace.clear();}
#endif
}
'''

if __name__ == '__main__':
    main()
