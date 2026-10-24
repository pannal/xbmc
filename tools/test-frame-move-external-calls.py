#!/usr/bin/env python3
"""Execute FrameMove's actual external-call window and decay block with host stubs.

The counter checks main-thread guard ownership on every read/reset. Releasing
the frame guard schedules a deterministic worker update before main continues;
the selected sleep must use the pre-release count. Locks, player and Sleep are
explicit stubs: this checks the code's access/order/policy, not real scheduling,
mutex fairness, Python/GIL behavior, rendering deadlines or device performance.
"""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


SOURCE = 'xbmc/application/Application.cpp'
BASELINE = 'cf812ff76c52becaa42d311bdacf92964202701f'


def block(source):
    start = source.index('if (m_WaitingExternalCalls)', source.index('void CApplication::FrameMove('))
    decay = source.index('if (m_ProcessedExternalDecay && --m_ProcessedExternalDecay == 0)', start)
    return source[start:source.index(';', decay) + 1]


HARNESS = r'''
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
enum class Owner { NONE, MAIN, WORKER };
static unsigned checks = 0;
static void Check(bool value, const char* description)
{
  ++checks;
  if (!value) throw std::runtime_error(description);
}
struct State
{
  Owner frame = Owner::MAIN, graphics = Owner::MAIN;
  unsigned counter = 0, reads = 0, resets = 0, workerUpdates = 0;
  unsigned waiting = 0, scheduledUpdates = 0;
  std::vector<std::string> order;
  std::vector<unsigned> sleeps;
};
struct TrackedCounter
{
  State& state;
  operator unsigned() const
  {
    Check(state.frame == Owner::MAIN, "unguarded processed-count read");
    ++state.reads;
    return state.counter;
  }
  TrackedCounter& operator=(unsigned value)
  {
    Check(state.frame == Owner::MAIN, "unguarded processed-count reset");
    ++state.resets;
    state.counter = value;
    return *this;
  }
};
struct FrameGuard
{
  State& state;
  void unlock()
  {
    Check(state.frame == Owner::MAIN && state.graphics == Owner::NONE,
          "release graphics before frame guard");
    state.order.emplace_back("frame.unlock");
    state.frame = Owner::NONE;
    // Model workers obtaining frame then graphics locks, completing and
    // releasing both, strictly before main's next expression is evaluated.
    for (unsigned i = 0; i < state.scheduledUpdates; ++i)
    {
      state.frame = Owner::WORKER;
      ++state.counter; // Actual LockFrameMoveGuard increments before graphics lock.
      ++state.workerUpdates;
      state.graphics = Owner::WORKER;
      state.graphics = Owner::NONE;
      state.frame = Owner::NONE;
    }
    state.scheduledUpdates = 0;
    state.waiting = 0;
  }
  void lock()
  {
    Check(state.frame == Owner::NONE && state.graphics == Owner::NONE,
          "reacquire frame guard before graphics");
    state.order.emplace_back("frame.lock");
    state.frame = Owner::MAIN;
  }
};
static State* active = nullptr;
struct WinSystem
{
  State& GetGfxContext() { return *active; }
};
struct CServiceBroker
{
  static WinSystem* GetWinSystem() { static WinSystem system; return &system; }
};
struct CSingleExit
{
  State& state;
  explicit CSingleExit(State& value) : state(value)
  {
    Check(state.frame == Owner::MAIN && state.graphics == Owner::MAIN,
          "enter external window holding both locks");
    state.order.emplace_back("graphics.unlock");
    state.graphics = Owner::NONE;
  }
  ~CSingleExit()
  {
    // Keep a deliberately injected access failure visible to main's reporter;
    // this stub does not model the production exception-unwinding path.
    if (std::uncaught_exceptions() != 0)
      return;
    if (state.frame != Owner::MAIN || state.graphics != Owner::NONE)
      std::terminate();
    state.order.emplace_back("graphics.lock");
    state.graphics = Owner::MAIN;
  }
};
struct Player
{
  bool playing, paused;
  bool IsPlayingVideo() const { return playing; }
  bool IsPausedPlayback() const { return paused; }
};
namespace KODI { namespace TIME {
void Sleep(std::chrono::milliseconds duration)
{
  Check(active->frame == Owner::NONE && active->graphics == Owner::NONE,
        "sleep holds neither GUI lock");
  active->order.emplace_back("sleep");
  active->sleeps.push_back(static_cast<unsigned>(duration.count()));
}
} }
struct Application
{
  State state;
  unsigned& m_WaitingExternalCalls = state.waiting;
  TrackedCounter m_ProcessedExternalCalls{state};
  unsigned m_ProcessedExternalDecay = 0;
  FrameGuard m_frameMoveGuard{state};
  void Frame(Player* appPlayer)
  {
    active = &state;
    @BLOCK@
  }
};
static unsigned ExpectedSleep(unsigned count, bool playing, bool paused)
{
  unsigned result = count / 4;
  if (result < 2) result = 2;
  const unsigned limit = playing && !paused ? 10 : 80;
  if (result > limit) result = limit;
  return result;
}
int main()
{
  try
  {
    const unsigned counts[] = {0,1,3,4,7,8,11,12,15,16,39,40,43,44,
                               100,319,320,324,10000,
                               std::numeric_limits<unsigned>::max() - 100};
    unsigned cases = 0;
    for (bool playing : {false,true}) for (bool paused : {false,true})
      for (unsigned count : counts) for (unsigned updates : {0u,1u,8u,100u})
      {
        Application app;
        app.state.counter = count;
        app.state.waiting = 1;
        app.state.scheduledUpdates = updates;
        Player player{playing,paused};
        app.Frame(&player);
        ++cases;
        Check(app.state.reads == 1, "one protected count snapshot per open window");
        Check(app.state.sleeps == std::vector<unsigned>{ExpectedSleep(count,playing,paused)},
              "unchanged sleep policy uses pre-release count");
        Check(app.state.counter == count + updates, "worker increments retained");
        Check(app.state.workerUpdates == updates, "scheduled worker updates executed");
        Check(app.state.order == std::vector<std::string>{"graphics.unlock","frame.unlock",
                       "sleep","frame.lock","graphics.lock"}, "unchanged lock/sleep order");
        Check(app.state.frame == Owner::MAIN && app.state.graphics == Owner::MAIN,
              "both locks restored");
        Check(app.m_ProcessedExternalDecay == 4 && app.state.resets == 0,
              "open window starts unchanged decay");
        // Four no-request iterations must reset, with no new sleep or read.
        for (unsigned remaining : {3u,2u,1u,0u})
        {
          app.state.order.clear();
          app.Frame(&player);
          Check(app.m_ProcessedExternalDecay == remaining, "decay advances once per frame");
          Check(app.state.order.empty() && app.state.sleeps.size() == 1,
                "inactive window does not release locks or sleep");
          Check(app.state.reads == 1, "inactive window takes no snapshot");
          Check(app.state.counter == (remaining ? count + updates : 0),
                "count retained until decay expires");
        }
        Check(app.state.resets == 1, "single reset when decay expires");
        app.Frame(&player);
        Check(app.state.resets == 1, "expired decay remains inert");
      }
    // Requests during a countdown refresh its five-frame lifetime.
    {
      Application app; Player player{true,false}; app.state.counter = 8;
      app.m_ProcessedExternalDecay = 1; app.state.waiting = 1;
      app.Frame(&player);
      Check(app.m_ProcessedExternalDecay == 4 && app.state.counter == 8,
            "new work refreshes decay instead of resetting count");
    }
    // Completed calls contribute to the following window's existing policy.
    {
      Application app; Player player{false,false}; app.state.counter = 8;
      for (unsigned frame = 0; frame < 5; ++frame)
      {
        app.state.waiting = 1; app.state.scheduledUpdates = 4;
        app.Frame(&player);
        Check(app.state.sleeps.back() == ExpectedSleep(8 + frame * 4,false,false),
              "next window observes preceding worker increments");
        Check(app.state.counter == 12 + frame * 4 && app.state.resets == 0,
              "continuous requests retain cumulative count");
        Check(app.m_ProcessedExternalDecay == 4, "continuous work refreshes decay");
      }
    }
    std::cout << "cases=" << cases << " checks=" << checks << '\n';
    return 0;
  }
  catch (const std::exception& error)
  {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cxx', default=os.environ.get('CXX', 'c++'))
    parser.add_argument('--baseline-ref', default=BASELINE)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    current = block((args.repo / SOURCE).read_text())
    baseline = block(subprocess.check_output(
        ['git', 'show', args.baseline_ref + ':' + SOURCE], cwd=args.repo, text=True))
    with tempfile.TemporaryDirectory(prefix='frame-move-external-calls-') as directory:
        for label, source in [('current',current), ('baseline',baseline)]:
            cpp = Path(directory) / (label + '.cpp')
            cpp.write_text(HARNESS.replace('@BLOCK@', source))
            binary = cpp.with_suffix('')
            command = shlex.split(args.cxx) + [
                '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                str(cpp), '-o', str(binary)]
            if args.sanitize:
                command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-fno-pie', '-no-pie']
            subprocess.run(command, check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True,
                                    env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
            if label == 'current':
                assert result.returncode == 0, (result.stdout,result.stderr)
                print('PASS current actual block:', result.stdout.strip())
            else:
                assert result.returncode != 0 and 'unguarded processed-count read' in result.stderr, (
                    'Original unguarded block was not rejected',result.stdout,result.stderr)
                print('PASS original negative control: unguarded counter read rejected')
    print('Limits: deterministic lock/worker/player/sleep stubs; no real mutex, Python, rendering or device timing.')


if __name__ == '__main__':
    main()
