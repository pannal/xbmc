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
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()

    def read(path):
        if args.baseline:
            return subprocess.check_output(["git", "show", f"{args.baseline}:{path}"],
                                           cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    aml, render, winsys = read(AML), read(RENDER), read(WINSYS)
    render_header = read("xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.h")
    applied_resolution = (function(render_header, "struct AppliedResolution") + ";"
                          if "struct AppliedResolution" in render_header else "")

    # The stand-in aml_dv_on() below mirrors this production request.
    dv_on = function(aml, "unsigned int aml_dv_on(")
    if not re.search(r"if \(!force_hdmi && !s_dvEngagingAtModeSet\)\s*\n\s*"
                     r"aml_dv_trigger_update_resolution\(StreamHdrType::HDR_TYPE_DOLBYVISION\)",
                     dv_on):
        raise SystemExit("aml_dv_on(): IPT update request changed; update the stand-in")

    engage = function(aml, "void aml_dv_engage_deferred_disc(")
    stale = function(aml, "void aml_dv_engage_stale_deferred_disc(")
    pending = function(aml, "void aml_dv_cancel_deferred_session(") + function(aml, "bool aml_dv_deferred_disc_pending(")
    continuation = function(render, "void CRenderManager::CancelDeferredDV()") + function(render, "bool CRenderManager::ContinueDeferredDV(")
    # Picture propagation must use the original decoder identity, never a global lookup.
    codec = read("xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp")
    picture = read("xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodec.cpp")
    assert 'm_dvSession = std::make_shared<const unsigned char>(0);' in codec
    assert re.search(r'aml_dv_open\([^;\n]+false, m_dvSession(?:, m_originalSourceHdrType)?\);', codec)
    if 'm_originalSourceHdrType' in codec:
        assert 'false, m_dvSession, m_originalSourceHdrType);' in codec
    assert 'aml_dv_cancel_deferred_session(m_dvSession);' in function(codec, 'bool CAMLCodec::BeginLifecycle(')
    getpicture = function(codec, "CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(")
    assert getpicture.index('if (!operation)') < getpicture.index('videoPicture.amlDVSession = m_dvSession;')
    assert 'amlDVSession.reset();' in function(picture, "void VideoPicture::Reset()")
    assert 'amlDVSession != pic.amlDVSession' in function(picture, "bool VideoPicture::IsSameParams(")
    assert '*this = pic;' in function(picture, "VideoPicture& VideoPicture::SetParams(")
    assert 'std::atomic_store(&s_dvPlaybackSession, std::move(session));' in function(aml, 'void aml_dv_open(')
    assert 'std::atomic_store(&s_dvPlaybackSession, std::shared_ptr<const void>{});' in function(aml, 'void aml_dv_close()')
    for signature in ['bool CRenderManager::Configure(const VideoPicture&', 'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestUnInit()', 'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestFlush(']:
        assert 'CancelDeferredDV();' in function(render, signature)
    frame = function(render, "void CRenderManager::FrameMove()")
    frame = frame[frame.index("  m_deferredDVResolutionAttempted = false;"):frame.index("\n  {", frame.index("  m_deferredDVResolutionAttempted = false;"))]
    update = function(render, "void CRenderManager::UpdateResolution(bool force)")
    create = function(winsys, "bool CWinSystemAmlogic::CreateNativeWindow(")
    gate = create[create.index("  // A disc session's first DV engage"):
                  create.index("  aml_set_native_resolution(")]

    harness = r'''
#include "windowing/amlogic/AMLNativeTransaction.h"
#include <atomic>
#include <optional>
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
bool g_windowReady = true;
bool g_modeChanging = false;  // what aml_display_mode_changing() reports
bool g_forceSwitch = false;   // CWinSystemAmlogic::m_force_mode_switch
bool g_fracPolicy = true;     // aml_has_frac_rate_policy()
bool aml_display_mode_changing(const RESOLUTION_INFO&) { return g_modeChanging; }
bool aml_has_frac_rate_policy() { return g_fracPolicy; }

static bool s_dvDiscDeferred = false;
static std::shared_ptr<const void> s_dvPlaybackSession;
CAMLSession nativeSession;
bool throwEngage=false;
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
  if (!s_dvEngagingAtModeSet) check(!nativeSession.AcquireDecoder(), "engage bypassed native admission");
  g_engages.push_back({s_dvEngagingAtModeSet});
  if (throwEngage) throw std::runtime_error("native failure");
  if (!force_hdmi && !s_dvEngagingAtModeSet)
    aml_dv_trigger_update_resolution(StreamHdrType::HDR_TYPE_DOLBYVISION);
  return mode;
}

void aml_dv_engage_deferred_disc(bool, const std::shared_ptr<const void>& session = {});
@PENDING@
@ENGAGE@

@STALE@

// ---- CWinSystemAmlogic::CreateNewWindow's engage gate ------------------------
int g_modeSets = 0;
uint64_t g_displayGeneration = 1;
void CreateNewWindow(const RESOLUTION_INFO& res) {
  const bool m_force_mode_switch = g_forceSwitch;
@GATE@
  if (g_modeChanging || (m_force_mode_switch && g_fracPolicy)) ++g_modeSets;
}

// ---- GfxContext / ServiceBroker ---------------------------------------------
bool g_fullscreen=true;
struct CGfxContext {
  bool IsFullScreenVideo() { return g_fullscreen; }
  bool IsFullScreenRoot() { return true; }
  void SetHDRType(StreamHdrType) {}
  int GetStereoMode() const { return 0; }
  bool SetVideoResolution(RESOLUTION, bool, bool = true) { if (!g_windowReady) return false; CreateNewWindow(RESOLUTION_INFO{}); ++g_displayGeneration; return true; }
};
struct CWinSystem {
  bool IsDisplayReadyForVideo() const { return g_windowReady; }
  uint64_t GetDisplayGeneration() const { return g_displayGeneration; }
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
  unsigned int iWidth = 3840, iHeight = 2160;
  StreamHdrType hdrType = StreamHdrType::HDR_TYPE_DOLBYVISION;
  std::shared_ptr<const void> amlDVSession;
};
struct PlayerPort { void VideoParamsChange() {} };
struct Renderer { void Update() {} };
class CRenderManager {
public:
  CCriticalSection m_resolutionlock;
@APPLIEDRESOLUTION@
@APPLIEDSTATE@
  bool m_closing=false;
  enum {STATE_CONFIGURED};int m_renderState=STATE_CONFIGURED;
  std::shared_ptr<const void> m_deferredDVSession;
  std::unique_ptr<CAMLNativeTransaction> m_deferredDVNative;
  bool m_deferredDVResolutionAttempted{false};
  void CancelDeferredDV();bool ContinueDeferredDV(bool);
  void FrameDeferredDV() {
@FRAME@
  }
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

@CONTINUATION@
@UPDATE@

// A disc session's first segment: decoder open deferred the engage.
void deferred_open() {
  g_render.CancelDeferredDV();
  s_dvPlaybackSession=std::make_shared<const int>(0);
  g_render.m_picture.amlDVSession=s_dvPlaybackSession;
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

void test_pending_window() {
  g_modeChanging = true; g_forceSwitch = false; g_refreshSwitching = 1;
  deferred_open(); g_windowReady = false;
  g_render.m_hdrType_override = StreamHdrType::HDR_TYPE_HDR10;
  g_render.UpdateResolution();
  check(g_engages.empty() && g_modeSets == 0 && s_dvDiscDeferred,
        "pending admission: native engage must wait for the window");
  check(g_render.m_bTriggerUpdateResolution &&
        g_render.m_hdrType_override == StreamHdrType::HDR_TYPE_HDR10,
        "pending admission: resolution/HDR intent lost");
  g_windowReady = true; g_render.UpdateResolution();
  check(g_engages.size() == 1 && g_engages[0].atModeSet && g_modeSets == 1,
        "resumed window: engage must still precede native mode set");
  check(!g_render.m_bTriggerUpdateResolution && !s_dvDiscDeferred,
        "resumed window: intent did not complete");
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
  s_dvPlaybackSession.reset(); // actual close invalidates the original renderer identity
  g_infoLogs = 0;
  g_nowMs += 3500;
  for (int frame = 0; frame < 100; ++frame)
    g_render.ContinueDeferredDV(true);
  check(g_engages.empty() && s_dvDiscDeferred, "no decoder: engaged or lost the deferral");
  check(g_infoLogs <= 1, "no decoder: last-resort check logs every frame");
  // The next segment's decoder open re-arms it; with no mode set it engages at 3 s.
  s_dvPlaybackActive = true;
  s_dvPlaybackSession=std::make_shared<const int>(0);
  g_render.m_picture.amlDVSession=s_dvPlaybackSession;
  s_dvDiscDeferredSinceMs = g_nowMs;
  g_render.ContinueDeferredDV(true);
  check(g_engages.empty(), "re-armed: engaged before 3 s");
  g_nowMs += 3100;
  g_render.ContinueDeferredDV(true);
  check(g_engages.size() == 1 && !s_dvDiscDeferred, "re-armed: last resort did not engage");
  g_render.UpdateResolution();
}

void test_background_fallback() {
  g_fullscreen=false;g_refreshSwitching=0;deferred_open();
  g_render.FrameDeferredDV();
  check(g_engages.empty(),"background engaged before timer");
  g_nowMs+=3100;
  auto permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());
  g_render.FrameDeferredDV();
  check(bool(g_render.m_deferredDVNative),"background fallback suppressed by resolution trigger");
  auto* retained=g_render.m_deferredDVNative.get();g_render.FrameDeferredDV();
  check(retained==g_render.m_deferredDVNative.get(),"background pending identity replaced");
  permit.reset();g_render.FrameDeferredDV();
  check(g_engages.size()==1&&!s_dvDiscDeferred,"background retry did not engage");
  g_fullscreen=true;deferred_open();
  permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());
  g_render.FrameDeferredDV();retained=g_render.m_deferredDVNative.get();
  check(retained!=nullptr,"normal pending attempt cancelled by stale poll");
  g_render.FrameDeferredDV();check(retained==g_render.m_deferredDVNative.get(),"normal pending attempt replaced");
  // Leaving fullscreen must not strand an already pending normal attempt.
  g_fullscreen=false;g_render.FrameDeferredDV();
  check(!g_render.m_deferredDVNative,"background retained pre-timer normal fence");
  g_nowMs+=3100;permit.reset();g_render.FrameDeferredDV();
  check(g_engages.size()==1,"fullscreen transition stranded fallback");
  g_fullscreen=true;g_refreshSwitching=1;
}

void test_pending_identity_and_cleanup() {
  g_refreshSwitching=0;g_modeChanging=false;deferred_open();
  auto original=g_render.m_picture.amlDVSession;
  auto permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());
  check(bool(*permit),"decoder permit");
  g_render.UpdateResolution();
  check(g_engages.empty()&&g_render.m_bTriggerUpdateResolution&&bool(g_render.m_deferredDVNative),"pending effect/trigger lost");
  auto* retained=g_render.m_deferredDVNative.get();g_render.UpdateResolution();
  check(g_render.m_deferredDVNative.get()==retained,"pending transaction replaced");
  s_dvPlaybackSession=std::make_shared<const int>(0); // replacement opens before old renderer resumes
  g_render.UpdateResolution();
  check(!g_render.m_deferredDVNative&&bool(nativeSession.AcquireDecoder()),"obsolete pending fence not cancelled");
  aml_dv_cancel_deferred_session(original);
  check(bool(s_dvPlaybackSession),"old close cancelled replacement identity");
  permit.reset();g_render.UpdateResolution();
  check(g_engages.empty()&&s_dvDiscDeferred&&!g_render.m_deferredDVNative,"old renderer consumed replacement");
  // Native utility also rejects old identity after admission (read/effect boundary).
  {CAMLNativeTransaction gate;check(gate.TryBegin(),"gate");aml_dv_engage_deferred_disc(false,original);}
  check(g_engages.empty()&&s_dvDiscDeferred,"effect recheck missing");
  g_render.m_picture.amlDVSession=s_dvPlaybackSession;g_render.m_bTriggerUpdateResolution=true;
  g_render.UpdateResolution();check(g_engages.size()==1,"new session did not engage");
  deferred_open();permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());
  g_render.UpdateResolution();g_render.m_closing=true;g_render.ContinueDeferredDV(false);
  check(!g_render.m_deferredDVNative&&bool(nativeSession.AcquireDecoder()),"close failed to cancel fence");
  permit.reset();g_render.m_closing=false;
  deferred_open();original=g_render.m_picture.amlDVSession;
  permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());g_render.UpdateResolution();
  aml_dv_cancel_deferred_session(original);g_render.UpdateResolution();
  check(!g_render.m_deferredDVNative&&g_engages.empty()&&bool(nativeSession.AcquireDecoder()),"requested close did not cancel pending work");
  permit.reset();
  deferred_open();throwEngage=true;
  try{g_render.UpdateResolution();check(false,"expected native failure");}catch(const std::runtime_error&){}
  throwEngage=false;
  check(!g_render.m_deferredDVNative&&!s_dvEngagingAtModeSet&&bool(nativeSession.AcquireDecoder()),"exception leaked admission");
  auto count=g_engages.size();g_render.UpdateResolution();check(g_engages.size()==count,"exception replayed engage");
  deferred_open();permit=std::make_unique<CAMLSession::Permit>(nativeSession.AcquireDecoder());
  g_render.UpdateResolution();g_windowReady=false;g_render.UpdateResolution();
  check(!g_render.m_deferredDVNative&&g_render.m_bTriggerUpdateResolution,"display wait retained native fence/lost intent");
  g_windowReady=true;permit.reset();g_render.UpdateResolution();
  check(g_engages.size()==1,"display recovery did not retry");
}
int main() {
  try {
    auto display=CAMLSession::FenceDisplay();check(CAMLSession::TryBeginDisplay(display),"display");
    check(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY),"ready");
    auto stream=nativeSession.Fence();check(nativeSession.BeginMutation(stream)&&nativeSession.Complete(stream,true),"session");
    test_mode_set();
    test_pending_window();
    test_window_without_mode_switch();
    test_forced_switch();
    test_refresh_switching_off();
    test_no_decoder_waits_quietly();
    test_pending_identity_and_cleanup();
    test_background_fallback();
    std::cout << "PASS: 6 disc DV policy scenarios plus background fallback and original-session admission/cleanup\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
'''
    for key, value in [("ENGAGE", engage), ("PENDING", pending), ("CONTINUATION", continuation), ("STALE", stale), ("GATE", gate),
                       ("UPDATE", update), ("FRAME", frame),
                       ("APPLIEDRESOLUTION", applied_resolution),
                       ("APPLIEDSTATE", "std::optional<AppliedResolution> m_appliedResolution;"
                        if applied_resolution else "")]:
        harness = harness.replace(f"@{key}@", value)
    def run(code, negative=False):
        with tempfile.TemporaryDirectory(prefix="dv-disc-hold-") as temp:
            cpp = pathlib.Path(temp) / "test.cpp"
            exe = pathlib.Path(temp) / "test"
            cpp.write_text(code)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", "-Wno-unused-variable", "-pthread",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie", "-g",
                            "-I", str(ROOT/'xbmc'), str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=15)
            if negative:
                assert result.returncode and ('FAIL:' in result.stderr or 'Assertion' in result.stderr), result.stdout+result.stderr
            else:
                assert result.returncode==0,result.stdout+result.stderr
                print(result.stdout.strip())
    run(harness)
    if args.negative_controls:
        for label,old,new in [
            ('bypass admission','if (!m_deferredDVNative->TryBegin())','if (false && !m_deferredDVNative->TryBegin())'),
            ('ignore snapshot identity','if (!session || session != std::atomic_load(&s_dvPlaybackSession))','if (false)'),
            ('ignore effect identity','if (!atModeSet && (!session || session != std::atomic_load(&s_dvPlaybackSession)))','if (false)'),
            ('retain fence on cancellation','m_deferredDVNative.reset();','/* leaked pending fence */'),
            ('clear follow-up after engage','aml_dv_engage_deferred_disc(false, session);\n  }','aml_dv_engage_deferred_disc(false, session);\n    m_bTriggerUpdateResolution = false;\n  }'),
            ('ignore stale timer','aml_steady_ms() - since >= 3000','true'),
            ('suppress background fallback','else if (!m_deferredDVResolutionAttempted || !m_deferredDVNative)','else if (!m_bTriggerUpdateResolution)'),
        ]:
            assert old in harness,label
            run(harness.replace(old,new),True);print('REJECTED:',label)



if __name__ == "__main__":
    main()
