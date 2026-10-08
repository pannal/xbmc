#!/usr/bin/env python3
"""Production recovery admission, target conversion and queue guards with boundary doubles.

Reuses actual message queue and flush/receipt continuations from the lifecycle
fixture. Demux I/O, display, input/menu objects and thread scheduling are modeled.
This is a host decision/continuation test, not full VideoPlayer or target proof.
"""
import importlib.util
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lifecycle', ROOT / 'tools/test-player-lifecycle.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
parent = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
messages = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
source = f.harness(ROOT).split('static std::shared_ptr<CDVDMsg> message(')[0]
source = source.replace('PLAYER_SEEK, PLAYER_VIDEO_RECOVERY',
                        'PLAYER_SEEK, PLAYER_SEEK_CHAPTER, PLAYER_VIDEO_RECOVERY')
source = source.replace('  void Flush(bool sync);\n  bool Recover', '  bool opens=true; bool OpenStream(int) {return opens;}\n  void Flush(bool sync);\n  bool Recover')
source = source.replace('struct CVideoPlayerAudio {',
                        f.block(messages, 'class CDVDMsgPlayerSeek :') + ';\n' +
                        f.block(messages, 'class CDVDMsgPlayerSeekChapter :') + ';\nstruct CVideoPlayerAudio {')
source = source.replace('struct CCurrentStream {', 'using IDVDStreamPlayerVideo=CVideoPlayerVideo;\nstruct CCurrentStream {')
source = source.replace('struct CCurrentStream {', '''
constexpr int CACHESTATE_FULL=6,CACHESTATE_INIT=7,DVDSTATE_NORMAL=0;
namespace StreamFlags {constexpr unsigned FLAG_STILL_IMAGES=1;}
struct CDVDInputStream {struct IMenus {virtual ~IMenus()=default;virtual bool IsTimeSearchAllowed() const=0;};};
struct CCurrentStream {''')
source = source.replace('  int id=0,syncState=', '  struct {unsigned flags=0;} hint;\n  int id=0,syncState=')
source = source.replace('struct ProcessInfo { double', 'struct ProcessInfo {float speed=1; float GetNewSpeed() const {return speed;} double')
source = source.replace('struct Input {bool realtime=false; bool IsRealtime() const {return realtime;} } input;\n  Input* m_pInputStream=&input;', '''
struct Input: CDVDInputStream::IMenus {
  bool realtime=false, timeSearch=true, posTime=false;
  bool IsRealtime() const {return realtime;}
  bool IsTimeSearchAllowed() const override {return timeSearch;}
  void* GetIPosTime() const {return posTime ? (void*)this : nullptr;}
};
std::shared_ptr<Input> m_pInputStream=std::make_shared<Input>();
Input& input=*m_pInputStream;''')
source = source.replace('struct {bool streamsReady=false; double time_offset=0;} m_State;',
                        'struct {bool streamsReady=false,canseek=true; double time_offset=0;} m_State;')
source = source.replace('  CDVDMessageQueue queue;\n  bool m_bAbortRequest', '''
  CDVDMessageQueue queue; CDVDMessageQueue& m_messenger=queue;
  bool m_waitingForVideoFlush=false,m_displayLost=false,inMenu=false;
  int m_streamPlayerSpeed=DVD_PLAYSPEED_NORMAL,m_caching=CACHESTATE_DONE;
  struct {int state=DVDSTATE_NORMAL;} m_dvd;
  bool IsInMenuInternal() const {return inMenu;}
  double m_demuxSeekBasePts=DVD_NOPTS_VALUE;
  struct {double GetTimeAfterRestoringCuts(double time) const {return time;}} m_Edl;
  std::vector<double> targets; std::vector<int> chapters;
  CVideoSeekQueueState GetVideoSeekQueueState();
  CVideoRecoveryGate::Conditions GetVideoRecoveryConditions(uint64_t);
  bool AdmitVideoStream();
  void DrainControls();
  bool m_bAbortRequest''')
source += '\n'.join(f.block(parent, marker).replace('CVideoPlayer::', 'Parent::') for marker in (
    'CVideoSeekQueueState CVideoPlayer::GetVideoSeekQueueState()',
    'CVideoRecoveryGate::Conditions CVideoPlayer::GetVideoRecoveryConditions('))

