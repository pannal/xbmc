#!/usr/bin/env python3
"""Exercise production pause packet returns, ownership and FIFO restoration."""
from pathlib import Path
import argparse
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


def harness(engine, sink, raw_delay=True):
    pause = re.search(r"case CActiveAEControlProtocol::PAUSESTREAM:(.*?)"
                      r"case CActiveAEControlProtocol::RESUMESTREAM:", engine, re.S).group(1)
    pause = pause[pause.index("if (!stream->m_paused"):]
    sink_pause = re.search(r"case CSinkControlProtocol::PAUSE:(.*?)"
                           r"case CSinkControlProtocol::APPFOCUSED:", sink, re.S).group(1)
    fallback = re.search(r"case CSinkDataProtocol::RETURNSAMPLE:(.*?)default:",
                         engine, re.S).group(1)
    stream_source = (AE / "ActiveAEStream.cpp").read_text()
    if not raw_delay:
        stream_source = stream_source.replace(
            block(stream_source, "  if (m_inputFormat.m_dataFormat == AE_FMT_RAW)"), "")
    configure_guard = block(engine, "void CActiveAE::Configure(").split("  bool initSink")[0]
    configure_guard = configure_guard[configure_guard.index("{") + 1:]
    stages_guard = block(engine, "bool CActiveAE::RunStages(").split("  bool busy")[0]
    stages_guard = stages_guard[stages_guard.index("{") + 1:]
    return r'''
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <iostream>
#include <list>
#include <memory>
#include <mutex>
#include <vector>
using namespace std::chrono_literals;
struct CLog {template<class... T> static void Log(int, T...) {}};
constexpr int LOGERROR = 1, LOGINFO = 2;
struct CSinkDataProtocol {enum {RETURNSAMPLE, ACC, PAUSEDSAMPLE, SAMPLE, DRAIN};};
struct CSinkControlProtocol {enum {PAUSE, ACC, ERR, STREAMING, FLUSH, PAUSED};};
struct Packet {
  int nb_samples=61440; double pause_burst_ms=0;
  struct {int sample_rate=48000;} config;
};
struct CSampleBuffer {
  int id, returns = 0; Packet packet{}; Packet* pkt=&packet;
  void Return() {++returns;}
};
struct Port;
struct Message {
  int signal;
  CSampleBuffer* sample = nullptr;
  unsigned char* data = reinterpret_cast<unsigned char*>(&sample);
  Port* port = nullptr;
  bool released = false;
  void Reply(int, void*, size_t);
  void Reply(int value) {signal = value;}
  void Release() {released = true;}
};
struct Port {
  std::deque<Message*> in, out;
  std::vector<std::unique_ptr<Message>> owned;
  void Queue(int signal, CSampleBuffer* sample, bool outgoing) {
    auto message = std::make_unique<Message>();
    message->signal = signal; message->sample = sample; message->port = this;
    auto* pointer = message.get(); owned.push_back(std::move(message));
    (outgoing ? out : in).push_back(pointer);
  }
  bool ReceiveOutMessage(Message** msg) {
    if (out.empty()) return false;
    *msg = out.front(); out.pop_front(); return true;
  }
  bool ReceiveInMessage(Message** msg) {
    if (in.empty()) return false;
    *msg = in.front(); in.pop_front(); return true;
  }
};
void Message::Reply(int value, void* buffer, size_t) {
  port->Queue(value, *static_cast<CSampleBuffer**>(buffer), false);
}
struct CActiveAESink {
  Port m_dataPort;
  void ReturnBuffers(bool preserve = false);
  void Pause(Message* msg) {@SINK_PAUSE@}
};
@RETURN@
struct Control {
  CActiveAESink* sink;
  Control(CActiveAESink* value):sink(value){}
  Message reply{};
  bool timeout = false, error = false, flushTimeout=false;
  std::deque<int> pending;
  bool SendOutMessageSync(int signal, Message** result, std::chrono::seconds) {
    pending.push_back(signal);
    if ((signal==CSinkControlProtocol::PAUSE && timeout) ||
        (signal==CSinkControlProtocol::FLUSH && flushTimeout)) return false;
    while(!pending.empty()) {
      int next=pending.front(); pending.pop_front();
      if(next==CSinkControlProtocol::PAUSE) {
        if(error) reply.signal=CSinkControlProtocol::ERR;
        else sink->Pause(&reply);
      } else {
        assert(next==CSinkControlProtocol::FLUSH);
        sink->ReturnBuffers(); reply.signal=CSinkControlProtocol::ACC;
      }
    }
    *result = &reply; return true;
  }
  void SendOutMessage(int signal, void*, size_t) {
    assert(signal == CSinkControlProtocol::STREAMING || signal == CSinkControlProtocol::PAUSED);
  }
};
constexpr int AE_FMT_RAW=1, AE_FMT_FLOAT=2;
struct CAEStreamInfo {
 enum {STREAM_TYPE_TRUEHD, STREAM_TYPE_AC3}; int m_type = STREAM_TYPE_TRUEHD;
 double GetDuration()const{return m_type==STREAM_TYPE_TRUEHD?20:32;}
};
struct AEAudioFormat {int m_dataFormat=AE_FMT_RAW; CAEStreamInfo m_streamInfo; unsigned int m_sampleRate=192000;};
struct CAESyncInfo {enum AESyncState {SYNC_START, SYNC_INSYNC};};
struct Pool {
 std::deque<CSampleBuffer*> m_outputSamples,m_inputSamples;
 float GetDelay() {
  float result=0;
  for(auto* q:{&m_outputSamples,&m_inputSamples})
   for(auto* b:*q)result+=float(b->pkt->nb_samples)/b->pkt->config.sample_rate;
  return result;
 }
 void Flush(){for(auto* q:{&m_outputSamples,&m_inputSamples}){for(auto* b:*q)b->Return();q->clear();}}
};
struct CActiveAEStreamBuffers:Pool {
 AEAudioFormat m_inputFormat;
 Pool resample,atempo;Pool* m_resampleBuffers=&resample;Pool* m_atempoBuffers=&atempo;
 float GetDelay();
 double GetRR(){return 1;}
 void Flush();
};
@DELAY@
@STREAM_FLUSH@
using Buffers=CActiveAEStreamBuffers;
using CCriticalSection=std::recursive_mutex;
struct CActiveAEStream {
  unsigned int m_id=1; CCriticalSection m_statsLock;
  float m_bufferedTime=0;
  struct {double GetLastError(int64_t& time){time=0;return 0;}} m_syncError;
  std::deque<CSampleBuffer*> m_processingSamples;
  bool m_paused = false, m_drain = false;
  CAESyncInfo::AESyncState m_syncState = CAESyncInfo::SYNC_INSYNC;
  Buffers buffers;
  Buffers* m_processingBuffers = &buffers;
};
struct AEDelayStatus {double delay=0;void SetDelay(double x){delay=x;}};
struct CEngineStats {
 CCriticalSection m_lock;
 AEDelayStatus m_sinkDelay;
 unsigned int m_sinkSampleRate=192000;
 int m_bufferedSamples=0;
 bool m_suspended=false,m_pcmOutput=false;
 double m_sinkLatency=0;
 AEAudioFormat m_sinkFormat;
 struct Stream {
  unsigned int m_streamId=1;
  CAESyncInfo::AESyncState m_syncState=CAESyncInfo::SYNC_INSYNC;
  double m_syncError=0,m_resampleRatio=1,m_bufferedTime=0;
  int64_t m_errorTime=0;
 };
 std::vector<Stream> m_streamStats{Stream{}};
 void Reset(unsigned int,bool);
 void UpdateStream(CActiveAEStream*);
 void GetDelay(AEDelayStatus&,CActiveAEStream*);
 void AddSamples(int,const std::list<CActiveAEStream*>&);
};
@STATS@
struct Stats:CEngineStats {
  int resets = 0, updates = 0;
  void Reset(unsigned int rate, bool pcm) {++resets;CEngineStats::Reset(rate,pcm);}
  void UpdateStream(CActiveAEStream* s) {++updates;CEngineStats::UpdateStream(s);}
};
struct CActiveAE {
  enum Mode {MODE_RAW, MODE_PCM}; Mode m_mode = MODE_RAW;
  AEAudioFormat m_sinkFormat;
  CActiveAESink sink;
  struct Sink {Control m_controlPort; Port& m_dataPort; bool m_needIecPack;
    bool NeedIecPack() const {return m_needIecPack;}} m_sink{{&sink}, sink.m_dataPort, false};
  Buffers buffers;
  Buffers* m_sinkBuffers = &buffers; Buffers* m_vizBuffers=nullptr;
  Stats m_stats;
  CActiveAEStream placeholder;
  std::list<CActiveAEStream*> m_streams{&placeholder};
  std::deque<CSampleBuffer*> m_pausedSinkSamples;
  bool m_pausePreserved = false, m_resumeEpoch = false;
  void ReleasePausedSinkSamples();
  bool m_extError = false, m_sinkFlushPending=false;
  int m_state=0,configured=0,submitted=0;
  static constexpr int AE_TOP_CONFIGURED_PLAY=2;
  std::chrono::milliseconds m_extTimeout{0};
  bool FlushEngine();
  void Configure(){@CONFIGURE_GUARD@ ++configured;}
  bool RunStages(){@STAGES_GUARD@ ++submitted;return true;}
  void SPreservePausedPackets(CActiveAEStream*);
  void Pause(CActiveAEStream* stream) {bool streaming = true; @PAUSE@}
  void Fallback(Message* msg) {@FALLBACK@}
};
@PRESERVE@
@RELEASE@
@FLUSH@
int main() {
  CSampleBuffer played{0}, a{1}, b{2}, c{3}, d{4}, e{5};
  CActiveAE engine;
  CActiveAEStream stream;
  engine.sink.m_dataPort.Queue(CSinkDataProtocol::RETURNSAMPLE, &played, false);
  engine.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE, &a, true);
  engine.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE, &b, true);
  engine.buffers.m_outputSamples.push_back(&c);
  engine.buffers.m_inputSamples.push_back(&d);
  stream.buffers.m_outputSamples.push_back(&e);
  engine.Pause(&stream);
  assert(stream.m_paused && !engine.m_extError);
  assert(played.returns == 1 && a.returns == 0 && b.returns == 0);
  assert(engine.m_stats.resets == 1 && engine.m_stats.updates == 1);
  AEDelayStatus delay;
  engine.m_stats.GetDelay(delay,&stream);
  assert(std::abs(delay.delay-.100)<.00001);
  assert(engine.buffers.m_inputSamples.empty() && engine.buffers.m_outputSamples.empty());
  std::vector<int> ids;
  for (auto* sample : stream.buffers.m_outputSamples) ids.push_back(sample->id);
  assert((ids == std::vector<int>{1,2,3,4,5}));
  // A subsequent seek can return each recovered packet exactly once.
  for (auto* sample : stream.buffers.m_outputSamples) sample->Return();
  stream.buffers.m_outputSamples.clear();
  for (auto* sample : {&a, &b, &c, &d, &e}) assert(sample->returns == 1);
  CSampleBuffer late{6};
  Message lateMessage; lateMessage.sample = &late; lateMessage.signal = CSinkDataProtocol::PAUSEDSAMPLE;
  engine.Fallback(&lateMessage); assert(late.returns == 1);
  for (bool pcm : {false, true}) {
    // Draining PCM and AC3 through a sink-side IEC packer keep the flush-on-pause path.
    CActiveAE other; CActiveAEStream audio;
    if (pcm) {other.m_mode = CActiveAE::MODE_PCM; audio.m_drain = true;}
    else other.m_sinkFormat.m_streamInfo.m_type = CAEStreamInfo::STREAM_TYPE_AC3;
    other.Pause(&audio);
    assert(other.m_stats.resets==1 && other.m_stats.updates==0 && audio.m_paused);
    assert(!other.m_pausePreserved);
  }
  for (bool pcm : {false, true}) {
    // In-sync PCM and Kodi-packed AC3 are preserved behind the resume gate instead.
    CActiveAE kept; CActiveAEStream audio; CSampleBuffer held{12}, queued{13};
    kept.m_streams = {&audio};
    kept.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE, &held, true);
    kept.buffers.m_outputSamples.push_back(&queued);
    if (pcm) kept.m_mode = CActiveAE::MODE_PCM;
    else {kept.m_sinkFormat.m_streamInfo.m_type = CAEStreamInfo::STREAM_TYPE_AC3; kept.m_sink.m_needIecPack = true;}
    kept.Pause(&audio);
    assert(audio.m_paused && kept.m_pausePreserved && !kept.m_resumeEpoch && kept.m_stats.updates==1);
    std::vector<int> order;
    for (auto* sample : pcm ? kept.m_pausedSinkSamples : audio.buffers.m_outputSamples) order.push_back(sample->id);
    assert((order == std::vector<int>{12,13}) && kept.buffers.m_outputSamples.empty());
    assert(kept.m_stats.resets == (pcm ? 0 : 1));
    assert(pcm ? audio.buffers.m_outputSamples.empty() : kept.m_pausedSinkSamples.empty());
    assert(kept.FlushEngine() && !kept.m_pausePreserved && kept.m_pausedSinkSamples.empty());
    assert(audio.m_syncState==CAESyncInfo::SYNC_START);
    assert(held.returns == (pcm ? 1 : 0) && queued.returns == (pcm ? 1 : 0));
  }
  for (bool timeout : {false, true}) {
    CActiveAE failed; CActiveAEStream audio;
    failed.m_sink.m_controlPort.timeout = timeout;
    failed.m_sink.m_controlPort.error = !timeout;
    CSampleBuffer discarded{7};
    failed.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE,&discarded,true);
    failed.Pause(&audio);
    assert(!failed.m_extError && audio.m_paused && discarded.returns==1);
    assert(audio.m_syncState==CAESyncInfo::SYNC_START && failed.m_stats.resets==1);
  }
  CActiveAE blocked; CActiveAEStream waiting; CSampleBuffer outstanding{8};
  blocked.m_stats.m_bufferedSamples=1;
  blocked.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE,&outstanding,true);
  blocked.m_sink.m_controlPort.timeout=true;
  blocked.m_sink.m_controlPort.flushTimeout=true;
  blocked.Pause(&waiting);
  assert(blocked.m_sinkFlushPending && blocked.m_extError && outstanding.returns==0);
  assert(blocked.m_stats.resets==0 && blocked.m_stats.m_bufferedSamples==1);
  blocked.Configure(); assert(blocked.configured==0);
  assert(!blocked.RunStages() && blocked.submitted==0);
  blocked.m_sink.m_controlPort.flushTimeout=false;
  blocked.Configure();
  assert(!blocked.m_sinkFlushPending && blocked.configured==1);
  assert(outstanding.returns==1 && blocked.m_stats.m_bufferedSamples==0);
  assert(blocked.RunStages() && blocked.submitted==1);
  CActiveAE empty; CActiveAEStream audio;
  empty.Pause(&audio); assert(!empty.m_extError && audio.buffers.m_outputSamples.empty());
  empty.Pause(&audio); assert(empty.m_stats.resets == 1);
  CActiveAE initial; CActiveAEStream starting;
  starting.m_syncState = CAESyncInfo::SYNC_START;
  initial.Pause(&starting); assert(initial.m_stats.resets==1);
  CActiveAE shared; CActiveAEStream mixed;
  shared.m_streams.push_back(&mixed);
  shared.Pause(&mixed); assert(shared.m_stats.resets == 0);
  CActiveAEStreamBuffers raw;
  CSampleBuffer mat{9}, pause{10}, dropped{11};
  pause.pkt->nb_samples=0;pause.pkt->pause_burst_ms=7;
  dropped.pkt->nb_samples=0;
  raw.m_outputSamples={&mat,&pause,&dropped};
  for(int rate:{48000,96000,192000}){
   mat.pkt->config.sample_rate=rate;assert(std::abs(raw.GetDelay()-.027)<.00001);
  }
  raw.m_inputFormat.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_AC3;
  assert(std::abs(raw.GetDelay()-.039)<.00001);
  raw.Flush();assert(mat.returns==1 && pause.returns==1 && dropped.returns==1);
  for(auto* queue:{&raw.m_inputSamples,&raw.m_outputSamples,
                  &raw.resample.m_inputSamples,&raw.resample.m_outputSamples,
                  &raw.atempo.m_inputSamples,&raw.atempo.m_outputSamples})queue->push_back(&mat);
  raw.m_inputFormat.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_TRUEHD;
  assert(std::abs(raw.GetDelay()-.120)<.00001);
  raw.m_inputFormat.m_dataFormat=AE_FMT_FLOAT;
  mat.pkt->nb_samples=480;mat.pkt->config.sample_rate=48000;
  assert(std::abs(raw.GetDelay()-.060)<.00001);
  std::cout << "production paused packet FIFO, ownership, fallback and error cases passed\n";
}
'''.replace("@SINK_PAUSE@", sink_pause).replace(
        "@RETURN@", block(sink, "void CActiveAESink::ReturnBuffers(")).replace(
        "@PAUSE@", pause).replace("@FALLBACK@", fallback).replace(
        "@PRESERVE@", block(engine, "void CActiveAE::SPreservePausedPackets(")).replace(
        "@RELEASE@", block(engine, "void CActiveAE::ReleasePausedSinkSamples(")).replace(
        "@FLUSH@", block(engine, "bool CActiveAE::FlushEngine(")).replace(
        "@DELAY@", block(stream_source, "float CActiveAEStreamBuffers::GetDelay(")).replace(
        "@STREAM_FLUSH@", block(stream_source, "void CActiveAEStreamBuffers::Flush(")).replace(
        "@STATS@", "\n".join(block(engine, signature) for signature in (
            "void CEngineStats::Reset(", "void CEngineStats::UpdateStream(", "void CEngineStats::AddSamples(",
            "void CEngineStats::GetDelay(AEDelayStatus& status, CActiveAEStream *stream)"))).replace(
        "@CONFIGURE_GUARD@", configure_guard).replace("@STAGES_GUARD@", stages_guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args()
    engine = (AE / "ActiveAE.cpp").read_text()
    sink = (AE / "ActiveAESink.cpp").read_text()
    with tempfile.TemporaryDirectory(prefix="ae-paused-packets-") as directory:
        root = Path(directory)
        variants = [("production", engine, sink)]
        if args.negative_control:
            variants.extend([
                ("discard-pending", engine, sink.replace("ReturnBuffers(true);", "ReturnBuffers();")),
                ("reordered-packets", engine.replace("output.insert(output.begin(), packets.begin()",
                                                    "output.insert(output.end(), packets.begin()"), sink),
                ("duplicated-ownership", engine.replace("queue->clear();", ""), sink),
                ("pcm-preservation", engine.replace("const bool trueHD = m_mode == MODE_RAW &&",
                                                   "const bool trueHD = m_mode >= MODE_RAW &&"), sink),
                ("late-return-leak", engine.replace("(*buffer)->Return();", ""), sink),
                ("unbarriered-recovery", engine.replace(
                    "if (m_sinkFlushPending && !FlushEngine())",
                    "if (false && m_sinkFlushPending && !FlushEngine())"), sink),
                ("write-before-barrier", engine.replace(
                    "if (m_sinkFlushPending)\n    return false;",
                    "if (false && m_sinkFlushPending)\n    return false;"), sink),
                ("raw-byte-delay", engine, sink),
            ])
        for name, implementation, sink_implementation in variants:
            if name not in ("production", "raw-byte-delay"):
                assert implementation != engine or sink_implementation != sink, name
            source, binary = root / (name + ".cpp"), root / name
            source.write_text(harness(implementation, sink_implementation,
                                      raw_delay=name != "raw-byte-delay"))
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(source), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if name == "production":
                print(result.stdout, end="")
                result.check_returncode()
            else:
                assert result.returncode != 0 and "Assertion" in result.stderr, (name, result.stderr)
                print(name + ": rejected")


if __name__ == "__main__":
    main()
