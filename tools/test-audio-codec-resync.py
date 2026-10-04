#!/usr/bin/env python3
"""Exercise production codec-recreation resync units and timestamp guards.

Extract the sink delay, recreation conditional and codec resync implementation.
Clock, stream, locking and logging are host stand-ins; no device sync is modeled.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLAYER = ROOT / "xbmc/cores/VideoPlayer"


def block(text, marker):
    start = text.index(marker)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


audio = (PLAYER / "VideoPlayerAudio.cpp").read_text()
codec = (PLAYER / "DVDCodecs/Audio/DVDAudioCodecPassthrough.cpp").read_text()
header = (PLAYER / "DVDCodecs/Audio/DVDAudioCodecPassthrough.h").read_text()
harness = r'''
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include <cassert>
#include <cmath>
#include <iostream>
#include <limits>
#include <mutex>
constexpr int LOGDEBUG = 0;
struct CLog {
  static inline double loggedDelay = -1;
  template<typename... T> static void Log(int, const char*, T...) {}
  static void Log(int, const char*, double, double delay) { loggedDelay = delay; }
};
using CCriticalSection = std::mutex;
struct Stream { double delay = 0.466; double GetDelay() { return delay; } };
struct CAudioSinkAE {
  CCriticalSection m_critSection;
  Stream* m_pAudioStream = nullptr;
  double GetDelay();
};
@DELAY@
struct Tracker { int resets = 0; void Reset() { ++resets; } };
struct CDVDAudioCodecPassthrough {
  @LIMIT@
  bool m_lavStyleSyncEnabled = true, m_needsResync = true;
  double m_internalClock = DVD_NOPTS_VALUE;
  Tracker m_jitterTracker;
  void SyncToResyncPts(double pts);
};
@RESYNC@
struct Clock { double value = 150000; double GetClock() { return value; } };
struct IDVDStreamPlayer { enum { SYNC_STARTING, SYNC_INSYNC }; };
void recreate(bool enableLavFull, int m_syncState, Clock* m_pClock,
              CAudioSinkAE& m_audioSink, CDVDAudioCodecPassthrough* passthroughCodec) {
@RECREATE@
}
void near(double actual, double expected) {
  if (std::abs(actual - expected) > 0.000001) {
    std::cerr << "expected " << expected << ", got " << actual << '\n';
    std::abort();
  }
}
int main() {
  CAudioSinkAE sink;
  CDVDAudioCodecPassthrough codec;
  Clock clock;
  // No live AE stream during recreation: 150 ms master + 300 ms fallback.
  near(sink.GetDelay(), 300000);
  recreate(true, IDVDStreamPlayer::SYNC_INSYNC, &clock, sink, &codec);
  near(codec.m_internalClock, 450000);
  near(CLog::loggedDelay, 0.3);
  assert(!codec.m_needsResync && codec.m_jitterTracker.resets == 1);
  // Active AE stream also supplies seconds, converted exactly once by the sink.
  Stream stream;
  sink.m_pAudioStream = &stream;
  clock.value = 123400000;
  recreate(true, IDVDStreamPlayer::SYNC_INSYNC, &clock, sink, &codec);
  near(codec.m_internalClock, 123866000);
  near(CLog::loggedDelay, 0.466);
  // Provisional initialization must not run before sync, without a clock or
  // when Full is disabled; an existing valid anchor stays intact.
  const int resets = codec.m_jitterTracker.resets;
  recreate(false, IDVDStreamPlayer::SYNC_INSYNC, &clock, sink, &codec);
  recreate(true, IDVDStreamPlayer::SYNC_STARTING, &clock, sink, &codec);
  recreate(true, IDVDStreamPlayer::SYNC_INSYNC, nullptr, sink, &codec);
  near(codec.m_internalClock, 123866000);
  assert(codec.m_jitterTracker.resets == resets);
  // A later coordinated timestamp supersedes provisional initialization.
  codec.SyncToResyncPts(344000);
  near(codec.m_internalClock, 344000);
  const int coordinatedResets = codec.m_jitterTracker.resets;
  for (double invalid : {static_cast<double>(DVD_NOPTS_VALUE), -1.0,
                         86400000001.0, std::numeric_limits<double>::infinity(),
                         std::numeric_limits<double>::quiet_NaN()})
    codec.SyncToResyncPts(invalid);
  near(codec.m_internalClock, 344000);
  assert(codec.m_jitterTracker.resets == coordinatedResets);
  codec.m_lavStyleSyncEnabled = false;
  codec.SyncToResyncPts(1000000);
  near(codec.m_internalClock, 344000);
  codec.m_lavStyleSyncEnabled = true;
  codec.SyncToResyncPts(0);
  near(codec.m_internalClock, 0);
  codec.SyncToResyncPts(86400000000.0);
  near(codec.m_internalClock, 86400000000.0);
  std::cout << "audio codec resync units, recreation gates and timestamp guards: PASS\n";
}
'''
harness = harness.replace("@DELAY@", block(
    (PLAYER / "AudioSinkAE.cpp").read_text(), "double CAudioSinkAE::GetDelay()"))
harness = harness.replace("@LIMIT@", re.search(
    r"static constexpr double MAX_REASONABLE_PTS = [^;]+;", header).group())
harness = harness.replace("@RESYNC@", block(
    codec, "void CDVDAudioCodecPassthrough::SyncToResyncPts(double pts)"))
harness = harness.replace("@RECREATE@", block(
    audio, "if (enableLavFull && m_syncState == IDVDStreamPlayer::SYNC_INSYNC && m_pClock)"))
with tempfile.TemporaryDirectory(prefix="audio-codec-resync-") as temp:
    source = Path(temp) / "test.cpp"
    binary = Path(temp) / "test"
    source.write_text(harness)
    subprocess.run([os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra",
                    "-Werror", "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                    "-I", str(ROOT / "xbmc"), str(source), "-o", str(binary)], check=True)
    # LSan cannot inspect threads under the desktop sandbox's ptrace supervisor.
    # Keep ASan/UBSan enabled; callers can override these runtime options.
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    subprocess.run([str(binary)], check=True, env=env)
