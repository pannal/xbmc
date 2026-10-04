#!/usr/bin/env python3
"""Execute production queue, video Render routing/tail and AML Commit/Poll.

Only the unrelated GUI overlay/debug body is replaced by a counter; clock,
platform, permit and codec I/O are fake. No native timing or scanout proof.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
BASE = 'xbmc/cores/VideoPlayer/VideoRenderers/'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', help='execute unchanged production methods at revision')
    parser.add_argument('--negative-control', action='store_true')
    args = parser.parse_args()

    def read(path):
        if args.baseline:
            return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    rm = read(BASE + 'RenderManager.cpp')
    rh = read(BASE + 'RenderManager.h')
    aml = read(BASE + 'HwDecRender/RendererAML.cpp')
    codec = read('xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp')
    render = function(rm, 'void CRenderManager::Render(bool')
    # Retain complete production routing and completion state machine. GUI work
    # itself is orthogonal; its entry/exit and later video tail remain verbatim.
    start = render.index('  if (gui)\n  {')
    block = function(render[start:], '  if (gui)')
    render = render[:start] + '  if (gui) { ++overlays; }' + render[start + len(block):]
    fixed = 'bool CRenderManager::SubmitVideoDraw' in rm
    source = PRELUDE.replace('@DRAW@', function(rh, 'struct VideoRenderPass') + ';\n' +
                            function(rh, 'struct PreparedVideoDraw') + ';')
    source = source.replace('@SUBMIT_TYPE@', 'bool' if fixed else 'void')
    methods = [render, function(rm, 'CRenderManager::PreparedVideoDraw CRenderManager::PrepareVideoDraw(')]
    for name in ['ProcessPresentationQueue', 'PrepareNextRender', 'RetireBuffer', 'SelectFrame', 'DiscardBuffer']:
        methods.append(function(rm, 'void CRenderManager::' + name + '('))
    methods.append(function(rm, ('bool' if fixed else 'void') + ' CRenderManager::SubmitVideoDraw('))
    if fixed:
        methods.append(function(aml, 'bool CRendererAML::RenderUpdateVideo('))
        methods.append(function(aml, 'void CRendererAML::RenderUpdate('))
    else:
        methods.append(function(aml, 'void CRendererAML::RenderUpdate('))
    for name in ['Commit', 'Poll', 'ApplyGeometry']:
        methods.append(function(codec, 'void CAMLVideoBuffer::' + name + '('))
    # Use the producer's production wake condition with a minimal queue writer.
    producer = function(rm, 'bool CRenderManager::AddVideoPicture(')
    wake = function(producer, '  if (m_presentstep == PRESENT_IDLE)')
    source = source.replace('@WAKE@', wake)
    tests = TESTS
    if not fixed:
        begin = tests.index(' r.renderer.m_pollCodec->admit=false;\n assert(!r.renderer.RenderUpdateVideo')
        end = tests.index(' r.renderer.m_resumeControl=true;', begin)
        tests = tests[:begin] + tests[end:]
    source += '\n'.join(methods) + tests
    with tempfile.TemporaryDirectory(prefix='video-admission-') as temporary:
        out = Path(temporary)

        def check(code, mutant=None):
            (out / 'test.cpp').write_text(code)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
            result = subprocess.run([str(out / 'test')], capture_output=True, text=True)
            if mutant:
                assert result.returncode != 0 and 'Assertion' in result.stderr, result
                print('Rejected:', mutant)
            else:
                print(result.stdout, end='')
                if result.returncode:
                    print(result.stderr, end='')
                result.check_returncode()

        check(source)
        if args.negative_control:
            mutations = [
                ('GUI-only completion', 'if (!videoComplete)', 'if (false)'),
                ('empty queue clears pending frame', 'if (m_presentstep == PRESENT_READY)\n      m_presentstep = PRESENT_IDLE;',
                 'if (true)\n      m_presentstep = PRESENT_IDLE;'),
                ('discard cannot release stale selection',
                 '  m_presentstep = PRESENT_IDLE;\n  m_presentevent.notifyAll();\n}',
                 '  if (m_presentstep == PRESENT_READY) m_presentstep = PRESENT_IDLE;\n  m_presentevent.notifyAll();\n}'),
                ('obsolete selection stalls reset', 'return !codec || codec->IsOperationInvalidated(buffer ? buffer->OperationEpoch() : m_pollEpoch);', 'return !codec && false;'),
                ('denied native admission', 'return !codec || codec->IsOperationInvalidated(buffer ? buffer->OperationEpoch() : m_pollEpoch);', 'return true;'),
            ]
            for name, old, new in mutations:
                assert source.count(old) == 1, name
                # The mutated guard intentionally leaves the result unused.
                check(source.replace(old, new).replace('bool videoComplete = false;',
                      '[[maybe_unused]] bool videoComplete = false;'), name)


PRELUDE = r'''
#define HAS_LIBAMCODEC 1
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <deque>
#include <iostream>
#include <memory>
#include <mutex>
#include <vector>
using namespace std::chrono_literals;
using DWORD=unsigned int;using CCriticalSection=std::recursive_mutex;
constexpr int LOGERROR=0,LOGDEBUG=0,LOGAVTIMING=0;
constexpr int RENDER_FLAG_BOT=1,RENDER_FLAG_TOP=2,RENDER_FLAG_FIELD0=4,RENDER_FLAG_FIELD1=8,RENDER_FLAG_NOOSD=16;
constexpr double DVD_TIME_BASE=1000000;
double DVD_MSEC_TO_TIME(double x){return x*1000;}
struct CLog {template<class...T>static void Log(T...){}template<class...T>static void LogFC(T...){}};
struct Gfx{float GetFPS(){return 24.0f;}};
struct Window{Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct CServiceBroker{static Window* GetWinSystem(){static Window w;return &w;}};
struct CSingleExit{explicit CSingleExit(Gfx&) {}};
struct Event{void notifyAll(){}};
struct Timer{void Set(std::chrono::milliseconds){}};
struct CRect{};
struct CAMLSession{struct Permit{bool admitted=false;explicit operator bool()const{return admitted;}
 bool IsRetirement()const{return false;}bool IsControl()const{return false;}};};
struct CAMLCodec {
 uint64_t epoch=1;bool open=true;
 bool IsOperationInvalidated(uint64_t e){return !open||e!=epoch;}
 bool admit=true;int submissions=0,polls=0,geometry=0;std::vector<int> returned;
 CAMLSession::Permit AcquirePresentation(uint64_t e){return {admit&&open&&e==epoch};}
 bool IsPresentationPermit(const CAMLSession::Permit& p,uint64_t){return bool(p);}
 void ReleaseFrame(int i,uint64_t,const CAMLSession::Permit&){++submissions;returned.push_back(i);}
 void SetVideoRect(const CRect&,const CRect&,uint64_t,const CAMLSession::Permit&){++geometry;}
 void PollFrame(const CAMLSession::Permit&){++polls;}
};
struct CVideoBuffer{virtual ~CVideoBuffer()=default;};
struct CAMLVideoBuffer:CVideoBuffer {
 enum class Consumption{PENDING,CLAIMED,CONSUMED};std::atomic<Consumption>m_consumption{Consumption::PENDING};
 std::shared_ptr<CAMLCodec> m_codec;uint64_t m_operationEpoch=1,m_presentationGeneration=1;
 int m_omxPts=0,m_bufferIndex=0;bool m_submitted=false;
 auto Codec()const{return m_codec;}uint64_t OperationEpoch()const{return m_operationEpoch;}
 bool WasSubmitted()const{return m_submitted;}
 CAMLSession::Permit AcquirePresentation(){return m_codec->AcquirePresentation(m_operationEpoch);}
 void Commit(const CAMLSession::Permit&,const CRect&,const CRect&,int&);
 void Poll(const CAMLSession::Permit&)const;void ApplyGeometry(const CAMLSession::Permit&,const CRect&,const CRect&);
};
struct CRendererAML {
 struct Buffer{CVideoBuffer* videoBuffer=nullptr;};Buffer m_buffers[8];
 std::shared_ptr<CAMLCodec> m_pollCodec=std::make_shared<CAMLCodec>();
 uint64_t m_pollEpoch=1;int m_prevVPts=-1;bool m_resumeControl=false,gui=false;
 std::vector<int> released;
 struct PreparedVideoGeometry{CRect source,destination;};
 PreparedVideoGeometry PrepareVideoLayer(){return {};}
 bool IsGuiLayer(){return gui;}bool NeedBuffer(int){return false;}
 void ReleaseBuffer(int i){released.push_back(i);}
 void RenderUpdate(int,int,bool,unsigned int,unsigned int);
 bool RenderUpdateVideo(int,int,bool,unsigned int,unsigned int);
};
struct Overlays{void Release(int){}int GetOverlays(int){return 0;}};
struct Clock{double pts=0;double GetClock(){return pts;}double GetClockSpeed(){return 1;}void SetVsyncAdjust(double){}};
struct Cache{void SetRenderPts(double){}};
struct CRenderManager {
 enum EPRESENTSTEP{PRESENT_IDLE,PRESENT_FLIP,PRESENT_FRAME,PRESENT_FRAME2,PRESENT_READY};
 enum EPRESENTMETHOD{PRESENT_METHOD_SINGLE,PRESENT_METHOD_BLEND,PRESENT_METHOD_BOB};
 enum EFIELDSYNC{FS_NONE,FS_TOP,FS_BOT};enum State{STATE_CONFIGURED};
 struct SPresent{double pts=0;EFIELDSYNC presentfield=FS_NONE;EPRESENTMETHOD presentmethod=PRESENT_METHOD_SINGLE;};
 struct FrameSelection{int source,past;SPresent present;int overlays;};
 @DRAW@
 struct Presenter{struct Queue{void Discard(){}} storage;Queue* queue=&storage;};
 std::unique_ptr<Presenter>m_amlPresenter;void UpdateAMLPresenter(){}void InvalidateReservations(){}
 CRendererAML renderer;CRendererAML* m_pRenderer=&renderer;Overlays m_overlays;
 std::shared_ptr<const FrameSelection>m_frameSelection;
 CCriticalSection m_statelock,m_presentlock;Event m_presentevent;Timer m_presentTimer;
 State m_renderState=STATE_CONFIGURED;EPRESENTSTEP m_presentstep=PRESENT_IDLE;
 bool m_presentstarted=false,m_showVideo=true,m_forceNext=false,m_bRenderGUI=true;
 int m_presentsource=0,m_presentsourcePast=-1,m_lateframes=0,m_QueueSkip=0,overlays=0;
 double m_displayLatency=0,m_latencyTweak=0,m_audioLatencyTweak=0,m_videoDelay=0,m_presentpts=0;
 struct{bool m_enabled=false;double m_error=0,m_syncOffset=0;int m_errCount=0;}m_clockSync;
 Clock m_dvdClock;Cache m_dataCacheCore;SPresent m_Queue[8];std::deque<int>m_queued,m_discard,m_free;
 CAMLVideoBuffer buffers[8];
 CRenderManager(){for(int i=0;i<8;++i){buffers[i].m_codec=renderer.m_pollCodec;buffers[i].m_bufferIndex=i;
 buffers[i].m_omxPts=i;renderer.m_buffers[i].videoBuffer=&buffers[i];}}
 void DiscardBuffer();void ProcessPresentationQueue();void PrepareNextRender();void RetireBuffer(int);void SelectFrame();
 void Render(bool,DWORD,DWORD,bool);@SUBMIT_TYPE@ SubmitVideoDraw(const PreparedVideoDraw&);
 static PreparedVideoDraw PrepareVideoDraw(const FrameSelection&,EPRESENTSTEP,bool,DWORD,DWORD);
 void Queue(int i,double pts){m_Queue[i].pts=pts;m_queued.push_back(i);@WAKE@}
 void Pass(bool gui){Render(false,0,255,gui);}
};
'''

TESTS = r'''
int main(){
 using R=CRenderManager;
 // Fullscreen GUI can draw while RenderEx skips the hardware video pass.
 // Repeated main iterations must retain A until a video pass submits/polls it.
 {R r;r.Queue(1,0);r.Queue(2,41667);r.ProcessPresentationQueue();
 for(int i=0;i<4;++i){r.Pass(true);r.m_dvdClock.pts+=4000;r.ProcessPresentationQueue();
 assert(r.m_presentsource==1&&r.m_presentstep==R::PRESENT_FRAME);}
 assert(r.overlays==4&&r.renderer.m_pollCodec->polls==0&&r.renderer.released.empty());
 r.Pass(false);assert(r.renderer.m_pollCodec->returned==std::vector<int>{1});
 assert(r.renderer.m_pollCodec->polls==1&&r.m_presentstep==R::PRESENT_READY);
 r.m_dvdClock.pts=41667;r.ProcessPresentationQueue();r.Pass(false);
 assert(r.renderer.m_pollCodec->returned==std::vector<int>({1,2}));}
 // Sole selected frame, skipped/rejected Render, empty queue, producer refill.
 {R r;r.Queue(1,0);r.ProcessPresentationQueue();r.ProcessPresentationQueue();
 assert(r.m_presentstep==R::PRESENT_FRAME);
 r.Queue(2,41667);r.m_dvdClock.pts=41667;r.ProcessPresentationQueue();
 assert(r.m_presentsource==1&&r.renderer.released.empty());
 r.renderer.m_pollCodec->admit=false;r.Pass(false);
 assert(r.m_presentstep==R::PRESENT_FRAME&&r.renderer.m_pollCodec->polls==0);
 r.ProcessPresentationQueue();assert(r.m_presentsource==1);
 r.renderer.m_pollCodec->admit=true;r.Pass(false);assert(r.m_presentstep==R::PRESENT_READY);
 r.ProcessPresentationQueue();r.Pass(false);assert(r.renderer.m_pollCodec->returned==std::vector<int>({1,2}));}
 // Reset/reopen/close without DiscardBuffer must not pin an invalid epoch.
 for(bool closed:{false,true}){R r;r.Queue(1,0);r.ProcessPresentationQueue();
 auto old=r.renderer.m_pollCodec;
 if(closed)old->open=false;else ++old->epoch;
 r.Pass(false);assert(r.m_presentstep==R::PRESENT_IDLE&&old->polls==0);
 auto fresh=std::make_shared<CAMLCodec>();r.buffers[2].m_codec=fresh;
 r.Queue(2,41667);r.m_dvdClock.pts=41667;r.ProcessPresentationQueue();r.Pass(false);
 assert(fresh->returned==std::vector<int>{2});}
 // An explicit decoder flush cancels the old selected epoch, even if its
 // presentation permit will never return. Fresh queue work must replace it.
 for(auto step:{R::PRESENT_FRAME,R::PRESENT_FRAME2}){R r;r.Queue(1,0);r.ProcessPresentationQueue();
 r.m_presentstep=step;r.renderer.m_pollCodec->admit=false;r.Pass(false);
 r.DiscardBuffer();assert(r.m_presentstep==R::PRESENT_IDLE);
 r.Queue(2,41667);r.m_dvdClock.pts=41667;r.ProcessPresentationQueue();
 assert(r.m_presentsource==2);r.renderer.m_pollCodec->admit=true;r.Pass(false);
 assert(r.renderer.m_pollCodec->returned==std::vector<int>{2});}
 // Hardware GUI pass between BOB fields must not skip the second field;
 // GUI renderers instead advance on GUI work, never the hardware pass.
 for(bool gui:{false,true}){R r;r.renderer.gui=gui;r.Queue(1,0);r.m_Queue[1].presentmethod=R::PRESENT_METHOD_BOB;
 r.ProcessPresentationQueue();r.Pass(!gui);assert(r.m_presentstep==R::PRESENT_FRAME);
 r.Pass(gui);assert(r.m_presentstep==R::PRESENT_FRAME2);
 r.ProcessPresentationQueue();r.Pass(!gui);assert(r.m_presentstep==R::PRESENT_FRAME2);
 r.Pass(gui);assert(r.m_presentstep==R::PRESENT_IDLE&&r.renderer.m_pollCodec->polls==2);
 assert(r.renderer.m_pollCodec->submissions==1);}
 // Duplicate PTS and already-consumed redraw still pace and finish; QBUF is
 // not retried and its success is deliberately not an advancement criterion.
 {R r;r.Queue(1,0);r.ProcessPresentationQueue();r.Pass(false);r.Pass(false);
 assert(r.renderer.m_pollCodec->submissions==1&&r.renderer.m_pollCodec->polls==2);
 r.Queue(2,41667);r.buffers[2].m_omxPts=1;r.m_dvdClock.pts=41667;r.ProcessPresentationQueue();r.Pass(false);
 assert(r.m_presentstep==R::PRESENT_IDLE&&!r.buffers[2].WasSubmitted());
 assert(r.renderer.m_pollCodec->polls==3);}
 // Independent executor observation must never advance synchronous state.
 {R r;r.Queue(1,0);r.ProcessPresentationQueue();r.m_amlPresenter=std::make_unique<R::Presenter>();
 r.Pass(false);r.Pass(true);assert(r.m_presentstep==R::PRESENT_FRAME);
 assert(r.renderer.m_pollCodec->polls==0);}
 // BLEND runs both video passes before completing, including duplicate pacing.
 {R r;r.Queue(1,0);r.m_Queue[1].presentmethod=R::PRESENT_METHOD_BLEND;
 r.ProcessPresentationQueue();r.Pass(false);
 assert(r.m_presentstep==R::PRESENT_IDLE&&r.renderer.m_pollCodec->polls==2);}
 // Empty-slot redraw and independent handoff resume retain the admitted poll.
 {R r;r.renderer.m_buffers[0].videoBuffer=nullptr;
 r.renderer.RenderUpdate(0,0,false,0,255);assert(r.renderer.m_pollCodec->polls==1);
 r.renderer.m_pollCodec->admit=false;
 assert(!r.renderer.RenderUpdateVideo(0,0,false,0,255));
 ++r.renderer.m_pollCodec->epoch;
 assert(r.renderer.RenderUpdateVideo(0,0,false,0,255));
 r.renderer.m_pollCodec->epoch=1;r.renderer.m_pollCodec->admit=true;
 r.renderer.m_resumeControl=true;r.buffers[1].m_submitted=true;
 r.renderer.RenderUpdate(1,0,false,0,255);assert(r.renderer.m_pollCodec->submissions==0);
 assert(r.renderer.m_pollCodec->geometry==1&&r.renderer.m_pollCodec->polls==2);}
 std::cout<<"PASS production video routing, queue ownership, admission, fields and Commit/Poll (ASan/UBSan)\n";
}
'''

if __name__ == '__main__':
    main()
