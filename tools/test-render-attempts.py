#!/usr/bin/env python3
"""Host behavioral checks of synchronous render attempts and AML presentation.

Extracts the complete Application::Render body, render-target token/result
methods, and AML composite begin/end/cancel and pending-switch presentation
methods, namespace admission and DestroyWindow. GUI, services, FBO binding,
EGL swap and kernel writes are recording
stubs with controlled failure/invalidation callbacks. This verifies owner-side
control flow, not real contexts, GPU completion, physical presentation or device
playback. Generated fixtures run under ASan/UBSan and are temporary.
"""
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    base = (ROOT / 'xbmc/rendering/RenderSystem.h').read_text()
    application = (ROOT / 'xbmc/application/Application.cpp').read_text()
    aml = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    gles = (ROOT / 'xbmc/rendering/gles/RenderSystemGLES.cpp').read_text()
    methods = '\n'.join(function(base, signature) for signature in [
        'RenderTargetToken CaptureRenderTarget()', 'bool IsRenderTargetCurrent(',
        'void InvalidateRenderTarget()', 'PresentResult GetPresentResult()',
        'void ResetPresentResult()'])
    fields = '\n'.join(re.search(pattern, base).group() for pattern in [
        r'const std::shared_ptr<const uint8_t> m_renderTargetIdentity[^\n]+;',
        r'uint64_t m_renderTargetGeneration[^\n]+;'])
    common = COMMON.replace('@METHODS@', methods).replace('@FIELDS@', fields)
    fixtures = {
        'application': common + APPLICATION + '\n' + function(application, 'void CApplication::Render()') + APPLICATION_TESTS,
        'aml': common + AML + '\n' + function(gles, 'bool CRenderSystemGLES::IsTextureContextCurrent(') + '\n' + '\n'.join(function(aml, signature) for signature in [
            'void CWinSystemAmlogicGLESContext::PresentRenderImpl(',
            'void CWinSystemAmlogicGLESContext::QueueKernelSwitch(',
            'void CWinSystemAmlogicGLESContext::ApplyPendingKernelSwitch()',
            'bool CWinSystemAmlogicGLESContext::BeginGuiComposite()',
            'bool CWinSystemAmlogicGLESContext::EndGuiComposite()',
            'void CWinSystemAmlogicGLESContext::CancelGuiComposite()',
            'bool CWinSystemAmlogicGLESContext::DestroyWindow()']) + AML_TESTS,
    }
    with tempfile.TemporaryDirectory(prefix='render-attempts-') as temporary:
        out = Path(temporary)
        for name, source in fixtures.items():
            cpp = out / (name + '.cpp')
            cpp.write_text(source)
            executable = out / name
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                            '-Werror', '-pthread', '-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                            str(cpp), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
    print('Render attempts: PASS (complete production Render and AML composite/presentation; '
          'ASan/UBSan; recording GUI/FBO/EGL/kernel stubs)')


COMMON = r'''
#include "rendering/RenderResource.h"
#include <algorithm>
#include <array>
#include <cassert>
#include <chrono>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>
std::vector<std::string> calls;
std::function<void(const std::string&)> hook;
void record(const std::string& name)
{
  calls.push_back(name);
  if (hook) hook(name);
}
bool called(const std::string& name)
{
  return std::find(calls.begin(), calls.end(), name) != calls.end();
}
void expect(std::initializer_list<const char*> expected)
{
  std::vector<std::string> wanted(expected.begin(), expected.end());
  assert(calls == wanted);
}
class CRenderSystemBase
{
public:
  virtual ~CRenderSystemBase() = default;
  virtual bool CanRender() const {return current;}
  bool current{true};
  PresentResult m_presentResult{PresentResult::NOT_ATTEMPTED};
  @METHODS@
  @FIELDS@
};
'''

