#!/usr/bin/env python3
"""Host regressions of production AML HDR parsing, snapshot and refresh wiring.

Uses the actual helper header and extracted Refresh/Get/IsHDR/GUI policy. Only
the sysfs directory is redirected to temporary files. The existing window
fixture executes admitted/same-mode and sampled-present hooks. Synthetic time
executes the actual HDMI probe. No physical HPD/EDID, sysfs or device guarantee.
ASan/UBSan run with leak detection disabled for restricted-host process access.
"""
import argparse
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def run(code, label, negative=False, headers=None):
    with tempfile.TemporaryDirectory(prefix='aml-hdr-test-') as temporary:
        out = Path(temporary)
        source = out / 'test.cpp'
        source.write_text(code)
        if headers:
            for name, text in headers.items():
                target = out / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        executable = out / 'test'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', '-I', str(out),
                        '-I', str(ROOT / 'xbmc'), str(source), '-o', str(executable)], check=True)
        environment = os.environ.copy()
        environment['ASAN_OPTIONS'] = environment.get('ASAN_OPTIONS', '') + ':detect_leaks=0'
        result = subprocess.run([str(executable), str(out)], env=environment,
                                capture_output=True, text=True, timeout=30)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, (label, result.stdout, result.stderr)
            print('REJECTED: ' + label + ' (compiled behavioural mutation)')
        else:
            assert result.returncode == 0, (label, result.stdout, result.stderr)
            print('PASS: ' + label)


PRELUDE = r'''
#include "windowing/amlogic/AMLHDRCapabilities.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>
std::string fixtureDirectory;
struct Clock {static inline std::chrono::steady_clock::time_point now{std::chrono::seconds(1)};
 static auto Now(){return now;}};
@MODES@
struct CSettings {@KEYS@};
struct Settings {std::map<std::string,int> values;
 int GetInt(const std::string& id){return values.at(id);}} settings;
struct Services {Settings* GetSettings(){return &settings;}} services;
struct CApplicationPlayer {bool playing=true;bool IsPlayingVideo()const{return playing;}};
struct Components {std::shared_ptr<CApplicationPlayer> player=std::make_shared<CApplicationPlayer>();
 template<class T>std::shared_ptr<T> GetComponent(){return player;}} components;
struct CServiceBroker {static Services* GetSettingsComponent(){return &services;}
 static Components& GetAppComponents(){return components;}};
enum class StreamHdrType{HDR_TYPE_NONE,HDR_TYPE_HDR10,HDR_TYPE_HLG,HDR_TYPE_DOLBYVISION};
struct Graphics {StreamHdrType source=StreamHdrType::HDR_TYPE_HLG;
 StreamHdrType GetHDRType()const{return source;}};
unsigned int outputMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;
unsigned int aml_gui_dv_output_mode(){return outputMode;}
class CWinSystemAmlogic {
public:
 CHDRCapabilities m_hdr_caps;
 mutable std::mutex m_hdrCapsMutex;
 std::mutex m_hdrRefreshMutex;
 std::atomic<bool> m_hdrRefreshPending{true};
 bool m_hdrCapsValid{false};
 std::optional<CHDRCapabilities> m_hdrLossCandidate;
 std::string m_hdrLossEDID;
 std::chrono::steady_clock::time_point m_hdrLossSince{};
 Graphics graphics;
 const Graphics& GetGfxContext()const{return graphics;}
 void RefreshHDRCapabilities();bool IsHDRDisplay();
 CHDRCapabilities GetDisplayHDRCapabilities()const;
 std::pair<float,float> GetGuiColourAdjustment()const;
};
'''

