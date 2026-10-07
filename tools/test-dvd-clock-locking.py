#!/usr/bin/env python3
"""Exercise the production DVD clock with instrumented host locks/reference time.

Checks concurrent snapshot/reset/framerate access and existing clock arithmetic.
No device timing or claim that this race caused a reported playback failure.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLOCK = ROOT / "xbmc/cores/VideoPlayer"

LOCK = r'''
#pragma once
#include <functional>
#include <mutex>
class CCriticalSection {
  std::recursive_mutex mutex;
public:
  std::function<void()> attempt;
  void lock() { if (attempt) attempt(); mutex.lock(); }
  void unlock() { mutex.unlock(); }
};
'''

REFERENCE = r'''
#pragma once
#include <cstdint>
#include <functional>
class CVideoReferenceClock {
public:
  static inline int64_t now = 1000000;
  static inline std::function<void()> sample;
  int64_t GetTime(bool = true) { if (sample) sample(); return now; }
  void SetSpeed(double speed) { m_speed = speed; }
  double GetSpeed() { return m_speed; }
  double GetRefreshRate(double*) { return 24; }
  bool GetClockInfo(int&, double&, double&) const { return false; }
private:
  double m_speed = 1;
};
'''

HARNESS = r'''
#include "DVDClock.cpp"
#include <condition_variable>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <string>
using namespace std::chrono_literals;
struct Clock : CDVDClock {
  CCriticalSection& Lock() { return m_critSection; }
};
static void require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
static void near(double value, double expected) {
  require(std::abs(value - expected) < 0.001, "clock arithmetic changed");
}
// The owner holds the playing-clock lock. A contender must attempt that lock
// before sampling reference time or returning. Events, rather than sleeps,
// distinguish exclusion from an unscheduled contender.
static void excluded(const std::string& operation) {
  Clock c;
  c.GetClock(); // consume the initial reset
  std::mutex mutex;
  std::condition_variable changed;
  std::string first;
  auto record = [&](const char* event) {
    std::lock_guard<std::mutex> guard(mutex);
    if (first.empty()) first = event;
    changed.notify_one();
  };
  std::unique_lock<CCriticalSection> owner(c.Lock());
  c.Lock().attempt = [&] { record("playing-lock"); };
  CVideoReferenceClock::sample = [&] { record("sample"); };
  double result = 0;
  std::thread contender([&] {
    if (operation == "snapshot") { double absolute; result = c.GetClock(absolute, false); }
    else if (operation == "reset") c.Reset();
    else if (operation == "framerate") c.UpdateFramerate(24);
    else throw std::runtime_error("unknown operation");
    record("completed");
  });
  bool serialized;
  {
    std::unique_lock<std::mutex> guard(mutex);
    if (!changed.wait_for(guard, 3s, [&] { return !first.empty(); })) std::abort();
    serialized = first == "playing-lock";
  }
  if (serialized && operation == "snapshot")
    c.Discontinuity(500000, c.GetAbsoluteClock());
  owner.unlock();
  contender.join();
  c.Lock().attempt = {};
  CVideoReferenceClock::sample = {};
  require(serialized, "operation escaped playing-clock lock");
  if (operation == "snapshot") near(result, 500000);
}
static void arithmetic() {
  Clock c;
  near(c.GetClock(), 0);
  CVideoReferenceClock::now += 100000;
  double absolute;
  near(c.GetClock(absolute, false), 100000);
  near(absolute, 100000);
  c.SetSpeedAdjust(0.01);
  CVideoReferenceClock::now += 100000;
  near(c.GetClock(), 201000);
  CVideoReferenceClock::now += 100000;
  near(c.GetClock(absolute), 302000);
  c.Discontinuity(700000, absolute);
  near(c.GetClock(), 700000);
  // ErrorAdjust recursively enters GetClock and Discontinuity.
  near(c.ErrorAdjust(10000, "test"), 10000);
  near(c.GetClock(), 710000);
  c.Pause(true);
  CVideoReferenceClock::now += 100000;
  near(c.GetClock(absolute), 710000);
  c.Pause(false);
  CVideoReferenceClock::now += 100000;
  near(c.GetClock(), 810000);
  c.UpdateFramerate(24);
  c.SetVsyncAdjust(1);
  near(c.ErrorAdjust(30000, "quantized"), 1000000.0 / 24);
  c.Reset();
  near(c.GetClock(absolute), 0);
  near(c.GetSpeedAdjust(), 0);
  near(c.GetVsyncAdjust(), 0);
}
int main(int argc, char** argv) {
  try {
    require(argc == 2, "expected a case name");
    if (std::string(argv[1]) == "arithmetic") arithmetic();
    else excluded(argv[1]);
    std::cout << "PASS " << argv[1] << '\n';
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
'''


def run(source, header, cases, must_pass):
    with tempfile.TemporaryDirectory(prefix="dvd-clock-locking-") as directory:
        work = Path(directory)
        files = {
            "DVDClock.cpp": source,
            "DVDClock.h": header,
            "VideoReferenceClock.h": REFERENCE,
            "threads/CriticalSection.h": LOCK,
            "utils/MathUtils.h": "#include <cmath>\nnamespace MathUtils { inline int round_int(double v) { return std::lround(v); } }\n",
            "utils/TimeUtils.h": "#include <cstdint>\ninline int64_t CurrentHostFrequency() { return 1000000; }\n",
            "utils/log.h": "constexpr int LOGDEBUG=0; struct CLog { template<class... T> static void Log(int, const char*, T...) {} };\n",
            "test.cpp": HARNESS,
        }
        for name, content in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        command = [os.environ.get("CXX", "g++"), "-std=c++17", "-pthread",
                   "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                   "-I", str(work), "-I", str(ROOT / "xbmc"),
                   str(work / "test.cpp"), "-o", str(work / "test")]
        subprocess.run(command, check=True)
        for case in cases:
            result = subprocess.run([str(work / "test"), case], capture_output=True,
                                    text=True, timeout=10)
            if (result.returncode == 0) != must_pass:
                raise RuntimeError(f"{case}: unexpected result\n{result.stdout}{result.stderr}")
            print(result.stdout.strip() if must_pass else f"REJECTED {case}: {result.stderr.strip()}")


if __name__ == "__main__":
    source = (CLOCK / "DVDClock.cpp").read_text()
    header = (CLOCK / "DVDClock.h").read_text()
    run(source, header, ["snapshot", "reset", "framerate", "arithmetic"], True)
    # Negative controls: restore each old synchronization defect independently.
    paired = "double CDVDClock::GetClock(double& absolute"
    start = source.index(paired)
    end = source.index("\nvoid CDVDClock::SetVsyncAdjust", start)
    body = source[start:end]
    lock = "std::unique_lock<CCriticalSection> lock(m_critSection);"
    assert lock in body
    run(source[:start] + body.replace(lock, "std::unique_lock<CCriticalSection> lock(m_systemsection);") + source[end:],
        header, ["snapshot"], False)
    # Taking the right lock only after the reference-time sample still permits
    # sampling across a concurrent discontinuity/speed change.
    late = body.replace("  " + lock + "\n", "")
    sample = "  int64_t current = m_videoRefClock->GetTime(interpolated);"
    late = late.replace(sample, sample + "\n  " + lock)
    run(source[:start] + late + source[end:], header, ["snapshot"], False)
    reset = "  std::unique_lock<CCriticalSection> lock(m_critSection);\n  m_bReset = true;"
    assert reset in source
    run(source.replace(reset, "  m_bReset = true;"), header, ["reset"], False)
    frame = "    std::unique_lock<CCriticalSection> lock(m_critSection);\n    m_frameTime = 1 / fps * DVD_TIME_BASE;"
    assert frame in source
    run(source.replace(frame, "    m_frameTime = 1 / fps * DVD_TIME_BASE;"),
        header, ["framerate"], False)
