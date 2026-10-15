#!/usr/bin/env python3
"""Exercise actual detector completion, cache-throttle and startup hold methods.

Real native worker threads/joins, source identities and production pending APIs;
recording player/clock/notification boundaries for the extracted parent methods.
FFmpeg/VFS, actual audio/video output and device GUI remain separate verification.
"""
import argparse
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
NATIVE = runpy.run_path(str(ROOT / 'tools/test-aml-native-workers.py'))
PARENT = runpy.run_path(str(ROOT / 'tools/test-player-lifecycle.py'))
function, run = NATIVE['function'], NATIVE['run']

NATIVE_TEST = r'''
int main() {
 ready(); CAMLSession sessionValue; init(sessionValue);
 settingsValue.wait=true;
 // Independently enabled detector modes select the truthful toast title.
 for(bool nativeDV:{false,true}){
  select("modes",nativeDV);const auto selected=aml_subtitle_active_area_source();
  for(bool subtitle:{false,true})for(bool dv:{false,true}){
   settingsValue.detect=subtitle;dvDetection=dv;
   assert(aml_subtitle_active_area_startup_modes(selected)==((subtitle?1:0)|(nativeDV&&dv?2:0)));
   assert(aml_subtitle_active_area_startup_modes({})==0);
   assert(aml_subtitle_active_area_startup_modes(std::make_shared<int>(1))==0);
  }
 }
 settingsValue.detect=true;dvDetection=true;
 // Admission is part of startup even before a backend can start its worker.
 aml_dv_detect_set_file("deferred");aml_subtitle_active_area_startup_begin();
 auto deferred=aml_subtitle_active_area_source(0,173);int checked,total;
 assert(aml_subtitle_active_area_startup_progress(deferred,checked,total)&&total==0);
 aml_subtitle_active_area_startup_admitted(std::make_shared<int>(1));
 assert(aml_subtitle_active_area_startup_pending(deferred));
 aml_subtitle_active_area_startup_admitted(deferred);
 assert(!aml_subtitle_active_area_startup_pending(deferred));
 settingsValue.detect=false;settingsValue.dvRequested=false;
 aml_subtitle_active_area_startup_begin();assert(!aml_subtitle_active_area_startup_pending(deferred));
 settingsValue.detect=true;settingsValue.dvRequested=true;
 for (int outcome=0; outcome<3; ++outcome) {
  select("startup",false);aml_subtitle_active_area_startup_begin();
  auto selected=aml_subtitle_active_area_source();
  aml_subtitle_active_area_startup_set_held(selected,true);
  assert(aml_subtitle_active_area_startup_held(selected));
  std::promise<void> entered,finish;auto gate=finish.get_future();
  scanHook=[&](const auto& source,const CAMLNativeWorker::Run& state){
   assert(detect_startup_wait_active(state));
   assert(detect_wait_for_cache(state,80,0)==100); // fake player cache is only10%
   s_detectThrottleActive=true;s_detectLastCacheCheckMs=0;
   assert(detect_interrupt_cb(const_cast<CAMLNativeWorker::Run*>(&state))==0);
   assert(!s_detectCacheStarved);
   for(int position=1;position<=3;++position){
    DetectPositionProgress progress{source->completedPositions,position};
   }
   entered.set_value();gate.wait();
   if(outcome==1)throw std::runtime_error("scanner exception");
   s_detectState=outcome==2?DV_DETECT_SKIPPED:DV_DETECT_OK;
  };
  aml_dv_detect_active_area_start();entered.get_future().wait();
  assert(aml_subtitle_active_area_startup_pending(selected));
  int completed,total;
  assert(aml_subtitle_active_area_startup_progress(selected,completed,total));
  assert(completed==3&&total==14);
  assert(!aml_subtitle_active_area_startup_pending(std::make_shared<int>(1)));
  finish.set_value();s_detectWorker.Stop();
  assert(!aml_subtitle_active_area_startup_pending(selected));
 }
 // Source cancellation releases the hold before a blocked worker is joined.
 select("cancel",false);aml_subtitle_active_area_startup_begin();
 auto old=aml_subtitle_active_area_source();
 std::promise<void> entered;
 scanHook=[&](const auto&,const CAMLNativeWorker::Run& state){
  entered.set_value();assert(!state.WaitFor(30s));
 };
 aml_dv_detect_active_area_start();entered.get_future().wait();
 assert(aml_subtitle_active_area_startup_pending(old));
 aml_subtitle_active_area_invalidate();
 assert(!aml_subtitle_active_area_startup_pending(old));
 s_detectWorker.Stop();
 // Changing files/stream configuration cannot reuse an old wait/completion.
 select("replacement",false);aml_subtitle_active_area_startup_begin();
 auto current=aml_subtitle_active_area_source();s_detectSource->pending=true;
 aml_subtitle_active_area_startup_complete(old);
 assert(aml_subtitle_active_area_startup_pending(current));
 const auto selection=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,selection));
 s_detectSource->pending=true;
 assert(aml_subtitle_active_area_startup_pending(selection));
 aml_subtitle_active_area_startup_complete(selection);
 assert(!aml_subtitle_active_area_startup_pending(selection));
 s_detectSource->pending=false; // end the manually controlled source-identity case
 // A requested startup wait alone never bypasses playback I/O protection.
 select("not-held-yet",false);aml_subtitle_active_area_startup_begin();
 assert(!aml_subtitle_active_area_startup_held(aml_subtitle_active_area_source()));
 s_detectSource->pending=true; // worker admitted, parent has not paused yet
 CAMLNativeWorker::Run notHeld(s_detectSource->superseded);
 assert(!detect_startup_wait_active(notHeld));
 assert(detect_wait_for_cache(notHeld,80,0)==10);s_detectSource->pending=false;
 // Normal background scans keep both I/O guards. Disabled wait does not hold.
 settingsValue.wait=false;aml_subtitle_active_area_startup_begin();
 CAMLNativeWorker::Run state(s_detectSource->superseded);
 assert(!aml_subtitle_active_area_startup_pending(selection));
 assert(detect_wait_for_cache(state,80,0)==10);
 s_detectThrottleActive=true;s_detectLastCacheCheckMs=0;s_detectCacheStarved=false;
 assert(detect_interrupt_cb(&state)==1&&s_detectCacheStarved);
 // Audio-only AVStarted retires the file selection before late video opens.
 settingsValue.wait=true;select("audio-then-video",false);
 aml_subtitle_active_area_startup_begin();
 aml_subtitle_active_area_startup_complete(aml_subtitle_active_area_source());
 const auto late=aml_subtitle_active_area_source(4,173);
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,late));
 s_detectSource->pending=true;CAMLNativeWorker::Run lateRun(s_detectSource->superseded);
 assert(!aml_subtitle_active_area_startup_pending(late));
 assert(!detect_startup_wait_active(lateRun));
 assert(detect_wait_for_cache(lateRun,80,0)==10);s_detectSource->pending=false;
 // Synchronous ineligibility and a failed CPU selection are terminal too.
 settingsValue.detect=false;dvDetection=false;aml_dv_detect_active_area_start();
 assert(!s_detectSource->pending);settingsValue.detect=true;dvDetection=true;
 std::cout<<"PASS: actual async completion/exception, cancellation, selection, startup-only throttle bypass and disabled/early return\n";
}
'''