TESTS = r'''
using Files=std::map<std::string,std::optional<std::string>>;
const std::string unsupported="The Rx don't support DolbyVision\n";
const std::string supported="DolbyVision RX support list:\nVSVDB Version: V2\n";
std::string hdr(int mask){return "HDR10Plus Supported: "+std::to_string(bool(mask&2))+"\n"
 "HDR Static Metadata:\n    Traditional HDR: "+std::to_string(bool(mask&1))+"\n"
 "    SMPTE ST 2084: 1\n    Hybrid Log-Gamma: "+std::to_string(bool(mask&4))+"\n";}
unsigned int mask(const CHDRCapabilities& caps){return caps.SupportsHDR10()|
 (caps.SupportsHDR10Plus()<<1)|(caps.SupportsHLG()<<2)|(caps.SupportsDolbyVision()<<3);}
Files files(int bits){return {{"hpd_state","1"},{"edid_parsing","ok\n"},
 {"rawedid","00ffffffffffff00"+std::string(240,'0')+"\n"},
 {"hdr_cap",hdr(bits)},{"dv_cap",bits&8?supported:unsupported}};}
void write(const std::string& name,const std::string& value){
 const auto path=fixtureDirectory+"/"+name;
 {std::ofstream file(path+".new",std::ios::binary);file.write(value.data(),value.size());assert(file.good());}
 std::filesystem::rename(path+".new",path);
}
void install(const Files& values){for(const auto& item:values){
 if(item.second)write(item.first,*item.second);else std::filesystem::remove(fixtureDirectory+"/"+item.first);}}
void expect(CWinSystemAmlogic& window,unsigned int expected,bool pending){
 assert(mask(window.GetDisplayHDRCapabilities())==expected);
 assert(window.m_hdrRefreshPending.load()==pending);
}
void confirm(CWinSystemAmlogic& window){window.RefreshHDRCapabilities();
 Clock::now+=std::chrono::seconds(1);window.RefreshHDRCapabilities();}
void pure(){
 for(int bits=0;bits<16;++bits){
  auto parsed=AML::HDR::Parse(hdr(bits),bits&8?supported:unsupported);
  assert(parsed&&mask(*parsed)==static_cast<unsigned>(bits));
  auto values=files(bits);int calls=0;
  const auto snapshot=AML::HDR::ReadStable([&](const char* name){++calls;return values.at(name);});
  assert(snapshot&&mask(*snapshot)==static_cast<unsigned>(bits)&&calls==10);
 }
 auto masked=AML::HDR::Parse("mask rx hdr capability\n",unsupported);assert(masked&&mask(*masked)==0);
 // Alternate driver's legacy typo must not erase HDR/DV or add new HLG support.
 auto legacy=hdr(7);legacy.replace(legacy.find("Hybrid Log-Gamma:"),std::string("Hybrid Log-Gamma:").size(),"Hybrif Log-Gamma:");
 const auto legacyCaps=AML::HDR::Parse(legacy,unsupported);assert(legacyCaps&&mask(*legacyCaps)==3);
 auto legacyFiles=files(15);legacyFiles["hdr_cap"]=legacy;
 const auto legacySnapshot=AML::HDR::ReadStable([&](const char* name){return legacyFiles.at(name);});
 assert(legacySnapshot&&mask(*legacySnapshot)==11);
 assert(!AML::HDR::Parse(legacy+"Hybrid Log-Gamma: 1\n",unsupported));
 assert(!AML::HDR::Parse(legacy+"Hybrif Log-Gamma: 0\n",unsupported));
 legacy.replace(legacy.find("Hybrif Log-Gamma: 1"),std::string("Hybrif Log-Gamma: 1").size(),"Hybrif Log-Gamma: 2");
 assert(!AML::HDR::Parse(legacy,unsupported));
 assert(!AML::HDR::Parse("",unsupported));assert(!AML::HDR::Parse(hdr(7),""));
 assert(!AML::HDR::Parse(hdr(7),"DolbyVision block is error\n"));
 assert(!AML::HDR::Parse(hdr(7),"The Rx don't support DolbyVision\nextra"));
 assert(!AML::HDR::Parse(hdr(7)+"Traditional HDR: 0\n",unsupported));
 assert(!AML::HDR::Parse("Traditional HDR: 2\nHDR10Plus Supported: 1\nHybrid Log-Gamma: 1\n",unsupported));
 assert(!AML::HDR::Parse("Traditional HDR: 1x\nHDR10Plus Supported: 1\nHybrid Log-Gamma: 1\n",unsupported));
 assert(AML::HDR::ReadyEDID("00ffffffffffff00"+std::string(240,'0')));
 assert(!AML::HDR::ReadyEDID(std::string(256,'0')));
 assert(!AML::HDR::ReadyEDID("00ffffffffffff00"+std::string(239,'0')));
 assert(!AML::HDR::ReadyEDID("00ffffffffffff00"+std::string(239,'0')+"g"));
 assert(!AML::HDR::ReadyEDID("00ffffffffffff00"+std::string(8*256,'0')));
 for(const auto& name:{"hpd_state","edid_parsing","rawedid","hdr_cap","dv_cap"}){
  auto values=files(15);values[name]=std::nullopt;
  assert(!AML::HDR::ReadStable([&](const char* field){return values.at(field);}));
  values=files(15);int calls=0;
  assert(!AML::HDR::ReadStable([&](const char* field)->std::optional<std::string>{
   ++calls;if(calls>5&&std::string(field)==name)return "changed";return values.at(field);}));
 }
 for(const auto& value:{"0","?",""}){auto values=files(15);values["hpd_state"]=value;
  assert(!AML::HDR::ReadStable([&](const char* field){return values.at(field);}));}
 auto values=files(15);values["edid_parsing"]="ng";
 assert(!AML::HDR::ReadStable([&](const char* field){return values.at(field);}));
}
void refresh(){
 CWinSystemAmlogic window;auto values=files(15);values["dv_cap"]=std::nullopt;install(values);
 window.RefreshHDRCapabilities();expect(window,0,true);
 for(int bits:{15,4,1,0,8,15}){install(files(bits));confirm(window);expect(window,bits,false);}
 for(const auto& field:{"hpd_state","edid_parsing","rawedid","hdr_cap","dv_cap"}){
  values=files(0);values[field]=std::nullopt;install(values);window.RefreshHDRCapabilities();expect(window,15,true);
  values=files(0);values[field]="";install(values);window.RefreshHDRCapabilities();expect(window,15,true);
 }
 for(const auto& item:std::vector<std::pair<std::string,std::string>>{
  {"hpd_state","0"},{"edid_parsing","ng"},{"rawedid",std::string(256,'0')},
  {"hdr_cap","Traditional HDR: 0\n"},{"dv_cap","DolbyVision block is error"}}){
  values=files(0);values[item.first]=item.second;install(values);window.RefreshHDRCapabilities();expect(window,15,true);
 }
 values=files(0);values["hdr_cap"]=hdr(0)+std::string(1,'\0')+"partial";install(values);
 window.RefreshHDRCapabilities();expect(window,15,true);
 // An opened directory produces a read error rather than a valid empty file.
 std::filesystem::remove(fixtureDirectory+"/hdr_cap");
 std::filesystem::create_directory(fixtureDirectory+"/hdr_cap");
 window.RefreshHDRCapabilities();expect(window,15,true);
 std::filesystem::remove(fixtureDirectory+"/hdr_cap");
 values=files(0);values["hdr_cap"]="mask rx hdr capability\n";install(values);
 confirm(window);expect(window,0,false);
 install(files(8));assert(!window.IsHDRDisplay());expect(window,8,false);
 install(files(7));confirm(window);assert(window.IsHDRDisplay());expect(window,7,false);
 // Same raw EDID, ready link, lost/gained capabilities must replace the cache.
 install(files(0));confirm(window);assert(!window.IsHDRDisplay());expect(window,0,false);
 install(files(15));assert(window.IsHDRDisplay());expect(window,15,false);
 // Memory-only reads cannot trigger refresh when files are unavailable.
 const auto directory=fixtureDirectory;fixtureDirectory+="/absent";
 for(int i=0;i<100;++i)expect(window,15,false);
 fixtureDirectory=directory;
 // Real GUI policy consumes the refreshed HLG bit, preserving output priority.
 settings.values={{"videoscreen.guisdrsaturation",125},{"videoscreen.guipeakluminance.hlg",25},
 {"videoscreen.guisaturation.hlg",175},{"videoscreen.guipeakluminance.dolbyvision",50},
 {"videoscreen.guisaturation.dolbyvision",50}};
 install(files(4));confirm(window);
 auto colour=window.GetGuiColourAdjustment();assert(std::abs(colour.first-0.53252054f)<0.000001f&&colour.second==1.75f);
 install(files(0));confirm(window);colour=window.GetGuiColourAdjustment();assert(colour.first==1.0f&&colour.second==1.25f);
 outputMode=DOLBY_VISION_OUTPUT_MODE_IPT;colour=window.GetGuiColourAdjustment();assert(std::abs(colour.first-0.72974005f)<0.000001f&&colour.second==0.5f);
 outputMode=DOLBY_VISION_OUTPUT_MODE_HDR10;colour=window.GetGuiColourAdjustment();assert(colour.first==1.0f&&colour.second==1.25f);
}
void lossConfirmation(){
 CWinSystemAmlogic first;install(files(0));first.RefreshHDRCapabilities();expect(first,0,true);
 assert(!first.m_hdrCapsValid);
 Clock::now+=std::chrono::milliseconds(999);first.RefreshHDRCapabilities();expect(first,0,true);
 Clock::now+=std::chrono::milliseconds(1);first.RefreshHDRCapabilities();expect(first,0,false);assert(first.m_hdrCapsValid);
 CWinSystemAmlogic window;install(files(15));window.RefreshHDRCapabilities();expect(window,15,false);
 // Configured driver resume exposes ready EDID + cleared flags for 100ms.
 install(files(0));window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(100);window.RefreshHDRCapabilities();expect(window,15,true);
 install(files(15));window.RefreshHDRCapabilities();expect(window,15,false);assert(!window.m_hdrLossCandidate);
 // Restoration cancels old candidate age; genuine loss needs a fresh interval.
 install(files(0));window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(999);window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(1);window.RefreshHDRCapabilities();expect(window,0,false);
 install(files(15));window.RefreshHDRCapabilities();expect(window,15,false);
 // A failed candidate read cancels confirmation even after time has elapsed.
 install(files(4));window.RefreshHDRCapabilities();expect(window,15,true);
 auto invalid=files(4);invalid["dv_cap"]=std::nullopt;install(invalid);
 Clock::now+=std::chrono::seconds(2);window.RefreshHDRCapabilities();expect(window,15,true);assert(!window.m_hdrLossCandidate);
 install(files(4));window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(600);
 install(files(1));window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(400);window.RefreshHDRCapabilities();expect(window,15,true);
 Clock::now+=std::chrono::milliseconds(600);window.RefreshHDRCapabilities();expect(window,1,false);
 // Gain without loss publishes immediately and cancels any pending candidate.
 install(files(0));window.RefreshHDRCapabilities();expect(window,1,true);
 install(files(9));window.RefreshHDRCapabilities();expect(window,9,false);assert(!window.m_hdrLossCandidate);
 // Identical loss mask on a different EDID must restart candidate confirmation.
 install(files(0));window.RefreshHDRCapabilities();expect(window,9,true);
 Clock::now+=std::chrono::seconds(2);auto newSink=files(0);
 (*newSink["rawedid"])[16]='1';install(newSink);
 window.RefreshHDRCapabilities();expect(window,9,true);
 Clock::now+=std::chrono::milliseconds(999);window.RefreshHDRCapabilities();expect(window,9,true);
 Clock::now+=std::chrono::milliseconds(1);window.RefreshHDRCapabilities();expect(window,0,false);
}
void concurrency(){
 CWinSystemAmlogic window;install(files(3));window.RefreshHDRCapabilities();
 std::atomic<bool> stop{false};std::atomic<unsigned int> reads{0};
 std::thread reader([&]{while(!stop.load()){
  const auto bits=mask(window.GetDisplayHDRCapabilities());assert(bits==3||bits==4);++reads;}});
 for(int i=0;i<300;++i){write("hdr_cap",hdr(i%2?3:4));confirm(window);expect(window,i%2?3:4,false);}
 stop.store(true);reader.join();assert(reads.load()>0);
}
int main(int argc,char** argv){assert(argc==2);fixtureDirectory=argv[1];pure();refresh();lossConfirmation();concurrency();}
'''

