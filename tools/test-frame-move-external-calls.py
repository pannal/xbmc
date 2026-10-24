#!/usr/bin/env python3
"""Exercise the production external-call admission block and guard methods.

Uses Kodi's CountingLockable/CSingleExit with host std::recursive_mutex, real
caller threads and a recording sleep fixture. Deterministic handoff cases do
not measure elapsed time; repeated bursts use real sleeps only to check progress.
This does not establish target scheduling, Python throughput or display timing.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'xbmc/application/Application.cpp'
BASELINE = '6621f6f2be26087b510bc36dbfcefe8c18ecd846'


def block(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda m: ' ' * len(m.group()), source)
    brace = masked.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


FIXTURE = r'''
#include "threads/SingleLock.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
static std::atomic_uint checks{0};
static unsigned sleepCalls = 0;
static bool realSleep = false;
static std::function<void()> duringSleep;
static void Check(bool value, const char* message)
{
  ++checks;
  if (!value) { std::fprintf(stderr, "FAIL: %s\n", message); std::exit(1); }
}
template<class P> static void Await(P predicate, const char* message)
{
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (!predicate())
  {
    Check(std::chrono::steady_clock::now() < deadline, message);
    std::this_thread::sleep_for(100us);
  }
}
struct Critical : CCriticalSection { unsigned Depth() const { return count; } };
struct Window
{
  Critical graphics;
  Critical& GetGfxContext() { return graphics; }
};
static Window window;
struct CServiceBroker { static Window* GetWinSystem() { return &window; } };
struct Player
{
  bool video = false, paused = false;
  bool IsPlayingVideo() const { return video; }
  bool IsPausedPlayback() const { return paused; }
};
namespace KODI::TIME
{
template<class T> void Sleep(T duration)
{
  ++sleepCalls;
  Check(duration == 2ms, "requested admission exceeds existing 2ms minimum");
  if (duringSleep) duringSleep();
  if (realSleep) std::this_thread::sleep_for(duration);
}
}
class CApplication
{
public:
  Critical m_frameMoveGuard;
  std::atomic_uint m_WaitingExternalCalls{0};
  // Retained only so the unmodified historical methods can also execute.
  unsigned m_ProcessedExternalCalls = 0, m_ProcessedExternalDecay = 0;
  void LockFrameMoveGuard();
  void UnlockFrameMoveGuard();
  void Admission(Player* appPlayer) { (void)appPlayer; @ADMISSION@ }
};
@LOCK@
@UNLOCK@
static void Own(CApplication& app, unsigned graphicsDepth)
{
  app.m_frameMoveGuard.lock();
  for (unsigned i = 0; i < graphicsDepth; ++i) window.graphics.lock();
}
static void Release(CApplication& app, unsigned graphicsDepth)
{
  Check(app.m_frameMoveGuard.Depth() == 1, "main frame guard restored exactly once");
  Check(window.graphics.Depth() == graphicsDepth, "graphics recursion restored exactly");
  for (unsigned i = 0; i < graphicsDepth; ++i) window.graphics.unlock();
  app.m_frameMoveGuard.unlock();
}
int main()
{
  CApplication app;
  Player player;
  Own(app, 3);
  app.Admission(&player);
  Check(sleepCalls == 0, "no waiting calls must not sleep");
  Release(app, 3);

  // Build real request history, rather than assigning the old counter directly.
  for (unsigned i = 0; i < 400; ++i)
  { app.LockFrameMoveGuard(); app.UnlockFrameMoveGuard(); }
  Check(app.m_WaitingExternalCalls == 0, "completed history is not pending work");

  // Test no-video, playing-video and paused-video policy without changing callers.
  for (unsigned mode = 0; mode < 3; ++mode)
  {
    player.video = mode != 0; player.paused = mode == 2;
    Own(app, 3);
    std::atomic<bool> entered{false}, finish{false}, done{false};
    std::thread caller([&] {
      app.LockFrameMoveGuard();
      app.LockFrameMoveGuard();
      entered = true;
      while (!finish) std::this_thread::yield();
      app.UnlockFrameMoveGuard();
      Check(app.m_WaitingExternalCalls == 1, "nested caller remains counted");
      // Main must wait for this outer acquisition, even after its sleep returns.
      std::this_thread::sleep_for(5ms);
      done = true;
      app.UnlockFrameMoveGuard();
    });
    Await([&] { return app.m_WaitingExternalCalls != 0; }, "caller queued");
    Check(!entered, "main guard excludes caller before admission");
    duringSleep = [&] {
      Await([&] { return entered.load(); }, "both guards must admit nested caller");
      Check(app.m_WaitingExternalCalls == 2, "nested calls are counted separately");
      finish = true;
    };
    app.Admission(&player);
    Check(done, "main waits for active caller after the requested window");
    caller.join();
    duringSleep = {};
    Check(app.m_WaitingExternalCalls == 0, "all nested callers retired");
    Release(app, 3);
  }

  // Repeated real concurrent bursts: no FIFO or throughput expectation.
  realSleep = true;
  unsigned frames = 0;
  for (unsigned round = 0; round < 4; ++round)
  {
    Own(app, 1);
    std::atomic<unsigned> ready{0}, completed{0};
    std::vector<std::thread> callers;
    for (unsigned worker = 0; worker < 8; ++worker)
      callers.emplace_back([&] {
        ++ready;
        for (unsigned i = 0; i < 300; ++i)
        {
          app.LockFrameMoveGuard();
          if (i % 5 == 0) app.LockFrameMoveGuard();
          ++completed;
          if (i % 5 == 0) app.UnlockFrameMoveGuard();
          app.UnlockFrameMoveGuard();
        }
      });
    Await([&] { return ready == 8 && app.m_WaitingExternalCalls == 8; },
          "burst callers queued behind main guard");
    const auto deadline = std::chrono::steady_clock::now() + 3s;
    do
    {
      app.Admission(&player);
      Check(app.m_frameMoveGuard.Depth() == 1 && window.graphics.Depth() == 1,
            "main owns both guards between openings");
      ++frames;
      Check(std::chrono::steady_clock::now() < deadline, "caller burst makes progress");
    } while (completed != 2400 || app.m_WaitingExternalCalls != 0);
    Release(app, 1);
    for (auto& caller : callers) caller.join();
    Check(completed == 2400, "all external calls complete");
  }
  std::printf("{\"checks\":%u,\"sleep_calls\":%u,\"main_openings\":%u,"
              "\"burst_calls\":9600,\"recursive_burst_calls\":1920}\n",
              checks.load(), sleepCalls, frames);
}
'''


def generate(source):
    admission = block(source, 'if (m_WaitingExternalCalls)')
    return (FIXTURE.replace('@ADMISSION@', admission)
            .replace('@LOCK@', block(source, 'void CApplication::LockFrameMoveGuard()'))
            .replace('@UNLOCK@', block(source, 'void CApplication::UnlockFrameMoveGuard()')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix='gui-external-call-window-'))
    output.mkdir(parents=True, exist_ok=True)
    source = (ROOT / SOURCE).read_text()
    baseline = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT, text=True)
    fixed = generate(source)
    cases = {'fixed': (fixed, None),
             'baseline': (generate(baseline), 'requested admission exceeds existing 2ms minimum'),
             'long-sleep': (fixed.replace('Sleep(2ms)', 'Sleep(80ms)'),
                            'requested admission exceeds existing 2ms minimum'),
             'always-open': (fixed.replace('if (m_WaitingExternalCalls)', 'if (true)'),
                             'no waiting calls must not sleep'),
             'keep-graphics': (fixed.replace('CSingleExit ex(CServiceBroker::GetWinSystem()->GetGfxContext());', ''),
                               'both guards must admit nested caller'),
             'no-relock': (fixed.replace('KODI::TIME::Sleep(2ms);\n      m_frameMoveGuard.lock();',
                                         'KODI::TIME::Sleep(2ms);'),
                           'main frame guard restored exactly once')}
    results = {}
    for name, (text, witness) in cases.items():
        if witness and text == fixed:
            raise RuntimeError(f'control {name} did not change the fixture')
        unit, binary = output / f'{name}.cpp', output / name
        unit.write_text(text)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-pthread', '-DTARGET_WINDOWS',
                   '-I' + str(ROOT / 'xbmc'), '-Wall', '-Wextra', '-Werror', '-O1', '-g',
                   str(unit), '-o', str(binary)]
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie']
        subprocess.run(command, check=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=12,
                             env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        (output / f'{name}.stdout').write_text(run.stdout)
        (output / f'{name}.stderr').write_text(run.stderr)
        accepted = run.returncode == 0 if witness is None else run.returncode == 1 and witness in run.stderr
        results[name] = {'exit_code': run.returncode, 'expected_witness': witness,
                         'accepted': accepted, 'source_sha256': hashlib.sha256(text.encode()).hexdigest()}
        if name == 'fixed' and run.returncode == 0:
            results[name]['observed'] = json.loads(run.stdout)
        (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        if not accepted:
            raise RuntimeError(f'{name} failed verification: {run.stderr}')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
