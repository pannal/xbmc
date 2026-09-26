#!/usr/bin/env python3
"""Exercise a disc session's deferred DV engage without hardware or a CE build.

Compile the production CRenderManager::UpdateResolution(), the deferred-engage
functions and the CreateNewWindow() engage gate with stand-ins for the display,
settings and aml_dv_on(). The stand-in aml_dv_on() requests a resolution update
exactly when the production IPT branch does (checked against the source): unless
the engage is at a mode set. This verifies the control flow between the render
thread's resolution update and the engage, not HDMI, kernel or device timing.
Use --baseline REV to run the checks against another revision's source.
Requires Python 3 and g++; temporary compiler outputs are removed automatically.
"""

import argparse
import pathlib
import re
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
AML = "xbmc/utils/AMLUtils.cpp"
RENDER = "xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp"
WINSYS = "xbmc/windowing/amlogic/WinSystemAmlogic.cpp"


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", metavar="REV", help="test the source at REV")
    args = parser.parse_args()

    def read(path):
        if args.baseline:
            return subprocess.check_output(["git", "show", f"{args.baseline}:{path}"],
                                           cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    aml, render, winsys = read(AML), read(RENDER), read(WINSYS)

    # The stand-in aml_dv_on() below mirrors this production request.
    dv_on = function(aml, "unsigned int aml_dv_on(")
    if not re.search(r"if \(!force_hdmi && !s_dvEngagingAtModeSet\)\s*\n\s*"
                     r"aml_dv_trigger_update_resolution\(StreamHdrType::HDR_TYPE_DOLBYVISION\)",
                     dv_on):
        raise SystemExit("aml_dv_on(): IPT update request changed; update the stand-in")

    engage = function(aml, "void aml_dv_engage_deferred_disc(bool atModeSet)")
    stale = function(aml, "void aml_dv_engage_stale_deferred_disc()")
    update = function(render, "void CRenderManager::UpdateResolution(bool force)")
    create = function(winsys, "bool CWinSystemAmlogic::CreateNewWindow(")
    gate = create[create.index("  // A disc session's first DV engage"):
                  create.index("  aml_set_native_resolution(")]

    harness = r'''
#include <atomic>
#include <cstdint>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

void check(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}

// ---- logging and settings -------------------------------------------------
constexpr int LOGDEBUG = 0, LOGINFO = 1;
int g_infoLogs = 0;
struct CLog {
  template<class... T> static void Log(int level, T&&...) { if (level == LOGINFO) ++g_infoLogs; }
};
struct CStreamDetails {
  template<class T> static const char* DynamicRangeToString(T) { return ""; }
};
enum class StreamHdrType { HDR_TYPE_NONE, HDR_TYPE_HDR10, HDR_TYPE_DOLBYVISION };
enum RENDER_STEREO_MODE { RENDER_STEREO_MODE_OFF, RENDER_STEREO_MODE_UNDEFINED };
enum STEREOSCOPIC_PLAYBACK_MODE { STEREOSCOPIC_PLAYBACK_MODE_ASK = 0 };
enum { ADJUST_REFRESHRATE_OFF = 0 };
using RESOLUTION = int;
struct RESOLUTION_INFO {};
int g_refreshSwitching = 1;
struct CSettings {
  static constexpr auto SETTING_VIDEOPLAYER_STEREOSCOPICPLAYBACKMODE = "stereo";
  static constexpr auto SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE = "adjust";
  int GetInt(const std::string& id) const { return id == "adjust" ? g_refreshSwitching : 1; }
};
struct CSettingsComponent {
  CSettings s;
  CSettings* GetSettings() { return &s; }
};
struct CStereoscopicsManager {
  RENDER_STEREO_MODE GetStereoModeByUser() { return RENDER_STEREO_MODE_OFF; }
};
struct CGUI {
  CStereoscopicsManager m;
  CStereoscopicsManager& GetStereoscopicsManager() { return m; }
};
struct CResolutionUtils {
  static RESOLUTION ChooseBestResolution(float, int, int, bool) { return 0; }
};

// ---- the display and the DV core --------------------------------------------
bool g_modeChanging = false;  // what aml_display_mode_changing() reports
bool g_forceSwitch = false;   // CWinSystemAmlogic::m_force_mode_switch
bool g_fracPolicy = true;     // aml_has_frac_rate_policy()
bool aml_display_mode_changing(const RESOLUTION_INFO&) { return g_modeChanging; }
bool aml_has_frac_rate_policy() { return g_fracPolicy; }

static bool s_dvDiscDeferred = false;
static unsigned int s_dvDiscDeferredMode = 0;
static bool s_dvEngagingAtModeSet = false;
static bool s_dvPlaybackActive = false;
static std::atomic<int64_t> s_dvDiscDeferredSinceMs{0};
int64_t g_nowMs = 100000;
int64_t aml_steady_ms() { return g_nowMs; }
bool g_dvEnabled = true;
bool aml_is_dv_enable() { return g_dvEnabled; }
const char* aml_dv_output_mode_to_string(unsigned int) { return "IPT"; }
void aml_dv_dump_state(const char*) {}
struct CDVCoreGuard {
  static std::recursive_mutex& m() { static std::recursive_mutex x; return x; }
  explicit CDVCoreGuard(const char*) { m().lock(); }
  ~CDVCoreGuard() { m().unlock(); }
};

struct Engage { bool atModeSet; };
std::vector<Engage> g_engages;
void aml_dv_trigger_update_resolution(StreamHdrType);
// Stand-in for the production IPT branch (asserted by the script): a DV engage
// requests a follow-up resolution update unless the mode set is about to happen.
unsigned int aml_dv_on(unsigned int mode, bool force_hdmi = false) {
  g_engages.push_back({s_dvEngagingAtModeSet});
  if (!force_hdmi && !s_dvEngagingAtModeSet)
    aml_dv_trigger_update_resolution(StreamHdrType::HDR_TYPE_DOLBYVISION);
  return mode;
}

@ENGAGE@

@STALE@

// ---- CWinSystemAmlogic::CreateNewWindow's engage gate ------------------------
int g_modeSets = 0;
void CreateNewWindow(const RESOLUTION_INFO& res) {
  const bool m_force_mode_switch = g_forceSwitch;
@GATE@
  if (g_modeChanging || (m_force_mode_switch && g_fracPolicy)) ++g_modeSets;
}

// ---- GfxContext / ServiceBroker ---------------------------------------------
struct CGfxContext {
  bool IsFullScreenVideo() { return true; }
  bool IsFullScreenRoot() { return true; }
  void SetHDRType(StreamHdrType) {}
  void SetVideoResolution(RESOLUTION, bool) { CreateNewWindow(RESOLUTION_INFO{}); }
};
struct CWinSystem {
  CGfxContext g;
  CGfxContext& GetGfxContext() { return g; }
};
struct CServiceBroker {
  static CWinSystem* GetWinSystem() { static CWinSystem w; return &w; }
  static CSettingsComponent* GetSettingsComponent() { static CSettingsComponent s; return &s; }
  static CGUI* GetGUI() { static CGUI g; return &g; }
};

using CCriticalSection = std::recursive_mutex;
struct Picture {
  std::string stereoMode;
  int iWidth = 3840, iHeight = 2160;
  StreamHdrType hdrType = StreamHdrType::HDR_TYPE_DOLBYVISION;
};
struct PlayerPort { void VideoParamsChange() {} };
struct Renderer { void Update() {} };
class CRenderManager {
public:
  CCriticalSection m_resolutionlock;
  bool m_bTriggerUpdateResolution = false;
  StreamHdrType m_hdrType_override = StreamHdrType::HDR_TYPE_NONE;
  float m_fps = 23.976f;
  Picture m_picture;
  Renderer renderer;
  Renderer* m_pRenderer = &renderer;
  PlayerPort port;
  PlayerPort* m_playerPort = &port;
  void UpdateLatencyTweak() {}
  void UpdateResolution(bool force = false);
};
CRenderManager g_render;
void aml_dv_trigger_update_resolution(StreamHdrType hdrType) {
  g_render.m_bTriggerUpdateResolution = true;
  g_render.m_hdrType_override = hdrType;
}

@UPDATE@

// A disc session's first segment: decoder open deferred the engage.
void deferred_open() {
  g_engages.clear();
  g_modeSets = 0;
  s_dvPlaybackActive = true;
  s_dvDiscDeferred = true;
  s_dvDiscDeferredSinceMs = g_nowMs;
  g_render.m_bTriggerUpdateResolution = true;  // the codec's first configure
  g_render.m_hdrType_override = StreamHdrType::HDR_TYPE_NONE;
}

void test_mode_set() {
  g_modeChanging = true; g_forceSwitch = false; g_refreshSwitching = 1;
  deferred_open();
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && g_engages[0].atModeSet, "mode set: engage not at the mode set");
  check(g_modeSets == 1, "mode set: no mode set");
  check(!s_dvDiscDeferred && !g_render.m_bTriggerUpdateResolution,
        "mode set: deferral or trigger left behind");
}

void test_window_without_mode_switch() {
  // Reviewed case: CreateNewWindow runs but the display mode is not changing.
  g_modeChanging = false; g_forceSwitch = false; g_refreshSwitching = 1;
  deferred_open();
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && !g_engages[0].atModeSet,
        "window without mode switch: engage missing or claimed a mode set");
  check(g_render.m_bTriggerUpdateResolution &&
            g_render.m_hdrType_override == StreamHdrType::HDR_TYPE_DOLBYVISION,
        "window without mode switch: aml_dv_on()'s resolution update was discarded");
  check(!s_dvDiscDeferred, "window without mode switch: deferral left pending");
  // The kept request runs on the next frame; nothing is engaged twice.
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && !g_render.m_bTriggerUpdateResolution,
        "window without mode switch: follow-up update engaged again or looped");
}

void test_forced_switch() {
  g_modeChanging = false; g_forceSwitch = true; g_fracPolicy = true; g_refreshSwitching = 1;
  deferred_open();
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && g_engages[0].atModeSet && g_modeSets == 1,
        "forced frac-rate switch: engage not at the mode set");
  g_fracPolicy = false;  // no frac-rate policy: a force writes nothing
  deferred_open();
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && !g_engages[0].atModeSet && g_render.m_bTriggerUpdateResolution,
        "force without frac-rate policy: treated as a mode set");
  g_render.UpdateResolution();
  g_forceSwitch = false; g_fracPolicy = true;
}

void test_refresh_switching_off() {
  g_modeChanging = true; g_refreshSwitching = 0;  // no SetVideoResolution at all
  deferred_open();
  g_render.UpdateResolution();
  check(g_engages.size() == 1 && !g_engages[0].atModeSet && g_modeSets == 0,
        "refresh switching off: engage missing");
  check(g_render.m_bTriggerUpdateResolution,
        "refresh switching off: aml_dv_on()'s resolution update was discarded");
  g_render.UpdateResolution();
  g_refreshSwitching = 1;
}

void test_no_decoder_waits_quietly() {
  // A short first segment closed under the hold: no decoder, deferral pending.
  deferred_open();
  s_dvPlaybackActive = false;
  g_infoLogs = 0;
  g_nowMs += 3500;
  for (int frame = 0; frame < 100; ++frame)
    aml_dv_engage_stale_deferred_disc();
  check(g_engages.empty() && s_dvDiscDeferred, "no decoder: engaged or lost the deferral");
  check(g_infoLogs <= 1, "no decoder: last-resort check logs every frame");
  // The next segment's decoder open re-arms it; with no mode set it engages at 3 s.
  s_dvPlaybackActive = true;
  s_dvDiscDeferredSinceMs = g_nowMs;
  aml_dv_engage_stale_deferred_disc();
  check(g_engages.empty(), "re-armed: engaged before 3 s");
  g_nowMs += 3100;
  aml_dv_engage_stale_deferred_disc();
  check(g_engages.size() == 1 && !s_dvDiscDeferred, "re-armed: last resort did not engage");
  g_render.UpdateResolution();
}

int main() {
  try {
    test_mode_set();
    test_window_without_mode_switch();
    test_forced_switch();
    test_refresh_switching_off();
    test_no_decoder_waits_quietly();
    std::cout << "PASS: 5 disc DV deferred-engage scenarios\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
'''
    for key, value in [("ENGAGE", engage), ("STALE", stale), ("GATE", gate),
                       ("UPDATE", update)]:
        harness = harness.replace(f"@{key}@", value)
    with tempfile.TemporaryDirectory(prefix="dv-disc-hold-") as temp:
        cpp = pathlib.Path(temp) / "test.cpp"
        exe = pathlib.Path(temp) / "test"
        cpp.write_text(harness)
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-Wno-unused-variable",
                        "-fsanitize=address,undefined", "-g",
                        str(cpp), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], timeout=15)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
