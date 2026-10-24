#!/usr/bin/env python3
"""Execute the complete production DrawTextInternal against explicit host stubs.

Checks allocation-free warm hits and baseline-identical vertices/cache ownership
for the GL/GLES branch and both DX clipping branches. Backend/cache lookup,
HarfBuzz shaping, glyph rasterization and GPU upload are deterministic stubs;
this does not test real cache matching, real shaping, pixels, GPU or device speed.
The two supplied shadow/main calls are inputs, not a test of the shadow caller.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == 'tools' else Path.cwd()
SOURCE = 'xbmc/guilib/GUIFontTTF.cpp'
BASELINE = '7f0b8436de'
SIGNATURE = 'void CGUIFontTTF::DrawTextInternal('


def method(text):
    start = text.index(SIGNATURE)
    brace = text.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <list>
#include <memory>
#include <new>
#include <queue>
#include <string>
#include <vector>
static bool count_allocations=false;
static size_t allocations=0;
__attribute__((noinline)) void* operator new(std::size_t n){if(count_allocations)++allocations;void* p=std::malloc(n?n:1);if(!p)throw std::bad_alloc();return p;}
__attribute__((noinline)) void operator delete(void* p) noexcept {std::free(p);}
__attribute__((noinline)) void operator delete(void* p,std::size_t) noexcept {std::free(p);}
namespace UTILS { namespace COLOR {using Color=uint32_t;} }
using character_t=uint32_t;
using vecText=std::vector<character_t>;
constexpr uint32_t XBFONT_TRUNCATED=1,XBFONT_TRUNCATED_LEFT=2,XBFONT_JUSTIFIED=4,
 XBFONT_CENTER_Y=8,XBFONT_RIGHT=16,XBFONT_CENTER_X=32;
constexpr size_t MAX_TRANSLATED_VERTEX=128,VERTEX_PER_GLYPH=4,MAX_GLYPHS_PER_TEXT_LINE=128;
namespace MathUtils {inline int round_int(double x){return static_cast<int>(std::lround(x));}}
struct CRect {float x1=0,y1=0,x2=0,y2=0;CRect()=default;CRect(float a,float b,float c,float d):x1(a),y1(b),x2(c),y2(d){};};
struct SVertex {float x,y;uint32_t glyph,color;};
struct Character {float m_advance=0;uint32_t glyph=0;};
struct Glyph {struct{uint32_t cluster,codepoint;}m_glyphInfo;struct{int x_offset,y_offset;}m_glyphPosition;};
struct CGUIFontCacheStaticPosition {float x,y;CGUIFontCacheStaticPosition(float a,float b):x(a),y(b){};};
struct CGUIFontCacheDynamicPosition {float m_x=0,m_y=0,m_z=0;CGUIFontCacheDynamicPosition()=default;CGUIFontCacheDynamicPosition(float a,float b,float c):m_x(a),m_y(b),m_z(c){};};
struct CVertexBuffer {unsigned bufferHandle=0;size_t size=0;CRect bounds;std::vector<SVertex> data;};
using CGUIFontCacheStaticValue=std::shared_ptr<std::vector<SVertex>>;
struct CGraphicContext {
 bool capture=false;int captures=0;float scale=1;CRect captured;
 float ScaleFinalXCoord(float x,float)const{return x*scale;}
 float ScaleFinalYCoord(float,float y)const{return y*scale;}
 float ScaleFinalZCoord(float,float)const{return 0;}
 CRect GetClipRegion()const{return CRect(0,0,1920,1080);}
 bool IsCapturingControlBounds()const{return capture;}
 void CaptureControlText(const CRect& b,float x,float y,float dx,float dy){++captures;captured=CRect(b.x1+x+dx,b.y1+y+dy,b.x2+x+dx,b.y2+y+dy);}
};
// Stable-address storage and allocation-free lookup on hits. The production
// cache implementation/equality/expiry are intentionally not reproduced here.
template<class Value> struct Cache {
 struct Entry{vecText text;std::vector<uint32_t> colors;uint32_t align;float width,scale,x,y;bool scroll;Value value;};
 std::list<Entry> entries;int misses=0,hits=0;
 template<class Position> Value& Lookup(CGraphicContext& c,Position&,const std::vector<uint32_t>& colors,const vecText& text,uint32_t align,float width,bool scroll,std::chrono::steady_clock::time_point,bool& dirty){
  for(auto& e:entries)if(e.text==text&&e.colors==colors&&e.align==align&&e.width==width&&e.scale==c.scale&&e.scroll==scroll){++hits;dirty=false;return e.value;}
  ++misses;dirty=true;entries.push_back({text,colors,align,width,c.scale,0,0,scroll,{}});return entries.back().value;
 }
 void Flush(){entries.clear();}
};
struct Translation {
 float x,y,z;CVertexBuffer* buffer;
 Translation(float a,float b,float c,CVertexBuffer* v,CRect,float=0,float=0):x(a),y(b),z(c),buffer(v){}
};
struct RenderSystem {bool hardware=true;bool ScissorsCanEffectClipping()const{return hardware;}};
class CGUIFontTTF {
public:
 RenderSystem backend;RenderSystem* m_renderSystem=&backend;
 Cache<CVertexBuffer> m_dynamicCache;Cache<CGUIFontCacheStaticValue> m_staticCache;
 std::vector<Translation> m_vertexTrans;std::vector<SVertex> m_vertex;
 bool m_ellipseCached=false;float m_ellipsesWidth=0,m_originX=0,m_originY=0,m_cellHeight=10;
 int begins=0,ends=0,shapes=0,builds=0;Character character;
 void Begin(){++begins;}void End(){++ends;}
 void ValidateAlignments(uint32_t&){}
 std::vector<Glyph> GetHarfBuzzShapedGlyphs(const vecText& t){++shapes;std::vector<Glyph> v;for(uint32_t i=0;i<t.size();++i)v.push_back({{i,t[i]&0xffff},{0,0}});return v;}
 Character* GetCharacter(character_t ch,uint32_t){if((ch&0xffff)==0xffff)return nullptr;character={1.0f,ch&0xffff};return &character;}
 float GetTabSpaceLength()const{return 4;}
 float GetTextWidthInternal(const vecText& t,const std::vector<Glyph>&){return static_cast<float>(t.size());}
 void RenderCharacter(CGraphicContext&,float x,float y,Character* c,uint32_t color,bool,std::vector<SVertex>& v){if(!c->glyph)return;for(int i=0;i<4;++i)v.push_back({x+static_cast<float>(i%2),y+static_cast<float>(i/2),c->glyph,color});}
 CVertexBuffer CreateVertexBuffer(const std::vector<SVertex>& v){++builds;CVertexBuffer b;b.bufferHandle=v.empty()?0:static_cast<unsigned>(builds);b.size=v.size()/4;b.data=v;return b;}
 void ClearFrame(){m_vertexTrans.clear();m_vertex.clear();}
 void Flush(){ClearFrame();m_dynamicCache.Flush();m_staticCache.Flush();}
 void DrawTextInternal(CGraphicContext&,float,float,const std::vector<uint32_t>&,const vecText&,uint32_t,float,bool,float,float);
};
@METHOD@
static uint64_t digest=1469598103934665603ULL;
static unsigned checks=0;
static void Check(bool b,const char* what){++checks;if(!b){std::cerr<<"FAIL "<<what<<'\n';std::exit(1);}}
static bool Same(const std::vector<SVertex>& a,const std::vector<SVertex>& b){return a.size()==b.size()&&(a.empty()||std::memcmp(a.data(),b.data(),a.size()*sizeof(SVertex))==0);}
static void Hash(const std::vector<SVertex>& v){for(const auto& x:v){const auto* p=reinterpret_cast<const unsigned char*>(&x);for(size_t i=0;i<sizeof(x);++i){digest^=p[i];digest*=1099511628211ULL;}}}
static bool Hardware(const CGUIFontTTF& f){
#if defined(HAS_DX)
 return f.backend.hardware;
#else
 (void)f;return true;
#endif
}
static std::vector<SVertex> Snapshot(const CGUIFontTTF& f){if(Hardware(f)){Check(!f.m_vertexTrans.empty(),"hardware draw queued");return f.m_vertexTrans.back().buffer->data;}return f.m_vertex;}
static vecText Text(const char* s){vecText t;for(;*s;++s)t.push_back(static_cast<unsigned char>(*s));return t;}
static void RunCase(bool hardware,uint32_t alignment,float maxWidth,bool scrolling,bool capture,int expectedHitAllocations){
 CGUIFontTTF f;f.backend.hardware=hardware;CGraphicContext c;c.capture=capture;std::vector<uint32_t> colors{0xffffffff,0xff000000};vecText text=Text("AB CDEFG");text[1]|=1u<<16;
 f.DrawTextInternal(c,10.4f,20.4f,colors,text,alignment,maxWidth,scrolling,1,2);
 Check(f.shapes==1,"miss shapes once");Check(f.begins==1&&f.ends==1,"balanced miss begin/end");
 auto first=Snapshot(f);Check(!first.empty(),"miss emits vertices");Hash(first);
 const CRect firstBounds=c.captured;const int initialBuilds=f.builds;
 if(alignment&(XBFONT_TRUNCATED|XBFONT_TRUNCATED_LEFT)){Check(first.size()==20,"truncation emits five glyphs");unsigned dots=0;for(size_t i=0;i<first.size();i+=4)dots+=first[i].glyph=='.';Check(dots==3,"ellipsis retained");}
 for(int repeat=0;repeat<3;++repeat){
  f.ClearFrame();allocations=0;count_allocations=true;
  f.DrawTextInternal(c,10.4f,20.4f,colors,text,alignment,maxWidth,scrolling,1,2);
  count_allocations=false;Check(allocations==static_cast<size_t>(expectedHitAllocations),"warm hit allocations");
  Check(f.shapes==1,"hit does not reshape");Check(Same(first,Snapshot(f)),"hit vertices match miss");Hash(Snapshot(f));
  Check(f.builds==initialBuilds,"hit does not rebuild vertex buffer");
#if !defined(HAS_DX)
  if(capture)Check(c.captured.x1==firstBounds.x1&&c.captured.y1==firstBounds.y1&&c.captured.x2==firstBounds.x2&&c.captured.y2==firstBounds.y2,"cached text bounds unchanged");
#else
  (void)firstBounds;
#endif
 }
 if(!Hardware(f)){Check(f.m_staticCache.entries.front().value.use_count()==1,"static cache owns vertices after temporary expires");}
#if !defined(HAS_DX)
 Check(c.captures==(capture?4:0),"bounds captured on hits and miss");
#endif
 f.Flush();f.DrawTextInternal(c,10.4f,20.4f,colors,text,alignment,maxWidth,scrolling,1,2);Check(f.shapes==2,"flush forces rebuild");Check(Same(first,Snapshot(f)),"flush retains geometry");
 f.ClearFrame();c.scale=2;f.DrawTextInternal(c,10.4f,20.4f,colors,text,alignment,maxWidth,scrolling,1,2);Check(f.shapes==3,"scale change misses stub cache");
}
static void RunEmptyGeometry(bool hardware,int expectedHitAllocations){
 CGUIFontTTF f;f.backend.hardware=hardware;CGraphicContext c;c.capture=true;const vecText text{0};const std::vector<uint32_t> colors{1};
 f.DrawTextInternal(c,0,0,colors,text,0,0,false,0,0);Check(Snapshot(f).empty(),"nonempty input can emit zero vertices");
 f.ClearFrame();allocations=0;count_allocations=true;f.DrawTextInternal(c,0,0,colors,text,0,0,false,0,0);count_allocations=false;
 Check(allocations==static_cast<size_t>(expectedHitAllocations),"zero-geometry warm hit allocations");Check(Snapshot(f).empty(),"zero-geometry cached hit");Check(f.shapes==1,"zero-geometry hit does not reshape");Check(c.captures==0,"zero-geometry bounds remain absent");
}
int main(int argc,char** argv){
 Check(argc==2,"expected hit-allocation count supplied");int expected=std::atoi(argv[1]);
 for(bool hw:{false,true})for(bool bounds:{false,true}){
  RunCase(hw,0,0,false,bounds,expected);
  RunCase(hw,XBFONT_TRUNCATED,5,false,bounds,expected);
  RunCase(hw,XBFONT_TRUNCATED_LEFT,5,true,bounds,expected);
  RunCase(hw,XBFONT_JUSTIFIED|XBFONT_CENTER_Y,15,false,bounds,expected);
  RunCase(hw,XBFONT_RIGHT,20,false,bounds,expected);
  RunCase(hw,XBFONT_CENTER_X,20,true,bounds,expected);
 }
 for(bool hw:{false,true})RunEmptyGeometry(hw,expected);
 // Empty calls never begin, allocate or touch caches.
 {CGUIFontTTF f;CGraphicContext c;const vecText empty;std::vector<uint32_t> colors{1};allocations=0;count_allocations=true;f.DrawTextInternal(c,0,0,colors,empty,0,0,false,0,0);count_allocations=false;Check(!allocations&&!f.begins&&!f.ends&&!f.shapes,"empty text is inert");}
 // Two supplied main/shadow calls retain separate color-specific ownership.
 {CGUIFontTTF f;CGraphicContext c;vecText t=Text("Shadow");for(uint32_t color:{0xff000000u,0xffffffffu}){std::vector<uint32_t> colors{color};f.DrawTextInternal(c,0,0,colors,t,0,0,false,0,0);auto v=Snapshot(f);Check(v.front().color==color,"supplied shadow/main colors retained");Hash(v);}Check(f.shapes==2,"supplied shadow/main runs separately built");}
 std::cout<<"checks="<<checks<<" digest="<<digest<<'\n';return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=REPO)
    parser.add_argument('--cxx', default=os.environ.get('CXX', 'c++'))
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--baseline-ref', default=BASELINE,
                        help='Git revision with the eager allocation (default: %(default)s)')
    args = parser.parse_args()
    current = method((args.repo / SOURCE).read_text())
    baseline = method(subprocess.check_output(['git', 'show', args.baseline_ref + ':' + SOURCE], cwd=args.repo, text=True))
    # This is a fault injection into an isolated copy, not a production edit.
    allocation = 'tempVertices = std::make_shared<std::vector<SVertex>>();'
    assert 'std::shared_ptr<std::vector<SVertex>> tempVertices;' in current and allocation in current, 'Expected deferred allocation site not found'
    broken_alias = current.replace(allocation, 'vertices = std::make_shared<std::vector<SVertex>>();', 1)
    with tempfile.TemporaryDirectory(prefix='font-cache-allocation-') as directory:
        work = Path(directory)
        binaries = {}
        for label, source in [('baseline', baseline), ('current', current), ('broken-alias', broken_alias)]:
            for backend in ('gles', 'dx'):
                cpp = work / (label + '-' + backend + '.cpp')
                cpp.write_text(HARNESS.replace('@METHOD@', source))
                binary = cpp.with_suffix('')
                command = shlex.split(args.cxx) + ['-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', str(cpp), '-o', str(binary)]
                if backend == 'dx':
                    command += ['-DHAS_DX=1']
                if args.sanitize:
                    command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
                subprocess.run(command, check=True)
                binaries[label, backend] = binary
        env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}
        def run(label, backend, expected, success=True):
            p = subprocess.run([str(binaries[label, backend]), str(expected)], capture_output=True, text=True, env=env)
            if success:
                assert p.returncode == 0, (label, backend, p.stdout, p.stderr)
                print(label, backend, p.stdout.strip())
                return p.stdout.split('digest=')[1].strip()
            assert p.returncode != 0, ('Unsafe mutation accepted', label, backend)
            return p
        for backend in ('gles', 'dx'):
            expected = run('baseline', backend, 1)
            assert run('current', backend, 0) == expected, ('Geometry changed', backend)
            failure = run('baseline', backend, 0, False)
            assert 'warm hit allocations' in failure.stderr, failure.stderr
            print('PASS:', backend, 'full production method, miss/hit/flush/ownership/vertices; original eager allocation rejected')
        alias_failure = run('broken-alias', 'dx', 0, False)
        assert (alias_failure.returncode == -11 or 'null pointer' in alias_failure.stderr or
                'AddressSanitizer' in alias_failure.stderr), alias_failure.stderr
        print('PASS: wrong-alias deferred allocation rejected in software-clipped DX path')
        print('Limits: stub caches/shaping/backend; supplied shadow inputs; no real glyph pixels, GL, DX, context loss or device/performance acceptance.')


if __name__ == '__main__':
    main()
