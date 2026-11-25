#!/usr/bin/env python3
"""Run production cache refill decisions against a seekable local sparse file.

Compiles the complete CircularCache implementation, CDoubleCache methods,
CWriteRate, FileCache Process/IoControl, VideoPlayer GetCachingTimes and its
CACHESTATE_FULL branch, SynchronizeStreams and SetCaching. Clock, events,
source pacing and decoder queues are
host adapters. This verifies refill classification and buffering exit, not
HTTP/FFmpeg execution, A/V resync or physical playback timing.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block(source, marker):
    start = source.index(marker)
    begin = source.index('{', start)
    level, end = 1, begin + 1
    while level:
        level += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


SUPPORT = r'''
#pragma once
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <unistd.h>
using namespace std::chrono_literals;
inline int64_t nowMs=0;
struct TestClock {
 using duration=std::chrono::milliseconds;
 using time_point=std::chrono::time_point<TestClock>;
 static time_point now(){return time_point(duration(nowMs));}
};
using CCriticalSection=std::recursive_mutex;
struct CEvent {
 bool signaled=false;
 void Set(){signaled=true;}
 void Reset(){signaled=false;}
 bool Wait(std::chrono::milliseconds duration){
  if(signaled)return true;
  nowMs+=duration.count();return false;
 }
};
namespace XbmcThreads {
template<class T=std::chrono::milliseconds> struct EndTime {
 int64_t end=0;
 EndTime()=default;
 explicit EndTime(T duration):end(nowMs+duration.count()){}
 void Set(T duration){end=nowMs+duration.count();}
 bool IsTimePast()const{return nowMs>=end;}
};
}
constexpr int LOGERROR=0, LOGDEBUG=1, LOGWARNING=2, LOGVIDEO=3, LOGAUDIO=4;
struct CLog {template<class...T>static void Log(T&&...) {}};
namespace fmt {template<class T>T* ptr(T* p){return p;}}
inline int GetLastError(){return 0;}
struct Settings {int GetInt(const char*){return 400;}};
struct SettingsComponent {std::shared_ptr<Settings> GetSettings(){return std::make_shared<Settings>();}};
struct Logging {bool IsLogLevelLogged(int){return false;}};
struct Messenger {void PostMsg(int){}};
struct CServiceBroker {
 static Messenger* GetAppMessenger(){static Messenger m;return &m;}
 static SettingsComponent* GetSettingsComponent(){static SettingsComponent s;return &s;}
 static Logging& GetLogging(){static Logging l;return l;}
};
struct CSettings {
 static constexpr const char* SETTING_COREELEC_AMLOGIC_DV_AUDIO_SEAMLESSBRANCH="sync";
 static constexpr const char* SETTING_FILECACHE_READFACTOR="factor";};
'''

HARNESS = r'''
#include "support.h"
#include "filesystem/CircularCache.h"
#include "filesystem/IFileTypes.h"
using namespace XFILE;
@STRATEGIES@
@RATE@;
constexpr int64_t MiB=1024*1024, CAPACITY=15*MiB/2;
struct LocalSource {
 FILE* file=std::tmpfile();
 int64_t length=512*MiB, reads=0, seeks=0, epoch=0;
 int fillMs=147;
 std::function<void()> beforeIteration;
 LocalSource(){assert(file);assert(ftruncate(fileno(file),length)==0);}
 ~LocalSource(){std::fclose(file);}
 int64_t GetLength(){if(beforeIteration)beforeIteration();return length;}
 int64_t Seek(int64_t pos,int whence){++seeks;assert(fseeko(file,pos,whence)==0);epoch=nowMs;reads=0;return ftello(file);}
 ssize_t Read(char* data,size_t size){
  // 59 x 128 KiB reaches the production near-full test for a 7.5 MiB buffer.
  ++reads;nowMs=epoch+reads*fillMs/59;
  return std::fread(data,1,size,file);
 }
 int IoControl(EIoControl,void*){return 1;}
};
struct CFileCache {
 std::unique_ptr<CCacheStrategy> m_pCache=std::make_unique<CDoubleCache>(new CCircularCache(CAPACITY,5*MiB/2));
 LocalSource m_source;
 std::string m_sourcePath="local fixture";
 bool m_bStop=false,m_bFilling=true;
 unsigned m_chunkSize=128*1024;
 int64_t m_forwardCacheSize=CAPACITY,m_maxForward=CAPACITY,m_fileSize=512*MiB;
 int64_t m_writePos=0,m_readPos=0,m_seekPos=0,m_nSeekResult=0;
 int m_seekPossible=1;
 uint32_t m_writeRate=11937742,m_writeRateActual=0,m_writeRateLowSpeed=0;
 CEvent m_seekEvent,m_seekEnded;
 CCriticalSection m_sync;
 std::chrono::milliseconds m_processWait{30};
 std::function<void()> m_readWaitCallback;
 std::function<bool()> m_readInterrupt;
 int eofWaits=0;
 CFileCache(){assert(m_pCache->Open()==CACHE_RC_OK);}
 int AbortableWait(CEvent&){++eofWaits;return 0;}
 void Process();
 int IoControl(EIoControl,void*);
};
constexpr int WAIT_SIGNALED=1;
@PROCESS@
@IOCONTROL@
struct CacheInfo {bool valid=false;double level=0,offset=0,time=0;};
struct Input {
 CFileCache* cache;
 bool GetCacheStatus(SCacheStatus* status){return cache->IoControl(IOCTRL_CACHE_STATUS,status)==0;}
 int64_t GetLength(){return cache->m_fileSize;}
 int64_t Seek(int64_t,int){return cache->m_readPos;}
 bool IsRealtime(){return false;}
};
struct Demux {int GetStreamLength(){return 45000;}};
constexpr double DVD_NOPTS_VALUE=-1, DVD_TIME_BASE=1000000;
constexpr int DVD_PLAYSPEED_NORMAL=1000, DVD_PLAYSPEED_PAUSE=0, TMSG_SWITCHTOFULLSCREEN=1;
#define DVD_SEC_TO_TIME(x) (1000000.0*(x))
struct CDVDMsg {enum {GENERAL_RESYNC};};
struct CDVDMsgDouble {int type;double value;CDVDMsgDouble(int t,double v):type(t),value(v){}};
struct Stream {
 int speed=0,resyncs=0;double pts=0;
 bool AcceptsData(){return true;}int GetLevel(){return 5;}
 bool IsStalled(){return false;}
 void SetSpeed(int value){speed=value;}
 void SendMessage(std::shared_ptr<CDVDMsgDouble> msg,int){++resyncs;pts=msg->value;}
};
struct IDVDStreamPlayer {enum {SYNC_STARTING,SYNC_WAITSYNC,SYNC_INSYNC};};
struct CCurrentStream {enum {AV_SYNC_NONE,AV_SYNC_CONT};};
struct Current {
 int id=1,syncState=IDVDStreamPlayer::SYNC_WAITSYNC,avsync=CCurrentStream::AV_SYNC_NONE;
 unsigned packets=21;
 double starttime=3843756000.0,cachetime=50000,cachetotal=100000;
};
enum ECacheState {CACHESTATE_FULL,CACHESTATE_INIT,CACHESTATE_PLAY,CACHESTATE_DONE,CACHESTATE_FLUSH};
struct Clock {
 int speed=0;double pts=0;
 double GetClock(){return pts;}
 void Discontinuity(double value){pts=value;}
 void SetSpeed(int value){speed=value;}
 void SetSpeedAdjust(double){}
};
struct CFileItem {};
struct IPlayerCallback {void OnAVStarted(CFileItem){}};
struct Outbound {template<class T>void Submit(T callback){callback();}};
inline int toasts=0;
struct CGUIDialogKaiToast {static void QueueNotification(int,int){++toasts;}};
struct Localize {int Get(int id){return id;}}g_localizeStrings;
#define DVD_MSEC_TO_TIME(x) (1000.0*(x))
struct CVideoPlayer {
 Input* m_pInputStream;
 Demux demux;Demux* m_pDemuxer=&demux;
 Stream audio,video;Stream* m_VideoPlayerAudio=&audio;Stream* m_VideoPlayerVideo=&video;
 Current m_CurrentAudio,m_CurrentVideo;
 double queueMs=0,m_messageQueueTimeSize=16;
 ECacheState m_caching=CACHESTATE_FULL;
 explicit CVideoPlayer(Input* input):m_pInputStream(input){}
 double GetQueueTime(){return queueMs;}
 bool m_bAbortRequest=false,m_bStop=false;
 bool ParentLifecyclePending(){return false;}
 bool WaitForActiveAreaDetection(){return false;}
 bool m_HasAudio=true,m_HasVideo=true;
 int m_playSpeed=DVD_PLAYSPEED_NORMAL,m_streamPlayerSpeed=0;
 Clock m_clock;
 XbmcThreads::EndTime<> m_syncTimer,m_cachingTimer;
 TestClock::time_point m_syncStartPtsWait{};
 double m_videoRecoveryStartPts=DVD_NOPTS_VALUE,m_videoRecoveryStartTime=DVD_NOPTS_VALUE;
 struct {bool streamsReady=true;}m_State;
 struct {bool fullscreen=false;}m_playerOptions;
 IPlayerCallback m_callback;CFileItem m_item;
 Outbound outbound;Outbound* m_outboundEvents=&outbound;
 void UpdatePlayState(int){}
 void FlushBuffers(double,bool,bool,std::initializer_list<int>,bool){assert(false);}
 void SetCaching(ECacheState state);
 void SynchronizeStreams(bool allowRecovery);
 CacheInfo GetCachingTimes();
 void CheckFull(){@FULL@}
};
@CACHING@
@SETCACHING@
@SYNC@

void runSequence(bool slow,bool observe){
 nowMs=0;toasts=0;
 CFileCache cache;
 cache.m_source.fillMs=slow?2950:147;
 Input input{&cache};CVideoPlayer player(&input);
 int epoch=0;
 int64_t start=0;
 bool awaitingFill=true;
 cache.m_source.beforeIteration=[&]{
  if(!awaitingFill||cache.m_bFilling)return;
  awaitingFill=false;
  assert(cache.m_pCache->WaitForData(0,0ms)>=CAPACITY-int64_t(cache.m_chunkSize));
  const int64_t elapsed=nowMs-start;
  assert(elapsed==(slow?2950:147));
  const uint32_t conservative=cache.m_writeRateActual;
  // Bias remains in published currate, including in the fixed implementation.
  const uint32_t expected=1000*((59*cache.m_chunkSize)/(elapsed+1000));
  assert(conservative==expected);
  const uint32_t measured=cache.m_writeRateLowSpeed;
  const int previousToasts=toasts;
  player.CheckFull();
  std::cout<<(slow?"slow":"fast")<<" epoch="<<epoch<<" fill_ms="<<elapsed
           <<" biased_Bps="<<conservative<<" lowrate="<<measured
           <<" toasts="<<toasts-previousToasts<<"\n";
  if(!observe){
   assert(toasts-previousToasts==(slow?1:0));
   assert(player.m_caching==(slow?CACHESTATE_INIT:CACHESTATE_FULL));
   if(slow)assert(measured>0&&measured<cache.m_writeRate);
  }
  // Status consumes the notification once, independently of the player's state.
  SCacheStatus status{};cache.IoControl(IOCTRL_CACHE_STATUS,&status);assert(status.lowrate==0);
  player.m_caching=CACHESTATE_FULL;player.CheckFull();assert(toasts==previousToasts+(measured>0));
  // Deliver valid decoder start PTS at the modeled capture's 220 ms point.
  // Run the complete production resync and clock/speed transitions. This is
  // controlled decoder readiness, not a measurement of real decode latency.
  nowMs=start+(slow?3000:220);
  player.m_CurrentAudio.syncState=player.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_WAITSYNC;
  player.SynchronizeStreams(true);
  assert(player.m_caching==CACHESTATE_DONE);
  assert(player.m_clock.speed==DVD_PLAYSPEED_NORMAL);
  assert(player.audio.speed==DVD_PLAYSPEED_NORMAL&&player.video.speed==DVD_PLAYSPEED_NORMAL);
  assert(player.audio.resyncs==epoch+1&&player.video.resyncs==epoch+1);
  assert(player.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_INSYNC);
  assert(player.m_CurrentVideo.syncState==IDVDStreamPlayer::SYNC_INSYNC);
  assert(player.m_clock.pts==player.audio.pts&&player.audio.pts==player.video.pts);
  if(!observe)assert(toasts==previousToasts+(slow?1:0));
  std::cout<<"resync and playback clock resumed epoch="<<epoch<<" at_ms="<<nowMs-start<<"\n";

  if(epoch==3){cache.m_bStop=true;return;}
  // Repeated full misses evict both ranges; this is not the near-forward wait.
  const int64_t target=(epoch+1)*64*MiB;
  assert(!cache.m_pCache->IsCachedPosition(target));
  assert(cache.m_pCache->Seek(target)==CACHE_RC_ERROR);
  cache.m_seekPos=target;cache.m_seekEvent.Set();
  start=nowMs;awaitingFill=true;++epoch;
  player.m_caching=CACHESTATE_FULL;player.queueMs=0;
 };
 cache.Process();assert(epoch==3);assert(cache.m_source.seeks==3);
 if(!observe)assert(toasts==(slow?4:0));
}

void cachedControls(){
 nowMs=0;toasts=0;
 CFileCache cache;Input input{&cache};CVideoPlayer player(&input);
 int stage=0;int64_t seeks=0;uint32_t previousRate=0;
 cache.m_source.beforeIteration=[&]{
  if(stage==0&&!cache.m_bFilling){
   assert(cache.m_writeRateLowSpeed==0);
   SCacheStatus s{};cache.IoControl(IOCTRL_CACHE_STATUS,&s);
   assert(cache.m_pCache->Seek(MiB)==MiB); // active cache hit, no writer event
   cache.m_readPos=MiB;assert(!cache.m_bFilling&&cache.m_source.seeks==0);
   cache.m_seekPos=64*MiB;cache.m_seekEvent.Set();stage=1;
  }else if(stage==1&&!cache.m_bFilling){
   assert(cache.m_source.seeks==1&&cache.m_writeRateLowSpeed==0);
   previousRate=cache.m_writeRateActual;
   // The old cache holds 1 MiB; a real double-cache seek requests the swap.
   assert(cache.m_pCache->IsCachedPosition(MiB));
   assert(cache.m_pCache->Seek(MiB)==CACHE_RC_ERROR);
   cache.m_seekPos=MiB;cache.m_seekEvent.Set();stage=2;seeks=cache.m_source.seeks;
  }else if(stage==2){
   assert(cache.m_source.seeks==seeks+1&&cache.m_nSeekResult==MiB);
   assert(!cache.m_bFilling&&cache.m_writeRateLowSpeed==0);
   // Preserved average spans both fills; an accidental full reset collapses it.
   assert(cache.m_writeRateActual>=previousRate);
   player.queueMs=16000;player.CheckFull();
   assert(toasts==0&&player.m_caching==CACHESTATE_INIT);
   cache.m_bStop=true;stage=3;
  }
 };
 cache.Process();assert(stage==3);
 std::cout<<"PASS active and old cached seeks: no new full refill or toast, average retained\n";
}

void eofControl(){
 nowMs=0;toasts=0;CFileCache cache;
 cache.m_source.length=2*int64_t(cache.m_chunkSize);
 cache.Process();
 assert(cache.eofWaits==1&&cache.m_pCache->IsEndOfInput());
 assert(cache.m_writePos==cache.m_source.length&&cache.m_writeRateLowSpeed==0);
 char data[128*1024];int64_t bytes=0;int count;
 while((count=cache.m_pCache->ReadFromCache(data,sizeof(data)))>0)bytes+=count;
 assert(count==0&&bytes==cache.m_source.length);
 std::cout<<"PASS short-file EOF: bytes retained, no low-speed warning\n";
}
int main(int argc,char**argv){
 const bool observe=argc>1&&std::string(argv[1])=="observe";
 runSequence(false,observe);runSequence(true,observe);
 if(!observe){cachedControls();eofControl();}
}
'''


def generate(revision=None, mutant=None):
    def read(path):
        if revision:
            return subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()
    cache = read('xbmc/filesystem/FileCache.cpp')
    strategies = read('xbmc/filesystem/CacheStrategy.cpp')
    player = read('xbmc/cores/VideoPlayer/VideoPlayer.cpp')
    if mutant == 'suppress-warning':
        cache = cache.replace('m_writeRateLowSpeed = rate;', 'm_writeRateLowSpeed = 0;')
    elif mutant == 'remove-buffering-bias':
        cache = cache.replace('average.Rate(m_writePos, 1000)', 'average.Rate(m_writePos)')
    elif mutant == 'reset-cached-average':
        cache = cache.replace('average.Reset(m_writePos, bCompleteReset)', 'average.Reset(m_writePos)')
    elif mutant == 'keep-latch':
        cache = cache.replace('m_writeRateLowSpeed = 0; // Reset low speed condition', '// keep latch')
    parts = {
        'STRATEGIES': strategies[strategies.index('CCacheStrategy::~CCacheStrategy()'):strategies.index('CSimpleFileCache::CSimpleFileCache()')] + strategies[strategies.index('CDoubleCache::CDoubleCache('):],
        'RATE': block(cache, 'class CWriteRate'),
        'PROCESS': block(cache, 'void CFileCache::Process()'),
        'IOCONTROL': block(cache, 'int CFileCache::IoControl('),
        'FULL': block(block(player, 'void CVideoPlayer::HandlePlaySpeed()'), 'if (m_caching == CACHESTATE_FULL)'),
        'CACHING': block(player, 'CacheInfo CVideoPlayer::GetCachingTimes()'),
        'SETCACHING': block(player, 'void CVideoPlayer::SetCaching('),
        'SYNC': block(player, 'void CVideoPlayer::SynchronizeStreams('),
    }
    result = HARNESS
    for name, source in parts.items():
        result = result.replace('@' + name + '@', source)
    return result.replace('std::chrono::steady_clock', 'TestClock')


def run(source, observe=False, sanitize=False):
    with tempfile.TemporaryDirectory(prefix='cache-refill-') as directory:
        temp = Path(directory)
        (temp / 'support.h').write_text(SUPPORT)
        for name in ['threads/Event.h', 'threads/CriticalSection.h', 'threads/SystemClock.h', 'utils/log.h']:
            stub = temp / name
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_text('#include "support.h"\n')
        (temp / 'test.cpp').write_text(source)
        command = ['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                   '-I', str(temp), '-I', str(ROOT / 'xbmc')]
        if sanitize:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
        subprocess.run(command + [str(temp / 'test.cpp'), str(ROOT / 'xbmc/filesystem/CircularCache.cpp'), '-o', str(temp / 'test')], check=True)
        return subprocess.run([str(temp / 'test')] + (['observe'] if observe else []), capture_output=True, text=True, timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--observe', action='store_true')
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    result = run(generate(args.baseline), args.observe, args.sanitize)
    print(result.stdout, end='')
    if result.returncode:
        print(result.stderr, end='')
        raise SystemExit(result.returncode)
    if args.negative_controls:
        for mutant in ['suppress-warning', 'remove-buffering-bias', 'reset-cached-average', 'keep-latch']:
            result = run(generate(mutant=mutant), sanitize=args.sanitize)
            assert result.returncode != 0 and 'Assertion' in result.stderr, (mutant, result.stdout, result.stderr)
            print(f'PASS negative control rejected {mutant}')


if __name__ == '__main__':
    main()
