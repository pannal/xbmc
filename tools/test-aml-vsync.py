#!/usr/bin/env python3
"""Production GLES/AML VSync, surface transaction and present methods.
Real display/native lifecycle, recording EGL/GLES/GUI boundaries; no scanout proof.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
function=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
PRELUDE=r"""
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "windowing/amlogic/AMLNativeTransaction.h"
#include "utils/PlaybackEndDiagnostics.h"
#include <cassert>
#include <cmath>
#include <iostream>
#include <optional>
#include <string>
#include <vector>
using namespace PLAYBACK_DIAGNOSTICS;
constexpr int LOGINFO=1,LOGERROR=2,LOGDEBUG=3;
struct CLog {static inline int infos=0,errors=0;template<class... T>static void Log(int n,const char*,T&&...){infos+=n==LOGINFO;errors+=n==LOGERROR;}};
namespace fmt {template<class... T>std::string format(const char*,T&&...){return {};}}
enum class StreamHdrType{HDR_TYPE_SDR,HDR_TYPE_DOLBYVISION};
struct CStreamDetails{static std::string DynamicRangeToString(StreamHdrType){return {};}};
using RENDER_STEREO_MODE=int;constexpr int D3DPRESENTFLAG_MODEMASK=255,DV_MODE_OFF=0;
struct RESOLUTION_INFO{int iWidth=1280,iHeight=720,iScreenWidth=1280,iScreenHeight=720;float fRefreshRate=60;int dwFlags=0;};
static RESOLUTION_INFO current;
static bool readNative=true;
static bool aml_has_frac_rate_policy(){return false;}
static bool aml_get_native_resolution(RESOLUTION_INFO* r){*r=current;return readNative;}
static int aml_dv_mode(){return 1;}
static void aml_dv_wait_for_pipeline(){}
static bool aml_dv_restore_gui_ipt(const char*){return false;}
static void aml_dv_display_trigger(){}
static void aml_hdr10plus_vsif_hold(bool){}
static void aml_hdmi_link_probe(const char*){}
struct CSysfsPath{CSysfsPath(const char*){}template<class T>std::optional<T>Get(){return T{};}};
using EGLNativeWindowType=uintptr_t;
constexpr int EGL_NO_DISPLAY=0,EGL_NO_SURFACE=0,EGL_TRUE=1;
static int intervalCalls=0,eglError=1234,errorReads=0,swapCalls=0;
static bool intervalSuccess=true,swapSuccess=true;
static int eglSwapInterval(int,bool){++intervalCalls;return intervalSuccess;}
static int eglSwapBuffers(int,int){++swapCalls;return swapSuccess;}
static int eglGetError(){++errorReads;return std::exchange(eglError,0);}
struct CEGLContextUtils{
 struct SwapDiagnostics{bool attempted=false;int error=0;};
 int m_eglDisplay=1,m_eglSurface=1;bool bound=true,createSuccess=true,bindSuccess=true;
 int creates=0,binds=0;
 bool SetVSync(bool);bool TrySwapBuffers(SwapDiagnostics* diagnostics=nullptr);
 bool CreateSurface(EGLNativeWindowType){++creates;if(!createSuccess)return false;m_eglSurface=1;return true;}
 bool BindContext(){++binds;bound=bindSuccess;return bound;}
 void DestroySurface(){m_eglSurface=0;bound=false;}
};
struct Gfx{StreamHdrType hdr=StreamHdrType::HDR_TYPE_SDR;int GetStereoMode(){return 0;}StreamHdrType GetHDRType(){return hdr;}};
struct BrokerWindow{Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct CServiceBroker{static inline BrokerWindow window;static BrokerWindow* GetWinSystem(){return &window;}};
struct IDispResource{int resets=0;void OnResetDisplay(){++resets;}};
using CCriticalSection=std::mutex;
struct CWinSystemAmlogic{
 bool m_bWindowCreated=true,m_bFullScreen=true,m_force_mode_switch=false,m_delayDispReset=false;
 int m_stereo_mode=0;uintptr_t m_nativeWindow=1;
 StreamHdrType m_hdrType=StreamHdrType::HDR_TYPE_SDR;
 struct Timer{bool past=false;bool IsTimePast(){return past;}}m_dispResetTimer;
 CCriticalSection m_resourceSection;std::vector<IDispResource*>m_resources;
 Gfx& GetGfxContext(){return CServiceBroker::window.gfx;}
 bool CreateNativeWindow(const std::string&,bool,RESOLUTION_INFO& r,CAMLSession::DisplayRequest){current=r;m_bWindowCreated=true;return true;}
 bool DestroyWindow(){m_bWindowCreated=false;return true;}
};
enum class PresentResult{SKIPPED,TARGET_INVALID,SWAP_ACCEPTED,SWAP_FAILED};
struct Target{std::shared_ptr<int>identity;uint64_t generation;};
struct CRenderSystemGLES{
 bool m_bVsyncInit=false,m_bRenderCreated=true;
 uint64_t generation=1;std::shared_ptr<int>identity=std::make_shared<int>(1);
 PresentResult m_presentResult=PresentResult::SKIPPED;
 void SetVSync(bool);void ResetVSync(){m_bVsyncInit=false;}
 virtual void SetVSyncImpl(bool)=0;
 virtual bool CanRender(){return m_bRenderCreated;}
 bool ResetRenderSystem(int,int){m_bRenderCreated=true;return true;}
 Target CaptureRenderTarget(){return {identity,generation};}
 bool IsRenderTargetCurrent(const Target& t){return t.identity==identity&&t.generation==generation;}
 void InvalidateRenderTarget(){++generation;}
};
struct CWinSystemAmlogicGLESContext:CWinSystemAmlogic,CRenderSystemGLES{
 CAMLDisplayLifecycle m_displayLifecycle;CEGLContextUtils m_pGLContext;
 bool m_displayGeometryReady=true,m_shutdownRequested=false,m_vsyncFailureReported=false;
 int kernelSwitches=0;
 bool IsPrimaryContextCurrent(){return m_pGLContext.bound;}
 bool CanRender()override{return m_bRenderCreated&&m_pGLContext.bound&&m_pGLContext.m_eglSurface;}
 void CancelGuiComposite(){}void ApplyPendingKernelSwitch(){++kernelSwitches;}
 void ObserveEndDisplay(const char*,bool=false){}
 bool CreateNewWindow(const std::string&,bool,RESOLUTION_INFO&);bool DestroyWindow();
 bool ResetRenderSystem(int,int);bool SetFullScreen(bool,RESOLUTION_INFO&,bool);
 void SetVSyncImpl(bool)override;void PresentRenderImpl(bool);
};
"""
TESTS=r"""
static void resetCounters(){CLog::infos=CLog::errors=intervalCalls=errorReads=swapCalls=0;intervalSuccess=swapSuccess=true;eglError=1234;current={};CServiceBroker::window.gfx.hdr=StreamHdrType::HDR_TYPE_SDR;}
int main(){
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.SetVSync(true);assert(w.m_bVsyncInit&&intervalCalls==1&&CLog::infos==1);
 for(int i=0;i<1000;++i)w.SetVSync(true);
 assert(intervalCalls==1&&eglError==1234&&errorReads==0);
 }
 resetCounters();{
 CWinSystemAmlogicGLESContext w;intervalSuccess=false;
 for(int i=0;i<1000;++i)w.SetVSync(true);
 assert(!w.m_bVsyncInit&&intervalCalls==1000&&CLog::errors==1&&CLog::infos==0);
 assert(eglError==1234&&errorReads==0);
 intervalSuccess=true;w.SetVSync(true);assert(w.m_bVsyncInit&&intervalCalls==1001&&CLog::infos==1);
 for(int i=0;i<1000;++i){w.SetVSync(true);}
 assert(intervalCalls==1001);
 }
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.SetVSync(true);RESOLUTION_INFO same;
 auto before=w.generation;assert(w.SetFullScreen(true,same,false));
 assert(w.m_bVsyncInit&&w.m_pGLContext.creates==0&&w.generation==before);
 w.SetVSync(true);assert(intervalCalls==1&&w.m_displayLifecycle.Ready());
 RESOLUTION_INFO different;different.iWidth=different.iScreenWidth=1920;
 assert(w.SetFullScreen(true,different,false));
 assert(!w.m_bVsyncInit&&w.m_pGLContext.creates==1&&w.generation>before);
 w.SetVSync(true);assert(w.m_bVsyncInit&&intervalCalls==2&&w.m_displayLifecycle.Ready());
 w.SetVSync(true);assert(intervalCalls==2);
 }
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.SetVSync(true);w.m_pGLContext.bindSuccess=false;
 RESOLUTION_INFO r;r.iWidth=1920;assert(!w.SetFullScreen(true,r,false));
 assert(!w.m_displayLifecycle.Ready()&&!w.CanRender()&&w.m_bVsyncInit);
 w.m_pGLContext.bindSuccess=true;assert(w.SetFullScreen(true,r,false));
 assert(!w.m_bVsyncInit);w.SetVSync(true);assert(intervalCalls==2);
 }
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.m_delayDispReset=true;
 RESOLUTION_INFO r;r.iWidth=1920;assert(w.SetFullScreen(true,r,false));
 assert(!w.m_displayLifecycle.Ready());intervalSuccess=false;
 IDispResource resource;w.m_resources.push_back(&resource);
 w.m_dispResetTimer.past=true;w.SetVSync(true);w.PresentRenderImpl(false);
 assert(!w.m_bVsyncInit&&!w.m_delayDispReset&&w.m_displayLifecycle.Ready()&&resource.resets==1);
 assert(swapCalls==0&&w.kernelSwitches==0);
 intervalSuccess=true;w.SetVSync(true);w.PresentRenderImpl(true);
 assert(w.m_bVsyncInit&&swapCalls==1&&w.kernelSwitches==1);
 }
 // A deferred display admission changes neither interval cache nor target.
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.SetVSync(true);CAMLSession decoder;
 auto request=decoder.Fence();assert(decoder.BeginMutation(request));assert(decoder.Complete(request,true));
 RESOLUTION_INFO r;r.iWidth=1920;
 {auto permit=decoder.AcquireDecoder();assert(permit);
  assert(!w.SetFullScreen(true,r,false)&&w.m_bVsyncInit&&w.m_pGLContext.creates==0);}
 assert(w.SetFullScreen(true,r,false)&&!w.m_bVsyncInit);
 w.SetVSync(true);assert(intervalCalls==2&&w.m_displayLifecycle.Ready());
 }
 resetCounters();{
 CWinSystemAmlogicGLESContext w;w.m_bRenderCreated=false;w.SetVSync(true);
 assert(!w.m_bVsyncInit&&intervalCalls==0);
 w.m_bRenderCreated=true;w.m_pGLContext.m_eglDisplay=EGL_NO_DISPLAY;
 w.SetVSync(true);assert(!w.m_bVsyncInit&&intervalCalls==0&&errorReads==0);
 }
 // Default and successful EGL calls preserve the existing error consumer.
 resetCounters();CEGLContextUtils egl;swapSuccess=false;
 assert(!egl.TrySwapBuffers()&&errorReads==0&&eglError==1234);
 CEGLContextUtils::SwapDiagnostics d;assert(!egl.TrySwapBuffers(&d)&&d.attempted&&d.error==1234&&errorReads==1);
 eglError=5678;swapSuccess=true;assert(egl.TrySwapBuffers(&d)&&errorReads==1&&eglError==5678);
 std::cout<<"PASS production VSync acceptance/cache, failure/retry/log cap, surface/same-window, delayed readiness and EGL error ownership\n";
}
"""
def harness():
    sources={
      'xbmc/rendering/gles/RenderSystemGLES.cpp':['void CRenderSystemGLES::SetVSync('],
      'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp':[
        'bool CWinSystemAmlogicGLESContext::CreateNewWindow(',
        'bool CWinSystemAmlogicGLESContext::DestroyWindow()',
        'bool CWinSystemAmlogicGLESContext::SetFullScreen(',
        'bool CWinSystemAmlogicGLESContext::ResetRenderSystem(',
        'void CWinSystemAmlogicGLESContext::SetVSyncImpl(',
        'void CWinSystemAmlogicGLESContext::PresentRenderImpl('],
      'xbmc/utils/EGLUtils.cpp':['bool CEGLContextUtils::SetVSync(', 'bool CEGLContextUtils::TrySwapBuffers(']}
    return PRELUDE+'\n'.join(function((ROOT/path).read_text(),sig) for path,sigs in sources.items() for sig in sigs)+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=harness()
    with tempfile.TemporaryDirectory(prefix='aml-vsync-') as tmp:
        p=Path(tmp)
        def run(code,expected=None):
            (p/'test.cpp').write_text(code)
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
              '-Wno-unused-parameter','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I',str(ROOT/'xbmc'),
              str(p/'test.cpp'),'-o',str(p/'test')],check=True)
            result=subprocess.run([str(p/'test')],capture_output=bool(expected),text=True,timeout=15)
            if expected:
                assert result.returncode!=0 and expected in result.stderr,result
                print('Rejected negative control:',expected)
            else:result.check_returncode()
        run(code)
        if args.negative_controls:
            controls=[
              ('    ResetVSync(); // The void GLES backend hook must not cache a rejected setup.','', '!w.m_bVsyncInit'),
              ('  ResetVSync(); // A newly bound EGL surface needs its own interval initialization.','', '!w.m_bVsyncInit&&w.m_pGLContext.creates==1'),
              ('    if (!m_vsyncFailureReported)','    if (true)','CLog::errors==1'),
              ('  if (m_bVsyncInit)\n  {','  if (true)\n  {','CLog::infos==0'),
              ('  if (m_bVsyncInit)\n    return;','', 'intervalCalls==1&&eglError==1234')]
            for before,after,expected in controls:
                assert code.count(before)==1,before
                run(code.replace(before,after),expected)
if __name__=='__main__':main()
