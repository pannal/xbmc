#!/usr/bin/env python3
"""Exercise actual native GUI sizing through the production window lifecycle.

Reuses recording EGL/native/service boundaries from the window fixture, replacing
its GUI sizing stubs with AMLUtils' production state and methods. Checks logical
window dimensions, calibration, admission rollback and blank/unblank requests;
no physical display, HDMI receiver or decoder is emulated.
"""
import argparse
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
window = runpy.run_path(str(ROOT / 'tools/test-aml-window-lifecycle.py'))
function = window['function']


def harness():
    source = window['harness']()

    def replace(old, new):
        nonlocal source
        assert source.count(old) == 1, old
        source = source.replace(old, new)

    resolution = (ROOT / 'xbmc/windowing/Resolution.h').read_text()
    types = function(resolution, 'struct OVERSCAN') + ';\n'
    types += function(resolution, 'struct EdgeInsets') + ';\n'
    types += function((ROOT / 'xbmc/windowing/Resolution.cpp').read_text(),
                      'EdgeInsets::EdgeInsets(') + '\n'
    replace('struct RESOLUTION_INFO{', types + 'struct RESOLUTION_INFO{')
    replace('std::string strId;};',
            'std::string strId, strMode; OVERSCAN Overscan; EdgeInsets guiInsets; int iSubtitles{0};};')
    replace('#define HAS_LIBAMCODEC 1',
            '#define HAS_LIBAMCODEC 1\n#define LINUX_VERSION_CODE ((4 << 16) | (9 << 8))\n#include <algorithm>')
    for signature in ('void aml_native_gui_prepare(RESOLUTION)', 'void aml_native_gui_commit()',
                      'void aml_native_gui_prepare_failed()', 'void aml_native_gui_osd_present(bool)',
                      'bool aml_native_gui_needs_update(RESOLUTION)', 'bool aml_native_gui_size_changing()'):
        replace(function(source, signature), signature + ';')
    replace('SETTING_COREELEC_AMLOGIC_DV_HANDSHAKE_BYPASS=3;',
            'SETTING_COREELEC_AMLOGIC_DV_HANDSHAKE_BYPASS=3,SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING=4,'
            'SETTING_COREELEC_AMLOGIC_GUI1080PFOR4K=5;')
    replace('bool m_fullScreen=true;',
            'bool nativeGui=true,gui1080pFor4k=false,m_gui1080pForHighRefreshVideo=true;'
            'bool m_fullScreen=true;')
    replace('bool GetBool(int){return false;}',
            'bool GetBool(int key){return key==CSettings::SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING?nativeGui:'
            'key==CSettings::SETTING_COREELEC_AMLOGIC_GUI1080PFOR4K&&gui1080pFor4k;}')
    replace('explicit CSysfsPath(const char*){}',
            'explicit CSysfsPath(const char*){} CSysfsPath(const std::string& path,int value){'
            'assert(path=="/sys/class/graphics/fb0/blank");event("blank:"+std::to_string(value));}')
    replace(function(source, 'struct CDisplaySettings') + ';', r'''
struct CDisplaySettings {
  std::vector<RESOLUTION_INFO> resolutions;
  static CDisplaySettings& GetInstance(){static CDisplaySettings instance;return instance;}
  RESOLUTION_INFO& GetResolutionInfo(int res){return resolutions.at(res);}
  const RESOLUTION_INFO& GetResolutionInfo(int res)const{return resolutions.at(res);}
  size_t ResolutionInfoSize()const{return resolutions.size();}
};''')
    utils = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    start = utils.index('enum class NativeGuiVideo')
    end = utils.index('\nvoid aml_handle_display_stereo_mode(', start)
    replace('struct CServiceBroker{', '''struct CApplicationPlayer {
  bool playingVideo = false;
  bool pendingVideo = false;
  bool IsPlayingLocalVideo() const { return playingVideo; }
  bool HasPendingVideoOpen() const { return pendingVideo; }
};
static CApplicationPlayer appPlayer;
struct AppComponents {
  template<class T> T* GetComponent() { return &appPlayer; }
};
struct CServiceBroker{
  static AppComponents& GetAppComponents() { static AppComponents c; return c; }
'''.rstrip())
    production = 'static bool s_nativeGuiOsdHold=false;\n'
    production += function(utils, 'static void aml_native_gui_osd_unblank(') + '\n'
    production += utils[start:end] + '\n'
    marker = 'bool CWinSystemAmlogicGLESContext::CreateNewWindow('
    replace(marker, production + marker)
    replace(function(source, 'int main()'), CASES)
    return source


