#!/usr/bin/env python3
"""Production AML window/resolution admission with recording native/EGL stubs.

Executes real CAMLDisplayLifecycle/AMLSession, full AML base and GLES window
creation, SetFullScreen/ResetRenderSystem/Resize/Present, graphics resolution
request/retry methods and RenderManager::UpdateResolution/FrameMove. FrameMove
queue, GUI, capture and clock-sync callees are stubs; its readiness guard is real. Native mode/DV/EGL,
GLES geometry implementation, services and callbacks are controlled stubs.
Direct shutdown/InitWindowSystem are outside this fixture and remain a separate
production dependency. No real display, driver, GL or worker readiness is claimed.
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
    sources = {
        'window': 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp',
        'base': 'xbmc/windowing/amlogic/WinSystemAmlogic.cpp',
        'gfx': 'xbmc/windowing/GraphicContext.cpp',
        'render': 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp',
    }
    signatures = {
        'window': ['bool CWinSystemAmlogicGLESContext::CreateNewWindow(',
                   'bool CWinSystemAmlogicGLESContext::DestroyWindow()',
                   'bool CWinSystemAmlogicGLESContext::SetFullScreen(',
                   'bool CWinSystemAmlogicGLESContext::ResetRenderSystem(',
                   'bool CWinSystemAmlogicGLESContext::ResizeWindow(',
                   'void CWinSystemAmlogicGLESContext::PresentRenderImpl(',
                   'void CWinSystemAmlogicGLESContext::QueueKernelSwitch(',
                   'void CWinSystemAmlogicGLESContext::ApplyPendingKernelSwitch()'],
        'base': ['bool CWinSystemAmlogic::CreateNativeWindow('],
        'gfx': ['bool CGraphicContext::SetVideoResolution(',
                'void CGraphicContext::ProcessPendingVideoResolution()',
                'bool CGraphicContext::SetVideoResolutionInternal('],
        'render': ['void CRenderManager::UpdateResolution(', 'void CRenderManager::FrameMove()'],
    }
    methods = []
    for group, path in sources.items():
        source = (ROOT / path).read_text()
        methods.extend(function(source, signature) for signature in signatures[group])
    header = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.h').read_text()
    query = function(header, 'bool IsDisplayReadyForVideo()').replace(' override', '')
    return PRELUDE.replace('@READY_QUERY@', query) + '\n'.join(methods) + TESTS


def run(source, negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-window-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter', '-pthread', '-fno-pie', '-no-pie', '-I', str(ROOT / 'xbmc'),
                   str(out / 'test.cpp'), '-o', str(out / 'test')]
        if not negative:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        subprocess.run(command, check=True)
        result = subprocess.run([str(out / 'test')], capture_output=negative,
                                text=True, check=not negative, timeout=20)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness()
    if args.negative_controls:
        controls = [
            ('window omits native child',
             'CAMLNativeTransaction native(display.Request());\n  if (!native.TryBegin())\n    return false;',
             '/* no native child */'),
            ('base omits native child',
             'CAMLNativeTransaction native(display);\n  if (!native.TryBegin())\n    return false;',
             '/* no native child */'),
            ('base accepts absent parent', 'if (!display)\n    return false;\n  CAMLNativeTransaction native(display);',
             'CAMLNativeTransaction native(display);'),
            ('window drops exact receipt', 'CreateNativeWindow(name, fullScreen, res, display.Request())',
             'CreateNativeWindow(name, fullScreen, res, {})'),
            ('failed bind reports success', 'if (!m_pGLContext.BindContext())',
             'if (false && !m_pGLContext.BindContext())'),
            ('pending graphics intent lost', 'm_pendingVideoResolution = ResolutionRequest{res, forceUpdate};',
             '(void)ResolutionRequest{res, forceUpdate};'),
            ('render trigger cleared while pending',
             'return; // Retain resolution/HDR intent until actual window completion.',
             '{m_bTriggerUpdateResolution = false;return;}'),
            ('no-mode engage bypasses display readiness',
             'if (!CServiceBroker::GetWinSystem()->IsDisplayReadyForVideo())',
             'if (false && !CServiceBroker::GetWinSystem()->IsDisplayReadyForVideo())'),
            ('stale engage bypasses display readiness',
             'if (!CServiceBroker::GetWinSystem()->IsDisplayReadyForVideo())\n    CancelDeferredDV();',
             'if (false)\n    CancelDeferredDV();'),
            ('failed swap applies kernel switches', 'else\n    m_presentResult = PresentResult::SWAP_FAILED;',
             'else {ApplyPendingKernelSwitch();m_presentResult = PresentResult::SWAP_FAILED;}'),
        ]
        for name, before, after in controls:
            assert before in source, name
            run(source.replace(before, after, 1), negative=True)
            print('Rejected runtime negative control:', name)
    else:
        run(source)
        print('AML window lifecycle: PASS (production window/graphics/renderer methods; '
              'ASan/UBSan; recording native/EGL/GLES/service effects)')


PRELUDE = r'''
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "windowing/amlogic/AMLNativeTransaction.h"
#include "rendering/RenderResource.h"
#include <cassert>
#include <cmath>
#include <functional>
#include <optional>
#include <string>
#include <vector>
#define HAS_LIBAMCODEC 1
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
using RESOLUTION=int;
constexpr int RES_WINDOW=0,RES_DESKTOP=1;
constexpr int LOGDEBUG=0,LOGERROR=1,LOGINFO=2,DV_MODE_OFF=0;
constexpr unsigned D3DPRESENTFLAG_MODEMASK=255;
constexpr int TMSG_SETVIDEORESOLUTION=1,GUI_MSG_NOTIFY_ALL=2,GUI_MSG_WINDOW_RESIZE=3;
enum RENDER_STEREO_MODE{RENDER_STEREO_MODE_OFF,RENDER_STEREO_MODE_HARDWAREBASED,RENDER_STEREO_MODE_UNDEFINED};
constexpr int RENDER_STEREO_VIEW_OFF=0;
enum STEREOSCOPIC_PLAYBACK_MODE{STEREOSCOPIC_PLAYBACK_MODE_OFF,STEREOSCOPIC_PLAYBACK_MODE_ASK};
constexpr int ADJUST_REFRESHRATE_OFF=0;
enum class StreamHdrType{HDR_TYPE_NONE,HDR_TYPE_HDR10,HDR_TYPE_DOLBYVISION};
struct RESOLUTION_INFO{int iWidth{1920},iHeight{1080},iScreenWidth{1920},iScreenHeight{1080},iBlanking{0};float fRefreshRate{60.0f};unsigned dwFlags{0};};
struct CLog{template<class... T>static void Log(T&&...) {}};
struct CStreamDetails{static const char* DynamicRangeToString(StreamHdrType){return "hdr";}};
std::vector<std::string> events;
CAMLSession* probe=nullptr;
void event(const std::string& name){events.push_back(name);}
std::function<void()> nativeScopeCheck;
void native(const std::string& name){if(nativeScopeCheck)nativeScopeCheck();if(probe){assert(probe->DisplayBlocked());assert(!probe->AcquireDecoder());}event(name);}
RESOLUTION_INFO currentNative;
bool nativeRead=true,fracPolicy=false,restoreGui=false,currentBound=true;
int fracValue=0,dvMode=1;
std::function<void(bool)> engageHook;
bool aml_get_native_resolution(RESOLUTION_INFO* info){native("native-read");*info=currentNative;return nativeRead;}
bool aml_has_frac_rate_policy(){return fracPolicy;}
int aml_dv_mode(){return dvMode;}
void aml_dv_wait_for_pipeline(){native("dv-wait");}
bool aml_dv_restore_gui_ipt(const char*){native("restore-ipt");return restoreGui;}
void aml_dv_display_trigger(){native("dv-trigger");}
bool aml_display_mode_changing(const RESOLUTION_INFO& info){return info.iWidth!=currentNative.iWidth||info.iHeight!=currentNative.iHeight||info.fRefreshRate!=currentNative.fRefreshRate;}
void aml_dv_engage_stale_deferred_disc(){event("engage-stale");}
void aml_dv_engage_deferred_disc(bool mode){event(mode?"engage-mode":"engage-no-mode");if(engageHook)engageHook(mode);}
void aml_set_native_resolution(const RESOLUTION_INFO& info,const std::string&,RENDER_STEREO_MODE,bool){native("native-set:"+std::to_string(info.iWidth));currentNative=info;}
void aml_hdr10plus_vsif_hold(bool hold){event(hold?"hdr-hold":"hdr-release");}
void aml_hdmi_link_probe(const char*){event("link-probe");}
struct CSysfsPath{explicit CSysfsPath(const char*){}template<class T>std::optional<T> Get(){return static_cast<T>(fracValue);}};
struct Rect{void SetRect(float,float,float,float){event("scissor");}};
struct CSettings{static constexpr int SETTING_VIDEOPLAYER_STEREOSCOPICPLAYBACKMODE=1,SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE=2;};
struct Settings{
  bool m_fullScreen=true;int delay=0,adjust=1,stereo=0;
  Settings* GetAdvancedSettings(){return this;}Settings* GetSettings(){return this;}
  int GetInt(const char*){return delay;}
  int GetInt(int setting){return setting==CSettings::SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE?adjust:stereo;}
};
Settings settings;
struct Messenger{bool main=true;bool IsProcessThread(){return main;}void SendMsg(int,int,int){event("send-resolution");}}messenger;
struct Input{void SetMouseResolution(int,int,int,int){event("mouse");}}input;
struct CGUIComponent{
  CGUIComponent& GetWindowManager(){return *this;}CGUIComponent& GetStereoscopicsManager(){return *this;}
  void SendMessage(int,int,int,int){event("window-resize");}
  RENDER_STEREO_MODE GetStereoModeByUser(){return RENDER_STEREO_MODE_OFF;}
}gui;
struct CDisplaySettings{
  static CDisplaySettings& GetInstance(){static CDisplaySettings instance;return instance;}
  RESOLUTION_INFO GetResolutionInfo(int res){RESOLUTION_INFO info;if(res==0){info.iWidth=info.iScreenWidth=1280;info.iHeight=info.iScreenHeight=720;}if(res==2){info.iWidth=info.iScreenWidth=2560;info.iHeight=info.iScreenHeight=1440;}if(res==3)info.fRefreshRate=59.94f;return info;}
};
class CWinSystemAmlogicGLESContext;
CWinSystemAmlogicGLESContext* window=nullptr;
struct CServiceBroker{
  static CWinSystemAmlogicGLESContext* GetWinSystem(){return window;}
  static Settings* GetSettingsComponent(){return &settings;}
  static Messenger* GetAppMessenger(){return &messenger;}
  static Input& GetInputManager(){return input;}
  static CGUIComponent* GetGUI(){return &gui;}
};
class CGraphicContext:public CCriticalSection{
public:
  struct ResolutionRequest{RESOLUTION resolution;bool forceUpdate;};
  std::optional<ResolutionRequest> m_pendingVideoResolution;
  RESOLUTION m_Resolution{RES_DESKTOP};bool m_bFullScreenRoot{true};
  int m_iScreenWidth{1920},m_iScreenHeight{1080};float m_fFPSOverride{0};Rect m_scissors;
  StreamHdrType hdr{StreamHdrType::HDR_TYPE_NONE};RENDER_STEREO_MODE stereo{RENDER_STEREO_MODE_OFF};
  bool SetVideoResolution(RESOLUTION,bool);bool SetVideoResolutionInternal(RESOLUTION,bool);
  void ProcessPendingVideoResolution();
  bool IsValidResolution(int res){return res>=0&&res<=3;}
  void UpdateInternalStateWithResolution(int res){m_Resolution=res;auto info=CDisplaySettings::GetInstance().GetResolutionInfo(res);m_iScreenWidth=info.iWidth;m_iScreenHeight=info.iHeight;m_fFPSOverride=info.fRefreshRate;event("provisional:"+std::to_string(res));}
  RENDER_STEREO_MODE GetStereoMode(){return stereo;}StreamHdrType GetHDRType(){return hdr;}
  void SetHDRType(StreamHdrType type){hdr=type;event("set-hdr");}
  void SetStereoView(int){event("stereo-view");}
  bool IsFullScreenVideo(){return true;}bool IsFullScreenRoot(){return m_bFullScreenRoot;}
};
CGraphicContext* graphics=nullptr;
struct IDispResource{
  virtual ~IDispResource()=default;
  virtual void OnLostDisplay(){native("lost");}
  virtual void OnResetDisplay(){native("reset-callback");}
};
struct Timer{bool past=false;void Set(std::chrono::milliseconds){past=false;}bool IsTimePast(){return past;}};
struct fbdev_window{int width{},height{};};
using EGLNativeWindowType=fbdev_window*;
struct EGL{
  bool surfaceOk=true,bindOk=true,swapOk=true;
  bool CreateSurface(EGLNativeWindowType){native("surface-create");return surfaceOk;}
  bool BindContext(){native("bind");currentBound=bindOk;return bindOk;}
  void DestroySurface(){native("surface-destroy");currentBound=false;}
  bool TrySwapBuffers(){event("swap");return swapOk;}
};
class CWinSystemAmlogic{
public:
  virtual ~CWinSystemAmlogic(){delete m_nativeWindow;}
  bool CreateNativeWindow(const std::string&,bool,RESOLUTION_INFO&,CAMLSession::DisplayRequest);
  bool DestroyWindow(){event("base-destroy");m_bWindowCreated=false;return true;}
  int m_nWidth{1920},m_nHeight{1080};float m_fRefreshRate{60};
  fbdev_window* m_nativeWindow{nullptr};
  bool m_delayDispReset{false},m_force_mode_switch{false},m_bWindowCreated{true},m_bFullScreen{true};
  Timer m_dispResetTimer;CCriticalSection m_resourceSection;std::vector<IDispResource*> m_resources;
  RENDER_STEREO_MODE m_stereo_mode{RENDER_STEREO_MODE_OFF};std::string m_framebuffer_name="fb0";
};
class CRenderSystemGLES{
public:
  bool geometryOk=true;
  bool ResetRenderSystem(int,int){native("geometry");return geometryOk&&currentBound;}
};
class CWinSystemAmlogicGLESContext:public CWinSystemAmlogic,public CRenderSystemGLES{
public:
  CAMLDisplayLifecycle m_displayLifecycle;bool m_displayGeometryReady{true};bool m_shutdownRequested=false;
  EGL m_pGLContext;StreamHdrType m_hdrType{StreamHdrType::HDR_TYPE_NONE};
  uint64_t target{1};PresentResult m_presentResult{PresentResult::NOT_ATTEMPTED};
  struct PendingSwitch{const char* path{nullptr};int value{0};};PendingSwitch m_pendingSwitches[2];
  CGraphicContext& GetGfxContext(){return *graphics;}
  bool CanRender()const{return currentBound;}
  bool IsPrimaryContextCurrent(){return currentBound;}
  uint64_t CaptureRenderTarget(){return target;}
  bool IsRenderTargetCurrent(uint64_t token){return CanRender()&&token==target;}
  void InvalidateRenderTarget(){++target;event("invalidate");}
  void CancelGuiComposite(){event("cancel-composite");}
  bool IsDisplayChangePending(){return m_displayLifecycle.Pending();}
  @READY_QUERY@
  bool CreateNewWindow(const std::string&,bool,RESOLUTION_INFO&);bool DestroyWindow();
  bool SetFullScreen(bool,RESOLUTION_INFO&,bool);bool ResetRenderSystem(int,int);
  bool ResizeWindow(int,int,int,int);void PresentRenderImpl(bool);
  void QueueKernelSwitch(const char*,int);void ApplyPendingKernelSwitch();
};
void SetKernelSwitch(const char* path,int value){event(std::string("kernel:")+path+":"+std::to_string(value));}
struct CResolutionUtils{static inline int chosen=2;static RESOLUTION ChooseBestResolution(float,int,int,bool){return chosen;}};
class CRenderManager{
public:
  CCriticalSection m_resolutionlock,m_statelock;bool m_bTriggerUpdateResolution{true};
  bool m_configuredFramePending{false};
  enum {STATE_UNCONFIGURED,STATE_CONFIGURING,STATE_CONFIGURED};
  int m_renderState{STATE_CONFIGURED};
  void ClearFrameSelection(){event("clear-selection");}
  bool m_deferredDVResolutionAttempted{false};
  bool m_deferredDVNative{false};
  void CancelDeferredDV(){}
  bool ContinueDeferredDV(bool stale){
    if(stale)aml_dv_engage_stale_deferred_disc();
    else {m_deferredDVResolutionAttempted=true;m_bTriggerUpdateResolution=false;m_hdrType_override=StreamHdrType::HDR_TYPE_NONE;aml_dv_engage_deferred_disc(false);}
    return true;
  }
  void FrameWait(std::chrono::milliseconds){event("frame-wait");}
  void CheckEnableClockSync(){}
  void ProcessPresentationQueue(){}
  void UpdateGuiPresentationState(bool){}
  void ManageCaptures(){}
  void FrameMove();
  StreamHdrType m_hdrType_override{StreamHdrType::HDR_TYPE_HDR10};float m_fps{24};
  struct{StreamHdrType hdrType{StreamHdrType::HDR_TYPE_NONE};int iWidth{1920},iHeight{1080};std::string stereoMode;}m_picture;
  struct Renderer{void Update(){event("renderer-update");}}renderer;Renderer* m_pRenderer{&renderer};
  struct Port{void VideoParamsChange(){event("params");}}port;Port* m_playerPort{&port};
  void UpdateLatencyTweak(){event("latency");}void UpdateResolution(bool force=false);
};
'''
TESTS = r'''
struct Fixture{
  CAMLSession session;CGraphicContext gfx;CWinSystemAmlogicGLESContext win;IDispResource resource;
  Fixture(){auto open=session.Fence();assert(session.BeginMutation(open)&&session.Complete(open,true));probe=&session;window=&win;graphics=&gfx;settings=Settings{};messenger.main=true;currentNative=RESOLUTION_INFO{};currentBound=true;nativeRead=true;restoreGui=false;fracPolicy=false;fracValue=0;dvMode=1;engageHook={};events.clear();win.m_resources.push_back(&resource);}
  ~Fixture(){probe=nullptr;engageHook={};assert(!session.DisplayBlocked());}
  void Repair(){win.m_pGLContext.bindOk=true;win.m_pGLContext.surfaceOk=true;win.geometryOk=true;win.m_delayDispReset=false;currentBound=true;assert(win.ResetRenderSystem(1920,1080));}
};
size_t at(const std::string& name){auto it=std::find(events.begin(),events.end(),name);assert(it!=events.end());return it-events.begin();}
bool has(const std::string& name){return std::find(events.begin(),events.end(),name)!=events.end();}
void ExactNativeParents(){
  Fixture f;auto info=CDisplaySettings::GetInstance().GetResolutionInfo(2);
  assert(!f.win.CreateNativeWindow("",true,info,{}));assert(events.empty());
  CAMLSession::DisplayRequest old;
  {
    CAMLDisplayLifecycle::Mutation parent(f.win.m_displayLifecycle);assert(parent);old=parent.Request();
    auto stale=old;++stale.serial;
    assert(!f.win.CreateNativeWindow("",true,info,stale));assert(events.empty());
    std::thread foreign([&]{assert(!f.win.CreateNativeWindow("",true,info,old));});foreign.join();
    assert(events.empty());
    nativeScopeCheck=[&]{assert(!CAMLSession::EndDisplay(old,CAMLSession::DisplayPhase::READY));};
    assert(f.win.CreateNativeWindow("",true,info,old));
    nativeScopeCheck={};parent.Finish(CAMLDisplayLifecycle::Phase::READY);
  }
  assert(!f.session.DisplayBlocked());events.clear();
  assert(!f.win.CreateNativeWindow("",true,info,old));assert(events.empty());
  {
    CAMLDisplayLifecycle::Mutation parent(f.win.m_displayLifecycle);assert(parent);
    nativeScopeCheck=[&]{assert(!CAMLSession::EndDisplay(parent.Request(),CAMLSession::DisplayPhase::READY));};
    assert(f.win.CreateNewWindow("",true,info));
    nativeScopeCheck={};parent.Finish(CAMLDisplayLifecycle::Phase::READY);
  }
  assert(!f.session.DisplayBlocked());
  {
    CAMLDisplayLifecycle::Mutation parent(f.win.m_displayLifecycle);assert(parent);
    nativeScopeCheck=[] {throw 7;};
    try{f.win.CreateNewWindow("",true,info);assert(false);}catch(int){}
    nativeScopeCheck={};parent.Finish(CAMLDisplayLifecycle::Phase::READY);
  }
  assert(!f.session.DisplayBlocked()&&f.session.AcquireDecoder());
  events.clear();
  {
    CAMLNativeTransaction unrelated;assert(unrelated.TryBegin());
    assert(!f.gfx.SetVideoResolution(1,false));
    assert(f.gfx.m_pendingVideoResolution&&!has("native-read")&&!has("restore-ipt"));
  }
  f.gfx.ProcessPendingVideoResolution();
  assert(!f.gfx.m_pendingVideoResolution&&!f.session.DisplayBlocked());
}
void PendingAndLatestPayload(){
  Fixture f;const auto epoch=f.session.Epoch();
  {
    auto decoder=f.session.AcquireDecoder();assert(decoder);
    assert(!f.gfx.SetVideoResolution(2,false));
    assert(f.gfx.m_pendingVideoResolution&&f.gfx.m_pendingVideoResolution->resolution==2);
    assert(f.gfx.m_Resolution==1&&f.gfx.m_iScreenWidth==1920&&f.gfx.m_bFullScreenRoot);
    assert(!has("native-read")&&!has("surface-destroy")&&!has("restore-ipt"));
    f.gfx.ProcessPendingVideoResolution();assert(f.gfx.m_pendingVideoResolution);
    assert(!f.gfx.SetVideoResolution(0,true));
    assert(f.gfx.m_pendingVideoResolution->resolution==0&&f.gfx.m_pendingVideoResolution->forceUpdate);
    assert(settings.m_fullScreen&&f.gfx.m_bFullScreenRoot); // provisional rollback
  }
  CAMLSession newSession;auto open=newSession.Fence();assert(newSession.BeginMutation(open)&&newSession.Complete(open,true));
  assert(newSession.DisplayBlocked()&&!newSession.AcquireDecoder());
  events.clear();f.gfx.ProcessPendingVideoResolution();
  assert(!f.gfx.m_pendingVideoResolution&&f.gfx.m_Resolution==0&&f.gfx.m_iScreenWidth==1280);
  assert(!settings.m_fullScreen&&!f.gfx.m_bFullScreenRoot);
  assert(at("surface-destroy")<at("native-set:1280")&&at("bind")<at("geometry"));
  assert(!f.session.DisplayBlocked()&&!newSession.DisplayBlocked()&&f.session.Epoch()==epoch);
  assert(!has("native-set:2560"));
}
void OwnCaptureAndDirectWindow(){
  Fixture f;auto info=CDisplaySettings::GetInstance().GetResolutionInfo(2);
  {
    auto capture=f.session.Acquire(f.session.Epoch());
    assert(!f.win.CreateNewWindow("",true,info));assert(f.win.IsDisplayChangePending());
    assert(events.empty()); // own permit yields Pending; no self-wait/native effects
  }
  assert(f.win.CreateNewWindow("",true,info));assert(f.session.DisplayBlocked());
  assert(!f.win.m_displayGeometryReady);f.win.PresentRenderImpl(false);assert(f.session.DisplayBlocked());
  assert(f.win.ResizeWindow(info.iWidth,info.iHeight,-1,-1));assert(!f.session.DisplayBlocked());
}
void FailedRebind(){
  Fixture f;f.win.m_pGLContext.bindOk=false;
  assert(!f.gfx.SetVideoResolution(2,false));
  assert(f.session.DisplayBlocked()&&!f.gfx.m_pendingVideoResolution);
  assert(f.gfx.m_Resolution==1&&f.gfx.m_iScreenWidth==1920);
  f.win.PresentRenderImpl(false);assert(f.session.DisplayBlocked());
  f.win.m_pGLContext.bindOk=true;
  assert(f.gfx.SetVideoResolution(2,false));assert(!f.session.DisplayBlocked());
  assert(f.gfx.m_Resolution==2&&at("geometry")>at("bind"));
}
void FailedGeometry(){
  Fixture f;f.win.geometryOk=false;
  assert(!f.gfx.SetVideoResolution(2,false));assert(f.session.DisplayBlocked());
  f.win.PresentRenderImpl(false);assert(f.session.DisplayBlocked());
  f.Repair();assert(!f.session.DisplayBlocked());
}
void DelayedResetAndSwap(){
  Fixture f;settings.delay=2;
  assert(f.gfx.SetVideoResolution(2,false));assert(f.win.m_displayGeometryReady);
  assert(f.session.DisplayBlocked()&&f.win.m_delayDispReset);
  f.win.QueueKernelSwitch("osd",1);f.win.m_pGLContext.swapOk=false;
  events.clear();f.win.PresentRenderImpl(true);
  assert(f.session.DisplayBlocked()&&f.win.m_presentResult==PresentResult::SWAP_FAILED);
  assert(has("swap")&&!has("kernel:osd:1"));
  f.win.m_dispResetTimer.past=true;events.clear();f.win.PresentRenderImpl(false);
  assert(!f.session.DisplayBlocked()&&!f.win.m_delayDispReset);
  assert(has("reset-callback")&&has("hdr-release")&&!has("swap")&&!has("kernel:osd:1"));
  f.win.m_pGLContext.swapOk=true;events.clear();f.win.PresentRenderImpl(true);
  assert(f.win.m_presentResult==PresentResult::SWAP_ACCEPTED&&at("swap")<at("kernel:osd:1"));
}
void RenderIntentAndEngage(){
  Fixture f;CRenderManager renderer;const auto epoch=f.session.Epoch();
  {
    auto active=f.session.AcquireDecoder();renderer.UpdateResolution(true);
    assert(renderer.m_bTriggerUpdateResolution&&renderer.m_hdrType_override==StreamHdrType::HDR_TYPE_HDR10);
    assert(!has("latency")&&!has("renderer-update")&&!has("engage-no-mode")&&!has("params"));
  }
  engageHook=[&](bool mode){if(!mode){assert(!renderer.m_bTriggerUpdateResolution);assert(renderer.m_hdrType_override==StreamHdrType::HDR_TYPE_NONE);renderer.m_bTriggerUpdateResolution=true;renderer.m_hdrType_override=StreamHdrType::HDR_TYPE_DOLBYVISION;}};
  events.clear();renderer.UpdateResolution(true);
  assert(at("engage-mode")<at("native-set:2560"));
  assert(at("geometry")<at("renderer-update")&&at("renderer-update")<at("engage-no-mode"));
  assert(renderer.m_bTriggerUpdateResolution&&renderer.m_hdrType_override==StreamHdrType::HDR_TYPE_DOLBYVISION);
  assert(f.session.Epoch()==epoch);
  engageHook={};settings.adjust=0;events.clear();renderer.UpdateResolution(true);
  assert(!renderer.m_bTriggerUpdateResolution&&!has("native-read")&&has("engage-no-mode"));
}
void NoModeAndForcedFrac(){
  Fixture f;restoreGui=true;
  assert(f.gfx.SetVideoResolution(1,false));
  assert(at("restore-ipt")<at("dv-trigger")&&has("geometry")&&!has("surface-destroy"));
  assert(!f.session.DisplayBlocked());
  restoreGui=false;fracPolicy=true;fracValue=1;events.clear();
  assert(f.gfx.SetVideoResolution(1,false));
  assert(at("engage-mode")<at("native-set:1920"));assert(!f.win.m_force_mode_switch);
  assert(!f.session.DisplayBlocked());
}
void NoModeNeedsReadiness(){
  Fixture f;CRenderManager renderer;settings.adjust=0;
  {
    auto active=f.session.AcquireDecoder();auto info=CDisplaySettings::GetInstance().GetResolutionInfo(2);
    assert(!f.win.SetFullScreen(true,info,false));
    assert(!f.win.IsDisplayReadyForVideo());events.clear();renderer.FrameMove();
    assert(renderer.m_bTriggerUpdateResolution&&renderer.m_hdrType_override==StreamHdrType::HDR_TYPE_HDR10);
    assert(has("clear-selection")&&!has("engage-no-mode")&&!has("engage-stale"));
  }
  settings.delay=2;assert(f.gfx.SetVideoResolution(2,false));
  assert(!f.win.IsDisplayReadyForVideo());events.clear();renderer.FrameMove();
  assert(renderer.m_bTriggerUpdateResolution&&!has("engage-no-mode")&&!has("engage-stale"));
  f.win.m_dispResetTimer.past=true;f.win.PresentRenderImpl(false);
  assert(f.win.IsDisplayReadyForVideo());events.clear();renderer.FrameMove();
  assert(!renderer.m_bTriggerUpdateResolution&&has("engage-no-mode")&&has("engage-stale"));
  assert(at("engage-no-mode")<at("engage-stale"));
  settings.delay=0;f.win.m_pGLContext.bindOk=false;
  assert(!f.gfx.SetVideoResolution(1,false));renderer.m_bTriggerUpdateResolution=true;
  renderer.m_hdrType_override=StreamHdrType::HDR_TYPE_DOLBYVISION;
  events.clear();renderer.FrameMove();
  assert(renderer.m_bTriggerUpdateResolution&&renderer.m_hdrType_override==StreamHdrType::HDR_TYPE_DOLBYVISION);
  assert(!has("engage-no-mode")&&!has("engage-stale"));
  f.win.m_pGLContext.bindOk=true;assert(f.gfx.SetVideoResolution(1,false));
  events.clear();renderer.FrameMove();
  assert(!renderer.m_bTriggerUpdateResolution&&has("engage-no-mode")&&has("engage-stale"));
}
void OffMainDispatch(){
  Fixture f;messenger.main=false;assert(!f.gfx.SetVideoResolution(2,false));
  assert(events==std::vector<std::string>{"send-resolution"});assert(!f.session.DisplayBlocked());
}
int main(){ExactNativeParents();PendingAndLatestPayload();OwnCaptureAndDirectWindow();FailedRebind();FailedGeometry();DelayedResetAndSwap();RenderIntentAndEngage();NoModeAndForcedFrac();NoModeNeedsReadiness();OffMainDispatch();}
'''

if __name__ == '__main__':
    main()
