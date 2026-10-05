#!/usr/bin/env python3
"""Production synchronous stop dispatch and close ownership with recording boundaries.

Runs SendMsg/ProcessMessages, the media-stop handler, Application stop/cleanup and
ApplicationPlayer retirement methods. Original successful/failed CloseFile bodies
are extracted from f69d242e7c's parent. Replay unchanged PM4K final credits/monitor
methods using observed native Stop return state. GUI, renderer and worker completion
are controlled boundaries, not a device or media-decoder test.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'f69d242e7c^'
PM4K = ROOT.parent / 'script.plexmod'
fixture = runpy.run_path(str(ROOT / 'tools/test-autoplay-fullscreen.py'))
function = fixture['function']

EVENT = r'''
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <future>
#include <queue>
#include <thread>
#include <functional>
#include <iostream>
using namespace std::chrono_literals;
class CEvent {
 std::mutex mutex;std::condition_variable cv;bool signaled=false;
public:
 explicit CEvent(bool=true){}
 void Set(){std::lock_guard<std::mutex> lock(mutex);signaled=true;cv.notify_all();}
 bool Wait(){std::unique_lock<std::mutex> lock(mutex);cv.wait(lock,[&]{return signaled;});return true;}
 bool Wait(std::chrono::milliseconds timeout){std::unique_lock<std::mutex> lock(mutex);return cv.wait_for(lock,timeout,[&]{return signaled;});}
};
struct CThread {static auto GetCurrentThreadId(){return std::this_thread::get_id();}};
struct CSingleExit {template<class T> explicit CSingleExit(T&) {}};
struct CacheCore {void Reset(){}};
struct CLog {template<class...T> static void Log(T&&...) {}};
constexpr int LOGINFO=0;
'''

MESSENGER = r'''
constexpr int TMSG_GUI_MESSAGE=100;
using CWinSystemBase=Window;
@THREADMESSAGE@
namespace KODI::MESSAGING {
class CApplicationMessenger {
public:
 CCriticalSection m_critSection;
 std::queue<ThreadMessage*> m_vecMessages,m_vecWindowMessages;
 std::thread::id m_guiThreadId=std::this_thread::get_id();bool m_bStop=false;
 std::function<void(ThreadMessage*)> target;
 int SendMsg(ThreadMessage&&,bool);
 void ProcessMessages();void ProcessWindowMessages();void Cleanup();
 void ProcessMessage(ThreadMessage* msg){target(msg);}
 bool Queued(){std::unique_lock<CCriticalSection> lock(m_critSection);return !m_vecMessages.empty() || !m_vecWindowMessages.empty();}
};
@METHODS@
}
using KODI::MESSAGING::CApplicationMessenger;
using KODI::MESSAGING::ThreadMessage;
'''

LEGACY = r'''
struct LegacyVideo {
 struct Stream {void Abort(){}};
 Stream* m_pDemuxer=nullptr;Stream* m_pSubtitleDemuxer=nullptr;Stream* m_pInputStream=nullptr;
 struct Renderer {bool ready=true;int uninit=0,pumps=0;bool UnInit(){++uninit;return ready;}void ProcessLifecycleRequests(){++pumps;}}m_renderManager;
 struct Edl {void Clear(){}}m_Edl;
 struct Info {void SetDataCache(CacheCore*){}}info;Info* m_processInfo=&info;
 bool m_bStop=false,m_bAbortRequest=false,m_bCloseRequest=false,m_HasVideo=true,m_HasAudio=true,running=true,terminalQueued=false;
 bool CloseFile(bool=false);bool IsRunning(){return running;}
 void StopThread(bool wait){m_bStop=true;if(wait)running=false;}
 void Join(std::chrono::milliseconds){running=false;terminalQueued=true;}
};
struct LegacyManager {
 CCriticalSection m_playerLock;std::shared_ptr<LegacyVideo> m_pPlayer;
 struct {std::shared_ptr<CFileItem> pItem;}m_nextItem;
 std::shared_ptr<LegacyVideo> GetInternal();void ClosePlayer();void ResetPlayer();bool CloseFile(bool=false);
};
@METHODS@
'''

TESTS = r'''
static void wait_queued(CApplicationMessenger& messenger){
 auto deadline=std::chrono::steady_clock::now()+2s;
 while(!messenger.Queued()){assert(std::chrono::steady_clock::now()<deadline);std::this_thread::yield();}
}
static auto send(CApplicationMessenger& messenger,CApplication& app,std::atomic<bool>* fullscreenAtReply=nullptr){
 return std::async(std::launch::async,[&,fullscreenAtReply]{messenger.SendMsg(ThreadMessage{5},true);if(fullscreenAtReply)*fullscreenAtReply=gfx.fullscreen;return app.player.IsPlaying();});
}
static void stopped(CApplication& app){app.player.OnPlaybackStopped();app.PlaybackCleanup();app.player.CompleteCloseCompletions();}
static bool native_probe(bool enforce){
 CApplication app;begin(app);auto old=app.player.m_pPlayer;old->playing=true;old->hasVideo=true;
 CApplicationMessenger messenger;
 messenger.target=[&](ThreadMessage* message){
   // Production TMSG_MEDIA_STOP body, same default playlist selector as Python.
   auto appPlayer=&app.player;auto& g_application=app;
   auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
 };
 std::atomic<bool> fullscreenAtReply=true;
 auto caller=send(messenger,app,&fullscreenAtReply);wait_queued(messenger);
 const auto start=std::chrono::steady_clock::now();messenger.ProcessMessages();
 assert(std::chrono::steady_clock::now()-start<100ms); // main never joins/waits
 bool early=caller.wait_for(100ms)==std::future_status::ready;
 bool earlyPlaying=early?caller.get():false;
 bool prematureRestore=!gfx.fullscreen;
 assert(app.player.m_closingPlayer==old);
 // Initial renderer acknowledgement sets StopThread(false), but final renderer
 // and main consumption of the terminal notification still own this close.
 old->playing=false;
 stopped(app);app.Frame();
 if(enforce){assert(!early);assert(gfx.fullscreen);}
 old->canClose=true;app.Frame();
 bool returnedPlaying=early?earlyPlaying:caller.get();
 assert(!app.player.HasPlayer()&&!gfx.fullscreen);
 std::cout<<"{\"current_stop_returned_while_old_playing\":"<<returnedPlaying
          <<",\"fullscreen_restored_before_retirement\":"<<prematureRestore<<"}\n";
 if(enforce)assert(!returnedPlaying&&!prematureRestore&&!fullscreenAtReply);
 return returnedPlaying;
}
static void original_close(){
 for(bool succeeds:{false,true}){
  LegacyManager app;auto old=std::make_shared<LegacyVideo>();app.m_pPlayer=old;
  old->m_renderManager.ready=succeeds;
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage*){app.ClosePlayer();};
  auto caller=std::async(std::launch::async,[&]{messenger.SendMsg(ThreadMessage{5},true);return app.m_pPlayer&& !app.m_pPlayer->m_bStop;});
  wait_queued(messenger);messenger.ProcessMessages();
  assert(caller.get()==!succeeds);
  assert(bool(app.m_pPlayer)==!succeeds);
  if(succeeds)assert(old->terminalQueued&&!old->m_HasVideo&&old->m_renderManager.uninit==2);
 }
 std::cout<<"PASS original native successful Stop completes thread/renderer retirement; failed UnInit retains player\n";
}
static void reply_cases(){
 // Both dispatcher queues preserve a transferred reply and its shared result.
 for(bool window:{false,true}){
  CApplicationMessenger messenger;std::shared_ptr<CEvent> held;
  messenger.target=[&](ThreadMessage* message){message->SetResult(42);held=message->DeferCompletion();};
  auto caller=std::async(std::launch::async,[&]{return messenger.SendMsg(ThreadMessage{static_cast<uint32_t>(window?TMSG_GUI_MESSAGE:5)},true);});
  wait_queued(messenger);
  if(window)messenger.ProcessWindowMessages();else messenger.ProcessMessages();
  assert(held&&caller.wait_for(1ms)==std::future_status::timeout);
  held->Set();assert(caller.get()==42);
 }

 // Main-thread SendMsg has no waiting event; PostMsg has no waiting caller.
 for(bool posted:{false,true}){
  CApplication app;begin(app);app.player.m_pPlayer->playing=true;
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage* message){
   auto appPlayer=&app.player;auto& g_application=app;auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
  };
  messenger.SendMsg(ThreadMessage{5},!posted);if(posted)messenger.ProcessMessages();
  assert(app.player.m_closingPlayer);app.player.m_pPlayer->playing=false;
  stopped(app);app.player.m_pPlayer->canClose=true;app.Frame();
 }
 // Empty and immediate completed stop never leave a caller waiting.
 for(bool empty:{false,true}){
  CApplication app;begin(app);app.player.m_waitForPlaybackStop=false;
  if(empty)app.player.m_pPlayer.reset();else app.player.m_pPlayer->canClose=true;
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage* message){
   auto appPlayer=&app.player;auto& g_application=app;auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
  };
  auto caller=send(messenger,app);wait_queued(messenger);messenger.ProcessMessages();
  assert(caller.wait_for(1s)==std::future_status::ready&&!caller.get());
 }
 // Two Stop requests share the same retained original. New intent and shutdown
 // cannot turn their completion into a wait for a different player's state.
 for(bool shutdown:{false,true}){
  CApplication app;begin(app);auto old=app.player.m_pPlayer;old->playing=true;
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage* message){
   auto appPlayer=&app.player;auto& g_application=app;auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
  };
  auto first=send(messenger,app);wait_queued(messenger);messenger.ProcessMessages();
  auto second=send(messenger,app);wait_queued(messenger);messenger.ProcessMessages();
  assert(first.wait_for(1ms)==std::future_status::timeout&&second.wait_for(1ms)==std::future_status::timeout);
  old->playing=false;app.player.OnPlaybackStopped();
  if(shutdown)app.player.ClosePlayer(true);
  else {CFileItem next;next.id=99;app.player.OpenFile(next,{},app.services.factory,"new",app.callback);}
  old->canClose=true;app.player.ContinueClose();app.PlaybackCleanup();app.player.CompleteCloseCompletions();
  assert(first.wait_for(1s)==std::future_status::ready&&second.wait_for(1s)==std::future_status::ready);
  assert(!first.get()&&!second.get());
  app.player.OpenNext(app.services.factory);
  assert(app.player.created==(shutdown?0:1));
  if(!shutdown)assert(app.player.m_pPlayer->lastItem==99);
 }
 // A final native acknowledgement cannot release the reply before main has
 // consumed the original terminal notification, irrespective of callback order.
 {
  CApplication app;begin(app);auto old=app.player.m_pPlayer;old->playing=true;
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage* message){
   auto appPlayer=&app.player;auto& g_application=app;auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
  };
  auto caller=send(messenger,app);wait_queued(messenger);messenger.ProcessMessages();
  old->playing=false;old->canClose=true;app.Frame();
  assert(caller.wait_for(1ms)==std::future_status::timeout&&gfx.fullscreen&&app.player.m_closingPlayer==old);
  stopped(app);app.Frame();assert(caller.wait_for(1s)==std::future_status::ready&&!caller.get());
 }
 // Destruction may reenter with a newer player. The old request must be attached
 // before ClosePlayer and moved out before destruction starts that newer close.
 {
  CApplication app;begin(app);auto old=app.player.m_pPlayer;
  app.player.m_waitForPlaybackStop=false;old->canClose=true;
  auto newerReply=std::make_shared<CEvent>();
  old->onDestroy=[&]{
   CFileItem next;next.id=77;assert(app.player.OpenFile(next,{},app.services.factory,"new",app.callback));
   app.player.ClosePlayer(false,newerReply);
  };
  old.reset();
  CApplicationMessenger messenger;messenger.target=[&](ThreadMessage* message){
   auto appPlayer=&app.player;auto& g_application=app;auto* pMsg=message;auto wakeScreensaver=[]{};
   @MEDIASTOP@
  };
  auto caller=send(messenger,app);wait_queued(messenger);messenger.ProcessMessages();
  assert(caller.wait_for(1s)==std::future_status::ready);caller.get();
  assert(app.player.m_closingPlayer&&app.player.m_pPlayer->lastItem==77&&!newerReply->Wait(1ms));
  app.player.m_pPlayer->playing=false;app.player.m_pPlayer->canClose=true;
  stopped(app);app.Frame();assert(newerReply->Wait(1s));
 }
 // A queued message abandoned at shutdown still releases its ordinary reply.
 CApplication app;begin(app);CApplicationMessenger messenger;messenger.target=[](ThreadMessage*){};
 auto caller=send(messenger,app);wait_queued(messenger);messenger.Cleanup();
 assert(caller.wait_for(1s)==std::future_status::ready);caller.get();
 std::cout<<"PASS synchronous/main/posted/empty/repeated/superseded/shutdown stop ownership\n";
}
int main(int argc,char**){original_close();native_probe(argc==1);if(argc==1){reply_cases();autoplay_cases();}}
'''


def build(baseline=None):
    def source(path):
        if baseline:
            return subprocess.check_output(['git','show',baseline+':'+path],cwd=ROOT,text=True)
        return (ROOT/path).read_text()
    prelude=fixture['PRELUDE']
    prelude=prelude.replace('struct CEvent {bool signaled=false;void Set(){signaled=true;}};', '')
    start=prelude.index('struct CCriticalSection {')
    end=prelude.index('struct CFileItem',start)
    prelude=prelude[:start]+'''struct CCriticalSection {
 std::recursive_mutex mutex;bool held=false;
 void lock(){mutex.lock();held=true;}void unlock(){held=false;mutex.unlock();}
};
'''+prelude[end:]
    if baseline:
        prelude=prelude.replace('bool ClosePlayer(bool=false,std::shared_ptr<CEvent> = {});void ResetPlayer(std::shared_ptr<CEvent> = {});',
                                'bool ClosePlayer(bool=false);void ResetPlayer();')
    prelude=prelude.replace('  ~IPlayer(){', '  std::function<void()> onDestroy;\n  ~IPlayer(){if(onDestroy)onDestroy();')
    application=source('xbmc/application/Application.cpp')
    player=source('xbmc/application/ApplicationPlayer.cpp')
    methods=['std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
             'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::CompleteCloseCompletions()', 'bool CApplicationPlayer::ClosePlayer(',
             'void CApplicationPlayer::OnPlaybackStopped()', 'void CApplicationPlayer::OpenNext(',
             'void CApplicationPlayer::ResetPlayer(', 'bool CApplicationPlayer::OpenFile(',
             'bool CApplicationPlayer::HasPendingOpen() const', 'bool CApplicationPlayer::HasPendingVideoOpen() const',
             'bool CApplicationPlayer::PreparePlaybackCleanup()', 'bool CApplicationPlayer::PlaybackCleanupCompleted() const']
    if baseline:
        methods.remove('void CApplicationPlayer::CompleteCloseCompletions()')
    if not baseline:
        for message,callback in [('GUI_MSG_PLAYBACK_STOPPED','OnPlayBackStopped()'),('GUI_MSG_PLAYBACK_ENDED','OnPlayBackEnded()')]:
            terminal=application[application.index('  case '+message+':'):]
            terminal=terminal[:terminal.index(callback)+len(callback)]
            assert terminal.rindex('PlaybackCleanup()') < terminal.rindex('CompleteCloseCompletions()') < terminal.rindex(callback)
    frame=function(application,'void CApplication::FrameMove(')
    continuation=frame[frame.index('  const bool openingVideo ='):frame.index('  // this will go away')]
    harness=fixture['HARNESS'].replace('int main(){','int autoplay_cases(){')
    if baseline:
        harness=harness.replace('void StopPlaying(std::shared_ptr<CEvent> = {});','void StopPlaying();')
    # Main's implicit return is unavailable in a named test function.
    pos=harness.rfind('\n}')
    harness=harness[:pos]+'\n return 0;'+harness[pos:]
    harness=harness.replace('static CGUIComponent* GetGUI()', 'static CacheCore& GetDataCacheCore(){static CacheCore cache;return cache;} static CGUIComponent* GetGUI()')
    harness=harness.replace('constexpr int TYPE_NONE=0,TYPE_PICTURE=1,TYPE_VIDEO=2,TYPE_MUSIC=3;',
                            'constexpr int TYPE_NONE=-1,TYPE_PICTURE=2,TYPE_VIDEO=1,TYPE_MUSIC=0;')
    harness=harness.replace('@CONTINUATION@',continuation).replace('@CLEANUP@',function(application,'void CApplication::PlaybackCleanup()'))
    harness=harness.replace('@STOP@',function(application,'void CApplication::StopPlaying('))
    playlist=source('xbmc/PlayListPlayer.cpp')
    stop=function(playlist,'  case TMSG_MEDIA_STOP:')
    stop=stop[stop.index('{')+1:-1]
    harness=harness.replace('@MEDIASTOP@',stop.replace('pMsg->DeferCompletion()', 'std::shared_ptr<CEvent>{}'))
    case=application[application.index('  case GUI_MSG_PLAYLISTPLAYER_STOPPED:'):application.index('  case GUI_MSG_PLAYBACK_AVSTARTED:')]
    harness=harness.replace('@PLAYLISTSTOP@',case[case.index(':')+1:]).replace('@PLAYNEXT@',function(playlist,'bool CPlayListPlayer::PlayNext('))
    messenger=source('xbmc/messaging/ApplicationMessenger.cpp')
    mm='\n'.join(function(messenger,s) for s in ['int CApplicationMessenger::SendMsg(ThreadMessage&&',
            'void CApplicationMessenger::ProcessMessages()', 'void CApplicationMessenger::ProcessWindowMessages()', 'void CApplicationMessenger::Cleanup()'])
    oldplayer=subprocess.check_output(['git','show',BASELINE+':xbmc/application/ApplicationPlayer.cpp'],cwd=ROOT,text=True)
    oldvideo=subprocess.check_output(['git','show',BASELINE+':xbmc/cores/VideoPlayer/VideoPlayer.cpp'],cwd=ROOT,text=True)
    lm=function(oldvideo,'bool CVideoPlayer::CloseFile(').replace('CVideoPlayer::','LegacyVideo::')
    lm+='\n'+'\n'.join(function(oldplayer,s) for s in ['std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
        'void CApplicationPlayer::ClosePlayer()', 'void CApplicationPlayer::ResetPlayer()', 'bool CApplicationPlayer::CloseFile('])
    lm=lm.replace('CApplicationPlayer::','LegacyManager::').replace('std::shared_ptr<IPlayer>','std::shared_ptr<LegacyVideo>')
    tests=TESTS
    if baseline:
        tests=tests[:tests.index('static void reply_cases()')]+\
            'int main(){original_close();native_probe(false);}'
        tests=tests.replace('app.player.CompleteCloseCompletions();','')
    thread_message=source('xbmc/messaging/ThreadMessage.h').replace('#pragma once','')
    return '#include <mutex>\n'+EVENT+prelude+'\n'.join(function(player,s) for s in methods)+harness+\
        MESSENGER.replace('@METHODS@',mm).replace('@THREADMESSAGE@',thread_message)+LEGACY.replace('@METHODS@',lm)+\
        tests.replace('auto wakeScreensaver=[]{};', 'auto wakeScreensaver=[]{};const bool m_bIsDeferredPlayPending=CServiceBroker::playlist.m_bIsDeferredPlayPending;').replace('@MEDIASTOP@',stop)


def addon_replay(early, corrected=False):
    psource=subprocess.check_output(['git','show',('9d4076c7' if corrected else '39245fd4')+':lib/player.py'],cwd=PM4K,text=True)
    dsource=subprocess.check_output(['git','show','39245fd4:lib/windows/seekdialog.py'],cwd=PM4K,text=True)
    def method(source,cls,name,ns):
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name==cls)
        node=next(n for n in node.body if isinstance(n,ast.FunctionDef) and n.name==name)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<production '+name+'>','exec'),ns)
        return ns[name]
    class Playback:
        def __init__(self):
            self.playing=True;self.fullscreen=True;self.busy=False;self._closed=False;self.isExternal=False
            self.hasOSD=self.hasSeekOSD=False;self.currentTime=1;self.opened=[];self.closed=[];self.waits=0;self.queued=False
            self.handler=SimpleNamespace(queuingNext=False,queuingSpecific=False,playbackID='E3',playlist=SimpleNamespace(hasNext=lambda:True))
        def isPlayingVideo(self):return self.playing
        def getTime(self):return 1.0
        def onVideoWindowOpened(self):self.opened.append(self.handler.playbackID)
        def onVideoWindowClosed(self):self.closed.append(self.handler.playbackID)
        def stop(self):self.playing=early;self.fullscreen=False
        def wait(self,*args):
            self.waits+=1
            if self.handler.queuingNext:
                self.queued=True;self.handler.queuingNext=False;self.handler.playbackID='E4';self.busy=True
            if self.handler.playbackID=='E4':
                self.fullscreen=True
                if self.waits>13:self._closed=True
            return False
        def visible(self,condition):return {'VideoPlayer.IsFullscreen':self.fullscreen,'Window.IsVisible(busydialog)':self.busy}.get(condition,False)
    p=Playback()
    monitor=SimpleNamespace(abortRequested=lambda:False,waitForAbort=p.wait,tv_standby=False)
    ns={'util':SimpleNamespace(DEBUG_LOG=lambda *a:None,MONITOR=monitor),'xbmc':SimpleNamespace(getCondVisibility=p.visible),'FINAL_MARKER_NEGOFF':3000}
    tick=method(psource,'PlexPlayer','_videoMonitor',ns)
    prepare=method(dsource,'SeekDialog','prepareNewPlayback',ns)
    final=method(dsource,'SeekDialog','handleFinalMarker',ns)
    dialog=SimpleNamespace(duration=100000,handler=p.handler,player=p,bingeMode=True,skipPostPlay=True,
                           sendTimeline=lambda **kw:None,killTimeKeeper=lambda:None)
    p.STATE_STOPPED='stopped'
    dialog.prepareNewPlayback=lambda **kw:prepare(dialog,**kw)
    p.handler.tick=lambda:final(dialog,{'marker':SimpleNamespace(endTimeOffset=100000)})
    tick(p)
    if not early:
        p.playing=True;p.fullscreen=True;p.busy=True;p._closed=False;p.handler.queuingNext=False;p.handler.playbackID='E4';p.waits=12
        p.handler.tick=lambda:None
        tick(p)
    assert p.queued==early,(early,corrected,p.queued)
    assert p.opened==(['E3'] if early and not corrected else ['E3','E4']),p.opened
    print('PASS addon replay:', 'corrected' if corrected else 'unchanged',
          'early_native_stop='+str(early), 'queue_wait='+str(p.queued), 'opened='+str(p.opened))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',nargs='?',const='0acd9d09fa',help='replay pre-correction native sources from this revision')
    parser.add_argument('--negative-controls',action='store_true',help='prove changed dispatch, close and ownership assertions fail when reverted')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='native-stop-') as tmp:
        out=Path(tmp);code=build(args.baseline);(out/'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-pthread',
                        '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
        result=subprocess.run([str(out/'test')]+(['probe'] if args.baseline else []),text=True,capture_output=True,timeout=20)
        print(result.stdout,end='');print(result.stderr,end='');result.check_returncode()
        observed=json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{')))
        early=bool(observed['current_stop_returned_while_old_playing'])
        assert early==bool(args.baseline),observed
        for corrected in (False,True):
            addon_replay(early,corrected)
        if args.negative_controls:
            assert not args.baseline, 'negative controls target the corrected production sources'
            mutations=[
                ('dispatcher retains reply before receiver',
                 '    ProcessMessage(pMsg);\n\n    std::shared_ptr<CEvent> waitEvent = pMsg->waitEvent;',
                 '    auto waitEvent = pMsg->waitEvent;\n    ProcessMessage(pMsg);'),
                ('receiver does not transfer reply', 'auto completion = pMsg->DeferCompletion();',
                 'std::shared_ptr<CEvent> completion;'),
                ('close signals reply before retirement',
                 'm_closeCompletionEvents.emplace_back(std::move(completion));', 'completion->Set();'),
                ('native close ignores terminal consumption',
                 'if (!m_closeAcknowledged || m_waitForPlaybackStop)', 'if (!m_closeAcknowledged)'),
                ('fullscreen leaves before retirement',
                 'stopVideo && !ownsPlayback &&', 'stopVideo && (ownsPlayback || !ownsPlayback) &&'),
            ]
            # Recreate the dangerous attach-after-close ordering in ResetPlayer.
            attach='  // Attach the reply before close/destruction can reenter with another player.\n  if (completion)\n  {\n    if (m_closingPlayer)\n      m_closeCompletionEvents.emplace_back(std::move(completion));\n    else\n      m_retiredCloseCompletionEvents.emplace_back(std::move(completion));\n  }\n  ContinueClose();'
            mutations.append(('reply attached after reentrant close',attach,
                              '  ContinueClose();\n'+attach[:attach.rindex('  ContinueClose();')]))
            for name,original,changed in mutations:
                assert original in code,name
                (out/'test.cpp').write_text(code.replace(original,changed))
                subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-pthread',
                                '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
                mutant=subprocess.run([str(out/'test')],text=True,capture_output=True,timeout=20)
                assert mutant.returncode!=0 and ('Assertion' in mutant.stderr or 'assertion' in mutant.stderr), (name,mutant.stdout,mutant.stderr)
                print('PASS negative control rejected:',name)

if __name__=='__main__':main()
