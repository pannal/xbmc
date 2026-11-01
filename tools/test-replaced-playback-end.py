#!/usr/bin/env python3
"""Execute the production end and stop handlers while a newer open waits for the old player.

The end or stop notification of the replaced playback arrives after the newer
open (the end callback waits for the video database). The notification must
not continue the stack, the playlist or EPG autoplay, or close the player,
because each of those drops or replaces the newer open. Recording boundaries
stand in for PVR, announcements, GUI and media players.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
autoplay = runpy.run_path(str(ROOT / 'tools/test-autoplay-fullscreen.py'))
function = autoplay['function']
PRELUDE = autoplay['PRELUDE'].replace(
    'struct CFileItem {',
    'struct CFileItem {struct Prop{bool v=false;bool asBoolean(bool)const{return v;}};'
    'bool epg=false;Prop GetProperty(const char*)const{return {epg};}')
# A backend open that processes messages (UPnP) runs the hook inside the open.
PRELUDE = PRELUDE.replace('struct IPlayer {', 'static std::function<void()> backendWait;\nstruct IPlayer {', 1)
PRELUDE = PRELUDE.replace('if(failOpen)return false;playing=true;return true;',
                          'if(failOpen)return false;if(backendWait){auto wait=std::move(backendWait);wait();}'
                          'playing=true;return true;', 1)
HARNESS = autoplay['HARNESS']
HARNESS = HARNESS[:HARNESS.index('static void ended(')]
HARNESS = HARNESS.replace(
    ' void SendThreadMessage(CGUIMessage&){}',
    ' void SendMessage(CGUIMessage&){}int threadMessages=0;void SendThreadMessage(CGUIMessage&){++threadMessages;}')
HARNESS = HARNESS.replace(
    'struct CApplicationStackHelper {int clears=0;void Clear(){++clears;}};',
    'struct CApplicationStackHelper {int clears=0,adopts=0;bool regular=false,next=false;void Clear(){++clears;}'
    'void Adopt(CApplicationStackHelper&&){++adopts;}'
    'bool IsPlayingRegularStack(){return regular;}bool HasNextStackPartFileItem(){return next;}'
    'int position=0;int GetCurrentPartNumber(){return position;}void SetStackPartCurrentFileItem(int p){position=p;}'
    'CFileItem GetStackPartFileItem(int p)const{CFileItem part;part.id=100+p;return part;}};')
HARNESS = HARNESS.replace(
    'struct CApplication;',
    'struct CApplication;\n'
    'constexpr int GUI_MSG_PLAYBACK_ENDED=8,GUI_MSG_PLAYBACK_STOPPED=9;\n'
    'namespace ANNOUNCEMENT {constexpr int Player=0;}\n'
    'struct CVariant {enum {VariantTypeObject};CVariant(int){}'
    'CVariant& operator[](const char*){return *this;}CVariant& operator=(bool){return *this;}};\n'
    'struct PVR {int ended=0,stopped=0;void OnPlaybackEnded(const CFileItem&){++ended;}'
    'void OnPlaybackStopped(const CFileItem&){++stopped;}};\n'
    'struct Announcements {int stops=0;template<class... A>void Announce(A&&...){++stops;}};')
HARNESS = HARNESS.replace(
    ' static inline Params params;',
    ' static inline Params params;static inline PVR pvr;static inline Announcements announcements;\n'
    ' static PVR& GetPVRManager(){return pvr;}static Announcements* GetAnnouncementManager(){return &announcements;}\n')
HARNESS = HARNESS.replace(
    'struct CGUIMessage {CGUIMessage(int,int,int,int,int){}};',
    'struct CGUIMessage {int id;CGUIMessage(int id,int,int,int=0,int=0):id(id){}int GetMessage()const{return id;}};')
HARNESS = HARNESS.replace('struct Party {', 'struct Party {bool IsEnabled()const{return false;}')
# The playlist state, its reset, its next index and its terminal handlers are production code.
PLAYLIST_PLAYER = HARNESS[HARNESS.index('struct CPlayListPlayer {'):HARNESS.index('struct CServiceBroker {')]
HARNESS = HARNESS.replace(PLAYLIST_PLAYER, r"""constexpr int GUI_MSG_PLAYLIST_CHANGED=10;
struct Cell {int value;Cell(int v=0):value(v){}int load()const{return value;}operator int()const{return value;}
 Cell& operator=(int v){value=v;return *this;}};
struct CUtil {static void ClearSubtitles(){}};
struct CPlayListPlayer {
 bool m_bIsDeferredPlayPending=false;void StopMessage(int);void MediaPlay(CFileItem*,int);
 Cell m_iCurrentPlayList=TYPE_VIDEO,m_iCurrentSong=0;bool m_bPlayedFirstFile=true,m_bPlaybackStarted=true;
 int selected=-1;CPlayList list;CApplication* app=nullptr;
 int GetNextItemIdx(int)const;const CPlayList& GetPlaylist(int)const{return list;}
 bool RepeatedOne(int)const{return false;}bool Repeated(int)const{return false;}
 void Reset();void ResetCurrentPlaylist();void SetCurrentPlaylist(int);int GetCurrentPlaylist(){return m_iCurrentPlayList;}
 bool Terminal(int);bool Play(int,const std::string&,bool);bool PlayNext(int offset=1,bool bAutoPlay=true);
};
""")
HARNESS = HARNESS.replace(
    'bool CPlayListPlayer::Play(int,const std::string& name,bool){',
    'bool CPlayListPlayer::Play(int index,const std::string& name,bool){\n'
    ' selected=index;m_iCurrentSong=index;m_bPlaybackStarted=false;')
HARNESS = HARNESS.replace(
    ' void Frame(){',
    ' struct {void Set(){}} m_playerEvent;int stackPlays=0,m_nextPlaylistItem=0;\n'
    ' void Intent(CFileItem item,bool bRestart,[[maybe_unused]] bool playlistItem);\n'
    ' std::function<void()> nested;void Accept(bool bRestart,bool playlistItem);\n'
    ' bool PlayFile(CFileItem item,const std::string& player,bool bRestart=false,bool playlistItem=false,uint64_t attempt=0);bool abandon=false;\n'
    ' bool StartPlayback(CFileItem item,const std::string& name,bool bRestart,bool playlistItem,uint64_t intent){\n'
    '  ++stackPlays;Intent(item,bRestart,playlistItem);if(nested){auto open=std::move(nested);open();}\n'
    '  if(abandon)return false;\n'
    '  const auto appPlayer=&player;@CANCELLED@Acceptance(item,bRestart,intent);\n'
    '  const bool ret=player.OpenFile(item,{},services.factory,name,callback,intent);@CANCELLED@Accept(bRestart,playlistItem);return ret;}\n'
    ' bool Ended();bool Stopped();void EndOpen(uint64_t intent);void Acceptance(CFileItem& item,bool bRestart,uint64_t intent);\n'
    ' struct StackPartOpen{uint64_t intent=0;int part=-1;} m_stackPartOpen;bool PlayStackPart(int part,CFileItem item);\n void Frame(){')
PRELUDE = PRELUDE.replace('struct CApplicationPlayer {', 'struct CApplicationPlayer {void SetPlaySpeed(float){}')
TESTS = r"""
@PLAYLISTFUNCTIONS@
bool CApplication::PlayFile(CFileItem item,const std::string& player,bool bRestart,bool playlistItem,uint64_t attempt){@PLAYFILE@}
@ENDOPEN@
bool CApplication::Ended(){switch(GUI_MSG_PLAYBACK_ENDED){@ENDED@}return false;}
bool CApplication::Stopped(){switch(GUI_MSG_PLAYBACK_STOPPED){@STOPPED@}return false;}
bool CPlayListPlayer::Terminal(int id){CGUIMessage message(id,0,0);switch(message.GetMessage()){@PLAYLISTTERMINAL@}return false;}
void CApplication::Intent([[maybe_unused]] CFileItem item,bool bRestart,bool playlistItem){
 @ENDPLAYLIST@@INTENT@}
// The production effects of an accepted open on the current playback.
struct StagedStack {CApplicationStackHelper stack;bool restart=false;};
void CApplication::Acceptance(CFileItem& item,bool bRestart,uint64_t intent){
 const auto appPlayer=&player;const auto stackHelper=&stack;StagedStack* staged=nullptr;@ACCEPTANCE@}
// The open repeats the reset once accepted, after a dialog or a resolution.
void CApplication::Accept(bool bRestart,bool playlistItem){@ENDPLAYLIST@ endPlaylist();}
void CPlayListPlayer::MediaPlay(CFileItem* payload,int restart){
 auto& g_application=*app;struct {void* lpVoid;int param1,param2;} message{payload,restart,0};auto* pMsg=&message;
 @MEDIAPLAY@}
