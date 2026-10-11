#!/usr/bin/env python3
"""Execute production picture projection, publication, GUI transform and label.

Host service/renderer/native-lifecycle substitutes; no GUI/device/HDMI claim.
Requires g++, fmt headers (FMT_INCLUDE if absent from /usr/include).
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = 'xbmc/cores/VideoPlayer/VideoRenderers/'


def read(path):
    return (ROOT / path).read_text()


def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def harness():
    manager = read(VIDEO + 'RenderManager.cpp')
    cache = read('xbmc/cores/DataCacheCore.cpp')
    header = read('xbmc/cores/DataCacheCore.h')
    label = read('xbmc/guilib/guiinfo/PlayerGUIInfo.cpp')
    label = label[label.index('    case PLAYER_PROCESS_VIDEO_ACTIVE_PICTURE:'):
                  label.index('    case PLAYER_PROCESS_VIDEO_DOVI_HAS_L5:')]
    # Registration and IDs are part of the optional Python surface.
    assert '{"video.active.picture", PLAYER_PROCESS_VIDEO_ACTIVE_PICTURE}' in read('xbmc/GUIInfoManager.cpp')
    labels = read('xbmc/guilib/guiinfo/GUIInfoLabels.h')
    offset = re.search(r'PLAYER_PROCESS_VIDEO_ACTIVE_PICTURE \(PLAYER_PROCESS \+ (\d+)\)', labels)[1]
    assert labels.count('(PLAYER_PROCESS + ' + offset + ')') == 1
    frame = function(manager, 'void CRenderManager::FrameMove(')
    assert 'UpdateActivePicture();' in frame
    for signature in ['void CRenderManager::PreInitOnMain(', 'void CRenderManager::UnInitOnMain(',
                      'bool CRenderManager::FlushOnMain(', 'bool CRenderManager::Configure(const VideoPicture&']:
        assert 'SetVideoActivePicture({});' in function(manager, signature)
    source = PRELUDE.replace('SNAPSHOT_STRUCT', function(header, 'struct VideoActivePicture') + ';')
    resolution = read('xbmc/windowing/Resolution.cpp')
    source += function(resolution, 'RESOLUTION_INFO::RESOLUTION_INFO(int width,') + '\n'
    source += function(resolution, 'RESOLUTION_INFO::RESOLUTION_INFO(const RESOLUTION_INFO&') + '\n'
    source += function(cache, 'void CDataCacheCore::SetVideoActivePicture(') + '\n'
    source += function(cache, 'CDataCacheCore::VideoActivePicture CDataCacheCore::GetVideoActivePicture(') + '\n'
    source += function(cache, 'void CDataCacheCore::Reset(') + '\n'
    source += function(read('xbmc/windowing/GraphicContext.cpp'), 'void CGraphicContext::GetGUIScaling(') + '\n'
    source += function(read(VIDEO + 'BaseRenderer.cpp'), 'void CBaseRenderer::CalcDestRect(') + '\n'
    for signature in ['CRect CRenderManager::CalcPictureActiveArea(', 'void CRenderManager::UpdateActivePicture(',
                      'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestUnInit(',
                      'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestFlush(']:
        source += function(manager, signature) + '\n'
    source += function(read('xbmc/utils/AMLUtils.cpp'), 'void aml_dv_set_xbmc_osd(') + '\n'
    source += 'bool Label(std::string& value) { switch (PLAYER_PROCESS_VIDEO_ACTIVE_PICTURE) {\n' + label + '} return false; }\n'
    return source


PRELUDE = r'''
#include "cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.h"
#include "utils/TransformMatrix.h"
#include "windowing/Resolution.h"
#include <fmt/format.h>
#include <atomic>
#include <cassert>
#include <cmath>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
using CCriticalSection = std::recursive_mutex;
constexpr int RENDER_STEREO_MODE_OFF=0,STATE_CONFIGURED=2,PLAYER_PROCESS_VIDEO_ACTIVE_PICTURE=1680;
struct CDataCacheCore {
  SNAPSHOT_STRUCT
  CCriticalSection m_videoPlayerSection,m_stateSection,m_renderSection,m_contentSection;
  struct Info {VideoActivePicture activePicture;} m_playerVideoInfo;
  struct Content {void Reset(){}} m_contentInfo;
  struct Empty {} m_stateInfo,m_renderInfo,m_timeInfo;
  bool m_playerStateChanged=false,m_hasAVInfoChanges=false;
  void SetVideoActivePicture(VideoActivePicture);
  VideoActivePicture GetVideoActivePicture();
  void Reset();
  struct Metadata {
    bool has_level5_metadata=false,level5_detected=false;
    uint16_t level5_active_area_top_offset=0,level5_active_area_bottom_offset=0,
      level5_active_area_left_offset=0,level5_active_area_right_offset=0;
  } meta;
  double requested=0;
  bool GetAVChangeExtended(){return false;}
  Metadata GetVideoDoViFrameMetadata(double pts){requested=pts;return meta;}
} cache;
struct CSettings {
  static constexpr int SETTING_COREELEC_AMLOGIC_DV_LEVEL5=1,SETTING_LOOKANDFEEL_SKINZOOM=2,
    SETTING_VIDEOPLAYER_ERRORINASPECT=3;
};
struct Settings {
  bool l5=true;int zoom=0;
  bool GetBool(int){return l5;} int GetInt(int id){return id==CSettings::SETTING_LOOKANDFEEL_SKINZOOM?zoom:0;}
} settings;
struct Component {Settings* GetSettings(){return &settings;}} component;
struct CGraphicContext {
  int stereo=0;RESOLUTION m_Resolution=RES_DESKTOP;
  RESOLUTION_INFO info;
  CGraphicContext(){info.iWidth=1920;info.iHeight=1080;info.Overscan.right=1920;info.Overscan.bottom=1080;info.fPixelRatio=1;}
  int GetStereoMode(){return stereo;}
  RESOLUTION GetVideoResolution(){return m_Resolution;}
  RESOLUTION_INFO GetResInfo(){return info;}
  void GetGUIScaling(const RESOLUTION_INFO&,float&,float&,TransformMatrix*);
} graphics;
struct WinSystem {CGraphicContext& GetGfxContext(){return graphics;}} win;
constexpr int WINDOW_HOME=10000,WINDOW_VIDEO_MENU=12901;
struct Property {std::string value="0";std::string asString(){return value;}} osdProperty;
struct CGUIWindow {Property GetProperty(const char*){return osdProperty;}} home;
struct WindowManager {
  CGUIWindow* GetWindow(int){return &home;}
  bool HasVisibleDialog(){return false;} bool IsWindowVisible(int){return false;}
} wm;
struct GUI {WindowManager& GetWindowManager(){return wm;}} gui;
struct CServiceBroker {
  static CDataCacheCore& GetDataCacheCore(){return cache;}
  static Component* GetSettingsComponent(){return &component;}
  static WinSystem* GetWinSystem(){return &win;}
  static GUI* GetGUI(){return &gui;}
};
bool s_pm4kActive=true;CGUIWindow* s_pm4kHome=&home;
std::atomic<bool> s_discMenuVisible=false;
int s_lastOsd=-1,emittedOsd=-1;
struct CSysfsPath {CSysfsPath(const char* path,int value){assert(std::string(path).find("dolby_vision_xbmc_osd")!=std::string::npos);emittedOsd=value;}};
void aml_dv_dump_state(const char*){}
struct CDisplaySettings {
  bool nonlinear=false;
  static CDisplaySettings& GetInstance(){static CDisplaySettings s;return s;}
  bool IsNonLinearStretched(){return nonlinear;}
};
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
bool native=true,manual=false,autoLb=false,additive=true,detected=false;
uint16_t mt=0,mb=0,ml=0,mr=0,dt=0,db=0,dl=0,dr=0;
bool aml_subtitle_native_dv(){return native;}
bool aml_dv_get_l5_override(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r){if(!manual)return false;t=mt;b=mb;l=ml;r=mr;return true;}
bool aml_dv_auto_letterbox_active(){return autoLb;}
bool aml_dv_auto_letterbox_additive(){return additive;}
bool aml_subtitle_detect_active_area_get(int,int,uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r,bool allowSubtitleEstimate=false){assert(!allowSubtitleEstimate);if(!detected)return false;t=dt;b=db;l=dl;r=dr;return true;}
struct MathUtils {static int round_int(double value){return std::lround(value);}};
struct CBaseRenderer {
  unsigned int m_renderOrientation=0;
  CRect src{0,0,1920,1080},dst{0,0,1920,1080},view{0,0,1920,1080};
  std::function<void()> update=[]{};
  void UpdateVideoRect(){update();}
  void GetVideoRect(CRect& s,CRect& d,CRect& v){s=src;d=dst;v=view;}
  void CalcDestRect(float,float,float,float,float,float,float,CRect&);
};
struct CAMLNativeTransaction {bool TryBegin(){return true;}};
struct CRenderLifecycle {
  struct Request {std::function<std::optional<bool>()> run;};
  template<class F> std::shared_ptr<Request> Submit(F f){return std::make_shared<Request>(Request{f});}
  void AdvanceSession(){}
};
struct CRenderManager {
  CDataCacheCore& m_dataCacheCore=cache;
  CCriticalSection m_statelock;
  bool m_closing=false,m_presentstarted=true,m_showVideo=true;
  int m_renderState=STATE_CONFIGURED;unsigned int m_orientation=0;
  uint64_t m_lifecycleGeneration=0;
  struct Picture {int iWidth=1920,iHeight=1080;std::string stereoMode;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;} m_picture;
  struct Frame {struct Present {double pts=123;} present;};
  std::shared_ptr<Frame> m_frameSelection=std::make_shared<Frame>();
  CBaseRenderer renderer;CBaseRenderer* m_pRenderer=&renderer;
  std::shared_ptr<CRenderLifecycle> m_lifecycle=std::make_shared<CRenderLifecycle>();
  CRect CalcPictureActiveArea(const CRect&,const CRect&,const CRect&,double,bool,bool=false);
  void UpdateActivePicture();
  std::shared_ptr<CRenderLifecycle::Request> RequestUnInit();
  std::shared_ptr<CRenderLifecycle::Request> RequestFlush(bool,bool);
  void CancelDeferredDV(){}
  void UnInitOnMain(){m_dataCacheCore.SetVideoActivePicture({});m_presentstarted=false;}
  bool FlushOnMain(bool){m_dataCacheCore.SetVideoActivePicture({});m_presentstarted=false;m_frameSelection.reset();return true;}
};
struct AppPlayer {bool playing=true;bool IsPlayingVideo(){return playing;}} app;
AppPlayer* m_appPlayer=&app;
struct StringUtils {
  template<class... Args> static std::string Format(fmt::format_string<Args...> text,Args&&... args){return fmt::format(text,std::forward<Args>(args)...);}
};
void near(float a,float b){assert(std::abs(a-b)<0.025f);}
void rect(CRect a,float l,float t,float r,float b){near(a.x1,l);near(a.y1,t);near(a.x2,r);near(a.y2,b);}
'''


TESTS = r'''
int main(){
  CRenderManager m;
  auto check=[&](float l,float t,float r,float b){m.UpdateActivePicture();auto p=cache.GetVideoActivePicture();assert(p.valid);rect(p.bounds,l,t,r,b);};
  auto invalid=[&]{m.UpdateActivePicture();assert(!cache.GetVideoActivePicture().valid);};
  check(0,0,1920,1080); // full-frame without metadata
  cache.meta.has_level5_metadata=true;
  cache.meta.level5_active_area_top_offset=140;cache.meta.level5_active_area_bottom_offset=140;
  check(0,140,1920,940);assert(cache.requested==123);
  // Actual PM4K OSD-property consumer writes the signal, while geometry stays raw.
  for(bool signal:{false,true,false}){
    osdProperty.value=signal?"1":"0";aml_dv_set_xbmc_osd();assert(emittedOsd==signal);check(0,140,1920,940);
  }
  cache.meta={};detected=true;dt=db=120;check(0,120,1920,960);
  cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=140;cache.meta.level5_active_area_bottom_offset=140;
  check(0,140,1920,940); // authored beats detector
  detected=false;
  m.renderer.src={0,0,1920,100};invalid(); // valid active area completely cropped away
  m.renderer.src={0,0,1920,1080};m.renderer.dst={0,1000,1920,2080};invalid(); // only bars in view
  m.renderer.dst={0,0,1920,1080};
  m.renderer.src={0,100,1920,980};check(0,49.0909f,1920,1030.9091f); // source crop
  m.renderer.src={0,0,1920,1080};m.renderer.dst={0,-100,1920,1180};check(0,65.9259f,1920,1014.0741f);
  m.renderer.dst={0,140,1920,940};cache.meta={};native=false;check(0,140,1920,940); // padded non-DV
  native=true;autoLb=true;additive=true;
  m.m_picture.iWidth=3840;m.m_picture.iHeight=1600;m.renderer.src={0,0,3840,1600};
  check(0,140,1920,940); // no double padding
  m.m_picture.iHeight=2024;m.renderer.src.y2=2024;m.renderer.dst={0,34,1920,1046};
  cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=208;cache.meta.level5_active_area_bottom_offset=208;
  check(0,138,1920,942); // additive 68+208 in 2160 output
  additive=false;check(0,34,1920,1046); // phantom replacement
  manual=true;mt=mb=276;check(0,138,1920,942); // final absolute override equals composed source+pad
  m.m_picture.iHeight=1600;m.renderer.src.y2=1600;m.renderer.dst={0,140,1920,940};
  mt=mb=280;check(0,140,1920,940); // manual padded canvas, not coded frame
  mt=mb=0;check(0,0,1920,1080); // zero absolute override includes added bars
  mt=mb=400;check(0,200,1920,880);
  for(bool signal:{false,true,false}){
    osdProperty.value=signal?"1":"0";aml_dv_set_xbmc_osd();assert(emittedOsd==signal);check(0,200,1920,880);
  }
  m.renderer.dst={0,1000,1920,1800};check(0,1060,1920,1080); // last active pixels remain
  m.renderer.dst={0,1040,1920,1840};invalid(); // manual active area shifted out of view
  m.renderer.dst={0,140,1920,940};
  settings.l5=false;cache.meta={};autoLb=false;check(0,140,1920,940); // manual not applicable
  settings.l5=true;mt=mb=4000;invalid(); // invalid override does not pretend full-frame safety
  manual=false;cache.meta={};
  // Execute renderer's production zoom/shift calculation before publication.
  m.renderer.update=[&]{m.renderer.CalcDestRect(0,0,1920,1080,2.4f,1.0f,1.0f,m.renderer.dst);};
  check(0,280,1920,1080);
  m.renderer.update=[&]{m.renderer.CalcDestRect(0,0,1920,1080,2.4f,1.2f,-1.0f,m.renderer.dst);};
  check(0,0,1920,960);
  m.renderer.update=[]{};m.renderer.src={0,0,1920,1080};m.m_picture.iWidth=1920;m.m_picture.iHeight=1080;
  m.renderer.dst={0,140,1920,940};m.renderer.view={0,0,1920,1080};
  // Same inverse GUI scaling as XML controls, with calibration and GUI zoom.
  graphics.info.Overscan.left=100;graphics.info.Overscan.right=1820;
  graphics.info.Overscan.top=40;graphics.info.Overscan.bottom=1040;
  graphics.info.guiInsets.left=20;graphics.info.guiInsets.right=20;
  graphics.info.guiInsets.top=10;graphics.info.guiInsets.bottom=10;
  settings.zoom=10;graphics.info.fPixelRatio=1.2f;
  m.UpdateActivePicture();auto converted=cache.GetVideoActivePicture();assert(converted.valid);
  RESOLUTION_INFO ref;ref.iWidth=1920;ref.iHeight=1080;float sx,sy;TransformMatrix matrix;
  graphics.GetGUIScaling(ref,sx,sy,&matrix);
  float x=converted.bounds.x1,y=converted.bounds.y1,z=0;matrix.TransformPosition(x,y,z);near(x,0);near(y,140);
  x=converted.bounds.x2;y=converted.bounds.y2;matrix.TransformPosition(x,y,z);near(x,1920);near(y,940);
  settings.zoom=0;graphics=CGraphicContext{};
  m.m_orientation=90;invalid();m.m_orientation=0;
  m.m_picture.stereoMode="left_right";invalid();m.m_picture.stereoMode.clear();
  graphics.stereo=1;invalid();graphics.stereo=0;
  CDisplaySettings::GetInstance().nonlinear=true;invalid();CDisplaySettings::GetInstance().nonlinear=false;
  m.m_showVideo=false;invalid();m.m_showVideo=true;
  m.m_presentstarted=false;invalid();m.m_presentstarted=true;
  m.renderer.view={};invalid();m.renderer.view={0,0,1920,1080};check(0,140,1920,940);
  std::string value;assert(Label(value));std::cout<<value<<'\n';
  // Read contract remains optional and rejects non-video player state.
  app.playing=false;assert(Label(value));std::cout<<value<<'\n';app.playing=true;
  // Close submission invalidates before native cleanup, and refuses republish.
  auto close=m.RequestUnInit();assert(!cache.GetVideoActivePicture().valid);invalid();
  close->run();invalid();
  m.m_closing=false;m.m_presentstarted=true;m.m_frameSelection=std::make_shared<CRenderManager::Frame>();
  check(0,140,1920,940);
  auto next=m.RequestFlush(false,true);assert(!cache.GetVideoActivePicture().valid);invalid();
  next->run();invalid();
  m.m_presentstarted=true;m.m_frameSelection=std::make_shared<CRenderManager::Frame>();check(0,140,1920,940);
  auto seek=m.RequestFlush(false,false);seek->run();invalid();
  cache.SetVideoActivePicture({{1,2,3,4},true});cache.Reset();assert(!cache.GetVideoActivePicture().valid);
  // Value copies must never tear validity or edges under competing readers/writers.
  std::atomic<bool> done=false;
  std::thread writer([&]{for(int i=0;i<100000;++i){cache.SetVideoActivePicture({{1,2,3,4},true});cache.SetVideoActivePicture({});}done=true;});
  do{auto p=cache.GetVideoActivePicture();if(p.valid)rect(p.bounds,1,2,3,4);else assert(p.bounds.IsEmpty());}while(!done);
  writer.join();
  std::cout<<"PASS: active-picture production geometry, GUI conversion, atomic cache, JSON and lifecycle\n";
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='active-picture-test-') as tmp:
        out = Path(tmp)
        fmt_include = Path(os.environ.get('FMT_INCLUDE', '/usr/include'))
        shutil.copytree(fmt_include / 'fmt', out / 'fmt')
        (out / 'PlatformDefs.h').write_text('#pragma once\n')
        (out / 'test.cpp').write_text(harness() + TESTS)
        subprocess.run(['g++', '-std=c++17', '-DFMT_HEADER_ONLY', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', '-pthread', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), str(ROOT / (VIDEO + 'BitmapSubtitlePosition.cpp')),
                        '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], text=True, capture_output=True,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = result.stdout.splitlines()
        valid, invalid = map(json.loads, lines[:2])
        assert valid == dict(version=1, valid=True, coordinate_system='gui-1080', width=1920,
                             height=1080, left=0, top=140, right=1920, bottom=940)
        assert invalid == dict(version=1, valid=False, coordinate_system='gui-1080', width=1920,
                               height=1080, left=None, top=None, right=None, bottom=None)
        print(lines[-1])


if __name__ == '__main__':
    main()
