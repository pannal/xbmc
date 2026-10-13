#!/usr/bin/env python3
"""Execute the production border-label switch with detector/player boundaries."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAMES = ["", ".detected", ".left.offset", ".right.offset", ".top.offset", ".bottom.offset"]
IDS = ["", "_DETECTED", "_LEFT_OFFSET", "_RIGHT_OFFSET", "_TOP_OFFSET", "_BOTTOM_OFFSET"]
PREFIX = "PLAYER_PROCESS_VIDEO_BORDERS"


def production_switch():
    source = (ROOT / "xbmc/guilib/guiinfo/PlayerGUIInfo.cpp").read_text()
    start = source.index("    case " + PREFIX + ":")
    end = source.index("    case PLAYER_PROCESS_VIDEO_DOVI_HAS_L5:", start)
    mapping = (ROOT / "xbmc/GUIInfoManager.cpp").read_text()
    constants = (ROOT / "xbmc/guilib/guiinfo/GUIInfoLabels.h").read_text()
    for name, suffix in zip(NAMES, IDS):
        assert re.search(r'\{"video\.borders' + re.escape(name) + r'",\s*' + PREFIX + suffix + r'\s*\}', mapping)
        assert re.search(r'^#define ' + PREFIX + suffix + r'\s+\(PLAYER_PROCESS \+ \d+\)', constants, re.M)
    l5_end = source.index("    case PLAYER_PROCESS_VIDEO_DOVI_L5_DETECT_STATE:", end)
    return source[start:l5_end]


PREAMBLE = r'''
#define FMT_HEADER_ONLY
#include <fmt/format.h>
#include "guilib/guiinfo/GUIInfoLabels.h"
#include <cstdint>
#include <cassert>
#include <iostream>
#include <string>
#include <vector>
namespace StringUtils {
template<class... Args> std::string Format(const char* text, Args... args) {
  return fmt::format(fmt::runtime(text), args...);
}
}
struct Metadata {
  bool has_level5_metadata=true;
  uint16_t level5_active_area_top_offset=180,level5_active_area_bottom_offset=180;
  uint16_t level5_active_area_left_offset=0,level5_active_area_right_offset=0;
} metadata;
struct Cache {
  int width=1920,height=1080;
  int GetVideoWidth() { return width; }
  int GetVideoHeight() { return height; }
  Metadata GetVideoDoViFrameMetadata() { return metadata; }
} cache;
namespace CServiceBroker { Cache& GetDataCacheCore() { return cache; } }
struct Player { bool playing=true; bool IsPlayingVideo() { return playing; } } player;
bool available=true,fullFrame=false;
bool nativeDV=true,manual=false,autoLb=false,additive=true,hc=true,stable=true;
uint16_t manualTop=0,manualBottom=0,nativeTop=140,nativeBottom=140;
bool aml_subtitle_native_dv() { return nativeDV; }
bool aml_dv_get_l5_override(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r) {
  t=manualTop;b=manualBottom;l=r=0;return manual;
}
bool aml_dv_auto_letterbox_get(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r) {
  t=b=20;l=r=0;return autoLb && !manual;
}
bool aml_dv_auto_letterbox_additive() { return additive; }
bool aml_dv_detect_active_area_override_get(int w,int h,uint16_t& t,uint16_t& b,
                                          uint16_t& l,uint16_t& r) {
  t=nativeTop;b=nativeBottom;l=r=0;
  return nativeDV && hc && w==1920 && h==1080;
}
bool aml_dv_detect_active_area_stable() { return nativeDV && stable; }
void aml_dv_detect_active_area_get(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r) {
  t=nativeTop;b=nativeBottom;l=r=0;
}
int calls=0;
bool aml_subtitle_detect_active_area_get(int w,int h,uint16_t& t,uint16_t& b,
                                       uint16_t& l,uint16_t& r,bool estimate) {
  ++calls;
  t=b=l=r=0;
  if(!available || w!=1920 || h!=1080 || !estimate) return false;
  if(!fullFrame) { t=140;b=142;l=3;r=5; }
  return true;
}
struct Info { int m_info; };
bool label(int id,std::string& value) {
  Player* m_appPlayer=&player;Info info{id};
  switch(id) {
'''
TRAILER = r'''
    default:return false;
  }
}
void dump() {
  for(int id : {PLAYER_PROCESS_VIDEO_BORDERS,PLAYER_PROCESS_VIDEO_BORDERS_DETECTED,
      PLAYER_PROCESS_VIDEO_BORDERS_LEFT_OFFSET,PLAYER_PROCESS_VIDEO_BORDERS_RIGHT_OFFSET,
      PLAYER_PROCESS_VIDEO_BORDERS_TOP_OFFSET,PLAYER_PROCESS_VIDEO_BORDERS_BOTTOM_OFFSET}) {
    std::string value="previous-video";
    if(!label(id,value)) std::abort();
    std::cout << value << '\n';
  }
}
void expectL5(const std::vector<std::string>& expected) {
  const std::vector<int> ids={PLAYER_PROCESS_VIDEO_DOVI_HAS_L5,
      PLAYER_PROCESS_VIDEO_DOVI_L5_TOP_OFFSET,PLAYER_PROCESS_VIDEO_DOVI_L5_BOTTOM_OFFSET,
      PLAYER_PROCESS_VIDEO_DOVI_L5_LEFT_OFFSET,PLAYER_PROCESS_VIDEO_DOVI_L5_RIGHT_OFFSET,
      PLAYER_PROCESS_VIDEO_DOVI_L5_DETECTED,PLAYER_PROCESS_VIDEO_DOVI_L5_AUTO,
      PLAYER_PROCESS_VIDEO_DOVI_ACTIVE_AREA_CLASS};
  assert(expected.size()==ids.size());
  for(size_t i=0;i<ids.size();++i) {
    std::string value="stale";assert(label(ids[i],value));assert(value==expected[i]);
  }
}
int main() {
  dump();fullFrame=true;dump();available=false;dump();
  available=true;fullFrame=false;cache.width=1280;dump();cache.width=1920;
  player.playing=false;const int before=calls;dump();
  if(calls!=before) return 2;
  player.playing=true;
  // Bad authored L5 is superseded only by invariant high-confidence detection.
  expectL5({"1","140","140","0","0","1","0","3"});
  nativeTop=nativeBottom=90;expectL5({"1","90","90","0","0","1","0","2"});
  manual=true;expectL5({"1","0","0","0","0","0","0","0"});
  manualTop=8;manualBottom=10;expectL5({"1","8","10","0","0","0","0","0"});
  manual=false;autoLb=true;expectL5({"1","200","200","0","0","0","1","3"});
  additive=false;expectL5({"1","20","20","0","0","0","1","0"});
  autoLb=false;hc=false;expectL5({"1","180","180","0","0","0","0","3"});
  metadata={false,0,0,0,0};nativeTop=nativeBottom=70;
  expectL5({"1","70","70","0","0","1","0","1"});
  metadata={true,180,180,0,0};hc=true;nativeTop=nativeBottom=0;
  expectL5({"1","0","0","0","0","1","0","0"});
  nativeDV=false;metadata={false,0,0,0,0};
  expectL5({"0","0","0","0","0","0","0","0"});
  nativeDV=true;autoLb=true;additive=true;metadata={true,65530,65530,0,0};
  expectL5({"1","65535","65535","0","0","0","1","3"});
}
'''


def check(binary, env):
    run = subprocess.run([str(binary)], env=env, capture_output=True, text=True)
    if run.returncode:
        return False
    rows = run.stdout.splitlines()
    if len(rows) != 30:
        return False
    for case in range(5):
        values = rows[case * 6:(case + 1) * 6]
        snapshot = json.loads(values[0])
        expected = dict(version=1, valid=case < 2, coordinate_system="coded-video-pixels",
                        width=1920 if case < 2 else None, height=1080 if case < 2 else None,
                        left=3 if case == 0 else 0 if case == 1 else None,
                        right=5 if case == 0 else 0 if case == 1 else None,
                        top=140 if case == 0 else 0 if case == 1 else None,
                        bottom=142 if case == 0 else 0 if case == 1 else None)
        offsets = ["3", "5", "140", "142"] if case == 0 else ["0"] * 4 if case == 1 else [""] * 4
        if snapshot != expected or values[1:] != ["1" if case < 2 else "0"] + offsets:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    parser.add_argument("--fmt-include", default=os.environ.get("FMT_INCLUDE_DIR"))
    args = parser.parse_args()
    switch = production_switch()
    variants = {"production": switch}
    if args.negative_controls:
        variants.update({
            "stopped-player": switch.replace("m_appPlayer->IsPlayingVideo() &&", "true &&"),
            "omit-subtitle-envelope": switch.replace("left, right, true)", "left, right, false)"),
            "swapped-top": switch.replace("std::to_string(top)", "std::to_string(bottom)"),
            "stale-unavailable": switch.replace("value.clear();", ""),
            "wrong-json-order": switch.replace("width, height, left, top, right, bottom", "width, height, right, top, left, bottom"),
            "authored-beats-high-confidence": switch.replace("const bool highConfidence = !manual && !autoLb &&", "const bool highConfidence = !sourceHasL5 && !manual && !autoLb &&"),
            "manual-zero-missing": switch.replace("if (manual)\n", "if (manual && (mTop || mBottom || mLeft || mRight))\n"),
            "auto-loses-authority": switch.replace("!manual && !autoLb &&", "!manual &&"),
            "weak-detection-overrides-source": switch.replace("!sourceHasL5 && aml_dv_detect_active_area_stable()", "aml_dv_detect_active_area_stable()"),
            "additive-overflow": switch.replace("sum > 0xFFFF ? 0xFFFF : sum", "sum"),
        })
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    with tempfile.TemporaryDirectory(prefix="border-labels-") as temp:
        folder = Path(temp)
        for name, body in variants.items():
            source = folder / (name + ".cpp")
            source.write_text(PREAMBLE + body + TRAILER)
            binary = folder / name
            command = [os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                       "-Wno-unused-variable", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                       "-no-pie", "-I" + str(ROOT / "xbmc")]
            if args.fmt_include:
                command += ["-I" + args.fmt_include]
            subprocess.run(command + [str(source), "-o", str(binary)], check=True)
            passed = check(binary, env)
            assert passed == (name == "production"), name
            print(("PASS " if passed else "REJECTED ") + name)


if __name__ == "__main__":
    main()