PLAYER_TEST = r'''
int main(){
 CVideoPlayerVideo video;Parent p(video);
 p.m_CurrentAudio.id=-1;p.m_CurrentVideo.id=0;
 p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_WAITSYNC;
 p.m_CurrentVideo.starttime=1000000;p.m_CurrentVideo.packets=1;
 p.m_caching=CACHESTATE_INIT;startupPending=true;
 // Decode must first report a picture and finish initial synchronization.
 assert(!p.WaitForActiveAreaDetection()&&!heldStatus);
 SStartMsg started{};started.player=VideoPlayer_VIDEO;started.epoch=video.GetSyncEpoch();
 started.timestamp=1000000;
 p.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,started));
 p.HandleMessages();assert(p.m_activeAreaVideoReady);
 p.SynchronizeStreams(false);
 assert(p.m_State.streamsReady&&p.m_callback.starts==1&&p.m_clock.anchors==1);
 assert(p.m_CurrentVideo.syncState==IDVDStreamPlayer::SYNC_INSYNC);
 p.HoldStep();assert(heldStatus&&p.m_clock.speed==0&&video.appliedSpeed==0);
 p.SetCaching(CACHESTATE_DONE);assert(p.m_caching==CACHESTATE_INIT&&p.m_clock.speed==0);
 p.RequestedSpeed(DVD_PLAYSPEED_NORMAL);assert(p.m_clock.speed==0);
 p.RequestedSpeed(DVD_PLAYSPEED_PAUSE);assert(p.m_playSpeed==0);
 p.m_videoWedgePts=9000;p.m_brokenFileStallBytes=999;
 p.HoldStep();assert(p.normalSteps==0&&p.m_videoWedgePts==DVD_NOPTS_VALUE);
 assert(p.m_brokenFileStallBytes==-1&&notifications==0);
 p.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
 assert(p.WaitForActiveAreaDetection());assert(p.WaitForActiveAreaDetection());
 assert(notifications==1);auto* progress=p.m_activeAreaProgress;
 assert(progress&&progress->completed==0&&progress->total==14&&progress->activeAreaStartup);
 progressPosition=7;assert(p.WaitForActiveAreaDetection());
 assert(progress->completed==7&&progress->text=="7/14"&&notifications==1);
 startupPending=false;p.HoldStep();
 assert(!heldStatus&&progress->finished&&!p.m_activeAreaProgress);
 assert(p.m_clock.speed==0&&p.audio.speed==0&&video.appliedSpeed==0);
 assert(p.m_callback.starts==1&&p.normalSteps==1);
 // Accepted geometry retires startup; an ordinary background scan has no hold.
 p.RequestedSpeed(DVD_PLAYSPEED_NORMAL);assert(p.m_clock.speed==DVD_PLAYSPEED_NORMAL);
 // Cached / immediate returns never flash a notification.
 Parent q(video);q.m_State.streamsReady=true;q.m_activeAreaVideoReady=true;
 q.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;startupPending=false;
 q.m_caching=CACHESTATE_INIT;assert(!q.WaitForActiveAreaDetection());q.SetCaching(CACHESTATE_DONE);
 assert(q.m_clock.speed==DVD_PLAYSPEED_NORMAL&&notifications==1);
 Parent audioOnly(video);audioOnly.m_CurrentVideo.id=-1;
 audioOnly.m_CurrentAudio.syncState=IDVDStreamPlayer::SYNC_WAITSYNC;
 audioOnly.m_CurrentAudio.packets=21;audioOnly.m_CurrentAudio.starttime=1000000;
 audioOnly.SynchronizeStreams(false);assert(audioOnly.m_State.streamsReady);
 assert(startupCompletions.back()==aml_subtitle_active_area_source());
 Parent stopped(video);startupPending=true;stopped.m_State.streamsReady=true;
 stopped.m_activeAreaVideoReady=true;stopped.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
 stopped.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
 assert(stopped.WaitForActiveAreaDetection());auto* stoppedProgress=stopped.m_activeAreaProgress;
 stopped.m_bAbortRequest=true;assert(!stopped.WaitForActiveAreaDetection());
 assert(!heldStatus&&stoppedProgress->finished&&!stopped.m_activeAreaProgress);
 // Every mode uses the actual parent title selection and marks the handle;
 // finishing only retires the handle. Rendering/closing is the GUI's lifetime.
 for(int modes:{1,2,3}){
  detectorModes=modes;startupPending=true;
  Parent titled(video);titled.m_CurrentVideo.id=0;titled.m_State.streamsReady=true;
  titled.m_activeAreaVideoReady=true;titled.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
  titled.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
  assert(titled.WaitForActiveAreaDetection());auto* h=titled.m_activeAreaProgress;
  assert(h&&h->activeAreaStartup&&!h->finished);
  assert(h->title=="localized-"+std::to_string(modes==2?69378:modes==3?69379:69376));
  titled.FinishActiveAreaProgress();assert(h->finished&&!titled.m_activeAreaProgress);
 }
 detectorModes=1;
 // A real flush resets first-frame readiness. Stale start reports cannot rearm it.
 Parent seeking(video);seeking.m_activeAreaVideoReady=true;seeking.FlushBuffers(0,true,true);
 assert(!seeking.m_activeAreaVideoReady);
 started.epoch=video.GetSyncEpoch()-1;
 seeking.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,started));
 seeking.HandleMessages();assert(!seeking.m_activeAreaVideoReady);
 std::cout<<"PASS: initial sync before hold, progress, user speed, cache/skip, stop and seek readiness\n";
}

'''


