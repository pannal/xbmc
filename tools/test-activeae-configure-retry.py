#!/usr/bin/env python3
"""Full production Configure retry after same-device format-change FLUSH timeout.

Configure, GetInputFormat, CompareFormat and FlushEngine are extracted whole,
without rewriting the retry predicate. Buffer allocation, settings application,
sink InitSink and ordered timeout/ACK delivery are recording boundaries. This is
a sequential cutpoint regression, not real actor threads or device playback.
The production AE_TOP_ERROR timeout case dispatches the recovery retry to PLAY.
Tests retain the original AUDIO-1 failure as a compiled negative control.
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
        "void CActiveAE::Configure(", "AEAudioFormat CActiveAE::GetInputFormat(",
        "bool CActiveAE::CompareFormat(", "bool CActiveAE::FlushEngine("))
    machine = paused.block(engine, "void CActiveAE::StateMachine(")
    error = machine[machine.index("    case AE_TOP_ERROR:"):
                    machine.index("    case AE_TOP_UNCONFIGURED:")]
    dispatch = """
void CActiveAE::RetryAfterError(){
  const int signal=CActiveAEControlProtocol::TIMEOUT;
  void* port=nullptr;
  switch(m_state){
    @ERROR@
    default:assert(false&&"unexpected state");
  }
}
""".replace("@ERROR@", error)
    return PREFIX + methods + dispatch + TESTS


PREFIX = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstring>
#include <deque>
#include <iostream>
#include <list>
#include <memory>
#include <string>
#include <vector>
using namespace std::chrono_literals;
constexpr int AE_FMT_FLOAT=1,AE_FMT_FLOATP=2,AE_FMT_RAW=3;
constexpr int AE_CH_LAYOUT_2_0=2,AE_CH_FC=1,AV_CODEC_ID_AC3=1,AV_CODEC_ID_EAC3=2;
constexpr int LOGERROR=1,LOGWARNING=2,AE_SOUND_ALWAYS=1,AE_SOUND_IDLE=2;
constexpr double MAX_BUFFER_TIME=.1,MAX_WATER_LEVEL=.2,MAX_CACHE_LEVEL=.4;
bool AE_IS_PLANAR(int format){return format==AE_FMT_FLOATP;}
struct CLog{template<class... T> static void Log(int,T...){}};
struct Channels {
  int count=2;
  Channels()=default;Channels(int n):count(n){}
  int Count()const{return count;}
  bool operator!=(const Channels& b)const{return count!=b.count;}
  Channels& operator+=(int){++count;return *this;}
  void ResolveChannels(const Channels&){}
};
struct CAEStreamInfo {
  enum {STREAM_TYPE_TRUEHD,STREAM_TYPE_AC3,STREAM_TYPE_EAC3};
  int m_type=STREAM_TYPE_TRUEHD,m_channels=8,m_sampleRate=48000,m_ac3FrameSize=0;
};
struct AEAudioFormat {
  int m_dataFormat=AE_FMT_RAW,m_sampleRate=48000,m_frames=960,m_frameSize=8;
  Channels m_channelLayout;
  CAEStreamInfo m_streamInfo;
};
struct CAEUtil {
  static int DataFormatToBits(int){return 32;}
  static uint64_t GetAVChannelLayout(Channels c){return c.Count();}
  static Channels GetAEChannelLayout(uint64_t c){return Channels(c);}
};
struct AESinkDevice {std::string name,driver;};
struct CAESinkFactory {
  static AESinkDevice ParseDevice(const std::string& device){
    auto separator=device.find(':');
    if(separator==std::string::npos)return {device,"same-driver"};
    return {device.substr(separator+1),device.substr(0,separator)};
  }
};
struct CSampleBuffer {int returns=0;void Return(){++returns;}};
struct CActiveAEBufferPool {
  AEAudioFormat m_format;
  explicit CActiveAEBufferPool(AEAudioFormat f={}):m_format(f){}
  virtual ~CActiveAEBufferPool()=default;
  template<class... T> void Create(T...){}
  virtual void Flush(){}
};
struct CActiveAEBufferPoolResample:CActiveAEBufferPool {
  AEAudioFormat m_inputFormat;
  CActiveAEBufferPoolResample(AEAudioFormat i,AEAudioFormat o,int):CActiveAEBufferPool(o),m_inputFormat(i){}
};
struct CActiveAEStreamBuffers {
  template<class... T> CActiveAEStreamBuffers(T...){}
  void ForceResampler(bool){}
  template<class... T> void Create(T...){}
  void FillBuffer(){}void Flush(){}
  std::unique_ptr<CActiveAEBufferPool> GetResampleBuffers(){return {};}
  std::unique_ptr<CActiveAEBufferPool> GetAtempoBuffers(){return {};}
};
struct CActiveAEStream {
  AEAudioFormat m_format;
  std::unique_ptr<CActiveAEBufferPool> m_inputBuffers;
  std::unique_ptr<CActiveAEStreamBuffers> m_processingBuffers;
  int m_streamSpace=0;bool m_forceResampler=false;
  void InitRemapper(){}
  struct {void SetSamplerate(int){}}m_limiter;
};
struct CAEEncoderFFmpeg {
  void Initialize(AEAudioFormat&,bool){}
  int GetCodecID(){return AV_CODEC_ID_AC3;}
};
struct CActiveAESound {void SetConverted(bool){}};
struct CServiceBroker {
  static CServiceBroker& GetDataCacheCore(){static CServiceBroker b;return b;}
  void ResetAudioCache(){}
};
struct CSinkControlProtocol {enum {FLUSH,ACC,ERR,VOLUME,STREAMING};};
struct CActiveAEControlProtocol {enum {TIMEOUT};};
constexpr int AE_TOP_ERROR=1,AE_TOP_CONFIGURED_PLAY=2;
struct CSinkDataProtocol {enum {RETURNSAMPLE,PAUSEDSAMPLE,ACC};};
struct Message {
  int signal=CSinkControlProtocol::ACC;
  unsigned char* data=nullptr;
  void Release(){}
};
struct Control {
  bool timeout=false;int flushRequests=0,flushAcks=0,pending=0;
  Message reply;
  bool SendOutMessageSync(int signal,Message** result,std::chrono::seconds){
    assert(signal==CSinkControlProtocol::FLUSH);++flushRequests;++pending;
    if(timeout)return false;
    flushAcks+=pending;pending=0;
    reply.signal=CSinkControlProtocol::ACC;*result=&reply;return true;
  }
  void SendOutMessage(int,void*,size_t){}
};
struct Stats {
  int resets=0;
  void Reset(int,bool){++resets;}
  template<class T> void AddSamples(int,T&){}
  double GetMaxDelay(){return 0;}
};
struct CActiveAE {
  enum {MODE_RAW,MODE_PCM,MODE_TRANSCODE};int m_mode=MODE_RAW;
  bool m_sinkFlushPending=false,m_extError=false,m_extDrain=false,m_vizInitialized=false;
  bool m_aeGUISoundForce=false;
  int initCalls=0,lowLatencyCalls=0,failedInits=0,negotiatedRate=0;
  int m_state=AE_TOP_ERROR;
  std::chrono::milliseconds m_extTimeout{0};
  AEAudioFormat m_sinkRequestFormat,m_sinkFormat,m_inputFormat,m_internalFormat,m_encoderFormat;
  AEAudioFormat configuredSink;
  std::chrono::milliseconds m_extKeepConfig{0};
  struct Settings {
    std::string device="same-device",passthroughdevice="same-device",driver="same-driver";
    int resampleQuality=0,guisoundmode=0;
    bool stereoupmix=false,normalizelevels=false;float mixSubLevel=0;
  }m_settings;
  std::string m_currDevice="same-device";
  float m_volume=1;
  std::list<CActiveAEStream*>m_streams;
  std::unique_ptr<CActiveAEBufferPool>m_silenceBuffers,m_encoderBuffers,m_vizBuffersInput;
  std::unique_ptr<CActiveAEBufferPoolResample>m_sinkBuffers,m_vizBuffers;
  std::vector<std::unique_ptr<CActiveAEBufferPool>>m_discardBufferPools;
  std::vector<CActiveAESound*>m_sounds;
  std::vector<int>m_sounds_playing,m_audioCallback;
  std::unique_ptr<CAEEncoderFFmpeg> encoderOwner;
  CAEEncoderFFmpeg* m_encoder=nullptr;
  Stats m_stats;
  struct {
    Control m_controlPort;
    struct {bool ReceiveInMessage(Message**){return false;}}m_dataPort;
  }m_sink;
  void Configure(AEAudioFormat* desiredFmt=nullptr);
  void RetryAfterError();
  void LoadSettings(){}
  AEAudioFormat GetInputFormat(AEAudioFormat*);
  bool CompareFormat(const AEAudioFormat&,const AEAudioFormat&);
  bool FlushEngine();
  void ApplySettingsToFormat(AEAudioFormat&,Settings&,int*){}
  bool InitSink(){
    ++initCalls;
    if(failedInits){--failedInits;m_extError=true;return false;}
    configuredSink=m_sinkRequestFormat;
    if(negotiatedRate)configuredSink.m_sampleRate=negotiatedRate;
    m_sinkFormat=configuredSink;return true;
  }
  void ConfigureLowLatency(){++lowLatencyCalls;}
  void ClearDiscardedBuffers(){m_discardBufferPools.clear();}
};
'''

TESTS = r'''
int main(){
  // Success control runs the whole RAW Configure body, including buffer setup.
  {
    CActiveAE engine;CActiveAEStream stream;engine.m_streams.push_back(&stream);
    stream.m_format.m_sampleRate=96000;
    engine.Configure();
    assert(engine.initCalls==1&&engine.configuredSink.m_sampleRate==96000);
    assert(engine.lowLatencyCalls==1&&stream.m_inputBuffers&&stream.m_processingBuffers);
    engine.Configure();assert(engine.initCalls==1&&engine.lowLatencyCalls==2);
    std::cout<<"PASS full Configure immediate format change and same-format retry\n";
  }
  CActiveAE engine;CActiveAEStream stream;engine.m_streams.push_back(&stream);
  stream.m_format.m_sampleRate=96000;
  engine.m_sink.m_controlPort.timeout=true;
  engine.Configure();
  assert(engine.initCalls==0&&engine.lowLatencyCalls==0);
  assert(engine.m_sinkFlushPending&&engine.m_extError);
  assert(engine.m_sinkRequestFormat.m_sampleRate==96000);
  assert(engine.configuredSink.m_sampleRate==48000);
  assert(engine.m_stats.resets==0);
  engine.Configure(); // Unacknowledged entry barrier must not enter body.
  assert(engine.initCalls==0&&engine.lowLatencyCalls==0&&engine.m_stats.resets==0);
  engine.m_sink.m_controlPort.timeout=false;
  engine.RetryAfterError(); // Actual full body after the pending FLUSH barrier finally ACKs.
  assert(!engine.m_sinkFlushPending&&engine.m_sink.m_controlPort.pending==0);
  assert(!engine.m_extError&&engine.m_state==AE_TOP_CONFIGURED_PLAY);
  assert(engine.lowLatencyCalls==1&&stream.m_inputBuffers&&stream.m_processingBuffers);
  std::cout<<"AUDIO-1 retry: requested="<<engine.m_sinkRequestFormat.m_sampleRate
           <<" actualConfigured="<<engine.configuredSink.m_sampleRate
           <<" InitSinkCalls="<<engine.initCalls<<" flushAcks="
           <<engine.m_sink.m_controlPort.flushAcks<<" state=PLAY"<<std::endl;
  assert(engine.initCalls==1&&engine.configuredSink.m_sampleRate==96000);
  // The successful retry acknowledges both its entry barrier and the
  // reinitialization barrier, in addition to the two earlier timed-out sends.
  assert(engine.m_sink.m_controlPort.flushAcks==4);

  // Repeated failed barriers must never allow recovery to enter PLAY early.
  {
    CActiveAE retry;CActiveAEStream stream;retry.m_streams.push_back(&stream);
    stream.m_format.m_sampleRate=96000;retry.m_sink.m_controlPort.timeout=true;
    retry.Configure();
    for(int i=0;i<3;++i){
      retry.RetryAfterError();
      assert(retry.m_state==AE_TOP_ERROR&&retry.m_extError);
      assert(retry.initCalls==0&&retry.lowLatencyCalls==0);
    }
    retry.m_sink.m_controlPort.timeout=false;retry.RetryAfterError();
    assert(retry.m_state==AE_TOP_CONFIGURED_PLAY&&!retry.m_extError);
    assert(retry.initCalls==1&&retry.configuredSink.m_sampleRate==96000);
    retry.Configure();assert(retry.initCalls==1);
  }
  // Sink initialization itself can fail after an acknowledged FLUSH. Returning
  // to the old format cannot trust the old cached actual format in that case.
  for(bool returnOld:{false,true}){
    CActiveAE retry;CActiveAEStream stream;retry.m_streams.push_back(&stream);
    stream.m_format.m_sampleRate=96000;retry.failedInits=2;
    retry.Configure();
    assert(retry.initCalls==1&&retry.lowLatencyCalls==0&&retry.m_extError);
    if(returnOld)stream.m_format.m_sampleRate=48000;
    retry.RetryAfterError();
    assert(retry.initCalls==2&&retry.m_state==AE_TOP_ERROR&&retry.m_extError);
    assert(retry.lowLatencyCalls==0);
    retry.RetryAfterError();
    assert(retry.initCalls==3&&retry.m_state==AE_TOP_CONFIGURED_PLAY&&!retry.m_extError);
    assert(retry.configuredSink.m_sampleRate==(returnOld?48000:96000));
    retry.Configure();assert(retry.initCalls==3);
  }
  // A format request can be canceled or replaced while its FLUSH is pending.
  for(int rate:{48000,192000}){
    CActiveAE retry;CActiveAEStream stream;retry.m_streams.push_back(&stream);
    stream.m_format.m_sampleRate=96000;retry.m_sink.m_controlPort.timeout=true;
    retry.Configure();stream.m_format.m_sampleRate=rate;
    retry.m_sink.m_controlPort.timeout=false;retry.RetryAfterError();
    assert(retry.m_state==AE_TOP_CONFIGURED_PLAY&&retry.initCalls==1);
    assert(retry.configuredSink.m_sampleRate==rate);
    retry.Configure();assert(retry.initCalls==1);
  }
  // Retry the latest device/driver, not the failed destination. Device and
  // driver changes alone still require initialization with unchanged format.
  for(const std::string destination:{"new-device","new-driver:same-device"}){
    for(bool cancel:{false,true}){
      CActiveAE retry;CActiveAEStream stream;retry.m_streams.push_back(&stream);
      retry.m_settings.passthroughdevice=destination;
      retry.m_sink.m_controlPort.timeout=true;retry.Configure();
      assert(retry.initCalls==0);
      if(cancel)retry.m_settings.passthroughdevice="same-device";
      retry.m_sink.m_controlPort.timeout=false;retry.RetryAfterError();
      const auto expected=CAESinkFactory::ParseDevice(retry.m_settings.passthroughdevice);
      assert(retry.initCalls==1&&retry.m_state==AE_TOP_CONFIGURED_PLAY);
      assert(retry.m_currDevice==expected.name&&retry.m_settings.driver==expected.driver);
      retry.Configure();assert(retry.initCalls==1);
    }
  }
  // An accepted negotiated format may differ from the request: the old-request
  // shortcut remains intentional after success, without reopening every time.
  {
    CActiveAE retry;CActiveAEStream stream;retry.m_streams.push_back(&stream);
    stream.m_format.m_sampleRate=96000;retry.negotiatedRate=48000;
    retry.Configure();
    assert(retry.initCalls==1&&retry.configuredSink.m_sampleRate==48000);
    retry.Configure();assert(retry.initCalls==1);
  }
  // A freed/replaced stream must not leave the failed format as retry intent.
  {
    CActiveAE retry;CActiveAEStream oldStream,newStream;
    retry.m_streams.push_back(&oldStream);oldStream.m_format.m_sampleRate=96000;
    retry.m_sink.m_controlPort.timeout=true;retry.Configure();
    retry.m_streams.clear();newStream.m_format.m_sampleRate=192000;
    retry.m_streams.push_back(&newStream);retry.m_sink.m_controlPort.timeout=false;
    retry.RetryAfterError();
    assert(retry.initCalls==1&&retry.configuredSink.m_sampleRate==192000);
    assert(retry.m_state==AE_TOP_CONFIGURED_PLAY);
  }
  std::cout<<"PASS full Configure retry, repeated failure, canceled/replaced format, "
              "device/driver, negotiated format and replaced-stream controls\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    source = harness((AE / "ActiveAE.cpp").read_text())
    variants = [("production", source)]
    if args.negative_controls:
        old = '    m_currDevice = "";\n'
        assert source.count(old) == 1
        variants.append(("original-configure-retry", source.replace(old, "")))
    with tempfile.TemporaryDirectory(prefix="ae-configure-retry-") as temp:
        for label, text in variants:
            cpp, exe = Path(temp) / (label + ".cpp"), Path(temp) / label
            cpp.write_text(text)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            if label == "production":
                print(result.stdout, end="", flush=True)
                print(result.stderr, end="", flush=True)
                result.check_returncode()
            else:
                assert result.returncode and "Assertion" in result.stderr, result.stderr
                assert "requested=96000 actualConfigured=48000 InitSinkCalls=0" in result.stdout
                print("REJECTED:", label)


if __name__ == "__main__":
    main()
