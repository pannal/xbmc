#!/usr/bin/env python3
"""RetroPlayer open and close with production methods and recording stubs.

A newer item can retire the game player while the open of the game waits in a
dialog: the game client selection with the emulator install, the savestate
question, or a wake dialog of a network share during the initialization of the
game client, the open of the game, the load of the savestate or the request of
the achievements. The router closes the player each frame until the close
completes, and opens the newer item after the close completes. The open owns
the item until the open returns: a close during the open (also from another
thread) cancels the open and completes only after the open released the item.
The game clients (the add-on cache) and the agent input are shared, so the
newer game uses them after the retired open released them. The close of a player
reports the end only of a started game, and a close without an open item
changes nothing: the retired player reports no end that stops the newer item,
and the close in the destructor of the retired player keeps the render
callbacks of a newer game. A shutdown waits for the close of the player, so a
stop that waits ends the waits of the main thread like the user: a modal
dialog, a progress (the wake of a network share, the install of the emulator)
and a busy dialog that the user can cancel. The open returns, the close
completes, and the shutdown continues.
"""
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PRELUDE = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
using CCriticalSection = std::recursive_mutex;
constexpr int LOGINFO=0, LOGERROR=1, LOGDEBUG=2, TMSG_SWITCHTOFULLSCREEN=3, WINDOW_FULLSCREEN_GAME=4;
constexpr int ACTION_PLAYER_RESET=5, ACTION_SHOW_OSD=6;
struct CAction {int id=0;int GetID()const{return id;}};
struct CLog {template<class... A> static void Log(A&&...) {}};
struct CURL {static std::string GetRedacted(const std::string& s){return s;}};
struct StringUtils {template<class... A> static std::string Format(A&&...){return {};}};
struct Localize {std::string Get(int){return {};}} g_localizeStrings;
static std::function<bool()> savestateDialog;
// A dialog that processes messages during a step of the open, in the order of the open
enum {SELECTION,INITIALIZE,GAME_OPEN,SAVESTATE_QUESTION,ACHIEVEMENTS,PLAYBACK,STEPS};
static int waitStep=-1;static std::function<void()> waitHook;
static void Wait(int step){if(step==waitStep&&waitHook)std::exchange(waitHook,nullptr)();}
static int savestates=0,questions=0;
// The agent input of the games is shared, also between different game clients.
static int agentInputs=0;
struct CGUIDialogYesNo {template<class... A> static bool ShowAndGetInput(A&&...){
 ++questions;Wait(SAVESTATE_QUESTION);return savestateDialog?std::exchange(savestateDialog,nullptr)():true;}};
struct GameTag {std::string GetGameClient()const{return "game.test";}};
struct CFileItem {std::string GetPath()const{return "game.rom";} GameTag* GetGameInfoTag(){static GameTag g;return &g;}};
struct CPlayerOptions {};
static std::function<void()> startedHook;
struct Callback {int started=0,ended=0,paused=0,resumed=0;void OnPlayBackPaused(){++paused;}void OnPlayBackResumed(){++resumed;}void OnPlayBackStarted(const CFileItem&){++started;if(startedHook)std::exchange(startedHook,nullptr)();}
 void OnAVStarted(const CFileItem&){} void OnPlayBackEnded(){++ended;}};
struct GameSettings {bool AutosaveEnabled(){return false;} std::string GetRAUsername(){return {};}
 std::string GetRAToken(){return {};}};
struct GameServices {::GameSettings& GameSettings(){static ::GameSettings s;return s;}};
struct Dummy {template<class... A> explicit Dummy(A&&...){} Dummy()=default;void ResetInfo(){}
 void SetDataCache(void*){} void SetVideoFps(float){} void StartAgentManager(){++agentInputs;} void StopAgentManager(){--agentInputs;}
 std::string CreateSavestate(bool){++savestates;return {};} void EnableRichPresence(){Wait(ACHIEVEMENTS);}
 void ResetRuntime(){} void EnableAudio(bool){} void Initialize(){} void Deinitialize(){}
 // the playback of a game; the playback without a game (CRealtimePlayback) can not pause or seek
 bool realtime=false;double speed=1;bool CanPause()const{return !realtime;}bool CanSeek()const{return !realtime;}
 uint64_t GetTimeMs()const{return 0;}uint64_t GetTotalTimeMs()const{return 0;}uint64_t GetCacheTimeMs()const{return 0;}
 double GetSpeed()const{return speed;}void SetSpeed(double s){speed=s;}void PauseAsync(){speed=0;} };
