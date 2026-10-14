#!/usr/bin/env python3
"""PM4K startup signal through actual player/worker synchronization and AVIO gates.

GUI/Python publication boundaries are separately checked; device decoding and
rendered notifications remain device verification, not host fixture claims.
"""
import argparse
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
ADMISSION = runpy.run_path(str(ROOT / 'tools/test-active-area-admission.py'))
function, run = ADMISSION['function'], ADMISSION['run']

TEST = r'''
uint64_t publish(){PM4K_STARTUP::Publish(true);return PM4K_STARTUP::Capture("script.plexmod");}
void currentFrame(Parent& p,CVideoPlayerVideo& v){
 SStartMsg m{};m.player=VideoPlayer_VIDEO;m.epoch=v.GetSyncEpoch();m.timestamp=1000000;
 p.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,m));p.HandleMessages();
 p.m_CurrentVideo.packets=1;p.m_CurrentAudio.id=-1;
 p.SynchronizeStreams(false);p.HoldStep();
}
void startScan(){const auto checking=gateChecks.load();{CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_start();}until([&]{return gateChecks>checking;});}
void noIO(Parent& p){
 p.HoldStep();std::this_thread::sleep_for(20ms);
 assert(!aml_subtitle_active_area_startup_held(p.m_CurrentVideo.hint.subtitleProbeSource));
 assert(entries==0&&reads==0&&seeks==0&&s_detectSource->pending&&!s_detectCacheStarved);
}
int main(){
 ready();CAMLSession sessionValue;init(sessionValue);
 // Current addon signal: initial ready -> forward seek -> ready during delay ->
 // return seek -> clear -> current epoch ready. No early pause or media I/O.
 for(bool nativeDV:{false,true})for(bool paused:{false,true})for(bool resume:{false,true}){
  settingsValue.wait=true;settingsValue.cache=false;cacheLevel=100;
  const auto token=publish();aml_dv_detect_set_file("pm4k-current");aml_subtitle_active_area_startup_begin(token);
  auto selected=aml_subtitle_active_area_source(0,173);
  assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selected));
  CVideoPlayerVideo v;Parent p(v);p.m_CurrentVideo.hint.subtitleProbeSource=selected;
  p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;
  scanHook=scan;entries=reads=seeks=0;startScan();currentFrame(p,v);noIO(p);
  assert(p.m_callback.starts==1&&p.m_clock.speed==DVD_PLAYSPEED_NORMAL&&v.appliedSpeed==DVD_PLAYSPEED_NORMAL);
  flush(p,v);assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selected));startScan();
  if(!resume){
   currentFrame(p,v);noIO(p); // PM4K's deliberate stabilization delay
   assert(p.m_clock.speed==DVD_PLAYSPEED_NORMAL);
   flush(p,v);assert(aml_subtitle_active_area_configure(1920,1080,nativeDV,true,selected));startScan();
  }
  cacheLevel=10;PM4K_STARTUP::Publish(false);noIO(p); // flag clear alone is insufficient
  SStartMsg stale{};stale.player=VideoPlayer_VIDEO;stale.epoch=v.GetSyncEpoch()-1;
  p.queue.Put(std::make_shared<CDVDMsgType<SStartMsg>>(CDVDMsg::PLAYER_STARTED,stale));p.HandleMessages();noIO(p);
  if(paused)p.RequestedSpeed(DVD_PLAYSPEED_PAUSE);
  PLAYER_FIXTURE::checkPause=[&]{assert(p.m_clock.speed==0&&v.appliedSpeed==0);};
  currentFrame(p,v);until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();p.HoldStep();
  assert(entries==1&&reads==14&&seeks==14&&s_detectSource->completedPositions==14&&!s_detectCacheStarved);
  assert(p.m_clock.speed==(paused?0:DVD_PLAYSPEED_NORMAL));PLAYER_FIXTURE::checkPause={};
 }
 // Missing/expired/consumed/cleared tokens cannot make a native open inherit
 // an unrelated Home flag, even when the global value remains initializing.
 for(int kind=0;kind<4;++kind){
  settingsValue.wait=true;settingsValue.cache=false;cacheLevel=100;
  auto token=publish();
  if(kind==0)token=0;
  if(kind==1){const auto old=PM4K_STARTUP::Clock::now()-PM4K_STARTUP::MAX_WAIT;
   PM4K_STARTUP::Publish(true,old);token=PM4K_STARTUP::Capture("script.plexmod",old);}
  if(kind==2)assert(PM4K_STARTUP::Claim(token)==token);
  if(kind==3)PM4K_STARTUP::Publish(false);
  select("unrelated-native",false);aml_subtitle_active_area_startup_begin(token);
  assert(s_detectSource->pm4kStartupToken==0);
  CVideoPlayerVideo v;Parent p(v);p.m_CurrentVideo.hint.subtitleProbeSource=aml_subtitle_active_area_source();
  entries=reads=seeks=0;scanHook=scan;startScan();currentFrame(p,v);
  until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();p.HoldStep();
  assert(entries==1&&reads==14&&!s_detectCacheStarved);
 }
 // Stop/replacement cannot wait for an orphaned addon flag, even under native admission.
 for(bool replace:{false,true}){
  settingsValue.wait=true;select("cancel-pm4k",false);aml_subtitle_active_area_startup_begin(publish());
  scanHook=scan;entries=reads=seeks=0;startScan();
  if(replace)aml_dv_detect_set_file("native-replacement");
  const auto start=PM4K_STARTUP::Clock::now();
  {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_stop();}
  assert(PM4K_STARTUP::Clock::now()-start<500ms&&entries==0&&reads==0);
 }
 // Wait disabled: initialization still defers competing I/O, never pauses;
 // after clear ordinary background critical-buffer protection still applies.
 settingsValue.wait=false;cacheLevel=100;select("pm4k-background",false);
 aml_subtitle_active_area_startup_begin(publish());const auto selected=aml_subtitle_active_area_source();
 CVideoPlayerVideo v;Parent p(v);p.m_CurrentVideo.hint.subtitleProbeSource=selected;
 entries=reads=seeks=0;s_detectCacheStarved=false;
 scanHook=[&](const auto&,const CAMLNativeWorker::Run& r){
  cacheLevel=10;s_detectLastCacheCheckMs=0;XFILE::CFile f;DetectInput input{f,r};uint8_t b;
  assert(detect_avio_read(&input,&b,1)==AVERROR_EXIT&&s_detectCacheStarved);
 };
 startScan();currentFrame(p,v);noIO(p);assert(p.m_clock.speed==DVD_PLAYSPEED_NORMAL);
 flush(p,v);assert(aml_subtitle_active_area_configure(1920,1080,false,true,selected));startScan();
 PM4K_STARTUP::Publish(false);noIO(p);currentFrame(p,v);
 until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 assert(entries==1&&reads==0&&s_detectCacheStarved);
 // Cached/unsupported exits never wait for the external signal.
 settingsValue.wait=true;cacheLevel=100;
 for(bool cached:{false,true}){
  settingsValue.cache=cached;select(cached?"cache":"bluray://unsupported",false);
  aml_subtitle_active_area_startup_begin(publish());entries=reads=seeks=0;aml_dv_detect_active_area_start();
  until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();assert(entries==0&&reads==0);
 }
 // Terminal input before video readiness retires a worker despite active PM4K flag.
 settingsValue.cache=false;select("terminal-pm4k",false);aml_subtitle_active_area_startup_begin(publish());
 const auto terminal=aml_subtitle_active_area_source();scanHook=scan;startScan();TerminalEOF(terminal,true);
 until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 std::cout<<"PASS: PM4K initial/forward/delay/return/resume, current-sync, single14/14 scan, pause, Stop/replacement, waitOFF guard, cache/unsupported/terminal\n";
}
'''


