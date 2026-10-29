#!/usr/bin/env python3
"""Production-connected Enky GUI candidate A/B host checks (no device/GPU proof).

Runs complete extracted GuiAB methods and PresentRenderImpl, real Variant.cpp,
DVBackendState, Application::Render, GUIWindowManager::Render/MarkDirty and
mode-3 dirty marking/solving/aging. AML BeginRender's exact final admitted caller
fragment is extracted; preceding hardware/menu route setup is a controlled
boundary. EGL/GL, service/session/cache, sysfs files and output writer are fakes.
GL clear/readback honor actual masks/scissors/packing; no hardware is accessed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

def make_source(aml, header):
    base = (ROOT / 'xbmc/rendering/RenderSystem.h').read_text()
    import re
    fields = '\n'.join(re.search(p, base).group() for p in [
        r'const std::shared_ptr<const uint8_t> m_renderTargetIdentity[^\n]+;',
        r'uint64_t m_renderTargetGeneration[^\n]+;'])
    methods = '\n'.join(function(base, s) for s in [
        'RenderTargetToken CaptureRenderTarget()', 'bool IsRenderTargetCurrent(',
        'void InvalidateRenderTarget()', 'PresentResult GetPresentResult()', 'void ResetPresentResult()'])
    structure = header[header.index('  struct GuiAB\n'):header.index('  bool GuiABIdentity(')]
    begin = function(aml, 'bool CWinSystemAmlogicGLESContext::BeginRender()')
    begin_tail = begin[begin.rindex('  const bool begun = CRenderSystemGLES::BeginRender();'):]
    selected = ['bool CWinSystemAmlogicGLESContext::GuiABIdentity(',
                'bool CWinSystemAmlogicGLESContext::GuiABCurrent()',
                'void CWinSystemAmlogicGLESContext::GuiABFinish(',
                'bool CWinSystemAmlogicGLESContext::MessagePump()',
                'void CWinSystemAmlogicGLESContext::GuiABBegin()',
                'bool CWinSystemAmlogicGLESContext::GuiABDraw()',
                'void CWinSystemAmlogicGLESContext::GuiABPresented(',
                'void CWinSystemAmlogicGLESContext::GuiABDrain()',
                'void CWinSystemAmlogicGLESContext::PresentRenderImpl(']
    if 'void CWinSystemAmlogicGLESContext::GuiABMemoryFailure()' in aml:
        selected.append('void CWinSystemAmlogicGLESContext::GuiABMemoryFailure()')
    production = '\n'.join(function(aml, s) for s in selected)
    readers = function(aml, 'bool GuiABRead(').replace('std::ifstream input(path);', 'std::ifstream input(SysfsPath(path));') + '\n' + function(aml, 'bool GuiABUint(')
    wm = (ROOT / 'xbmc/guilib/GUIWindowManager.cpp').read_text()
    dirt = (ROOT / 'xbmc/guilib/DirtyRegionTracker.cpp').read_text()
    solver = (ROOT / 'xbmc/guilib/DirtyRegionSolvers.cpp').read_text()
    dirty_production = '\n'.join(function(dirt, s) for s in [
        'void CDirtyRegionTracker::MarkDirtyRegion(', 'CDirtyRegionList CDirtyRegionTracker::GetDirtyRegions()',
        'void CDirtyRegionTracker::CleanMarkedRegions(']) + '\n' + function(solver, 'void CFillViewportOnChangeRegionSolver::Solve(') + '\n' + function(solver, 'void CUnionDirtyRegionSolver::Solve(')
    wm_production = '\n'.join(function(wm, s) for s in ['void CGUIWindowManager::MarkDirty()',
        'void CGUIWindowManager::MarkDirty(const CRect&', 'bool CGUIWindowManager::Render()'])
    app = function((ROOT/'xbmc/application/Application.cpp').read_text(), 'void CApplication::Render()')
    return (COMMON.replace('@BASE_METHODS@', methods).replace('@BASE_FIELDS@', fields)
            .replace('@GUIAB@', structure) + readers + '\n' + production + '\n'
            + 'bool CWinSystemAmlogicGLESContext::BeginRender() {\n' + begin_tail + '\n'
            + dirty_production + '\n' + wm_production + '\n' + app + TESTS)

COMMON = r'''
#include "utils/AMLVideoMute.h"
#include "utils/GUIFrameTiming.h"
void aml_native_gui_osd_present(bool){}
CAMLVideoMute& aml_video_mute_controller(){static CAMLVideoMute c([](bool){});return c;}
#include "utils/Variant.h"
#include "utils/DVBackendState.h"
#include "utils/TransformMatrix.h"
#include "rendering/RenderResource.h"
#include "guilib/DirtyRegionSolvers.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <type_traits>
#include <vector>
using namespace std::string_literals;
using CCriticalSection = std::recursive_mutex;
using GLenum = unsigned int; using GLuint = unsigned int; using GLint = int;
using EGLint = int; using GLfloat = float; using GLboolean = unsigned char;
constexpr int LOGINFO=1, LOGERROR=2;
constexpr int WINDOW_HOME=1, WINDOW_FULLSCREEN_VIDEO=2;
constexpr int RENDER_STEREO_MODE_OFF=0, RENDER_STEREO_MODE_MONO=1,
              RENDER_STEREO_VIEW_LEFT=1, RENDER_STEREO_VIEW_RIGHT=2, RENDER_STEREO_VIEW_OFF=0;
constexpr int GL_NO_ERROR=0, GL_SCISSOR_TEST=1, GL_VERSION=2, GL_EXTENSIONS=3,
 GL_FRAMEBUFFER_BINDING=4, GL_ALPHA_BITS=5, GL_RED_BITS=6, GL_GREEN_BITS=7,
 GL_BLUE_BITS=8, GL_PACK_ALIGNMENT=9, GL_VIEWPORT=10, GL_COLOR_CLEAR_VALUE=11,
 GL_COLOR_WRITEMASK=12, GL_SCISSOR_BOX=13, GL_COLOR_BUFFER_BIT=14,
 GL_RGBA=15, GL_UNSIGNED_BYTE=16, EGL_READ=17, EGL_WIDTH=18, EGL_HEIGHT=19;
constexpr GLboolean GL_TRUE=1;
struct Counters {int sysfs=0, gl=0, reads=0, swaps=0, writes=0, marks=0, gui=0, flip=0, clears=0, json=0, fileWrites=0;} count;
std::function<void(const std::string&)> hook;
void event(const std::string& text) {if(hook)hook(text);}
uint64_t nowUs=100;
namespace PLAYBACK_DIAGNOSTICS {
uint64_t NowUs(){return nowUs;}
bool Enabled(){return false;}
struct Trace {
 void Generation(uint64_t){} bool Active(uint64_t){return false;}
 void SwapResult(uint64_t,bool,bool,int){}
 bool PresentChanged(uint64_t,bool,int,bool,bool){return false;}
 template<class F> void Record(uint64_t,const char*,F){}
} endDisplay;
}
namespace fmt {template<class...T>std::string format(const char*,T&&...){return {};}}
struct CLog {template<class...T>static void Log(int,const char*,T&&...){}};
struct GLState {
 int width=129,height=131,pack=8,framebuffer=0,alpha=8,red=8,green=8,blue=8;
 std::array<float,4> clear{0.2f,0.4f,0.6f,0.8f};
 std::array<GLboolean,4> mask{0,1,0,1};
 std::array<int,4> scissor{3,7,8,11}; bool enabled=true;
 bool owned=true, readOwned=true, dimensions=true, readFail=false, corrupt=false;
 std::string version="OpenGL ES 2.0", extensions="";
 unsigned error=0; std::vector<uint8_t> framebufferPixels;
 void allocate(){framebufferPixels.assign(static_cast<size_t>(width)*height*4,0);}
} gl;
void glGetIntegerv(int token,int* data) {
 ++count.gl;
 if(token==GL_VIEWPORT){data[0]=data[1]=0;data[2]=gl.width;data[3]=gl.height;return;}
 if(token==GL_SCISSOR_BOX){std::copy(gl.scissor.begin(),gl.scissor.end(),data);return;}
 *data=token==GL_FRAMEBUFFER_BINDING?gl.framebuffer:token==GL_ALPHA_BITS?gl.alpha:
 token==GL_RED_BITS?gl.red:token==GL_GREEN_BITS?gl.green:token==GL_BLUE_BITS?gl.blue:gl.pack;
}
void glGetFloatv(int,float* p){++count.gl;std::copy(gl.clear.begin(),gl.clear.end(),p);}
void glGetBooleanv(int,GLboolean* p){++count.gl;std::copy(gl.mask.begin(),gl.mask.end(),p);}
GLboolean glIsEnabled(int){++count.gl;return gl.enabled;}
unsigned glGetError(){++count.gl;unsigned v=gl.error;gl.error=0;return v;}
const unsigned char* glGetString(int t){++count.gl;return reinterpret_cast<const unsigned char*>((t==GL_VERSION?gl.version:gl.extensions).c_str());}
void glColorMask(GLboolean a,GLboolean b,GLboolean c,GLboolean d){++count.gl;gl.mask={a,b,c,d};}
void glClearColor(float a,float b,float c,float d){++count.gl;gl.clear={a,b,c,d};}
void glEnable(int){++count.gl;gl.enabled=true;}
void glDisable(int){++count.gl;gl.enabled=false;}
void glScissor(int x,int y,int w,int h){++count.gl;gl.scissor={x,y,w,h};}
void glClear(int) {
 ++count.gl;++count.clears;event("clear");
 for(int y=0;y<gl.height;++y)for(int x=0;x<gl.width;++x) {
   if(gl.enabled && (x<gl.scissor[0]||y<gl.scissor[1]||x>=gl.scissor[0]+gl.scissor[2]||y>=gl.scissor[1]+gl.scissor[3]))continue;
   for(int c=0;c<4;++c)if(gl.mask[c])gl.framebufferPixels[(static_cast<size_t>(y)*gl.width+x)*4+c]=static_cast<uint8_t>(std::lround(gl.clear[c]*255));
 }
}
void glReadPixels(int,int,int w,int h,int format,int type,void* dest) {
 assert(format==GL_RGBA&&type==GL_UNSIGNED_BYTE);++count.gl;++count.reads;
 const size_t stride=(static_cast<size_t>(w)*4+gl.pack-1)/gl.pack*gl.pack;
 auto* p=static_cast<uint8_t*>(dest);
 for(int y=0;y<h;++y){std::copy_n(gl.framebufferPixels.data()+static_cast<size_t>(y)*w*4,w*4,p+static_cast<size_t>(y)*stride);
 std::fill(p+static_cast<size_t>(y)*stride+w*4,p+(static_cast<size_t>(y)+1)*stride,0xa5);}
 if(gl.corrupt)p[(static_cast<size_t>(h/2)*stride+(w/2-36)*4)+3]=0;
 event("read");if(gl.readFail)gl.error=0x502;
}
int eglGetCurrentSurface(int){return gl.readOwned?1:2;}
bool eglQuerySurface(int,int,int token,int* value){*value=token==EGL_WIDTH?gl.width:gl.height;return gl.dimensions;}
std::string sysfsRoot;
std::string SysfsPath(const std::string& path){++count.sysfs;event("sysfs");return sysfsRoot+path;}
void node(const std::string& path,const std::string& value){auto p=std::filesystem::path(sysfsRoot+path);std::filesystem::create_directories(p.parent_path());std::ofstream(p)<<value<<'\n';}
bool GuiABRead(const std::string&,std::string&);
bool GuiABUint(const std::string&,uint64_t&);
struct Diagnostic {uint64_t id=1,epoch=1;bool fenced=false,displayFenced=false,nativeFenced=false,transferring=false,controlPending=false,controlActive=false;};
struct Codec {Diagnostic d;Diagnostic GetDiagnostics(){event("session");return d;}};
struct CAMLCodec {
 struct CaptureSource{std::shared_ptr<Codec> codec;uint64_t epoch=1;};
 static inline CaptureSource source{std::make_shared<Codec>(),1};
 static CaptureSource GetCaptureSource(){event("source");return source;}
};
struct Metadata {bool has_level5_metadata=true,level5_detected=false;uint16_t level5_active_area_top_offset=0,level5_active_area_bottom_offset=208,level5_active_area_left_offset=0,level5_active_area_right_offset=0;};
struct DataCache {struct VideoActivePicture {CRect bounds{0,0,1920,1080};bool valid=true;}activePicture;
 VideoActivePicture GetVideoActivePicture(){return activePicture;}
 double pts=100;int width=3840,height=2024;Metadata metadata;
 double GetRenderPts(){return pts;}Metadata GetVideoDoViFrameMetadata(double){return metadata;}
 int GetVideoWidth(){return width;}int GetVideoHeight(){return height;}
} cache;
struct CEGLContextUtils{struct SwapDiagnostics{bool attempted=false;int error=0;};};
struct Context {
 bool swap=true;int GetEGLSurface(){return 1;}
 bool TrySwapBuffers(CEGLContextUtils::SwapDiagnostics* d){++count.swaps;event("swap");if(d){d->attempted=true;d->error=swap?0:1;}return swap;}
};
struct Lifecycle {uint64_t serial=1;bool ready=true;uint64_t Serial(){return serial;}bool Ready(){return ready;}void Resume(){}};
struct Timer {bool IsTimePast(){return false;}};
struct IDispResource{virtual void OnResetDisplay()=0;};
enum class AMLHDMILinkSample {SKIPPED,CHANGED};
AMLHDMILinkSample aml_hdmi_link_probe(const char*){return AMLHDMILinkSample::SKIPPED;}
void aml_hdr10plus_vsif_hold(bool){}
class CWinSystemAmlogicGLESContext;
extern CWinSystemAmlogicGLESContext* activeWindow;
struct RESOLUTION_INFO {int iWidth=0,iHeight=0;};
struct Gfx {
 bool badScale=false;
 void GetGUIScaling(const RESOLUTION_INFO& r,float&sx,float&sy,TransformMatrix*matrix){sx=float(gl.width)/r.iWidth;sy=float(gl.height)/r.iHeight;*matrix=TransformMatrix::CreateScaler(sx,sy);if(badScale)sx=std::numeric_limits<float>::quiet_NaN();}
 int stereo=0;bool pq=false;bool IsTransferPQ(){return pq;}int GetStereoMode(){return stereo;}
 bool IsFullScreenVideo(){return true;}void SetStereoView(int){}
 int GetWidth(){return gl.width;}int GetHeight(){return gl.height;}
 CRect GetViewWindow(){return CRect(0,0,float(gl.width),float(gl.height));}
 CRect dirtyScissors;
 void Flip(bool,bool);void NextControlBoundsFrame(){}void SetScissors(const CRect&r){dirtyScissors=r;}
 void ResetScissors(){dirtyScissors=GetViewWindow();}void SetRenderingResolution(int,bool){}int GetResInfo(){return 0;}
} gfx;
struct CWinSystemAmlogic {
 bool MessagePump(){return false;}std::atomic<bool>m_hdrRefreshPending{false};void RefreshHDRCapabilities(){}
};
class CRenderSystemBase {
 public:bool current=true;PresentResult m_presentResult=PresentResult::NOT_ATTEMPTED;
 bool CanRender()const{return current;} @BASE_METHODS@ @BASE_FIELDS@
};
class CRenderSystemGLES:public CRenderSystemBase {public:bool begin=true,end=true;bool BeginRender(){return begin;}bool EndRender(){return end;}};
class CWinSystemAmlogicGLESContext:public CWinSystemAmlogic,public CRenderSystemGLES {
 public:
 @GUIAB@
 enum class MenuRoute{NONE,ACTIVE};MenuRoute m_menuRoute=MenuRoute::NONE;
 struct PendingSwitch {const char*path=nullptr;};std::array<PendingSwitch,2>m_pendingSwitches;
 bool m_menuShown=false,m_shutdownRequested=false,m_guiFboBound=false,m_delayDispReset=false,m_displayGeometryReady=true;
 Lifecycle m_displayLifecycle;Context m_pGLContext;Timer m_dispResetTimer;
 CCriticalSection m_resourceSection;std::vector<IDispResource*>m_resources;
 bool limited=false,suppressFlip=false;
 bool IsDisplayReadyForVideo(){return m_displayLifecycle.Ready()&&m_displayGeometryReady&&!m_delayDispReset&&CanRender();}
 bool IsPrimaryContextCurrent(){return gl.owned;}
 bool UseLimitedColor(){return limited;}Gfx&GetGfxContext(){return gfx;}
 int GetEGLDisplay(){return 1;}int GetEGLSurface(){return 1;}
 bool GuiABIdentity(std::vector<std::string>&);bool GuiABCurrent();void GuiABFinish(const char*);
 bool MessagePump();void GuiABBegin();bool GuiABDraw();void GuiABPresented(bool);void GuiABDrain();void GuiABMemoryFailure() noexcept;
 bool BeginRender();uint64_t m_videoMuteHandoff{0};void PresentRenderImpl(bool);
 void ObserveEndDisplay(const char*,bool=false){}void ApplyPendingKernelSwitch(){}
 bool BeginGuiComposite(){return true;}bool EndGuiComposite(){return true;}
 void CancelGuiComposite(){GuiABFinish("render_cancelled");}
 int GetBufferAge(){return 1;}void SetDirtyRegions(const CDirtyRegionList&){}
};
using CWinSystemBase=CWinSystemAmlogicGLESContext;
CWinSystemAmlogicGLESContext* activeWindow=nullptr;
void Gfx::Flip(bool rendered,bool){++count.flip;event("flip");if(!activeWindow->suppressFlip)activeWindow->PresentRenderImpl(rendered);}
struct CGUIWindow {
 std::map<std::string,CVariant> props;bool fullscreen=true;
 CVariant GetProperty(const std::string& key){return props[key];}
 void SetProperty(const std::string& key,const CVariant& v){props[key]=v;}
 void MarkDirtyRegion(){}bool IsDialogRunning(){return false;}
} home;
struct CDirtyRegionTracker {
 CDirtyRegionList m_markedRegions;CFillViewportOnChangeRegionSolver solver;CUnionDirtyRegionSolver unionSolver;
 IDirtyRegionSolver*m_solver=&solver;
 void MarkDirtyRegion(const CDirtyRegion&);CDirtyRegionList GetDirtyRegions();void CleanMarkedRegions(int);
 const CDirtyRegionList&GetMarkedRegions(){return m_markedRegions;}
};
struct AdvancedSettings {bool m_guiVisualizeDirtyRegions=false;int m_guiAlgorithmDirtyRegions=3;};
struct Settings {AdvancedSettings advanced;AdvancedSettings*GetAdvancedSettings(){return &advanced;}} settings;
struct CGUITexture {static void DrawQuad(const CRect&,unsigned){}};
struct CSingleExit {explicit CSingleExit(Gfx&) {}};
std::vector<uint8_t> NormalScene() {
 std::vector<uint8_t> pixels(static_cast<size_t>(gl.width)*gl.height*4,0);
 for(int y=gl.height-9;y<gl.height;++y)for(int x=0;x<gl.width;++x)
   for(int c=0;c<4;++c)pixels[(static_cast<size_t>(y)*gl.width+x)*4+c]=
     y==gl.height-9?(c==3?0:99):(c==3?32:64);
 return pixels;
}
struct CGUIWindowManager {
 CDirtyRegionTracker m_tracker;std::vector<CGUIWindow*>m_activeDialogs;
 bool IsWindowActive(int){return home.fullscreen;}CGUIWindow*GetWindow(int){return &home;}
 int GetActiveWindow(){return 0;}void MarkDirty();void MarkDirty(const CRect&);bool Render();
 void RenderEx(){event("video");}void AfterRender(){event("after");}void PublishRenderBounds(){}
 void RenderPass(){++count.gui;event("gui");
   // The normal mode3 pass redraws the full scene. Partial-region mode uses
   // the actual WM-selected dirty scissor; it cannot remove unrendered cues.
   auto normal=NormalScene();
   const bool partial=settings.advanced.m_guiAlgorithmDirtyRegions!=DIRTYREGION_SOLVER_FILL_VIEWPORT_ON_CHANGE;
   for(int y=0;y<gl.height;++y)for(int x=0;x<gl.width;++x){
     if(partial&&(x<gfx.dirtyScissors.x1||x>=gfx.dirtyScissors.x2||y<gfx.dirtyScissors.y1||y>=gfx.dirtyScissors.y2))continue;
     for(int c=0;c<4;++c)gl.framebufferPixels[(static_cast<size_t>(y)*gl.width+x)*4+c]=normal[(static_cast<size_t>(y)*gl.width+x)*4+c];
   }
 }
};
struct InfoProviders {
 InfoProviders&GetGUIControlsInfoProvider(){return *this;}InfoProviders&GetSystemInfoProvider(){return *this;}
 void ResetContainerMovingCache(){}void UpdateFPS(){}
};
struct CGUIInfoManager {InfoProviders providers;void ResetCache(){}InfoProviders&GetInfoProviders(){return providers;}};
struct CGUIComponent {CGUIWindowManager manager;CGUIInfoManager info;
 CGUIWindowManager&GetWindowManager(){return manager;}CGUIInfoManager&GetInfoManager(){return info;}
} gui;
struct Messenger {bool IsProcessThread(){return true;}} messenger;
struct CApplicationPowerHandling;
struct Components {bool missing=false;template<class T>std::shared_ptr<T> GetComponent();} components;
struct CServiceBroker {
 static Components&GetAppComponents(){return components;}
 static CGUIComponent*GetGUI(){return &gui;}static DataCache&GetDataCacheCore(){return cache;}
 static CWinSystemBase*GetWinSystem(){return activeWindow;}static CWinSystemAmlogicGLESContext*GetRenderSystem(){return activeWindow;}
 static Settings*GetSettingsComponent(){return &settings;}static Messenger*GetAppMessenger(){return &messenger;}
};
void MarkGuiDirty(){++count.marks;gui.GetWindowManager().MarkDirty();}
bool IsFullscreenVideoActive(){return home.fullscreen;}
bool jsonFail=false,fileFail=false,shortWrite=false;
struct CJSONVariantWriter {static bool Write(const CVariant&,std::string&s,bool){++count.json;s="{}";return !jsonFail;}};
namespace XFILE {struct CFile {bool OpenForWrite(const char*,bool){++count.writes;return !fileFail;}ssize_t Write(const char*,size_t n){++count.fileWrites;return shortWrite?n-1:n;}};}
struct CApplicationPlayer {bool IsExternalPlaying(){return false;}bool IsPausedPlayback(){return false;}bool IsRenderingVideoLayer(){return true;}};
struct CApplicationPowerHandling {bool gui=true;bool IsPythonScreenSaverActive(){return false;}void ResetScreenSaver(){}bool GetRenderGUI(){return gui;}};
std::shared_ptr<CApplicationPowerHandling> appPower=std::make_shared<CApplicationPowerHandling>();
template<class T>std::shared_ptr<T>Components::GetComponent(){static_assert(std::is_same_v<T,CApplicationPowerHandling>);if(missing)throw std::logic_error("missing component");return appPower;}
struct CTimeUtils {static void UpdateFrameTime(bool){}};
class CApplication {
 public:bool m_bStop=false,m_AppFocused=true,m_skipGuiRender=false;
 CApplicationPlayer player;CApplicationPowerHandling&power=*appPower;RenderAttemptResult m_lastRenderAttempt;
 std::chrono::steady_clock::time_point m_lastRenderTime{};
 template<class T>T*GetComponent(){if constexpr(std::is_same_v<T,CApplicationPlayer>)return &player;else return &power;}
 void Render();
};
'''

TESTS = r'''
void setup(CWinSystemAmlogicGLESContext& w) {
 count={};hook={};jsonFail=fileFail=shortWrite=false;gl=GLState{};gl.allocate();nowUs=100;activeWindow=&w;
 home=CGUIWindow{};gui=CGUIComponent{};gui.manager.m_tracker.m_solver=&gui.manager.m_tracker.solver;
 cache=DataCache{};gfx=Gfx{};gfx.ResetScissors();settings=Settings{};components=Components{};appPower=std::make_shared<CApplicationPowerHandling>();
 CAMLCodec::source={std::make_shared<Codec>(),1};
 const std::string prefix="/sys/module/amdolby_vision/parameters/";
 for(const char* name:{"dolby_vision_mode","dolby_vision_target_mode","dolby_vision_status","dolby_vision_mask","dolby_vision_flags","dolby_vision_ll_policy","xbmc_dv_vp","dolby_vision_xbmc_osd","dolby_vision_policy","dolby_vision_hdr10_policy","dolby_vision_subtitles","dolby_vision_enable","xbmc_meta_level_5","xbmc_meta_level_5_osdst","xbmc_meta_level_5_subt","dolby_vision_graphic_pq","xbmc_force_l5_override","xbmc_override_l5_top","xbmc_override_l5_bottom","xbmc_override_l5_left","xbmc_override_l5_right","xbmc_l5_override_additive"}) node(prefix+name,"0");
 for(const char* name:{"dolby_vision_mode","dolby_vision_target_mode","dolby_vision_enable","xbmc_meta_level_5"})node(prefix+name,"1");
 node(prefix+"dolby_vision_status","3");node(prefix+"dolby_vision_mask","7");
 node("/sys/class/amdolby_vision/backend_state","7 1 1");
 node("/sys/class/video/presentation_state","1 9 0");
 node("/sys/class/amdolby_vision/dv_video_on","1");
 node("/sys/class/display/mode","1080p60hz");node("/sys/class/amhdmitx/amhdmitx0/attr","422,12bit");
 node("/sys/class/graphics/fb0/free_scale","0");node("/sys/class/graphics/fb0/window_axis","0 0 1919 1079");
 count={};
}
void arm(CWinSystemAmlogicGLESContext&w){home.SetProperty("enky.gui_ab","start");w.MessagePump();assert(w.m_guiAB.phase==CWinSystemAmlogicGLESContext::GuiAB::Phase::A);}
void frame(CWinSystemAmlogicGLESContext&w,CApplication&app){nowUs+=1000;w.MessagePump();app.Render();}
void verifyState(const GLState&before){assert(gl.clear==before.clear);assert(gl.mask==before.mask);assert(gl.scissor==before.scissor);assert(gl.enabled==before.enabled);assert(gl.pack==before.pack);}
void complete(CWinSystemAmlogicGLESContext&w,CApplication&app,bool valid=true){
 if(valid){home.SetProperty("enky.gui_ab.a","bar_cue");home.SetProperty("enky.gui_ab.b","clear_cue");}
 for(int i=0;i<147;++i){auto saved=gl;frame(w,app);verifyState(saved);}
 w.MessagePump();
 assert(gl.framebufferPixels==NormalScene()); // Actual full restoration removes both cue boxes.
 assert(w.m_guiAB.accepted[0]==72&&w.m_guiAB.accepted[1]==72);
 assert(count.reads==2&&count.swaps==147&&count.gui==147);
 assert(w.m_guiAB.report["software_redraws_accepted"].asInteger()==3);
 assert(w.m_guiAB.report["completed"].asBoolean());
 assert(w.m_guiAB.report["b_interpretable"].asBoolean()==valid);
 assert(!w.m_guiAB.report["physical_page_or_completion_observed"].asBoolean());
 assert(w.m_guiAB.report["A"]["cue_pixels_valid"].asBoolean());
 assert(w.m_guiAB.report["B"]["zero_rgba_outside_cue"].asBoolean());
 assert(w.m_guiAB.report["A"]["stride"].asInteger()==520);
 assert(w.m_guiAB.phase==CWinSystemAmlogicGLESContext::GuiAB::Phase::IDLE);
 auto&a=w.m_guiAB.report["A"];auto&b=w.m_guiAB.report["B"];
 auto&ar=a["rows_excluding_cue_coverage_alphaSum_rgbSum"];auto&br=b["rows_excluding_cue_coverage_alphaSum_rgbSum"];
 assert(ar.size()==131&&br.size()==131);
 assert(ar[122u][0u].asUnsignedInteger()==0&&ar[122u][1u].asUnsignedInteger()==0&&ar[122u][2u].asUnsignedInteger()==uint64_t(99*3*129));
 assert(ar[123u][0u].asUnsignedInteger()==129&&ar[123u][1u].asUnsignedInteger()==32*129&&ar[123u][2u].asUnsignedInteger()==64*3*129);
 for(unsigned y=0;y<br.size();++y)for(unsigned c=0;c<3;++c)assert(br[y][c].asUnsignedInteger()==0);
 assert(a["top_rows"].asInteger()==32&&a["top_rgba_hex"].asString().size()==32*129*8);
 auto top=a["top_rgba_hex"].asString();assert(top.substr(top.size()-8)=="40404020");
 assert(top.substr(23*129*8,8)=="63636300");
 assert(a["cue_rgba_hex"].asString().size()==32*16*8&&b["cue_rgba_hex"].asString().size()==2*32*16*8);
 auto before=count;for(int i=0;i<5;++i)frame(w,app);auto aged=count;
 for(int i=0;i<5;++i)frame(w,app);
 assert(count.swaps==aged.swaps&&count.marks==before.marks&&count.gl==before.gl&&count.reads==before.reads&&count.sysfs==before.sysfs);
}
void failureReason(CWinSystemAmlogicGLESContext&w,const char* value){assert(w.m_guiAB.report["reason"].asString()==value);assert(!w.m_guiAB.report["b_interpretable"].asBoolean());}
int main(int argc,char**argv) {
 assert(argc==2);sysfsRoot=argv[1];using W=CWinSystemAmlogicGLESContext;
 {W w;setup(w);CApplication app;for(int i=0;i<20;++i)frame(w,app);assert(count.sysfs==0&&count.gl==0&&count.reads==0&&count.swaps==0&&count.marks==0);}
 // Independent partial dirty-region witness: old history is retired and
 // only a corner needs normal work when the diagnostic begins restoration.
 {W w;setup(w);settings.advanced.m_guiAlgorithmDirtyRegions=DIRTYREGION_SOLVER_UNION;gui.manager.m_tracker.m_solver=&gui.manager.m_tracker.unionSolver;arm(w);CApplication app;home.SetProperty("enky.gui_ab.a","bar_cue");home.SetProperty("enky.gui_ab.b","clear_cue");for(int i=0;i<144;++i)frame(w,app);assert(w.m_guiAB.phase==W::GuiAB::Phase::RESTORE);gui.manager.m_tracker.m_markedRegions.clear();gui.manager.m_tracker.MarkDirtyRegion(CDirtyRegion(0,0,4,4));for(int i=0;i<3;++i)frame(w,app);w.MessagePump();assert(gl.framebufferPixels==NormalScene());assert(w.m_guiAB.report["software_redraws_accepted"].asInteger()==3);assert(count.reads==2&&count.swaps==147);}
 for(bool scissor:{false,true})for(int mask=0;mask<3;++mask){W w;setup(w);gl.enabled=scissor;gl.mask=mask==0?std::array<GLboolean,4>{0,0,0,0}:mask==1?std::array<GLboolean,4>{1,1,1,1}:std::array<GLboolean,4>{0,1,0,1};arm(w);CApplication app;complete(w,app);}
 {W w;setup(w);gl.extensions="GL_EXT_unpack_subimage";arm(w);CApplication app;complete(w,app);}
 {W w;setup(w);arm(w);CApplication app;home.SetProperty("enky.gui_ab.b","clear_cue");complete(w,app,false);assert(w.m_guiAB.report["a"].asString()=="unknown");}
 {W w;setup(w);arm(w);CApplication app;home.SetProperty("enky.gui_ab.a","bar_cue");complete(w,app,false);assert(w.m_guiAB.report["b"].asString()=="unknown");}
 for(const char*a:{"no_cue","clear_cue"}){W w;setup(w);arm(w);home.SetProperty("enky.gui_ab.a",a);CApplication app;frame(w,app);failureReason(w,"A_validity_control_failed");assert(w.m_guiAB.accepted[1]==0&&count.reads==0);}
 // Every unsafe target rejects before touching/readback, but the ordinary swap remains.
 for(int mode=0;mode<11;++mode){W w;setup(w);CApplication app;
   if(mode==0)gl.alpha=0;if(mode==1)gl.framebuffer=7;if(mode==2)gl.version="OpenGL ES 3.2";
   if(mode==3)gl.extensions="GL_NV_pixel_buffer_object";if(mode==4)gl.readOwned=false;
   if(mode==5){gl.width=4096;gl.height=4096;gl.allocate();}
   if(mode==6)gl.dimensions=false;if(mode==7)gl.pack=3;
   if(mode==8)gl.extensions="GL_ANGLE_pack_reverse_row_order";if(mode==9)gl.extensions="GL_MESA_pack_invert";if(mode==10)gl.extensions="GL_NV_pack_subimage";
   arm(w);frame(w,app);assert(count.reads==0&&w.m_guiAB.accepted[0]==0&&count.swaps==1&&count.clears==0);
 }
 {W w;setup(w);arm(w);CApplication app;gl.readFail=true;frame(w,app);w.MessagePump();failureReason(w,"GL_read_failed");assert(count.reads==1&&w.m_guiAB.accepted[0]==0);}
 {W w;setup(w);arm(w);CApplication app;w.m_pGLContext.swap=false;frame(w,app);w.MessagePump();failureReason(w,"skipped_or_failed_swap");assert(w.m_guiAB.accepted[0]==0);}
 {W w;setup(w);arm(w);CApplication app;w.suppressFlip=true;frame(w,app);w.MessagePump();failureReason(w,"incomplete_render_attempt");assert(count.reads==0&&count.swaps==0);}
 {W w;setup(w);arm(w);CApplication app;app.m_bStop=true;frame(w,app);assert(count.reads==0);nowUs+=10000000;w.MessagePump();failureReason(w,"expired");}
 {W w;setup(w);arm(w);nowUs+=10000000;w.MessagePump();failureReason(w,"expired");assert(count.gl==0);}
 {W w;setup(w);arm(w);home.SetProperty("enky.gui_ab","cancel");w.MessagePump();failureReason(w,"cancelled");assert(count.gl==0);}
 for(int mode=0;mode<10;++mode){W w;setup(w);arm(w);CApplication app;
   if(mode==0)CAMLCodec::source={std::make_shared<Codec>(),1}; // id must differ in actual registry.
   if(mode==0)CAMLCodec::source.codec->d.id=2;
   if(mode==1)++CAMLCodec::source.codec->d.epoch;
   if(mode==2)w.InvalidateRenderTarget();if(mode==3)++w.m_displayLifecycle.serial;
   if(mode==4)home.fullscreen=false;if(mode==5)CAMLCodec::source.codec->d.fenced=true;
   if(mode==6)node("/sys/class/video/presentation_state","1 10 0");
   if(mode==7)cache.metadata.level5_active_area_bottom_offset=209;
   if(mode==8)std::filesystem::remove(sysfsRoot+"/sys/class/amdolby_vision/backend_state");
   if(mode==9)node("/sys/module/amdolby_vision/parameters/dolby_vision_flags","1");
   frame(w,app);failureReason(w,"identity_changed_or_unavailable");assert(count.reads==0);
 }
 // Raw toggle and provider receipt progress preserve the stable session projection.
 {W w;setup(w);arm(w);node("/sys/module/amdolby_vision/parameters/dolby_vision_flags","2147483648");node("/sys/class/video/presentation_state","1 9 9");assert(w.GuiABCurrent());}
 for(const char* text:{"", "-1", "1 garbage", "18446744073709551616"}){W w;setup(w);node("/sys/module/amdolby_vision/parameters/dolby_vision_mask",text);home.SetProperty("enky.gui_ab","start");w.MessagePump();failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");}
 {W w;setup(w);arm(w);CApplication app;hook=[&](const std::string&stage){if(stage=="read")++CAMLCodec::source.codec->d.epoch;};frame(w,app);hook={};w.MessagePump();failureReason(w,"identity_changed_after_draw_or_read");assert(w.m_guiAB.accepted[0]==0);}
 {W w;setup(w);arm(w);gl.corrupt=true;CApplication app;frame(w,app);w.MessagePump();failureReason(w,"readback_cue_or_transparent_substitute_invalid");assert(w.m_guiAB.accepted[1]==0);}
 // Arrival after a read or a final B swap cannot complete the diagnostic late.
 {W w;setup(w);arm(w);CApplication app;hook=[&](const std::string& stage){if(stage=="read")nowUs+=10000000;};frame(w,app);hook={};w.MessagePump();failureReason(w,"expired_after_draw_or_read");assert(w.m_guiAB.accepted[0]==0&&count.reads==1);}
 {W w;setup(w);arm(w);CApplication app;home.SetProperty("enky.gui_ab.a","bar_cue");home.SetProperty("enky.gui_ab.b","clear_cue");for(int i=0;i<143;++i)frame(w,app);assert(w.m_guiAB.accepted[1]==71);hook=[&](const std::string& stage){if(stage=="swap")nowUs+=10000000;};frame(w,app);hook={};w.MessagePump();failureReason(w,"expired_after_swap");assert(w.m_guiAB.accepted[1]==71&&!w.m_guiAB.report["completed"].asBoolean());}
 for(const char* at:{"read","swap"})for(bool cancel:{false,true}){W w;setup(w);arm(w);CApplication app;hook=[&](const std::string&stage){if(stage==at){if(cancel)home.SetProperty("enky.gui_ab","cancel");else ++CAMLCodec::source.codec->d.epoch;}};frame(w,app);hook={};w.MessagePump();assert(w.m_guiAB.accepted[0]==0&&!w.m_guiAB.report["completed"].asBoolean());assert(count.swaps==1);}
 // Identity collection itself is bracketed against source replacement.
 {W w;setup(w);bool replaced=false;hook=[&](const std::string&stage){if(stage=="sysfs"&&!replaced){replaced=true;CAMLCodec::source={std::make_shared<Codec>(),1};CAMLCodec::source.codec->d.id=2;}};home.SetProperty("enky.gui_ab","start");w.MessagePump();hook={};failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");assert(count.gl==0);}
 for(const char*text:{"1 -9 0","1 9 -1","1 9 0 4","2 9 0","1 0 0","1 18446744073709551616 0"}){W w;setup(w);node("/sys/class/video/presentation_state",text);home.SetProperty("enky.gui_ab","start");w.MessagePump();failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");assert(count.gl==0);}
 for(int mode=0;mode<7;++mode){W w;setup(w);if(mode==0)cache.metadata.has_level5_metadata=false;if(mode==1)cache.metadata.level5_detected=true;if(mode==2)cache.width=0;if(mode==3)w.m_menuShown=true;if(mode==4)w.m_pendingSwitches[0].path="queued";if(mode==5)gfx.stereo=1;if(mode==6)cache.metadata.level5_active_area_top_offset=1012;home.SetProperty("enky.gui_ab","start");w.MessagePump();failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");assert(count.gl==0&&count.swaps==0);}
 // The same diagnostic error must not repeat a GL read on restoration renders.
 {W w;setup(w);arm(w);CApplication app;gl.error=0x501;frame(w,app);w.MessagePump();failureReason(w,"preexisting_GL_error_consumed_and_recorded");assert(count.reads==0);}
 // Natural GUI suppression and failed EndRender do not fabricate GUI work/Flip.
 for(int mode=0;mode<3;++mode){W w;setup(w);arm(w);CApplication app;if(mode==0)app.m_skipGuiRender=true;if(mode==1)app.power.gui=false;if(mode==2)w.end=false;frame(w,app);w.MessagePump();assert(w.m_guiAB.accepted[0]==0&&count.reads==0&&count.swaps==0);assert(app.m_lastRenderAttempt.status==(mode==2?RenderAttemptStatus::END_REJECTED:RenderAttemptStatus::COMMANDS_COMPLETED));}
 // Published active geometry must contain both physical-candidate cue boxes.
 for(int side=0;side<4;++side){W w;setup(w);const float sx=float(gl.width)/1920,sy=float(gl.height)/1080;
 if(side==0)cache.activePicture.bounds.x1=(gl.width/2-35)/sx;
 if(side==1)cache.activePicture.bounds.x2=(gl.width/2+35)/sx;
 if(side==2)cache.activePicture.bounds.y1=(gl.height-(gl.height/2+7))/sy;
 if(side==3)cache.activePicture.bounds.y2=(gl.height-(gl.height/2-7))/sy;
 arm(w);CApplication app;frame(w,app);w.MessagePump();failureReason(w,"NO_TEST_cue_outside_active_picture");assert(count.reads==0);}
 for(int mode=0;mode<3;++mode){W w;setup(w);if(mode==0)cache.activePicture.valid=false;if(mode==1)gfx.badScale=true;if(mode==2)appPower->gui=false;home.SetProperty("enky.gui_ab","start");w.MessagePump();failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");assert(count.gl==0);}
 // Repeated swap failures cannot force unbounded diagnostic invalidations.
 {W w;setup(w);arm(w);CApplication app;w.m_pGLContext.swap=false;frame(w,app);for(int i=0;i<20;++i)frame(w,app);assert(w.m_guiAB.phase==W::GuiAB::Phase::RESTORE);assert(w.m_guiAB.restoreAttempts==6&&count.marks==7);assert(w.m_guiAB.report["software_restore_attempts_exhausted"].asBoolean());assert(w.m_guiAB.report["software_restoration_pending"].asBoolean());assert(w.m_guiAB.report["software_redraws_accepted"].asInteger()==0);auto before=count;for(int i=0;i<20;++i)frame(w,app);assert(count.marks==before.marks&&count.writes==before.writes&&count.reads==1);}
 // Allocation failure at safe diagnostic boundaries never escapes to the loop.
 {W w;setup(w);hook=[](const std::string&stage){if(stage=="sysfs")throw std::bad_alloc();};home.SetProperty("enky.gui_ab","start");w.MessagePump();hook={};assert(w.m_guiAB.memoryFailure&&w.m_guiAB.phase==W::GuiAB::Phase::IDLE&&w.m_guiAB.pixels.empty()&&w.m_guiAB.report.isNull());assert(count.gl==0);}
 {W w;setup(w);arm(w);CApplication app;hook=[](const std::string&stage){if(stage=="read")throw std::bad_alloc();};auto saved=gl;frame(w,app);hook={};verifyState(saved);assert(w.m_guiAB.memoryFailure&&w.m_guiAB.phase==W::GuiAB::Phase::RESTORE&&w.m_guiAB.pixels.empty()&&w.m_guiAB.report.isNull());assert(w.m_guiAB.accepted[0]==0&&count.swaps==1);}
 // Actual component API may throw during teardown; null and absent also fail closed.
 for(bool absent:{false,true}){W w;setup(w);if(absent)components.missing=true;else appPower.reset();home.SetProperty("enky.gui_ab","start");w.MessagePump();failureReason(w,"NO_TEST_identity_unavailable_or_ineligible");assert(count.gl==0&&count.marks==0&&count.sysfs==0);}
 // Each failed result publication is attempted once, outside Render, without retry.
 for(int mode=0;mode<3;++mode){W w;setup(w);jsonFail=mode==0;fileFail=mode==1;shortWrite=mode==2;arm(w);assert(count.json==1);assert(count.writes==(mode==0?0:1));assert(count.fileWrites==(mode==2?1:0));auto before=count;for(int i=0;i<10;++i)w.MessagePump();assert(count.json==before.json&&count.writes==before.writes&&count.fileWrites==before.fileWrites&&count.gl==0&&count.marks==0);}
 // Cancellation/target invalidation during final sysfs collection must be rechecked.
 for(bool cancel:{false,true}){W w;setup(w);arm(w);CApplication app;bool swapped=false,changed=false;hook=[&](const std::string&stage){if(stage=="swap")swapped=true;if(stage=="sysfs"&&swapped&&!changed){changed=true;if(cancel)home.SetProperty("enky.gui_ab","cancel");else w.InvalidateRenderTarget();}};frame(w,app);hook={};w.MessagePump();assert(changed&&w.m_guiAB.accepted[0]==0&&!w.m_guiAB.report["completed"].asBoolean());assert(count.swaps==1);}
 // Failed render begin never creates a ticket or submits the diagnostic.
 {W w;setup(w);arm(w);w.begin=false;CApplication app;frame(w,app);failureReason(w,"begin_render_failed");assert(count.gl==0&&count.swaps==0);}
 std::cout<<"PASS: production GuiAB, Variant, Application Render, actual mode3 dirty renderer, 72+72+3; guarded EGL/GL/sysfs boundaries\n";
}
'''

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--negative-controls',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    aml_path=ROOT/'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp'
    header_path=aml_path.with_suffix('.h')
    aml,header=aml_path.read_text(),header_path.read_text()
    source=make_source(aml,header)
    input_hashes={str(aml_path.relative_to(ROOT)):hashlib.sha256(aml.encode()).hexdigest(),
                  str(header_path.relative_to(ROOT)):hashlib.sha256(header.encode()).hexdigest(),
                  "tools/test-aml-gui-ab.py":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "xbmc/utils/Variant.cpp":hashlib.sha256((ROOT/"xbmc/utils/Variant.cpp").read_bytes()).hexdigest()}
    controls={
      'missing-dirty-mark':('MarkGuiDirty(); // Before GUIWindowManager::Render, never fake hasRendered.','; // lost diagnostic dirty mark'),
      'wrong-budget':('BUDGET = 72','BUDGET = 71'),
      'opaque-B':('glClearColor(0.0f, 0.0f, 0.0f, 0.0f);','glClearColor(0.0f, 0.0f, 0.0f, 1.0f);'),
      'scissored-B':('glDisable(GL_SCISSOR_TEST);\n    glClearColor','glEnable(GL_SCISSOR_TEST);\n    glClearColor'),
      'masked-clear':('glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);','glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, false);'),
      'lost-state-mask':('glColorMask(mask[0], mask[1], mask[2], mask[3]);','glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);'),
      'lost-state-scissor':('glScissor(scissor[0], scissor[1], scissor[2], scissor[3]);','glScissor(0, 0, 1, 1);'),
      'lost-state-clear':('glClearColor(clear[0], clear[1], clear[2], clear[3]);','glClearColor(0, 0, 0, 0);'),
      'lost-state-enable':('if (!enabled)\n    glDisable(GL_SCISSOR_TEST);','if (enabled)\n    glDisable(GL_SCISSOR_TEST);'),
      'missing-alpha-gate':('alpha != 8','false'),
      'missing-byte-cap':('stride > GuiAB::MAX_BYTES / static_cast<size_t>(height)','false'),
      'failed-swap-counted':('if (!m_guiAB.ticket || !accepted)','if (!m_guiAB.ticket)'),
      'unknown-A-accepted':('m_guiAB.report["a"].asString() == "bar_cue"','true'),
      'unknown-B-accepted':('m_guiAB.report["b"].asString() == "clear_cue"','true'),
      'no-read-target-guard':('eglGetCurrentSurface(EGL_READ) != m_pGLContext.GetEGLSurface()','false'),
      'read-every-frame':('if (!m_guiAB.read[phase])','if (true)'),
      'late-swap-accepted':('GuiABFinish("expired_after_swap");\n    return;', 'm_guiAB.publish = true; // ignore expired completion'),
      'late-read-check-omitted':('return fail("expired_after_draw_or_read");', '(void)0; // ignore expired read'),
      'unguarded-swap-identity':('GuiABFinish("identity_or_request_changed_during_swap");\n    return;', 'm_guiAB.publish = true; // ignore replacement during swap'),
      'unbounded-restore':('m_guiAB.restoreAttempts < 6','true'),
      'cue-geometry-ignored':('picture.x1 > width / 2 - 36','false'),
      'missing-caller':('if (begun) GuiABBegin();','if (begun) (void)0;'),
      'stable-policy-ignored':('values[i] & ~uint64_t{0x80000000}','uint64_t{0}'),
      'stable-identity-ignored':('identity == m_guiAB.identity','true'),
      'reverse-pack-extension-ignored':('std::strstr(extensions, "pack_reverse_row_order")','false'),
      'late-dirty-mark':(("MarkGuiDirty(); // Before GUIWindowManager::Render, never fake hasRendered.","(void)0; // removed pre-GUI dirty mark"),("m_guiAB.touched = true;","MarkGuiDirty(); m_guiAB.touched = true;")),
      'forced-hasRendered':('bool hasRendered = false;', 'bool hasRendered = true;'),
      'extra-direct-swap':('m_guiAB.readEnter = PLAYBACK_DIAGNOSTICS::NowUs();', 'm_pGLContext.TrySwapBuffers(nullptr); m_guiAB.readEnter = PLAYBACK_DIAGNOSTICS::NowUs();'),
      'restoration-dirty-mark-omitted':('++m_guiAB.restoreAttempts; MarkGuiDirty();','++m_guiAB.restoreAttempts; (void)0;'),
      'false-coverage-from-RGB':('coverage += pixel[3] != 0;','coverage += pixel[0] != 0;'),
      'post-collection-identity-omitted':('identity == m_guiAB.identity && home->GetProperty("enky.gui_ab").asString() == "running" && IsRenderTargetCurrent(m_guiAB.target) && m_displayLifecycle.Serial() == m_guiAB.display && IsDisplayReadyForVideo() && !m_shutdownRequested && IsFullscreenVideoActive()', 'identity == m_guiAB.identity'),
    }
    results=[]
    out=args.output
    if out:out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aml-gui-ab-') as tmp:
        tmp=Path(tmp); fixture=tmp/'fixture.cpp'; exe=tmp/'fixture'
        compile_flags=[os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-Wno-unused-parameter','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I',str(ROOT/'xbmc')]
        variant_object=tmp/'variant.o'
        subprocess.run(compile_flags+['-c',str(ROOT/'xbmc/utils/Variant.cpp'),'-o',str(variant_object)],check=True)
        variants=[('positive',source)]
        if args.negative_controls:
            for name,changes in controls.items():
                import re
                if isinstance(changes[0],str):changes=[changes]
                mutant=source
                for old,new in changes:
                    pattern=r'\s+'.join(re.escape(part) for part in old.split())
                    mutant,n=re.subn(pattern,lambda match:new,mutant)
                    if not n:raise RuntimeError('mutation anchor missing: '+name)
                variants.append((name,mutant))
        for name,text in variants:
            fixture.write_text(text)
            compile_command=compile_flags+[str(fixture),str(variant_object),'-o',str(exe)]
            subprocess.run(compile_command,check=True)
            env=dict(os.environ);env['ASAN_OPTIONS']='detect_leaks=1';env['UBSAN_OPTIONS']='halt_on_error=1'
            result=subprocess.run([str(exe),str(tmp/'sysfs')],env=env,text=True,capture_output=True)
            if out:(out/(name+'.log')).write_text(result.stdout+result.stderr)
            if name=='positive':
                print(result.stdout,end='')
                if result.returncode:print(result.stderr,end='');raise RuntimeError('positive runtime failed')
            elif not result.returncode or 'Assertion' not in result.stderr:
                print(result.stderr,end='');raise RuntimeError('negative did not fail intended runtime assertion: '+name)
            else:print('REJECTED:',name)
            results.append({'case':name,'returncode':result.returncode,'assertion':next((line for line in result.stderr.splitlines() if 'Assertion' in line),None)})
            if name=='positive' and out:(out/'extraction.cpp').write_text(text)
    manifest={'files':input_hashes,'extraction_sha256':hashlib.sha256(source.encode()).hexdigest(),'results':results,'limits':'host recording boundary fixture; no actual GPU/device/physical acceptance; AML BeginRender final caller fragment only'}
    manifest['inputs_changed_during_run']=[name for name,digest in input_hashes.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest]
    if out:(out/'results.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if manifest['inputs_changed_during_run']:raise RuntimeError('inputs changed during run; rerun final source: '+','.join(manifest['inputs_changed_during_run']))

if __name__=='__main__':main()
