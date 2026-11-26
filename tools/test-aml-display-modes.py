#!/usr/bin/env python3
"""Exercise the Amlogic mode parser and probe with a synthetic HDMI capability list."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / "tools/test-render-slot-publication.py"))["function"]

FIXTURE = r'''
#include "windowing/Resolution.h"
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <iterator>
#include <optional>
#include <sstream>
#include <strings.h>
#include <utility>
#include <vector>

constexpr int D3DPRESENTFLAG_INTERLACED = 1, D3DPRESENTFLAG_PROGRESSIVE = 4,
    D3DPRESENTFLAG_MODE3DSBS = 8, D3DPRESENTFLAG_MODE3DTB = 16, D3DPRESENTFLAG_MODE3DFP = 32;
enum { AML_DISPLAY_AXIS_PARAM_WIDTH, AML_DISPLAY_AXIS_PARAM_HEIGHT };
enum { AML_NO_H264_4K2K, AML_HAS_H264_4K2K };
static bool nativeGui, fractional;
static std::string capabilities;
int aml_axis_value(int) { return 0; }
int aml_support_h264_4k2k() { return AML_HAS_H264_4K2K; }
bool aml_display_is_widescreen() { return true; }
bool aml_display_support_3d() { return false; }
bool aml_has_frac_rate_policy() { return fractional; }
struct CSettings {
  static constexpr int SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING = 1;
  CSettings* GetSettings() { return this; }
  bool GetBool(int) { return nativeGui; }
};
struct CServiceBroker {
  static CSettings* GetSettingsComponent() { static CSettings settings; return &settings; }
};
struct CSpecialProtocol {
  static std::string TranslatePath(const char* path) { return path; }
};
struct CSysfsPath {
  std::string path;
  bool Exists() const { return path == "/sys/class/amhdmitx/amhdmitx0/disp_cap"; }
  template<typename T> std::optional<T> Get() const { assert(Exists()); return capabilities; }
};
namespace StringUtils {
void Trim(std::string& s) {
  s.erase(0, s.find_first_not_of(" \t\r\n"));
  s.erase(s.find_last_not_of(" \t\r\n") + 1);
}
bool EndsWith(const std::string& s, const std::string& suffix) {
  return s.size() >= suffix.size() && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}
bool StartsWith(const char* s, const char* prefix) { return std::string(s).find(prefix) == 0; }
bool EqualsNoCase(const std::string& s, const char* other) { return !strcasecmp(s.c_str(), other); }
template<typename... Args> std::string Format(const char*, Args&&...) { return {}; }
std::vector<std::string> Split(const std::string& s, const char* delimiter) {
  std::vector<std::string> lines;
  std::istringstream input(s);
  for (std::string line; std::getline(input, line, *delimiter); ) lines.push_back(line);
  return lines;
}
}
'''

CHECKS = r'''
int main() {
  for (bool native : {false, true}) {
    nativeGui = native;
    for (bool useFractional : {false, true}) {
      fractional = useFractional;
      for (const auto& [name, rate] : std::vector<std::pair<std::string, int>>{
               {"smpte25hz", 25}, {"smpte30hz", 30}, {"smpte50hz", 50}, {"smpte60hz", 60},
               {"smpte24hz", 24}, {"4k2ksmpte", 24}, {"SMPTE24HZ", 24},
               {"smpte50hz420", 50}, {"smpte60hz420", 60}, {"4096x2160p60hz", 60}}) {
        capabilities = name + "*\n0p60hz\n";
        std::vector<RESOLUTION_INFO> modes;
        assert(aml_probe_resolutions(modes));
        bool hasFractional = fractional && (rate == 24 || rate == 30 || rate == 60);
        assert(modes.size() == (hasFractional ? 2u : 1u));
        for (const auto& mode : modes) {
          assert(mode.strId == name && mode.dwFlags == D3DPRESENTFLAG_PROGRESSIVE);
          assert(mode.iScreenWidth == 4096 && mode.iScreenHeight == 2160);
          assert(mode.iWidth == (native ? 4096 : 1920) && mode.iHeight == (native ? 2160 : 1080));
        }
        assert(modes.front().fRefreshRate == rate);
        if (hasFractional) assert(std::abs(modes.back().fRefreshRate - rate / 1.001f) < 0.001f);
      }
    }
  }
  RESOLUTION_INFO result;
  assert(!aml_mode_to_resolution("smpte120hz", &result));
  assert(!aml_mode_to_resolution("smpte25hz420", &result));
  assert(aml_mode_to_resolution("2160p60hz", &result) && result.iScreenWidth == 3840);
}
'''


def main():
    aml = (ROOT / "xbmc/utils/AMLUtils.cpp").read_text()
    resolution = (ROOT / "xbmc/windowing/Resolution.cpp").read_text()
    methods = [function(resolution, "RESOLUTION_INFO::RESOLUTION_INFO(int width"),
               function(resolution, "RESOLUTION_INFO::RESOLUTION_INFO(const RESOLUTION_INFO&"),
               function(aml, "bool aml_mode_to_resolution("),
               function(aml, "bool aml_probe_resolutions(")]
    with tempfile.TemporaryDirectory() as directory:
        exe = Path(directory) / "display-modes"
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Werror", "-I", str(ROOT / "xbmc"),
                        "-x", "c++", "-", "-o", str(exe)], input="\n".join([FIXTURE, *methods, CHECKS]),
                       text=True, check=True)
        subprocess.run([str(exe)], cwd=directory, check=True)
    print("PASS: SMPTE timing IDs, GUI sizing and fractional mode enumeration")


if __name__ == "__main__":
    main()
