#!/usr/bin/env python3
"""Connected startup/restart I/O admission; production worker/player/AVIO gates.

Real native workers, cancellation, parent flush/message/sync/buffering methods.
Extract actual media-open admission and disc rejection from scanner; recording
VFS and scanner samples replace file transport and FFmpeg decoding only.
"""
import argparse
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
STARTUP = runpy.run_path(str(ROOT/'tools/test-active-area-startup.py'))
function, run = STARTUP['function'], STARTUP['run']


def source():
    aml = (ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    code = STARTUP['connected_source']()
    code = code.replace(function(code, 'int main()'), '')
    code = code.replace('struct CApplicationPlayer{bool IsPlaying(){return true;}int GetCacheLevel(){return 10;}};',
        'std::atomic<int> cacheLevel{100},cacheQueries{0};\nstruct CApplicationPlayer{bool IsPlaying(){return true;}int GetCacheLevel(){++cacheQueries;return cacheLevel;}};')
    code = code.replace('bool detect=true,wait=false,dvRequested=true;', 'bool detect=true,wait=false,dvRequested=true,cache=false;')
    code = code.replace('id==5?wait:id==2?detect:', 'id==5?wait:id==2?detect:id==3?cache:')
    code = code.replace('inline bool ReadIdentity(Identity&){assert(false);return false;}', 'inline bool ReadIdentity(Identity&){return true;}')
    code = code.replace('inline bool Load(const Identity&,Record&){assert(false);return false;}', 'inline bool Load(const Identity&,Record& r){r.hasSubtitle=true;r.subtitle={20,20,0,0};r.state=DV_DETECT_SKIPPED;return true;}')
    code = code.replace('inline bool Store(const Identity&,Record&){assert(false);return false;}', 'inline bool Store(const Identity&,Record&){return true;}')
    # VFS operations count actual callback admissions, not hypothetical samples.
    callbacks = function(aml, 'struct DetectInput')+';\n'
    callbacks += '\n'.join(function(aml, marker) for marker in ('static int detect_avio_read(', 'static int64_t detect_avio_seek('))
    support = r'''
constexpr int AVERROR_EOF=-1,AVERROR_EXIT=-2,AVSEEK_SIZE=0x10000,AVSEEK_FORCE=0x20000;
std::atomic<int> reads{0},seeks{0},entries{0},gateChecks{0};
namespace XFILE {struct CFile {
 int Read(uint8_t*,int){++reads;return 1;}
 int64_t Seek(int64_t p,int){++seeks;return p;}
 int64_t GetLength(){return 100;}
};}
struct StringUtils {static bool StartsWithNoCase(const std::string& s,const std::string& p){return s.rfind(p,0)==0;}};
'''
    # Retain the actual gate invocation before media open, and the unsupported
    # disc branch that must precede it. A wiring change must break this fixture.
    scanner = function(aml, 'static void DetectActiveAreaFromFile(')
    disc = function(scanner, '  if (StringUtils::StartsWithNoCase(filePath, "bluray://")')
    gate = '  if (!detect_wait_for_startup_hold(run))\n    goto cleanup;'
    assert gate in scanner
    before_open = scanner.index('if (!file.Open(')
    assert scanner.index(disc) < scanner.index(gate) < before_open
    assert 'avio_alloc_context(avioBuf, bufSize, 0, &input,' in scanner
    replacement = 'void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& state){\nconst auto& run=state;const auto& filePath=source->path;\n' + disc + '\n' + gate + '\ns_detectThrottleActive=true;++entries;scanHook(source,run);\ncleanup:return;\n}'
    # Instrument entry into the real wait to prove the worker reached deferral.
    code = code.replace('static bool detect_wait_for_startup_hold(const CAMLNativeWorker::Run& run)\n{',
                        'static bool detect_wait_for_startup_hold(const CAMLNativeWorker::Run& run)\n{\n  ++gateChecks;')
    old = function(code, 'void DetectActiveAreaFromFile(')
    # Counters/types must precede production wait and interrupt helpers.
    index = code.index('static bool detect_wait_for_startup_hold(')
    code = code[:index]+support+code[index:]
    code = code.replace(old, callbacks+'\n'+replacement)
    code = code.replace('void aml_subtitle_active_area_startup_set_held(const std::shared_ptr<const void>& selected,bool held){::aml_subtitle_active_area_startup_set_held(selected,held);}',
        'std::function<void()> checkPause;void aml_subtitle_active_area_startup_set_held(const std::shared_ptr<const void>& selected,bool held){if(held&&checkPause)checkPause();::aml_subtitle_active_area_startup_set_held(selected,held);}')
    owner = (ROOT/'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    eof = function(owner, 'if (decoderState == CDVDVideoCodec::VC_EOF)\n  {')
    terminal = eof[eof.index('{')+1:eof.index('    m_mpeg2Cadence.Drain();')]
    terminal = terminal.replace('#if defined(HAS_LIBAMCODEC)', '#if 1')
    code += '\nvoid TerminalEOF(const std::shared_ptr<const void>& source,bool m_inputEnded){struct{std::shared_ptr<const void> subtitleProbeSource;}m_hints{source};int m_syncState=PLAYER_FIXTURE::IDVDStreamPlayer::SYNC_STARTING;using IDVDStreamPlayer=PLAYER_FIXTURE::IDVDStreamPlayer;'+terminal+'}\n'
    return code+TEST


TEST = r'''
using PLAYER_FIXTURE::Parent;using PLAYER_FIXTURE::CVideoPlayerVideo;
using PLAYER_FIXTURE::SStartMsg;using PLAYER_FIXTURE::VideoPlayer_VIDEO;
using PLAYER_FIXTURE::CDVDMsgType;using PLAYER_FIXTURE::CDVDMsg;
using PLAYER_FIXTURE::IDVDStreamPlayer;using PLAYER_FIXTURE::DVD_PLAYSPEED_NORMAL;
using PLAYER_FIXTURE::DVD_PLAYSPEED_PAUSE;
void frame(Parent& p,CVideoPlayerVideo& v,double pts=1000000){
 SStartMsg m{};m.player=VideoPlayer_VIDEO;m.epoch=v.GetSyncEpoch();m.timestamp=pts;
 p.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,m));p.HandleMessages();
 p.m_CurrentVideo.packets=1;p.m_CurrentAudio.id=-1;
 assert(!p.WaitForActiveAreaDetection());p.SynchronizeStreams(false);p.HoldStep();
 assert(::aml_subtitle_active_area_startup_held(p.m_CurrentVideo.hint.subtitleProbeSource));
 assert(p.m_clock.speed==0&&v.appliedSpeed==0);
}
void flush(Parent& p,CVideoPlayerVideo& v){
 p.FlushBuffers(2000000,true,true,{},true);assert(!p.WaitForActiveAreaDetection());
 s_detectWorker.Stop();
 for(int n=0;v.IsFlushPending()&&n<8;++n){v.Step();v.m_pVideoCodec->ready=true;}
 assert(!v.IsFlushPending()&&p.ContinueParentLifecycle());
}
void scan(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& r){
 XFILE::CFile file;DetectInput input{file,r};uint8_t b=0;
 for(int n=1;n<=14;++n){
  if(detect_avio_seek(&input,n,0)<0||detect_avio_read(&input,&b,1)<0)return;
  source->completedPositions=n;
 }
 detect_publish_subtitle(r,source,{20,20,0,0});s_detectState=DV_DETECT_SKIPPED;
}
int main(){
 ready();CAMLSession sessionValue;init(sessionValue);
 for(bool nativeDV:{false,true})for(bool paused:{false,true}){
  settingsValue.wait=true;settingsValue.cache=false;cacheLevel=100;entries=reads=seeks=0;
  aml_dv_detect_set_file("logged-start");aml_subtitle_active_area_startup_begin();
  const auto selected=aml_subtitle_active_area_source(0,173);
  CVideoPlayerVideo video;Parent p(video);PLAYER_FIXTURE::checkPause=[&]{assert(p.m_clock.speed==0&&video.appliedSpeed==0);};p.m_CurrentVideo.hint.subtitleProbeSource=selected;
  p.m_CurrentAudio.id=-1;p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selected));
  std::promise<void> six;
  scanHook=[&](const auto& source,const CAMLNativeWorker::Run& r){
   XFILE::CFile file;DetectInput input{file,r};uint8_t b=0;
   for(int n=1;n<=6;++n){assert(detect_avio_read(&input,&b,1)>0);source->completedPositions=n;}
   six.set_value();assert(!r.WaitFor(30s));
  };
  const auto checking=gateChecks.load();
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  until([&]{return gateChecks>checking;});
  assert(entries==0&&reads==0&&seeks==0&&s_detectSource->pending);
  // Audio-first startup remains runnable; no worker/native lock blocks player.
  assert(!p.WaitForActiveAreaDetection());frame(p,video);
  assert(six.get_future().wait_for(2s)==std::future_status::ready);
  assert(s_detectSource->completedPositions==6);
  flush(p,video);entries=reads=seeks=0;
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selected));
  scanHook=scan;cacheLevel=100;const auto restartCheck=gateChecks.load();
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}
  until([&]{return gateChecks>restartCheck;});
  cacheLevel=10;std::this_thread::sleep_for(30ms);
  assert(entries==0&&reads==0&&seeks==0&&s_detectSource->pending&&!s_detectCacheStarved);
  assert(aml_subtitle_active_area_startup_pending(selected));
  if(paused)p.RequestedSpeed(DVD_PLAYSPEED_PAUSE);
  frame(p,video,2000000);
  until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();p.HoldStep();
  assert(entries==1&&reads==14&&seeks==14&&s_detectSource->completedPositions==14);
  assert(!s_detectCacheStarved&&!p.WaitForActiveAreaDetection());
  p.HoldStep();assert(p.m_clock.speed==(paused?0:DVD_PLAYSPEED_NORMAL));
  uint16_t t,b,l,r;assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true)&&t==20);PLAYER_FIXTURE::checkPause={};
 }

 // Recovery can temporarily lose an established hold without replacing the run.
 settingsValue.wait=true;settingsValue.cache=false;cacheLevel=100;
 select("lost-hold",false);aml_subtitle_active_area_startup_begin();
 const auto recovered=aml_subtitle_active_area_source();
 CVideoPlayerVideo recoveryVideo;Parent recovery(recoveryVideo);PLAYER_FIXTURE::checkPause=[&]{assert(recovery.m_clock.speed==0&&recoveryVideo.appliedSpeed==0);};
 recovery.m_CurrentVideo.hint.subtitleProbeSource=recovered;
 recovery.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;recovery.m_CurrentAudio.id=-1;
 std::promise<void> firstRead,readAgain;auto again=readAgain.get_future();entries=reads=seeks=0;
 scanHook=[&](const auto& source,const CAMLNativeWorker::Run& r){
  XFILE::CFile file;DetectInput input{file,r};uint8_t b=0;
  assert(detect_avio_read(&input,&b,1)>0);firstRead.set_value();again.wait();
  assert(detect_wait_for_cache(r,80,0)==100);
  assert(detect_avio_seek(&input,2,0)>=0&&detect_avio_read(&input,&b,1)>0);
  detect_publish_subtitle(r,source,{20,20,0,0});
 };
 aml_dv_detect_active_area_start();frame(recovery,recoveryVideo);
 assert(firstRead.get_future().wait_for(2s)==std::future_status::ready);
 recovery.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_WAITSYNC;
 assert(!recovery.WaitForActiveAreaDetection());cacheLevel=10;
 const auto lossCheck=gateChecks.load();readAgain.set_value();until([&]{return gateChecks>lossCheck;});
 std::this_thread::sleep_for(30ms);assert(reads==1&&seeks==0&&s_detectSource->pending&&!s_detectCacheStarved);
 recovery.SynchronizeStreams(false);recovery.HoldStep();
 until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();recovery.HoldStep();
 assert(reads==2&&seeks==1&&!s_detectCacheStarved);PLAYER_FIXTURE::checkPause={};

 // Stop and source replacement while a run waits must promptly join without I/O.
 for(bool replace:{false,true}){
  settingsValue.wait=true;select("stop-deferred",false);aml_subtitle_active_area_startup_begin();
  const auto checking=gateChecks.load();entries=reads=0;scanHook=scan;
  aml_dv_detect_active_area_start();until([&]{return gateChecks>checking;});
  assert(s_detectSource->pending&&entries==0);
  if(replace)aml_dv_detect_set_file("new-file");
  const auto start=std::chrono::steady_clock::now();aml_dv_detect_active_area_stop();
  assert(std::chrono::steady_clock::now()-start<500ms&&entries==0&&reads==0);
 }
 // Terminal EOF before first picture must retire an already deferred worker.
 select("terminal",false);aml_subtitle_active_area_startup_begin();
 const auto terminal=aml_subtitle_active_area_source();const auto checking=gateChecks.load();
 aml_dv_detect_active_area_start();until([&]{return gateChecks>checking;});
 TerminalEOF(terminal,false); // a decoder reset/drain is not terminal input
 assert(aml_subtitle_active_area_startup_pending(terminal)&&s_detectSource->pending);
 TerminalEOF(terminal,true);
 until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();assert(!aml_subtitle_active_area_startup_pending(terminal));
 // Known unsupported sources and cache reuse must return without any hold.
 for(bool cached:{false,true}){
  settingsValue.cache=cached;select(cached?"cached":"bluray://unsupported",false);
  aml_subtitle_active_area_startup_begin();entries=reads=0;aml_dv_detect_active_area_start();
  until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();assert(entries==0&&reads==0);
  if(cached)assert(s_detectSource->subtitleResult&&s_detectSource->subtitleResult->top==20);
 }
 settingsValue.cache=false;
 // With waiting disabled, the same 100%-then-critical ordering still aborts.
 settingsValue.wait=false;select("background",false);aml_subtitle_active_area_startup_begin();
 cacheLevel=100;s_detectThrottleActive=true;s_detectLastCacheCheckMs=0;s_detectCacheStarved=false;
 scanHook=[&](const auto&,const CAMLNativeWorker::Run& r){
  assert(detect_wait_for_cache(r,80,0)==100);cacheLevel=10;
  XFILE::CFile file;DetectInput input{file,r};uint8_t b=0;
  assert(detect_avio_read(&input,&b,1)==AVERROR_EXIT&&s_detectCacheStarved);
 };
 aml_dv_detect_active_area_start();until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 assert(s_detectCacheStarved&&!aml_subtitle_active_area_startup_pending(aml_subtitle_active_area_source()));
 std::cout<<"PASS: connected initial/replacement I/O admission,6/14 seek,14/14 finish,pause,Stop,terminal,cache,unsupported,waitOFF guard\n";
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    code=source();run(code)
    if args.negative_controls:
        for label,old,new in [
            ('no startup I/O deferral','if (s_detectSource->startupHeld->load())\n        return true;','if (true)\n        return true;'),
            ('temporary EOF cancels replacement','if (m_inputEnded && m_syncState == IDVDStreamPlayer::SYNC_STARTING)','if ((m_inputEnded || true) && m_syncState == IDVDStreamPlayer::SYNC_STARTING)'),
            ('hold published before pause','SetCaching(CACHESTATE_INIT);\n    aml_subtitle_active_area_startup_set_held(m_CurrentVideo.hint.subtitleProbeSource, true);','aml_subtitle_active_area_startup_set_held(m_CurrentVideo.hint.subtitleProbeSource, true);\n    SetCaching(CACHESTATE_INIT);'),
            ('waitOFF loses buffer protection','if (s_detectThrottleActive.load() && !detect_startup_wait_active(*run))','if (false)'),
            ('EOF strands deferred worker','::aml_subtitle_active_area_startup_complete(m_hints.subtitleProbeSource);','(void)0; /* no retirement */'),
        ]:
            # TerminalEOF was assembled in global scope, without qualification.
            if label.startswith('EOF'):old=old.removeprefix('::')
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