static void reset(){CServiceBroker::pvr={};CServiceBroker::announcements={};}
// The old playback has ended, and the player still closes when the user opens a newer file.
static void replace(CApplication& app){
 CFileItem latest;latest.id=42;
 assert(app.player.OpenFile(latest,{},app.services.factory,"old",app.callback));
 assert(app.player.HasPendingOpen()&&app.player.m_closingPlayer);
}
static void opensLatest(CApplication& app,const std::shared_ptr<IPlayer>& old){
 old->canClose=true;app.Frame();
 assert(app.player.created==1&&app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==42);
}
int main(){
 // The end of the replaced playback: no playlist, stack, EPG autoplay or close.
 for(int kind:{0,1,2}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  app.m_itemCurrentFile->epg=kind==1;app.stack.regular=app.stack.next=kind==2;
  replace(app);app.Ended();
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==42);
  assert(app.stackPlays==0&&CServiceBroker::pvr.ended==0&&CServiceBroker::pvr.stopped==1);
  assert(CServiceBroker::announcements.stops==1);
  opensLatest(app,old);
 }
 // A remote player is never reused: a newer open replaces the playing player, so
 // every notification of the replaced item comes before the newer open. The end
 // that the closing player reports does not continue the playlist.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;
  replace(app);
  assert(app.player.m_closingPlayer==old&&old->opens==0&&old->closes==1);
  app.Ended();
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==42);
  assert(CServiceBroker::pvr.ended==0&&CServiceBroker::gui.wm.threadMessages==0);
  opensLatest(app,old);}
 // A remote player reports the stop when the close starts, before the renderer
 // stops: the stop keeps the newer open and the playlist.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;
  replace(app);app.Stopped();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==42&&app.player.m_closingPlayer==old);
  assert(CServiceBroker::gui.wm.threadMessages==0&&CServiceBroker::playlist.m_iCurrentPlayList!=TYPE_NONE);
  assert(CServiceBroker::pvr.stopped==1&&CServiceBroker::pvr.ended==0);
  opensLatest(app,old);}
 // After a stop, the remote player reports the stop before the renderer stops:
 // the window stays until the close of the player completes.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;
  app.StopPlaying();app.Stopped();
  assert(app.player.m_closingPlayer==old&&CServiceBroker::gui.wm.previous==0);
  app.Frame();assert(app.player.m_closingPlayer==old&&CServiceBroker::gui.wm.previous==0);
  old->canClose=true;app.Frame();
  assert(!app.player.HasPlayer()&&CServiceBroker::gui.wm.previous==1);}
 // A normal end still continues the playlist and the EPG autoplay.
 {CApplication app;begin(app);reset();app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(CServiceBroker::pvr.ended==1&&CServiceBroker::pvr.stopped==0);
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==9);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);}
 {CApplication app;begin(app);reset();app.stack.regular=app.stack.next=true;app.Ended();
  assert(app.stackPlays==1);}
 // The stop of the replaced playback does not stop the playlist under the newer open.
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;CFileItem latest;latest.id=42;
  app.Intent(latest,true,false);replace(app);
  app.player.OnPlaybackStopped();
  CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);
  const int messages=CServiceBroker::gui.wm.threadMessages;
  if(messages)app.PlaylistStopped();
  assert(messages==0&&CServiceBroker::playlist.m_iCurrentPlayList!=TYPE_NONE);
  assert(app.player.HasPendingOpen());opensLatest(app,old);
 }
 // An explicit stop still ends the playlist and the pending open.
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;replace(app);app.StopPlaying();
  assert(!app.player.HasPendingOpen());app.player.OnPlaybackStopped();
  assert(CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED));
  assert(CServiceBroker::gui.wm.threadMessages==1&&CServiceBroker::playlist.m_iCurrentPlayList==TYPE_NONE);
  old->canClose=true;app.Frame();assert(app.player.created==0);
 }
 // A standalone open ends the playlist when the open starts. Neither the end or
 // stop of the replaced playback nor the end of the standalone file continues
 // the playlist, and the playlist stop does not drop the standalone open.
 for(bool oldEnded:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  CFileItem latest;latest.id=42;app.Intent(latest,false,false);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_NONE);
  replace(app);
  if(oldEnded){app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);}
  else{app.player.OnPlaybackStopped();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);}
  assert(CServiceBroker::gui.wm.threadMessages==0&&app.player.HasPendingOpen());
  opensLatest(app,old);
  app.player.m_pPlayer->playing=false;app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(CServiceBroker::playlist.selected==-1&&!app.player.HasPendingOpen());
 }
 // The playlist's own open and a restart keep the playlist and its position.
 for(bool restart:{false,true}){CApplication app;begin(app);CServiceBroker::playlist.m_iCurrentSong=1;
  CFileItem item;item.id=9;app.Intent(item,restart,!restart);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::playlist.m_iCurrentSong==1);
 }
 // A single-item media message ends the playlist. A stack part seek uses the
 // same message as a restart and keeps the playlist position.
 for(int restart:{0,1}){CApplication app;begin(app);CServiceBroker::playlist.m_iCurrentSong=1;
  auto* item=new CFileItem;item->id=50;CServiceBroker::playlist.MediaPlay(item,restart);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==(restart?TYPE_VIDEO:TYPE_NONE));
  assert(!restart||CServiceBroker::playlist.m_iCurrentSong==1);
  assert(app.stack.clears==(restart?0:1));
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==50);
 }
 // An explicit stop after the old playback reached the end, also after a newer
 // open: the late end does not continue the stack, the playlist or the EPG
 // autoplay, and ends the playlist like a stop.
 for(int kind:{0,1,2})for(bool replaced:{false,true}){CApplication app;begin(app);reset();
  auto old=app.player.m_pPlayer;
  app.m_itemCurrentFile->epg=kind==1;app.stack.regular=app.stack.next=kind==2;
  if(replaced)replace(app);
  app.StopPlaying();assert(!app.player.HasPendingOpen());
  app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(!app.player.HasPendingOpen()&&app.stackPlays==0&&CServiceBroker::playlist.selected==-1);
  assert(CServiceBroker::pvr.ended==0&&CServiceBroker::pvr.stopped==1);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_NONE&&CServiceBroker::gui.wm.threadMessages==1);
  old->canClose=true;app.Frame();assert(app.player.created==0&&!app.player.HasPlayer());
 }
 // An explicit stop during the open of a playlist item ends the playlist.
 {CApplication app;begin(app);
  assert(CServiceBroker::playlist.PlayNext(1,false)&&app.player.HasPendingOpen());
  app.StopPlaying();app.player.OnPlaybackStopped();
  assert(CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED));
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_NONE&&CServiceBroker::gui.wm.threadMessages==1);
 }
 // A newer open after the stop wins: the late end keeps the newer open and the playlist.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  app.StopPlaying();assert(CServiceBroker::playlist.PlayNext(1,false));
  app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==9&&CServiceBroker::pvr.stopped==1);
  assert(CServiceBroker::playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);
  old->canClose=true;app.Frame();assert(app.player.created==1&&app.player.m_pPlayer->lastItem==9);
 }
 // An earlier stop does not affect the end of a later playback.
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;app.StopPlaying();app.player.OnPlaybackStopped();
  CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);old->canClose=true;app.Frame();
  CServiceBroker::playlist.SetCurrentPlaylist(TYPE_VIDEO);assert(CServiceBroker::playlist.Play(0,"old",false));
  assert(app.player.IsPlaying());app.player.m_pPlayer->playing=false;app.Ended();
  assert(CServiceBroker::playlist.selected==1&&app.player.HasPendingOpen());
 }
 // A playlist open nested in the resolution or a dialog of a standalone open
 // is newer and supersedes the standalone open: the playlist item and the
 // playlist stay.
 {CApplication app;begin(app);reset();auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{playlist.SetCurrentPlaylist(TYPE_VIDEO);playlist.m_iCurrentSong=0;
   CFileItem nested;nested.id=30;assert(app.PlayFile(nested,"old",false,true));
   assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO);};
  CFileItem standalone;standalone.id=42;assert(!app.PlayFile(standalone,"old"));
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&app.player.m_nextItem.pItem->id==30);
 }
 // A stop after the end queued the next playlist item: no notification
 // remains, so the stop ends the playlist and leaves the visualisation.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;auto& playlist=CServiceBroker::playlist;
  playlist.SetCurrentPlaylist(TYPE_MUSIC);CServiceBroker::gui.wm.active=WINDOW_VISUALISATION;old->hasAudio=true;
  app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(app.player.HasPendingOpen()&&!app.player.m_waitForPlaybackStop);
  app.StopPlaying();assert(playlist.GetCurrentPlaylist()==TYPE_NONE);
  old->canClose=true;app.Frame();app.PlaybackCleanup();
  assert(!app.player.HasPlayer()&&!app.player.HasPendingOpen()&&app.player.created==0);
  assert(CServiceBroker::gui.wm.active==HOME);
 }
 // A stop, then a playlist item whose resolution processes the late end or stop
 // of the stopped playback: the newer open supersedes the stop, so the
 // playlist stays for the item and continues after the item.
 for(bool oldEnded:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  auto& playlist=CServiceBroker::playlist;app.StopPlaying();
  app.nested=[&]{if(oldEnded)app.Ended();else app.player.OnPlaybackStopped();
   playlist.Terminal(oldEnded?GUI_MSG_PLAYBACK_ENDED:GUI_MSG_PLAYBACK_STOPPED);};
  playlist.m_iCurrentSong=0;playlist.m_bPlaybackStarted=false;
  CFileItem item;item.id=60;assert(app.PlayFile(item,"old",false,true));
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);
  assert(app.stackPlays==1&&CServiceBroker::pvr.ended==0&&app.player.m_nextItem.pItem->id==60);
  old->canClose=true;app.Frame();assert(app.player.m_pPlayer->lastItem==60);
  app.player.m_pPlayer->playing=false;app.Ended();
  assert(playlist.selected==1&&app.player.HasPendingOpen());
 }
 // A stop and its late end, then a playlist item that waits for the old player,
 // then a stop of that item: no notification remains, so the stop ends the
 // playlist and leaves the visualisation, through both stop routes.
 for(bool deferred:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  auto& playlist=CServiceBroker::playlist;
  app.StopPlaying();app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.PlaylistStopped();
  playlist.SetCurrentPlaylist(TYPE_MUSIC);playlist.m_iCurrentSong=0;
  CServiceBroker::gui.wm.active=WINDOW_VISUALISATION;
  CFileItem item;item.id=60;assert(app.PlayFile(item,"old",false,true));
  assert(app.player.HasPendingOpen()&&!app.player.m_waitForPlaybackStop);
  playlist.m_bIsDeferredPlayPending=deferred;
  if(deferred)playlist.StopMessage(TYPE_NONE);else app.StopPlaying();
  assert(playlist.GetCurrentPlaylist()==TYPE_NONE);
  old->canClose=true;app.Frame();app.PlaybackCleanup();
  assert(CServiceBroker::gui.wm.active==HOME&&!app.player.HasPlayer()&&app.player.created==0);
 }
 // The close of a stopped player can process messages (the wake dialog of a
 // network share during the save of a game), and a request during the close is
 // newer than the stop: the newer item and the playlist of the item stay, through
 // both stop routes (Astra's round 39 probe).
 for(bool deferred:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  old->m_type="game";auto& playlist=CServiceBroker::playlist;uint64_t newer=0;
  old->onClose=[&]{playlist.SetCurrentPlaylist(TYPE_VIDEO);playlist.m_iCurrentSong=0;
   CFileItem item;item.id=77;assert(app.PlayFile(item,"old",false,true));
   newer=app.player.m_nextItem.intent;old->canClose=true;};
  playlist.m_bIsDeferredPlayPending=deferred;
  if(deferred)playlist.StopMessage(TYPE_NONE);else app.StopPlaying();
  playlist.m_bIsDeferredPlayPending=false;
  assert(newer&&!app.player.OpenSuperseded(newer)&&playlist.GetCurrentPlaylist()==TYPE_VIDEO);
  app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.HasPlayer()&&app.player.m_pPlayer!=old&&app.player.m_pPlayer->lastItem==77);
 }
 // An open that does not reach the player, for example a cancelled Blu-ray
 // title selection, does not supersede the end of the playback that continues.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->playing=true;
  app.abandon=true;CFileItem title;title.id=70;assert(!app.PlayFile(title,"old",false,true));
  assert(!app.player.IsOpening()&&!app.player.HasPendingOpen());
  old->playing=false;app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
  assert(CServiceBroker::playlist.selected==1&&app.player.m_nextItem.pItem->id==9&&CServiceBroker::pvr.ended==1);
 }
 // The end of the previous playback during the resolution of the next playlist
 // item: the terminal cleanup waits for the open, so the item stays fullscreen.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.m_iCurrentSong=0;playlist.m_bPlaybackStarted=false;
  app.nested=[&]{app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.Frame();
   assert(app.player.IsOpening()&&gfx.fullscreen&&gfx.restores==0);};
  CFileItem next;next.id=60;assert(app.PlayFile(next,"old",false,true));
  app.Frame();assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==60);
  assert(gfx.fullscreen&&gfx.restores==0&&playlist.GetCurrentPlaylist()==TYPE_VIDEO);
 }
 // The same, but the item does not reach the player: the waiting cleanup runs.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;
  app.nested=[&]{app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.Frame();};
  app.abandon=true;CFileItem next;next.id=60;assert(!app.PlayFile(next,"old",false,true));
  app.Frame();assert(!app.player.HasPlayer()&&!gfx.fullscreen&&gfx.restores==1);
 }
 // The end of the previous playback during the resolution of an item that then
 // fails to open, or opens audio: the waiting cleanup leaves fullscreen video.
 for(int outcome:{0,1}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;
  app.StopPlaying();
  app.nested=[&]{app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
   app.Frame();assert(!app.player.HasPlayer()&&app.m_playbackCleanupDeferred);};
  CFileItem next;next.id=80;app.player.failCreate=outcome==0;next.video=outcome==0;
  app.PlayFile(next,"old",false,true);if(outcome==1)app.player.m_pPlayer->hasAudio=true;app.Frame();
  assert(!gfx.fullscreen&&gfx.restores==1&&!app.m_playbackCleanupDeferred);
 }
 // A nested playback that starts and ends during the resolution of a cancelled
 // open, also after a stop of the nested playback: the waiting cleanup runs.
 for(bool stop:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  app.nested=[&]{CFileItem nested;nested.id=30;assert(app.PlayFile(nested,"old",false,false));
   if(stop)app.StopPlaying();
   app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);
   old->canClose=true;app.Frame();
   if(!stop){assert(app.player.IsPlaying());auto current=app.player.m_pPlayer;current->playing=false;
    current->canClose=true;app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.Frame();}
   assert(!app.player.HasPlayer()&&app.m_playbackCleanupDeferred);app.abandon=true;};
  CFileItem outer;outer.id=60;assert(!app.PlayFile(outer,"old",false,false));app.Frame();
  assert(!gfx.fullscreen&&gfx.restores==1&&!app.m_playbackCleanupDeferred);
 }
 // A nested video that still plays when the outer open is cancelled keeps the screen.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;app.StopPlaying();
  app.nested=[&]{app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.Frame();
   CFileItem nested;nested.id=30;assert(app.PlayFile(nested,"old",false,false));
   assert(app.player.IsPlaying()&&app.m_playbackCleanupDeferred);app.abandon=true;};
  CFileItem outer;outer.id=60;assert(!app.PlayFile(outer,"old",false,false));app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==30&&gfx.fullscreen&&gfx.restores==0);
 }
 // A stop during the resolution of an open cancels the open, with the end or
 // stop notification of the stopped playback before or after the cancel. The
 // stop ends the playlist, so the playlist does not skip to the next item.
 for(bool lateNotification:{false,true}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;
  auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{app.StopPlaying();assert(playlist.GetCurrentPlaylist()==TYPE_NONE);
   if(!lateNotification){app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);old->canClose=true;app.Frame();}};
  CFileItem next;next.id=60;assert(!app.PlayFile(next,"old",false,true));
  assert(app.player.created==0&&!app.player.HasPendingOpen()&&!app.player.IsOpening());
  if(lateNotification){app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);}
  old->canClose=true;app.Frame();
  assert(!app.player.HasPlayer()&&!app.player.HasPendingOpen()&&app.player.created==0);
  assert(playlist.selected==-1&&playlist.GetCurrentPlaylist()==TYPE_NONE&&CServiceBroker::pvr.ended==0);
  assert(!gfx.fullscreen&&gfx.restores==1);
 }
 // The end of a playlist that was queued before an open, for example Next past
 // the last item, stops the old player but does not cancel the newer open.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;
  app.nested=[&]{app.PlaylistStopped();assert(app.player.m_closingPlayer);};
  CFileItem next;next.id=60;assert(app.PlayFile(next,"old",false,false));
  assert(app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==60);
  app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==60&&gfx.fullscreen);
 }
 // The same during a newer playlist item, after the old end was acknowledged:
 // the playlist stays for the item, and the item continues the playlist.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;auto& playlist=CServiceBroker::playlist;
  playlist.m_iCurrentSong=0;playlist.m_bPlaybackStarted=false;
  app.nested=[&]{app.PlaylistStopped();
   assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&!app.player.StopRequested()&&app.player.m_closingPlayer);};
  CFileItem item;item.id=60;assert(app.PlayFile(item,"old",false,true));
  app.player.OnPlaybackStopped();playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);
  old->canClose=true;app.Frame();
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&app.player.m_pPlayer->lastItem==60);
  app.player.m_pPlayer->playing=false;app.Ended();
  assert(playlist.selected==1&&app.player.HasPendingOpen());
 }
 // During a deferred play, the screensaver stops its own video: that stop
 // cancels no open. A stop request through the same message does.
 for(bool deferred:{true,false}){CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{playlist.m_bIsDeferredPlayPending=deferred;playlist.StopMessage(TYPE_NONE);
   playlist.m_bIsDeferredPlayPending=false;app.player.OnPlaybackStopped();app.Frame();};
  CFileItem next;next.id=60;assert(app.PlayFile(next,"old",false,false)==deferred);
  assert(deferred==(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==60));
 }
 // A stop without playback during a resolution cancels the open too. A stop
 // before an open does not cancel the open.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;app.StopPlaying();
  app.Ended();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);old->canClose=true;app.Frame();
  assert(!app.player.HasPlayer());
  app.nested=[&]{app.StopPlaying();};CFileItem next;next.id=60;assert(!app.PlayFile(next,"old",false,false));
  assert(app.player.created==0&&!app.player.HasPlayer());
  CFileItem later;later.id=61;assert(app.PlayFile(later,"old",false,false));
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==61);
 }
 // Playlist.PlayOffset of the other playlist stops the playback and starts the
 // other playlist at the offset, also while the close of the old player waits
 // for the renderer: the switch resets the position, so the offset is absolute.
 for(int offset:{0,1})for(bool remote:{false,true}){CApplication app;begin(app);g_playOffsetApp=&app;
  auto old=app.player.m_pPlayer;old->m_type=remote?"remote":"video";old->playing=true;old->hasVideo=true;
  auto& playlist=CServiceBroker::playlist;playlist.m_iCurrentPlayList=TYPE_VIDEO;playlist.m_iCurrentSong=0;
  PlayOffset({"music",std::to_string(offset)});
  assert(playlist.GetCurrentPlaylist()==TYPE_MUSIC&&playlist.selected==offset&&app.player.HasPendingOpen());
  old->canClose=true;app.Stopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer!=old&&playlist.selected==offset);
 }
 // An offset in the current playlist stays relative to the playing item.
 {CApplication app;begin(app);g_playOffsetApp=&app;app.player.m_pPlayer->playing=true;
  auto& playlist=CServiceBroker::playlist;playlist.m_iCurrentPlayList=TYPE_MUSIC;playlist.m_iCurrentSong=1;
  PlayOffset({"music","-1"});
  assert(playlist.selected==0);
 }
 // Music that the user queues after a stop starts, also while the close of the
 // old player waits for the renderer. Music that the user queues while a newer
 // item waits for the close joins the playlist and does not replace the item.
 for(bool closed:{false,true}){CApplication app;begin(app);reset();g_playOffsetApp=&app;
  auto old=app.player.m_pPlayer;old->m_type="remote";old->playing=true;
  app.StopPlaying();app.Stopped();CServiceBroker::playlist.Terminal(GUI_MSG_PLAYBACK_STOPPED);
  if(closed){old->canClose=true;app.Frame();}
  QueueMusic();
  assert(CServiceBroker::playlist.adds==1&&CServiceBroker::playlist.selected>=0);
  old->canClose=true;app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer!=old);
 }
 {CApplication app;begin(app);reset();g_playOffsetApp=&app;
  auto old=app.player.m_pPlayer;old->m_type="remote";old->playing=true;
  CFileItem newer;newer.id=70;assert(app.PlayFile(newer,"old",false,false));assert(app.player.HasPendingOpen());
  QueueMusic();
  assert(CServiceBroker::playlist.adds==1&&CServiceBroker::playlist.selected==-1);
  app.player.OnPlaybackStopped();old->canClose=true;app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==70);
 }
 std::cout<<"PASS replaced playback end and stop keep the newer open; standalone opens end the playlist; a stop supersedes a late end; normal end, stack and explicit stop unchanged; Playlist.PlayOffset and queue\n";
}
"""


# The production playlist Play, with its failure handling, replaces the recording stand-in.
PLAY_TESTS = r"""
static void playlist(CApplication& app){
 auto& playlist=CServiceBroker::playlist;playlist.list.m_vecItems[0]->id=40;playlist.list.m_vecItems[1]->id=41;(void)app;
}
int main(){
 (void)&replace;(void)&opensLatest; // The cases of the first build use the helpers.
 // A stop during the resolution of a playlist item cancels the item: the item
 // is not unplayable, and the playlist does not continue.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{app.StopPlaying();app.player.OnPlaybackStopped();app.Frame();};
  assert(!playlist.Play(0,"old"));
  assert(playlist.list.unplayable==0&&playlist.GetCurrentPlaylist()==TYPE_NONE&&!app.player.HasPlayer());
 }
 // A stop or a newer item that reaches the player during the expansion of a
 // playlist item cancels the item before the playlist changes.
 for(bool newer:{false,true}){CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.m_iCurrentSong=1;int after=-2,list=-2;
  playlist.list.m_vecItems[0]->path="smb://share/list.m3u";
  expansion=[&]{
   if(!newer)app.StopPlaying(),app.player.OnPlaybackStopped(),app.Frame();
   else{CFileItem c;c.id=50;assert(app.PlayFile(c,"old",false,false));
    app.player.OnPlaybackStopped();app.Frame();assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50);}
   after=playlist.m_iCurrentSong.load();list=playlist.GetCurrentPlaylist();};
  assert(!playlist.Play(0,"old"));
  // the cancelled attempt leaves the playlist as the stop or the newer item left the playlist
  assert(playlist.list.unplayable==0&&playlist.m_iCurrentSong.load()==after&&after!=0);
  assert(playlist.GetCurrentPlaylist()==list&&app.stackPlays==(newer?1:0));
  if(newer)assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50&&!app.player.HasPendingOpen());
  else assert(!app.player.HasPlayer()&&!app.player.HasPendingOpen());
 }
 // The user selects the next item during the expansion: the item plays, the
 // cancelled expansion leaves the playlist unchanged, and the playlist continues
 // after the item, also at the end of the item.
 for(bool ends:{false,true}){CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  playlist.list.m_vecItems[0]->path="smb://share/list.m3u";
  auto z=std::make_shared<CFileItem>();z->id=42;playlist.list.m_vecItems.push_back(z);
  expansion=[&]{assert(playlist.Play(1,"old"));app.player.OnPlaybackStopped();app.Frame();
   assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==41);};
  assert(!playlist.Play(0,"old"));
  assert(playlist.list.size()==3&&playlist.list.m_vecItems[0]->id==40&&playlist.m_iCurrentSong.load()==1);
  if(!ends)assert(playlist.PlayNext());
  else{app.player.m_pPlayer->playing=false;app.player.m_pPlayer->canClose=true;app.Ended();
   if(app.player.HasPendingOpen()){app.player.OnPlaybackStopped();app.Frame();}}
  assert(app.player.m_pPlayer->lastItem==42);
 }
 // An expansion that nothing supersedes replaces the playlist item with the
 // entries, and the first entry plays.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  playlist.list.m_vecItems[0]->path="smb://share/list.m3u";
  assert(playlist.Play(0,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(playlist.list.size()==4&&playlist.list.m_vecItems[0]->id==50&&playlist.list.m_vecItems[3]->id==41);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50);
 }
 // The same, but the user restarts the item from the playlist during the
 // resolution: the restart plays, and the cancelled attempt changes nothing.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{app.StopPlaying();app.player.OnPlaybackStopped();app.Frame();
   playlist.SetCurrentPlaylist(TYPE_VIDEO);assert(playlist.Play(0,"old"));};
  assert(!playlist.Play(0,"old"));
  assert(playlist.list.unplayable==0&&playlist.m_iCurrentSong.load()==0);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40);
 }
 // A failed item without a stop is still unplayable, and the playlist continues.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;app.StopPlaying();app.player.OnPlaybackStopped();app.Frame();
  playlist.SetCurrentPlaylist(TYPE_VIDEO);app.player.failCreate=true;
  assert(!playlist.Play(0,"old"));assert(playlist.list.unplayable==2);
 }
 // The end of A starts the next item B. A stop cancels B, and the user opens C
 // during B's resolution: the end of A does not close C.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  app.nested=[&]{app.StopPlaying();app.Frame();CFileItem c;c.id=50;assert(app.PlayFile(c,"old",false,false));
   assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50);};
  old->playing=false;app.Ended();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50&&!app.player.m_closingPlayer);
  // C has not reported the video stream yet, and keeps fullscreen.
  assert(!app.player.m_pPlayer->hasVideo&&gfx.fullscreen&&gfx.restores==0);
 }
 // After the retries, the failure dialog: a stop and a restart of the same item
 // during the dialog. The cancelled attempt does not end the newer playlist.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlistRetries=1;
  app.StopPlaying();app.player.OnPlaybackStopped();app.Frame();playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{app.abandon=true;};
  failureDialog=[&]{app.StopPlaying();playlist.SetCurrentPlaylist(TYPE_VIDEO);assert(playlist.Play(0,"old"));};
  assert(!playlist.Play(0,"old"));playlistRetries=100;failureDialog={};
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40);
 }
 // A newer playlist attempt of the same item supersedes the failure handling of
 // the older attempt: a restart during the failure dialog, without a stop, and a
 // restart during the resolution of an attempt that then fails.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlistRetries=1;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{app.abandon=true;};
  failureDialog=[&]{assert(playlist.Play(0,"old"));};
  assert(!playlist.Play(0,"old"));playlistRetries=100;failureDialog={};
  app.player.OnPlaybackStopped();app.Frame();
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40&&!app.player.m_closingPlayer);
 }
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{assert(playlist.Play(0,"old"));app.abandon=true;};
  assert(!playlist.Play(0,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(playlist.list.unplayable==0&&playlist.GetCurrentPlaylist()==TYPE_VIDEO);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40&&!app.player.m_closingPlayer);
 }
 // The end of A during the resolution of item B; B fails, and the playlist
 // continues at C: the waiting cleanup does not leave fullscreen before C.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;
  app.nested=[&]{app.Ended();playlist.Terminal(GUI_MSG_PLAYBACK_ENDED);app.Frame();
   assert(app.m_playbackCleanupDeferred);app.abandon=true;};
  playlist.SetCurrentPlaylist(TYPE_VIDEO);assert(playlist.Play(0,"old"));app.Frame();
  assert(playlist.list.unplayable==1&&app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==41);
  assert(gfx.fullscreen&&gfx.restores==0);
 }
 // A restart of the current playback (TMSG_MEDIA_RESTART) during the failure
 // dialog is a newer open: the failed attempt keeps the playlist.
 {CApplication app;begin(app);reset();playlist(app);
  auto old=app.player.m_pPlayer;old->canClose=true;old->playing=true;old->hasVideo=true;
  auto& playlist=CServiceBroker::playlist;playlistRetries=1;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{app.abandon=true;};
  failureDialog=[&]{CFileItem current;current.id=30;assert(app.PlayFile(current,"old",true));};
  assert(!playlist.Play(0,"old"));playlistRetries=100;failureDialog={};
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&CServiceBroker::gui.wm.threadMessages==0);
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==30&&!app.player.m_closingPlayer);
 }
 // A newer open supersedes every older open still in progress, whatever
 // started the older open: the next item that the end of A started,
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  app.nested=[&]{CFileItem y;y.id=50;assert(app.PlayFile(y,"old",false,false));app.Frame();};
  app.Ended();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50&&!app.player.m_closingPlayer);
  assert(CServiceBroker::playlist.list.unplayable==0);
 }
 // a playlist item that the user started,
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{CFileItem y;y.id=50;assert(app.PlayFile(y,"old",false,false));};
  assert(!playlist.Play(0,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50&&!app.player.m_closingPlayer);
  assert(playlist.list.unplayable==0&&CServiceBroker::gui.wm.threadMessages==0);
 }
 // and an open whose backend processes messages: a stop and a newer playlist
 // during the backend open keep the newer playlist and the newer player state.
 {CApplication app;begin(app);reset();playlist(app);
  app.player.m_pPlayer.reset();app.player.m_waitForPlaybackStop=false;
  auto& playlist=CServiceBroker::playlist;std::shared_ptr<IPlayer> newer;
  backendWait=[&]{app.player.m_pPlayer->canClose=true;app.StopPlaying();assert(!app.player.HasPlayer());
   playlist.SetCurrentPlaylist(TYPE_VIDEO);assert(playlist.Play(0,"new"));newer=app.player.m_pPlayer;
   newer->hasVideo=true;app.player.OnPlaybackStopped();};
  CFileItem x;x.id=99;assert(!app.PlayFile(x,"remote",false,false));
  assert(playlist.GetCurrentPlaylist()==TYPE_VIDEO&&playlist.list.size()==2);
  assert(app.player.m_pPlayer==newer&&newer->lastItem==40&&app.player.IsPlaying());
 }
 // Without a stop, the newer open replaces the backend player that still opens.
 {CApplication app;begin(app);reset();
  app.player.m_pPlayer.reset();app.player.m_waitForPlaybackStop=false;
  backendWait=[&]{app.player.m_pPlayer->canClose=true;CFileItem y;y.id=50;
   assert(app.PlayFile(y,"old",false,false)&&app.player.HasPendingOpen());};
  CFileItem x;x.id=99;assert(!app.PlayFile(x,"remote",false,false));
  assert(app.player.HasPendingOpen());app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==50);
 }
 // A newer request that fails before the player does not supersede: the
 // playlist item, during the resolution of which the request failed, plays.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{CFileItem y;y.id=50;app.nested=[&]{app.abandon=true;};assert(!app.PlayFile(y,"old",false,true));};
  assert(playlist.Play(0,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40&&playlist.list.unplayable==0);
 }
 // A standalone request ends the playlist when the request begins, like the
 // GUI callers before PlayFile, also when the request then fails.
 {CApplication app;begin(app);reset();playlist(app);auto old=app.player.m_pPlayer;old->canClose=true;
  auto& playlist=CServiceBroker::playlist;playlist.SetCurrentPlaylist(TYPE_VIDEO);
  app.nested=[&]{CFileItem y;y.id=50;app.nested=[&]{app.abandon=true;};assert(!app.PlayFile(y,"old",false,false));};
  assert(playlist.Play(0,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40&&playlist.GetCurrentPlaylist()==TYPE_NONE);
 }
 // A queued open keeps the intent of the request and owns the player state.
 {CApplication app;begin(app);reset();auto old=app.player.m_pPlayer;old->canClose=true;old->playing=false;
  CFileItem x;x.id=60;assert(app.PlayFile(x,"old",false,false)&&app.player.HasPendingOpen());
  app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==60&&app.player.m_waitForPlaybackStop);
 }
 // A queued open X that opens and ends during the resolution of a newer item Y:
 // Y supersedes the end of X, and Y plays.
 {CApplication app;begin(app);reset();playlist(app);auto& playlist=CServiceBroker::playlist;
  auto z=std::make_shared<CFileItem>();z->id=42;playlist.list.m_vecItems.push_back(z);
  auto old=app.player.m_pPlayer;old->playing=false;
  assert(playlist.Play(0,"old")&&app.player.HasPendingOpen());app.player.OnPlaybackStopped();
  app.nested=[&]{old->canClose=true;app.Frame();assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==40);
   app.player.m_pPlayer->playing=false;app.player.m_pPlayer->canClose=true;app.Ended();
   assert(!app.player.HasPendingOpen());};
  assert(playlist.Play(1,"old"));app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==41);
 }
 // A request that fails before the player keeps the stack of the current
 // playback; an accepted request ends the stack.
 {CApplication app;begin(app);reset();app.stack.regular=true;
  app.abandon=true;CFileItem y;y.id=50;assert(!app.PlayFile(y,"old",false,false));
  assert(app.stack.clears==0&&app.stack.adopts==0);
  CFileItem z;z.id=51;assert(app.PlayFile(z,"old",false,false));assert(app.stack.clears==1);
 }
 // A stop alone during a backend open: the open writes no player state.
 {CApplication app;begin(app);reset();
  app.player.m_pPlayer.reset();app.player.m_waitForPlaybackStop=false;
  backendWait=[&]{app.player.m_pPlayer->canClose=true;app.StopPlaying();assert(!app.player.HasPlayer());};
  CFileItem x;x.id=99;assert(!app.PlayFile(x,"remote",false,false));
  assert(!app.player.HasPlayer()&&!app.player.m_waitForPlaybackStop&&!app.player.m_openedVideo);
 }
 // A newer request that fails during a backend open: the backend open plays.
 {CApplication app;begin(app);reset();
  app.player.m_pPlayer.reset();app.player.m_waitForPlaybackStop=false;
  backendWait=[&]{CFileItem y;y.id=50;app.nested=[&]{app.abandon=true;};assert(!app.PlayFile(y,"old",false,false));};
  CFileItem x;x.id=99;assert(app.PlayFile(x,"remote",false,false));
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==99&&app.player.m_waitForPlaybackStop);
 }
 std::cout<<"PASS production playlist Play: a stop cancels an item without a failure\n";
}
"""


def production_play(code, playlist, extract):
    """Swap the recording Play for the production Play and its failure handling."""
    code = code[:code.index('int main(){')]
    code = code.replace('#include <cassert>', '#include <cassert>\n#include <chrono>\n#include "' + str(ROOT / 'xbmc/utils/ScopeGuard.h') + '"')
    code = code.replace('struct CFileItem {', """struct Tag {};
