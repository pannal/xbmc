#!/usr/bin/env python3
"""Replay ActiveAE sync accounting with ordinary captured TrueHD errors.

The sampler arithmetic, complete SyncStream and CSyncError are production code.
Clock, delay, timer and packet pools are controlled host boundaries. This checks
error units and convergence, not MAT bytes, live ALSA, receiver lock or scanout.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE"


def block(text, marker):
    start = text.index(marker)
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def harness(engine):
    header = (AE / "ActiveAEStream.h").read_text()
    # Include the actual TrueHD predicate and sampler, not a duplicate equation.
    predicate = re.search(r"const bool isTrueHDPassthrough =.*?;", engine, re.S).group()
    sample = engine[engine.index("        AEDelayStatus status;", engine.index("// calculate sync error")):
                    engine.index("        if (error > maxError)", engine.index("// calculate sync error"))]
    sample = sample.replace("(*it)->", "stream->")
    constants = "\n".join(re.findall(r"constexpr double TRUEHD_SYNC_ERROR_SCALE = .*?;", engine))
    resume_cases = re.findall(
        r"case CActiveAEControlProtocol::RESUMESTREAM:(.*?)"
        r"case CActiveAEControlProtocol::FLUSHSTREAM:", engine, re.S)
    resume = resume_cases[0].split("stream = *(CActiveAEStream**)msg->data;", 1)[1]
    resume = resume[:resume.index("streaming = true;")]
    idle_resume = resume_cases[1].split("stream = *(CActiveAEStream**)msg->data;", 1)[1]
    idle_resume = idle_resume[:idle_resume.index("m_state =")]
    flush = re.search(r"stream->m_syncState = [^;]+;", block(
        engine, "void CActiveAE::SFlushStream(")).group()
    return r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <iostream>
using namespace std::chrono_literals;
static long nowMs = 0;
namespace XbmcThreads {
template<class T = void> struct EndTime {
  long start = 0, duration = 0;
  void Set(std::chrono::milliseconds value) {start = nowMs; duration = value.count();}
  bool IsTimePast() const {return nowMs - start >= duration;}
  auto GetStartTime() const {return std::chrono::steady_clock::time_point(std::chrono::milliseconds(start));}
};
}
@SYNC_ERROR@;
constexpr int LOGDEBUG = 0, LOGAUDIO = 1;
struct CLog {template<class... T> static void Log(int, T...) {}};
struct CAESyncInfo {enum AESyncState {SYNC_START, SYNC_MUTE, SYNC_ADJUST, SYNC_INSYNC};};
struct CAEStreamInfo {
  enum Type {STREAM_TYPE_TRUEHD, STREAM_TYPE_AC3};
  Type m_type = STREAM_TYPE_TRUEHD;
  int GetDuration() const {return 20;}
};
struct Format {CAEStreamInfo m_streamInfo; int m_frames = 960;};
struct Clock {double value = 1000; double GetClock() const {return value;} double GetClockSpeed() const {return 1;}};
struct Config {int sample_rate = 48000, channels = 1;};
struct Packet {
  Config config;
  int nb_samples = 960, max_nb_samples = 48000, pause_burst_ms = 0;
  int planes = 1, bytes_per_sample = 1, linesize = 48000;
  unsigned char storage[48000]{};
  unsigned char* data[1]{storage};
};
struct CSampleBuffer {
  Packet packet;
  Packet* pkt = &packet;
  double timestamp = 0;
  int pkt_start_offset = 0;
  void Return() {}
};
struct Buffers {
  Format m_inputFormat;
  std::deque<CSampleBuffer*> m_outputSamples;
  void SetRR(double, double) {}
};
struct Pool {
  CSampleBuffer buffer;
  bool available = true;
  CSampleBuffer* GetFreeBuffer() {return available ? &buffer : nullptr;}
};
struct CActiveAEStream {
  Clock clock;
  Clock* m_pClock = &clock;
  Buffers buffers;
  Buffers* m_processingBuffers = &buffers;
  Format m_format;
  CAESyncInfo::AESyncState m_syncState = CAESyncInfo::SYNC_START;
  CSyncError m_syncError;
  bool m_resampleMode = false;
  bool m_paused = false;
  double m_resampleIntegral = 0, m_lastSyncError = 0;
  std::chrono::milliseconds GetErrorInterval() const {return 1000ms;}
  double CalcResampleRatio(double) const {return 1;}
};
struct AEDelayStatus {double delay = 0; double GetDelay() const {return delay;}};
struct Stats {double delay = 0.1; void GetDelay(AEDelayStatus& status) {status.delay = delay;}};
struct CSinkControlProtocol {enum {STREAMING};};
struct CSinkDataProtocol {enum {SAMPLE};};
@CONSTANTS@
struct CActiveAE {
  enum Mode {MODE_RAW, MODE_PCM, MODE_TRANSCODE};
  Mode m_mode = MODE_RAW;
  Format m_sinkFormat, m_encoderFormat;
  struct {double atempoThreshold = 0;} m_settings;
  Pool pool;
  Pool* m_silenceBuffers = &pool;
  Stats m_stats;
  struct {struct {
    void SendOutMessage(int signal, void* value, size_t) {
      assert(signal == CSinkControlProtocol::STREAMING && *static_cast<bool*>(value));
    }
  } m_controlPort;
  struct {
    std::deque<CSampleBuffer*> sent;
    void SendOutMessage(int signal, void* value, size_t) {
      assert(signal == CSinkDataProtocol::SAMPLE);
      sent.push_back(*static_cast<CSampleBuffer**>(value));
    }
  } m_dataPort;} m_sink;
  std::deque<CSampleBuffer*> m_pausedSinkSamples;
  bool m_pausePreserved = false, m_resumeEpoch = false;
  XbmcThreads::EndTime<> m_resumeEpochTimer;
  void SendPausedSinkSamples();
  CSampleBuffer* SyncStream(CActiveAEStream* stream);
  void SResumeStream(CActiveAEStream* stream);
  void Resume(CActiveAEStream* stream) {
    @RESUME@
  }
  void ResumeIdle(CActiveAEStream* stream) {
    bool streaming;
    @IDLE_RESUME@
  }
  void Sample(CActiveAEStream* stream) {
    auto* buf = stream->m_processingBuffers->m_outputSamples.front();
    @PREDICATE@
    @SAMPLE@
    (void)maxError;
    stream->m_syncError.Add(error);
  }
};
@SYNC_STREAM@
@SEND_PAUSED@
@RESUME_SYNC@
static void near(double actual, double expected) {
  if (std::abs(actual - expected) > 1e-7) {
    std::cerr << "expected " << expected << ", got " << actual << '\n';
    std::abort();
  }
}
struct Replay {
  CActiveAE engine;
  CActiveAEStream stream;
  CSampleBuffer packet;
  double rawError = 0, scale = 0.45;
  int pauseMs = 0, droppedMs = 0;
  Replay(bool truehd = true, bool pcm = false) {
    nowMs = 0;
    stream.m_syncError.Flush();
    stream.buffers.m_outputSamples.push_back(&packet);
    if (!truehd) {
      engine.m_sinkFormat.m_streamInfo.m_type = CAEStreamInfo::STREAM_TYPE_AC3;
      stream.m_format.m_streamInfo.m_type = CAEStreamInfo::STREAM_TYPE_AC3;
    }
    if (pcm) engine.m_mode = CActiveAE::MODE_PCM;
    if (!truehd || pcm) scale = 1;
  }
  void sample() {
    // Submit the same known physical lead through the production delay equation.
    packet.timestamp = stream.clock.value + engine.m_stats.delay * 1000 + rawError;
    packet.pkt_start_offset = 0;
    engine.Sample(&stream);
  }
  void resetPacket() {packet.pkt->nb_samples = 960; packet.pkt->pause_burst_ms = 0;}
  void acquire(double measuredError) {
    rawError = measuredError / scale;
    assert(engine.SyncStream(&stream) == nullptr);
    assert(stream.m_syncState == CAESyncInfo::SYNC_MUTE);
    resetPacket();
    nowMs += 101;
    sample();
  }
  void step(bool checkUnits = true) {
    auto* out = engine.SyncStream(&stream);
    if (out) {
      double elapsed = engine.m_mode == CActiveAE::MODE_RAW ? out->pkt->pause_burst_ms :
                       out->pkt->nb_samples * 1000.0 / out->pkt->config.sample_rate;
      rawError -= elapsed;
      pauseMs += elapsed;
    }
    else if (packet.pkt->nb_samples < 960 && stream.m_syncState != CAESyncInfo::SYNC_MUTE) {
      double elapsed = engine.m_mode == CActiveAE::MODE_RAW ? 20 :
                       (960 - packet.pkt->nb_samples) * 1000.0 / packet.pkt->config.sample_rate;
      rawError += elapsed;
      droppedMs += elapsed;
    }
    if (checkUnits && stream.m_syncState == CAESyncInfo::SYNC_ADJUST) {
      unsigned int time;
      near(stream.m_syncError.GetLastError(time), scale * rawError);
    }
    resetPacket();
  }
  void settle(bool checkUnits = true) {
    for (int i = 0; i < 100; ++i) {
      step(checkUnits);
      if (stream.m_syncState == CAESyncInfo::SYNC_INSYNC) return;
    }
    assert(false && "adjustment did not converge");
  }
  void nextWindow() {
    nowMs += 1001;
    sample();
    step();
  }
};
int main(int argc, char**) {
  const bool describeOriginal = argc > 1;
  // Acquisition must remain inaudible, before either sign of correction.
  for(bool pcm:{false,true})for(bool truehd:{false,true}){
    Replay replay(truehd,pcm);
    std::memset(replay.packet.pkt->storage,0x5a,sizeof(replay.packet.pkt->storage));
    assert(replay.engine.SyncStream(&replay.stream)==nullptr);
    assert(replay.stream.m_syncState==CAESyncInfo::SYNC_MUTE);
    if(pcm){
      for(auto value:replay.packet.pkt->storage)assert(value==0);
    }else{
      assert(replay.packet.pkt->nb_samples==0&&replay.packet.pkt->pause_burst_ms==20);
      for(auto value:replay.packet.pkt->storage)assert(value==0x5a);
    }
    replay.resetPacket();
    assert(replay.engine.SyncStream(&replay.stream)==nullptr);
    if(!pcm)assert(replay.packet.pkt->nb_samples==0);
  }
  // Reported good/earlier-bad/newer-bad. Original correction counts are archived.
  const double errors[]{171.458574, 194.959359, 179.284037};
  const double nextErrors[]{91.195204, 106.850631, 105.593104};
  const int correctedMs[]{320, 380, 340}, originalMs[]{160, 180, 160};
  for (int i = 0; i < 3; ++i) {
    Replay replay;
    replay.acquire(errors[i]);
    replay.settle(!describeOriginal);
    if (describeOriginal) {
      assert(replay.pauseMs == originalMs[i]);
      std::cout << "original cycle " << i << ": pause=" << replay.pauseMs
                << "ms physical residual=" << replay.rawError
                << "ms next scaled=" << 0.45 * replay.rawError << "ms\n";
      continue;
    }
    assert(replay.pauseMs == correctedMs[i]);
    assert(std::abs(replay.scale * replay.rawError) < 30);
    // Preserve the observed change between windows, beyond the physical pause
    // correction. Individual clock/delay samples were not logged in these cases.
    replay.rawError += (nextErrors[i] - (errors[i] - 0.45 * originalMs[i])) / 0.45;
    replay.nextWindow();
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
    assert(replay.pauseMs == correctedMs[i]);
  }
  if (describeOriginal) return 0;
  // Negative error must use the same units; both signs retain packet granularity.
  for (bool truehd : {true, false}) for (double error : {-179.284037, 179.284037}) {
    Replay replay(truehd);
    replay.acquire(error);
    replay.settle();
    assert(std::abs(replay.scale * replay.rawError) < 30);
    assert((error > 0 ? replay.pauseMs : replay.droppedMs) == (truehd ? 340 : 160));
    replay.nextWindow();
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
  }
  // Jitter damping and the original steady-state gate remain, for both signs.
  for (double raw : {-220.0, 220.0}) {
    Replay replay;
    replay.stream.m_syncState = CAESyncInfo::SYNC_INSYNC;
    replay.rawError = raw;
    nowMs = 1001;
    replay.sample();
    replay.step();
    assert(replay.pauseMs == 0 && replay.droppedMs == 0);
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
  }
  // PCM uses undamped milliseconds; no raw packet duration leaks into it.
  for (double error : {-90.0, 90.0}) {
    Replay replay(false, true);
    replay.acquire(error);
    replay.settle();
    assert(std::abs(replay.rawError) < 30);
  }
  // A large initial average retains the extra mute/re-measure recovery window.
  {
    Replay replay;
    replay.acquire(205);
    for (int i = 0; i < 100; ++i) {
      replay.step();
      if (replay.stream.m_syncState == CAESyncInfo::SYNC_MUTE) break;
    }
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_MUTE);
    assert(replay.pauseMs == 400);
    nowMs += 101;
    replay.sample();
    replay.settle();
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
  }
  // Clockless streams and exhausted pause-buffer pools do not fabricate correction.
  {
    Replay replay;
    replay.stream.m_pClock = nullptr;
    assert(replay.engine.SyncStream(&replay.stream) == nullptr);
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_START);
  }
  {
    Replay replay;
    replay.acquire(179.284037);
    replay.engine.pool.available = false;
    replay.step();
    assert(replay.pauseMs == 0 && replay.stream.m_syncState == CAESyncInfo::SYNC_ADJUST);
    replay.engine.pool.available = true;
    replay.settle();
  }
  // TrueHD resume preserves synchronized content; flush still reacquires.
  for (bool idle : {false, true}) for (bool resume : {false, true}) {
    Replay replay;
    replay.acquire(179.284037);
    replay.settle();
    if (resume) {
      replay.stream.m_paused = true;
      replay.stream.m_syncError.Add(1000);
      if (idle) replay.engine.ResumeIdle(&replay.stream);
      else replay.engine.Resume(&replay.stream);
      assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
      int paused = replay.pauseMs, dropped = replay.droppedMs;
      replay.rawError = 0;
      replay.sample();
      replay.step();
      assert(replay.pauseMs == paused && replay.droppedMs == dropped);
    } else {
      @FLUSH@
      replay.acquire(171.458574);
      replay.settle();
    }
    replay.nextWindow();
    assert(replay.stream.m_syncState == CAESyncInfo::SYNC_INSYNC);
  }
  // Initial starts, unsynchronized TrueHD and other formats retain acquisition.
  for (bool idle : {false, true}) for (bool truehd : {false, true})
      for (bool pcm : {false, true}) {
    Replay replay(truehd, pcm);
    for (auto state : {CAESyncInfo::SYNC_START, CAESyncInfo::SYNC_ADJUST,
                       CAESyncInfo::SYNC_INSYNC}) {
      replay.stream.m_paused = true;
      replay.stream.m_syncState = state;
      if (idle) replay.engine.ResumeIdle(&replay.stream);
      else replay.engine.Resume(&replay.stream);
      const bool preserve = truehd && !pcm && state == CAESyncInfo::SYNC_INSYNC;
      assert(replay.stream.m_syncState ==
             (preserve ? CAESyncInfo::SYNC_INSYNC : CAESyncInfo::SYNC_START));
    }
  }
  // Preserved non-TrueHD pauses open the resume epoch, or release held audio to reacquire.
  for (bool pcm : {false, true}) for (bool insync : {false, true}) {
    Replay replay(false, pcm);
    CSampleBuffer held;
    replay.engine.m_pausePreserved = true;
    replay.engine.m_pausedSinkSamples.push_back(&held);
    replay.stream.m_paused = true;
    replay.stream.m_syncState = insync ? CAESyncInfo::SYNC_INSYNC : CAESyncInfo::SYNC_ADJUST;
    replay.engine.Resume(&replay.stream);
    assert(!replay.engine.m_pausePreserved && replay.engine.m_resumeEpoch == insync);
    assert(replay.stream.m_syncState ==
           (insync ? CAESyncInfo::SYNC_INSYNC : CAESyncInfo::SYNC_START));
    assert(replay.engine.m_sink.m_dataPort.sent.size() == (insync ? 0u : 1u));
    assert(replay.engine.m_pausedSinkSamples.size() == (insync ? 1u : 0u));
    assert(!insync || !replay.engine.m_resumeEpochTimer.IsTimePast());
  }
  {
    Replay replay;
    replay.stream.m_syncState = CAESyncInfo::SYNC_INSYNC;
    replay.stream.m_paused = false;
    replay.stream.m_syncError.Correction(27);
    replay.engine.Resume(&replay.stream);
    unsigned int time;
    near(replay.stream.m_syncError.GetLastError(time), 27);
  }
  std::cout << "production TrueHD sync: captured errors, both signs, jitter, RAW/PCM, "
               "recovery, resume/flush and unavailable boundaries PASS\n";
}
'''.replace("@SYNC_ERROR@", block(header, "class CSyncError")).replace(
        "@CONSTANTS@", constants).replace("@PREDICATE@", predicate).replace(
        "@SAMPLE@", sample).replace("@SYNC_STREAM@", block(engine, "CSampleBuffer* CActiveAE::SyncStream(")).replace(
        "@RESUME@", resume).replace("@IDLE_RESUME@", idle_resume).replace(
        "@RESUME_SYNC@", block(engine, "void CActiveAE::SResumeStream(")).replace(
        "@SEND_PAUSED@", block(engine, "void CActiveAE::SendPausedSinkSamples(")).replace(
        "@FLUSH@", flush.replace("stream->", "replay.stream."))


def run(source, directory, name, original=False, fail=False):
    cpp, binary = directory / (name + ".cpp"), directory / name
    cpp.write_text(source)
    subprocess.run([os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-pie", "-no-pie", str(cpp), "-o", str(binary)], check=True)
    env = dict(os.environ)
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    result = subprocess.run([str(binary)] + (["original"] if original else []), env=env,
                            capture_output=True, text=True)
    if fail:
        assert result.returncode != 0, name + " unexpectedly passed"
        assert "expected " in result.stderr or "Assertion" in result.stderr, result.stderr
        print(name + ": rejected")
    else:
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, end="")
        assert result.returncode == 0, name + " failed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe-original", action="store_true")
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--engine-source", type=Path, default=AE / "ActiveAE.cpp")
    args = parser.parse_args()
    engine = args.engine_source.read_text()
    with tempfile.TemporaryDirectory(prefix="activeae-truehd-sync-") as temp:
        directory = Path(temp)
        run(harness(engine), directory, "production", original=args.describe_original)
        if args.negative_control:
            mutations = {
                "forced-resume-mute": engine.replace(
                    "if (trueHD && stream->m_syncState == CAESyncInfo::AESyncState::SYNC_INSYNC)",
                    "if (false && trueHD && stream->m_syncState == CAESyncInfo::AESyncState::SYNC_INSYNC)"),
                "unscaled-pause": engine.replace("ret->pkt->pause_burst_ms * syncErrorScale;",
                                                 "ret->pkt->pause_burst_ms;"),
                "unscaled-drop": engine.replace("stream->m_format.m_streamInfo.GetDuration() * syncErrorScale;",
                                                "stream->m_format.m_streamInfo.GetDuration();"),
                "wrong-nontruehd-scale": engine.replace("? TRUEHD_SYNC_ERROR_SCALE : 1.0;", "? TRUEHD_SYNC_ERROR_SCALE : 0.45;"),
                "undamped-sampling": engine.replace("error *= TRUEHD_SYNC_ERROR_SCALE;", "error *= 1.0;"),
                "relaxed-residual": engine.replace("if (fabs(error) < 30)", "if (fabs(error) < 120)"),
                "wrong-local-residual": engine.replace("error -= correction;", "error -= ret->pkt->pause_burst_ms;"),
                "audible-raw-acquisition": engine.replace(
                    "buf->pkt->pause_burst_ms = stream->m_processingBuffers->m_inputFormat.m_streamInfo.GetDuration();",
                    "buf->pkt->nb_samples = 960;"),
                "audible-pcm-acquisition": engine.replace(
                    "memset(buf->pkt->data[i], 0, buf->pkt->linesize);",
                    "memset(buf->pkt->data[i], 0x5a, buf->pkt->linesize);"),
            }
            for name, mutant in mutations.items():
                assert mutant != engine, name + " did not change production"
                run(harness(mutant), directory, name, fail=True)


if __name__ == "__main__":
    main()
