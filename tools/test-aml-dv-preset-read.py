#!/usr/bin/env python3
"""Real recursive setting locks, loaded values and pending GUI preset retries."""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]


def source():
    manager = runpy.run_path(str(ROOT / "tools/test-aml-dv-manager-dependency.py"))
    oracle, code = manager["source"]()
    code = code.split("int main(){")[0]
    code = oracle["replace"](code, "virtual ~CSetting()=default;",
                             "virtual ~CSetting()=default;virtual void Reset(){}")
    setting = (ROOT / "xbmc/settings/lib/Setting.cpp").read_text()
    header = (ROOT / "xbmc/settings/lib/Setting.h").read_text()
    integer = header[header.index("class CSettingInt "):]
    extra = r"""
 void SetDefault(int value);
 bool FromString(const std::string& value);
 static bool fromString(const std::string& text,int& value){
   std::istringstream input(text);input>>value;return input&&!input.rdbuf()->in_avail();
 }
""" + oracle["function"](integer, "void Reset() override")
    code = "#include <sstream>\n#include <stdexcept>\n" + code
    code = oracle["replace"](code, " bool CheckValidity(int)const{return true;}",
                             " bool CheckValidity(int)const{return true;}" + extra)
    code += oracle["function"](setting, "void CSettingInt::SetDefault(")
    code += oracle["function"](setting, "bool CSettingInt::FromString(")
    return oracle, code + TESTS