struct CFileItem {
 void SetProperty(const char*,bool){}void ClearProperty(const char*){}void SetDetectorBinding(std::shared_ptr<int>){}
 bool IsVideoDb()const{return false;} bool HasVideoInfoTag()const{return false;}
 Tag tag;Tag* GetVideoInfoTag(){return &tag;}std::string path="probe",dynpath;int m_iprogramCount=0;
 const std::string& GetPath()const{return path;}const std::string& GetDynPath()const{return dynpath.empty()?path:dynpath;}
 void SetPath(const std::string& p){path=p;}void SetDynPath(const std::string& p){dynpath=p;}
 int GetStartOffset()const{return 0;}void SetStartOffset(int){}
""", 1)
    code = code.replace('struct SettingsComponent {', """static int playlistRetries=100;static std::function<void()> failureDialog;
struct CAdvancedSettings {int m_playlistRetries=playlistRetries,m_playlistTimeout=0;};
struct SettingsComponent {
 std::shared_ptr<CAdvancedSettings> GetAdvancedSettings(){return std::make_shared<CAdvancedSettings>();}
""", 1)
    old = 'struct CPlayList {int size()const{return 2;}int GetPlayable()const{return 2;}};'
    assert code.count(old) == 1
    code = code.replace(old, """using CFileItemPtr=std::shared_ptr<CFileItem>;
// The production expansion. The load of a playlist file can process messages
// (the wake-on-access dialog), and a loaded playlist has the entries 50, 51, 52.
static std::function<void()> expansion;
struct CPlayList {
 std::vector<CFileItemPtr> m_vecItems{std::make_shared<CFileItem>(),std::make_shared<CFileItem>()};int unplayable=0;
 CCriticalSection m_critSection;
 int size()const{return m_vecItems.size();}int GetPlayable()const{return size()-unplayable;}
 bool Expand(@EXPAND@);CFileItemPtr TryGet(int n)const{return n>=0&&n<size()?m_vecItems[n]:nullptr;}
 void SetPlayed(bool){}void SetUnPlayable(int){++unplayable;}void Clear(){m_vecItems.clear();}
 CFileItemPtr operator[](int n){return m_vecItems[n];}void Remove(int n){m_vecItems.erase(m_vecItems.begin()+n);}
 std::vector<CFileItemPtr> GetItems()const{return m_vecItems;}
 void DecrementOrder(int){}void AnnounceRemove(int){}void AnnounceAdd(const CFileItemPtr&,int){}
 int AddLocked(const CFileItemPtr& entry,int position,int){m_vecItems.insert(m_vecItems.begin()+position,entry);return position;}
 bool Load(const std::string&){
  if(auto hook=std::exchange(expansion,{}))hook();
  m_vecItems.clear();
  for(int id:{50,51,52}){auto entry=std::make_shared<CFileItem>();entry->id=id;entry->path="entry.mkv";m_vecItems.push_back(entry);}
  return true;
 }
};
struct CPlayListFactory {
 static CPlayList* Create(const CFileItem& item){
  const std::string& path=item.GetDynPath();
  return path.size()>4&&path.compare(path.size()-4,4,".m3u")==0?new CPlayList:nullptr;
 }
};
namespace StringUtils {bool EqualsNoCase(const std::string& a,const std::string& b){return a==b;}}""")
    # An abandoned resolution fails one item only
    assert code.count('  if(abandon)return false;') == 1
    code = code.replace('  if(abandon)return false;', '  if(std::exchange(abandon,false))return false;')
    code = code.replace('int selected=-1;', 'int m_iFailedSongs=0;std::chrono::steady_clock::time_point m_failedSongsStart;int selected=-1;')
    code = code.replace('const CPlayList& GetPlaylist(int)const{return list;}',
                        'const CPlayList& GetPlaylist(int)const{return list;} CPlayList& GetPlaylist(int){return list;}')
    signature = 'bool CPlayListPlayer::Play(int iSong,'
    production = extract(playlist, signature)
    declaration = production[:production.index('{')]
    optional = 'std::optional<bool>' in declaration
    code = code.replace('bool Play(int,const std::string&,bool);',
                        'bool Play(int,const std::string&,bool=false,bool=false' +
                        (',std::optional<bool> = std::nullopt' if optional else '') +
                        ');bool PlayPrevious(){return PlayNext(-1);}')
    production = production.replace('  const Id playlistId', '  auto& g_application=*app;\n  const Id playlistId', 1)
    expand = extract((ROOT / 'xbmc/playlists/PlayList.cpp').read_text(), 'bool CPlayList::Expand(')
    code = code.replace('@EXPAND@', expand[expand.index('(') + 1:expand.rindex(')', 0, expand.index('{'))], 1)
    stub = code[code.index('bool CPlayListPlayer::Play(int index,'):]
    stub = stub[:stub.index('\n}\n') + 3]
    return code.replace(stub, """constexpr int STARTOFFSET_RESUME=-1,LOGERROR=1,LOGDEBUG=2;
struct CLog {template<class... A>static void Log(A&&...){}};
struct CURL {CURL(const std::string&){} static std::string GetRedacted(const std::string& s){return s;}};
namespace XFILE {struct CVideoDatabaseFile {static Tag GetVideoTag(const CURL&){return {};}};}
namespace HELPERS {template<class... A>void ShowOKDialogText(A&&...){if(failureDialog){auto dialog=std::move(failureDialog);dialog();}}}
namespace DETECTOR_PLAYBACK {constexpr auto PROPERTY="playback";}
namespace DETECTOR_ADMISSION {constexpr auto ITEM_SKIP="skip";void CaptureItem(CFileItem&) {}}
""" + production + expand) + PLAY_TESTS



# The Blu-ray menu of a stack part: the production stack branch and menu block run
# in a small StartPlayback. A newer open accepted during the menu supersedes the
# open, also when the user then cancels the menu.
MENU = r"""
#include <cassert>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
struct CFileItem {
  bool stack = false;
  bool IsStack() const { return stack; }
  bool IsBDFile() const { return !stack; }
  bool IsDiscImage() const { return false; }
};
struct CPlayerOptions { double startpercent = 0, starttime = 0; };
struct Playlist { int current = 0; void ResetCurrentPlaylist() { current = -1; } } playlist;
struct CServiceBroker { static Playlist& GetPlaylistPlayer() { return playlist; } };
struct Factory {
  std::string GetDefaultPlayer(const CFileItem&) const { return "VideoPlayer"; }
  bool IsExternalPlayer(const std::string&) const { return false; }
  bool IsRemotePlayer(const std::string&) const { return false; }
};
struct ServiceManager { Factory factory; Factory& GetPlayerCoreFactory() { return factory; } };
struct Player {
  uint64_t accepted = 0, sequence = 0;
  bool OpenSuperseded(uint64_t intent) const { return intent < accepted; }
};
struct CGUIDialogSimpleMenu {
  static std::function<bool()> menu;
  static bool ShowPlaySelection(CFileItem&) { return menu(); }
};
std::function<bool()> CGUIDialogSimpleMenu::menu;
struct App {
  std::unique_ptr<ServiceManager> m_ServiceManager = std::make_unique<ServiceManager>();
  Player player;
  int opened = 0;
  bool PlayStack(CFileItem&, bool, uint64_t intent) {
    return StartPlayback(CFileItem{}, "", true, false, intent);
  }
  bool StartPlayback(CFileItem item, const std::string& player, bool bRestart, bool playlistItem,
                     uint64_t intent) {
    auto* appPlayer = &this->player;
    CPlayerOptions options;
@ENDPLAYLIST@
@STACK@
@MENU@
  if (appPlayer->OpenSuperseded(intent))
    return false;
  appPlayer->accepted = intent;
  ++opened;
  endPlaylist();
  return true;
  }
};
int main() {
  for (const bool select : {false, true}) {
    App app;
    playlist.current = 0;
    const uint64_t older = ++app.player.sequence;
    CGUIDialogSimpleMenu::menu = [&] {
      // a newer playlist item is accepted during the menu
      playlist.current = 1;
      app.player.accepted = ++app.player.sequence;
      return select;
    };
    assert(!app.StartPlayback(CFileItem{true}, "", false, false, older));
    assert(playlist.current == 1 && app.opened == 0);
  }
  for (const bool select : {false, true}) {
    App app;
    playlist.current = 0;
    CGUIDialogSimpleMenu::menu = [&] { return select; };
    // a cancelled menu ends the request without an error, a selection opens the part
    assert(app.StartPlayback(CFileItem{true}, "", false, false, ++app.player.sequence));
    assert(playlist.current == -1 && app.opened == (select ? 1 : 0));
  }
}
"""


def menu_program(play):
    lambda_ = play.index('  const auto endPlaylist = [&]')
    stack = play.index('  if (item.IsStack())')
    menu = play.index('  // a disc image might be Blu-Ray disc')
    return (MENU.replace('@ENDPLAYLIST@', play[lambda_:play.index('  };\n', lambda_) + 5])
            .replace('@STACK@', play[stack:play.index('\n  }\n', stack) + 4])
            .replace('@MENU@', play[menu:play.index('  // this really aught to be inside !bRestart', menu)]))


def compile_run(code, label, sanitizer='address,undefined'):
    with tempfile.TemporaryDirectory(prefix='replaced-playback-end-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(code)
        if os.environ.get('DUMP'): Path(os.environ['DUMP'] + label).write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                        '-fsanitize=' + sanitizer, '-fno-omit-frame-pointer',
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=15)

# The seeks of a stack run on the main thread, and the caller waits: the later
# controls of the caller follow the seek. A seek after the stop, or a seek that
# a stop on the main thread overtakes, changes no stack and restarts nothing. A
# seek into a part whose open waits for the close of the previous part sets the
# start of the waiting open.
SEEK = r"""
 auto& messenger=CServiceBroker::messenger;
 for(int order=0;order<2;++order){CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  if(order==0){app.StopPlaying();app.Stopped();assert(app.player.m_closingPlayer==old&&!app.player.IsPlaying());}
  // the seek of a script checks the playback, then the main thread stops first
  if(order==1)app.player.canSeekHook=[&]{app.StopPlaying();app.Stopped();};
  app.SeekTime(120);
  assert(old->seeks==0&&app.stack.position==0&&!messenger.queued&&messenger.posted.empty());
  assert(messenger.sent==order&&!app.player.HasPendingOpen());
  old->canClose=true;app.Frame();
  assert(!app.player.HasPlayer()&&!app.player.HasPendingOpen());
 }
 // a seek in the current part, then a relative seek of the same caller
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  app.SeekTime(50);app.player.SeekTimeRelative(-10000);
  assert(messenger.sent==1&&messenger.posted.empty()&&!app.player.HasPendingOpen());
  assert((old->seekLog==std::vector<std::string>{"abs50000","rel-10000"}));
 }
 // the newer seek wins: a seek into another part, then a seek into the current part
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  app.SeekTime(120);app.SeekTime(50);
  assert(old->seeks==0&&messenger.sent==2);
  assert(app.stack.position==0&&app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==100);
  assert(app.player.m_nextItem.pItem->startOffset==50000);
  old->canClose=true;app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==100);
 }
 // a newer seek during the open of another part (a dialog) plays the part of
 // the newest seek: the stack plays the old part until the open is accepted
 for(int target:{150,30}){CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  app.nested=[&]{assert(app.stack.position==0);app.SeekTime(target);};
  app.SeekTime(120);
  const int part=target/100;
  assert(old->seeks==0&&app.stack.position==part&&app.player.HasPendingOpen()&&app.stackPlays==2);
  assert(app.player.m_nextItem.pItem->id==100+part&&app.player.m_nextItem.pItem->startOffset==uint64_t(target%100*1000));
  assert(app.m_stackPartOpen.intent==0);
  old->canClose=true;app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==100+part);
 }
 // a newer part open that fails before the player during the open of another
 // part: the older part open stays the user's intent and changes the part
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  app.nested=[&]{app.abandon=true;app.SeekTime(30);app.abandon=false;
   assert(app.stack.position==0&&app.m_stackPartOpen.part==1);};
  app.SeekTime(120);
  assert(old->seeks==0&&app.stack.position==1&&app.player.HasPendingOpen()&&app.stackPlays==2);
  assert(app.player.m_nextItem.pItem->id==101&&app.player.m_nextItem.pItem->startOffset==20000);
  assert(app.m_stackPartOpen.intent==0);
 }
 // a failed open of another part leaves the stack at the playing part
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;app.abandon=true;
  app.SeekTime(120);
  assert(app.stack.position==0&&!app.player.HasPendingOpen()&&app.m_stackPartOpen.intent==0);
  app.abandon=false;app.SeekTime(50);assert(old->seeks==1);
 }
 // the end of a part: a seek during the open of the next part opens the part of
 // the seek
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=app.stack.next=true;
  app.nested=[&]{assert(app.stack.position==0);app.SeekTime(130);};
  app.Ended();
  assert(app.stack.position==1&&app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==101);
  assert(app.player.m_nextItem.pItem->startOffset==30000&&app.m_stackPartOpen.intent==0);
 }
 // a seek into the part that waits for the close of the previous part
 {CApplication app;begin(app);reset();messenger={};messenger.main=&app;auto old=app.player.m_pPlayer;
  old->m_type="remote";old->playing=true;app.stack.regular=true;
  app.SeekTime(120);
  assert(app.stack.position==1&&app.player.HasPendingOpen()&&app.player.m_nextItem.pItem->id==101);
  assert(app.player.m_nextItem.pItem->startOffset==20000);
  app.SeekTime(150);app.player.SeekTimeRelative(-5000);
  assert(old->seeks==0&&old->seekLog.empty()&&app.player.m_nextItem.pItem->id==101);
  assert(app.player.m_nextItem.options.starttime==45&&app.player.m_nextItem.pItem->startOffset==45000);
  old->canClose=true;app.player.OnPlaybackStopped();app.Frame();
  assert(app.player.IsPlaying()&&app.player.m_pPlayer->lastItem==101);
 }
