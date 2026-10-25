#!/usr/bin/env python3
"""Replay actual texture clipping and GLES batching with an owned command recorder.

Compiles both CGUITexture::Render overloads, texture orientation, GraphicContext
clip methods and GLES Begin/End/Draw from pinned old and current source. Visible
batches must preserve exact ordered backend calls, initialized vertex/index data,
colors, depth and captured quads. Fully rejected batches retain their original
prefetch upload checks and timing, with no later binding/shader/blend/draw work;
actual clipping, rather than nominal bounds, decides whether draw setup runs.

Textures, transforms, shader/GL calls and resource readiness are owned fixtures.
This is CPU logic/API-order evidence, not real GL execution, GPU performance,
async resource lifetime, pixels or device verification. No source is rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

BASELINE = 'd49327ba12d6b654951d4f4d9fc0a003865affd6'
FILES = ('xbmc/guilib/GUITexture.cpp', 'xbmc/guilib/GUITextureGLES.cpp',
         'xbmc/guilib/GUITextureGLES.h', 'xbmc/windowing/GraphicContext.cpp')


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


COMMON = r'''
#include "utils/Geometry.h"
#include <array>
#include <vector>
#include <stack>
#include <memory>
#include <string>
#include <cstdio>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>
using GLint=int;using GLfloat=float;using GLubyte=unsigned char;using GLushort=unsigned short;
constexpr int GL_BLEND=1,GL_SRC_ALPHA=2,GL_ONE_MINUS_SRC_ALPHA=3,GL_ONE_MINUS_DST_ALPHA=4;
constexpr int GL_ONE=5,GL_FLOAT=6,GL_TRIANGLES=7,GL_UNSIGNED_SHORT=8,GL_TEXTURE0=9;
static unsigned long checks=0,cases=0,emptyWitnesses=0,visibleWitnesses=0;
static void Check(bool value,const char* reason){++checks;if(!value)throw std::runtime_error(reason);}
static int64_t Bits(float v){uint32_t b;std::memcpy(&b,&v,sizeof(b));return b;}
struct Event {std::string op;std::vector<int64_t> values;
 bool operator==(const Event&b)const{return op==b.op&&values==b.values;}};
struct Attr {int count=0;std::size_t stride=0;const unsigned char*data=nullptr;};
struct Draw {std::vector<std::array<int64_t,7>> vertices;std::vector<unsigned> indices;
 bool operator==(const Draw&b)const{return vertices==b.vertices&&indices==b.indices;}};
struct Recorder {
 std::vector<Event> events;std::vector<Draw> draws;std::vector<std::array<int64_t,12>> quads;
 std::map<int,Attr> attrs;std::set<int> enabled;bool blend=false;int shader=-1,active=GL_TEXTURE0;
 void Clear(){events.clear();draws.clear();quads.clear();attrs.clear();enabled.clear();blend=false;shader=-1;active=GL_TEXTURE0;}
};
static Recorder rec;
static void Emit(const char* op,std::initializer_list<int64_t>values={}){rec.events.push_back({op,values});}
void glBlendFuncSeparate(int a,int b,int c,int d){Emit("blendfunc",{a,b,c,d});}
void glEnable(int x){Emit("enable",{x});if(x==GL_BLEND)rec.blend=true;}
void glDisable(int x){Emit("disable",{x});if(x==GL_BLEND)rec.blend=false;}
void glUniform4f(int l,float a,float b,float c,float d){Emit("uniform4",{l,Bits(a),Bits(b),Bits(c),Bits(d)});}
void glUniform1f(int l,float a){Emit("uniform1",{l,Bits(a)});}
void glVertexAttribPointer(int l,int n,int t,int normalized,std::size_t stride,const void*p)
{Emit("attrib",{l,n,t,normalized,static_cast<int64_t>(stride)});rec.attrs[l]={n,stride,static_cast<const unsigned char*>(p)};}
void glEnableVertexAttribArray(int l){Emit("attrib-enable",{l});rec.enabled.insert(l);}
void glDisableVertexAttribArray(int l){Emit("attrib-disable",{l});rec.enabled.erase(l);}
static float AttrAt(int location,std::size_t index,int channel){const auto&a=rec.attrs.at(location);float v;
 std::memcpy(&v,a.data+index*a.stride+channel*sizeof(float),sizeof(v));return v;}
void glDrawElements(int mode,std::size_t count,int type,const void*ptr){
 Emit("draw",{mode,static_cast<int64_t>(count),type});Check(count%6==0,"whole indexed quads");
 Draw draw;const auto*idx=static_cast<const GLushort*>(ptr);
 for(std::size_t i=0;i<count;++i)draw.indices.push_back(idx[i]);
 for(std::size_t i=0;i<count/6*4;++i){std::array<int64_t,7>v{};
  for(int j=0;j<3;++j)v[j]=Bits(AttrAt(1,i,j));
  for(int j=0;j<2;++j)v[3+j]=Bits(AttrAt(2,i,j));
  // The other two fields are intentionally uninitialized when diffuse is absent.
  if(rec.enabled.count(3))for(int j=0;j<2;++j)v[5+j]=Bits(AttrAt(3,i,j));
  draw.vertices.push_back(v);}
 rec.draws.push_back(std::move(draw));}
void glActiveTexture(int unit){Emit("active",{unit});rec.active=unit;}
namespace UTILS {namespace COLOR{using Color=unsigned int;}}
namespace KODI {namespace UTILS{namespace GL{
 enum class ColorChannel{R,G,B,A};
 unsigned char GetChannelFromARGB(ColorChannel c,unsigned int v){return(v>>(c==ColorChannel::A?24:c==ColorChannel::R?16:c==ColorChannel::G?8:0))&255;}
}}}
namespace MathUtils{int round_int(double x){return static_cast<int>(std::round(x));}}
enum RENDER_ORDER{RENDER_ORDER_ALL_BACK_TO_FRONT,RENDER_ORDER_FRONT_TO_BACK,RENDER_ORDER_BACK_TO_FRONT};
enum class ShaderMethodGLES{SM_MULTI,SM_MULTI_BLENDCOLOR,SM_TEXTURE_NOBLEND_GUI,SM_TEXTURE};
struct CRenderSystemGLES{
 bool uniformColor=true;
 void EnableGUIShader(ShaderMethodGLES s){Emit("shader-enable",{static_cast<int>(s)});rec.shader=static_cast<int>(s);}
 void DisableGUIShader(){Emit("shader-disable");rec.shader=-1;}
 int GUIShaderGetPos(){return 1;}int GUIShaderGetCoord0(){return 2;}int GUIShaderGetCoord1(){return 3;}
 int GUIShaderGetUniCol(){return uniformColor?4:-1;}int GUIShaderGetDepth(){return 5;}
};
struct CTexture{int id=0;bool alpha=false;
 // Include the number of quads already captured: moving uploads after geometry
 // must fail even if their relative order among GL calls remains unchanged.
 void LoadToGPU(){Emit("upload-check",{id,static_cast<int64_t>(rec.quads.size())});}
 void BindToUnit(unsigned u){Emit("bind",{id,u});}
 bool HasAlpha(){return alpha;}};
struct CGraphicContext{
 std::stack<CRect>m_clipRegions;std::stack<CPoint>m_origins;RENDER_ORDER order=RENDER_ORDER_ALL_BACK_TO_FRONT;
 bool transform=false;unsigned mergeAlpha=255;
 bool SetClipRegion(float,float,float,float);void RestoreClipRegion();void ClipRect(CRect&,CRect&,CRect*);
 unsigned MergeColor(unsigned c){return (((mergeAlpha*(c>>24))/255)<<24)|(c&0x00ffffff);}
 float GetTransformDepth(int d=0){return .5f+d*.01f;}float GetNormalizedDepth(int d){return d*.01f;}
 RENDER_ORDER GetRenderOrder(){return order;}
 float ScaleFinalXCoord(float x,float y){return transform?x+.25f*y+3.5f:x;}
 float ScaleFinalYCoord(float x,float y){return transform?y-.25f*x+4.5f:y;}
 float ScaleFinalZCoord(float x,float y){return transform?x*.125f+y*.25f:0;}
 void CaptureControlQuad(float*x,float*y,float*z){std::array<int64_t,12>q{};
  for(int i=0;i<4;++i){q[i*3]=Bits(x[i]);q[i*3+1]=Bits(y[i]);q[i*3+2]=Bits(z[i]);}rec.quads.push_back(q);}
};
struct Window{CGraphicContext gfx;bool limited=false;CGraphicContext&GetGfxContext(){return gfx;}bool UseLimitedColor(){return limited;}};
static Window window;static CRenderSystemGLES renderer;
struct CServiceBroker{static Window*GetWinSystem(){return &window;}};
struct InfoColor{bool set=false;unsigned color=0xffffffff;
 explicit operator bool()const{return set;}explicit operator unsigned()const{return color;}};
struct Info{CRect border{};bool m_infill=true;int orientation=0;InfoColor diffuseColor;};
struct TexArray{std::vector<std::shared_ptr<CTexture>>m_textures;bool m_texCoordsArePixels=false;int m_orientation=0;
 std::size_t size()const{return m_textures.size();}};
'''

DECLARATIONS = r'''
class CGUITexture{public:
 virtual~CGUITexture()=default;void Render(int32_t depthOffset=0,int32_t overrideDepth=-1);
 void Render(float,float,float,float,float,float,float,float,float,float);
 virtual void Begin(UTILS::COLOR::Color)=0;virtual void End()=0;
 virtual void Draw(float*,float*,float*,const CRect&,const CRect&,int)=0;
 int GetOrientation()const;void OrientateTexture(CRect&,float,float,int);
 bool m_visible=true;TexArray m_texture,m_diffuse;int m_currentFrame=0;Info m_info;
 unsigned m_diffuseColor=0xffffffff;unsigned char m_alpha=255;
 float m_depth=.5f,m_width=100,m_height=100,m_posX=100,m_posY=100;
 CRect m_vertex{100,100,200,200};float m_frameWidth=100,m_frameHeight=100;
 float m_texCoordsScaleU=.01f,m_texCoordsScaleV=.01f,m_diffuseScaleU=1,m_diffuseScaleV=1,m_diffuseU=1,m_diffuseV=1;
 CPoint m_diffuseOffset;
};
@PACKED@
class CGUITextureGLES:public CGUITexture{public:
 CGUITextureGLES(){m_texture.m_textures.push_back(std::make_shared<CTexture>());}
 void Begin(UTILS::COLOR::Color)override;void End()override;
 void Draw(float*,float*,float*,const CRect&,const CRect&,int)override;
 @DEFER_DECL@
 UTILS::COLOR::Color m_pendingColor{0};std::array<GLubyte,4>m_col;
 std::vector<PackedVertex>m_packedVertices;std::vector<GLushort>m_idx;
 CRenderSystemGLES*m_renderSystem=&renderer;
};
'''


def variant(sources, current):
    texture, gles, header, _ = [sources[path] for path in FILES]
    decl = DECLARATIONS.replace('@PACKED@', function(header, 'struct PackedVertex') + ';')
    decl = decl.replace('@DEFER_DECL@', 'void BeginDraw(UTILS::COLOR::Color);' if current else '')
    methods = [function(texture, sig) for sig in (
        'void CGUITexture::Render(int32_t depthOffset', 'void CGUITexture::Render(float left,',
        'void CGUITexture::OrientateTexture(', 'int CGUITexture::GetOrientation() const')]
    methods += [function(gles, sig) for sig in (
        'void CGUITextureGLES::Begin(', 'void CGUITextureGLES::End(', 'void CGUITextureGLES::Draw(float')]
    if current:
        methods.append(function(gles, 'void CGUITextureGLES::BeginDraw('))
    return decl + '\n' + '\n\n'.join(methods)


TESTS = r'''
struct Config{
 int clip=0,borders=0,orientation=0,diffuseOrientation=0;bool diffuse=false,alpha=false,limited=false;
 unsigned color=0xffffffff;unsigned char opacity=255;bool infoColor=false,transform=false;
 bool visible=true,emptyTexture=false,pixels=false,uniformColor=true;int pass=0,depthOffset=0,overrideDepth=-1;
};
template<class T>static void Configure(T&t,const Config&c){
 window=Window{};renderer.uniformColor=c.uniformColor;window.limited=c.limited;window.gfx.transform=c.transform;
 window.gfx.order=static_cast<RENDER_ORDER>(c.pass);window.gfx.mergeAlpha=c.infoColor?192:255;
 window.gfx.m_clipRegions.push(CRect(0,0,500,500));
 if(c.clip==1)t.m_vertex=CRect(600,600,700,700);
 if(c.clip==2)t.m_vertex=CRect(450,450,550,550);
 if(c.clip==3)t.m_vertex=CRect(500,100,600,200);
 if(c.clip==4){t.m_vertex=CRect(-20,-30,180,170);t.m_posX=t.m_posY=0;}
 if(c.clip==5){window.gfx.m_origins.push(CPoint(10,20));t.m_vertex=CRect(450,450,550,550);}
 if(c.clip==6){window.gfx.m_clipRegions.pop();window.gfx.m_clipRegions.push(CRect(120,130,170,180));}
 if(c.clip==7){t.m_vertex=CRect(600,600,800,800);t.m_posX=t.m_posY=600;}
 if(c.borders){t.m_info.border=CRect(10,15,20,25);t.m_info.m_infill=c.borders==1;}
 t.m_visible=c.visible;t.m_info.orientation=c.diffuseOrientation;t.m_texture.m_orientation=c.orientation;
 t.m_texture.m_texCoordsArePixels=c.pixels;
 if(c.pixels){t.m_texCoordsScaleU=t.m_texCoordsScaleV=1;t.m_diffuseScaleU=t.m_diffuseScaleV=100;}
 t.m_texture.m_textures[0]->alpha=c.alpha;t.m_texture.m_textures[0]->id=0;
 t.m_diffuseColor=c.color;t.m_alpha=c.opacity;t.m_info.diffuseColor.set=c.infoColor;
 t.m_info.diffuseColor.color=c.color;t.m_diffuseOffset={.125f,.25f};
 if(c.diffuse){auto d=std::make_shared<CTexture>();d->id=1;d->alpha=c.alpha;t.m_diffuse.m_textures.push_back(d);}
 if(c.emptyTexture)t.m_texture.m_textures.clear();
}
struct Result{std::vector<Event>events;std::vector<Draw>draws;std::vector<std::array<int64_t,12>>quads;
 std::size_t clips,origins;bool blend;int shader,active;};
static Result ResultNow(){return{rec.events,rec.draws,rec.quads,window.gfx.m_clipRegions.size(),window.gfx.m_origins.size(),rec.blend,rec.shader,rec.active};}
template<class T>static Result Run(T&t,const Config&c){Configure(t,c);rec.Clear();t.Render(c.depthOffset,c.overrideDepth);return ResultNow();}
static std::vector<Event> UploadsOnly(const std::vector<Event>&events){std::vector<Event>result;
 for(const auto&e:events){if(e.op=="upload-check")result.push_back(e);}
 return result;}
template<class T>static std::vector<Event> BeginUploads(const T&t){std::vector<Event>result;
 result.push_back({"upload-check",{t.m_texture.m_textures[t.m_currentFrame]->id,0}});
 if(t.m_diffuse.size())result.push_back({"upload-check",{t.m_diffuse.m_textures[0]->id,0}});
 return result;}
static void Compare(const Result&old,const Result&now){
 ++cases;Check(old.quads==now.quads,"quad coordinates and capture depth preserved");
 Check(old.clips==now.clips&&old.origins==now.origins,"clip/origin restoration preserved");
 if(!old.draws.empty()){
  ++visibleWitnesses;Check(old.events==now.events,"nonempty GPU command sequence preserved");
  Check(old.draws==now.draws,"nonempty packed geometry and indices preserved");
  Check(old.blend==now.blend&&old.shader==now.shader&&old.active==now.active,"nonempty backend restoration preserved");
 }else{
  const auto uploads=UploadsOnly(old.events);
  Check(now.events==uploads&&now.draws.empty(),"empty batch preserves uploads without draw setup");
  if(old.events.size()>uploads.size())++emptyWitnesses;
 }
}
static void Matrix(){
 for(int clip=0;clip<8;++clip)for(int border:{0,1,2})for(bool diffuse:{false,true})
 for(bool alpha:{false,true})for(bool limited:{false,true})for(int pass:{0,1,2}){
  Config c;c.clip=clip;c.borders=border;c.diffuse=diffuse;c.alpha=alpha;c.limited=limited;c.pass=pass;
  c.color=alpha?0x80604020:0xffffffff;c.opacity=alpha?173:255;c.depthOffset=7;c.overrideDepth=42;
  old::CGUITextureGLES b;now::CGUITextureGLES n;Compare(Run(b,c),Run(n,c));
 }
 for(int orientation=0;orientation<8;++orientation)for(int diffuseOrientation=0;diffuseOrientation<8;++diffuseOrientation)
 for(bool pixels:{false,true})for(int clip:{0,2,4,5,6}){
  Config c;c.diffuse=true;c.orientation=orientation;c.diffuseOrientation=diffuseOrientation;c.pixels=pixels;
  c.clip=clip;c.borders=1;c.color=0xc01895ef;c.infoColor=true;c.transform=true;c.uniformColor=orientation%2==0;
  old::CGUITextureGLES b;now::CGUITextureGLES n;Compare(Run(b,c),Run(n,c));
 }
 for(bool visible:{false,true})for(bool empty:{false,true}){
  Config c;c.visible=visible;c.emptyTexture=empty;old::CGUITextureGLES b;now::CGUITextureGLES n;Compare(Run(b,c),Run(n,c));
 }
}
static void KnownGeometry(){
 Config c;c.clip=2;old::CGUITextureGLES b;now::CGUITextureGLES n;auto old=Run(b,c);auto got=Run(n,c);Compare(old,got);
 Check(got.quads.size()==1&&got.draws.size()==1,"partial clipping retains one quad");
 const std::array<int64_t,12>q={Bits(450),Bits(450),Bits(0),Bits(500),Bits(450),Bits(0),Bits(500),Bits(500),Bits(0),Bits(450),Bits(500),Bits(0)};
 Check(got.quads[0]==q,"independent partial clip coordinates");
 const auto&v=got.draws[0].vertices;
 Check(v.size()==4&&v[0][3]==Bits(0)&&v[0][4]==Bits(0)&&v[2][3]==Bits(.5f)&&v[2][4]==Bits(.5f),"independent partial clip texture coordinates");
 Check(got.draws[0].indices==std::vector<unsigned>({0,1,2,2,3,0}),"independent quad index order");
 for(int border:{1,2}){
  Config x;x.borders=border;old::CGUITextureGLES ob;now::CGUITextureGLES nn;auto r=Run(nn,x);Compare(Run(ob,x),r);
  Check(r.quads.size()==(border==1?9u:8u),"independent nine-slice and no-infill counts");
  Check(r.draws.size()==1&&r.draws[0].indices.size()==r.quads.size()*6,"all slice quads share one batch");
 }
}
static void Repeated(){
 old::CGUITextureGLES b;now::CGUITextureGLES n;
 for(int i=0;i<16;++i){Config c;c.color=i%2?0xa0704820:0xffffffff;c.diffuse=i%3==0;c.limited=i%2;
  c.clip=i%4==1?1:i%4==3?3:2;
  // Reuse batching vectors and indices, while resetting only fixture resources.
  b.m_diffuse.m_textures.clear();n.m_diffuse.m_textures.clear();Compare(Run(b,c),Run(n,c));
 }
 // Begin retains prefetch uploads, even after a populated prior cycle.
 const auto uploads=BeginUploads(n);rec.Clear();n.Begin(0x40123456);
 Check(rec.events==uploads,"Begin preserves upload timing and defers draw setup");n.End();
 Check(rec.events==uploads&&rec.draws.empty(),"empty repeated Begin/End has uploads only");
 for(unsigned color:{0xffffffffu,0x40123456u}){
  float x[4]={10,20,20,10},y[4]={30,30,40,40},z[4]={.25f,.25f,.25f,.25f};
  b.m_diffuse.m_textures.clear();n.m_diffuse.m_textures.clear();window=Window{};
  rec.Clear();b.Begin(color);b.Draw(x,y,z,CRect(0,0,1,1),CRect(0,0,1,1),0);b.End();auto r=ResultNow();
  rec.Clear();n.Begin(color);Check(rec.events==BeginUploads(n),"Begin preserves upload timing and defers draw setup");
  n.Draw(x,y,z,CRect(0,0,1,1),CRect(0,0,1,1),0);n.End();Compare(r,ResultNow());
 }
}
int main(){try{Matrix();KnownGeometry();Repeated();
 Check(emptyWitnesses>100&&visibleWitnesses>100,"matrix observes real old empty work and visible preservation");
 std::printf("PASS %lu cases %lu checks; %lu empty old-work witnesses, %lu preserved nonempty batches\n",cases,checks,emptyWitnesses,visibleWitnesses);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def mutations(code):
    changes = {
        'empty-end-guard-omitted': ('if (m_packedVertices.empty())\n    return;', '(void)0;'),
        'main-upload-omitted': ('texture->LoadToGPU();', '(void)texture;'),
        'eager-begin': ('m_pendingColor = color;', 'BeginDraw(color);\n  m_pendingColor = color;'),
        'missing-begin-clear': ('m_packedVertices.clear();', '(void)0;'),
        'stale-color': ('m_pendingColor = color;', 'm_pendingColor = 0xffffffff;'),
        'skip-nonempty': ('BeginDraw(m_pendingColor);', 'return;'),
        'late-clear': ('BeginDraw(m_pendingColor);', 'BeginDraw(m_pendingColor);\n  m_packedVertices.clear();'),
        'diffuse-upload-omitted': ('m_diffuse.m_textures[0]->LoadToGPU();', '(void)0;'),
        'shader-restore-omitted': ('m_renderSystem->DisableGUIShader();', '(void)0;'),
        'vertex-position-changed': ('vertices[i].x = x[i];', 'vertices[i].x = x[i] + 1.0f;'),
    }
    for name, (old, new) in changes.items():
        assert code.count(old) == 1, 'Mutation anchor ambiguous: ' + name
        yield name, code.replace(old, new, 1)
    # Move the main upload from Begin to End, preserving its order relative to
    # subsequent driver calls. The independent quad-phase tag must reject it.
    assert code.count('texture->LoadToGPU();') == 1
    assert code.count('BeginDraw(m_pendingColor);') == 1
    yield 'main-upload-after-geometry', code.replace('texture->LoadToGPU();', '(void)texture;', 1).replace(
        'BeginDraw(m_pendingColor);',
        'm_texture.m_textures[m_currentFrame]->LoadToGPU();\n  BeginDraw(m_pendingColor);', 1)


MUTATION_WITNESSES = {
    'empty-end-guard-omitted': 'empty batch preserves uploads without draw setup',
    'main-upload-omitted': 'nonempty GPU command sequence preserved',
    'main-upload-after-geometry': 'nonempty GPU command sequence preserved',
    'eager-begin': 'nonempty GPU command sequence preserved',
    'missing-begin-clear': 'empty batch preserves uploads without draw setup',
    'stale-color': 'nonempty GPU command sequence preserved',
    'skip-nonempty': 'nonempty GPU command sequence preserved',
    'late-clear': 'nonempty GPU command sequence preserved',
    'diffuse-upload-omitted': 'nonempty GPU command sequence preserved',
    'shader-restore-omitted': 'nonempty GPU command sequence preserved',
    'vertex-position-changed': 'nonempty packed geometry and indices preserved',
}


def semantic_failure(run, witness):
    return (run.returncode == 1 and run.stderr == 'FAIL ' + witness + '\n' and
            not any(x in run.stdout + run.stderr for x in (
                'AddressSanitizer', 'UndefinedBehaviorSanitizer', 'LeakSanitizer',
                'runtime error:', 'DEADLYSIGNAL', 'Sanitizer CHECK failed')))


def check_classifier():
    witness = 'expected assertion'
    def result(code=1, stdout='', stderr='FAIL expected assertion\n'):
        return subprocess.CompletedProcess([], code, stdout, stderr)
    assert semantic_failure(result(), witness)
    for code in (0, 2, -6, -11, 139):
        assert not semantic_failure(result(code), witness)
    for stderr in ('', 'FAIL another assertion\n', 'expected assertion\n'):
        assert not semantic_failure(result(stderr=stderr), witness)
    for marker in ('AddressSanitizer', 'UndefinedBehaviorSanitizer', 'LeakSanitizer',
                   'runtime error:', 'DEADLYSIGNAL', 'Sanitizer CHECK failed'):
        assert not semantic_failure(result(stdout=marker), witness)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default=BASELINE)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    check_classifier()
    root = Path(__file__).resolve().parents[1]
    out = args.out or Path(tempfile.mkdtemp(prefix='gui-texture-gles-empty-'))
    out.mkdir(parents=True, exist_ok=True)
    baseline = subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=root, text=True).strip()
    old = {p: subprocess.check_output(['git', 'show', baseline + ':' + p], cwd=root, text=True) for p in FILES}
    current = {p: (root / p).read_text() for p in FILES}
    graphics = '\n'.join(function(current[FILES[3]], sig) for sig in (
        'bool CGraphicContext::SetClipRegion(', 'void CGraphicContext::RestoreClipRegion(',
        'void CGraphicContext::ClipRect('))
    prefix = COMMON + graphics + '\nnamespace old {\n' + variant(old, False) + '\n}\nnamespace now {\n'
    code = variant(current, True)
    records = []
    env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'}
    for name, body in [('current', code), *mutations(code)]:
        source, exe = out / (name + '.cpp'), out / name
        source.write_text(prefix + body + '\n}\n' + TESTS)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter', '-fno-omit-frame-pointer', '-I', str(root / 'xbmc')]
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined', '-fno-pie', '-no-pie']
        command += [str(source), '-o', str(exe)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=120)
        (out / (name + '-compile.txt')).write_text(built.stdout + built.stderr)
        assert built.returncode == 0, name + ' compile failed; see ' + str(out)
        run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=60)
        (out / (name + '-stdout.txt')).write_text(run.stdout)
        (out / (name + '-stderr.txt')).write_text(run.stderr)
        witness = MUTATION_WITNESSES.get(name)
        accepted = (run.returncode == 0 and not run.stderr if name == 'current'
                    else semantic_failure(run, witness))
        records.append({'name': name, 'compile_argv': command, 'compile_exit': built.returncode,
                        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'exit': run.returncode, 'witness': witness, 'accepted': accepted})
        assert accepted, name + ' did not produce required semantic result; see ' + str(out)
    receipt = {'baseline': baseline, 'sanitize': args.sanitize, 'limits': __doc__,
               'source_hashes': {p: hashlib.sha256(current[p].encode()).hexdigest() for p in FILES},
               'baseline_source_hashes': {p: hashlib.sha256(old[p].encode()).hexdigest() for p in FILES},
               'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'results': records}
    (out / 'results.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print((out / 'current-stdout.txt').read_text(), end='')
    print('PASS %d semantic controls; receipts %s' % (len(records) - 1, out))


if __name__ == '__main__':
    main()
