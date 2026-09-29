#!/usr/bin/env python3
"""Exercise the production renderer lifecycle mailbox with ASan/UBSan.

The real RenderLifecycle.h supplies queueing, completion, cancellation and the
registry. Actual renderer lifecycle wrappers and CVideoPlayer::CloseFile are
extracted and executed with recording renderer/service/thread substitutes.
The complete Configure request method and actual main-helper failure branches
run with a recording renderer creation/configuration substitute.
Actual thread joins, display mode changes, GL and drivers are not exercised.
Production owner-thread rejection and bounded dispatch per pump are exercised.
Use --negative-controls to execute temporary broken-header variants as well.
"""
import argparse
import os
import runpy
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = Path('cores/VideoPlayer/VideoRenderers/RenderLifecycle.h')

HARNESS = r'''
#include "cores/VideoPlayer/VideoRenderers/RenderLifecycle.h"
#include "windowing/amlogic/AMLNativeTransaction.h"
#include <cassert>
#include <future>
#include <stdexcept>
#include <thread>
using Mailbox = CRenderLifecycle;
using Status = Mailbox::Status;
using namespace std::chrono_literals;

static void pending_continuation()
{
  auto mailbox = Mailbox::Create();
  bool ready = false; int calls = 0, dependent = 0;
  auto request = mailbox->Submit([&]() -> std::optional<bool> {
    ++calls;
    if (!ready) return std::nullopt;
    return true;
  });
  auto next = mailbox->Submit([&] { ++dependent; return true; });
  mailbox->Process();
  assert(calls == 1 && dependent == 0 && !request->Wait(0ms) && request->execute);
  mailbox->Process(); assert(calls == 2 && dependent == 0);
  ready = true; mailbox->Process();
  assert(request->Wait(0ms) && next->Wait(0ms) && dependent == 1 && calls == 3);
  auto pending = mailbox->Submit([]() -> std::optional<bool> { return std::nullopt; });
  mailbox->Process(); mailbox->AdvanceSession();
  assert(pending->status == Status::CANCELLED && !pending->execute);
  assert(mailbox->Close());
}

static void delayed_and_exactly_once()
{
  auto mailbox = Mailbox::Create();
  const auto main = std::this_thread::get_id();
  int calls = 0;
  bool saveBuffers = false;
  auto request = mailbox->Submit([&, originalFlag = saveBuffers] {
    assert(std::this_thread::get_id() == main);
    assert(!originalFlag);
    assert(++calls == 1);
    return true;
  });
  saveBuffers = true;
  assert(request && request->session != 0 && request->serial != 0);
  assert(!request->Wait(0ms));
  assert(request->status == Status::PENDING && calls == 0);
  assert(request->execute); // A timeout retains its original action/resources.
  Mailbox::ProcessAll();
  assert(request->Wait(0ms) && calls == 1 && !request->execute);
  mailbox->Process();
  Mailbox::ProcessAll();
  assert(calls == 1);
  assert(mailbox->Close());
}

static void bounded_backpressure()
{
  auto mailbox = Mailbox::Create();
  std::vector<std::shared_ptr<Mailbox::Request>> requests;
  std::vector<int> order;
  for (int i = 0; i < 4; ++i)
  {
    auto request = mailbox->Submit([&, i] {
      order.push_back(i);
      return true;
    });
    assert(request);
    if (!requests.empty())
      assert(request->serial > requests.back()->serial &&
             request->session == requests.back()->session);
    requests.push_back(request);
  }
  assert(!mailbox->Submit([] { assert(false); return true; }));
  for (const auto& request : requests)
  {
    assert(!request->Wait(0ms));
    assert(request->status == Status::PENDING);
  }
  // Timeout must not free admission for a replacement of an older request.
  assert(!mailbox->Submit([] { assert(false); return true; }));
  mailbox->Process();
  assert((order == std::vector<int>{0, 1, 2, 3}));
  for (const auto& request : requests)
    assert(request->Wait(0ms));
  auto next = mailbox->Submit([] { return true; });
  assert(next && next->serial > requests.back()->serial);
  assert(mailbox->Close());
  assert(next->status == Status::CANCELLED && !next->Wait(0ms));
}

static void active_request_is_pinned()
{
  auto mailbox = Mailbox::Create();
  auto resource = std::make_shared<int>(7);
  std::weak_ptr<int> retained = resource;
  int secondCalls = 0;
  std::shared_ptr<Mailbox::Request> request;
  auto action = [&, resource] {
    assert(*resource == 7 && request->status == Status::EXECUTING);
    assert(!request->Wait(0ms));
    assert(!mailbox->Close()); // No ownership transfer while executing.
    mailbox->Process();       // No recursive executor / second request.
    Mailbox::ProcessAll();
    assert(secondCalls == 0 && !retained.expired());
    return true;
  };
  request = mailbox->Submit(std::move(action));
  auto second = mailbox->Submit([&] { ++secondCalls; return true; });
  resource.reset();
  assert(!retained.expired());
  mailbox->Process();
  assert(request->Wait(0ms) && second->Wait(0ms) && secondCalls == 1);
  assert(retained.expired());
  assert(mailbox->Close());
}

static void replaced_session_and_late_dispatch()
{
  struct Target { int generation{1}; int calls{0}; } target;
  auto old = Mailbox::Create();
  auto resource = std::make_shared<int>(1);
  std::weak_ptr<int> retained = resource;
  auto stale = old->Submit([&target, resource] {
    assert(target.generation == 1);
    ++target.calls;
    return *resource == 1;
  });
  resource.reset();
  assert(!stale->Wait(0ms) && !retained.expired());
  assert(old->Close());
  assert(stale->status == Status::CANCELLED && !stale->execute);
  assert(retained.expired());
  assert(!old->Submit([] { assert(false); return true; }));
  target.generation = 2; // Same target allocation, different original session.
  auto replacement = Mailbox::Create();
  auto fresh = replacement->Submit([&target] {
    assert(target.generation == 2);
    ++target.calls;
    return true;
  });
  old->Process();
  assert(target.calls == 0 && fresh->status == Status::PENDING);
  Mailbox::ProcessAll();
  assert(target.calls == 1 && fresh->Wait(0ms) && !stale->Wait(0ms));
  old.reset(); // Expired registry entries cannot affect replacement.
  Mailbox::ProcessAll();
  assert(target.calls == 1);
  assert(replacement->Close());
}

static void failed_and_exceptional_actions()
{
  auto mailbox = Mailbox::Create();
  auto failed = mailbox->Submit([] { return false; });
  mailbox->Process();
  assert(failed->status == Status::FAILED && !failed->Wait(0ms));
  auto resource = std::make_shared<int>(3);
  std::weak_ptr<int> retained = resource;
  auto throws = mailbox->Submit([resource]() -> bool {
    assert(*resource == 3);
    throw std::runtime_error("controlled renderer failure");
  });
  resource.reset();
  auto pending = mailbox->Submit([] { return true; });
  bool caught = false;
  try { mailbox->Process(); }
  catch (const std::runtime_error&) { caught = true; }
  assert(caught && throws->status == Status::FAILED && !throws->Wait(0ms));
  assert(!throws->execute && retained.expired());
  assert(pending->status == Status::PENDING);
  assert(mailbox->Close()); // Exception must clear the executing state.
  assert(pending->status == Status::CANCELLED);
}

static void own_mailbox_progress_during_join()
{
  auto original = Mailbox::Create();
  auto current = Mailbox::Create();
  int originalCalls = 0;
  int currentCalls = 0;
  auto unrelated = current->Submit([&] { ++currentCalls; return true; });
  std::promise<void> posted;
  auto postedFuture = posted.get_future();
  std::promise<void> exited;
  auto exitedFuture = exited.get_future();
  const auto main = std::this_thread::get_id();
  std::thread player([&] {
    auto request = original->Submit([&] {
      assert(std::this_thread::get_id() == main);
      ++originalCalls;
      return true;
    });
    assert(request);
    posted.set_value();
    assert(request->Wait(5s));
    exited.set_value();
  });
  assert(postedFuture.wait_for(5s) == std::future_status::ready);
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (exitedFuture.wait_for(0ms) != std::future_status::ready)
  {
    assert(std::chrono::steady_clock::now() < deadline);
    original->Process(); // Fake close/join loop, never global/current dispatch.
    exitedFuture.wait_for(1ms);
  }
  player.join();
  assert(originalCalls == 1 && currentCalls == 0);
  assert(unrelated->status == Status::PENDING);
  assert(original->Close() && current->Close());
}


static void wrong_thread_rejection()
{
  auto mailbox = Mailbox::Create();
  int calls = 0;
  auto request = mailbox->Submit([&] { ++calls; return true; });
  std::thread wrong([&] {
    mailbox->Process();
    Mailbox::ProcessAll();
  });
  wrong.join();
  assert(calls == 0 && request->status == Status::PENDING);
  mailbox->Process();
  assert(calls == 1 && request->Wait(0ms));
  assert(mailbox->Close());
}

static void bounded_dispatch_per_pump()
{
  auto mailbox = Mailbox::Create();
  int calls = 0;
  std::function<bool()> action;
  action = [&] {
    ++calls;
    if (calls < 9)
      assert(mailbox->Submit(action));
    return true;
  };
  assert(mailbox->Submit(action));
  mailbox->Process();
  assert(calls == 4);
  mailbox->Process();
  assert(calls == 8);
  mailbox->Process();
  assert(calls == 9);
  assert(mailbox->Close());
}


static void advancing_original_session()
{
  auto mailbox = Mailbox::Create();
  int calls = 0;
  auto old = mailbox->Submit([&] { ++calls; return true; });
  std::thread wrong([&] { mailbox->AdvanceSession(); });
  wrong.join();
  assert(old->status == Status::PENDING);
  mailbox->AdvanceSession();
  assert(old->status == Status::CANCELLED && !old->execute && calls == 0);
  auto next = mailbox->Submit([&] { ++calls; return true; });
  assert(next->session > old->session && next->serial > old->serial);
  mailbox->Process();
  assert(next->Wait(0ms) && calls == 1 && !old->Wait(0ms));
  assert(mailbox->Close());
}

static void explicit_main_owner()
{
  const auto main = std::this_thread::get_id();
  std::shared_ptr<Mailbox> mailbox;
  std::shared_ptr<Mailbox::Request> request;
  int calls = 0;
  std::thread creator([&] {
    mailbox = Mailbox::Create(main);
    request = mailbox->Submit([&] { ++calls; return true; });
    mailbox->Process();
  });
  creator.join();
  assert(calls == 0 && request->status == Status::PENDING);
  mailbox->Process();
  assert(calls == 1 && request->Wait(0ms));
  assert(mailbox->Close());
}


static void close_waits_for_original_callback()
{
  auto mailbox = Mailbox::Create();
  std::atomic<bool> destroyed{false};
  struct Target
  {
    std::atomic<bool>& destroyed;
    int value{42};
    explicit Target(std::atomic<bool>& flag) : destroyed(flag) {}
    ~Target() { destroyed = true; }
  };
  auto target = std::make_unique<Target>(destroyed);
  auto* original = target.get();
  std::promise<void> entered, closing, retired;
  auto enteredFuture = entered.get_future();
  auto closingFuture = closing.get_future();
  auto retiredFuture = retired.get_future();
  auto active = mailbox->Submit([&] {
    entered.set_value();
    assert(closingFuture.wait_for(5s) == std::future_status::ready);
    assert(retiredFuture.wait_for(20ms) == std::future_status::timeout);
    assert(!destroyed && original->value == 42);
    return true;
  });
  auto queued = mailbox->Submit([original] { assert(false); return original->value == 42; });
  std::thread closer([&] {
    assert(enteredFuture.wait_for(5s) == std::future_status::ready);
    closing.set_value();
    assert(mailbox->Close());
    target.reset();
    retired.set_value();
  });
  mailbox->Process();
  closer.join();
  assert(destroyed && active->Wait(0ms));
  assert(queued->status == Status::CANCELLED && !queued->execute);
  assert(!mailbox->Submit([] { assert(false); return true; }));
  mailbox->Process();
  Mailbox::ProcessAll(); // Retained registry/mailbox cannot call deleted target.
}

static void close_from_nonowner_cancels_queued()
{
  auto mailbox = Mailbox::Create();
  int calls = 0;
  auto pending = mailbox->Submit([&] { ++calls; return true; });
  std::thread closer([&] { assert(mailbox->Close()); });
  closer.join();
  assert(pending->status == Status::CANCELLED && !pending->execute);
  mailbox->Process();
  assert(calls == 0);
}

int main()
{
  close_waits_for_original_callback();
  close_from_nonowner_cancels_queued();
  explicit_main_owner();
  advancing_original_session();
  wrong_thread_rejection();
  bounded_dispatch_per_pump();
  pending_continuation();
  delayed_and_exactly_once();
  bounded_backpressure();
  active_request_is_pinned();
  replaced_session_and_late_dispatch();
  failed_and_exceptional_actions();
  own_mailbox_progress_during_join();
  Mailbox::ProcessAll(); // Only expired/closed mailboxes remain.
}
'''