"""


# The accepted open adopts the staged stack on the main thread while a seek or a
# time query on another thread reads the current stack without the lock of the
# helper (Astra's round 26 probe): the list object of the current stack stays,
# and the reader sees the adopted items.
def stack_adopt_program(function):
    stack = (ROOT / 'xbmc/application/ApplicationStackHelper.cpp').read_text()
    items = (ROOT / 'xbmc/FileItem.cpp').read_text()
    size = function(items, 'int CFileItemList::Size() const')
    # The reader stops after the load of the list pointer, before the list lock.
    size = size.replace('{', '{\n if(reader){entered=true;while(!resumeReader)std::this_thread::yield();}', 1)
    return r"""#include <atomic>
#include <cassert>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
using CCriticalSection=std::recursive_mutex;
std::atomic<bool> entered{false},resumeReader{false};
thread_local bool reader=false;
struct CFileItemList {
 mutable CCriticalSection m_lock;std::vector<int> m_items;
 ~CFileItemList(){std::unique_lock<CCriticalSection> lock(m_lock);m_items.clear();}
 int Size() const;void Clear(){std::unique_lock<CCriticalSection> lock(m_lock);m_items.clear();}
 void Assign(const CFileItemList& list){std::unique_lock<CCriticalSection> lock(m_lock);m_items=list.m_items;}
};
struct CApplicationStackHelper {
 CCriticalSection m_critSection;std::map<int,int> m_stackmap;
 std::unique_ptr<CFileItemList> m_currentStack=std::make_unique<CFileItemList>();
 int m_currentStackPosition=0;bool m_currentStackIsDiscImageStack=false;std::string m_currentStackPath;
 void Adopt(CApplicationStackHelper&&);bool IsPlayingRegularStack() const;
};
""" + size + function(stack, 'void CApplicationStackHelper::Adopt(') + \
        function(stack, 'bool CApplicationStackHelper::IsPlayingRegularStack() const') + r"""