APPLICATION = r'''
constexpr int RENDER_STEREO_VIEW_LEFT=1, RENDER_STEREO_VIEW_RIGHT=2,
              RENDER_STEREO_VIEW_OFF=0, RENDER_STEREO_MODE_MONO=1;
struct Renderer : CRenderSystemBase
{
  bool begin{true}, end{true};
  bool BeginRender() {record("begin");return begin;}
  bool EndRender() {record("end");return end;}
} renderer;
struct Gfx
{
  bool fullscreen{false};
  int stereo{0};
  std::optional<PresentResult> outcome{PresentResult::SWAP_ACCEPTED};
  bool flipGui{false}, flipVideo{false};
  bool IsFullScreenVideo() const {return fullscreen;}
  int GetStereoMode() const {return stereo;}
  void SetStereoView(int view) {record(view==1 ? "left" : view==2 ? "right" : "off");}
  void Flip(bool gui, bool video)
  {
    flipGui=gui;flipVideo=video;record("flip");
    if (outcome) renderer.m_presentResult=*outcome;
  }
};
struct CWinSystemBase
{
  Gfx gfx;
  bool begin{true}, end{true};
  Gfx& GetGfxContext() {return gfx;}
  bool BeginGuiComposite() {record("composite-begin");return begin;}
  bool EndGuiComposite() {record("composite-end");return end;}
  void CancelGuiComposite() {record("cancel");}
} window;
struct CApplicationPlayer
{
  bool external{false}, paused{false}, video{true};
  bool IsExternalPlaying() const {return external;}
  bool IsPausedPlayback() const {return paused;}
  bool IsRenderingVideoLayer() const {return video;}
};
struct CApplicationPowerHandling
{
  bool pythonSaver{false}, gui{true};
  bool IsPythonScreenSaverActive() const {return pythonSaver;}
  void ResetScreenSaver() {record("screensaver");}
  bool GetRenderGUI() const {return gui;}
};
struct WindowManager
{
  std::vector<bool> results{true};
  size_t index{0};
  void RenderEx() {record("video");}
  bool Render() {record("gui");assert(index<results.size());return results[index++];}
  void AfterRender() {record("after");}
};
struct InfoProviders
{
  InfoProviders& GetGUIControlsInfoProvider() {return *this;}
  InfoProviders& GetSystemInfoProvider() {return *this;}
  void ResetContainerMovingCache() {record("moving-cache");}
  void UpdateFPS() {record("fps");}
};
struct CGUIInfoManager
{
  InfoProviders providers;
  void ResetCache() {record("cache");}
  InfoProviders& GetInfoProviders() {return providers;}
};
struct GUI
{
  WindowManager manager;
  CGUIInfoManager info;
  WindowManager& GetWindowManager() {return manager;}
  CGUIInfoManager& GetInfoManager() {return info;}
} gui;
struct CServiceBroker
{
  static Renderer* GetRenderSystem() {return &renderer;}
  static CWinSystemBase* GetWinSystem() {return &window;}
  static GUI* GetGUI() {return &gui;}
};
struct CTimeUtils
{
  static inline bool rendered{false};
  static void UpdateFrameTime(bool value) {rendered=value;record("frame-time");}
};
class CApplication
{
public:
  bool m_bStop{false}, m_AppFocused{true}, m_skipGuiRender{false};
  CApplicationPlayer player;
  CApplicationPowerHandling power;
  RenderAttemptResult m_lastRenderAttempt;
  std::chrono::steady_clock::time_point m_lastRenderTime{};
  template<class T> T* GetComponent()
  {
    if constexpr(std::is_same_v<T,CApplicationPlayer>) return &player;
    else return &power;
  }
  void Render();
};
void reset()
{
  hook={};calls.clear();renderer.begin=renderer.end=renderer.current=true;
  renderer.m_presentResult=PresentResult::SWAP_ACCEPTED;
  window=CWinSystemBase{};gui=GUI{};CTimeUtils::rendered=false;
}
void result(const CApplication& app, RenderAttemptStatus status,
            bool rendered=false, PresentResult present=PresentResult::NOT_ATTEMPTED)
{
  assert(app.m_lastRenderAttempt.status==status);
  assert(app.m_lastRenderAttempt.guiRendered==rendered);
  assert(app.m_lastRenderAttempt.present==present);
}
'''