def run_harness(header=None, expect_failure=False, source_text=None):
    with tempfile.TemporaryDirectory(prefix='render-lifecycle-') as temporary:
        out = Path(temporary)
        if header is not None:
            destination = out / HEADER
            destination.parent.mkdir(parents=True)
            destination.write_text(header)
        source = out / 'test.cpp'
        source.write_text(HARNESS if source_text is None else source_text)
        binary = out / 'test'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        str(source), '-o', str(binary)], check=True)
        result = subprocess.run([str(binary)], capture_output=expect_failure,
                                text=True, timeout=20)
        if expect_failure:
            assert result.returncode != 0, 'negative control unexpectedly passed'
            assert 'Assertion' in result.stderr, result.stderr
        else:
            result.check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--native-only', action='store_true', help='Only retained native renderer requests')
    parser.add_argument('--close-only', action='store_true', help='Only changed close/retirement behavior')
    options = parser.parse_args()
    if options.native_only:
        source = caller_harness()
        source = source[:source.rindex('int main()')] + 'int main() { production_native_admission(); }'
        source = source.replace('static void ', '[[maybe_unused]] static void ')
        run_harness(source_text=source)
        print('Native renderer requests: PASS (ASan/UBSan)')
        if options.negative_controls:
            for label, before, after in [
                ('bypass native admission', 'if (!native->TryBegin())', 'if (false && !native->TryBegin())'),
                ('complete pending admission', 'return std::nullopt;', 'return false;'),
                ('skip failed-init cleanup', 'if (!result)\n        UnInitOnMain();', 'if (false)\n        UnInitOnMain();'),
                ('skip exception cleanup', 'catch (...)\n      {\n        UnInitOnMain();', 'catch (...)\n      {'),
                ('hold native through display', 'native.reset(); // Display admission', '/* retain native */ // Display admission'),
                ('lose partial-open cleanup', 'm_dvOpened = true;', 'm_dvOpened = false;'),
                ('ignore superseding close', 'if (m_closing)', 'if (false)'),
            ]:
                assert before in source, label
                run_harness(source_text=source.replace(before, after), expect_failure=True)
                print('Negative control rejected at runtime:', label)
        return
    if options.close_only:
        source = caller_harness()
        source = source[:source.rindex('int main()')] + 'int main() { production_close_join(); }'
        # Other production wrapper tests remain compiled but are not rerun here.
        source = source.replace('static void ', '[[maybe_unused]] static void ')
        run_harness(source_text=source)
        print('Player close: PASS (production CloseFile/RequestUnInit; ASan/UBSan)')
        if options.negative_controls:
            for label, before, after in [
                ('skip thread exit', 'if (IsRunning() || m_outboundEvents->IsProcessing())', 'if (false)'),
                ('skip outbound retirement', 'IsRunning() || m_outboundEvents->IsProcessing()', 'IsRunning()'),
                ('replace retained receipt', 'if (!m_closeReceipt)', 'if (true)'),
                ('skip exact acknowledgment', 'if (!m_closeReceipt || !m_closeReceipt->Wait(0ms))', 'if (!m_closeReceipt)'),
            ]:
                assert before in source
                run_harness(source_text=source.replace(before, after), expect_failure=True)
                print('Negative control rejected at runtime:', label)
        return
    run_harness()
    run_harness(source_text=caller_harness())
    print('Render lifecycle: PASS (real mailbox, Configure/wrappers/CloseFile; ASan/UBSan; real recursive gfx lock, recording renderer/thread stubs)')
    if options.negative_controls:
        source = (ROOT / 'xbmc' / HEADER).read_text()
        for label, before, after in [
            ('finish pending request', 'if (!complete)', 'if (false)'),
            ('capacity', 'm_requests.size() + (m_executing ? 1 : 0) >= 4', 'm_requests.size() + (m_executing ? 1 : 0) >= 5'),
            ('owner rejection', 'if (std::this_thread::get_id() != m_owner)', 'if (false)'),
            ('dispatch budget', 'dispatched < 4', 'dispatched < 5'),
            ('false completion', '*complete ? Status::COMPLETED : Status::FAILED',
             '*complete ? Status::COMPLETED : Status::COMPLETED'),
            ('cancel acknowledgement', 'request->Finish(Status::CANCELLED);',
             'request->Finish(Status::COMPLETED);'),
            ('timeout result', 'return status == Status::COMPLETED;', 'return true;'),
            ('close quiescence', 'm_idle.wait(lock, [&] { return !m_executing; });', ''),
        ]:
            assert source.count(before) >= 1, label
            run_harness(source.replace(before, after), expect_failure=True)
            print(f'Negative control rejected at runtime: {label}')

        source = caller_harness()
        for label, before, after in [
            ('original lifecycle generation', 'if (generation != m_lifecycleGeneration)',
             'if (false)'),
            ('original flush flags', 'auto request = RequestFlush(saveBuffers);',
             'auto request = RequestFlush(true);'),
            ('graphics held during acknowledgment',
             'CSingleExit graphics(CServiceBroker::GetWinSystem()->GetGfxContext());', ''),
            ('delayed configure payload corrupted', 'payload->CopyRef(picture);',
             'payload->CopyRef(picture); payload->iWidth += 1;'),
            ('failed configure accepted as unchanged', 'm_renderState == STATE_CONFIGURED && ', ''),
            ('failed configure leaves presentation pending',
             'm_renderState = STATE_UNCONFIGURED;\n    m_presentstep = PRESENT_IDLE;',
             'm_renderState = STATE_UNCONFIGURED;'),
        ]:
            assert before in source, label
            run_harness(source_text=source.replace(before, after), expect_failure=True)
            print(f'Negative control rejected at runtime: {label}')