stream = parent[parent.index('    // The codec-change message carries this token'):]
stream = stream[:stream.index('    player->SendMessage')]
source += 'bool Parent::AdmitVideoStream() { auto* player=m_VideoPlayerVideo; int hint=0;\n' + stream + 'return true; }\n'

request = f.body(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_VIDEO_RECOVERY))')
seek = f.body(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK) ||')
admission = seek[:seek.index('      if (!m_State.canseek)')]
conversion = seek[seek.index('      double time = msg.GetTime();'):seek.index('      CLog::Log(LOGDEBUG, "demuxer seek to:')]
chapter = f.body(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK_CHAPTER))')
chapter_guard = chapter[:chapter.index('      m_processInfo->SeekFinished(0);')]
source += '''
void Parent::DrainControls() {
  std::shared_ptr<CDVDMsg> pMsg;int priority=0;
  while(m_messenger.Get(pMsg,0ms,priority,(m_waitingForVideoFlush || ParentLifecyclePending()) ? 2 : 0)==MSGQ_OK) {
    priority=0;
    if(pMsg->IsType(CDVDMsg::PLAYER_VIDEO_RECOVERY)) { @REQUEST@ }
    else if(pMsg->IsType(CDVDMsg::PLAYER_SEEK) || pMsg->IsType(CDVDMsg::PLAYER_VIDEO_RECOVERY_SEEK)) {
      @ADMISSION@
      @CONVERSION@
      targets.push_back(time);
      FlushBuffers(time*1000,msg.GetAccurate(),msg.GetSync());
    }
    else if(pMsg->IsType(CDVDMsg::PLAYER_SEEK_CHAPTER)) {
      @CHAPTER@
      chapters.push_back(std::static_pointer_cast<CDVDMsgPlayerSeekChapter>(pMsg)->GetChapter());
      FlushBuffers(90000000,true,true);
    }
  }
}
'''.replace('@REQUEST@', request).replace('@ADMISSION@', admission).replace('@CONVERSION@', conversion).replace('@CHAPTER@', chapter_guard)
source += r'''
static CVideoRecoveryGate::Conditions ready() {
  CVideoRecoveryGate::Conditions c;
  c.generationMatches=c.canSeek=c.normalPlayback=c.streamPlaying=c.cacheReady=c.displayAvailable=c.sourceEligible=true;
  return c;
}
static void pending(Parent& p) {
  assert(p.m_videoRecoveryGate.TryBegin(std::chrono::steady_clock::now(),ready()));
  CDVDMsgPlayerSeek::CMode mode;mode.videoRecovery=true;mode.recovery=true;
  mode.videoRecoveryGeneration=10;mode.trickplay=true;mode.relative=true;mode.restore=false;
  p.queue.Put(std::make_shared<CDVDMsgPlayerSeek>(mode));
}
int main() {
  // Full-queue video-only startup may recover while child speed is paused.
  for(int excluded=0;excluded<10;++excluded) {
    CVideoPlayerVideo video;Parent p(video);
    p.m_CurrentAudio.id=-1;p.m_streamPlayerSpeed=0;p.m_caching=CACHESTATE_INIT;
    video.accepts=false;
    if(excluded==1)p.info.speed=0;
    if(excluded==2)video.accepts=true;
    if(excluded==3)p.m_displayLost=true;
    if(excluded==4)p.input.realtime=true;
    if(excluded==5)p.inMenu=true;
    if(excluded==6)p.input.timeSearch=false;
    if(excluded==7)p.m_CurrentVideo.hint.flags=StreamFlags::FLAG_STILL_IMAGES;
    if(excluded==8)p.m_bAbortRequest=true;
    if(excluded==9)p.m_waitingForVideoFlush=true;
    CVideoRecoveryGate gate;
    assert(gate.TryBegin(std::chrono::steady_clock::now(),p.GetVideoRecoveryConditions(10))==(excluded==0));
  }
  // Preserve the originally synchronized target despite demux read-ahead and changed offsets.
  for(int domain:{0,1,2}) {
    CVideoPlayerVideo video;Parent p(video);video.accepts=false;
    p.m_streamPlayerSpeed=0;p.m_caching=CACHESTATE_INIT;
    p.m_videoRecoveryStartPts=70000000;p.m_videoRecoveryStartTime=75000;
    p.m_CurrentVideo.dts=99000000;p.m_State.time_offset=11000000;
    if(domain==1)p.m_demuxSeekBasePts=20000000;
    if(domain==2)p.input.posTime=true;
    p.queue.Put(std::make_shared<CDVDMsgVideoRecoveryRequest>(10));p.DrainControls();
    assert(p.targets.size()==1 && p.targets[0]==(domain==2 ? 75000 : 70000));
    assert(p.ParentLifecyclePending());
    assert(!p.GetVideoRecoveryConditions(p.m_videoRecoveryGeneration).generationMatches);
  }
  // Revalidate after acceptance, including changed source restrictions and generation.
  for(int excluded:{0,1,2,3}) {
    CVideoPlayerVideo video;Parent p(video);pending(p);
    if(excluded==0)p.input.timeSearch=false;
    if(excluded==1)p.info.speed=0;
    if(excluded==2)++p.m_videoRecoveryGeneration;
    if(excluded==3)p.m_displayLost=true;
    p.DrainControls();assert(p.targets.empty());
    assert(!p.m_videoRecoveryGate.CanExecute(ready()));
  }
  // A user seek queued before execution wins; recovery cannot coalesce it away.
  for(bool chapter:{false,true}) {
    CVideoPlayerVideo video;Parent p(video);pending(p);
    if(chapter)p.queue.Put(std::make_shared<CDVDMsgPlayerSeekChapter>(4));
    else {CDVDMsgPlayerSeek::CMode mode;mode.time=12345;p.queue.Put(std::make_shared<CDVDMsgPlayerSeek>(mode));}
    p.DrainControls();assert(!p.m_videoRecoveryGate.CanExecute(ready()));
    if(chapter)assert(p.targets.empty() && p.chapters==std::vector<int>{4});
    else assert(p.targets==std::vector<double>{12345} && p.chapters.empty());
  }
  // A codec change discovered in post-seek packets cannot erase the pending target.
  {
    CVideoPlayerVideo video;Parent p(video);p.m_State.time_offset=5000000;
    p.FlushBuffers(70000000,true,true);
    auto before=p.m_videoRecoveryGeneration;
    assert(p.m_videoRecoveryGate.TryBegin(std::chrono::steady_clock::now(),ready()));
    p.m_videoRecoveryGate.OnFlush();
    assert(p.AdmitVideoStream());
    assert(p.m_videoRecoveryGate.TryBegin(std::chrono::steady_clock::now(),ready()));
    assert(p.m_videoRecoveryGeneration==before+1);
    assert(p.m_videoRecoveryStartPts==70000000 && p.m_videoRecoveryStartTime==75000);
  }
  // Deferred flush admission must not advance identity or replace the active target early.
  CVideoPlayerVideo video;Parent p(video);p.m_State.time_offset=5000000;
  p.FlushBuffers(70000000,true,true);
  auto generation=p.m_videoRecoveryGeneration;
  p.FlushBuffers(90000000,true,true);
  assert(p.m_videoRecoveryGeneration==generation && p.m_videoRecoveryStartPts==70000000);
  assert(p.m_videoRecoveryStartTime==75000);
  for(int i=0;video.IsFlushPending() && i<8;++i) {video.Step();video.m_pVideoCodec->ready=true;}
  p.ContinueParentLifecycle();
  assert(p.m_videoRecoveryGeneration==generation+1 && p.m_videoRecoveryStartPts==90000000);
}
'''
os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
with tempfile.TemporaryDirectory(prefix='video-timeout-recovery-') as tmp:
    f.run(source, Path(tmp), 'video-timeout-recovery')
    # Each mutant removes a specific safety/lifecycle side effect.
    for label, old, new in (
        ('erase-post-seek-target', 'if (m_CurrentVideo.syncState != IDVDStreamPlayer::SYNC_STARTING)', 'if (true)'),
        ('skip-menu-time-search', '(!menus || menus->IsTimeSearchAllowed()) &&', ''),
        ('skip-parent-lifecycle', '!m_bStop && !m_waitingForVideoFlush && !ParentLifecyclePending()', '!m_bStop && !m_waitingForVideoFlush'),
        ('inherit-stream-cooldown', 'm_videoRecoveryGate.Reset();', 'm_videoRecoveryGate.OnFlush();'),
    ):
        assert old in source, label
        f.run(source.replace(old, new, 1), Path(tmp), label, expect_failure=True)