struct CRealtimePlayback: Dummy {CRealtimePlayback(){realtime=true;}};
struct CRPProcessInfo: Dummy {static std::unique_ptr<CRPProcessInfo> CreateInstance(){return std::make_unique<CRPProcessInfo>();}};
using CGUIGameMessenger=Dummy;using CRPRenderManager=Dummy;using CRPStreamManager=Dummy;
using CRetroPlayerInput=Dummy;using CCheevos=Dummy;using CGUIPlaybackControl=Dummy;using CRetroPlayerAutoSave=Dummy;
static int gameOpens=0,gameCloses=0,gameSaves=0;static bool initializeFails=false,gameOpenFails=false;
struct CGameClient {bool playing=false,loaded=false;bool Initialize(){Wait(INITIALIZE);loaded=true;return !initializeFails;}
 template<class... A> bool OpenFile(A&&...){Wait(GAME_OPEN);if(gameOpenFails)return false;++gameOpens;playing=true;return true;}
 template<class... A> bool OpenStandalone(A&&...){++gameOpens;playing=true;return true;}
 std::string ID(){return "game.test";}std::string Name(){return "test";}float GetFrameRate(){return 60;}
 // like the production close, a close of a game that is not playing does nothing
 void CloseFile(bool saveGame){if(playing){++gameCloses;gameSaves+=saveGame;}playing=false;}void Unload(){loaded=false;playing=false;}
};
// The add-on manager returns the one cached game client of an add-on to every player.
static std::shared_ptr<CGameClient>& CachedClient(){static auto client=std::make_shared<CGameClient>();return client;}
namespace ADDON {using AddonPtr=std::shared_ptr<CGameClient>;enum class AddonType{GAMEDLL};enum class OnlyEnabled{CHOICE_YES};}
using ADDON::OnlyEnabled;
struct AddonMgr {bool GetAddon(const std::string&, ADDON::AddonPtr& p, ADDON::AddonType, OnlyEnabled){p=CachedClient();return true;}
 bool GetAddon(const std::string&, ADDON::AddonPtr& p, OnlyEnabled){p=std::make_shared<CGameClient>();return true;}};
struct Messenger {int fullscreen=0;void PostMsg(int){++fullscreen;}};
struct Windows {Windows& GetWindowManager(){return *this;}int GetActiveWindowOrDialog(){return WINDOW_FULLSCREEN_GAME;}};
struct CServiceBroker {static AddonMgr& GetAddonMgr(){static AddonMgr a;return a;}
 static int& GetDataCacheCore(){static int cache=0;return cache;} static void* GetPeripherals(){return nullptr;}
 static Messenger* GetAppMessenger(){static Messenger m;return &m;} static Windows* GetGUI(){static Windows w;return &w;}};
// A savestate of another game client asks before the open continues.
static bool otherClientSave=false;
struct ISavestate {std::string GameClientID(){return otherClientSave?"game.other":"game.test";}};
struct CSavestateDatabase {static std::unique_ptr<ISavestate> AllocateSavestate(){return std::make_unique<ISavestate>();}
 bool GetSavestate(const std::string&, ISavestate&){return otherClientSave;}};
static std::function<bool()> clientDialog;
namespace GAME {struct CGameUtils {static bool FillInGameClient(CFileItem&,std::string&){
 Wait(SELECTION);return clientDialog?std::exchange(clientDialog,nullptr)():true;}};}
