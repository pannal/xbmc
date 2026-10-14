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
 for (int outcome=0; outcome<3; ++outcome) {
  select("startup",false);aml_subtitle_active_area_startup_begin();
  auto selected=aml_subtitle_active_area_source();
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
 p.m_caching=CACHESTATE_DONE;startupPending=true;
 p.SynchronizeStreams(false);
 assert(p.m_clock.anchors==0&&!p.m_State.streamsReady&&p.m_callback.starts==0);
 assert(p.m_clock.speed==0&&p.audio.speed==0&&video.appliedSpeed==0);
 p.SetCaching(CACHESTATE_DONE);
 assert(p.m_caching==CACHESTATE_INIT&&p.m_clock.speed==0);
 // A user's resume cannot bypass the hold; their later pause must survive it.
 p.RequestedSpeed(DVD_PLAYSPEED_NORMAL);assert(p.m_clock.speed==0);
 p.RequestedSpeed(DVD_PLAYSPEED_PAUSE);assert(p.m_playSpeed==0);
 p.m_videoWedgePts=9000;p.m_brokenFileStallBytes=999;
 p.HoldStep();assert(p.normalSteps==0&&p.m_videoWedgePts==DVD_NOPTS_VALUE);
 assert(p.m_brokenFileStallBytes==-1);
 assert(notifications==0);
 p.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
 assert(p.WaitForActiveAreaDetection());assert(p.WaitForActiveAreaDetection());
 assert(notifications==1);
 auto* progress=p.m_activeAreaProgress;
 assert(progress&&progress->completed==0&&progress->total==14);
 assert(progressDialog.startup);
 progressPosition=7;assert(p.WaitForActiveAreaDetection());
 assert(progress->completed==7&&progress->text=="7/14"&&notifications==1);
 startupPending=false;p.SynchronizeStreams(false);
 assert(p.m_State.streamsReady&&p.m_callback.starts==1&&p.m_clock.anchors==1);
 assert(progress->finished&&!p.m_activeAreaProgress);
 assert(!progressDialog.startup);
 assert(p.m_clock.speed==0&&p.audio.speed==0&&video.appliedSpeed==0);
 p.HoldStep();assert(p.normalSteps==1);
 // A later scan cannot introduce another startup hold after AVStarted.
 startupPending=true;assert(!p.WaitForActiveAreaDetection());
 p.SetCaching(CACHESTATE_DONE);assert(p.m_clock.speed==0);
 p.RequestedSpeed(DVD_PLAYSPEED_NORMAL);assert(p.m_clock.speed==DVD_PLAYSPEED_NORMAL);
 // Fast cached/no-scan paths generate no notification and release normally.
 Parent q(video);startupPending=false;q.m_caching=CACHESTATE_INIT;
 assert(!q.WaitForActiveAreaDetection());q.SetCaching(CACHESTATE_DONE);
 assert(q.m_clock.speed==DVD_PLAYSPEED_NORMAL&&notifications==1);
 Parent audioOnly(video);audioOnly.m_CurrentVideo.id=-1;
 audioOnly.m_CurrentAudio.syncState=IDVDStreamPlayer::SYNC_WAITSYNC;
 audioOnly.m_CurrentAudio.packets=21;audioOnly.m_CurrentAudio.starttime=1000000;
 audioOnly.SynchronizeStreams(false);assert(audioOnly.m_State.streamsReady);
 assert(startupCompletions.back()==aml_subtitle_active_area_source());
 Parent stopped(video);startupPending=true;
 stopped.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
 assert(stopped.WaitForActiveAreaDetection());auto* stoppedProgress=stopped.m_activeAreaProgress;
 stopped.m_bAbortRequest=true;assert(!stopped.WaitForActiveAreaDetection());
 assert(stoppedProgress->finished&&!stopped.m_activeAreaProgress);
 assert(!progressDialog.startup);
 std::cout<<"PASS: production startup sync/cache/user-speed hold, notification, pause preservation and instant/no-scan release\n";
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
bool startupPending=false;int notifications=0,progressPosition=0;
bool aml_subtitle_active_area_startup_progress(const std::shared_ptr<const void>&,int& completed,int& total){completed=progressPosition;total=14;return startupPending;}
struct Localize{std::string Get(int){return "detecting";}} g_localizeStrings;
struct StringUtils{static std::string Format(const std::string&,int a,int b){return std::to_string(a)+"/"+std::to_string(b);}};
struct CGUIDialogProgressBarHandle{
 bool finished=false;int completed=-1,total=0;std::string text;
 void SetText(const std::string& value){assert(!finished);text=value;}
 void SetProgress(int value,int count){assert(!finished);completed=value;total=count;}
 void MarkFinished(){assert(!finished);finished=true;}
};
struct CGUIDialogExtendedProgressBar{
 bool startup=false;void SetProperty(const std::string&,bool v){startup=v;}
 void SetProperty(const std::string&,const char* v){startup=std::string(v)!="";}
 std::vector<std::unique_ptr<CGUIDialogProgressBarHandle>> handles;
 auto GetHandle(const std::string&){++notifications;handles.emplace_back(std::make_unique<CGUIDialogProgressBarHandle>());return handles.back().get();}
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
struct CacheTimer{void Set(std::chrono::milliseconds){}} m_cachingTimer;
''')
    code = code.replace('void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); ++cacheChanges;}',
                        'void SetCaching(int value);')
    code = code.replace('double GetClock() const {return value;}',
                        'int speed=1000;void SetSpeed(int v){speed=v;}void SetSpeedAdjust(int){}\n double GetClock() const {return value;}')
    code = code.replace('uint64_t epoch=0; bool accepts=true; int level=20;',
                        'int speed=1000;void SetSpeed(int v){speed=v;}\n uint64_t epoch=0; bool accepts=true; int level=20;')
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
    methods += '\nvoid Parent::HoldStep(){' + guard + '++normalSteps;}\n'
    methods = methods.replace('CVideoPlayer::', 'Parent::').replace('#if defined(HAS_LIBAMCODEC)', '#if 1')
    code += methods + PLAYER_TEST
    # These call sites are essential: decoding/configuration must remain active.
    open_file = function(source, 'bool CVideoPlayer::OpenFile(')
    assert open_file.index('aml_dv_detect_set_file(') < open_file.index('aml_subtitle_active_area_startup_begin();')
    assert 'FinishActiveAreaProgress();' in function(source,'void CVideoPlayer::OnExit()')
    assert 'aml_subtitle_active_area_startup_complete(' in function(source,'void CVideoPlayer::OnExit()')
    assert 'FinishActiveAreaProgress();' in function(source,'CVideoPlayer::~CVideoPlayer()')
    assert function(source, 'void CVideoPlayer::HandlePlaySpeed()').index('if (WaitForActiveAreaDetection())') < handle.index('const bool isInMenu')
    return code


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    native, player=native_source(),player_source()
    run(native);run(player)
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
        for code,label,old,new in [
            (native,'lost completion','source->pending.store(false);','/* completion lost */'),
            (native,'throttle waits on held playback','if (detect_startup_wait_active(run))\n      return 100;','/* no startup bypass */'),
            (native,'low-buffer abort during hold',' && !detect_startup_wait_active(*run)',''),
            (native,'stale selection wait','expectedSource != s_detectSource->selection ||','false ||'),
            (native,'cancelled source wait','s_detectSource->superseded->load() || !s_detectSource->startupWait->load() ||','!s_detectSource->startupWait->load() ||'),
            (player,'premature startup sync','if (WaitForActiveAreaDetection())\n  {\n    SetCaching(CACHESTATE_INIT);\n    return;\n  }','/* startup guard lost */'),
            (player,'buffer timeout resumes during hold','if ((state == CACHESTATE_PLAY || state == CACHESTATE_DONE) && WaitForActiveAreaDetection())','if (false)'),
            (player,'force resume after user pause','m_clock.SetSpeed(m_playSpeed);','m_clock.SetSpeed(DVD_PLAYSPEED_NORMAL);'),
            (player,'user resume bypasses hold','if (WaitForActiveAreaDetection())\n        SetCaching(CACHESTATE_INIT);','if (false)\n        SetCaching(CACHESTATE_INIT);'),
            (player,'audio-only startup ownership leaked','m_CurrentVideo.id >= 0 ? m_CurrentVideo.hint.subtitleProbeSource\n                                   : aml_subtitle_active_area_source()','m_CurrentVideo.hint.subtitleProbeSource'),
            (native,'progress counter discarded','completed = std::clamp(s_detectSource->completedPositions.load(), 0, total);','completed = 0;'),
            (player,'finished progress handle leaked','m_activeAreaProgress->MarkFinished();','/* leaked */'),
        ]:
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)


if __name__=='__main__':main()
