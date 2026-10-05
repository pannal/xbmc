#!/usr/bin/env python3
"""Exercise production debug callbacks/visibility with setting and GUI stubs.
Checks XML and translated strings; no rendered GUI/device claim.
"""
import ast
from pathlib import Path
import runpy
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
HARNESS = r'''
#include "commons/ilog.h"
#include <algorithm>
#include <cassert>
#include <map>
#include <memory>
#include <string>
#include <iostream>
struct CSetting {std::string id;const std::string& GetId()const{return id;}};
struct CSettings {
 static constexpr auto SETTING_DEBUG_SHOWLOGINFO="debug.showloginfo";
 static constexpr auto SETTING_DEBUG_SHOWLOGOVERLAY="debug.showlogoverlay";
 std::map<std::string,bool> values;
 bool GetBool(const std::string& id){return values[id];}
};
struct CProfileManager {};
struct CAdvancedSettings {
 int m_logLevelHint=LOG_LEVEL_NORMAL,m_logLevel=LOG_LEVEL_NORMAL;
 std::string m_videoDefaultPlayer="VideoPlayer",m_audioDefaultPlayer="paplayer";
 void Load(CProfileManager&){}
 void OnSettingsLoaded();void OnSettingChanged(const std::shared_ptr<const CSetting>&);
 void SetDebugMode(bool);
};
struct Component {
 std::shared_ptr<CSettings> settings=std::make_shared<CSettings>();
 std::shared_ptr<CAdvancedSettings> advanced=std::make_shared<CAdvancedSettings>();
 auto GetSettings(){return settings;}auto GetAdvancedSettings(){return advanced;}
 auto GetProfileManager(){return std::make_shared<CProfileManager>();}
};
struct CLog {int level=0;void SetLogLevel(int l){level=l;}template<class...T>static void Log(T&&...) {}};
struct CServiceBroker {
 static inline Component component;static inline CLog logging;
 static auto GetSettingsComponent(){return &component;}static auto& GetLogging(){return logging;}
};
struct Skin {bool debugging=false;bool IsDebugging(){return debugging;}};
static auto g_SkinInfo=std::make_shared<Skin>();
struct CGUIWindowDebugInfo {
 bool visible=false;void Open(){visible=true;}void Close(){visible=false;}void UpdateVisibility();
};
@METHODS@
int main(){
 auto settings=CServiceBroker::component.settings;
 auto advanced=CServiceBroker::component.advanced;
 auto changed=[](const std::string& id){auto s=std::make_shared<CSetting>();s->id=id;CServiceBroker::component.advanced->OnSettingChanged(s);};
 CGUIWindowDebugInfo overlay;
 advanced->OnSettingsLoaded();overlay.UpdateVisibility();
 assert(advanced->m_logLevel==0&&!overlay.visible);
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGINFO]=true;changed(CSettings::SETTING_DEBUG_SHOWLOGINFO);
 overlay.UpdateVisibility();assert(advanced->m_logLevel==1&&CServiceBroker::logging.level==1&&!overlay.visible);
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGOVERLAY]=true;changed(CSettings::SETTING_DEBUG_SHOWLOGOVERLAY);
 overlay.UpdateVisibility();assert(advanced->m_logLevel==2&&CServiceBroker::logging.level==2&&overlay.visible);
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGOVERLAY]=false;changed(CSettings::SETTING_DEBUG_SHOWLOGOVERLAY);
 overlay.UpdateVisibility();assert(advanced->m_logLevel==1&&!overlay.visible);
 advanced->OnSettingsLoaded();assert(advanced->m_logLevel==1); // persisted logging, new default overlay off
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGOVERLAY]=true;advanced->OnSettingsLoaded();assert(advanced->m_logLevel==2);
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGINFO]=false;changed(CSettings::SETTING_DEBUG_SHOWLOGINFO);
 overlay.UpdateVisibility();assert(advanced->m_logLevel==0&&!overlay.visible);
 // An overlay callback cannot itself enable logging.
 changed(CSettings::SETTING_DEBUG_SHOWLOGOVERLAY);assert(advanced->m_logLevel==0);
 advanced->m_logLevelHint=1;advanced->OnSettingsLoaded();overlay.UpdateVisibility();assert(advanced->m_logLevel==1&&!overlay.visible);
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGINFO]=true;
 settings->values[CSettings::SETTING_DEBUG_SHOWLOGOVERLAY]=false;
 advanced->m_logLevelHint=2;changed(CSettings::SETTING_DEBUG_SHOWLOGINFO);
 overlay.UpdateVisibility();assert(advanced->m_logLevel==2&&overlay.visible); // retain explicit XML hint
 advanced->m_logLevelHint=0;changed(CSettings::SETTING_DEBUG_SHOWLOGINFO);
 g_SkinInfo->debugging=true;overlay.UpdateVisibility();assert(overlay.visible); // skin debugging remains independent
 advanced->OnSettingChanged(nullptr);
 std::cout<<"PASS GUI debug logging/overlay defaults, live callbacks, reload, XML hint and skin debugging\n";
}
'''


