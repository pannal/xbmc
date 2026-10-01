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
 assert(request&&!gate.RequiresNativeGuiWait()); // Starting presenter pointer cannot enable.
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
 std::cout<<"PASS acknowledged startup/return, seek fence, fallback disable, surface replacement, shutdown lease, old-kernel handshake and bounded failure retries\n";
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
    assert 'SetNativeGuiWait(m_amlPresenter->RequiresNativeGuiWait())' in function(manager,'void CRenderManager::UpdateAMLPresenter()')
    frame=function(manager,'void CRenderManager::FrameMove()')
    assert 'if (!m_amlPresenter)\n    CServiceBroker::GetWinSystem()->SetNativeGuiWait(false);' in frame
    window=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    destroy=function(window,'bool CWinSystemAmlogicGLESContext::DestroyWindow()')
    assert destroy.index('SetNativeGuiWait(false)')<destroy.index('DestroySurface()')
    assert 'SetNativeGuiWait(false)' in function(window,'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()')
    with tempfile.TemporaryDirectory(prefix='native-gui-wait-') as tmp:
        out=Path(tmp);(out/'windowing/amlogic').mkdir(parents=True)
        def run(h,g,negative=False):
            (out/'windowing/amlogic/AMLNativeGuiWait.h').write_text(h)
            (out/'test.cpp').write_text(PRELUDE.replace('@GATE@',g))
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie','-I',str(out),'-I',str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            result=subprocess.run([str(out/'test')],capture_output=negative,text=True,timeout=10)
            if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result
            else:result.check_returncode()
        run(header,gate)
        if args.negative_controls:
            run(header,gate.replace('!owner.mainOwner','(!owner.mainOwner || true)'),True)
            print('Rejected negative control: presenter pointer treated as acknowledged ownership')
            reset=function(header,'bool Reset()')
            changed=reset.replace('IO::Set(m_fd, &value)','(static_cast<void>(value), 0)')
            assert changed!=reset;run(header.replace(reset,changed),gate,True)
            print('Rejected negative control: close without explicit disable')
if __name__=='__main__':main()