int main(){
 CApplicationStackHelper current,staged;current.m_currentStack->m_items={1};
 staged.m_currentStack->m_items={1,2};staged.m_currentStackPosition=1;
 const CFileItemList* list=current.m_currentStack.get();bool playing=false;
 std::thread seek([&]{reader=true;playing=current.IsPlayingRegularStack();});
 while(!entered)std::this_thread::yield();
 current.Adopt(std::move(staged));
 resumeReader=true;seek.join();
 assert(playing&&current.m_currentStack.get()==list&&current.m_currentStack->m_items.size()==2);
 assert(current.m_currentStackPosition==1&&staged.m_currentStack->m_items.empty());
}
"""


# A start callback of the player can arrive for an older item after the main
# thread accepted a newer stack (Astra's round 35 probe). The cleanup of the
# registrations at a start keeps the registrations of the accepted stack, and
# the stack seek and the fullscreen condition find the part of the stack.
# Every reader and writer of the registrations: the player threads (the start and
# the close callbacks), a job (the save of the file state) and the main thread
# (the adoption and the initialization of a stack, the fullscreen condition).
# The production key of a registration
STACK_HEADER = (ROOT / 'xbmc/application/ApplicationStackHelper.h').read_text()
REGISTRATION_KEY = STACK_HEADER[STACK_HEADER.index('  using RegistrationKey'):STACK_HEADER.index('  typedef std::map<RegistrationKey')]
REGISTRATION_ACCESSORS = (
    'void CApplicationStackHelper::Clear()',
    'bool CApplicationStackHelper::InitializeStack(',
    'CApplicationStackHelper::RegistrationKey CApplicationStackHelper::GetRegistrationKey(',
    'CFileItem& CApplicationStackHelper::GetStackPartFileItem(int partNumber)\n{',
    'const CFileItem& CApplicationStackHelper::GetStackPartFileItem(int partNumber) const',
    'void CApplicationStackHelper::ClearAllRegisteredStackInformation()',
    'void CApplicationStackHelper::SetRegisteredStack(',
    'int CApplicationStackHelper::GetRegisteredStackPartNumber(',
    'void CApplicationStackHelper::SetRegisteredStackPartNumber(',
    'uint64_t CApplicationStackHelper::GetRegisteredStackPartStartTimeMs(',
    'void CApplicationStackHelper::SetRegisteredStackPartStartTimeMs(',
    'uint64_t CApplicationStackHelper::GetRegisteredStackTotalTimeMs(',
    'void CApplicationStackHelper::SetRegisteredStackTotalTimeMs(',
    'CApplicationStackHelper::StackPartInformationPtr CApplicationStackHelper::GetStackPartInformation(\n    const RegistrationKey& key)\n{')


# A start callback of a player reads and removes registrations while the main
# thread adopts a newer stack and registers the parts of a stack. The lock of the
# registrations orders every access (ThreadSanitizer).
def stack_race_program(function):
    code = stack_registration_program(function)
    return code[:code.index('int main(){')] + r"""
