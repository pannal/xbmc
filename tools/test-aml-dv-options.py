#!/usr/bin/env python3
"""Exercise production DV fillers, integer pending publication and GUI retries.

Real native admission; sysfs, setting storage and widgets are substitutes.
"""
import argparse
import json
from pathlib import Path
import runpy
ROOT=Path(__file__).resolve().parents[1]
f=runpy.run_path(str(ROOT/'tools/test-aml-native-continuation.py'))
function,run=f['function'],f['run']
PREFIX=r'''
#include "windowing/amlogic/AMLNativeTransaction.h"
#include "settings/lib/SettingOptionsPending.h"
#include <atomic>
#include <cassert>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <iterator>
#include <unistd.h>
#include <map>
#include <optional>
#include <string>
#include <vector>
using CSharedSection=std::recursive_mutex;
enum class SettingType {Integer,List,String};
struct CSetting {virtual ~CSetting()=default;virtual SettingType GetType()const{return SettingType::Integer;}};
using SettingConstPtr=std::shared_ptr<const CSetting>;
struct IntegerSettingOption {std::string label;int value;IntegerSettingOption(std::string l,int v):label(std::move(l)),value(v){}};
using IntegerSettingOptions=std::vector<IntegerSettingOption>;
using IntegerSettingOptionsFiller=void(*)(const SettingConstPtr&,IntegerSettingOptions&,int&,void*);
struct Manager {void* GetSettingOptionsFiller(const SettingConstPtr&){return nullptr;}};
struct Logger {template<class...T> void warn(T&&...) {}};
struct CSettingInt: CSetting,std::enable_shared_from_this<CSettingInt> {
 CSharedSection m_critical;
 IntegerSettingOptionsFiller m_optionsFiller=nullptr;
 std::string m_optionsFillerName,m_id;
 Manager* m_settingsManager=nullptr;void* m_optionsFillerData=nullptr;
 int m_value=42,writes=0,notifications=0;
 IntegerSettingOptions m_dynamicOptions;
 std::atomic<bool> m_dynamicOptionsPending{false};
 Logger logger;Logger* s_logger=&logger;
 template<class T>std::shared_ptr<T> shared_from_base(){return std::static_pointer_cast<T>(shared_from_this());}
 void SetValue(int v){m_value=v;++writes;}
 void OnSettingPropertyChanged(const SettingConstPtr&,const char*){++notifications;}
 bool DynamicOptionsPending()const{return m_dynamicOptionsPending.load();}
 IntegerSettingOptions UpdateDynamicOptions(bool* pending=nullptr);
};
struct CSettingList: CSetting {std::shared_ptr<CSetting> definition;SettingType GetType()const override{return SettingType::List;}auto GetDefinition(){return definition;}};
@UPDATE@
bool forceModes=false;
static bool force_modes(){return forceModes;}
struct Localize {std::string Get(int id){return std::to_string(id);}} g_localizeStrings;
enum DV_TYPE {DV_TYPE_DISPLAY_LED,DV_TYPE_PLAYER_LED_LLDV,DV_TYPE_PLAYER_LED_HDR,DV_TYPE_PLAYER_LED_HDR2,DV_TYPE_VS10_ONLY};
constexpr int DOLBY_VISION_OUTPUT_MODE_BYPASS=5,DOLBY_VISION_OUTPUT_MODE_SDR10=4,DOLBY_VISION_OUTPUT_MODE_HDR10=2,DOLBY_VISION_OUTPUT_MODE_IPT=1;
CAMLSession decoder;
std::map<std::string,std::optional<std::string>> files;
int reads=0;
struct CSysfsPath {
 std::string path;explicit CSysfsPath(const char* p):path(p){}
 bool Exists(){assert(!decoder.AcquireDecoder());return files.count(path);}
};
struct TempFiles {
 std::filesystem::path directory=std::filesystem::temp_directory_path()/("dv-options-"+std::to_string(getpid()));
 TempFiles(){std::filesystem::create_directory(directory);}
 ~TempFiles(){std::filesystem::remove_all(directory);}
 std::filesystem::path Path(const char* p){
  assert(!decoder.AcquireDecoder());++reads;
  const auto path=directory/(std::string(p).find("hdr_cap")!=std::string::npos?"hdr":"dv");
  if(files.at(p)){std::ofstream out(path);out<<*files.at(p);}
  else std::filesystem::remove(path); // Exists passed, but the actual open now fails.
  return path;
 }
} tempFiles;
struct FixtureFile:std::ifstream {explicit FixtureFile(const char* p):std::ifstream(tempFiles.Path(p)) {}};
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
@FILLERS@
#pragma GCC diagnostic pop
struct Spin {
 bool disabled=false;
 void SetEnabled(bool enabled){disabled=!enabled;}
 bool IsDisabled()const{return disabled;}
 int GetMaximum()const{return 3;}int GetMinimum()const{return 0;}
};
struct CGUIControlBaseSetting {
 std::shared_ptr<CSetting> m_pSetting;
 Spin spin;
 virtual ~CGUIControlBaseSetting()=default;
 auto GetSetting(){return m_pSetting;}
 virtual void Update(bool,bool){spin.SetEnabled(true);}
 void UpdateFromSetting(){Update(false,false);}
};
struct CGUIControlSpinExSetting:CGUIControlBaseSetting {
 Spin* m_pSpin=&spin;
 int updates=0;
 void FillControl(bool refresh){++updates;if(refresh)std::static_pointer_cast<CSettingInt>(m_pSetting)->UpdateDynamicOptions();}
 void Update(bool fromControl,bool updateDisplayOnly)override;
};
@SPIN@
struct CGUIDialogSettingsBase {
 unsigned int m_optionsRetryTime{0};
 std::vector<std::shared_ptr<CGUIControlBaseSetting>> m_settingControls;
 void RetryPendingOptions(unsigned int currentTime);
};
@RETRY@
'''
TESTS=r'''
const std::string dvPath="/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap",hdrPath="/sys/class/amhdmitx/amhdmitx0/hdr_cap";
std::vector<int> values(const IntegerSettingOptions& o){std::vector<int> v;for(const auto& x:o)v.push_back(x.value);return v;}
void expect(IntegerSettingOptionsFiller filler,std::vector<int> expected){
 auto setting=std::make_shared<CSettingInt>();setting->m_optionsFiller=filler;
 assert(values(setting->UpdateDynamicOptions())==expected);assert(!setting->DynamicOptionsPending()&&setting->writes==0);
}
void policy(){
 files.clear();expect(dv_type_filler,{4});expect(dv_processor_filler,{0});
 expect(vs10_sdr_filler,{5,4});expect(vs10_hdr10_filler,{4});expect(vs10_hdr_hlg_filler,{4});expect(vs10_dv_filler,{4});
 files[dvPath]="DV_RGB_444_8BIT LL_YCbCr_422_12BIT";files[hdrPath]="SMPTE ST 2084: 1 Hybrid Log-Gamma: 1";
 expect(dv_type_filler,{0,1,2,3,4});expect(dv_processor_filler,{0,2,1,4,3});
 expect(vs10_sdr_filler,{5,4,2,1});expect(vs10_hdr10_filler,{5,4,2,1});expect(vs10_hdr_hlg_filler,{5,4,2,1});expect(vs10_dv_filler,{4,1});
 files[dvPath]="The Rx don't support DolbyVision";files[hdrPath]="SMPTE ST 2084: 1";
 expect(dv_type_filler,{2,3,4});expect(vs10_hdr_hlg_filler,{4,2,1});
 files[hdrPath]="Hybrid Log-Gamma: 1";expect(vs10_hdr_hlg_filler,{5,4});expect(vs10_dv_filler,{4});
 auto permit=decoder.AcquireDecoder();assert(permit);forceModes=true;const int before=reads;
 expect(dv_type_filler,{0,1,2,3,4});expect(dv_processor_filler,{0,2,1,4,3});
 expect(vs10_sdr_filler,{5,4,2,1});expect(vs10_hdr10_filler,{5,4,2,1});expect(vs10_hdr_hlg_filler,{5,4,2,1});expect(vs10_dv_filler,{4,1});assert(reads==before);forceModes=false;
}
void pending(){
 files[dvPath]="DV_RGB_444_8BIT";files[hdrPath]="SMPTE ST 2084: 1";
 auto setting=std::make_shared<CSettingInt>();setting->m_optionsFiller=dv_type_filler;
 assert(!setting->UpdateDynamicOptions().empty());
 auto control=std::make_shared<CGUIControlSpinExSetting>();control->m_pSetting=setting;
 CGUIDialogSettingsBase dialog;dialog.m_settingControls={control};
 auto permit=std::make_unique<CAMLSession::Permit>(decoder.AcquireDecoder());assert(*permit);
 const int before=reads;control->UpdateFromSetting();
 assert(reads==before&&setting->DynamicOptionsPending()&&setting->m_dynamicOptions.empty());
 assert(setting->m_value==42&&setting->writes==0&&control->spin.IsDisabled());
 // A synchronous failed attempt releases its fence, so other decoder work can progress.
 assert(decoder.AcquireDecoder());
 dialog.RetryPendingOptions(100);assert(control->updates==1);
 dialog.RetryPendingOptions(250);assert(control->updates==2&&reads==before);
 // Replace capability while blocked; retry reads current data, never the old choices.
 files[dvPath]="LL_YCbCr_422_12BIT";files[hdrPath]="";permit.reset();
 dialog.RetryPendingOptions(500);
 assert(!setting->DynamicOptionsPending()&&!control->spin.IsDisabled());
 assert(values(setting->m_dynamicOptions)==std::vector<int>({1,4}));
 const int updates=control->updates;dialog.RetryPendingOptions(750);assert(control->updates==updates);
 // Transient read failure is pending, not unsupported and not a stale result.
 files[dvPath]=std::nullopt;control->UpdateFromSetting();assert(setting->DynamicOptionsPending()&&setting->m_dynamicOptions.empty());
 files[dvPath]="";dialog.RetryPendingOptions(1000);assert(!setting->DynamicOptionsPending());
 // No work survives category replacement/window closure.
 files[dvPath]=std::nullopt;control->UpdateFromSetting();dialog.m_settingControls.clear();const int finalReads=reads;
 dialog.RetryPendingOptions(1250);assert(reads==finalReads&&decoder.AcquireDecoder());
}
void ready(const SettingConstPtr&,IntegerSettingOptions& options,int& current,void*){
 options.emplace_back("ready",7);current=7;
}
void partial(const SettingConstPtr&,IntegerSettingOptions& options,int& current,void*){
 options.emplace_back("partial",99);current=99;throw CSettingOptionsPending{};
}
int main(){
 auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 auto stream=decoder.Fence();assert(decoder.BeginMutation(stream)&&decoder.Complete(stream,true));
 policy();pending();auto setting=std::make_shared<CSettingInt>();setting->m_optionsFiller=partial;
 bool resultPending=false;assert(setting->UpdateDynamicOptions(&resultPending).empty()&&resultPending&&setting->m_value==42&&setting->writes==0);
 assert(setting->DynamicOptionsPending());
 setting->m_optionsFiller=ready;assert(values(setting->UpdateDynamicOptions(&resultPending))==std::vector<int>({7}));
 assert(!resultPending&&!setting->DynamicOptionsPending()&&setting->m_value==7&&setting->writes==1);
 std::cout<<"PASS: six DV/VS10 filler policies, pending value preservation, fresh retries and GUI retirement\n";
}
'''
def source():
    dv=(ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    setting=(ROOT/'xbmc/settings/lib/Setting.cpp').read_text()
    dialog=(ROOT/'xbmc/settings/dialogs/GUIDialogSettingsBase.cpp').read_text()
    gui=(ROOT/'xbmc/settings/windows/GUIControlSettings.cpp').read_text()
    parts=[function(dv,'struct AMLDVOptions')+';',
           function(dv,'static std::string read_dv_option_file(').replace('std::ifstream stream(path);','FixtureFile stream(path);'),
           function(dv,'static AMLDVOptions read_dv_options()')]
    names=['dv_type_filler','dv_processor_filler','add_vs10_bypass','add_vs10_dv_bypass','add_vs10_sdr','add_vs10_hdr10','add_vs10_dv','vs10_sdr_filler','vs10_hdr10_filler','vs10_hdr_hlg_filler','vs10_dv_filler']
    parts += [function(dv,'void '+name+'(') for name in names]
    code=PREFIX
    for key,value in [('FILLERS','\n'.join(parts)),('UPDATE',function(setting,'IntegerSettingOptions CSettingInt::UpdateDynamicOptions(')),('SPIN',function(gui,'void CGUIControlSpinExSetting::Update(')),('RETRY',function(dialog,'void CGUIDialogSettingsBase::RetryPendingOptions('))]:
        code=code.replace('@'+key+'@',value)
    assert 'RetryPendingOptions(currentTime);' in function(dialog,'void CGUIDialogSettingsBase::DoProcess(')
    assert 'm_dynamicOptionsPending = setting.m_dynamicOptionsPending.load();' in function(setting,'void CSettingInt::copy(')
    rpc=(ROOT/'xbmc/interfaces/json-rpc/SettingsOperations.cpp').read_text()
    assert 'UpdateDynamicOptions(&pending)' in function(rpc,'bool CSettingsOperations::SerializeSettingInt(')
    assert 'obj["optionspending"] = pending;' in rpc
    schema=json.loads((ROOT/'xbmc/interfaces/json-rpc/schema/types.json').read_text())
    assert schema['Setting.Details.SettingInt']['properties']['optionspending']['type']=='boolean'
    return code+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        for label,old,new in [
          ('bypass admission','if (!native.TryBegin())','if (false && !native.TryBegin())'),
          ('retain stale choices','m_dynamicOptions.clear();','/* stale retained */'),
          ('apply partial value','return {}; // Do not apply','SetValue(bestMatchingValue); return {}; // Do not apply'),
          ('lose pending retry','m_dynamicOptionsPending = true;','m_dynamicOptionsPending = false;'),
          ('cache prior sink','const auto dv = read_dv_option_file(','static const auto dv = read_dv_option_file('),
          ('force mode consults native','if (force_modes())','if (false && force_modes())'),
          ('failed open becomes unsupported','if (!stream.is_open())','if (false && !stream.is_open())'),
        ]:
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)
if __name__=='__main__':main()
