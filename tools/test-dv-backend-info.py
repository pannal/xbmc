#!/usr/bin/env python3
"""Host-check live backend labels/cache, lifecycle guards and automatic backend use.

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
    for name in ['aml_dv_backend_label(', 'aml_dv_backend_available_label(']:
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
    speed = function(codec, 'void CAMLCodec::SetSpeed(')
    assert speed.index('aml_dv_backend_pause(') < speed.index('AcquireDecoder()')
    assert 'CSysfsPath' not in function(aml, 'void aml_dv_backend_pause(')
    assert 'std::atomic_store(&s_dvPlaybackSession, std::shared_ptr<const void>{})' in function(aml, 'void aml_dv_close(')
    assert 'atomic_compare_exchange_strong(&s_dvPlaybackSession' in function(aml, 'void aml_dv_cancel_deferred_session(')

    xml = ET.fromstring((ROOT / 'system/settings/settings.xml').read_text())
    controls = {s.get('id'): s for s in xml.findall('.//setting')}
    assert len(controls) == len(xml.findall('.//setting'))
    switch_id = 'coreelec.amlogic.dolbyvision.new.backend'
    assert switch_id not in controls
    assert 'SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND' not in dv
    assert 'aml_dv_enable_new_backend()' in function(dv, 'bool CDolbyVisionAML::Setup(')
    assert 'Set(true)' in function(aml, 'void aml_dv_enable_new_backend(')

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

 auto first=std::make_shared<const int>(1);std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>(first));
 int64_t sampled=0;
 assert(!aml_dv_backend_epoch());
 aml_dv_backend_sample(first,0,sampled);assert(aml_dv_backend_label().empty());
 nodes[capability]="0";nodes[path]="10 -1 0";clock_ms+=250;
 aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_available_label()=="0");
 nodes[capability]="1";nodes[path]="10 1 1";clock_ms+=250;
 aml_dv_backend_sample(first,10,sampled);assert(aml_dv_backend_label().empty());assert(aml_dv_backend_available_label()=="1");
 nodes[path]="11 0 1";clock_ms+=250;aml_dv_backend_sample(first,10,sampled);
 assert(aml_dv_backend_label()=="dovi"); // available newer, actual original
 int reads=CSysfsPath::reads;
 for(int i=0;i<100;++i){assert(aml_dv_backend_label()=="dovi");assert(aml_dv_backend_available_label()=="1");aml_dv_backend_sample(first,10,sampled);}
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
 clock_ms+=1001;assert(aml_dv_backend_label().empty()); // ordinary running stall expires
 aml_dv_backend_pause(second,true);assert(aml_dv_backend_label().empty()); // cannot revive expired identity
 nodes.erase(path);aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty());
 nodes[path]="13 1 1";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi5");
 nodes[path]="bad";clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label().empty());
 nodes[path]="13 0 0";clock_ms+=250;
 aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_label()=="dovi");assert(aml_dv_backend_available_label()=="1");
 // Automatic enabling must recover even if the old GUI preference left the
 // native switch off. Backend identity still reports actual fallback on failure.
 nodes[enable]="0";aml_dv_enable_new_backend();assert(nodes[enable]=="1");
 assert(aml_dv_backend_label()=="dovi"); // failed newer processing remains original
 nodes.erase(enable);aml_dv_enable_new_backend();assert(!nodes.count(enable));
 assert(aml_dv_backend_available_label()=="1");
 nodes.erase(capability);clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_available_label().empty());
 for(auto bad:{"", "2", "1x", "Y", "-1", "4294967296"}) {
  nodes[capability]=bad;clock_ms+=250;aml_dv_backend_sample(second,baseline,sampled);assert(aml_dv_backend_available_label().empty());
 }
 std::cout<<"Backend cache/session/expiry/capability and automatic backend checks passed\n";
}
'''
    # Execute the actual speed message handler and admitted GetPicture prefix.
    # Native driver effects are a recording substitute; admission is production.
    pause_codec = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
constexpr int DVD_PLAYSPEED_PAUSE=0, DVD_PLAYSPEED_NORMAL=1000;
struct CDVDVideoCodec {enum VCReturn {VC_NONE, VC_ERROR};};
struct VideoPicture {std::shared_ptr<const void> amlDVSession;};
struct CAMLCodec {
 CAMLSession m_session;
 std::shared_ptr<const void> m_dvSession;
 uint64_t m_dvBackendEpoch=20;
 int64_t m_dvBackendSampleTime=0;
 bool m_dvBackendPaused=false,m_opened=true,m_speedPending=false;
 int m_speed=1000,m_requestedSpeed=1000,driverCalls=0;
 bool LifecyclePending(){return false;}
 bool ContinueLifecycle(){return true;}
 void SetSpeed(int);
 void SetSpeedInternal(int speed){m_speed=speed;++driverCalls;}
 CDVDVideoCodec::VCReturn GetPicture(VideoPicture&);
 CAMLCodec(){auto r=m_session.Fence();assert(m_session.BeginMutation(r));assert(m_session.Complete(r,true));}
};
''' + speed + '\n' + picture[:picture.index('  struct vdec_info vi;')] + 'return CDVDVideoCodec::VC_NONE;\n}\n'
    pause_tests = r'''
int main(){
 const char* path="/sys/class/amdolby_vision/backend_state";
 CSysfsPath::nodes[path]="21 1 1";
 CAMLCodec codec;auto owner=std::make_shared<const int>(3);codec.m_dvSession=owner;
 std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>(owner));
 VideoPicture picture;
 codec.SetSpeed(0);codec.GetPicture(picture);clock_ms+=5000;
 assert(aml_dv_backend_label().empty()); // pause before confirmation cannot sample/establish identity
 codec.SetSpeed(1000);codec.GetPicture(picture);assert(aml_dv_backend_label()=="dovi5");
 // Message handling retains immediately even while native speed work is deferred.
 auto display=CAMLSession::FenceDisplay();int calls=codec.driverCalls;
 codec.SetSpeed(0);assert(codec.m_speedPending&&codec.m_speed==1000&&codec.driverCalls==calls);
 clock_ms+=5000;assert(aml_dv_backend_label()=="dovi5");
 auto stale=std::make_shared<const int>(4);
 aml_dv_backend_pause(stale,false);aml_dv_backend_pause(stale,true);
 assert(aml_dv_backend_label()=="dovi5");
 assert(CAMLSession::TryBeginDisplay(display));
 assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
 codec.SetSpeed(0); // retrying native pause must not renew/reset the retained observation
 int reads=CSysfsPath::reads;
 CSysfsPath::nodes[path]="22 0 1";codec.GetPicture(picture);
 assert(CSysfsPath::reads==reads&&aml_dv_backend_label()=="dovi5");
 codec.SetSpeed(1000);assert(aml_dv_backend_label().empty());
 codec.GetPicture(picture);assert(aml_dv_backend_label()=="dovi"); // fresh resume, even inside 250ms window
 clock_ms+=1001;assert(aml_dv_backend_label().empty()); // running stalls still expire
 codec.GetPicture(picture);assert(aml_dv_backend_label()=="dovi");
 codec.SetSpeed(0);clock_ms+=5000;assert(aml_dv_backend_label()=="dovi");
 aml_dv_backend_invalidate(owner);assert(aml_dv_backend_label().empty()); // reset/seek/reconfiguration
 codec.SetSpeed(0);codec.GetPicture(picture);assert(aml_dv_backend_label().empty());
 codec.SetSpeed(1000);codec.GetPicture(picture);assert(aml_dv_backend_label()=="dovi");
 codec.SetSpeed(0);std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>{});
 assert(aml_dv_backend_label().empty()); // stop/owner revocation while paused
 auto replacement=std::make_shared<const int>(5);
 std::atomic_store(&s_dvPlaybackSession,std::shared_ptr<const void>(replacement));
 assert(aml_dv_backend_label().empty());
 int64_t sampled=0;aml_dv_backend_sample(replacement,21,sampled);
 assert(aml_dv_backend_label()=="dovi");
 codec.SetSpeed(1000);codec.SetSpeed(0); // stale codec cannot retain or erase replacement
 assert(aml_dv_backend_label()=="dovi");clock_ms+=1001;assert(aml_dv_backend_label().empty());
 std::cout<<"Explicit pause, deferred speed, resume, invalidation, owner and running expiry passed\n";
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
    for number in range(60350, 60353):
        key = '#' + str(number)
        assert english[key]['msgid'] == german[key]['msgid']
        assert german[key]['msgstr']

    system_info = (ROOT / 'xbmc/windows/GUIWindowSystemInfo.cpp').read_text()
    module_method = function(system_info, 'void CGUIWindowSystemInfo::UpdateDVModuleStatus(')
    assert 'SetWidthControl(label->GetWidth(), true)' in system_info
    assert '{0}' in english['#60352']['msgid'] and '{1}' in english['#60352']['msgid']
    assert '{0}' in german['#60352']['msgstr'] and '{1}' in german['#60352']['msgstr']

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

        module_preamble = r'''
#include <cassert>
#include <chrono>
#include <fstream>
#include <map>
#include <string>
int directoryReads=0;
std::map<std::string,bool> modules;
namespace XFILE {struct CDirectory {static bool Exists(const char* path){++directoryReads;return modules[path];}};}
struct CGUIWindowSystemInfo {
 std::chrono::steady_clock::time_point m_dvModuleStatusUpdated{};
 bool m_doviLoaded=false,m_dovi5Loaded=false;
 std::string m_doviModulePath,m_dovi5ModulePath;
 void UpdateDVModuleStatus();
};
'''
        module_tests = r'''
int main(){
 CGUIWindowSystemInfo window;
 const char* first="@ROOT@/dovi-loaded-path";
 const char* second="@ROOT@/dovi5-loaded-path";
 for(bool old:{false,true})for(bool newer:{false,true}) {
  std::ofstream(first)<<"/flash/dovi.ko\n";
  std::ofstream(second)<<"/storage/.dovi5/generations/example/dovi5.ko\n";
  modules["/sys/module/dovi"]=old;modules["/sys/module/dovi5"]=newer;
  window.m_dvModuleStatusUpdated={};window.UpdateDVModuleStatus();
  assert(window.m_doviLoaded==old&&window.m_dovi5Loaded==newer);
  assert(window.m_doviModulePath==(old?"/flash/dovi.ko":""));
  assert(window.m_dovi5ModulePath==(newer?"/storage/.dovi5/generations/example/dovi5.ko":""));
  const int reads=directoryReads;
  modules["/sys/module/dovi"]=!old;modules["/sys/module/dovi5"]=!newer;
  window.UpdateDVModuleStatus();assert(directoryReads==reads); // per-frame calls stay cached
  assert(window.m_doviLoaded==old&&window.m_dovi5Loaded==newer);
  window.m_dvModuleStatusUpdated-=std::chrono::seconds(1);window.UpdateDVModuleStatus();
  assert(directoryReads==reads+2&&window.m_doviLoaded==!old&&window.m_dovi5Loaded==!newer);
 }
 modules["/sys/module/dovi"]=true;modules["/sys/module/dovi5"]=true;
 std::remove(first);std::ofstream(second)<<"relative/candidate.ko\n";
 window.m_dvModuleStatusUpdated={};window.UpdateDVModuleStatus();
 assert(window.m_doviLoaded&&window.m_dovi5Loaded); // residency survives unknown provenance
 assert(window.m_doviModulePath.empty()&&window.m_dovi5ModulePath.empty());
}
'''.replace('@ROOT@',str(tmp))
        adapted_module = module_method.replace('/run/',str(tmp)+'/')
        run(module_preamble + adapted_module + module_tests, 'module-residency-path')
        if args.negative_controls:
            for name, old, new in [
                    ('module-cache-bypassed', 'now - m_dvModuleStatusUpdated < std::chrono::seconds(1)', 'false'),
                    ('unloaded-module-uses-stale-path', 'if (loaded)', 'if (true)'),
                    ('dovi5-residency-aliases-original', 'Exists("/sys/module/dovi5")', 'Exists("/sys/module/dovi")')]:
                assert old in adapted_module
                run(module_preamble + adapted_module.replace(old,new) + module_tests,name,False)

        run(preamble + state + tests, 'cache')
        run(preamble + state + pause_codec + pause_tests, 'pause')
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
            for name, old, new in [
                    ('resume-keeps-paused', 'expected->paused != paused', 'expected->paused != paused && paused'),
                    ('pause-samples-unknown', 'if (!m_dvBackendPaused)', 'if (true)'),
                    ('pause-revives-expired', 'paused && aml_steady_ms() - expected->sampled <= 1000', 'paused')]:
                source = preamble + state + pause_codec
                assert old in source
                # Expired retention is exercised by the original running-expiry checks.
                suffix = tests if name == 'pause-revives-expired' else pause_tests
                run(source.replace(old, new) + suffix, name, False)
    print('Backend label IDs, GUI build guards, native admission and DV-tree wiring passed')


if __name__ == '__main__':
    main()