struct CRetroPlayer {
 Callback& m_callback;GameServices m_gameServices;CCriticalSection m_mutex;@MEMBERS@
 std::unique_ptr<CRPProcessInfo> m_processInfo;std::unique_ptr<CGUIGameMessenger> m_guiMessenger;
 std::unique_ptr<CRPRenderManager> m_renderManager;std::unique_ptr<CRPStreamManager> m_streamManager;
 std::unique_ptr<CRetroPlayerInput> m_input;std::unique_ptr<Dummy> m_playback;
 std::shared_ptr<CGameClient> m_gameClient;std::shared_ptr<CCheevos> m_cheevos;
 std::unique_ptr<CGUIPlaybackControl> m_playbackControl;std::unique_ptr<CRetroPlayerAutoSave> m_autoSave;
 explicit CRetroPlayer(Callback& c):m_callback(c){ResetPlayback();}
 ~CRetroPlayer(){CloseFile();}
 bool OpenFile(const CFileItem&,const CPlayerOptions&);bool OpenItem(const CFileItem&,const CPlayerOptions&);
 bool OpenCancelled();bool CloseFile(bool=false);void CloseItem();
 bool IsPlaying()const{return m_gameClient&&m_gameClient->playing;}
 bool CanPause()const;bool CanSeek()const;void Pause();uint64_t GetTime();uint64_t GetTotalTime();float GetCachePercentage()const;
 void SetSpeed(float);void SetSpeedInternal(double);void OnSpeedChange(double);void ResetPlayback();bool OnAction(const CAction&);
 void OpenOSD(){} void CloseOSD(){}
 void PrintGameInfo(const CFileItem&){} void RegisterWindowCallbacks(){}void UnregisterWindowCallbacks(){}
 void CreatePlayback(const std::string&){Wait(PLAYBACK);assert(m_gameClient);m_playback=std::make_unique<Dummy>();}
};
'''

# The production playback of a player without a game, which the constructor and
# the close create.
PRELUDE += function((ROOT / 'xbmc/cores/RetroPlayer/RetroPlayer.cpp').read_text(), 'void CRetroPlayer::ResetPlayback()')

MAIN = r'''
// The controls and the queries of a player without a started game (a failed,
// cancelled or closed open, an open in progress) change nothing and report no
// pause or resume. A player without a game keeps a playback without a game.
static void controls(CRetroPlayer& player,const Callback& cb){
 const int paused=cb.paused,resumed=cb.resumed;
 assert(player.m_playback&&player.m_playback->realtime);
 assert(!player.CanPause()&&!player.CanSeek()&&player.GetTime()==0&&player.GetTotalTime()==0&&player.GetCachePercentage()==0);
 player.Pause();player.SetSpeed(2);player.SetSpeed(0);player.SetSpeed(1);
 assert(player.OnAction(CAction{ACTION_PLAYER_RESET}));
 assert(cb.paused==paused&&cb.resumed==resumed&&player.m_playback->GetSpeed()==1);
}
static void reset(){gameOpens=gameCloses=gameSaves=savestates=questions=agentInputs=0;otherClientSave=initializeFails=gameOpenFails=false;waitStep=-1;waitHook=nullptr;
 startedHook=nullptr;CachedClient()=std::make_shared<CGameClient>();CServiceBroker::GetAppMessenger()->fullscreen=0;}