def caller_harness():
    function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
    source = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    methods = '\n'.join(function(source, signature) for signature in [
        'void CRenderManager::ProcessLifecycleRequests()',
        'bool CRenderManager::PreInit()', 'bool CRenderManager::UnInit()',
        'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestUnInit()',
        'bool CRenderManager::Configure(const VideoPicture&',
        'bool CRenderManager::Flush(bool wait, bool saveBuffers)',
        'std::shared_ptr<CRenderLifecycle::Request> CRenderManager::RequestFlush('])
    player = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    close = function(player, 'bool CVideoPlayer::CloseFile(bool reopen)')
    configure = function(source, 'bool CRenderManager::Configure()')
    creation = configure[configure.index('    CreateRenderer();'):]
    creation_failure = function(creation, 'if (!m_pRenderer)')
    configuration_failure = function(configure, '  else\n  {')
    prelude = CALLER_PRELUDE.replace('@CREATE_FAILED@', creation_failure)
    prelude = prelude.replace('@CONFIGURE_FAILED@', configuration_failure)
    gles = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/LinuxRendererGLES.cpp').read_text()
    gles_config = function(gles, 'bool CLinuxRendererGLES::Configure(')
    gles_close = function(gles, 'void CLinuxRendererGLES::UnInit()')
    prelude = prelude.replace('@GLES_OPEN@', function(gles_config, 'if (!m_dvOpened)'))
    prelude = prelude.replace('@GLES_CLOSE@', function(gles_close, 'if (m_dvOpened)'))
    assert 'UpdateResolution();' not in configure  # Display follows native release in the request.
    return prelude + methods + close + CALLER_TESTS


