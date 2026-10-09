#!/usr/bin/env python3
"""Run exact worker cancellation/admission, real joins and extracted AML boundaries.

FFmpeg scanning, settings, metadata and sysfs are substitutes. Scanner publication,
watch loop, start/stop/replacement and pre-display shutdown methods are production.
"""
import argparse
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parents[1]
fixture=runpy.run_path(str(ROOT/'tools/test-aml-native-continuation.py'))
function,run=fixture['function'],fixture['run']
PREFIX=r'''
#include "utils/AMLNativeWorker.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include <cassert>
#include <cmath>
#include <future>
#include <iostream>
#include <string>
#include <vector>
using namespace std::chrono_literals;
constexpr int LOGINFO=0,LOGDEBUG=1,LOGWARNING=2,LOGERROR=3;
struct CLog{template<class...T>static void Log(T...){}};
struct Metadata{bool has_level5_metadata=false;int level5_active_area_top_offset=0,level5_active_area_bottom_offset=0,level5_active_area_left_offset=0,level5_active_area_right_offset=0;bool level5_detected=false;};
struct Cache{Metadata meta;Metadata GetVideoDoViFrameMetadata(){return meta;}void ClearVideoDoViFrameMetadata(){meta={};}}cache;
struct CApplicationPlayer{bool IsPlaying(){return true;}int GetCacheLevel(){return 10;}};
struct Components{template<class T>std::shared_ptr<T> GetComponent(){static auto p=std::make_shared<T>();return p;}}components;
struct CServiceBroker{static Cache& GetDataCacheCore(){return cache;}static bool IsServiceManagerUp(){return true;}static Components& GetAppComponents(){return components;}};
struct App{std::string CurrentFile(){return "fallback";}}g_application;
CAMLSession* session=nullptr;
std::atomic<int> writes{0},watchWrites{0};
std::function<void()> writeHook;
struct CSysfsPath{CSysfsPath(const char*,int){assert(session&&!session->AcquireDecoder());++writes;if(writeHook)writeHook();}};
void aml_dv_apply_l5_override_sysfs(){assert(session&&!session->AcquireDecoder());++watchWrites;if(writeHook)writeHook();}
@AUTO_GLOBALS@
struct CSettings {static constexpr int SETTING_COREELEC_AMLOGIC_DV_L5_AUTO_LETTERBOX=1,SETTING_SUBTITLES_DETECTACTIVEAREA=2;};
struct Settings {bool detect=true;bool GetBool(int id){return id==2?detect:true;}} settingsValue;
Settings* settings(){return &settingsValue;}
bool aml_dv_l5_override_active(){return false;}
@CONSTANTS@
@GEOMETRY@
bool aml_dv_auto_letterbox_active(){uint16_t t,b,l,r;return _auto_letterbox_geometry(t,b,l,r);}
@SAMPLE@
constexpr int DV_DETECT_FAILED=0,DV_DETECT_RUNNING=1,DV_DETECT_SKIPPED=2,DV_DETECT_OK=3,DV_DETECT_SKIP_NON16X9=4,DV_DETECT_INACTIVE=5;
bool dvDetection=true;bool aml_dv_detect_active_area_enabled(){return dvDetection;}
std::atomic<bool> s_detectStable{false},s_detectThrottleActive{false},s_detectCacheStarved{false};
std::atomic<int> s_detectState{DV_DETECT_FAILED};
std::atomic<uint16_t> s_detectedTop{0},s_detectedBottom{0},s_detectedLeft{0},s_detectedRight{0};
constexpr int kDetectCacheCriticalPct=40;
std::atomic<int64_t> s_detectLastCacheCheckMs{0};
@DETECT_GLOBALS@
@IO@
@PUBLISH@
std::function<void(const std::shared_ptr<DetectSource>&,const CAMLNativeWorker::Run&)> scanHook;
void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& state){assert(scanHook);scanHook(source,state);}
void aml_dv_detect_set_file(const std::string&);
void select(const std::string& path,bool native=true,int w=1920,int h=1080){
  aml_dv_detect_set_file(path);assert(aml_subtitle_active_area_configure(w,h,native,true,aml_subtitle_active_area_source()));
}
@METHODS@
struct DV{bool ready=false;bool Retire(){return ready;}};
struct CWinSystemAmlogic{std::unique_ptr<DV> m_dolbyVisionAML=std::make_unique<DV>();bool RetireNativeTransactions();};
@RETIRE@
struct CWinSystemAmlogicGLESContext:CWinSystemAmlogic{
 void SetNativeGuiWait(bool){} // lease policy is covered by the native GUI wait fixture
 bool m_shutdownRequested=false;CAMLDisplayLifecycle m_displayLifecycle;
 std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;bool PrepareForShutdown();
};
@PREPARE@
template<class P>void until(P p){auto end=std::chrono::steady_clock::now()+3s;while(!p()){assert(std::chrono::steady_clock::now()<end);std::this_thread::sleep_for(1ms);}}
void ready(){auto d=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(d));assert(CAMLSession::EndDisplay(d,CAMLSession::DisplayPhase::READY));}
void open(CAMLSession& s){auto r=s.Fence();assert(s.BeginMutation(r));assert(s.Complete(r,true));}
void init(CAMLSession& s){ready();open(s);session=&s;writes=0;watchWrites=0;writeHook={};cache.meta={};}
'''
TESTS=r'''
void cancel_before_native_join(){
 // Real joins while the joining owner holds native admission. A lost Cancel
 // becomes a bounded failed future assertion, never an unbounded test hang.
 auto joined=std::async(std::launch::async,[]{
  CAMLNativeWorker worker;CAMLNativeTransaction owner;assert(owner.TryBegin());
  std::promise<void> entered;auto started=entered.get_future();
  worker.Start([&](const CAMLNativeWorker::Run& run){entered.set_value();CAMLNativeTransaction native;assert(!run.Admit(native));});
  assert(started.wait_for(2s)==std::future_status::ready);worker.Stop();assert(!worker.Failure());
 });
 assert(joined.wait_for(3s)==std::future_status::ready);joined.get();
}
void active_retirement_exception(){
 CAMLNativeWorker worker;std::promise<void> entered,release;auto gate=release.get_future().share();
 auto capture=std::make_shared<int>(1);std::weak_ptr<int> weak=capture;
 worker.Start([&,capture](const CAMLNativeWorker::Run& run){CAMLNativeTransaction native;assert(run.Admit(native));entered.set_value();gate.wait();throw 1;});capture.reset();
 auto active=entered.get_future();assert(active.wait_for(2s)==std::future_status::ready);
 assert(!worker.Retire()&&!weak.expired());
 auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
 release.set_value();until([&]{return worker.Retire();});assert(weak.expired()&&worker.Failure());
 assert(CAMLSession::TryBeginDisplay(display));assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 assert(!worker.Start([](const auto&){assert(false);}));
}
void detector_replacement(){
 CAMLSession s;init(s);std::atomic<int> scanned{0};std::promise<void> entered,cancelled;
 scanHook=[&](const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& run){assert(source->path=="first");++scanned;entered.set_value();detect_publish(run,source,10,10,0,0);cancelled.set_value();};
 {
  CAMLNativeTransaction owner;assert(owner.TryBegin());select("first");aml_dv_detect_active_area_start();
  auto started=entered.get_future();assert(started.wait_for(2s)==std::future_status::ready);
  assert(writes==4); // only caller-owned reset, never pending result publication
  aml_dv_detect_set_file("first"); // even identical path replaces stream identity
  auto stopped=cancelled.get_future();assert(stopped.wait_for(2s)==std::future_status::ready);
  s_detectWorker.Stop();assert(writes==4);assert(!s_detectWorker.Failure());
  uint16_t t,b,l,r;assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r));
 }
 // Latest captured selection publishes only after owner admission releases.
 std::promise<void> finished;auto complete=finished.get_future();
 scanHook=[&](const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& run){assert(source->path=="second");++scanned;detect_publish(run,source,20,20,0,0);finished.set_value();};
 {
  CAMLNativeTransaction owner;assert(owner.TryBegin());select("second");aml_dv_detect_active_area_start();
  assert(writes==8);
 }
 assert(complete.wait_for(2s)==std::future_status::ready);s_detectWorker.Stop();assert(writes==12&&scanned==2&&s_detectedTop==20&&s_detectStable);
 // Source-authored L5 still suppresses detector injection after admission.
 cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=8;
 CAMLNativeWorker::Run state({});detect_publish(state,s_detectSource,30,30,0,0);assert(writes==12&&s_detectState==DV_DETECT_OK); // geometry is independent; authored L5 still prevents injection
 {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_stop();}assert(writes==16&&!s_detectStable);
}
void stop_resets_after_join(){
 CAMLSession s;init(s);std::promise<void> entered;
 scanHook=[&](const std::shared_ptr<DetectSource>&,const CAMLNativeWorker::Run& run){entered.set_value();assert(detect_wait_for_cache(run,80,12000)==-1);assert(detect_interrupt_cb(const_cast<CAMLNativeWorker::Run*>(&run))==1);s_detectStable=true;s_detectState=DV_DETECT_OK;};
 {
  CAMLNativeTransaction owner;assert(owner.TryBegin());select("scan");aml_dv_detect_active_area_start();
  auto active=entered.get_future();assert(active.wait_for(2s)==std::future_status::ready);
  aml_dv_detect_active_area_stop();assert(!s_detectStable&&s_detectState==DV_DETECT_FAILED);
 }
}
void watcher_identity_and_join(){
 CAMLSession s;init(s);
 {
  CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_set_active_area_geometry(1918,802,true);aml_dv_auto_letterbox_watch_start();
  auto original=std::atomic_load(&s_autoLbGeneration);
  aml_dv_set_active_area_geometry(3840,1600,true);assert(original->load());
  aml_dv_auto_letterbox_watch_stop();assert(watchWrites==0&&s_autoLbAdditive);
 }
 cache.meta={true,280,280,0,0,false};
 std::promise<void> applied;auto complete=applied.get_future();writeHook=[&]{applied.set_value();};
 {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_auto_letterbox_watch_start();}
 assert(complete.wait_for(2s)==std::future_status::ready);aml_dv_auto_letterbox_watch_stop();assert(watchWrites==1&&!s_autoLbAdditive);
}
void shutdown_drains_before_display(){
 CAMLSession s;init(s);CWinSystemAmlogicGLESContext window;
 std::promise<void> entered,release;auto gate=release.get_future().share();
 scanHook=[&](const std::shared_ptr<DetectSource>&,const CAMLNativeWorker::Run& run){entered.set_value();gate.wait();assert(run.Cancelled());};
 {CAMLNativeTransaction owner;assert(owner.TryBegin());select("last");aml_dv_detect_active_area_start();aml_dv_auto_letterbox_watch_start();}
 auto active=entered.get_future();assert(active.wait_for(2s)==std::future_status::ready);
 assert(!window.PrepareForShutdown()&&!window.m_shutdownAdmission);
 assert(s_detectWorker.Closed()&&s_autoLbWorker.Closed()); // cancellation even while settings unready
 release.set_value();until([&]{return aml_dv_retire_background_work();});
 assert(!window.PrepareForShutdown()&&!window.m_shutdownAdmission);
 window.m_dolbyVisionAML->ready=true;assert(window.PrepareForShutdown());
 auto count=writes.load();aml_dv_detect_active_area_start();aml_dv_auto_letterbox_watch_start();assert(writes==count);
}

void generic_detector(){
 CAMLSession s;init(s);cache.meta={};settingsValue.detect=true;
 auto count=writes.load();
 std::promise<void> finished;auto done=finished.get_future();
 scanHook=[&](const auto& source,const CAMLNativeWorker::Run& run){
   detect_publish(run,source,140,140,0,0);finished.set_value();
 };
 {CAMLNativeTransaction owner;assert(owner.TryBegin());select("SDR|Header=value",false);aml_dv_detect_active_area_start();}
 assert(done.wait_for(2s)==std::future_status::ready);s_detectWorker.Stop();assert(writes==count);
 uint16_t t,b,l,r;assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==140&&b==140);
 assert(!aml_dv_detect_active_area_stable());
 assert(!aml_subtitle_detect_active_area_get(3840,2160,t,b,l,r));
 auto selected=aml_subtitle_active_area_source();auto oldSource=s_detectSource;
 aml_subtitle_active_area_invalidate();assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r));
 CAMLNativeWorker::Run run(oldSource->superseded);detect_publish(run,oldSource,200,200,0,0);
 assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r));
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,selected));
 CAMLNativeWorker::Run current(s_detectSource->superseded);detect_publish(current,s_detectSource,100,100,0,0);
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==100);
 aml_dv_detect_set_file("SDR|Header=value"); // identical path, new playback identity
 assert(!aml_subtitle_active_area_configure(1920,1080,true,true,selected));
 detect_publish(current,oldSource,300,300,0,0);assert(!s_detectSource->result);
 assert(writes==count);
 // Cropped sources are declined before the worker/source I/O starts.
 select("crop",false,1920,800);aml_dv_detect_active_area_start();assert(s_detectState==DV_DETECT_SKIP_NON16X9&&writes==count);
 // Native initial cleanup remains unconditional even after a previous process crash.
 {CAMLNativeTransaction owner;assert(owner.TryBegin());select("native");dvDetection=false;
  aml_dv_detect_active_area_start();assert(writes==count+4);dvDetection=true;
  aml_dv_detect_active_area_stop();}
 assert(writes==count+8);
}

int main(){ready();cancel_before_native_join();active_retirement_exception();detector_replacement();stop_resets_after_join();generic_detector();watcher_identity_and_join();shutdown_drains_before_display();std::cout<<"PASS: native participants, exact runs, real joins and shutdown retirement (ASan/UBSan)\n";}
'''