CASES = r'''
void NativeSetup(Fixture& f,bool native=true) {
  auto& display=CDisplaySettings::GetInstance();display.resolutions.assign(4,RESOLUTION_INFO{});
  settings.nativeGui=native;
  settings.gui1080pFor4k = false;
  settings.m_gui1080pForHighRefreshVideo = true;
  appPlayer.playingVideo = false;
  appPlayer.pendingVideo = false;
  s_nativeGuiVideo=NativeGuiVideo::None;s_nativeGuiSuspended=false;s_nativeGuiSuspendedBefore=false;
  s_nativeGuiBlankPending=false;s_nativeGuiOsdHold=false;s_nativeGuiCalibrations.clear();
  for(int res=1;res<=3;res++) {
    auto& info=display.GetResolutionInfo(res);
    info.iScreenWidth=3840;info.iScreenHeight=2160;
    info.iWidth=native?3840:1920;info.iHeight=native?2160:1080;
    info.fRefreshRate=res==1?50.0f:res==2?60.0f:24.0f;
    info.strMode="UHD"+std::to_string(res);
    info.Overscan.left=19;info.Overscan.top=27;info.Overscan.right=3811;info.Overscan.bottom=2129;
    info.guiInsets=EdgeInsets(3.25f,5.5f,7.75f,9.0f);info.iSubtitles=2087;
  }
  currentNative=display.GetResolutionInfo(1);
  f.gfx.m_iScreenWidth=f.win.m_nWidth=currentNative.iWidth;
  f.gfx.m_iScreenHeight=f.win.m_nHeight=currentNative.iHeight;
  f.win.m_fRefreshRate=50.0f;events.clear();
}
void CheckSize(Fixture& f,int width,int height,int res) {
  auto& info=CDisplaySettings::GetInstance().GetResolutionInfo(res);
  assert(info.iWidth==width&&info.iHeight==height);
  assert(f.gfx.m_Resolution==res&&f.gfx.m_iScreenWidth==width&&f.gfx.m_iScreenHeight==height);
  assert(f.win.m_nWidth==width&&f.win.m_nHeight==height);
  if(f.win.m_nativeWindow)
    assert(f.win.m_nativeWindow->width==width&&f.win.m_nativeWindow->height==height);
  assert(currentNative.iScreenWidth==3840&&currentNative.iScreenHeight==2160);
  assert(currentNative.fRefreshRate==info.fRefreshRate);
}
void CheckCalibration() {
  for(int res=1;res<=3;res++) {
    auto& info=CDisplaySettings::GetInstance().GetResolutionInfo(res);
    assert(info.Overscan.left==19&&info.Overscan.top==27&&info.Overscan.right==3811&&info.Overscan.bottom==2129);
    assert(info.guiInsets.left==3.25f&&info.guiInsets.top==5.5f&&info.guiInsets.right==7.75f&&info.guiInsets.bottom==9.0f);
    assert(info.iSubtitles==2087);
  }
}
void StartHeavy(Fixture& f) {
  appPlayer.playingVideo = true;
  aml_native_gui_set_video(true,false);
  assert(f.gfx.SetVideoResolution(1,false));CheckSize(f,1920,1080,1);
  assert(s_nativeGuiSuspended&&has("blank:1"));
  auto& info=CDisplaySettings::GetInstance().GetResolutionInfo(1);
  assert(info.Overscan.left==10&&info.Overscan.top==14&&info.iSubtitles==1044);
  f.win.PresentRenderImpl(true);f.win.PresentRenderImpl(true);
  assert(!s_nativeGuiOsdHold&&has("blank:0"));events.clear();
}
void StopKeepsOutputAndRestoresSize() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  appPlayer.playingVideo = false;
  aml_native_gui_set_video(false,false);aml_native_gui_playback_stopped();
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,1);CheckCalibration();
  assert(!s_nativeGuiSuspended&&s_nativeGuiOsdHold&&has("blank:1"));
  assert(!has("engage-mode")); // GUI-only restoration, unchanged UHD50
  f.win.PresentRenderImpl(true);assert(s_nativeGuiOsdHold);
  f.win.PresentRenderImpl(true);assert(!s_nativeGuiOsdHold&&has("blank:0"));
}
void CleanupWins() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  appPlayer.playingVideo = false;
  aml_native_gui_set_video(false,false);aml_native_gui_playback_stopped();
  assert(f.gfx.SetVideoResolution(2,false));events.clear();
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,2);CheckCalibration();
  assert(!has("native-set:3840")&&!has("engage-mode"));
}
void BusyDisplayRestoresOnRetry() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  appPlayer.playingVideo = false;
  aml_native_gui_set_video(false,false);aml_native_gui_playback_stopped();
  {
    auto permit=f.session.AcquireDecoder();assert(permit);
    assert(!f.gfx.SetVideoResolution(2,false));
    f.gfx.ProcessPendingVideoResolution();CheckSize(f,1920,1080,1);
    assert(s_nativeGuiSuspended&&f.gfx.m_pendingVideoResolution&&!has("blank:1"));
  }
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,2);CheckCalibration();
  assert(!s_nativeGuiSuspended&&!f.gfx.m_pendingVideoResolution&&has("blank:1"));
}
void ReplacementKeepsHeavyPolicy() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  aml_native_gui_playback_stopped(); // new heavy decoder already owns the flag
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,1920,1080,1);
  assert(s_nativeGuiVideo==NativeGuiVideo::Above1080p&&s_nativeGuiSuspended&&!has("blank:1"));
  // A newer 24 Hz selection requires native GUI even with heavy video.
  aml_native_gui_playback_stopped();
  {
    auto permit=f.session.AcquireDecoder();assert(permit);
    assert(!f.gfx.SetVideoResolution(3,false));
  }
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,3);CheckCalibration();
  assert(s_nativeGuiVideo==NativeGuiVideo::Above1080p&&!s_nativeGuiSuspended);
}
void NativeDisabledAndLightControls() {
  for(bool native:{false,true}) {
    Fixture f;NativeSetup(f,native);
    aml_native_gui_set_video(!native,false);
    assert(f.gfx.SetVideoResolution(1,false));events.clear();
    aml_native_gui_playback_stopped();f.gfx.ProcessPendingVideoResolution();
    CheckSize(f,native?3840:1920,native?2160:1080,1);
    assert(!s_nativeGuiSuspended&&!f.gfx.m_videoResolutionRefreshPending&&events.empty());
  }
}
void OptInAtFilmRate() {
  Fixture f;NativeSetup(f);
  assert(f.gfx.SetVideoResolution(3,false));CheckSize(f,3840,2160,3);events.clear();
  aml_native_gui_set_video(true,false);
  assert(!aml_native_gui_suspend_wanted(3));
  settings.gui1080pFor4k=true;
  assert(f.gfx.SetVideoResolution(3,false));CheckSize(f,1920,1080,3);
  assert(!lastNativeForced&&!has("engage-mode"));
  aml_native_gui_set_video(false,false);aml_native_gui_playback_stopped();
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,3);CheckCalibration();
  aml_native_gui_set_video(false,true);
  appPlayer.playingVideo = true;
  assert(!aml_native_gui_suspend_wanted(3)&&aml_native_gui_suspend_wanted(2));
}
void AutomaticFallbackFollowsVideoAndOutputRate() {
  Fixture f;
  NativeSetup(f);
  assert(f.gfx.SetVideoResolution(2, false));
  CheckSize(f, 3840, 2160, 2);
  events.clear();

  // An in-process video with no AML heavy-stream classification needs headroom too.
  appPlayer.playingVideo = true;
  f.gfx.ProcessPendingVideoResolution();
  CheckSize(f, 1920, 1080, 2);
  assert(!lastNativeForced && !has("engage-mode"));

  // A queued output change takes precedence over a GUI-only refresh.
  CDisplaySettings::GetInstance().GetResolutionInfo(3).fRefreshRate = 30.0f;
  f.gfx.m_pendingVideoResolution = CGraphicContext::ResolutionRequest{3, false};
  f.gfx.ProcessPendingVideoResolution();
  CheckSize(f, 3840, 2160, 3);
  CheckCalibration();
  assert(f.gfx.SetVideoResolution(2, false));
  CheckSize(f, 1920, 1080, 2);

  appPlayer.playingVideo = false;
  appPlayer.pendingVideo = true;
  f.gfx.ProcessPendingVideoResolution();
  CheckSize(f, 1920, 1080, 2);
  appPlayer.pendingVideo = false;
  f.gfx.ProcessPendingVideoResolution();
  CheckSize(f, 3840, 2160, 2);
  CheckCalibration();
}
void AutomaticFallbackIgnores1440pOutput() {
  Fixture f;
  NativeSetup(f);
  auto& info = CDisplaySettings::GetInstance().GetResolutionInfo(2);
  info.iWidth = info.iScreenWidth = 2560;
  info.iHeight = info.iScreenHeight = 1440;
  appPlayer.playingVideo = true;
  assert(!aml_native_gui_suspend_wanted(2));
}
void AutomaticFallbackCanBeDisabled() {
  Fixture f;
  NativeSetup(f);
  appPlayer.playingVideo = true;
  aml_native_gui_set_video(true, false);
  settings.m_gui1080pForHighRefreshVideo = false;
  assert(f.gfx.SetVideoResolution(2, false));
  CheckSize(f, 3840, 2160, 2);
  settings.gui1080pFor4k = true;
  f.gfx.ProcessPendingVideoResolution();
  CheckSize(f, 1920, 1080, 2);
}
void RejectedResizeIsRetried() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  appPlayer.playingVideo = false;
  aml_native_gui_set_video(false,false);aml_native_gui_playback_stopped();
  rejectNativeResolution=true;
  f.gfx.ProcessPendingVideoResolution();
  assert(s_nativeGuiSuspended&&f.gfx.m_pendingVideoResolution);
  assert(currentNative.iWidth==1920&&f.win.m_nWidth==1920);
  assert(has("blank:1")&&has("blank:0")&&!s_nativeGuiOsdHold);
  rejectNativeResolution=false;
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,1);CheckCalibration();
  assert(!s_nativeGuiSuspended&&!f.gfx.m_pendingVideoResolution);
}
int main() {
  StopKeepsOutputAndRestoresSize();CleanupWins();BusyDisplayRestoresOnRetry();
  ReplacementKeepsHeavyPolicy();NativeDisabledAndLightControls();OptInAtFilmRate();
  AutomaticFallbackFollowsVideoAndOutputRate();AutomaticFallbackIgnores1440pOutput();
  AutomaticFallbackCanBeDisabled();
  RejectedResizeIsRetried();
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness()
    if not args.negative_controls:
        window['run'](source)
        print('Native GUI sizing: PASS (production sizing and window methods; ASan/UBSan)')
        return
    controls = [
        ('no sizing preparation', '  aml_native_gui_prepare(res);', ''),
        ('no rollback', '  aml_native_gui_apply(s_nativeGuiSuspendedBefore);', ''),
        ('no framebuffer hold', '  CSysfsPath("/sys/class/graphics/fb0/blank", 1);', ''),
        ('no exact calibration restore', '      info.Overscan = saved->overscan;', ''),
        ('clear replacement decoder', '  if (s_nativeGuiSuspended)\n',
         '  s_nativeGuiVideo = NativeGuiVideo::None;\n  if (s_nativeGuiSuspended)\n'),
        ('ignore newer output', 'm_pendingVideoResolution.value_or(ResolutionRequest{m_Resolution, false})',
         'ResolutionRequest{m_Resolution, false}'),
        ('ignore opt-in', 'guiSettings->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_GUI1080PFOR4K)', 'false'),
        ('ignore playback', 'player->IsPlayingLocalVideo() ||', 'true ||'),
        ('ignore opt-out', '!settings->GetAdvancedSettings()->m_gui1080pForHighRefreshVideo',
         'false'),
        ('ignore failed framebuffer resize', '  if (!resolutionSet)\n', '  if (false && !resolutionSet)\n'),
    ]
    for name, before, after in controls:
        assert source.count(before) == 1, name
        window['run'](source.replace(before, after), negative=True)
        print('Rejected sizing negative control:', name)


if __name__ == '__main__':
    main()