int main(){
 // A newer item retires the player during each step of the open that can wait
 // in a dialog, and during the start of the game (STEPS). The close comes from
 // the dialog on the thread of the open, or from another thread. The close is
 // incomplete until the open returns; the open then releases the item: the
 // game client closes, and a game that never started reports no end.
 for(int thread=0;thread<2;++thread)
 for(int step=0;step<=STEPS;++step){reset();Callback cb;const bool started=step==STEPS;
  {CRetroPlayer player(cb);int closes=0;
   const auto close=[&]{bool done=true;
    if(thread){std::thread other([&]{done=player.CloseFile();});other.join();}else done=player.CloseFile();
    assert(!done);++closes;};
   // A savestate of another game client asks the question before the open continues.
   otherClientSave=true;
   if(started)startedHook=[&]{close();};else{waitStep=step;waitHook=[&]{controls(player,cb);close();close();controls(player,cb);};}
   assert(!player.OpenFile(CFileItem{},CPlayerOptions{}));
   assert(closes==(started?1:2)&&player.CloseFile()&&agentInputs==0);
   assert(cb.started==started&&cb.ended==started&&!player.IsPlaying()&&savestates==0);
   assert(!player.m_gameClient&&!player.m_processInfo&&!player.m_input);
   controls(player,cb);
   assert(gameCloses==(step==SELECTION?0:1)&&CServiceBroker::GetAppMessenger()->fullscreen==started);
   // only a started game keeps the progress of the game (the in-game saves)
   assert(gameSaves==started);
   // A cancelled open asks no question
   assert(questions==(step>=SAVESTATE_QUESTION));
  }
  // the destructor reports no second end
  assert(cb.ended==started);
 }
 // An open without a close plays; the close reports the end once.
 {reset();Callback cb;
  {CRetroPlayer player(cb);
   assert(player.OpenFile(CFileItem{},CPlayerOptions{})&&cb.started==1&&player.IsPlaying());
   assert(player.CloseFile()&&cb.ended==1&&!player.IsPlaying()&&gameCloses==1&&gameSaves==1&&!player.m_gameClient);
   controls(player,cb);
   assert(player.CloseFile()&&cb.ended==1);
  }
  assert(cb.ended==1);
 }
 // A cancelled game client selection, a declined savestate question, a failed
 // open of the game and a failed initialization fail the open; the game never
 // started, and the close reports no end. The failed open releases the item
 // like a close: the shared agent input stops, an opened game closes, and the
 // cached game client unloads.
 for(int failure=0;failure<4;++failure){reset();Callback cb;
  {CRetroPlayer player(cb);
   if(failure==0)clientDialog=[]{return false;};
   else if(failure==1){otherClientSave=true;savestateDialog=[]{return false;};}
   else if(failure==2)gameOpenFails=true;
   else initializeFails=true;
   assert(!player.OpenFile(CFileItem{},CPlayerOptions{})&&cb.started==0&&cb.ended==0);
   assert(agentInputs==0&&gameCloses==gameOpens&&gameSaves==0&&!CachedClient()->playing&&!CachedClient()->loaded);
   assert(!player.m_gameClient&&!player.m_input&&!player.m_processInfo);
   controls(player,cb);
   assert(player.CloseFile());
   controls(player,cb);
  }
  assert(cb.ended==0&&agentInputs==0&&gameCloses==gameOpens);
 }
 // A player that never opened reports no end.
 {reset();Callback cb;{CRetroPlayer player(cb);controls(player,cb);}assert(cb.ended==0&&cb.paused==0&&cb.resumed==0);}
}
'''

# The render manager of the games is shared, and the unregistration has no
# owner. The router opens the newer game after the close of the retired player
# completed: the newer game keeps the game client, the render callbacks and the
# render targets after the retired player ends.
RENDER_MANAGER = r'''
struct Dummy; struct CRetroPlayer;
using CGUIRenderTargetFactory=Dummy; using IRenderCallback=Dummy; using IGameCallback=CRetroPlayer;
struct CGUIRenderHandle {}; struct CGUIRenderTarget {};
struct CGUIGameRenderManager {
 CCriticalSection m_targetMutex,m_callbackMutex,m_gameCallbackMutex;
 CGUIRenderTargetFactory* m_factory=nullptr; IRenderCallback* m_callback=nullptr;
 IGameCallback* m_gameCallback=nullptr;
 std::map<CGUIRenderHandle*,std::shared_ptr<CGUIRenderTarget>> m_renderTargets;
 void RegisterPlayer(CGUIRenderTargetFactory*,IRenderCallback*,IGameCallback*);
 void UnregisterPlayer(); void UpdateRenderTargets(); bool IsPlayingGame();
 CGUIRenderTarget* CreateRenderTarget(CGUIRenderHandle*){return new CGUIRenderTarget;}
};
static CGUIGameRenderManager sharedRenderer;
'''

RENDER_MAIN = r'''
int main(){
 for(int step=0;step<STEPS;++step){
  Callback oldCb,newCb;savestates=agentInputs=0;CachedClient()=std::make_shared<CGameClient>();
  auto old=std::make_unique<CRetroPlayer>(oldCb);
  auto newer=std::make_unique<CRetroPlayer>(newCb);
  CGUIRenderHandle handle;
  sharedRenderer.m_renderTargets.emplace(&handle,nullptr);
  otherClientSave=step==SAVESTATE_QUESTION;waitStep=step;waitHook=[&]{assert(!old->CloseFile());};
  assert(!old->OpenFile(CFileItem{},CPlayerOptions{}));
  assert(old->CloseFile()&&agentInputs==0);
  otherClientSave=false;
  assert(newer->OpenFile(CFileItem{},CPlayerOptions{}));
  assert(sharedRenderer.m_gameCallback==newer.get()&&sharedRenderer.m_renderTargets.at(&handle));
  old.reset();
  assert(oldCb.started==0&&oldCb.ended==0&&savestates==0);
  assert(newer->IsPlaying()&&CachedClient()->loaded&&agentInputs==1&&newCb.started==1&&newCb.ended==0);
  assert(sharedRenderer.IsPlayingGame()&&sharedRenderer.m_gameCallback==newer.get());
  assert(sharedRenderer.m_renderTargets.at(&handle));
  newer.reset();
  assert(newCb.ended==1&&savestates==1&&!sharedRenderer.IsPlayingGame());
  sharedRenderer.m_renderTargets.clear();
 }
 std::cout<<"RetroPlayer render callbacks: PASS (production registration; ASan/UBSan)\n";
}
'''


# The production application stop, the frame continuation of a waiting stop,
# the close of the router, the modal loop of the window manager, and the waits
# of the progress (WakeOnAccess) and the busy dialog.
SHUTDOWN = r'''
using IPlayer=CRetroPlayer;
using namespace std::chrono_literals;
static int64_t clockMs=0;
namespace KODI {namespace TIME {void Sleep(std::chrono::milliseconds delay){clockMs+=delay.count();}}}
namespace XbmcThreads {template<class=void> struct EndTime {
 int64_t until;explicit EndTime(std::chrono::milliseconds delay):until(clockMs+delay.count()){}
 bool IsTimePast()const{return clockMs>=until;}
 std::chrono::milliseconds GetTimeLeft()const{return std::chrono::milliseconds(until-clockMs);}
};}
// An event that the wait never receives
struct CEvent {void Set(){} bool Wait(std::chrono::milliseconds delay){clockMs+=delay.count();assert(clockMs<60000);return false;}};
struct CSingleExit {explicit CSingleExit(CCriticalSection&) {}};
struct CApplicationPlayer {
 mutable CCriticalSection m_playerLock;
 std::shared_ptr<IPlayer> m_pPlayer,m_closingPlayer;
 struct {std::shared_ptr<CFileItem> pItem;} m_nextItem;
 bool m_shutdown=false,m_closeAcknowledged=false,m_waitForPlaybackStop=false;std::map<std::thread::id,int> m_controls;
 std::vector<std::shared_ptr<CEvent>> m_closeCompletionEvents,m_retiredCloseCompletionEvents;
 enum class Supersession {NONE,OPEN,STOP}; Supersession m_supersededBy=Supersession::NONE;
 uint64_t m_supersededAt=0,m_intentSequence=0;
 std::shared_ptr<IPlayer> GetInternal(); std::shared_ptr<const IPlayer> GetInternal()const;
 bool ClosePlayer(bool=false,std::shared_ptr<CEvent> = {});
 void ResetPlayer(std::shared_ptr<CEvent> = {}); void ContinueClose();void CompleteCloseCompletions();
 void Supersede(Supersession);bool HasPlayer()const;
};
@ROUTER@
constexpr int EXITCODE_QUIT=0,EXITCODE_POWERDOWN=1,EXITCODE_REBOOT=2;
static int powerActions=0;
struct PowerManager {void Powerdown(){++powerActions;} void Reboot(){++powerActions;}};
struct CApplication {
 CApplicationPlayer player;bool m_bStop=false;std::optional<int> m_pendingStop;
 template<class T>T* GetComponent(){return &player;}
 bool IsStopPending() const;
 bool Stop(int exitCode){@STOP@m_bStop=true;return true;}
 void FrameMove(){player.ContinueClose();@FRAME@}
} g_application;
@PENDING@
// A Powerdown from a remote arrives in the first frame of the wait.
static int frames=0;
struct CallbackLoop {bool GetRenderGUI(){return true;}
 void Process(){if(++frames==1)assert(!g_application.Stop(EXITCODE_POWERDOWN)&&g_application.m_pendingStop&&!g_application.m_bStop);}
 void FrameMove(bool){g_application.FrameMove();}void Render(){}};
