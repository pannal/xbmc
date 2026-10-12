#!/usr/bin/env python3
"""Exercise the actual TV preset against display/backend/VP/output combinations.

Settings writes and EDID/backend queries are recorded stubs. Does not exercise
Kodi callbacks, GUI scheduling, the loaded module or HDMI output on a device.
"""
import argparse
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]

PREFIX = r'''

#include <cassert>
#include <iostream>
#include <map>
#include <string>
struct CLog
{
  template<class... T>
  static void Log(T...)
  {
  }
};
constexpr int LOGINFO = 0;
enum DV_MODE
{
  DV_MODE_ON = 0,
  DV_MODE_ON_DEMAND = 1,
  DV_MODE_OFF = 2
};
enum DV_TYPE
{
  DV_TYPE_DISPLAY_LED = 0,
  DV_TYPE_PLAYER_LED_LLDV = 1,
  DV_TYPE_PLAYER_LED_HDR = 2,
  DV_TYPE_VS10_ONLY = 3,
  DV_TYPE_PLAYER_LED_HDR2 = 4
};
struct CSettings
{
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_VS10_DV =
    "coreelec.amlogic.dolbyvision.vs10.dv";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND =
    "coreelec.amlogic.dolbyvision.cmv40.append";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP =
    "coreelec.amlogic.dolbyvision.cmv40.strip";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY =
    "coreelec.amlogic.dolbyvision.dual.priority";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT =
    "coreelec.amlogic.dolbyvision.hdr10plus.convert";
  static constexpr const char*
    SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PREFER_CONVERT =
      "coreelec.amlogic.dolbyvision.hdr10plus.prefer.convert";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_MODE =
    "coreelec.amlogic.dolbyvision.mode";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_TYPE =
    "coreelec.amlogic.dolbyvision.type";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR =
    "coreelec.amlogic.dolbyvision.video.processor";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_VS10_SDR8 =
    "coreelec.amlogic.dolbyvision.vs10.sdr8";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_PREFER_12BIT =
    "coreelec.amlogic.prefer.12bit";
};
struct Settings
{
  std::map<std::string, int> v;
  int GetInt(const char* k) { return v[k]; }
  void SetInt(const char* k, int x)
  {
    v[k] = x;
    if (std::string(k) == CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND &&
        x)
      v[CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP] = 0;
  }
  void SetBool(const char* k, bool x)
  {
    v[k] = x;
    if (std::string(k) == CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP &&
        x)
      v[CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND] = 0;
  }
} st;
Settings*
settings()
{
  return &st;
}
struct Cap
{
  const char* edid_pnpid = "test";
  bool dv = false, std = false, ll = false, plus = false, pq = false;
} cap;
Cap
aml_read_dv_cap()
{
  return cap;
}
bool
aml_display_support_dv()
{
  return cap.dv;
}
bool
aml_display_support_dv_std()
{
  return cap.std;
}
bool
aml_display_support_dv_ll()
{
  return cap.ll;
}
bool
aml_display_support_hdr10plus()
{
  return cap.plus;
}
bool
aml_display_support_hdr_pq()
{
  return cap.pq;
}
struct CDolbyVisionAML
{
  void apply_tv_preset(int);
};
enum TV_PRESET : int
{
  TV_PRESET_MANUAL = 0,
  TV_PRESET_AUTO,
  TV_PRESET_LG,
  TV_PRESET_SONY,
  TV_PRESET_SAMSUNG,
  TV_PRESET_PANASONIC,
  TV_PRESET_PHILIPS,
  TV_PRESET_TCL
};

constexpr unsigned int DOLBY_VISION_OUTPUT_MODE_IPT = 0,
                       DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL = 1;
bool backend = false;
bool
aml_dv_new_backend_available()
{
  return backend;
}
'''
TEST = r'''


int
main()
{
  CDolbyVisionAML dv;
  int checks = 0;
  for (int route = 0; route < 4; ++route)
    for (bool available : { false, true })
      for (int output : { 0, 1, 2, 3, 4, 5 })
        for (int vp : { 0, 1, 4 })
          for (int mode : { 0, 1, 2 }) {
            backend = available;
            cap = {
              "test", route < 3, route == 0, route == 1, route == 3, true
            };
            st.v.clear();
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE] = mode;
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND] = 0;
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP] = 1;
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV] = output;
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR] = vp;
            st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR8] = 5;
            dv.apply_tv_preset(TV_PRESET_AUTO);
            // Standard DV keeps its legacy CMv4 route. Native LLDV and HDR2
            // need dovi5.
            const bool eligible =
              vp == 0 && (route == 0 || (available && output < 2));
            assert(
              st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND) ==
              (eligible ? 3 : 0));
            assert(
              st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP) ==
              !eligible);
            assert(st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE) ==
                   (mode == 2 ? 1 : mode));
            assert(st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE) ==
                   (route == 0   ? 0
                    : route == 1 ? 1
                                 : 4));
            assert(st.GetInt(
                     CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR) ==
                   vp);
            assert(st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV) ==
                   output);
            assert(
              st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR8) == 5);
            assert(
              st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY) ==
              (route == 3 ? 1 : 0));
            ++checks;
          }
  cap = { "test", true, false, false, false, false };
  st.v.clear();
  st.v[CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP] = 1;
  dv.apply_tv_preset(TV_PRESET_AUTO);
  assert(st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE) == 3);
  assert(st.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP) == 1);
  for (int preset : { TV_PRESET_MANUAL, TV_PRESET_AUTO }) {
    cap = {};
    auto before = st.v;
    dv.apply_tv_preset(preset);
    assert(st.v == before);
  }
  std::cout
    << checks
    << " preset route checks passed; Manual, SDR-only and VS10 preserved\n";
}
'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=pathlib.Path, default=ROOT / 'xbmc/windowing/amlogic/DolbyVisionAML.cpp')
    args = parser.parse_args()
    production = function(args.source.read_text(), 'void CDolbyVisionAML::apply_tv_preset(int preset)')
    with tempfile.TemporaryDirectory(prefix='dv-tv-preset-') as tmp:
        cpp, exe = pathlib.Path(tmp) / 'preset.cpp', pathlib.Path(tmp) / 'preset'
        cpp.write_text(PREFIX + production + TEST)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wdouble-promotion', str(cpp), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

if __name__ == '__main__':
    main()