TESTS = r"""
struct NestedRead:Callback {
 CSettingsManager& manager;int reads=0;bool reject=false,throwChange=false;
 explicit NestedRead(CSettingsManager& value):manager(value){}
 bool OnSettingChanging(const SettingConstPtr& setting)override{
   const auto integer=std::static_pointer_cast<const CSettingInt>(setting);
   for(int index=0;index<8;++index){
     int current=-9;assert(integer->TryGetValue(current));
     assert(current==integer->GetValue());++reads;
   }
   if(throwChange)throw std::runtime_error("controlled callback rejection");
   return !reject&&manager.OnSettingChanging(setting);
 }
 void OnSettingChanged(const SettingConstPtr& setting)override{
   manager.OnSettingChanged(setting);
 }
};
void exclusive_available(CSettingInt& setting,bool expected){
 auto result=std::async(std::launch::async,[&]{
   if(!setting.m_critical.try_lock())return false;
   setting.m_critical.unlock();return true;
 });
 assert(result.get()==expected);
}
int main(){
 auto display=CAMLSession::FenceDisplay();
 assert(CAMLSession::TryBeginDisplay(display));
 assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 auto stream=decoder.Fence();assert(decoder.BeginMutation(stream)&&decoder.Complete(stream,true));
 files[dvPath]="";files[hdrPath]="";concurrencyGate=false;
 auto preset=std::make_shared<CSettingInt>(),type=std::make_shared<CSettingInt>();
 preset->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET;
 type->id=CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE;
 type->m_value=DV_TYPE_PLAYER_LED_LLDV;type->m_optionsFiller=dv_type_filler;
 optionsSettings.presetValue=preset;optionsSettings.typeValue=type;
 CDolbyVisionAML callback;CSettingsManager settingsManager(&callback);
 auto lease=std::make_shared<CSettingsManager::Lease>();lease->callback=&callback;
 settingsManager.m_settings[preset->id]={preset,{{1,lease}},
   {{type->id,{CSettingDependency{}}}}};
 settingsManager.m_settings[type->id]={type,{},{}};
 NestedRead nested(settingsManager);preset->m_callback=&nested;

 // Load uses FromString while manager callbacks/dependencies are suppressed.
 settingsManager.m_loaded=false;preset->SetDefault(TV_PRESET_AUTO);
 assert(preset->FromString(std::to_string(TV_PRESET_MANUAL)));
 assert(values(type->UpdateDynamicOptions())==std::vector<int>({1,4}));
 preset->SetDefault(TV_PRESET_TCL);
 int current=-1;assert(preset->TryGetValue(current)&&current==TV_PRESET_MANUAL);
 settingsManager.m_loaded=true;preset->Reset();
 assert(preset->GetValue()==TV_PRESET_TCL&&!type->DynamicOptionsPending());
 assert(values(type->m_dynamicOptions)==std::vector<int>({4}));
 assert(!preset->FromString("invalid"));assert(preset->GetValue()==TV_PRESET_TCL);
 nested.reject=true;assert(!preset->SetValue(TV_PRESET_MANUAL));
 nested.reject=false;assert(preset->GetValue()==TV_PRESET_TCL);
 nested.throwChange=true;
 try{preset->SetValue(TV_PRESET_MANUAL);assert(false);}
 catch(const std::runtime_error&){}
 nested.throwChange=false;
 assert(preset->TryGetValue(current)&&current==TV_PRESET_MANUAL);
 assert(preset->SetValue(TV_PRESET_AUTO));assert(preset->SetValue(TV_PRESET_MANUAL));
 assert(nested.reads>=48);

 {
   std::unique_lock<CSharedSection> exclusive(preset->m_critical);
   for(int index=0;index<32;++index){
     int observed=-99;assert(preset->TryGetValue(observed)&&observed==TV_PRESET_MANUAL);
   }
   exclusive_available(*preset,false);
 }
 exclusive_available(*preset,true); // no leaked shared count or changed exclusive ownership
 auto independent=std::make_shared<CSettingInt>();independent->SetDefault(TV_PRESET_AUTO);
 assert(independent->TryGetValue(current)&&current==TV_PRESET_AUTO);
 assert(preset->TryGetValue(current)&&current==TV_PRESET_MANUAL);

 assert(values(type->UpdateDynamicOptions())==std::vector<int>({1,4}));
 auto control=std::make_shared<CGUIControlSpinExSetting>();control->m_pSetting=type;
 CGUIDialogSettingsBase dialog;dialog.m_settingControls={control};
 std::promise<void> locked,change,newPreset,finish;
 auto changeNow=change.get_future(),finishNow=finish.get_future();
 std::thread writer([&]{
   std::unique_lock<CSharedSection> held(preset->m_critical);
   locked.set_value();changeNow.wait();
   assert(preset->SetValue(TV_PRESET_AUTO));newPreset.set_value();finishNow.wait();
 });
 assert(locked.get_future().wait_for(2s)==std::future_status::ready);
 current=123;assert(!preset->TryGetValue(current)&&current==123);
 IntegerSettingOptions untouched{{"sentinel",99}};int selected=DV_TYPE_PLAYER_LED_LLDV;
 try{dv_type_filler(type,untouched,selected,nullptr);assert(false);}
 catch(const CSettingOptionsPending&){}
 assert(values(untouched)==std::vector<int>({99})&&selected==DV_TYPE_PLAYER_LED_LLDV);
 control->UpdateFromSetting();
 assert(type->DynamicOptionsPending()&&type->m_dynamicOptions.empty());
 assert(control->spin.IsDisabled()&&type->m_value==DV_TYPE_PLAYER_LED_LLDV);
 dialog.RetryPendingOptions(250);assert(type->DynamicOptionsPending());
 change.set_value();
 assert(newPreset.get_future().wait_for(2s)==std::future_status::ready);
 assert(!type->DynamicOptionsPending()&&values(type->m_dynamicOptions)==std::vector<int>({4}));
 control->UpdateFromSetting();assert(type->DynamicOptionsPending()&&type->m_dynamicOptions.empty());
 dialog.RetryPendingOptions(500);assert(type->DynamicOptionsPending()&&control->spin.IsDisabled());
 finish.set_value();writer.join();exclusive_available(*preset,true);
 dialog.RetryPendingOptions(750);
 assert(!type->DynamicOptionsPending()&&!control->spin.IsDisabled());
 assert(values(type->m_dynamicOptions)==std::vector<int>({4}));
 assert(type->m_value==DV_TYPE_PLAYER_LED_LLDV); // keep actual value, not stale offered choices
 assert(preset->SetValue(TV_PRESET_MANUAL));
 assert(values(type->m_dynamicOptions)==std::vector<int>({1,4}));
 files[dvPath]="LL_YCbCr_422_12BIT";assert(preset->SetValue(TV_PRESET_AUTO));
 assert(values(type->m_dynamicOptions)==std::vector<int>({1,4}));
 std::cout<<"PASS: recursive nested reads retain lock ownership/balance; load/default/reset/rejection "
             "stay truthful; busy preset clears stale exposed choices and GUI retries current policy\n";
}
"""

if __name__ == "__main__":
    oracle, code = source()
    oracle["dv"]["run"](code)
