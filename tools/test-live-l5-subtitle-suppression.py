#!/usr/bin/env python3
"""Exercise exact-picture L5 transport, overlap geometry and temporary suppression.

Uses production picture projection/overlap and, with --kernel-root, the real
amlvideo encoder and Dolby L5 output builder. Host provider/settings/render
boundaries are substituted; this does not establish on-device cue visibility.
"""
import argparse
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PICTURE = runpy.run_path(str(ROOT / 'tools/test-active-picture.py'))
function = PICTURE['function']

PREFIX = r'''
#include <linux/videodev2.h>
#include <cstring>
using u16 = uint16_t;
struct vframe_s {};
struct provider_active_area_req_s {const vframe_s* vf;bool valid;u16 top,bottom,left,right;};
struct vivi_dev {vframe_s* vf;const char* vf_receiver_name;};
constexpr int VFRAME_EVENT_RECEIVER_GET_ACTIVE_AREA=0x2000;
provider_active_area_req_s provider{};
void vf_notify_provider(const char*,int event,provider_active_area_req_s* area){
 assert(event==VFRAME_EVENT_RECEIVER_GET_ACTIVE_AREA&&area->vf==provider.vf);*area=provider;
}
bool xbmc_meta_level_5_osdst=true,dolby_vision_xbmc_osd=false;
bool xbmc_meta_level_5_subt=true,dolby_vision_subtitles=false;
namespace OVERLAY {
struct GlyphOverlay {bool m_discMenuOverlay=false;std::vector<CRect> m_glyphBounds;};
struct CRenderer {
 struct State {double x=0,y=0,width=1,height=1;};
 struct Item {std::shared_ptr<GlyphOverlay> overlay;State state;};
 using PreparedOverlays=std::vector<Item>;
 static bool HasTextSubOutsideActiveArea(const PreparedOverlays&,const CRect&,const CRect&);
};
}
'''

