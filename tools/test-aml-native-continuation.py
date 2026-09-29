#!/usr/bin/env python3
"""Run real admission/deferred headers and extracted Setup/VSVDB production bodies.

Settings, services and DV effects are recording substitutes. No CE/device claim.
"""
import argparse
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PREFIX = r'''
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "windowing/amlogic/AMLDeferredWork.h"
#include <atomic>
#include <cassert>
#include <future>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
using namespace std::chrono_literals;
struct CLog {template<class... T> static void Log(T...) {}};
constexpr int LOGDEBUG=0;
enum DV_TYPE {DV_TYPE_DISPLAY_LED=1};
constexpr int DV_MODE_OFF=0;
struct CSettings {@IDS@};
struct Settings {
  std::atomic<int> type{1}, luminance{400}, reads{0};
  std::atomic<bool> overrideEdid{false};
  Settings* GetSettingsManager(){return this;}
  void RegisterSettingOptionsFiller(const char*,int){}
  void RegisterCallback(void*,const std::set<std::string>&){}
  bool GetBool(const char*){return overrideEdid;}
  int GetInt(const char* key){++reads;return std::string(key)==CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE?type.load():luminance.load();}
  void SetInt(const char*,int){}
};
Settings values;
Settings* settings(){return &values;}
@FILLERS@
void set_visible(const char*,bool){} void set_dv_settings_visible(bool){}
bool supported=true;
bool aml_support_dolby_vision(){return supported;}
struct DOVIStreamMetadata {int source_max_pq;};
struct Cache {std::atomic<int> pq{1000};DOVIStreamMetadata GetVideoDoViStreamMetadata(){return {pq};}};
struct Announcer {void AddAnnouncer(void*){}};
struct CServiceBroker {
 static Cache& GetDataCacheCore(){static Cache cache;return cache;}
 static Announcer* GetAnnouncementManager(){static Announcer a;return &a;}
};
std::function<void()> startupHook;
void aml_dv_start(){assert(startupHook);startupHook();}
std::function<void(int,int,int)> payloadHook;
struct AMLDVCapability {};
AMLDVCapability aml_read_dv_cap(){return {};}
void set_vsvdb_payload_ver(DV_TYPE t,int lum,int pq,const AMLDVCapability&){assert(payloadHook);payloadHook(t,lum,pq);}
struct CDolbyVisionAML {
 CAMLDeferredWork m_deferredWork;
 std::atomic<bool> m_vsvdb_apply_scheduled{false},m_applying_vsvdb{false};
 bool m_registered=false;
 bool Setup(CAMLSession::DisplayRequest display={});
 void schedule_vsvdb_payload_apply();
};
@METHODS@
template<class Predicate> void until(Predicate predicate){
 const auto deadline=std::chrono::steady_clock::now()+3s;
 while(!predicate()){assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(1ms);}
}
void ready(){auto r=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(r));assert(CAMLSession::EndDisplay(r,CAMLSession::DisplayPhase::READY));}
void open(CAMLSession& s){auto r=s.Fence();assert(s.BeginMutation(r));assert(s.Complete(r,true));}
void drain(CAMLDeferredWork& w){until([&]{return w.Cancel();});}
'''
TESTS = r'''
void display_nesting(){
 ready();CAMLSession session;open(session);
 auto queued=CAMLSession::FenceNative();assert(queued);
 CAMLDisplayLifecycle display;
 CAMLSession::DisplayRequest old;
 {
  CAMLDisplayLifecycle::Mutation parent(display);assert(parent);old=parent.Request();
  CDolbyVisionAML dv;
  startupHook=[&]{
   assert(!session.AcquireDecoder());
   assert(!CAMLSession::TryBeginNative(queued));
   assert(!CAMLSession::EndDisplay(old,CAMLSession::DisplayPhase::READY));
   CAMLNativeTransaction nested(old);assert(nested.TryBegin());
  };
  assert(dv.Setup(old));
  auto stale=old;++stale.serial;assert(!dv.Setup(stale));
  std::thread wrong([&]{assert(!dv.Setup(old));});wrong.join();
  startupHook=[] {throw std::runtime_error("startup");};
  try{dv.Setup(old);assert(false);}catch(const std::runtime_error&){}
  parent.Finish(CAMLSession::DisplayPhase::READY);
 }
 assert(display.Ready());assert(CAMLSession::TryBeginNative(queued));assert(CAMLSession::EndNative(queued));
 CDolbyVisionAML dv;assert(!dv.Setup(old));
 // A standalone caller may acquire its own gate, but never borrow another one.
 CAMLNativeTransaction blocker;assert(blocker.TryBegin());assert(!dv.Setup());
}
void native_owner_and_release(){
 ready();CAMLSession session;open(session);
 {
  CAMLNativeTransaction native;assert(native.TryBegin());assert(native.TryBegin());
  assert(!session.AcquireDecoder());
  std::thread wrong([&]{assert(!native.TryBegin());});wrong.join();
  auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::CancelDisplay(display));
 }
 assert(session.AcquireDecoder());
 try {CAMLNativeTransaction native;assert(native.TryBegin());throw 1;}catch(int){}
 assert(session.AcquireDecoder());
 {
  auto permit=session.AcquireDecoder();assert(permit);
  CAMLNativeTransaction pending;assert(!pending.TryBegin());
 }
 assert(session.AcquireDecoder());
}
void deferred_cancellation(){
 ready();CAMLSession session;open(session);
 auto permit=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());assert(*permit);
 CAMLDeferredWork work;std::atomic<int> calls{0};
 auto lifetime=std::make_shared<int>(1);std::weak_ptr<int> weak=lifetime;
 assert(work.ScheduleNative(0ms,[&,lifetime]{++calls;}));lifetime.reset();
 until([&]{return !session.AcquireDecoder();}); // pending native fence exists
 drain(work);assert(calls==0&&weak.expired());
 // Cancellation removed its fence despite the still-active decoder call.
 assert(session.AcquireDecoder());permit.reset();assert(!work.ScheduleNative(0ms,[]{}));
 // A waiting worker must also be cancellable when display, not codec, owns exclusion.
 auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
 CAMLDeferredWork underDisplay;assert(underDisplay.ScheduleNative(0ms,[&]{++calls;}));
 drain(underDisplay);assert(calls==0);assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
}
void deferred_active_and_failure(){
 ready();CAMLSession session;open(session);
 CAMLDeferredWork work;std::promise<void> entered,release;auto gate=release.get_future().share();
 const auto caller=std::this_thread::get_id();
 assert(work.ScheduleNative(0ms,[&]{assert(std::this_thread::get_id()!=caller);entered.set_value();gate.wait();throw std::runtime_error("apply");}));
 auto started=entered.get_future();assert(started.wait_for(3s)==std::future_status::ready);
 assert(!work.Cancel());assert(!session.AcquireDecoder());
 auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
 release.set_value();drain(work);assert(work.Failure());
 assert(CAMLSession::TryBeginDisplay(display));assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::FAILED));
 assert(!session.AcquireDecoder());ready();assert(session.AcquireDecoder());
}
void payload_freshness(){
 ready();CAMLSession session;open(session);CDolbyVisionAML dv;
 values.reads=0;std::atomic<int> calls{0};
 auto permit=std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());assert(*permit);
 std::promise<void> first,second;const auto caller=std::this_thread::get_id();
 payloadHook=[&](int type,int lum,int pq){
  assert(type==3&&lum==1200&&pq==3000);assert(std::this_thread::get_id()!=caller);
  assert(!session.AcquireDecoder());assert(dv.m_applying_vsvdb);
  if(++calls==1){dv.schedule_vsvdb_payload_apply();first.set_value();}
  else {assert(calls==2);second.set_value();}
 };
 dv.schedule_vsvdb_payload_apply();
 until([&]{return !session.AcquireDecoder();});
 assert(values.reads==0&&calls==0&&dv.m_vsvdb_apply_scheduled);
 for(int i=0;i<10;++i)dv.schedule_vsvdb_payload_apply();
 values.type=3;values.luminance=1200;CServiceBroker::GetDataCacheCore().pq=3000;
 permit.reset();
 auto firstDone=first.get_future(),secondDone=second.get_future();
 assert(firstDone.wait_for(3s)==std::future_status::ready);
 assert(secondDone.wait_for(3s)==std::future_status::ready);
 drain(dv.m_deferredWork);assert(calls==2&&!dv.m_applying_vsvdb&&!dv.m_deferredWork.Failure());
 // A failed payload releases both the suppression flag and native admission.
 CDolbyVisionAML failed;std::promise<void> failing;
 payloadHook=[&](int,int,int){failing.set_value();throw std::runtime_error("payload");};
 failed.schedule_vsvdb_payload_apply();auto called=failing.get_future();assert(called.wait_for(3s)==std::future_status::ready);
 drain(failed.m_deferredWork);assert(failed.m_deferredWork.Failure()&&!failed.m_applying_vsvdb);
 assert(session.AcquireDecoder());
}
int main(){display_nesting();native_owner_and_release();deferred_cancellation();deferred_active_and_failure();payload_freshness();std::cout<<"PASS: explicit display nesting and cancellable native continuations (ASan/UBSan)\n";}
'''

