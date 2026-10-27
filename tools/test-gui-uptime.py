#!/usr/bin/env python3
"""Test the production GUI uptime and frame clock against independent clocks.

Inject only steady_clock::now() and gettimeofday() observations. Compile the
actual uptime and animation-update bodies with synthetic display context.
Wall-time steps must not affect animation cadence; monotonic idle elapsed time
and the existing 32-bit uptime wrap remain supported. No device is exercised.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from importlib.util import module_from_spec, spec_from_file_location

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location('frame_clock_fixture', ROOT / 'tools/test-gui-frame-clock.py')
FIXTURE = module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)

PRELUDE = r'''
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <sys/time.h>

static int64_t steadyNs = 123000000000LL;
static int64_t wallOffsetMs = 1700000000000LL;
static float fps = 60;
[[maybe_unused]] static std::chrono::steady_clock::time_point FakeSteadyNow()
{
  return std::chrono::steady_clock::time_point(std::chrono::nanoseconds(steadyNs));
}
[[maybe_unused]] static int FakeGettimeofday(timeval* value, void*)
{
  const int64_t micros = steadyNs / 1000 + wallOffsetMs * 1000;
  value->tv_sec = micros / 1000000;
  value->tv_usec = micros % 1000000;
  return 0;
}
#define gettimeofday FakeGettimeofday
struct CGraphicContext { float GetFPS() const { return fps; } };
struct CWinSystem { CGraphicContext gfx; CGraphicContext& GetGfxContext() { return gfx; } };
struct CServiceBroker
{
  static CWinSystem* GetWinSystem() { static CWinSystem w; return &w; }
};
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
static void Check(bool value, const char* message)
{
  if (!value)
  {
    if (failures < 10)
      std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
  }
}
int main()
{
  const int64_t origin = steadyNs;
  Check(GetUptimeInMs() == 0, "uptime starts at zero");
  CTimeUtils::UpdateFrameTime(false);
  // Settle at 60 Hz before altering only wall time. The first rollback occurs
  // after ten seconds so it reproduces the original five-second animation hold.
  for (int frame = 0; frame < 2400; ++frame)
  {
    if (frame == 600)
      wallOffsetMs -= 5000;
    if (frame == 1200)
      wallOffsetMs += 3600000;
    if (frame == 1800)
      wallOffsetMs -= 7200000;
    steadyNs += 16666667;
    const unsigned int before = CTimeUtils::GetFrameTime();
    const unsigned int expected = static_cast<unsigned int>((steadyNs - origin) / 1000000);
    Check(GetUptimeInMs() == expected, "wall-clock step changed animation uptime");
    CTimeUtils::UpdateFrameTime(true);
    const unsigned int step = CTimeUtils::GetFrameTime() - before;
    Check(step >= 16 && step <= 17, "paced animation froze or jumped after wall-clock step");
  }
  // Idle consumes monotonic elapsed time, independent of wall-time corrections.
  wallOffsetMs += 86400000;
  steadyNs += 300000000000LL;
  CTimeUtils::UpdateFrameTime(false);
  Check(CTimeUtils::GetFrameTime() == GetUptimeInMs(), "idle did not follow elapsed time");
  Check(GetUptimeInMs() == static_cast<unsigned int>((steadyNs - origin) / 1000000),
        "wall-clock change affected idle uptime");

  // Seed a long-lived process near uint32 wrap. This explicitly advances only
  // elapsed time; the seed avoids inventing a 49-day gap between adjacent frames.
  steadyNs = origin + (4294967296LL - 500) * 1000000;
  CTimeUtils::frameTime = GetUptimeInMs();
  CTimeUtils::UpdateFrameTime(false);
  for (int frame = 0; frame < 120; ++frame)
  {
    steadyNs += 16666667;
    const unsigned int expected = static_cast<unsigned int>((steadyNs - origin) / 1000000);
    Check(GetUptimeInMs() == expected, "uptime failed uint32 wrap");
    const unsigned int before = CTimeUtils::GetFrameTime();
    CTimeUtils::UpdateFrameTime(true);
    const unsigned int step = CTimeUtils::GetFrameTime() - before;
    Check(step >= 16 && step <= 17, "animation lost cadence across uptime wrap");
  }
  if (failures)
    return 1;
  std::puts("PASS: production uptime/frame clock survives backward and forward wall steps, "
            "idle elapsed time and uint32 wrap");
}
'''


def run(source_path):
    source = source_path.read_text()
    methods = []
    if 'static inline uint64_t GetTimeInMs()' in source:
        methods.append(FIXTURE.function(source, 'static inline uint64_t GetTimeInMs()'))
    methods.extend(FIXTURE.function(source, signature) for signature in (
        'static unsigned int GetUptimeInMs()',
        'void CTimeUtils::UpdateFrameTime(bool flip)',
        'unsigned int CTimeUtils::GetFrameTime()',
    ))
    # Clock seam only: the production origin, elapsed conversion and all frame
    # math remain intact. Historical source instead reads FakeGettimeofday.
    bodies = '\n'.join(methods).replace('std::chrono::steady_clock::now()', 'FakeSteadyNow()')
    with tempfile.TemporaryDirectory(prefix='gui-uptime-') as directory:
        out = Path(directory)
        cpp = out / 'uptime.cpp'
        cpp.write_text(PRELUDE + bodies + MAIN)
        exe = out / 'uptime'
        subprocess.run([
            os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
            '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined',
            '-fno-pie', '-no-pie', str(cpp), '-o', str(exe),
        ], check=True, timeout=60)
        subprocess.run([str(exe)], check=True, timeout=15,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                            'UBSAN_OPTIONS': 'halt_on_error=1'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'xbmc/utils/TimeUtils.cpp')
    run(parser.parse_args().source)
