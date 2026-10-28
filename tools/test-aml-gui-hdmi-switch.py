#!/usr/bin/env python3
"""Check production DV mode-switch decisions across GUI-only resizing.

Extract the actual DV requirement, forced-switch decision and native mode/trigger
branches. The hardware boundary is simulated; this does not prove receiver lock.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--window-source', type=Path)
args = parser.parse_args()
window = (args.window_source or ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
base = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogic.cpp').read_text()
start = window.index('  bool force_mode_switch_by_dv =')
requirement = window[start:window.index('\n\n', start)]
end = window.index('  // refresh backup data', start)
start = window.rindex('  if (', start, end)
# The outer decision includes one nested log but no nested if.
force = window[start:end]
start = base.index('  const bool modeChanging =')
changing = base[start:base.index('  aml_set_native_resolution(', start)]
start = base.index('  if (modeChanging || !aml_native_gui_size_changing())')
trigger = base[start:base.index('  m_bWindowCreated = true;', start)]
preamble = r'''
#include <cstdio>
struct Info { int iWidth,iHeight,iScreenWidth,iScreenHeight; float fRefreshRate; };
enum class StreamHdrType { SDR, HDR10, HDR_TYPE_DOLBYVISION };
struct CLog { template<class... T> static void Log(T...) {} };
constexpr int LOGDEBUG=0, DV_MODE_OFF=0;
static int dvMode;
static bool outputChanges, guiChanges, hasFrac, engaged, triggered;
static int aml_dv_mode() { return dvMode; }
static bool aml_display_mode_changing(const Info&) { return outputChanges; }
static bool aml_has_frac_rate_policy() { return hasFrac; }
static bool aml_native_gui_size_changing() { return guiChanges; }
static void aml_dv_engage_deferred_disc(bool) { engaged=true; }
static void aml_dv_display_trigger() { triggered=true; }
static int failures=0, checks=0;
#define CHECK(x) do { checks++; if (!(x)) { \
    std::fprintf(stderr, "%s: %s\n", name, #x); failures++; } } while (0)
struct Case {
    const char* name;
    int beforeGui, afterGui;
    StreamHdrType beforeHdr, afterHdr;
    bool bypass, dvEnabled, physicalChange, stereoChange, fractionalChange;
    bool expectForce, expectMode, expectTrigger;
};
static void check(const Case& c) {
    const char* name=c.name;
    Info current_resolution{c.beforeGui,c.beforeGui/16*9,3840,2160,60.0f};
    Info res{c.afterGui,c.afterGui/16*9,c.physicalChange ? 1920:3840,
             c.physicalChange ? 1080:2160,60.0f};
    StreamHdrType hdrType=c.afterHdr,m_hdrType=c.beforeHdr;
    bool bypass_dv_mode_switch_gui=c.bypass;
    bool m_force_mode_switch=false;
    int fractional_rate=0,cur_fractional_rate=c.fractionalChange ? 1:0;
    int m_stereo_mode=0,stereo_mode=c.stereoChange ? 1:0;
    dvMode=c.dvEnabled ? 1:0;
    outputChanges=c.physicalChange || c.fractionalChange;
    guiChanges=c.beforeGui != c.afterGui;
    hasFrac=true;engaged=triggered=false;
'''
cases = r'''
    CHECK(m_force_mode_switch == c.expectForce);
    CHECK(modeChanging == c.expectMode);
    CHECK(engaged == c.expectMode);
    CHECK(triggered == c.expectTrigger);
}
int main() {
    using H=StreamHdrType;
    const Case cases[] = {
        {"ordinary DV entry",3840,3840,H::SDR,H::HDR_TYPE_DOLBYVISION,false,true,false,false,false,true,true,true},
        {"resize with DV entry",3840,1920,H::SDR,H::HDR_TYPE_DOLBYVISION,false,true,false,false,false,true,true,true},
        {"restore with DV exit",1920,3840,H::HDR_TYPE_DOLBYVISION,H::SDR,false,true,false,false,false,true,true,true},
        {"restore DV to HDR10",1920,3840,H::HDR_TYPE_DOLBYVISION,H::HDR10,false,true,false,false,false,true,true,true},
        {"SDR resize only",3840,1920,H::SDR,H::SDR,false,true,false,false,false,false,false,false},
        {"SDR restore only",1920,3840,H::SDR,H::SDR,false,true,false,false,false,false,false,false},
        {"unchanged DV resizing",3840,1920,H::HDR_TYPE_DOLBYVISION,H::HDR_TYPE_DOLBYVISION,false,true,false,false,false,false,false,false},
        {"explicit handshake bypass",3840,1920,H::SDR,H::HDR_TYPE_DOLBYVISION,true,true,false,false,false,false,false,false},
        {"DV disabled",3840,1920,H::SDR,H::HDR_TYPE_DOLBYVISION,false,false,false,false,false,false,false,false},
        {"real output switch",3840,1920,H::SDR,H::HDR_TYPE_DOLBYVISION,false,true,true,false,false,false,true,true},
        {"fractional policy switch",3840,1920,H::SDR,H::SDR,false,true,false,false,true,true,true,true},
        {"stereo switch",3840,1920,H::SDR,H::SDR,false,true,false,true,false,true,true,true},
    };
    for (const auto& c:cases) check(c);
    std::printf("GUI/HDMI decisions: %d checks, %d failures\n",checks,failures);
    return failures ? 1:0;
}
'''
with tempfile.TemporaryDirectory(prefix='aml-gui-hdmi-') as tmp:
    c=Path(tmp)/'test.cpp';exe=Path(tmp)/'test'
    c.write_text(preamble+textwrap.indent(requirement+'\n'+force+changing+trigger, '  ')+cases)
    subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                    '-fsanitize=undefined','-fno-sanitize-recover=all',str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