def po_entries(path):
    entries = {}
    current = {}
    key = None
    for line in path.read_text().splitlines() + ['']:
        if not line.strip():
            if 'msgctxt' in current:
                assert current['msgctxt'] not in entries, current['msgctxt']
                entries[current['msgctxt']] = current
            current = {}
            key = None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            key, value = line.split(' ', 1)
            current[key] = ast.literal_eval(value)
        elif line.startswith('"') and key:
            current[key] += ast.literal_eval(line)
    return entries


def main():
    tree = ET.parse(ROOT / 'system/settings/settings.xml')
    settings = tree.findall('.//setting')
    ids = [setting.get('id') for setting in settings]
    assert len(ids) == len(set(ids))
    overlay = next(s for s in settings if s.get('id') == 'debug.showlogoverlay')
    assert overlay.findtext('default') == 'false' and overlay.findtext('level') == '1'
    dependency = overlay.find('dependencies/dependency')
    assert dependency.get('setting') == 'debug.showloginfo' and dependency.text == 'true'
    assert overlay.find('control').get('type') == 'toggle'
    en = po_entries(ROOT / 'addons/resource.language.en_gb/resources/strings.po')
    de = po_entries(ROOT / 'addons/resource.language.de_de/resources/strings.po')
    for name in ['label', 'help']:
        context = '#' + overlay.get(name)
        assert en[context]['msgid'] == de[context]['msgid'] and de[context]['msgstr']
    source = (ROOT / 'xbmc/settings/AdvancedSettings.cpp').read_text()
    assert 'settingSet.insert(CSettings::SETTING_DEBUG_SHOWLOGOVERLAY);' in source
    header = (ROOT / 'xbmc/settings/Settings.h').read_text()
    assert 'SETTING_DEBUG_SHOWLOGOVERLAY = "debug.showlogoverlay"' in header
    methods = '\n'.join(extract(source, signature) for signature in [
        'void CAdvancedSettings::OnSettingsLoaded()',
        'void CAdvancedSettings::OnSettingChanged(',
        'void CAdvancedSettings::SetDebugMode('])
    methods += extract((ROOT / 'xbmc/windows/GUIWindowDebugInfo.cpp').read_text(),
                       'void CGUIWindowDebugInfo::UpdateVisibility()')
    with tempfile.TemporaryDirectory(prefix='debug-overlay-setting-') as tmp:
        path = Path(tmp)
        cpp = path / 'check.cpp'
        exe = path / 'check'
        cpp.write_text(HARNESS.replace('@METHODS@', methods))
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I' + str(ROOT / 'xbmc'),
                        str(cpp), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS XML setting IDs/default/dependency and English/German labels/help')


if __name__ == '__main__':
    main()
