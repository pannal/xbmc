#!/usr/bin/env python3
"""Run production queue filtering and player reset/flush continuations under ASan/UBSan.

Extracts whole DVDMessageQueue Put/Get/Flush methods, the production decode-loop
lifecycle selection, GENERAL_RESET/FLUSH and VC_FLUSHED/REOPEN handlers,
Flush and receipt queries, CloseStream's cancellation fragment, and the parent's
pending flush, startup handlers and clock/resync policy. Queue metadata, threading/event
primitives, codec, renderer and unrelated message handlers are recording stubs.
The single-step executor models decode scheduling, not actual player threads,
full Process or hardware. Parent startup delivery, clock selection and actual
GENERAL_RESYNC generation are exercised with recording clock/player stubs.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block(source, marker):
    start = source.index(marker)
    begin = source.index('{', start)
    level = 1
    end = begin + 1
    while level:
        level += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def body(source, marker):
    result = block(source, marker)
    return result[result.index('{') + 1:-1]


PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <functional>
#include <iterator>
#include <list>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection = std::mutex;
constexpr int LOGFATAL=0, LOGWARNING=1, LOGDEBUG=2, LOGERROR=3, LOGAUDIO=4, LOGVIDEO=5;
constexpr double DVD_NOPTS_VALUE=-1;
constexpr int DVD_PLAYSPEED_NORMAL=1000, DVD_PLAYSPEED_PAUSE=0;
constexpr int SYNCSOURCE_AUDIO=1,SYNCSOURCE_VIDEO=2,CACHESTATE_FLUSH=3,CACHESTATE_DONE=4;
constexpr int VideoPlayer_AUDIO=1, VideoPlayer_VIDEO=2, TMSG_SWITCHTOFULLSCREEN=3;
constexpr double DVD_TIME_BASE=1000000;
constexpr double DVD_SEC_TO_TIME(double value) { return value*DVD_TIME_BASE; }
struct CLog { template<class... T> static void Log(T&&...) {} };
namespace PLAYBACK_DIAGNOSTICS { inline int64_t NowUs() { return 0; } }
struct Event { void Set() {} void Reset() {} bool Wait(std::chrono::milliseconds) { return false; } };
struct CDVDMsg {
  enum Message { DEMUXER_PACKET, GENERAL_RESYNC, GENERAL_PAUSE, GENERAL_RESET,
    GENERAL_FLUSH, GENERAL_SYNCHRONIZE, GENERAL_STREAMCHANGE, VIDEO_DRAIN,
    PLAYER_STARTED, PLAYER_REPORT_STATE, PLAYER_ABORT, PLAYER_SEEK, GENERAL_EOF, NONE };
  explicit CDVDMsg(Message type): type(type) {}
  virtual ~CDVDMsg()=default;
  bool IsType(Message candidate) const { return type==candidate; }
  Message type;
};
template<class T> struct CDVDMsgType : CDVDMsg {
  CDVDMsgType(Message type, T value): CDVDMsg(type), m_value(value) {}
  T m_value;
};
using CDVDMsgBool=CDVDMsgType<bool>;
using CDVDMsgDouble=CDVDMsgType<double>;
struct CDVDMsgGeneralSynchronize : CDVDMsg {
  CDVDMsgGeneralSynchronize(std::chrono::milliseconds,int):CDVDMsg(GENERAL_SYNCHRONIZE) {}
  bool Wait(std::chrono::milliseconds duration,int) { assert(duration==0ms); ++waits; return ready; }
  bool ready=true; // Models either both arrivals or the existing media timeout.
  static inline int waits=0;
};
@FLUSH_RECEIPT@
@STREAM_FLUSH_MESSAGE@
@FLUSH_MESSAGE@
struct DemuxPacket { int iSize=7; };
struct CDVDMsgDemuxerPacket : CDVDMsg {
  explicit CDVDMsgDemuxerPacket(int id): CDVDMsg(DEMUXER_PACKET), id(id) {}
  DemuxPacket* GetPacket() { return &packet; }
  DemuxPacket packet; int id;
};
struct DVDMessageListItem {
  DVDMessageListItem(std::shared_ptr<CDVDMsg> message, int priority): message(std::move(message)), priority(priority) {}
  std::shared_ptr<CDVDMsg> message; int priority;
};
enum MsgQueueReturnCode { MSGQ_OK=1, MSGQ_TIMEOUT=0, MSGQ_ABORT=-1,
  MSGQ_NOT_INITIALIZED=-2, MSGQ_INVALID_MSG=-3 };
struct CDVDMessageQueue {
  MsgQueueReturnCode Put(const std::shared_ptr<CDVDMsg>& msg, int priority=0) { return Put(msg,priority,true); }
  MsgQueueReturnCode PutBack(const std::shared_ptr<CDVDMsg>& msg, int priority=0) { return Put(msg,priority,false); }
  MsgQueueReturnCode Put(const std::shared_ptr<CDVDMsg>&, int, bool);
  MsgQueueReturnCode Get(std::shared_ptr<CDVDMsg>&, std::chrono::milliseconds, int&, int=0);
  void UpdateTimeBack() { ++updates; }
  void UpdateTimeFront() { ++updates; }
  bool IsInited() const { return m_bInitialized; }
  int GetDataSize() const { return m_iDataSize; }
  void Flush(CDVDMsg::Message type=CDVDMsg::DEMUXER_PACKET);
  bool m_bInitialized=true, m_bAbortRequest=false, m_drain=false;
  CCriticalSection m_section; Event m_hEvent; std::string m_owner="fixture";
  std::list<DVDMessageListItem> m_messages,m_prioMessages;
  int m_iDataSize=0, updates=0; double m_TimeBack=0,m_TimeFront=0;
};
@PUT@
@GET@
@QUEUE_FLUSH@
struct CVideoPlayerAudio {
  CDVDMessageQueue m_messageQueue;
  bool m_eofPending=true;
  uint64_t m_syncRequest=40;
  struct Sink { int aborts=0; void AbortAddPackets() { ++aborts; } } m_audioSink;
  void Flush(bool sync);
  @AUDIO_FLUSH_MESSAGES@
};
@AUDIO_FLUSH@
struct CDVDVideoCodec { enum VCReturn { VC_FLUSHED, VC_REOPEN }; };
struct FakeCodec {
  bool pending=false, ready=false, failed=false; int resets=0, aborts=0, reopens=0;
  void Reset() { ++resets; pending=true; ready=false; }
  void Reopen() { ++reopens; pending=true; ready=false; }
  void Abort() { ++aborts; }
  bool LifecycleFailed() const { return failed; }
  bool LifecyclePending() const { return pending; }
  bool ContinueLifecycle() { if (pending && ready) pending=false; return !pending; }
};
struct VideoBuffer { int releases=0; void Release() { ++releases; } };
struct Stats { int resets=0; void Reset() { ++resets; } void Flush() { ++resets; } };
struct Renderer { int discards=0, hides=0; void DiscardBuffer() { ++discards; } void ShowVideo(bool show) { if(!show) ++hides; } };
struct IDVDStreamPlayer { enum ESyncState { SYNC_STARTING, SYNC_WAITSYNC, SYNC_INSYNC }; };
struct CVideoPlayerVideo {
  struct Diagnostics {
    struct Counter { void Add(int64_t) {} } lifecycle, input;
    uint64_t modes=0, messagesTimedOut=0, inputEmpty=0;
  } m_diagnostics;
  void LogSyncTransition(const char*, double) {}
  std::atomic<uint64_t> m_syncRequest{0}; uint64_t m_syncEpoch=0;
  uint64_t GetSyncEpoch() const { return m_syncRequest.load(); }
  std::shared_ptr<CVideoFlushRequest> GetFlushRequest() const { return std::atomic_load(&m_flushRequest); }
  bool AcceptsData() const { return accepts; } bool IsStalled() const { return m_stalled; }
  int GetLevel() const { return level; } bool accepts=true; int level=20;
  void Flush(bool sync);
  bool Recover(CDVDVideoCodec::VCReturn decoderState) {
    if (decoderState == CDVDVideoCodec::VC_FLUSHED) { @VC_FLUSHED@ }
    if (decoderState == CDVDVideoCodec::VC_REOPEN) { @VC_REOPEN@ }
    return false;
  }
  bool IsFlushPending() const { @IS_PENDING@ }
  bool FlushFailed() const { @FLUSH_FAILED@ }
  void RetireSession() { @RETIRE_SESSION@ }
  void SendMessage(std::shared_ptr<CDVDMsg> msg,int priority) { m_messageQueue.Put(msg,priority); }
  void FlushMessages() { m_messageQueue.Flush(); }
  void ResetFrameRateCalc() { ++frameRateResets; }
  void Step() {
    [[maybe_unused]] double pts=17;
    for(int once=0; once<1; ++once) {
      int iPriority=0; auto timeout=0ms; bool onlyPrioMsgs=false;
      @SELECTION@
      (void)onlyPrioMsgs;
      if(ret==MSGQ_ABORT) { aborted=true; return; }
      if(ret!=MSGQ_OK) return;
      if(pMsg->IsType(CDVDMsg::GENERAL_RESET)) { @RESET@ }
      else if(pMsg->IsType(CDVDMsg::GENERAL_FLUSH)) { @FLUSH_HANDLER@ }
      else if(pMsg->IsType(CDVDMsg::GENERAL_RESYNC)) ++resyncs;
      else if(pMsg->IsType(CDVDMsg::GENERAL_PAUSE)) ++pauses;
      else if(pMsg->IsType(CDVDMsg::GENERAL_SYNCHRONIZE)) ++synchronizes;
      else if(pMsg->IsType(CDVDMsg::DEMUXER_PACKET))
        delivered.push_back(std::static_pointer_cast<CDVDMsgDemuxerPacket>(pMsg)->id);
    }
  }
  std::shared_ptr<FakeCodec> m_pVideoCodec=std::make_shared<FakeCodec>();
  Renderer m_renderManager; CDVDMessageQueue m_messageQueue,m_messageParent;
  std::shared_ptr<CDVDMsg> m_pendingResetMessage;
  bool m_pendingRecoveryDiscard=false,m_isEOS=false,m_rewindStalled=true,m_stalled=false,m_bAbortOutput=false;
  bool aborted=false; int m_syncState=IDVDStreamPlayer::SYNC_INSYNC;
  struct { VideoBuffer* videoBuffer=nullptr; } m_picture;
  std::list<DVDMessageListItem> m_packets; Stats m_droppingStats,m_ptsTracker;
  std::shared_ptr<CVideoFlushRequest> m_flushRequest;
  int frameRateResets=0,resyncs=0,pauses=0,synchronizes=0;
  std::vector<int> delivered;
};
@FLUSH@
@START_MSG@
@STATE_MSG@
struct Settings {
  bool GetBool(int) const {return false;} int GetInt(int) const {return algorithm;}
  Settings* GetSettings() {return this;} int algorithm=0;
};
struct CSettings { enum {SETTING_COREELEC_RESET_PTS_ON_SEEK,SETTING_COREELEC_AMLOGIC_DV_AUDIO_SEAMLESSBRANCH}; };
struct CServiceBroker {
  static Settings* GetSettingsComponent() {static Settings settings; return &settings;}
  static CServiceBroker* GetAppMessenger() {static CServiceBroker messenger; return &messenger;}
  void PostMsg(int) {}
};
struct CRenderLifecycle {
  enum class Status {PENDING,EXECUTING,COMPLETED,CANCELLED};
  struct Request {std::atomic<Status> status{Status::PENDING};};
};
struct CCurrentStream {
  enum {AV_SYNC_NONE,AV_SYNC_CONT,AV_SYNC_FORCE};
  int id=0,syncState=IDVDStreamPlayer::SYNC_STARTING,avsync=AV_SYNC_FORCE;
  bool inited=false; unsigned packets=0;
  double dts=0,startpts=0,lastdts=0,starttime=DVD_NOPTS_VALUE,cachetime=0,cachetotal=0;
};
struct IPlayerCallback { int starts=0; void OnAVStarted(int) {++starts;} };
using CFileItem=int;
struct Parent {
  explicit Parent(CVideoPlayerVideo& video):m_VideoPlayerVideo(&video) {}
  struct Audio {
    uint64_t epoch=0; bool accepts=true; int level=20;
    std::vector<std::shared_ptr<CDVDMsg>> sent;
    void SendMessage(const std::shared_ptr<CDVDMsg>& msg,int) {sent.push_back(msg);}
    void Flush(bool) {++epoch;} uint64_t GetSyncEpoch() const {return epoch;}
    bool AcceptsData() const {return accepts;} int GetLevel() const {return level;}
  } audio;
  struct Other {int flushes=0; void Flush() {++flushes;} } otherPlayer;
  Other *m_VideoPlayerSubtitle=&otherPlayer,*m_VideoPlayerTeletext=&otherPlayer,
        *m_VideoPlayerRadioRDS=&otherPlayer,*m_VideoPlayerAudioID3=&otherPlayer;
  struct ProcessInfo { double MinTempoPlatform() const { return 0.5; } double MaxTempoPlatform() const { return 1.5; } } info;
  CCurrentStream m_CurrentAudio,m_CurrentVideo,m_CurrentSubtitle,m_CurrentTeletext,m_CurrentRadioRDS,m_CurrentAudioID3;
  Audio* m_VideoPlayerAudio=&audio;
  ProcessInfo* m_processInfo=&info;
  struct Input {bool realtime=false; bool IsRealtime() const {return realtime;} } input;
  Input* m_pInputStream=&input;
  struct Demux {int speed=0; void SetSpeed(int v) {speed=v;} } demux;
  Demux* m_pDemuxer=&demux; bool m_pSubtitleDemuxer=false;
  struct Speed {int resets=0; void Reset(double) {++resets;} } m_SpeedState;
  struct Clock {
    double value=0; int anchors=0;
    double GetClock() const {return value;}
    void Discontinuity(double v) {value=v;++anchors;}
  } m_clock;
  struct Timer {void Set(std::chrono::milliseconds) {} } m_syncTimer;
  struct {bool streamsReady=false;} m_State;
  struct {bool fullscreen=false;} m_playerOptions;
  IPlayerCallback m_callback; int m_item=0;
  struct Events {void Submit(std::function<void()> call) {call();} } events;
  Events* m_outboundEvents=&events;
  double m_offset_pts=0;
  std::chrono::steady_clock::time_point m_syncStartPtsWait{};
  int m_playSpeed=DVD_PLAYSPEED_NORMAL, cacheChanges=0,m_demuxerSpeed=0,updates=0,replacements=0;
  void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); ++cacheChanges;}
  void UpdatePlayState(int) {++updates;}
  void CompleteFileReplacement() {++replacements;}
  @PENDING_FIELDS@
  struct { void Suspend() {} } m_vs10Action;
  void FlushBuffers(double pts, bool accurate, bool sync, std::function<void()> complete={});
  void CancelParentLifecycle(); bool ContinueParentLifecycle(); void SynchronizeStreams(bool);
  void HandleMessages() {
    std::shared_ptr<CDVDMsg> pMsg; int priority=0;
    while(queue.Get(pMsg,0ms,priority,ParentLifecyclePending() ? 2 : 0)==MSGQ_OK) {
      priority=0;
      if(pMsg->IsType(CDVDMsg::PLAYER_STARTED)) { @START_HANDLER@ }
      else if(pMsg->IsType(CDVDMsg::PLAYER_REPORT_STATE)) { @STATE_HANDLER@ }
      else if(pMsg->IsType(CDVDMsg::PLAYER_ABORT)) m_bAbortRequest=true;
      else ++other;
    }
  }
  CVideoPlayerVideo* m_VideoPlayerVideo;
  CDVDMessageQueue queue;
  bool m_bAbortRequest=false,m_bStop=false;
  int other=0;
};
@PARENT_METHODS@
'''

TESTS = r'''
static std::shared_ptr<CDVDMsg> message(CDVDMsg::Message kind) { return std::make_shared<CDVDMsg>(kind); }
static int receive(CDVDMessageQueue& queue,int mode,int wanted=0) {
  std::shared_ptr<CDVDMsg> msg; int priority=wanted;
  int result=queue.Get(msg,0ms,priority,mode);
  return result==MSGQ_OK ? 100+msg->type : result;
}
static void queues() {
  CDVDMessageQueue q;
  q.Put(std::make_shared<CDVDMsgDemuxerPacket>(1),10);
  q.Put(std::make_shared<CDVDMsgDemuxerPacket>(2),10);
  q.Put(message(CDVDMsg::GENERAL_SYNCHRONIZE),1);
  q.Put(message(CDVDMsg::GENERAL_RESYNC),1);
  q.Put(message(CDVDMsg::GENERAL_PAUSE),1);
  assert(receive(q,1)==100+CDVDMsg::GENERAL_RESYNC);
  assert(receive(q,1)==100+CDVDMsg::GENERAL_PAUSE);
  assert(receive(q,1)==MSGQ_TIMEOUT);
  for(int id:{1,2}) {
    std::shared_ptr<CDVDMsg> msg; int priority=0;
    assert(q.Get(msg,0ms,priority)==MSGQ_OK);
    assert(priority==10 && std::static_pointer_cast<CDVDMsgDemuxerPacket>(msg)->id==id);
  }
  assert(receive(q,0)==100+CDVDMsg::GENERAL_SYNCHRONIZE);
  q.Put(std::make_shared<CDVDMsgDemuxerPacket>(3));
  assert(q.m_iDataSize==7);
  assert(receive(q,1)==MSGQ_TIMEOUT && q.m_iDataSize==7);
  assert(receive(q,0)==100+CDVDMsg::DEMUXER_PACKET && q.m_iDataSize==0);
  q.Put(message(CDVDMsg::GENERAL_RESYNC));
  q.m_bAbortRequest=true;
  assert(receive(q,1)==MSGQ_ABORT && q.m_messages.size()==1);
  q.m_bAbortRequest=false; q.m_bInitialized=false;
  assert(receive(q,1)==MSGQ_NOT_INITIALIZED);

  CDVDMessageQueue control;
  control.Put(std::make_shared<CDVDMsgDemuxerPacket>(4),10);
  control.Put(message(CDVDMsg::GENERAL_PAUSE));
  assert(receive(control,1,1)==100+CDVDMsg::GENERAL_PAUSE);
  assert(receive(control,1,1)==MSGQ_TIMEOUT);
  assert(receive(control,0)==100+CDVDMsg::DEMUXER_PACKET);

  CDVDMessageQueue parent;
  parent.Put(message(CDVDMsg::PLAYER_SEEK),1);
  parent.Put(message(CDVDMsg::PLAYER_STARTED));
  parent.Put(message(CDVDMsg::PLAYER_ABORT));
  assert(receive(parent,2)==100+CDVDMsg::PLAYER_STARTED);
  assert(receive(parent,2)==100+CDVDMsg::PLAYER_ABORT);
  assert(receive(parent,2)==MSGQ_TIMEOUT);
  assert(receive(parent,0)==100+CDVDMsg::PLAYER_SEEK);
}
static void audio_seek_eof() {
  for (bool sync : {false, true}) {
    CVideoPlayerAudio audio;
    auto& q=audio.m_messageQueue;
    q.Put(std::make_shared<CDVDMsgDemuxerPacket>(1));
    q.Put(message(CDVDMsg::GENERAL_EOF));
    audio.Flush(sync);
    assert(!audio.m_eofPending && audio.m_audioSink.aborts==1);
    std::shared_ptr<CDVDMsg> msg; int priority=0;
    assert(q.Get(msg,0ms,priority)==MSGQ_OK);
    auto flush=std::dynamic_pointer_cast<CDVDMsgStreamFlush>(msg);
    assert(flush && flush->epoch==41 && flush->m_value==sync);
    assert(receive(q,0)==MSGQ_TIMEOUT);
    q.Put(std::make_shared<CDVDMsgDemuxerPacket>(2));
    q.Put(message(CDVDMsg::GENERAL_EOF));
    assert(receive(q,0)==100+CDVDMsg::DEMUXER_PACKET);
    assert(receive(q,0)==100+CDVDMsg::GENERAL_EOF);
    q.Put(message(CDVDMsg::GENERAL_EOF));
    q.Put(std::make_shared<CDVDMsgDemuxerPacket>(3));
    audio.m_eofPending=true;
    audio.FlushMessages();
    assert(!audio.m_eofPending && receive(q,0)==MSGQ_TIMEOUT);
  }
}
static void continuation() {
  CVideoPlayerVideo video; VideoBuffer buffer;
  video.m_picture.videoBuffer=&buffer;
  video.m_packets.emplace_back(std::make_shared<CDVDMsgDemuxerPacket>(1),0);
  video.m_packets.emplace_back(std::make_shared<CDVDMsgDemuxerPacket>(2),0);
  video.Flush(true);
  assert(video.IsFlushPending() && video.m_pVideoCodec->aborts==1);
  video.Step();
  assert(video.m_pendingResetMessage && video.m_pVideoCodec->resets==1);
  assert(video.IsFlushPending() && buffer.releases==0 && video.m_packets.size()==2);
  assert(video.m_renderManager.discards==0 && video.frameRateResets==0);
  video.SendMessage(std::make_shared<CDVDMsgDemuxerPacket>(7),10);
  video.SendMessage(message(CDVDMsg::GENERAL_RESYNC),1);
  video.SendMessage(message(CDVDMsg::GENERAL_PAUSE),1);
  video.SendMessage(message(CDVDMsg::GENERAL_SYNCHRONIZE),1);
  video.Step(); video.Step(); video.Step();
  assert(video.resyncs==1 && video.pauses==1 && video.synchronizes==0);
  assert(video.IsFlushPending() && video.delivered.empty());
  // A timeout/general A/V event has no authority to complete this request.
  for(int i=0;i<5;++i) video.Step();
  assert(video.IsFlushPending() && video.m_pVideoCodec->resets==1);
  video.m_pVideoCodec->ready=true;
  video.Step();
  assert(!video.IsFlushPending() && !video.m_pendingResetMessage);
  assert(video.m_pVideoCodec->resets==1 && buffer.releases==1);
  assert(video.m_packets.empty() && video.m_renderManager.discards==1);
  assert(video.m_syncState==IDVDStreamPlayer::SYNC_STARTING && video.m_renderManager.hides==1);
  video.Step(); assert(video.synchronizes==1);

  // A later request cannot inherit an earlier completion.
  auto oldReceipt=video.m_flushRequest;
  video.Flush(false); video.Step();
  assert(video.IsFlushPending() && video.m_flushRequest!=oldReceipt);
  assert(oldReceipt->state==CVideoFlushRequest::State::COMPLETED);
  video.Step(); assert(video.IsFlushPending());
  video.m_pVideoCodec->ready=true; video.Step();
  assert(!video.IsFlushPending() && video.m_pVideoCodec->resets==2);
  assert(video.m_renderManager.hides==1); // original sync=false preserved

  CVideoPlayerVideo reset;
  reset.SendMessage(message(CDVDMsg::GENERAL_RESET),0);
  reset.Step(); assert(reset.m_pendingResetMessage && reset.m_pVideoCodec->resets==1);
  reset.m_pVideoCodec->ready=true; reset.Step();
  assert(!reset.m_pendingResetMessage && reset.m_pVideoCodec->resets==1);
  assert(reset.m_syncState==IDVDStreamPlayer::SYNC_STARTING);

  for(auto state:{CDVDVideoCodec::VC_FLUSHED,CDVDVideoCodec::VC_REOPEN}) {
    CVideoPlayerVideo recovery;
    recovery.m_packets.emplace_back(std::make_shared<CDVDMsgDemuxerPacket>(1),0);
    recovery.m_packets.emplace_back(std::make_shared<CDVDMsgDemuxerPacket>(2),0);
    assert(!recovery.Recover(state));
    assert(recovery.m_packets.empty() && recovery.m_pendingRecoveryDiscard);
    assert(recovery.m_pVideoCodec->resets+recovery.m_pVideoCodec->reopens==1);
    recovery.Step(); assert(recovery.delivered.empty() && recovery.m_renderManager.discards==0);
    recovery.m_pVideoCodec->ready=true; recovery.Step(); recovery.Step();
    assert((recovery.delivered==std::vector<int>{1,2}) && recovery.m_renderManager.discards==1);
  }
}
static void multiple_flushes() {
  CVideoPlayerVideo video;
  video.Flush(false); auto first=video.m_flushRequest;
  video.Flush(true); auto second=video.m_flushRequest;
  assert(first!=second);
  video.Step(); assert(video.m_pVideoCodec->resets==1);
  video.m_pVideoCodec->ready=true; video.Step();
  assert(first->state==CVideoFlushRequest::State::COMPLETED);
  assert(second->state==CVideoFlushRequest::State::PENDING && video.IsFlushPending());
  assert(video.m_renderManager.hides==0);
  video.Step(); assert(video.m_pVideoCodec->resets==2 && video.IsFlushPending());
  video.m_pVideoCodec->ready=true; video.Step();
  assert(second->state==CVideoFlushRequest::State::COMPLETED && !video.IsFlushPending());
  assert(video.m_renderManager.hides==1 && video.m_renderManager.discards==2);
  video.m_messageQueue.Flush(CDVDMsg::NONE);
  assert(first->state==CVideoFlushRequest::State::COMPLETED);

  CVideoPlayerVideo abandoned;
  abandoned.Flush(false); auto old=abandoned.m_flushRequest;
  abandoned.Flush(true); auto latest=abandoned.m_flushRequest;
  abandoned.m_messageQueue.Flush(CDVDMsg::NONE);
  assert(old->state==CVideoFlushRequest::State::CANCELLED);
  assert(latest->state==CVideoFlushRequest::State::CANCELLED);
  assert(!abandoned.IsFlushPending() && abandoned.FlushFailed());
}
static void failed_lifecycle() {
  CVideoPlayerVideo video;
  video.Flush(true); video.Step();
  video.m_pVideoCodec->failed=true;
  video.Step();
  assert(video.IsFlushPending() && video.m_pendingResetMessage);
  assert(receive(video.m_messageParent,0)==100+CDVDMsg::PLAYER_ABORT);
  assert(video.m_renderManager.discards==0 && video.m_pVideoCodec->resets==1);
}
static void cancelled_session() {
  CVideoPlayerVideo video;
  video.Flush(true); video.Step();
  auto oldReceipt=video.m_flushRequest;
  video.m_pendingRecoveryDiscard=true;
  video.RetireSession();
  assert(oldReceipt->state==CVideoFlushRequest::State::CANCELLED);
  assert(!video.m_pendingResetMessage && !video.m_pendingRecoveryDiscard);
  assert(!video.IsFlushPending() && video.FlushFailed());
  video.m_pVideoCodec=std::make_shared<FakeCodec>();
  video.Step();
  assert(video.m_pVideoCodec->resets==0 && video.m_renderManager.discards==0);
  video.Flush(false); video.Step();
  auto fresh=video.m_flushRequest;
  assert(fresh!=oldReceipt && video.IsFlushPending());
  video.m_pVideoCodec->ready=true; video.Step();
  assert(!video.IsFlushPending() && !video.FlushFailed());
  assert(oldReceipt->state==CVideoFlushRequest::State::CANCELLED);
}
static void start(Parent& parent,int player,uint64_t epoch,double timestamp) {
  SStartMsg msg{}; msg.player=player;msg.epoch=epoch;msg.timestamp=timestamp;
  msg.cachetime=100000;msg.cachetotal=200000;
  parent.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,msg));
}
static void complete(CVideoPlayerVideo& video) {
  for(int step=0;video.IsFlushPending() && step<8;++step) {
    video.Step();video.m_pVideoCodec->ready=true;
  }
  assert(!video.IsFlushPending() && !video.FlushFailed());
}
static void parent_wait() {
  for(int speed:{1000,0,1250,2000,-1000}) {
    CVideoPlayerVideo video; Parent parent(video); parent.m_playSpeed=speed;
    int callbacks=0;
    parent.FlushBuffers(123,true,true,[&]{++callbacks;});
    const bool standard=speed==1000 || speed==0 || speed==1250;
    assert(bool(parent.m_pendingFlush->streams)==standard);
    auto original=parent.m_pendingFlush->video;
    parent.queue.Put(message(CDVDMsg::PLAYER_SEEK),1);
    start(parent,VideoPlayer_VIDEO,video.GetSyncEpoch()-1,3000000);
    start(parent,VideoPlayer_VIDEO,video.GetSyncEpoch(),5000000);
    start(parent,VideoPlayer_AUDIO,parent.audio.GetSyncEpoch(),5200000);
    for(int tick=0;tick<3;++tick) {
      parent.HandleMessages();
      assert(!parent.ContinueParentLifecycle());
      parent.SynchronizeStreams(false);
      assert(callbacks==0 && parent.other==0 && parent.m_clock.anchors==0);
      assert(parent.m_pendingFlush->video==original);
      assert(parent.m_CurrentVideo.starttime==5000000);
      video.Step();
    }
    video.m_pVideoCodec->ready=true;video.Step();
    if(standard) {
      parent.m_pendingFlush->streams->ready=false;
      assert(!parent.ContinueParentLifecycle() && callbacks==0);
      parent.m_pendingFlush->streams->ready=true;
    }
    assert(parent.ContinueParentLifecycle());
    assert(parent.ContinueParentLifecycle() && callbacks==1);
    assert(parent.cacheChanges==(standard ? 1 : 0));
    assert(parent.otherPlayer.flushes==4 && parent.m_SpeedState.resets==1);
    assert(receive(parent.queue,0)==100+CDVDMsg::PLAYER_SEEK);
    parent.m_CurrentVideo.packets=parent.m_CurrentAudio.packets=1;
    parent.SynchronizeStreams(false);
    const double expected=speed==0 ? 5000000 : 4800000;
    assert(parent.m_clock.anchors==1 && parent.m_clock.value==expected);
    assert(parent.m_CurrentVideo.syncState==IDVDStreamPlayer::SYNC_INSYNC);
    assert(parent.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_INSYNC);
    assert(parent.m_callback.starts==1);
    auto resync=std::static_pointer_cast<CDVDMsgDouble>(parent.audio.sent.back());
    assert(resync->IsType(CDVDMsg::GENERAL_RESYNC) && resync->m_value==expected);
    video.Step();
    if (standard) video.Step(); // The preceding general-sync event retains queue order.
    assert(video.resyncs==1);
    parent.SynchronizeStreams(false);assert(parent.m_clock.anchors==1);
  }
  for(int cause:{0,1,2,3}) {
    CVideoPlayerVideo video;Parent parent(video);int callbacks=0;
    parent.FlushBuffers(0,false,true,[&]{++callbacks;});
    auto old=parent.m_pendingFlush->video;
    if(cause==0) parent.queue.Put(message(CDVDMsg::PLAYER_ABORT));
    if(cause==1) old->state=CVideoFlushRequest::State::CANCELLED;
    if(cause==2) video.Flush(true);
    if(cause==3) parent.m_bStop=true;
    parent.HandleMessages();assert(!parent.ContinueParentLifecycle());
    assert(!parent.ParentLifecyclePending() && callbacks==0);
  }
}
static void nested_and_renderer() {
  CVideoPlayerVideo video;Parent parent(video);std::vector<int> order;
  parent.FlushBuffers(1,false,false,[&]{order.push_back(1);});
  auto first=parent.m_pendingFlush->video;
  parent.FlushBuffers(2,true,true,[&]{order.push_back(2);});
  assert(parent.m_pendingFlush->video==first && parent.m_SpeedState.resets==1);
  complete(video);assert(!parent.ContinueParentLifecycle());
  assert(order==std::vector<int>{1} && parent.m_SpeedState.resets==2);
  assert(parent.m_pendingFlush->video!=first);
  complete(video);assert(parent.ContinueParentLifecycle());
  assert((order==std::vector<int>{1,2}));
  parent.FlushBuffers(3,true,true,[&]{parent.m_rendererRetirement=std::make_shared<CRenderLifecycle::Request>();});
  complete(video);assert(!parent.ContinueParentLifecycle());
  auto retirement=parent.m_rendererRetirement;
  for(auto status:{CRenderLifecycle::Status::PENDING,CRenderLifecycle::Status::EXECUTING}) {
    retirement->status=status;assert(!parent.ContinueParentLifecycle());
    assert(parent.m_rendererRetirement==retirement && parent.replacements==0);
  }
  retirement->status=CRenderLifecycle::Status::COMPLETED;
  assert(parent.ContinueParentLifecycle() && parent.replacements==1);
  assert(parent.ContinueParentLifecycle() && parent.replacements==1);
  parent.m_rendererRetirement=std::make_shared<CRenderLifecycle::Request>();
  parent.m_rendererRetirement->status=CRenderLifecycle::Status::CANCELLED;
  assert(!parent.ContinueParentLifecycle() && parent.replacements==1 && parent.m_bAbortRequest);
}
static void stale_and_clock_policy() {
  for(int scenario=0;scenario<6;++scenario) {
    CVideoPlayerVideo video;Parent parent(video);
    parent.FlushBuffers(0,true,true);complete(video);assert(parent.ContinueParentLifecycle());
    start(parent,VideoPlayer_AUDIO,parent.audio.GetSyncEpoch(),10000000);
    start(parent,VideoPlayer_VIDEO,video.GetSyncEpoch(),scenario==0 ? 15000000 : 5000000);
    parent.HandleMessages();
    start(parent,VideoPlayer_VIDEO,video.GetSyncEpoch()-1,99000000);
    SStateMsg stale{};stale.player=VideoPlayer_VIDEO;stale.epoch=video.GetSyncEpoch()-1;
    stale.syncState=IDVDStreamPlayer::SYNC_STARTING;
    parent.queue.Put(std::make_shared<CDVDMsgType<SStateMsg>>(CDVDMsg::PLAYER_REPORT_STATE,stale));
    parent.HandleMessages();assert(parent.m_CurrentVideo.syncState==IDVDStreamPlayer::SYNC_WAITSYNC);
    parent.m_CurrentVideo.packets=parent.m_CurrentAudio.packets=1;
    if(scenario==1)parent.input.realtime=true;
    if(scenario==3) {parent.m_CurrentVideo.avsync=CCurrentStream::AV_SYNC_CONT; parent.m_clock.value=77;}
    if(scenario==4) {
      parent.m_CurrentVideo.starttime=parent.m_CurrentAudio.starttime=DVD_NOPTS_VALUE;
      parent.SynchronizeStreams(false);assert(parent.m_clock.anchors==0);
      parent.m_syncStartPtsWait=std::chrono::steady_clock::now()-3s;
    }
    if(scenario==5) {
      CServiceBroker::GetSettingsComponent()->algorithm=5;
      parent.m_CurrentVideo.starttime=DVD_NOPTS_VALUE;
    }
    parent.SynchronizeStreams(false);
    if(scenario==0)assert(parent.m_clock.value==14800000);
    if(scenario==1 || scenario==2)assert(parent.m_clock.value==9900000); // live policy / >2s pullback cap
    if(scenario==3)assert(parent.m_clock.value==77 && parent.m_clock.anchors==0);
    if(scenario==4)assert(parent.m_clock.value==0 && parent.m_clock.anchors==1);
    if(scenario==5) {
      assert(parent.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_WAITSYNC);
      assert(!parent.audio.sent.back()->IsType(CDVDMsg::GENERAL_RESYNC));
      parent.SynchronizeStreams(false); // existing audio catch-up once video is in sync
      assert(parent.audio.sent.back()->IsType(CDVDMsg::GENERAL_RESYNC));
    }
    CServiceBroker::GetSettingsComponent()->algorithm=0;
  }
}

int main() { audio_seek_eof(); queues(); continuation(); multiple_flushes(); failed_lifecycle(); cancelled_session(); parent_wait(); nested_and_renderer(); stale_and_clock_policy(); }
'''


def harness(root=ROOT):
    queue = (root / 'xbmc/cores/VideoPlayer/DVDMessageQueue.cpp').read_text()
    video = (root / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    parent = (root / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    messages = (root / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
    selection = video[video.index('    const auto lifecycleStart ='):]
    selection = selection[:selection.index('    onlyPrioMsgs = false;') + len('    onlyPrioMsgs = false;')]
    header = (root / 'xbmc/cores/VideoPlayer/VideoPlayer.h').read_text()
    interface = (root / 'xbmc/cores/VideoPlayer/IVideoPlayer.h').read_text()
    fields = header[header.index('  struct PendingFlush'):header.index('  bool ContinueParentLifecycle();')]
    methods = '\n'.join(block(parent, signature).replace('CVideoPlayer::', 'Parent::') for signature in (
        'void CVideoPlayer::FlushBuffers(', 'void CVideoPlayer::CancelParentLifecycle(',
        'bool CVideoPlayer::ContinueParentLifecycle(', 'void CVideoPlayer::SynchronizeStreams('))
    close = block(video, 'void CVideoPlayerVideo::CloseStream(')
    retirement = close[close.index('  if (auto request ='):close.index('  m_pVideoCodec.reset();')]
    audio = (root / 'xbmc/cores/VideoPlayer/VideoPlayerAudio.cpp').read_text()
    audio_header = (root / 'xbmc/cores/VideoPlayer/VideoPlayerAudio.h').read_text()
    replacements = {
        '@AUDIO_FLUSH@': block(audio, 'void CVideoPlayerAudio::Flush(bool sync)'),
        '@AUDIO_FLUSH_MESSAGES@': block(audio_header, 'void FlushMessages()').replace(' override', ''),
        '@PUT@': block(queue, 'MsgQueueReturnCode CDVDMessageQueue::Put(const std::shared_ptr<CDVDMsg>& pMsg,\n'),
        '@QUEUE_FLUSH@': block(queue, 'void CDVDMessageQueue::Flush('),
        '@GET@': block(queue, 'MsgQueueReturnCode CDVDMessageQueue::Get('),
        '@VC_FLUSHED@': body(video, 'if (decoderState == CDVDVideoCodec::VC_FLUSHED)'),
        '@VC_REOPEN@': body(video, 'if (decoderState == CDVDVideoCodec::VC_REOPEN)'),
        '@FLUSH@': block(video, 'void CVideoPlayerVideo::Flush(bool sync)'),
        '@FLUSH_RECEIPT@': block(messages, 'struct CVideoFlushRequest') + ';',
        '@FLUSH_FAILED@': body(video, 'bool CVideoPlayerVideo::FlushFailed() const'),
        '@RETIRE_SESSION@': retirement,
        '@FLUSH_MESSAGE@': block(messages, 'class CDVDMsgVideoFlush') + ';',
        '@IS_PENDING@': body(video, 'bool CVideoPlayerVideo::IsFlushPending() const'),
        '@SELECTION@': selection,
        '@RESET@': body(video, 'else if (pMsg->IsType(CDVDMsg::GENERAL_RESET))'),
        '@FLUSH_HANDLER@': body(video, 'else if (pMsg->IsType(CDVDMsg::GENERAL_FLUSH))'),
        '@STREAM_FLUSH_MESSAGE@': block(messages, 'class CDVDMsgStreamFlush') + ';',
        '@START_MSG@': block(interface, 'struct SStartMsg') + ';',
        '@STATE_MSG@': block(interface, 'struct SStateMsg') + ';',
        '@START_HANDLER@': body(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_STARTED))'),
        '@STATE_HANDLER@': body(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_REPORT_STATE))'),
        '@PENDING_FIELDS@': fields,
        '@PARENT_METHODS@': methods,
    }
    source = PREFIX + TESTS
    for key, value in replacements.items():
        source = source.replace(key, value)
    return source


def run(source, directory, label, expect_failure=False):
    cpp = directory / (label + '.cpp')
    exe = directory / label
    cpp.write_text(source)
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                    str(cpp), '-o', str(exe)], check=True)
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    if expect_failure:
        if result.returncode == 0:
            raise AssertionError(label + ': broken production accepted')
        print('REJECTED:', label)
    elif result.returncode:
        raise AssertionError(result.stderr)
    else:
        print('PASS:', label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness(args.root)
    with tempfile.TemporaryDirectory(prefix='player-lifecycle-') as tmp:
        directory = Path(tmp)
        run(source, directory, 'player-lifecycle')
        if args.negative_controls:
            mutants = {
                'audio-stale-eof': ('  m_messageQueue.Flush(CDVDMsg::GENERAL_EOF);', ''),
                'audio-loses-epoch': ('std::make_shared<CDVDMsgStreamFlush>(sync, ++m_syncRequest)', 'std::make_shared<CDVDMsgBool>(CDVDMsg::GENERAL_FLUSH, sync)'),
                'admit-replay-while-pending': ('if (lifecyclePending)', 'if (false && lifecyclePending)'),
                'repeat-reset-on-continuation': ('m_pVideoCodec && !continuingReset', 'm_pVideoCodec && (continuingReset || !continuingReset)'),
                'receipt-false-completion': ('return request && request->state == CVideoFlushRequest::State::PENDING;', 'return request && false;'),
                'abandoned-receipt-not-cancelled': ('request->state.compare_exchange_strong(pending, CVideoFlushRequest::State::CANCELLED)', 'request->state.compare_exchange_strong(pending, CVideoFlushRequest::State::PENDING)'),
                'trickplay-skips-receipt': ('if (request && request->state == CVideoFlushRequest::State::PENDING)', 'if (m_playSpeed != 2000 && request && request->state == CVideoFlushRequest::State::PENDING)'),
                'parent-skips-receipt': ('if (request && request->state == CVideoFlushRequest::State::PENDING)', 'if (false && request && request->state == CVideoFlushRequest::State::PENDING)'),
            }
            mutants.update({
                'accept-stale-start': ('msg.epoch != m_VideoPlayerVideo->GetSyncEpoch()', 'false'),
                'skip-renderer-retirement': ('status == CRenderLifecycle::Status::PENDING || status == CRenderLifecycle::Status::EXECUTING', 'false'),
                'anchor-while-pending': ('!m_pInputStream || ParentLifecyclePending() ||', '!m_pInputStream ||'),
                'lose-current-start': ('if (m_pendingFlush->streams)\n      SetCaching', 'm_CurrentVideo.syncState = IDVDStreamPlayer::SYNC_STARTING;\n    if (m_pendingFlush->streams)\n      SetCaching'),
            })
            for label, (old, new) in mutants.items():
                if old not in source:
                    raise AssertionError('negative control marker missing: ' + label)
                run(source.replace(old, new, 1), directory, label, expect_failure=True)


if __name__ == '__main__':
    main()