TESTS = r'''
int main(){
 CRenderManager manager;manager.m_picture.iWidth=3840;manager.m_picture.iHeight=2160;
 const CRect src(0,0,3840,2160),dst(0,0,1920,1080),view=dst;
 liveL5Eligible=true;native=true;detected=false;manual=false;autoLb=false;
 const AML_FRAME_L5::Bounds bars=std::array<uint16_t,4>{274,274,0,0};
 const AML_FRAME_L5::Bounds full=std::array<uint16_t,4>{0,0,0,0};
 auto area=[&](const AML_FRAME_L5::Bounds& b){return manager.CalcPictureActiveArea(src,dst,view,1000000,false,false,b);};
 auto cue=std::make_shared<OVERLAY::GlyphOverlay>();cue->m_glyphBounds={{750,1000,1170,1040}};
 OVERLAY::CRenderer::PreparedOverlays items{{cue,{}}};
 auto overlaps=[&](const AML_FRAME_L5::Bounds& b){return OVERLAY::CRenderer::HasTextSubOutsideActiveArea(items,area(b),view);};
 // Missing/zero authored L5 and subtitle detection off: only exact frame fill proves bars.
 assert(!overlaps({}));assert(overlaps(bars));rect(area(bars),0,137,1920,943);
 cache.meta.has_level5_metadata=true;assert(overlaps(bars));
 // Placement receives no live L5 argument, and keeps this cue's black-bar position.
 rect(manager.CalcPictureActiveArea(src,dst,view,1000000,false,true),0,0,1920,1080);
 rect(cue->m_glyphBounds[0],750,1000,1170,1040);
 assert(!overlaps(full));assert(overlaps(bars)); // alternating pictures, no latest-sample latch
 detected=true;dt=db=300;
 assert(!overlaps(full));rect(area({}),0,150,1920,930);rect(area(bars),0,137,1920,943);
 detected=false; // confirmed zero must not fall back to stale file geometry
 for(int i=0;i<100;++i)assert(overlaps(bars)); // held paused picture remains stable
 cue->m_glyphBounds={{750,800,1170,840}};assert(!overlaps(bars));
 cue->m_glyphBounds={{750,930,1170,960}};assert(overlaps(bars)); // partially overlaps
 cue->m_glyphBounds={{750,1100,1170,1140}};assert(!overlaps(bars)); // off display
 cue->m_glyphBounds={{750,1000,1170,1040}};
 cue->m_discMenuOverlay=true;assert(!overlaps(bars));cue->m_discMenuOverlay=false;
 items.clear();assert(!overlaps(bars));items={{cue,{}}};
 // Manual (including explicit zero), phantom and authored source retain priority.
 manual=true;mt=mb=100;rect(area(bars),0,50,1920,1030);
 mt=mb=0;assert(!overlaps(bars));manual=false;
 autoLb=true;additive=false;assert(!overlaps(bars));autoLb=false;additive=true;
 cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=200;
 rect(area(bars),0,100,1920,980);
 cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=0;
 liveL5Eligible=false;assert(!overlaps(bars));liveL5Eligible=true; // retired session/disabled native fill
 native=false;assert(!overlaps(bars));native=true;
 manager.m_picture.stereoMode="left_right";assert(!overlaps(bars));manager.m_picture.stereoMode.clear();
 // Unknown transport versions and impossible bounds never enter picture geometry.
 const std::array<uint8_t,8> bytes{1,18,1,18,0,0,0,0};
 assert(AML_FRAME_L5::FromDequeue(0x50334c01,bytes,3840,2160)==bars);
 assert(!AML_FRAME_L5::FromDequeue(0,bytes,3840,2160));
 assert(!AML_FRAME_L5::FromDequeue(0x50334c02,bytes,3840,2160));
 assert(!AML_FRAME_L5::FromDequeue(0x50334c01,bytes,0,2160));
 assert(!AML_FRAME_L5::FromDequeue(0x50334c01,bytes,3840,548));
 assert(AML_FRAME_L5::FromDequeue(0x50334c01,{},3840,2160)==full);
#ifdef WITH_KERNEL
 vframe_s vf;vivi_dev dev{&vf,"decoder"};v4l2_buffer buffer{};
 provider={&vf,true,274,272,6,4};buffer.timecode.type=3840;buffer.timecode.flags=2160;
 amlvideo_frame_l5(&dev,&buffer);
 auto decode=[&]{return AML_FRAME_L5::FromDequeue(buffer.reserved2,
   {buffer.timecode.frames,buffer.timecode.seconds,buffer.timecode.minutes,buffer.timecode.hours,
    buffer.timecode.userbits[0],buffer.timecode.userbits[1],buffer.timecode.userbits[2],buffer.timecode.userbits[3]},
   buffer.timecode.type,buffer.timecode.flags);};
 assert((decode()==AML_FRAME_L5::Bounds{{274,272,6,4}}));
 assert(buffer.timecode.type==3840&&buffer.timecode.flags==2160);
 provider.valid=false;buffer.reserved2=0;std::memset(&buffer.timecode,0,sizeof(buffer.timecode));
 amlvideo_frame_l5(&dev,&buffer);assert(!decode());
 provider={&vf,true,0,0,0,0};buffer.timecode.type=3840;buffer.timecode.flags=2160;
 amlvideo_frame_l5(&dev,&buffer);assert(decode()==full);
 unsigned char packed[13];
 auto output=[&](bool cuePresent,bool menu){
  dolby_vision_subtitles=cuePresent&&overlaps(bars);dolby_vision_xbmc_osd=menu;
  build_level_5_data_from(packed,274,274,0,0);
  return (static_cast<unsigned>(packed[9])<<8)|packed[10];
 };
 assert(output(false,false)==274);
 for(int i=0;i<100;++i)assert(output(true,false)==0&&overlaps(bars));
 assert(output(false,false)==274); // unmodified effective geometry restores live output
 cue->m_glyphBounds={{750,800,1170,840}};assert(output(true,false)==274);
 assert(output(false,true)==0);assert(output(false,false)==274); // independent OSD gate
#endif
 std::cout<<"PASS: exact-picture transport, live-only and zero L5, overlap/placement separation, authored/manual priority, held frames, cuts and independent output suppression\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = PICTURE['harness']().replace('assert(!allowSubtitleEstimate&&!preferLiveFEL);',
                                       '(void)allowSubtitleEstimate;(void)preferLiveFEL;')
    code += PREFIX
    code += function((ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/OverlayRenderer.cpp').read_text(),
                     'bool CRenderer::HasTextSubOutsideActiveArea(').replace(
                         'bool CRenderer::HasTextSubOutsideActiveArea(',
                         'bool OVERLAY::CRenderer::HasTextSubOutsideActiveArea(')
    if args.kernel_root:
        amlvideo = (args.kernel_root / 'drivers/amlogic/media/video_processor/video_dev/amlvideo.c').read_text()
        dolby = (args.kernel_root / 'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c').read_text()
        code += '\n#define WITH_KERNEL\n' + function(amlvideo, 'static void amlvideo_frame_l5(')
        code += function(dolby, 'static inline void build_level_5_data_from(')
        dequeue = function(amlvideo, 'static int vidioc_dqbuf(')
        assert dequeue.index('p->reserved2 = 0;') < dequeue.index('vf_get(')
        assert dequeue.index('amlvideo_frame_l5(dev, p);') < dequeue.index('vfq_push(&dev->q_omx')
    code += TESTS
    with tempfile.TemporaryDirectory(prefix='live-l5-subs-') as temp:
        out = Path(temp)
        shutil.copytree(Path(os.environ.get('FMT_INCLUDE', '/usr/include')) / 'fmt', out / 'fmt')
        (out / 'PlatformDefs.h').write_text('#pragma once\n')
        variants = [('current', code)]
        if args.negative_controls:
            mutations = [
                ('missing-live-geometry', '!phantom && liveL5 &&', '!phantom && false && liveL5 &&'),
                ('authored-priority-lost', '!phantom && authoredL5)', '!phantom && false && authoredL5)'),
                ('zero-falls-back', '!phantom && liveL5 &&',
                 '!phantom && liveL5 && ((*liveL5)[0] || (*liveL5)[1] || (*liveL5)[2] || (*liveL5)[3]) &&'),
            ]
            if args.kernel_root:
                mutations.append(('suppression-ignored', 'if (!suppress) {', 'if (!suppress || true) {'))
            for name, old, new in mutations:
                assert code.count(old) == 1, (name, code.count(old))
                variants.append((name, code.replace(old, new)))
        for name, candidate in variants:
            (out / 'test.cpp').write_text(candidate)
            subprocess.run(['g++', '-std=c++17', '-DFMT_HEADER_ONLY', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-missing-field-initializers',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        '-pthread', '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                        str(ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.cpp'),
                        '-o', str(out / 'test')], check=True)
            result = subprocess.run([str(out / 'test')], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            if name == 'current':
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout.strip(), flush=True)
            else:
                assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr
                print('REJECTED:', name, flush=True)


if __name__ == '__main__':
    main()