#include <atomic>
#include <thread>
int main(){
 (void)Fullscreen;  // the main thread program checks the condition
 CApplicationStackHelper current;current.Stage({"a1.mkv","a2.mkv"},1);
 const CFileItem a1=current.GetStackPartFileItem(0),a2=current.GetStackPartFileItem(1),older{"older.mkv"};
 // The threads take turns through a relaxed counter, which orders nothing for
 // ThreadSanitizer: only the lock of the registrations orders the accesses, and a
 // read follows each write.
 std::atomic<int> turn{0};
 const auto await=[&](int value){while(turn.load(std::memory_order_relaxed)<value){}};
 std::thread callback([&]{
  for(int n=0;n<2000;++n){
   await(2*n+1);
   // the start of a part (ApplicationPlayerCallback::OnPlayBackStarted), the close of a part, a job
   for(const auto* part:{&a1,&a2}){
    const auto stack=current.GetRegisteredStack(*part);
    if(stack)assert(!stack->GetPath().empty());
    current.HasRegisteredStack(*part);
    current.GetRegisteredStackTotalTimeMs(*part);current.GetRegisteredStackPartStartTimeMs(*part);
    current.GetRegisteredStackPartNumber(*part);
   }
   current.OnPlayBackStarted(n%2?a2:older);
   turn.store(2*n+2,std::memory_order_relaxed);
  }});
 for(int n=0;n<2000;++n){
  CApplicationStackHelper staged;staged.Stage({"a1.mkv","a2.mkv"},1);
  current.Adopt(std::move(staged));
  for(const auto* part:{&a1,&a2}){
   current.SetRegisteredStack(*part,std::make_shared<CFileItem>(CFileItem{"stack://a1.mkv"}));
   current.SetRegisteredStackPartNumber(*part,1);current.SetRegisteredStackPartStartTimeMs(*part,n);
   current.SetRegisteredStackTotalTimeMs(*part,n);
  }
  if(n%100==99)current.ClearAllRegisteredStackInformation();
  turn.store(2*n+1,std::memory_order_relaxed);
  await(2*n+2);
 }
 callback.join();
}
"""


# The close callback of a part reads the registrations under the lock and saves
# the state of the item (the database) without the lock: the main thread reads the
# registrations at the open of the next part and does not wait for the save.
def close_callback_program(function):
    code = stack_registration_program(function)
    callback = (ROOT / 'xbmc/application/ApplicationPlayerCallback.cpp').read_text()
    close = function(callback, 'void CApplicationPlayerCallback::OnPlayerCloseFile(')
    return code[:code.index('int main(){')] + r"""
#include <thread>
struct CStreamDetails {double duration=0;void SetVideoDuration(int,double d){duration=d;}};
struct CVideoInfoTag {CStreamDetails m_streamDetails;};
struct CBookmark {double timeInSeconds=0,totalTimeInSeconds=0;int partNumber=0;};
struct CPlayedItem: CFileItem {CVideoInfoTag tag;CPlayedItem(const CFileItem& item):CFileItem(item){}
 bool IsAudio() const {return false;} bool IsVideo() const {return true;} CVideoInfoTag* GetVideoInfoTag(){return &tag;}};
struct CAdvancedSettings {int m_audioPlayCountMinimumPercent=0,m_videoPlayCountMinimumPercent=90,
 m_videoIgnorePercentAtEnd=8,m_videoIgnoreSecondsAtStart=180;};
struct CProfile {bool canWriteDatabases() const {return true;}};
struct CProfileManager {CProfile profile;const CProfile& GetCurrentProfile() const {return profile;}};
struct CSettingsComponent {std::shared_ptr<CAdvancedSettings> settings=std::make_shared<CAdvancedSettings>();
 std::shared_ptr<CAdvancedSettings> GetAdvancedSettings(){return settings;}
 std::shared_ptr<CProfileManager> GetProfileManager(){static auto m=std::make_shared<CProfileManager>();return m;}};
struct CAppComponents {std::shared_ptr<CApplicationStackHelper> stack=std::make_shared<CApplicationStackHelper>();
 template<class T> std::shared_ptr<T> GetComponent() const {return stack;}};
struct CServiceBroker {static CAppComponents& GetAppComponents(){static CAppComponents c;return c;}
 static CSettingsComponent* GetSettingsComponent(){static CSettingsComponent c;return &c;}};
static int saves=0;static CBookmark saved;static std::string savedPath;
struct CSaveFileState {static void DoWork(const CPlayedItem& item,const CBookmark& bookmark,bool){
 // the main thread can read the registrations during the save
 auto& lock=CServiceBroker::GetAppComponents().stack->m_critSection;bool free=false;
 std::thread main([&]{free=lock.try_lock();if(free)lock.unlock();});main.join();
 assert(free);++saves;saved=bookmark;savedPath=item.GetPath();}};
struct CApplicationPlayerCallback {void OnPlayerCloseFile(const CFileItem&,const CBookmark&);};
""" + close.replace('CFileItem fileItem(file);', 'CPlayedItem fileItem(file);') + r"""
int main(){
 (void)Fullscreen;  // the main thread program checks the condition
 auto& stack=*CServiceBroker::GetAppComponents().stack;stack.Stage({"a1.mkv","a2.mkv"},1);
 const CFileItem a2=stack.GetStackPartFileItem(1);
 stack.SetRegisteredStackTotalTimeMs(a2,3600000);stack.SetRegisteredStackPartStartTimeMs(a2,1800000);
 CBookmark bookmark;bookmark.timeInSeconds=600;bookmark.totalTimeInSeconds=1800;
 CApplicationPlayerCallback().OnPlayerCloseFile(a2,bookmark);
 // the bookmark of a part is a bookmark of the stack
 assert(saves==1&&savedPath=="stack://a1.mkv , a2.mkv"&&saved.timeInSeconds==2400&&saved.totalTimeInSeconds==3600&&saved.partNumber==1);
 // Stack A plays the part that stack B shares, and the user opens B: the late
 // close of the part of A saves the bookmark of A, not of B.
 stack.Stage({"shared.mkv","a2.mkv"},0);
 const CFileItem sharedA=stack.GetStackPartFileItem(0);
 stack.SetRegisteredStackTotalTimeMs(sharedA,3600000);
 CApplicationStackHelper staged;staged.Stage({"b1.mkv","shared.mkv"},1);
 const CFileItem sharedB=staged.GetStackPartFileItem(1);
 staged.SetRegisteredStackTotalTimeMs(sharedB,4000000);staged.SetRegisteredStackPartStartTimeMs(sharedB,3600000);
 stack.Adopt(std::move(staged));
 CApplicationPlayerCallback().OnPlayerCloseFile(sharedA,bookmark);
 assert(saves==2&&savedPath=="stack://shared.mkv , a2.mkv"&&saved.timeInSeconds==600&&saved.totalTimeInSeconds==3600&&saved.partNumber==0);
 // a single video of the same file saves the bookmark of the file
 CApplicationPlayerCallback().OnPlayerCloseFile(CFileItem{"shared.mkv"},bookmark);
 assert(saves==3&&savedPath=="shared.mkv"&&saved.timeInSeconds==600&&saved.totalTimeInSeconds==1800);
 // A stack can repeat a part (Sol's round 38 probe): each position of the part
 // saves the bookmark of the position.
 stack.Stage({"a.mkv","b.mkv","a.mkv"},0);
 for(int i=0;i<3;++i){const CFileItem part=stack.GetStackPartFileItem(i);
  stack.SetRegisteredStackPartStartTimeMs(part,i*1800000);stack.SetRegisteredStackTotalTimeMs(part,5400000);}
 for(int i:{0,2}){
  CApplicationPlayerCallback().OnPlayerCloseFile(stack.GetStackPartFileItem(i),bookmark);
  assert(savedPath=="stack://a.mkv , b.mkv , a.mkv"&&saved.timeInSeconds==600+i*1800&&saved.partNumber==i);
 }
}
"""


def stack_registration_program(function):
    stack = (ROOT / 'xbmc/application/ApplicationStackHelper.cpp').read_text()
    application = (ROOT / 'xbmc/application/Application.cpp').read_text()
    condition = application[application.index('    if (stack->GetCurrentPartNumber() == 0 ||'):]
    condition = condition[:condition.index('\n      options.fullscreen')]
    return r"""#include <algorithm>
#include <cassert>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
struct CURL {std::string path;};
// A property is a text or a number; a missing property is null.
struct CVariant {std::string value;bool null=true;CVariant()=default;
 CVariant(std::string text):value(std::move(text)),null(false){} CVariant(int number):value(std::to_string(number)),null(false){}
 const std::string& asString() const {return value;} int64_t asInteger(int64_t fallback=0) const {return null?fallback:std::stoll(value);}};
