#!/usr/bin/env python3
"""Production queue/flush checks for audio recovery and deliberate seek precedence.

Reuses the lifecycle fixture's real flush/epoch continuations. Audio reopening,
demux results and scheduling are modeled; queue selection, recovery scheduling,
seek coalescing and startup guard are extracted from production. No hardware.
"""
import importlib.util
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lifecycle', ROOT / 'tools/test-player-lifecycle.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
parent = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
queue = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessageQueue.cpp').read_text()
messages = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
source = f.harness(ROOT).split('static std::shared_ptr<CDVDMsg> message(')[0]
source = source.replace('using CCriticalSection = std::mutex;', 'using CCriticalSection = std::recursive_mutex;')
source = source.replace('PLAYER_SEEK, GENERAL_EOF', 'PLAYER_SEEK, PLAYER_SEEK_CHAPTER, PLAYER_SET_AUDIOSTREAM, GENERAL_EOF')
source = source.replace('bool IsInited() const', '''
  bool PutIfNoMessages(const std::shared_ptr<CDVDMsg>&, std::initializer_list<CDVDMsg::Message>);
  unsigned GetPacketCount(CDVDMsg::Message);
  bool IsInited() const''')
source = source.replace('struct CVideoPlayerAudio {', '\n'.join((
    f.block(queue, 'bool CDVDMessageQueue::PutIfNoMessages('),
    f.block(queue, 'unsigned CDVDMessageQueue::GetPacketCount('),
    f.block(messages, 'class CDVDMsgPlayerSeek :') + ';',
    f.block(messages, 'class CDVDMsgPlayerSeekChapter :') + ';',
    'struct CVideoPlayerAudio {')))
source = source.replace('struct {bool streamsReady=false;} m_State;',
                        'struct {bool streamsReady=false, canseek=true; double lastSeek=2000000;} m_State;')
source = source.replace('double GetClock() const {return value;}',
                        'double absolute=6000000; double GetAbsoluteClock() const {return absolute;} double GetClock() const {return value;}')
source = source.replace('struct ProcessInfo { double', 'struct ProcessInfo { void SetStateSeeking(bool) {} double')
source = source.replace('  CDVDMessageQueue queue;\n  bool m_bAbortRequest', '''
  CDVDMessageQueue queue;
  CDVDMessageQueue& m_messenger=queue;
  bool m_waitingForVideoFlush=false;
  long GetUpdatedTime() {return -76;}
  void QueueAudioRecoverySeek();
  void DrainControls();
  bool chapterWorks=true;
  std::vector<double> targets;
  std::vector<int> chapters;
  int switches=0, completedSeeks=0;
  bool m_bAbortRequest''')
