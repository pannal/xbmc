#!/usr/bin/env python3
"""Production deferred player/playlist/cleanup ordering with recording GUI/player boundaries.
No full GUI, media decoder, HDMI or device acceptance.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
retirement = runpy.run_path(str(ROOT / 'tools/test-player-retirement.py'))
function = retirement['function']
PRELUDE = retirement['PRELUDE'].replace(
    'bool IsDVDFile()const{return false;}',
    'bool IsDVDFile()const{return false;} bool video=true;bool IsVideo()const{return video;} bool IsCDDA()const{return false;} bool IsOnDVD()const{return false;}')
PRELUDE = PRELUDE.replace('struct CPlayerOptions {};', 'struct CPlayerOptions {bool fullscreen=false;};')
PRELUDE = PRELUDE.replace('#include <cassert>', '#include <cassert>\n#include <functional>')
PRELUDE = PRELUDE.replace('CCriticalSection* applicationLock=nullptr;', 'std::function<void()> onClose;CCriticalSection* applicationLock=nullptr;')
PRELUDE = PRELUDE.replace('++closes;return canClose;', 'auto work=std::move(onClose);onClose={};if(work)work();++closes;return canClose;')
PRELUDE = PRELUDE.replace('bool playing=false,canClose=false;', 'bool playing=false,canClose=false,hasVideo=false,hasAudio=false,lastFullscreen=false,failOpen=false;')
PRELUDE = PRELUDE.replace('const CPlayerOptions&){++opens;', 'const CPlayerOptions& options){lastFullscreen=options.fullscreen;++opens;')
PRELUDE = PRELUDE.replace('playing=true;return true;', 'if(failOpen)return false;playing=true;return true;')
PRELUDE = PRELUDE.replace('m_pPlayer->applicationLock=&m_playerLock;', 'm_pPlayer->applicationLock=&m_playerLock;m_pPlayer->failOpen=failOpen;')
PRELUDE = PRELUDE.replace('int created=0;', """int created=0;bool failCreate=false,failOpen=false;
  bool HasPendingOpen()const;bool HasPendingVideoOpen()const;
  bool HasPlayer()const{return bool(m_pPlayer);}
  bool PreparePlaybackCleanup();bool PlaybackCleanupCompleted()const;bool PlaybackCleanupPending()const{return m_cleanupPending;}
  bool IsPlaying(){return m_pPlayer&&m_pPlayer->playing;}
  bool IsPlayingVideo(){return IsPlaying()&&m_pPlayer->hasVideo;}
  bool IsPlayingAudio(){return IsPlaying()&&m_pPlayer->hasAudio;}
  std::string GetName(){return "old";}""")
PRELUDE = PRELUDE.replace('assert(!m_pPlayer);++created;', 'assert(!m_pPlayer);if(failCreate)return;++created;')
HARNESS = r"""
#include <iostream>
constexpr int WINDOW_FULLSCREEN_VIDEO=1,WINDOW_FULLSCREEN_GAME=2,WINDOW_VISUALISATION=3,HOME=4;
constexpr int GUI_MSG_PLAYLISTPLAYER_STOPPED=5,TMSG_QUIT=6;
constexpr int WINDOW_SLIDESHOW=7;
namespace PLAYLIST {using Id=int;constexpr int TYPE_NONE=0,TYPE_PICTURE=1,TYPE_VIDEO=2,TYPE_MUSIC=3;}
using namespace PLAYLIST;
struct CGUIMessage {CGUIMessage(int,int,int,int,int){}};
struct CGUIDialogKaiToast {enum {Info};static void QueueNotification(int,int,int){}};
struct Strings {int Get(int n){return n;}}g_localizeStrings;
struct CApplicationStackHelper {int clears=0;void Clear(){++clears;}};
struct CApplicationPowerHandling {void WakeUpScreenSaverAndDPMS(){}};
struct Party {int disabled=0;void Disable(){++disabled;}}g_partyModeManager;
struct Gfx {bool fullscreen=true;int restores=0,hdr=1;std::vector<int> restoreHdr;
 void SetFullScreenVideo(bool b){fullscreen=b;if(!b){++restores;restoreHdr.push_back(hdr);}}};
