#!/usr/bin/env python3
"""Execute application player close/replacement methods with recording players.

Checks pending close preserves the original target and destruction occurs outside
its application lock. Actual media threads/GUI callbacks are not simulated here.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
splice = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def function(source, signature):
    # The open of the router reads the volume of the application from the stub
    # components; the harnesses that share the prelude share the splice.
    return splice(source, signature).replace(
        'CServiceBroker::GetAppComponents().GetComponent<CApplicationVolumeHandling>()',
        'appComponents.GetComponent<CApplicationVolumeHandling>()')
PRELUDE = r'''
#include <cassert>
#include <algorithm>
#include <vector>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
struct CEvent {bool signaled=false;void Set(){signaled=true;}};
struct CCriticalSection {
  bool held=false;
  void lock(){assert(!held);held=true;}
  void unlock(){assert(held);held=false;}
};
struct CFileItem {int id=0;bool disc=false,video=true;bool IsDiscImage()const{return disc;}bool IsDVDFile()const{return false;}bool IsVideo()const{return video;}};
struct CPlayerOptions {};
struct IPlayerCallback {};
// The volume of the application (CApplicationVolumeHandling)
struct CApplicationVolumeHandling {float ratio=1;bool muted=false;
  float GetVolumeRatio()const{return ratio;}bool IsMuted()const{return muted;}};
static auto appVolume=std::make_shared<CApplicationVolumeHandling>();
struct AppComponents {template<class T> std::shared_ptr<T> GetComponent()const{return appVolume;}};
static const AppComponents appComponents;
struct IPlayer {
  std::string m_name="old",m_type="video";
  bool playing=false,canClose=false;int closes=0,opens=0,lastItem=-1;
  float volume=-1;bool muted=false;int volumes=0,mutes=0;
  void SetVolume(float v){++volumes;volume=v;}void SetMute(bool m){++mutes;muted=m;}
  CCriticalSection* applicationLock=nullptr;int* destroyed=nullptr;
  ~IPlayer(){assert(!applicationLock||!applicationLock->held);if(destroyed)++*destroyed;}
  bool IsPlaying()const{return playing;}
  bool CloseFile(bool=false){++closes;return canClose;}
  // A backend that processes messages during the open (UPnP) runs duringOpen.
  std::function<void()> duringOpen;
  bool OpenFile(const CFileItem& file,const CPlayerOptions&){
    ++opens;if(auto hook=std::exchange(duringOpen,nullptr))hook();
    lastItem=file.id;playing=true;return true;}
};
struct CPlayerCoreFactory {
  std::string GetDefaultPlayer(const CFileItem&)const{return "new";}
};
struct Timer {int expires=0;void SetExpired(){++expires;}};
struct CApplicationPlayer {
  mutable CCriticalSection m_playerLock;
  std::shared_ptr<IPlayer> m_pPlayer,m_closingPlayer;
  std::vector<std::shared_ptr<CEvent>> m_closeCompletionEvents,m_retiredCloseCompletionEvents;
  bool m_closeAcknowledged=false,m_waitForPlaybackStop=false,m_shutdown=false;std::map<std::thread::id,int> m_controls;
  enum class Supersession {NONE,OPEN,STOP} m_supersededBy=Supersession::NONE;
  uint64_t m_supersededAt=0,m_intentSequence=0,m_opensCancelledAt=0,m_lastAcceptedIntent=0;std::vector<uint64_t> m_openIntents;bool m_openedVideo=false;
  uint64_t BeginOpen();void EndOpen(uint64_t);bool IsOpening()const{return !m_openIntents.empty();}
@INLINE_INTENTS@
  bool IsPlayingOpenedVideo()const;uint64_t GetOpenGeneration()const{return m_openGeneration;}
  bool OpenRequested()const;bool StopRequested()const;void Supersede(Supersession);
  uint64_t m_openGeneration=0;std::optional<uint64_t> m_cleanupGeneration;bool m_cleanupPending=false;
  struct {
    std::shared_ptr<CFileItem> pItem;CPlayerOptions options;
    std::string playerName;IPlayerCallback* callback=nullptr;uint64_t intent=0;
  }m_nextItem;
  Timer m_audioStreamUpdate,m_videoStreamUpdate,m_subtitleStreamUpdate;
  int created=0;
  // The type and the open hook of the next created player
  std::string createdType="video";std::function<void()> createdDuringOpen;
  std::shared_ptr<IPlayer> GetInternal();std::shared_ptr<const IPlayer> GetInternal()const{return m_pPlayer;}
  bool HasPlayer() {return bool(GetInternal());}
  bool ClosePlayer(bool=false,std::shared_ptr<CEvent> = {});void ResetPlayer(std::shared_ptr<CEvent> = {});void ContinueClose();void OnPlaybackStopped();void CompleteCloseCompletions();
  void OpenNext(const CPlayerCoreFactory&);
  bool OpenFile(const CFileItem&,const CPlayerOptions&,const CPlayerCoreFactory&,
                const std::string&,IPlayerCallback&,uint64_t intent);
  // A direct call in a test is a new request.
  bool OpenFile(const CFileItem& f,const CPlayerOptions& o,const CPlayerCoreFactory& fa,
                const std::string& n,IPlayerCallback& c){return OpenFile(f,o,fa,n,c,++m_intentSequence);}
  void CreatePlayer(const CPlayerCoreFactory&,const std::string& name,IPlayerCallback&) {
    assert(!m_pPlayer);++created;m_pPlayer=std::make_shared<IPlayer>();
    m_pPlayer->m_name=name;m_pPlayer->applicationLock=&m_playerLock;
    m_pPlayer->m_type=createdType;m_pPlayer->duringOpen=std::exchange(createdDuringOpen,nullptr);
  }
};
'''
# A control of a stopped item resumes, pauses or seeks the item.
CONTROLS_PLAYER = '''
  int controls=0;double startTime=-1,startPercent=-1;int64_t startOffset=-1;
  void Pause(){++controls;}void Seek(bool,bool,bool){++controls;}void SeekPercentage(float){++controls;}
  int SeekChapter(int){++controls;return 0;}bool SeekScene(bool){++controls;return true;}
  void SeekTime(int64_t){++controls;}bool SeekTimeRelative(int64_t){++controls;return true;}
  // A control that runs a modal dialog of the player (the Game OSD) runs frames.
  std::function<void()> duringControl;
  void SetSpeed(float){++controls;if(auto hook=std::exchange(duringControl,nullptr))hook();}void SetTempo(float){++controls;}void FrameAdvance(int){++controls;}
  bool OnAction(const CAction&){++controls;return true;}'''
CONTROLS_ROUTER = '''
  std::shared_ptr<IPlayer> GetControllable();bool SeekWaitingItem(int64_t,bool);
  bool IsPlaying()const;bool IsPlayingAudio()const{return false;}mutable std::function<void()> queryHook;
  bool IsPlayingVideo()const{if(queryHook){auto hook=std::move(queryHook);hook();}return IsPlaying();}
  int64_t GetTime()const{return 0;}
  void Pause();void Seek(bool,bool=false,bool=false);void SeekPercentage(float);int SeekChapter(int);
  bool SeekScene(bool);void SeekTime(int64_t);void SeekTimeRelative(int64_t);void SetSpeed(float);
  void SetPlaySpeed(float);void SetTempo(float);void FrameAdvance(int);void SetMute(bool);
  void SetVolume(float);bool OnAction(const CAction&);'''
# The inline intent predicates are the production ones.
HEADER = (ROOT / 'xbmc/application/ApplicationPlayer.h').read_text()

PRELUDE = PRELUDE.replace('@INLINE_INTENTS@', HEADER[HEADER.index('  void CancelOpens()'):
                                                      HEADER.index('  // The player plays the video item')])
TESTS = r'''
// The user stopped a closing player: no playback control reaches the player, also
// the speed reset of a newer open, until the close completes. A control before
// the stop reaches the player.
static void controls_during_close(){
  CFileItem file;file.id=1;CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  CApplicationPlayer app;auto old=std::make_shared<IPlayer>();old->playing=true;app.m_pPlayer=old;
  const auto all=[&]{app.Pause();app.Seek(true);app.SeekPercentage(50);app.SeekChapter(2);app.SeekScene(true);
    app.SeekTime(1000);app.SeekTimeRelative(1000);app.SetPlaySpeed(1);app.SetSpeed(2);app.SetTempo(1.5f);
    app.FrameAdvance(1);app.SetMute(true);app.SetVolume(0.5f);app.OnAction(CAction{});};
  all();
  // the volume and the mute reach the player like the other controls
  assert(old->controls==12&&old->volumes==1&&old->mutes==1);
  app.m_waitForPlaybackStop=true;
  assert(!app.ClosePlayer());
  assert(app.m_closingPlayer==old&&app.GetInternal()==old);
  all();
  CFileItem newer;newer.id=2;
  assert(app.OpenFile(newer,options,factory,"new",callback));
  all();
  assert(old->controls==12&&old->volumes==1&&old->mutes==1);
  old->canClose=true;app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
  auto current=app.GetInternal();
  assert(current&&current!=old&&current->lastItem==2);
  all();
  assert(current->controls==12&&old->controls==12&&old->volumes==1&&old->mutes==1);
}
// The player of an open plays at the volume of the application: the player of a
// direct open, and the player of a newer item that waited for the close of a
// remote player (OpenNext), also after a volume change during the close. An
// open that a newer open superseded during the open sets no volume.
static void volume_after_open(){
  CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  *appVolume={0.2f,false};
  CApplicationPlayer app;app.createdType="remote";
  CFileItem first;first.id=1;
  assert(app.OpenFile(first,options,factory,"new",callback));
  auto old=app.GetInternal();
  assert(old&&old->volume==0.2f&&!old->muted&&old->volumes==1&&old->mutes==1);
  CFileItem newer;newer.id=2;
  assert(app.OpenFile(newer,options,factory,"new",callback)&&app.m_nextItem.pItem);
  assert(app.m_closingPlayer==old&&old->volumes==1);
  *appVolume={0.5f,true};
  old->canClose=true;app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
  auto current=app.GetInternal();
  assert(current&&current!=old&&current->lastItem==2);
  assert(current->volume==0.5f&&current->muted&&current->volumes==1&&current->mutes==1&&old->volumes==1);
  // a newer open during the open of a remote player supersedes the open
  CFileItem latest;latest.id=3;
  CApplicationPlayer other;other.createdType="remote";
  other.createdDuringOpen=[&]{assert(other.OpenFile(latest,options,factory,"new",callback));};
  assert(!other.OpenFile(first,options,factory,"new",callback));
  auto superseded=other.GetInternal()?other.GetInternal():other.m_closingPlayer;
  assert(superseded&&superseded->lastItem==1&&superseded->volumes==0&&superseded->mutes==0);
  *appVolume={};
}
// The speed reset of an open takes the player once: the close of the player and
// a newer open during the reset do not move the reset to the newer player. A
// control that took the player before the close (also on another thread: JSON-RPC,
// Python) reaches the old player before the newer player opens: the old backend
// changes no shared state of the newer playback (the data cache, the pause
// notification) (Sol's round 39 probe). The close completes after the control,
// without another close of the player. The main thread runs between the control
// and the backend call.
static void speed_after_replacement(){
  CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  CApplicationPlayer app;auto old=std::make_shared<IPlayer>();old->playing=true;app.m_pPlayer=old;
  app.queryHook=[&]{std::thread([&]{
    app.m_waitForPlaybackStop=true;assert(!app.ClosePlayer());
    CFileItem newer;newer.id=2;assert(app.OpenFile(newer,options,factory,"new",callback));
    old->canClose=true;app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
    assert(app.m_closingPlayer==old&&app.m_nextItem.pItem&&app.created==0&&old->controls==0);}).join();};
  app.SetPlaySpeed(1);
  assert(old->controls==1&&app.m_closingPlayer==old&&app.created==0);
  app.ContinueClose();app.OpenNext(factory);
  auto current=app.GetInternal();
  assert(current&&current!=old&&current->lastItem==2&&current->controls==0&&old->controls==1&&old->closes==3);
}
// A control on the main thread that runs a modal dialog of the player (the Game
// OSD that a pause of a game opens) runs the frames of the application below the
// control. A stop in the dialog completes the close in those frames, and a newer
// open plays: the close waits only for the controls of other threads (Sol's round
// 40 finding). The control ends without another close of the player.
static void control_runs_close(){
  CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  CApplicationPlayer app;auto old=std::make_shared<IPlayer>();old->playing=true;app.m_pPlayer=old;
  old->duringControl=[&]{
    old->canClose=true;assert(app.ClosePlayer());
    assert(!app.m_closingPlayer&&!app.GetInternal());
    CFileItem newer;newer.id=2;assert(app.OpenFile(newer,options,factory,"new",callback));
    assert(app.GetInternal()&&app.GetInternal()!=old&&app.GetInternal()->lastItem==2);};
  app.SetPlaySpeed(0);
  auto current=app.GetInternal();
  assert(current&&current!=old&&current->controls==0&&old->controls==1&&old->closes==1);
  assert(app.m_controls.empty());
}
// A newer item that waits for the close of the old player takes the time of an
// absolute or relative seek, like a new request for the time: the start time
// (VideoPlayer, UPnP) and the start offset of the item (PAPlayer). A relative
// seek from a start in percent, a seek in a cue sheet track and a seek in an
// item with a saved player state (the disc position) drop, like the other
// seeks, which need the opened item.
static void seek_waiting_item(){
  for(int kind=0;kind<4;++kind){
   CPlayerOptions options;options.starttime=kind==1?0:20;options.startpercent=kind==1?40:0;
   if(kind==3)options.state="disc";
   CPlayerCoreFactory factory;IPlayerCallback callback;
   CApplicationPlayer app;auto old=std::make_shared<IPlayer>();old->playing=true;app.m_pPlayer=old;
   app.m_waitForPlaybackStop=true;assert(!app.ClosePlayer());
   CFileItem newer;newer.id=2;newer.startOffset=kind==1?0:20000;newer.cue=kind==2;
   assert(app.OpenFile(newer,options,factory,"new",callback));
   const auto& next=app.m_nextItem;
   if(kind==0){
    app.SeekTime(50000);assert(next.options.starttime==50&&next.pItem->startOffset==50000);
    app.SeekTimeRelative(-10000);assert(next.options.starttime==40&&next.pItem->startOffset==40000);
    app.SeekTimeRelative(-60000);assert(next.options.starttime==0&&next.pItem->startOffset==0);
    app.SeekTime(30000);app.SeekTimeRelative(5000);
   }
   if(kind==1){
    app.SeekTimeRelative(5000);assert(next.options.startpercent==40&&next.options.starttime==0&&next.pItem->startOffset==0);
    app.SeekTime(30000);assert(next.options.startpercent==0&&next.options.starttime==30&&next.pItem->startOffset==30000);
    app.SeekTimeRelative(5000);
   }
   if(kind>=2){
    app.SeekTime(0);app.SeekTime(50000);app.SeekTimeRelative(-5000);
    assert(next.options.starttime==20&&next.pItem->startOffset==20000&&next.options.state==(kind==3?"disc":""));
   }
   assert(old->controls==0);
   old->canClose=true;app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
   auto current=app.GetInternal();
   assert(current&&current!=old&&current->lastItem==2&&current->controls==0);
   assert(current->startTime==(kind>=2?20:35)&&current->startOffset==(kind>=2?20000:35000)&&current->startPercent==0);
  }
}
// A stopped player that closes is not playing. A newer open that waits for the
// close continues the playback, like an open in progress.
static void state_during_close(){
  CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  CApplicationPlayer app;auto old=std::make_shared<IPlayer>();old->playing=true;app.m_pPlayer=old;
  assert(app.IsPlaying());
  app.m_waitForPlaybackStop=true;
  assert(!app.ClosePlayer());
  assert(app.GetInternal()==old&&old->playing&&!app.IsPlaying());
  CFileItem newer;newer.id=2;
  assert(app.OpenFile(newer,options,factory,"new",callback));
  assert(app.m_nextItem.pItem&&app.IsPlaying());
  old->canClose=true;app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
  auto current=app.GetInternal();
  assert(current&&current!=old&&current->lastItem==2&&app.IsPlaying());
  // a stop that ends the pending open leaves nothing playing
  current->canClose=false;CFileItem latest;latest.id=3;
  assert(app.OpenFile(latest,options,factory,"other",callback));
  assert(app.m_nextItem.pItem&&app.IsPlaying());
  assert(!app.ClosePlayer());
  assert(!app.m_nextItem.pItem&&!app.IsPlaying());
}
int main(){
  state_during_close();
  controls_during_close();
  volume_after_open();
  speed_after_replacement();
  control_runs_close();
  seek_waiting_item();
  shutdown_cases();
  discarded_notifications();
  CFileItem file;file.id=1;CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  for(bool playing:{false,true}) {
    CApplicationPlayer app;int destroyed=0;
    auto original=std::make_shared<IPlayer>();original->playing=playing;
    original->applicationLock=&app.m_playerLock;original->destroyed=&destroyed;
    app.m_pPlayer=original;app.m_waitForPlaybackStop=true;
    assert(app.OpenFile(file,options,factory,"new",callback));
    assert(app.GetInternal()==original && app.m_closingPlayer==original && app.created==0);
    for(int frame=0;frame<3;++frame) {app.ContinueClose();app.OpenNext(factory);}
    assert(app.GetInternal()==original && app.created==0 && destroyed==0);
    CFileItem newest;newest.id=2;
    assert(app.OpenFile(newest,options,factory,"new",callback));
    original->canClose=true;
    app.ContinueClose();app.OpenNext(factory);
    assert(app.GetInternal()==original && app.created==0); // old STOP notification still queued
    const int acknowledgedCalls=original->closes;
    app.ContinueClose();assert(original->closes==acknowledgedCalls);
    app.OnPlaybackStopped();app.ContinueClose();
    assert(!app.GetInternal() && !app.m_closingPlayer);
    original.reset();assert(destroyed==1);
    app.OpenNext(factory);
    assert(app.created==1 && app.GetInternal()->lastItem==2 && app.GetInternal()->opens==1);
    app.OpenNext(factory);assert(app.created==1 && app.GetInternal()->opens==1);
    assert(app.m_waitForPlaybackStop && !app.m_nextItem.pItem);
  }
  for(bool shutdown:{false,true}) {
    CApplicationPlayer app;
    app.m_pPlayer=std::make_shared<IPlayer>();auto original=app.m_pPlayer;
    app.m_waitForPlaybackStop=true;
    assert(app.OpenFile(file,options,factory,"new",callback));
    assert(!app.ClosePlayer(shutdown)); // Stop cancels only queued replacement, not retirement.
    assert(!app.m_nextItem.pItem && app.m_closingPlayer==original);
    if(shutdown) assert(!app.OpenFile(file,options,factory,"new",callback));
    app.OnPlaybackStopped();app.ContinueClose();assert(app.GetInternal()==original);
    original->canClose=true;app.ContinueClose();app.OpenNext(factory);
    assert(!app.GetInternal() && app.created==0 && app.ClosePlayer(shutdown));
  }
  // A naturally stopped player's last reference is retained until actual close.
  {CApplicationPlayer app;int destroyed=0;app.m_pPlayer=std::make_shared<IPlayer>();
   app.m_pPlayer->applicationLock=&app.m_playerLock;app.m_pPlayer->destroyed=&destroyed;
   app.ResetPlayer();assert(destroyed==0 && app.GetInternal());
   app.m_pPlayer->canClose=true;app.ContinueClose();assert(destroyed==1 && !app.GetInternal());}
  // Even an unexpected current-player change cannot redirect a late close.
  {CApplicationPlayer app;auto original=std::make_shared<IPlayer>();app.m_pPlayer=original;
   app.ResetPlayer();auto replacement=std::make_shared<IPlayer>();app.m_pPlayer=replacement;
   original->canClose=true;app.ContinueClose();
   assert(app.GetInternal()==replacement && replacement->closes==0 && !app.m_closingPlayer);}
  // A newer open during the backend open of a remote player replaces the player:
  // a remote player is never reused, so one backend never runs two opens.
  {CApplicationPlayer app;CFileItem older;older.id=10;CFileItem newer;newer.id=11;
   app.createdType="remote";
   app.createdDuringOpen=[&]{
     // the open published a playing renderer state: only the type replaces the player
     auto opening=app.GetInternal();opening->playing=true;
     assert(app.OpenFile(newer,options,factory,"new",callback));
     assert(app.m_closingPlayer==opening && opening->closes==1 && opening->opens==1 &&
            app.m_nextItem.pItem->id==11);};
   assert(!app.OpenFile(older,options,factory,"new",callback));
   auto first=app.GetInternal();
   assert(app.created==1 && first->opens==1 && app.m_closingPlayer==first && app.m_nextItem.pItem);
   first->canClose=true;app.OnPlaybackStopped();app.ContinueClose();
   assert(!app.GetInternal() && !app.m_closingPlayer);
   app.OpenNext(factory);
   auto second=app.GetInternal();
   assert(app.created==2 && second!=first && second->lastItem==11 && second->opens==1);
   // a later open also replaces the playing remote player
   CFileItem later;later.id=12;
   assert(app.OpenFile(later,options,factory,"new",callback));
   assert(app.m_closingPlayer==second && second->opens==1 && app.m_nextItem.pItem->id==12);}
  // Ordinary playing video stream changes retain the player and its startup epoch domain.
  {CApplicationPlayer app;auto player=std::make_shared<IPlayer>();app.m_pPlayer=player;
   player->playing=true;assert(app.OpenFile(file,options,factory,"old",callback));
   assert(app.GetInternal()==player && player->opens==1 && player->closes==0 && !app.m_closingPlayer);}
}
'''


def main():
    source = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    code = PRELUDE + '\n'.join(function(source, signature) for signature in [
        'std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
        'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::CompleteCloseCompletions()', 'bool CApplicationPlayer::ClosePlayer(',
        'void CApplicationPlayer::OnPlaybackStopped()', 'void CApplicationPlayer::OpenNext(',
        'void CApplicationPlayer::ResetPlayer(', 'bool CApplicationPlayer::OpenFile(',
        'void CApplicationPlayer::Supersede(', 'void CApplicationPlayer::Pause()',
        'void CApplicationPlayer::Seek(bool', 'void CApplicationPlayer::SeekPercentage(',
        'int  CApplicationPlayer::SeekChapter(', 'bool CApplicationPlayer::SeekScene(',
        'void CApplicationPlayer::SeekTime(', 'void CApplicationPlayer::SeekTimeRelative(',
        'void CApplicationPlayer::SetSpeed(', 'void CApplicationPlayer::SetPlaySpeed(',
        'void CApplicationPlayer::SetTempo(', 'void CApplicationPlayer::FrameAdvance(',
        'void CApplicationPlayer::SetMute(', 'void CApplicationPlayer::SetVolume(',
        'bool CApplicationPlayer::OnAction(', 'std::shared_ptr<IPlayer> CApplicationPlayer::GetControllable()',
        'bool CApplicationPlayer::SeekWaitingItem(',
        'bool CApplicationPlayer::IsPlaying() const']) + TESTS
    code = '#include <cmath>\n' + code
    # The playback controls of the router and the players; other harnesses share the prelude.
    for anchor, stubs in (('struct IPlayer {', 'struct CAction {int GetID()const{return 0;}};\nstruct IPlayer {' + CONTROLS_PLAYER),
                          ('struct CPlayerOptions {};', 'struct CPlayerOptions {double startpercent=0,starttime=0;std::string state;};'),
                          ('struct CFileItem {int id=0;', 'struct CFileItem {int id=0;int64_t startOffset=0;bool cue=false;'
                           'void SetStartOffset(int64_t o){startOffset=o;}int64_t GetStartOffset()const{return startOffset;}'
                           'bool HasProperty(const std::string& p)const{return cue&&p=="item_start";}'),
                          ('  bool OpenFile(const CFileItem& file,const CPlayerOptions&){\n    ++opens;',
                           '  bool OpenFile(const CFileItem& file,const CPlayerOptions& o){\n    ++opens;startTime=o.starttime;'
                           'startPercent=o.startpercent;startOffset=file.startOffset;'),
                          ('  bool HasPlayer() {return bool(GetInternal());}', '  bool HasPlayer() {return bool(GetInternal());}' + CONTROLS_ROUTER)):
        assert code.count(anchor) == 1, anchor
        code = code.replace(anchor, stubs, 1)
    # Main-frame retry and shutdown order are source wiring checks, not a full
    # Application runtime. The management methods above execute real bodies.
    application = (ROOT / 'xbmc/application/Application.cpp').read_text()
    frame = function(application, 'void CApplication::FrameMove(')
    assert frame.index('CRenderLifecycle::ProcessAll()') < frame.index('appPlayer->FrameMove()')
    assert frame.index('appPlayer->FrameMove()') < frame.index('appPlayer->OpenNext(')
    stop = function(application, 'bool CApplication::Stop(')
    assert stop.index('if (!appPlayer->ClosePlayer(true))') < stop.index('aml_dv_auto_letterbox_watch_stop()')
    assert 'm_pendingStop = exitCode;' in stop and 'return false;' in stop
    assert 'm_pendingStop && !appPlayer->HasPlayer()' in frame
    assert 'Powerdown()' in frame and 'Reboot()' in frame
    assert 'if (appPlayer->HasPlayer() || appPlayer->HasPendingOpen() || appPlayer->IsOpening())' in function(application, 'void CApplication::StopPlaying(')
    playlist = (ROOT / 'xbmc/PlayListPlayer.cpp').read_text()
    media_stop = playlist[playlist.index('  case TMSG_MEDIA_STOP:'):playlist.index('  case TMSG_MEDIA_PAUSE:')]
    assert 'if (appPlayer->HasPlayer() || appPlayer->HasPendingOpen() ||\n        (!m_bIsDeferredPlayPending && appPlayer->IsOpening()))' in media_stop
    assert 'if (!m_shutdownWatchdogStarted)' in stop
    gate_start = stop.index('  const auto appPlayer = GetComponent<CApplicationPlayer>();')
    gate_end = stop.index('  // Safety net', gate_start)
    gate = stop[gate_start:gate_end]
    latch = stop[stop.index('  if (m_pendingStop)'):stop.index('  CLog::Log')]
    retry = function(frame, 'if (m_pendingStop && !appPlayer->HasPlayer())')
    shutdown = r'''
constexpr int EXITCODE_POWERDOWN=1,EXITCODE_REBOOT=2,EXITCODE_QUIT=3,EXITCODE_RESTARTAPP=4;
struct Power {int downs=0,reboots=0;void Powerdown(){++downs;}void Reboot(){++reboots;}};
constexpr int LOGDEBUG=0, GUI_MSG_PLAYBACK_STARTED=1,GUI_MSG_PLAYBACK_ENDED=2,GUI_MSG_PLAYBACK_STOPPED=3,
 GUI_MSG_PLAYLIST_CHANGED=4,GUI_MSG_PLAYLISTPLAYER_STOPPED=5,GUI_MSG_PLAYLISTPLAYER_STARTED=6,
 GUI_MSG_PLAYLISTPLAYER_CHANGED=7,GUI_MSG_QUEUE_NEXT_ITEM=8;
struct CLog {template<class...T> static void LogF(T&&...) {}};
struct Gui {
  std::vector<int> messages;
  Gui& GetWindowManager(){return *this;}
  int RemoveThreadMessageByMessageIds(int* ids){
    int count=0;
    for(;*ids;++ids) {
      auto end=std::remove(messages.begin(),messages.end(),*ids);
      count+=std::distance(end,messages.end());messages.erase(end,messages.end());
    }
    return count;
  }
};
struct Window {bool ready=true;bool PrepareForShutdown(){return ready;}};
struct CServiceBroker {
  static Window* GetWinSystem(){static Window window;return &window;}
  static Power& GetPowerManager(){static Power power;return power;}
  static Gui* GetGUI(){static Gui gui;return &gui;}
};
static void discard(CApplicationPlayer* appPlayer) @DISCARD@
static void discarded_notifications() {
  for(int terminal:{GUI_MSG_PLAYBACK_STOPPED,GUI_MSG_PLAYBACK_ENDED,GUI_MSG_PLAYBACK_STARTED}) {
    CApplicationPlayer app;auto original=std::make_shared<IPlayer>();
    app.m_pPlayer=original;app.m_waitForPlaybackStop=true;
    CServiceBroker::GetGUI()->messages={terminal,GUI_MSG_PLAYLIST_CHANGED};
    discard(&app);assert(CServiceBroker::GetGUI()->messages.empty());
    CFileItem file;CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
    assert(app.OpenFile(file,options,factory,"new",callback));
    original->canClose=true;app.ContinueClose();app.OpenNext(factory);
    if(terminal==GUI_MSG_PLAYBACK_STARTED) {
      assert(app.GetInternal()==original && app.created==0 && app.m_waitForPlaybackStop);
      app.OnPlaybackStopped();app.ContinueClose();app.OpenNext(factory);
    }
    assert(app.created==1 && app.GetInternal()!=original);
  }
}
struct Shutdown {
  CApplicationPlayer app; std::optional<int> m_pendingStop;int teardowns=0,lastExit=0;
  template<class T> T* GetComponent(){return &app;}
  bool Stop(int exitCode) {
@LATCH@
@GATE@
    ++teardowns;lastExit=exitCode;return true;
  }
  void Frame() {auto* appPlayer=&app; @RETRY@}
};
static void shutdown_cases() {
  for(int code:{EXITCODE_POWERDOWN,EXITCODE_REBOOT,EXITCODE_QUIT,EXITCODE_RESTARTAPP}) {
    Shutdown shutdown;shutdown.app.m_pPlayer=std::make_shared<IPlayer>();
    shutdown.app.m_waitForPlaybackStop=true;
    auto original=shutdown.app.m_pPlayer;
    auto& power=CServiceBroker::GetPowerManager();power={};
    assert(!shutdown.Stop(code));
    for(int frame=0;frame<3;++frame) {shutdown.app.ContinueClose();shutdown.Frame();}
    assert(shutdown.teardowns==0 && shutdown.m_pendingStop==code);
    assert(!shutdown.Stop(EXITCODE_QUIT) && shutdown.m_pendingStop==code);
    original->canClose=true;shutdown.app.ContinueClose();shutdown.Frame();
    assert(shutdown.teardowns==0); // old callback still has to be consumed
    CServiceBroker::GetWinSystem()->ready=false;
    shutdown.app.OnPlaybackStopped();shutdown.app.ContinueClose();shutdown.Frame();
    assert(shutdown.teardowns==0 && shutdown.m_pendingStop==code);
    CServiceBroker::GetWinSystem()->ready=true;
    shutdown.Frame();shutdown.Frame();assert(shutdown.teardowns==1 && shutdown.lastExit==code);
    assert(!shutdown.m_pendingStop && !shutdown.app.HasPlayer());
    assert(power.downs==(code==EXITCODE_POWERDOWN) && power.reboots==(code==EXITCODE_REBOOT));
  }
}
'''
    discard = function(application, '  {\n    // for playing a new item,')
    shutdown = shutdown.replace('@DISCARD@', discard)
    shutdown = shutdown.replace('@LATCH@', latch).replace('@GATE@', gate).replace('@RETRY@', retry)
    code = code.replace(TESTS, shutdown + TESTS)
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    options = parser.parse_args()
    run(code)
    if options.negative_controls:
        for label, before, after in [
            ('detach before close', 'm_closeAcknowledged = original->CloseFile();', 'm_closeAcknowledged = true;'),
            ('ignore old stop notification', ' || m_waitForPlaybackStop)', ')'),
            ('retire wrong owner', 'if (m_pPlayer == original)', 'if (true)'),
            ('lose queued replacement', 'if (m_closingPlayer || m_waitForPlaybackStop || m_shutdown || !m_nextItem.pItem)', 'm_nextItem.pItem.reset(); if (m_closingPlayer || m_waitForPlaybackStop || m_shutdown || !m_nextItem.pItem)'),
            ('shutdown admits replacement', 'if (m_shutdown)', 'if (false)'),
            ('discarded terminal not acknowledged', 'appPlayer->OnPlaybackStopped();', '(void)appPlayer;'),
            ('shutdown tears down early', 'if (!appPlayer->ClosePlayer(true))', 'if (!appPlayer->ClosePlayer(true) && false)'),
            ('close ignores running controls', 'return control.first != thread;', 'return control.first != thread && false;'),
            ('close waits for own thread', 'return control.first != thread;', 'return control.first != thread || true;'),
        ]:
            assert before in code
            run(code.replace(before, after), True)
            print('Negative control rejected at runtime:', label)
    print('Player retirement: PASS (production management; ASan/UBSan; recording players; shutdown source wiring)')


def run(code, expect_failure=False):
    with tempfile.TemporaryDirectory(prefix='player-retirement-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], capture_output=True, text=True, timeout=10)
        if expect_failure:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr


if __name__ == '__main__':
    main()