APPLICATION_TESTS = r'''
int main()
{
  using S=RenderAttemptStatus;
  reset();CApplication stopped;stopped.m_bStop=true;
  stopped.m_lastRenderAttempt={S::COMMANDS_COMPLETED,true,PresentResult::SWAP_ACCEPTED};
  stopped.Render();result(stopped,S::STOPPED);expect({});

  reset();CApplication rejected;renderer.begin=false;
  rejected.Render();result(rejected,S::BEGIN_REJECTED);expect({"begin"});

  reset();CApplication normal;
  normal.Render();result(normal,S::COMMANDS_COMPLETED,true,PresentResult::SWAP_ACCEPTED);
  expect({"begin","video","composite-begin","gui","after","composite-end","end",
          "cache","moving-cache","fps","flip","frame-time"});
  assert(window.gfx.flipGui && window.gfx.flipVideo && CTimeUtils::rendered);
  assert(normal.m_lastRenderTime!=std::chrono::steady_clock::time_point{});

  // A successful command pass may contain no GUI work. Disabled GUI and skip
  // still run the video pass and composite scope; the no-op GUI runs AfterRender.
  for (int mode=0;mode<3;++mode)
  {
    reset();CApplication app;app.power.gui=mode!=0;app.m_skipGuiRender=mode==1;
    gui.manager.results={false};window.gfx.outcome=PresentResult::SKIPPED;
    app.Render();result(app,S::COMMANDS_COMPLETED,false,PresentResult::SKIPPED);
    assert(called("video") && called("composite-begin") && called("composite-end"));
    assert(called("gui")== (mode==2) && called("after")== (mode==2));
    assert(!called("fps") && !called("cancel") && !window.gfx.flipGui && !CTimeUtils::rendered);
    assert((app.m_lastRenderTime!=std::chrono::steady_clock::time_point{})==(mode==2));
  }
  for (int stereo : {RENDER_STEREO_MODE_MONO,2})
  {
    reset();CApplication app;window.gfx.stereo=stereo;
    gui.manager.results=stereo==RENDER_STEREO_MODE_MONO ? std::vector<bool>{true} : std::vector<bool>{false,true};
    app.Render();result(app,S::COMMANDS_COMPLETED,true,PresentResult::SWAP_ACCEPTED);
    assert(called("left") && called("off"));
    assert(called("right")== (stereo!=RENDER_STEREO_MODE_MONO));
    assert(gui.manager.index== (stereo==RENDER_STEREO_MODE_MONO ? 1u : 2u));
    if(stereo==RENDER_STEREO_MODE_MONO)
      expect({"begin","video","composite-begin","left","gui","off","after",
              "composite-end","end","cache","moving-cache","fps","flip","frame-time"});
    else
      expect({"begin","video","composite-begin","left","gui","right","gui","off","after",
              "composite-end","end","cache","moving-cache","fps","flip","frame-time"});
  }
  for (bool failBegin : {false,true})
  {
    reset();CApplication app;window.begin=!failBegin;window.end=failBegin;
    app.Render();result(app,S::CANCELLED,!failBegin);
    assert(calls.back()=="cancel" && !called("end") && !called("flip"));
    assert(called("gui")==!failBegin);
  }
  reset();CApplication failedEnd;renderer.end=false;
  failedEnd.Render();result(failedEnd,S::END_REJECTED,true);
  assert(called("composite-end") && !called("cancel") && !called("cache") && !called("flip"));

  // Invalidation and shutdown at each re-entrant service boundary cannot reach
  // flip. Before composite release the cancellation guard must run exactly once.
  for (const std::string point : {"video","gui","after","composite-end","end","cache","moving-cache","fps"})
    for (bool shutdown : {false,true})
    {
      reset();CApplication app;
      hook=[&](const std::string& event) {
        if (event==point) {if(shutdown)app.m_bStop=true;else renderer.InvalidateRenderTarget();}
      };
      app.Render();hook={};result(app,S::CANCELLED,point!="video");
      assert(!called("flip") && !called("frame-time"));
      const bool active=point=="video" || point=="gui" || point=="after";
      assert(std::count(calls.begin(),calls.end(),"cancel")== (active?1:0));
      if(point=="video") assert(!called("composite-begin") && !called("gui"));
      if(point=="composite-end") assert(!called("end") && !called("cache"));
    }
  for (const std::string point : {"video","composite-begin","gui","after","composite-end"})
  {
    reset();CApplication app;
    hook=[&](const std::string& event) {if(event==point)throw std::runtime_error("fixture");};
    bool caught=false;try {app.Render();} catch(const std::runtime_error&) {caught=true;}
    hook={};assert(caught && called("cancel") && !called("flip"));
    assert(app.m_lastRenderAttempt.status==S::CANCELLED);
    assert(app.m_lastRenderAttempt.present==PresentResult::NOT_ATTEMPTED);
  }
  // Prior frame acceptance must never leak into an unreported backend result.
  for (const auto outcome : {PresentResult::SKIPPED,PresentResult::TARGET_INVALID,
                            PresentResult::SWAP_FAILED,PresentResult::SWAP_ACCEPTED,
                            PresentResult::UNREPORTED})
  {
    reset();CApplication app;
    if(outcome==PresentResult::UNREPORTED)window.gfx.outcome.reset();else window.gfx.outcome=outcome;
    app.Render();result(app,S::COMMANDS_COMPLETED,true,outcome);
    assert(called("frame-time"));
    app.m_bStop=true;app.Render();result(app,S::STOPPED);
  }
  // Existing screensaver behavior remains before admission; external unfocused
  // playback, paused playback and the Python saver suppress the reset.
  for(int mode=0;mode<4;++mode)
  {
    reset();CApplication app;window.gfx.fullscreen=true;
    if(mode==1){app.player.external=true;app.m_AppFocused=false;}
    if(mode==2)app.player.paused=true;
    if(mode==3)app.power.pythonSaver=true;
    app.Render();assert(called("screensaver")== (mode==0));
  }
}
'''