CALLER_PRELUDE = r'''
#include <algorithm>
#include "cores/VideoPlayer/VideoRenderers/RenderLifecycle.h"
#include "windowing/amlogic/AMLNativeTransaction.h"
#include <cassert>
#include <future>
#include <string>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
const auto mainThread = std::this_thread::get_id();
std::vector<std::string> trace;
struct Messenger
{
  bool IsProcessThread() { return std::this_thread::get_id() == mainThread; }
};
struct RESOLUTION_INFO { int iScreenWidth{1920}, iScreenHeight{1080}; float fPixelRatio{1.0f}; };
struct Graphics
{
  std::recursive_timed_mutex mutex;
  static inline thread_local int depth{0}, released{0};
  void lock() { mutex.lock(); ++depth; }
  void unlock() { assert(depth > 0); --depth; mutex.unlock(); }
  bool try_lock_for(std::chrono::milliseconds timeout)
  { if (!mutex.try_lock_for(timeout)) return false; ++depth; return true; }
  int GetVideoResolution() { return 0; }
  RESOLUTION_INFO GetResInfo(int) { return {}; }
};
using CGraphicContext = Graphics;
struct Window { Graphics graphics; Graphics& GetGfxContext() { return graphics; } };
struct Cache { void Reset() { trace.push_back("cache-reset"); } };
enum class StreamHdrType {NONE};
struct CStreamDetails { static int DynamicRangeToString(StreamHdrType) { return 0; } };
struct VideoPicture
{
  int iWidth{0}, iHeight{0}, iDisplayWidth{0}, iDisplayHeight{0};
  StreamHdrType hdrType{StreamHdrType::NONE};
  int colorBits{10}, color_primaries{1};
  std::string stereoMode;
  std::shared_ptr<int> pixels;
  bool IsSameParams(const VideoPicture& other) const
  { return iWidth == other.iWidth && iHeight == other.iHeight; }
  void CopyRef(const VideoPicture& other) { *this = other; }
  void SetParams(const VideoPicture& other) { *this = other; pixels.reset(); }
};
struct Settings { int GetInt(int) { return 0; } };
struct SettingsComponent { Settings* GetSettings() { static Settings value; return &value; } };
struct CSettings { static constexpr int SETTING_VIDEOPLAYER_ADJUSTREFRESHRATE = 1; };
constexpr int ADJUST_REFRESHRATE_OFF = 0;
struct CResolutionUtils { static int ChooseBestResolution(float,int,int,bool) { return 0; } };
struct ClockSync { void Reset() {} };
struct Clock { void SetVsyncAdjust(int) {} };
int nativeOpens=0, nativeCloses=0;bool throwNativeOpen=false;
void aml_dv_open(StreamHdrType,int,int,bool software){assert(software);++nativeOpens;if(throwNativeOpen)throw 8;}
void aml_dv_close(){++nativeCloses;}
struct RenderBackend {
 bool m_dvOpened=false;
 bool ConfigChanged(const VideoPicture&) { return false; }
 void ConfigureNative(const VideoPicture& picture) { @GLES_OPEN@ }
 void CloseNative() { @GLES_CLOSE@ }
};
struct PresentEvent
{
  template<class T> void wait(T& lock, std::chrono::milliseconds duration)
  { lock.unlock(); std::this_thread::sleep_for(std::min(duration, 1ms)); lock.lock(); }
  int notifications{0};
  void notifyAll() { ++notifications; }
};
namespace XbmcThreads {
template<class T=void> struct EndTime
{
  std::chrono::steady_clock::time_point end;
  explicit EndTime(std::chrono::milliseconds timeout) : end(std::chrono::steady_clock::now()+timeout) {}
  bool IsTimePast() { return std::chrono::steady_clock::now() >= end; }
  auto GetTimeLeft() { return std::chrono::duration_cast<std::chrono::milliseconds>(end-std::chrono::steady_clock::now()); }
};
}

struct CServiceBroker
{
  static SettingsComponent* GetSettingsComponent() { static SettingsComponent value; return &value; }
  static Messenger* GetAppMessenger() { static Messenger value; return &value; }
  static Window* GetWinSystem() { static Window value; return &value; }
  static Cache& GetDataCacheCore() { static Cache value; return value; }
};
struct CSingleExit
{
  Graphics& graphics;
  int depth;
  explicit CSingleExit(Graphics& value) : graphics(value), depth(value.depth)
  { ++graphics.released; for (int i=0;i<depth;++i) graphics.unlock(); }
  ~CSingleExit() { for (int i=0;i<depth;++i) graphics.lock(); --graphics.released; }
};
constexpr int LOGINFO = 1, LOGDEBUG = 2, LOGWARNING = 3;
struct CLog { template<class... T> static void Log(T&&...) {} };
struct CRenderManager
{
  std::shared_ptr<CRenderLifecycle> m_lifecycle{CRenderLifecycle::Create()};
  std::atomic<bool> m_closing{false};
  std::recursive_mutex m_statelock;
  uint64_t m_lifecycleGeneration{0};
  bool flushResult{true};
  enum { PRESENT_IDLE, PRESENT_READY, STATE_UNCONFIGURED, STATE_CONFIGURING, STATE_CONFIGURED };
  std::shared_ptr<CRenderLifecycle::Request> m_configRequest;
  VideoPicture m_picture;
  std::unique_ptr<VideoPicture> m_pConfigPicture;
  float m_fps{0}; unsigned m_orientation{0}; int m_NumberBuffers{0};
  std::recursive_mutex m_presentlock;
  PresentEvent m_presentevent;
  ClockSync m_clockSync;
  Clock m_dvdClock;
  int m_presentstep{PRESENT_IDLE}, m_renderState{STATE_UNCONFIGURED};
  bool m_forceNext{false}, m_bRenderGUI{true}, m_bTriggerUpdateResolution{false};
  CAMLSession* nativeSession{nullptr};
  bool m_configuredFramePending{false}, configureResult{true}, createResult{true}, throwConfigure{false};
  void UpdateResolution() { CAMLNativeTransaction check; assert(check.TryBegin()); }
  RenderBackend backend;
  RenderBackend* m_pRenderer{nullptr};
  int configurations{0}, invalidations{0};
  VideoPicture observed;
  void InvalidateReservations() { ++invalidations; }
  void CancelDeferredDV() {}
  bool Configure(const VideoPicture&,float,unsigned,StreamHdrType,int);
  bool Configure()
  {
    assert(std::this_thread::get_id() == mainThread);
    if (nativeSession) assert(!nativeSession->AcquireDecoder());
    ++configurations;
    observed = *m_pConfigPicture;
    m_pRenderer = createResult ? &backend : nullptr;
    @CREATE_FAILED@
    m_pRenderer->ConfigureNative(*m_pConfigPicture);
    if (throwConfigure) throw 7;
    if (configureResult)
    {
      m_presentstep = PRESENT_IDLE;
      m_renderState = STATE_CONFIGURED;
    }
    @CONFIGURE_FAILED@
    m_pConfigPicture.reset();
    return configureResult;
  }
  int preInitCalls{0}, unInitCalls{0};
  std::vector<bool> flags;
  bool PreInit();
  bool UnInit();
  std::shared_ptr<CRenderLifecycle::Request> RequestUnInit();
  bool Flush(bool wait, bool saveBuffers);
  std::shared_ptr<CRenderLifecycle::Request> RequestFlush(bool saveBuffers, bool newSession = false);
  void ProcessLifecycleRequests();
  void PreInitOnMain()
  {
    assert(std::this_thread::get_id() == mainThread);
    ++preInitCalls;
    trace.push_back("preinit");
  }
  void UnInitOnMain()
  {
    assert(std::this_thread::get_id() == mainThread);
    if (nativeSession) assert(!nativeSession->AcquireDecoder());
    ++unInitCalls;
    if (m_pRenderer) m_pRenderer->CloseNative();
    m_pRenderer = nullptr;
    m_pConfigPicture.reset();
    trace.push_back("uninit");
  }
  bool FlushOnMain(bool saveBuffers)
  {
    assert(std::this_thread::get_id() == mainThread);
    flags.push_back(saveBuffers);
    trace.push_back(saveBuffers ? "flush-save" : "flush-drop");
    return flushResult;
  }
};
struct Aborter
{
  std::string name;
  void Abort() { trace.push_back(name); }
};
struct Edl { void Clear() { trace.push_back("edl-clear"); } };
struct ProcessInfo { void SetDataCache(Cache*) { trace.push_back("set-cache"); } };
struct CVideoPlayer
{
  struct { void Invalidate() {} } m_vs10Action;
  CRenderManager m_renderManager;
  bool m_bAbortRequest{false}, m_bCloseRequest{false};
  bool m_HasVideo{true}, m_HasAudio{true};
  Aborter demux{"abort-demux"}, subtitle{"abort-subtitle"}, input{"abort-input"};
  Aborter* m_pDemuxer{&demux};
  Aborter* m_pSubtitleDemuxer{&subtitle};
  Aborter* m_pInputStream{&input};
  Edl m_Edl;
  ProcessInfo process;
  ProcessInfo* m_processInfo{&process};
  enum class CloseStage { NONE, INITIAL_RENDERER, THREAD, FINAL_RENDERER, COMPLETE };
  CloseStage m_closeStage{CloseStage::NONE};
  std::shared_ptr<CRenderLifecycle::Request> m_closeReceipt;
  struct Events {bool busy=false;bool IsProcessing()const{return busy;}} events;
  Events* m_outboundEvents{&events};
  bool running{false};
  int joins{0}, stopped{0};
  std::shared_ptr<CRenderLifecycle::Request> exitRequest;
  bool CloseFile(bool reopen);
  void StopThread(bool wait)
  {
    assert(CServiceBroker::GetWinSystem()->graphics.released == 1);
    trace.push_back(wait ? "stop-join" : "stop-request");
    ++stopped;
    if (wait)
      assert(!running);
  }
  bool IsRunning() { return running; }

};
'''

