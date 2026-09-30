#!/usr/bin/env python3
"""Bounded production observer and EGL failure-boundary checks; no device proof."""
import os
from pathlib import Path
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]

def function(source, signature):
    start=source.index(signature); end=source.index('{',start)+1; depth=1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[start:end]

HARNESS=r'''
#include "utils/PlaybackEndDiagnostics.h"
#include <cassert>
#include <vector>
#include <iostream>
using PLAYBACK_DIAGNOSTICS::EndDisplayTrace;
using EGLint=int;
constexpr int EGL_SUCCESS=0x3000,EGL_NO_DISPLAY=0,EGL_NO_SURFACE=0,EGL_TRUE=1;
static int swaps=0,errors=0;static bool accepted=true;
int eglSwapBuffers(int,int){++swaps;return accepted;}
int eglGetError(){++errors;return 0x300d;}
struct CEGLContextUtils {
 struct SwapDiagnostics {bool attempted=false;EGLint error=EGL_SUCCESS;};
 int m_eglDisplay=1,m_eglSurface=1;
 bool TrySwapBuffers(SwapDiagnostics* diagnostics=nullptr);
};
@SWAP@
int main(){
 CEGLContextUtils gl;CEGLContextUtils::SwapDiagnostics result;
 assert(gl.TrySwapBuffers(&result)&&result.attempted&&result.error==EGL_SUCCESS&&errors==0);
 accepted=false;assert(!gl.TrySwapBuffers()&&errors==0); // unchanged default consumer
 assert(!gl.TrySwapBuffers(&result)&&result.attempted&&result.error==0x300d&&errors==1);
 gl.m_eglSurface=0;const int before=swaps;
 assert(!gl.TrySwapBuffers(&result)&&!result.attempted&&result.error==EGL_SUCCESS&&swaps==before&&errors==1);
 EndDisplayTrace trace;std::vector<EndDisplayTrace::Entry> records;
 auto drain=[&]{trace.Drain([&](const auto& e){records.push_back(e);});};
 int formatted=0;auto detail=[&]{++formatted;return std::string("detail");};
 trace.Record(1,"inactive",detail);assert(formatted==0);
 trace.Generation(10);trace.Begin(100,"ended");
 assert(std::string(trace.SnapshotReason(100))=="first-pump");
 for(int i=0;i<10000;++i){
   trace.Record(101+i,"repeated",detail);
   trace.Frame(101+i,10,1,false,0,10025,false,false,false,detail);
 }
 assert(formatted==32);
 // First draw/swap after detail exhaustion still gets a reserved generation marker.
 trace.Frame(20000,10,4,true,5,10025,false,true,false,detail);
 assert(std::string(trace.SnapshotReason(20000))=="first-gui-swap");
 assert(trace.SnapshotReason(20001)==nullptr);
 trace.Generation(11);trace.Frame(20002,11,4,true,5,10025,false,true,false,detail);
 assert(trace.SnapshotReason(5000100)==nullptr);
 trace.SwapResult(20003,true,false,0x300d);
 trace.Record(20004,"delay-expired",detail,true);
 trace.Expire(5000100);drain();
 assert(records.size()<=42&&std::string(records.back().kind)=="summary");
 assert(records.back().detail.find("suppressed=")!=std::string::npos);
 assert(records.back().detail.find("swap_failures=1")!=std::string::npos);
 assert(records.back().detail.find("last_egl_error=12301")!=std::string::npos);
 bool expiry=false;for(const auto& e:records)expiry|=std::string(e.kind)=="delay-expired";assert(expiry);
 bool first=false,second=false;
 for(const auto& e:records)if(std::string(e.kind)=="first-generation-gui-swap"){
  first|=e.display==10;second|=e.display==11;}
 assert(first&&second);
 // Production schedule: target 11 captured; AfterRender replaces it with 12;
 // Render reports CANCELLED with GUI commands issued to 11, no swap.
 records.clear();trace.Begin(5100000,"ended");trace.Generation(12);
 trace.Frame(5100001,11,2,true,0,10025,false,false,false,detail);
 trace.Expire(10100000);drain();
 bool oldDraw=false,newDraw=false;
 for(const auto& e:records)if(std::string(e.kind)=="first-generation-draw"){
   oldDraw|=e.display==11;newDraw|=e.display==12;}
 assert(oldDraw&&!newDraw);
 assert(records.back().detail.find("last_display_draw=0")!=std::string::npos);
 // No draw, no present and even no intermediate pump must still terminate with evidence.
 records.clear();trace.Begin(11000000,"stopped");
 assert(std::string(trace.SnapshotReason(16000000))=="timeout-no-gui-swap");
 trace.Expire(16000000);drain();
 assert(records.size()==2&&records.back().detail.find("first_gui_swap_us=0")!=std::string::npos);
 // Rapid replay/end supersedes the prior trace; queue stays bounded, loss explicit.
 records.clear();for(int i=0;i<100;++i)trace.Begin(17000000+i,"ended");drain();
 assert(records.size()==EndDisplayTrace::QUEUE_LIMIT&&trace.TakeLost()>0&&trace.TakeLost()==0);
 for(size_t i=1;i<records.size();++i)assert(records[i].trace>=records[i-1].trace);
 // Present state includes display identity even if all outcomes are identical.
 assert(trace.PresentChanged(17000200,true,5,true,false));
 assert(!trace.PresentChanged(17000201,true,5,true,false));
 trace.Generation(13);assert(trace.PresentChanged(17000202,true,5,true,false));
 trace.Begin(20000000,"ended");trace.Frame(20000001,13,4,true,5,10025,false,true,false,detail);
 assert(std::string(trace.SnapshotReason(20000002))=="first-pump-gui-swap");
 assert(trace.SnapshotReason(20000003)==nullptr&&trace.SnapshotReason(25000000)==nullptr);
 std::cout<<"PASS bounded events/snapshots, late generation milestones, absent-render timeout, rapid replay loss and EGL error ownership\n";
}
'''