static Gfx gfx;
struct Manager {int active=WINDOW_FULLSCREEN_VIDEO,previous=0;int GetActiveWindow(){return active;}
 void PreviousWindow(){++previous;active=HOME;gfx.SetFullScreenVideo(false);}
 void SendThreadMessage(CGUIMessage&){}
};
struct Audio {int enables=0;void Enable(bool value){if(value)++enables;}};
struct CGUIComponent {Manager wm;Audio audio;Manager& GetWindowManager(){return wm;}Audio& GetAudioManager(){return audio;}};
struct Window {Gfx& GetGfxContext(){return gfx;}};
struct Settings {void Save(){}};
struct SettingsComponent {Settings settings;Settings* GetSettings(){return &settings;}};
struct Media {bool IsDiscInDrive(){return true;}};
struct Params {bool IsTestMode(){return false;}};
struct Messenger {void PostMsg(int){}};
struct CPlayList {int size()const{return 2;}int GetPlayable()const{return 2;}};
struct CApplication;
struct Components {CApplicationPlayer* player=nullptr;template<class T>T* GetComponent()const{return player;}};
struct CPlayListPlayer {
 bool m_bIsDeferredPlayPending=false;void StopMessage(int);
 int m_iCurrentPlayList=1,m_iCurrentSong=0;bool atEnd=false;CPlayList list;CApplication* app=nullptr;
 int GetNextItemIdx(int){return atEnd?2:1;}const CPlayList& GetPlaylist(int){return list;}
 void Reset(){}int GetCurrentPlaylist(){return m_iCurrentPlayList;}
 bool Play(int,const std::string&,bool);bool PlayNext(int offset=1,bool bAutoPlay=true);
};
struct CServiceBroker {
 static inline CGUIComponent gui;static inline Window window;static inline CPlayListPlayer playlist;
 static inline Components components;static inline SettingsComponent settings;static inline Media media;
 static inline Params params;static inline Messenger messenger;
 static CGUIComponent* GetGUI(){return &gui;}static Window* GetWinSystem(){return &window;}
 static CPlayListPlayer& GetPlaylistPlayer(){return playlist;}static const Components& GetAppComponents(){return components;}
 static SettingsComponent* GetSettingsComponent(){return &settings;}static Media& GetMediaManager(){return media;}
 static Params* GetAppParams(){return &params;}static Messenger* GetAppMessenger(){return &messenger;}
};
struct Services {CPlayerCoreFactory factory;CPlayerCoreFactory& GetPlayerCoreFactory(){return factory;}};
struct CApplication {
 CApplicationPlayer player;CApplicationStackHelper stack;CApplicationPowerHandling power;
 Services services;Services* m_ServiceManager=&services;bool m_bStop=false;
 std::shared_ptr<CFileItem> m_itemCurrentFile=std::make_shared<CFileItem>();
 IPlayerCallback callback;
 template<class T>T* GetComponent(){if constexpr(std::is_same_v<T,CApplicationPlayer>)return &player;
 else if constexpr(std::is_same_v<T,CApplicationStackHelper>)return &stack;else return &power;}
 void PlaybackCleanup();void StopPlaying(std::shared_ptr<CEvent> = {});bool PlaylistStopped();void ResetCurrentItem(){}
 void Frame(){auto appPlayer=&player;appPlayer->ContinueClose();@CONTINUATION@}
};
bool CPlayListPlayer::Play(int,const std::string& name,bool){
 // PlayFile deliberately sets fullscreen=false for a subsequent playlist item;
 // that policy is source-checked below, not overridden by this fixture.
 CFileItem item;item.id=9;return app->player.OpenFile(item,{},app->services.factory,name,app->callback);
}
void CPlayListPlayer::StopMessage(int requestedPlaylist){
 auto appPlayer=&app->player;auto& g_application=*app;
 struct {int param1;std::shared_ptr<CEvent> DeferCompletion(){return {};}} message{requestedPlaylist};auto* pMsg=&message;auto wakeScreensaver=[]{};
 @MEDIASTOP@
}
bool CApplication::PlaylistStopped(){@PLAYLISTSTOP@}
@PLAYNEXT@
@CLEANUP@
@STOP@
static void begin(CApplication& app,bool fullscreen=true){
 CServiceBroker::gui={};gfx={};gfx.fullscreen=fullscreen;
 CServiceBroker::gui.wm.active=fullscreen?WINDOW_FULLSCREEN_VIDEO:HOME;
 CServiceBroker::playlist={};CServiceBroker::playlist.app=&app;CServiceBroker::components.player=&app.player;
 app.player.m_pPlayer=std::make_shared<IPlayer>();app.player.m_pPlayer->m_name="old";
 app.player.m_waitForPlaybackStop=true;
}
static void ended(CApplication& app){
 app.player.OnPlaybackStopped();
 if(!CServiceBroker::playlist.PlayNext(1,true))app.player.ClosePlayer();
 app.PlaybackCleanup();
}
int main(){
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);
  assert(app.player.m_nextItem.pItem&&app.player.m_closingPlayer==old);
  assert(gfx.fullscreen&&CServiceBroker::gui.wm.previous==0&&app.stack.clears==0);
  for(int i=0;i<3;++i){app.Frame();app.PlaybackCleanup();assert(gfx.fullscreen&&app.player.created==0);}
  old->canClose=true;app.Frame();assert(app.player.created==1&&app.player.IsPlaying());
  assert(!app.player.m_pPlayer->hasVideo&&!app.player.m_pPlayer->lastFullscreen&&gfx.fullscreen);
  app.player.m_pPlayer->hasVideo=true;app.Frame();assert(gfx.fullscreen);
 }
 for(bool userLeaves:{false,true}){CApplication app;begin(app,userLeaves);auto old=app.player.m_pPlayer;
  ended(app);if(userLeaves){CServiceBroker::gui.wm.PreviousWindow();}
  old->canClose=true;app.Frame();assert(!gfx.fullscreen&&app.player.IsPlaying()&&!app.player.m_pPlayer->lastFullscreen);
 }
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;old->canClose=true;ended(app);
  assert(gfx.fullscreen);app.Frame();assert(gfx.fullscreen&&app.player.created==1);}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);app.StopPlaying();
  assert(!app.player.m_nextItem.pItem&&gfx.fullscreen);old->canClose=true;app.Frame();assert(app.player.created==0&&!gfx.fullscreen);}
 {CApplication app;begin(app);app.player.m_pPlayer->canClose=true;ended(app);
  assert(!app.player.HasPlayer()&&app.player.HasPendingOpen());app.StopPlaying();
  assert(!app.player.HasPendingOpen());app.Frame();assert(app.player.created==0&&!gfx.fullscreen);}
 for(int route:{0,1,2}){CApplication app;begin(app);app.player.m_pPlayer->canClose=true;ended(app);
  assert(!app.player.HasPlayer()&&app.player.HasPendingOpen());
  if(route==0)app.PlaylistStopped();else {CServiceBroker::playlist.m_bIsDeferredPlayPending=route==2;
   CServiceBroker::playlist.StopMessage(TYPE_NONE);app.PlaybackCleanup();}
  assert(!app.player.HasPendingOpen());app.Frame();assert(app.player.created==0&&!gfx.fullscreen);}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);app.player.ClosePlayer(true);
  app.PlaybackCleanup();old->canClose=true;app.Frame();assert(!gfx.fullscreen&&app.player.created==0);}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);app.player.failCreate=true;
  old->canClose=true;app.Frame();assert(!gfx.fullscreen&&app.stack.clears==1&&!app.player.HasPlayer());}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);app.player.failOpen=true;
  old->canClose=true;app.Frame();assert(gfx.fullscreen&&app.stack.clears==0&&app.player.m_closingPlayer);
  app.player.m_pPlayer->canClose=true;app.Frame();assert(!gfx.fullscreen&&app.stack.clears==1);}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);old->canClose=true;app.Frame();
  app.player.m_pPlayer->playing=false;app.player.OnPlaybackStopped();app.PlaybackCleanup();assert(gfx.fullscreen);
  app.player.m_pPlayer->canClose=true;app.Frame();assert(!gfx.fullscreen);}
 {CApplication app;begin(app);CServiceBroker::playlist.atEnd=true;ended(app);
  assert(gfx.fullscreen&&!app.player.m_nextItem.pItem&&app.player.PlaybackCleanupPending());
  auto old=app.player.m_pPlayer;
  const int calls=old->closes;
  for(int i=0;i<10;++i){app.Frame();assert(gfx.fullscreen&&gfx.restores==0);}
  assert(old->closes==calls+10);
  app.PlaylistStopped();assert(gfx.fullscreen&&gfx.restores==0);
  // Recording retirement clears the software HDR tag before GUI restoration.
  gfx.hdr=0;old->canClose=true;app.Frame();
  assert(!gfx.fullscreen&&gfx.restores==1&&gfx.restoreHdr==std::vector<int>{0});
  app.PlaylistStopped();app.PlaybackCleanup();app.Frame();assert(gfx.restores==1);
 }
 // A new accepted intent invalidates pending old cleanup, including before HasVideo.
 {CApplication app;begin(app);CServiceBroker::playlist.atEnd=true;ended(app);
  auto old=app.player.m_pPlayer;CFileItem latest;latest.id=42;
  assert(app.player.OpenFile(latest,{},app.services.factory,"old",app.callback));
  assert(!app.player.PlaybackCleanupPending());old->canClose=true;app.Frame();
  assert(app.player.IsPlaying()&&gfx.fullscreen&&gfx.restores==0);
 }
 // Even an immediate reentrant open during close rejects the old cleanup.
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;old->canClose=true;
  old->onClose=[&]{CFileItem latest;latest.id=43;
   assert(app.player.OpenFile(latest,{},app.services.factory,"old",app.callback));};
  app.player.OnPlaybackStopped();app.PlaybackCleanup();
  assert(app.player.HasPendingOpen()&&gfx.fullscreen&&gfx.restores==0);
  app.Frame();assert(app.player.IsPlaying()&&gfx.fullscreen&&gfx.restores==0);
 }
 // Application handles STOPPED before playlist state changes: desktop effects
 // run once, but the late notification must still leave visualization.
 for(bool immediate:{false,true}){CApplication app;begin(app);
  CServiceBroker::gui.wm.active=WINDOW_VISUALISATION;
  CServiceBroker::playlist.m_iCurrentPlayList=PLAYLIST::TYPE_MUSIC;
  auto old=app.player.m_pPlayer;old->hasAudio=true;old->canClose=immediate;
  app.player.OnPlaybackStopped();app.PlaybackCleanup();
  assert(CServiceBroker::gui.wm.active==WINDOW_VISUALISATION);
  CServiceBroker::playlist.m_iCurrentPlayList=PLAYLIST::TYPE_NONE;
  app.PlaylistStopped();old->canClose=true;app.Frame();
  assert(CServiceBroker::gui.wm.active==HOME&&app.stack.clears==1);
  auto restores=gfx.restores;app.PlaylistStopped();app.Frame();assert(gfx.restores==restores);
 }
 // Background EOF remains in the browser, restoring only after original retirement.
 {CApplication app;begin(app,false);CServiceBroker::playlist.atEnd=true;ended(app);
  assert(!gfx.fullscreen&&gfx.restores==0);app.player.m_pPlayer->canClose=true;app.Frame();
  assert(!gfx.fullscreen&&CServiceBroker::gui.wm.previous==0&&gfx.restores==1);
 }
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);
  CFileItem audio;audio.video=false;audio.id=10;app.player.OpenFile(audio,{},app.services.factory,"old",app.callback);
  old->canClose=true;app.Frame();assert(app.player.m_pPlayer->lastItem==10&&!gfx.fullscreen);}
 {CApplication app;begin(app);auto old=app.player.m_pPlayer;ended(app);
  CFileItem latest;latest.id=11;app.player.OpenFile(latest,{},app.services.factory,"old",app.callback);
  old->canClose=true;app.Frame();assert(app.player.m_pPlayer->lastItem==11&&gfx.fullscreen);}
 std::cout<<"PASS deferred autoplay fullscreen/background/navigation, immediate close, cancel/shutdown, failure, audio and latest intent\n";
}
"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--negative-control',action='store_true')
    args=parser.parse_args()
    source=(ROOT/'xbmc/application/ApplicationPlayer.cpp').read_text()
    signatures=['std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
                'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::CompleteCloseCompletions()', 'bool CApplicationPlayer::ClosePlayer(',
                'void CApplicationPlayer::OnPlaybackStopped()', 'void CApplicationPlayer::OpenNext(',
                'void CApplicationPlayer::ResetPlayer(', 'bool CApplicationPlayer::OpenFile(']
    signatures+=['bool CApplicationPlayer::HasPendingOpen() const',
                     'bool CApplicationPlayer::HasPendingVideoOpen() const',
                     'bool CApplicationPlayer::PreparePlaybackCleanup()',
                     'bool CApplicationPlayer::PlaybackCleanupCompleted() const']
    application=(ROOT/'xbmc/application/Application.cpp').read_text()
    frame=function(application,'void CApplication::FrameMove(')
    continuation=frame[frame.index('  const bool openingVideo ='):frame.index('  // this will go away')]
    cleanup=function(application,'void CApplication::PlaybackCleanup()')
    ended_body=application[application.index('  case GUI_MSG_PLAYBACK_ENDED:'):application.index('  case GUI_MSG_PLAYLISTPLAYER_STOPPED:')]
    assert ended_body.index('OnPlaybackStopped()')<ended_body.index('PlayNext(1, true)')<ended_body.index('PlaybackCleanup()')
    playfile=function(application,'bool CApplication::PlayFile(')
    assert 'options.fullscreen = !CServiceBroker::GetPlaylistPlayer().HasPlayedFirstFile()' in playfile
    code=PRELUDE+'\n'.join(function(source,s) for s in signatures)+HARNESS
    code=code.replace('@CONTINUATION@',continuation).replace('@CLEANUP@',cleanup)
    code=code.replace('@STOP@',function(application,'void CApplication::StopPlaying('))
    playlist=(ROOT/'xbmc/PlayListPlayer.cpp').read_text()
    stop=function(playlist,'  case TMSG_MEDIA_STOP:')
    code=code.replace('@MEDIASTOP@',stop[stop.index('{')+1:-1])
    stop=application[application.index('  case GUI_MSG_PLAYLISTPLAYER_STOPPED:'):application.index('  case GUI_MSG_PLAYBACK_AVSTARTED:')]
    code=code.replace('@PLAYLISTSTOP@',stop[stop.index(':')+1:])
    code=code.replace('@PLAYNEXT@',function(playlist,'bool CPlayListPlayer::PlayNext('))
    with tempfile.TemporaryDirectory(prefix='autoplay-fullscreen-') as tmp:
        out=Path(tmp)
        def run(text,failure=None):
            (out/'test.cpp').write_text(text)
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                            '-fsanitize=address,undefined','-fno-omit-frame-pointer',str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            result=subprocess.run([str(out/'test')],
                                  capture_output=bool(failure),text=True,timeout=15)
            if failure:
                assert result.returncode!=0 and failure in result.stderr,result
                print('Rejected negative control:', failure)
            else: result.check_returncode()
        run(code)
        if args.negative_control:
            needle='  if (appPlayer->HasPendingOpen())\n    return;'
            assert code.count(needle)==1
            run(code.replace(needle,'').replace('  if (HasPendingOpen())\n    return false;','')
                .replace('generation != m_openGeneration || HasPendingOpen() || HasPlayer()',
                         'generation != m_openGeneration || HasPlayer()'),
                "Assertion `gfx.fullscreen'")
            needle='  if (opening && !appPlayer->HasPendingOpen() && (!openingVideo || !appPlayer->IsPlaying()))'
            assert code.count(needle)==1
            run(code.replace(needle,needle.replace('if (opening', 'if (false && opening')),
                '!gfx.fullscreen&&app.stack.clears==1&&!app.player.HasPlayer()')
            needle='  if (generation != m_openGeneration || HasPendingOpen() || HasPlayer())\n    return false;'
            assert code.count(needle)==1
            run(code.replace(needle,''),'gfx.fullscreen')
            needle='  if (m_cleanupGeneration == m_openGeneration && !m_cleanupPending)\n    return false;'
            assert code.count(needle)==1
            run(code.replace(needle,''),'gfx.restores==1')
            needle='  m_cleanupPending = false;\n  const std::string newPlayer'
            assert code.count(needle)==1
            run(code.replace(needle,'  const std::string newPlayer'),'!app.player.PlaybackCleanupPending()')
            needle='  if (!m_cleanupPending)\n  {'
            assert code.count(needle)==1
            run(code.replace(needle,'  if (true)\n  {'),'old->closes==calls+10')
            needle='generation != m_openGeneration || HasPendingOpen() || HasPlayer()'
            assert code.count(needle)==1
            run(code.replace(needle,'HasPlayer()'),'app.player.HasPendingOpen()&&gfx.fullscreen&&gfx.restores==0')
            needle='  if (!cleanup && !appPlayer->PlaybackCleanupCompleted())'
            assert code.count(needle)==1
            run(code.replace(needle,'  if (!cleanup)'),
                'CServiceBroker::gui.wm.active==HOME&&app.stack.clears==1')
            needle='if (appPlayer->HasPlayer() || appPlayer->HasPendingOpen())'
            assert code.count(needle)==2
            run(code.replace(needle,'if (appPlayer->HasPlayer())'),'!app.player.HasPendingOpen()')


if __name__=='__main__':main()
