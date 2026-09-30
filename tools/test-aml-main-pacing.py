#!/usr/bin/env python3
"""Execute production application loop and GLES/AML present branches with synthetic time.
No full GUI, real input backend, OS CPU measurement or target proof.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(path, signature):
    source = (ROOT / path).read_text()
    start = source.index(signature)
    body = source.index('{', start)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


HARNESS = r"""
#include <cassert>
#include <chrono>
#include <memory>
#include <mutex>
#include <vector>
#include <iostream>
#include "rendering/RenderResource.h"
#include "utils/PlaybackEndDiagnostics.h"
namespace fmt {template<class... T>std::string format(const char*,T...){return {};}}
namespace PLAYBACK_DIAGNOSTICS {static uint64_t NowUs(){return 0;}}
inline void aml_end_display_diagnostics_pump(){}
struct CEGLContextUtils {struct SwapDiagnostics {bool attempted=false;int error=0;};};
using namespace std::chrono_literals;
struct Clock {
 static inline std::chrono::steady_clock::time_point now{};
 static auto Now(){return now;}
 static auto Us(){return std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();}
};
static int sleeps,swaps,iterations,inputs;
static long long waited,inputAt=-1,inputHandled=-1;
static bool independent=true,renderGUI=true,dirty=false,videoLayer=true,stopInFrame=false,reject=false;
static int workUs=0;
static bool fallBack=false,inputDirty=false;
namespace KODI::TIME {void Sleep(std::chrono::milliseconds d){++sleeps;waited+=d.count()*1000;Clock::now+=d;}}
constexpr int LOGINFO=0,TMSG_PLAYLISTPLAYER_PLAY=0;
struct CLog {template<class... T>static void Log(int,const char*,T...) {}};
namespace PLAYLIST {enum Id{TYPE_MUSIC};}
struct CFileItemList{int Size(){return 0;}};
struct Params{CFileItemList list;CFileItemList& GetPlaylist(){return list;}};
struct Playlist{void Add(PLAYLIST::Id,CFileItemList&){} void SetCurrentPlaylist(PLAYLIST::Id){}};
struct Messenger{void PostMsg(int,int){}};
struct Gfx {bool IsFullScreenVideo(){return true;}};
struct Window {Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct Manager {int GetActiveWindow(){return 10025;}};
struct Gui {Manager manager;Manager& GetWindowManager(){return manager;}};
struct CServiceBroker{
 static Gui* GetGUI(){static Gui gui;return &gui;}
 static Window* GetWinSystem(){static Window window;return &window;}
 static Params* GetAppParams(){static Params p;return &p;}
 static Playlist& GetPlaylistPlayer(){static Playlist p;return p;}
 static Messenger* GetAppMessenger(){static Messenger p;return &p;}
};
struct CApplicationPowerHandling {bool GetRenderGUI(){return renderGUI;}};
struct CRenderManager {std::shared_ptr<int> m_amlPresenter;bool IsVideoPresentationIndependent()const;};
struct IPlayer {virtual ~IPlayer()=default;virtual bool IsVideoPresentationIndependent()const{return false;}};
struct CVideoPlayer:IPlayer {CRenderManager m_renderManager;bool IsVideoPresentationIndependent()const override;};
struct CApplicationPlayer {
 std::shared_ptr<IPlayer> player;
 std::shared_ptr<const IPlayer> GetInternal()const{return player;}
 bool IsVideoPresentationIndependent()const;
};
@QUERIES@
struct IDispResource {void OnResetDisplay(){}};
static void aml_hdmi_link_probe(const char*){}
static void aml_hdr10plus_vsif_hold(bool){}
using CCriticalSection=std::mutex;
struct CWinSystemAmlogicGLESContext {
 bool m_delayDispReset=false,m_displayGeometryReady=true;
 struct Timer {bool IsTimePast(){return false;}}m_dispResetTimer;
 CCriticalSection m_resourceSection;
 std::vector<IDispResource*>m_resources;
 struct Life {void Resume(){}bool Ready(){return true;}uint64_t Serial(){return 1;}}m_displayLifecycle;
 struct GL {bool TrySwapBuffers(CEGLContextUtils::SwapDiagnostics* = nullptr){++swaps;Clock::now+=10ms;return true;}}m_pGLContext;
 PresentResult m_presentResult=PresentResult::NOT_ATTEMPTED;
 int CaptureRenderTarget(){return 1;} bool CanRender(){return true;}
 bool IsRenderTargetCurrent(int){return true;}void ApplyPendingKernelSwitch(){}
 void ObserveEndDisplay(const char*,bool = false){}
 void PresentRenderImpl(bool rendered);
};
@AML@
struct CRenderSystemGLES:CWinSystemAmlogicGLESContext{
 void SetVSync(bool){}void PresentRender(bool rendered,bool videoLayer);
};
@GLES@
struct CApplication {
 bool m_bStop=false;int m_ExitCode=0;
 RenderAttemptResult m_lastRenderAttempt;
 uint64_t m_lastRenderDisplay=1,m_lastRenderTargetGeneration=1;
 CApplicationPlayer player;CApplicationPowerHandling power;CRenderSystemGLES renderer;
 CApplication(){auto v=std::make_shared<CVideoPlayer>();if(independent)v->m_renderManager.m_amlPresenter=std::make_shared<int>(1);player.player=v;}
 template<class T>T* GetComponent(){if constexpr(std::is_same_v<T,CApplicationPlayer>)return &player;else return &power;}
 void Process(){++iterations;Clock::now+=100us;if(Clock::Us()>=1000000)m_bStop=true;assert(iterations<=10001);}
 void FrameMove(bool events,bool gui){
  assert(events&&gui==renderGUI);
  if(inputAt>=0&&inputHandled<0&&Clock::Us()>=inputAt){++inputs;inputHandled=Clock::Us();if(inputDirty)dirty=true;}
  Clock::now+=std::chrono::microseconds(workUs);
  if(fallBack)std::static_pointer_cast<CVideoPlayer>(player.player)->m_renderManager.m_amlPresenter.reset();
  if(stopInFrame)m_bStop=true;
 }
 void Render(){
  m_lastRenderAttempt={};
  if(reject){m_lastRenderAttempt.status=RenderAttemptStatus::BEGIN_REJECTED;return;}
  renderer.PresentRender(dirty,videoLayer);
  m_lastRenderAttempt={RenderAttemptStatus::COMMANDS_COMPLETED,dirty,renderer.m_presentResult};
 }
 void Cleanup(){}int Run();
};
@RUN@
static void reset(){Clock::now={};sleeps=swaps=iterations=inputs=0;waited=0;inputAt=inputHandled=-1;
 independent=renderGUI=videoLayer=true;dirty=stopInFrame=reject=fallBack=inputDirty=false;workUs=0;}
int main(){
#ifndef HAS_LIBAMCODEC
 CRenderManager manager;manager.m_amlPresenter=std::make_shared<int>(1);
 assert(!manager.IsVideoPresentationIndependent());
 std::cout<<"PASS non-AML default remains synchronous\n";return 0;
#endif
 PLAYBACK_DIAGNOSTICS::endDisplay.Begin(0,"ended");
 reset();CApplication idle;idle.Run();
 std::cout<<"clean independent: iterations="<<iterations<<" sleep_us="<<waited<<" swaps="<<swaps<<std::endl;
 assert(iterations>=66&&iterations<=73&&sleeps==iterations-1&&swaps==0&&waited>=980000);
 reset();inputAt=153;inputDirty=true;CApplication input;input.Run();
 // Existing millisecond budget rounds elapsed work down; include 100 us of
 // work on each side of the wait, distinct from real OS/input-backend latency.
 assert(inputs==1&&inputHandled-inputAt<=15200&&swaps>0&&sleeps==1);
 reset();workUs=20000;CApplication busy;busy.Run();assert(sleeps==0);
 reset();workUs=5000;CApplication partial;partial.Run();assert(sleeps>0&&waited==sleeps*10000);
 reset();dirty=true;CApplication gui;gui.Run();assert(swaps>0&&sleeps==0);
 reset();independent=false;CApplication legacy;legacy.Run();assert(iterations==10000&&sleeps==0);
 reset();fallBack=true;CApplication fallback;fallback.Run();assert(sleeps==0);
 reset();stopInFrame=true;CApplication stopped;stopped.Run();assert(sleeps==0&&swaps==0&&iterations==1);
 reset();reject=true;CApplication cancelled;cancelled.Run();assert(sleeps==0);
 reset();renderGUI=false;CApplication hidden;hidden.Run();assert(sleeps>0&&iterations<=73);
 reset();videoLayer=false;CApplication noVideo;noVideo.Run();assert(iterations<=26&&sleeps==iterations-1);
 CApplicationPlayer empty;assert(!empty.IsVideoPresentationIndependent());
 IPlayer other;assert(!other.IsVideoPresentationIndependent());
 std::cout<<"PASS clean-loop pacing, input/dirty wake, elapsed budget, fallback, stop and existing presentation branches\n";
}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-control', action='store_true')
    args = parser.parse_args()
    run = function('xbmc/application/Application.cpp', 'int CApplication::Run()')
    run = run.replace('std::chrono::steady_clock::now()', 'Clock::Now()')
    queries = '\n'.join(function(path, signature) for path, signature in [
        ('xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp', 'bool CRenderManager::IsVideoPresentationIndependent() const'),
        ('xbmc/cores/VideoPlayer/VideoPlayer.cpp', 'bool CVideoPlayer::IsVideoPresentationIndependent() const'),
        ('xbmc/application/ApplicationPlayer.cpp', 'bool CApplicationPlayer::IsVideoPresentationIndependent() const')])
    # Ensure the observation is exposed through every production interface.
    for path in ['xbmc/cores/IPlayer.h', 'xbmc/cores/VideoPlayer/VideoPlayer.h',
                 'xbmc/application/ApplicationPlayer.h', 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.h']:
        assert 'IsVideoPresentationIndependent() const' in (ROOT/path).read_text()
    render = function('xbmc/application/Application.cpp', 'void CApplication::Render()')
    assert 'm_lastRenderAttempt.guiRendered = hasRendered;' in render
    assert 'm_lastRenderAttempt.present = renderSystem->GetPresentResult();' in render
    assert 'm_lastRenderAttempt.status = RenderAttemptStatus::COMMANDS_COMPLETED;' in render
    source = HARNESS.replace('@QUERIES@', queries).replace('@RUN@', run)
    source = source.replace('@AML@', function('xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp',
                                             'void CWinSystemAmlogicGLESContext::PresentRenderImpl(bool rendered)'))
    source = source.replace('@GLES@', function('xbmc/rendering/gles/RenderSystemGLES.cpp',
                                              'void CRenderSystemGLES::PresentRender(bool rendered, bool videoLayer)'))
    with tempfile.TemporaryDirectory(prefix='aml-main-pacing-') as tmp:
        out = Path(tmp)
        def check(code, negative=False, aml=True):
            (out/'test.cpp').write_text(code)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            *(['-DHAS_LIBAMCODEC'] if aml else []), '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-I'+str(ROOT/'xbmc'), str(out/'test.cpp'), '-o', str(out/'test')], check=True)
            result = subprocess.run([str(out/'test')],
                                    capture_output=negative, text=True, timeout=10)
            if negative:
                assert result.returncode != 0 and 'iterations>=66' in result.stderr, result
                print('Rejected negative control: restoring the GUI-enabled no-redraw pacing gap')
            else:
                result.check_returncode()
        check(source)
        check(source, aml=False)
        if args.negative_control:
            needle = '(!renderGUI || idleIndependentVideo)'
            assert source.count(needle) == 1
            check(source.replace(needle, '(!renderGUI || (idleIndependentVideo && false))'), True)


if __name__ == '__main__':
    main()
