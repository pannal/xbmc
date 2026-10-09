#!/usr/bin/env python3
"""Execute production subtitle shift case blocks and offset persistence helpers.

Application/player/settings services are recording substitutes, including unchanged
setting callbacks and a setting-lock ordering oracle. No GUI/device acceptance.
"""
import argparse
from pathlib import Path
import os
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def source():
    player = (ROOT / 'xbmc/video/PlayerController.cpp').read_text()
    settings = (ROOT / 'xbmc/settings/SubtitlesSettings.cpp').read_text()
    code = PREFIX
    for sig in ['float CSubtitlesSettings::GetBitmapOffset()',
                'void CSubtitlesSettings::SetBitmapOffset(',
                'void CSubtitlesSettings::SetBitmapPreference(',
                'void CSubtitlesSettings::EndBitmapPosition()']:
        code += function(settings, sig) + '\n'
    code += function(player, 'bool UseBitmapManualPosition(') + '\n'
    code += 'bool Controller::Shift(const Action& action){switch(action.GetID()){\n'
    for which in ['UP', 'DOWN']:
        code += function(player, 'case ACTION_SUBTITLE_VSHIFT_' + which + ':') + '\n'
    code += 'default:return false;}}\n'
    vobsub = (ROOT / 'xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxVobsub.h').read_text()
    code += 'struct VobSub {std::vector<int> m_Streams{0,1};\n'
    code += function(vobsub, 'std::string GetStreamCodecName(').replace(' override', '') + ';};\n'
    # Keep actual renderer/dialog integration and source stream-info wiring explicit.
    dialog = (ROOT / 'xbmc/video/dialogs/GUIDialogSubtitleSettings.cpp').read_text()
    assert 'GetSubtitlesSettings()->SetBitmapPreference(' in function(dialog, 'void CGUIDialogSubtitleSettings::OnSettingChanged(')
    assert 'GetSubtitlesSettings()->EndBitmapPosition();' in (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/OverlayRenderer.cpp').read_text()
    assert 'info.codecName = s.codec;' in function((ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text(), 'void CVideoPlayer::GetSubtitleStreamInfo(')
    return code + TESTS


PREFIX = r'''
#include <algorithm>
#include <cassert>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>
constexpr int ACTION_SUBTITLE_VSHIFT_UP=1,ACTION_SUBTITLE_VSHIFT_DOWN=2;
namespace SUBTITLES {enum class Align{MANUAL,BOTTOM_INSIDE,BOTTOM_OUTSIDE,TOP_INSIDE,TOP_OUTSIDE};}
struct CSettings {
 static constexpr int SETTING_SUBTITLES_BITMAPPOSITION=1,SETTING_SUBTITLES_BITMAPOFFSET=2;
 int mode=5,writes=0,saves=0;double number=0;std::function<void()> callback,onRead;
 int GetInt(int){return mode;}
 double GetNumber(int){if(onRead)onRead();return number;}
 void SetNumber(int,double n){++writes;if(n!=number){number=n;if(callback)callback();}}
 void Save(){++saves;}
};
struct CSubtitlesSettings {
 std::shared_ptr<CSettings> m_settings;
 std::mutex m_bitmapPositionMutex;
 std::optional<float> m_bitmapOffset,m_bitmapSavedOffset;
 SUBTITLES::Align align=SUBTITLES::Align::TOP_INSIDE;float margin=3.5f;
 float GetVerticalMarginPerc(){return margin;}SUBTITLES::Align GetAlignment(){return align;}
 float GetBitmapOffset();void SetBitmapOffset(float,bool);void SetBitmapPreference(float);void EndBitmapPosition();
};
struct RESOLUTION_INFO {int iHeight=1080;struct{int top=0,bottom=1080;}Overscan;};
struct Window {RESOLUTION_INFO info;Window& GetGfxContext(){return *this;}RESOLUTION_INFO GetResInfo(){return info;}}window;
struct Components {std::shared_ptr<CSettings> settings=std::make_shared<CSettings>();std::shared_ptr<CSubtitlesSettings> subtitles=std::make_shared<CSubtitlesSettings>();
 auto GetSettings(){return settings;}auto GetSubtitlesSettings(){return subtitles;}}components;
struct CServiceBroker {static Components* GetSettingsComponent(){return &components;}static Window* GetWinSystem(){return &window;}};
struct SubtitleStreamInfo {bool valid=true;std::string codecName="hdmv_pgs_subtitle";};
struct CVideoSettings {int m_subtitleVerticalPosition=900;};
struct CApplicationPlayer {SubtitleStreamInfo info;CVideoSettings video;int textMoves=0;bool lastSave=false,inMenu=false;
 int GetSubtitle(){return 0;}void GetSubtitleStreamInfo(int,SubtitleStreamInfo& out){out=info;}bool IsInMenu(){return inMenu;}
 CVideoSettings GetVideoSettings(){return video;}
 void SetSubtitleVerticalPosition(int pos,bool save){video.m_subtitleVerticalPosition=pos;lastSave=save;++textMoves;}
};
struct Action {int id;std::string text;int GetID()const{return id;}std::string GetText()const{return text;}};
struct Controller {
 std::shared_ptr<CApplicationPlayer> appPlayer=std::make_shared<CApplicationPlayer>();
 struct Speed {float distance=10.8f;float GetUpdatedDistance(int){return distance;}}m_movingSpeed;
 int sliders=0,label=0;float last=0;
 void ShowSlider(int,int l,float value,float,float,float){++sliders;label=l;last=value;}
 bool Shift(const Action&);
};
'''

TESTS = r'''
int main(){
 components.subtitles->m_settings=components.settings;
 auto settings=components.subtitles;auto prefs=components.settings;
 prefs->callback=[&]{std::lock_guard<std::mutex> lock(settings->m_bitmapPositionMutex);settings->m_bitmapOffset.reset();settings->m_bitmapSavedOffset.reset();};
 // GetNumber must never execute while the action-state mutex is held.
 prefs->onRead=[&]{assert(settings->m_bitmapPositionMutex.try_lock());settings->m_bitmapPositionMutex.unlock();};
 Controller c;
 assert(c.Shift({ACTION_SUBTITLE_VSHIFT_UP,""}));assert(c.appPlayer->textMoves==0);assert(c.label==69322);
 assert(std::abs(settings->GetBitmapOffset()-1.0f)<0.001f&&prefs->writes==0&&prefs->saves==0);
 assert(c.Shift({ACTION_SUBTITLE_VSHIFT_DOWN,""}));assert(std::abs(settings->GetBitmapOffset())<0.001f);
 // PGS shift works even when text has top/inside alignment; 2160p scales equally.
 window.info.iHeight=2160;c.m_movingSpeed.distance=21.6f;
 c.Shift({ACTION_SUBTITLE_VSHIFT_UP,"save"});assert(std::abs(settings->GetBitmapOffset()-1.0f)<0.001f);
 c.Shift({ACTION_SUBTITLE_VSHIFT_UP,""});assert(std::abs(settings->GetBitmapOffset()-2.0f)<0.001f);
 settings->EndBitmapPosition();assert(prefs->number==1&&prefs->writes==1&&prefs->saves==1);
 assert(settings->GetBitmapOffset()==1); // last explicit saved value, not later repeats
 for(int i=0;i<1000;++i)c.Shift({ACTION_SUBTITLE_VSHIFT_UP,""});
 assert(settings->GetBitmapOffset()==100&&prefs->writes==1&&prefs->saves==1);
 settings->EndBitmapPosition();assert(settings->GetBitmapOffset()==1&&prefs->saves==1);
 // GUI choosing the already saved value still cancels a transient and pending save.
 settings->SetBitmapOffset(5,true);settings->SetBitmapPreference(1);
 assert(settings->GetBitmapOffset()==1);settings->EndBitmapPosition();assert(prefs->number==1&&prefs->saves==1);
 // External VobSub's actual demux method supplies the identity used by the action.
 VobSub vob;assert(vob.GetStreamCodecName(0)=="dvd_subtitle"&&vob.GetStreamCodecName(1)=="dvd_subtitle");
 assert(vob.GetStreamCodecName(-1).empty()&&vob.GetStreamCodecName(2).empty());
 c.appPlayer->info.codecName=vob.GetStreamCodecName(0);c.Shift({ACTION_SUBTITLE_VSHIFT_DOWN,""});assert(settings->GetBitmapOffset()==0);
 // Saved bitmap Manual must never steal selected text actions.
 c.appPlayer->info.codecName="subrip";settings->align=SUBTITLES::Align::MANUAL;
 int before=c.appPlayer->textMoves;c.Shift({ACTION_SUBTITLE_VSHIFT_UP,"save"});
 assert(c.appPlayer->textMoves==before+1&&c.appPlayer->lastSave&&c.label==277);
 assert(settings->GetBitmapOffset()==0);
 settings->align=SUBTITLES::Align::TOP_INSIDE;before=c.appPlayer->textMoves;c.Shift({ACTION_SUBTITLE_VSHIFT_DOWN,""});assert(c.appPlayer->textMoves==before);
 settings->align=SUBTITLES::Align::BOTTOM_OUTSIDE;c.Shift({ACTION_SUBTITLE_VSHIFT_DOWN,""});assert(c.appPlayer->textMoves==before+1);
 c.appPlayer->info.codecName="ass";c.Shift({ACTION_SUBTITLE_VSHIFT_UP,""});assert(c.appPlayer->textMoves==before+2);
 c.appPlayer->info.codecName="hdmv_pgs_subtitle";c.appPlayer->inMenu=true;assert(!UseBitmapManualPosition(c.appPlayer));
 c.appPlayer->inMenu=false;c.appPlayer->info.valid=false;assert(!UseBitmapManualPosition(c.appPlayer));
 std::cout<<"PASS: production bitmap actions, selected text/VobSub gates, scaled offset and deferred/GUI save semantics\n";
}
'''


def run(code, negative=False):
    with tempfile.TemporaryDirectory(prefix='bitmap-subtitle-actions-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(code)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wdouble-promotion',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], text=True, capture_output=True,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=10)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = source()
    run(code)
    if args.negative_controls:
        for label, old, new in [
            ('text intercepted', 'return info.valid && (info.codecName', 'return info.valid || (info.codecName'),
            ('save per repeat', 'if (save)\n    m_bitmapSavedOffset', 'm_settings->Save();\n  if (save)\n    m_bitmapSavedOffset'),
            ('unchanged GUI preference retains action', 'm_bitmapOffset.reset();\n    m_bitmapSavedOffset.reset();', '/* retain stale transient and pending save */'),
            ('settings lock inversion', 'const float saved = static_cast<float>(m_settings->GetNumber(CSettings::SETTING_SUBTITLES_BITMAPOFFSET));\n  std::lock_guard<std::mutex> lock(m_bitmapPositionMutex);', 'std::lock_guard<std::mutex> lock(m_bitmapPositionMutex);\n  const float saved = static_cast<float>(m_settings->GetNumber(CSettings::SETTING_SUBTITLES_BITMAPOFFSET));'),
        ]:
            assert old in code
            run(code.replace(old, new, 1), True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
