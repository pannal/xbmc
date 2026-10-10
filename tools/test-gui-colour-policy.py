#!/usr/bin/env python3
"""Execute production Amlogic GUI colour policy with controlled host services.

Checks settings/EN-DE contracts and applied-mode publication anchors as well.
The cache-publication fixture executes extracted cache assignments/stores and
IPT normalization; it is not a sysfs lifecycle or device transition test.
ASan/UBSan cover the extracted policy; LeakSanitizer is disabled because this
host's restricted environment cannot inspect process state. This does not test
sysfs, the rendered settings GUI, CE compilation or device colour output.
"""
import argparse
import ast
from collections import Counter
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
CONTRACTS = [
    ('GUISDRSATURATION', 'videoscreen.guisdrsaturation', 69351, 200),
    ('GUIPEAKLUMINANCE_HLG', 'videoscreen.guipeakluminance.hlg', 69353, 100),
    ('GUISATURATION_HLG', 'videoscreen.guisaturation.hlg', 69355, 200),
    ('GUIPEAKLUMINANCE_DV', 'videoscreen.guipeakluminance.dolbyvision', 69357, 100),
    ('GUISATURATION_DV', 'videoscreen.guisaturation.dolbyvision', 69359, 200),
]


def po(path):
    entries, entry, field = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if not line.strip():
            if 'msgctxt' in entry:
                assert entry['msgctxt'] not in entries, (path, entry['msgctxt'])
                entries[entry['msgctxt']] = entry
            entry, field = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            field, value = line.split(' ', 1)
            entry[field] = ast.literal_eval(value)
        elif line.startswith('"') and field:
            entry[field] += ast.literal_eval(line)
    return entries


def canonical(item):
    return (item.tag, sorted(item.attrib.items()), (item.text or '').strip(),
            [canonical(child) for child in item])


def settings_contract(baseline, window):
    root = ET.parse(ROOT / 'system/settings/settings.xml')
    items = root.findall('.//setting')
    assert all(count == 1 for count in Counter(item.get('id') for item in items).values())
    by_id = {item.get('id'): item for item in items}
    old = subprocess.check_output(['git', 'show', baseline + ':system/settings/settings.xml'],
                                  cwd=ROOT, text=True)
    old_peak = ET.fromstring(old).find('.//setting[@id="videoscreen.guipeakluminance"]')
    assert canonical(old_peak) == canonical(by_id['videoscreen.guipeakluminance'])
    old_window = subprocess.check_output(
        ['git', 'show', baseline + ':xbmc/windowing/amlogic/WinSystemAmlogic.cpp'],
        cwd=ROOT, text=True)
    signature = 'float CWinSystemAmlogic::GetGuiSdrPeakLuminance() const'
    assert function(window, signature) == function(old_window, signature)
    en = po(ROOT / 'addons/resource.language.en_gb/resources/strings.po')
    de = po(ROOT / 'addons/resource.language.de_de/resources/strings.po')
    settings_header = (ROOT / 'xbmc/settings/Settings.h').read_text()
    declarations = []
    for suffix, sid, label, maximum in CONTRACTS:
        item = by_id[sid]
        assert item.attrib == {'id': sid, 'type': 'integer', 'label': str(label),
                               'help': str(label + 1)}, sid
        assert item.findtext('level') == '2' and item.findtext('default') == '100', sid
        requirement = item.find('requirement')
        assert [node.tag for node in requirement] == ['and'], sid
        assert [(node.tag, node.attrib, node.text.casefold()) for node in requirement[0]] == [
            ('condition', {}, 'have_amcodec'), ('condition', {}, 'has_glesv2')], sid
        assert [item.findtext('constraints/' + field) for field in
                ('minimum', 'step', 'maximum')] == ['0', '5', str(maximum)], sid
        assert item.find('control').attrib == {'type': 'slider', 'format': 'percentage'}, sid
        dependencies = item.find('dependencies')
        if suffix == 'GUISDRSATURATION':
            assert dependencies is None
        else:
            assert len(dependencies) == 1 and dependencies[0].attrib == {
                'type': 'visible', 'on': 'property', 'name': 'ishdrdisplay'}, sid
        for key in (f'#{label}', f'#{label + 1}'):
            assert en[key]['msgid'] == de[key]['msgid'] and de[key]['msgstr'].strip(), key
            assert en[key]['msgid'].strip(), key
        constant = 'SETTING_VIDEOSCREEN_' + suffix
        match = re.search(r'static constexpr auto ' + constant + r'\s*=\s*"([^"]+)";',
                          settings_header)
        assert match and match[1] == sid, constant
        declarations.append('static constexpr const char* ' + constant + '="' + match[1] + '";')
    # These labels describe relative output white, not physical peak nits.
    assert en['#69353']['msgid'] == 'HLG GUI output white'
    assert en['#69357']['msgid'] == 'Dolby Vision GUI output white'
    assert 'not nits' in en['#69354']['msgid'] and 'not nits' in en['#69358']['msgid']
    conditions = (ROOT / 'xbmc/settings/SettingConditions.cpp').read_text()
    assert '#if HAS_GLES >= 2\n  m_simpleConditions.emplace("has_glesv2");' in conditions
    assert '#ifdef HAS_LIBAMCODEC\n  m_simpleConditions.insert("have_amcodec");' in conditions
    print('PASS: five IDs/defaults/ranges/build gates/HDR visibility, multiline EN/DE PO, old GUI peak unchanged')
    return '\n'.join(declarations)