def source():
    aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    globals=aml[aml.index('static CAMLNativeWorker s_autoLbWorker;'):aml.index('void aml_dv_set_active_area_geometry(')]
    detector=aml[aml.index('static CAMLNativeWorker s_detectWorker;'):aml.index('/* Mid-read cache guard.')]
    methods='\n'.join(function(aml,sig) for sig in (
      'void aml_dv_set_active_area_geometry(', 'static void _auto_letterbox_watch_run(',
      'void aml_dv_auto_letterbox_watch_start()', 'void aml_dv_auto_letterbox_watch_stop()',
      'void aml_dv_detect_set_file(', 'static void detect_clear_injection(', 'void aml_dv_detect_active_area_start()',
      'void aml_dv_detect_active_area_stop()', 'bool aml_dv_retire_background_work()'))
    # Wiring into FFmpeg's interrupt and final publication; the full scan is not host-compiled.
    scan=function(aml,'static void DetectActiveAreaFromFile(')
    assert 'fmtCtx->interrupt_callback.opaque = const_cast<CAMLNativeWorker::Run*>(&run);' in scan
    assert 'detect_publish(run, source, detTop, detBottom, detLeft, detRight);' in scan
    assert 's_detectCancel' not in aml
    base=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogic.cpp').read_text()
    window=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    constants=aml[aml.index('static constexpr uint32_t AUTO_LB_AR_MAX'):aml.index('/* One sample of the current frame')]
    import re
    constants+=re.search(r'enum\s*\{\s*AUTO_LB_SAMPLE_OK.*?\};',aml,re.S).group()
    return PREFIX.replace('@CONSTANTS@',constants).replace('@GEOMETRY@',function(aml,'static bool _auto_letterbox_geometry(')).replace('@SAMPLE@',function(aml,'static int _auto_letterbox_duplicate_sample(')).replace('@AUTO_GLOBALS@',globals).replace('@DETECT_GLOBALS@',detector).replace('@IO@','\n'.join(function(aml,sig) for sig in ('static int64_t detect_steady_ms()', 'static int detect_interrupt_cb(', 'static int detect_wait_for_cache('))).replace('@PUBLISH@',function(aml,'static void detect_publish(')).replace('@METHODS@',methods).replace('@RETIRE@',function(base,'bool CWinSystemAmlogic::RetireNativeTransactions()')).replace('@PREPARE@',function(window,'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()'))+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        path='utils/AMLNativeWorker.h';original=(ROOT/'xbmc'/path).read_text()
        for label,old,new in [
          ('join without cancellation','if (run)\n      run->Cancel();','if (false && run)\n      run->Cancel();'),
          ('ignore stream replacement','m_cancelled || (m_superseded && m_superseded->load())','m_cancelled || (false && m_superseded && m_superseded->load())'),
          ('publish without admission','if (native.TryBegin())','if (native.TryBegin() || true)'),
          ('retire active participant','if (run && !run->m_done)\n      return false;','if (run && !run->m_done)\n      return true;'),
        ]:
            assert old in original
            run(code,(path,original.replace(old,new)),True);print('REJECTED:',label)
        for label,old,new in [
          ('stop misses state reset','s_detectStable.store(false);\n  s_detectState.store(DV_DETECT_FAILED);','/* omit reset */'),
          ('skip background retirement while settings pending','const bool background = aml_dv_retire_background_work();','const bool background = settings && aml_dv_retire_background_work();'),
        ]:
            assert old in code
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
