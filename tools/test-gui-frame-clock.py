#!/usr/bin/env python3
"""Host regression for the GUI frame time that animations read.

Compiles the production CTimeUtils::UpdateFrameTime and GetFrameTime from
xbmc/utils/TimeUtils.cpp against a fake uptime clock and refresh rate, and
drives them with swap return times like those of an AM6B+ at 60 Hz, where a
displayed frame is shown one refresh after the previous one. Every presented
frame must advance the frame time by one refresh (16 or 17 ms at 60 Hz), also
in the frames after a click from idle, whose swaps return unevenly (capture:
24, 7.2, 9.5 ms, ...), and with a display slightly slower than reported. A
missed refresh advances two refreshes; 50 Hz stays exact; idle follows the
clock; a loop not paced by the display stays bounded; the time never goes
backwards, also across the uptime wrap. The historical integer, rounded-up
frame step must show the 0 and 32 ms steps, and destructive mutations must be
rejected. Host fixtures only: no Kodi main loop, display or device timing.
"""
import argparse
from pathlib import Path
import re
import subprocess

TIME_UTILS = 'xbmc/utils/TimeUtils.cpp'

HISTORICAL = r'''
void CTimeUtils::UpdateFrameTime(bool flip)
{
  unsigned int currentTime = GetUptimeInMs();
  unsigned int framedur = (unsigned int)(1000.0 / static_cast<double>(CServiceBroker::GetWinSystem()->GetGfxContext().GetFPS()));

  frameTime += framedur * ((currentTime - frameTime + framedur - 1) / framedur);
}
'''


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

static double g_clock = 0; // ms, may be fractional; the uptime is its integer part
static float g_fps = 60;
static unsigned int GetUptimeInMs() { return static_cast<unsigned int>(static_cast<int64_t>(std::floor(g_clock))); }
struct CGraphicContext { float GetFPS() const { return g_fps; } };
struct CWinSystem { CGraphicContext gfx; CGraphicContext& GetGfxContext() { return gfx; } };
struct CServiceBroker { static CWinSystem* GetWinSystem() { static CWinSystem w; return &w; } };
class CTimeUtils
{
public:
  static void UpdateFrameTime(bool flip);
  static unsigned int GetFrameTime();
  static unsigned int frameTime;
};
unsigned int CTimeUtils::frameTime = 0;
'''

MAIN = r'''
static int failures = 0;
static void Check(bool value, const std::string& message)
{
  if (!value)
  {
    std::fprintf(stderr, "FAIL: %s\n", message.c_str());
    ++failures;
  }
}

// One main-loop iteration ending at the current clock.
static unsigned int Frame(bool flip)
{
  const unsigned int before = CTimeUtils::GetFrameTime();
  CTimeUtils::UpdateFrameTime(flip);
  const unsigned int after = CTimeUtils::GetFrameTime();
  Check(static_cast<int32_t>(after - before) >= 0, "frame time went backwards");
  return after - before;
}

static void Idle(int iterations)
{
  for (int i = 0; i < iterations; ++i)
  {
    g_clock += 40; // the 40 ms idle sleep
    Frame(false);
  }
}

static int32_t Lead() { return static_cast<int32_t>(CTimeUtils::GetFrameTime() - GetUptimeInMs()); }

static bool OneRefresh(unsigned int step, double period)
{
  return step >= std::floor(period) && step <= std::ceil(period);
}

// Steady display-paced frames: swaps return just after each refresh, with jitter.
static std::vector<unsigned int> Steady(int frames, double realPeriod, double jitter = 0.3)
{
  std::vector<unsigned int> steps;
  double vsync = g_clock;
  for (int i = 0; i < frames; ++i)
  {
    vsync += realPeriod;
    g_clock = vsync + ((i * 7919) % 11) / 10.0 * jitter;
    steps.push_back(Frame(true));
  }
  return steps;
}

static void Reset(double clock, float fps)
{
  g_clock = clock;
  g_fps = fps;
  CTimeUtils::frameTime = GetUptimeInMs();
  Idle(3);
}

