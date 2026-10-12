#!/usr/bin/env python3
"""Exercise production GUI-startup policy with recorded settings/sysfs effects.

Checks both successful window paths and that EGL failures precede engagement.
No device or HDMI verification is implied.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
PREFIX = r'''
#include <cassert>
#include <optional>
#include <string>
#include <vector>
constexpr int DV_MODE_ON=0,DV_MODE_ON_DEMAND=1,DV_MODE_OFF=2;
constexpr unsigned DOLBY_VISION_OUTPUT_MODE_IPT=0,DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL=1;
constexpr unsigned DOLBY_VISION_OUTPUT_MODE_HDR10=2,DOLBY_VISION_OUTPUT_MODE_SDR10=3,DOLBY_VISION_OUTPUT_MODE_BYPASS=5;
constexpr int LOGINFO=1,LOGDEBUG=0;
struct CLog {template<class... T> static void Log(T...) {}};
struct CSettings {
 static constexpr auto SETTING_COREELEC_AMLOGIC_DV_ENGAGE_AT_STARTUP="startup";
 static constexpr auto SETTING_COREELEC_AMLOGIC_DV_VS10_DV="mapping";
};
struct Settings {
 bool enabled=true;int mapping=DOLBY_VISION_OUTPUT_MODE_SDR10;
 bool GetBool(const char*)const{return enabled;}
 int GetInt(const char*)const{return mapping;}
 Settings* GetSettings(){return this;}
} settings;
struct CServiceBroker {static Settings* GetSettingsComponent(){return &settings;}};
int dvMode=DV_MODE_ON_DEMAND,reads=0;
bool playing=false;
std::optional<unsigned> currentMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;
std::vector<std::string> effects;
int aml_dv_mode(){return dvMode;}
bool aml_dv_playback_active(){return playing;}
void aml_dv_on(unsigned mode){assert(mode==DOLBY_VISION_OUTPUT_MODE_SDR10);effects.push_back("on");currentMode=mode;}
void aml_dv_display_trigger(){effects.push_back("HDMI");}
struct CSysfsPath {
 explicit CSysfsPath(const char*){}
 template<class T>std::optional<T> Get(){++reads;return currentMode;}
};
struct CWinSystemAmlogicGLESContext {
 bool m_firstStartGUI=true;
 void EngageDolbyVisionAtStartup();
};
'''
TESTS = r'''
void reset(){settings=Settings{};dvMode=DV_MODE_ON_DEMAND;playing=false;reads=0;currentMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;effects.clear();}
int main(){
 for(int initial:{DV_MODE_OFF,DV_MODE_ON}) {
  reset();CWinSystemAmlogicGLESContext gui;dvMode=initial;
  gui.EngageDolbyVisionAtStartup();assert(!gui.m_firstStartGUI&&effects.empty()&&reads==0);
  dvMode=DV_MODE_ON_DEMAND;playing=true;gui.EngageDolbyVisionAtStartup();assert(effects.empty());
  playing=false;gui.EngageDolbyVisionAtStartup();assert(effects.empty());
 }
 reset();CWinSystemAmlogicGLESContext disabled;settings.enabled=false;
 disabled.EngageDolbyVisionAtStartup();settings.enabled=true;
 disabled.EngageDolbyVisionAtStartup();assert(effects.empty()&&reads==0);
 reset();CWinSystemAmlogicGLESContext busy;playing=true;
 busy.EngageDolbyVisionAtStartup();playing=false;
 busy.EngageDolbyVisionAtStartup();assert(effects.empty()&&reads==0);
 for(int mapping:{0,1,2,5,99}) {
  reset();CWinSystemAmlogicGLESContext unsupported;settings.mapping=mapping;
  unsupported.EngageDolbyVisionAtStartup();assert(effects.empty()&&reads==0&&!unsupported.m_firstStartGUI);
 }
 reset();CWinSystemAmlogicGLESContext missing;currentMode=std::nullopt;
 missing.EngageDolbyVisionAtStartup();assert(effects.empty()&&reads==1);
 currentMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;missing.EngageDolbyVisionAtStartup();assert(effects.empty());
 reset();CWinSystemAmlogicGLESContext same;currentMode=DOLBY_VISION_OUTPUT_MODE_SDR10;
 same.EngageDolbyVisionAtStartup();assert(effects.empty()&&reads==1);
 reset();CWinSystemAmlogicGLESContext eligible;
 eligible.EngageDolbyVisionAtStartup();assert(effects==std::vector<std::string>({"on","HDMI"}));
 currentMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;eligible.EngageDolbyVisionAtStartup();
 assert(effects.size()==2&&reads==1&&!eligible.m_firstStartGUI);
}
'''

def source():
    cpp = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    window = function(cpp, 'bool CWinSystemAmlogicGLESContext::CreateNewWindow(')
    # These are the actual successful exits; all fallible initialization precedes them.
    hook = 'EngageDolbyVisionAtStartup();'
    assert window.count(hook) == 2
    early = window.index(hook)
    assert window.index('IsPrimaryContextCurrent()') < early < window.index('return true;', early)
    late = window.rindex(hook)
    assert window.index('if (!native.TryBegin())') < early
    for operation in ('CreateNativeWindow(', 'CreateSurface(', 'BindContext()'):
        pos = window.index(operation)
        assert early < pos < window.index('return false;', pos) < late
    assert late < window.rindex('return true;')
    method = function(cpp, 'void CWinSystemAmlogicGLESContext::EngageDolbyVisionAtStartup()')
    return PREFIX + method + TESTS

def run(code, negative=False):
    with tempfile.TemporaryDirectory(prefix='dv-startup-') as tmp:
        p = Path(tmp); (p / 'test.cpp').write_text(code)
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer','-fno-pie','-no-pie',str(p/'test.cpp'),'-o',str(p/'test')],check=True)
        result = subprocess.run([str(p/'test')],capture_output=True,text=True,timeout=10)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print('PASS: startup consumption, playback exclusion, supported mapping and HDMI publication')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    code=source();run(code)
    if args.negative_controls:
        for label, old, new in (
            ('leave startup armed outside On Demand','  m_firstStartGUI = false;','  if (aml_dv_mode() == DV_MODE_ON_DEMAND) m_firstStartGUI = false;'),
            ('engage during playback',' || aml_dv_playback_active()',''),
            ('engage unsupported conversion','configuredMode != static_cast<int>(DOLBY_VISION_OUTPUT_MODE_SDR10)','configuredMode == static_cast<int>(DOLBY_VISION_OUTPUT_MODE_IPT)'),
            ('omit HDMI publication','  aml_dv_display_trigger();',''),
            ('repeat startup engagement','  if (!m_firstStartGUI)\n    return;',''),
        ):
            assert code.count(old)==1,label
            run(code.replace(old,new,1),True);print('REJECTED:',label)

if __name__ == '__main__':
    main()
