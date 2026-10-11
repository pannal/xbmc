#!/usr/bin/env python3
"""Source-extracted AVI admission/player regression with packaged FFmpeg seek code.

Requires --ffmpeg-source pointing at the actual CE FFmpeg source. Executes whole
Kodi SeekTime, its index preflight, PLAYER_SEEK body, SetCaching and FinishSeek,
plus FFmpeg index/AVI/core/generic seek functions. AVIO, parser flush effects,
packet reads, input and downstream players are recording host adapters. This is
not a complete AVI decode or device test. Sanitizers retain leak detection.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lifecycle', ROOT / 'tools/test-player-lifecycle.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <inttypes.h>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
#define FFMAX std::max
#define FFMIN std::min
#define av_assert0 assert
#define CONFIG_DV_DEMUXER 0
#define AVERROR(x) (-(x))
#define AVERROR_INVALIDDATA (-1094995529)
#define AVERROR_EOF (-541478725)
#define AVERROR_EXIT (-1414092869)
#define AV_NOPTS_VALUE INT64_MIN
constexpr int AVSEEK_FLAG_BACKWARD=1,AVSEEK_FLAG_BYTE=2,AVSEEK_FLAG_ANY=4;
constexpr int AVINDEX_KEYFRAME=1,AVINDEX_DISCARD_FRAME=2,AV_PKT_FLAG_KEY=1;
constexpr int AVMEDIA_TYPE_VIDEO=0,AVMEDIA_TYPE_AUDIO=1,AV_CODEC_ID_CDGRAPHICS=1,AV_CODEC_ID_DVVIDEO=2;
constexpr int AVFMT_NO_BYTE_SEEK=0x8000,AVFMT_NOBINSEARCH=0x2000,AVFMT_NOGENSEARCH=0x4000;
constexpr int AVFMT_NOTIMESTAMPS=0x80,AV_TIME_BASE=1000000,AV_LOG_DEBUG=48,AV_LOG_TRACE=56,AV_LOG_ERROR=16;
constexpr int LOGDEBUG=0,LOGERROR=1,LOGWARNING=2,LOGVIDEO=3,TOAST_DISPLAY_TIME=10;
constexpr int DVDSTREAM_TYPE_DVD=1,DVDSTREAM_TYPE_BLURAY=2,DVDSTREAM_TYPE_FFMPEG=3,SEEK_POSSIBLE=0x10;
constexpr int DVD_PLAYSPEED_NORMAL=1000,DVD_PLAYSPEED_PAUSE=0,DVDSTATE_SEEK=1;
constexpr double DVD_NOPTS_VALUE=-1e30,DVD_TIME_BASE=1000000;
#define DVD_MSEC_TO_TIME(x) ((x)*1000.0)
#define DVD_TIME_TO_MSEC(x) ((x)/1000.0)
struct AVRational{int num,den;};
struct AVIndexEntry{int64_t pos,timestamp;int flags;};
struct AVPacket{int stream_index=0;int64_t dts=0;int flags=0;int owned=1;};
struct AVIOContext{int64_t pos=95000;};
struct AVFormatContext;
struct AVIStream{int sample_size=0,scale=1,rate=25,packet_size=42,remaining=42;
 int64_t seek_pos=123,frame_offset=2375;AVFormatContext* sub_ctx=nullptr;};
struct AVIContext{void* dv_demux=nullptr;int index_loaded=1,stream_index=0,dts_max=0,non_interleaved=0;};
struct AVCodecParameters{int codec_type=AVMEDIA_TYPE_VIDEO,codec_id=0;};
struct FFStream{AVIndexEntry* index_entries;int nb_index_entries;};
struct AVStream{AVIStream* priv_data;AVRational time_base{1,25};AVCodecParameters* codecpar;FFStream internal;};
struct FFInputFormat{int flags=0;int (*read_seek)(AVFormatContext*,int,int64_t,int)=nullptr;
 bool read_timestamp=false,read_seek2=false;const char* name="avi";};
struct FFFormatContext{AVPacket* pkt;int64_t data_offset=0;};
struct AVFormatContext{AVIContext* priv_data;AVStream** streams;unsigned nb_streams=1;
 AVIOContext* pb;FFInputFormat* iformat;FFFormatContext internal;int io_repositioned=0;
 int64_t start_time=0,duration=120000000;};
FFStream* ffstream(AVStream* s){return &s->internal;}
FFInputFormat* ffifmt(FFInputFormat* f){return f;}
FFFormatContext* ffformatcontext(AVFormatContext* s){return &s->internal;}
int ioSeeks=0,frameReads=0,ffFlushes=0,unrefs=0,defaultStream=0;
bool rejectIO=false,repairIndex=false;
int64_t avio_seek(AVIOContext* pb,int64_t p,int){++ioSeeks;if(rejectIO)return AVERROR(EIO);return pb->pos=p;}
// FFmpeg av_rescale uses nearest rounding, with halfway values away from zero.
int64_t av_rescale(int64_t a,int64_t b,int64_t c){return std::llround(static_cast<long double>(a)*b/c);}
int64_t av_rescale_q(int64_t a,AVRational b,AVRational c){return av_rescale(a,int64_t(b.num)*c.den,int64_t(b.den)*c.num);}
void av_log(AVFormatContext*,int,const char*,...){}
void av_packet_unref(AVPacket* p){++unrefs;p->owned=0;}
void seek_subtitle(AVStream*,AVStream*,int64_t){assert(false);}
void ff_dv_ts_reset(void*,int64_t){assert(false);}
int avi_load_index(AVFormatContext*){assert(false);return -1;}
void avpriv_update_cur_dts(AVFormatContext*,AVStream*,int64_t){}
// Library packet/parser flush effects are recorded; AVI offset changes execute real code.
void ff_read_frame_flush(AVFormatContext*){++ffFlushes;}
int av_read_frame(AVFormatContext* ctx,AVPacket* p){
 ++frameReads;
 if(repairIndex){auto& i=ctx->streams[defaultStream]->internal;
   i.index_entries[0].flags=AVINDEX_KEYFRAME;repairIndex=false;
   p->stream_index=defaultStream;p->dts=9999;p->flags=AV_PKT_FLAG_KEY;return 0;}
 return AVERROR_EOF; // Deliberate bounded source; not reporter bytes.
}
int av_find_default_stream_index(AVFormatContext* ctx){return ctx->nb_streams?defaultStream:-1;}
int avformat_index_get_entries_count(const AVStream* st){return st->internal.nb_index_entries;}
int seek_frame_byte(AVFormatContext*,int,int64_t,int){assert(false);return -1;}
int ff_seek_frame_binary(AVFormatContext*,int,int64_t,int){assert(false);return -1;}
int avformat_seek_file(AVFormatContext*,int,int64_t,int64_t,int64_t,int){assert(false);return -1;}
int avformat_queue_attached_pictures(AVFormatContext*){return 0;}
struct CLog{template<class...T>static void Log(T&&...) {}};
namespace XbmcThreads {template<class T=void>struct EndTime{
 bool expired=false;EndTime()=default;template<class D>explicit EndTime(D){}
 template<class D>void Set(D){}void SetInfinite(){}bool IsTimePast()const{return expired;}
};}
namespace KODI::TIME{template<class D>void Sleep(D){}}
struct Settings{bool enabled=false;bool GetBool(int){return enabled;}};
struct SettingsComponent{Settings settings;Settings* GetSettings(){return &settings;}};
struct CServiceBroker{static SettingsComponent* GetSettingsComponent(){static SettingsComponent s;return &s;}};
struct CSettings{static constexpr int SETTING_COREELEC_VIDEOPLAYER_DETECT_BROKEN_FILES=0;};
struct Localize{std::string Get(int){return {};}} g_localizeStrings;
struct CGUIDialogKaiToast{enum{Warning};template<class...T>static void QueueNotification(T...){}};
struct CDVDInputStream{
 struct IPosTime{bool works=false;int attempts=0;bool PosTime(int){++attempts;return works;}};
 struct IMenus{virtual ~IMenus()=default;virtual bool IsTimeSearchAllowed(){return true;}};
 virtual ~CDVDInputStream()=default;
 IPosTime* posTime=nullptr;int type=0,queries=0;bool seekable=true,eof=false,realtime=false,closed=false;
 std::function<void()> onPosQuery;
 IPosTime* GetIPosTime(){if(onPosQuery)onPosQuery();return posTime;}
 bool IsStreamType(int t)const{return type==t;}bool IsEOF(){return eof;}
 bool IsRealtime(){return realtime;}void Close(){closed=true;}
 int Seek(int64_t,int){++queries;return seekable;}
};
struct DemuxPacket{};
struct CDVDDemuxUtils{static void FreeDemuxPacket(DemuxPacket* p){delete p;}};
struct SSIF{int flushes=0;void Flush(){++flushes;}};
struct CDVDDemux{
 virtual ~CDVDDemux()=default;
 virtual bool SeekTime(double,bool,double* = nullptr)=0;
 @BASE_OUTCOME@
};
struct CDVDDemuxFFmpeg:CDVDDemux{
 AVFormatContext* m_pFormatContext=nullptr;std::shared_ptr<CDVDInputStream> m_pInput;
 struct{AVPacket pkt;int result=0;}m_pkt;
 bool m_seekRejectedWithoutChange=false,m_bAVI=true,m_bSup=false,m_checkTransportStream=false;
 bool m_brokenFileDetected=false,m_seekToKeyFrame=false,aborted=false;
 int m_seekStream=-1;double m_startTime=0,m_currentPts=94776000;
 int64_t m_sourceReadBytes=0;static constexpr int64_t BROKEN_SOURCE_MIN_SCAN_BYTES=16*1024*1024;
 CCriticalSection m_critSection;XbmcThreads::EndTime<> m_timeout;SSIF* m_pSSIF=nullptr;
 bool Aborted(){return aborted;}bool IsTransportStreamReady(){return true;}
 void Flush(){++ffFlushes;m_seekRejectedWithoutChange=false;}
 DemuxPacket* Read(){return ReadInternal(false);}
 DemuxPacket* ReadInternal(bool){m_currentPts=95200000;m_pkt.result=0;m_pkt.pkt.owned=1;return new DemuxPacket;}
 bool SeekTime(double,bool,double*)override;
 @OUTCOME@
 @PREFLIGHT_DECL@
};
@PREFLIGHT@
@SEEK_TIME@
struct Fixture{
 AVIStream ast;AVIContext avi;AVCodecParameters cp;AVIOContext io;AVPacket pkt;
 // First frame not indexed as a key; first known usable key is95sec, then105sec.
 AVIndexEntry index[4]={{100,0,0},{5000,125,0},{95000,2375,AVINDEX_KEYFRAME},{105000,2625,AVINDEX_KEYFRAME}};
 AVStream stream{&ast,{1,25},&cp,{index,4}};AVStream* streams[1]={&stream};FFInputFormat format;
 AVFormatContext ctx{&avi,streams,1,&io,&format,{&pkt,0}};
 std::shared_ptr<CDVDInputStream> input=std::make_shared<CDVDInputStream>();CDVDDemuxFFmpeg demux;
 Fixture(){format.read_seek=avi_read_seek;demux.m_pFormatContext=&ctx;demux.m_pInput=input;
   ioSeeks=frameReads=ffFlushes=unrefs=0;defaultStream=0;rejectIO=repairIndex=false;}
};
struct IDVDStreamPlayer{enum{SYNC_INSYNC=2};};
@RECOVERY_HEADER@
struct Msg{
 double target=50;bool backwards=true,trick=false,accurate=true,sync=true,relative=false,recovery=false,restore=false;
 bool videoRecovery=false;bool IsVideoRecovery()const{return videoRecovery;}
 double GetTime()const{return target;}bool GetBackward()const{return backwards;}
 bool GetTrickPlay()const{return trick;}bool GetAccurate()const{return accurate;}
 bool GetSync()const{return sync;}bool GetRelative()const{return relative;}
 bool GetRecovery()const{return recovery;}bool GetRestore()const{return restore;}
};
struct ProcessInfo{int finishes=0;bool seeking=true;void SeekFinished(int){++finishes;}
 void SetStateSeeking(bool b){seeking=b;}};
struct Clock{
 double value=17000,absolute=6000000;int speed=1000;double adjust=0.1;
 double GetClock()const{return value;}double GetAbsoluteClock()const{return absolute;}
 void SetSpeed(int s){speed=s;}void SetSpeedAdjust(double a){adjust=a;}
};
struct StreamPlayer{int speed=1000;void SetSpeed(int s){speed=s;}};
struct CVideoPlayer{
 @CACHE_ENUM@
 struct CacheInfo{bool valid;};bool cacheValid=true;
 ECacheState m_caching=CACHESTATE_DONE;
 struct{bool canseek=true;double lastSeek=2000000,dts=17000,time_offset=0;}m_State;
 struct{int id=0,syncState=2;double starttime=17000;}m_CurrentVideo;
 Clock m_clock;StreamPlayer audio,video;StreamPlayer* m_VideoPlayerAudio=&audio,*m_VideoPlayerVideo=&video;
 ProcessInfo process;ProcessInfo* m_processInfo=&process;
 int m_playSpeed=1000,m_streamPlayerSpeed=1000;std::atomic<int> m_chapterSeekTarget{3};
 XbmcThreads::EndTime<> m_cachingTimer;
 std::shared_ptr<CDVDInputStream> m_pInputStream;CDVDDemux* m_pDemuxer=nullptr,*m_pSubtitleDemuxer=nullptr;
 double m_demuxSeekBasePts=DVD_NOPTS_VALUE;struct{double GetTimeAfterRestoringCuts(double t){return t;}}m_Edl;
 struct{int state=0;}m_dvd;bool m_subtitleSeekNewRun=false;
 std::vector<double> queued{17000,40000,80000,120000};
 CVideoRecoveryGate m_videoRecoveryGate;
 int flushes=0,recalls=0,speedChanges=0;bool flushAccurate=false,flushSync=false;
 double flushStart=DVD_NOPTS_VALUE;std::function<void()> complete;
 CacheInfo GetCachingTimes(){return {cacheValid};}bool IsInMenuInternal(){return false;}
 void SetCaching(ECacheState);void FinishSeek(bool);void Handle(Msg msg);
 void RecallSubtitlesAfterSeek(double,double){++recalls;}
 void SetPlaySpeed(int s){++speedChanges;m_playSpeed=s;}
 // Downstream flush is a recording deferred adapter; its full lifecycle has its own suite.
 void FlushBuffers(double s,bool a,bool y,std::function<void()> done,bool = false){++flushes;queued.clear();
   flushStart=s;flushAccurate=a;flushSync=y;complete=std::move(done);}
 void Complete(){assert(complete);auto done=std::move(complete);done();}
};
@SET_CACHING@
@FINISH_SEEK@
void CVideoPlayer::Handle(Msg msg){do{@PLAYER_SEEK@}while(false);}
'''

TESTS = r'''
void unchangedDemux(){
 Fixture f;double start=DVD_NOPTS_VALUE;
 assert(!f.demux.SeekTime(50,true,&start));
 assert(f.demux.WasSeekRejectedWithoutChange() && "early indexed-key gap must be rejected unchanged");
 assert(ioSeeks==0&&frameReads==0&&ffFlushes==0&&unrefs==0);
 assert(f.demux.m_pkt.result==0&&f.demux.m_pkt.pkt.owned==1&&f.demux.m_currentPts==94776000);
 assert(f.ast.packet_size==42&&f.ast.remaining==42&&f.ast.frame_offset==2375&&f.ast.seek_pos==123);
 assert(f.io.pos==95000&&!f.input->closed&&start==DVD_NOPTS_VALUE&&!f.demux.m_seekToKeyFrame);
 // Negative values/inexistent input reset the outcome and keep legacy disposition.
 f.demux.m_pInput.reset();assert(!f.demux.SeekTime(50,true,&start));assert(!f.demux.WasSeekRejectedWithoutChange());
}
void unchangedPlayer(){
 for(auto cache:{CVideoPlayer::CACHESTATE_DONE,CVideoPlayer::CACHESTATE_INIT,
                 CVideoPlayer::CACHESTATE_FULL,CVideoPlayer::CACHESTATE_PLAY})
 for(int speed:{DVD_PLAYSPEED_NORMAL,DVD_PLAYSPEED_PAUSE,2000})
 for(bool valid:{true,false}){
  Fixture f;CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;
  p.m_playSpeed=speed;p.m_streamPlayerSpeed=p.audio.speed=p.video.speed=p.m_clock.speed=speed;
  p.cacheValid=valid;p.SetCaching(cache);p.process.finishes=0;
  auto before=p.queued;const int streamSpeed=p.m_streamPlayerSpeed;
  for(int repeat=0;repeat<4;++repeat){
   p.process.seeking=true;p.Handle({});
   assert(p.flushes==0 && "unchanged rejection must preserve queued playback");
   assert(p.queued==before&&p.m_State.dts==17000&&p.m_State.lastSeek==2000000&&p.m_clock.value==17000);
   assert(p.m_caching==cache&&p.m_streamPlayerSpeed==streamSpeed&&p.audio.speed==streamSpeed&&p.video.speed==streamSpeed);
   assert(p.m_playSpeed==speed&&p.speedChanges==0&&!p.m_subtitleSeekNewRun&&p.recalls==0);
   assert(p.m_chapterSeekTarget==3&&!p.complete&&!p.process.seeking&&p.process.finishes==2*(repeat+1));
  }
 }
 // Trickplay does not enter/restore caching; completion only clears seeking.
 Fixture f;CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;
 Msg msg;msg.trick=true;p.Handle(msg);
 assert(p.flushes==0&&p.m_caching==CVideoPlayer::CACHESTATE_DONE&&p.process.finishes==0&&!p.process.seeking);
 // Do not overwrite a newer chapter request published during the seek check.
 f.input->onPosQuery=[&p]{p.m_chapterSeekTarget=8;};p.Handle({});
 assert(p.m_chapterSeekTarget==8&&p.flushes==0);
}
void adjacent(){
 // Healthy forward/return seeks flush once and finish only after the deferred callback.
 for(double target:{5000.0,50.0}){
  Fixture f;f.index[0].flags=AVINDEX_KEYFRAME;f.index[1].flags=AVINDEX_KEYFRAME;
  CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;
  Msg msg;msg.target=target;p.Handle(msg);
  assert(!f.demux.WasSeekRejectedWithoutChange()&&p.flushes==1&&p.queued.empty());
  assert(p.flushAccurate&&p.flushSync&&p.m_State.dts==target*1000&&p.m_State.lastSeek==6000000);
  assert(p.process.seeking&&p.process.finishes==1&&p.recalls==0&&!p.m_subtitleSeekNewRun);
  p.Complete();assert(!p.process.seeking&&p.process.finishes==2&&p.recalls==1&&p.m_subtitleSeekNewRun);
 }
 // Do not claim unchanged after an actual FFmpeg I/O rejection mutates AVI offsets.
 {
  Fixture f;f.index[0].flags=AVINDEX_KEYFRAME;rejectIO=true;
  CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;p.m_playSpeed=2000;
  p.Handle({});assert(!f.demux.WasSeekRejectedWithoutChange()&&f.ast.frame_offset==0&&ioSeeks>0&&ffFlushes>0);
  assert(p.flushes==1&&!p.flushAccurate&&p.flushSync&&p.process.seeking);
  p.Complete();assert(!p.process.seeking&&p.m_playSpeed==DVD_PLAYSPEED_NORMAL&&p.speedChanges==1);
 }
 // EOF failure/success normalization and negative/past-duration seeks retain old paths.
 for(int kind=0;kind<5;++kind){
  Fixture f;CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;
  Msg msg;if(kind==0)f.input->eof=true;
  if(kind==1)msg.target=-10;
  if(kind>=2){msg.target=200000;rejectIO=true;}
  if(kind==3)f.input->realtime=true;if(kind==4)p.m_playSpeed=DVD_PLAYSPEED_PAUSE;
  p.Handle(msg);assert(!f.demux.WasSeekRejectedWithoutChange()&&p.flushes==1);
  assert(kind<2||f.input->closed==(kind!=3));
  p.Complete();assert(!p.process.seeking);
  if(kind==4)assert(p.m_playSpeed==DVD_PLAYSPEED_PAUSE&&p.speedChanges==0);
 }
 // Exclusions and lack of trustworthy/later keys never get an unchanged disposition.
 for(int kind=0;kind<15;++kind){
  Fixture f;SSIF ssif;CDVDInputStream::IPosTime pos;double target=50;bool backwards=true;
  if(kind==0)f.stream.internal.nb_index_entries=0;
  if(kind==1)for(auto& entry:f.index)entry.flags=0;
  if(kind==2)backwards=false;
  if(kind==3)f.cp.codec_type=AVMEDIA_TYPE_AUDIO;
  if(kind==4)f.cp.codec_id=AV_CODEC_ID_DVVIDEO;
  if(kind==5)f.input->posTime=&pos;
  if(kind==6)f.demux.m_pSSIF=&ssif;
  if(kind==7){f.demux.m_checkTransportStream=true;f.demux.m_seekStream=0;}
  if(kind==8)f.input->type=DVDSTREAM_TYPE_DVD;
  if(kind==9)f.input->type=DVDSTREAM_TYPE_BLURAY;
  if(kind==10)f.demux.m_brokenFileDetected=true;
  if(kind==11)f.demux.aborted=true;
  if(kind==12){target=100000;rejectIO=true;}
  if(kind==13)f.demux.m_bAVI=false;
  if(kind==14)f.ctx.start_time=95000000; // Exact start-time offset reaches a usable key.
  f.demux.SeekTime(target,backwards,nullptr);
  assert(!f.demux.WasSeekRejectedWithoutChange());
  if(kind==5)assert(pos.attempts==1);else assert(ffFlushes>0);
  if(kind==6)assert(ssif.flushes==1);
 }
 // Type-IPosTime failure does not require a format context and remains unsafe.
 {Fixture f;CDVDInputStream::IPosTime pos;f.input->posTime=&pos;f.demux.m_pFormatContext=nullptr;
  assert(!f.demux.SeekTime(50,true,nullptr)&&!f.demux.WasSeekRejectedWithoutChange());}
 // Exact default stream and tick rescaling:50ms rounds to1tick at25Hz;20ms rounds to1 at halfway.
 {Fixture f;assert(f.demux.IsAVISeekBeforeFirstKeyframe(50000));
  f.index[0]={100,1,AVINDEX_KEYFRAME};assert(!f.demux.IsAVISeekBeforeFirstKeyframe(20000));
  f.demux.m_seekStream=0;assert(!f.demux.IsAVISeekBeforeFirstKeyframe(50));
  f.demux.m_seekStream=1;assert(!f.demux.IsAVISeekBeforeFirstKeyframe(50));
  f.demux.m_seekStream=-1;defaultStream=-1;assert(!f.demux.IsAVISeekBeforeFirstKeyframe(50));}
 // A repeated rejected request followed by success/unsafe failure cannot inherit the disposition.
 {Fixture f;assert(!f.demux.SeekTime(50,true,nullptr)&&f.demux.WasSeekRejectedWithoutChange());
  f.index[0].flags=AVINDEX_KEYFRAME;assert(f.demux.SeekTime(50,true,nullptr));
  assert(!f.demux.WasSeekRejectedWithoutChange());rejectIO=true;
  assert(!f.demux.SeekTime(50,true,nullptr)&&!f.demux.WasSeekRejectedWithoutChange());}
 // Explicitly document policy tradeoff: the excluded early gap *could* be repaired by generic search.
 {Fixture f;repairIndex=true;assert(av_seek_frame(&f.ctx,-1,50000,AVSEEK_FLAG_BACKWARD)==0);
  assert(frameReads>0&&ioSeeks>0&&ffFlushes>0);}
 // Legacy disc failure completion still sets DVD hop-channel state.
 {Fixture f;CDVDInputStream::IPosTime pos;f.input->posTime=&pos;f.input->type=DVDSTREAM_TYPE_DVD;
  CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;p.Handle({});
  assert(p.flushes==1&&p.m_dvd.state==0);p.Complete();assert(p.m_dvd.state==DVDSTATE_SEEK);}
}
static CVideoRecoveryGate::Conditions eligible(){
 CVideoRecoveryGate::Conditions c;
 c.generationMatches=c.canSeek=c.normalPlayback=c.streamPlaying=c.cacheReady=c.displayAvailable=c.sourceEligible=true;
 return c;
}
void unchangedRecovery(){
 // Same message the parent builds for a recovery: silent (trickplay), accurate and synchronized.
 Fixture f;CVideoPlayer p;p.m_pDemuxer=&f.demux;p.m_pInputStream=f.input;
 const auto now=std::chrono::steady_clock::now();
 assert(p.m_videoRecoveryGate.TryBegin(now,eligible()));
 Msg msg;msg.trick=true;msg.recovery=true;msg.videoRecovery=true;
 auto before=p.queued;p.process.seeking=true;p.Handle(msg);
 assert(f.demux.WasSeekRejectedWithoutChange());
 assert(p.flushes==0&&p.queued==before&&p.m_State.dts==17000&&p.m_State.lastSeek==2000000);
 assert(p.m_clock.value==17000&&p.m_caching==CVideoPlayer::CACHESTATE_DONE&&p.recalls==0);
 assert(!p.m_subtitleSeekNewRun&&p.m_chapterSeekTarget==3&&!p.complete&&!p.process.seeking);
 assert(!p.m_videoRecoveryGate.CanExecute(eligible()) && "rejected recovery must end its pending attempt");
 // The attempt counts: one more executed recovery exhausts the per-stream budget.
 assert(p.m_videoRecoveryGate.TryBegin(now+std::chrono::seconds(60),eligible()));
 p.m_videoRecoveryGate.OnExecute();p.m_videoRecoveryGate.CancelPending();
 assert(!p.m_videoRecoveryGate.TryBegin(now+std::chrono::seconds(600),eligible()) &&
        "rejected recovery counts against the budget");
}
int main(int argc,char** argv){
 const std::string which=argc>1?argv[1]:"all";
 if(which=="all"||which=="demux")unchangedDemux();
 if(which=="all"||which=="player")unchangedPlayer();
 if(which=="all")adjacent();
 if(which=="all"||which=="recovery")unchangedRecovery();
 std::cout<<"AVI seek rejection "<<which<<": PASS\n";
}
'''


RECOVERY_HEADER = (ROOT / 'xbmc/cores/VideoPlayer/DecoderFlushRecovery.h').read_text().replace('#pragma once', '')


def harness(root, ffmpeg):
    demux_path = 'xbmc/cores/VideoPlayer/DVDDemuxers/'
    demux = (root / (demux_path + 'DVDDemuxFFmpeg.cpp')).read_text()
    header = (root / (demux_path + 'DVDDemuxFFmpeg.h')).read_text()
    base = (root / (demux_path + 'DVDDemux.h')).read_text()
    player = (root / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    seek = (ffmpeg / 'libavformat/seek.c').read_text()
    avi = (ffmpeg / 'libavformat/avidec.c').read_text()
    library = '\n'.join(f.block(seek, sig) for sig in
                         ('int ff_index_search_timestamp(', 'int av_index_search_timestamp('))
    library += f.block(avi, 'static int avi_read_seek(').replace('"PRId64', '" PRId64 ')
    library += '\n'.join(f.block(seek, sig) for sig in
                           ('static int seek_frame_generic(', 'static int seek_frame_internal(', 'int av_seek_frame('))
    source = PREFIX.replace('struct CDVDDemux{', library + '\nstruct CDVDDemux{')
    # Allow original source to compile so its failure is a behavioral assertion,
    # never a missing-new-symbol compilation error.
    new_api = 'WasSeekRejectedWithoutChange' in base
    source = source.replace('@BASE_OUTCOME@', f.block(base, 'virtual bool WasSeekRejectedWithoutChange')
                            if new_api else 'virtual bool WasSeekRejectedWithoutChange() const {return false;}')
    source = source.replace('@OUTCOME@', f.block(header, 'bool WasSeekRejectedWithoutChange')
                            if new_api else 'bool WasSeekRejectedWithoutChange() const override {return false;}')
    helper = 'bool CDVDDemuxFFmpeg::IsAVISeekBeforeFirstKeyframe('
    source = source.replace('@PREFLIGHT_DECL@', 'bool IsAVISeekBeforeFirstKeyframe(int64_t);')
    source = source.replace('@PREFLIGHT@', f.block(demux, helper) if helper in demux else
                            'bool CDVDDemuxFFmpeg::IsAVISeekBeforeFirstKeyframe(int64_t){return false;}')
    source = source.replace('@SEEK_TIME@', f.block(demux, 'bool CDVDDemuxFFmpeg::SeekTime('))
    source = source.replace('@CACHE_ENUM@', f.block((root / 'xbmc/cores/VideoPlayer/VideoPlayer.h').read_text(), 'enum ECacheState') + ';')
    source = source.replace('@SET_CACHING@', f.block(player, 'void CVideoPlayer::SetCaching('))
    source = source.replace('@FINISH_SEEK@', f.block(player, 'void CVideoPlayer::FinishSeek('))
    marker = next(m for m in ('else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK) ||',
                              'else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK) &&') if m in player)
    branch = f.body(player, marker)
    branch = branch[branch.index('      if (!m_State.canseek)'):]
    source = source.replace('@PLAYER_SEEK@', branch)
    source = source.replace('@RECOVERY_HEADER@', RECOVERY_HEADER)
    return source + TESTS


def run(source, tmp, name, mode='all', expected=None):
    path = tmp / (name + '.cpp')
    path.write_text(source)
    exe = tmp / name
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-Wno-sign-compare', '-Wno-misleading-indentation', '-fno-pie', '-no-pie',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(path), '-o', str(exe)], check=True)
    result = subprocess.run([str(exe), mode], text=True, capture_output=True)
    if expected:
        assert result.returncode != 0 and expected in result.stderr, result.stdout + result.stderr
        print(name + ': expected behavioral assertion: ' + expected)
    else:
        print(result.stdout, end='')
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg-source', type=Path, required=True)
    parser.add_argument('--original-root', type=Path, help='Pristine pre-correction source for negative controls')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='avi-rejection-') as directory:
        tmp = Path(directory)
        source = harness(ROOT, args.ffmpeg_source)
        run(source, tmp, 'current')
        # Source mutations catch late classification and unsafe blanket preservation.
        early = 'm_seekRejectedWithoutChange = true;\n      return false;'
        assert early in source
        run(source.replace(early, 'av_packet_unref(&m_pkt.pkt);\n      ' + early), tmp,
            'late-preflight', 'demux', 'unrefs==0')
        raw_failure = '  if (ret >= 0)\n  {\n    if (!hitEnd)'
        assert raw_failure in source
        run(source.replace(raw_failure, '  if (ret < 0) m_seekRejectedWithoutChange = true;\n' + raw_failure),
            tmp, 'unsafe-negative', expected='!f.demux.WasSeekRejectedWithoutChange()&&f.ast.frame_offset==0')
        cancel = 'if (msg.IsVideoRecovery())\n          m_videoRecoveryGate.CancelPending();\n        int expectedChapterTarget = 0;'
        assert cancel in source
        run(source.replace(cancel, 'int expectedChapterTarget = 0;', 1),
            tmp, 'rejected-recovery-stays-pending', 'recovery', 'rejected recovery must end its pending attempt')
        execute = 'if (msg.IsVideoRecovery())\n        m_videoRecoveryGate.OnExecute();'
        assert execute in source
        run(source.replace(execute, '', 1), tmp, 'rejected-recovery-not-counted', 'recovery',
            'rejected recovery counts against the budget')
        if args.original_root:
            old = harness(args.original_root, args.ffmpeg_source)
            run(old, tmp, 'original-demux', 'demux', 'early indexed-key gap must be rejected unchanged')
            # Isolate original player defect while supplying the now-safe demux outcome.
            old_player = source.replace(f.body(source, 'void CVideoPlayer::Handle(Msg msg)'),
                                       f.body(old, 'void CVideoPlayer::Handle(Msg msg)'))
            run(old_player, tmp, 'original-player', 'player', 'unchanged rejection must preserve queued playback')


if __name__ == '__main__':
    main()
