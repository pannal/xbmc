#!/usr/bin/env python3
"""Host regression of the production arrow-preview/seek/GUI handoff.

Runs SeekHandler, the complete VideoPlayer time-seek branch, real queue and
flush continuations, the resynchronized state-publication tail, DataCache
target accessors and the GUI percentage calculation. Demux/codec receipts,
clock, input and GUI services are controlled adapters, not device playback.
Requires Python 3 and g++; --negative-controls must compile and then fail.
"""
import argparse
import importlib.util
from pathlib import Path
import runpy
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lifecycle", ROOT / "tools/test-player-lifecycle.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def replace(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


def harness():
    parent = (ROOT / "xbmc/cores/VideoPlayer/VideoPlayer.cpp").read_text()
    messages = (ROOT / "xbmc/cores/VideoPlayer/DVDMessage.h").read_text()
    cache = (ROOT / "xbmc/cores/DataCacheCore.cpp").read_text()
    handler = (ROOT / "xbmc/SeekHandler.cpp").read_text()
    header = (ROOT / "xbmc/SeekHandler.h").read_text()
    gui = (ROOT / "xbmc/guilib/guiinfo/PlayerGUIInfo.cpp").read_text()
    dialog = (ROOT / "xbmc/dialogs/GUIDialogSeekBar.cpp").read_text()
    source = f.harness(ROOT).split("static std::shared_ptr<CDVDMsg> message(")[0]
    source = replace(source, "using CCriticalSection = std::mutex;",
                     "using CCriticalSection = std::recursive_mutex;")
    cache_methods = "\n".join(f.block(cache, name) for name in (
        "void CDataCacheCore::SetSeekTarget(", "std::optional<int64_t> CDataCacheCore::GetSeekTarget()",
        "int64_t CDataCacheCore::GetSeekTargetOffset()", "void CDataCacheCore::SetPlayTimes(",
        "int64_t CDataCacheCore::GetPlayTime()", "bool CDataCacheCore::IsSeeking()"))
    source = replace(source, "struct CServiceBroker {", CACHE + """
struct CServiceBroker {
  static CDataCacheCore& GetDataCacheCore(){return cache;}
  static Components& GetAppComponents(){static Components c;return c;}
  static GUI* GetGUI();
  static Window* GetWinSystem(){static Window w;return &w;}
""")
    source = replace(source, "struct Parent {", cache_methods + INPUT + "\nstruct Parent {")
    source = replace(source, "PLAYER_SEEK, PLAYER_VIDEO_RECOVERY",
                     "PLAYER_SEEK, PLAYER_SEEK_CHAPTER, PLAYER_VIDEO_RECOVERY")
    if "class CDVDMsgPlayerSeek :" not in source:
        source = replace(source, "struct CVideoPlayerAudio {",
                         f.block(messages, "class CDVDMsgPlayerSeek :") + ";\nstruct CVideoPlayerAudio {")
    source = replace(source, "  struct ProcessInfo { double", """
  struct ProcessInfo {
    void SetStateSeeking(bool active) {CServiceBroker::GetDataCacheCore().m_stateInfo.m_stateSeeking=active;}
    void SeekFinished(int64_t) {}
    double""")
    source = replace(source, "  struct Input {bool realtime=false; bool IsRealtime() const {return realtime;} } input;\n  Input* m_pInputStream=&input;",
                     "  std::shared_ptr<CDVDInputStream> m_pInputStream=std::make_shared<CDVDInputStream>();")
    source = replace(source, "  struct Demux {int speed=0; void SetSpeed(int v) {speed=v;} } demux;\n  Demux* m_pDemuxer=&demux; bool m_pSubtitleDemuxer=false;",
                     DEMUX)
    source = replace(source, "    double GetClock() const {return value;}", """
    double absolute=6000000;
    double GetAbsoluteClock() const {return absolute;}
    double GetClock() const {return value;}""")
    source = replace(source, "  struct {bool streamsReady=false; double time_offset=0;} m_State;", """
  struct State {
    bool streamsReady=false,canseek=true; int chapter=0;
    double time_offset=0,lastSeek=2000000,timestamp=0,time=100000,dts=0,timeMin=0,timeMax=1000000;
  } m_State;""")
    source = replace(source, "struct IPlayerCallback { int starts=0;",
                     "struct IPlayerCallback {void OnPlayBackSeek(int64_t,int64_t) {} int starts=0;")
    source = replace(source, "  void ClearSeekTarget() {}", "  void ClearSeekTarget();")
    source = replace(source, "  CDVDMessageQueue queue;", PARENT_FIELDS + "\n  CDVDMessageQueue queue;")
    source += "\n".join(f.block(parent, name).replace("CVideoPlayer::", "Parent::") for name in (
        "void CVideoPlayer::QueueTimeSeek(", "void CVideoPlayer::ClearSeekTarget(",
        "void CVideoPlayer::ClearRejectedLargeSkip(", "bool CVideoPlayer::SeekTimeRelative(int64_t iTime)",
        "bool CVideoPlayer::SeekTimeRelative(int64_t iTime,",
        "CVideoSeekQueueState CVideoPlayer::GetVideoSeekQueueState()",
        "void CVideoPlayer::FinishSeek("))
    # Keep exactly the production locked state/intent publication. Earlier
    # UpdatePlayState providers are replaced by explicit clock samples below.
    update = f.block(parent, "void CVideoPlayer::UpdatePlayState(")
    tail = update[update.rindex("  std::unique_lock<CCriticalSection> lock(m_StateSection);"):]
    source += "\nvoid Parent::Publish(State state) {\n" + tail
    seek = f.body(parent, "else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK) ||")
    source += """
void Parent::DrainControls() {
  std::shared_ptr<CDVDMsg> pMsg; int lifecyclePriority=0;
  @LOOP@ {
    lifecyclePriority=0;
    if(pMsg->IsType(CDVDMsg::PLAYER_ABORT)){m_bAbortRequest=true;break;}
    if(pMsg->IsType(CDVDMsg::PLAYER_SEEK)){@SEEK@}
  }
}
""".replace("@LOOP@", parent[parent.index("  while (m_messenger.Get(pMsg,"):
                                       parent.index("\n  {", parent.index("  while (m_messenger.Get(pMsg,"))]) \
       .replace("@SEEK@", seek)
    handler_class = f.block(header, "class CSeekHandler :").replace("private:", "public:") + ";"
    source += GUI_ADAPTERS.replace("@HANDLER@", handler_class)
    source += "\n".join(f.block(handler, name) for name in (
        "CSeekHandler::~CSeekHandler()", "void CSeekHandler::Reset()",
        "int CSeekHandler::GetSeekStepSize(", "void CSeekHandler::Seek(bool",
        "void CSeekHandler::SeekSeconds(", "int CSeekHandler::GetSeekSize()",
        "double CSeekHandler::GetSeekTime()",
        "void CSeekHandler::SetSeekSize(", "bool CSeekHandler::InProgress()",
        "void CSeekHandler::FrameMove()"))
    source += "\n" + f.block(gui, "float CPlayerGUIInfo::GetSeekPercent()")
    source += "\n" + f.block(dialog, "int CGUIDialogSeekBar::GetProgress() const")
    return source + TESTS


CACHE = r"""
#include <map>
#include <optional>
#include <cmath>
#include <type_traits>
struct CDataCacheCore {
  mutable CCriticalSection m_stateSection;
  struct {std::optional<int64_t> m_seekTarget;bool m_stateSeeking=false;} m_stateInfo;
  struct {time_t m_startTime=0;int64_t m_time=100000,m_timeMin=0,m_timeMax=1000000;} m_timeInfo;
  void SetSeekTarget(std::optional<int64_t>);
  std::optional<int64_t> GetSeekTarget() const;
  int64_t GetSeekTargetOffset() const;
  void SetPlayTimes(time_t,int64_t,int64_t,int64_t);
  int64_t GetPlayTime();bool IsSeeking();
};
static CDataCacheCore cache;
struct CApplicationPlayer;
struct Components {template<class T> std::shared_ptr<T> GetComponent() const;};
struct GUI;
struct Window {struct Gfx {float GetFPS(){return 60;}} gfx;Gfx& GetGfxContext(){return gfx;}};
"""

INPUT = r"""
using ECacheState=int;
constexpr int DVDSTREAM_TYPE_DVD=1,DVDSTATE_SEEK=1;
constexpr double DVD_MSEC_TO_TIME(double time){return time*1000;}
struct CDVDInputStream {
  struct IMenus {virtual ~IMenus()=default;virtual bool IsTimeSearchAllowed(){return false;}};
  virtual ~CDVDInputStream()=default;
  bool IsStreamType(int){return false;}bool IsRealtime(){return false;}
  void* GetIPosTime(){return nullptr;}
};
"""

DEMUX = r"""
  struct Demux {
    enum Result {SUCCESS,REJECTED,FAILED}; Result result=SUCCESS,lastResult=SUCCESS;
    std::function<void()> duringSeek;
    std::vector<double> targets;
    void SetSpeed(int) {}
    bool SeekTime(double time,bool,double* start){
      targets.push_back(time);*start=time*1000;lastResult=result;const auto outcome=result;
      if(auto run=std::exchange(duringSeek,{}))run();
      return outcome==SUCCESS;
    }
    bool WasSeekRejectedWithoutChange(){return lastResult==REJECTED;}
  } demux;
  Demux* m_pDemuxer=&demux;
  struct SubtitleDemux {bool SeekTime(double,bool){return true;}};
  SubtitleDemux* m_pSubtitleDemuxer=nullptr;
"""

PARENT_FIELDS = r"""
  CCriticalSection m_StateSection;
  CDVDMessageQueue& m_messenger=queue;
  std::shared_ptr<CDVDMsgPlayerSeek> m_pendingLargeSkip;
  std::atomic<int> m_chapterSeekTarget{0};
  bool m_waitingForVideoFlush=false,m_subtitleSeekNewRun=false,playerThread=true;
  int m_caching=CACHESTATE_DONE,completedSeeks=0,m_streamPlayerSpeed=1000;
  struct {int state=0;} m_dvd;
  double m_demuxSeekBasePts=DVD_NOPTS_VALUE;
  struct EDL {
    bool cuts=false;bool HasCuts(){return cuts;}
    double GetTimeAfterRestoringCuts(double t){return cuts?t+10000:t;}
    int64_t GetTimeWithoutCuts(int64_t t){return cuts?t-10000:t;}
  } m_Edl;
  bool IsCurrentThread()const{return playerThread;}
  bool IsInMenuInternal(){return false;}
  int64_t GetTime(){return std::llround(m_State.time);}
  void SeekTime(int64_t time){CDVDMsgPlayerSeek::CMode m;m.time=time;QueueTimeSeek(m);}
  void SetPlaySpeed(int speed){m_playSpeed=speed;}
  void RecallSubtitlesAfterSeek(double,double) {}
  void FinishSeek(bool);
  void QueueTimeSeek(const CDVDMsgPlayerSeek::CMode&,bool=false);
  void ClearRejectedLargeSkip(const CDVDMsg*);
  bool SeekTimeRelative(int64_t);
  bool SeekTimeRelative(int64_t,std::optional<int64_t>);
  CVideoSeekQueueState GetVideoSeekQueueState();
  std::chrono::milliseconds GetVideoRecoveryPosition()const{return 0ms;}
  CVideoRecoveryGate::Conditions GetVideoRecoveryConditions(uint64_t)const{return {};}
  void DrainControls();void Publish(State);
  void Sample(int64_t time,bool synchronized) {
    m_CurrentVideo.syncState=m_CurrentAudio.syncState=
      synchronized?IDVDStreamPlayer::SYNC_INSYNC:IDVDStreamPlayer::SYNC_STARTING;
    m_caching=CACHESTATE_DONE;
    State next=m_State;next.time=time;
    cache.SetPlayTimes(0,time,0,1000000);Publish(next);
  }
"""

GUI_ADAPTERS = r"""
struct IntegerSettingOption;struct CSetting;struct CAction;
struct ISettingCallback {virtual ~ISettingCallback()=default;
  virtual void OnSettingChanged(const std::shared_ptr<const CSetting>&)=0;};
namespace KODI::ACTION {struct IActionListener {virtual ~IActionListener()=default;
  virtual bool OnAction(const CAction&)=0;};}
struct CStopWatch {double elapsed=0;void StartZero(){elapsed=0;}
  double GetElapsedMilliseconds()const{return elapsed;}};
enum SeekType {SEEK_TYPE_VIDEO,SEEK_TYPE_MUSIC};
@HANDLER@
void CSeekHandler::OnSettingChanged(const std::shared_ptr<const CSetting>&) {}
bool CSeekHandler::OnAction(const CAction&) {return false;}
struct CApplicationPlayer {
  Parent* player=nullptr;
  CSeekHandler handler;
  int64_t GetTime(){return cache.GetPlayTime();}
  int64_t GetMinTime(){return 0;}int64_t GetMaxTime(){return 1000000;}
  void SeekTimeRelative(int64_t time){player->SeekTimeRelative(time);}
  void SeekTimeRelative(int64_t time,std::optional<int64_t> origin){player->SeekTimeRelative(time,origin);}
  void SeekTime(int64_t time){player->SeekTime(time);}
  CSeekHandler& GetSeekHandler(){return handler;}
};
static auto app=std::make_shared<CApplicationPlayer>();
struct CApplicationStackHelper {
  uint64_t start=0;bool regular=false;
  bool IsPlayingRegularStack(){return regular;}
  uint64_t GetCurrentStackPartStartTimeMs(){return start;}
};
static auto stack=std::make_shared<CApplicationStackHelper>();
template<class T> std::shared_ptr<T> Components::GetComponent()const{
  if constexpr(std::is_same_v<T,CApplicationPlayer>)return app;
  else return stack;
}
static struct {
  double GetTime(){return (cache.GetPlayTime()+(stack->regular?stack->start:0))/1000.0;}
  double GetTotalTime(){return 1000;}
} g_application;
namespace MathUtils {int round_int(double value){return std::lround(value);}}
constexpr int GUI_MSG_NOTIFY_ALL=1,GUI_MSG_STATE_CHANGED=2;
struct CPlayerGUIInfo {
  std::shared_ptr<CApplicationPlayer> m_appPlayer=app;
  int GetTotalPlayTime()const{return 1000;}
  int GetPlayTime()const{return std::lrint(g_application.GetTime());}
  float GetSeekPercent()const;
};
constexpr int PLAYER_SEEKBAR=1,PLAYER_PROGRESS=2;
namespace INFO {constexpr int DEFAULT_CONTEXT=0;}
struct CGUIInfoManager {
  void GetInt(int& value,int type,int)const{
    value=type==PLAYER_SEEKBAR?std::lrintf(CPlayerGUIInfo{}.GetSeekPercent()):
                             std::lrint(g_application.GetTime()/10);
  }
};
struct GUI {
  struct Manager {void SendMessage(int,int,int,int) {}};
  Manager& GetWindowManager(){static Manager m;return m;}
  const CGUIInfoManager& GetInfoManager(){static CGUIInfoManager i;return i;}
};
GUI* CServiceBroker::GetGUI(){static GUI gui;return &gui;}
struct CGUIDialogSeekBar {int GetProgress()const;};
"""

TESTS = r"""
struct Trial {
  CVideoPlayerVideo video;Parent player;
  Trial():player(video) {
    cache.m_stateInfo={};cache.SetPlayTimes(0,100000,0,1000000);
    app->player=&player;app->handler.Reset();
    stack->regular=false;stack->start=0;
    app->handler.m_seekDelays[SEEK_TYPE_VIDEO]=500;
    app->handler.m_forwardSeekSteps[SEEK_TYPE_VIDEO]={10,30,60};
    app->handler.m_backwardSeekSteps[SEEK_TYPE_VIDEO]={-10,-30,-60};
    player.m_clock.value=100000000;player.Sample(100000,true);
  }
  void Arrow(bool forward){app->handler.Seek(forward,1);}
  void Commit(){app->handler.m_timer.elapsed=500;app->handler.FrameMove();}
  void Marker(int expected) {
    assert(CGUIDialogSeekBar{}.GetProgress()==expected && "timeline_marker_target");
  }
  void FinishFlush() {
    assert(player.ParentLifecyclePending());
    for(int i=0;video.IsFlushPending()&&i<8;++i){video.Step();video.m_pVideoCodec->ready=true;}
    assert(player.ContinueParentLifecycle());
  }
};
int main() {
  // Dispatch, demux ack and flush ack all precede stream/clock resynchronization.
  // Every GUI sample must retain 130s, never the old 100s (or 230s double-offset).
  for(int speed:{0,1000})for(bool forward:{false,true}) {
    Trial t;t.player.m_playSpeed=speed;
    t.Arrow(forward);t.Arrow(forward);const int target=forward?13:7;t.Marker(target);
    t.Commit();t.Marker(target);assert(app->handler.InProgress());
    cache.SetPlayTimes(0,100795,0,1000000);
    assert(std::abs(app->handler.GetSeekTime()-target*10.0)<0.000001);
    assert(std::abs(CPlayerGUIInfo{}.GetSeekPercent()-target)<0.000001);
    cache.SetPlayTimes(0,100000,0,1000000);
    t.player.DrainControls();t.Marker(target);
    t.player.Sample(100000,false);t.Marker(target);
    t.FinishFlush();t.Marker(target);
    assert(!cache.IsSeeking() && app->handler.InProgress());
    t.player.Sample(100000,false);t.Marker(target);
    // Audio/video can finish separately; neither old-clock sample retires intent.
    t.player.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
    t.player.Publish(t.player.m_State);t.Marker(target);
    // Land at a different keyframe. Actual completion must be truthful, not
    // wait for target-nearness or stick at the requested time indefinitely.
    t.player.Sample(forward?120000:60000,true);t.Marker(forward?12:6);
    assert(!cache.GetSeekTarget() && !app->handler.InProgress());
    assert(cache.GetPlayTime()==(forward?120000:60000));
  }
  // Latest user intent wins while the old clock and first receipt are pending.
  for(bool secondForward:{false,true}) {
    Trial t;t.Arrow(true);t.Arrow(true);t.Commit();t.player.DrainControls();
    t.Arrow(secondForward);t.Marker(secondForward?14:12);t.Commit();t.Marker(secondForward?14:12);
    t.FinishFlush();t.Marker(secondForward?14:12);
    t.player.Sample(130000,true);t.Marker(secondForward?14:12); // newer request still queued
    t.player.m_clock.value=130000000;t.player.DrainControls();
    assert(t.player.demux.targets.back()==(secondForward?140000:120000));
    t.FinishFlush();t.player.Sample(secondForward?140000:120000,true);
    t.Marker(secondForward?14:12);assert(!cache.GetSeekTarget());
  }
  // Coalescing several queued commits must still add to the displayed target,
  // even though the old playback clock has never moved.
  {Trial t;for(int i=0;i<3;++i){t.Arrow(true);t.Commit();}
   t.Marker(13);t.player.DrainControls();assert(t.player.demux.targets==std::vector<double>{130000});
   t.FinishFlush();t.player.Sample(130000,true);t.Marker(13);}
  {Trial t;t.Arrow(true);t.Arrow(true);t.Arrow(false);t.Marker(11);
   t.Arrow(false);t.Commit();t.Marker(10);assert(!cache.GetSeekTarget());}
  {Trial t;app->handler.m_seekDelays[SEEK_TYPE_VIDEO]=0;
   t.Arrow(true);t.Marker(11);t.Arrow(true);t.Marker(12);t.Arrow(false);t.Marker(11);
   t.player.DrainControls();assert(t.player.demux.targets==std::vector<double>{110000});}
  {Trial t;t.Arrow(true);t.Commit();t.player.SeekTime(700000);t.Marker(70);
   t.player.DrainControls();assert(t.player.demux.targets==std::vector<double>{700000});}
  // Reversing an uncommitted preview cancels only that preview, not an earlier
  // dispatched seek. Completion can also occur during a fresh preview.
  {Trial t;t.Arrow(true);t.Commit();t.Arrow(true);t.Arrow(false);t.Marker(11);
   t.player.DrainControls();t.FinishFlush();t.Arrow(true);
   t.player.Sample(110000,true);t.Marker(12);t.Commit();
   t.player.m_clock.value=110000000;t.player.DrainControls();
   assert(t.player.demux.targets.back()==120000);}
  // Rejected/failed/no-demux seeks release the target, but an older rejection
  // must not erase a newer seek admitted inside the demux operation.
  for(int result:{1,2,3}) {
    Trial t;t.Arrow(true);t.Commit();
    if(result==3)t.player.m_pDemuxer=nullptr;
    else t.player.demux.result=static_cast<Parent::Demux::Result>(result);
    t.player.DrainControls();t.Marker(10);assert(!cache.GetSeekTarget());
  }
  {Trial t;t.Arrow(true);t.Commit();t.player.m_State.canseek=false;
   t.player.DrainControls();t.Marker(10);assert(!cache.GetSeekTarget());}
  {Trial t;t.player.demux.result=Parent::Demux::REJECTED;t.Arrow(true);t.Commit();
   t.player.demux.duringSeek=[&]{t.player.SeekTimeRelative(10000);t.player.demux.result=Parent::Demux::SUCCESS;};
   t.player.DrainControls();t.Marker(12);assert(cache.GetSeekTarget()==120000);}
  // Queue admission failure does not invent an accepted target.
  {Trial t;t.player.queue.m_bInitialized=false;t.Arrow(true);t.Commit();
   t.Marker(10);assert(!cache.GetSeekTarget());}
  // Cancellation (including a cancelled device receipt) and a fresh item use
  // the existing lifecycle invalidation, not an arbitrary GUI hold timeout.
  {Trial t;t.Arrow(true);t.Commit();t.player.DrainControls();
   t.video.GetFlushRequest()->state=CVideoFlushRequest::State::CANCELLED;
   assert(!t.player.ContinueParentLifecycle());t.Marker(10);assert(!cache.GetSeekTarget());}
  {Trial t;t.Arrow(true);t.Commit();t.player.CancelParentLifecycle();
   t.player.Sample(400000,true);t.Marker(40);assert(!cache.GetSeekTarget());}
  // Clamp fresh previews against the pending destination, not the stale clock.
  {Trial t;t.player.SeekTime(995000);t.Arrow(true);assert(app->handler.m_seekSize==5);t.Marker(100);}
  {Trial t;t.player.SeekTime(5000);t.Arrow(false);assert(app->handler.m_seekSize==-5);t.Marker(0);}
  // EDL targets stay in display time until the existing demux conversion.
  {Trial t;t.player.m_Edl.cuts=true;t.Arrow(true);t.Commit();t.Arrow(true);t.Commit();
   t.Marker(12);t.player.DrainControls();assert(t.player.demux.targets.back()==130000);}
  {Trial t;t.player.m_Edl.cuts=true;CDVDMsgPlayerSeek::CMode m;m.time=130000;m.restore=false;
   t.player.QueueTimeSeek(m);t.Marker(12);t.Arrow(true);t.Commit();t.Marker(13);
   t.player.DrainControls();assert(t.player.demux.targets.back()==140000);}
  // Reading from a non-player thread cannot retire intent.
  {Trial t;t.Arrow(true);t.Commit();t.player.playerThread=false;
   t.player.Sample(100000,true);t.Marker(11);assert(cache.GetSeekTarget());}
  {Trial t;stack->regular=true;stack->start=300000;t.Arrow(true);t.Arrow(true);t.Commit();
   assert(app->handler.GetSeekTime()==430.0);t.Marker(43);}
}
"""

def application_harness():
    retirement = runpy.run_path(str(ROOT / "tools/test-player-retirement.py"))
    application = (ROOT / "xbmc/application/ApplicationPlayer.cpp").read_text()
    handler = (ROOT / "xbmc/SeekHandler.cpp").read_text()
    code = retirement["PRELUDE"].replace(
        "  struct {void Reset() {}} m_seekHandler;", """
  struct Preview {
    bool m_requireSeek=true,m_analogSeek=true;
    int m_seekStep=2,m_seekSize=30,m_timeCodePosition=3;
    std::optional<int64_t> m_seekBase;
    void Reset();
  } m_seekHandler;""")
    code += f.block(handler, "void CSeekHandler::Reset()").replace(
        "CSeekHandler::", "CApplicationPlayer::Preview::")
    code += "\n".join(retirement["function"](application, name) for name in (
        "std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()",
        "void CApplicationPlayer::ContinueClose()", "bool CApplicationPlayer::ClosePlayer(",
        "void CApplicationPlayer::ResetPlayer(", "bool CApplicationPlayer::OpenFile(",
        "void CApplicationPlayer::Supersede("))
    return code + r"""
int main() {
  CFileItem file;CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  for(bool samePlayer:{false,true}) {
    CApplicationPlayer app;
    if(samePlayer){app.m_pPlayer=std::make_shared<IPlayer>();app.m_pPlayer->playing=true;}
    assert(app.m_seekHandler.m_requireSeek);
    assert(app.OpenFile(file,options,factory,samePlayer?"old":"new",callback));
    assert(!app.m_seekHandler.m_requireSeek && app.m_seekHandler.m_seekSize==0 &&
           app.m_seekHandler.m_timeCodePosition==0);
  }
  {CApplicationPlayer app;app.m_pPlayer=std::make_shared<IPlayer>();
   assert(!app.ClosePlayer());
   assert(!app.m_seekHandler.m_requireSeek && app.m_seekHandler.m_seekSize==0);}
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    source = harness()
    with tempfile.TemporaryDirectory(prefix="seek-marker-test-") as directory:
        output = Path(directory)
        f.run(source, output, "seek-marker")
        f.run(application_harness(), output, "seek-preview-item-stop-reset")
        if args.negative_controls:
            variants = {
                "original-preview-reset": (
                    f.block(source, "double CSeekHandler::GetSeekTime()"),
                    "double CSeekHandler::GetSeekTime() const {return g_application.GetTime() + m_seekSize;}"),
                "release-at-flush-ack": (
                    "void Parent::FinishSeek(bool trickplay)\n{",
                    "void Parent::FinishSeek(bool trickplay)\n{\n  ClearSeekTarget();"),
                "old-clock-new-seek": (
                    "pendingTarget.value_or(GetTime()) + iTime", "GetTime() + iTime"),
            }
            for name, (old, new) in variants.items():
                result = f.run(replace(source, old, new), output, name, expect_failure=True)
                assert "Assertion" in result.stderr, result.stderr
    print("Seek timeline marker: PASS (production paths, ASan/UBSan; host adapters)")


if __name__ == "__main__":
    main()