SIGNAL = runpy.run_path(str(ROOT / 'tools/test-pm4k-startup-signal.py'))
boundary_source = SIGNAL['source']


def source():
    prepare = function((ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text(), 'void CVideoPlayer::Prepare()')
    assert 'm_item.GetProperty(PM4K_STARTUP::ITEM_TOKEN).asUnsignedInteger()' in prepare
    code = ADMISSION['source']()
    return code.replace(function(code, 'int main()'), TEST)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
    run(boundary_source())
    code = source()
    run(code)
    if args.negative_controls:
        header = (ROOT / 'xbmc/utils/PM4KStartup.h').read_text()
        for label, old, new in [
            ('wrong addon owns signal', 'if (addon != "script.plexmod")', 'if (false && addon != "script.plexmod")'),
            ('replayed token owns later native item', 'token <= signal.consumed ||', 'false ||'),
            ('orphan signal never expires', 'now - signal.published < MAX_WAIT;', 'now - signal.published < MAX_WAIT * 100;'),
        ]:
            assert old in header, label
            run(boundary_source(), ('utils/PM4KStartup.h', header.replace(old, new)), negative=True)
            print('REJECTED:', label)
        for label, old, new in [
            ('pause during PM4K startup', '!aml_subtitle_active_area_startup_external_pending(m_CurrentVideo.hint.subtitleProbeSource)', 'true'),
            ('scan during PM4K startup', '!PM4K_STARTUP::Pending(s_detectSource->pm4kStartupToken)', 'true'),
            ('flag clear bypasses current synchronization', '                    videoReady;', '                    true;'),
            ('stream configuration loses startup token', 's_detectSource->pm4kStartupToken = previous->pm4kStartupToken;', 's_detectSource->pm4kStartupToken = 0;'),
            ('waitOFF skips current-sync I/O deferral', '(!s_detectSource->pm4kStartupToken || s_detectSource->startupVideoReady)', 'true'),
            ('waitOFF skips addon I/O deferral', '!PM4K_STARTUP::Pending(s_detectSource->pm4kStartupToken)', '(!s_detectSource->startupScan || !PM4K_STARTUP::Pending(s_detectSource->pm4kStartupToken))'),
        ]:
            assert old in code, label
            run(code.replace(old, new), negative=True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
