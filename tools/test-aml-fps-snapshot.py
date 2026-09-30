#!/usr/bin/env python3
"""Production FPS-label sampling with modeled sysfs/time; no device CPU claim."""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
function=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
PRELUDE=r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
std::atomic<long long> milliseconds{10000};
auto FakeNow(){return std::chrono::steady_clock::time_point(std::chrono::milliseconds(milliseconds.load()));}
std::atomic<int> reads{0},exists{0};bool present=true;
std::optional<std::string> input="in:19 out:19";
struct CSysfsPath {
 explicit CSysfsPath(const char*){}
 bool Exists(){++exists;return present;}
 template<class T> std::optional<T> Get(){++reads;return input;}
};
'''
TESTS=r'''
void reset(){aml_video_fps_reset();reads=0;exists=0;present=true;input="in:19 out:19";milliseconds=10000;}
void pair_and_deadline(){
 reset();assert(aml_video_fps_info()=="025 - 025 - 000 |" && aml_video_fps_drop().empty());
 for(int i=0;i<1000;++i){aml_video_fps_info();aml_video_fps_drop();}
 assert(reads==1 && exists==1 && video_fps_snapshot().history.size()==1);
 milliseconds=10099;assert(aml_video_fps_drop().empty() && reads==1);
 milliseconds=10100;aml_video_fps_info();assert(reads==2 && video_fps_snapshot().history.size()==2);
}
void history_and_drop(){
 reset();aml_video_fps_info();milliseconds=10100;input="in:19 out:f";
 assert(aml_video_fps_info()=="025 - 020 - 005 /" && aml_video_fps_drop()=="20");
 // A duplicate provider poll must not change sample weighting.
 milliseconds=10150;input="in:19 out:0";assert(aml_video_fps_drop()=="20" && reads==2);
 milliseconds=11200;input="in:19 out:19";
 assert(aml_video_fps_info().find("025 - 025 - 000")==0 && aml_video_fps_drop()=="20");
 assert(video_fps_snapshot().history.size()==1);
 milliseconds=13099;assert(aml_video_fps_drop()=="20");
 milliseconds=13200;assert(aml_video_fps_drop().empty());
}
void failures_and_replacement(){
 reset();input="in:19 out:f";assert(aml_video_fps_drop()=="15");
 milliseconds=10100;input.reset();assert(aml_video_fps_info().find("000 - 000 - 000")==0);
 assert(aml_video_fps_drop().empty() && video_fps_snapshot().history.empty());
 assert(reads==2);aml_video_fps_info();assert(reads==2);
 milliseconds=10200;input="malformed";aml_video_fps_info();assert(video_fps_snapshot().history.empty());
 milliseconds=10300;present=false;aml_video_fps_info();assert(reads==3);
 present=true;input="in:32 out:32";aml_video_fps_reset();
 assert(aml_video_fps_info()=="050 - 050 - 000 |" && reads==4);
 assert(video_fps_snapshot().history.size()==1 && aml_video_fps_drop().empty());
}
void concurrent_reset(){
 reset();std::vector<std::thread> threads;
 for(int j=0;j<4;++j)threads.emplace_back([]{for(int i=0;i<500;++i){aml_video_fps_info();aml_video_fps_drop();}});
 threads.emplace_back([]{for(int i=0;i<50;++i)aml_video_fps_reset();});
 for(auto& t:threads)t.join();
 assert(aml_video_fps_info().find("025 - 025 - 000")==0);
 assert(video_fps_snapshot().history.size()==1);
}
int main(){pair_and_deadline();history_and_drop();failures_and_replacement();concurrent_reset();
 std::cout<<"PASS shared sample/deadline, weighted history, drop hold, failures, replacement and concurrent reset\n";}
'''
def harness():
    aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    block=aml[aml.index('struct FpsData {'):aml.index('unsigned int aml_dv_video_processor_mode()')]
    codec=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    for signature in ('bool CAMLCodec::OpenDecoderInternal()', 'void CAMLCodec::CloseDecoderInternal()'):
        assert 'aml_video_fps_reset();' in function(codec,signature).splitlines()[2]
    assert 'aml_video_fps_reset' not in function(codec,'void CAMLCodec::ResetInternal()')
    return PRELUDE+block.replace('std::chrono::steady_clock::now()', 'FakeNow()')+TESTS

def run(source,negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-fps-') as name:
        out=Path(name);(out/'test.cpp').write_text(source)
        cmd=[os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fno-pie','-no-pie',str(out/'test.cpp'),'-o',str(out/'test')]
        if not negative:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
        subprocess.run(cmd,check=True)
        result=subprocess.run([str(out/'test')],capture_output=negative,text=True,check=not negative,timeout=20)
        if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result.stderr

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    source=harness()
    if args.negative_controls:
        controls=[('resample paired labels','snapshot.sampled && now - snapshot.lastUpdate < UPDATE_INTERVAL','false'),
                  ('retain replacement cache','snapshot.sampled = false;','/* omitted */'),
                  ('retain failed history','  snapshot.history.clear();\n  snapshot.lowestOutput = 0;','  snapshot.lowestOutput = 0;'),
                  ('extend history','HISTORY_DURATION(1)','HISTORY_DURATION(2)'),
                  ('shorten drop hold','HOLD_PERIOD(3)','HOLD_PERIOD(1)')]
        for name,before,after in controls:
            assert before in source;run(source.replace(before,after,1),True);print('Rejected runtime negative control:',name)
    else:run(source);print('FPS snapshot: PASS (production functions; ASan/UBSan; modeled clock/sysfs)')
if __name__=='__main__':main()
