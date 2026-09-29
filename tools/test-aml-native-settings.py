#!/usr/bin/env python3
"""Execute production settings dispatch, ordered native apply and retirement.

Uses real admission/deferred/registration headers and production class fields;
settings storage, services and hardware effects are recording substitutes.
"""
import argparse
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-aml-native-continuation.py'))
function, run = fixture['function'], fixture['run']

PREFIX = r'''
#include "windowing/amlogic/AMLDeferredWork.h"
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
#include "settings/lib/SettingCallbackRegistration.h"
#pragma GCC diagnostic pop
#include <algorithm>
#include <atomic>
#include <cassert>
#include <future>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>
using namespace std::chrono_literals;
struct CLog {template<class... T> static void Log(T...) {}};
constexpr int LOGINFO=0,LOGERROR=1;
struct CVariant {};
namespace ANNOUNCEMENT {
 enum AnnouncementFlag {System,Player};
 struct IAnnouncer {
  virtual ~IAnnouncer()=default;
  virtual bool ContinueAnnounce(){return true;}
  virtual void Announce(AnnouncementFlag,const std::string&,const std::string&,const CVariant&)=0;
 };
}
struct CSetting {std::string id;const std::string& GetId()const{return id;}};
enum DV_TYPE {DV_TYPE_DISPLAY_LED,DV_TYPE_PLAYER_LED_HDR2,DV_TYPE_VS10_ONLY};
enum DV_MODE {DV_MODE_OFF,DV_MODE_ON,DV_MODE_ON_DEMAND};
constexpr int TV_PRESET_MANUAL=0;
constexpr int DOLBY_VISION_OUTPUT_MODE_IPT=0,DOLBY_VISION_OUTPUT_MODE_HDR10=1,DOLBY_VISION_OUTPUT_MODE_SDR10=2,DOLBY_VISION_OUTPUT_MODE_SDR8=3;
struct CSettings {@IDS@};
thread_local bool inCallback=false;
struct Settings {
 std::recursive_mutex mutex;
 std::map<std::string,int> values;
 std::function<void(const std::shared_ptr<const CSetting>&)> callback;
 std::shared_ptr<CSettingCallbackRegistration> receipt=std::make_shared<CSettingCallbackRegistration>(nullptr);
 Settings* GetSettingsManager(){return this;}
 std::shared_ptr<CSettingCallbackRegistration> RevokeCallback(void*){receipt->Revoke();return receipt;}
 int GetInt(const std::string& key){std::lock_guard<std::recursive_mutex> lock(mutex);return values[key];}
 bool GetBool(const std::string& key){return GetInt(key)!=0;}
 void Put(const std::string& key,int value){std::lock_guard<std::recursive_mutex> lock(mutex);values[key]=value;}
 void SetInt(const std::string& key,int value){
  std::lock_guard<std::recursive_mutex> lock(mutex);
  if(values[key]==value)return;
  values[key]=value;
  if(callback){auto lease=receipt->Acquire();if(lease){const bool previous=inCallback;inCallback=true;callback(std::make_shared<CSetting>(CSetting{key}));inCallback=previous;}}
 }
 void SetBool(const std::string& key,bool value){SetInt(key,value);}
} storage;
Settings* settings(){return &storage;}
struct DOVIStreamMetadata {int source_max_pq=123;};
struct CServiceBroker {
 static CServiceBroker& GetDataCacheCore(){static CServiceBroker cache;return cache;}
 DOVIStreamMetadata GetVideoDoViStreamMetadata(){return {};}
 static CServiceBroker* GetAnnouncementManager(){return &GetDataCacheCore();}
 void RemoveAnnouncer(void*){}
};
std::atomic<bool> enabled{true},playing{true},overrideActive{false};
std::atomic<unsigned int> mode{DOLBY_VISION_OUTPUT_MODE_IPT};
bool aml_is_dv_enable(){return enabled;}
bool aml_dv_playback_active(){return playing;}
unsigned int aml_dv_dolby_vision_mode(){return mode;}
bool aml_dv_l5_override_active(){return overrideActive;}
bool aml_display_support_dv_std(){return true;}
bool aml_support_dolby_vision(){return true;}
bool force_modes(){return false;}
void set_vsvdb_children_visible(bool){}
void set_visible(const std::string&,bool){} void set_dv_settings_visible(bool){}
CAMLSession* session=nullptr;
std::mutex effectMutex;
std::vector<std::pair<std::string,int>> effects;
std::function<void(const std::string&,int)> effectHook;
void effect(const std::string& name,int value=0){
 assert(!inCallback);assert(session&&!session->AcquireDecoder());
 {std::lock_guard<std::mutex> lock(effectMutex);effects.emplace_back(name,value);}
 if(effectHook)effectHook(name,value);
}
void aml_set_audio_ddr_urgent(bool v){effect("ddr",v);}
void aml_dv_set_osd_max(int v){effect("gui",v);}
void aml_dv_set_osd_brightness(int v){effect("osd",v);}
void aml_dv_set_hdr10_osd_brightness(int v){effect("hdr",v);}
void aml_dv_set_sdr_source_max_nits(int v){effect("sdr",v);}
void aml_dv_set_sdr_keep_ext(bool v){effect("keep",v);}
void aml_dv_set_target_min_lum(int v){effect("min",v);}
void aml_dv_apply_l5_override_sysfs(){effect("override");}
void aml_dv_apply_l5_sysfs(){effect("l5");}
void aml_dv_detect_active_area_stop(){effect("stop");}
void set_vsvdb_payload_ver(DV_TYPE,int,int){effect("payload");}
@BOOST@
#define private public
@CLASS@;
#undef private
CDolbyVisionAML::CDolbyVisionAML()=default;
bool CDolbyVisionAML::ContinueAnnounce(){return true;}
void CDolbyVisionAML::Announce(ANNOUNCEMENT::AnnouncementFlag,const std::string&,const std::string&,const CVariant&){}
void CDolbyVisionAML::schedule_tv_preset_apply(int){}
@METHODS@
template<class P>void until(P p){auto end=std::chrono::steady_clock::now()+3s;while(!p()){assert(std::chrono::steady_clock::now()<end);std::this_thread::sleep_for(1ms);}}
void drain(CDolbyVisionAML& dv){until([&]{return dv.Retire();});}
void ready(){auto r=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(r));assert(CAMLSession::EndDisplay(r,CAMLSession::DisplayPhase::READY));}
void open(CAMLSession& s){auto r=s.Fence();assert(s.BeginMutation(r));assert(s.Complete(r,true));}
std::vector<std::pair<std::string,int>> snapshot(){std::lock_guard<std::mutex> lock(effectMutex);return effects;}
void init(CDolbyVisionAML& dv,CAMLSession& s){
 ready();open(s);session=&s;storage.values.clear();storage.receipt=std::make_shared<CSettingCallbackRegistration>(&dv);
 storage.callback=[&](const auto& setting){dv.OnSettingChanged(setting);};
 effects.clear();effectHook={};enabled=true;playing=true;overrideActive=false;mode=DOLBY_VISION_OUTPUT_MODE_IPT;
}
void changed(const char* key,int value){settings()->SetInt(key,value);}
#define KEY(name) CSettings::SETTING_COREELEC_AMLOGIC_DV_##name
'''
TESTS = r'''
void ordered_fresh_apply(){
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);
 auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());assert(*permit);
 changed(KEY(MODE_ON_LUMINANCE),100);changed(KEY(OSD_BRIGHTNESS),50);changed(KEY(MODE_ON_LUMINANCE),300);
 until([&]{return !s.AcquireDecoder();});assert(snapshot().empty());
 settings()->Put(KEY(MODE_ON_LUMINANCE),400); // latest read occurs after admission
 std::promise<void> done;auto finished=done.get_future();
 effectHook=[&](const std::string& name,int){if(name=="payload")done.set_value();};
 permit.reset();assert(finished.wait_for(3s)==std::future_status::ready);drain(dv);
 const std::vector<std::pair<std::string,int>> expected={{"osd",50},{"gui",400},{"payload",0}};
 assert(snapshot()==expected);assert(!dv.m_deferredWork.Failure());
}
void later_batch_and_retirement(){
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);
 std::promise<void> first,release,second,releaseSecond;auto gate=release.get_future().share(),gate2=releaseSecond.get_future().share();
 std::thread::id firstOwner;const auto caller=std::this_thread::get_id();
 effectHook=[&](const std::string& name,int){
  if(name=="ddr"){firstOwner=std::this_thread::get_id();assert(firstOwner!=caller);first.set_value();gate.wait();assert(std::this_thread::get_id()==firstOwner);}
  if(name=="min"){second.set_value();gate2.wait();}
 };
 changed(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY,1);auto one=first.get_future();assert(one.wait_for(3s)==std::future_status::ready);
 changed(KEY(VS10_TARGET_MIN_LUM),3);changed(KEY(VS10_TARGET_MIN_LUM),9);
 auto two=second.get_future();assert(two.wait_for(50ms)==std::future_status::timeout);
 release.set_value();assert(two.wait_for(3s)==std::future_status::ready);
 assert(!dv.Retire());auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
 releaseSecond.set_value();drain(dv);assert(CAMLSession::TryBeginDisplay(display));assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 const std::vector<std::pair<std::string,int>> expected={{"ddr",1},{"min",9}};assert(snapshot()==expected);
}
void cancel_pending(){
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);
 auto permit=s.AcquireDecoder();assert(permit);
 changed(KEY(OSD_BRIGHTNESS),10);until([&]{return !s.AcquireDecoder();});
 drain(dv);assert(snapshot().empty());assert(s.AcquireDecoder());
 changed(KEY(OSD_BRIGHTNESS),20);assert(dv.Retire()&&snapshot().empty());
}
void exception_does_not_drop_batch(){
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);
 auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());
 changed(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY,1);changed(KEY(VS10_TARGET_MIN_LUM),7);
 until([&]{return !s.AcquireDecoder();});
 std::promise<void> done;auto completed=done.get_future();
 effectHook=[&](const std::string& name,int){if(name=="ddr")throw std::runtime_error("native write");if(name=="min")done.set_value();};
 permit.reset();assert(completed.wait_for(3s)==std::future_status::ready);drain(dv);
 assert(dv.m_deferredWork.Failure()&&s.AcquireDecoder());assert(snapshot().size()==2);
}
void policy_cases(){
 // Every native setting branch is exercised via real OnSettingChanged dispatch.
 struct Case {const char* key;int value;bool on,play;unsigned int output;std::vector<std::pair<std::string,int>> expected;};
 const std::vector<Case> cases={
  {KEY(OSD_BRIGHTNESS),80,true,false,DOLBY_VISION_OUTPUT_MODE_IPT,{}},
  {KEY(OSD_BRIGHTNESS),80,true,true,DOLBY_VISION_OUTPUT_MODE_HDR10,{}},
  {KEY(VS10_HDR10_OSD_BRIGHTNESS),70,true,true,DOLBY_VISION_OUTPUT_MODE_HDR10,{{"hdr",70}}},
  {KEY(VS10_HDR10_OSD_BRIGHTNESS),70,true,true,DOLBY_VISION_OUTPUT_MODE_IPT,{}},
  {KEY(VS10_SDR_BOOST),2,true,true,DOLBY_VISION_OUTPUT_MODE_SDR10,{{"sdr",500}}},
  {KEY(VS10_SDR_SRC_MAX_NITS),600,true,true,DOLBY_VISION_OUTPUT_MODE_SDR8,{{"sdr",600}}},
  {KEY(VS10_SDR_BOOST),1,true,true,DOLBY_VISION_OUTPUT_MODE_IPT,{}},
  {KEY(VS10_SDR_PER_FRAME_METADATA),1,true,true,DOLBY_VISION_OUTPUT_MODE_SDR8,{{"keep",1}}},
  {KEY(VS10_SDR_PER_FRAME_METADATA),1,false,true,DOLBY_VISION_OUTPUT_MODE_SDR10,{}},
  {KEY(VS10_TARGET_MIN_LUM),5,true,false,DOLBY_VISION_OUTPUT_MODE_IPT,{{"min",5}}},
  {KEY(VS10_TARGET_MIN_LUM),5,false,false,DOLBY_VISION_OUTPUT_MODE_IPT,{}},
 };
 for(const auto& test:cases){
  CDolbyVisionAML dv;CAMLSession s;init(dv,s);enabled=test.on;playing=test.play;mode=test.output;
  settings()->Put(KEY(VS10_SDR_BOOST),std::string(test.key)==KEY(VS10_SDR_BOOST)?0:2);settings()->Put(KEY(VS10_SDR_SRC_MAX_NITS),500);
  // Append a marker to the same pending batch, so even a policy no-op is observed.
  auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());
  changed(test.key,test.value);changed(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY,1);
  std::promise<void> done;auto completed=done.get_future();effectHook=[&](const std::string& name,int){if(name=="ddr")done.set_value();};
  permit.reset();assert(completed.wait_for(3s)==std::future_status::ready);drain(dv);
  auto expected=test.expected;expected.emplace_back("ddr",1);assert(snapshot()==expected);
 }
 for(const auto* key:{KEY(LEVEL5),KEY(STD_SOURCE_LEVEL_5),KEY(STD_SOURCE_LEVEL_5_OSDST),KEY(LEVEL5_SIGNAL_SUBS),KEY(DETECT_ACTIVE_AREA),KEY(L5_AUTO_LETTERBOX),KEY(LEVEL5_OVERRIDE)}){
  CDolbyVisionAML dv;CAMLSession s;init(dv,s);overrideActive=true;
  std::promise<void> done;auto completed=done.get_future();
  effectHook=[&](const std::string& name,int){if(name=="payload"||name=="stop")done.set_value();};
  changed(key,1);assert(completed.wait_for(3s)==std::future_status::ready);drain(dv);
  const std::vector<std::pair<std::string,int>> expected={{"override",0},{"l5",0},{std::string(key)==KEY(LEVEL5_OVERRIDE)?"stop":"payload",0}};
  assert(snapshot()==expected);
 }
}
int main(){ordered_fresh_apply();later_batch_and_retirement();cancel_pending();exception_does_not_drop_batch();policy_cases();std::cout<<"PASS: ordered native settings continuations, policy and retirement (ASan/UBSan)\n";}
'''

