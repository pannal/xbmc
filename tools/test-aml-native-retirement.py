#!/usr/bin/env python3
"""Run production native admission, announcement continuation and teardown entry points.

Real AMLSession/display gates and extracted caller bodies; native, GL, DV policy,
services and announcement events are recording stubs. Includes a real cross-owner
retirement race. No target compilation or full application scheduling claim.
"""
import argparse
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PREFIX = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include <atomic>
#include <cassert>
#include <deque>
#include <functional>
#include <future>
#include <string>
#include <vector>
using namespace std::chrono_literals;
struct CVariant {};
namespace ANNOUNCEMENT { enum AnnouncementFlag {System,Player,Other}; }
using namespace ANNOUNCEMENT;
@INTERFACE@
std::vector<std::string> effects;
std::function<void()> nativeHook;
std::thread::id nativeOwner;
void aml_dv_start(){nativeOwner=std::this_thread::get_id();effects.push_back("dv-start");if(nativeHook)nativeHook();}
bool aml_dv_restore_gui_ipt(const char*){nativeOwner=std::this_thread::get_id();effects.push_back("dv-restore");if(nativeHook)nativeHook();return true;}
struct CDolbyVisionAML: IAnnouncer {
  enum class Pending { NONE, START, RESTORE };
  Pending m_pending{Pending::NONE};
  std::shared_ptr<CAMLSession::NativeRequest> m_nativeRequest;
  std::atomic<bool> m_retiring{false};
  bool ContinueAnnounce() override;
  void Retire();
  void Announce(AnnouncementFlag,const std::string&,const std::string&,const CVariant&) override;
};
using CCriticalSection=std::recursive_mutex;
struct CSingleExit {CCriticalSection& lock;explicit CSingleExit(CCriticalSection& l):lock(l){lock.unlock();}~CSingleExit(){lock.lock();}};
struct CThread {static inline std::function<void()> tick;static void Sleep(std::chrono::milliseconds delay){assert(delay==10ms);tick();}};
enum class ThreadPriority {LOWEST};
struct CAnnouncementManager {
  struct Event {std::function<void()> wait;void Wait(){wait();}} m_queueEvent;
  struct Message {AnnouncementFlag flag;std::string sender,message;int item=0;CVariant data{};};
  std::deque<Message> m_announcementQueue;
  std::vector<IAnnouncer*> m_announcers;
  CCriticalSection m_queueCritSection,m_announcersCritSection;
  bool m_bStop=false;int delivered=0;
  void SetPriority(ThreadPriority){} void Process();
  void DoAnnounce(AnnouncementFlag flag,const std::string& sender,const std::string& message,int,const CVariant& data){
    ++delivered;for(auto* listener:m_announcers)listener->Announce(flag,sender,message,data);
  }
};
struct CWinSystemAmlogic {
  CDolbyVisionAML dv;
  void RetireNativeTransactions(){dv.Retire();}
  bool InitWindowSystem(){effects.push_back("base-init");return true;}
  bool DestroyWindowSystem(){effects.push_back("base-destroy");return true;}
  bool DestroyWindow(){effects.push_back("window-destroy");return true;}
  int m_nativeDisplay=0;
};
constexpr int EGL_OPENGL_ES_API=1,EGL_OPENGL_ES2_BIT=2,EGL_CONTEXT_CLIENT_VERSION=3;
constexpr int OSD_PQ_PASSTHROUGH=1,DV_GRAPHIC_PQ=2;
void SetKernelSwitch(int,int){effects.push_back("switch");}
struct CEGLAttributesVec {void Add(std::initializer_list<std::pair<int,int>>) {}};
struct EGL {
  bool CreateDisplay(int){effects.push_back("display-create");return true;}
  bool InitializeDisplay(int){return true;}bool ChooseConfig(int){return true;}
  bool CreateContext(const CEGLAttributesVec&){effects.push_back("context-create");return true;}
  void DestroyContext(){effects.push_back("context-destroy");}
  void Destroy(){effects.push_back("display-destroy");}
  void DestroySurface(){effects.push_back("surface-destroy");}
};
struct CRenderSystemGLES {bool DestroyRenderSystem(){effects.push_back("render-destroy");return true;}};
struct CWinSystemAmlogicGLESContext:CWinSystemAmlogic,CRenderSystemGLES {
  CAMLDisplayLifecycle m_displayLifecycle;
  bool m_shutdownRequested=false,m_displayGeometryReady=true;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
  EGL m_pGLContext;
  enum class MenuRoute {NONE,OSD_VPP};MenuRoute m_menuRoute=MenuRoute::OSD_VPP;
  std::shared_ptr<int> m_compositeShader;
  void InvalidateRenderTarget(){effects.push_back("invalidate");}
  void ReleaseCompositeResources(){effects.push_back("composite-release");}
  void CloseTextureResources(){effects.push_back("textures-close");}
  void DisengageMenuComposite(){effects.push_back("menu-disengage");}
  void ApplyPendingKernelSwitch(){effects.push_back("shutdown-switch");}
  void CancelGuiComposite(){effects.push_back("composite-cancel");}
  bool InitWindowSystem();bool PrepareForShutdown();bool DestroyWindowSystem();
  bool DestroyWindow();bool DestroyRenderSystem();
};
@METHODS@
static void ready(){auto r=CAMLSession::FenceDisplay();assert(r&&CAMLSession::TryBeginDisplay(r));assert(CAMLSession::EndDisplay(r,CAMLSession::DisplayPhase::READY));}
static void open(CAMLSession& session){auto r=session.Fence();assert(session.BeginMutation(r));assert(session.Complete(r,true));}
'''
TESTS = r'''
static void admission(){
  ready();CAMLSession session;open(session);auto epoch=session.Epoch();
  auto permit=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());assert(*permit);
  auto original=CAMLSession::FenceNative();assert(original&&!CAMLSession::FenceNative());
  assert(!CAMLSession::TryBeginNative(original)&&!session.AcquireDecoder());
  assert(!session.Acquire(epoch,true));
  auto reset=session.Fence();assert(!session.BeginMutation(reset));
  CAMLSession newcomer;auto create=newcomer.Fence();assert(!newcomer.BeginMutation(create));
  permit.reset();assert(CAMLSession::TryBeginNative(original));
  auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
  assert(!CAMLSession::CancelNative(original));
  assert(CAMLSession::EndNative(original));assert(CAMLSession::TryBeginDisplay(display));
  auto next=CAMLSession::FenceNative();assert(next&&!CAMLSession::TryBeginNative(next));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::FAILED));
  assert(CAMLSession::TryBeginNative(next));assert(CAMLSession::EndNative(next));
  assert(!session.AcquireDecoder()); // Native completion never repairs a failed display.
  ready();auto cancelled=CAMLSession::FenceNative();assert(CAMLSession::CancelNative(cancelled));
  auto replacement=CAMLSession::FenceNative();
  assert(!CAMLSession::TryBeginNative(std::make_shared<CAMLSession::NativeRequest>()));
  assert(!CAMLSession::TryBeginNative(cancelled)&&!CAMLSession::EndNative(original));
  bool wrong=false;std::thread other([&]{wrong=CAMLSession::TryBeginNative(replacement);});other.join();assert(!wrong);
  assert(CAMLSession::CancelNative(replacement));
  assert(session.BeginMutation(reset)&&session.Complete(reset,true));
  assert(session.Epoch()==epoch+1);
}
static void announcement_retry(){
  ready();effects.clear();CAMLSession session;open(session);
  auto held=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
  CDolbyVisionAML dv;
  struct Listener:IAnnouncer {int calls=0;void Announce(AnnouncementFlag,const std::string&,const std::string&,const CVariant&)override{++calls;}} observer;
  CAnnouncementManager manager;manager.m_announcers={&dv,&observer};
  manager.m_announcementQueue={{System,"xbmc","OnWake"},{Player,"xbmc","OnStop"}};
  auto ticks=0;std::shared_ptr<CAMLSession::NativeRequest> receipt;
  CThread::tick=[&]{
    assert(manager.delivered==1&&observer.calls==1&&effects.empty());
    auto retained=std::atomic_load(&dv.m_nativeRequest);assert(retained);
    if(receipt){assert(receipt==retained);}receipt=retained;
    if(++ticks==3){held.reset();}assert(ticks<=3);
  };
  manager.m_queueEvent.wait=[&]{manager.m_bStop=true;};manager.Process();
  assert(ticks==3&&manager.delivered==2&&observer.calls==2);
  assert((effects==std::vector<std::string>{"dv-start","dv-restore"}));
  assert(nativeOwner==std::this_thread::get_id()&&session.AcquireDecoder());
  held=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
  dv.Announce(System,"","OnWake",{});auto cancelled=std::atomic_load(&dv.m_nativeRequest);
  assert(cancelled);dv.Retire();held.reset();assert(dv.ContinueAnnounce());
  assert(!CAMLSession::TryBeginNative(cancelled));
  dv.Announce(System,"","OnWake",{});assert(effects.size()==2);
}
static void standalone(){
  for(int stage=0;stage<4;++stage){
    ready();effects.clear();CAMLSession session;open(session);
    auto held=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
    CWinSystemAmlogicGLESContext window;
    auto call=[&]{switch(stage){case 0:return window.InitWindowSystem();case 1:return window.DestroyWindow();case 2:return window.DestroyRenderSystem();default:return window.DestroyWindowSystem();}};
    for(int tick=0;tick<3;++tick){assert(!call()&&effects.empty());}
    held.reset();assert(call());assert(!effects.empty()&&!session.AcquireDecoder());
    if(stage==3){
      auto a=std::find(effects.begin(),effects.end(),"shutdown-switch");
      auto b=std::find(effects.begin(),effects.end(),"context-destroy");assert(a<b);
      assert(!window.InitWindowSystem());
    }
  }
}
static void active_native_retirement(){
  ready();effects.clear();CWinSystemAmlogicGLESContext window;
  std::promise<void> entered,release;auto released=release.get_future();
  nativeHook=[&]{entered.set_value();released.wait();};
  std::thread announcement([&]{window.dv.Announce(System,"","OnWake",{});});
  entered.get_future().wait();
  const auto count=effects.size();
  assert(!window.PrepareForShutdown()&&!window.DestroyWindowSystem());
  assert(effects.size()==count); // Cannot cancel an active native owner into destruction.
  release.set_value();announcement.join();nativeHook={};
  assert(window.PrepareForShutdown());assert(window.PrepareForShutdown());
  assert(window.DestroyRenderSystem());assert(window.DestroyWindow());assert(window.DestroyWindowSystem());
  assert(nativeOwner!=std::this_thread::get_id());
}
int main(){admission();announcement_retry();standalone();active_native_retirement();}
'''


def harness():
    dv = (ROOT / 'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    window = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    announcement = (ROOT / 'xbmc/interfaces/AnnouncementManager.cpp').read_text()
    interface = (ROOT / 'xbmc/interfaces/IAnnouncer.h').read_text()
    methods = '\n'.join(function(dv, signature) for signature in (
        'void CDolbyVisionAML::Retire()', 'bool CDolbyVisionAML::ContinueAnnounce()',
        'void CDolbyVisionAML::Announce('))
    methods += '\n' + function(announcement, 'void CAnnouncementManager::Process()')
    methods += '\n' + '\n'.join(function(window, 'bool CWinSystemAmlogicGLESContext::' + signature)
                                for signature in ('InitWindowSystem()', 'PrepareForShutdown()',
                                'DestroyWindow()', 'DestroyRenderSystem()', 'DestroyWindowSystem()'))
    return PREFIX.replace('@INTERFACE@', function(interface, 'class IAnnouncer') + ';').replace('@METHODS@', methods) + TESTS


def run(source, mutated_header=None, fail=False):
    with tempfile.TemporaryDirectory(prefix='aml-native-') as tmp:
        out = Path(tmp)
        if mutated_header:
            h = out / 'cores/VideoPlayer/DVDCodecs/Video/AMLSession.h'
            h.parent.mkdir(parents=True);h.write_text(mutated_header)
        (out / 'test.cpp').write_text(source)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        '-pthread', '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out/'test.cpp'), '-o', str(out/'test')], check=True)
        result = subprocess.run([str(out/'test')], capture_output=True, text=True, timeout=15)
        if fail:
            assert result.returncode and 'Assertion' in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    source=harness();run(source);print('PASS: native admission, announcement continuation and standalone teardown (ASan/UBSan)')
    if args.negative_controls:
        header=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLSession.h').read_text()
        for label,old,new in (
            ('admit active codec', 'ready = ready && !state.active && !state.retiring && !state.mutating;', 'ready = true;'),
            ('cancel active native', 'request->phase != NativeRequest::Phase::PENDING)\n      return false;', 'false)\n      return false;'),
            ('late completion replaces owner', 'request != s_nativeRequest || request->owner', 'false || request->owner'),
        ):
            assert old in header;run(source,header.replace(old,new),True);print('REJECTED:',label)
        for label,old,new in (
            ('announcement replay', 'if (pending)\n    {', 'if (false && pending)\n    {'),
            ('early shutdown admission', 'return bool(*m_shutdownAdmission);', 'return true;'),
            ('ignore standalone gate', 'if (!display)\n    return false;', 'if (false && !display)\n    return false;'),
        ):
            assert old in source;run(source.replace(old,new),fail=True);print('REJECTED:',label)


if __name__=='__main__':main()
