#!/usr/bin/env python3
"""Exercise production owned-output and render-cache methods against real libass.

Needs a host libass runtime, matching portable libass headers, and a font file.
Use --ass-include for a root containing ass/ (only that subtree is copied, so CE
headers can be used without importing the ARM sysroot into a host build).
The handler bootstrap and ApplyStyle are fixtures; rasterization is real. This
is not full Kodi style/UI, GPU, device, or bitmap/SPU producer validation.
"""
import argparse
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ass-include', type=Path, default=Path('/usr/include'))
    parser.add_argument('--ass-library', default='-lass')
    parser.add_argument('--font', default='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    args = parser.parse_args()
    assert Path(args.font).is_file(), args.font
    source = (ROOT / 'xbmc/cores/VideoPlayer/DVDSubtitles/DVDSubtitlesLibass.cpp').read_text()
    code = PRELUDE
    for signature in ['CLibassRenderResult::CLibassRenderResult(', 'bool RenderOptsEqual(',
                      'std::shared_ptr<const CLibassRenderResult> CDVDSubtitlesLibass::RenderImage(',
                      'bool CDVDSubtitlesLibass::IsDynamicEvent(',
                      'void CDVDSubtitlesLibass::UpdateRenderCache(',
                      'void CDVDSubtitlesLibass::InvalidateRenderCache(',
                      'void CDVDSubtitlesLibass::FlushRenderCache(',
                      'void CDVDSubtitlesLibass::FlushEvents(']:
        code += '\n' + function(source, signature)
    code += '\nCLibassRenderResult::~CLibassRenderResult() = default;\n' + TESTS
    with tempfile.TemporaryDirectory(prefix='libass-owned-test-') as temporary:
        out = Path(temporary)
        shutil.copytree(args.ass_include / 'ass', out / 'ass')
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                        args.ass_library, '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test'), args.font], check=True)
    print('Owned libass output: PASS (real rasterization; production copy/render/cache/flush; ASan/UBSan)')


PRELUDE = r'''
#include <algorithm>
#include <cassert>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include <ass/ass.h>
#include "cores/VideoPlayer/DVDSubtitles/DVDSubtitlesLibassRenderResult.h"
#include "cores/VideoPlayer/DVDSubtitles/SubtitlesStyle.h"
using namespace KODI::SUBTITLES::STYLE;
using CCriticalSection=std::recursive_mutex;
#define DVD_TIME_TO_MSEC(x) ((x)/1000)
constexpr int LOGERROR=1,ASS_NO_ID=-1,NATIVE=0;
struct CLog{template<typename... T>static void Log(T...){}};
struct StringUtils{static void ToLower(std::string& text){
  for(char& c:text)c=static_cast<char>(std::tolower(static_cast<unsigned char>(c)));}};
// Only bootstrap and style application are fixtures; the ownership and timing
// methods below are extracted unchanged from production.
class CDVDSubtitlesLibass {
public:
  ASS_Library* m_library=nullptr;ASS_Renderer* m_renderer=nullptr;ASS_Track* m_track=nullptr;
  CCriticalSection m_section;int m_subtitleType=NATIVE,m_currentDefaultStyleId=0;
  std::shared_ptr<const CLibassRenderResult> m_lastResult;
  renderOpts m_lastOpts{};bool m_renderCacheValid=false;
  int64_t m_cacheValidFrom=0,m_cacheValidUntil=0;
  ~CDVDSubtitlesLibass(){ass_free_track(m_track);ass_renderer_done(m_renderer);ass_library_done(m_library);}
  void Init(const char* font);
  void ApplyStyle(const std::shared_ptr<style>& value,renderOpts){
    for(int i=0;i<m_track->n_styles;++i)m_track->styles[i].FontSize=value->fontSize;
    m_currentDefaultStyleId=0;
  }
  std::shared_ptr<const CLibassRenderResult> RenderImage(double,renderOpts,bool,const std::shared_ptr<style>&);
  bool IsDynamicEvent(const ASS_Event*)const;
  void UpdateRenderCache(int64_t);void InvalidateRenderCache();void FlushRenderCache();void FlushEvents();
};
namespace OVERLAY {
// Test access uses the same private friendship as the synchronous factory.
class COverlay {
public:
  static std::vector<unsigned char> Bytes(const CLibassRenderResult& result){
    std::vector<unsigned char> bytes;
    for(auto* p=result.m_images.get();p;p=p->next){
      assert(p->stride==p->w);
      for(int y=0;y<p->h;++y)for(int x=0;x<p->w;++x)bytes.push_back(p->bitmap[y*p->stride+x]);
    }
    return bytes;
  }
  static const ASS_Image* Images(const CLibassRenderResult& r){return r.m_images.get();}
  static const void* Storage(const CLibassRenderResult& r){return r.m_bitmaps.get();}
  static std::vector<int> Geometry(const CLibassRenderResult& r){
    std::vector<int> v;for(auto* p=r.m_images.get();p;p=p->next){v.push_back(p->dst_x);v.push_back(p->dst_y);}
    return v;
  }
};
}
using Inspect=OVERLAY::COverlay;
'''

