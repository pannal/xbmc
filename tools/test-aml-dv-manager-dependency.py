#!/usr/bin/env python3
"""VIDEO-2: real manager callback and XML-dependent options across paired threads."""
import argparse
from pathlib import Path
import runpy
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def source(root=ROOT):
    oracle = runpy.run_path(str(root / "tools/test-aml-dv-setting-locks.py"))
    code = oracle["source"]()
    function = oracle["function"]
    manager = (root / "xbmc/settings/lib/SettingsManager.cpp").read_text()
    xml = ET.parse(root / "system/settings/settings.xml")
    type_setting = xml.find(".//setting[@id='coreelec.amlogic.dolbyvision.type']")
    dependencies = type_setting.findall("dependencies/dependency")
    dependency = next(dep for dep in dependencies if dep.get("type") == "update" and
                      dep.get("setting") == "coreelec.amlogic.dolbyvision.tv.preset")
    code = code.replace('"preset"', '"' + dependency.get("setting") + '"')
    code = code.replace('"SETTING_COREELEC_AMLOGIC_DV_TYPE"',
                        '"' + type_setting.get("id") + '"')
    code = code.replace("struct CSetting {", """
enum class SettingOptionsType {Unknown,Dynamic};
struct CSetting {""", 1)
    code = code.replace(" int m_default=42;bool m_changed=false;",
                        " SettingOptionsType GetOptionsType()const{return SettingOptionsType::Dynamic;}\n"
                        " int m_default=42;bool m_changed=false;", 1)
    definitions = r"""
#include <map>
using SettingPtr=std::shared_ptr<CSetting>;
enum class SettingDependencyType {Enable,Update,Visible,Unknown};
struct CSettingDependency {
 SettingDependencyType GetType()const{return SettingDependencyType::Update;}
};
using SettingDependencyMap=std::map<std::string,std::vector<CSettingDependency>>;
using SettingConstPtr=std::shared_ptr<const CSetting>;
struct CSettingString:CSetting {
 SettingOptionsType GetOptionsType()const{return SettingOptionsType::Unknown;}
 void UpdateDynamicOptions(){assert(false);}
};
struct CSettingsManager:Callback {
 struct Lease {Callback* callback;Callback* Acquire(){return callback;}};
 struct Setting {
   SettingPtr setting;std::map<int,std::shared_ptr<Lease>> callbacks;
   SettingDependencyMap dependencies;
 };
 mutable CSharedSection m_settingsCritical;bool m_loaded=true;
 std::map<std::string,Setting> m_settings;
 CDolbyVisionAML* dv;
 explicit CSettingsManager(CDolbyVisionAML* callback):dv(callback){}
 auto FindSetting(const std::string& id){return m_settings.find(id);}
 auto FindSetting(const std::string& id)const{return m_settings.find(id);}
 bool OnSettingChanging(const std::shared_ptr<const CSetting>& setting)override{
   return dv->OnSettingChanging(setting);
 }
 void OnSettingChanged(const std::shared_ptr<const CSetting>&)override;
 void OnSettingPropertyChanged(const std::shared_ptr<const CSetting>&,const char*){}
 void RefreshVisibilityAndEnableStatus(const std::shared_ptr<const CSetting>&){}
 SettingDependencyMap GetDependencies(const std::string&)const;
 SettingDependencyMap GetDependencies(const SettingConstPtr&)const;
 void UpdateSettingByDependency(const std::string&,const CSettingDependency&);
 void UpdateSettingByDependency(const std::string&,SettingDependencyType);
};
"""
    definitions += function(manager, "void CSettingsManager::OnSettingChanged(")
    definitions += function(manager, "SettingDependencyMap CSettingsManager::GetDependencies(const std::string")
    definitions += function(manager, "SettingDependencyMap CSettingsManager::GetDependencies(const SettingConstPtr")
    definitions += function(manager, "void CSettingsManager::UpdateSettingByDependency(const std::string &settingId, const CSettingDependency")
    definitions += function(manager, "void CSettingsManager::UpdateSettingByDependency(const std::string &settingId, SettingDependencyType")
    assert code.count("int main(){") == 1
    code = code.replace("int main(){", definitions + "int main(){")
    code = code.replace("CDolbyVisionAML callback;preset->m_callback=&callback;", """
 CDolbyVisionAML callback;CSettingsManager settingsManager(&callback);
 auto lease=std::make_shared<CSettingsManager::Lease>();lease->callback=&callback;
 settingsManager.m_settings[preset->id]={preset,{{1,lease}},
   {{type->id,{CSettingDependency{}}}}};
 settingsManager.m_settings[type->id]={type,{},{}};
 preset->m_callback=&settingsManager;""")
    return oracle, code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--blocking-control", action="store_true")
    args = parser.parse_args()
    oracle, code = source(args.root)
    if args.blocking_control:
        original = "preset->TryGetValue(presetValue)"
        assert code.count(original) == 1
        code = code.replace(original, "(presetValue = preset->GetValue(), true)")
    oracle["dv"]["run"](code)
