#!/usr/bin/env python3
"""Production interval/counter and fixed-affinity boundaries; no target/device claim."""
import os
import runpy
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST = r'''
#include "utils/PlaybackDiagnostics.h"
#include "threads/PerformanceCores.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <cassert>
#include <future>
#include <thread>
using namespace PLAYBACK_DIAGNOSTICS;
using namespace std::chrono_literals;
int main() {
  PLAYBACK_DIAGNOSTICS::SetEnabled(true);
  MainService tracker;
  Sample first{100,{}}; first.progress.qbufAttempts=500;
  assert(!tracker.Observe(first));
  Sample second=first;second.atUs=300100;second.progress.qbufAttempts+=2;second.progress.completed+=1;
  assert(!tracker.Observe(second));
  Sample third=second;third.atUs=400100;third.progress.qbufAttempts+=900;
  assert(!tracker.Observe(third));
  Sample fourth=third;fourth.atUs=5400100;fourth.progress.polls=3;
  auto report=tracker.Observe(fourth);assert(report);
  assert(report->gaps==2 && report->worst.fromUs==third.atUs);
  assert(report->worst.toUs==fourth.atUs && report->worst.progress.qbufAttempts==0);
  assert(report->worst.progress.polls==3 && report->interval.qbufAttempts==902);
  Sample last=fourth;last.atUs+=10;auto final=tracker.Observe(last,true);
  assert(final && final->gaps==0 && final->interval.qbufAttempts==0);
  // A separate session cannot inherit a previous lifetime's counters.
  MainService fresh;assert(!fresh.Observe({1,{}}));
  CaptureTimings captures;
  auto c=captures.Complete(100,300,true);assert(c && c->duration.calls==1 && c->duration.totalUs==200);
  assert(!captures.Complete(500,800,false));
  c=captures.Complete(5000300,5000400,true);
  assert(c && c->duration.calls==2 && c->failures==1 && c->duration.totalUs==400 && c->duration.maxUs==300);
  VideoStages video;video.sinceUs=video.loopUs=100;
  video.BeginIteration(6000100,2);
  assert(video.maxLoopUs==6000000 && video.modes==4);
  video.BeginIteration(6000110,0);assert(video.maxLoopUs==6000000 && video.modes==5);
  Duration d;d.Add(7);d.Add(2);assert(d.calls==2&&d.totalUs==9&&d.maxUs==7);
  CAMLSession session;
  auto request=session.Fence();assert(session.BeginMutation(request));assert(session.Complete(request,true));
  const auto original=session.Diagnostics();
  session.RecordQbuf(false,0,12);session.RecordQbuf(true,-1,21);
  session.RecordPoll(0,false,50000);session.RecordPoll(1,true,10);
  session.RecordPoll(-1,false,5);session.RecordPoll(1,false,8);
  auto a=session.Diagnostics();
  assert(a.id==original.id && a.epoch==original.epoch && a.request==original.request);
  assert(a.qbufCalls==1&&a.dropCalls==1&&a.qbufErrors==1&&a.qbufUs==33);
  assert(a.pollReady==1&&a.pollTimeout==1&&a.pollError==1&&a.pollOther==1&&a.pollUs==50023);
  {
    auto lease=session.AcquireControl(a.epoch,true);assert(lease&&lease.IsControl());
    assert(session.Diagnostics().controlActive);
    assert(!session.AcquireControl(a.epoch));
    std::this_thread::sleep_for(1ms);
  }
  a=session.Diagnostics();assert(!a.controlActive && a.controlDenied==1 && a.controlHoldUs>0);
  auto native=CAMLSession::FenceNative();assert(native);
  a=session.Diagnostics();assert(a.native==native->serial&&a.nativePhase==0&&a.nativeFenced);
  assert(CAMLSession::TryBeginNative(native));assert(session.Diagnostics().nativePhase==1);
  assert(CAMLSession::EndNative(native));assert(!session.Diagnostics().nativeFenced);
  assert(PERFORMANCE_CORES::Requested()==std::set<int>({2,3,4,5}));
  for(int mode=0;mode<4;++mode){
    int sets=0,reads=0;
    const auto result=PERFORMANCE_CORES::Apply([&](const std::set<int>& mask){
      ++sets;assert(mask==std::set<int>({2,3,4,5}));return mode!=1;
    },[&](std::set<int>& mask){++reads;mask=mode==2?std::set<int>{2,3}:std::set<int>{2,3,4,5};return mode!=3;});
    assert(sets==1&&reads==1&&result.Complete()==(mode==0));
  }
}
'''


