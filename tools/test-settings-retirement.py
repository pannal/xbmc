#!/usr/bin/env python3
"""Exercise production settings dispatch/revocation and DV deferred retirement.

Actual method bodies and lifetime headers; settings storage, DV native effects,
EDID capability reads and platform services are recording substitutes.
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
#include "settings/lib/SettingCallbackRegistration.h"
#include "windowing/amlogic/AMLDeferredWork.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include <atomic>
#include <cassert>
#include <future>
#include <map>
#include <set>
#include <shared_mutex>
#include <string>
#include <unordered_set>
#include <vector>
#include <iostream>
using namespace std::chrono_literals;
using CSharedSection=std::shared_mutex;
struct CSetting {
  std::string id="value";
  std::string GetId()const{return id;}
  bool IsReference()const{return false;}
  std::string GetReferencedId()const{return "";}
  std::string ToString()const{return "";}
  void FromString(const std::string&){}
};
using SettingPtr=std::shared_ptr<CSetting>;
enum class SettingDependencyType{Unknown,Enable,Visible};
struct StringUtils{static bool EqualsNoCase(const char* a,const char* b){return std::string(a)==b;}};
struct CSettingsManager {
  using CallbackSet=std::map<ISettingCallback*,std::shared_ptr<CSettingCallbackRegistration>>;
  struct Setting {
    SettingPtr setting;
    std::map<int,std::vector<SettingDependencyType>> dependencies;
    std::set<std::string> children;
    CallbackSet callbacks;
    std::unordered_set<std::string> references;
  };
  using SettingMap=std::map<std::string,Setting>;
  SettingMap m_settings;
  CallbackSet m_callbackRegistrations;
  CSharedSection m_settingsCritical;
  bool m_initialized=false,m_loaded=true;
  auto FindSetting(const std::string& id){return m_settings.find(id);}
  auto InsertSetting(const std::string& id,const Setting& value){return m_settings.emplace(id,value);}
  std::map<int,std::vector<SettingDependencyType>> GetDependencies(const std::shared_ptr<const CSetting>&){return {};}
  void UpdateSettingByDependency(int,SettingDependencyType){}
  void UpdateSettingByDependency(const std::string&,SettingDependencyType){}
  void RegisterCallback(ISettingCallback*,const std::set<std::string>&);
  void UnregisterCallback(ISettingCallback*);
  std::shared_ptr<CSettingCallbackRegistration> RevokeCallback(ISettingCallback*);
  bool OnSettingChanging(const std::shared_ptr<const CSetting>&);
  void OnSettingChanged(const std::shared_ptr<const CSetting>&);
  void OnSettingAction(const std::shared_ptr<const CSetting>&);
  bool OnSettingUpdate(const SettingPtr&,const char*,const TiXmlNode*);
  void OnSettingPropertyChanged(const std::shared_ptr<const CSetting>&,const char*);
};
@MANAGER@
struct Callback: ISettingCallback {
  std::function<void()> hook;
  int calls=0;
  bool changing=true,updated=false;
  void Call(){++calls;if(hook)hook();}
  bool OnSettingChanging(const std::shared_ptr<const CSetting>&)override{Call();return changing;}
  void OnSettingChanged(const std::shared_ptr<const CSetting>&)override{Call();}
  void OnSettingAction(const std::shared_ptr<const CSetting>&)override{Call();}
  bool OnSettingUpdate(const SettingPtr&,const char*,const TiXmlNode*)override{Call();return updated;}
  void OnSettingPropertyChanged(const std::shared_ptr<const CSetting>&,const char*)override{Call();}
};
void dispatch(CSettingsManager& manager,int kind){
  auto value=std::make_shared<CSetting>();
  switch(kind){
    case 0:manager.OnSettingChanging(value);break;
    case 1:manager.OnSettingChanged(value);break;
    case 2:manager.OnSettingAction(value);break;
    case 3:manager.OnSettingUpdate(value,"old",nullptr);break;
    case 4:manager.OnSettingPropertyChanged(value,"visible");break;
  }
}
void registration_cases(){
  for(int kind=0;kind<5;++kind){
    CSettingsManager manager; Callback targets[2];
    manager.RegisterCallback(&targets[0],{"value"});
    manager.RegisterCallback(&targets[1],{"value","second"});
    auto old=manager.m_settings["value"].callbacks[&targets[1]];
    assert(old==manager.m_settings["second"].callbacks[&targets[1]]);
    std::promise<void> entered,release;auto ready=entered.get_future(),gate=release.get_future();
    targets[0].hook=[&]{entered.set_value();gate.wait();};
    auto running=std::async(std::launch::async,[&]{dispatch(manager,kind);});
    assert(ready.wait_for(2s)==std::future_status::ready);
    // First subscriber blocks after the manager copied BOTH registration records.
    auto receipt=manager.RevokeCallback(&targets[1]);
    assert(receipt==old && receipt->Drained());
    manager.RegisterCallback(&targets[1],{"value"});
    assert(manager.m_settings["value"].callbacks[&targets[1]]!=old);
    release.set_value();running.get();assert(targets[1].calls==0);
    targets[0].hook={};dispatch(manager,kind);assert(targets[1].calls==1);
    // Active callback must drain; revocation itself cannot wait for it.
    std::promise<void> active,finish;auto begun=active.get_future(),done=finish.get_future();
    targets[1].hook=[&]{active.set_value();done.wait();};
    running=std::async(std::launch::async,[&]{dispatch(manager,kind);});
    assert(begun.wait_for(2s)==std::future_status::ready);
    receipt=manager.RevokeCallback(&targets[1]);assert(receipt && !receipt->Drained());
    finish.set_value();running.get();assert(receipt->Drained());
    dispatch(manager,kind);assert(targets[1].calls==2);
  }
  CSettingsManager manager;Callback target;
  manager.RegisterCallback(&target,{"value"});
  std::shared_ptr<CSettingCallbackRegistration> receipt;
  target.hook=[&]{receipt=manager.RevokeCallback(&target);assert(!receipt->Drained());};
  dispatch(manager,1);assert(receipt->Drained()); // self-unregister never waits
  manager.RegisterCallback(&target,{"value"});
  target.hook=[] {throw 7;};
  try{dispatch(manager,1);assert(false);}catch(int){}
  receipt=manager.RevokeCallback(&target);assert(receipt->Drained());
  manager.RegisterCallback(&target,{"value"});
  auto copy=manager.m_settings["value"].callbacks;
  manager.m_settings.clear(); // reload must not lose the revocation handle
  receipt=manager.RevokeCallback(&target);assert(receipt->Drained());
  assert(!copy.begin()->second->Acquire());
  Callback policy[2];manager.RegisterCallback(&policy[0],{"value"});
  manager.RegisterCallback(&policy[1],{"value"});auto value=std::make_shared<CSetting>();
  policy[0].changing=false;
  assert(!manager.OnSettingChanging(value) && policy[1].calls==0);
  policy[1].updated=true;
  assert(manager.OnSettingUpdate(value,"old",nullptr));
  assert(policy[0].calls==2 && policy[1].calls==1);
  manager.UnregisterCallback(&policy[0]);
  dispatch(manager,2);assert(policy[0].calls==2 && policy[1].calls==2);
}
enum TV_PRESET{TV_PRESET_MANUAL,TV_PRESET_AUTO,TV_PRESET_LG,TV_PRESET_SONY,TV_PRESET_SAMSUNG,TV_PRESET_PANASONIC,TV_PRESET_PHILIPS,TV_PRESET_TCL};
enum DV_TYPE{DV_TYPE_DISPLAY_LED,DV_TYPE_PLAYER_LED_LLDV,DV_TYPE_PLAYER_LED_HDR2,DV_TYPE_VS10_ONLY};
enum DV_MODE{DV_MODE_OFF,DV_MODE_ON_DEMAND};
constexpr int LOGINFO=1, LOGERROR=2;
struct CLog{template<class...T>static void Log(T&&...){}};
namespace xbmc_dv_cap {std::string edid_pnpid="test";}
bool aml_display_support_dv(){return true;}
bool aml_display_support_hdr10plus(){return false;}
bool aml_display_support_hdr_pq(){return true;}
bool aml_display_support_dv_std(){return true;}
bool aml_display_support_dv_ll(){return true;}
struct CSettings {
@IDS@
  CSettingsManager manager;
  std::mutex mutex;
  std::map<std::string,int> values;
  std::function<void(const std::string&)> onWrite;
  auto* GetSettingsManager(){return &manager;}
  int GetInt(const std::string& key){std::lock_guard<std::mutex> lock(mutex);return values[key];}
  void SetInt(const std::string& key,int value){
    {std::lock_guard<std::mutex> lock(mutex);values[key]=value;}
    if(onWrite)onWrite(key);
  }
  void SetBool(const std::string& key,bool value){SetInt(key,value);}
} settingsInstance;
CSettings* settings(){return &settingsInstance;}
struct DOVIStreamMetadata{int source_max_pq=333;};
struct CServiceBroker {
  static inline bool alive=true;
  static CServiceBroker& GetDataCacheCore(){assert(alive);static CServiceBroker cache;return cache;}
  DOVIStreamMetadata GetVideoDoViStreamMetadata(){return {};}
};
std::function<void(int,int,int)> payloadHook;
void set_vsvdb_payload_ver(DV_TYPE type,int max,int pq){assert(CServiceBroker::alive);payloadHook(type,max,pq);}
struct CDolbyVisionAML: ISettingCallback {
  CAMLDeferredWork m_deferredWork;
  std::shared_ptr<CSettingCallbackRegistration> m_settingsRetirement;
  std::atomic<bool> m_applying_tv_preset{false},m_tv_preset_apply_scheduled{false},m_vsvdb_apply_scheduled{false},m_applying_vsvdb{false};
  std::atomic<int> m_tv_preset_pending{0};
  std::atomic<bool> m_retiring{false};
  std::shared_ptr<CAMLSession::NativeRequest> m_nativeRequest;
  void apply_tv_preset(int);
  void schedule_tv_preset_apply(int);
  void schedule_vsvdb_payload_apply();
  bool Retire();
};
@DV@
struct CWinSystemAmlogicGLESContext {
  CDolbyVisionAML* dv;
  bool m_shutdownRequested=false;
  CAMLDisplayLifecycle m_displayLifecycle;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
  bool RetireNativeTransactions(){return dv->Retire();}
  bool PrepareForShutdown();
};
@WINDOW@
void awaitRetirement(CDolbyVisionAML& dv){
  auto end=std::chrono::steady_clock::now()+2s;
  while(!dv.Retire()){assert(std::chrono::steady_clock::now()<end);std::this_thread::yield();}
}
void deferred_cases(){
  // Queued work is cancelled and captures retired before the receipt is ready.
  {CAMLDeferredWork work;std::atomic<int> calls=0;auto lifetime=std::make_shared<int>(1);
   std::weak_ptr<int> weak=lifetime;
   assert(work.Schedule(1h,[lifetime,&calls]{++calls;}));lifetime.reset();
   auto end=std::chrono::steady_clock::now()+2s;
   while(!work.Cancel()){assert(std::chrono::steady_clock::now()<end);std::this_thread::yield();}
   assert(weak.expired() && calls==0 && !work.Schedule(0ms,[&]{++calls;}));}
  // Actual preset apply preserves the last coalesced choice and programmatic flag.
  {CDolbyVisionAML dv;std::promise<void> applied;auto finished=applied.get_future();
   settings()->onWrite=[&](const std::string& key){assert(dv.m_applying_tv_preset);
     if(key==CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT)applied.set_value();};
   dv.schedule_tv_preset_apply(TV_PRESET_LG);dv.schedule_tv_preset_apply(TV_PRESET_SAMSUNG);
   assert(dv.m_tv_preset_apply_scheduled);
   assert(finished.wait_for(2s)==std::future_status::ready);awaitRetirement(dv);
   assert(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY)==1);
   assert(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)==DV_TYPE_PLAYER_LED_HDR2);
   assert(!dv.m_applying_tv_preset);settings()->onWrite={};}
  // Actual VSVDB job reads latest state; clear-before-read permits a fresh apply
  // while the previous one is still running. Native effects serialize, and
  // retirement still waits for the admitted job.
  {CDolbyVisionAML dv;std::atomic<int> calls=0;std::promise<void> first,second,release,releaseSecond;
   auto firstReady=first.get_future(),secondReady=second.get_future(),gate=release.get_future(),gateSecond=releaseSecond.get_future();
   settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM,100);
   payloadHook=[&](int,int max,int pq){assert(dv.m_applying_vsvdb && pq==333);
     if(++calls==1){assert(max==200);first.set_value();gate.wait();}
     else {assert(max==300);second.set_value();gateSecond.wait();}};
   dv.schedule_vsvdb_payload_apply();
   settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM,200);
   assert(firstReady.wait_for(2s)==std::future_status::ready);
   assert(!dv.m_vsvdb_apply_scheduled);
   settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM,300);
   dv.schedule_vsvdb_payload_apply();
   assert(secondReady.wait_for(100ms)==std::future_status::timeout);
   release.set_value();
   assert(secondReady.wait_for(2s)==std::future_status::ready);
   assert(!dv.Retire());releaseSecond.set_value();awaitRetirement(dv);assert(calls==2);}
  // A settings callback already admitted but not yet returned retains the DV
  // owner, and blocks display admission before any service teardown.
  {CDolbyVisionAML dv;auto* manager=settings()->GetSettingsManager();
   manager->RegisterCallback(&dv,{"value"});
   auto copied=manager->m_settings["value"].callbacks[&dv];
   {auto active=copied->Acquire();assert(active);
    CWinSystemAmlogicGLESContext window{&dv};
    assert(!window.PrepareForShutdown() && !window.m_shutdownAdmission);
    assert(!copied->Acquire());assert(!dv.Retire());}
   awaitRetirement(dv);assert(copied->Drained());}
  {CDolbyVisionAML dv;CWinSystemAmlogicGLESContext window{&dv};
   std::promise<void> entered,release;auto ready=entered.get_future(),gate=release.get_future();
   payloadHook=[&](int,int,int){entered.set_value();gate.wait();};
   dv.schedule_vsvdb_payload_apply();assert(ready.wait_for(2s)==std::future_status::ready);
   assert(!window.PrepareForShutdown() && !window.m_shutdownAdmission);
   assert(CServiceBroker::alive);release.set_value();awaitRetirement(dv);
   assert(window.PrepareForShutdown());CServiceBroker::alive=false;
   dv.schedule_vsvdb_payload_apply();assert(dv.Retire());}
}
int main(){registration_cases();deferred_cases();std::cout<<"PASS: five dispatch paths, registration receipts and DV deferred retirement\n";}
'''


def source():
    manager=(ROOT/'xbmc/settings/lib/SettingsManager.cpp').read_text()
    signatures=['void CSettingsManager::RegisterCallback(', 'void CSettingsManager::UnregisterCallback(',
                'std::shared_ptr<CSettingCallbackRegistration> CSettingsManager::RevokeCallback(',
                'bool CSettingsManager::OnSettingChanging(', 'void CSettingsManager::OnSettingChanged(',
                'void CSettingsManager::OnSettingAction(', 'bool CSettingsManager::OnSettingUpdate(',
                'void CSettingsManager::OnSettingPropertyChanged(']
    code=PREFIX.replace('@MANAGER@','\n'.join(function(manager,sig) for sig in signatures))
    dv=(ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    bodies='\n'.join(function(dv,sig) for sig in ['void CDolbyVisionAML::apply_tv_preset(',
        'void CDolbyVisionAML::schedule_tv_preset_apply(', 'void CDolbyVisionAML::schedule_vsvdb_payload_apply(',
        'bool CDolbyVisionAML::Retire()'])
    ids=sorted(set(re.findall(r'CSettings::(SETTING_\w+)',bodies)))
    code=code.replace('@IDS@','\n'.join(f'static constexpr const char* {key}="{key}";' for key in ids))
    code=code.replace('@DV@',bodies)
    window=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    code=code.replace('@WINDOW@',function(window,'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()'))
    base=(ROOT/'xbmc/windowing/amlogic/WinSystemAmlogic.cpp').read_text()
    destroy=function(base,'bool CWinSystemAmlogic::DestroyWindowSystem()')
    assert destroy.index('if (!RetireNativeTransactions())')<destroy.index('m_dolbyVisionAML.reset()')
    assert 'return !m_dolbyVisionAML || m_dolbyVisionAML->Retire();' in base
    assert 'static std::atomic<bool> s_applying' not in dv
    assert 'if (!setting || m_retiring) return;' in dv
    assert '.detach()' not in dv
    return code


def run(code,patch=None,negative=False):
    with tempfile.TemporaryDirectory(prefix='settings-retirement-') as tmp:
        out=Path(tmp)
        if patch:
            name,text=patch;path=out/name;path.parent.mkdir(parents=True);path.write_text(text)
        (out/'test.cpp').write_text(code)
        built=subprocess.run(['g++','-std=c++17','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer',
            '-fno-pie','-no-pie','-I',str(out),'-I',str(ROOT/'xbmc'),'-I',str(ROOT/'xbmc/settings/lib'),str(out/'test.cpp'),'-o',str(out/'test')],capture_output=True,text=True)
        assert built.returncode==0,built.stderr
        result=subprocess.run([str(out/'test')],capture_output=True,text=True,timeout=15)
        if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result.stdout+result.stderr
        else:assert result.returncode==0,result.stdout+result.stderr;print(result.stdout.strip())


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        registration='settings/lib/SettingCallbackRegistration.h'
        work='windowing/amlogic/AMLDeferredWork.h'
        controls=[
          ('admit revoked copy',registration,'if (m_state->revoked)','if (false)'),
          ('ignore active callback',registration,'m_state->revoked && m_state->active == 0','m_state->revoked'),
          ('run cancelled job',work,'const bool run = !state->closed;','const bool run = true;'),
          ('ignore running jobs',work,'return m_state->outstanding == 0;','return true;'),
        ]
        for label,path,old,new in controls:
            original=(ROOT/'xbmc'/path).read_text();assert original.count(old)==1
            run(code,(path,original.replace(old,new)),True);print('rejected:',label)
        for label,old,new in [
          ('skip receipt drain','return workDrained && (!m_settingsRetirement || m_settingsRetirement->Drained());','return workDrained;'),
          ('admit display before drain','if (!RetireNativeTransactions())\n    return false;','RetireNativeTransactions();'),
          ('lose preset coalescing','m_tv_preset_pending.store(preset);','if (!m_tv_preset_apply_scheduled) m_tv_preset_pending.store(preset);'),
        ]:
            assert code.count(old)==1,label
            run(code.replace(old,new),negative=True);print('rejected:',label)


if __name__=='__main__':main()