def source():
    dv=(ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    methods='\n'.join(function(dv,sig) for sig in ('bool CDolbyVisionAML::Setup(', 'void CDolbyVisionAML::schedule_vsvdb_payload_apply('))
    ids=sorted(set(re.findall(r'CSettings::(SETTING_\w+)',methods)))
    fillers=sorted(set(re.findall(r'RegisterSettingOptionsFiller\("[^"]+", (\w+)\)',methods)))
    return PREFIX.replace('@IDS@','\n'.join(f'static constexpr const char* {i}="{i}";' for i in ids)).replace('@FILLERS@','\n'.join(f'constexpr int {i}=0;' for i in fillers)).replace('@METHODS@',methods)+TESTS

def run(code,patch=None,negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-native-continuation-') as tmp:
        out=Path(tmp)
        if patch:
            name,text=patch;p=out/name;p.parent.mkdir(parents=True);p.write_text(text)
        (out/'test.cpp').write_text(code)
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie','-I',str(out),'-I',str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
        result=subprocess.run([str(out/'test')],capture_output=True,text=True,timeout=15)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr,result.stdout+result.stderr
        else:
            assert result.returncode==0,result.stdout+result.stderr
            print(result.stdout.strip())

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        for label,path,old,new in [
            ('ignore display identity','cores/VideoPlayer/DVDCodecs/Video/AMLSession.h','if (!MatchesDisplay(request) || s_displayPhase != DisplayPhase::MUTATING)\n      return false;','if ((void)request, s_displayPhase != DisplayPhase::MUTATING)\n      return false;'),
            ('release parent under nested native','cores/VideoPlayer/DVDCodecs/Video/AMLSession.h','if (!MatchesDisplay(request) || s_displayNativeDepth ||','if (!MatchesDisplay(request) ||'),
            ('run native job before admission','windowing/amlogic/AMLDeferredWork.h','if (admitted)','if (admitted || true)'),
            ('leak active native token','windowing/amlogic/AMLNativeTransaction.h','CAMLSession::EndNative(m_request);','(void)m_request;'),
            ('leak pending native token','windowing/amlogic/AMLNativeTransaction.h','CAMLSession::CancelNative(m_request);','(void)m_request;'),
        ]:
            original=(ROOT/'xbmc'/path).read_text();assert original.count(old)==1
            run(code,(path,original.replace(old,new)),True);print('REJECTED:',label)
        for label,old,new in [
            ('payload bypasses native gate','m_deferredWork.ScheduleNative(','m_deferredWork.Schedule('),
            ('lose exception suppression cleanup','m_applying_vsvdb = false;\n      throw;','throw;'),
        ]:
            assert code.count(old)==1
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
