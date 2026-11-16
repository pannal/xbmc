#!/usr/bin/env python3
"""Execute production speed/seek handlers and flush/startup continuations.

The queue, speed transition, complete time-seek branch, caching policy, video
flush receipt and parent clock/resync methods are production code. Demux results,
audio sink/flush acknowledgments, presented PTS and scheduling are recording
adapters. The audio speed handler and resync resume gate are production code. This proves timeline
and lifecycle routing, not TrueHD bytes, real threads, target compilation or
audible hardware recovery. No device access is performed.
"""
import argparse
import importlib.util
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lifecycle", ROOT / "tools/test-player-lifecycle.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def replace(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


def harness(root):
    parent = (root / "xbmc/cores/VideoPlayer/VideoPlayer.cpp").read_text()
    audio = (root / "xbmc/cores/VideoPlayer/VideoPlayerAudio.cpp").read_text()
    messages = (root / "xbmc/cores/VideoPlayer/DVDMessage.h").read_text()
    source = f.harness(root).split("static std::shared_ptr<CDVDMsg> message(")[0]
    source = replace(source, "using CCriticalSection = std::mutex;",
                     "using CCriticalSection = std::recursive_mutex;")
    source = replace(source, "PLAYER_SEEK, PLAYER_VIDEO_RECOVERY",
                     "PLAYER_SEEK, PLAYER_SEEK_CHAPTER, PLAYER_SETSPEED, PLAYER_VIDEO_RECOVERY")
    source = replace(source, "using CDVDMsgDouble=CDVDMsgType<double>;",
                     "using CDVDMsgDouble=CDVDMsgType<double>; using CDVDMsgInt=CDVDMsgType<int>;")
    source = replace(source, "struct CVideoPlayerAudio {",
                     f.block(messages, "class CDVDMsgPlayerSetSpeed :") + ";\n" +
                     "struct CVideoPlayerAudio {")
    source = replace(source, "  void LogSyncTransition(const char*, double) {}",
                     "  double presentedPts=90000000; double GetCurrentPts(){return presentedPts;}\n"
                     "  void SetSpeed(int speed){m_speed=speed;}\n"
                     "  void LogSyncTransition(const char*, double) {}")
    source = replace(source, "struct Parent {", INPUT + "\nstruct Parent {")
    source = replace(source, "    uint64_t epoch=0; bool accepts=true; int level=20;",
                     AUDIO_FIELDS)
    source = replace(source, "void Flush(bool) {++epoch;}",
                     "void Flush(bool sync) {++epoch; if(sync){m_syncState=IDVDStreamPlayer::SYNC_STARTING; m_audioSink.Pause();}}")
    source = replace(source, "  struct ProcessInfo { double", """
  struct ProcessInfo {
    void SetStateSeeking(bool) {} void SeekFinished(int) {}
    void SetTempo(float) {} void SetSpeed(float) {} void SetFrameAdvance(bool) {}
    double""")
    source = replace(source, "    void SendMessage(const std::shared_ptr<CDVDMsg>& msg,int) {sent.push_back(msg);}", """
    void SendMessage(const std::shared_ptr<CDVDMsg>& msg,int) {
      sent.push_back(msg);
      if(msg->IsType(CDVDMsg::GENERAL_RESYNC)) {
        @AUDIO_RESYNC_GATE@
      }
    }
    void SetSpeed(int value) {
      auto pMsg=std::make_shared<CDVDMsgInt>(CDVDMsg::PLAYER_SETSPEED,value);
      @AUDIO_SPEED@
    }""".replace("@AUDIO_SPEED@", f.body(audio, "else if (pMsg->IsType(CDVDMsg::PLAYER_SETSPEED))"))
                     .replace("@AUDIO_RESYNC_GATE@", audio[
                         audio.index("      if (m_speed != DVD_PLAYSPEED_PAUSE && !m_paused)"):
                         audio.index("      m_syncTimer.Set(3000ms);")]))
    source = replace(source, "    void SetSpeed(int value) {", """
    void Reset() {
      struct {int nb_frames=12;} audioframe;
      @AUDIO_RESET@
      assert(audioframe.nb_frames==0);
    }
    void SetSpeed(int value) {""".replace(
        "@AUDIO_RESET@", f.body(audio, "else if (pMsg->IsType(CDVDMsg::GENERAL_RESET))")))
    source = replace(source, "  struct Input {bool realtime=false; bool IsRealtime() const {return realtime;} } input;\n  Input* m_pInputStream=&input;",
                     "  std::shared_ptr<CDVDInputStream> m_pInputStream=std::make_shared<CDVDInputStream>();")
    source = replace(source, "  struct Demux {int speed=0; void SetSpeed(int v) {speed=v;} } demux;\n  Demux* m_pDemuxer=&demux; bool m_pSubtitleDemuxer=false;",
                     DEMUX)
    source = replace(source, "    double GetClock() const {return value;}", """
    double absolute=6000000; int speed=1000;
    double GetAbsoluteClock() const {return absolute;}
    void SetSpeed(int v){speed=v;} void SetSpeedAdjust(int) {}
    double GetClock() const {return value;}""")
    source = replace(source, "  struct {bool streamsReady=false; double time_offset=0;} m_State;", """
  struct {
    bool streamsReady=false,canseek=true;
    double time_offset=0,lastSeek=2000000,timestamp=0,time=0,dts=0;
  } m_State;""")
    source = replace(source, "struct IPlayerCallback { int starts=0;",
                     "struct IPlayerCallback { void OnPlayBackSpeedChanged(int) {} int starts=0;")
    source = replace(source, "  void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); ++cacheChanges;}",
                     "  void SetCaching(ECacheState);")
    source = replace(source, "  CDVDMessageQueue queue;", PARENT_FIELDS + "\n  CDVDMessageQueue queue;")
    source += "\n" + f.block(parent, "void CVideoPlayer::SetCaching(").replace("CVideoPlayer::", "Parent::")
    source += "\n" + f.block(parent, "CVideoSeekQueueState CVideoPlayer::GetVideoSeekQueueState()").replace("CVideoPlayer::", "Parent::")
    speed = f.body(parent, "else if (pMsg->IsType(CDVDMsg::PLAYER_SETSPEED))")
    seek = f.body(parent, "else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK) ||")
    source += """
void Parent::DrainControls() {
  std::shared_ptr<CDVDMsg> pMsg; int lifecyclePriority=0;
  @LOOP@ {
    lifecyclePriority=0;
    if(pMsg->IsType(CDVDMsg::PLAYER_ABORT)){m_bAbortRequest=true;break;}
    if(pMsg->IsType(CDVDMsg::PLAYER_SETSPEED)){@SPEED@}
    else if(pMsg->IsType(CDVDMsg::PLAYER_SEEK)){@SEEK@}
  }
}
""".replace("@LOOP@", parent[parent.index("  while (m_messenger.Get(pMsg,"):parent.index("\n  {", parent.index("  while (m_messenger.Get(pMsg,"))]).replace("@SPEED@", speed).replace("@SEEK@", seek)
    return source + TESTS


INPUT = r"""
using ECacheState=int;
constexpr int CACHESTATE_FULL=6,CACHESTATE_PLAY=7;
constexpr int DVDSTREAM_TYPE_PVRMANAGER=1;
constexpr double DVD_MSEC_TO_TIME(double time){return time*1000;}
struct CDVDInputStream {
  struct IMenus {virtual ~IMenus()=default; virtual bool IsTimeSearchAllowed(){return false;}};
  virtual ~CDVDInputStream()=default;
  bool IsStreamType(int){return false;}
  bool IsRealtime(){return false;}
  void* GetIPosTime(){return nullptr;}
};
struct CInputStreamPVRBase : CDVDInputStream {void Pause(bool){}};
"""

AUDIO_FIELDS = r"""
    uint64_t epoch=0; bool accepts=true; int level=20;
    int m_speed=1000,m_syncState=IDVDStreamPlayer::SYNC_INSYNC;
    bool m_paused=false,m_stalled=false;
    struct Sink {
      bool paused=false; int resumes=0,flushes=0;
      void Flush(){++flushes;}
      void Pause(){paused=true;} void Resume(){paused=false;++resumes;}
    } m_audioSink;
    struct Codec{int resets=0;void Reset(){++resets;}};
    std::unique_ptr<Codec> m_pAudioCodec=std::make_unique<Codec>();
    struct {void SetAudioDialNorm(){}} m_dataCacheCore;
    double m_audioClock=123,m_pcmOutputClock=456;
    static constexpr double LOCAL_NOPTS=-1;
    bool m_lavStylePcmSyncEnabled=false,m_pcmResyncTimestamp=false;
    struct {int resets=0;void Reset(){++resets;}}m_pcmJitterTracker;
    struct Info {bool IsTempoAllowed(float speed){return speed>=0.5f&&speed<=1.5f;}} m_processInfo;
"""

DEMUX = r"""
  struct Demux {
    enum Result {SUCCESS,REJECTED,FAILED}; Result result=SUCCESS;
    int speed=0; std::vector<double> targets; std::vector<bool> backwards;
    void SetSpeed(int v){speed=v;}
    bool SeekTime(double time,bool backward,double* start){
      targets.push_back(time);backwards.push_back(backward);
      *start=time*1000;return result==SUCCESS;
    }
    bool WasSeekRejectedWithoutChange(){return result==REJECTED;}
  } demux;
  Demux* m_pDemuxer=&demux;
  struct SubtitleDemux {bool SeekTime(double,bool){return true;}};
  SubtitleDemux* m_pSubtitleDemuxer=nullptr;
"""

PARENT_FIELDS = r"""
  int m_streamPlayerSpeed=1000,m_caching=CACHESTATE_DONE,completedSeeks=0;
  double m_demuxSeekBasePts=DVD_NOPTS_VALUE;
  bool m_waitingForVideoFlush=false,m_subtitleSeekNewRun=false;
  std::atomic<int> m_chapterSeekTarget{0};
  CCriticalSection m_StateSection;
  std::shared_ptr<CDVDMsg> m_pendingLargeSkip;
  CDVDMessageQueue& m_messenger=queue;
  struct CacheInfo{bool valid=false;};
  CacheInfo GetCachingTimes(){return {};}
  Timer m_cachingTimer;
  struct EDL{double GetTimeAfterRestoringCuts(double value){return value;}} m_Edl;
  bool IsInMenuInternal(){return false;}
  void RecallSubtitlesAfterSeek(double,double) {}
  void FinishSeek(bool){++completedSeeks;}
  void ClearRejectedLargeSkip(const CDVDMsg* request){
    if(m_pendingLargeSkip.get()==request)m_pendingLargeSkip.reset();
  }
  std::chrono::milliseconds GetVideoRecoveryPosition() const{return 0ms;}
  CVideoRecoveryGate::Conditions GetVideoRecoveryConditions(uint64_t) const{return {};}
  void SetPlaySpeed(int speed){
    m_messenger.Put(std::make_shared<CDVDMsgPlayerSetSpeed>(CDVDMsgPlayerSetSpeed::SpeedParams{speed,false}));
  }
  CVideoSeekQueueState GetVideoSeekQueueState();
  void DrainControls();
"""

TESTS = r"""
static void speed(Parent& p,int value) {p.SetPlaySpeed(value);p.DrainControls();}
static void finish(Parent& p,CVideoPlayerVideo& v) {
  assert(p.ParentLifecyclePending());
  assert(!p.ContinueParentLifecycle());
  for(int i=0;v.IsFlushPending()&&i<8;++i){v.Step();v.m_pVideoCodec->ready=true;}
  assert(!v.IsFlushPending()&&!v.FlushFailed());
  assert(p.ContinueParentLifecycle());
}
static void synchronize(Parent& p,CVideoPlayerVideo& v,double target) {
  for(int player:{VideoPlayer_AUDIO,VideoPlayer_VIDEO}) {
    SStartMsg note{};note.player=player;note.timestamp=target;note.cachetime=100000;
    note.cachetotal=200000;note.epoch=player==VideoPlayer_AUDIO?p.audio.GetSyncEpoch():v.GetSyncEpoch();
    p.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,note));
  }
  p.HandleMessages();p.m_CurrentAudio.packets=p.m_CurrentVideo.packets=1;
  p.SynchronizeStreams(false);
  assert(p.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_INSYNC);
  assert(p.audio.sent.back()->IsType(CDVDMsg::GENERAL_RESYNC));
}
static auto userSeek(double target) {
  CDVDMsgPlayerSeek::CMode mode;mode.time=target;
  return std::make_shared<CDVDMsgPlayerSeek>(mode);
}
int main() {
  // Exact failing sequence: stale reverse clock, real speed-zero pause, then 1x.
  // Exercise both directions, both receipt styles, and no displayed-PTS fallback.
  for(int reverse:{-8000,8000})for(bool async:{false,true})for(bool noPts:{false,true}) {
    CVideoPlayerVideo v;Parent p(v);v.m_pVideoCodec->asynchronous=async;
    p.m_CurrentAudio.syncState=p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
    p.m_clock.value=140000000;p.m_State.time_offset=7000000;
    if(noPts)v.presentedPts=DVD_NOPTS_VALUE;
    speed(p,reverse);assert(p.demux.targets.empty()&&p.audio.m_audioSink.paused);
    speed(p,0);
    assert(p.demux.targets==std::vector<double>{noPts?140000.:90000.});
    assert(p.demux.backwards==std::vector<bool>{reverse<0});
    assert(p.m_playSpeed==0&&p.audio.m_audioSink.paused&&p.audio.m_audioSink.resumes==0);
    assert(p.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_STARTING);
    finish(p,v);assert(p.audio.m_audioSink.paused&&p.completedSeeks==1);
    const double target=noPts?140000000:90000000;
    synchronize(p,v,target);assert(p.m_clock.value==target&&p.m_clock.speed==0);
    speed(p,1000);
    assert(p.demux.targets.size()==1&&p.audio.m_audioSink.resumes==1);
    assert(!p.audio.m_audioSink.paused&&p.m_clock.speed==1000);
  }
  // Ordinary pause, repeated pause, direct trickplay return and speed changes.
  for(bool pcm:{false,true}){
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);speed(p,0);finish(p,v);
    synchronize(p,v,90000000);
    p.audio.m_lavStylePcmSyncEnabled=pcm;
    p.audio.Reset();
    assert(p.audio.m_audioSink.paused&&p.audio.m_audioSink.flushes==1);
    assert(p.audio.m_pAudioCodec->resets==1&&p.audio.m_audioClock==0);
    assert(p.audio.m_syncState==IDVDStreamPlayer::SYNC_STARTING);
    if(pcm)assert(p.audio.m_pcmOutputClock==-1&&p.audio.m_pcmResyncTimestamp&&
                  p.audio.m_pcmJitterTracker.resets==1);
    speed(p,1000);assert(p.audio.m_audioSink.paused);
    synchronize(p,v,90000000);
    assert(!p.audio.m_audioSink.paused&&p.audio.m_audioSink.resumes==1);
  }
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,0);speed(p,0);speed(p,1000);
    assert(p.demux.targets.empty()&&!p.audio.m_audioSink.paused);
    speed(p,-8000);speed(p,-4000);assert(p.demux.targets.empty());
    speed(p,1000);assert(p.demux.targets==std::vector<double>{90000});finish(p,v);
  }
  // Fast pause/resume producers can queue 1x before the internal seek. A recent
  // scan must not coalesce the synchronized return, even before video INSYNC.
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);
    p.m_State.lastSeek=p.m_clock.absolute;
    p.SetPlaySpeed(0);p.SetPlaySpeed(1000);p.DrainControls();
    assert(p.demux.targets==std::vector<double>{90000});
    assert(p.ParentLifecyclePending());finish(p,v);
  }
  // The old guard still coalesces unsynchronized scans.
  {
    CVideoPlayerVideo v;Parent p(v);p.m_State.lastSeek=p.m_clock.absolute;
    CDVDMsgPlayerSeek::CMode mode;mode.trickplay=true;mode.sync=false;
    p.queue.Put(std::make_shared<CDVDMsgPlayerSeek>(mode));p.DrainControls();
    assert(p.demux.targets.empty()&&!p.ParentLifecyclePending());
  }
  // A queued scan is not a user target and cannot replace the synchronized
  // pause return. Both queue priorities use the production payload filter.
  for(int priority:{0,1}) {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);p.SetPlaySpeed(0);
    CDVDMsgPlayerSeek::CMode mode;mode.time=120000;mode.trickplay=true;mode.sync=false;
    p.queue.Put(std::make_shared<CDVDMsgPlayerSeek>(mode),priority);
    p.DrainControls();
    for(int step=0;p.ParentLifecyclePending()&&step<4;++step){finish(p,v);p.DrainControls();}
    assert(!p.ParentLifecyclePending());
    assert(p.demux.targets.back()==90000);
    assert(p.m_CurrentAudio.syncState==IDVDStreamPlayer::SYNC_STARTING);
    assert(p.m_playSpeed==0&&p.audio.m_audioSink.paused);
    speed(p,1000);assert(p.demux.targets.back()==90000);
  }
  // A queued deliberate seek wins over the internal pause target. No second
  // return seek is manufactured by resume after the synchronized user seek.
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);
    p.SetPlaySpeed(0);p.queue.Put(userSeek(60000));p.DrainControls();
    assert(p.demux.targets==std::vector<double>{60000});finish(p,v);
    speed(p,1000);assert(p.demux.targets.size()==1);
  }
  // A deliberate request arriving after the pause seek waits for its receipt,
  // then replaces that position. Resume must wait for the new receipt too.
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);speed(p,0);
    p.queue.Put(userSeek(60000));p.SetPlaySpeed(1000);p.DrainControls();
    assert(p.demux.targets==std::vector<double>{90000});finish(p,v);
    p.DrainControls();assert((p.demux.targets==std::vector<double>{90000,60000}));
    assert(p.m_playSpeed==0);finish(p,v);p.DrainControls();
    assert(p.m_playSpeed==1000&&p.demux.targets.size()==2);
  }
  // Resume while the seek is pending is fenced, then normal speed in STARTING
  // cannot resume old packets. The production resync gate releases fresh audio.
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);speed(p,0);
    p.SetPlaySpeed(1000);p.DrainControls();
    assert(p.m_playSpeed==0&&p.audio.m_audioSink.resumes==0);finish(p,v);
    p.DrainControls();assert(p.m_playSpeed==1000&&p.audio.m_audioSink.paused);
    assert(p.audio.m_audioSink.resumes==0);
    synchronize(p,v,90000000);
    assert(!p.audio.m_audioSink.paused&&p.audio.m_audioSink.resumes==1);
  }
  // Demux rejection/nonseekable input needs no flush; resume is not stranded.
  for(bool nonseekable:{false,true}) {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);
    p.m_State.canseek=!nonseekable;p.demux.result=Parent::Demux::REJECTED;
    speed(p,0);assert(!p.ParentLifecyclePending());
    speed(p,1000);assert(!p.audio.m_audioSink.paused);
    assert(p.demux.targets.size()==(nonseekable?0u:1u));
  }
  // Changed-state failures retain the existing synchronized fallback flush.
  {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);p.demux.result=Parent::Demux::FAILED;
    speed(p,0);finish(p,v);assert(p.audio.m_audioSink.paused);
    synchronize(p,v,90000000);speed(p,1000);assert(!p.audio.m_audioSink.paused);
  }
  // Pending receipt fences resume; cancellation cannot run a stale callback.
  for(bool stop:{false,true}) {
    CVideoPlayerVideo v;Parent p(v);speed(p,-8000);speed(p,0);p.SetPlaySpeed(1000);
    p.DrainControls();assert(p.m_playSpeed==0&&p.audio.m_audioSink.paused);
    if(stop)p.m_bStop=true;else v.RetireSession();
    assert(!p.ContinueParentLifecycle()&&!p.ParentLifecyclePending()&&p.completedSeeks==0);
    // Reopen uses fresh queue/player state: no deferred resume or seek debt.
    CVideoPlayerVideo next;Parent reopened(next);speed(reopened,0);speed(reopened,1000);
    assert(reopened.demux.targets.empty()&&!reopened.audio.m_audioSink.paused);
  }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    source = harness(args.root)
    with tempfile.TemporaryDirectory(prefix="paused-trickplay-return-") as temp:
        directory = Path(temp)
        f.run(source, directory, "paused-trickplay-return")
        if args.negative_controls:
            for label, old, new in (
                ("original-paused-return", "speed == DVD_PLAYSPEED_NORMAL || speed == DVD_PLAYSPEED_PAUSE", "speed == DVD_PLAYSPEED_NORMAL"),
                ("coalesce-synchronized-return", "msg.GetTrickPlay() && !msg.GetSync() && !msg.IsVideoRecovery()", "msg.GetTrickPlay() && !msg.IsVideoRecovery()"),
                ("overwrite-user-seek", "!GetVideoSeekQueueState().HasQueuedUserSeek()", "true"),
                ("resume-before-flush-receipt", "(m_waitingForVideoFlush || ParentLifecyclePending()) ? 2 : 0", "0"),
                ("scan-masquerades-as-user-seek", "!std::static_pointer_cast<CDVDMsgPlayerSeek>(item.message)->GetTrickPlay()", "true"),
                ("paused-audio-reset-keeps-stale-sync", "audioframe.nb_frames = 0;\n      m_syncState = IDVDStreamPlayer::SYNC_STARTING;",
                 "audioframe.nb_frames = 0;\n      m_syncState = IDVDStreamPlayer::SYNC_INSYNC;"),
            ):
                mutant = replace(source, old, new)
                result = f.run(mutant, directory, label, expect_failure=True)
                assert "Assertion" in result.stderr, result.stderr


if __name__ == "__main__":
    main()