def publication_contract(aml):
    declaration = re.search(r'static std::atomic<unsigned int> s_guiDvOutputMode\{[^;]+;', aml)
    assert declaration and 'DOLBY_VISION_OUTPUT_MODE_BYPASS' in declaration[0]
    getter = function(aml, 'unsigned int aml_gui_dv_output_mode()')
    assert 'return s_guiDvOutputMode.load(std::memory_order_relaxed);' in getter
    assert 'CSysfsPath' not in getter
    on = function(aml, 'unsigned int aml_dv_on(unsigned int mode, bool force_hdmi)')
    off = function(aml, 'void aml_dv_off(bool skip_hdmi_update)')
    on_store = 's_guiDvOutputMode.store(mode, std::memory_order_relaxed);'
    off_store = 's_guiDvOutputMode.store(DOLBY_VISION_OUTPUT_MODE_BYPASS, std::memory_order_relaxed);'
    assert on.count(on_store) == off.count(off_store) == 1
    assert on.index('mode = DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL;') < on.index(on_store)
    assert on.index('s_dvModeCached = mode;') < on.index(on_store)
    assert off.index('s_dvModeCached = DOLBY_VISION_OUTPUT_MODE_BYPASS;') < off.index(off_store)
    assert aml.count('s_guiDvOutputMode.store(') == 2
    cache = re.search(r'static unsigned int s_dvModeCached\s*=\s*[^;]+;', aml)
    assert cache and 'DOLBY_VISION_OUTPUT_MODE_BYPASS' in cache[0]
    normalization = re.search(
        r'if \(\(mode == DOLBY_VISION_OUTPUT_MODE_IPT\) && '
        r'\(dv_type == DV_TYPE_DISPLAY_LED\)\)\s*'
        r'mode = DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL;', on)
    on_publication = re.search(r's_dvModeCached = mode;\s*' + re.escape(on_store), on)
    off_publication = re.search(
        r's_dvModeCached = DOLBY_VISION_OUTPUT_MODE_BYPASS;\s*' + re.escape(off_store), off)
    assert normalization and on_publication and off_publication
    print('PASS: atomic bypass initialization/load; applied on/off publication follows normalized mode/cache')
    return '\n'.join([
        cache[0], declaration[0], getter,
        # Only these extracted steps execute: no full aml_dv_on/off or sysfs.
        'void PublishAppliedMode(unsigned int mode, DV_TYPE dv_type) {\n' +
        normalization[0] + '\n' + on_publication[0] + '\n}',
        'void PublishBypass() {\n' + off_publication[0] + '\n}',
    ])


PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
@MODES@
@PUBLICATION@
struct CSettings {
@KEYS@
static constexpr const char* SETTING_VIDEOSCREEN_GUISDRPEAKLUMINANCE="videoscreen.guipeakluminance";
};
struct Settings {
 std::map<std::string,int> values, reads;
 int GetInt(const std::string& id){++reads[id];return values.at(id);}
} settings;
struct Services {Settings* GetSettings(){return &settings;}} services;
struct CApplicationPlayer {bool playing=true;bool IsPlayingVideo() const{return playing;}};
struct Components {
 std::shared_ptr<CApplicationPlayer> player=std::make_shared<CApplicationPlayer>();
 template<class T> std::shared_ptr<T> GetComponent(){return player;}
} components;
struct CServiceBroker {
 static Services* GetSettingsComponent(){return &services;}
 static Components& GetAppComponents(){return components;}
};
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_HDR10,HDR_TYPE_HLG,HDR_TYPE_DOLBYVISION};
struct Graphics {StreamHdrType source=StreamHdrType::HDR_TYPE_NONE;
 StreamHdrType GetHDRType() const{return source;}};