AML = r'''
#include "rendering/gles/TextureResources.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "utils/PlaybackEndDiagnostics.h"
namespace fmt {template<class... T>std::string format(const char*,T&&...){return {};}}
struct CEGLContextUtils {struct SwapDiagnostics {bool attempted=false;int error=0;};};
using CCriticalSection=std::recursive_mutex;
constexpr int GL_SCISSOR_TEST=1;
bool scissor{true};
bool glIsEnabled(int) {return scissor;}
void glEnable(int) {scissor=true;record("scissor-on");}
void glDisable(int) {scissor=false;record("scissor-off");}
enum class AMLHDMILinkSample {SKIPPED,UNCHANGED,CHANGED};
AMLHDMILinkSample aml_hdmi_link_probe(const char*) {record("probe");return AMLHDMILinkSample::SKIPPED;}
void aml_hdr10plus_vsif_hold(bool) {record("hdr-release");}
struct IDispResource
{
  virtual ~IDispResource()=default;
  virtual void OnResetDisplay()=0;
};
struct Timer {bool past{false};bool IsTimePast() const {return past;}};
struct Fbo
{
  bool begin{true};
  bool BeginRender() {record("fbo-begin");return begin;}
  void EndRender() {record("fbo-end");}
};
struct Context
{
  bool swap{true};
  bool TrySwapBuffers(CEGLContextUtils::SwapDiagnostics* diagnostics=nullptr) {
    record("swap");if(diagnostics){diagnostics->attempted=true;diagnostics->error=swap?0:1;}return swap;}
  void DestroySurface() {record("destroy-surface");}
};
class CRenderSystemGLES : public CRenderSystemBase
{
public:
  std::shared_ptr<CGLESTextureResources> m_textureResources{std::make_shared<CGLESTextureResources>()};
  std::shared_ptr<CGLESTextureResources> GetTextureResources() const {return m_textureResources;}
  bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>& resources) const;
};
struct CWinSystemAmlogic
{
  std::atomic<bool> m_hdrRefreshPending{false};
  void RefreshHDRCapabilities() {}
  void SetNativeGuiWait(bool enabled) {assert(!enabled);}
  bool DestroyWindow() {record("destroy-window");return true;}
};
class CWinSystemAmlogicGLESContext : public CRenderSystemGLES, public CWinSystemAmlogic
{
public:
  enum class MenuRoute {NONE,ACTIVE};
  struct PendingSwitch {const char* path{nullptr};int value{0};};
  std::array<PendingSwitch,2> m_pendingSwitches;
  bool m_delayDispReset{false},m_guiFboBound{false},m_menuFboHasContent{false},m_menuEngageFailed{false};
  bool composite{true};
  CAMLDisplayLifecycle m_displayLifecycle;
  bool m_displayGeometryReady{false};
  MenuRoute m_menuRoute{MenuRoute::NONE};
  RenderTargetToken m_guiTarget;
  std::shared_ptr<CGLESTextureResources> m_guiResources;
  bool m_guiScissor{false};
  Timer m_dispResetTimer;
  CCriticalSection m_resourceSection;
  std::vector<IDispResource*> m_resources;
  Fbo m_guiFbo;
  Context m_pGLContext;
  std::vector<std::pair<std::string,int>> writes;
  void SetKernelSwitch(const char* path,int value) {record("kernel");writes.emplace_back(path,value);}
  bool CompositeGui() {record("composite");return composite;}
  void ObserveEndDisplay(const char*,bool=false) {}
  void PresentRenderImpl(bool rendered);
  void QueueKernelSwitch(const char* path,int value);
  void ApplyPendingKernelSwitch();
  bool BeginGuiComposite();
  bool EndGuiComposite();
  void CancelGuiComposite();
  bool DestroyWindow();
};
'''

