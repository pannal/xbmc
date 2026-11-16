#!/usr/bin/env python3
"""VIDEO-2: paired threads must finish preset SetValue and type options.

Runs production SetValue/GetValue/UpdateDynamicOptions, CSetting callback
dispatch, full DV callback/filler and CSharedSection. Only OS condition-variable,
settings lookup, native/sysfs and effects use the existing host adapters.
This is a real two-thread lock cycle, not an ordering-only source assertion.
"""
import argparse
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parents[1]
dv = runpy.run_path(str(ROOT / "tools/test-aml-dv-options.py"))
function = dv["function"]


def replace(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


def source(case="concurrent"):
    code = dv["source"]().split("int main(){")[0]
    setting = (ROOT / "xbmc/settings/lib/Setting.cpp").read_text()
    header = (ROOT / "xbmc/settings/lib/Setting.h").read_text()
    shared = (ROOT / "xbmc/threads/SharedSection.h").read_text()
    production = (ROOT / "xbmc/windowing/amlogic/DolbyVisionAML.cpp").read_text()
    callback = function(production, "void CDolbyVisionAML::OnSettingChanged(")
    code = replace(code, "using CSharedSection=std::shared_mutex;", r"""
#include <condition_variable>
#include <future>
#include <thread>
#include <shared_mutex>
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
namespace XbmcThreads {
struct ConditionVariable {
  std::condition_variable_any value;
  template<class L,class P> void wait(L& lock,P predicate){value.wait(lock,predicate);}
  void notifyAll(){value.notify_all();}
};
}
""" + function(shared, "class CSharedSection") + ";")
    code = replace(code,
                   "struct CSetting {virtual ~CSetting()=default;virtual SettingType GetType()const{return SettingType::Integer;}};",
                   r"""
struct CSetting;
struct Callback {
  virtual ~Callback()=default;
  virtual bool OnSettingChanging(const std::shared_ptr<const CSetting>&)=0;
  virtual void OnSettingChanged(const std::shared_ptr<const CSetting>&)=0;
};
struct CSetting {
  virtual ~CSetting()=default;
  virtual SettingType GetType()const{return SettingType::Integer;}
  std::string id;
  const std::string& GetId()const{return id;}
  Callback* m_callback=nullptr;
  bool OnSettingChanging(const std::shared_ptr<const CSetting>&);
  void OnSettingChanged(const std::shared_ptr<const CSetting>&);
};
""" + function(setting, "bool CSetting::OnSettingChanging(")
                   + function(setting, "void CSetting::OnSettingChanged("))
    code = replace(code, " void SetValue(int v){m_value=v;++writes;}", r"""
 int m_default=42;bool m_changed=false;
 bool CheckValidity(int)const{return true;}
 bool SetValue(int value);
 """ + function(header, "int GetValue() const"))
    code = replace(code, "IntegerSettingOptions CSettingInt::UpdateDynamicOptions(",
                   function(setting, "bool CSettingInt::SetValue(") +
                   "\nIntegerSettingOptions CSettingInt::UpdateDynamicOptions(")
    ids = sorted(set(re.findall(r"CSettings::(SETTING_\w+)", callback)))
    constants = "\n".join(
        'static constexpr auto ' + name + '="' +
        ("preset" if name.endswith("_DV_TV_PRESET") else name) + '";' for name in ids)
    code = replace(code,
                   'struct CSettings {static constexpr auto SETTING_COREELEC_AMLOGIC_DV_TV_PRESET="preset";};',
                   "struct CSettings {" + constants + "};")
    code = replace(code, """struct Settings {
 int preset=1;
 int GetInt(const char*) const{return preset;}
 std::shared_ptr<CSetting> GetSetting(const char*) const{
   auto value=std::make_shared<CSettingInt>();value->m_value=preset;return value;
 }
} optionsSettings;""", r"""
static thread_local bool enumerating=false;
static std::atomic<bool> optionsReadsPreset{false};
struct Settings {
 int preset=1;
 std::shared_ptr<CSettingInt> typeValue,presetValue;
 std::shared_ptr<CSetting> GetSetting(const char* key)const{
   assert(std::string(key)==CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET);
   if(enumerating)optionsReadsPreset=true;
   return presetValue;
 }
 int GetInt(const char* key)const{
   const std::string id(key);
   if(id==CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET){
     if(enumerating)optionsReadsPreset=true;
     return presetValue?presetValue->GetValue():preset;
   }
   if(id==CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)return typeValue->GetValue();
   return 0;
 }
 bool GetBool(const char*)const{return false;}
 void SetInt(const char*,int){assert(false && "unexpected preset policy write");}
 void SetBool(const char*,bool){assert(false && "unexpected preset policy write");}
} optionsSettings;""")
    code += r"""
constexpr int LOGINFO=1;
struct CLog {template<class...T>static void Log(T&&...){}};
enum DV_MODE {DV_MODE_OFF,DV_MODE_ON,DV_MODE_ON_DEMAND};
bool aml_display_support_dv_std(){return false;}
bool aml_support_dolby_vision(){return false;}
void set_visible(const char*,bool){}
void set_dv_settings_visible(bool){}
void set_vsvdb_children_visible(bool){}
std::promise<void> presetHeld,releasePreset;
bool concurrencyGate=true;
struct CDolbyVisionAML:Callback {
  bool m_retiring=false,m_applying_tv_preset=false;
  std::atomic<bool> m_tv_preset_apply_scheduled{false},m_applying_vsvdb{false};
  bool OnSettingChanging(const std::shared_ptr<const CSetting>&)override{
    if(!concurrencyGate)return true;
    presetHeld.set_value();
    releasePreset.get_future().wait();
    return true;
  }
  void OnSettingChanged(const std::shared_ptr<const CSetting>&)override;
  void schedule_tv_preset_apply(int){}
  void schedule_native_setting_apply(const std::string&){}
  void schedule_vsvdb_payload_apply(){}
};
"""
    code += callback
    if case == "serial":
        return code + SERIAL
    return code + r"""
int main(){
 auto display=CAMLSession::FenceDisplay();
 assert(CAMLSession::TryBeginDisplay(display));
 assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 auto stream=decoder.Fence();assert(decoder.BeginMutation(stream)&&decoder.Complete(stream,true));
 auto preset=std::make_shared<CSettingInt>(),type=std::make_shared<CSettingInt>();
 preset->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET;preset->m_value=TV_PRESET_AUTO;
 type->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE;type->m_value=DV_TYPE_PLAYER_LED_LLDV;
 type->m_optionsFiller=dv_type_filler;
 optionsSettings.presetValue=preset;optionsSettings.typeValue=type;
 CDolbyVisionAML callback;preset->m_callback=&callback;
 std::promise<void> presetDone,optionsDone;
 auto held=presetHeld.get_future(),presetFinished=presetDone.get_future(),optionsFinished=optionsDone.get_future();
 std::thread writer([&]{assert(preset->SetValue(TV_PRESET_MANUAL));presetDone.set_value();});
 assert(held.wait_for(2s)==std::future_status::ready);
 const bool setterOwnsLock=!preset->m_critical.try_lock();
 if(!setterOwnsLock)preset->m_critical.unlock();
 std::thread reader([&]{enumerating=true;type->UpdateDynamicOptions();optionsDone.set_value();});
 const auto deadline=std::chrono::steady_clock::now()+2s;
 while(!optionsReadsPreset && optionsFinished.wait_for(0ms)!=std::future_status::ready &&
       std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
 assert(optionsReadsPreset || optionsFinished.wait_for(0ms)==std::future_status::ready);
 releasePreset.set_value(); // synchronous production callback now tries the type read
 const bool writerReady=presetFinished.wait_for(2s)==std::future_status::ready;
 const bool readerReady=optionsFinished.wait_for(2s)==std::future_status::ready;
 std::cerr<<"VIDEO-2 paired threads: setterOwnsLock="<<setterOwnsLock
          <<" optionsReadsPreset="<<optionsReadsPreset
          <<" writerReady="<<writerReady<<" readerReady="<<readerReady<<"\n";
 assert(writerReady && readerReady &&
        "VIDEO-2 real preset setter/type options threads deadlocked across production setting locks");
 writer.join();reader.join();
 assert(preset->GetValue()==TV_PRESET_MANUAL);
 std::cout<<"PASS: paired preset setter/type enumeration completed without ABBA\n";
}
"""

SERIAL = r"""
int main(){
 auto display=CAMLSession::FenceDisplay();
 assert(CAMLSession::TryBeginDisplay(display));
 assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 auto stream=decoder.Fence();assert(decoder.BeginMutation(stream)&&decoder.Complete(stream,true));
 auto preset=std::make_shared<CSettingInt>(),type=std::make_shared<CSettingInt>();
 preset->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET;preset->m_value=TV_PRESET_AUTO;
 type->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE;type->m_value=DV_TYPE_PLAYER_LED_LLDV;
 type->m_optionsFiller=dv_type_filler;
 optionsSettings.presetValue=preset;optionsSettings.typeValue=type;
 CDolbyVisionAML callback;preset->m_callback=&callback;concurrencyGate=false;
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({4}));
 assert(preset->SetValue(TV_PRESET_MANUAL));
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({1,4}));
 for(int automatic=TV_PRESET_AUTO;automatic<=TV_PRESET_TCL;++automatic){
   assert(preset->SetValue(automatic));
   assert(values(type->UpdateDynamicOptions())==std::vector<int>({4}));
 }
 files[dvPath]="LL_YCbCr_422_12BIT";
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({1,4}));
 files[dvPath]="";forceModes=true;
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({0,1,2,3,4}));
 forceModes=false;
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({4}));
 std::cout<<"PASS: actual preset SetValue/callback changes preserve Manual-only opt-in and automatic EDID gating\n";
}
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["concurrent", "serial"], default="concurrent")
    dv["run"](source(parser.parse_args().case))