def native_source():
    code = NATIVE['source']()
    aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    progress=function(aml,'struct DetectPositionProgress')+';\n'
    assert 'DetectPositionProgress progress{source->completedPositions, s + 1};' in aml
    assert 'const int numSeeks = 14;' in aml
    return code.replace(function(code, 'int main()'), progress+NATIVE_TEST)


def player_source():
    source = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    code = PARENT['harness']()
    assert code.endswith(PARENT['TESTS'])
    code = code[:-len(PARENT['TESTS'])]
    code = code.replace('using namespace std::chrono_literals;',
                        '#include <iostream>\nusing namespace std::chrono_literals;')
    code = code.replace('CACHESTATE_INIT=5;', 'CACHESTATE_INIT=5,CACHESTATE_FULL=6,CACHESTATE_PLAY=7;')
    code = code.replace('struct CServiceBroker {', r'''
bool externalPending=false;
void aml_subtitle_active_area_startup_set_video_ready(const std::shared_ptr<const void>&,bool){}
bool aml_subtitle_active_area_startup_external_pending(const std::shared_ptr<const void>&){return externalPending;}
bool startupPending=false,heldStatus=false;int notifications=0,progressPosition=0;
void aml_subtitle_active_area_startup_set_held(const std::shared_ptr<const void>&,bool held){heldStatus=held;}
int progressTotal=14, detectorModes=1;
int aml_subtitle_active_area_startup_modes(const std::shared_ptr<const void>&){return detectorModes;}
bool aml_subtitle_active_area_startup_progress(const std::shared_ptr<const void>&,int& completed,int& total){completed=progressPosition;total=progressTotal;return startupPending;}
struct Localize{std::string Get(int id){return "localized-"+std::to_string(id);}} g_localizeStrings;
struct StringUtils{static std::string Format(const std::string&,int a,int b){return std::to_string(a)+"/"+std::to_string(b);}};
struct CGUIDialogProgressBarHandle{
 bool finished=false,activeAreaStartup=false;int completed=-1,total=0;std::string text,title;
 void SetText(const std::string& value){assert(!finished);text=value;}
 void SetProgress(int value,int count){assert(!finished);completed=value;total=count;}
 void SetPercentage(float value){assert(!finished);assert(value==0.0f);completed=0;}
 void MarkFinished(){assert(!finished);finished=true;}
};
struct CGUIDialogExtendedProgressBar{
 std::vector<std::unique_ptr<CGUIDialogProgressBarHandle>> handles;
 auto GetHandle(const std::string& title,bool activeAreaStartup=false){++notifications;handles.emplace_back(std::make_unique<CGUIDialogProgressBarHandle>());auto* h=handles.back().get();h->title=title;h->activeAreaStartup=activeAreaStartup;return h;}
} progressDialog;
constexpr int WINDOW_DIALOG_EXT_PROGRESS=1;
struct GUI{GUI&GetWindowManager(){return *this;}template<class T>T*GetWindow(int){return &progressDialog;}} gui;
struct CServiceBroker {static GUI* GetGUI(){return &gui;}
''')
    code = code.replace('bool WaitForActiveAreaDetection(){return false;}', r'''
bool WaitForActiveAreaDetection();void FinishActiveAreaProgress();void RequestedSpeed(int speed);void HoldStep();
std::chrono::steady_clock::time_point m_activeAreaWaitStart,m_brokenFileStallStart;
CGUIDialogProgressBarHandle* m_activeAreaProgress=nullptr;
bool m_brokenFileStallStarveLogged=false;
double m_videoWedgePts=DVD_NOPTS_VALUE;int64_t m_brokenFileStallBytes=-1;int normalSteps=0;
struct CacheInfo{bool valid=false;};CacheInfo GetCachingTimes(){return {};}
int m_caching=CACHESTATE_DONE,m_streamPlayerSpeed=DVD_PLAYSPEED_NORMAL;
struct CacheTimer{void Set(std::chrono::milliseconds){} bool IsTimePast()const{return false;}} m_cachingTimer;
''')
    code = code.replace('void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); ++cacheChanges;}',
                        'void SetCaching(int value);')
    code = code.replace('double GetClock() const {return value;}',
                        'int speed=1000;void SetSpeed(int v){speed=v;}void SetSpeedAdjust(int){}\n double GetClock() const {return value;}')
    code = code.replace('uint64_t epoch=0; bool accepts=true; int level=20;',
                        'int speed=1000;void SetSpeed(int v){speed=v;} bool IsStalled()const{return false;}\n uint64_t epoch=0; bool accepts=true; int level=20;')
    marker='struct CVideoPlayerVideo {'
    assert marker in code
    code = code.replace(marker, marker+'\npublic: int appliedSpeed=1000;void SetSpeed(int v){appliedSpeed=v;}')
    methods = function(source, 'bool CVideoPlayer::WaitForActiveAreaDetection()')
    methods += '\n' + function(source, 'void CVideoPlayer::FinishActiveAreaProgress()')
    methods += '\n' + function(source, 'void CVideoPlayer::SetCaching(').replace('ECacheState', 'int')
    speed = function(source, 'else if (pMsg->IsType(CDVDMsg::PLAYER_SETSPEED))')
    methods += '\nvoid Parent::RequestedSpeed(int speed){' + speed[speed.index('      m_playSpeed = speed;'):-1] + '}\n'
    handle = function(source, 'void CVideoPlayer::HandlePlaySpeed()')
    guard = function(handle, '  if (WaitForActiveAreaDetection())')
    methods += '\nvoid Parent::HoldStep(){' + guard + '++normalSteps;' + function(handle,'  if (m_caching == CACHESTATE_INIT)') + function(handle,'  if (m_caching == CACHESTATE_PLAY)') + '}\n'
    methods = methods.replace('CVideoPlayer::', 'Parent::').replace('#if defined(HAS_LIBAMCODEC)', '#if 1')
    code += methods + PLAYER_TEST
    # These call sites are essential: decoding/configuration must remain active.
    open_file = function(source, 'bool CVideoPlayer::OpenFile(')
    assert 'aml_dv_detect_set_file(' in open_file
    prepare = function(source, 'void CVideoPlayer::Prepare()')
    assert prepare.index('m_State.Clear();') < prepare.index('aml_subtitle_active_area_startup_begin(')
    assert 'Prepare();' in function(source, 'void CVideoPlayer::CompleteFileReplacement()')
    assert 'FinishActiveAreaProgress();' in function(source,'void CVideoPlayer::OnExit()')
    assert 'aml_subtitle_active_area_startup_complete(' in function(source,'void CVideoPlayer::OnExit()')
    assert 'FinishActiveAreaProgress();' in function(source,'CVideoPlayer::~CVideoPlayer()')
    assert function(source, 'void CVideoPlayer::HandlePlaySpeed()').index('if (WaitForActiveAreaDetection())') < handle.index('const bool isInMenu')
    return code


