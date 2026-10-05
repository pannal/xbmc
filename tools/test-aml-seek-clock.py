#!/usr/bin/env python3
"""Actual DVDClock correction/discontinuity and presenter selection with synthetic reference time.
No audio sink, full audio correction gates, hardware vsync, GUI or target proof.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
HARNESS=r"""
#include <cassert>
#include <cmath>
#include <mutex>
#include <memory>
#include <string>
#include <string_view>
#include <iostream>
class CVideoReferenceClock {
public:
 int64_t time=1000000;
 int64_t GetTime(bool=true){return time;}
};
#include "cores/VideoPlayer/DVDClock.h"
#include "Presenter.h"
constexpr int LOGDEBUG=0,LOGINFO=1;
struct CLog {template<class... T> static void Log(int,const char* format,T...){
 size_t count=0;for(std::string_view s(format);!s.empty();s.remove_prefix(1))if(s.front()=='{')++count;
 assert(count==sizeof...(T));
}};
CDVDClock::CDVDClock(){
 m_videoRefClock=std::make_unique<CVideoReferenceClock>();
 m_systemFrequency=m_systemUsed=1000000;m_systemOffset=0;m_startClock=1000000;
 m_pauseClock=0;m_iDisc=0;m_bReset=false;m_systemAdjust=0;m_lastSystemTime=1000000;
 m_speedAdjust=0;m_vSyncAdjust=0;m_frameTime=1e6*1001/24000;
}
CDVDClock::~CDVDClock()=default;
struct TestClock:CDVDClock {void Tick(){m_videoRefClock->time+=41708;}void SpeedAdjust(double value){m_speedAdjust=value;}};
@CLOCK@
struct CVideoPlayerVideo {
 struct Render {uint64_t DiagnosticId(){return 1;}} m_renderManager;
 uint64_t m_syncEpoch=2;int m_syncState=1;CDVDClock* m_pClock;
 void LogSyncTransition(const char*,double);
};
@VIDEO@
struct Frame:CAMLPresenter::Frame {
 Submission Submit(int&)override{return Submission::SUBMITTED;}
 bool Poll()override{return true;}bool Retire()override{return true;}
};
void selection(TestClock& clock,bool correction){
 clock.Discontinuity(1000000000,1000000);clock.SetVsyncAdjust(-30000);
 CAMLPresenter::Hooks hooks;hooks.adjustClock=[&](double value){clock.SetVsyncAdjust(value);};
 CAMLPresenter q(16,hooks);q.Show(true);q.m_syncOffset=30000;q.m_samples=12;
 for(int i=0;i<6;++i){auto f=std::make_shared<Frame>();f->epoch=3;f->pts=1000085000+i*(1e6*1001/24000);
  const auto serial=q.Reserve(std::chrono::milliseconds(0));assert(serial&&q.Publish(serial,f));}
 if(correction)assert(std::abs(clock.ErrorAdjust(30000,"test")-1e6*1001/24000)<0.001);
 q.Select({clock.GetClock(),1,24000.0/1001,135000,true});
 assert(q.Skipped()==(correction?1:0));
 if(correction){auto events=q.TakeSkipEpisodes();assert(events.size()==1&&events[0].render-events[0].pts>62000);}
 // Show/discard retain phase state in current policy; this alone does not establish a defect.
 const double offset=q.m_syncOffset;const int samples=q.m_samples;
 q.Show(false);q.Discard();clock.Discontinuity(500000000,1000000);
 assert(q.m_syncOffset==offset&&q.m_samples==samples&&clock.GetVsyncAdjust()==-30000);
 q.TakeFrames();
}
int main(){
 PLAYBACK_DIAGNOSTICS::SetEnabled(true);
 TestClock c;
 c.Discontinuity(1000000000,1000000);auto first=c.GetDiagnosticEvents(0);
 assert(first.serial==1&&first.events.size()==1&&!first.events[0].correction);
 assert(first.events[0].target==1000000000);
 c.SetVsyncAdjust(-30000);
 assert(c.ErrorAdjust(19000,"small")==0&&c.GetDiagnosticEvents(1).events.empty());
 assert(c.ErrorAdjust(-26000,"small")==0);
 c.SpeedAdjust(0.001);assert(c.ErrorAdjust(30000,"speed-adjust")==0);c.SpeedAdjust(0);
 // Feed the correction back through the actual clock, rather than a no-op adjustment hook.
 double desired=c.GetClock()+30000;double corrected=0;
 for(int i=0;i<240;++i){corrected+=c.ErrorAdjust(desired-c.GetClock(),"feedback");c.Tick();desired+=41708;}
 assert(std::abs(corrected-1e6*1001/24000)<0.001);
 auto report=c.GetDiagnosticEvents(1);assert(report.events.size()==1&&report.events[0].correction);
 assert(report.events[0].error==30000&&report.events[0].adjustment==corrected&&report.events[0].vsync==-30000);
 auto serial=report.serial;assert(c.GetDiagnosticEvents(serial).events.empty());
 c.Discontinuity(500000000,c.GetAbsoluteClock());desired=c.GetClock()-30000;corrected=0;
 for(int i=0;i<240;++i){corrected+=c.ErrorAdjust(desired-c.GetClock(),"seek-feedback");c.Tick();desired+=41708;}
 assert(std::abs(corrected+1e6*1001/24000)<0.001);
 report=c.GetDiagnosticEvents(serial);assert(report.events.size()==2&&!report.events[0].correction&&report.events[1].correction);
 c.SetVsyncAdjust(0);assert(c.ErrorAdjust(30000,"unquantized")==30000);
 // Eight-event bounded history and explicit loss; unchanged cursors do not consume events.
 serial=c.GetDiagnosticEvents(0).serial;
 for(int i=0;i<12;++i)c.Discontinuity(i*1000000,c.GetAbsoluteClock());
 report=c.GetDiagnosticEvents(serial);assert(report.events.size()==8&&report.lost==4);
 assert(report.events.front().serial==serial+5&&report.events.back().serial==serial+12);
 assert(c.GetDiagnosticEvents(report.serial).events.empty());
 TestClock a,b;selection(a,false);selection(b,true);
 CVideoPlayerVideo video;video.m_pClock=&c;video.LogSyncTransition("resync",c.GetClock());
 std::cout<<"PASS actual DVDClock feedback/seek anchors, phase retention, correction-induced skip witness, bounded history and sync log formatter\n";
}
"""

def main():
    extract=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
    src=(ROOT/'xbmc/cores/VideoPlayer/DVDClock.cpp').read_text()
    names=['double CDVDClock::GetClock(bool','double CDVDClock::GetClock(double&',
           'double CDVDClock::GetAbsoluteClock(', 'double CDVDClock::SystemToAbsolute(',
           'int64_t CDVDClock::AbsoluteToSystem(', 'double CDVDClock::SystemToPlaying(',
           'void CDVDClock::SetVsyncAdjust(', 'double CDVDClock::GetVsyncAdjust(',
           'double CDVDClock::ErrorAdjust(', 'void CDVDClock::Discontinuity(',
           'PLAYBACK_DIAGNOSTICS::ClockHistory::Report CDVDClock::GetDiagnosticEvents(']
    methods='\n'.join(extract(src,n) for n in names)
    video=(ROOT/'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    for name in ['resync','reset-complete','flush-complete','waitsync']:
        assert video.count('LogSyncTransition("'+name+'"')==1
    waiting=video[video.index('m_syncState = IDVDStreamPlayer::SYNC_WAITSYNC;'):]
    assert waiting.index('msg.timestamp = hasTimestamp') < waiting.index('LogSyncTransition("waitsync"') < waiting.index('m_messageParent.Put(std::make_shared<CDVDMsgType<SStartMsg>>')
    helper=extract(video,'void CVideoPlayerVideo::LogSyncTransition(')
    code=HARNESS.replace('@CLOCK@',methods).replace('@VIDEO@',helper)
    with tempfile.TemporaryDirectory(prefix='seek-clock-') as tmp:
        out=Path(tmp);(out/'threads').mkdir()
        (out/'threads/CriticalSection.h').write_text('#pragma once\n#include <mutex>\nusing CCriticalSection=std::recursive_mutex;\n')
        header=(ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h').read_text()
        (out/'Presenter.h').write_text(header.replace('\nprivate:','\npublic:'))
        (out/'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread',
                        '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(out),
                        '-I'+str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
        subprocess.run([str(out/'test')],check=True,timeout=20)

if __name__=='__main__':main()
