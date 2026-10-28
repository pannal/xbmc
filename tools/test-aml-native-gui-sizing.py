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
            'SETTING_COREELEC_AMLOGIC_DV_HANDSHAKE_BYPASS=3,SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING=4;')
    replace('bool m_fullScreen=true;', 'bool nativeGui=true;bool m_fullScreen=true;')
    replace('bool GetBool(int){return false;}',
            'bool GetBool(int key){return key==CSettings::SETTING_COREELEC_AMLOGIC_DISABLEGUISCALING&&nativeGui;}')
    replace('explicit CSysfsPath(const char*){}',
            'explicit CSysfsPath(const char*){} CSysfsPath(const char* path,int value){'
            'assert(std::string(path)=="/sys/class/graphics/fb0/blank");event("blank:"+std::to_string(value));}')
    replace(function(source, 'struct CDisplaySettings') + ';', r'''
struct CDisplaySettings {
  std::vector<RESOLUTION_INFO> resolutions;
  static CDisplaySettings& GetInstance(){static CDisplaySettings instance;return instance;}
  RESOLUTION_INFO& GetResolutionInfo(int res){return resolutions.at(res);}
  size_t ResolutionInfoSize()const{return resolutions.size();}
};''')
    utils = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    start = utils.index('static std::atomic<bool> s_heavyVideo{false};')
    end = utils.index('\nvoid aml_handle_display_stereo_mode(', start)
    production = 'static bool s_nativeGuiOsdHold=false;\n' + utils[start:end] + '\n'
    marker = 'bool CWinSystemAmlogicGLESContext::CreateNewWindow('
    replace(marker, production + marker)
    replace(function(source, 'int main()'), CASES)
    return source


CASES = r'''
void NativeSetup(Fixture& f,bool native=true) {
  auto& display=CDisplaySettings::GetInstance();display.resolutions.assign(4,RESOLUTION_INFO{});
  settings.nativeGui=native;
  s_heavyVideo=false;s_nativeGuiSuspended=false;s_nativeGuiSuspendedBefore=false;
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
  aml_native_gui_set_heavy_video(true);
  assert(f.gfx.SetVideoResolution(1,false));CheckSize(f,1920,1080,1);
  assert(s_nativeGuiSuspended&&has("blank:1"));
  auto& info=CDisplaySettings::GetInstance().GetResolutionInfo(1);
  assert(info.Overscan.left==10&&info.Overscan.top==14&&info.iSubtitles==1044);
  f.win.PresentRenderImpl(true);f.win.PresentRenderImpl(true);
  assert(!s_nativeGuiOsdHold&&has("blank:0"));events.clear();
}
void StopKeepsOutputAndRestoresSize() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  aml_native_gui_set_heavy_video(false);aml_native_gui_playback_stopped();
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,1);CheckCalibration();
  assert(!s_nativeGuiSuspended&&s_nativeGuiOsdHold&&has("blank:1"));
  assert(!has("engage-mode")); // GUI-only restoration, unchanged UHD50
  f.win.PresentRenderImpl(true);assert(s_nativeGuiOsdHold);
  f.win.PresentRenderImpl(true);assert(!s_nativeGuiOsdHold&&has("blank:0"));
}
void CleanupWins() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  aml_native_gui_set_heavy_video(false);aml_native_gui_playback_stopped();
  assert(f.gfx.SetVideoResolution(2,false));events.clear();
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,2);CheckCalibration();
  assert(!has("native-set:3840")&&!has("engage-mode"));
}
void BusyDisplayRestoresOnRetry() {
  Fixture f;NativeSetup(f);StartHeavy(f);
  aml_native_gui_set_heavy_video(false);aml_native_gui_playback_stopped();
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
  assert(s_heavyVideo&&s_nativeGuiSuspended&&!has("blank:1"));
  // A newer 24 Hz selection requires native GUI even with heavy video.
  aml_native_gui_playback_stopped();
  {
    auto permit=f.session.AcquireDecoder();assert(permit);
    assert(!f.gfx.SetVideoResolution(3,false));
  }
  f.gfx.ProcessPendingVideoResolution();CheckSize(f,3840,2160,3);CheckCalibration();
  assert(s_heavyVideo&&!s_nativeGuiSuspended);
}
void NativeDisabledAndLightControls() {
  for(bool native:{false,true}) {
    Fixture f;NativeSetup(f,native);
    aml_native_gui_set_heavy_video(!native);
    assert(f.gfx.SetVideoResolution(1,false));events.clear();
    aml_native_gui_playback_stopped();f.gfx.ProcessPendingVideoResolution();
    CheckSize(f,native?3840:1920,native?2160:1080,1);
    assert(!s_nativeGuiSuspended&&!f.gfx.m_videoResolutionRefreshPending&&events.empty());
  }
}
int main() {
  StopKeepsOutputAndRestoresSize();CleanupWins();BusyDisplayRestoresOnRetry();
  ReplacementKeepsHeavyPolicy();NativeDisabledAndLightControls();
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness()
    if not args.negative_controls:
        window['run'](source)
        print('Native GUI sizing: PASS (six scenarios; production sizing and window methods; ASan/UBSan)')
        return
    controls = [
        ('no sizing preparation', '  aml_native_gui_prepare(res);', ''),
        ('no rollback', '  aml_native_gui_apply(s_nativeGuiSuspendedBefore);', ''),
        ('no framebuffer hold', '  CSysfsPath("/sys/class/graphics/fb0/blank", 1);', ''),
        ('no exact calibration restore', '      info.Overscan = saved->overscan;', ''),
        ('clear replacement decoder', '  if (s_nativeGuiSuspended)\n', '  s_heavyVideo = false;\n  if (s_nativeGuiSuspended)\n'),
        ('ignore newer output', 'm_pendingVideoResolution.value_or(ResolutionRequest{m_Resolution, false})',
         'ResolutionRequest{m_Resolution, false}'),
    ]
    for name, before, after in controls:
        assert source.count(before) == 1, name
        window['run'](source.replace(before, after), negative=True)
        print('Rejected sizing negative control:', name)


if __name__ == '__main__':
    main()
