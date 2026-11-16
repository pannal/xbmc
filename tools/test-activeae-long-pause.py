#!/usr/bin/env python3
"""Exercise production sink states/timers through long TrueHD pauses.

Clock, actor delivery and transport are controlled host boundaries. This tests
transport ownership and power management, not receiver lock or MAT validity.
"""
import argparse
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


def harness(sink, engine):
    header = (AE / "ActiveAESink.h").read_text()
    control = block(header, "class CSinkControlProtocol")
    data = block(header, "class CSinkDataProtocol")
    # The control enums need their class scope, unlike the sink state enum.
    enums = block(sink, "enum SINK_STATES") + ";\nstruct CSinkControlProtocol {\n" + (
        block(control, "enum OutSignal") + ";\n" +
        block(control, "enum InSignal") + ";\n};\nstruct CSinkDataProtocol {\n" +
        block(data, "enum OutSignal") + ";\n" +
        block(data, "enum InSignal") + ";\n};")
    parents = re.search(r"int SINK_parentStates\[\].*?;", sink, re.S).group()
    pause = re.search(r"case CActiveAEControlProtocol::PAUSESTREAM:(.*?)"
                      r"case CActiveAEControlProtocol::RESUMESTREAM:", engine, re.S).group(1)
    pause = pause.split("stream = *(CActiveAEStream**)msg->data;", 1)[1]
    resume = re.findall(r"case CActiveAEControlProtocol::RESUMESTREAM:(.*?)"
                        r"case CActiveAEControlProtocol::FLUSHSTREAM:", engine, re.S)[1]
    resume = resume.split("stream = *(CActiveAEStream**)msg->data;", 1)[1]
    resume = resume[:resume.index("m_state =")]
    discard = block(engine, "void CActiveAE::DiscardStream(")
    free = block(discard, "  if (m_streams.empty())")
    return r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
