#!/usr/bin/env python3
"""Exercise native startup and disc deferred DV output without hardware.

Compile production aml_dv_open/close, CRenderManager::UpdateResolution(), deferred-engage
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
    parser.add_argument("--baseline-open", metavar="REV", help="substitute only the old production open policy")
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

    transition = function(aml, "struct AMLDVOutputTransition") + ";\n"
    transition += "static std::shared_ptr<AMLDVOutputTransition> s_dvOutputTransition;\n"
    output = function(aml, "bool aml_dv_output_transition_pending(") + function(aml, "void aml_dv_complete_output_transition(")
    opening = function(aml, "void aml_dv_open(")
    if args.baseline_open:
        old = subprocess.check_output(["git", "show", f"{args.baseline_open}:{AML}"], cwd=ROOT, text=True)
        opening = function(old, "void aml_dv_open(").replace(
            "StreamHdrType originalSourceHdrType)", "StreamHdrType originalSourceHdrType, bool startupBlackout)")
    closing = function(aml, "void aml_dv_close()") + function(aml, "void aml_dv_set_disc_hold(")
    engage = function(aml, "void aml_dv_engage_deferred_disc(")
    stale = function(aml, "void aml_dv_engage_stale_deferred_disc(")
    pending = function(aml, "void aml_dv_cancel_deferred_session(") + function(aml, "bool aml_dv_deferred_disc_pending(")
    continuation = function(render, "void CRenderManager::CancelDeferredDV()") + function(render, "bool CRenderManager::ContinueDeferredDV(")
    # Picture propagation must use the original decoder identity, never a global lookup.
    codec = read("xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp")
    picture = read("xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodec.cpp")
    assert 'm_dvSession = std::make_shared<const unsigned char>(0);' in codec
    assert re.search(r'aml_dv_open\([^;]+false, m_dvSession,\s*m_originalSourceHdrType, startupBlackout\);', codec)
    assert 'startupBlackout = m_videoHoldActive;' in function(codec, 'bool CAMLCodec::OpenDecoderInternal(')
    assert 'm_videoHoldDVWaiting = m_videoHoldActive && aml_dv_output_transition_pending(m_dvSession);' in function(codec, 'bool CAMLCodec::OpenDecoderInternal(')
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
enum AVColorPrimaries { AVCOL_PRI_BT709, AVCOL_PRI_BT2020 };
enum DV_MODE {DV_MODE_ON, DV_MODE_ON_DEMAND, DV_MODE_OFF};
constexpr unsigned DOLBY_VISION_OUTPUT_MODE_IPT=0, DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL=1,
 DOLBY_VISION_OUTPUT_MODE_HDR10=2, DOLBY_VISION_OUTPUT_MODE_SDR10=3, DOLBY_VISION_OUTPUT_MODE_BYPASS=5;
constexpr int DOLBY_VISION_FORCE_OUTPUT_MODE=2, WINDOW_HOME=0;
enum RENDER_STEREO_MODE { RENDER_STEREO_MODE_OFF, RENDER_STEREO_MODE_UNDEFINED };
enum STEREOSCOPIC_PLAYBACK_MODE { STEREOSCOPIC_PLAYBACK_MODE_ASK = 0 };
enum { ADJUST_REFRESHRATE_OFF = 0 };
using RESOLUTION = int;
struct RESOLUTION_INFO {};
int g_refreshSwitching = 1;
struct CSettings {
  static constexpr auto SETTING_VIDEOPLAYER_STEREOSCOPICPLAYBACKMODE = "stereo";
  static constexpr auto SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE = "adjust";
  static constexpr auto SETTING_COREELEC_AMLOGIC_DV_KEEP_SDR_STATE = "keepSDR";
  bool GetBool(const std::string&) const { return false; }
  int GetInt(const std::string& id) const { return id == "adjust" ? g_refreshSwitching : 1; }
};
struct CSettingsComponent {
  CSettings s;
  CSettings* GetSettings() { return &s; }
};
struct CStereoscopicsManager {
  RENDER_STEREO_MODE GetStereoModeByUser() { return RENDER_STEREO_MODE_OFF; }
};
struct CGUIWindow {
 struct Property {std::string asString(){return {};}};
 Property GetProperty(const char*){return {};}
};
struct CGUI {
  CGUI& GetWindowManager(){return *this;}
  CGUIWindow* GetWindow(int){return nullptr;}
  CStereoscopicsManager m;
  CStereoscopicsManager& GetStereoscopicsManager() { return m; }
};
struct CResolutionUtils {
  static RESOLUTION ChooseBestResolution(float, int, int, bool) { return 0; }
};

// ---- the display and the DV core --------------------------------------------
bool g_windowReady = true;
bool g_delayReset = false;
bool g_modeChanging = false;  // what aml_display_mode_changing() reports
bool g_forceSwitch = false;   // CWinSystemAmlogic::m_force_mode_switch
bool g_fracPolicy = true;     // aml_has_frac_rate_policy()
bool aml_display_mode_changing(const RESOLUTION_INFO&) { return g_modeChanging; }
bool aml_has_frac_rate_policy() { return g_fracPolicy; }

@TRANSITION@
static bool s_dvDiscHold = false;
static std::atomic<bool> s_pm4kActive{false};
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
DV_MODE g_dvMode=DV_MODE_ON_DEMAND;
DV_MODE aml_dv_mode(){return g_dvMode;}
const char* aml_dv_mode_to_string(DV_MODE){return "";}
int aml_dv_type(){return 0;}
const char* aml_dv_type_to_string(int){return "";}
unsigned g_mapping=DOLBY_VISION_OUTPUT_MODE_IPT;
unsigned aml_vs10_by_hdrtype(StreamHdrType,unsigned){return g_mapping;}
int g_offs=0;
void aml_dv_off(){g_dvEnabled=false;++g_offs;}
void aml_dv_auto_letterbox_watch_stop(){}
void aml_dv_detect_active_area_stop(){}
unsigned aml_dv_dolby_vision_mode(){return g_mapping;}
struct CSysfsPath {
 std::string path;explicit CSysfsPath(const char* p):path(p){}
 template<class T> CSysfsPath(const char* p,T v):path(p){Set(v);}
 bool Exists(){return true;}
 void Set(const char* v){if(path.find("dolby_vision_enable")!=std::string::npos)g_dvEnabled=std::string(v)=="Y";}
 template<class T>void Set(T){}
};
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
@OUTPUT@
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
  if (g_delayReset) g_windowReady = false;
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

CSettings* settings(){return CServiceBroker::GetSettingsComponent()->GetSettings();}
@OPENING@
@CLOSING@
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
  g_dvMode=DV_MODE_ON_DEMAND;g_dvEnabled=false;s_dvDiscHold=true;g_mapping=DOLBY_VISION_OUTPUT_MODE_IPT;
  {CAMLNativeTransaction native;check(native.TryBegin(),"open admission");
   aml_dv_open(StreamHdrType::HDR_TYPE_DOLBYVISION,10,AVCOL_PRI_BT709,false,s_dvPlaybackSession,
               StreamHdrType::HDR_TYPE_DOLBYVISION,true);}
  check(g_dvEnabled && s_dvDiscDeferred && aml_dv_output_transition_pending(s_dvPlaybackSession),
        "decoder core enabled, output deferred under blackout");
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

void test_native_startup_policy() {
  struct Case {bool blackout;StreamHdrType hdr,original;unsigned mapping;DV_MODE mode;
               bool enabled,sw,disc,owner,defer;};
  const auto dv=StreamHdrType::HDR_TYPE_DOLBYVISION, hdr=StreamHdrType::HDR_TYPE_HDR10;
  const Case cases[]={
    {true,dv,dv,0,DV_MODE_ON_DEMAND,false,false,false,true,true},
    {true,dv,dv,1,DV_MODE_ON_DEMAND,false,false,false,true,true},
    {false,dv,dv,0,DV_MODE_ON_DEMAND,false,false,false,true,false},
    {true,dv,hdr,0,DV_MODE_ON_DEMAND,false,false,false,true,false},
    {true,hdr,hdr,0,DV_MODE_ON_DEMAND,false,false,false,true,false},
    {true,dv,dv,2,DV_MODE_ON_DEMAND,false,false,false,true,false},
    {true,dv,dv,5,DV_MODE_ON_DEMAND,false,false,false,true,false},
    {true,dv,dv,0,DV_MODE_ON,false,false,false,true,false},
    {true,dv,dv,0,DV_MODE_OFF,false,false,false,true,false},
    {true,dv,dv,0,DV_MODE_ON_DEMAND,true,false,false,true,false},
    {true,dv,dv,0,DV_MODE_ON_DEMAND,false,true,false,true,false},
    {true,dv,dv,0,DV_MODE_ON_DEMAND,false,false,false,false,false},
    {false,hdr,hdr,2,DV_MODE_ON_DEMAND,false,false,true,true,true},
  };
  for(const auto& c:cases){
    g_render.CancelDeferredDV();g_dvMode=c.mode;g_dvEnabled=c.enabled;s_dvDiscHold=c.disc;
    s_dvDiscDeferred=false;g_mapping=c.mapping;g_engages.clear();
    auto owner=c.owner?std::shared_ptr<const void>(std::make_shared<const int>(0)):nullptr;
    {CAMLNativeTransaction native;check(native.TryBegin(),"open policy admission");
     aml_dv_open(c.hdr,10,AVCOL_PRI_BT709,c.sw,owner,c.original,c.blackout);}
    check(aml_dv_output_transition_pending(owner)==(c.defer&&c.owner),"native startup eligibility");
    check(s_dvDiscDeferred==c.defer,"native output deferral policy");
    if(c.defer){
      check(g_dvEnabled && g_engages.empty(),"deferred open enables core without output");
      aml_dv_complete_output_transition(owner);
      check(aml_dv_output_transition_pending(owner),"unprogrammed output cannot complete");
    }
  }
  // Actual ordinary close cancels both pending output and core ownership.
  g_dvMode=DV_MODE_ON_DEMAND;s_dvDiscHold=false;g_mapping=0;g_dvEnabled=false;
  auto owner=std::shared_ptr<const void>(std::make_shared<const int>(0));
  {CAMLNativeTransaction native;check(native.TryBegin(),"close policy admission");
   aml_dv_open(dv,10,AVCOL_PRI_BT709,false,owner,dv,true);
   check(aml_dv_output_transition_pending(owner),"ordinary startup armed");
   aml_dv_set_disc_hold(false);
   check(aml_dv_output_transition_pending(owner)&&s_dvDiscDeferred,
         "inactive disc cleanup cannot cancel ordinary startup");
   aml_dv_close();}
  check(!aml_dv_output_transition_pending(owner)&&!g_dvEnabled&&!s_dvDiscDeferred,
        "ordinary close cancels pending output and disables DV");
  // Disc close still retains the core and deferred engage for the next segment,
  // but retires the old decoder's blackout completion identity.
  {CAMLNativeTransaction native;check(native.TryBegin(),"disc close admission");
   aml_dv_set_disc_hold(true);
   aml_dv_open(dv,10,AVCOL_PRI_BT709,false,owner,dv,true);aml_dv_close();
   check(g_dvEnabled&&s_dvDiscDeferred&&!aml_dv_output_transition_pending(owner),"disc close retains core only");
   auto next=std::shared_ptr<const void>(std::make_shared<const int>(1));
   aml_dv_open(dv,10,AVCOL_PRI_BT709,false,next,dv,true);
   aml_dv_complete_output_transition(owner);aml_dv_cancel_deferred_session(owner);
   check(aml_dv_output_transition_pending(next)&&s_dvDiscDeferred,"next disc segment owns transition");
   aml_dv_close();aml_dv_set_disc_hold(false);
   check(!g_dvEnabled&&!s_dvDiscDeferred,"disc exit releases held core");}
}
void test_output_completion() {
  g_modeChanging=true;g_forceSwitch=false;g_refreshSwitching=1;g_windowReady=true;
  deferred_open();auto owner=s_dvPlaybackSession;
  g_delayReset=true;g_render.UpdateResolution();
  check(g_engages.size()==1&&aml_dv_output_transition_pending(owner),"programmed output waits for display reset");
  g_render.FrameDeferredDV();
  check(aml_dv_output_transition_pending(owner),"stale poll cannot bypass delayed reset");
  g_delayReset=false;g_windowReady=true;g_render.UpdateResolution();
  check(!aml_dv_output_transition_pending(owner),"display ready completes output");
  // Same output mode must retain the follow-up requested by DV, even though
  // programming already succeeded. Only the next successful update completes.
  g_modeChanging=false;deferred_open();owner=s_dvPlaybackSession;
  g_render.UpdateResolution();
  check(aml_dv_output_transition_pending(owner),"same mode waits for follow-up update");
  g_render.ContinueDeferredDV(true);
  check(aml_dv_output_transition_pending(owner),"idle stale poll cannot complete follow-up");
  g_fullscreen=false;g_render.ContinueDeferredDV(true);
  check(aml_dv_output_transition_pending(owner),"windowed idle stale poll cannot complete follow-up");
  g_fullscreen=true;
  g_render.FrameDeferredDV();
  check(!aml_dv_output_transition_pending(owner),"same mode follow-up completes output");
  // No fullscreen update: bounded fallback must complete at the current mode.
  g_fullscreen=false;deferred_open();owner=s_dvPlaybackSession;g_nowMs+=3100;
  g_render.FrameDeferredDV();
  check(!aml_dv_output_transition_pending(owner),"background engage completes output");
  g_fullscreen=true;
  // A stale engage reached in fullscreen must wait for its own follow-up.
  deferred_open();owner=s_dvPlaybackSession;g_nowMs+=3100;
  g_render.ContinueDeferredDV(true);
  check(aml_dv_output_transition_pending(owner),"fullscreen stale engage waits for follow-up");
  g_render.FrameDeferredDV();
  check(!aml_dv_output_transition_pending(owner),"fullscreen fallback follow-up completes output");
  // Consuming a request is not success. Failure keeps blackout eligible for
  // bounded recovery, rather than the next idle update declaring completion.
  deferred_open();owner=s_dvPlaybackSession;throwEngage=true;
  try{g_render.UpdateResolution();check(false,"expected failure");}catch(const std::runtime_error&){}
  throwEngage=false;g_render.UpdateResolution();g_render.ContinueDeferredDV(false);
  check(aml_dv_output_transition_pending(owner),"failed programming cannot complete output");
  // A late old renderer/close must not discharge a replacement session.
  auto old=owner;deferred_open();owner=s_dvPlaybackSession;
  aml_dv_complete_output_transition(old);aml_dv_cancel_deferred_session(old);
  check(aml_dv_output_transition_pending(owner),"old identity cannot cancel replacement output");
  aml_dv_cancel_deferred_session(owner);
  check(!aml_dv_output_transition_pending(owner),"matching cancellation clears output");
}

int main() {
  try {
    auto display=CAMLSession::FenceDisplay();check(CAMLSession::TryBeginDisplay(display),"display");
    check(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY),"ready");
    auto stream=nativeSession.Fence();check(nativeSession.BeginMutation(stream)&&nativeSession.Complete(stream,true),"session");
    test_native_startup_policy();
    test_output_completion();
    test_mode_set();
    test_pending_window();
    test_window_without_mode_switch();
    test_forced_switch();
    test_refresh_switching_off();
    test_no_decoder_waits_quietly();
    test_pending_identity_and_cleanup();
    test_background_fallback();
    std::cout << "PASS: native startup eligibility, output completion/cancellation/failure, 6 disc DV scenarios, background fallback and session admission\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
'''
    for key, value in [("TRANSITION", transition), ("OUTPUT", output), ("OPENING", opening), ("CLOSING", closing), ("ENGAGE", engage), ("PENDING", pending), ("CONTINUATION", continuation), ("STALE", stale), ("GATE", gate),
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
            ('inactive disc cleanup cancels native startup', '  if (!wasHeld)\n    return; // A non-disc startup can also own a deferred output transition.', ''),
            ('early output on native startup', '(s_dvDiscHold || nativeStartup)', 's_dvDiscHold'),
            ('complete before programming', ' && transition->programmed.load()', ''),
            ('publish success before effects', '    aml_dv_on(s_dvDiscDeferredMode);', '    if(auto t=std::atomic_load(&s_dvOutputTransition)) { t->programmed=true; }\n    aml_dv_on(s_dvDiscDeferredMode);'),
            ('clear blackout at engage', 'transition->programmed = true;', '{ transition->programmed = true; aml_dv_complete_output_transition(transition->session); }'),
            ('stale poll discharges follow-up', 'staleOnly && pending &&', 'staleOnly &&'),
            ('fullscreen fallback skips follow-up', '(!fullscreen || !m_bTriggerUpdateResolution)', 'true'),
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
