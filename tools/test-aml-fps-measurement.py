#!/usr/bin/env python3
"""Actual kernel FPS producers + Kodi consumer under controlled arrivals/read schedules.

Compiles production fps_info_show/frame_rate_show and the complete Kodi FPS block.
Atomic/IRQ/frame ownership, sysfs, GUI timing and frame arrivals are host adapters;
this is not complete IRQ, hardware/display or installed-PPI acceptance. --trace
compares schedules without assertions; --kodi-revision/--kernel-revision reproduce
old code. Default positives and named controls test the passive observation.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
KERNEL = Path('/home/panni/linux-amlogic-local')
function = runpy.run_path(str(ROOT/'tools/test-aml-fps-snapshot.py'))['function']
PRELUDE = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <vector>
using u32=uint32_t;using u64=unsigned long long;
constexpr unsigned HZ=250,DEBUG_FLAG_CALC_PTS_INC=1,PAGE_SIZE=4096;
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,y) ((x)=(y))
#define scnprintf snprintf
// Barriers are modeled here; actual CE/source review validates their placement.
#define smp_rmb() ((void)0)
struct class_stub{};struct class_attribute{};
struct {unsigned duration=3840;} frame;auto* cur_dispbuf=&frame;
u32 frame_count=250,last_frame_count=0,last_frame_time=0,jiffies=2500,vsync_count=0,vsync_pts_inc=1500,debugflags=0,fps_sample_duration=3840;
u64 presentation_epoch=1;int video_unreg_flag=0,output_fps=0;
struct Vinfo{unsigned sync_duration_den=1,sync_duration_num=25;} vi;auto* vinfo=&vi;
int epochReads=0,flagReads=0,changeEpochAt=0,retireAt=0;bool changeEpochOnFlag=false;
u64 atomic64_read(u64* p){if(++epochReads==changeEpochAt)++*p;return *p;}
int atomic_read(int* p){++flagReads;if(changeEpochOnFlag){++presentation_epoch;changeEpochOnFlag=false;}if(flagReads==retireAt)*p=1;return *p;}
@PRODUCER@
void publish_duration(){auto* vf=&frame;@DURATION@ (void)vf;}
long long milliseconds=10000;unsigned reads=0,exists=0;bool extra=false;
std::optional<std::string> override_text;bool fail=false;
auto FakeNow(){return std::chrono::steady_clock::time_point(std::chrono::milliseconds(milliseconds));}
struct CSysfsPath {
 explicit CSysfsPath(const char*){}bool Exists(){++exists;return true;}
 template<class T>std::optional<T> Get(){++reads;if(fail)return {};if(override_text)return override_text;
 char b[PAGE_SIZE];if(extra)frame_rate_show(nullptr,nullptr,b);fps_info_show(nullptr,nullptr,b);return std::string(b);}
};
@CONSUMER@
void reset(unsigned duration=3840){
#if SHARED
 aml_video_fps_reset();
#endif
 frame.duration=duration;publish_duration();milliseconds=10000;jiffies=2500;frame_count=250;last_frame_count=0;last_frame_time=0;
 presentation_epoch=1;video_unreg_flag=0;epochReads=flagReads=changeEpochAt=retireAt=0;changeEpochOnFlag=false;output_fps=0;reads=exists=0;extra=fail=false;override_text.reset();}
std::string observe(unsigned ms,u32 frames,bool paired=true){milliseconds=10000+ms;jiffies=2500+ms*HZ/1000;frame_count=frames;
 auto info=aml_video_fps_info();if(paired)aml_video_fps_drop();return info;}
unsigned number(const std::string& s,unsigned offset){return std::stoul(s.substr(offset,3));}
unsigned deficit(const std::string& s){return number(s,12);}
void freeze(){reset();observe(0,250);extra=true;unsigned low=1000,high=0;
 for(unsigned ms=40;ms<=6000;ms+=40){auto s=observe(ms,250+ms/50);if(ms>3000){low=std::min(low,deficit(s));high=std::max(high,deficit(s));}}
 // The1.08s window counts21/22arrivals: deficits5.556/4.630 round6/5.
 assert(reads==51&&exists==51&&"paired labels retain shared CPU read bound");
 assert(low>=5&&high<=6&&"extra reader must not freeze genuine deficit");}
void trace(int step,double speed,bool competitor){reset(4004);observe(0,250);extra=competitor;unsigned lo=1000,hi=0,nz=0;
 for(int ms=step;ms<=6000;ms+=step){auto s=observe(ms,250+static_cast<u32>(ms*speed/1000.0));if(ms>2000){auto d=deficit(s);lo=std::min(lo,d);hi=std::max(hi,d);nz+=d!=0;}}
 std::cout<<"step="<<step<<" speed="<<speed<<" extra="<<extra<<" deficit="<<lo<<".."<<hi<<" nonzero="<<nz<<" reads="<<reads<<" label="<<aml_video_fps_info()<<'\n';}
'''
TESTS = r'''
void rounding(){
 for(auto [duration,rounded]:std::vector<std::pair<unsigned,unsigned>>{{4004,24},{4000,24},{3840,25},{1920,50},{3203,30},{1602,60}}){
  reset(duration);observe(0,250);const unsigned frames=static_cast<unsigned>(std::ceil(96000.0/duration));
  auto s=observe(1000,250+frames);assert(number(s,0)==rounded&&number(s,6)==rounded&&deficit(s)==0&&"round genuine rates only at display");
 }
 // Input24.4026, output23.6: both display24, but the unrounded deficit is0.8026.
 reset(3934);observe(0,250);auto s=observe(2500,309);
 assert(s.substr(0,15)=="024 - 024 - 001"&&"deficit must precede display rounding");
}
void schedules(){
 for(auto [duration,speed]:std::vector<std::pair<unsigned,double>>{{4004,23.976},{4000,24},{3840,25},{1920,50},{3203,29.97},{1602,59.94}})
 for(unsigned step:{16U,42U,100U,250U,1000U,1500U})for(double phase:{0.0,0.37,0.91}){
  reset(duration);observe(0,250);extra=true;
  for(unsigned ms=step;ms<=8000;ms+=step){auto s=observe(ms,250+static_cast<u32>(ms*speed/1000.0+phase));
   if(ms>3000){assert(number(s,0)==static_cast<unsigned>(96000.0/duration+0.5));
    // Finite arrival/tick boundaries produce up to aboutone frame of uncertainty.
    assert(deficit(s)<=2&&"healthy counter uncertainty is bounded");
    assert(std::abs(static_cast<double>(number(s,6))-speed)<=2);}
   assert(video_fps_snapshot().counters.size()<=12&&"counter window is bounded");
  }
  assert(reads<=8000/100+2&&exists==reads&&"GUI rate cannot restore per-label sysfs reads");
 }
}
void losses(){
 reset();observe(0,250);extra=true;unsigned largest=0;
 for(unsigned ms=40;ms<=6000;ms+=40){unsigned removed=ms>=2200?std::min(3U,(ms-2160)/40):0;
  auto s=observe(ms,250+ms/40-removed);largest=std::max(largest,deficit(s));}
 assert(largest>=2&&deficit(aml_video_fps_info())==0&&"short real loss must be visible and then age out");
 reset();observe(0,250);bool sawZero=false;
 for(unsigned ms=40;ms<=10000;ms+=40){unsigned advancing=std::min(ms,2000U)+(ms>3600?ms-3600:0);
  auto s=observe(ms,250+advancing/40);if(ms>3100&&ms<3500){sawZero|=number(s,6)==0;}
 }
 assert(sawZero&&deficit(aml_video_fps_info())==0&&"pause/stall measurement must recover on resume");
 assert(aml_video_fps_drop().empty()&&"zero-output held label must expire");
}
void lifecycle(){
 reset();observe(0,250);observe(1000,270);assert(deficit(aml_video_fps_info())==5);
 ++presentation_epoch;auto s=observe(1120,272);
 assert(s.substr(0,15)=="025 - 000 - 000"&&video_fps_snapshot().counters.size()==1&&"new provider needs a measured baseline");
 fps_sample_duration=0;s=observe(1240,274);
 assert(s.substr(0,15)=="000 - 000 - 000"&&video_fps_snapshot().counters.empty()&&aml_video_fps_drop().empty());
 frame.duration=4004;publish_duration();s=observe(1360,275);
 assert(s.substr(0,15)=="024 - 000 - 000"&&aml_video_fps_drop().empty()&&"new provider needs a measured baseline");
 s=observe(2440,301);assert(number(s,0)==24&&deficit(s)==0);
 // Duration change and Kodi decoder invalidation cannot mix prior observations.
 frame.duration=1920;publish_duration();s=observe(2560,307);assert(number(s,0)==50&&number(s,6)==0);
 aml_video_fps_reset();s=observe(2680,313);assert(video_fps_snapshot().counters.size()==1&&number(s,6)==0);
 fail=true;s=observe(2800,319);assert(video_fps_snapshot().counters.empty());fail=false;
 // Retirement while the scalar is still valid is explicitly untrusted.
 video_unreg_flag=1;s=observe(2920,325);assert(video_fps_snapshot().counters.empty());video_unreg_flag=0;
}
void retirement_races(){
 reset();observe(0,250);observe(1000,270);flagReads=0;changeEpochOnFlag=true;
 auto s=observe(1120,273);assert(s.substr(0,15)=="000 - 000 - 000"&&"epoch race invalidates passive observation");
 reset();observe(0,250);observe(1000,270);flagReads=0;retireAt=2;
 s=observe(1120,273);assert(s.substr(0,15)=="000 - 000 - 000"&&"retirement race invalidates passive observation");
}
void rollover_and_gaps(){
 reset();jiffies=UINT32_MAX-249;frame_count=UINT32_MAX-24;milliseconds=10000;aml_video_fps_info();
 jiffies=0;frame_count=0;milliseconds=11000;auto s=aml_video_fps_info();
 assert(s.substr(0,15)=="025 - 025 - 000"&&"u32 rollover is a valid bounded delta");
 frame_count=0;jiffies=250;milliseconds=12000;aml_video_fps_info(); // actual stall
 frame_count=UINT32_MAX;jiffies=275;milliseconds=12100;s=aml_video_fps_info();
 assert(number(s,6)==0&&video_fps_snapshot().counters.size()==1&&"backwards frame reset needs baseline");
 jiffies=10000;milliseconds=51000;s=aml_video_fps_info();assert(number(s,6)==0&&"inactive long gap needs baseline");
 // No elapsed tick with a changed count cannot prove an instantaneous rate.
 frame_count=1;milliseconds=51100;s=aml_video_fps_info();assert(video_fps_snapshot().counters.size()==1);
}
void malformed(){
 for(const char* suffix:{"sample", "sample:0x2 frames:0x1 ticks:0x1 hz:0xfa duration:0xf00 epoch:0x1",
  "sample:0x1 frames:0x1 ticks:0x1 hz:0x0 duration:0xf00 epoch:0x1",
  "sample:0x1 frames:0x1 ticks:0x1 hz:0xfa duration:0x0 epoch:0x1",
  "sample:0x1 frames:0x1 ticks:0x1 hz:0xfa duration:0xf00 epoch:0x0",
  "sample:0x1 frames:0x100000000 ticks:0x1 hz:0xfa duration:0xf00 epoch:0x1",
  "sample:0x1 frames:0x-1 ticks:0x1 hz:0xfa duration:0xf00 epoch:0x1",
  "sample:0x1 frames:0x1 ticks:0x1 hz:0xfa duration:0xf00 epoch:0x1 extra"}){
  reset();observe(0,250);observe(1000,270);
  override_text=std::string("input_fps:0x19 output_fps:0x19 drop_fps:0x0 ")+suffix;
  auto s=observe(1120,273);assert(s.substr(0,15)=="000 - 000 - 000"&&video_fps_snapshot().counters.empty()&&aml_video_fps_drop().empty()&&"malformed extension cannot select legacy healthy rate");
 }
}
int main(int argc,char** argv){if(argc>1&&std::string(argv[1])=="trace"){
 const std::vector<std::tuple<int,double,bool>> cases{{42,23.976,false},{16,24,false},{40,20,true},{1000,20,true}};auto [step,speed,other]=cases.at(std::atoi(argv[2]));trace(step,speed,other);return 0;}
 freeze();rounding();schedules();losses();malformed();lifecycle();retirement_races();rollover_and_gaps();
 std::cout<<"PASS actual producer/consumer: destructive-reader freeze, fractional display/pre-round deficit, GUI cadence/phase, losses/stalls, provider/duration/reset, rollover/gap, malformed extension\n";}
'''
BASELINE = r'''
int main(int argc,char** argv){if(argc>1&&std::string(argv[1])=="trace"){
 const std::vector<std::tuple<int,double,bool>> cases{{42,23.976,false},{16,24,false},{40,20,true},{1000,20,true}};auto [step,speed,other]=cases.at(std::atoi(argv[2]));trace(step,speed,other);return 0;}freeze();}
'''

def source_at(root, rel, rev):
    return subprocess.check_output(['git','show',f'{rev}:{rel}'],cwd=root,text=True) if rev else (root/rel).read_text()

def harness(args):
    video=source_at(args.kernel_root,'drivers/amlogic/media/video_sink/video.c',args.kernel_revision)
    aml=source_at(ROOT,'xbmc/utils/AMLUtils.cpp',args.kodi_revision)
    producer='\n'.join(function(video,sig).replace('struct class *','struct class_stub *') for sig in ('static ssize_t fps_info_show(', 'static ssize_t frame_rate_show('))
    block=aml[aml.index('struct FpsData'):aml.index('unsigned int aml_dv_video_processor_mode()')]
    shared='struct FpsSnapshot' in block
    duration='WRITE_ONCE(fps_sample_duration, vf->duration);' if 'WRITE_ONCE(fps_sample_duration, vf->duration);' in function(video,'static struct vframe_s *vsync_toggle_frame(') else 'fps_sample_duration=frame.duration;'
    if not args.kernel_revision:
        assert duration == 'WRITE_ONCE(fps_sample_duration, vf->duration);', 'current production duration publication missing'
        for sig in ('static void video_vf_unreg_provider(', 'static void video_vf_light_unreg_provider('):
            body=function(video,sig)
            assert body.index('while (atomic_read(&video_inirq_flag)') < body.index('WRITE_ONCE(fps_sample_duration, 0);') < body.index('atomic_dec(&video_unreg_flag)')
    source=('#include <tuple>\n#define SHARED '+str(int(shared))+'\n'+PRELUDE.replace('@PRODUCER@',producer).replace('@DURATION@',duration)
            .replace('@CONSUMER@',block.replace('std::chrono::steady_clock::now()', 'FakeNow()')))
    return source+(BASELINE if args.kodi_revision or args.kernel_revision else TESTS)

def run(source, mode=None, expected=None):
    with tempfile.TemporaryDirectory(prefix='aml-fps-measurement-') as temp:
        p=Path(temp);(p/'test.cpp').write_text(source)
        cmd=[os.environ.get('CXX','g++'),'-std=c++17','-fno-strict-overflow','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-sign-compare','-pthread','-fno-pie','-no-pie',str(p/'test.cpp'),'-o',str(p/'test')]
        if expected is None:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
        subprocess.run(cmd,check=True)
        # Each historical trace gets a fresh process: old static histories have
        # no reset API and cannot be reused after the modeled clock rewinds.
        for mode_args in ([['trace',str(i)] for i in range(4)] if mode=='trace' else [[]]):
            r=subprocess.run([str(p/'test')]+mode_args,capture_output=True,text=True,timeout=25,env={**os.environ,'UBSAN_OPTIONS':'halt_on_error=1'})
            if expected:assert r.returncode and 'Assertion' in r.stderr and expected in r.stderr,r.stderr
            else:print(r.stdout,end='');assert r.returncode==0,r.stderr

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kernel-root',type=Path,default=KERNEL);p.add_argument('--kernel-revision');p.add_argument('--kodi-revision');p.add_argument('--trace',action='store_true');p.add_argument('--negative-controls',action='store_true');args=p.parse_args()
    source=harness(args);run(source,'trace' if args.trace else None)
    if args.negative_controls:
        for name,old,new,expected in [
            ('ignore passive fields','if (input.find(" sample") != std::string::npos)','if (false)','extra reader must not freeze genuine deficit'),
            ('round deficit from rounded fields','info.avg_drop_fps + 0.5','static_cast<double>(inputFps - outputFps)','deficit must precede display rounding'),
            ('truncate whole FPS','info.avg_input_fps + 0.5','info.avg_input_fps','round genuine rates only at display'),
            ('lose fractional nominal rate','96000.0 / current.duration','static_cast<double>(96000 / current.duration)','round genuine rates only at display'),
            ('clamp low output to nominal','std::min(input, static_cast<double>(frames) * current.hz / elapsed)','std::max(input, static_cast<double>(frames) * current.hz / elapsed)','extra reader must not freeze genuine deficit'),
            ('paired labels resample','snapshot.sampled && now - snapshot.lastUpdate < UPDATE_INTERVAL','false','paired labels retain shared CPU read bound'),
            ('omit epoch invalidation','current.epoch != previous.epoch','false','new provider needs a measured baseline'),
            ('keep zero held sentinel','snapshot.hasLowestOutput && now - snapshot.lastDrop >= HOLD_PERIOD','snapshot.lowestOutput != 0 && now - snapshot.lastDrop >= HOLD_PERIOD','zero-output held label must expire'),
            ('omit duration publication','WRITE_ONCE(fps_sample_duration, vf->duration);','(void)vf;','round genuine rates only at display'),
            ('use destructive delta as passive count','frames, tmp, HZ, duration, epoch','frames - last_frame_count, tmp, HZ, duration, epoch','extra reader must not freeze genuine deficit'),
            ('use shared elapsed as passive timestamp','frames, tmp, HZ, duration, epoch','frames, time, HZ, duration, epoch','extra reader must not freeze genuine deficit'),
            ('omit epoch revalidation','epoch != atomic64_read(&presentation_epoch)','false','epoch race invalidates passive observation'),
            ('trust unknown extension version','version != 1','false','malformed extension cannot select legacy healthy rate'),
        ]:
            assert old in source,name;run(source.replace(old,new,1),expected=expected);print('Rejected:',name)
if __name__=='__main__':main()