PROBE_PRELUDE = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <utility>
@ENUM@
struct Clock {static inline std::chrono::steady_clock::time_point now{std::chrono::seconds(1)};
 static auto Now(){return now;}};
std::map<std::string,std::string> values;
int reads=0;
struct CSysfsPath {std::string path;explicit CSysfsPath(const char* p):path(p){}
 bool Exists(){return values.count(path);}
 template<class T>std::optional<T>Get(){++reads;return values.at(path);}};
namespace StringUtils {template<class... T>std::string Format(const char*,const T&... args){
 std::ostringstream out;((out<<args<<'|'),...);return out.str();}}
constexpr int LOGWARNING=0,LOGINFO=1;
struct CLog {template<class... T>static void Log(int,const char*,const T&...) {}};
'''
PROBE_TESTS = r'''
int main(){
 for(const auto& name:{"hpd_state","rxsense_state","rhpd_state","hdmi_used","disp_mode","sink_type"})
  values[std::string("/sys/class/amhdmitx/amhdmitx0/")+name]="1";
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::CHANGED&&reads==6);
 Clock::now+=std::chrono::milliseconds(999);
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::SKIPPED&&reads==6);
 Clock::now+=std::chrono::milliseconds(1);
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::UNCHANGED&&reads==12);
 values["/sys/class/amhdmitx/amhdmitx0/hpd_state"]="0";
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::SKIPPED&&reads==12);
 Clock::now+=std::chrono::seconds(1);
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::CHANGED&&reads==18);
 Clock::now+=std::chrono::seconds(1);
 assert(aml_hdmi_link_probe("test")==AMLHDMILinkSample::UNCHANGED&&reads==24);
}
'''

HOOK_TESTS = r'''
void HDRHooks(){
 Fixture f;auto info=currentNative;
 {auto permit=f.session.Acquire(f.session.Epoch());assert(permit);
  assert(!f.win.CreateNewWindow("",true,info));assert(f.win.hdrRefreshes==0);}
 assert(f.win.CreateNewWindow("",true,info));assert(f.win.hdrRefreshes==1);
 assert(!has("surface-destroy")); // same-mode early return still refreshed
 const int initial=f.win.hdrRefreshes;
 f.win.m_hdrRefreshPending.store(true);linkSample=AMLHDMILinkSample::SKIPPED;
 f.win.PresentRenderImpl(false);assert(f.win.hdrRefreshes==initial);
 linkSample=AMLHDMILinkSample::UNCHANGED;f.win.PresentRenderImpl(false);assert(f.win.hdrRefreshes==initial+1);
 f.win.m_hdrRefreshPending.store(false);f.win.PresentRenderImpl(false);assert(f.win.hdrRefreshes==initial+1);
 linkSample=AMLHDMILinkSample::CHANGED;f.win.PresentRenderImpl(false);assert(f.win.hdrRefreshes==initial+2);
 linkSample=AMLHDMILinkSample::SKIPPED;
}
'''

SCRIPTED_READER = r'''
std::map<std::string,std::optional<std::string>> scriptedValues;
std::map<std::string,int> scriptedReads;
std::string changeField;
std::optional<std::string> scriptedRead(const char* name){
 const bool second=++scriptedReads[name]==2;
 if(second&&changeField==name)return "changed generation";
 return scriptedValues.at(name);
}
'''
SCRIPTED_TEST = r'''
int main(int argc,char** argv){
 assert(argc==2);fixtureDirectory=argv[1];CWinSystemAmlogic window;
 scriptedValues=files(15);window.RefreshHDRCapabilities();expect(window,15,false);
 for(const auto& field:{"hpd_state","edid_parsing","rawedid","hdr_cap","dv_cap"}){
  scriptedReads.clear();changeField=field;window.RefreshHDRCapabilities();expect(window,15,true);
 }
 changeField.clear();scriptedReads.clear();scriptedValues=files(0);
 confirm(window);expect(window,0,false);
 scriptedReads.clear();scriptedValues=files(15);
 window.RefreshHDRCapabilities();expect(window,15,false);
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    window = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogic.cpp').read_text()
    getter = function(window, 'std::pair<float, float> CWinSystemAmlogic::GetGuiColourAdjustment() const')
    settings = (ROOT / 'xbmc/settings/Settings.h').read_text()
    keys = '\n'.join(re.search(r'static constexpr auto ' + name + r'\s*=\s*"[^"]+";', settings)[0]
                     for name in sorted(set(re.findall(r'CSettings::(SETTING_\w+)', getter))))
    aml_header = (ROOT / 'xbmc/utils/AMLUtils.h').read_text()
    modes = '\n'.join(re.findall(r'^#define DOLBY_VISION_OUTPUT_MODE_.*$', aml_header, re.M))
    prelude = PRELUDE.replace('@KEYS@', keys).replace('@MODES@', modes)
    refresh = function(window, 'void CWinSystemAmlogic::RefreshHDRCapabilities()')
    prefix = 'std::string("/sys/class/amhdmitx/amhdmitx0/")'
    assert refresh.count(prefix) == 1
    refresh = refresh.replace(prefix, '(fixtureDirectory + "/")')
    refresh = refresh.replace('std::chrono::steady_clock::now()', 'Clock::Now()')
    query = function(window, 'bool CWinSystemAmlogic::IsHDRDisplay()')
    cache_getter = function(window, 'CHDRCapabilities CWinSystemAmlogic::GetDisplayHDRCapabilities() const')
    code = prelude + refresh + query + cache_getter + getter + TESTS
    run(code, 'production helper + actual file reader/refresh/cache/GUI consumer; ASan/UBSan')
    # A deterministic cross-generation read needs an injected read boundary;
    # execute the same production Refresh publication body with that boundary.
    reader = function(refresh, '[](const char* name) -> std::optional<std::string>')
    scripted_refresh = refresh.replace(reader,
        '[](const char* name) -> std::optional<std::string> {return scriptedRead(name);}')
    scripted_tests = TESTS.replace(function(TESTS, 'int main(int argc,char** argv)'), SCRIPTED_TEST)
    run(prelude + SCRIPTED_READER + scripted_refresh + query + cache_getter + getter + scripted_tests,
        'actual refresh retains previous snapshot for each cross-generation field; retry loss/gain')
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    probe = function(aml, 'AMLHDMILinkSample aml_hdmi_link_probe(').replace(
        'std::chrono::steady_clock::now()', 'Clock::Now()')
    probe_code = PROBE_PRELUDE.replace('@ENUM@', function(aml_header, 'enum class AMLHDMILinkSample') + ';') + probe + PROBE_TESTS
    run(probe_code, 'actual probe 1Hz boundary/SKIPPED/UNCHANGED/CHANGED; six existing sampled reads')
    lifecycle = runpy.run_path(str(ROOT / 'tools/test-aml-window-lifecycle.py'))['harness']()
    main_function = function(lifecycle, 'int main()')
    hooks = lifecycle.replace(main_function, HOOK_TESTS + main_function.replace('{', '{HDRHooks();', 1))
    run(hooks, 'actual admitted same-mode + present pending/changed hooks; all old lifecycle assertions')
    initial = function(window, 'bool CWinSystemAmlogic::InitWindowSystem(CAMLSession::DisplayRequest display)')
    update = function(window, 'void CWinSystemAmlogic::UpdateResolutions()')
    assert 'RefreshHDRCapabilities();' in initial and 'RefreshHDRCapabilities();' in update
    if args.negative_controls:
        for label, before, after in [
            ('old accumulating capability cache', 'm_hdr_caps = *caps;',
             'if(caps->SupportsHDR10()){m_hdr_caps.SetHDR10();}\n'
             'if(caps->SupportsHDR10Plus()){m_hdr_caps.SetHDR10Plus();}\n'
             'if(caps->SupportsHLG()){m_hdr_caps.SetHLG();}\n'
             'if(caps->SupportsDolbyVision()){m_hdr_caps.SetDolbyVision();}'),
            ('clear valid cache on unavailable data', 'm_hdrRefreshPending.store(true);\n    return;',
             'm_hdr_caps=CHDRCapabilities{};m_hdrRefreshPending.store(true);\n    return;'),
            ('render capability getter performs file I/O',
             'CHDRCapabilities CWinSystemAmlogic::GetDisplayHDRCapabilities() const\n{',
             'CHDRCapabilities CWinSystemAmlogic::GetDisplayHDRCapabilities() const\n{\n'
             'std::ifstream unwanted(fixtureDirectory+"/hdr_cap");'
             'const_cast<CWinSystemAmlogic*>(this)->m_hdrRefreshPending.store(!unwanted.is_open());'),
            ('destructive candidate confirmation disabled',
             'now - m_hdrLossSince < std::chrono::seconds(1)',
             'now - m_hdrLossSince < std::chrono::seconds(0)'),
            ('new EDID reuses old loss candidate age', 'm_hdrLossEDID != edid', 'false'),
        ]:
            assert code.count(before) == 1, label
            run(code.replace(before, after), label, negative=True)
        helper_path = 'windowing/amlogic/AMLHDRCapabilities.h'
        helper = (ROOT / 'xbmc' / helper_path).read_text()
        before = 'before[0] != "1" || before[1] != "ok" || !ReadyEDID(before[2])'
        assert before in helper
        run(code, 'missing link/EDID readiness gate', negative=True,
            headers={helper_path: helper.replace(before, 'false')})
        before = 'link != AMLHDMILinkSample::SKIPPED && m_hdrRefreshPending.load()'
        assert before in hooks
        run(hooks.replace(before, 'm_hdrRefreshPending.load()'),
            'pending retry on every skipped frame', negative=True)
    print('AML HDR capabilities: PASS; sampled reconnect/physical HDMI acceptance remains device work')


if __name__ == '__main__':
    main()
