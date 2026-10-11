#!/usr/bin/env python3
"""Exercise production cached reads and player buffering during an input gap.

Runs whole CircularCache read/write/wait, FileCache read/IoControl, CFile read,
DVDInputStreamFile read/observer setter, dvd_file_read, primary ReadPacket and
SetCaching/CheckCacheReadStall. The cache writer/event scheduler, timers, clock,
stream players and demux packet parser are recording host adapters. Virtual
elapsed time reproduces the capture's successful-read gap without pretending to
replay reporter media, FFmpeg's full parser, device output or HTTP delivery.
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


HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <climits>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include "filesystem/IFileTypes.h"
#include "utils/ScopeGuard.h"
using namespace std::chrono_literals;
using namespace XFILE;
using CCriticalSection = std::recursive_mutex;
constexpr int LOGERROR=0,LOGWARNING=1,LOGDEBUG=2,LOGVIDEO=3;
constexpr int CACHE_RC_WOULD_BLOCK=-2,CACHE_RC_TIMEOUT=-3;
constexpr int DVD_PLAYSPEED_NORMAL=1000,DVD_PLAYSPEED_PAUSE=0;
constexpr double DVD_NOPTS_VALUE=-1;
enum ECacheState {CACHESTATE_FULL=1,CACHESTATE_INIT=2,CACHESTATE_PLAY=3,CACHESTATE_DONE=0,CACHESTATE_FLUSH=4};
namespace StreamFlags {constexpr int FLAG_STILL_IMAGES=1;}
struct CLog {template<class... T> static void Log(T&&...) {}}
;
int64_t nowMs=0;
std::function<void()> tick;
int eventWaits=0;
bool fractionalTail=false;
namespace XbmcThreads {
template<class T=std::chrono::milliseconds> struct EndTime {
 int64_t end=0;
 EndTime()=default;
 template<class D> explicit EndTime(D duration){Set(duration);}
 template<class D> void Set(D duration){end=nowMs+std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();}
 bool IsTimePast()const{return nowMs>=end;}
 std::chrono::milliseconds GetTimeLeft()const{return std::chrono::milliseconds(std::max<int64_t>(0,end-nowMs-(fractionalTail?1:0)));}
};
}
struct Event {
 bool signaled=false;
 void Set(){signaled=true;}
 bool Wait(std::chrono::milliseconds time){
  assert(time.count()>0);assert(++eventWaits<1000);
  if(signaled){signaled=false;return true;}
  nowMs+=time.count();if(tick)tick();
  if(signaled){signaled=false;return true;}return false;
 }
};
struct CCacheStrategy {
 virtual ~CCacheStrategy()=default;
 virtual int ReadFromCache(char*,size_t)=0;
 virtual int WriteToCache(const char*,size_t)=0;
 virtual int64_t WaitForData(uint32_t,std::chrono::milliseconds)=0;
 bool m_bEndOfInput=false;
 virtual bool IsEndOfInput(){return m_bEndOfInput;}
};
struct CCircularCache : CCacheStrategy {
 CCriticalSection m_sync;
 int64_t m_beg=0,m_cur=0,m_end=0;
 size_t m_size=256,m_size_back=64;
 char storage[256]{},*m_buf=storage;
 bool ended=false;
 Event m_written,m_space;
 bool IsEndOfInput(){return ended;}
 int ReadFromCache(char*,size_t);
 int WriteToCache(const char*,size_t);
 int64_t WaitForData(uint32_t,std::chrono::milliseconds);
};
@CIRCULAR@
constexpr int CACHE_RC_ERROR=-1;
struct Disk {
 std::vector<char> bytes;size_t pos=0;
 ssize_t Write(const char* data,size_t size){bytes.insert(bytes.end(),data,data+size);return size;}
 ssize_t Read(char* data,size_t size){auto count=std::min(size,bytes.size()-pos);std::memcpy(data,bytes.data()+pos,count);pos+=count;return count;}
};
struct CSimpleFileCache : CCacheStrategy {
 int64_t m_nWritePosition=0,m_nReadPosition=0;
 Disk disk;Disk* m_cacheFileRead=&disk;Disk* m_cacheFileWrite=&disk;
 Event event,m_space;Event* m_hDataAvailEvent=&event;
 std::string m_filename="test";
 int64_t GetAvailableRead();
 int WriteToCache(const char*,size_t)override;
 int ReadFromCache(char*,size_t)override;
 int64_t WaitForData(uint32_t,std::chrono::milliseconds)override;
};
struct CDoubleCache : CCacheStrategy {
 std::shared_ptr<CCacheStrategy> owner;CCacheStrategy* m_pCache;
 explicit CDoubleCache(std::shared_ptr<CCacheStrategy> cache):owner(cache),m_pCache(cache.get()){}
 int WriteToCache(const char*,size_t)override;
 int ReadFromCache(char*,size_t)override;
 int64_t WaitForData(uint32_t,std::chrono::milliseconds)override;
};
@STRATEGIES@
struct CFileCache {
 CCriticalSection m_sync;
 std::shared_ptr<CCircularCache> ring=std::make_shared<CCircularCache>();
 std::shared_ptr<CCacheStrategy> m_pCache=ring;
 std::string m_sourcePath="test";
 int64_t m_readPos=0,m_maxForward=256;
 int m_seekPossible=1;
 uint32_t m_writeRate=0,m_writeRateActual=0,m_writeRateLowSpeed=0;
 std::chrono::milliseconds m_processWait{100ms};
 std::function<void()> m_readWaitCallback;
 ssize_t Read(void*,size_t);
 int IoControl(EIoControl,void*);
 int64_t GetLength(){return 4096;}
 int64_t Seek(int64_t,int){return m_readPos;}
};
@CACHE@
#define XBMCCOMMONS_HANDLE_UNCHECKED
struct Stats {void AddSampleBytes(ssize_t){}}
;
struct Buffer {
 std::streamsize in_avail(){assert(false);return 0;}
 std::streamsize sgetn(char*,std::streamsize){assert(false);return 0;}
};
struct CFile {
 CFileCache* m_pFile=nullptr;
 std::unique_ptr<Buffer> m_pBuffer;
 unsigned m_flags=READ_TRUNCATED;
 std::unique_ptr<Stats> m_bitStreamStats;
 ssize_t Read(void*,size_t);
 int IoControl(EIoControl,void*);
};
@FILE@
struct CDVDInputStream {
 virtual ~CDVDInputStream()=default;
 virtual int Read(uint8_t*,int)=0;
 virtual void SetReadWaitCallback(const std::function<void()>&){}
 virtual bool IsRealtime(){return realtime;}
 bool realtime=false;
};
struct CDVDInputStreamFile : CDVDInputStream {
 CFile* m_pFile=nullptr;bool m_eof=false;
 int Read(uint8_t*,int)override;
 void SetReadWaitCallback(const std::function<void()>&)override;
};
@INPUT@
constexpr int AVERROR_EOF=-541478725,AVERROR_EXIT=-1414092869;
struct CDVDDemuxFFmpeg {
 std::shared_ptr<CDVDInputStream> m_pInput;
 bool m_brokenFileDetected=false;
 int64_t m_sourceReadBytes=0;
 void RecordSeamRead(int){}
};
int interrupt_cb(void*){return 0;}
@READ_CALLBACK@
struct DemuxPacket {int iStreamId=-1,demuxerId=0;};
struct CDemuxStream {int source=1;};
constexpr int STREAM_SOURCE_NONE=0,STREAM_NONE=0,STREAM_SOURCE_DEMUX=1,STREAM_SOURCE_DEMUX_SUB=2;
constexpr int DMX_SPECIALID_STREAMCHANGE=-2;
struct Demux {
 CDVDDemuxFFmpeg io;
 std::function<void()> afterRead;
 bool throwAfterRead=false;
 int result=0,reads=0;uint8_t bytes[4]{};
 DemuxPacket* Read(){++reads;result=dvd_file_read(&io,bytes,4);if(afterRead)afterRead();if(throwAfterRead)throw std::runtime_error("parser failure");return result>0?new DemuxPacket:nullptr;}
 CDemuxStream* GetStream(int,int){return nullptr;}
 void GetPrograms(std::vector<int>&){}
};
struct StreamPlayer {
 bool stalled=true,accepts=true;
 int level=0,speed=1000,changes=0;
 bool IsStalled(){return stalled;}
 int GetLevel(){return level;}
 bool AcceptsData(){return accepts;}
 void SetSpeed(int value){speed=value;++changes;}
};
struct Clock {
 int speed=1000;double value=5454641000.0;int64_t last=0,pauseAt=-1;
 double GetClock(){value+=(nowMs-last)*speed;last=nowMs;return value;}
 void SetSpeed(int value){GetClock();speed=value;if(value==0)pauseAt=nowMs;}
 void SetSpeedAdjust(double){}
};
struct Current {int id=1;bool inited=true;struct {int flags=0;}hint;double starttime=5454741000.0;};
struct Selection {template<class...T>void Clear(T...){}template<class...T>void Update(T...){}}
;
struct CVideoPlayer {
 std::shared_ptr<CDVDInputStream> m_pInputStream;
 std::unique_ptr<Demux> m_pDemuxer,m_pSubtitleDemuxer;
 std::unique_ptr<StreamPlayer> m_VideoPlayerAudio=std::make_unique<StreamPlayer>(),m_VideoPlayerVideo=std::make_unique<StreamPlayer>(),m_VideoPlayerSubtitle=std::make_unique<StreamPlayer>();
 Current m_CurrentAudio,m_CurrentVideo;
 bool m_bAbortRequest=false,m_bStop=false,m_displayLost=false,pending=false,menu=false;
 ECacheState m_caching=CACHESTATE_DONE;
 int m_playSpeed=1000,m_streamPlayerSpeed=1000;
 XbmcThreads::EndTime<> m_syncTimer,m_cachingTimer;
 Clock m_clock;
 Selection m_SelectionStreams;
 std::vector<int> m_programs;
 double m_offset_pts=0;bool m_HasVideo=true,m_HasAudio=true;
 bool ParentLifecyclePending(){return pending;}
 bool IsInMenuInternal(){return menu;}
 void UpdateCorrection(DemuxPacket*,double){}
 void UpdateContent(){}void OpenDefaultStreams(bool){}
 struct CacheInfo {bool valid=false;};
 CacheInfo GetCachingTimes(){assert(false);return {};}
 void CheckCacheReadStall();
 void SetCaching(ECacheState);
 bool ReadPacket(DemuxPacket*&,CDemuxStream*&);
};
@PLAYER@
struct Fixture {
 CFileCache cache;CFile file;std::shared_ptr<CDVDInputStreamFile> input=std::make_shared<CDVDInputStreamFile>();CVideoPlayer player;
 Fixture(){nowMs=0;eventWaits=0;fractionalTail=false;tick={};file.m_pFile=&cache;input->m_pFile=&file;player.m_pInputStream=input;player.m_pDemuxer=std::make_unique<Demux>();player.m_pDemuxer->io.m_pInput=input;}
 ~Fixture(){tick={};}
 void DeliverAfter(int64_t at,const std::string& bytes="DATA"){
  tick=[this,at,bytes,delivered=false]()mutable{if(!delivered&&nowMs>=at){assert(cache.m_pCache->WriteToCache(bytes.data(),bytes.size())==int(bytes.size()));delivered=true;}};
 }
 bool Read(){DemuxPacket* packet=nullptr;CDemuxStream* stream=nullptr;bool result=player.ReadPacket(packet,stream);delete packet;return result;}
};
void waitSequence(bool observe){
 Fixture f;f.player.m_syncTimer.Set(3000ms);
 f.player.m_VideoPlayerAudio->stalled=false;f.player.m_VideoPlayerVideo->stalled=false;
 // Capture: resync at29.221, local video still at30.101, audio stall at32.266,
 // first successful demux progress at36.800. Cache delivery gap is synthetic.
 f.DeliverAfter(7579);
 auto delivery=tick;
 tick=[&]{f.player.m_VideoPlayerVideo->stalled=nowMs>=880;f.player.m_VideoPlayerAudio->stalled=nowMs>=3045;delivery();};
 assert(f.Read());assert(f.player.m_pDemuxer->result==4);
 assert(std::memcmp(f.player.m_pDemuxer->bytes,"DATA",4)==0);
 assert(f.player.m_pDemuxer->io.m_sourceReadBytes==4&&!f.input->m_eof);
 assert(nowMs>=7579&&nowMs<=7629);
 const double advance=(f.player.m_clock.GetClock()-5454641000.0)/1000.0;
 if(observe){std::cout<<"OBSERVED blocked-read clock advance="<<advance<<"ms caching="<<f.player.m_caching<<"\n";return;}
 assert(f.player.m_caching==CACHESTATE_FULL);
 assert(f.player.m_clock.pauseAt>=3000&&f.player.m_clock.pauseAt<=3100);
 assert(advance<=3100);assert(f.player.m_VideoPlayerAudio->speed==0&&f.player.m_VideoPlayerVideo->speed==0);
 assert(!f.cache.m_readWaitCallback); // guard also covers all post-read early returns
 f.player.SetCaching(CACHESTATE_INIT);f.player.SetCaching(CACHESTATE_PLAY);f.player.SetCaching(CACHESTATE_DONE);
 nowMs+=1000;assert(f.player.m_clock.GetClock()==5454641000.0+(advance+1000)*1000.0);
 std::cout<<"PASS successful gap pauses existing clock at "<<advance<<"ms, bytes/position preserved and resumes\n";
}
void timeoutSequence(bool observe){
 Fixture f;
 DemuxPacket* packet=nullptr;CDemuxStream* stream=nullptr;
 assert(!f.player.ReadPacket(packet,stream));assert(packet==nullptr);
 if(observe){std::cout<<"OBSERVED empty-active read result="<<f.player.m_pDemuxer->result<<" eof="<<f.input->m_eof<<" time="<<nowMs<<"\n";return;}
 assert(nowMs==10000);assert(f.player.m_pDemuxer->result==-1);assert(!f.input->m_eof);
 assert(!f.cache.m_readWaitCallback);
 // An observer consumes time from the original deadline, never a new10s wait.
 Fixture costly;costly.input->SetReadWaitCallback([]{nowMs+=5000;});
 uint8_t bytes[4];assert(costly.input->Read(bytes,4)==-1);
 assert(nowMs==10200&&eventWaits==4&&!costly.input->m_eof);
 // Submillisecond deadline rounding must not turn an active wait into EOF.
 Fixture fractional;fractionalTail=true;
 fractional.input->SetReadWaitCallback([]{nowMs=9999;});
 assert(fractional.input->Read(bytes,4)==-1&&nowMs==9999&&!fractional.input->m_eof);
 // Immediate true EOF is retained and never triggers a wait callback.
 Fixture eof;eof.cache.ring->ended=true;assert(!eof.Read());
 assert(nowMs==0&&eof.input->m_eof&&eof.player.m_pDemuxer->result==AVERROR_EOF);
 assert(eof.player.m_caching==CACHESTATE_DONE&&!eof.cache.m_readWaitCallback);
 // Status probes and available partial bytes must remain ordinary nonnegative results.
 Fixture partial;assert(partial.cache.m_pCache->WaitForData(1,0ms)==0);
 partial.cache.m_pCache->WriteToCache("AB",2);
 assert(partial.cache.m_pCache->WaitForData(4,100ms)==2);
 assert(partial.Read());assert(partial.player.m_pDemuxer->result==2&&!partial.input->m_eof);
 std::cout<<"PASS bounded timeout distinct from true EOF, zero-time probes and partial availability\n";
}
void guardsAndCleanup(){
 for(int mode=0;mode<15;++mode){
  Fixture f;f.DeliverAfter(300);auto& p=f.player;
  switch(mode){
   case 0:p.m_playSpeed=0;break;case 1:p.m_playSpeed=-1000;break;
   case 2:f.input->realtime=true;break;case 3:p.menu=true;break;
   case 4:p.m_CurrentVideo.hint.flags=StreamFlags::FLAG_STILL_IMAGES;break;
   case 5:p.m_bStop=true;break;case 6:p.m_bAbortRequest=true;break;
   case 7:p.pending=true;break;case 8:p.m_displayLost=true;break;
   case 9:p.m_caching=CACHESTATE_INIT;break;
   case 10:p.m_CurrentAudio.inited=p.m_CurrentVideo.inited=false;break;
   case 11:p.m_VideoPlayerAudio->stalled=p.m_VideoPlayerVideo->stalled=false;break;
   case 12:p.m_syncTimer.Set(500ms);break;
   case 13:p.m_VideoPlayerAudio->level=p.m_VideoPlayerVideo->level=21;break;
   case 14:p.m_caching=CACHESTATE_FULL;break;
  }
  auto before=p.m_caching;assert(f.Read());assert(p.m_caching==before);assert(p.m_clock.pauseAt==-1);assert(!f.cache.m_readWaitCallback);
 }
 // Real CFile zero-byte probes never wait or invoke an installed observer.
 Fixture zero;int observations=0;
 zero.input->SetReadWaitCallback([&]{++observations;zero.player.CheckCacheReadStall();});
 assert(zero.file.Read(nullptr,0)==0);assert(nowMs==0&&eventWaits==0&&observations==0);
 assert(zero.player.m_caching==CACHESTATE_DONE&&zero.player.m_clock.pauseAt==-1);
 zero.input->SetReadWaitCallback({});
 // A non-player reader remains blocking, without an observer or any clock effect.
 Fixture plain;plain.DeliverAfter(7500);uint8_t bytes[4];assert(plain.input->Read(bytes,4)==4);assert(nowMs==7500&&plain.player.m_clock.pauseAt==-1);
 // Callback failure cannot become a media read error or EOF; continue same read.
 Fixture failed;failed.DeliverAfter(300);
 failed.input->SetReadWaitCallback([]{throw std::runtime_error("observer failure");});
 assert(failed.input->Read(bytes,4)==4);assert(!failed.input->m_eof);assert(!failed.cache.m_readWaitCallback);
 // Parser exceptions still remove the foreground observer during unwind.
 Fixture throwing;throwing.DeliverAfter(300);throwing.player.m_pDemuxer->throwAfterRead=true;
 bool caught=false;try{throwing.Read();}catch(const std::runtime_error&){caught=true;}
 assert(caught&&!throwing.cache.m_readWaitCallback);
 // The old input must be cleared before a replacement and its ordinary reads.
 Fixture replaced;replaced.DeliverAfter(300);assert(replaced.Read());auto old=replaced.input;
 assert(!replaced.cache.m_readWaitCallback);replaced.player.m_pInputStream.reset();
 replaced.player.m_caching=CACHESTATE_DONE;replaced.player.m_clock.pauseAt=-1;
 replaced.DeliverAfter(nowMs+300);assert(old->Read(bytes,4)==4);
 assert(replaced.player.m_clock.pauseAt==-1&&!replaced.cache.m_readWaitCallback);
 // Retain/clear the original input even if a demux substitutes its owner before returning.
 Fixture during;during.DeliverAfter(300);
 auto newInput=std::make_shared<CDVDInputStreamFile>();
 during.player.m_pDemuxer->afterRead=[&]{during.player.m_pInputStream=newInput;};
 assert(during.Read());assert(during.player.m_pInputStream==newInput);
 assert(!during.cache.m_readWaitCallback);
 // Stop/replacement queued during the wait never acquires new buffering work.
 Fixture stopping;stopping.player.m_syncTimer.Set(3000ms);stopping.DeliverAfter(7500);
 auto stopDelivery=tick;
 tick=[&]{if(nowMs>=1000)stopping.player.m_bAbortRequest=true;stopDelivery();};
 assert(stopping.Read());assert(stopping.player.m_caching==CACHESTATE_DONE&&!stopping.cache.m_readWaitCallback);
 // External subtitle demux input never borrows the primary observer.
 Fixture primary;Fixture subtitles;subtitles.DeliverAfter(300);
 primary.player.m_pSubtitleDemuxer=std::move(subtitles.player.m_pDemuxer);
 assert(primary.Read());assert(primary.player.m_pDemuxer->reads==0);
 assert(primary.player.m_clock.pauseAt==-1&&!primary.cache.m_readWaitCallback&&!subtitles.cache.m_readWaitCallback);
 // Source EOF published during a blocked read stays terminal and clears the observer.
 Fixture endDuring;tick=[&]{if(nowMs>=300)endDuring.cache.ring->ended=true;};
 assert(!endDuring.Read());assert(nowMs==300&&endDuring.input->m_eof);
 assert(endDuring.player.m_pDemuxer->result==AVERROR_EOF&&!endDuring.cache.m_readWaitCallback);
 // Filled reads never register a lasting observer or pause playback.
 Fixture immediate;immediate.cache.m_pCache->WriteToCache("DATA",4);assert(immediate.Read());assert(nowMs==0&&immediate.player.m_clock.pauseAt==-1&&!immediate.cache.m_readWaitCallback);
 std::cout<<"PASS paused/reverse/realtime/menu/still/startup/lifecycle guards, ordinary readers and error/replacement cleanup\n";
}
void cacheStrategies(){
 for(int mode=0;mode<3;++mode){
  Fixture f;
  auto simple=std::make_shared<CSimpleFileCache>();
  if(mode==0)f.cache.m_pCache=simple;
  if(mode==1)f.cache.m_pCache=std::make_shared<CDoubleCache>(f.cache.ring);
  if(mode==2)f.cache.m_pCache=std::make_shared<CDoubleCache>(simple);
  int observations=0;
  f.input->SetReadWaitCallback([&]{++observations;f.player.CheckCacheReadStall();});
  assert(f.file.Read(nullptr,0)==0&&nowMs==0&&eventWaits==0&&observations==0);
  assert(f.player.m_clock.pauseAt==-1&&f.player.m_caching==CACHESTATE_DONE);
  f.input->SetReadWaitCallback({});
  f.player.m_syncTimer.Set(3000ms);f.DeliverAfter(7579);
  assert(f.Read());assert(f.player.m_clock.pauseAt==3000);
  assert(f.player.m_pDemuxer->result==4&&!f.input->m_eof&&!f.cache.m_readWaitCallback);
  tick={};nowMs=0;eventWaits=0;f.player.m_caching=CACHESTATE_DONE;
  assert(!f.Read());assert(nowMs==10000&&f.player.m_pDemuxer->result==-1&&!f.input->m_eof);
 }
 std::cout<<"PASS actual simple and double-cache read/write/wait paths preserve successful delivery and timeout contract\n";
}
int main(int argc,char**argv){
 const std::string scenario=argc>1?argv[1]:"all";const bool observe=argc>2;
 if(scenario=="all"||scenario=="wait")waitSequence(observe);
 if(scenario=="all"||scenario=="timeout")timeoutSequence(observe);
 if(scenario=="all"&&!observe){guardsAndCleanup();cacheStrategies();}
}
'''


def generate(revision=None, mutant=None):
    def read(path):
        if revision:
            return subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()
    circular = read('xbmc/filesystem/CircularCache.cpp')
    cache = read('xbmc/filesystem/FileCache.cpp')
    file = read('xbmc/filesystem/File.cpp')
    strategies = read('xbmc/filesystem/CacheStrategy.cpp')
    inp = read('xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamFile.cpp')
    player = read('xbmc/cores/VideoPlayer/VideoPlayer.cpp')
    demux = read('xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp')
    if mutant == 'no-observer':
        player = player.replace('input->SetReadWaitCallback([this] { CheckCacheReadStall(); });', '(void)input;')
    if mutant == 'no-clock-pause':
        player = player.replace('m_clock.SetSpeed(DVD_PLAYSPEED_PAUSE);', '(void)m_clock;')
    if mutant == 'false-eof':
        circular = circular.replace('return CACHE_RC_TIMEOUT;', 'return 0;')
    if mutant == 'leaked-observer':
        player = player.replace('stream->SetReadWaitCallback({});', '(void)stream;')
    if mutant == 'unbounded-deadline':
        cache = cache.replace('iRc = m_pCache->WaitForData(1, wait);', 'deadline.Set(10s); iRc = m_pCache->WaitForData(1, wait);')
    if mutant == 'zero-read-wait':
        cache = cache.replace('  if (uiBufSize == 0)\n    return 0;\n', '')
    if mutant == 'observer-error':
        cache = cache.replace('m_readWaitCallback = {};\n          CLog::Log', 'throw;\n          CLog::Log')
    setter = (block(inp, 'void CDVDInputStreamFile::SetReadWaitCallback(')
              if 'void CDVDInputStreamFile::SetReadWaitCallback(' in inp else
              'void CDVDInputStreamFile::SetReadWaitCallback(const std::function<void()>&) {}')
    callback = (block(player, 'void CVideoPlayer::CheckCacheReadStall(')
                if 'void CVideoPlayer::CheckCacheReadStall(' in player else
                'void CVideoPlayer::CheckCacheReadStall() {}')
    sources = {
        'CIRCULAR': '\n'.join(block(circular, marker) for marker in
                             ['int CCircularCache::ReadFromCache(', 'int CCircularCache::WriteToCache(', 'int64_t CCircularCache::WaitForData(']),
        'STRATEGIES': '\n'.join(block(strategies, marker) for marker in [
            'int64_t CSimpleFileCache::GetAvailableRead(', 'int CSimpleFileCache::ReadFromCache(',
            'int CSimpleFileCache::WriteToCache(', 'int64_t CSimpleFileCache::WaitForData(',
            'int CDoubleCache::WriteToCache(', 'int CDoubleCache::ReadFromCache(',
            'int64_t CDoubleCache::WaitForData(']),
        'CACHE': '\n'.join(block(cache, marker) for marker in
                          ['ssize_t CFileCache::Read(', 'int CFileCache::IoControl(']),
        'FILE': '\n'.join(block(file, marker) for marker in ['ssize_t CFile::Read(', 'int CFile::IoControl(']),
        'INPUT': block(inp, 'int CDVDInputStreamFile::Read(') + '\n' + setter,
        'READ_CALLBACK': block(demux, 'static int dvd_file_read('),
        'PLAYER': callback + '\n' + '\n'.join(block(player, marker) for marker in
                                             ['void CVideoPlayer::SetCaching(', 'bool CVideoPlayer::ReadPacket(']),
    }
    result = HARNESS
    for key, source in sources.items():
        result = result.replace('@' + key + '@', source)
    return result


def run(source, scenario, observe=False, sanitize=False):
    with tempfile.TemporaryDirectory(prefix='cache-read-stall-') as temp:
        temp = Path(temp)
        (temp / 'test.cpp').write_text(source)
        args = ['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g', '-I', str(ROOT / 'xbmc')]
        if sanitize:
            args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        built = subprocess.run(args + [str(temp / 'test.cpp'), '-o', str(temp / 'test')], capture_output=True, text=True)
        if built.returncode:
            raise RuntimeError(built.stderr)
        return subprocess.run([str(temp / 'test'), scenario] + (['observe'] if observe else []), capture_output=True, text=True, timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--observe-original', action='store_true')
    parser.add_argument('--scenario', choices=['all', 'wait', 'timeout'], default='all')
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    result = run(generate(args.baseline), args.scenario, args.observe_original, args.sanitize)
    print(result.stdout, end='')
    if result.returncode:
        print(result.stderr, end='')
        raise SystemExit(result.returncode)
    if args.negative_controls:
        for mutant, scenario in [('no-observer', 'wait'), ('no-clock-pause', 'wait'),
                                 ('false-eof', 'timeout'), ('leaked-observer', 'wait'),
                                 ('unbounded-deadline', 'timeout'), ('observer-error', 'all'), ('zero-read-wait', 'all')]:
            result = run(generate(mutant=mutant), scenario, sanitize=args.sanitize)
            assert result.returncode != 0, f'undetected mutation: {mutant}'
            assert 'Assertion' in result.stderr or 'observer failure' in result.stderr, result.stderr
            print(f'PASS negative control detected {mutant}')


if __name__ == '__main__':
    main()
