#!/usr/bin/env python3
"""Host-check live backend labels/cache, lifecycle guards and GUI switch policy.

Sysfs and clocks are injected; this verifies code and cache semantics, not native
video timing or hardware. Production parser, sampler and label readers are used.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    pos = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[pos] == '{') - (source[pos] == '}')
        pos += 1
    return source[start:pos]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    gui = (ROOT / 'xbmc/guilib/guiinfo/PlayerGUIInfo.cpp').read_text()
    labels = (ROOT / 'xbmc/guilib/guiinfo/GUIInfoLabels.h').read_text()
    manager = (ROOT / 'xbmc/GUIInfoManager.cpp').read_text()
    codec = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    dv = (ROOT / 'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    conditions = (ROOT / 'xbmc/settings/SettingConditions.cpp').read_text()
    for key, name, offset in [('amlogic.dv.backend', 'PLAYER_PROCESS_AML_DV_BACKEND', 138),
                              ('amlogic.dv.backend.available', 'PLAYER_PROCESS_AML_DV_BACKEND_AVAILABLE', 139)]:
        assert manager.count('{"' + key + '", ' + name + ' }') == 1
        assert f'#define {name} (PLAYER_PROCESS + {offset})' in labels
        offsets = re.findall(r'^#define (\w+) \(PLAYER_PROCESS \+ ' + str(offset) + r'\)', labels, re.M)
        assert offsets == [name], offsets
    cases = gui[gui.index('    case PLAYER_PROCESS_AML_DV_BACKEND:'):gui.index('    case PLAYER_PROCESS_AML_PIXELFORMAT:')]
    assert cases.count('#ifdef HAS_LIBAMCODEC') == 2
    for name in ['aml_dv_backend_label(', 'aml_dv_backend_available_label(', 'aml_dv_new_backend_status(']:
        body = function(aml, name if name.startswith('std::') else
                        ('std::string ' if 'label' in name else 'int ') + name)
        assert 'CSysfsPath' not in body and 'CDVCoreGuard' not in body
    begin = function(codec, 'bool CAMLCodec::BeginLifecycle(')
    assert 'aml_dv_backend_invalidate(m_dvSession)' in begin
    for signature, call in [('bool CAMLCodec::OpenDecoderInternal(', 'codec_init('),
                             ('void CAMLCodec::ResetInternal(', 'codec_reset(')]:
        body = function(codec, signature)
        assert body.index('aml_dv_backend_epoch()') < body.index('m_dll->' + call)
    picture = function(codec, 'CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(')
    assert picture.index('AcquireDecoder()') < picture.index('aml_dv_backend_sample(')
    assert 'std::atomic_store(&s_dvPlaybackSession, std::shared_ptr<const void>{})' in function(aml, 'void aml_dv_close(')
    assert 'atomic_compare_exchange_strong(&s_dvPlaybackSession' in function(aml, 'void aml_dv_cancel_deferred_session(')

    xml = ET.fromstring((ROOT / 'system/settings/settings.xml').read_text())
    controls = {s.get('id'): s for s in xml.findall('.//setting')}
    assert len(controls) == len(xml.findall('.//setting'))
    switch_id = 'coreelec.amlogic.dolbyvision.new.backend'
    switch = controls[switch_id]
    group = next(g for g in xml.findall('.//group') if switch in list(g))
    siblings = [s.get('id') for s in group.findall('setting')]
    assert siblings.index(switch_id) == siblings.index('coreelec.amlogic.dolbyvision.mode') + 1
    assert switch.get('parent') == 'coreelec.amlogic.dolbyvision.mode'
    assert switch.findtext('requirement') == 'HAVE_AMCODEC'
    assert switch.findtext('level') == '3' and switch.findtext('default') == 'true'
    assert switch.find('./dependencies/dependency[@type="enable"]') is None
    assert 'SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND, show' in dv
    assert 'settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND)' in dv
    setting_change = function(dv, 'void CDolbyVisionAML::OnSettingChanged(')
    assert 'aml_dv_apply_new_backend_setting()' not in setting_change
    assert setting_change.index('SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND') < setting_change.index('schedule_native_setting_apply(setting->GetId())')
    assert 'aml_dv_apply_new_backend_setting()' in function(dv, 'void CDolbyVisionAML::Setup(') if 'void CDolbyVisionAML::Setup(' in dv else 'aml_dv_apply_new_backend_setting()' in function(dv, 'bool CDolbyVisionAML::Setup(')

    state = aml[aml.index('static std::atomic<bool> s_dvNewBackendAvailable'):aml.index('bool aml_support_dolby_vision(')]
    preamble = r'''
#include "utils/DVBackendState.h"
#include <atomic>
#include <cassert>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <optional>
#include <type_traits>
static int64_t clock_ms=1000;
static int64_t aml_steady_ms(){return clock_ms;}
static std::shared_ptr<const void> s_dvPlaybackSession;
struct CSettings {static constexpr auto SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND="switch";};
struct Settings {
 bool enabled=true;int writes=0;
 bool GetBool(const char*) {return enabled;}
 void SetBool(const char*,bool value) {enabled=value;++writes;}
};
static Settings preferences;
static Settings* settings(){return &preferences;}
struct CSysfsPath {
 static std::map<std::string,std::string> nodes;static int reads;
 std::string path;
 explicit CSysfsPath(const char* p):path(p){}
 bool Exists() const{return nodes.count(path);}
 template<class T> std::optional<T> Get() const {
  ++reads;auto it=nodes.find(path);if(it==nodes.end())return std::nullopt;
  if constexpr(std::is_same_v<T,std::string>)return it->second;
  else {T value{};const char* end=it->second.data()+it->second.size();auto r=std::from_chars(it->second.data(),end,value);if(r.ec!=std::errc{}||r.ptr!=end)return std::nullopt;return value;}
 }
 template<class T> void Set(T value){nodes[path]=value?"1":"0";}
};
std::map<std::string,std::string> CSysfsPath::nodes;
int CSysfsPath::reads=0;
bool aml_dv_new_backend_available();
'''
    tests = r'''
int main(){
 const char* path="/sys/class/amdolby_vision/backend_state";
 const char* capability="/sys/module/amdolby_vision/parameters/dv_new_backend_available";
 const char* enable="/sys/module/amdolby_vision/parameters/dv_new_blob_enable";
 auto& nodes=CSysfsPath::nodes;
 for(auto text:{"", "0 1 1", "-1 1 1", "1 2 1", "1 -2 1", "1 1 2", "1 1", "1 1 1x", "18446744073709551616 1 1", "1 1 1 0"})assert(!DVBackendState::Parse(text));
 assert(!DVBackendState::Parse(std::string_view{}));assert(DVBackendState::Parse("2 -1 0\n"));
 assert(aml_dv_backend_label().empty());assert(aml_dv_backend_available_label().empty());
 assert(aml_dv_new_backend_status()==-1);
 auto first=std::make_shared<const int>(1);std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>(first));
 int64_t sampled=0;
 assert(!aml_dv_backend_epoch());
 aml_dv_backend_sample(first,0,sampled);assert(aml_dv_backend_label().empty());assert(preferences.enabled);
 nodes[capability]="0";nodes[path]="10 -1 0";clock_ms+=250;
 aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_available_label()=="0");assert(aml_dv_new_backend_status()==0);
 nodes[capability]="1";nodes[path]="10 1 1";clock_ms+=250;
 aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_label().empty());assert(aml_dv_backend_available_label()=="1");assert(aml_dv_new_backend_status()==1);
 nodes[path]="11 0 1";clock_ms+=250;aml_dv_backend_sample(first,10,sampled);
 assert(aml_dv_backend_label()=="dovi"); // available newer, actual original
 int reads=CSysfsPath::reads;
 for(int i=0;i<100;++i){assert(aml_dv_backend_label()=="dovi");assert(aml_dv_backend_available_label()=="1");assert(aml_dv_new_backend_status()==1);aml_dv_backend_sample(first,10,sampled);}
 assert(CSysfsPath::reads==reads); // CPU readers and sampling throttle
 nodes[path]="11 1 1";clock_ms+=250;aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_label()=="dovi5");
 nodes[path]="11 0 1";clock_ms+=250;aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_label()=="dovi");
 nodes[path]="11 1 1";clock_ms+=250;aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_label()=="dovi5");
 std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>{});assert(aml_dv_backend_label().empty());
 auto second=std::make_shared<const int>(2);std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>(second));
 assert(aml_dv_backend_label().empty());sampled=0;uint64_t baseline=aml_dv_backend_epoch();
 aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty()); // unchanged last applied value
 nodes[path]="12 -1 1";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty());
 nodes[path]="12 1 1";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi5");
 aml_dv_backend_invalidate(first);assert(aml_dv_backend_label()=="dovi5"); // stale owner cannot erase newer state
 aml_dv_backend_invalidate(second);assert(aml_dv_backend_label().empty());
 clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi5");
 clock_ms+=1001;assert(aml_dv_backend_label().empty()); // paused/stalled sampling expires
 nodes.erase(path);aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty());assert(aml_dv_new_backend_status()==-1);
 nodes[path]="13 1 1";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi5");
 nodes[path]="bad";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty());
 nodes[path]="13 0 0";preferences.enabled=true;clock_ms+=250;
 aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi");assert(aml_dv_backend_available_label()=="1");assert(aml_dv_new_backend_status()==2);assert(preferences.enabled);
 nodes[enable]="1";preferences.enabled=true;aml_dv_apply_new_backend_setting();assert(preferences.enabled);assert(nodes[enable]=="1");
 nodes[path]="13 0 1";preferences.enabled=true;aml_dv_apply_new_backend_setting();assert(preferences.enabled);assert(nodes[enable]=="1");assert(aml_dv_new_backend_status()==1);
 preferences.enabled=false;aml_dv_apply_new_backend_setting();assert(nodes[enable]=="0");assert(aml_dv_backend_available_label()=="1");assert(aml_dv_new_backend_status()==1);
 nodes.erase(capability);clock_ms+=250;preferences.enabled=true;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_available_label().empty());assert(preferences.enabled);
 for(auto bad:{"", "2", "1x", "Y", "-1", "4294967296"}) {
  nodes[capability]=bad;clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_available_label().empty());
 }
 std::cout<<"Backend cache/session/expiry/capability and switch checks passed\n";
}
'''
    import ast
    def entries(text):
        found, values, field = {}, {}, None
        for line in text.splitlines() + ['']:
            if not line.strip():
                if 'msgctxt' in values:
                    assert values['msgctxt'] not in found
                    found[values['msgctxt']] = values
                values, field = {}, None
            elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
                field, literal = line.split(' ', 1)
                values[field] = ast.literal_eval(literal)
            elif line.startswith('"') and field:
                values[field] += ast.literal_eval(line)
        return found
    english = entries((ROOT / 'addons/resource.language.en_gb/resources/strings.po').read_text())
    german = entries((ROOT / 'addons/resource.language.de_de/resources/strings.po').read_text())
    for number in range(60350, 60356):
        key = '#' + str(number)
        assert english[key]['msgid'] == german[key]['msgid']
        assert german[key]['msgstr']

    description = (ROOT / 'xbmc/settings/windows/GUIWindowSettingsCategory.cpp').read_text()
    status = function(description, 'static int DVBackendDescriptionStatus(')
    assert 'CSysfsPath' not in status and 'CDVCoreGuard' not in status
    assert 'm_dvBackendDescriptionStatus != DVBackendDescriptionStatus()' in description

    with tempfile.TemporaryDirectory(prefix='dv-backend-info-') as tmp:
        tmp = Path(tmp)

        def run(text, name, expected=True):
            src, exe = tmp / (name + '.cpp'), tmp / name
            src.write_text(text)
            subprocess.run(['g++', '-std=c++17', '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie', '-I' + str(ROOT / 'xbmc'), str(src), '-o', str(exe)], check=True)
            r = subprocess.run([str(exe)], capture_output=True, text=True, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            assert (r.returncode == 0) == expected, (name, r.stdout, r.stderr)
            if expected:
                print(r.stdout.strip())
            else:
                print('Rejected negative control:', name)

        run(preamble + state + tests, 'cache')
        run('#include <cassert>\nint current=-1;int aml_dv_new_backend_status(){return current;}\n' + status +
            'int main(){assert(DVBackendDescriptionStatus()==60355);current=0;assert(DVBackendDescriptionStatus()==60353);current=1;assert(DVBackendDescriptionStatus()==60352);current=2;assert(DVBackendDescriptionStatus()==60354);}', 'help-status')
        help_methods = function(description, 'void CGUIWindowSettingsCategory::SetDescription(')
        help_methods += function(description, 'void CGUIWindowSettingsCategory::DoProcess(')
        for supported in [False, True]:
            help_source = ('#define HAS_LIBAMCODEC\n' if supported else '') + r'''
#include <cassert>
#include <string>
struct CVariant {
 int number=-1;std::string text;
 CVariant(int n):number(n){} CVariant(std::string t):text(t){}
 bool isInteger()const{return number>=0;}int asInteger()const{return number;}
};
struct CDirtyRegionList{};
int current=-1, labels=0;
int aml_dv_new_backend_status(){return current;}
struct Localize {std::string Get(int id){++labels;return std::to_string(id);}} g_localizeStrings;
struct CGUIDialogSettingsManagerBase {
 std::string shown;int processes=0;
 void SetDescription(const CVariant& label){shown=label.isInteger()?std::to_string(label.asInteger()):label.text;}
 void DoProcess(unsigned int,CDirtyRegionList&){++processes;}
};
struct CGUIWindowSettingsCategory:CGUIDialogSettingsManagerBase {
 int m_dvBackendDescriptionStatus=0;
 void SetDescription(const CVariant&);
 void DoProcess(unsigned int,CDirtyRegionList&);
};
''' + status + help_methods + r'''
int main(){CGUIWindowSettingsCategory window;CDirtyRegionList dirty;
 window.SetDescription(CVariant{60351});
''' + (r'''
 assert(window.shown=="60351 60355");current=1;window.DoProcess(0,dirty);assert(window.shown=="60351 60352");
 int count=labels;window.DoProcess(1,dirty);assert(labels==count);
 current=2;window.DoProcess(2,dirty);assert(window.shown=="60351 60354");
 window.SetDescription(CVariant{100});current=0;window.DoProcess(3,dirty);assert(window.shown=="100");
''' if supported else r'''
 assert(window.shown=="60351");current=1;window.DoProcess(0,dirty);assert(window.shown=="60351");assert(labels==0);
''') + '}'
            run(help_source, 'help-window-' + str(supported))
        # Compile and execute actual cases for supported and unsupported builds.
        for supported in [False, True]:
            source = ('#define HAS_LIBAMCODEC\n' if supported else '') + r'''
#include <cassert>
#include <string>
constexpr int PLAYER_PROCESS_AML_DV_BACKEND=1,PLAYER_PROCESS_AML_DV_BACKEND_AVAILABLE=2;
struct Player {bool playing=false;bool IsPlayingVideo(){return playing;}};
int calls=0;
std::string aml_dv_backend_label(){++calls;return "dovi5";}
std::string aml_dv_backend_available_label(){++calls;return "1";}
struct GUI {Player player;Player* m_appPlayer=&player;
bool Get(int info,std::string& value){switch(info){
''' + cases + r'''default:return false;}}};
int main(){GUI gui;std::string value="stale";assert(gui.Get(1,value)&&value.empty());gui.player.playing=true;assert(gui.Get(1,value));
''' + ('assert(value=="dovi5");assert(gui.Get(2,value)&&value=="1");assert(calls==2);' if supported else 'assert(value.empty());assert(gui.Get(2,value)&&value.empty());assert(calls==0);') + '}'
            run(source, 'gui-' + str(supported))
        if args.negative_controls:
            for name, old, new in [('stale-session', 'snapshot->session != std::atomic_load(&s_dvPlaybackSession)', 'false'),
                                   ('old-generation', 'state->epoch <= baseline', 'false')]:
                assert old in state
                run(preamble + state.replace(old, new) + tests, name, False)
    print('Backend label IDs, GUI build guards, native admission and DV-tree wiring passed')


if __name__ == '__main__':
    main()
