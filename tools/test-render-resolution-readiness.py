#!/usr/bin/env python3
"""Production resolution apply/readiness and deferred-DV effects with real AML admission.

Service/settings/window effects are deterministic substitutes. Real display/native
lifecycle headers enforce leases. No EGL, kernel, device or scanout claim.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    source = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    header = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.h').read_text()
    record = function(header, '  struct AppliedResolution') + ';'
    methods = '\n'.join(function(source, signature) for signature in [
        'void CRenderManager::UpdateResolution(bool force)',
        'bool CRenderManager::ContinueDeferredDV(bool staleOnly)',
        'void CRenderManager::CancelDeferredDV()',
        'void CRenderManager::TriggerUpdateResolutionHdr(',
        'void CRenderManager::TriggerUpdateResolution(float fps,'])
    # Exercise the actual reset prefix of each main lifecycle entry without
    # reproducing its unrelated renderer/capture/queue teardown dependencies.
    entries = []
    for name, signature in [('ConfigureEntry', 'bool CRenderManager::Configure()'),
                            ('PreInitEntry', 'void CRenderManager::PreInitOnMain()'),
                            ('UnInitEntry', 'void CRenderManager::UnInitOnMain()')]:
        body = function(source, signature)
        prefix = body[body.index('{') + 1:body.index('#if')]
        assert 'm_appliedResolution.reset();' in prefix, name
        entries.append(f'void CRenderManager::{name}() {{{prefix}}}')
    assert 'm_amlPresenter' not in function(source, 'void CRenderManager::UpdateResolution(bool force)')
    gfx = (ROOT / 'xbmc/windowing/GraphicContext.cpp').read_text()
    graphics = '\n'.join(function(gfx, signature) for signature in [
        'bool CGraphicContext::SetVideoResolution(',
        'void CGraphicContext::ProcessPendingVideoResolution()',
        'bool CGraphicContext::SetVideoResolutionInternal('])
    gfx_header = (ROOT / 'xbmc/windowing/GraphicContext.h').read_text()
    prelude = PRELUDE
    for marker, method in [('@PUBLIC_RESOLUTION@', 'SetVideoResolution'),
                           ('@INTERNAL_RESOLUTION@', 'SetVideoResolutionInternal')]:
        declaration = next(line.strip() for line in gfx_header.splitlines()
                           if f'bool {method}(' in line)
        prelude = prelude.replace(marker, declaration)
    return prelude + CLASS.replace('@RECORD@', record) + graphics + methods + '\n'.join(entries) + TESTS


def run(source, negative=False):
    with tempfile.TemporaryDirectory(prefix='render-resolution-') as directory:
        out = Path(directory)
        (out / 'test.cpp').write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                   '-pthread', '-fno-pie', '-no-pie', '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')]
        if not negative:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        subprocess.run(command, check=True)
        result = subprocess.run([str(out / 'test')], capture_output=negative, text=True,
                                check=not negative, timeout=20)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness()
    if args.negative_controls:
        mutations = [
            ('repeat completed apply', '!m_appliedResolution || !m_appliedResolution->Matches(applied)', 'true'),
            ('ignore changed intent', '!m_appliedResolution || !m_appliedResolution->Matches(applied)', '!m_appliedResolution'),
            ('store pre-apply display generation', 'applied.displayGeneration = window->GetDisplayGeneration();', ''),
            ('consume before readiness', 'if (!CServiceBroker::GetWinSystem()->IsDisplayReadyForVideo())', 'if (false)'),
            ('forget failed apply retry', 'return; // Failed/pending window work must still retry.',
             '{ m_bTriggerUpdateResolution = false; return; }'),
            ('lose deferred-DV reentrant intent', 'm_playerPort->VideoParamsChange();',
             'm_bTriggerUpdateResolution = false; m_playerPort->VideoParamsChange();'),
            ('duplicate graphics retry owner', 'gfx.SetVideoResolution(res, false, false)',
             'gfx.SetVideoResolution(res, false)'),
            ('reuse configuration completion', 'void CRenderManager::ConfigureEntry() {\n  m_appliedResolution.reset();',
             'void CRenderManager::ConfigureEntry() {\n  /* omitted */'),
        ]
        for name, before, after in mutations:
            assert before in source, name
            run(source.replace(before, after, 1), negative=True)
            print('Rejected runtime negative control:', name)
    else:
        run(source)
        print('Resolution readiness: PASS (production apply/graphics retry/DV/trigger/reset prefixes; '
              'real AML display/native admission; ASan/UBSan; modeled window/services)')


PRELUDE = r'''
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "windowing/amlogic/AMLNativeTransaction.h"
#include <cassert>
#include <functional>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <vector>
using CCriticalSection = std::recursive_mutex;
enum class StreamHdrType { HDR_TYPE_NONE, HDR_TYPE_HDR10, HDR_TYPE_DOLBYVISION };
using RENDER_STEREO_MODE=int; using STEREOSCOPIC_PLAYBACK_MODE=int; using RESOLUTION=int;
constexpr int STEREOSCOPIC_PLAYBACK_MODE_ASK=1,RENDER_STEREO_MODE_UNDEFINED=-1;
#define HAS_LIBAMCODEC 1
constexpr int ADJUST_REFRESHRATE_OFF=0,LOGINFO=1,RES_DESKTOP=1;
constexpr int RENDER_STEREO_MODE_HARDWAREBASED=3,RENDER_STEREO_VIEW_OFF=0;
constexpr int TMSG_SETVIDEORESOLUTION=1,GUI_MSG_NOTIFY_ALL=1,GUI_MSG_WINDOW_RESIZE=2;
struct RESOLUTION_INFO {int resolution=1,iWidth=1920,iHeight=1080,iScreenHeight=1080,iBlanking=0;};
struct CDisplaySettings {
 static CDisplaySettings& GetInstance(){static CDisplaySettings settings;return settings;}
 RESOLUTION_INFO GetResolutionInfo(int res){RESOLUTION_INFO info;info.resolution=res;return info;}
};
struct Rect {void SetRect(float,float,float,float){}};
struct Messenger {bool main=true;int messages=0;bool IsProcessThread(){return main;}
 void SendMsg(int,int,int){++messages;}};
struct Input {void SetMouseResolution(int,int,int,int){}};
struct CSettings {static constexpr int SETTING_VIDEOPLAYER_STEREOSCOPICPLAYBACKMODE=1,
 SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE=2;};
struct Settings {int adjust=2,stereo=0;int GetInt(int id){return id==1?stereo:adjust;}};
struct SettingsComponent {Settings settings;bool m_fullScreen=true;
 Settings* GetSettings(){return &settings;}SettingsComponent* GetAdvancedSettings(){return this;}};
struct Stereo {int user=0;int GetStereoModeByUser(){return user;}};
struct CGUIComponent {Stereo stereo;Stereo& GetStereoscopicsManager(){return stereo;}
 CGUIComponent& GetWindowManager(){return *this;}void SendMessage(int,int,int,int){}};
using GUI=CGUIComponent;
struct Win;
struct CGraphicContext:CCriticalSection {
 explicit CGraphicContext(Win* owner):window(owner){}
 Win* window;bool fullscreen=true;int sets=0,hdrSets=0,failures=0,stereo=0;
 StreamHdrType hdr=StreamHdrType::HDR_TYPE_NONE;
 std::vector<std::pair<int,StreamHdrType>> intents;
 struct ResolutionRequest {RESOLUTION resolution;bool forceUpdate;};
 std::optional<ResolutionRequest> m_pendingVideoResolution;
 RESOLUTION m_Resolution=RES_DESKTOP;bool m_bFullScreenRoot=true;
 int m_iScreenWidth=1920,m_iScreenHeight=1080;float m_fFPSOverride=0.f;Rect m_scissors;
 bool IsFullScreenVideo(){return fullscreen;}bool IsFullScreenRoot(){return fullscreen&&m_bFullScreenRoot;}
 int GetStereoMode(){return stereo;}
 void SetHDRType(StreamHdrType value){++hdrSets;hdr=value;}
 bool IsValidResolution(int res){return res>=0;}
 void UpdateInternalStateWithResolution(int res){m_Resolution=res;}
 void SetStereoView(int){}
 @PUBLIC_RESOLUTION@
 @INTERNAL_RESOLUTION@
 void ProcessPendingVideoResolution();
};
struct Win {
 CAMLDisplayLifecycle display;CGraphicContext context{this};bool immediate=false;int presents=0;
 CGraphicContext& GetGfxContext(){return context;}
 uint64_t GetDisplayGeneration() const {return display.Serial();}
 bool IsDisplayReadyForVideo() const {return display.Ready();}
 bool IsDisplayChangePending() const {return display.Pending();}
 void Present(){++presents;display.Resume();}
 bool SetFullScreen(bool,RESOLUTION_INFO& res,bool){
  ++context.sets;context.intents.emplace_back(res.resolution,context.hdr);
  CAMLDisplayLifecycle::Mutation mutation(display);
  if(!mutation)return false;
  if(context.failures){--context.failures;return false;}
  mutation.Finish(immediate?CAMLSession::DisplayPhase::READY:
                  CAMLSession::DisplayPhase::WAITING_FOR_RESET);
  return true;
 }
 bool ResizeWindow(int,int,int,int){RESOLUTION_INFO info;return SetFullScreen(false,info,false);}
};
struct CServiceBroker {
 static inline Win* win=nullptr;static inline GUI gui;static inline SettingsComponent settings;
 static inline Messenger messenger;static inline Input input;
 static Messenger* GetAppMessenger(){return &messenger;}
 static Input& GetInputManager(){return input;}
 static Win* GetWinSystem(){return win;}static GUI* GetGUI(){return &gui;}
 static SettingsComponent* GetSettingsComponent(){return &settings;}
};
struct CStreamDetails {static std::string DynamicRangeToString(StreamHdrType){return "";}};
struct CResolutionUtils {
 static inline int policy=0;
 static int ChooseBestResolution(float fps,int,int,bool){return static_cast<int>(fps*1000)+policy;}
};
struct CLog {template<class... T> static void Log(T&&...){} };
bool dvPending=false;int dvEffects=0;std::function<void()> dvEffect;
bool aml_dv_deferred_disc_pending(const std::shared_ptr<const void>&,bool){return dvPending;}
void aml_dv_engage_deferred_disc(bool,const std::shared_ptr<const void>&){
 ++dvEffects;dvPending=false;if(dvEffect)dvEffect();
}
void aml_dv_engage_stale_deferred_disc(const std::shared_ptr<const void>&){++dvEffects;}
struct Renderer {int updates=0;void Update(){++updates;}};
struct Port {int changes=0;void VideoParamsChange(){++changes;}};
'''
CLASS = r'''
struct CRenderManager {
 @RECORD@
 std::optional<AppliedResolution> m_appliedResolution;
 CCriticalSection m_resolutionlock;bool m_bTriggerUpdateResolution=true;
 float m_fps=24.f;StreamHdrType m_hdrType_override=StreamHdrType::HDR_TYPE_NONE;
 struct {std::string stereoMode;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;
  unsigned int iWidth=3840,iHeight=1606;std::shared_ptr<const void> amlDVSession=std::make_shared<int>(1);} m_picture;
 Renderer renderer;Renderer* m_pRenderer=&renderer;Port port;Port* m_playerPort=&port;int latency=0;
 bool m_closing=false,m_deferredDVResolutionAttempted=false;
 enum {STATE_CONFIGURED=1};int m_renderState=STATE_CONFIGURED;
 std::shared_ptr<const void> m_deferredDVSession;std::unique_ptr<CAMLNativeTransaction> m_deferredDVNative;
 void UpdateLatencyTweak(){++latency;}void CancelDeferredDV();bool ContinueDeferredDV(bool);
 void UpdateResolution(bool force=false);void TriggerUpdateResolutionHdr(StreamHdrType);
 void TriggerUpdateResolution(float,int,int,std::string&);
 void ConfigureEntry();void PreInitEntry();void UnInitEntry();
};
'''
TESTS = r'''
void open(CAMLSession& session){auto r=session.Fence();assert(session.BeginMutation(r));assert(session.Complete(r,true));}
struct Fixture {
 Win win;CAMLSession session;CRenderManager render;
 Fixture(){CServiceBroker::win=&win;CServiceBroker::gui.stereo.user=0;
  CServiceBroker::settings={};CServiceBroker::messenger={};CResolutionUtils::policy=0;
  dvPending=false;dvEffects=0;dvEffect={};open(session);}
 ~Fixture(){render.CancelDeferredDV();win.Present();assert(win.IsDisplayReadyForVideo());}
 void ready(){win.Present();render.UpdateResolution();assert(!render.m_bTriggerUpdateResolution);}
};
void apply_once(){
 Fixture f;f.render.UpdateResolution();assert(f.win.context.sets==1);
 assert(f.render.m_appliedResolution && f.render.m_bTriggerUpdateResolution);
 const auto generation=f.win.GetDisplayGeneration();
 for(int i=0;i<607;++i){f.render.UpdateResolution();assert(!f.session.Acquire(f.session.Epoch()));}
 assert(f.win.context.sets==1 && f.win.context.hdrSets==1 && f.render.latency==1);
 assert(f.render.renderer.updates==1 && f.render.port.changes==0);
 assert(f.win.GetDisplayGeneration()==generation);
 f.ready();assert(f.win.context.sets==1 && f.render.port.changes==1);
 assert(!f.render.m_appliedResolution && f.session.Acquire(f.session.Epoch()));
}
void failed_applies(){
 Fixture f;f.win.context.failures=2;
 for(int i=0;i<2;++i){f.render.UpdateResolution();assert(f.render.m_bTriggerUpdateResolution);
  assert(!f.render.m_appliedResolution && !f.session.Acquire(f.session.Epoch()));}
 f.render.UpdateResolution();assert(f.win.context.sets==3 && f.render.renderer.updates==1);
 for(int i=0;i<60;++i){f.render.UpdateResolution();}
 assert(f.win.context.sets==3);
 f.ready();assert(f.win.context.sets==3);
}
void pending_admission(){
 Fixture f;std::optional<CAMLSession::Permit> held(f.session.Acquire(f.session.Epoch()));assert(*held);
 f.render.UpdateResolution();assert(f.render.m_bTriggerUpdateResolution && !f.render.m_appliedResolution);
 assert(f.win.display.Pending() && f.render.latency==0 && f.render.renderer.updates==0);
 const auto pending=f.win.GetDisplayGeneration();
 // Actual application ordering: graphics pending retry pump precedes player FrameMove.
 f.win.context.ProcessPendingVideoResolution();
 f.render.UpdateResolution();assert(f.win.context.sets==2 && f.win.GetDisplayGeneration()==pending);
 held.reset();f.win.context.ProcessPendingVideoResolution();
 assert(f.win.context.sets==2 && !f.render.m_appliedResolution);
 f.render.UpdateResolution();assert(f.win.context.sets==3 && f.render.m_appliedResolution);
 assert(f.win.GetDisplayGeneration()==pending);
 for(int i=0;i<60;++i){f.render.UpdateResolution();}
 assert(f.win.context.sets==3 && f.render.latency==1 && f.render.renderer.updates==1);
 f.ready();assert(f.session.Acquire(f.session.Epoch()));
}
void automatic_graphics_retry(){
 Fixture f;std::optional<CAMLSession::Permit> held(f.session.Acquire(f.session.Epoch()));assert(*held);
 auto& gfx=f.win.context;
 assert(!gfx.SetVideoResolution(25,false) && gfx.m_pendingVideoResolution);
 assert(gfx.sets==1 && f.win.display.Pending());
 held.reset();gfx.ProcessPendingVideoResolution();
 assert(gfx.sets==2 && !gfx.m_pendingVideoResolution && !f.win.IsDisplayReadyForVideo());
 gfx.ProcessPendingVideoResolution();assert(gfx.sets==2);
 f.win.Present();assert(f.win.IsDisplayReadyForVideo());
}
void changed_intents(){
 Fixture f;f.render.UpdateResolution();
 f.render.TriggerUpdateResolutionHdr(StreamHdrType::HDR_TYPE_HDR10);f.render.UpdateResolution();
 assert(f.win.context.sets==2 && f.win.context.hdr==StreamHdrType::HDR_TYPE_HDR10);
 std::string stereo="split_horizontal";
 f.render.TriggerUpdateResolution(30.f,1920,1080,stereo);f.render.UpdateResolution();
 assert(f.win.context.sets==3 && f.render.m_appliedResolution->width==1920);
 assert(f.render.m_appliedResolution->videoStereoMode==stereo && f.render.m_appliedResolution->fps==30.f);
 f.win.context.stereo=2;f.render.UpdateResolution();assert(f.win.context.sets==4);
 ++CResolutionUtils::policy;f.render.UpdateResolution();assert(f.win.context.sets==5);
 for(int i=0;i<60;++i){f.render.UpdateResolution();}
 assert(f.win.context.sets==5);
 f.ready();assert(f.win.context.sets==5);
}
void replaced_display(){
 Fixture f;f.render.UpdateResolution();const auto old=f.win.GetDisplayGeneration();
 // A different main-owned display transaction makes old successful work stale,
 // even when its selected resolution and stream metadata are unchanged.
 {CAMLDisplayLifecycle::Mutation changed(f.win.display);assert(changed);
  changed.Finish(CAMLSession::DisplayPhase::WAITING_FOR_RESET);}
 assert(f.win.GetDisplayGeneration()!=old);
 f.render.UpdateResolution();assert(f.win.context.sets==2);
 f.ready();assert(f.win.context.sets==2);
}
void replaced_configuration(){
 Fixture f;f.render.UpdateResolution();
 f.render.ConfigureEntry();assert(!f.render.m_appliedResolution);
 f.render.UpdateResolution();assert(f.win.context.sets==2);
 f.render.UnInitEntry();assert(!f.render.m_appliedResolution);
 f.render.PreInitEntry();assert(!f.render.m_appliedResolution);
 f.render.m_picture.amlDVSession=std::make_shared<int>(2);
 f.render.UpdateResolution();assert(f.win.context.sets==3);f.ready();
}
void deferred_dv(){
 Fixture f;dvPending=true;f.render.UpdateResolution();assert(dvEffects==0);
 f.win.Present();std::optional<CAMLSession::Permit> held(f.session.Acquire(f.session.Epoch()));assert(*held);
 f.render.UpdateResolution();assert(f.render.m_bTriggerUpdateResolution && dvEffects==0);
 assert(f.render.m_deferredDVNative); // awaiting counted native admission
 held.reset();
 dvEffect=[&]{f.render.TriggerUpdateResolutionHdr(StreamHdrType::HDR_TYPE_HDR10);};
 f.render.UpdateResolution();assert(dvEffects==1 && f.win.context.sets==1);
 assert(f.render.m_bTriggerUpdateResolution && !f.render.m_appliedResolution);
 assert(f.render.m_hdrType_override==StreamHdrType::HDR_TYPE_HDR10);
 f.render.UpdateResolution();assert(f.win.context.sets==2);
 f.ready();assert(dvEffects==1 && f.win.context.sets==2);
}
void unchanged_trigger_and_adjust_off(){
 Fixture f;f.render.UpdateResolution();std::string stereo;
 f.render.TriggerUpdateResolution(0.f,0,0,stereo);f.render.UpdateResolution();
 assert(f.win.context.sets==1);CServiceBroker::settings.settings.adjust=ADJUST_REFRESHRATE_OFF;
 f.render.UpdateResolution();assert(f.win.context.sets==1 && f.render.m_bTriggerUpdateResolution);
 f.ready();assert(f.win.context.sets==1);
}
int main(){apply_once();failed_applies();pending_admission();automatic_graphics_retry();changed_intents();replaced_display();
 replaced_configuration();deferred_dv();unchanged_trigger_and_adjust_off();
 std::cout<<"apply once, failures, pending admission, supersession, display/config replacement, DV reentrancy and admission: PASS\n";}
'''

if __name__ == '__main__':
    main()
