#!/usr/bin/env python3
"""Host checks of the production DV target label with player/settings/sysfs stubs."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
label = 'PLAYER_PROCESS_VIDEO_DOVI_VSVDB_MAX_LUM'
mapping = (root / 'xbmc/GUIInfoManager.cpp').read_text()
for alias in ('amlogic.dv.target.max.nits', 'video.dovi.vsvdb.max.lum'):
    matches = re.findall(r'\{"' + re.escape(alias) + r'",\s*(\w+)\s*\}', mapping)
    assert matches == [label], (alias, matches)

labels = (root / 'xbmc/guilib/guiinfo/GUIInfoLabels.h').read_text()
offset = re.search(r'#define ' + label + r' \(PLAYER_PROCESS \+ (\d+)\)', labels)[1]
assert len(re.findall(r'\(PLAYER_PROCESS \+ ' + offset + r'\)', labels)) == 1

source = (root / 'xbmc/guilib/guiinfo/PlayerGUIInfo.cpp').read_text()
start = source.index('    case ' + label + ':')
end = source.index('    case PLAYER_PROCESS_VIDEO_DOVI_L1_MIN_PQ:', start)
body = source[start:end]
aml = (root / 'xbmc/utils/AMLUtils.h').read_text()
types = re.search(r'enum DV_TYPE : int\s*\{.*?\};', aml, re.S)[0]
modes = '\n'.join(line for line in aml.splitlines()
                  if line.startswith('#define DOLBY_VISION_OUTPUT_MODE_'))

preamble = r'''
#include <cassert>
#include <cstdio>
#include <optional>
#include <string>
static bool playing = true;
static int target = 1000;
static std::optional<std::string> mode = "0";
struct Player { bool IsPlayingVideo() const { return playing; } } player;
static Player* m_appPlayer = &player;
struct CSettings {
  static constexpr auto SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM = "target";
  int GetInt(const char*) const { return target; }
} settings;
struct SettingsComponent {
  CSettings* GetSettings() { return &settings; }
} component;
struct CServiceBroker {
  static SettingsComponent* GetSettingsComponent() { return &component; }
};
struct CSysfsPath {
  explicit CSysfsPath(const char* path) {
    assert(std::string(path) == "/sys/module/amdolby_vision/parameters/dolby_vision_mode");
  }
  template<typename T> std::optional<T> Get() { return mode; }
};
'''
harness = r'''
static DV_TYPE type = DV_TYPE_PLAYER_LED_LLDV;
static DV_TYPE aml_dv_type() { return type; }
static bool get(std::string& value) {
  switch (0) {
'''
cases = r'''
  }
  return false;
}
int main() {
  int checks = 0;
  for (int t = -1; t <= 5; ++t) {
    type = static_cast<DV_TYPE>(t);
    for (const auto& m : {"0", "1", "2", "3", "4", "5", "", "bad", "0garbage"}) {
      mode = m;
      for (bool p : {false, true}) {
        playing = p;
        for (int n : {-1, 0, 96, 447, 1000, 10000}) {
          target = n;
          std::string value = "stale";
          assert(get(value));
          const bool applicable = p && (t == 1 || t == 2 || t == 4) &&
                                  (std::string(m) == "0" || std::string(m) == "1") && n > 0;
          assert(value == (applicable ? std::to_string(n) : ""));
          ++checks;
        }
      }
    }
  }
  playing = true;
  type = DV_TYPE_PLAYER_LED_LLDV;
  target = 1000;
  mode = std::nullopt;
  std::string value = "stale";
  assert(get(value) && value.empty());
  mode = "0";
  assert(get(value) && value == "1000");
  target = 447; // Live EDID-derived/override value, without payload quantization.
  assert(get(value) && value == "447");
  playing = false;
  assert(get(value) && value.empty());
  std::printf("Both aliases and %d availability/value cases passed\n", checks + 4);
}
'''
with tempfile.TemporaryDirectory(prefix='kodi-dv-target-') as tmp:
    src = Path(tmp) / 'test.cpp'
    binary = Path(tmp) / 'test'
    src.write_text(preamble + types + '\n' + modes + harness +
                   body.replace('case ' + label + ':', 'case 0:') + cases)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-fsanitize=address,undefined', '-o', str(binary), str(src)],
                   check=True)
    subprocess.run([str(binary)], check=True)
