#!/usr/bin/env python3
"""Actual lease controller and session ownership; ioctl/render hardware modeled."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
def function(source, signature):
    a=source.index(signature); b=source.index('{',a); depth=1;e=b+1
    while depth:
        depth+=(source[e]=='{')-(source[e]=='}');e+=1
    return source[a:e]

PRELUDE=r'''
#include "windowing/amlogic/AMLNativeGuiWait.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <cassert>
#include <future>
#include <iostream>
#include <memory>
#include <vector>
using namespace KODI::WINDOWING::AML;
struct FakeIO {
 static inline int opens=0,creates=0,sets=0,closed=0,nextLease=20,mode=0;
 static inline bool held=false;static inline std::vector<uint32_t> values;
 static int Open(const std::string&p){assert(p=="/dev/fb2");++opens;if(mode==3){errno=EACCES;return -1;}return 10;}
 static int Create(int fd,int32_t*out){assert(fd==10);++creates;if(mode==1)return 0;if(mode==2){errno=ENOTTY;return -1;}*out=nextLease++;return 0;}
 static int Set(int fd,uint32_t*v){assert(fd>=20);++sets;values.push_back(*v);if(mode==4){errno=EIO;return -1;}held=*v;return 0;}
 static void Close(int fd){assert(fd==10||fd>=20);++closed;}
};
struct Codec {CAMLSession session;auto GetDiagnostics()const{return session.Diagnostics();}};
struct Gate {
 std::shared_ptr<Codec> m_codec;
 @GATE@
};
@ACTIVATION@
int main(){
 assert(GUI_WAIT_CREATE==0x80044622UL&&GUI_WAIT_SET==0x40044623UL);
 NativeGuiWait<FakeIO> wait;
 assert(wait.Apply(false,"/dev/fb2")&&FakeIO::opens==0);
 assert(wait.Apply(true,"/dev/fb2")&&wait.Enabled()&&FakeIO::held);
 int calls=FakeIO::sets;for(int n=0;n<1000;++n)assert(wait.Apply(true,"/dev/fb2"));
 assert(FakeIO::sets==calls&&FakeIO::creates==1);
 // Explicit disable matters even with a hypothetical fork-retained descriptor.
 assert(wait.Apply(false,"/dev/fb2")&&!wait.Enabled()&&!FakeIO::held);
 assert(FakeIO::values.back()==0);
 for(int mode=1;mode<=4;++mode){FakeIO::mode=mode;wait.Reset();int attempts=FakeIO::opens;
 assert(!wait.Apply(true,"/dev/fb2")&&!wait.Enabled()&&wait.Error());
 for(int n=0;n<1000;++n)assert(!wait.Apply(true,"/dev/fb2"));
 assert(FakeIO::opens==attempts+1);assert(wait.Apply(false,"/dev/fb2"));}
 FakeIO::mode=0;
 auto codec=std::make_shared<Codec>();Gate gate{codec};
 assert(!gate.RequiresNativeGuiWait());
 std::promise<CAMLSession::OwnerTransfer> start,back;
 std::promise<void> accepted,stop;auto stopped=stop.get_future();
 auto main=std::this_thread::get_id();
 std::thread worker([&]{assert(codec->session.AcceptOwner(start.get_future().get()));accepted.set_value();stopped.wait();back.set_value(codec->session.RequestOwner(main));});
 auto request=codec->session.RequestOwner(worker.get_id());
 assert(request&&gate.RequiresNativeGuiWait()); // Outgoing transfer already fences synchronous poll.
 assert(wait.Apply(gate.RequiresNativeGuiWait(),"/dev/fb2")&&FakeIO::held);
 start.set_value(request);accepted.get_future().wait();
 assert(gate.RequiresNativeGuiWait());assert(wait.Apply(gate.RequiresNativeGuiWait(),"/dev/fb2"));
 codec->session.Fence();assert(gate.RequiresNativeGuiWait()); // Seek/decoder fence keeps worker ownership.
 // Surface teardown drops the lease; replacement re-evaluates original owner.
 assert(wait.Reset()&&!FakeIO::held);int creations=FakeIO::creates;
 assert(wait.Apply(gate.RequiresNativeGuiWait(),"/dev/fb2")&&FakeIO::creates==creations+1);
 stop.set_value();auto returned=back.get_future().get();worker.join();
 assert(gate.RequiresNativeGuiWait());assert(codec->session.AcceptOwner(returned));
 assert(!gate.RequiresNativeGuiWait());assert(wait.Apply(false,"/dev/fb2")&&!FakeIO::held);
 {NativeGuiWait<FakeIO> cleanup;assert(cleanup.Apply(true,"/dev/fb2"));}
 assert(!FakeIO::held);
 activationCases();
 std::cout<<"PASS protected startup/return, seek fence, fallback disable, surface replacement, shutdown lease, old-kernel handshake and bounded failure retries\n";
}
'''
ACTIVATION=r'''

#include <mutex>
struct CLog { template<class... T> static void Log(T&&...) {} };
constexpr int LOGERROR=0,LOGINFO=1;
constexpr int EGL_NO_SURFACE=0;
constexpr double DVD_MSEC_TO_TIME(int value){return value*1000.0;}
using CCriticalSection=std::recursive_mutex;
struct CWinSystemBase { @RESULT@; };
struct CWinSystemAmlogicGLESContext : CWinSystemBase {
 NativeGuiWait<FakeIO> m_nativeGuiWait;
 int m_nativeGuiWaitReportedError=0;
 bool m_shutdownRequested=false;
 struct GL {int surface=1;int GetEGLSurface(){return surface;}} m_pGLContext;
 std::string m_framebuffer_name="fb2";
 NativeGuiWaitResult SetNativeGuiWait(bool enabled);
 struct Gfx {double GetFPS(){return 24.0;}} gfx;
 Gfx& GetGfxContext(){return gfx;}
};
@WINDOW@
struct CServiceBroker {
 static inline CWinSystemAmlogicGLESContext* window=nullptr;
 static auto* GetWinSystem(){return window;}
};
struct CRect {};
struct CRendererAML {int PrepareIndependentControl(CRect&,CRect&){return 1;}};
struct ActivationSession : Gate {
 struct Queue {void SetControl(int){} int Skipped(){return 0;}};
 std::shared_ptr<Queue> queue=std::make_shared<Queue>();
 CAMLSession::OwnerTransfer token;
 bool authorized=false;
 void Authorize(){assert(FakeIO::held);authorized=true;}
 void SetTiming(double,double,double){}
 void ApplyControl(const CRect&,const CRect&){}
};
struct CRenderManager {
 std::shared_ptr<ActivationSession> m_amlPresenter;
 CCriticalSection m_statelock,m_presentlock;
 CRendererAML renderer; CRendererAML* m_pRenderer=&renderer;
 float m_fps=24.0f;int m_latencyTweak=0,m_audioLatencyTweak=0,m_videoDelay=0,m_QueueSkip=0;
 int fallbacks=0;
 void LogAMLPresenter(const char*,bool=false){}
 void StopAMLPresenter(bool migrate){assert(migrate);++fallbacks;
   assert(m_amlPresenter->m_codec->session.CancelOwner(m_amlPresenter->token));
   CServiceBroker::window->SetNativeGuiWait(false);m_amlPresenter.reset();}
 bool UpdateAMLPresenter();
};
@UPDATE@
static void activationCases() {
 for(int condition=0;condition<4;++condition) {
   CWinSystemAmlogicGLESContext window;CServiceBroker::window=&window;
   CRenderManager manager;
   auto session=std::make_shared<ActivationSession>();
   session->m_codec=std::make_shared<Codec>();
   std::thread identity([]{});auto target=identity.get_id();identity.join();
   session->token=session->m_codec->session.RequestOwner(target);assert(session->token);
   manager.m_amlPresenter=session;
   FakeIO::mode=condition==2?2:0;
   if(condition==1)window.m_pGLContext.surface=0;
   if(condition==3)window.m_shutdownRequested=true;
   const bool kept=manager.UpdateAMLPresenter();
   if(condition==2){assert(!kept && manager.fallbacks==1 && !session->authorized && !FakeIO::held);}
   else if(condition==1 || condition==3){
     assert(kept && !session->authorized && !FakeIO::held);
     window.m_pGLContext.surface=1;window.m_shutdownRequested=false;
     assert(manager.UpdateAMLPresenter() && session->authorized && FakeIO::held);
     manager.StopAMLPresenter(true);
   } else {
     assert(kept && session->authorized && FakeIO::held);
     manager.StopAMLPresenter(true);
   }
 }
 FakeIO::mode=0;assert(!FakeIO::held);
}
'''

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    header=(ROOT/'xbmc/windowing/amlogic/AMLNativeGuiWait.h').read_text()
    session=(ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/HwDecRender/AMLPresenterSession.h').read_text()
    gate=function(session,'bool RequiresNativeGuiWait() const')
    manager=(ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    stop=function(manager,'void CRenderManager::StopAMLPresenter(')
    assert stop.index('m_amlPresenter->Stop()')<stop.index('SetNativeGuiWait(false)')<stop.index('m_amlPresenter.reset()')
    assert 'm_amlPresenter->RequiresNativeGuiWait()' in function(manager,'bool CRenderManager::UpdateAMLPresenter()')
    frame=function(manager,'void CRenderManager::FrameMove()')
    assert 'if (!m_amlPresenter)\n    CServiceBroker::GetWinSystem()->SetNativeGuiWait(false);' in frame
    window=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    destroy=function(window,'bool CWinSystemAmlogicGLESContext::DestroyWindow()')
    assert destroy.index('SetNativeGuiWait(false)')<destroy.index('DestroySurface()')
    assert 'SetNativeGuiWait(false)' in function(window,'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()')
    base=(ROOT/'xbmc/windowing/WinSystem.h').read_text()
    activation=ACTIVATION.replace('@RESULT@',function(base,'enum class NativeGuiWaitResult'))
    activation=activation.replace('@WINDOW@',function(window,'CWinSystemBase::NativeGuiWaitResult CWinSystemAmlogicGLESContext::SetNativeGuiWait('))
    activation=activation.replace('@UPDATE@',function(manager,'bool CRenderManager::UpdateAMLPresenter()'))
    with tempfile.TemporaryDirectory(prefix='native-gui-wait-') as tmp:
        out=Path(tmp);(out/'windowing/amlogic').mkdir(parents=True)
        def run(h,g,negative=False):
            (out/'windowing/amlogic/AMLNativeGuiWait.h').write_text(h)
            (out/'test.cpp').write_text(PRELUDE.replace('@GATE@',g).replace('@ACTIVATION@',activation))
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie','-I',str(out),'-I',str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            result=subprocess.run([str(out/'test')],capture_output=negative,text=True,timeout=10)
            if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result
            else:result.check_returncode()
        run(header,gate)
        if args.negative_controls:
            run(header,gate.replace('owner.transferring || !owner.mainOwner','!owner.mainOwner'),True)
            print('Rejected negative control: pending transfer left without GUI pacing')
            reset=function(header,'bool Reset()')
            changed=reset.replace('IO::Set(m_fd, &value)','(static_cast<void>(value), 0)')
            assert changed!=reset;run(header.replace(reset,changed),gate,True)
            print('Rejected negative control: close without explicit disable')
if __name__=='__main__':main()