source += f.block(parent, 'void CVideoPlayer::QueueAudioRecoverySeek()').replace('CVideoPlayer::', 'Parent::')
selection = re.search(r'else if \((pMsg->IsType\(CDVDMsg::PLAYER_SEEK\).*?)\)\n    \{', parent, re.S).group(1)
chapter_selection = re.search(r'else if \((pMsg->IsType\(CDVDMsg::PLAYER_SEEK_CHAPTER\).*?)\)\n    \{', parent, re.S).group(1)
guard = f.block(parent, '      if (m_CurrentVideo.id >= 0 &&\n          m_CurrentVideo.syncState != IDVDStreamPlayer::SYNC_INSYNC)')
# Both production audio switch paths must request recovery without appending a seek.
audio_branch = parent.split('else if (pMsg->IsType(CDVDMsg::PLAYER_SET_AUDIOSTREAM))')[1].split('else if (pMsg->IsType(CDVDMsg::PLAYER_SET_VIDEOSTREAM))')[0]
assert audio_branch.count('m_audioRecoveryPending = true;') == 2
assert 'm_messenger.Put(' not in audio_branch
assert 'QueueAudioRecoverySeek();' in f.block(parent, 'void CVideoPlayer::HandleMessages()')
assert 'm_audioRecoveryPending = false;' in f.block(parent, 'void CVideoPlayer::Prepare()')
source += r'''
#include <thread>
void Parent::DrainControls() {
  std::shared_ptr<CDVDMsg> pMsg;
  int lifecyclePriority=0;
  while (m_messenger.Get(pMsg,0ms,lifecyclePriority,
         (m_waitingForVideoFlush || ParentLifecyclePending()) ? 2 : 0)==MSGQ_OK) {
    lifecyclePriority=0;
    if (pMsg->IsType(CDVDMsg::PLAYER_ABORT)) {m_bAbortRequest=true; break;}
    if (pMsg->IsType(CDVDMsg::PLAYER_SET_AUDIOSTREAM)) {
      ++switches;
      m_audioRecoveryPending = true;
    }
    else if (@SELECT@) {
      auto& msg=*std::static_pointer_cast<CDVDMsgPlayerSeek>(pMsg);
      if (!m_State.canseek) continue;
      @GUARD@
      const double time=msg.GetTime()+(msg.GetRelative()?m_clock.GetClock()/1000:0);
      targets.push_back(time);
      FlushBuffers(time*1000,msg.GetAccurate(),msg.GetSync(),[this]{++completedSeeks;});
    }
    else if (@CHAPTER@) {
      auto& msg=*std::static_pointer_cast<CDVDMsgPlayerSeekChapter>(pMsg);
      chapters.push_back(msg.GetChapter());
      if (chapterWorks) FlushBuffers(90000000,true,true,[this]{++completedSeeks;});
    }
  }
  QueueAudioRecoverySeek();
}
static auto seek(double target, bool relative=false, bool sync=true) {
  CDVDMsgPlayerSeek::CMode mode; mode.time=target; mode.relative=relative;
  mode.sync=sync; mode.trickplay=!sync;
  return std::make_shared<CDVDMsgPlayerSeek>(mode);
}
static auto audioChange() {return std::make_shared<CDVDMsg>(CDVDMsg::PLAYER_SET_AUDIOSTREAM);}
static void finish(Parent& p,CVideoPlayerVideo& v) {
  assert(p.ParentLifecyclePending());
  for(int step=0;v.IsFlushPending() && step<8;++step) {
    v.Step();v.m_pVideoCodec->ready=true;
  }
  assert(!v.IsFlushPending() && !v.FlushFailed());
  assert(p.ContinueParentLifecycle());
}
int main() {
  // Deliberate absolute/relative/chapter targets survive several deferred changes.
  for (int kind=0;kind<3;++kind) for (int switches : {1,3}) {
    CVideoPlayerVideo v; Parent p(v); p.m_clock.value=10000000;
    p.m_waitingForVideoFlush=true;
    for(int i=0;i<switches;++i)p.queue.Put(audioChange());
    if(kind==2)p.queue.Put(std::make_shared<CDVDMsgPlayerSeekChapter>(3));
    else p.queue.Put(seek(50,kind==1));
    p.DrainControls(); assert(p.switches==0 && p.targets.empty());
    p.m_waitingForVideoFlush=false; p.DrainControls();
    assert(p.switches==switches && !p.m_audioRecoveryPending);
    if(kind==2)assert(p.chapters==std::vector<int>{3});
    else assert(p.targets==std::vector<double>{kind==1?10050.0:50.0});
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==0);
    assert(p.completedSeeks==0 && v.GetSyncEpoch()==1 && p.audio.GetSyncEpoch()==1);
    p.DrainControls(); assert(p.completedSeeks==0); // pending receipt cannot complete early
    finish(p,v); assert(p.completedSeeks==1);
  }
  // Latest deliberate request wins; priority requests are also preserved.
  for(int priority : {0,1}) {
    CVideoPlayerVideo v;Parent p(v);
    p.queue.Put(audioChange());p.queue.Put(seek(50));p.queue.Put(seek(80));
    p.DrainControls();assert(p.targets==std::vector<double>{80});finish(p,v);
    p.m_audioRecoveryPending=true;
    p.queue.Put(seek(120),priority);p.QueueAudioRecoverySeek();
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==1);
  }
  // No deliberate request: one recovery remains until the synchronizing flush starts.
  {
    CVideoPlayerVideo v;Parent p(v);p.queue.Put(audioChange());p.queue.Put(audioChange());
    p.DrainControls();assert(p.targets.empty() && p.m_audioRecoveryPending);
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==1);
    p.QueueAudioRecoverySeek();assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==1);
    p.DrainControls();assert(p.targets==std::vector<double>{-76});
    assert(!p.m_audioRecoveryPending);finish(p,v);
    p.queue.Put(audioChange());p.DrainControls();assert(p.m_audioRecoveryPending);
  }
  // Producer before/after conditional insertion: explicit target always wins.
  for(bool before : {true,false}) {
    CVideoPlayerVideo v;Parent p(v);p.m_audioRecoveryPending=true;
    if(before)p.queue.Put(seek(50));
    p.QueueAudioRecoverySeek();
    if(!before)p.queue.Put(seek(50));
    p.DrainControls();assert(p.targets==std::vector<double>{50});finish(p,v);
  }
  // Actual concurrent producers exercise both serialized insertion orders.
  for(int i=0;i<100;++i) {
    CVideoPlayerVideo v;Parent p(v);p.m_audioRecoveryPending=true;
    std::atomic<bool> go{false};
    std::thread producer([&]{while(!go.load())std::this_thread::yield();p.queue.Put(seek(50));});
    go=true;p.QueueAudioRecoverySeek();producer.join();
    p.DrainControls();assert(p.targets==std::vector<double>{50});finish(p,v);
  }
  // Startup trickplay guard may reject recovery; retry remains one per pump.
  {
    CVideoPlayerVideo v;Parent p(v);p.m_clock.absolute=2100000;
    p.queue.Put(audioChange());p.DrainControls();p.DrainControls();
    assert(p.targets.empty() && p.m_audioRecoveryPending);
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==1);
    p.m_clock.absolute=6000000;p.DrainControls();
    assert(p.targets==std::vector<double>{-76});finish(p,v);
  }
  // Refused chapter and non-sync scan do not discharge audio recovery.
  for(bool chapter : {false,true}) {
    CVideoPlayerVideo v;Parent p(v);p.chapterWorks=false;
    p.queue.Put(audioChange());
    if(chapter)p.queue.Put(std::make_shared<CDVDMsgPlayerSeekChapter>(99));
    else p.queue.Put(seek(700,false,false));
    p.DrainControls();assert(p.m_audioRecoveryPending);
    if(!chapter) {finish(p,v);p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;p.QueueAudioRecoverySeek();}
    p.DrainControls();assert(!p.m_audioRecoveryPending && p.targets.back()==-76);
    finish(p,v);
  }
  // Deferred sync flush clears debt only when begun; cancellation clears it.
  {
    CVideoPlayerVideo v;Parent p(v);p.FlushBuffers(0,true,false);
    p.m_audioRecoveryPending=true;p.FlushBuffers(1000,true,true);
    assert(p.m_audioRecoveryPending);p.QueueAudioRecoverySeek();
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==0);
    for(int step=0;v.IsFlushPending() && step<8;++step) {
      v.Step();v.m_pVideoCodec->ready=true;
    }
    p.ContinueParentLifecycle();assert(!p.m_audioRecoveryPending);
    p.m_audioRecoveryPending=true;p.CancelParentLifecycle();assert(!p.m_audioRecoveryPending);
  }
  // Stop/display fences never enqueue recovery; nonseekable input cannot spin.
  for(int condition=0;condition<4;++condition) {
    CVideoPlayerVideo v;Parent p(v);p.m_audioRecoveryPending=true;
    if(condition==0)p.m_bStop=true;
    if(condition==1)p.m_bAbortRequest=true;
    if(condition==2)p.m_waitingForVideoFlush=true;
    if(condition==3)p.m_State.canseek=false;
    for(int i=0;i<3;++i)p.QueueAudioRecoverySeek();
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==0);
    if(condition==3)assert(!p.m_audioRecoveryPending);
    p.CancelParentLifecycle();assert(!p.m_audioRecoveryPending);
  }
  {
    CVideoPlayerVideo v;Parent p(v);p.m_waitingForVideoFlush=true;
    p.queue.Put(audioChange());p.queue.Put(seek(50));
    p.queue.Put(std::make_shared<CDVDMsg>(CDVDMsg::PLAYER_ABORT));
    p.DrainControls();assert(p.m_bAbortRequest && p.switches==0);
    p.CancelParentLifecycle();p.queue.Flush(CDVDMsg::NONE);
    assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK)==0);
    p.queue.m_bAbortRequest=true;
    assert(!p.queue.PutIfNoMessages(seek(-76),{CDVDMsg::PLAYER_SEEK}));
  }
}
'''
source=source.replace('@SELECT@',selection).replace('@CHAPTER@',chapter_selection).replace('@GUARD@',guard)
os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
with tempfile.TemporaryDirectory(prefix='audio-recovery-seek-') as tmp:
    f.run(source,Path(tmp),'audio-recovery-seek')
    # Restore old audio-handler append behavior; the same deliberate-target checks must fail.
    old=source.replace('++switches;\n      m_audioRecoveryPending = true;', '''++switches;
      CDVDMsgPlayerSeek::CMode mode;mode.time=GetUpdatedTime();mode.trickplay=true;
      m_messenger.Put(std::make_shared<CDVDMsgPlayerSeek>(mode));''')
    assert old != source
    f.run(old,Path(tmp),'old-audio-enqueue',expect_failure=True)
    lost=source.replace('if (sync)\n  {\n    m_audioRecoveryPending = false;', 'if (sync)\n  {')
    assert lost != source
    f.run(lost,Path(tmp),'recovery-not-fulfilled',expect_failure=True)