struct Caps {bool hlg=true;bool SupportsHLG() const{return hlg;}};
struct CWinSystemAmlogic {
 Graphics graphics;Caps caps;
 const Graphics& GetGfxContext() const{return graphics;}
 const Caps& GetDisplayHDRCapabilities() const{return caps;}
 std::pair<float,float> GetGuiColourAdjustment() const;
 float GetGuiSdrPeakLuminance() const;
};
'''

TESTS = r'''
void reset(){
 settings.values={{"videoscreen.guisdrsaturation",100},{"videoscreen.guipeakluminance.hlg",100},
 {"videoscreen.guisaturation.hlg",100},{"videoscreen.guipeakluminance.dolbyvision",100},
 {"videoscreen.guisaturation.dolbyvision",100},{"videoscreen.guipeakluminance",100}};
 settings.reads.clear();components.player=std::make_shared<CApplicationPlayer>();
 s_guiDvOutputMode.store(DOLBY_VISION_OUTPUT_MODE_BYPASS,std::memory_order_relaxed);
}
void expect(const CWinSystemAmlogic& w,float white,float saturation){
 const auto actual=w.GetGuiColourAdjustment();
 assert(std::isfinite(actual.first)&&std::isfinite(actual.second));
 assert(std::abs(actual.first-white)<0.000001f&&std::abs(actual.second-saturation)<0.000001f);
}
int main(){
 CWinSystemAmlogic w;
 const StreamHdrType sources[]={StreamHdrType::HDR_TYPE_NONE,StreamHdrType::HDR_TYPE_HDR10,
 StreamHdrType::HDR_TYPE_HLG,StreamHdrType::HDR_TYPE_DOLBYVISION};
 // Defaults are bit-exact neutral for every source/output/capability/player state.
 for(unsigned int mode=0;mode<=6;++mode)for(auto source:sources)for(bool cap:{false,true}){
  reset();s_guiDvOutputMode.store(mode);w.graphics.source=source;w.caps.hlg=cap;
  components.player.reset();const auto actual=w.GetGuiColourAdjustment();
  assert(actual.first==1.0f&&actual.second==1.0f);
  for(const auto& id:settings.values)assert(settings.reads[id.first]==(id.first=="videoscreen.guipeakluminance"?0:1));
 }
 // Distinct saved values expose every key and route; applied output wins over source.
 reset();settings.values["videoscreen.guisdrsaturation"]=125;
 settings.values["videoscreen.guipeakluminance.hlg"]=25;
 settings.values["videoscreen.guisaturation.hlg"]=175;
 settings.values["videoscreen.guipeakluminance.dolbyvision"]=50;
 settings.values["videoscreen.guisaturation.dolbyvision"]=50;
 for(auto source:sources)for(unsigned int mode:{DOLBY_VISION_OUTPUT_MODE_IPT,DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL}){
  w.graphics.source=source;w.caps.hlg=false;components.player.reset();s_guiDvOutputMode.store(mode);
  expect(w,0.72974005f,0.5f);
 }
 components.player=std::make_shared<CApplicationPlayer>();w.caps.hlg=true;
 for(auto source:sources)for(unsigned int mode:{DOLBY_VISION_OUTPUT_MODE_HDR10,DOLBY_VISION_OUTPUT_MODE_SDR10,DOLBY_VISION_OUTPUT_MODE_SDR8}){
  w.graphics.source=source;s_guiDvOutputMode.store(mode);expect(w,1.0f,1.25f);
 }
 s_guiDvOutputMode.store(DOLBY_VISION_OUTPUT_MODE_BYPASS);
 for(auto source:sources)for(bool cap:{false,true})for(bool playing:{false,true}){
  w.graphics.source=source;w.caps.hlg=cap;components.player->playing=playing;
  if(source==StreamHdrType::HDR_TYPE_HLG&&cap&&playing)expect(w,0.53252054f,1.75f);
  else expect(w,1.0f,1.25f);
 }
 w.graphics.source=StreamHdrType::HDR_TYPE_HLG;w.caps.hlg=true;components.player.reset();expect(w,1.0f,1.25f);
 s_guiDvOutputMode.store(6);expect(w,1.0f,1.0f);
 // Each live preference independently affects only its output route, including 0.
 for(const auto& key: {"videoscreen.guisdrsaturation","videoscreen.guipeakluminance.hlg",
 "videoscreen.guisaturation.hlg","videoscreen.guipeakluminance.dolbyvision","videoscreen.guisaturation.dolbyvision"}){
  reset();w.caps.hlg=true;w.graphics.source=StreamHdrType::HDR_TYPE_HLG;settings.values[key]=0;
  const std::string id=key;
  for(unsigned int mode:{DOLBY_VISION_OUTPUT_MODE_BYPASS,DOLBY_VISION_OUTPUT_MODE_IPT,DOLBY_VISION_OUTPUT_MODE_HDR10}){
   s_guiDvOutputMode.store(mode);
   const bool used=(mode==DOLBY_VISION_OUTPUT_MODE_IPT&&id.find("dolbyvision")!=std::string::npos)||
    (mode==DOLBY_VISION_OUTPUT_MODE_BYPASS&&id.find(".hlg")!=std::string::npos)||
    (mode==DOLBY_VISION_OUTPUT_MODE_HDR10&&id=="videoscreen.guisdrsaturation");
   const bool peak=id.find("peak")!=std::string::npos;
   expect(w,used&&peak?0.0f:1.0f,used&&!peak?0.0f:1.0f);
  }
 }
 // Corrupt/out-of-range saved values clamp without negative or super-white gain.
 for(int value:{-100,0,100,200,300}){
  reset();w.graphics.source=StreamHdrType::HDR_TYPE_HLG;w.caps.hlg=true;
  for(auto& entry:settings.values)entry.second=value;
  const float white=value<=0?0.0f:1.0f;
  const float saturation=value<=0?0.0f:(value>=200?2.0f:1.0f);
  for(unsigned int mode:{DOLBY_VISION_OUTPUT_MODE_BYPASS,DOLBY_VISION_OUTPUT_MODE_IPT,DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL}){
   s_guiDvOutputMode.store(mode);expect(w,white,saturation);
  }
  s_guiDvOutputMode.store(DOLBY_VISION_OUTPUT_MODE_HDR10);expect(w,1.0f,saturation);
 }
 // Old output-peak mapping is independent, and slider changes are read live.
 reset();w.graphics.source=StreamHdrType::HDR_TYPE_NONE;
 for(int value:{0,50,100}){
  settings.values["videoscreen.guipeakluminance"]=value;
  const float oldExpected=value==0?0.3f:(value==50?0.65f:1.0f);
  assert(std::abs(w.GetGuiSdrPeakLuminance()-oldExpected)<0.000001f);expect(w,1.0f,1.0f);
 }
 settings.values["videoscreen.guisdrsaturation"]=150;expect(w,1.0f,1.5f);
 settings.values["videoscreen.guisdrsaturation"]=0;expect(w,1.0f,0.0f);
 std::cout<<"PASS: extracted policy defaults, output/source precedence, HLG gates, every live saved key and bounds\n";
 // Cache-publication fixture: extracted assignments/stores and normalization,
 // not execution of sysfs, complete DV lifecycle or hardware transitions.
 reset();w.graphics.source=StreamHdrType::HDR_TYPE_HLG;w.caps.hlg=true;
 settings.values["videoscreen.guisdrsaturation"]=125;
 settings.values["videoscreen.guipeakluminance.hlg"]=25;
 settings.values["videoscreen.guisaturation.hlg"]=175;
 settings.values["videoscreen.guipeakluminance.dolbyvision"]=50;
 settings.values["videoscreen.guisaturation.dolbyvision"]=50;
 assert(aml_gui_dv_output_mode()==DOLBY_VISION_OUTPUT_MODE_BYPASS);
 PublishAppliedMode(DOLBY_VISION_OUTPUT_MODE_IPT,DV_TYPE_DISPLAY_LED);
 assert(s_dvModeCached==DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL);
 assert(aml_gui_dv_output_mode()==DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL);
 expect(w,0.72974005f,0.5f);
 for(unsigned int mode:{DOLBY_VISION_OUTPUT_MODE_HDR10,DOLBY_VISION_OUTPUT_MODE_SDR10,DOLBY_VISION_OUTPUT_MODE_SDR8}){
  PublishAppliedMode(mode,DV_TYPE_PLAYER_LED_LLDV);
  assert(s_dvModeCached==mode&&aml_gui_dv_output_mode()==mode);expect(w,1.0f,1.25f);
 }
 PublishBypass();
 assert(s_dvModeCached==DOLBY_VISION_OUTPUT_MODE_BYPASS);
 assert(aml_gui_dv_output_mode()==DOLBY_VISION_OUTPUT_MODE_BYPASS);expect(w,0.53252054f,1.75f);
 // A later playback reopens after preset changes, without retaining old output.
 settings.values["videoscreen.guipeakluminance.dolbyvision"]=25;
 settings.values["videoscreen.guisaturation.dolbyvision"]=150;
 settings.values["videoscreen.guisdrsaturation"]=75;
 w.graphics.source=StreamHdrType::HDR_TYPE_HDR10;
 PublishAppliedMode(DOLBY_VISION_OUTPUT_MODE_IPT,DV_TYPE_PLAYER_LED_LLDV);
 assert(s_dvModeCached==DOLBY_VISION_OUTPUT_MODE_IPT);
 assert(aml_gui_dv_output_mode()==DOLBY_VISION_OUTPUT_MODE_IPT);expect(w,0.53252054f,1.5f);
 PublishBypass();expect(w,1.0f,0.75f);
 PublishAppliedMode(6,DV_TYPE_PLAYER_LED_LLDV);
 assert(s_dvModeCached==6&&aml_gui_dv_output_mode()==6);expect(w,1.0f,1.0f);
 PublishBypass();expect(w,1.0f,0.75f);
 std::cout<<"PASS: extracted cache publication on/change/off/reopen and final IPT tunnel; no sysfs/device lifecycle test\n";
}
'''


def execute(code, name, negative=False):
    with tempfile.TemporaryDirectory(prefix='gui-colour-policy-') as temporary:
        source = Path(temporary) / 'test.cpp'
        source.write_text(code)
        executable = Path(temporary) / 'test'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', str(source), '-o', str(executable)], check=True)
        environment = os.environ.copy()
        environment['ASAN_OPTIONS'] = environment.get('ASAN_OPTIONS', '') + ':detect_leaks=0'
        result = subprocess.run([str(executable)], env=environment, capture_output=True,
                                text=True, timeout=20)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, (name, result.stdout, result.stderr)
            print('REJECTED: ' + name + ' (compiled behavioural mutation)')
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='HEAD', help='Committed old GUI peak contract (default HEAD)')
    parser.add_argument('--negative-controls', action='store_true', help='Compile and reject policy mutations')
    args = parser.parse_args()
    window = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogic.cpp').read_text()
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    keys = settings_contract(args.baseline, window)
    publication = publication_contract(aml)
    aml_header = (ROOT / 'xbmc/utils/AMLUtils.h').read_text()
    modes = '\n'.join(re.findall(r'^#define DOLBY_VISION_OUTPUT_MODE_.*$', aml_header, re.M))
    modes += '\n' + function(aml_header, 'enum DV_TYPE : int') + ';'
    prelude = PRELUDE.replace('@KEYS@', keys).replace('@MODES@', modes).replace('@PUBLICATION@', publication)
    getter = function(window, 'std::pair<float, float> CWinSystemAmlogic::GetGuiColourAdjustment() const')
    old = function(window, 'float CWinSystemAmlogic::GetGuiSdrPeakLuminance() const')
    execute(prelude + getter + old + TESTS, 'production')
    if args.negative_controls:
        mutations = [
            ('source/HLG priority overrides applied output', 'switch (aml_gui_dv_output_mode())',
             'switch (GetGfxContext().GetHDRType() == StreamHdrType::HDR_TYPE_HLG ? '
             'DOLBY_VISION_OUTPUT_MODE_BYPASS : aml_gui_dv_output_mode())'),
            ('missing native HLG display capability gate', 'GetDisplayHDRCapabilities().SupportsHLG()', 'true'),
            ('missing native HLG playing gate', 'player->IsPlayingVideo()', 'true'),
            ('missing white lower/upper clamp', 'std::clamp(peak, 0, 100)', 'peak'),
            ('missing saturation lower/upper clamp', 'std::clamp(saturation, 0, 200)', 'saturation'),
            ('HLG saturation reads wrong saved preference',
             'GetInt(CSettings::SETTING_VIDEOSCREEN_GUISATURATION_HLG)',
             'GetInt(CSettings::SETTING_VIDEOSCREEN_GUISATURATION_DV)'),
        ]
        for name, before, after in mutations:
            assert getter.count(before) == 1, (name, before)
            execute(prelude + getter.replace(before, after) + old + TESTS, name, negative=True)
        for name, store in [
            ('missing applied-mode atomic publication',
             's_guiDvOutputMode.store(mode, std::memory_order_relaxed);'),
            ('missing bypass/off atomic publication',
             's_guiDvOutputMode.store(DOLBY_VISION_OUTPUT_MODE_BYPASS, std::memory_order_relaxed);'),
        ]:
            assert prelude.count(store) == 1, name
            execute(prelude.replace(store, '') + getter + old + TESTS, name, negative=True)
    print('GUI colour policy: PASS (production extraction, host ASan/UBSan; device/GUI acceptance unverified)')


if __name__ == '__main__':
    main()