CALLER_TESTS = r'''
static void production_wrappers()
{
  CRenderManager manager;
  assert(manager.PreInit() && manager.preInitCalls == 1);
  assert(manager.Flush(true, false));
  assert(manager.Flush(false, true));
  assert((manager.flags == std::vector<bool>{false, true}));
  manager.flushResult = false;
  assert(!manager.Flush(true, false));
  assert(manager.flags.size() == 3);
  manager.flushResult = true;

  // Run the actual fixed-duration waits without dispatch: neither timeout nor
  // asynchronous return authorizes caller-thread execution or success.
  std::thread caller([&] {
    assert(!manager.PreInit());
    assert(!manager.UnInit());
    assert(!manager.Flush(false, false));
  });
  caller.join();
  assert(manager.preInitCalls == 1 && manager.unInitCalls == 0 && manager.flags.size() == 3);
  manager.ProcessLifecycleRequests();
  assert(manager.preInitCalls == 1 && manager.unInitCalls == 1);
  assert(manager.m_closing); // Older delayed PreInit must not reopen newer UnInit.
  assert(manager.flags.size() == 4 && !manager.flags.back());
  manager.ProcessLifecycleRequests();
  assert(manager.preInitCalls == 1 && manager.unInitCalls == 1 && manager.flags.size() == 4);
  assert(manager.m_lifecycle->Close());
  assert(!manager.PreInit() && !manager.UnInit() && !manager.Flush(true, true));
  assert(manager.preInitCalls == 1 && manager.unInitCalls == 1 && manager.flags.size() == 4);
}

static void production_close_join()
{
  trace.clear();
  CVideoPlayer player;
  CRenderManager replacement;
  auto unrelated = replacement.m_lifecycle->Submit([&] { replacement.PreInitOnMain(); return true; });
  player.running = true;
  assert(!player.CloseFile(false));
  auto initial = player.m_closeReceipt;
  assert(initial && player.stopped==0 && player.m_HasVideo);
  for(int frame=0;frame<3;++frame) {
    assert(!player.CloseFile(false));
    assert(player.stopped==1 && player.m_HasVideo);
  }
  assert(initial->status==CRenderLifecycle::Status::COMPLETED);
  assert(player.m_renderManager.unInitCalls==1);
  // OnExit runs off-main and publishes to the retained original renderer.
  player.exitRequest = player.m_renderManager.RequestFlush(false);
  player.running = false;
  player.events.busy=true;
  assert(!player.CloseFile(false) && player.stopped==1);
  assert(player.exitRequest->Wait(0ms));
  player.events.busy=false;
  assert(!player.CloseFile(false) && player.stopped==2);
  assert(!player.CloseFile(false));
  auto final=player.m_closeReceipt;
  assert(final && final!=initial);
  assert(player.CloseFile(false));
  assert(player.CloseFile(false)); // Complete is idempotent, no duplicate uninit/join.
  assert(player.stopped==2 && player.m_renderManager.unInitCalls==2);
  assert(!player.m_HasVideo && !player.m_HasAudio);
  assert((player.m_renderManager.flags == std::vector<bool>{false}));
  assert(replacement.preInitCalls == 0 && unrelated->status == CRenderLifecycle::Status::PENDING);
  assert((std::vector<std::string>(trace.begin(), trace.begin() + 3) ==
          std::vector<std::string>{"abort-demux", "abort-subtitle", "abort-input"}));
  assert(std::count(trace.begin(),trace.end(),"abort-input")==1);
  assert(std::count(trace.begin(),trace.end(),"edl-clear")==1);
  assert(CServiceBroker::GetWinSystem()->graphics.released == 0);
  assert(player.m_renderManager.m_lifecycle->Close() && replacement.m_lifecycle->Close());

  CVideoPlayer neverStarted;
  for(int frame=0;frame<5 && !neverStarted.CloseFile(false);++frame) {}
  assert(neverStarted.CloseFile(false) && neverStarted.stopped==2);
  assert(neverStarted.m_renderManager.m_lifecycle->Close());

  // Rejected/cancelled requests must keep the original owner alive indefinitely.
  for(auto status:{CRenderLifecycle::Status::FAILED,CRenderLifecycle::Status::CANCELLED}) {
    CVideoPlayer failed;
    assert(!failed.CloseFile(false));
    auto original=failed.m_closeReceipt;
    failed.m_renderManager.ProcessLifecycleRequests();
    original->status=status;
    for(int frame=0;frame<3;++frame) {
      assert(!failed.CloseFile(false));
      assert(failed.m_closeReceipt==original && failed.m_HasVideo && failed.stopped==0);
    }
    assert(failed.m_renderManager.m_lifecycle->Close());
  }
}


static void production_session_requests()
{
  CRenderManager manager;
  assert(manager.PreInit());
  auto stale = manager.RequestFlush(true);
  assert(stale && stale->status == CRenderLifecycle::Status::PENDING);
  assert(manager.UnInit()); // A newer lifecycle generation rejects stale work.
  assert(stale->status == CRenderLifecycle::Status::FAILED && manager.flags.empty());
  assert(manager.m_closing);
  assert(manager.PreInit() && !manager.m_closing);

  auto transition = manager.RequestFlush(false, true);
  auto queuedOld = manager.RequestFlush(true);
  assert(transition && queuedOld && manager.m_closing);
  assert(transition->session == queuedOld->session);
  assert(!transition->Wait(0ms));
  manager.ProcessLifecycleRequests();
  assert(transition->Wait(0ms) && !manager.m_closing);
  assert(queuedOld->status == CRenderLifecycle::Status::CANCELLED);
  assert((manager.flags == std::vector<bool>{false}));
  auto fresh = manager.RequestFlush(true);
  assert(fresh && fresh->session > transition->session);
  manager.ProcessLifecycleRequests();
  assert(fresh->Wait(0ms) && (manager.flags == std::vector<bool>{false, true}));

  manager.flushResult = false;
  auto failedTransition = manager.RequestFlush(false, true);
  manager.ProcessLifecycleRequests();
  assert(!failedTransition->Wait(0ms));
  assert(failedTransition->status == CRenderLifecycle::Status::FAILED && manager.m_closing);
  assert(manager.m_lifecycle->Close());
}

static VideoPicture picture(int width)
{
  VideoPicture result;
  result.iWidth = result.iDisplayWidth = width;
  result.iHeight = result.iDisplayHeight = 1080;
  result.pixels = std::make_shared<int>(42);
  return result;
}

static void production_native_admission()
{
  auto display = CAMLSession::FenceDisplay();
  assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display, CAMLSession::DisplayPhase::READY));
  CAMLSession session; auto stream = session.Fence();
  assert(session.BeginMutation(stream) && session.Complete(stream, true));
  auto input = picture(1920);
  {
    CRenderManager manager; manager.nativeSession = &session;
    auto permit = std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
    assert(!manager.Configure(input, 24.0f, 0, StreamHdrType::NONE, 3));
    auto configure = manager.m_configRequest;
    assert(configure && configure->status == CRenderLifecycle::Status::PENDING);
    assert(configure->execute && manager.configurations == 0 && !manager.m_pRenderer);
    auto dependent = manager.RequestFlush(true);
    manager.ProcessLifecycleRequests();
    assert(!configure->Wait(0ms) && manager.flags.empty());
    permit.reset(); manager.ProcessLifecycleRequests();
    assert(configure->Wait(0ms) && dependent->Wait(0ms) && manager.configurations == 1);
    assert(manager.observed.iWidth == 1920 && session.AcquireDecoder());
    permit = std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
    auto close = manager.RequestUnInit(); manager.ProcessLifecycleRequests();
    assert(close->status == CRenderLifecycle::Status::PENDING && manager.unInitCalls == 0);
    assert(manager.m_pRenderer && !close->Wait(0ms));
    permit.reset(); manager.ProcessLifecycleRequests();
    assert(close->Wait(0ms) && manager.unInitCalls == 1 && !manager.m_pRenderer);
    assert(manager.m_lifecycle->Close() && session.AcquireDecoder());
  }
  {
    CRenderManager manager; manager.nativeSession = &session;
    auto permit = std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
    assert(!manager.Configure(input, 24.0f, 0, StreamHdrType::NONE, 3));
    auto abandoned = manager.m_configRequest;
    auto close = manager.RequestUnInit(); manager.ProcessLifecycleRequests();
    assert(abandoned->status == CRenderLifecycle::Status::FAILED && !abandoned->execute);
    assert(manager.configurations == 0 && close->status == CRenderLifecycle::Status::PENDING);
    permit.reset(); manager.ProcessLifecycleRequests();
    assert(close->Wait(0ms) && manager.configurations == 0 && manager.unInitCalls == 1);
    assert(manager.m_lifecycle->Close());
  }
  {
    CRenderManager manager; manager.nativeSession = &session;
    auto permit = std::make_unique<CAMLSession::Permit>(session.AcquireDecoder());
    assert(!manager.Configure(input,24.0f,0,StreamHdrType::NONE,3));
    auto cancelled=manager.m_configRequest; manager.m_closing=true;
    manager.ProcessLifecycleRequests();
    assert(cancelled->status==CRenderLifecycle::Status::FAILED && manager.configurations==0);
    assert(session.AcquireDecoder() && manager.m_lifecycle->Close());
  }
  for (int failure : {0, 1, 2})
  {
    const bool throws = failure != 0;
    nativeOpens=nativeCloses=0;throwNativeOpen=failure==2;
    CRenderManager manager; manager.nativeSession = &session;
    manager.configureResult = false; manager.throwConfigure = failure==1;
    try { assert(!manager.Configure(input,24.0f,0,StreamHdrType::NONE,3)); assert(!throws); }
    catch (int) { assert(throws); }
    assert(manager.m_configRequest->status == CRenderLifecycle::Status::FAILED);
    assert(manager.unInitCalls == 1 && !manager.m_pRenderer && !manager.m_pConfigPicture);
    assert(nativeOpens==1 && nativeCloses==1 && !manager.backend.m_dvOpened);
    assert(session.AcquireDecoder() && manager.m_lifecycle->Close());
  }
  throwNativeOpen=false;
}

static void production_configure_request()
{
  CRenderManager manager;
  auto input = picture(1920);
  auto original = input;
  bool returned = true;
  std::thread caller([&] { returned = manager.Configure(input, 24.0f, 90, StreamHdrType::NONE, 3); });
  caller.join();
  assert(!returned && manager.configurations == 0);
  auto request = manager.m_configRequest;
  assert(request && request->status == CRenderLifecycle::Status::PENDING);
  assert(manager.m_renderState == CRenderManager::STATE_CONFIGURING);
  input = picture(720); // Original stack/value reuse cannot rewrite delayed work.
  assert(!manager.Configure(input, 60.0f, 180, StreamHdrType::NONE, 2));
  assert(manager.m_configRequest == request && manager.configurations == 0);
  manager.ProcessLifecycleRequests();
  assert(request->Wait(0ms) && manager.configurations == 1);
  assert(manager.observed.iWidth == original.iWidth && manager.observed.pixels == original.pixels);
  assert(manager.m_fps == 24.0f && manager.m_orientation == 90 && manager.m_NumberBuffers == 3);
  assert(manager.m_configuredFramePending);
  assert(manager.Configure(original, 24.0f, 90, StreamHdrType::NONE, 3));
  assert(manager.configurations == 1 && !manager.m_configRequest);
  assert(manager.m_lifecycle->Close());

  CRenderManager full;
  for (int i=0;i<4;++i) assert(full.m_lifecycle->Submit([] { return true; }));
  assert(!full.Configure(original, 24.0f, 0, StreamHdrType::NONE, 3));
  assert(full.configurations == 0 && !full.m_configRequest && full.invalidations == 0);
  assert(full.m_lifecycle->Close());

  for (bool closing : {false, true})
  {
    CRenderManager stale;
    std::thread delayed([&] { assert(!stale.Configure(original, 24.0f, 0, StreamHdrType::NONE, 3)); });
    delayed.join();
    auto old = stale.m_configRequest;
    // Exercise both later generation and later close using production wrappers.
    if (closing) assert(stale.UnInit());
    else assert(stale.PreInit());
    assert(old->status == CRenderLifecycle::Status::FAILED);
    assert(stale.configurations == 0 && !stale.m_configuredFramePending);
    assert(stale.m_closing == closing);
    assert(stale.m_lifecycle->Close());
  }

  CRenderManager failed;
  failed.configureResult = false;
  assert(!failed.Configure(original, 24.0f, 0, StreamHdrType::NONE, 3));
  assert(failed.m_configRequest->status == CRenderLifecycle::Status::FAILED);
  assert(failed.configurations == 1 && !failed.m_configuredFramePending);
  assert(failed.m_presentstep == CRenderManager::PRESENT_IDLE && failed.m_presentevent.notifications == 2);
  // A matching retry cannot convert an explicitly failed main completion into success.
  assert(!failed.Configure(original, 24.0f, 0, StreamHdrType::NONE, 3));
  assert(!failed.m_configuredFramePending);
  assert(!failed.Configure(original, 25.0f, 0, StreamHdrType::NONE, 3));
  assert(!failed.m_configuredFramePending && failed.configurations == 3);
  failed.configureResult = true;
  assert(failed.Configure(original, 25.0f, 0, StreamHdrType::NONE, 3));
  assert(failed.configurations == 4 && failed.m_configuredFramePending);
  assert(failed.m_configRequest->Wait(0ms));
  assert(failed.m_lifecycle->Close());

  CRenderManager creation;
  creation.createResult = false;
  assert(!creation.Configure(original,24.0f,0,StreamHdrType::NONE,3));
  assert(!creation.m_pConfigPicture && !creation.m_pRenderer);
  assert(creation.m_renderState == CRenderManager::STATE_UNCONFIGURED);
  assert(creation.m_presentstep == CRenderManager::PRESENT_IDLE && creation.m_presentevent.notifications == 2);
  creation.createResult = true;
  assert(creation.Configure(original,24.0f,0,StreamHdrType::NONE,3));
  assert(creation.configurations == 2 && creation.m_configRequest->Wait(0ms));
  assert(creation.m_lifecycle->Close());
}

static void graphics_release_while_waiting()
{
  for (int operation=0;operation<4;++operation)
  {
    CRenderManager manager;
    auto input = picture(1920);
    auto& gfx = CServiceBroker::GetWinSystem()->graphics;
    std::promise<void> locked;
    auto lockedFuture = locked.get_future();
    std::promise<bool> finished;
    auto finishedFuture = finished.get_future();
    std::thread caller([&] {
      gfx.lock(); gfx.lock();
      locked.set_value();
      bool result = false;
      if (operation == 0) result = manager.PreInit();
      if (operation == 1) result = manager.UnInit();
      if (operation == 2) result = manager.Flush(true, false);
      if (operation == 3) result = manager.Configure(input,24.0f,0,StreamHdrType::NONE,3);
      assert(gfx.depth == 2); // The complete recursive depth is restored.
      gfx.unlock(); gfx.unlock();
      finished.set_value(result);
    });
    assert(lockedFuture.wait_for(5s) == std::future_status::ready);
    assert(gfx.try_lock_for(250ms)); // Main must acquire it before caller timeout.
    const auto deadline = std::chrono::steady_clock::now() + 500ms;
    while (manager.preInitCalls + manager.unInitCalls + manager.flags.size() + manager.configurations == 0)
    {
      assert(std::chrono::steady_clock::now() < deadline);
      manager.ProcessLifecycleRequests();
      std::this_thread::yield();
    }
    gfx.unlock();
    assert(finishedFuture.wait_for(1s) == std::future_status::ready && finishedFuture.get());
    caller.join();
    assert(manager.m_lifecycle->Close());
  }
}

int main()
{
  graphics_release_while_waiting();
  production_native_admission();
  production_configure_request();
  production_session_requests();
  production_wrappers();
  production_close_join();
  CRenderLifecycle::ProcessAll();
}
'''

if __name__ == '__main__':
    main()
