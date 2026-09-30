#!/usr/bin/env python3
"""Production AML rate-query, timing and permit boundaries with recording driver stubs.
No real decoder/DV/HDMI or target playback acceptance.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]

def function(source,signature):
    start=source.index(signature);end=source.index('{',start)+1;depth=1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[start:end]

HARNESS=r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <future>
#include <functional>
#include <iostream>
#include <limits>
#include <mutex>
#include <string>
#include <type_traits>
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
using namespace std::chrono_literals;
constexpr int UNIT_FREQ=96000,PTS_FREQ=90000,DVD_TIME_BASE=1000000,DVD_PLAYSPEED_NORMAL=1000,LOGDEBUG=0;
@BOUNDS@
@HINTS@
std::mutex pollSyncMutex;
struct CRect {};
struct vdec_info {unsigned int frame_dur=0;};
struct CodecHandle {int handle=7;};
struct Private {@RATE_MEMBER@ CodecHandle vcodec;};
struct CLog {template<class... T>static void Log(T&&...) {}};
struct Driver {
 uint32_t duration=4004;int result=0,calls=0;std::function<void()> onQuery;
 int codec_get_vdec_info(CodecHandle*,vdec_info* vi){++calls;if(onQuery)onQuery();vi->frame_dur=duration;return result;}
};
struct CAMLCodec {
 CAMLSession m_session;std::mutex m_presentationMutex;
 bool m_opened=true,m_presentationActive=true;uint64_t m_presentationGeneration=1;
 @SPEED_MEMBER@
 int m_pollDevice=7;Private storage;Private* am_private=&storage;Driver driver;Driver* m_dll=&driver;
 CAMLCodec(){m_speed=DVD_PLAYSPEED_NORMAL;storage.video_rate=4004;auto r=m_session.Fence();assert(m_session.BeginMutation(r));assert(m_session.Complete(r,true));}
 unsigned int GetDecoderVideoRate();void RefreshDecoderRate(const CAMLSession::Permit&);
 void SetVideoRect(const CRect&,const CRect&,uint64_t,const CAMLSession::Permit&);
 void SetVideoRate(int);int GetAmlDuration()const;
 void Update(bool rect=false){auto permit=m_session.Acquire(m_session.Epoch());assert(permit);
  if(rect)SetVideoRect({},{},m_presentationGeneration,permit);else RefreshDecoderRate(permit);}
};
@QUERY@
@REFRESH@
@RECT@
@SET@
@DURATION@
static double PictureDuration(Private* am_private){@PICTURE@ return rate_duration;}
static int64_t DrainDuration(Private* am_private){@DRAIN@ return poll_ms;}
int main(){
 static_assert(std::is_same_v<decltype(Private::video_rate),std::atomic<unsigned int>>);
 static_assert(std::is_same_v<decltype(CAMLCodec::m_speed),std::atomic<int>>);
 // Reproduce the actual old unsigned wrap, without treating it as device evidence.
 const uint32_t wrapped=static_cast<uint32_t>(-64);
 const uint32_t oldDrain=(wrapped*10000u+UNIT_FREQ-1)/UNIT_FREQ;
 assert(oldDrain==44733&&((4004u*10000u+UNIT_FREQ-1)/UNIT_FREQ)==418);
 std::cout<<"REPRODUCED u32(-64): old drain="<<oldDrain<<"ms vs 4004 ticks=418ms\n";
 assert(VideoRateFromHints(24000,1001)==4004&&VideoRateFromHints(30000,1001)==3203);
 assert(VideoRateFromHints(50,1,2)==3840&&VideoRateFromHints(60000,1001,2)==3203);
 assert(VideoRateFromHints(60,1,2)==3200&&VideoRateFromHints(50,1)==1920);
 assert(VideoRateFromHints(120,1)==800&&VideoRateFromHints(5,1)==19200);
 assert(VideoRateFromHints(1,1)==96000&&VideoRateFromHints(1,10)==960000);
 assert(VideoRateFromHints(240,1)==400); // Unusual hint remains, not clamped by probe bounds.
 for(auto pair:{std::pair{0,1},std::pair{30,0},std::pair{30,-1},std::pair{-1,1},
                std::pair{1,std::numeric_limits<int>::max()},std::pair{std::numeric_limits<int>::max(),1}})
  assert(VideoRateFromHints(pair.first,pair.second)==3203);
 for(bool rect:{false,true}){
  CAMLCodec codec;
  for(uint32_t rate:{800u,801u,1600u,1602u,1920u,3200u,3203u,3840u,4000u,4004u,4800u,19200u}){
   codec.driver.duration=rate;codec.Update(rect);assert(codec.storage.video_rate==rate);
  }
  codec.SetVideoRate(4004);
  for(uint32_t bad:{0u,1u,799u,19201u,wrapped,std::numeric_limits<uint32_t>::max()}){
   codec.driver.duration=bad;codec.Update(rect);assert(codec.storage.video_rate==4004);
  }
  assert(DrainDuration(codec.am_private)==418&&codec.GetAmlDuration()==3753);
  codec.driver.result=-1;codec.driver.duration=3200;codec.Update(rect);assert(codec.storage.video_rate==4004);
  codec.driver.result=0;int before=codec.driver.calls;
  codec.m_opened=false;codec.Update(rect);assert(codec.driver.calls==before);
  codec.m_opened=true;codec.m_pollDevice=-1;codec.Update(rect);assert(codec.driver.calls==before);
  codec.m_pollDevice=7;codec.m_speed=0;codec.Update(rect);assert(codec.driver.calls==before);
  codec.m_speed=DVD_PLAYSPEED_NORMAL;codec.m_presentationActive=false;codec.Update(rect);assert(codec.driver.calls==before);
  codec.m_presentationActive=true;
  CAMLSession::Permit empty;codec.RefreshDecoderRate(empty);codec.SetVideoRect({},{},1,empty);assert(codec.driver.calls==before);
  {auto permit=codec.m_session.Acquire(codec.m_session.Epoch());codec.SetVideoRect({},{},0,permit);assert(codec.driver.calls==before);}
  // A decoder rate probe must not wait on the global PollFrame mutex.
  {std::lock_guard<std::mutex> poll(pollSyncMutex);codec.Update(rect);}
  assert(codec.storage.video_rate==3200);
  codec.SetVideoRate(-64);codec.SetVideoRate(0);assert(codec.storage.video_rate==3200);
  // Retain legitimate slow stream timing; widen all products before multiplication.
  codec.SetVideoRate(960000);codec.driver.duration=0;codec.Update(rect);
  assert(codec.storage.video_rate==960000&&DrainDuration(codec.am_private)==100000);
  assert(codec.GetAmlDuration()==900000&&PictureDuration(codec.am_private)==10000000.0);
  codec.SetVideoRate(19200);assert(PictureDuration(codec.am_private)==200000.0);
 }
 // A query owns a counted permit: reset/close can fence but cannot mutate until
 // the query returns. Use the real presenter-owner handoff rather than moving a
 // main-owner permit to a worker and calling that production authorization.
 {
  CAMLCodec codec;std::promise<std::thread::id> workerId;std::promise<CAMLSession::OwnerTransfer> transfer;
  std::promise<void> entered,resume;auto resumed=resume.get_future().share();
  codec.driver.onQuery=[&]{entered.set_value();resumed.wait();};
  auto worker=std::async(std::launch::async,[&]{workerId.set_value(std::this_thread::get_id());
   assert(codec.m_session.AcceptOwner(transfer.get_future().get(),true));
   codec.Update();});
  transfer.set_value(codec.m_session.RequestOwner(workerId.get_future().get()));
  entered.get_future().wait();auto request=codec.m_session.Fence();
  assert(!codec.m_session.Wait(request,0ms)&&!codec.m_session.BeginMutation(request));
  assert(!codec.m_session.AcquireControl(codec.m_session.Epoch()));
  resume.set_value();worker.get();assert(codec.m_session.Wait(request,0ms));
  assert(codec.m_session.BeginMutation(request));codec.m_pollDevice=-1;codec.m_opened=false;
  assert(codec.m_session.Complete(request,false));assert(!codec.m_session.AcquireDecoder());
 }
 std::cout<<"PASS production queries/updates, rates/units, widened durations, permit exclusion and poll independence\n";
}
'''

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-control',action='store_true');args=parser.parse_args()
    source=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    header=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
    bounds='\n'.join(re.findall(r'constexpr unsigned int (?:MIN|MAX)_DECODER_VIDEO_RATE[^;]+;',source))
    rate_member=re.search(r'std::atomic<unsigned int> video_rate\{0\};',source).group()
    speed_member=re.search(r'std::atomic<int> m_speed;',header).group()
    rect=function(source,'void CAMLCodec::SetVideoRect(')
    rect=rect[:rect.index('  // video view mode')]+ '(void)update;(void)SrcRect;(void)DestRect;}\n'
    parts={'BOUNDS':bounds,'HINTS':function(source,'unsigned int VideoRateFromHints('),
      'RATE_MEMBER':rate_member,'SPEED_MEMBER':speed_member,
      'QUERY':function(source,'unsigned int CAMLCodec::GetDecoderVideoRate()'),
      'REFRESH':function(source,'void CAMLCodec::RefreshDecoderRate('),'RECT':rect,
      'SET':function(source,'void CAMLCodec::SetVideoRate('),'DURATION':function(source,'int CAMLCodec::GetAmlDuration() const'),
      'PICTURE':re.search(r'const double rate_duration\s*=\s*[^;]+;',source).group(),
      'DRAIN':re.search(r'const int64_t poll_ms\s*=\s*[^;]+;',source).group()}
    code=HARNESS
    for name,part in parts.items():code=code.replace('@'+name+'@',part)
    with tempfile.TemporaryDirectory(prefix='aml-video-rate-') as temp:
      cpp=Path(temp)/'check.cpp';exe=Path(temp)/'check'
      def run(text,negative=False):
        cpp.write_text(text)
        subprocess.run(['g++','-std=c++17','-pthread','-Wall','-Wextra','-Werror',
          '-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie',
          '-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(exe)],check=True)
        try:
          result=subprocess.run([str(exe)],env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=8)
        except subprocess.TimeoutExpired:
          assert negative;return 'bounded timeout'
        if negative:assert result.returncode!=0,result.stdout;return 'runtime rejection'
        print(result.stdout,end='');result.check_returncode()
      run(code)
      if args.negative_control:
        query=parts['QUERY'];guard='  if (vi.frame_dur < MIN_DECODER_VIDEO_RATE || vi.frame_dur > MAX_DECODER_VIDEO_RATE)\n    return 0;\n'
        assert guard in query
        mutations={
          'unguarded-decoder-duration':code.replace(query,query.replace(guard,'')),
          'drain-u32-product':code.replace(parts['DRAIN'],parts['DRAIN'].replace('static_cast<int64_t>(am_private->video_rate.load(std::memory_order_relaxed))','am_private->video_rate.load(std::memory_order_relaxed)')),
          'picture-u32-product':code.replace(parts['PICTURE'],'const double rate_duration = static_cast<double>(am_private->video_rate.load(std::memory_order_relaxed) * DVD_TIME_BASE) / UNIT_FREQ;'),
          'pts-u32-product':code.replace('static_cast<uint64_t>(am_private->video_rate.load(std::memory_order_relaxed))','am_private->video_rate.load(std::memory_order_relaxed)'),
          'poll-mutex-import':code.replace(query,query.replace('{','{\n  std::lock_guard<std::mutex> lock(pollSyncMutex);',1))}
        for name,mutant in mutations.items():
          assert mutant!=code;print('REJECTED',name,run(mutant,True))
if __name__=='__main__':main()
