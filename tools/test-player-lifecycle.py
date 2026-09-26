#!/usr/bin/env python3
"""Run production queue filtering and player reset/flush continuations under ASan/UBSan.

Extracts whole DVDMessageQueue Put/Get/Flush methods, the production decode-loop
lifecycle selection, GENERAL_RESET/FLUSH and VC_FLUSHED/REOPEN handlers,
Flush and receipt queries, CloseStream's cancellation fragment, and the parent's
flush-receipt wait including the surrounding speed/cache policy. Queue metadata, threading/event
primitives, codec, renderer and unrelated message handlers are recording stubs.
The single-step executor models decode scheduling, not actual player threads,
clock policy, full Process, or hardware. Parent startup delivery is exercised;
clock selection and subsequent GENERAL_RESYNC generation are outside this fixture.
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
#include <string>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection = std::mutex;
constexpr int LOGFATAL=0, LOGWARNING=1, LOGDEBUG=2;
constexpr double DVD_NOPTS_VALUE=-1;
constexpr int DVD_PLAYSPEED_NORMAL=1000, DVD_PLAYSPEED_PAUSE=0;
constexpr int SYNCSOURCE_AUDIO=1,SYNCSOURCE_VIDEO=2,CACHESTATE_FLUSH=3;
struct CLog { template<class... T> static void Log(T&&...) {} };
struct Event { void Set() {} void Reset() {} bool Wait(std::chrono::milliseconds) { return false; } };
struct CDVDMsg {
  enum Message { DEMUXER_PACKET, GENERAL_RESYNC, GENERAL_PAUSE, GENERAL_RESET,
    GENERAL_FLUSH, GENERAL_SYNCHRONIZE, GENERAL_STREAMCHANGE, VIDEO_DRAIN,
    PLAYER_STARTED, PLAYER_ABORT, PLAYER_SEEK, NONE };
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
struct CDVDMsgGeneralSynchronize : CDVDMsg {
  CDVDMsgGeneralSynchronize(std::chrono::milliseconds,int):CDVDMsg(GENERAL_SYNCHRONIZE) {}
  void Wait(bool&,int) { ++waits; } // Historical general-sync timeout reports success.
  static inline int waits=0;
};
@FLUSH_RECEIPT@
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
  void Flush(CDVDMsg::Message type=CDVDMsg::DEMUXER_PACKET);
  bool m_bInitialized=true, m_bAbortRequest=false, m_drain=false;
  CCriticalSection m_section; Event m_hEvent; std::string m_owner="fixture";
  std::list<DVDMessageListItem> m_messages,m_prioMessages;
  int m_iDataSize=0, updates=0; double m_TimeBack=0,m_TimeFront=0;
};
@PUT@
@GET@
@QUEUE_FLUSH@
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
struct IDVDStreamPlayer { enum { SYNC_STARTING, SYNC_INSYNC }; };
struct CVideoPlayerVideo {
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
struct CThread { static inline std::function<void()> tick; static void Sleep(std::chrono::milliseconds) { tick(); } };
struct Parent {
  explicit Parent(CVideoPlayerVideo& video):m_VideoPlayerVideo(&video) {}
  struct Audio { void SendMessage(const std::shared_ptr<CDVDMsg>&,int) {} } audio;
  struct ProcessInfo { double MinTempoPlatform() const { return 0.5; } double MaxTempoPlatform() const { return 1.5; } } info;
  struct Current { int syncState=7; } m_CurrentAudio,m_CurrentVideo;
  Audio* m_VideoPlayerAudio=&audio;
  ProcessInfo* m_processInfo=&info;
  int m_playSpeed=DVD_PLAYSPEED_NORMAL, cacheChanges=0;
  void SetCaching(int value) { assert(value==CACHESTATE_FLUSH); ++cacheChanges; }
  void HandleMessages() {
    std::shared_ptr<CDVDMsg> msg; int priority=0;
    while(queue.Get(msg,0ms,priority,m_waitingForVideoFlush ? 2 : 0)==MSGQ_OK) {
      priority=0;
      if(msg->IsType(CDVDMsg::PLAYER_STARTED)) ++starts;
      else if(msg->IsType(CDVDMsg::PLAYER_ABORT)) m_bAbortRequest=true;
      else ++other;
    }
  }
  void Wait(bool sync=true) { @PARENT_WAIT@ advanced=true; }
  CVideoPlayerVideo* m_VideoPlayerVideo;
  CDVDMessageQueue queue;
  CDVDMessageQueue& m_messenger=queue;
  bool m_waitingForVideoFlush=false,m_bAbortRequest=false,m_bStop=false,advanced=false;
  int starts=0,other=0;
};
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
static void parent_wait() {
  for(int speed:{1000,0,1250,2000,-1000}) {
    CVideoPlayerVideo video; video.Flush(true); video.Step();
    Parent parent(video); parent.m_playSpeed=speed;
    parent.queue.Put(message(CDVDMsg::PLAYER_SEEK),1);
    parent.queue.Put(message(CDVDMsg::PLAYER_STARTED));
    int ticks=0;
    const int priorWaits=CDVDMsgGeneralSynchronize::waits;
    CThread::tick=[&] {
      assert(parent.m_waitingForVideoFlush && !parent.advanced);
      assert(parent.starts==1 && parent.other==0);
      if(++ticks==3) video.m_pVideoCodec->ready=true;
      assert(ticks<5);
      video.Step();
    };
    parent.Wait();
    assert(ticks==3 && parent.advanced && !parent.m_waitingForVideoFlush);
    assert(receive(parent.queue,0)==100+CDVDMsg::PLAYER_SEEK);
    const bool standard=speed==1000 || speed==0 || speed==1250;
    assert(parent.cacheChanges==(standard ? 1 : 0));
    assert(CDVDMsgGeneralSynchronize::waits-priorWaits==(standard ? 1 : 0));
    assert(parent.m_CurrentVideo.syncState==(standard ? IDVDStreamPlayer::SYNC_STARTING : 7));
  }
  CVideoPlayerVideo video;
  video.Flush(false); video.Step();
  Parent abort(video); abort.queue.Put(message(CDVDMsg::PLAYER_ABORT));
  CThread::tick=[] {};
  abort.Wait();
  assert(abort.m_bAbortRequest && !abort.advanced && video.IsFlushPending());
  assert(!abort.m_waitingForVideoFlush);
}
int main() { queues(); continuation(); multiple_flushes(); failed_lifecycle(); cancelled_session(); parent_wait(); }
'''


def harness(root=ROOT):
    queue = (root / 'xbmc/cores/VideoPlayer/DVDMessageQueue.cpp').read_text()
    video = (root / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    parent = (root / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    messages = (root / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
    selection = video[video.index('    const bool lifecyclePending ='):]
    selection = selection[:selection.index('    onlyPrioMsgs = false;') + len('    onlyPrioMsgs = false;')]
    flush_buffers = block(parent, 'void CVideoPlayer::FlushBuffers(')
    wait = flush_buffers[flush_buffers.index('  if (m_playSpeed == DVD_PLAYSPEED_NORMAL'): ]
    wait = wait[:wait.index('  m_CurrentVideo.lastdts = DVD_NOPTS_VALUE;')]
    close = block(video, 'void CVideoPlayerVideo::CloseStream(')
    retirement = close[close.index('  if (auto request ='):close.index('  m_pVideoCodec.reset();')]
    replacements = {
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
        '@PARENT_WAIT@': wait,
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
                'admit-replay-while-pending': ('if (lifecyclePending)', 'if (false && lifecyclePending)'),
                'repeat-reset-on-continuation': ('m_pVideoCodec && !continuingReset', 'm_pVideoCodec && (continuingReset || !continuingReset)'),
                'receipt-false-completion': ('return request && request->state == CVideoFlushRequest::State::PENDING;', 'return request && false;'),
                'abandoned-receipt-not-cancelled': ('request->state.compare_exchange_strong(pending, CVideoFlushRequest::State::CANCELLED)', 'request->state.compare_exchange_strong(pending, CVideoFlushRequest::State::PENDING)'),
                'trickplay-skips-receipt': ('while (m_VideoPlayerVideo->IsFlushPending() &&', 'while (m_playSpeed != 2000 && m_VideoPlayerVideo->IsFlushPending() &&'),
                'parent-skips-receipt': ('while (m_VideoPlayerVideo->IsFlushPending() &&', 'while (false && m_VideoPlayerVideo->IsFlushPending() &&'),
            }
            for label, (old, new) in mutants.items():
                if old not in source:
                    raise AssertionError('negative control marker missing: ' + label)
                run(source.replace(old, new, 1), directory, label, expect_failure=True)


if __name__ == '__main__':
    main()