using namespace std::chrono_literals;
static std::chrono::milliseconds now{0};
namespace XbmcThreads {
template<class T = std::chrono::milliseconds> struct EndTime {
  std::chrono::milliseconds expires{0};
  static T Max() {return T::max();}
  void Set(std::chrono::milliseconds value) {
    expires=value==std::chrono::milliseconds::max()?value:now+value;
  }
  bool IsTimePast() const {return now >= expires;}
};
}
@ENUMS@
@PARENTS@
constexpr int LOGWARNING=0, LOGERROR=1, AE_FMT_RAW=1, AE_FMT_FLOAT=2;
struct CLog {template<class... T> static void Log(int,T...) {}};
struct CThread {static void Sleep(std::chrono::milliseconds) {}};
struct Protocol {std::string portName="test";};
struct Message {
  void* data=nullptr;
  int reply=-1, returns=0;
  void Reply(int value) {reply=value;}
  void Reply(int value, void*, size_t) {reply=value;++returns;}
};
struct CAEStreamInfo {
  enum {STREAM_TYPE_TRUEHD, STREAM_TYPE_AC3};
  int m_type=STREAM_TYPE_TRUEHD;
};
struct Format {
  int m_dataFormat=AE_FMT_RAW, m_frames=61440, m_sampleRate=192000;
  CAEStreamInfo m_streamInfo;
};
struct Stats {};
struct SinkConfig {Format format;Stats* stats;std::string* device;};
struct SinkReply {Format format;double cacheTotal,latency;bool hasVolume;};
struct Packet {
  int nb_samples=0;
  struct {int sample_rate=192000;} config;
};
struct CSampleBuffer {std::unique_ptr<Packet> pkt=std::make_unique<Packet>();};
struct TransportStats {int opens=1, drains=0, closes=0, silence=0, audio=0;};
struct Transport {
  TransportStats* stats;
  void Drain(){++stats->drains;}
  void Deinitialize(){++stats->closes;}
  double GetCacheTotal(){return .1;}
  double GetLatency(){return 0;}
  bool HasVolume(){return false;}
  void SetVolume(float){}
};
struct CActiveAESink {
  Protocol m_controlPort,m_dataPort;
  int m_state=S_TOP_CONFIGURED_PLAY,m_sinkLatency=0;
  bool m_extError=false,m_extStreaming=true,m_extPaused=false,m_extAppFocused=true;
  bool m_streamNoise=false,m_needIecPack=true,m_bStateMachineSelfTrigger=false;
  std::chrono::milliseconds m_extTimeout{0},m_extSilenceTimeout{0};
  std::chrono::minutes m_silenceTimeOut{1};
  XbmcThreads::EndTime<> m_extSilenceTimer;
  Format m_requestedFormat,m_sinkFormat;
  std::string m_device;
  Stats* m_stats=nullptr;
  float m_volume=0;
  CSampleBuffer m_sampleOfSilence;
  TransportStats transport;
  std::unique_ptr<Transport> m_sink=std::make_unique<Transport>(Transport{&transport});
  bool openFails=false,writeFails=false;
  void OpenSink() {
    if(openFails){m_extError=true;return;}
    ++transport.opens;
    m_sink=std::make_unique<Transport>(Transport{&transport});
  }
  void ReturnBuffers(bool=false){}
  void GenerateNoise(){}
  unsigned int OutputSamples(CSampleBuffer* sample) {
    assert(m_sink);
    if(writeFails)m_extError=true;
    else if(sample==&m_sampleOfSilence)++transport.silence;
    else ++transport.audio;
    return 20;
  }
  void SetSilenceTimer();
  void StateMachine(int,Protocol*,Message*);
  void Control(int signal,bool value) {
    Message msg;msg.data=&value;StateMachine(signal,&m_controlPort,&msg);
  }
  void Tick(std::chrono::milliseconds time) {
    now=time;Message msg;StateMachine(CSinkControlProtocol::TIMEOUT,nullptr,&msg);
  }
  void Sample() {
    CSampleBuffer sample;CSampleBuffer* pointer=&sample;
    Message msg;msg.data=&pointer;
    StateMachine(CSinkDataProtocol::SAMPLE,&m_dataPort,&msg);
    if(m_bStateMachineSelfTrigger){
      m_bStateMachineSelfTrigger=false;
      StateMachine(CSinkDataProtocol::SAMPLE,&m_dataPort,&msg);
    }
    assert(msg.returns==1);
  }
};
@TIMER@
@STATES@
struct CAESyncInfo {enum AESyncState {SYNC_START,SYNC_INSYNC};};
struct CActiveAEStream {
  bool m_paused=false,m_drain=false;
  CAESyncInfo::AESyncState m_syncState=CAESyncInfo::SYNC_INSYNC;
};
struct CActiveAE {
  enum {MODE_RAW,MODE_PCM};
  int m_mode=MODE_RAW;
  Format m_sinkFormat;
  std::vector<int> m_streams{1};
  CActiveAESink sink;
  struct {
    struct Port {
      CActiveAESink* sink;
      void SendOutMessage(int signal,void* data,size_t) {
        sink->Control(signal,*static_cast<bool*>(data));
      }
    } m_controlPort;
    bool NeedIecPack() const {return m_controlPort.sink->m_needIecPack;}
  } m_sink{{&sink}};
  void SPreservePausedPackets(CActiveAEStream*){}
  void FlushEngine(){}
  // SPreservePausedPackets is a no-op here, so there is never preserved audio to release.
  void ReleasePausedSinkSamples(){}
  void SResumeStream(CActiveAEStream* stream){stream->m_paused=false;}
  void Pause(CActiveAEStream* stream) {bool streaming;@PAUSE@}
  void ResumeIdle(CActiveAEStream* stream) {bool streaming;@RESUME@}
  void FreeStream() {m_streams.clear();@FREE@}
};
static bool configured(const CActiveAESink& sink) {
  return sink.m_state==S_TOP_CONFIGURED_PLAY || sink.m_state==S_TOP_CONFIGURED_SILENCE;
}
int main(){
  for(auto paused:{65001ms,75000ms,3600000ms}){
    now=0ms;CActiveAE engine;CActiveAEStream stream;
    engine.Pause(&stream);
    auto& sink=engine.sink;
    sink.Tick(paused);sink.Tick(paused+1ms);
    assert(configured(sink) && sink.transport.closes==0 && sink.transport.drains==0);
    engine.ResumeIdle(&stream);
    assert(sink.m_extStreaming && !sink.m_extPaused);
    sink.Sample();
    assert(sink.transport.opens==1 && sink.transport.audio==1);
    engine.Pause(&stream);
    engine.FreeStream();
    sink.Tick(paused+60002ms);sink.Tick(paused+60003ms);sink.Tick(paused+70003ms);
    assert(sink.transport.drains==1 && sink.transport.closes==1);
  }
  // PCM, AC3, AudioTrack RAW and disabled keepalive retain their silence policy.
  for(int mode=0;mode<4;++mode){
    now=0ms;CActiveAE engine;CActiveAEStream stream;
    if(mode==0){engine.m_mode=CActiveAE::MODE_PCM;engine.sink.m_requestedFormat.m_dataFormat=AE_FMT_FLOAT;}
    if(mode==1){engine.m_sinkFormat.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_AC3;
               engine.sink.m_sinkFormat.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_AC3;}
    if(mode==2)engine.sink.m_needIecPack=false;
    if(mode==3)engine.sink.m_silenceTimeOut=0min;
    engine.Pause(&stream);
    engine.sink.Tick(75000ms);engine.sink.Tick(75001ms);engine.sink.Tick(85001ms);
    assert(engine.sink.transport.drains==1 && engine.sink.transport.closes==1);
    engine.ResumeIdle(&stream);engine.sink.Sample();
    assert(engine.sink.transport.opens==2 && engine.sink.transport.audio==1);
  }
  {
    now=0ms;CActiveAE engine;CActiveAEStream stream;engine.Pause(&stream);
    engine.sink.Control(CSinkControlProtocol::APPFOCUSED,false);
    engine.sink.Tick(0ms);engine.sink.Tick(0ms);engine.sink.Tick(0ms);
    assert(engine.sink.transport.closes==1);
  }
  {
    now=0ms;CActiveAE engine;CActiveAEStream stream;engine.Pause(&stream);
    int timeout=0;Message msg;msg.data=&timeout;
    engine.sink.StateMachine(CSinkControlProtocol::SETSILENCETIMEOUT,
                            &engine.sink.m_controlPort,&msg);
    engine.sink.Tick(0ms);engine.sink.Tick(0ms);engine.sink.Tick(10000ms);
    assert(engine.sink.transport.closes==1);
  }
  {
    now=0ms;CActiveAE engine;CActiveAEStream stream;engine.Pause(&stream);
    Message msg;engine.sink.StateMachine(CSinkControlProtocol::UNCONFIGURE,
                                        &engine.sink.m_controlPort,&msg);
    assert(!engine.sink.m_extPaused && engine.sink.transport.closes==1);
  }
  {
    now=0ms;CActiveAE engine;CActiveAEStream stream;engine.Pause(&stream);
    engine.sink.writeFails=true;engine.sink.Tick(75000ms);
    assert(engine.sink.m_extError && engine.sink.m_state==S_TOP_CONFIGURED_SUSPEND);
    engine.ResumeIdle(&stream);engine.sink.openFails=true;
    CSampleBuffer sample;CSampleBuffer* pointer=&sample;Message msg;msg.data=&pointer;
    engine.sink.StateMachine(CSinkDataProtocol::SAMPLE,&engine.sink.m_dataPort,&msg);
    assert(engine.sink.m_extError && engine.sink.m_state==S_TOP_UNCONFIGURED);
    engine.FreeStream();
    assert(!engine.sink.m_extPaused && !engine.sink.m_sink);
  }
  {
    now=0ms;CActiveAE engine;CActiveAEStream stream;engine.Pause(&stream);
    Stats stats;std::string device;SinkConfig config{Format{},&stats,&device};
    Message msg;msg.data=&config;
    engine.sink.StateMachine(CSinkControlProtocol::CONFIGURE,&engine.sink.m_controlPort,&msg);
    assert(!engine.sink.m_extPaused && !engine.sink.m_extStreaming);
    assert(msg.reply==CSinkControlProtocol::ACC);
  }
  std::cout<<"production long-pause transport ownership, resume and power management passed\n";
}
'''.replace("@ENUMS@", enums).replace("@PARENTS@", parents).replace(
        "@STATES@", block(sink, "void CActiveAESink::StateMachine(")).replace(
        "@TIMER@", block(sink, "void CActiveAESink::SetSilenceTimer(")).replace(
        "@PAUSE@", pause).replace("@RESUME@", resume).replace("@FREE@", free)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args()
    sink = (AE / "ActiveAESink.cpp").read_text()
    engine = (AE / "ActiveAE.cpp").read_text()
    variants = [("production", sink, engine)]
    if args.negative_control:
        variants.extend((
            ("idle-during-pause", sink.replace("m_extPaused && m_silenceTimeOut", "false && m_silenceTimeOut"), engine),
            ("missing-pause-ownership", sink, engine.replace("bool paused = true;", "bool paused = false;")),
            ("idle-resume-not-streaming", sink, engine.replace(
                "streaming = true;", "streaming = false;")),
            ("stop-leaks-transport", sink, engine.replace("bool paused = false;", "bool paused = true;")),
            ("pcm-retains-transport", sink.replace(
                "m_needIecPack &&\n        m_requestedFormat.m_dataFormat == AE_FMT_RAW &&",
                ""), engine),
            ("disabled-keepalive-ignored", sink.replace(
                "m_silenceTimeOut > std::chrono::minutes::zero() &&", ""), engine),
            ("live-keepalive-disable-ignored", sink.replace(
                "if (m_extPaused)\n          {", "if (false)\n          {"), engine),
            ("focus-leaks-transport", sink.replace("else if (m_extAppFocused)", "else if (true)"), engine),
        ))
    with tempfile.TemporaryDirectory(prefix="ae-long-pause-") as directory:
        root = Path(directory)
        for name, impl, ae_impl in variants:
            if name != "production":
                assert impl != sink or ae_impl != engine, name
            source, binary = root / (name + ".cpp"), root / name
            source.write_text(harness(impl, ae_impl))
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(source), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if name == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            else:
                assert result.returncode != 0 and "Assertion" in result.stderr, (name, result.stderr)
                print(name + ": rejected")


if __name__ == "__main__":
    main()
