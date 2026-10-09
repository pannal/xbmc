#!/usr/bin/env python3
"""Production restart-hold, QBUF, poll and DQBUF-release fragment with real session
admission; device/sysfs, settings and clock ageing are controlled host inputs.
No hardware or scanout acceptance. --baseline-dqbuf replaces only the successful
DQBUF fragment with the requested old revision to reproduce its early release.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CODEC = 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp'

def function(s, sig):
    a = s.index(sig); b = s.index('{', a) + 1; depth = 1
    while depth:
        depth += (s[b] == '{') - (s[b] == '}'); b += 1
    return s[a:b]

PRELUDE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <cassert>
#include <cerrno>
#include <cstring>
#include <deque>
#include <fstream>
#include <future>
#include <linux/videodev2.h>
#include <memory>
#include <poll.h>
#include <string>
#include <thread>
constexpr int LOGDEBUG=0, LOGVIDEO=0, LOGERROR=0, LOGAVTIMING=0;
struct CLog {template<class... T> static void Log(T&&...) {}};
struct CSettings {static constexpr int SETTING_COREELEC_AMLOGIC_VIDEO_RESTART_MUTE_SETTLE=0, SETTING_COREELEC_AMLOGIC_VIDEO_RESTART_MUTE=1, SETTING_COREELEC_AMLOGIC_VIDEO_RESTART_MUTE_DV_ONLY=2;};
struct Settings {bool master=true,dvOnly=false,settle=false;int GetInt(const char*) {return 0;} bool GetBool(int id) {return id==1?master:id==2?dvOnly:settle;} Settings* GetSettings(){return this;}} settings;
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
struct Graphics {double GetFPS(){return 24.;} Graphics& GetGfxContext(){return *this;}} graphics;
struct CServiceBroker {static Settings* GetSettingsComponent(){return &settings;} static Graphics* GetWinSystem(){return &graphics;}};
static bool muted; static unsigned unmutes;
static std::function<void()> muteHook;
void aml_video_mute(bool mute){if(muteHook)muteHook();muted=mute;if(!mute)++unmutes;}
static std::string receiptPath;
static int nextQbuf=0, nextPoll=1; static unsigned qbufs; static short nextEvents=POLLOUT;
struct Device {int IOControl(unsigned long op,v4l2_buffer*){assert(op==VIDIOC_QBUF);++qbufs;errno=EIO;return nextQbuf;}};
int fixture_poll(pollfd* fds,nfds_t n,int timeout){assert(n==1&&timeout==50);fds[0].revents=nextEvents;return nextPoll;}
#define poll fixture_poll
std::mutex pollSyncMutex;
struct {void Set(){}} g_aml_sync_event;
class CAMLCodec {
public:
 CAMLSession m_session;
 struct {StreamHdrType hdrType=StreamHdrType::HDR_TYPE_NONE;}m_hints;
 bool VideoRestartHoldWanted() const;
 std::mutex m_presentationMutex,m_videoHoldMutex;
 bool m_presentationActive=true,m_videoHoldActive=false;
 uint64_t m_presentationGeneration=1,m_videoHoldProviderEpoch=0;
 int m_videoHoldTimeoutMs=3000,m_pollDevice=7;
 std::chrono::steady_clock::time_point m_videoHoldStart;
 std::unique_ptr<Device> m_amlVideoFile=std::make_unique<Device>();
 struct Frame {uint64_t pts,index;}; std::deque<Frame> m_reorderQueue{{5,3}};
 uint64_t m_prev_last_pts=0,m_last_pts=0,m_cur_pts=0,m_bufferIndex=0;
 void HoldVideo(bool);void CheckVideoHold();void ReleaseVideoHoldLocked();
 static bool ReadVideoPresentation(uint64_t&,uint64_t&);
 int PollFrame(const CAMLSession::Permit&);
 int ReleaseFrame(uint32_t,uint64_t,const CAMLSession::Permit&,bool=false);
 void DQBuf(){struct {int iFlags=0;} videoPicture; @DQBUF@ (void)videoPicture;}
 void Open(){auto r=m_session.Fence();assert(m_session.BeginMutation(r));assert(m_session.Complete(r,true));}
 void Reset(){auto r=m_session.Fence();assert(m_session.BeginMutation(r));HoldVideo(VideoRestartHoldWanted());assert(m_session.Complete(r,true));}
 void Submit(bool drop=false){auto p=m_session.Acquire(m_session.Epoch(),drop);assert(p);ReleaseFrame(3,1,p,drop);}
 void Poll(){auto p=m_session.Acquire(m_session.Epoch());assert(p);PollFrame(p);}
};
'''
TESTS = r'''
void receipt(const char* text){std::ofstream out(receiptPath);out<<text;}
void age(CAMLCodec& c){c.m_videoHoldStart-=std::chrono::seconds(4);}
int main(int argc,char** argv){
 assert(argc==2);receiptPath=argv[1];
 CAMLCodec c;c.Open();receipt("1 8 0");c.HoldVideo(true);c.DQBuf();
 assert(muted && c.m_videoHoldActive); // old GetPicture releases here
 c.Poll();assert(muted); // old/pacing wake without accepted submission
 receipt("1 8 8");c.Submit(true);c.Poll();assert(muted); // drop never arms
 nextQbuf=-1;c.Submit();c.Poll();assert(muted);nextQbuf=0;
 c.m_presentationActive=false;c.Submit();c.Poll();assert(muted);c.m_presentationActive=true;
 {auto p=c.m_session.Acquire(c.m_session.Epoch());c.ReleaseFrame(3,99,p);}c.Poll();assert(muted);
 {CAMLSession other;auto r=other.Fence();assert(other.BeginMutation(r));assert(other.Complete(r,true));
 auto p=other.Acquire(other.Epoch());c.ReleaseFrame(3,1,p);}c.Poll();assert(muted);
 receipt("1 8 0");c.Submit();c.Poll();assert(muted);
 receipt("1 7 7");c.Poll();assert(muted); // stale provider receipt
 receipt("1 9 9");c.Poll();assert(muted); // replaced provider receipt
 receipt("1 8 8");c.Poll();assert(!muted&&unmutes==1); // paused first frame, no second DQBUF
 c.Reset();receipt("1 10 0");c.Submit();c.Reset();
 receipt("1 10 10");c.Poll();assert(muted); // prior seek's completion
 receipt("1 11 0");c.Submit();c.Poll();assert(muted);
 receipt("1 11 11");c.Poll();assert(!muted&&unmutes==2);
 for(const char* bad:{"", "1", "broken", "2 12 12", "1 0 0"}){
  c.Reset();receipt(bad);c.Submit();c.Poll();assert(muted);age(c);c.Poll();assert(!muted);
 }
 c.Reset();receipt("1 13 0");c.Submit();age(c);c.CheckVideoHold();assert(!muted); // decoder-side cap
 c.Reset();c.HoldVideo(false);assert(!muted); // close/settings-off cancellation
 receipt("1 13 13");c.Poll();assert(!muted);
 // A concurrent release cannot cross renewed hold ownership. The mute write is
 // inside the same lock as epoch/deadline changes, not an atomic bool-only fix.
 c.Reset();receipt("1 14 14");c.Submit();
 std::promise<void> entered,finish;auto ready=finish.get_future().share();
 std::atomic<int> calls{0};muteHook=[&]{if(calls.fetch_add(1)==0){entered.set_value();ready.wait();}};
 auto release=std::async(std::launch::async,[&]{c.CheckVideoHold();});entered.get_future().wait();
 auto renew=std::async(std::launch::async,[&]{c.HoldVideo(true);});
 assert(renew.wait_for(std::chrono::milliseconds(20))==std::future_status::timeout);
 finish.set_value();release.get();renew.get();muteHook={};assert(muted&&c.m_videoHoldProviderEpoch==0);
 c.HoldVideo(false);
 settings.master=false;c.Reset();assert(!muted);c.Submit();c.Poll();assert(!muted);
 settings.master=true;settings.dvOnly=true;c.Reset();assert(!muted);
 c.m_hints.hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;c.Reset();assert(muted);c.HoldVideo(false);
 settings.settle=true;c.Reset();assert(muted);c.HoldVideo(false); // existing opt-in assertion settle only
 settings.dvOnly=false;settings.settle=false;c.m_hints.hdrType=StreamHdrType::HDR_TYPE_NONE;
 c.Reset();assert(muted);settings.master=false;c.Reset();assert(!muted); // live disable cancels existing hold
 puts("PASS restart hold: DQBUF/QBUF/poll separation, stale/drop/failure guards, epochs, paused frame, timeout/cancel, serialized ownership");
}
'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--negative-controls',action='store_true')
    p.add_argument('--baseline-dqbuf')
    args=p.parse_args();s=(ROOT/CODEC).read_text();dq=s
    if args.baseline_dqbuf:
        dq=subprocess.check_output(['git','show',f'{args.baseline_dqbuf}:{CODEC}'],cwd=ROOT,text=True)
    body=function(dq,'CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(')
    dq=body[body.index('    m_prev_last_pts ='):body.index('    // Frame mode:')]
    methods='\n'.join(function(s,sig) for sig in [
        'bool CAMLCodec::VideoRestartHoldWanted(', 'void CAMLCodec::HoldVideo(', 'bool CAMLCodec::ReadVideoPresentation(',
        'void CAMLCodec::ReleaseVideoHoldLocked(', 'void CAMLCodec::CheckVideoHold(',
        'int CAMLCodec::ReleaseFrame(', 'int CAMLCodec::PollFrame('])
    methods=methods.replace('"/sys/class/video/presentation_state"','receiptPath')
    assert 'CheckVideoHold();' in function(s,'CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(')
    assert 'HoldVideo(false);' in function(s,'void CAMLCodec::CloseDecoderInternal(')
    assert 'HoldVideo(VideoRestartHoldWanted());' in function(s,'void CAMLCodec::ResetInternal(')
    source=PRELUDE.replace('@DQBUF@',dq)+methods+TESTS
    with tempfile.TemporaryDirectory(prefix='aml-restart-hold-') as d:
        d=Path(d)
        def run(source,negative=False):
            (d/'test.cpp').write_text(source)
            cmd=[os.environ.get('CXX','g++'),'-std=c++17','-pthread','-Wall','-Wextra','-Werror',
                 '-I',str(ROOT/'xbmc'),str(d/'test.cpp'),'-o',str(d/'test')]
            if not negative:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
            subprocess.run(cmd,check=True)
            r=subprocess.run([str(d/'test'),str(d/'receipt')],capture_output=True,text=True,timeout=15,
                             env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
            if negative:
                assert r.returncode and 'Assertion' in r.stderr,r.stderr
            else:
                print(r.stdout,end='');assert r.returncode==0,r.stderr
        run(source)
        if args.negative_controls:
            for name,old,new in [
                ('DQBUF releases old frame',dq,dq+'HoldVideo(false);'),
                ('generic poll means applied','applied == epoch','true'),
                ('provider mismatch accepted','epoch == m_videoHoldProviderEpoch','true'),
                ('drop arms receipt','else if (!drop)','else'),
                ('failed QBUF arms receipt','else if (!drop)','if (!drop)'),
                ('renewal retains prior submission','m_videoHoldProviderEpoch = 0;\n    m_videoHoldStart','m_videoHoldStart'),
                ('paused poll does not observe receipt','  CheckVideoHold();','  // no receipt check'),
            ]:
                assert old in source;run(source.replace(old,new,1),True);print('Rejected:',name)
if __name__=='__main__':main()