struct CGUIWindowManager {CallbackLoop* m_pCallback;int m_iNested=0;bool ProcessRenderLoop(bool);};
@LOOP@
static CGUIWindowManager* manager=nullptr;
struct CVariant {explicit CVariant(const std::string&) {}};
struct CGUIDialog {bool m_active=true;bool IsActive()const{return m_active;}void Open(){m_active=true;}
 void Close(bool=false){m_active=false;}void ProcessRenderLoop(bool renderOnly=false);};
@DIALOG@
struct CGUIDialogProgress:CGUIDialog {
 static constexpr int CHOICE_NONE=-2,CHOICE_CANCELED=-1;int m_iChoice=CHOICE_NONE;
 bool IsCanceled()const{return m_iChoice==CHOICE_CANCELED;}
 void SetLine(int,const CVariant&){} void SetPercentage(int){} void Progress();
};
@PROGRESS@
struct WaitCondition {bool SuccessWaiting()const{return false;}};
struct ProgressDialogHelper {
 enum wait_result {TimedOut,Canceled,Success};
 CGUIDialogProgress* m_dialog;
 wait_result ShowAndWait(const WaitCondition&,unsigned,const std::string&);
};
@SHOW@
struct CGUIDialogBusy:CGUIDialog {bool m_cancelled=false;uint32_t m_waiters=0;
 static bool WaitOnEvent(CEvent&,unsigned int=100,bool=true);};