def main():
    source=(ROOT/'xbmc/utils/EGLUtils.cpp').read_text()
    swap=function(source,'bool CEGLContextUtils::TrySwapBuffers(')
    with tempfile.TemporaryDirectory(prefix='end-display-') as temp:
        cpp=Path(temp)/'check.cpp';exe=Path(temp)/'check'
        cpp.write_text(HARNESS.replace('@SWAP@',swap))
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie','-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(exe)],check=True)
        env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'}
        subprocess.run([str(exe)],check=True,env=env)
        # Each mutation must break a runtime assertion, not merely compilation.
        mutations={
          'consume-default-error':swap.replace('if (!success)','if (!success)').replace('  if (diagnostics)\n  {\n    diagnostics->attempted', '  if (!diagnostics) eglGetError();\n  if (diagnostics)\n  {\n    diagnostics->attempted'),
          'miss-failure-error':swap.replace('diagnostics->error = eglGetError();','diagnostics->error = EGL_SUCCESS;'),
        }
        for name,mutant in mutations.items():
            assert mutant!=swap
            cpp.write_text(HARNESS.replace('@SWAP@',mutant))
            subprocess.run(['g++','-std=c++17','-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(exe)],check=True)
            result=subprocess.run([str(exe)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            assert result.returncode!=0,name
            print('REJECTED',name)
    # Wiring evidence supplements the isolated observer; not a full GUI integration test.
    app=(ROOT/'xbmc/application/Application.cpp').read_text()
    run=function(app,'int CApplication::Run()')
    render=function(app,'void CApplication::Render()')
    assert render.index('m_lastRenderDisplay = PLAYBACK_DIAGNOSTICS::endDisplay.Display()') < render.index('AfterRender();')
    assert 'diagnosticRenderAttempted ? m_lastRenderDisplay : 0' in run
    assert run.index('Render();') < run.index('aml_end_display_diagnostics_pump();')
    assert run.index('aml_end_display_diagnostics_pump();') < run.index('// A clean GUI')
    aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    pump=function(aml,'void aml_end_display_diagnostics_pump()')
    assert '.Set(' not in pump and 'Sleep' not in pump and 'CDVCoreGuard' not in pump
    print('PASS deferred pump and read-only snapshot wiring')
if __name__=='__main__':main()
