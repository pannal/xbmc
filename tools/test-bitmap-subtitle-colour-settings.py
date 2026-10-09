#!/usr/bin/env python3
"""Verify SDR colour settings/translations and execute actual dialog callbacks.

Settings/player/base GUI objects are recording substitutes; controls and callbacks
are production source. This does not claim rendered GUI acceptance.
"""
import argparse
import ast
from collections import Counter
import xml.etree.ElementTree as ET
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
dialog = (ROOT / 'xbmc/video/dialogs/GUIDialogSubtitleSettings.cpp').read_text()
header = r'''
#include <memory>
#include <string>
#include <cassert>
#include <map>
#include <iostream>
constexpr const char* SETTING_SUBTITLE_ENABLE="subtitles.enable",*SETTING_SUBTITLE_DELAY="subtitles.delay",*SETTING_SUBTITLE_STREAM="subtitles.stream";
struct CSettings {static constexpr const char* SETTING_SUBTITLES_BITMAPPOSITION="subtitles.bitmapposition",*SETTING_SUBTITLES_BITMAPASPECT="subtitles.bitmapaspect",*SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS="subtitles.bitmapsdrbrightness",*SETTING_SUBTITLES_BITMAPSDRSATURATION="subtitles.bitmapsdrsaturation",*SETTING_SUBTITLES_BITMAPOFFSET="subtitles.bitmapoffset",*SETTING_SUBTITLES_BITMAPMARGIN="subtitles.bitmapmargin";};
struct CSetting {std::string id;explicit CSetting(const char*s):id(s){};const std::string& GetId()const{return id;}virtual ~CSetting()=default;};
struct CSettingInt:CSetting {int value;CSettingInt(const char*s,int v):CSetting(s),value(v){};int GetValue()const{return value;}};
struct CSettingNumber:CSetting {using CSetting::CSetting;double GetValue()const{return 0;}};
struct CSettingBool:CSetting {using CSetting::CSetting;bool GetValue()const{return false;}};
struct CApplicationPlayer {void SetSubtitle(int){}void SetSubtitleVisible(bool){}void SetSubTitleDelay(float){}};
struct Components {template<class T>std::shared_ptr<T> GetComponent(){return std::make_shared<T>();}};
struct Settings {std::map<std::string,int> ints;int saves=0;void SetInt(const std::string&s,int v){ints[s]=v;}void SetNumber(const std::string&,double){}void Save(){++saves;}};
struct SubtitleSettings {void SetBitmapPreference(float){}};
struct Services {Settings settings;SubtitleSettings subtitles;Settings*GetSettings(){return &settings;}SubtitleSettings*GetSubtitlesSettings(){return &subtitles;}} services;
struct CServiceBroker {static Components&GetAppComponents(){static Components c;return c;}static Services*GetSettingsComponent(){return &services;}};
struct CGUIDialogSettingsManualBase {void OnSettingChanged(const std::shared_ptr<const CSetting>&){}void OnDeinitWindow(int){}};
struct CGUIDialogSubtitleSettings:CGUIDialogSettingsManualBase {bool m_bitmapSettingsChanged=false;int m_subtitleStream=0;void OnSettingChanged(const std::shared_ptr<const CSetting>&);void OnDeinitWindow(int);};
'''
tests = r'''
int main(){CGUIDialogSubtitleSettings d;
 services.settings.ints[CSettings::SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS]=100;
 services.settings.ints[CSettings::SETTING_SUBTITLES_BITMAPSDRSATURATION]=100;
 d.OnSettingChanged(std::make_shared<CSettingInt>(CSettings::SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS,150));
 assert(services.settings.ints[CSettings::SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS]==150&&services.settings.saves==0);
 d.OnSettingChanged(std::make_shared<CSettingInt>(CSettings::SETTING_SUBTITLES_BITMAPSDRSATURATION,0));
 assert(services.settings.ints[CSettings::SETTING_SUBTITLES_BITMAPSDRSATURATION]==0&&services.settings.saves==0);
 d.OnDeinitWindow(0);assert(services.settings.saves==1&&!d.m_bitmapSettingsChanged);
 d.OnDeinitWindow(0);assert(services.settings.saves==1);
 std::cout<<"PASS: actual dialog callbacks update both global integer preferences immediately and save once on close\n";
}
'''
def po(path):
    entries, entry, field = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if not line:
            if 'msgctxt' in entry:
                assert entry['msgctxt'] not in entries, entry['msgctxt']
                entries[entry['msgctxt']] = entry
            entry, field = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            field, text = line.split(' ', 1)
            entry[field] = ast.literal_eval(text)
        elif line.startswith('"') and field:
            entry[field] += ast.literal_eval(line)
    return entries


