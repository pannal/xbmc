#!/usr/bin/env python3
"""Production renderer Configure and mute controller, with recording renderer stubs.
Tests the non-AML successor path without a device. --baseline REF must fail.
"""
import argparse, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILE='xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp'
def function(s, marker):
 a=s.index(marker);i=s.index('{',a)+1;depth=1
 while depth:depth+=(s[i]=='{')-(s[i]=='}');i+=1
 return s[a:i]
PRE=r'''
#include "utils/AMLVideoMute.h"
#include <cassert>
#include <stdexcept>
#include <string>
#include <vector>
#include <optional>
#include <iostream>
#define HAS_LIBAMCODEC
using CCriticalSection=std::recursive_mutex;
constexpr int LOGWARNING=0,LOGINFO=1,LOGDEBUG=2,RENDER_STEREO_MODE_OFF=0;
constexpr double DVD_NOPTS_VALUE=-1;
struct CLog{template<class... T>static void Log(T&&...) {}};
namespace PLAYBACK_DIAGNOSTICS{int NowUs(){return 0;}}
static bool muteState=false,selectAML=false,create=true,configured=true,throwConfigure=false;
CAMLVideoMute& aml_video_mute_controller(){static CAMLVideoMute c([](bool b){muteState=b;});return c;}
struct Picture {std::string stereoMode;};
struct CRenderInfo{int max_buffer_size=3;};
struct Renderer{
 virtual ~Renderer()=default;
 void SetVideoSettings(int){};bool Configure(Picture&,float,unsigned){if(throwConfigure)throw std::runtime_error("configure");return configured;}
 CRenderInfo GetRenderInfo(){return {};}
 void SetBufferSize(int){}void Update(){}bool IsGuiLayer(){return true;}
};
struct CRendererAML:Renderer{int PresenterCodec(){return 1;}};
struct Clock{void SetVsyncAdjust(int){}};
struct Port{int GetVideoSettings(){return 0;}void UpdateRenderInfo(CRenderInfo){}void UpdateGuiRender(bool){}void UpdateVideoRender(bool){}void UpdateClockSync(bool){}void VideoParamsChange(){}};
struct CAMLPresenterSession{
 template<class... T>CAMLPresenterSession(T&&...){}
 struct Q{void Show(bool){}} q;Q* queue=&q;
};
struct Graphics{int GetStereoMode(){return 0;}};
struct Window{Graphics g;Graphics& GetGfxContext(){return g;}};
struct CServiceBroker{static Window* GetWinSystem(){static Window w;return &w;}};
class CRenderManager {
public:
 enum {STATE_UNCONFIGURED,STATE_CONFIGURED,PRESENT_IDLE};
 std::optional<int> m_appliedResolution;
 CCriticalSection m_statelock,m_presentlock,m_datalock;
 int m_flushGeneration=0,m_renderState=STATE_UNCONFIGURED,m_presentstep=PRESENT_IDLE;
 int m_QueueSize=0,m_NumberBuffers=0,m_presentsource=0,m_presentsourcePast=0,m_lateframes=0;
 int m_diagnosticId=0,m_lifecycleGeneration=0;
 float m_fps=24;unsigned m_orientation=0;
 double m_presentpts=0;
 bool m_presentstarted=false,m_bRenderGUI=false,m_bTriggerUpdateResolution=false,m_renderedOverlay=false,m_renderDebug=false;
 bool m_amlIndependentPresenter=false,m_showVideo=true;
 std::vector<int>m_queued,m_discard,m_free;
 Renderer* m_pRenderer=nullptr;
 std::unique_ptr<Picture> m_pConfigPicture=std::make_unique<Picture>();Picture m_picture;
 Port port;Port* m_playerPort=&port;
 std::shared_ptr<int> m_processInfoLifetime=std::make_shared<int>(1);
 std::shared_ptr<CAMLPresenterSession>m_amlPresenter;
 Clock m_dvdClock;
 struct Sync{void Reset(){}}m_clockSync;
 struct Overlay{void Reset(){}void SetStereoMode(std::string){}}m_overlays;
 struct Event{void notifyAll(){}}m_presentevent;
 struct Cache{void SetRenderPts(double){}}m_dataCacheCore;
 void StopAMLPresenter(bool){}void ClearFrameSelection(){}void InvalidateReservations(){}
 void CreateRenderer(){if(create)m_pRenderer=selectAML?new CRendererAML:new Renderer;}
 void DeleteRenderer(){delete m_pRenderer;m_pRenderer=nullptr;}
 bool UpdateAMLPresenter(){return true;}void LogAMLPresenter(const char*,bool){}
 ~CRenderManager(){DeleteRenderer();}
 bool Configure();
};
'''
TEST=r'''
int main(){
 auto& mute=aml_video_mute_controller();auto deadline=CAMLVideoMute::Clock::now()+std::chrono::seconds(6);
 auto hold=mute.Acquire();mute.Handoff(hold,deadline);assert(muteState);
 {CRenderManager r;assert(r.Configure());assert(!muteState && "non-AML configure cancels inherited blackout");}
 // The old close may publish late; configuration invalidates that generation.
 hold=mute.Acquire();{CRenderManager r;assert(r.Configure());assert(muteState);}
 mute.Handoff(hold,deadline);assert(!muteState && !mute.HandoffGeneration());
 // No cancellation on creation failure, configure failure or exception.
 for(int path=0;path<3;path++){
  hold=mute.Acquire();mute.Handoff(hold,deadline);
  create=path!=0;configured=path!=1;throwConfigure=path==2;
  {CRenderManager r;try{assert(!r.Configure());assert(path!=2);}catch(const std::runtime_error&){assert(path==2);}}
  assert(muteState&&mute.HandoffGeneration());mute.CancelHandoff();
 }
 create=configured=true;throwConfigure=false;
 // Both legacy and independent AML rendering retain active/pending masks.
 selectAML=true;
 for(bool independent:{false,true}){
  hold=mute.Acquire();mute.Handoff(hold,deadline);
  {CRenderManager r;r.m_amlIndependentPresenter=independent;assert(r.Configure());}
  assert(muteState&&mute.HandoffGeneration());mute.CancelHandoff();
 }
 // Other active decoder owners are not canceled by a software successor.
 selectAML=false;hold=mute.Acquire();auto other=mute.Acquire();
 mute.Handoff(hold,deadline);{CRenderManager r;assert(r.Configure());}
 assert(muteState);mute.Release(other);assert(!muteState);
 std::cout<<"PASS: production non-AML configure, late close, failure/exception and AML/active-owner preservation\n";
}
'''
def main():
 p=argparse.ArgumentParser();p.add_argument('--baseline');p.add_argument('--negative-controls',action='store_true');args=p.parse_args()
 s=subprocess.check_output(['git','show',args.baseline+':'+FILE],cwd=ROOT,text=True) if args.baseline else (ROOT/FILE).read_text()
 body=function(s,'bool CRenderManager::Configure()')
 def run(body,negative=False):
  with tempfile.TemporaryDirectory(prefix='aml-mute-renderer-') as tmp:
   d=Path(tmp);(d/'test.cpp').write_text(PRE+body+TEST)
   subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie','-I',str(ROOT/'xbmc'),str(d/'test.cpp'),'-o',str(d/'test')],check=True)
   r=subprocess.run([str(d/'test')],capture_output=True,text=True,timeout=20)
   if negative:assert r.returncode and 'Assertion' in r.stderr,r.stdout+r.stderr
   else:assert not r.returncode,r.stdout+r.stderr;print(r.stdout.strip())
 run(body)
 if args.negative_controls:
  for label,old,new in [('omit successor cancellation','      aml_video_mute_controller().CancelHandoff();','      (void)0;'),('cancel AML too','if (!aml)','if (true)'),('cancel before configure success','  if (result)\n  {','  aml_video_mute_controller().CancelHandoff();\n  if (result)\n  {')]:
   assert old in body,label;run(body.replace(old,new,1),True);print('REJECTED:',label)
if __name__=='__main__':main()
