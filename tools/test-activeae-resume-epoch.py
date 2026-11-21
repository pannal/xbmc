#!/usr/bin/env python3
"""Exercise the production resume epoch: one re-anchor of preserved pause audio.

SResumeStream, RunResumeEpoch and the preservation release/abandon/flush helpers
are production code. Clock, delay stats, buffer pools and sink ports are controlled
host boundaries; delay is a fixed sample (no elapsed-time correction). This checks
padding units, FIFO order and fallbacks, not receiver lock or live sink delay.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "paused", ROOT / "tools/test-activeae-paused-packets.py")
paused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paused)
AE = ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE"


def harness(engine):
    methods = "\n".join(paused.block(engine, marker) for marker in (
        "void CActiveAE::ReleasePausedSinkSamples(", "void CActiveAE::AbandonPausePreservation(",
        "void CActiveAE::SendPausedSinkSamples(", "bool CActiveAE::RunResumeEpoch(",
        "void CActiveAE::SResumeStream(", "bool CActiveAE::FlushEngine("))
    return PREFIX + methods + TESTS


PREFIX = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <iostream>
#include <list>
#include <memory>
#include <vector>
using namespace std::chrono_literals;
static long nowMs = 0;
namespace XbmcThreads {
template<class T = void> struct EndTime {
  long expires = 0;
  void Set(std::chrono::milliseconds value) {expires = nowMs + value.count();}
  bool IsTimePast() const {return nowMs >= expires;}
};
}
constexpr int LOGERROR = 1, LOGINFO = 2;
struct CLog {template<class... T> static void Log(int, T...) {}};
struct CAESyncInfo {enum AESyncState {SYNC_START, SYNC_INSYNC};};
struct CAEStreamInfo {
  enum {STREAM_TYPE_TRUEHD, STREAM_TYPE_AC3};
  int m_type = STREAM_TYPE_AC3;
  double GetDuration() const {return 32;}
};
struct AEAudioFormat {unsigned int m_sampleRate = 48000, m_frames = 1024; CAEStreamInfo m_streamInfo;};
struct Packet {
  struct {int sample_rate = 48000;} config;
  int nb_samples = 0, max_nb_samples = 1024, pause_burst_ms = 0, planes = 1, linesize = 16;
  unsigned char storage[16];
  unsigned char* data[1]{storage};
};
struct CSampleBuffer {
  int returns = 0;
  Packet packet;
  Packet* pkt = &packet;
  double timestamp = 0;
  int pkt_start_offset = 0;
  void Return() {++returns;}
};
struct CActiveAEBufferPool {
  AEAudioFormat m_format;
  std::vector<std::unique_ptr<CSampleBuffer>> all;
  size_t used = 0;
  explicit CActiveAEBufferPool(const AEAudioFormat& format) : m_format(format) {}
  bool Create(unsigned int) {
    for (int i = 0; i < 32; i++) {
      all.push_back(std::make_unique<CSampleBuffer>());
      all.back()->pkt->max_nb_samples = m_format.m_frames;
      std::memset(all.back()->pkt->storage, 0x5a, sizeof(Packet::storage));
    }
    return true;
  }
  CSampleBuffer* GetFreeBuffer() {return used < all.size() ? all[used++].get() : nullptr;}
};
struct SinkBuffers {
  std::deque<CSampleBuffer*> m_inputSamples, m_outputSamples;
  void Flush() {
    for (auto* q : {&m_inputSamples, &m_outputSamples}) {for (auto* b : *q) b->Return(); q->clear();}
  }
};
struct CActiveAEStream {
  struct Clock {double value = 1000; double GetClock() const {return value;}} clock;
  Clock* m_pClock = &clock;
  struct Buffers {std::deque<CSampleBuffer*> m_outputSamples;} buffers;
  Buffers* m_processingBuffers = &buffers;
  bool m_paused = true;
  CAESyncInfo::AESyncState m_syncState = CAESyncInfo::SYNC_INSYNC;
  struct {int flushes = 0; void Flush(std::chrono::milliseconds) {++flushes;}} m_syncError;
  std::chrono::milliseconds GetErrorInterval() const {return 1000ms;}
};
struct AEDelayStatus {
  double delay = 0;
  std::chrono::steady_clock::time_point startTime{};
  double GetDelay() const {return delay;}
};
struct Stats {
  double delay = .1;
  bool stale = false;
  int added = 0, updates = 0, resets = 0;
  void GetDelay(AEDelayStatus& status) {
    status.delay = delay;
    status.startTime = std::chrono::steady_clock::now() - (stale ? 300ms : 0ms);
  }
  void AddSamples(int samples, const std::list<CActiveAEStream*>&) {added += samples;}
  void UpdateStream(CActiveAEStream*) {++updates;}
  void Reset(unsigned int, bool) {++resets;}
};
struct CSinkDataProtocol {enum {SAMPLE, RETURNSAMPLE, PAUSEDSAMPLE, ACC};};
struct CSinkControlProtocol {enum {FLUSH, ACC};};
struct Message {int signal = CSinkControlProtocol::ACC; unsigned char* data = nullptr; void Release() {}};
struct CActiveAE {
  enum Mode {MODE_RAW, MODE_PCM};
  Mode m_mode = MODE_RAW;
  AEAudioFormat m_sinkFormat;
  std::list<CActiveAEStream*> m_streams;
  Stats m_stats;
  SinkBuffers sinkBuffers;
  SinkBuffers* m_sinkBuffers = &sinkBuffers;
  SinkBuffers* m_vizBuffers = nullptr;
  std::unique_ptr<CActiveAEBufferPool> m_silenceBuffers, m_resumePadBuffers;
  std::vector<std::unique_ptr<CActiveAEBufferPool>> m_discardBufferPools;
  std::deque<CSampleBuffer*> m_pausedSinkSamples;
  bool m_pausePreserved = false, m_resumeEpoch = false, m_sinkFlushPending = false, m_extError = false;
  XbmcThreads::EndTime<> m_resumeEpochTimer;
  struct {
    struct {
      int flushes = 0;
      Message reply;
      bool SendOutMessageSync(int signal, Message** result, std::chrono::seconds) {
        assert(signal == CSinkControlProtocol::FLUSH);
        ++flushes; *result = &reply; return true;
      }
    } m_controlPort;
    struct {
      std::vector<CSampleBuffer*> sent;
      void SendOutMessage(int signal, void* data, size_t) {
        assert(signal == CSinkDataProtocol::SAMPLE);
        sent.push_back(*static_cast<CSampleBuffer**>(data));
      }
      bool ReceiveInMessage(Message**) {return false;}
    } m_dataPort;
  } m_sink;
  CActiveAE() {
    m_silenceBuffers = std::make_unique<CActiveAEBufferPool>(m_sinkFormat);
    m_silenceBuffers->Create(0);
  }
  bool CompareFormat(const AEAudioFormat& a, const AEAudioFormat& b) {
    return a.m_sampleRate == b.m_sampleRate;
  }
  void ReleasePausedSinkSamples();
  void AbandonPausePreservation();
  void SendPausedSinkSamples();
  bool RunResumeEpoch();
  void SResumeStream(CActiveAEStream* stream);
  bool FlushEngine();
};
'''

TESTS = r'''
struct Fixture {
  CActiveAE engine;
  CActiveAEStream stream;
  std::deque<std::unique_ptr<CSampleBuffer>> owned;
  explicit Fixture(bool pcm) {
    nowMs = 0;
    engine.m_streams.push_back(&stream);
    if (pcm) engine.m_mode = CActiveAE::MODE_PCM;
  }
  CSampleBuffer* make(int frames) {
    owned.push_back(std::make_unique<CSampleBuffer>());
    owned.back()->pkt->nb_samples = frames;
    return owned.back().get();
  }
  // Clock 1000 ms and delay 100 ms: a packet stamped 1100 + error has that error.
  CSampleBuffer* add(double timestamp, int offset = 0, int frames = 1536) {
    auto* buffer = make(frames);
    buffer->timestamp = timestamp; buffer->pkt_start_offset = offset;
    stream.buffers.m_outputSamples.push_back(buffer);
    return buffer;
  }
  CSampleBuffer* hold(int frames) {
    auto* buffer = make(frames);
    engine.m_pausedSinkSamples.push_back(buffer);
    return buffer;
  }
  void open() {
    engine.m_pausePreserved = true;
    engine.SResumeStream(&stream);
    assert(engine.m_resumeEpoch && !engine.m_pausePreserved && !stream.m_paused);
  }
  void done() {
    assert(!engine.m_resumeEpoch && !engine.m_pausePreserved && engine.m_pausedSinkSamples.empty());
    assert(engine.m_sink.m_controlPort.flushes == 0 && stream.m_syncError.flushes == 1);
    assert(stream.m_syncState == CAESyncInfo::SYNC_INSYNC && engine.m_stats.updates == 1);
  }
  void fallback() {
    assert(engine.RunResumeEpoch());
    assert(!engine.m_resumeEpoch && !engine.m_pausePreserved && engine.m_pausedSinkSamples.empty());
    assert(engine.m_sink.m_controlPort.flushes == 1 && engine.m_stats.resets == 1);
    assert(engine.m_sink.m_dataPort.sent.empty() && engine.sinkBuffers.m_inputSamples.empty());
    assert(stream.m_syncState == CAESyncInfo::SYNC_START && stream.m_syncError.flushes == 0);
    assert(engine.m_stats.updates == 1 && engine.m_stats.added <= 0);
  }
};
int main() {
  // RAW: whole packet-duration pause bursts ahead of the preserved packets.
  for (auto [error, bursts] : {std::pair{100.0, 3}, {-29.0, 0}, {499.0, 16}}) {
    Fixture f(false);
    auto* packet = f.add(1100 + error);
    f.open();
    assert(f.engine.RunResumeEpoch());
    f.done();
    auto& input = f.engine.sinkBuffers.m_inputSamples;
    assert(static_cast<int>(input.size()) == bursts && f.engine.m_stats.added == bursts);
    for (auto* burst : input)
      assert(burst->pkt->nb_samples == 0 && burst->pkt->pause_burst_ms == 32 && burst->timestamp == 0);
    assert(f.engine.m_sink.m_dataPort.sent.empty());
    assert(f.stream.buffers.m_outputSamples.size() == 1 && f.stream.buffers.m_outputSamples[0] == packet);
  }
  // Leading untimed RAW pause bursts are already ahead of the timed packet.
  {
    Fixture f(false);
    f.add(0, 0, 0); f.add(0, 0, 0);
    f.add(1100 + 64 + 2 * 32);
    f.open();
    assert(f.engine.RunResumeEpoch());
    f.done();
    assert(f.engine.sinkBuffers.m_inputSamples.size() == 2 && f.engine.m_stats.added == 2);
  }
  // PCM: zeroed sink-format pad frames, then held samples in their original order.
  for (auto [error, frames] : {std::pair{50.0, 2400}, {-29.0, 0}}) {
    Fixture f(true);
    f.add(1110 + error, 480, 1024);  // 480 frames at 48 kHz already played: 10 ms
    auto* a = f.hold(480);
    auto* b = f.hold(480);
    f.open();
    assert(f.engine.RunResumeEpoch());
    f.done();
    auto& sent = f.engine.m_sink.m_dataPort.sent;
    assert(sent.size() >= 2 && sent[sent.size() - 2] == a && sent.back() == b);
    int padded = 0;
    for (size_t i = 0; i + 2 < sent.size(); i++) {
      auto* pad = sent[i]->pkt;
      assert(pad->nb_samples > 0 && pad->nb_samples <= 1024 && pad->pause_burst_ms == 0);
      assert(sent[i]->timestamp == 0);
      for (auto byte : pad->storage) assert(byte == 0);
      padded += pad->nb_samples;
    }
    assert(padded == frames && f.engine.m_stats.added == frames);
    assert(a->returns == 0 && b->returns == 0 && f.engine.sinkBuffers.m_inputSamples.empty());
  }
  // Outside the window, or with a stale sink delay: flush and reacquire, never pad.
  for (bool pcm : {false, true}) for (int which = 0; which < 3; which++) {
    Fixture f(pcm);
    f.add(1100 + (which == 0 ? -31.0 : which == 1 ? 501.0 : 50.0));
    auto* held = pcm ? f.hold(480) : nullptr;
    f.engine.m_stats.stale = which == 2;
    f.open();
    f.fallback();
    assert(!held || held->returns == 1);
  }
  // No timed audio yet: keep waiting until the epoch timer expires, then reacquire.
  {
    Fixture f(false);
    f.add(0, 0, 0); f.add(0, 0, 0);
    f.open();
    nowMs = 999;
    assert(!f.engine.RunResumeEpoch() && f.engine.m_resumeEpoch);
    assert(f.engine.m_sink.m_controlPort.flushes == 0 && f.engine.sinkBuffers.m_inputSamples.empty());
    assert(f.stream.m_syncState == CAESyncInfo::SYNC_INSYNC && f.engine.m_stats.updates == 0);
    nowMs = 1000;
    f.fallback();
  }
  {
    // PCM only anchors on the frontier packet itself.
    Fixture f(true);
    f.add(0); f.add(1150);
    f.open();
    assert(!f.engine.RunResumeEpoch() && f.engine.m_resumeEpoch);
  }
  std::cout << "PASS production resume epoch: RAW bursts, leading untimed bursts, PCM pad/FIFO, "
               "window/stale fallbacks and timed-audio wait\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    engine = (AE / "ActiveAE.cpp").read_text()
    variants = [("production", engine)]
    if args.negative_controls:
        for label, old, new in (
            ("wider-negative-window", "error < -30.0", "error < -40.0"),
            ("counted-leading-bursts", "pts -= index * duration;", ""),
            ("padded-negative-error", "std::max(error, 0.0)", "std::abs(error)"),
            ("stale-sink-trusted", "std::chrono::milliseconds(250)", "std::chrono::milliseconds(500)"),
        ):
            assert engine.count(old) == 1, label
            variants.append((label, engine.replace(old, new)))
    with tempfile.TemporaryDirectory(prefix="ae-resume-epoch-") as temp:
        for label, impl in variants:
            cpp, exe = Path(temp) / (label + ".cpp"), Path(temp) / label
            cpp.write_text(harness(impl))
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            if label == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            else:
                assert result.returncode and "Assertion" in result.stderr, (label, result.stderr)
                print("REJECTED:", label)


if __name__ == "__main__":
    main()