// The lock counts the locks of the thread, and the map of the registrations
// fails every access of a thread without the lock.
static thread_local int t_locks=0;
struct CCriticalSection {
 std::recursive_mutex mutex;
 void lock(){mutex.lock();++t_locks;}
 bool try_lock(){if(!mutex.try_lock())return false;++t_locks;return true;}
 void unlock(){--t_locks;mutex.unlock();}
};
template<class K,class V> struct CheckedMap {
 std::map<K,V> map;using iterator=typename std::map<K,V>::iterator;
 static void Check(){assert(t_locks>0&&"a registration access without the lock");}
 size_t count(const K& key) const {Check();return map.count(key);}
 V& operator[](const K& key){Check();return map[key];}
 iterator find(const K& key){Check();return map.find(key);}
 typename std::map<K,V>::const_iterator find(const K& key) const {Check();return map.find(key);}
 iterator begin(){Check();return map.begin();} iterator end(){Check();return map.end();}
 typename std::map<K,V>::const_iterator end() const {Check();return map.end();}
 iterator erase(iterator it){Check();return map.erase(it);}
 void clear(){Check();map.clear();} bool empty() const {Check();return map.empty();}
};
struct CFileItem {
 std::string path;int offset=0;std::map<std::string,CVariant> properties;
 CFileItem(std::string itemPath={},int startOffset=0):path(std::move(itemPath)),offset(startOffset){}
 const std::string& GetPath() const {return path;} const std::string& GetDynPath() const {return path;}
 int GetStartOffset() const {return offset;}
 bool IsStack() const {return path.rfind("stack://",0)==0;} CURL GetURL() const {return {path};}
 CVariant GetProperty(const std::string& key) const
 {const auto it=properties.find(key);return it==properties.end()?CVariant():it->second;}
 void SetProperty(const std::string& key,const CVariant& value){properties[key]=value;}
};
struct CFileItemList {
 std::vector<std::shared_ptr<CFileItem>> items;
 void Assign(const CFileItemList& list){items=list.items;} void Clear(){items.clear();}
 int Size() const {return static_cast<int>(items.size());} bool IsEmpty() const {return items.empty();}
 std::shared_ptr<CFileItem> operator[](int i) const {return items[i];}
};
// The parts of "stack://a.mkv , b.mkv"
struct CStackDirectory {bool GetDirectory(const CURL& url,CFileItemList& list){
 std::string rest=url.path.substr(8);
 for(size_t end;(end=rest.find(" , "))!=std::string::npos;rest=rest.substr(end+3))
  list.items.push_back(std::make_shared<CFileItem>(CFileItem{rest.substr(0,end)}));
 list.items.push_back(std::make_shared<CFileItem>(CFileItem{rest}));return true;}};
struct URIUtils {static bool IsDiscImageStack(const std::string&){return false;}};
struct CApplicationStackHelper {
 struct StackPartInformation {uint64_t m_lStackPartStartTimeMs=0;uint64_t m_lStackTotalTimeMs=0;
  int m_lStackPartNumber=0;std::shared_ptr<CFileItem> m_pStack;};
 using StackPartInformationPtr=std::shared_ptr<StackPartInformation>;
 """ + REGISTRATION_KEY + r"""
 using Stackmap=CheckedMap<RegistrationKey,StackPartInformationPtr>;
 mutable CCriticalSection m_critSection;Stackmap m_stackmap;
 std::unique_ptr<CFileItemList> m_currentStack=std::make_unique<CFileItemList>();
 int m_currentStackPosition=0;bool m_currentStackIsDiscImageStack=false;std::string m_currentStackPath;
 static RegistrationKey GetRegistrationKey(const CFileItem&);
 bool InitializeStack(const CFileItem&);void Clear();
 CFileItem& GetStackPartFileItem(int);const CFileItem& GetStackPartFileItem(int) const;
 void Adopt(CApplicationStackHelper&&);void OnPlayBackStarted(const CFileItem&);
 bool HasRegisteredStack(const CFileItem&) const;
 std::shared_ptr<const CFileItem> GetRegisteredStack(const CFileItem&) const;
 void ClearAllRegisteredStackInformation();
 void SetRegisteredStack(const CFileItem&,std::shared_ptr<CFileItem>);
 int GetRegisteredStackPartNumber(const CFileItem&);void SetRegisteredStackPartNumber(const CFileItem&,int);
 uint64_t GetRegisteredStackPartStartTimeMs(const CFileItem&) const;void SetRegisteredStackPartStartTimeMs(const CFileItem&,uint64_t);
 uint64_t GetRegisteredStackTotalTimeMs(const CFileItem&) const;void SetRegisteredStackTotalTimeMs(const CFileItem&,uint64_t);
 StackPartInformationPtr GetStackPartInformation(const RegistrationKey&);
 StackPartInformationPtr GetStackPartInformation(const RegistrationKey&) const;
 bool IsPlayingRegularStack() const;int GetCurrentPartNumber() const {return m_currentStackPosition;}
 // a stack with the parts of the paths, resumed at a part (CApplication::PlayStack)
 void Stage(const std::vector<std::string>& paths,int position)
 {std::string url="stack://";for(size_t i=0;i<paths.size();++i)url+=(i?" , ":"")+paths[i];
  const bool initialized=InitializeStack(CFileItem{url});assert(initialized);m_currentStackPosition=position;}
};
// The fullscreen condition of a stack part (CApplication::PlayFile)
static bool Fullscreen(const CApplicationStackHelper* stack,const CFileItem& item)
{bool fullscreen=false;
""" + condition + r"""
      fullscreen=true;
 return fullscreen;}
""" + stack[stack.index('namespace\n{'):stack.index('} // namespace\n') + 16] + ''.join(function(stack, signature) for signature in (
        'void CApplicationStackHelper::Adopt(', 'void CApplicationStackHelper::OnPlayBackStarted(',
        'bool CApplicationStackHelper::HasRegisteredStack(',
        'std::shared_ptr<const CFileItem> CApplicationStackHelper::GetRegisteredStack(',
        'CApplicationStackHelper::StackPartInformationPtr CApplicationStackHelper::GetStackPartInformation(\n    const RegistrationKey& key) const',
        'bool CApplicationStackHelper::IsPlayingRegularStack() const') + REGISTRATION_ACCESSORS) + r"""
int main(){
 const CFileItem older{"older.mkv"};
 for(int startedItem=0;startedItem<2;++startedItem){
  // stack A plays; the user opens stack B, resumed at part 2
  CApplicationStackHelper current,staged;current.Stage({"a1.mkv","a2.mkv"},1);
  staged.Stage({"b1.mkv","b2.mkv"},1);
  // the part items that the players open
  const CFileItem a2=current.GetStackPartFileItem(1),b1=staged.GetStackPartFileItem(0),b2=staged.GetStackPartFileItem(1);
  const auto stackB=staged.GetRegisteredStack(b2);
  current.Adopt(std::move(staged));
  // the start of an older item (a single video, or a part of stack A) arrives after the adoption
  current.OnPlayBackStarted(startedItem==0?older:a2);
  assert(current.IsPlayingRegularStack()&&current.GetRegisteredStack(b1)==stackB&&current.GetRegisteredStack(b2)==stackB);
  assert(Fullscreen(&current,b2)==false);
  // the started part of stack A keeps stack A for the close of the part (the resume point of the stack)
  assert(current.HasRegisteredStack(a2)==(startedItem==1));
  // the start of the part of stack B removes the registrations of the older stack
  current.OnPlayBackStarted(b2);
  assert(current.GetRegisteredStack(b2)==stackB&&current.GetRegisteredStack(b1)==stackB&&!current.HasRegisteredStack(a2));
  // a single video replaces the stack: the start of the video removes every registration
  current.Clear();current.OnPlayBackStarted(older);
  std::unique_lock<CCriticalSection> lock(current.m_critSection);
  assert(current.m_stackmap.empty());
 }
 // Stack A and stack B share a part. A plays the shared part, and the user opens
 // B before the end of A: every playback keeps the registration of the playback.
 {CApplicationStackHelper current,staged;current.Stage({"shared.mkv","a2.mkv"},0);
  staged.Stage({"b1.mkv","shared.mkv"},1);
  const CFileItem sharedA=current.GetStackPartFileItem(0),sharedB=staged.GetStackPartFileItem(1);
  const auto stackA=current.GetRegisteredStack(sharedA),stackB=staged.GetRegisteredStack(sharedB);
  assert(stackA&&stackB&&stackA!=stackB);
  current.Adopt(std::move(staged));
  // the late start and the close of the part of A find A; the part of B finds B
  current.OnPlayBackStarted(sharedA);
  assert(current.GetRegisteredStack(sharedA)==stackA&&current.GetRegisteredStack(sharedB)==stackB);
  assert(current.GetRegisteredStackPartNumber(sharedA)==0&&current.GetRegisteredStackPartNumber(sharedB)==1);
  // a single video of the same file belongs to no stack
  assert(!current.HasRegisteredStack(CFileItem{"shared.mkv"})&&!current.GetRegisteredStack(CFileItem{"shared.mkv"}));
  // the start of the part of B removes A and keeps B
  current.OnPlayBackStarted(sharedB);
  assert(!current.HasRegisteredStack(sharedA)&&current.GetRegisteredStack(sharedB)==stackB);
 }
}
"""


def main():
    source = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    signatures = ['std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()', 'bool CApplicationPlayer::IsPlaying() const',
                  'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::CompleteCloseCompletions()',
                  'bool CApplicationPlayer::ClosePlayer(', 'void CApplicationPlayer::OnPlaybackStopped()',
                  'void CApplicationPlayer::OpenNext(', 'void CApplicationPlayer::ResetPlayer(',
                  'bool CApplicationPlayer::OpenFile(', 'bool CApplicationPlayer::HasPendingOpen() const', 'uint64_t CApplicationPlayer::BeginOpen()',
                  'void CApplicationPlayer::EndOpen(', 'void CApplicationPlayer::Supersede(',
                  'bool CApplicationPlayer::IsPlayingOpenedVideo() const',
                  'bool CApplicationPlayer::OpenRequested() const', 'bool CApplicationPlayer::StopRequested() const',
                  'bool CApplicationPlayer::HasPendingVideoOpen() const',
                  'bool CApplicationPlayer::PreparePlaybackCleanup()',
                  'bool CApplicationPlayer::PlaybackCleanupCompleted() const']
    application = (ROOT / 'xbmc/application/Application.cpp').read_text()
    playlist = (ROOT / 'xbmc/PlayListPlayer.cpp').read_text()
    frame = function(application, 'void CApplication::FrameMove(')
    code = PRELUDE + '\n'.join(function(source, s) for s in signatures) + HARNESS + TESTS
    builtins = (ROOT / 'xbmc/interfaces/builtins/PlayerBuiltins.cpp').read_text()
    builtin = function(builtins, 'static int PlayOffset(')
    # The queue branch of PlayMedia: music that the user queues starts when nothing plays.
    queue = builtins.index('          const int oldSize = playlistPlayer.GetPlaylist(playlistId).size();')
    queue = builtins[queue:builtins.index('\n        }\n      }', queue)]
    code = code.replace('@CONTINUATION@', frame[frame.index('  const bool openingVideo ='):
                                                 frame.index('  // this will go away')])
    code = code.replace('@CLEANUP@', function(application, 'void CApplication::PlaybackCleanup()'))
    code = code.replace('@STOP@', function(application, 'void CApplication::StopPlaying('))
    stop = function(playlist, '  case TMSG_MEDIA_STOP:')
    code = code.replace('@MEDIASTOP@', stop[stop.index('{') + 1:-1])
    stop = application[application.index('  case GUI_MSG_PLAYLISTPLAYER_STOPPED:'):
                       application.index('  case GUI_MSG_PLAYBACK_AVSTARTED:')]
    code = code.replace('@PLAYLISTSTOP@', stop[stop.index(':') + 1:])
    code = code.replace('@PLAYNEXT@', function(playlist, 'bool CPlayListPlayer::PlayNext('))
    code = code.replace('@ENDED@', application[application.index('  case GUI_MSG_PLAYBACK_ENDED:'):
                                               application.index('  case GUI_MSG_PLAYLISTPLAYER_STOPPED:')])
    code = code.replace('@STOPPED@', application[application.index('  case GUI_MSG_PLAYBACK_STOPPED:'):
                                                 application.index('  case GUI_MSG_PLAYBACK_ENDED:')])
    terminal = min(playlist.find(case) for case in ('  case GUI_MSG_PLAYBACK_ENDED:', '  case GUI_MSG_PLAYBACK_STOPPED:')
                   if case in playlist)
    code = code.replace('@PLAYLISTTERMINAL@', playlist[terminal:playlist.index('  case GUI_MSG_PLAYBACK_STARTED:')])
    code = code.replace('@PLAYLISTFUNCTIONS@', '\n'.join(function(playlist, s) for s in (
        'int CPlayListPlayer::GetNextItemIdx(int offset) const', 'void CPlayListPlayer::Reset()',
        'void CPlayListPlayer::ResetCurrentPlaylist()',
        'void CPlayListPlayer::SetCurrentPlaylist(')))
    wrapper = function(application, 'bool CApplication::PlayFile(')
    code = code.replace('@PLAYFILE@', wrapper[wrapper.index('{') + 1:wrapper.rindex('}')])
    code = code.replace('@ENDOPEN@', function(application, 'void CApplication::EndOpen(') +
                        function(application, 'bool CApplication::PlayStackPart('))
    play = function(application, 'bool CApplication::StartPlayback(')
    intent = play.index('  if (!bRestart)\n  {')
    code = code.replace('@INTENT@', play[intent:play.index('\n  }\n', intent) + 4])
    lambda_ = play.index('  const auto endPlaylist = [&]')
    code = code.replace('@ENDPLAYLIST@', play[lambda_:play.index('  };\n', lambda_) + 5])
    # The accepted open and the accepted stack both repeat the reset.
    accept = play.index('  appPlayer->OpenFile(item, options,')
    # A stop or a newer open during a resolution, a dialog or a backend open
    # supersedes the open: the check comes after the last dialog and before the
    # accepted open and the accepted stack, and again after the open.
    cancelled = '  if (appPlayer->OpenSuperseded(intent))\n    return false;\n'
    code = code.replace('@CANCELLED@', cancelled)
    assert play.rindex(cancelled, 0, accept) > play.index('  // --- END ---'), 'open accepted after a stop'
    stack_open = play.index('    if (!PlayStack(item, bRestart, intent))')
    assert play.rindex(cancelled, 0, stack_open) > play.index('if (dir && !dir->Resolve(item))'), 'stack accepted after a stop'
    assert '  endPlaylist();' in play[accept:], 'accepted open does not end the playlist'
    assert play.index(cancelled, accept) < play.index('  endPlaylist();', accept), 'backend open not superseded'
    # One request owns one intent: the playlist attempt and the stack part.
    assert 'if (attempt)\n    return StartPlayback(std::move(item), player, bRestart, playlistItem, attempt);' in wrapper
    assert 'PlayStack(item, bRestart, intent)' in play
    # A stack request prepares the stack privately; the accepted open adopts the
    # stack. Every effect on the current playback follows the last dialog check,
    # and the open reads the stack of the open, not the current stack.
    stack = function(application, 'bool CApplication::PlayStack(')
    assert 'GetComponent<CApplicationStackHelper>' not in stack
    assert 'return StartPlayback(std::move(selectedStackPart), "", true, false, intent, &staged);' in stack
    end = play.index('  // --- END ---')
    for effect in ('stackHelper->Adopt(', 'stackHelper->Clear()', 'SetPlaySpeed(1)', 'CUtil::ClearSubtitles()',
                   'RemoveThreadMessageByMessageIds('):
        assert effect in play and play.rindex(cancelled, 0, play.index(effect)) > end, effect
    assert 'stackHelper->Is' not in play and 'stackHelper->Get' not in play, 'open reads the current stack'
    acceptance = play.index('  // The open is accepted')
    code = code.replace('@ACCEPTANCE@', play[acceptance:play.index('  {\n    // for playing a new item', acceptance)])
    callback = (ROOT / 'xbmc/application/ApplicationPlayerCallback.cpp').read_text()
    started = function(callback, 'void CApplicationPlayerCallback::OnPlayBackStarted(')
    assert 'const auto stack = stackHelper->GetRegisteredStack(file);\n  if (stack && (' in started, 'callback reads an absent stack'
    media = function(application, 'bool CApplication::StartMedia(')
    assert media.index('ResetCurrentPlaylist();') < media.index('GetResolvedPluginResult(item)'), 'request keeps the playlist'
    assert 'g_application.PlayFile(*item, player, bAutoPlay, true, attempt);' in playlist or \
        'g_application.PlayFile(request, player, bAutoPlay, true, attempt);' in playlist
    stack = play[play.index('  if (item.IsStack())'):]
    assert stack.index('  endPlaylist();') < stack.index('  return true;'), 'accepted stack does not end the playlist'
    # The open registers before the resolution and the dialogs that process messages.
    assert wrapper.index('appPlayer->BeginOpen();') < wrapper.rindex('StartPlayback('), 'open registers late'
    # A trailer is a new open: the playlist and the stack of the movie end.
    trailer = application[application.index('  case GUI_MSG_PLAY_TRAILER:'):]
    trailer = trailer[:trailer.index('    break;')]
    assert 'PostMsg(TMSG_MEDIA_PLAY, 0, 0,' in trailer and 'PostMsg(TMSG_MEDIA_PLAY, 1,' not in trailer
    media = playlist.index('    // first check if we were called from the PlayFile() function')
    code = code.replace('@MEDIAPLAY@', playlist[media:playlist.index('      return;\n    }\n', media) + 20])
    # A request that resolves before PlayFile registers its open first, and a
    # stop during the resolution cancels the request.
    media = function(application, 'bool CApplication::PlayMedia(')
    assert 'appPlayer->BeginOpen();' in media and 'EndOpen(intent);' in media, 'PlayMedia without an open'
    start = function(application, 'bool CApplication::StartMedia(')
    resolved = start.index('GetResolvedPluginResult(item)')
    assert start.index('if (appPlayer->OpenSuperseded(intent))', resolved) < start.index('if (item.IsSmartPlayList())')
    # The last step that can wake a share before the handoff: the name read of a
    # smart playlist, the load of a playlist
    for anchor, handoff in (('GetRecursiveListing(', 'if (items.Size())'),
                            ('OpenAndReadName(', 'ProcessAndStartPlaylist(smartpl'),
                            ('CGUIDialogBusy::Wait(&getPlaylist', 'ProcessAndStartPlaylist(item.GetPath()')):
        assert start.index('OpenSuperseded(intent)', start.index(anchor)) < start.index(handoff), anchor
    single = playlist[playlist.index('        if (list->Size() == 1 && !(*list)[0]->IsPlayList())'):]
    single = single[:single.index('        }\n        else\n')]
    assert single.index('appPlayer->BeginOpen();') < single.index('GetResolvedPluginResult(*item)')
    assert single.index('!appPlayer->OpenSuperseded(intent)') < single.index('Play(item, pMsg->strParam);')
    assert single.rindex('g_application.EndOpen(intent);') > single.index('PlayMedia(*item')
    # The base program also runs the production Playlist.PlayOffset builtin.
    base = code.replace('bool Play(int,const std::string&,bool);', 'bool Play(int,const std::string&,bool=false);', 1)
    base = base.replace('int selected=-1;CPlayList list;', 'int adds=0;void Add(int,int){++adds;}void Insert(int,int,int){++adds;}'
                        'int GetCurrentItemIdx(){return m_iCurrentSong;}int selected=-1;CPlayList list;', 1)
    base = base.replace('int main(){', """constexpr int LOGERROR=0;
