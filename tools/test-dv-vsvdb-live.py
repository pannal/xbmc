#!/usr/bin/env python3
"""Host checks of the live VSVDB refresh against a model of the kernel's per-frame counters."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'xbmc/utils/AMLUtils.cpp').read_text()
start = source.index('void aml_dv_refresh_vsvdb_live()')
end = source.index('\n}\n', start) + 3
body = source[start:end]

preamble = r'''
#include <cassert>
#include <cstdio>
#include <map>
#include <optional>
#include <string>
#include <vector>
#define LOGINFO 0
static bool dv_enabled = true;
static std::string configured_payload;
static std::map<std::string, std::string> sysfs;
static std::vector<std::string> writes;
static const std::string P = "/sys/module/amdolby_vision/parameters/";
struct CDVCoreGuard { explicit CDVCoreGuard(const char*) {} };
static bool aml_is_dv_enable() { return dv_enabled; }
struct CSettings {
  static constexpr auto SETTING_COREELEC_AMLOGIC_DV_VSVDB_PAYLOAD = "payload";
  static constexpr auto SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM = "maxlum";
  std::string GetString(const char*) const { return configured_payload; }
  int GetInt(const char*) const { return 1000; }
} settings_instance;
static CSettings* settings() { return &settings_instance; }
struct CLog { template<typename... A> static void Log(int, A&&...) {} };
struct CSysfsPath {
  std::string path;
  explicit CSysfsPath(const std::string& p) : path(p) {}
  CSysfsPath(const std::string& p, const std::string& v) : path(p) { set(v); }
  CSysfsPath(const std::string& p, unsigned int v) : path(p) { set(std::to_string(v)); }
  void set(const std::string& v) { sysfs[path] = v; writes.push_back(path.substr(P.size())); }
  template<typename T> std::optional<T> Get() {
    auto it = sysfs.find(path);
    if (it == sysfs.end()) return std::nullopt;
    return static_cast<T>(std::stoul(it->second));
  }
};
'''

harness = r'''
// Model of the legacy (no new backend) per-frame path in amdolby_vision.c:
// load_dolby_vsvdb while inject_num < 24, then the non-VP DV-LL clamp of the
// VSVDB peak to the source max PQ while source_lum_limit_num < 24, which also
// sends HDR10 metadata while hdr10_for_dv_ll_inject_num < 24.
static unsigned num(const char* n) { return std::stoul(sysfs[P + n]); }
static void put(const char* n, unsigned v) { sysfs[P + n] = std::to_string(v); }
static unsigned vsvdb_peak = 0;
static unsigned hdr10_sent = 0;
static void frame(unsigned source_max_pq) {
  if (num("xbmc_dv_vsvdb_inject_num") < 24) {
    vsvdb_peak = std::stoul(sysfs[P + "xbmc_dv_vsvdb_payload"]);
    put("xbmc_dv_vsvdb_inject_num", num("xbmc_dv_vsvdb_inject_num") + 1);
  }
  if (num("xbmc_dv_vp") == 0 && num("xbmc_dv_vsvdb_source_lum_limit_num") < 24) {
    if (vsvdb_peak > source_max_pq) {
      vsvdb_peak = source_max_pq;
      if (num("xbmc_dv_hdr10_for_dv_ll_inject_num") < 24) {
        ++hdr10_sent;
        put("xbmc_dv_hdr10_for_dv_ll_inject_num", num("xbmc_dv_hdr10_for_dv_ll_inject_num") + 1);
      }
    }
    put("xbmc_dv_vsvdb_source_lum_limit_num", num("xbmc_dv_vsvdb_source_lum_limit_num") + 1);
  }
}
static void engage(const std::string& payload) {
  // Counter resets and payload write done by aml_dv_on.
  put("xbmc_dv_vsvdb_source_lum_limit_num", 0);
  put("xbmc_dv_hdr10_for_dv_ll_inject_num", 0);
  put("xbmc_dv_vsvdb_inject_num", 0);
  sysfs[P + "xbmc_dv_vsvdb_payload"] = payload;
}
int main() {
  const unsigned source_max_pq = 3079;
  put("xbmc_dv_vp", 0);

  // Fresh engage with a payload above the source peak: clamped.
  engage("3500");
  for (int i = 0; i < 100; ++i) frame(source_max_pq);
  assert(vsvdb_peak == source_max_pq && hdr10_sent > 0);
  const unsigned engaged_hdr10 = hdr10_sent;

  // Live change well after the 24-frame windows: same result as an engage.
  configured_payload = "3745";
  writes.clear();
  aml_dv_refresh_vsvdb_live();
  assert(!writes.empty() && writes.back() == "xbmc_dv_vsvdb_inject_num");
  for (int i = 0; i < 100; ++i) frame(source_max_pq);
  assert(vsvdb_peak == source_max_pq && hdr10_sent > engaged_hdr10);
  const unsigned live = vsvdb_peak;
  engage("3745");
  for (int i = 0; i < 100; ++i) frame(source_max_pq);
  assert(vsvdb_peak == live);

  // A payload below the source peak is loaded as is.
  configured_payload = "2900";
  aml_dv_refresh_vsvdb_live();
  for (int i = 0; i < 100; ++i) frame(source_max_pq);
  assert(vsvdb_peak == 2900);

  // VP modes and a disabled DV core are left alone.
  writes.clear();
  put("xbmc_dv_vp", 1);
  aml_dv_refresh_vsvdb_live();
  put("xbmc_dv_vp", 0);
  dv_enabled = false;
  aml_dv_refresh_vsvdb_live();
  assert(writes.empty());
  std::printf("Live VSVDB refresh matches engage after the 24-frame window\n");
}
'''

with tempfile.TemporaryDirectory(prefix='kodi-dv-vsvdb-live-') as tmp:
    src = Path(tmp) / 'test.cpp'
    binary = Path(tmp) / 'test'
    src.write_text(preamble + body + harness)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-fsanitize=address,undefined', '-o', str(binary), str(src)],
                   check=True)
    subprocess.run([str(binary)], check=True)
