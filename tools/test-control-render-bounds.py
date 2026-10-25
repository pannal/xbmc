#!/usr/bin/env python3
"""Run production bounds capture/cache/control/label code with host GUI/GPU substitutes.

No native GUI, framebuffer, playback or HDMI verification. Requires g++ and fmt
headers; set FMT_INCLUDE to a directory containing fmt/ when needed.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-active-picture.py'))['function']


def read(path):
    return (ROOT / 'xbmc' / path).read_text()


def harness(control=None):
    if control is None:
        control = read('guilib/GUIControl.cpp')
    gfx = read('windowing/GraphicContext.cpp')
    cache = read('cores/DataCacheCore.cpp')
    info = read('guilib/guiinfo/GUIControlsGUIInfo.cpp')
    label = info[info.index('    case CONTROL_GET_RENDER_BOUNDS:'):info.index('    case CONTROL_GET_LABEL:')]
    translator = read('GUIInfoManager.cpp')
    start = translator.index('      if (prop.name == "getrenderbounds")')
    parser = function(translator[start:], 'if (prop.name == "getrenderbounds")')
    # Verify production integration points, not just the test substitutes.
    texture = function(read('guilib/GUITexture.cpp'), 'void CGUITexture::Render(float left,')
    assert texture.index('ClipRect(') < texture.index('CaptureControlQuad(') < texture.index('  Draw(')
    font = function(read('guilib/GUIFontTTF.cpp'), 'void CGUIFontTTF::DrawTextInternal(')
    assert 'newVertexBuffer.bounds = CRect(left, top, right, bottom);' in font
    assert 'context.CaptureControlText(buffer.bounds, x, y, dx, dy);' in font
    for signature in ['void CGUIWindowManager::Process(', 'bool CGUIWindowManager::Render(',
                      'void CGUIWindowManager::AfterRender(']:
        assert 'PublishRenderBounds();' in function(read('guilib/GUIWindowManager.cpp'), signature)
    remove = function(read('guilib/GUIWindowManager.cpp'), 'void CGUIWindowManager::Remove(int id)')
    assert remove.index('lock(') < remove.index('SetGUIRenderBounds({})')
    source = PRELUDE.replace('@STATE@', function(read('guilib/GUIControl.h'), 'struct RenderedBoundsState') + ';')
    for signature in ['void CGraphicContext::CaptureControlQuad(', 'void CGraphicContext::CaptureControlRect(',
                      'void CGraphicContext::CaptureControlText(', 'CRect CGraphicContext::GenerateAABB(']:
        source += function(gfx, signature) + '\n'
    for signature in ['void CDataCacheCore::SetGUIRenderBounds(',
                      'CDataCacheCore::GUIRenderBounds CDataCacheCore::GetGUIRenderBounds(',
                      'void CDataCacheCore::InvalidateGUIRenderBounds(']:
        source += function(cache, signature) + '\n'
    for signature in ['CGUIControl::~CGUIControl(', 'void CGUIControl::InvalidateRenderedBounds(',
                      'uint64_t CGUIControl::NextBoundsInstance(', 'CGUIRenderBounds CGUIControl::GetRenderedBounds(',
                      'void CGUIControl::AllocResources(', 'void CGUIControl::FreeResources(',
                      'void CGUIControl::DoProcess(', 'void CGUIControl::Process(', 'void CGUIControl::DoRender(']:
        source += function(control, signature) + '\n'
    source += function(read('guilib/GUIControlLookup.cpp'), 'void CGUIControlLookup::CollectRenderBounds(') + '\n'
    source += function(read('guilib/GUIButtonControl.cpp'), 'float CGUIButtonControl::GetWidth() const') + '\n'
    source += 'int Translate(const Property& prop) { ' + parser + ' return 0; }\n'
    source += 'bool Label(std::string& value, Info info) { switch (CONTROL_GET_RENDER_BOUNDS) {\n' + label + '} return false; }\n'
    return source + TESTS


PRELUDE = r'''
#define HAS_GLES 1
#include "guilib/GUIRenderBounds.h"
#include "utils/TransformMatrix.h"
#include <fmt/format.h>
#include <atomic>
#include <cassert>
#include <charconv>
#include <cmath>
#include <functional>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>
using CCriticalSection=std::recursive_mutex;
using CDirtyRegionList=std::vector<CRect>;
constexpr int RENDER_STEREO_MODE_OFF=0, RENDER_STEREO_MODE_MONO=1;
constexpr int GUICONTROL_BUTTON=1, DIRTY_STATE_CONTROL=1, ANIM_TYPE_CONDITIONAL=1;
constexpr int CONTROL_GET_RENDER_BOUNDS=29995;
#define GUIPROFILER_RENDER_BEGIN(x)
#define GUIPROFILER_RENDER_END(x)
#define CLAMP(x,a,b) std::clamp(x,a,b)
struct RESOLUTION_INFO {int iWidth=1920,iHeight=1080;};
struct CDataCacheCore {
 using GUIRenderBounds=CGUIRenderBounds; using GUIRenderBoundsMap=::GUIRenderBoundsMap;
 CCriticalSection m_guiSection; GUIRenderBoundsMap m_guiRenderBounds;
 void SetGUIRenderBounds(GUIRenderBoundsMap); GUIRenderBounds GetGUIRenderBounds(int,int);
 void InvalidateGUIRenderBounds(uint64_t);
};
struct Renderer {
 bool scissor=false; float projectX=0, projectY=0;
 void Project(float& x,float& y,float&) {x+=projectX;y+=projectY;}
 bool ScissorsCanEffectClipping() {return scissor;}
 CRect ClipRectToScissorRect(const CRect&);
};
struct CGraphicContext {
 CRect* m_controlBounds=nullptr; uint64_t m_controlBoundsFrame=1;
 TransformMatrix transform, reference; float sx=1,sy=1;
 CRect clip{-10000,-10000,10000,10000},scissors{0,0,3840,2160};
 std::vector<TransformMatrix> stack; int stereo=0,screenWidth=3840,screenHeight=2160;
 unsigned scissorReads=0;
 void BeginControlBounds(CRect* b) {m_controlBounds=b;} void EndControlBounds(){m_controlBounds=nullptr;}
 void CaptureControlQuad(const float*,const float*,const float*);
 void CaptureControlRect(const CRect&); void CaptureControlText(const CRect&,float,float,float,float);
 CRect GenerateAABB(const CRect&) const;
 void ScaleFinalCoords(float& x,float& y,float& z) const {transform.TransformPosition(x,y,z);}
 float ScaleFinalXCoord(float x,float y) const {return transform.TransformXCoord(x,y,0);}
 float ScaleFinalYCoord(float x,float y) const {return transform.TransformYCoord(x,y,0);}
 float GetGUIScaleX(){return sx;} float GetGUIScaleY(){return sy;}
 CRect GetClipRegion(){return clip;} CRect GetScissors(){++scissorReads;return scissors;}
 int GetWidth(){return screenWidth;} int GetHeight(){return screenHeight;} int GetStereoMode(){return stereo;}
 void GetGUIScaling(const RESOLUTION_INFO&,float& x,float& y,TransformMatrix* m){*m=reference;x=reference.m[0][0];y=reference.m[1][1];}
 uint64_t GetControlBoundsFrame(){return m_controlBoundsFrame;}
 void NextControlBoundsFrame(){++m_controlBoundsFrame;}
 void SetTransform(const TransformMatrix& m){stack.push_back(transform);transform=m;}
 TransformMatrix AddTransform(const TransformMatrix& m){stack.push_back(transform);transform*=m;return transform;}
 void RemoveTransform(){transform=stack.back();stack.pop_back();}
 void SetCameraPosition(const CPoint&){} void RestoreCameraPosition(){}
 void SetStereoFactor(float){} void RestoreStereoFactor(){}
 unsigned int MergeAlpha(unsigned int color){return color;}
};
struct WinSystem {CGraphicContext gfx; CGraphicContext& GetGfxContext(){return gfx;}};
WinSystem win; Renderer renderer; CDataCacheCore cache; bool services=true;
struct CServiceBroker {
 static WinSystem* GetWinSystem(){return &win;} static Renderer* GetRenderSystem(){return &renderer;}
 static CDataCacheCore& GetDataCacheCore(){assert(services);return cache;}
 static bool IsAddonInterfaceUp(){return services;}
};
CRect Renderer::ClipRectToScissorRect(const CRect& rect){return win.gfx.GenerateAABB(rect);}
struct CGUITexture {static void DrawQuad(const CRect&,unsigned int){}};
struct CAnimation {int GetType(){return 0;} void ResetAnimation(){}};
struct CGUIControl {
 static uint64_t NextBoundsInstance(); @STATE@
 RenderedBoundsState m_renderedBounds;
 TransformMatrix m_cachedTransform,m_transform; CPoint m_camera;
 CGUIControl* m_parentControl=nullptr;
 CRect m_renderRegion{0,0,3840,2160},m_hitRect;
 bool visible=true,m_isCulled=false,m_hasProcessed=true,m_hasCamera=false,renderable=true;
 bool m_bAllocated=false,m_bInvalidated=false;
 int ControlType=GUICONTROL_BUTTON,m_controlDirtyState=0; float m_stereo=0;
 unsigned int m_hitColor=0xffffffff;
 std::vector<CAnimation> m_animations; std::function<void()> draw=[]{};
 CGUIControl()=default; CGUIControl(const CGUIControl&)=default; ~CGUIControl();
 bool IsControlRenderable(){return renderable;} bool IsVisible() const{return visible;}
 bool Animate(unsigned int){return false;} void MarkDirtyRegion(){m_controlDirtyState|=1;}
 void UpdateControlStats(){} CRect CalcRenderRegion(){return m_renderRegion;}
 void Render(){draw();} void DoRender(); void DoProcess(unsigned int,CDirtyRegionList&);
 void Process(unsigned int,CDirtyRegionList&); void AllocResources(); void FreeResources(bool);
 CGUIRenderBounds GetRenderedBounds() const; void InvalidateRenderedBounds();
};
struct CGUIControlLookup {
 std::multimap<int,CGUIControl*> m_lookup;
 void CollectRenderBounds(int,GUIRenderBoundsMap&) const;
};
struct FakeLabel {float width=0; struct {float offsetX=0;} info;
 float GetTextWidth() const{return width;} auto GetLabelInfo() const{return info;}};
struct CGUIButtonControl {float m_minWidth=100,m_width=120,m_maxWidth=0;FakeLabel m_label,m_label2;float GetWidth() const;};
struct StringUtils {template<class... A> static std::string Format(fmt::format_string<A...> f,A&&... a){return fmt::format(f,std::forward<A>(a)...);}};
struct Info {int id=0,control=0,window=0;Info()=default;Info(int i,int c,int w):id(i),control(c),window(w){} int GetData1()const{return control;}int GetData2()const{return window;}};
using CGUIInfo=Info;
Info translated{};
int AddMultiInfo(Info info){translated=info;return 1;}
struct Property {std::string name="getrenderbounds";std::vector<std::string> args;
 int num_params()const{return args.size();}std::string param(int i)const{return args.at(i);}};
'''

TESTS = r'''
void eq(float a,float b){assert(std::abs(a-b)<0.001f);}
void rect(const CRect& a,float l,float t,float r,float b){eq(a.x1,l);eq(a.y1,t);eq(a.x2,r);eq(a.y2,b);}
int main(){
 auto& g=win.gfx; CRect out;
 // Hidden and alpha-culled controls reject before geometry/context queries.
 // Visible leaf clipping stays active; groups must still traverse outside bounds.
 for(bool visible:{false,true})for(bool culled:{false,true})
 for(bool renderable:{false,true})for(bool outside:{false,true}){
   CGUIControl probe;probe.visible=visible;probe.m_isCulled=culled;probe.renderable=renderable;
   if(outside)probe.m_renderRegion=CRect(4000,4000,4100,4100);
   int draws=0;probe.draw=[&]{++draws;};g.scissorReads=0;
   probe.DoRender();
   const bool eligible=visible&&!culled;
   if(g.scissorReads!=static_cast<unsigned>(eligible&&renderable)){
     std::cerr<<"FAIL hidden/cull scissor query\n";return 1;
   }
   if(draws!=static_cast<int>(eligible&&(!renderable||!outside))||!g.stack.empty()){
     std::cerr<<"FAIL visibility/clipping render or transform preservation\n";return 1;
   }
 }
 // Final projected texture vertices, including camera/projection offset.
 renderer.projectX=7;renderer.projectY=11;g.BeginControlBounds(&out);
 float x[4]{10,110,110,10},y[4]{20,20,60,60},z[4]{};
 g.CaptureControlQuad(x,y,z);rect(out,17,31,117,71);g.EndControlBounds();
 renderer.projectX=renderer.projectY=0;
 // Fractional raster correction follows shader clip. Both edge directions.
 g.transform=TransformMatrix::CreateTranslation(0.75f,0.75f);
 g.clip=CRect(0,0,100,100);out={};g.BeginControlBounds(&out);
 g.CaptureControlText(CRect(-10,-10,110,110),0,0,0,0);rect(out,1,1,101,101);
 g.transform=TransformMatrix::CreateTranslation(0.25f,0.25f);out={};
 g.CaptureControlText(CRect(-10,-10,110,110),0,0,0,0);rect(out,0,0,100,100);
 // Hardware structural scissors round screen edges, not local edges.
 renderer.scissor=true;g.transform=TransformMatrix::CreateTranslation(0.75f,0.75f);out={};
 g.CaptureControlText(CRect(-10,-10,110,110),0,0,0,0);rect(out,1,1,101,101);
 // Scroll offset and GUI scale correction precede group/animation projection.
 renderer.scissor=false;g.transform=TransformMatrix::CreateTranslation(10,20);
 g.sx=2;g.sy=3;g.clip=CRect(-1000,-1000,1000,1000);out={};
 g.CaptureControlText(CRect(1,2,4,6),30,40,5,-8);rect(out,47,58,53,70);
 g.sx=g.sy=1;g.EndControlBounds();
 // Parent placement and animated scale are in the cached final transform.
 CGUIControl parent,button;button.m_parentControl=&parent;
 button.m_cachedTransform=TransformMatrix::CreateTranslation(200,100)*TransformMatrix::CreateScaler(2,2);
 g.reference=TransformMatrix::CreateScaler(2,2);
 button.draw=[&]{g.CaptureControlRect(CRect(10,20,110,60));};button.DoRender();
 assert(button.GetRenderedBounds().valid);rect(button.GetRenderedBounds().bounds,110,70,210,110);
 // Dynamic text width uses actual native button calculation, not wrapper width.
 CGUIButtonControl dynamic;dynamic.m_label.width=180;dynamic.m_label.info.offsetX=10;
 eq(dynamic.GetWidth(),200);dynamic.m_label2.width=60;eq(dynamic.GetWidth(),270);
 g.NextControlBoundsFrame();button.draw=[&]{g.CaptureControlRect(CRect(10,20,10+dynamic.GetWidth(),60));};
 button.DoRender();rect(button.GetRenderedBounds().bounds,110,70,380,110);
 // Both render passes contribute, and repaint scissors do not shrink bounds.
 button.draw=[&]{g.CaptureControlRect(CRect(300,20,320,60));};g.scissors=CRect(200,100,220,120);
 button.DoRender();rect(button.GetRenderedBounds().bounds,110,70,420,110);
 g.NextControlBoundsFrame();assert(button.GetRenderedBounds().valid); // clean retained frame
 // Parent hiding/culling, dirty geometry, and stereo disable validity.
 parent.visible=false;assert(!button.GetRenderedBounds().visible);parent.visible=true;
 parent.m_isCulled=true;assert(!button.GetRenderedBounds().valid);parent.m_isCulled=false;
 g.scissors=CRect(0,0,3840,2160);button.MarkDirtyRegion();CDirtyRegionList dirty;
 g.transform=TransformMatrix();button.m_transform=button.m_cachedTransform;button.DoProcess(0,dirty);
 assert(!button.GetRenderedBounds().valid);button.DoRender();assert(button.GetRenderedBounds().valid);
 g.stereo=2;assert(!button.GetRenderedBounds().valid);button.DoRender();assert(!button.GetRenderedBounds().valid);
 g.stereo=0;button.DoRender();assert(button.GetRenderedBounds().valid);
 // Unsupported extra drawing paths cannot retain standard button bounds.
 button.m_hitColor=0xff000000;button.DoRender();assert(!button.GetRenderedBounds().valid);
 button.m_hitColor=0xffffffff;button.DoRender();assert(button.GetRenderedBounds().valid);
 // Calibration/mode changes invalidate retained reference coordinates.
 g.reference=TransformMatrix::CreateScaler(3,3);assert(!button.GetRenderedBounds().valid);
 g.reference=TransformMatrix::CreateScaler(2,2);
 g.screenWidth=1920;assert(!button.GetRenderedBounds().valid);g.screenWidth=3840;
 g.screenHeight=1080;assert(!button.GetRenderedBounds().valid);g.screenHeight=2160;
 // Rotated geometry includes all four projected corners.
 CRect rotated;g.BeginControlBounds(&rotated);
 g.transform=TransformMatrix::CreateZRotation(1.57079632679f,0,0,1);
 g.CaptureControlRect(CRect(10,20,30,40));rect(rotated,-40,10,-20,30);g.EndControlBounds();
 // Unique native lifetime and duplicate IDs.
 CGUIControl clone(button);assert(clone.m_renderedBounds.instance!=button.m_renderedBounds.instance);
 assert(!clone.GetRenderedBounds().valid);
 CGUIControlLookup lookup;lookup.m_lookup.emplace(901,&button);GUIRenderBoundsMap map;
 lookup.CollectRenderBounds(12005,map);cache.SetGUIRenderBounds(map);
 assert(cache.GetGUIRenderBounds(12005,901).valid);
 lookup.m_lookup.emplace(901,&clone);map.clear();lookup.CollectRenderBounds(12005,map);
 assert(!map.at({12005,901}).valid && map.at({12005,901}).visible);
 // Old teardown cannot erase a replacement's ID.
 auto replacement=button.GetRenderedBounds();replacement.instance=clone.m_renderedBounds.instance;
 cache.SetGUIRenderBounds({{{12005,901},replacement}});
 button.FreeResources(true);assert(cache.GetGUIRenderBounds(12005,901).instance==replacement.instance);
 clone.AllocResources();assert(!cache.GetGUIRenderBounds(12005,901).valid);
 // Cache is absent in StageOne-only tests and after StageTwo shutdown.
 services=false;{CGUIControl temporary;temporary.AllocResources();temporary.FreeResources(true);}services=true;
 // Explicit full window IDs, strict parsing and atomic JSON serialization.
 assert(Translate(Property{"getrenderbounds",{"77541","901"}})==1);
 assert(translated.window==77541 && translated.control==901);
 for(const auto& args:std::vector<std::vector<std::string>>{{"12005"},{"12005","0"},{"-1","901"},{"12005x","901"},{"12005","16777216"},{"2147483648","901"}})
   assert(Translate(Property{"getrenderbounds",args})==0);
 replacement.bounds=CRect(1500,900,1850,980);replacement.valid=true;replacement.visible=true;
 cache.SetGUIRenderBounds({{{12005,901},replacement}});std::string label;
 assert(Label(label,Info{CONTROL_GET_RENDER_BOUNDS,901,12005}));std::cout<<label<<'\n';
 cache.SetGUIRenderBounds({});Label(label,Info{CONTROL_GET_RENDER_BOUNDS,901,12005});std::cout<<label<<'\n';
 // Concurrent reads never tear a four-edge snapshot.
 std::atomic<bool> finished=false;std::thread writer([&]{for(int i=1;i<10000;i++){
   auto value=replacement;value.bounds=CRect(i,i+1,i+2,i+3);
   cache.SetGUIRenderBounds({{{12005,901},value}});}finished=true;});
 while(!finished){const auto value=cache.GetGUIRenderBounds(12005,901);
   if(value.valid){eq(value.bounds.y1,value.bounds.x1+1);eq(value.bounds.x2,value.bounds.x1+2);eq(value.bounds.y2,value.bounds.x1+3);}}
 writer.join();cache.SetGUIRenderBounds({});
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--baseline', default='d49327ba12d6b654951d4f4d9fc0a003865affd6')
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    context = nullcontext(str(args.output)) if args.output else tempfile.TemporaryDirectory(prefix='control-bounds-test-')
    with context as tmp:
        out = Path(tmp)
        fmt = Path(os.environ.get('FMT_INCLUDE', '/usr/include')) / 'fmt'
        shutil.copytree(fmt, out / 'fmt', dirs_exist_ok=True)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        baseline = subprocess.check_output(
            ['git', 'show', args.baseline + ':xbmc/guilib/GUIControl.cpp'], cwd=ROOT, text=True)
        receipts = []
        for name, source, expected in [('current', harness(), 0), ('baseline-eager-scissor', harness(baseline), 1)]:
            cpp, exe = out / (name + '.cpp'), out / name
            cpp.write_text(source)
            command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-DFMT_HEADER_ONLY',
                       '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                       '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined',
                       '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', '-pthread',
                       '-I', str(out), '-I', str(ROOT / 'xbmc'), str(cpp), '-o', str(exe)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True,
                                    env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0',
                                             UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1'))
            (out / (name + '-stdout.txt')).write_text(result.stdout)
            (out / (name + '-stderr.txt')).write_text(result.stderr)
            assert result.returncode == expected, (name, result.returncode, result.stderr)
            if expected:
                assert result.stderr == 'FAIL hidden/cull scissor query\n' and not result.stdout, result.stderr
            else:
                assert not result.stderr, result.stderr
                valid, invalid = map(json.loads, result.stdout.splitlines())
                assert valid['valid'] and valid['coordinate_system'] == 'gui-1080'
                assert [valid[k] for k in ('left','top','right','bottom')] == [1500,900,1850,980]
                assert not invalid['valid'] and not invalid['visible'] and invalid['instance'] == 0
                assert all(invalid[k] is None for k in ('left','top','right','bottom'))
            receipts.append({'name': name, 'compile_command': command, 'exit': result.returncode,
                             'harness_sha256': hashlib.sha256(source.encode()).hexdigest()})
        (out / 'results.json').write_text(json.dumps(
            {'baseline': args.baseline, 'source_sha256': hashlib.sha256(
                (ROOT / 'xbmc/guilib/GUIControl.cpp').read_bytes()).hexdigest(),
             'results': receipts, 'limits': __doc__}, indent=2) + '\n')
    print('Control render bounds/visibility: PASS (production extracts; ASan/UBSan; '
          '16 visibility/clipping cases; eager-scissor baseline rejected; GUI/GPU/services substituted)')


if __name__ == '__main__':
    main()