static CGUIDialogBusy busyDialog;
@BUSY@
enum {MODAL,PROGRESS,BUSY,WAITS};
int main(){
 // A shutdown during each step of the open that waits in a dialog
 for(int wait=0;wait<WAITS;++wait)
 for(int step=0;step<STEPS;++step){
  g_application.m_bStop=false;g_application.m_pendingStop.reset();powerActions=0;frames=0;clockMs=0;
  Callback cb;auto game=std::make_shared<CRetroPlayer>(cb);g_application.player.m_pPlayer=game;
  g_application.player.m_shutdown=false;
  CallbackLoop loop;CGUIWindowManager windows{&loop};manager=&windows;
  otherClientSave=true;waitStep=step;
  waitHook=[&]{
   if(wait==MODAL){
    // the loop of CGUIDialog::Open_Internal
    while(windows.ProcessRenderLoop(false))assert(frames<100);
   }else if(wait==PROGRESS){
    // the wait for the network of WakeOnAccess (40 s)
    CGUIDialogProgress progress;ProgressDialogHelper wake{&progress};
    assert(wake.ShowAndWait(WaitCondition{},40,"wake")==ProgressDialogHelper::Canceled);
   }else{
    busyDialog=CGUIDialogBusy{};CEvent never;
    assert(!CGUIDialogBusy::WaitOnEvent(never,100,true)&&busyDialog.m_waiters==0);
   }
   // the wait ends in the frame of the stop (the busy dialog repaints once after the wait)
   assert(frames==(wait==BUSY?2:1)&&clockMs<=200&&!g_application.m_bStop);
  };
  assert(!game->OpenFile(CFileItem{},CPlayerOptions{}));
  assert(frames==(wait==BUSY?2:1)&&cb.started==0&&cb.ended==0);
  game.reset();
  g_application.FrameMove();
  assert(g_application.m_bStop&&!g_application.m_pendingStop&&!g_application.player.HasPlayer()&&powerActions==1);
 }
 std::cout<<"RetroPlayer shutdown: PASS (production stop, router close, modal loop, progress and busy waits; ASan/UBSan)\n";
}
'''


# The close of the game client releases the game; only a close that keeps the
# progress saves the in-game saves (SRAM, RTC), whose path has no game client.
GAME_CLIENT = r'''
#include <cassert>
#include <memory>
#include <mutex>
#include <string>
struct CCriticalSection : std::recursive_mutex {};
static int sramSaves=0,unloads=0,inputStops=0,streamResets=0;
struct CGameClientInGameSaves {void Save(){++sramSaves;}};
struct Input {void Stop(){++inputStops;}};
struct Streams {void Deinitialize(){++streamResets;}};
struct ToAddon {int UnloadGame(void*){++unloads;return 0;}};
struct Game {ToAddon* toAddon;};
struct Ifc {Game* game;};
struct CGameClient {
 CCriticalSection m_critSection;bool m_bIsPlaying=true;std::unique_ptr<CGameClientInGameSaves> m_inGameSaves;
 std::string m_gamePath="rom.sfc";size_t m_serializeSize=1;void* m_input=this;Ifc m_ifc;
 ::Input input;::Streams streams;::Input& Input(){return input;} ::Streams& Streams(){return streams;}
 void LogError(int,const char*){} void LogException(const char*){}
 void CloseFile(bool saveGame);
};
@CLOSE@
int main(){
 ToAddon addon;Game game{&addon};
 for(int save=0;save<2;++save){
  sramSaves=unloads=inputStops=streamResets=0;
  CGameClient client;client.m_ifc.game=&game;client.m_inGameSaves=std::make_unique<CGameClientInGameSaves>();
  client.CloseFile(save);
  // the close always releases the game
  assert(!client.m_bIsPlaying&&!client.m_inGameSaves&&client.m_gamePath.empty()&&unloads==1&&inputStops==1&&streamResets==1);
  assert(sramSaves==save);
  client.CloseFile(true);  // a second close does nothing
  assert(unloads==1&&sramSaves==save);
 }
}
'''


def compile_run(code):
    with tempfile.TemporaryDirectory(prefix='retroplayer-open-close-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(code)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        '-fno-omit-frame-pointer', '-pthread', str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=15)


def main():
    source = (ROOT / 'xbmc/cores/RetroPlayer/RetroPlayer.cpp').read_text()
    header = (ROOT / 'xbmc/cores/RetroPlayer/RetroPlayer.h').read_text()
    # The production state members of the open and the close.
    members = header[header.index('  // Synchronization parameters'):header.index('};\n} // namespace RETRO')]
    members = members.replace('  CCriticalSection m_mutex;\n', '')
    prelude = PRELUDE.replace('@MEMBERS@', members)
    player = ''.join(function(source, signature) for signature in (
        'bool CRetroPlayer::OpenFile(', 'bool CRetroPlayer::OpenItem(', 'bool CRetroPlayer::OpenCancelled()',
        'bool CRetroPlayer::CloseFile(', 'void CRetroPlayer::CloseItem()'))
    # The production controls and queries; the reset action of a started game
    # resets the game client.
    controls = prelude.replace('struct CGameClient {', 'struct CGameClient {struct Controls {void HardwareReset(){}} input;'
                               'Controls& Input(){return input;}', 1)
    controls += player + ''.join(function(source, signature) for signature in (
        'bool CRetroPlayer::CanPause() const', 'void CRetroPlayer::Pause()', 'bool CRetroPlayer::CanSeek() const',
        'float CRetroPlayer::GetCachePercentage() const', 'uint64_t CRetroPlayer::GetTime()',
        'uint64_t CRetroPlayer::GetTotalTime()', 'void CRetroPlayer::SetSpeed(', 'bool CRetroPlayer::OnAction(',
        'void CRetroPlayer::SetSpeedInternal(', 'void CRetroPlayer::OnSpeedChange('))
    compile_run(controls + MAIN)
    # The production registration of the window callbacks and the render manager.
    manager = (ROOT / 'xbmc/cores/RetroPlayer/guibridge/GUIGameRenderManager.cpp').read_text()
    render = prelude.replace('#include <atomic>', '#include <atomic>\n#include <map>', 1)
    render = render.replace('struct GameServices {', RENDER_MANAGER +
                            'struct GameServices {CGUIGameRenderManager& GameRenderManager(){return sharedRenderer;}', 1)
    render = render.replace('struct Dummy {', 'struct Dummy {Dummy* GetGUIRenderTargetFactory(){return this;}', 1)
    render = render.replace('void RegisterWindowCallbacks(){}void UnregisterWindowCallbacks(){}',
                            'void RegisterWindowCallbacks();void UnregisterWindowCallbacks();', 1)
    # The close saves the game.
    render = render.replace('bool AutosaveEnabled(){return false;}', 'bool AutosaveEnabled(){return true;}', 1)
    for signature in ('void CGUIGameRenderManager::RegisterPlayer(', 'void CGUIGameRenderManager::UnregisterPlayer()',
                      'void CGUIGameRenderManager::UpdateRenderTargets()', 'bool CGUIGameRenderManager::IsPlayingGame()'):
        render += function(manager, signature)
    for signature in ('void CRetroPlayer::RegisterWindowCallbacks()', 'void CRetroPlayer::UnregisterWindowCallbacks()'):
        render += function(source, signature)
    compile_run(render + player + RENDER_MAIN)
    # The shutdown during a dialog of the open.
    app = (ROOT / 'xbmc/application/Application.cpp').read_text()
    router = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    windows = (ROOT / 'xbmc/guilib/GUIWindowManager.cpp').read_text()
    stop = function(app, 'bool CApplication::Stop(')
    frame = function(app, 'void CApplication::FrameMove(')
    frame = frame[frame.index('  if (m_pendingStop && !appPlayer->HasPlayer())'):frame.rindex('}')]
    pending = function(app, 'bool CApplication::IsStopPending() const')
    shutdown = SHUTDOWN.replace('@ROUTER@', ''.join(function(router, signature) for signature in (
        'std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
        'std::shared_ptr<const IPlayer> CApplicationPlayer::GetInternal() const',
        'bool CApplicationPlayer::ClosePlayer(', 'void CApplicationPlayer::ResetPlayer(',
        'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::CompleteCloseCompletions()',
        'void CApplicationPlayer::Supersede(', 'bool CApplicationPlayer::HasPlayer() const')))
    shutdown = shutdown.replace('@STOP@', stop[stop.index('  if (m_pendingStop)'):stop.index('  CLog::Log')] +
                                stop[stop.index('  const auto appPlayer = GetComponent<CApplicationPlayer>();'):
                                     stop.index('  // Safety net')])
    shutdown = shutdown.replace('@FRAME@', frame.replace('appPlayer->', 'player.').replace(
        'CServiceBroker::GetPowerManager()', 'PowerManager{}'))
    shutdown = shutdown.replace('@PENDING@', pending).replace('@LOOP@', function(windows, 'bool CGUIWindowManager::ProcessRenderLoop('))
    dialog = function((ROOT / 'xbmc/guilib/GUIDialog.cpp').read_text(), 'void CGUIDialog::ProcessRenderLoop(')
    shutdown = shutdown.replace('@DIALOG@', dialog.replace('CServiceBroker::GetGUI()->GetWindowManager()', '(*manager)'))
    shutdown = shutdown.replace('@PROGRESS@', function((ROOT / 'xbmc/dialogs/GUIDialogProgress.cpp').read_text(),
                                                       'void CGUIDialogProgress::Progress()'))
    show = function((ROOT / 'xbmc/network/WakeOnAccess.cpp').read_text(), '  wait_result ShowAndWait (')
    shutdown = shutdown.replace('@SHOW@', show.replace(
        '  wait_result ShowAndWait (', 'ProgressDialogHelper::wait_result ProgressDialogHelper::ShowAndWait (', 1))
    busy = function((ROOT / 'xbmc/dialogs/GUIDialogBusy.cpp').read_text(), 'bool CGUIDialogBusy::WaitOnEvent(')
    shutdown = shutdown.replace('@BUSY@', busy.replace('static_cast<CGUIDialogBusy*>(\n        CServiceBroker::GetGUI()->GetWindowManager().GetWindow(WINDOW_DIALOG_BUSY))', '&busyDialog'))
    stopping = prelude.replace('#include <atomic>', '#include <algorithm>\n#include <atomic>\n#include <chrono>\n#include <optional>\n#include <vector>', 1)
    stopping = stopping.replace('struct CServiceBroker {', 'struct Window {CCriticalSection gfx;auto& GetGfxContext(){return gfx;} '
                                'bool PrepareForShutdown(){return true;}};\nstruct CServiceBroker {static Window* GetWinSystem()'
                                '{static Window w;return &w;}', 1)
    stopping = stopping.replace('struct Messenger {int fullscreen=0;', 'struct Messenger {bool IsProcessThread(){return true;} int fullscreen=0;', 1)
    compile_run(stopping + player + shutdown)
    client = function((ROOT / 'xbmc/games/addons/GameClient.cpp').read_text(), 'void CGameClient::CloseFile(')
    compile_run(GAME_CLIENT.replace('@CLOSE@', client))
    print('RetroPlayer open/close: PASS (production OpenFile, CloseFile and render registration; ASan/UBSan)')


if __name__ == '__main__':
    sys.exit(main())