struct CLog {template<class... A>static void Log(A&&...){}};
namespace StringUtils {void ToLower(std::string& s){for(auto& c:s)c=std::tolower(c);}}
static CApplication* g_playOffsetApp;
#define g_application (*g_playOffsetApp)
""" + builtin + """
static void QueueMusic(){
  auto& playlistPlayer=CServiceBroker::GetPlaylistPlayer();
  const auto& components=CServiceBroker::GetAppComponents();
  int playlistId=TYPE_MUSIC,items=1,playOffset=0;
  bool containsMusic=true,playNext=false,hasPlayOffset=false;
""" + queue + """
}
#undef g_application
int main(){""", 1)
    # The base program also runs the production stack seek.
    base = base.replace('struct CFileItem {', 'struct CFileItem {uint64_t startOffset=0;void SetStartOffset(uint64_t o){startOffset=o;}'
                        'bool HasProperty(const char*)const{return false;}', 1)
    base = base.replace('struct IPlayer {', 'struct IPlayer {int seeks=0;std::vector<std::string> seekLog;'
                        'void SeekTime(int64_t t){++seeks;seekLog.push_back("abs"+std::to_string(t));}'
                        'bool SeekTimeRelative(int64_t t){seekLog.push_back("rel"+std::to_string(t));return true;}', 1)
    assert base.count('struct CPlayerOptions {bool fullscreen=false;};')==1
    base = base.replace('struct CPlayerOptions {bool fullscreen=false;};',
                        'struct CPlayerOptions {bool fullscreen=false;double startpercent=0,starttime=0;std::string state;};', 1)
    base = base.replace('struct CApplicationPlayer {', 'struct CApplicationPlayer {mutable std::function<void()> canSeekHook;'
                        'bool CanSeek()const{if(canSeekHook){auto hook=std::move(canSeekHook);hook();}return true;}'
                        'void SeekTime(int64_t);void SeekTimeRelative(int64_t);bool SeekWaitingItem(int64_t,bool);'
                        'int64_t GetTime()const{return 0;}std::shared_ptr<IPlayer> GetControllable();', 1)
    base = base.replace('struct CApplicationStackHelper {', 'struct CApplicationStackHelper {'
                        'int GetStackPartNumberAtTimeMs(uint64_t t){return t>=100000?1:0;}'
                        'uint64_t GetStackPartStartTimeMs(int part){return part*100000;}', 1)
    assert 'struct Messenger {void PostMsg(int){}};' in base
    base = base.replace('struct Messenger {void PostMsg(int){}};', 'constexpr int TMSG_MEDIA_PLAY=8,TMSG_MEDIA_SEEK_STACK_TIME=9;\n'
                        'struct CApplication;\n'
                        'struct Messenger {void* queued=nullptr;CApplication* main=nullptr;int sent=0;std::vector<int64_t> posted;'
                        'void PostMsg(int){}void PostMsg(int,int,int,void* payload){queued=payload;}'
                        'void PostMsg(int,int64_t ms){posted.push_back(ms);}int SendMsg(int id,int64_t ms);};', 1)
    base = base.replace('struct CApplication {', 'struct CApplication {void SeekTime(double);void SeekStackTime(int64_t);', 1)
    base = '#include <cmath>\n' + base
    # The main thread runs the message, and the caller waits for the seek.
    base += ('int Messenger::SendMsg(int id,int64_t ms){assert(id==TMSG_MEDIA_SEEK_STACK_TIME);++sent;'
             'main->SeekStackTime(ms);return 0;}\n')
    base = base.replace(' std::cout<<"PASS replaced playback end', SEEK + ' std::cout<<"PASS replaced playback end', 1)
    base += function(source, 'std::shared_ptr<IPlayer> CApplicationPlayer::GetControllable()')
    base += function(source, 'bool CApplicationPlayer::SeekWaitingItem(')
    base += function(source, 'void CApplicationPlayer::SeekTime(')
    base += function(source, 'void CApplicationPlayer::SeekTimeRelative(')
    base += function(application, 'void CApplication::SeekTime(')
    base += function(application, 'void CApplication::SeekStackTime(')
    # The playlist player runs the stack seek of the message on the main thread.
    handler = playlist[playlist.index('  case TMSG_MEDIA_SEEK_STACK_TIME:'):]
    assert 'g_application.SeekStackTime(pMsg->param3);' in handler[:handler.index('break;')]
    base = base.replace('Playlist.PlayOffset and queue\\n"', 'Playlist.PlayOffset and queue; stack seek\\n"', 1)
    compile_run(base, '')
    compile_run(production_play(code, playlist, function), '-play')
    compile_run(menu_program(play), '-menu')
    compile_run(stack_adopt_program(function), '-adopt')
    compile_run(stack_registration_program(function), '-registrations')
    # The lock of the registrations is the lock of the start callback and of the close callback.
    header = (ROOT / 'xbmc/application/ApplicationStackHelper.h').read_text()
    assert 'mutable CCriticalSection m_critSection;' in header, 'const readers of the registrations need the lock'
    compile_run(stack_race_program(function), '-registration-race', 'thread')
    compile_run(close_callback_program(function), '-close-callback')
    print('PASS production Blu-ray menu: a newer open during the menu supersedes the open, also on cancel')
    print('PASS production stack adoption: a reader on another thread keeps the list of the current stack')
    print('PASS production stack registrations: the start of an older item keeps the registrations of the accepted stack')
    print('PASS production stack registration lock: the callbacks and the main thread access the registrations under the lock (ThreadSanitizer)')
    print('PASS production close callback: the bookmark of a part is a bookmark of the stack, and the save runs without the lock of the registrations')


if __name__ == '__main__':
    main()