def connected_source():
    """Drive parent startup against actual selection/admission/worker APIs."""
    import re
    native = native_source()
    native = native.replace(function(native, 'int main()'), '')
    player = player_source()
    player = player.replace(function(player, 'int main()'), '')
    player = player.replace(function(player, 'int aml_subtitle_active_area_startup_modes('),
        'int aml_subtitle_active_area_startup_modes(const std::shared_ptr<const void>& selected){return ::aml_subtitle_active_area_startup_modes(selected);}')
    player = player.replace(function(player, 'void aml_subtitle_active_area_startup_set_video_ready('),
        'void aml_subtitle_active_area_startup_set_video_ready(const std::shared_ptr<const void>& selected,bool ready){::aml_subtitle_active_area_startup_set_video_ready(selected,ready);}')
    player = player.replace(function(player, 'bool aml_subtitle_active_area_startup_external_pending('),
        'bool aml_subtitle_active_area_startup_external_pending(const std::shared_ptr<const void>& selected){return ::aml_subtitle_active_area_startup_external_pending(selected);}')
    player = player.replace(function(player, 'bool aml_subtitle_active_area_startup_progress('),
        'bool aml_subtitle_active_area_startup_progress(const std::shared_ptr<const void>& selected,int& n,int& total){return ::aml_subtitle_active_area_startup_progress(selected,n,total);}')
    player = player.replace(function(player, 'void aml_subtitle_active_area_startup_complete('),
        'void aml_subtitle_active_area_startup_complete(const std::shared_ptr<const void>& selected){startupCompletions.push_back(selected);::aml_subtitle_active_area_startup_complete(selected);}')
    player = player.replace(function(player, 'std::shared_ptr<const void> aml_subtitle_active_area_source()'),
        'std::shared_ptr<const void> aml_subtitle_active_area_source(){return ::aml_subtitle_active_area_source();}')
    player = player.replace(function(player, 'void aml_subtitle_active_area_startup_set_held('),
        'void aml_subtitle_active_area_startup_set_held(const std::shared_ptr<const void>& selected,bool held){::aml_subtitle_active_area_startup_set_held(selected,held);}')
    player = player.replace(function(player, 'void aml_subtitle_active_area_invalidate('),
        'void aml_subtitle_active_area_invalidate(bool preserve,bool restart){subtitleInvalidations.push_back(preserve);::aml_subtitle_active_area_invalidate(preserve,restart);}')
    owner = (ROOT/'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    reset = function(owner, 'void CVideoPlayerVideo::ResetSubtitleProbe()')
    for name in ('invalidate', 'reset'):
        reset = reset.replace('aml_subtitle_active_area_'+name+'(', '::aml_subtitle_active_area_'+name+'(')
    fake = function(player, 'struct FakeCodec')
    player = player.replace(fake+';', fake.replace('struct FakeCodec {','struct FakeCodec {virtual ~FakeCodec()=default;') + ';\nstruct CDVDVideoCodecFFmpeg:FakeCodec {void* GetHWAccel(){return nullptr;}};\nnamespace StreamHdrType {constexpr int HDR_TYPE_DOLBYVISION=1;}')
    player = player.replace('std::make_shared<FakeCodec>()', 'std::make_shared<CDVDVideoCodecFFmpeg>()')
    player = player.replace('struct CVideoPlayerVideo {', '''struct CVideoPlayerVideo {
std::shared_ptr<const void> m_subtitleProbeSource;int m_subtitleProbeWidth=0,m_subtitleProbeHeight=0;
struct {std::shared_ptr<const void> subtitleProbeSource;int hdrType=0;}m_hints;
void ResetSubtitleProbe();''', 1) + '\n' + reset
    assert 'aml_subtitle_active_area_startup_admitted(m_hints.subtitleProbeSource);' in function(owner, 'if (decoderState == CDVDVideoCodec::VC_EOF)\n  {')
    # Hoist includes so C++/Kodi types keep their real namespace. Only the
    # recording player/UI services live in a namespace to avoid native stubs.
    includes = re.findall(r'^#include .+$', player, re.M)
    player = re.sub(r'^#include .+$', '', player, flags=re.M)
    test = r"""
int main(){
 ready();CAMLSession sessionValue;init(sessionValue);settingsValue.wait=true;
 // Connect independent real detector toggles all the way through the actual
 // source helper and held parent title selection, with a real admitted worker.
 for(int modes:{1,2,3}){
  settingsValue.detect=modes&1;dvDetection=modes&2;
  select("connected-title-"+std::to_string(modes),true);
  aml_subtitle_active_area_startup_begin();const auto selected=aml_subtitle_active_area_source();
  PLAYER_FIXTURE::CVideoPlayerVideo titleVideo;PLAYER_FIXTURE::Parent titleParent(titleVideo);
  titleParent.m_CurrentVideo.id=0;titleParent.m_CurrentVideo.hint.subtitleProbeSource=selected;
  titleParent.m_State.streamsReady=true;titleParent.m_activeAreaVideoReady=true;
  titleParent.m_CurrentVideo.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_INSYNC;
  assert(titleParent.WaitForActiveAreaDetection());
  std::promise<void> entered,finish;auto gate=finish.get_future();
  scanHook=[&](const auto& source,const CAMLNativeWorker::Run&){
   source->completedPositions=3;entered.set_value();gate.wait();
  };
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  assert(entered.get_future().wait_for(2s)==std::future_status::ready);
  titleParent.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
  assert(titleParent.WaitForActiveAreaDetection());auto* h=titleParent.m_activeAreaProgress;
  assert(h&&h->activeAreaStartup&&h->completed==3&&h->total==14);
  assert(h->title=="localized-"+std::to_string(modes==2?69378:modes==3?69379:69376));
  finish.set_value();s_detectWorker.Stop();titleParent.HoldStep();assert(h->finished);
 }
 settingsValue.detect=true;dvDetection=true;
 for(bool nativeDV:{false,true}){
  aml_dv_detect_set_file(nativeDV?"deferred-DV":"deferred-SDR");aml_subtitle_active_area_startup_begin();
  const auto selection=aml_subtitle_active_area_source(0,173);
  PLAYER_FIXTURE::CVideoPlayerVideo video;PLAYER_FIXTURE::Parent p(video);
  p.m_CurrentVideo.id=0;p.m_CurrentVideo.hint.subtitleProbeSource=selection;
  p.m_CurrentVideo.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_STARTING;
  p.m_CurrentVideo.packets=0;p.m_CurrentAudio.packets=21;
  p.m_CurrentAudio.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_WAITSYNC;
  p.m_CurrentAudio.starttime=1000000;
  // A full audio queue cannot prevent startup synchronization / further feed.
  p.audio.accepts=false;video.level=0;
  p.SynchronizeStreams(false);
  assert(p.m_State.streamsReady&&p.m_callback.starts==1&&p.m_clock.anchors==1);
  assert(p.m_clock.speed==PLAYER_FIXTURE::DVD_PLAYSPEED_NORMAL&&!p.m_activeAreaVideoReady);
  assert(aml_subtitle_active_area_startup_pending(selection));
  assert(!aml_subtitle_active_area_startup_held(selection));
  assert(!p.WaitForActiveAreaDetection());
  // The audio environment can drain after real RESYNC at normal speed.
  assert(!p.audio.sent.empty());p.audio.accepts=true;
  auto first=std::make_shared<std::promise<void>>();auto firstReady=first->get_future();
  scanHook=[first](const auto& source,const CAMLNativeWorker::Run& run){
   source->completedPositions=3;assert(!detect_startup_wait_active(run));first->set_value();assert(!run.WaitFor(30s));
  };
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selection));
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  assert(firstReady.wait_for(2s)==std::future_status::ready);
  PLAYER_FIXTURE::SStartMsg report{};report.player=PLAYER_FIXTURE::VideoPlayer_VIDEO;
  report.epoch=video.GetSyncEpoch();report.timestamp=1000000;
  p.queue.Put(std::make_shared<PLAYER_FIXTURE::CDVDMsgType<PLAYER_FIXTURE::SStartMsg>>(PLAYER_FIXTURE::CDVDMsg::PLAYER_STARTED,report));
  p.HandleMessages();assert(p.m_activeAreaVideoReady);
  p.m_CurrentVideo.packets=1;
  assert(!p.WaitForActiveAreaDetection()); // resync must still reach the video thread
  p.SynchronizeStreams(false);
  assert(p.WaitForActiveAreaDetection()&&aml_subtitle_active_area_startup_held(selection));
  p.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
  assert(p.WaitForActiveAreaDetection());auto* handle=p.m_activeAreaProgress;
  assert(handle&&handle->completed==3&&handle->total==14);
  // PM4K can seek immediately after AVStarted, during the first held run.
  p.FlushBuffers(2000000,true,true,{},true);
  assert(!p.m_activeAreaVideoReady&&!p.WaitForActiveAreaDetection());
  assert(handle->finished&&!aml_subtitle_active_area_startup_held(selection));
  s_detectWorker.Stop();
  for(int step=0;video.IsFlushPending()&&step<8;++step){video.Step();video.m_pVideoCodec->ready=true;}
  assert(!video.IsFlushPending()&&p.ContinueParentLifecycle());
  assert(!p.WaitForActiveAreaDetection());
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selection));
  std::promise<void> seekEntered;
  scanHook=[&](const auto& source,const CAMLNativeWorker::Run& run){
   assert(!detect_startup_wait_active(run));source->completedPositions=3;
   seekEntered.set_value();assert(!run.WaitFor(30s));
  };
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  assert(seekEntered.get_future().wait_for(2s)==std::future_status::ready);
  p.queue.Put(std::make_shared<PLAYER_FIXTURE::CDVDMsgType<PLAYER_FIXTURE::SStartMsg>>(PLAYER_FIXTURE::CDVDMsg::PLAYER_STARTED,report));
  p.HandleMessages();assert(!p.m_activeAreaVideoReady); // pre-seek epoch
  report.epoch=video.GetSyncEpoch();report.timestamp=2000000;
  p.queue.Put(std::make_shared<PLAYER_FIXTURE::CDVDMsgType<PLAYER_FIXTURE::SStartMsg>>(PLAYER_FIXTURE::CDVDMsg::PLAYER_STARTED,report));
  p.HandleMessages();assert(p.m_activeAreaVideoReady);
  p.m_CurrentVideo.packets=1;p.m_CurrentAudio.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_WAITSYNC;
  p.m_CurrentAudio.starttime=2000000;p.m_CurrentAudio.packets=21;
  assert(!p.WaitForActiveAreaDetection());p.SynchronizeStreams(false);
  assert(p.WaitForActiveAreaDetection());handle=p.m_activeAreaProgress;
  assert(handle&&!handle->finished&&p.m_callback.starts==1);
  // Local decoder recovery retains the established first-frame readiness.

  aml_subtitle_active_area_invalidate(true,true);s_detectWorker.Stop();
  aml_subtitle_active_area_startup_admitted(selection); // stale EOF cannot resolve the reset
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  p.SynchronizeStreams(false);assert(p.m_State.streamsReady&&!handle->finished);
  assert(handle->completed==0&&handle->text=="localized-"+std::to_string(nativeDV?69379:69376));
  // REOPEN temporarily stops and configures an unavailable route; its
  // selection admission must survive those intermediate operations too.
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_stop();}
  assert(aml_subtitle_active_area_configure(0,0,false,false,selection));
  assert(p.WaitForActiveAreaDetection()&&!handle->finished);
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selection));
  assert(p.WaitForActiveAreaDetection());
  std::promise<void> second,finish;auto gate=finish.get_future();
  scanHook=[&](const auto& source,const CAMLNativeWorker::Run& run){
   assert(detect_startup_wait_active(run));
   assert(detect_wait_for_cache(run,80,0)==100);source->completedPositions=7;
   second.set_value();gate.wait();detect_publish(run,source,132,132,0,0);
  };
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  assert(second.get_future().wait_for(2s)==std::future_status::ready);
  p.SynchronizeStreams(false);assert(p.m_State.streamsReady&&p.m_activeAreaProgress==handle);
  assert(!handle->finished&&handle->completed==7);
  finish.set_value();until([]{return !s_detectSource->pending.load();});
  s_detectWorker.Stop();p.HoldStep();p.SynchronizeStreams(false);
  assert(p.m_State.streamsReady&&p.m_callback.starts==1&&handle->finished);
  assert(p.m_clock.speed==PLAYER_FIXTURE::DVD_PLAYSPEED_NORMAL);
  uint16_t t,b,l,r;assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==132&&b==132);
  // Seeking after accepted geometry neither re-arms the hold nor discards it.
  aml_subtitle_active_area_invalidate(true,true);
  assert(!aml_subtitle_active_area_startup_pending(selection));
  assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==132);
 }
 // Execute the production software reset, then the no-picture EOF admission.
 for(bool alreadyProbed:{false,true}){
 aml_dv_detect_set_file("software-reset-eof");aml_subtitle_active_area_startup_begin();
 const auto eofSelection=aml_subtitle_active_area_source(0,173);
 if(alreadyProbed)assert(aml_subtitle_active_area_configure(1920,1080,false,true,eofSelection));
 PLAYER_FIXTURE::CVideoPlayerVideo emptyVideo;PLAYER_FIXTURE::Parent emptyParent(emptyVideo);
 emptyVideo.m_hints.subtitleProbeSource=eofSelection;
 if(alreadyProbed){emptyVideo.m_subtitleProbeSource=eofSelection;emptyVideo.m_subtitleProbeWidth=1920;emptyVideo.m_subtitleProbeHeight=1080;}
 emptyParent.m_CurrentVideo.id=0;emptyParent.m_CurrentVideo.hint.subtitleProbeSource=eofSelection;
 emptyParent.m_CurrentVideo.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_WAITSYNC;
 emptyParent.m_CurrentAudio.syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_WAITSYNC;
 emptyParent.m_CurrentAudio.packets=21;emptyParent.m_CurrentAudio.starttime=1000000;
 emptyVideo.ResetSubtitleProbe();assert(aml_subtitle_active_area_startup_pending(eofSelection));
 // Back-to-back parent flush/reset must preserve dimensions even with local
 // cached probe dimensions already zeroed by the first reset.
 aml_subtitle_active_area_invalidate(true,true);emptyVideo.ResetSubtitleProbe();
 assert(s_detectSource->width==(alreadyProbed?1920:0)&&s_detectSource->height==(alreadyProbed?1080:0));
 aml_subtitle_active_area_startup_admitted(eofSelection);
 assert(!aml_subtitle_active_area_startup_pending(eofSelection));
 emptyParent.SynchronizeStreams(false);assert(emptyParent.m_State.streamsReady);
 }
 // EOF/unsupported resolves admission without a worker; cache/immediate skip
 // can complete between polls without ever making a progress handle.
 aml_dv_detect_set_file("empty-or-unsupported");aml_subtitle_active_area_startup_begin();
 auto empty=aml_subtitle_active_area_source(0,173);
 assert(aml_subtitle_active_area_startup_pending(empty));
 aml_subtitle_active_area_startup_admitted(empty);
 assert(!aml_subtitle_active_area_startup_pending(empty));
 std::cout<<"PASS: connected real source/worker -> parent admission, seek/reopen handoff, accepted geometry, post-start seek and explicit skip\n";
}
"""
    return '\n'.join(includes) + '\n' + native + '\nnamespace PLAYER_FIXTURE {\n' + player + '\n}\n' + test


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    native, player=native_source(),player_source()
    run(native);run(player);run(connected_source())
    # Launch failure destroys the real Start capture without entering the worker.
    launch = native.replace(function(native,'int main()'), r'''
int main(){ready();CAMLSession value;init(value);settingsValue.wait=true;
 select("launch-failure",false);aml_subtitle_active_area_startup_begin();
 bool failed=false;try{aml_dv_detect_active_area_start();}catch(const std::exception&){failed=true;}
 assert(failed&&!s_detectSource->pending);
 assert(!aml_subtitle_active_area_startup_pending(aml_subtitle_active_area_source()));
 std::cout<<"PASS: injected native thread-launch failure releases pending startup\n";}
''')
    header=(ROOT/'xbmc/utils/AMLNativeWorker.h').read_text()
    old='m_thread = std::thread([run, action = std::move(action)]() mutable {'
    assert old in header
    injected='#include <stdexcept>\n'+header.replace(old,'throw std::runtime_error("thread creation failed");\n      '+old)
    run(launch,('utils/AMLNativeWorker.h',injected))
    if args.negative_controls:
        connected=connected_source()
        for label,old,new in [
            ('deferred admission lost','if (s_detectSource->startupAdmission->load())\n    return true;','/* admission ignored */'),
            ('seek restart drops startup ownership','if (restartProbe && s_detectSource->startupWait->load())','if (false && restartProbe)'),
            ('configuration loses admission','s_detectSource->startupAdmission = previous->startupAdmission;','/* admission identity lost */'),
            ('old completion clears replacement admission','source->pending.store(false);','source->pending.store(false);source->startupAdmission->store(false);'),
            ('stale route resolves restarting admission','expectedSource == s_detectSource->selection &&\n      !s_detectSource->superseded->load()','expectedSource == s_detectSource->selection'),
            ('software reset cannot finish at EOF','::aml_subtitle_active_area_reset(source);','/* route renewal lost */'),
        ]:
            assert old in connected,label
            run(connected.replace(old,new),negative=True);print('REJECTED:',label)
        for code,label,old,new in [
            (connected,'hold before initial video/sync','                    videoReady;','                    true;'),
            (connected,'retire wait at AVStarted','if (m_CurrentVideo.id < 0)\n          aml_subtitle_active_area_startup_complete(aml_subtitle_active_area_source());','aml_subtitle_active_area_startup_complete(aml_subtitle_active_area_source());'),
            (native,'requested wait bypasses I/O before held','s_detectSource->startupHeld->load() &&\n         s_detectSource->pending.load()','s_detectSource->startupWait->load() &&\n         s_detectSource->pending.load()'),
            (player,'flush retains obsolete picture readiness','if (sync)\n  {\n    m_activeAreaVideoReady = false;','if (sync)\n  {'),
            (native,'lost completion','source->pending.store(false);','/* completion lost */'),
            (native,'throttle waits on held playback','if (detect_startup_wait_active(run))\n      return 100;','/* no startup bypass */'),
            (native,'low-buffer abort during hold',' && !detect_startup_wait_active(*run)',''),
            (native,'stale selection wait','expectedSource != s_detectSource->selection ||','false ||'),
            (native,'cancelled source wait','if (s_detectSource->superseded->load() || !s_detectSource->pending.load())','if (!s_detectSource->pending.load())'),
            # The old pre-sync guard mutant is redundant after post-sync admission.
            (connected,'audio fallback counts as first picture','                          m_activeAreaVideoReady &&',''),
            (player,'buffer timeout resumes during hold','if ((state == CACHESTATE_PLAY || state == CACHESTATE_DONE) && WaitForActiveAreaDetection())','if (false)'),
            (player,'force resume after user pause','m_clock.SetSpeed(m_playSpeed);','m_clock.SetSpeed(DVD_PLAYSPEED_NORMAL);'),
            (player,'user resume bypasses hold','if (WaitForActiveAreaDetection())\n        SetCaching(CACHESTATE_INIT);','if (false)\n        SetCaching(CACHESTATE_INIT);'),
            (player,'audio-only startup ownership leaked','aml_subtitle_active_area_startup_complete(aml_subtitle_active_area_source());','aml_subtitle_active_area_startup_complete(m_CurrentVideo.hint.subtitleProbeSource);'),
            (native,'progress counter discarded','completed = std::clamp(s_detectSource->completedPositions.load(), 0, total);','completed = 0;'),
            (player,'DV title incorrectly says black borders','modes == 2 ? 69378','modes == 2 ? 69376'),
            (player,'combined title hides L5 mode','modes == 3 ? 69379','modes == 3 ? 69376'),
            (player,'detector handle lacks rendered metadata','GetHandle(g_localizeStrings.Get(title), true)','GetHandle(g_localizeStrings.Get(title), false)'),
            (native,'DV modes coupled to subtitle toggle','s_detectSource->nativeDV && aml_dv_detect_active_area_enabled() ? 2 : 0','settings()->GetBool(CSettings::SETTING_SUBTITLES_DETECTACTIVEAREA) && s_detectSource->nativeDV && aml_dv_detect_active_area_enabled() ? 2 : 0'),
            (player,'finished progress handle leaked','m_activeAreaProgress->MarkFinished();','/* leaked */'),
        ]:
            assert old in code,label
            mutant = code.replace(old,new)
            # Admission now rechecks hold state after querying the cache.
            # Remove both bypasses to model the same broken held-scan policy.
            if label == 'throttle waits on held playback':
                mutant = mutant.replace('return detect_startup_wait_active(run) ? 100 : level;', 'return level;')
            elif label == 'low-buffer abort during hold':
                mutant = mutant.replace('if (detect_startup_wait_active(*run))\n          return 0;', '/* late hold protection lost */')
            run(mutant,negative=True);print('REJECTED:',label)


if __name__=='__main__':main()