NATIVE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include "utils/log.h"
#include <poll.h>
#include <linux/videodev2.h>
#include <mutex>
#include <memory>
#include <cstring>
#include <cerrno>
constexpr int LOGAVTIMING=0, LOGVIDEO=0;
int nextPoll=0,nextQbuf=0;short nextEvents=0;
int FakePoll(pollfd* fds,nfds_t count,int timeout){assert(count==1&&timeout==50);fds[0].revents=nextEvents;return nextPoll;}
#define poll FakePoll
std::mutex pollSyncMutex;
struct {void Set(){}} g_aml_sync_event;
struct Device {int IOControl(unsigned long cmd,v4l2_buffer*){assert(cmd==VIDIOC_QBUF);errno=EIO;return nextQbuf;}};
struct CAMLCodec {
  CAMLSession m_session;
  std::mutex m_presentationMutex;
  bool m_presentationActive=true;
  uint64_t m_presentationGeneration=1;
  int m_pollDevice=7;
  std::unique_ptr<Device> m_amlVideoFile=std::make_unique<Device>();
  int PollFrame(const CAMLSession::Permit&);
  int ReleaseFrame(uint32_t,uint64_t,const CAMLSession::Permit&,bool=false);
};
struct CVideoPlayerVideo {
  PLAYBACK_DIAGNOSTICS::VideoStages m_diagnostics;
  struct {uint64_t DiagnosticId(){return 7;}}m_renderManager;
  uint64_t m_syncEpoch=3;
  int m_speed=1000,m_syncState=2,m_iDroppedFrames=0;
  bool m_paused=false,m_pendingResetMessage=false;
  std::atomic_bool m_isEOS{false},m_stalled{false};
  struct {int GetDataSize(){return 0;}}m_messageQueue;
  void LogDiagnostics(bool);
};
namespace PERFORMANCE_CORES {int CurrentCpu(){return -1;}}
@METHODS@
int main(){
  PLAYBACK_DIAGNOSTICS::SetEnabled(false);
  CAMLCodec unobserved;auto offRequest=unobserved.m_session.Fence();
  assert(unobserved.m_session.BeginMutation(offRequest));assert(unobserved.m_session.Complete(offRequest,true));
  {
    auto permit=unobserved.m_session.Acquire(unobserved.m_session.Epoch());assert(permit);
    assert(unobserved.PollFrame(permit)==1);
    assert(unobserved.ReleaseFrame(2,1,permit)==0);
  }
  const auto off=unobserved.m_session.Diagnostics();
  assert(off.pollTimeout==0&&off.qbufCalls==0&&off.pollUs==0&&off.qbufUs==0);
  CVideoPlayerVideo muted;muted.m_diagnostics.pictures=3;CLog::lastLevel=-1;
  muted.LogDiagnostics(true);assert(CLog::lastLevel==-1);
  PLAYBACK_DIAGNOSTICS::SetEnabled(true);
  CVideoPlayerVideo video;video.m_diagnostics.pictures=3;video.LogDiagnostics(true);
  assert(video.m_diagnostics.pictures==0);
  const auto origin=video.m_diagnostics.loopUs;assert(origin>0&&video.m_diagnostics.sinceUs==origin);
  video.m_diagnostics.BeginIteration(origin+6000000,0);
  assert(video.m_diagnostics.maxLoopUs==6000000);
  CAMLCodec codec;auto r=codec.m_session.Fence();assert(codec.m_session.BeginMutation(r));assert(codec.m_session.Complete(r,true));
  {
    auto permit=codec.m_session.Acquire(codec.m_session.Epoch());assert(permit);
    assert(codec.PollFrame(permit)==1); // Preserve existing timeout return policy.
    nextPoll=-1;assert(codec.PollFrame(permit)==1);
    nextPoll=1;nextEvents=POLLOUT;assert(codec.PollFrame(permit)==1);
    nextEvents=POLLERR;assert(codec.PollFrame(permit)==1);
    assert(codec.ReleaseFrame(2,1,permit)==0);
    nextQbuf=-1;assert(codec.ReleaseFrame(3,1,permit,true)==-1);
  }
  auto d=codec.m_session.Diagnostics();
  assert(d.pollTimeout==1&&d.pollError==1&&d.pollReady==1&&d.pollOther==1);
  assert(d.qbufCalls==1&&d.dropCalls==1&&d.qbufErrors==1);
  auto control=codec.m_session.AcquireControl(d.epoch);assert(control);
  assert(codec.PollFrame(control)==0&&codec.ReleaseFrame(2,1,control)==0);
  auto after=codec.m_session.Diagnostics();assert(after.qbufCalls==1&&after.pollReady==1);
}
'''


AFFINITY = r'''
#include <sched.h>
#include <cassert>
#include <cerrno>
#include <thread>
#include <string>
#include "utils/log.h"
static const auto parent=std::this_thread::get_id();
static int mode=0,sets=0,reads=0;
int FakeSet(pid_t tid,size_t,const cpu_set_t* mask) {
  assert(tid==0 && std::this_thread::get_id()!=parent);++sets;
  for(int n=0;n<CPU_SETSIZE;++n)assert(bool(CPU_ISSET(n,mask))==(n>=2&&n<=5));
  if(mode==1){errno=EPERM;return -1;}return 0;
}
int FakeGet(pid_t tid,size_t,cpu_set_t* mask) {
  assert(tid==0);++reads;CPU_ZERO(mask);
  if(mode==3){errno=EINVAL;return -1;}
  for(int n=2;n<=(mode==2?3:5);++n)CPU_SET(n,mask);
  return 0;
}
#define sched_setaffinity FakeSet
#define sched_getaffinity FakeGet
#include "threads/PerformanceCores.cpp"
int main() {
  PLAYBACK_DIAGNOSTICS::SetEnabled(true);
  for(mode=0;mode<4;++mode){
    std::thread worker([]{PERFORMANCE_CORES::ApplyCurrentThread("fixture",19);});worker.join();
#ifdef HAS_LIBAMCODEC
    assert(sets==mode+1 && reads==mode+1);
    assert(CLog::lastLevel==(mode==0?LOGINFO:LOGERROR));
#else
    assert(sets==0 && reads==0 && CLog::lastLevel==LOGDEBUG);
#endif
  }
}
'''
LOG_STUB = r'''
#pragma once
#include <cassert>
#include <string>
constexpr int LOGDEBUG=0,LOGINFO=1,LOGERROR=2;
struct CLog {
  static inline int lastLevel=-1;
  template<class...T> static void Log(int level,const char* format,T&&...) {
    lastLevel=level;size_t count=0;
    for(const char* p=format;*p;++p)if(*p=='{')++count;
    assert(count==sizeof...(T));
  }
  template<class...T> static void Log(int level,int,const char* format,T&&...args) {
    Log(level,format,std::forward<T>(args)...);
  }
};
'''


def main():
    with tempfile.TemporaryDirectory(prefix='playback-diagnostics-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(TEST)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=20)
        (out / 'utils').mkdir()
        (out / 'utils/log.h').write_text(LOG_STUB)
        extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
        source = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
        methods = '\n'.join(extract(source, name) for name in
                            ['int CAMLCodec::PollFrame(', 'int CAMLCodec::ReleaseFrame('])
        video_source = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
        methods += '\n' + extract(video_source, 'void CVideoPlayerVideo::LogDiagnostics(')
        (out / 'native.cpp').write_text(NATIVE.replace('@METHODS@', methods))
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        str(out / 'native.cpp'), '-o', str(out / 'native')], check=True)
        subprocess.run([str(out / 'native')], check=True, timeout=20)
        (out / 'affinity.cpp').write_text(AFFINITY)
        for target in [True, False]:
            defines = ['-DTARGET_LINUX=1', '-DHAS_LIBAMCODEC=1'] if target else []
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            *defines, '-I', str(out), '-I', str(ROOT / 'xbmc'),
                            str(out / 'affinity.cpp'), '-o', str(out / 'affinity')], check=True)
            subprocess.run([str(out / 'affinity')], check=True, timeout=20)
    print('Playback diagnostics: PASS (production intervals/counters; fixed mask and error/restriction results; ASan/UBSan)')


if __name__ == '__main__':
    main()