def source():
    dv=(ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    methods='\n'.join(function(dv,sig) for sig in (
      'void CDolbyVisionAML::schedule_native_setting_apply(', 'void CDolbyVisionAML::apply_native_setting(',
      'void CDolbyVisionAML::OnSettingChanged(', 'void CDolbyVisionAML::schedule_vsvdb_payload_apply(',
      'bool CDolbyVisionAML::Retire()', 'CDolbyVisionAML::~CDolbyVisionAML()'))
    boost=function((ROOT/'xbmc/utils/AMLUtils.cpp').read_text(),'int aml_dv_sdr_boost_param()')
    declaration=function((ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.h').read_text(),'class CDolbyVisionAML')
    ids=sorted(set(re.findall(r'CSettings::(SETTING_\w+)',methods+boost)))
    return PREFIX.replace('@IDS@','\n'.join(f'static constexpr const char* {i}="{i}";' for i in ids)).replace('@CLASS@',declaration).replace('@BOOST@',boost).replace('@METHODS@',methods)+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        for label,old,new in [
          ('callback performs native write','schedule_native_setting_apply(setting->GetId());','apply_native_setting(setting->GetId());'),
          ('bypass admission','m_deferredWork.ScheduleNative(std::chrono::milliseconds(0)','m_deferredWork.Schedule(std::chrono::milliseconds(0)'),
          ('lose coalescing/order','pending.erase(std::remove(pending.begin(), pending.end(), settingId), pending.end());','/* keep duplicate intents */'),
          ('lose changes during apply','m_nativeSettingsScheduled = false;\n      }','/* never rearm */\n      }'),
          ('drop rest of failed batch','failure = std::current_exception();','failure = std::current_exception();\n          break;'),
          ('ignore playback policy','aml_is_dv_enable() && aml_dv_playback_active() &&','aml_is_dv_enable() &&'),
          ('omit L5 flags','    aml_dv_apply_l5_sysfs();','    /* omit flags */'),
        ]:
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