TESTS = r'''
void CDVDSubtitlesLibass::Init(const char* font){
  m_library=ass_library_init();assert(m_library);
  m_renderer=ass_renderer_init(m_library);assert(m_renderer);
  ass_set_fonts(m_renderer,font,"DejaVu Sans",ASS_FONTPROVIDER_NONE,nullptr,1);
  std::string script=R"ASS([Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,28,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,Owned static
Dialogue: 0,0:00:02.00,0:00:06.00,Default,,0,0,0,,{\move(100,120,500,120)}Owned motion
Dialogue: 0,0:00:06.00,0:00:10.00,Default,,0,0,0,,{\t(0,4000,\fs56)}Owned animation
)ASS";
  m_track=ass_read_memory(m_library,script.data(),script.size(),nullptr);assert(m_track&&m_track->n_events==3);
}
int main(int argc,char** argv){
  assert(argc==2);std::printf("libass runtime: 0x%x; headers: 0x%x\n",ass_library_version(),LIBASS_VERSION);
  // Exact minimum source allocation: final row has no stride padding. ASan
  // catches accidental stride*h copying. Header/palette color/list are owned.
  {std::vector<unsigned char> bitmap={1,2,99,99,3,4};ASS_Image nodes[3]{};
   nodes[0].w=2;nodes[0].h=2;nodes[0].stride=4;nodes[0].bitmap=bitmap.data();nodes[0].color=0x11223344;
   nodes[0].type=ASS_Image::IMAGE_TYPE_OUTLINE;nodes[0].dst_x=7;nodes[0].dst_y=9;nodes[0].next=&nodes[1];
   nodes[1].w=0;nodes[1].h=2;nodes[1].next=&nodes[2];nodes[2].w=2;nodes[2].h=0;
   CLibassRenderResult owned(nodes);auto* copy=Inspect::Images(owned);
   assert(copy!=nodes&&copy->bitmap!=bitmap.data()&&copy->next!=&nodes[1]);
   assert(copy->color==0x11223344&&copy->type==ASS_Image::IMAGE_TYPE_OUTLINE&&copy->dst_x==7&&copy->dst_y==9);
   assert(copy->next->w==0&&copy->next->next->h==0&&!copy->next->next->next);
   nodes[0].dst_x=10;CLibassRenderResult moved(nodes,&owned);
   assert(Inspect::Storage(moved)==Inspect::Storage(owned)&&Inspect::Images(moved)->dst_x==10&&copy->dst_x==7);
   bitmap.assign(6,88);nodes[0].color=0;nodes[0].next=nullptr;
   assert(Inspect::Bytes(owned)==std::vector<unsigned char>({1,2,3,4}));
   assert(Inspect::Bytes(moved)==Inspect::Bytes(owned)&&copy->color==0x11223344);
   CLibassRenderResult empty(nullptr);assert(!Inspect::Images(empty));}
  renderOpts opts{};opts.frameWidth=opts.videoWidth=opts.sourceWidth=640;
  opts.frameHeight=opts.videoHeight=opts.sourceHeight=360;opts.m_par=1;
  auto subStyle=std::make_shared<style>();subStyle->fontSize=28;
  std::shared_ptr<const CLibassRenderResult> retained;
  std::vector<unsigned char> retainedBytes;
  {CDVDSubtitlesLibass h;h.Init(argv[1]);
   retained=h.RenderImage(500000,opts,false,subStyle);assert(retained);
   retainedBytes=Inspect::Bytes(*retained);assert(!retainedBytes.empty());
   assert(h.m_renderCacheValid);
   for(int i=0;i<20;++i)assert(h.RenderImage(500000+i*1000,opts,false,subStyle)==retained);
   h.FlushRenderCache();auto rebuilt=h.RenderImage(500000,opts,false,subStyle);
   assert(rebuilt!=retained&&Inspect::Bytes(*rebuilt)==retainedBytes);
   // An actual moving subtitle changes position without changing pixels.
   auto motion=h.RenderImage(2500000,opts,false,subStyle);assert(motion&&!h.m_renderCacheValid);
   auto moved=h.RenderImage(3500000,opts,false,subStyle);assert(moved&&moved!=motion);
   assert(Inspect::Geometry(*moved)!=Inspect::Geometry(*motion));
   assert(Inspect::Storage(*moved)==Inspect::Storage(*motion));
   assert(h.RenderImage(3500000,opts,false,subStyle)==moved);
   auto animated=h.RenderImage(6500000,opts,false,subStyle);
   auto animatedLater=h.RenderImage(8500000,opts,false,subStyle);assert(animated&&animatedLater);
   assert(animated!=animatedLater&&Inspect::Bytes(*animated)!=Inspect::Bytes(*animatedLater));
   assert(Inspect::Storage(*animated)!=Inspect::Storage(*animatedLater));
   // Style fixture changes real libass track font sizes at the production
   // updateStyle evaluation point; this is not Kodi's complete ApplyStyle.
   auto beforeStyle=h.RenderImage(500000,opts,false,subStyle);
   subStyle->fontSize=44;auto styled=h.RenderImage(500000,opts,true,subStyle);
   assert(styled!=beforeStyle&&Inspect::Bytes(*styled)!=Inspect::Bytes(*beforeStyle));
   // Fractional canvas change rounds to the same libass dimensions, but the
   // GPU conversion denominator changes: new identity, shared owned pixels.
   auto fractional=opts;fractional.frameWidth=640.25f;
   auto resized=h.RenderImage(500000,fractional,false,subStyle);
   assert(resized!=styled&&Inspect::Storage(*resized)==Inspect::Storage(*styled));
   // Replacement event text and clear/hide must not alter selected output.
   {std::unique_lock<CCriticalSection> lock(h.m_section);
    free(h.m_track->events[0].Text);h.m_track->events[0].Text=strdup("Replacement text");h.InvalidateRenderCache();}
   auto replacement=h.RenderImage(500000,opts,false,subStyle);assert(replacement);
   assert(Inspect::Bytes(*replacement)!=Inspect::Bytes(*retained));
   h.FlushEvents();assert(!h.RenderImage(500000,opts,false,subStyle));
   assert(Inspect::Bytes(*retained)==retainedBytes);}
  // No renderer, track, event or handler remains alive here.
  assert(Inspect::Bytes(*retained)==retainedBytes);
}
'''

if __name__ == '__main__':
    main()
