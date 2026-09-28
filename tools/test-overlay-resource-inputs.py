#!/usr/bin/env python3
"""Production CPU image preparation/upload admission and owned matrix setters.

Real image/style geometry types, pixel conversion and constructor bodies; GL,
texture upload, capabilities and service/context state are recording stubs.
This does not test GPU pixels, shader execution or context switching on a device.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    folder = ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers'
    source = (folder / 'OverlayRendererGLES.cpp').read_text()
    header = (folder / 'OverlayRendererGLES.h').read_text()
    util = (folder / 'OverlayRendererUtil.cpp').read_text()
    base = (ROOT / 'xbmc/rendering/RenderSystem.h').read_text()
    code = PRELUDE.replace('@PREPARED@', function(header, 'struct PreparedImage') + ';')
    util_header = (folder / 'OverlayRendererUtil.h').read_text()
    code = code.replace('@QUADS@', function(util_header, 'struct SQuad\n') + ';\n' + function(util_header, 'struct SQuads\n') + ';')
    code = code.replace('@GLYPH_TYPES@', function(header, 'struct VERTEX') + ';\n' + function(header, 'struct Page') + ';')
    code = code.replace('@TOKEN_METHODS@', '\n'.join(function(base, name) for name in [
        'RenderTargetToken CaptureRenderTarget()', 'bool IsRenderTargetCurrent(', 'void InvalidateRenderTarget()']))
    code += '\nnamespace OVERLAY {\n'
    for name in ['float SrgbToLinear(', 'int LinearToSrgb8(', 'static uint32_t build_rgba(int a,',
                 'void convert_rgba(const CDVDOverlayImage&']:
        code += function(util, name) + '\n'
    code += '}\n' + function(source, 'uint32_t PremultiplyPlain(')
    for name in ['COverlayTextureGLES::PreparedImage COverlayTextureGLES::PrepareImage(',
                 'COverlayTextureGLES::COverlayTextureGLES(const CDVDOverlayImage&',
                 'COverlayTextureGLES::~COverlayTextureGLES()',
                 'bool COverlayTextureGLES::IsValid()',
                 'COverlayGlyphGLES::COverlayGlyphGLES(',
                 'COverlayGlyphGLES::~COverlayGlyphGLES()',
                 'bool COverlayGlyphGLES::IsValid()']:
        code += '\n' + function(source, name)
    for cls, path, sig in [
        ('Yuv', folder / 'VideoShaders/YUV2RGBShaderGLES.h', 'void SetMatrices('),
        ('Filter', folder / 'VideoShaders/VideoFilterShaderGLES.h', 'virtual void SetMatrices('),
        ('Composite', ROOT / 'xbmc/rendering/gles/GuiCompositeShaderGLES.h', 'void SetProjection(')]:
        code += '\nstruct ' + cls + ' {\n' + function(path.read_text(), sig)
        code += '\nstd::array<float,16> m_projection{},m_modelview{};const float* m_proj=nullptr;const float* m_model=nullptr;};\n'
    return code + TESTS


def main():
    with tempfile.TemporaryDirectory(prefix='overlay-resource-inputs-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n#define PIXEL_RSHIFT 16\n#define PIXEL_GSHIFT 8\n#define PIXEL_BSHIFT 0\n')
        (out / 'test.cpp').write_text(harness())
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Overlay resource inputs: PASS (production preparation/constructor/admission/matrix setters; recording GL; ASan/UBSan)')


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstring>
#include <memory>
#include <vector>
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "rendering/RenderResource.h"
#include "rendering/gles/TextureResources.h"
#include "utils/Geometry.h"
using GLint=int;using GLubyte=uint8_t;struct ASS_Image {};
using GLuint=uint32_t;using GLfloat=float;using GLenum=int;
constexpr int GL_TEXTURE_2D=1,GL_TEXTURE_WRAP_S=2,GL_TEXTURE_WRAP_T=3,GL_CLAMP=4,
 GL_TEXTURE_MAG_FILTER=5,GL_TEXTURE_MIN_FILTER=6,GL_LINEAR=7,GL_MAX_TEXTURE_SIZE=8;
#define USE_PREMULTIPLIED_ALPHA 1
struct CRenderSystemBase {virtual ~CRenderSystemBase()=default;};
struct CRenderSystemGLES:CRenderSystemBase {
 bool current=true,ready=true;
 std::shared_ptr<CGLESTextureResources> resources=std::make_shared<CGLESTextureResources>();
 std::shared_ptr<const uint8_t> m_renderTargetIdentity=std::make_shared<const uint8_t>(0);
 uint64_t m_renderTargetGeneration=1;
 bool CanRender()const{return current&&ready&&resources->IsOpen()&&resources->IsOwner();}
 bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>& r)const{return CanRender()&&r==resources;}
 std::shared_ptr<CGLESTextureResources> GetTextureResources(){return resources;}
 @TOKEN_METHODS@
};
CRenderSystemGLES renderSystem;
struct CServiceBroker {static CRenderSystemBase* GetRenderSystem(){return &renderSystem;}};
int generated=0,uploads=0,failNameAfter=-1;std::vector<uint32_t> uploaded;
void glGenTextures(int n,GLuint* p){assert(n==1&&renderSystem.CanRender());if(failNameAfter==0){*p=0;return;}if(failNameAfter>0)--failNameAfter;*p=++generated;}
void glGetIntegerv(int,GLint* p){assert(renderSystem.CanRender());*p=2048;}
void glBindTexture(int,GLuint){assert(renderSystem.CanRender());}
void glTexParameteri(int,int,int){assert(renderSystem.CanRender());}
void glGenerateMipmap(int){assert(renderSystem.CanRender());}
void LoadTexture(int,int width,int height,int stride,float* u,float* v,bool alpha,const void* pixels){
 assert(renderSystem.CanRender());++uploads;*u=*v=1;uploaded.clear();if(alpha)return;
 for(int row=0;row<height;++row){auto* bytes=static_cast<const uint8_t*>(pixels)+row*stride;
  auto* data=reinterpret_cast<const uint32_t*>(bytes);uploaded.insert(uploaded.end(),data,data+width);}
}
namespace OVERLAY {
@QUADS@
std::vector<SQuads> packedPages;bool invalidatePacking=false;
bool convert_quads(ASS_Image*,std::vector<SQuads>& pages,int){
 pages=packedPages;if(invalidatePacking)renderSystem.InvalidateRenderTarget();return !pages.empty();
}
struct COverlay {
 enum{POSITION_RELATIVE,POSITION_ABSOLUTE,ALIGN_VIDEO,ALIGN_SCREEN_AR,ALIGN_SCREEN};
 int m_pos=0,m_align=0;float m_x=0,m_y=0,m_width=0,m_height=0,m_source_width=0,m_source_height=0;
 bool m_rawPqMenu=false,m_plainPmaMenu=false,m_isBitmapOverlay=false;
 bool IsSquareResolution(float r){return r>1.22f&&r<1.34f;}
};
struct COverlayTextureGLES:COverlay {
 @PREPARED@
 static PreparedImage PrepareImage(const CDVDOverlayImage&,bool,RenderTargetToken,bool=false);
 COverlayTextureGLES(const CDVDOverlayImage&,CRect&,PreparedImage);
 ~COverlayTextureGLES();bool IsValid()const;
 std::shared_ptr<CGLESTextureResources> m_textureResources;
 GLuint m_texture=0;float m_u=0,m_v=0;bool m_pma=false,m_isHdrPqAuthored=false;
};
struct COverlayGlyphGLES:COverlay {
 @GLYPH_TYPES@
 COverlayGlyphGLES(ASS_Image*,float,float);~COverlayGlyphGLES();bool IsValid()const;
 std::shared_ptr<CGLESTextureResources> m_textureResources;
 std::vector<Page> m_pages;bool m_uploadFailed=false;
};
}
using namespace OVERLAY;
'''
TESTS = r'''
int main(){
 CRect source(0,0,1920,1080);
 CDVDOverlayImage o;o.width=2;o.height=1;o.linesize=8;o.source_width=1920;o.source_height=1080;
 o.x=40;o.y=50;o.m_isHdrPq=true;
 const uint32_t colors[]={0x80ffffff,0xff123456};
 o.pixels.resize(sizeof(colors));std::memcpy(o.pixels.data(),colors,sizeof(colors));
 auto target=renderSystem.CaptureRenderTarget();
 auto raw=COverlayTextureGLES::PrepareImage(o,true,target);
 auto regular=COverlayTextureGLES::PrepareImage(o,false,target);
 assert(generated==0&&uploads==0&&raw.rawPqMenu&&raw.premultiplied&&regular.premultiplied);
 assert(raw.pixels==std::vector<uint32_t>({0x80808080,0xff123456}));
 assert(regular.pixels==std::vector<uint32_t>({0x80bababa,0xff123456}));
 // Disc menu graphics on SDR output: plain premultiply, and the texture records it.
 auto plain=COverlayTextureGLES::PrepareImage(o,false,target,true);
 assert(plain.plainPmaMenu&&plain.premultiplied&&plain.pixels==std::vector<uint32_t>({0x80808080,0xff123456}));
 // Padded ARGB rows: the plain path honours linesize.
 {CDVDOverlayImage padded;padded.width=1;padded.height=2;padded.linesize=8;
  const uint32_t rows[]={0x80ffffff,0xdeadbeef,0xff123456,0xdeadbeef};
  padded.pixels.resize(sizeof(rows));std::memcpy(padded.pixels.data(),rows,sizeof(rows));
  auto p=COverlayTextureGLES::PrepareImage(padded,false,target,true);
  assert(p.stride==4&&p.pixels==std::vector<uint32_t>({0x80808080,0xff123456}));}
 // Prepared bytes are owned; later producer edits cannot affect upload.
 o.pixels.assign(o.pixels.size(),0);
 {COverlayTextureGLES texture(o,source,raw);assert(texture.IsValid()&&uploads==1);
  assert(uploaded==raw.pixels&&texture.m_rawPqMenu&&!texture.m_isHdrPqAuthored);
  assert(texture.m_pos==COverlay::POSITION_RELATIVE&&texture.m_align==COverlay::ALIGN_VIDEO);
  assert(texture.m_x==41/1920.0f&&texture.m_y==50.5f/1080.0f);
  renderSystem.InvalidateRenderTarget();assert(texture.IsValid()); // Surface/route provenance != GL namespace.
 }
 assert(renderSystem.resources->TakeRetired()==std::vector<uint32_t>{1});
 // Returning to the same route/size cannot admit retained CPU work from an old generation.
 int before=generated;
 {COverlayTextureGLES stale(o,source,raw);assert(!stale.IsValid()&&generated==before);}
 auto fresh=COverlayTextureGLES::PrepareImage(o,false,renderSystem.CaptureRenderTarget());
 renderSystem.current=false;
 {COverlayTextureGLES unbound(o,source,fresh);assert(!unbound.IsValid()&&generated==before);}
 renderSystem.current=true;
 auto oldNamespace=renderSystem.resources;
 {COverlayTextureGLES texture(o,source,fresh);assert(texture.IsValid());
  oldNamespace->Close();renderSystem.resources=std::make_shared<CGLESTextureResources>();
  assert(!texture.IsValid());}
 assert(renderSystem.resources->TakeRetired().empty());
 // A failed name allocation cannot be cached as a valid overlay.
 failNameAfter=0;fresh=COverlayTextureGLES::PrepareImage(o,false,renderSystem.CaptureRenderTarget());
 before=uploads;
 {COverlayTextureGLES failed(o,source,fresh);assert(!failed.IsValid()&&uploads==before);}
 failNameAfter=-1;
 // Actual glyph upload/retirement consumes controlled packer output. Atlas
 // packing is a stub boundary; this fixture checks upload and retirement.
 {COverlayGlyphGLES empty(nullptr,1920,1080);assert(empty.IsValid()&&empty.m_pages.empty());}
 SQuads page;page.size_x=page.size_y=2;page.texture={1,2,3,4};
 SQuad quad{};quad.w=quad.h=2;quad.a=255;page.quad.push_back(quad);
 packedPages={page,page};before=generated;
 {COverlayGlyphGLES glyph(nullptr,1920,1080);assert(glyph.IsValid()&&glyph.m_pages.size()==2);
  assert(glyph.m_pages[0].vertex.size()==4&&glyph.m_pages[0].vertex[3].a==255);
  assert(glyph.m_pages[0].vertex[3].x==2/1920.0f);
  renderSystem.InvalidateRenderTarget();assert(glyph.IsValid());}
 assert(renderSystem.resources->TakeRetired()==std::vector<uint32_t>({uint32_t(before+1),uint32_t(before+2)}));
 before=generated;failNameAfter=1;
 {COverlayGlyphGLES partial(nullptr,1920,1080);assert(!partial.IsValid()&&partial.m_pages.size()==1);}
 assert(renderSystem.resources->TakeRetired()==std::vector<uint32_t>{uint32_t(before+1)});
 failNameAfter=-1;invalidatePacking=true;before=generated;
 {COverlayGlyphGLES stale(nullptr,1920,1080);assert(!stale.IsValid()&&generated==before);}
 invalidatePacking=false;
 oldNamespace=renderSystem.resources;
 {COverlayGlyphGLES glyph(nullptr,1920,1080);assert(glyph.IsValid());oldNamespace->Close();
  renderSystem.resources=std::make_shared<CGLESTextureResources>();assert(!glyph.IsValid());}
 assert(renderSystem.resources->TakeRetired().empty());
 // PQ palette selection and padded indexed rows use actual production conversion.
 o.width=1;o.height=2;o.linesize=2;o.pixels={0,255,1,255};o.palette={0xff123456,0x80ffffff};
 o.pqMenuPalette={0xff654321,0x800000ff};
 raw=COverlayTextureGLES::PrepareImage(o,true,renderSystem.CaptureRenderTarget());
 regular=COverlayTextureGLES::PrepareImage(o,false,renderSystem.CaptureRenderTarget());
 assert(raw.pixels==std::vector<uint32_t>({0xff654321,0x80000080}));
 assert(regular.pixels==std::vector<uint32_t>({0xff123456,0x80bababa}));
 assert(COverlayTextureGLES::PrepareImage(o,false,renderSystem.CaptureRenderTarget(),true).pixels==
        std::vector<uint32_t>({0xff123456,0x80808080}));
 o.pqMenuPalette.clear();raw=COverlayTextureGLES::PrepareImage(o,true,renderSystem.CaptureRenderTarget());
 assert(raw.pixels==std::vector<uint32_t>({0xff123456,0x80808080}));
 {auto plainTex=COverlayTextureGLES::PrepareImage(o,false,renderSystem.CaptureRenderTarget(),true);
  COverlayTextureGLES texture(o,source,plainTex);assert(texture.IsValid()&&texture.m_plainPmaMenu&&uploaded==plainTex.pixels);}
 renderSystem.resources->TakeRetired();
 // The setters copy exactly at their late evaluation points, including capture transforms.
 float projection[16],model[16];for(int i=0;i<16;++i){projection[i]=i+1;model[i]=100+i;}
 Yuv yuv;Filter filter;Composite composite;
 yuv.SetMatrices(projection,model);filter.SetMatrices(projection,model);composite.SetProjection(projection);
 std::fill_n(projection,16,-1);std::fill_n(model,16,-2);
 for(int i=0;i<16;++i){assert(yuv.m_proj[i]==i+1&&yuv.m_model[i]==100+i);
  assert(filter.m_proj[i]==i+1&&filter.m_model[i]==100+i&&composite.m_proj[i]==i+1);}
 yuv.SetMatrices(projection,model);filter.SetMatrices(projection,model);composite.SetProjection(projection);
 assert(yuv.m_proj[0]==-1&&filter.m_model[15]==-2&&composite.m_proj[15]==-1);
 yuv.SetMatrices(nullptr,nullptr);filter.SetMatrices(nullptr,nullptr);composite.SetProjection(nullptr);
 assert(!yuv.m_proj&&!yuv.m_model&&!filter.m_proj&&!filter.m_model&&!composite.m_proj);
}
'''

if __name__ == '__main__':
    main()