int main(int argc, char** argv)
{
  const bool historical = argc > 1 && std::string(argv[1]) == "historical";
  const double p60 = 1000.0 / 60;

  if (historical)
  {
    Reset(1000.25, 60);
    int uneven = 0;
    for (unsigned int step : Steady(600, p60))
      uneven += !OneRefresh(step, p60);
    const double starts[] = {24.0, 24.0, 7.2, 9.5, 15.6, 16.7, 16.7, 16.7};
    g_clock += 25;
    Frame(true);
    int unevenStart = 0;
    for (double interval : starts)
    {
      g_clock += interval;
      unevenStart += !OneRefresh(Frame(true), p60);
    }
    std::printf("historical: %d of 600 steady 60 Hz frames and %d of 8 frames after a click do not "
                "advance one refresh\n",
                uneven, unevenStart);
    return (uneven >= 20 && unevenStart >= 1) ? 3 : 0;
  }

  // steady 60 Hz: every frame advances one refresh, close to the clock
  Reset(1000.25, 60);
  for (unsigned int step : Steady(600, p60))
    Check(OneRefresh(step, p60), "60 Hz frame advances " + std::to_string(step) + " ms");
  Check(std::abs(Lead()) <= 2, "60 Hz frame time stays on the clock (" + std::to_string(Lead()) + ")");

  // a 59.94 Hz display reported as 60 Hz: still one refresh per frame, bounded lead
  Reset(5000.5, 60);
  const double p5994 = 1000.0 / 59.94;
  for (unsigned int step : Steady(36000, p5994))
    Check(OneRefresh(step, p60) || OneRefresh(step, p5994),
          "59.94 Hz frame advances " + std::to_string(step) + " ms");
  Check(std::abs(Lead()) <= 3, "59.94 Hz frame time follows the clock (" + std::to_string(Lead()) + ")");

  // after a click from idle the swaps return unevenly; the frames still show one
  // refresh apart (swap return intervals from an AM6B+ capture)
  const std::vector<std::vector<double>> starts = {
      {25.0, 24.0, 7.2, 9.5, 15.6, 16.7, 16.7, 16.7, 16.7},
      {35.1, 22.5, 7.6, 9.1, 15.7, 16.6, 16.7, 16.7, 16.7},
      {24.0, 27.4, 14.6, 7.3, 9.3, 15.6, 16.7, 16.7, 16.7},
      {22.4, 21.8, 26.9, 16.9, 16.3, 10.8, 6.1, 15.5, 16.7},
      {14.9, 12.7, 7.9, 7.8, 16.7, 15.5, 16.7, 16.7, 16.7},
      {25.0, 29.3, 16.5, 7.0, 9.8, 15.6, 16.7, 16.7, 16.7}};
  for (size_t n = 0; n < starts.size(); ++n)
    for (double phase : {0.0, 3.3, 7.9, 12.4})
    {
      Reset(20000 + phase, 60);
      for (size_t k = 0; k < starts[n].size(); ++k)
      {
        g_clock += starts[n][k];
        const unsigned int step = Frame(true);
        // the click frame itself is not part of the motion; the slow pull may add
        // up to a millisecond while it takes up the click frame's lag
        if (k > 0)
          Check(OneRefresh(step, p60) || step == std::ceil(p60) + 1,
                "start " + std::to_string(n) + " frame " + std::to_string(k) + " advances " +
                    std::to_string(step) + " ms");
      }
      for (unsigned int step : Steady(120, p60))
        Check(OneRefresh(step, p60), "settled frame advances " + std::to_string(step) + " ms");
      Check(std::abs(Lead()) <= 30, "start lead bounded (" + std::to_string(Lead()) + ")");
    }

  // a click frame that stalls (a window opening) is caught up, not carried as lag
  Reset(30000.2, 60);
  g_clock += 450;
  Frame(true);
  Steady(60, p60);
  Check(std::abs(Lead()) <= 2, "frame time follows the clock after a stall (" + std::to_string(Lead()) + ")");

  // frames slower than the display (a heavy view or a window opening) are shown
  // for one or two refreshes each: they advance one or two refreshes, keep the
  // animation speed and never jump further
  {
    struct Slow
    {
      const char* name;
      std::vector<double> intervals;
    };
    std::vector<Slow> slow = {{"20 ms", std::vector<double>(240, 20.0)},
                              {"25 ms", std::vector<double>(240, 25.0)},
                              {"28 ms", std::vector<double>(240, 28.0)},
                              {"1 and 2 refreshes", {}},
                              {"17-30 ms", {}}};
    for (int i = 0; i < 120; ++i)
    {
      slow[3].intervals.push_back(p60);
      slow[3].intervals.push_back(2 * p60);
    }
    for (int i = 0; i < 240; ++i)
      slow[4].intervals.push_back(17 + (i * 7919 % 131) / 10.0);
    for (const Slow& s : slow)
    {
      Reset(50000.3, 60);
      Steady(30, p60);
      const unsigned int start = CTimeUtils::GetFrameTime();
      const double began = g_clock;
      for (double interval : s.intervals)
      {
        g_clock += interval;
        const unsigned int step = Frame(true);
        Check(OneRefresh(step, p60) || OneRefresh(step, 2 * p60),
              std::string("frames ") + s.name + " apart advance " + std::to_string(step) + " ms");
      }
      const double speed = (CTimeUtils::GetFrameTime() - start) / (g_clock - began);
      Check(speed > 0.97 && speed < 1.03,
            std::string("frames ") + s.name + " apart keep the speed (" + std::to_string(speed) + ")");
    }
  }

  // a missed refresh while running advances two refreshes
  Reset(40000.75, 60);
  Steady(120, p60);
  g_clock += 2 * p60;
  const unsigned int missed = Frame(true);
  Check(missed >= 33 && missed <= 34, "missed refresh advances " + std::to_string(missed) + " ms");
  for (unsigned int step : Steady(60, p60))
    Check(OneRefresh(step, p60), "frame after a miss advances " + std::to_string(step) + " ms");

  // 50 Hz: exactly 20 ms per frame
  Reset(60000.1, 50);
  for (unsigned int step : Steady(500, 20.0))
    Check(step == 20, "50 Hz frame advances " + std::to_string(step) + " ms");

  // idle follows the clock exactly
  Reset(70000.4, 60);
  Steady(30, p60);
  Idle(5);
  Check(CTimeUtils::GetFrameTime() == GetUptimeInMs(), "idle frame time is the clock");

  // frames not paced by the display (5 ms apart) stay within three periods of the
  // clock: they hold beyond two, after at most one more period
  Reset(80000, 60);
  int maxLead = 0;
  for (int i = 0; i < 2000; ++i)
  {
    g_clock += 5;
    Frame(true);
    maxLead = std::max(maxLead, static_cast<int>(Lead()));
  }
  Check(maxLead <= 3 * p60 + 1 && Lead() >= -2,
        "unpaced frames stay near the clock (max lead " + std::to_string(maxLead) + ")");

  // uptime wrap
  Reset(4294967296.0 - 500, 60);
  for (unsigned int step : Steady(120, p60))
    Check(OneRefresh(step, p60), "frame across the uptime wrap advances " + std::to_string(step) + " ms");
  Check(std::abs(Lead()) <= 2, "frame time follows the clock across the wrap");

  // the wall clock stepping back does not move the frame time back
  Reset(90000, 60);
  Steady(60, p60);
  g_clock -= 5000;
  for (int i = 0; i < 400; ++i)
  {
    g_clock += p60;
    Frame(true);
  }
  Idle(2);
  Steady(120, p60);
  Check(std::abs(Lead()) <= 2, "frame time follows the clock again after it stepped back (" +
                                   std::to_string(Lead()) + ")");

  if (failures)
    return 1;
  std::printf("PASS: one refresh per presented frame at 60, 59.94 and 50 Hz and after clicks from "
              "idle, two after a miss, idle on the clock, unpaced bounded, monotonic across wrap and "
              "clock steps\n");
  return 0;
}
'''


def build(out, name, update):
    source = '\n'.join([PRELUDE, update, function(ROOT_SOURCE, 'unsigned int CTimeUtils::GetFrameTime()'),
                        MAIN])
    path = out / (name + '.cpp')
    path.write_text(source)
    exe = out / name
    compiled = subprocess.run(['g++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                               '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined',
                               str(path), '-o', str(exe)], capture_output=True, text=True, timeout=120)
    if compiled.returncode:
        raise RuntimeError(name + ' did not compile:\n' + compiled.stderr)
    return exe


def run(exe, *args):
    return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=120)


MUTATIONS = {
    # the historical integer frame step
    'integer-period': (r'const double period = 1000\.0 / fps;',
                       'const double period = static_cast<unsigned int>(1000.0 / fps);'),
    # the first frames after idle are counted from their uneven swaps
    'no-settle': (r'const bool settling = presented < 2;', 'const bool settling = false;'),
    # half a refresh of extra swap time already counts as a missed refresh
    'round-intervals': (r'std::floor\(uncounted / period \+ 0\.25\)', 'std::round(uncounted / period)'),
    # time short of a refresh is dropped: slow frames fall behind, then jump
    'no-carry': (r'uncounted = std::clamp\(\(uncounted - frames \* period\) \* 63 / 64, -period, period\);',
                 'uncounted = 0;'),
    # a mismatched refresh rate builds up in the carry until it adds a refresh
    'carry-without-leak': (r'\(uncounted - frames \* period\) \* 63 / 64', 'uncounted - frames * period'),
    # a stall is carried as lag instead of caught up
    'no-catch-up': (r'if \(ahead - frames \* period > 4 \* period\)\s*frames = std::round\(ahead / period\);'
                    r'[^\n]*\n\s*else ', ''),
    # no pull towards the clock: a mismatched refresh rate drifts
    'no-clock-pull': (r'\(ahead - frames \* period\) / 64', '0'),
    # frames not paced by the display run ahead without bound
    'no-hold': (r'else if \(ahead < -2 \* period\)\s*frames = 0;', ''),
    # the frame time can move backwards
    'backwards': (r'if \(!\(step > 0\)\)\s*return; // never backwards', ''),
    # idle iterations are counted as presented frames
    'idle-as-frames': (r'if \(!flip\)\s*presented = 0;\s*else if \(fps > 0\)', 'if (fps > 0)'),
    # the sub-millisecond part is dropped
    'no-fraction': (r'frameFraction = step - whole;', 'frameFraction = 0;'),
}

ROOT_SOURCE = ''


def main():
    global ROOT_SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('/tmp/gui-frame-clock'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    ROOT_SOURCE = (args.root / TIME_UTILS).read_text()
    update = function(ROOT_SOURCE, 'void CTimeUtils::UpdateFrameTime(bool flip)')

    historical = run(build(args.output, 'historical', HISTORICAL), 'historical')
    assert historical.returncode == 3, (historical.returncode, historical.stdout, historical.stderr)
    print('Historical frame step reproduced: ' + historical.stdout.strip(), flush=True)
    fixed = run(build(args.output, 'fixed', update))
    assert fixed.returncode == 0, (fixed.stdout, fixed.stderr)
    print(fixed.stdout.strip(), flush=True)

    for name, (pattern, replacement) in MUTATIONS.items():
        mutated, count = re.subn(pattern, replacement, update)
        assert count == 1, 'mutation did not apply: ' + name
        bad = run(build(args.output, 'mutant-' + name, mutated))
        assert bad.returncode != 0, 'destructive mutation survived: ' + name
        print('Rejected mutation: ' + name, flush=True)
    print('GUI frame clock: PASS; historical steps reproduced, %d mutations rejected; '
          'host fixtures, not main-loop/display/device verification' % len(MUTATIONS))


if __name__ == '__main__':
    main()
