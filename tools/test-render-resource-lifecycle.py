#!/usr/bin/env python3
"""Production AML lifecycle/route methods with recording EGL/GL/resource stubs.

Executes primary-context proof, surface destruction, composite release, normal
and direct/failed-startup teardown, FBO replacement, and route/transfer changes.
Uses real target tokens/texture registry and extracted GLES teardown/admission.
EGL binding, FBO/shader allocation, route inputs and base presentation are stubs.
CreateNewWindow is checked structurally at destroy/bind/invalidate/callback sites;
its mode-switch/DV logic is not executed. SetFullScreen uses a controlled window
creation result; InitRenderSystem executes its production prefix up to the first
GL bootstrap call, where a recording stub replaces successful initialization.
No real EGL/GPU/device claim is made.
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
    aml = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    gles = (ROOT / 'xbmc/rendering/gles/RenderSystemGLES.cpp').read_text()
    base = (ROOT / 'xbmc/rendering/RenderSystem.h').read_text()
    create = function(aml, 'bool CWinSystemAmlogicGLESContext::CreateNewWindow(')
    # This is only wiring coverage, not execution of mode-switch/DV callbacks.
    destroy = create.index('if (!DestroyWindow())')
    surface = create.index('if (!m_pGLContext.CreateSurface(')
    bind = create.index('if (!m_pGLContext.BindContext())')
    invalidation = create.index('InvalidateRenderTarget();')
    reset = create.index('(*i)->OnResetDisplay();')
    assert destroy < surface < bind < invalidation < reset
    assert 'return false;' in create[surface:bind]
    assert 'return false;' in create[bind:invalidation]
    init = function(gles, 'bool CRenderSystemGLES::InitRenderSystem()')
    # Failed bootstrap must not leave the previous shader regime for later use.
    assert init.index('if (!IsPrimaryContextCurrent())') < init.index('shader.second->Abandon();')
    assert init.index('shader.second->Abandon();') < init.index('ReleaseShaders();')
    assert init.index('ReleaseShaders();') < init.index('CloseTextureResources();')
    assert init.index('CloseTextureResources();') < init.index('glGetIntegerv(')
    methods = '\n'.join(function(base, signature) for signature in [
        'RenderTargetToken CaptureRenderTarget()', 'bool IsRenderTargetCurrent(',
        'void InvalidateRenderTarget()'])
    fields = '\n'.join(re.search(pattern, base).group() for pattern in [
        r'const std::shared_ptr<const uint8_t> m_renderTargetIdentity[^\n]+;',
        r'uint64_t m_renderTargetGeneration[^\n]+;'])
    source = PRELUDE.replace('@BASE_METHODS@', methods).replace('@BASE_FIELDS@', fields)
    # Execute the real failure path; successful GL initialization is a stub boundary.
    source += '\n' + init[:init.index('  GLint maxTextureSize;')]
    source += '  gpu("bootstrap"); return true;\n}\n'
    for signature in ['bool CRenderSystemGLES::CanRender()',
                      'bool CRenderSystemGLES::IsTextureContextCurrent(',
                      'bool CRenderSystemGLES::ResetRenderSystem(',
                      'void CRenderSystemGLES::DrainTextureResources()',
                      'void CRenderSystemGLES::CloseTextureResources()',
                      'bool CRenderSystemGLES::DestroyRenderSystem()']:
        source += '\n' + function(gles, signature)
    for signature in ['bool CWinSystemAmlogicGLESContext::IsPrimaryContextCurrent()',
                      'void CWinSystemAmlogicGLESContext::ReleaseCompositeResources()',
                      'bool CWinSystemAmlogicGLESContext::DestroyRenderSystem()',
                      'bool CWinSystemAmlogicGLESContext::DestroyWindowSystem()',
                      'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()',
                      'bool CWinSystemAmlogicGLESContext::DestroyWindow()',
                      'void CWinSystemAmlogicGLESContext::CancelGuiComposite()',
                      'bool CWinSystemAmlogicGLESContext::SetFullScreen(',
                      'bool CWinSystemAmlogicGLESContext::ResizeWindow(',
                      'bool CWinSystemAmlogicGLESContext::ResetRenderSystem(',
                      'bool CWinSystemAmlogicGLESContext::EngageMenuComposite(',
                      'void CWinSystemAmlogicGLESContext::DisengageMenuComposite()',
                      'bool CWinSystemAmlogicGLESContext::BeginRender()',
                      'bool CWinSystemAmlogicGLESContext::EnsureFbo(',
                      'bool CWinSystemAmlogicGLESContext::EnsureCompositeFbos()']:
        source += '\n' + function(aml, signature)
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='render-resource-lifecycle-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Render resource lifecycle: PASS (production AML teardown/FBO/route/transfer; '
          'ASan/UBSan; recording EGL/GL; CreateNewWindow wiring only)')


PRELUDE = r'''
#include "rendering/RenderResource.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include "rendering/gles/TextureResources.h"
#include <algorithm>
#include <cassert>
#include <chrono>
#include <map>
#include <string>
#include <vector>
using GLuint = uint32_t;
constexpr int EGL_NO_DISPLAY=0, EGL_NO_CONTEXT=0, EGL_NO_SURFACE=0, EGL_DRAW=1;
constexpr int GL_TEXTURE_2D=1, GL_RGBA=2, GL_SCISSOR_TEST=3, GL_COLOR_BUFFER_BIT=4;
constexpr int GL_SRC_ALPHA=5, GL_ONE=6, GL_BLEND=7;
constexpr int LOGERROR=1, LOGINFO=2, AVCOL_TRC_SMPTE2084=16;
const char* DV_GRAPHIC_PQ="dv";
const char* OSD_PQ_PASSTHROUGH="osd";
const char* DV_OUTPUT_MODE="mode";
constexpr auto MENU_RELEASE_DELAY=std::chrono::milliseconds(500);
constexpr auto ROUTE_INPUTS_REFRESH=std::chrono::milliseconds(250);
constexpr auto OSD_ROUTE_FIRST_SETTLE=std::chrono::milliseconds(250);
constexpr auto MENU_ROUTE_SETTLE=std::chrono::milliseconds(500);
int currentDisplay=1, currentContext=1, currentSurface=1;
std::vector<std::string> events;
int eglGetCurrentDisplay(){return currentDisplay;}
int eglGetCurrentContext(){return currentContext;}
int eglGetCurrentSurface(int){return currentSurface;}
void gpu(const std::string& name){assert(currentContext && currentSurface);events.push_back(name);}
void glDeleteTextures(int count,const GLuint*){assert(count==1);gpu("delete texture");}
void glFinish(){gpu("finish");}
void glDisable(int){gpu("disable");}
void glEnable(int){gpu("enable");}
void glClearColor(float,float,float,float){gpu("clear color");}
void glClear(int){gpu("clear");}
void glBlendFunc(int,int){gpu("blend");}
struct CRect{CRect(int,int,int,int){}};
struct RESOLUTION_INFO{int iWidth=1280,iHeight=720;};
struct Matrix
{
  void Clear(){gpu("matrix clear");}void Load(){gpu("matrix load");}
  void LoadIdentity(){gpu("matrix identity");}
  void Ortho(float,float,float,float,float,float){gpu("ortho");}
  Matrix* operator->(){return this;}
} glMatrixProject,glMatrixModview,glMatrixTexture;
struct CLog{template<class... T>static void Log(int,const char*,T&&...) {}};
bool aml_is_dv_enable(){return true;}
unsigned int aml_dv_video_processor_mode(){return 1;}
unsigned int ReadUint(const char*){return 1;}
bool KernelSwitchAvailable(const char*){return true;}
bool IsFullscreenVideoActive(){return true;}
void MarkGuiDirty(){events.push_back("dirty");}
struct Gfx{int GetViewWindow(){return 0;}};
struct Window{Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct CServiceBroker{static Window* GetWinSystem(){static Window window;return &window;}};
using CDirtyRegion=int;
using CDirtyRegionList=std::vector<int>;
struct CEGLContextUtils
{
  int display=1,context=1,surface=1;bool bindSucceeds=true;
  int GetEGLDisplay()const{return display;}
  int GetEGLContext()const{return context;}
  int GetEGLSurface()const{return surface;}
  void DestroySurface(){events.push_back("unbind");currentSurface=0;currentContext=0;surface=0;}
  bool BindContext(){if(!bindSucceeds)return false;currentContext=context;currentSurface=surface;return true;}
  void DestroyContext(){events.push_back("destroy context");currentContext=0;context=0;}
  void Destroy(){events.push_back("destroy egl");currentSurface=0;surface=0;}
};
struct CFrameBufferObject
{
  bool valid=false,bound=false,initSucceeds=true,createSucceeds=true;
  bool IsValid()const{return valid;}bool IsBound()const{return bound;}
  void Cleanup(){if(valid)gpu("delete fbo");valid=bound=false;}
  void Abandon(){events.push_back("abandon fbo");valid=bound=false;}
  bool Initialize(){gpu("initialize fbo");valid=initSucceeds;return valid;}
  bool CreateAndBindToTexture(int,int,int,int){gpu("allocate fbo");bound=createSucceeds;return bound;}
  bool BeginRender(){gpu("begin fbo");return valid&&bound;}
  void EndRender(){gpu("end fbo");}
};
struct CGuiCompositeShaderGLES
{
  struct GuiTransfer
  {
    float white=1,gamma=2.2f,blackLift=0,inputScale=1;
    bool operator==(const GuiTransfer& o)const
    {return white==o.white&&gamma==o.gamma&&blackLift==o.blackLift&&inputScale==o.inputScale;}
  };
  bool live=false,compileSucceeds=true,lutSucceeds=true;GuiTransfer transfer;
  explicit CGuiCompositeShaderGLES(const std::string& = ""){}
  ~CGuiCompositeShaderGLES(){if(live)gpu("delete shader");}
  void Abandon(){events.push_back("abandon shader");live=false;}
  bool CompileAndLink(){gpu("compile shader");live=compileSucceeds;return live;}
  void SetGuiTransfer(const GuiTransfer& value){transfer=value;}
  bool CreateLUTs(int){gpu("create luts");return lutSucceeds;}
};
class CRenderSystemBase
{
public:
  virtual ~CRenderSystemBase()=default;
  virtual bool CanRender()const{return m_bRenderCreated;}
  bool m_bRenderCreated=true;
  @BASE_METHODS@
  @BASE_FIELDS@
};
class CRenderSystemGLES:public CRenderSystemBase
{
public:
  virtual bool IsPrimaryContextCurrent()const=0;
  bool CanRender()const override;
  void DrainTextureResources();void CloseTextureResources();
  bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>&)const;
  bool InitRenderSystem();bool ResetRenderSystem(int,int);
  int m_width=1920,m_height=1080;
  void CalculateMaxTexturesize(){gpu("calculate texture size");}
  void SetViewPort(const CRect&){gpu("set viewport");}
  bool DestroyRenderSystem();
  bool BeginRender(){events.push_back("base begin");return CanRender();}
  void ResetScissors(){gpu("reset scissors");}
  void ClearBuffers(int){gpu("clear buffers");}
  void PresentRenderImpl(bool){gpu("present");}
  void ReleaseShaders(){m_pShader.clear();}
  std::map<int,std::unique_ptr<CGuiCompositeShaderGLES>> m_pShader;
  std::shared_ptr<CGLESTextureResources> m_textureResources=std::make_shared<CGLESTextureResources>();
};
class CWinSystemAmlogic
{
public:
  bool DestroyWindow(){events.push_back("destroy native window");return true;}
  bool DestroyWindowSystem(){events.push_back("destroy native system");return true;}
};
class CWinSystemAmlogicGLESContext:public CWinSystemAmlogic,public CRenderSystemGLES
{
public:
  enum class MenuRoute{NONE,OSD_VPP,DV_CORE2};
  struct RouteInputs{bool dvEnable=false;unsigned int dvVideoProcessor=0,dvOutputMode=0;
    bool dvSwitch=false,osdSwitch=false;};
  bool IsPrimaryContextCurrent()const override;
  void ReleaseCompositeResources();bool DestroyRenderSystem();bool DestroyWindowSystem();
  bool DestroyWindow();bool EngageMenuComposite(MenuRoute);void DisengageMenuComposite();
  void CancelGuiComposite();bool SetFullScreen(bool,RESOLUTION_INFO&,bool);
  bool ResizeWindow(int,int,int,int);
  bool ResetRenderSystem(int,int);
  bool PrepareForShutdown();bool RetireNativeTransactions(){return true;}
  bool m_shutdownRequested=false;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
  CAMLDisplayLifecycle m_displayLifecycle;
  bool m_displayGeometryReady=false,m_delayDispReset=false;
  bool createWindowSucceeds=true;
  bool CreateNewWindow(const std::string&,bool,RESOLUTION_INFO&)
  {events.push_back("create window");return createWindowSucceeds;}
  bool m_guiScissor=false;
  std::shared_ptr<CGLESTextureResources> m_guiResources;
  bool BeginRender();bool EnsureFbo(CFrameBufferObject&,int&,int&);bool EnsureCompositeFbos();
  bool UseLimitedColor()const{return false;}
  void RequestMenuComposite(bool value){m_menuShown=value;}
  MenuRoute MenuCompositeRoute()const{return want;}
  CGuiCompositeShaderGLES::GuiTransfer MenuCompositeGuiTransfer(MenuRoute)const{return desiredTransfer;}
  void QueueKernelSwitch(const char*,int value){events.push_back(value?"queue on":"queue off");}
  void ApplyPendingKernelSwitch(){events.push_back("apply switches");}
  CEGLContextUtils m_pGLContext;
  bool m_menuShown=false,m_menuReported=false,m_menuEngageFailed=false;
  std::chrono::steady_clock::time_point m_menuGoneSince{},m_routeInputsRead{},m_pendingRouteSince{};
  MenuRoute m_menuRoute=MenuRoute::NONE,m_pendingRoute=MenuRoute::NONE,want=MenuRoute::NONE;
  RouteInputs m_routeInputs;
  CGuiCompositeShaderGLES::GuiTransfer m_guiTransfer,desiredTransfer;
  std::unique_ptr<CGuiCompositeShaderGLES> m_compositeShader;
  CFrameBufferObject m_guiFbo,m_menuFbo;
  int m_guiFboWidth=0,m_guiFboHeight=0,m_menuFboWidth=0,m_menuFboHeight=0;
  int m_nWidth=1920,m_nHeight=1080;
  bool m_guiFboBound=false,m_menuFboHasContent=false;
};
void resetEgl(){currentDisplay=currentContext=currentSurface=1;events.clear();}
void provision(CWinSystemAmlogicGLESContext& w)
{
  assert(w.EnsureCompositeFbos());
  w.m_compositeShader=std::make_unique<CGuiCompositeShaderGLES>();
  assert(w.m_compositeShader->CompileAndLink());
  auto shader=std::make_unique<CGuiCompositeShaderGLES>();
  assert(shader->CompileAndLink());w.m_pShader.emplace(1,std::move(shader));
  w.m_textureResources->Register(42);
  w.m_guiFboBound=w.m_menuFboHasContent=true;
  events.clear();
}
size_t event(const std::string& name)
{
  auto i=std::find(events.begin(),events.end(),name);assert(i!=events.end());
  return static_cast<size_t>(i-events.begin());
}
void checkReleaseBefore(const std::string& boundary)
{
  auto end=event(boundary);
  assert(event("delete fbo")<end&&event("delete shader")<end&&event("delete texture")<end);
  for(size_t i=end+1;i<events.size();++i)
    assert(events[i].find("delete ")!=0);
}
'''

TESTS = r'''
int main()
{
  using Route=CWinSystemAmlogicGLESContext::MenuRoute;
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;
    assert(w.IsPrimaryContextCurrent()&&w.CanRender());
    w.m_pGLContext.display=0;currentDisplay=0;
    assert(!w.IsPrimaryContextCurrent()&&!w.CanRender());
    w.m_pGLContext.display=1;currentDisplay=2;
    assert(!w.IsPrimaryContextCurrent()&&!w.CanRender());
    currentDisplay=1;
    w.m_pGLContext.context=0;currentContext=0;
    assert(!w.IsPrimaryContextCurrent());
    w.m_pGLContext.context=1;currentContext=2;
    assert(!w.IsPrimaryContextCurrent());
    currentContext=1;w.m_pGLContext.surface=0;currentSurface=0;
    assert(!w.IsPrimaryContextCurrent());
    w.m_pGLContext.surface=1;currentSurface=2;
    assert(!w.IsPrimaryContextCurrent());
    currentSurface=1;assert(w.IsPrimaryContextCurrent());
    const auto token=w.CaptureRenderTarget();
    auto textures=w.m_textureResources;
    w.m_guiFboBound=w.m_menuFboHasContent=true;
    w.m_guiResources=w.m_textureResources;
    assert(w.DestroyWindow());
    assert(event("end fbo")<event("unbind"));
    assert(event("disable")<event("unbind"));
    assert(!w.CanRender()&&!w.IsRenderTargetCurrent(token));
    assert(!w.m_guiFboBound&&!w.m_menuFboHasContent);
    assert(textures->IsOpen()); // surface loss retains the context namespace
    w.m_pGLContext.surface=2;w.m_pGLContext.bindSucceeds=false;
    assert(!w.m_pGLContext.BindContext()&&!w.CanRender());
    w.m_pGLContext.bindSucceeds=true;assert(w.m_pGLContext.BindContext());
    assert(w.CanRender()&&!w.IsRenderTargetCurrent(token));
    assert(w.m_textureResources==textures);
    w.DestroyWindowSystem();
  }

  // Reset/fullscreen/resize failures report rejection before any GL work.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;RESOLUTION_INFO resolution;
    auto token=w.CaptureRenderTarget();
    w.createWindowSucceeds=false;
    assert(!w.SetFullScreen(true,resolution,false));
    assert(events==std::vector<std::string>{"create window"});
    assert(w.IsRenderTargetCurrent(token));
    events.clear();w.createWindowSucceeds=true;currentContext=currentSurface=0;
    assert(!w.SetFullScreen(true,resolution,false));
    assert(events==std::vector<std::string>{"create window"});
    assert(w.m_width==1920&&w.m_height==1080);
    events.clear();assert(!w.ResizeWindow(640,480,0,0));assert(events.empty());
    assert(!w.ResetRenderSystem(320,240));assert(events.empty());
    currentContext=currentSurface=1;assert(!w.IsRenderTargetCurrent(token));
    assert(w.SetFullScreen(true,resolution,false));
    assert(w.m_width==1280&&w.m_height==720);
    assert(w.ResizeWindow(640,480,0,0));assert(w.m_width==640&&w.m_height==480);
    w.DestroyWindowSystem();
  }
  // Reinitialization failure abandons the previous regime before reaching GL.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;provision(w);
    currentContext=currentSurface=0;
    assert(!w.InitRenderSystem());
    assert(!w.m_bRenderCreated&&w.m_pShader.empty()&&!w.m_textureResources->IsOpen());
    assert(std::find(events.begin(),events.end(),"bootstrap")==events.end());
    for(const auto& e:events)assert(e.find("delete ")!=0);
    w.DestroyWindowSystem();
  }

  // The application normally destroys renderer resources before unbinding.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;provision(w);w.m_menuRoute=Route::DV_CORE2;
    const auto token=w.CaptureRenderTarget();
    assert(w.DestroyRenderSystem());
    assert(!w.CanRender()&&!w.IsRenderTargetCurrent(token));
    assert(!w.m_compositeShader&&!w.m_guiFbo.IsValid()&&!w.m_menuFbo.IsValid());
    assert(!w.m_guiFboBound&&!w.m_menuFboHasContent);
    assert(w.m_guiFboWidth==0&&w.m_menuFboWidth==0);
    assert(w.DestroyWindow());checkReleaseBefore("unbind");
    assert(w.DestroyWindowSystem());
    assert(event("apply switches")<event("destroy context"));
    checkReleaseBefore("unbind");
  }
  // Direct shutdown must perform the same cleanup without Application::Cleanup.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;provision(w);w.m_menuRoute=Route::OSD_VPP;
    assert(w.DestroyWindowSystem());checkReleaseBefore("destroy context");
    assert(event("apply switches")<event("destroy context"));
    assert(event("destroy context")<event("destroy egl"));
    assert(event("destroy egl")<event("destroy native system"));
  }
  // Startup/teardown with no current context abandons names without GL calls.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;provision(w);
    currentContext=currentSurface=0;w.m_bRenderCreated=false;
    assert(w.DestroyWindowSystem());
    assert(!w.m_textureResources->IsOpen()&&!w.m_guiFbo.IsValid());
    assert(!w.m_compositeShader&&w.m_pShader.empty());
    for(const auto& e:events)assert(e.find("delete ")!=0);
  }

  // Real EnsureFbo handles same-size reuse, replacement A->B->A, and failures.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;
    assert(w.EnsureCompositeFbos());auto token=w.CaptureRenderTarget();events.clear();
    assert(w.EnsureCompositeFbos());assert(w.IsRenderTargetCurrent(token));assert(events.empty());
    w.m_nWidth=1280;assert(w.EnsureCompositeFbos());assert(!w.IsRenderTargetCurrent(token));
    const auto smaller=w.CaptureRenderTarget();w.m_nWidth=1920;
    assert(w.EnsureCompositeFbos());assert(!w.IsRenderTargetCurrent(token));
    assert(!w.IsRenderTargetCurrent(smaller));
    token=w.CaptureRenderTarget();w.m_guiFbo.bound=false;w.m_guiFbo.createSucceeds=false;
    assert(!w.EnsureCompositeFbos());assert(!w.IsRenderTargetCurrent(token));
    assert(w.m_guiFboWidth==0&&w.m_guiFboHeight==0&&!w.m_guiFbo.IsValid());
    token=w.CaptureRenderTarget();w.m_guiFbo.initSucceeds=false;
    assert(!w.EnsureCompositeFbos());assert(!w.IsRenderTargetCurrent(token));
    w.DestroyWindowSystem();
  }

  // Route and GUI-transfer replacement invalidate old targets even after ABA.
  resetEgl();
  {
    CWinSystemAmlogicGLESContext w;assert(w.EnsureCompositeFbos());
    const auto plain=w.CaptureRenderTarget();
    assert(w.EngageMenuComposite(Route::DV_CORE2));assert(!w.IsRenderTargetCurrent(plain));
    const auto dv=w.CaptureRenderTarget();w.DisengageMenuComposite();
    assert(!w.IsRenderTargetCurrent(dv));assert(w.EngageMenuComposite(Route::DV_CORE2));
    assert(!w.IsRenderTargetCurrent(dv));
    w.want=Route::DV_CORE2;w.m_menuShown=true;
    auto token=w.CaptureRenderTarget();w.desiredTransfer.white=2;
    w.m_routeInputsRead={};assert(w.BeginRender());
    assert(!w.IsRenderTargetCurrent(token)&&w.m_guiTransfer.white==2);
    token=w.CaptureRenderTarget();w.desiredTransfer.white=1;w.m_routeInputsRead={};
    assert(w.BeginRender());assert(!w.IsRenderTargetCurrent(token)&&w.m_guiTransfer.white==1);
    token=w.CaptureRenderTarget();w.m_routeInputsRead={};assert(w.BeginRender());
    assert(w.IsRenderTargetCurrent(token));
    // Failed LUT update preserves the published transfer and target generation.
    w.desiredTransfer.white=3;w.m_compositeShader->lutSucceeds=false;w.m_routeInputsRead={};
    assert(w.BeginRender());assert(w.IsRenderTargetCurrent(token)&&w.m_guiTransfer.white==1);
    // Failed Begin admission must not touch the route/FBO state through GL.
    currentContext=currentSurface=0;events.clear();
    assert(!w.BeginRender());assert(events==std::vector<std::string>{"base begin"});
    currentContext=currentSurface=1;
    // A failed FBO resize disengages an existing route before continuing.
    w.m_nWidth=1280;w.m_menuFbo.createSucceeds=false;
    assert(w.BeginRender());assert(w.m_menuRoute==Route::NONE&&w.m_menuEngageFailed);
    assert(!w.IsRenderTargetCurrent(token));
    w.DestroyWindowSystem();
  }
}
'''

if __name__ == '__main__':
    main()
