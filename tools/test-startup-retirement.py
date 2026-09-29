#!/usr/bin/env python3
"""Run the production pre-run retirement pump/entry wiring with recording services.

Real application-player close methods, render mailbox and AML display/native
admission are used. Full service teardown, platform callbacks and GL are not.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-player-retirement.py'))
function = fixture['function']

HARNESS = r'''
#include "cores/VideoPlayer/VideoRenderers/RenderLifecycle.h"
#include "windowing/amlogic/AMLDisplayLifecycle.h"
#include <atomic>
#include <functional>
#include <iostream>
using namespace std::chrono_literals;
constexpr int EXITCODE_QUIT=0;
constexpr int GUI_MSG_PLAYBACK_ENDED=1, GUI_MSG_PLAYBACK_STOPPED=2, GUI_MSG_PLAYBACK_STARTED=3;
struct CSingleExit {
  CCriticalSection& guard; bool saved;
  CSingleExit(CCriticalSection& g):guard(g),saved(g.held){if(saved)guard.unlock();}
  ~CSingleExit(){if(saved)guard.lock();}
};
struct WindowManager {
  std::vector<int> messages;
  int RemoveThreadMessageByMessageIds(int* ids) {
    int removed=0;
    for(int* id=ids;*id;++id) {
      auto end=std::remove(messages.begin(),messages.end(),*id);
      removed+=messages.end()-end; messages.erase(end,messages.end());
    }
    return removed;
  }
};
struct GUI {WindowManager wm;WindowManager& GetWindowManager(){return wm;}};
struct CWinSystemAmlogicGLESContext {
  bool m_shutdownRequested=false;
  int retireCalls=0;
  CAMLDisplayLifecycle m_displayLifecycle;
  std::unique_ptr<CAMLDisplayLifecycle::Mutation> m_shutdownAdmission;
  void RetireNativeTransactions(){++retireCalls;}
  bool PrepareForShutdown();
};
@PREPARE@
struct CServiceBroker {
  static inline CWinSystemAmlogicGLESContext* window=nullptr;
  static auto* GetWinSystem(){return window;}
};
struct CAMLVideoBufferPool {
  static inline int returns=0;
  static void ProcessReturns(){++returns;}
};
namespace KODI::TIME {
  inline std::function<void()> onSleep;
  void Sleep(std::chrono::milliseconds delay){assert(delay==10ms);onSleep();}
}
struct CApplication {
  CApplicationPlayer app;
  CCriticalSection m_frameMoveGuard;
  std::optional<int> m_pendingStop;
  std::unique_ptr<GUI> m_pGUI;
  int stopCalls=0,teardowns=0,cleanups=0,runs=0;
  bool create=true,gui=false,initialize=false,stopFailure=false,cleanupResult=true;
  template<class T> T* GetComponent(){return &app;}
  bool Stop(int exitCode) {
    ++stopCalls;assert(exitCode==EXITCODE_QUIT);
@LATCH@
@GATE@
    assert(m_frameMoveGuard.held);
    m_frameMoveGuard.unlock();
    ++teardowns;
    return !stopFailure;
  }
  bool Cleanup(){assert(teardowns==1 && !app.HasPlayer());++cleanups;return cleanupResult;}
  bool StopBeforeRun(bool renderGUI);
  bool Create(){return create;}
  bool CreateGUI(){m_frameMoveGuard.lock();return gui;}
  bool Initialize(){return initialize;}
  int Run(){++runs;return 42;}
} g_application;
@PUMP@
struct CMessagePrinter {static void DisplayError(const char*){}};
@ENTRY@
int main(int argc,char** argv) {
  assert(argc==2);
  const int scenario=std::stoi(argv[1]);
  auto& app=g_application;
  int sleeps=0, destroyed=0;
  std::atomic<bool> nativeActive=false,releaseNative=false,nativeDone=false;
  std::thread native;
  std::unique_ptr<CWinSystemAmlogicGLESContext> window;
  std::shared_ptr<CRenderLifecycle> renderer;
  std::shared_ptr<CRenderLifecycle::Request> receipt;
  if(scenario==1 || scenario==2) {
    window=std::make_unique<CWinSystemAmlogicGLESContext>();
    CServiceBroker::window=window.get();
    native=std::thread([&]{
      auto token=CAMLSession::FenceNative();assert(token && CAMLSession::TryBeginNative(token));
      nativeActive=true;
      while(!releaseNative.load())std::this_thread::yield();
      assert(CAMLSession::EndNative(token));nativeDone=true;
    });
    while(!nativeActive.load())std::this_thread::yield();
  }
  if(scenario==2) {
    app.gui=true;app.m_pGUI=std::make_unique<GUI>();
    app.m_pGUI->wm.messages={GUI_MSG_PLAYBACK_STARTED};
    app.app.m_pPlayer=std::make_shared<IPlayer>();
    app.app.m_pPlayer->applicationLock=&app.app.m_playerLock;
    app.app.m_pPlayer->destroyed=&destroyed;
    app.app.m_waitForPlaybackStop=true;
    app.app.m_nextItem.pItem=std::make_shared<CFileItem>();
    std::weak_ptr<IPlayer> original=app.app.m_pPlayer;
    renderer=CRenderLifecycle::Create();
    receipt=renderer->Submit([original]{auto player=original.lock();assert(player);player->canClose=true;return true;});
  }
  if(scenario==3)app.stopFailure=true;
  if(scenario==4)app.cleanupResult=false;
  if(scenario==5) {app.gui=true;app.initialize=true;}
  if(scenario==6)app.create=false;
  KODI::TIME::onSleep=[&]{
    ++sleeps; assert(sleeps<=101); // fixture bound; production has no timeout completion
    assert(!app.m_frameMoveGuard.held && app.teardowns==0 && app.cleanups==0);
    if(window)assert(CServiceBroker::window==window.get());
#ifdef HAS_LIBAMCODEC
    assert(CAMLVideoBufferPool::returns==sleeps);
#endif
    if(scenario==2) {
      assert(receipt->status==CRenderLifecycle::Status::COMPLETED);
      assert(!app.app.m_nextItem.pItem && app.app.m_shutdown);
      if(sleeps<=8)assert(destroyed==0 && app.app.HasPlayer());
      if(sleeps==8)app.m_pGUI->wm.messages.push_back(GUI_MSG_PLAYBACK_ENDED);
      if(sleeps>9)assert(destroyed==1 && !app.app.HasPlayer());
    }
    if(sleeps==100) {
      assert(window && window->m_shutdownRequested && !*window->m_shutdownAdmission);
      releaseNative=true;
      while(!nativeDone.load())std::this_thread::yield();
    }
  };
  assert(XBMC_Run(scenario!=7)==(scenario==5?42:-1));
  assert(app.runs==(scenario==5));
  if(scenario==5 || scenario==6)assert(app.stopCalls==0 && app.cleanups==0);
  else {
    assert(app.teardowns==1 && !app.m_frameMoveGuard.held);
    assert(app.cleanups==(scenario==3?0:1));
    assert(sleeps==((scenario==1 || scenario==2)?100:0));
    if(window)assert(*window->m_shutdownAdmission);
    if(scenario==2) {
      assert(destroyed==1);
      assert(app.m_pGUI->wm.messages==std::vector<int>{GUI_MSG_PLAYBACK_STARTED});
    }
  }
  if(native.joinable())native.join();
  std::cout << "startup scenario " << scenario << " passed\n";
}
'''


def build_source():
    app = (ROOT / 'xbmc/application/Application.cpp').read_text()
    stop = function(app, 'bool CApplication::Stop(')
    latch = stop[stop.index('  if (m_pendingStop)'):stop.index('  CLog::Log')]
    gate = stop[stop.index('  const auto appPlayer = GetComponent<CApplicationPlayer>();'):stop.index('  // Safety net')]
    player = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    methods = '\n'.join(function(player, signature) for signature in [
        'std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
        'bool CApplicationPlayer::ClosePlayer(', 'void CApplicationPlayer::ResetPlayer()',
        'void CApplicationPlayer::ContinueClose()', 'void CApplicationPlayer::OnPlaybackStopped()'])
    window = (ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp').read_text()
    entry = (ROOT / 'xbmc/platform/xbmc.cpp').read_text()
    harness = HARNESS.replace('@PREPARE@', function(window, 'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()'))
    harness = harness.replace('@LATCH@', latch).replace('@GATE@', gate)
    harness = harness.replace('@PUMP@', function(app, 'bool CApplication::StopBeforeRun('))
    harness = harness.replace('@ENTRY@', function(entry, 'extern "C" int XBMC_Run('))
    assert 'bool StopBeforeRun(bool renderGUI);' in (ROOT / 'xbmc/application/Application.h').read_text()
    # Early GUI failure occurs before texture-cache registration. Check the
    # production null guard rather than claiming to run the full skin subsystem.
    skin = function((ROOT / 'xbmc/application/ApplicationSkinHandling.cpp').read_text(),
                    'void CApplicationSkinHandling::UnloadSkin()')
    assert 'if (auto textureCache = CServiceBroker::GetTextureCache())\n      textureCache->Deinitialize();' in skin
    return fixture['PRELUDE'] + methods + harness


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    options = parser.parse_args()
    source = build_source()
    with tempfile.TemporaryDirectory(prefix='startup-retirement-') as directory:
        cpp = Path(directory) / 'fixture.cpp'
        binary = Path(directory) / 'fixture'
        def compile_run(code, scenarios, aml=True, expect_failure=False):
            cpp.write_text(code)
            command = [os.environ.get('CXX', 'c++'), '-std=c++17', '-pthread', '-I', str(ROOT / 'xbmc')]
            if aml:
                command.append('-DHAS_LIBAMCODEC')
            subprocess.run(command + [str(cpp), '-o', str(binary)], check=True, capture_output=True, text=True)
            for scenario in scenarios:
                result = subprocess.run([str(binary), str(scenario)], capture_output=True, text=True, timeout=5)
                if expect_failure:
                    assert result.returncode != 0, 'mutation survived'
                else:
                    assert result.returncode == 0, result.stdout + result.stderr
                    print(result.stdout.strip())
        compile_run(source, range(8))
        compile_run(source, [0, 2], aml=False)
        if options.negative_controls:
            mutations = [
                ('early return while pending', '    if (!m_pendingStop)\n      return false;', '    return false;', 1),
                ('skip render acknowledgments', '      CRenderLifecycle::ProcessAll();', '', 2),
                ('skip AML returns', '      CAMLVideoBufferPool::ProcessReturns();', '', 1),
                ('wait under frame guard', '      CSingleExit exit(m_frameMoveGuard);', '', 1),
                ('false terminal acknowledgment', 'GUI_MSG_PLAYBACK_ENDED, GUI_MSG_PLAYBACK_STOPPED, 0', 'GUI_MSG_PLAYBACK_STARTED, 0', 2),
                ('omit stop retry unlatch', '    m_pendingStop.reset();\n  }\n  return Cleanup();', '  }\n  return Cleanup();', 1),
                ('skip Initialize-failure cleanup', '    if (!g_application.StopBeforeRun(renderGUI))\n      CMessagePrinter::DisplayError("ERROR: Failed to clean up application startup");', '', 2),
            ]
            for label, old, new, scenario in mutations:
                assert source.count(old)==1, label
                compile_run(source.replace(old,new), [scenario], expect_failure=True)
                print('rejected:', label)
    print('PASS: production startup retirement; full service/platform teardown remains unverified')


if __name__ == '__main__':
    main()