AML_TESTS = r'''
int main()
{
  using W=CWinSystemAmlogicGLESContext;
  using R=W::MenuRoute;
  W window;
  const char* oldRoute="old";const char* newRoute="new";
  window.QueueKernelSwitch(oldRoute,1);
  window.QueueKernelSwitch(oldRoute,0);
  window.QueueKernelSwitch(newRoute,1);
  window.PresentRenderImpl(false);
  assert(window.GetPresentResult()==PresentResult::SKIPPED && window.writes.empty());
  expect({"probe"});calls.clear();
  window.current=false;window.PresentRenderImpl(true);
  assert(window.GetPresentResult()==PresentResult::TARGET_INVALID && window.writes.empty());
  expect({"probe"});calls.clear();
  window.current=true;window.m_pGLContext.swap=false;window.PresentRenderImpl(true);
  assert(window.GetPresentResult()==PresentResult::SWAP_FAILED && window.writes.empty());
  expect({"probe","swap"});calls.clear();
  assert(window.m_pendingSwitches[0].path && window.m_pendingSwitches[1].path);
  window.m_pGLContext.swap=true;window.PresentRenderImpl(true);
  assert(window.GetPresentResult()==PresentResult::SWAP_ACCEPTED);
  expect({"probe","swap","kernel","kernel"});calls.clear();
  assert((window.writes==std::vector<std::pair<std::string,int>>{{"old",0},{"new",1}}));
  assert(!window.m_pendingSwitches[0].path && !window.m_pendingSwitches[1].path);
  window.PresentRenderImpl(true);expect({"probe","swap"});calls.clear();
  assert(window.writes.size()==2);

  // Delayed display reset callbacks precede admission and may destroy readiness.
  struct Reset : IDispResource
  {
    W& window;
    bool generationOnly{false};
    explicit Reset(W& w):window(w){}
    void OnResetDisplay() override
    {
      record("reset-display");
      if(generationOnly)window.InvalidateRenderTarget();else window.current=false;
    }
  } reset(window);
  window.QueueKernelSwitch(newRoute,0);
  window.m_resources={&reset};window.m_delayDispReset=true;window.m_dispResetTimer.past=true;
  window.PresentRenderImpl(true);
  assert(window.GetPresentResult()==PresentResult::TARGET_INVALID && !window.m_delayDispReset);
  expect({"probe","reset-display","hdr-release"});calls.clear();
  assert(window.writes.size()==2 && window.m_pendingSwitches[0].path);
  window.current=true;
  reset.generationOnly=true;window.m_delayDispReset=true;
  const auto previousTarget=window.CaptureRenderTarget();
  window.PresentRenderImpl(true);
  assert(window.CanRender() && !window.IsRenderTargetCurrent(previousTarget));
  assert(window.GetPresentResult()==PresentResult::TARGET_INVALID && !window.m_delayDispReset);
  expect({"probe","reset-display","hdr-release"});calls.clear();
  assert(window.writes.size()==2 && window.m_pendingSwitches[0].path);
  // A fresh attempt captures the new generation and can consume the still-pending switch.
  window.PresentRenderImpl(true);
  assert(window.GetPresentResult()==PresentResult::SWAP_ACCEPTED);
  expect({"probe","swap","kernel"});calls.clear();
  assert(window.writes.back()==std::make_pair(std::string("new"),0));
  assert(!window.m_pendingSwitches[0].path);

  // No-route composition succeeds without binding. Disabled readiness does not.
  assert(window.BeginGuiComposite() && window.EndGuiComposite());expect({});
  window.current=false;
  assert(!window.BeginGuiComposite() && !window.EndGuiComposite());expect({});
  window.current=true;window.m_menuRoute=R::ACTIVE;
  window.m_guiFbo.begin=false;
  assert(!window.BeginGuiComposite() && window.m_menuEngageFailed && !window.m_guiFboBound);
  assert(!window.EndGuiComposite());expect({"fbo-begin"});calls.clear();
  window.m_guiFbo.begin=true;window.m_menuFboHasContent=true;
  assert(window.BeginGuiComposite() && window.m_guiFboBound && !window.m_menuFboHasContent);
  assert(window.EndGuiComposite() && !window.m_guiFboBound);
  expect({"fbo-begin","fbo-end","composite"});calls.clear();

  // Cancellation restores the original scissor and unbinds exactly once even
  // after route/target invalidation when the texture namespace is still current.
  for(bool originalScissor : {false,true})
    for(bool invalidate : {false,true})
    {
      scissor=originalScissor;
      assert(window.BeginGuiComposite());window.m_menuFboHasContent=true;
      const auto resources=window.m_guiResources;
      scissor=!originalScissor;
      if(invalidate)window.InvalidateRenderTarget();
      assert(window.IsTextureContextCurrent(resources));
      window.CancelGuiComposite();window.CancelGuiComposite();
      assert(!window.m_guiFboBound && !window.m_menuFboHasContent && scissor==originalScissor);
      expect({"fbo-begin","fbo-end",originalScissor?"scissor-on":"scissor-off"});calls.clear();
    }
  // EndGuiComposite delegates a stale target to that same namespace-aware
  // cancellation: rejection still cleans up live-context binding/scissor state.
  for(bool originalScissor : {false,true})
  {
    scissor=originalScissor;
    assert(window.BeginGuiComposite());scissor=!originalScissor;
    window.InvalidateRenderTarget();
    assert(!window.EndGuiComposite() && !window.m_guiFboBound && scissor==originalScissor);
    expect({"fbo-begin","fbo-end",originalScissor?"scissor-on":"scissor-off"});calls.clear();
  }
  // Namespace replacement and loss of current-context admission are distinct
  // from a generation-only change: neither permits any GL restoration call.
  for(bool replacement : {false,true})
    for(bool viaEnd : {false,true})
    {
      scissor=false;assert(window.BeginGuiComposite());scissor=true;
      const auto resources=window.m_guiResources;
      if(replacement)
      {
        window.m_textureResources=std::make_shared<CGLESTextureResources>();
        window.InvalidateRenderTarget();
      }
      else window.current=false;
      assert(!window.IsTextureContextCurrent(resources));
      if(viaEnd)assert(!window.EndGuiComposite());else window.CancelGuiComposite();
      assert(!window.m_guiFboBound && !window.m_menuFboHasContent && scissor);
      expect({"fbo-begin"});calls.clear();window.current=true;
    }
  assert(window.BeginGuiComposite());window.composite=false;
  assert(!window.EndGuiComposite() && !window.m_guiFboBound);
  expect({"fbo-begin","fbo-end","composite"});calls.clear();
  window.CancelGuiComposite();expect({});

  // Actual surface teardown invalidates the target first, then cancels while
  // the namespace is usable, before the surface and base window are destroyed.
  W teardown;teardown.m_menuRoute=R::ACTIVE;
  scissor=false;assert(teardown.BeginGuiComposite());scissor=true;
  const auto beforeDestroy=teardown.CaptureRenderTarget();
  assert(teardown.DestroyWindow());
  assert(!teardown.IsRenderTargetCurrent(beforeDestroy) && !teardown.m_guiFboBound && !scissor);
  expect({"fbo-begin","fbo-end","scissor-off","destroy-surface","destroy-window"});calls.clear();

}
'''

if __name__ == '__main__':
    main()