def canonical(item):
    return item.tag, sorted(item.attrib.items()), (item.text or "").strip(), [canonical(child) for child in item]


def settings(baseline):
    root = ET.parse(ROOT / 'system/settings/settings.xml')
    all_settings = root.findall('.//setting')
    assert all(n == 1 for n in Counter(s.get('id') for s in all_settings).values())
    by_id = {s.get('id'): s for s in all_settings}
    if baseline:
        old = subprocess.check_output(['git', 'show', baseline + ':system/settings/settings.xml'], cwd=ROOT, text=True)
        for item in ET.fromstring(old).findall('.//setting'):
            assert canonical(by_id[item.get('id')]) == canonical(item), item.get('id')
    en = po(ROOT / 'addons/resource.language.en_gb/resources/strings.po')
    de = po(ROOT / 'addons/resource.language.de_de/resources/strings.po')
    for value in range(69337, 69341):
        key = f'#{value}'
        assert en[key]['msgid'] == de[key]['msgid'] and de[key]['msgstr']
    for name, label in [('brightness', 69337), ('saturation', 69339)]:
        sid = 'subtitles.bitmapsdr' + name
        item = by_id[sid]
        assert item.get('type') == 'integer' and item.get('label') == str(label)
        assert item.get('help') == str(label + 1)
        assert item.findtext('default') == '100'
        assert item.findtext('requirement') == 'has_glesv2'
        assert item.find('dependencies') is None
        assert [item.findtext('constraints/' + x) for x in ['minimum', 'step', 'maximum']] == ['0', '5', '200']
        assert item.find('control').get('format') == 'percentage'
        assert root.find('.//category[@id="subtitles"]//setting[@id="' + sid + '"]') is not None
    conditions = (ROOT / 'xbmc/settings/SettingConditions.cpp').read_text()
    assert '#if HAS_GLES >= 2\n  m_simpleConditions.emplace("has_glesv2");' in conditions
    initial = function(dialog, 'void CGUIDialogSubtitleSettings::InitializeSettings()')
    colour_sliders = initial[initial.index('#if HAS_GLES >= 2'):initial.index('#endif', initial.index('#if HAS_GLES >= 2'))]
    for name in ['BITMAPSDRBRIGHTNESS', 'BITMAPSDRSATURATION']:
        assert 'CSettings::SETTING_SUBTITLES_' + name in colour_sliders
    print('PASS: unique settings/PO IDs, matching EN/DE, default100/ranges/GLES visibility and existing settings preservation')


def run(code, negative=False):
    with tempfile.TemporaryDirectory(prefix='bitmap-colour-settings-') as tmp:
        source = Path(tmp) / 'test.cpp'
        source.write_text(code)
        executable = Path(tmp) / 'test'
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(executable)], check=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', help='Verify every setting from this revision remains unchanged')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    settings(args.baseline)
    code = header + function(dialog, 'void CGUIDialogSubtitleSettings::OnSettingChanged(') + function(dialog, 'void CGUIDialogSubtitleSettings::OnDeinitWindow(') + tests
    run(code)
    if args.negative_controls:
        for label, old, new in [
            ('brightness callback missing', 'settingId == CSettings::SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS', 'false'),
            ('saturation callback missing', 'settingId == CSettings::SETTING_SUBTITLES_BITMAPSDRSATURATION', 'false'),
            ('save on every change', 'm_bitmapSettingsChanged = true;', 'm_bitmapSettingsChanged = true; CServiceBroker::GetSettingsComponent()->GetSettings()->Save();'),
            ('saved dirty flag retained', '    m_bitmapSettingsChanged = false;', '    /* omit reset */'),
        ]:
            assert old in code
            run(code.replace(old, new), True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
