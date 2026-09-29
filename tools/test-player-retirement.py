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
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
PRELUDE = r'''
#include <cassert>
#include <algorithm>
#include <vector>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
struct CCriticalSection {
  bool held=false;
  void lock(){assert(!held);held=true;}
  void unlock(){assert(held);held=false;}
};
struct CFileItem {int id=0;bool disc=false;bool IsDiscImage()const{return disc;}bool IsDVDFile()const{return false;}};
struct CPlayerOptions {};
struct IPlayerCallback {};
struct IPlayer {
  std::string m_name="old",m_type="video";
  bool playing=false,canClose=false;int closes=0,opens=0,lastItem=-1;
  CCriticalSection* applicationLock=nullptr;int* destroyed=nullptr;
  ~IPlayer(){assert(!applicationLock||!applicationLock->held);if(destroyed)++*destroyed;}
  bool IsPlaying(){return playing;}
  bool CloseFile(bool=false){++closes;return canClose;}
  bool OpenFile(const CFileItem& file,const CPlayerOptions&){++opens;lastItem=file.id;playing=true;return true;}
};
struct CPlayerCoreFactory {
  std::string GetDefaultPlayer(const CFileItem&)const{return "new";}
};
struct Timer {int expires=0;void SetExpired(){++expires;}};
struct CApplicationPlayer {
  CCriticalSection m_playerLock;
  std::shared_ptr<IPlayer> m_pPlayer,m_closingPlayer;
  bool m_closeAcknowledged=false,m_waitForPlaybackStop=false,m_shutdown=false;
  struct {
    std::shared_ptr<CFileItem> pItem;CPlayerOptions options;
    std::string playerName;IPlayerCallback* callback=nullptr;
  }m_nextItem;
  Timer m_audioStreamUpdate,m_videoStreamUpdate,m_subtitleStreamUpdate;
  int created=0;
  std::shared_ptr<IPlayer> GetInternal();
  bool HasPlayer() {return bool(GetInternal());}
  bool ClosePlayer(bool=false);void ResetPlayer();void ContinueClose();void OnPlaybackStopped();
  void OpenNext(const CPlayerCoreFactory&);
  bool OpenFile(const CFileItem&,const CPlayerOptions&,const CPlayerCoreFactory&,
                const std::string&,IPlayerCallback&);
  void CreatePlayer(const CPlayerCoreFactory&,const std::string& name,IPlayerCallback&) {
    assert(!m_pPlayer);++created;m_pPlayer=std::make_shared<IPlayer>();
    m_pPlayer->m_name=name;m_pPlayer->applicationLock=&m_playerLock;
  }
};
'''
TESTS = r'''
int main(){
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
        'void CApplicationPlayer::ContinueClose()', 'bool CApplicationPlayer::ClosePlayer(',
        'void CApplicationPlayer::OnPlaybackStopped()', 'void CApplicationPlayer::OpenNext(',
        'void CApplicationPlayer::ResetPlayer()', 'bool CApplicationPlayer::OpenFile(']) + TESTS
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
    assert 'if (appPlayer->HasPlayer())' in function(application, 'void CApplication::StopPlaying()')
    playlist = (ROOT / 'xbmc/PlayListPlayer.cpp').read_text()
    media_stop = playlist[playlist.index('  case TMSG_MEDIA_STOP:'):playlist.index('  case TMSG_MEDIA_PAUSE:')]
    assert 'if (appPlayer->HasPlayer())' in media_stop
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
                        '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], capture_output=True, text=True, timeout=10)
        if expect_failure:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr


if __name__ == '__main__':
    main()
