#!/usr/bin/env python3
"""Exercise bounded TrueHD restart tags through actual queue/sync/sink methods.

The source methods are extracted from the selected parent and working trees.
Payload/control equality is independent of diagnostic assertions. Timers, packet
pools and accepted-write endpoints are host adapters, not HDMI/device evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1]
HEADER = "xbmc/cores/AudioEngine/Utils/TrueHDRestartTrace.h"
AE = "xbmc/cores/AudioEngine/Engines/ActiveAE/"
BASELINE_REF = "ba4ab0c2679c886f2dbc7977f591b4297bc70b63"


def source(path, ref=None):
    return subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT, text=True) if ref else (ROOT/path).read_text()


def block(text, marker):
    start = text.index(marker)
    at = text.index("{", start)
    depth, end = 1, at + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def load_delivery():
    # Keep the established host boundaries stable while production is compared.
    module = types.ModuleType("restart_delivery_fixture")
    module.__file__ = str(ROOT/"tools/test-truehd-delivery.py")
    text = source("tools/test-truehd-delivery.py", BASELINE_REF)
    exec(compile(text, module.__file__, "exec"), module.__dict__)
    return module


def execute(directory, name, text, extra_sources=(), sanitize=False):
    cpp, binary = directory/(name+".cpp"), directory/name
    cpp.write_text(text)
    command = [os.environ.get("CXX", "g++"), "-std=c++17", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-DTARGET_POSIX", "-I"+str(directory), str(cpp), *map(str, extra_sources), "-o", str(binary)]
    if sanitize:
        command[1:1] = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-pie", "-no-pie"]
    compiled = subprocess.run(command, capture_output=True, text=True)
    (directory/(name+"-compile.stdout")).write_text(compiled.stdout)
    (directory/(name+"-compile.stderr")).write_text(compiled.stderr)
    if compiled.returncode:
        raise RuntimeError(compiled.stderr)
    ran = subprocess.run([str(binary), str(directory/(name+".bin"))], capture_output=True, text=True, env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"})
    (directory/(name+".stdout")).write_text(ran.stdout)
    (directory/(name+".stderr")).write_text(ran.stderr)
    if ran.returncode:
        raise RuntimeError(ran.stderr)
    return (directory/(name+".bin")).read_bytes(), ran.stdout


WINDOW = r'''
#include <cassert>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <vector>
#include "TrueHDRestartTrace.h"
int main(int argc, char** argv) {
  assert(argc == 2); std::ofstream out(argv[1], std::ios::binary);
  std::vector<uint8_t> mat(61440); mat[32]=0xf8;mat[33]=0x72;mat[34]=0x6f;mat[35]=0xba;
  AE::CTrueHDRestartTrace window; window.Reset(false);
  assert(!window.Next(mat.data(), mat.size()));
  window.Reset(true); auto epoch = window.Epoch(); assert(epoch);
  AE::TrueHDRestartTrace old;
  for (unsigned n=1; n<=32; ++n) {
    auto tag=window.Next(mat.data(), mat.size());
    assert(tag && tag.epoch==epoch && tag.packet==n && tag.packetBytes==61440 && tag.bytes==61440 && tag.majorAtStart);
    if(n==1)old=tag;
  }
  for(int n=0;n<100;++n)assert(!window.Next(mat.data(), mat.size()));
  window.Reset(true); assert(window.Epoch()!=epoch && old.epoch==epoch);
  auto fresh=window.Next(mat.data(),mat.size()); assert(fresh.packet==1);
  old.insertedPauses=7;old.insertedPauseMs=140;
  auto cut=old.Slice(37,511);assert(cut && cut.epoch==epoch && cut.packet==1 && cut.offset==37 && cut.bytes==511 && cut.insertedPauses==0 && cut.insertedPauseMs==0);
  assert(!old.Slice(61441,0) && !old.Slice(61440,1) && !old.Slice(0,61441));
  for(unsigned size=0;size<36;++size) {
    window.Reset(true); auto shortTag=window.Next(mat.data(),size);
    assert(size==0 ? !shortTag : (shortTag && !shortTag.majorAtStart));
  }
  window.Reset(true);assert(!window.Next(nullptr,61440));
  window.Reset(true);mat[35]^=1;assert(!window.Next(mat.data(),mat.size()).majorAtStart);
  window.Reset(false);assert(window.Epoch()==0 && !window.Next(mat.data(),mat.size()));
  out.put('1');std::cout<<"window: debug gating, exact32 bound, epochs, slices and short-input guards PASS\n";
}
'''

TRANSFER = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include "TrueHDRestartTrace.h"
#include "utils/log.h"
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
constexpr int AE_FMT_RAW=0,AE_FMT_FLOAT=1;
constexpr double DVD_TIME_BASE=1000000;
struct Logging{bool enabled=true;bool IsLogLevelLogged(int)const{return enabled;}};
struct CServiceBroker{inline static Logging log;static Logging&GetLogging(){return log;}};
struct CAESyncInfo{enum AESyncState{SYNC_OFF,SYNC_INSYNC,SYNC_START,SYNC_MUTE,SYNC_ADJUST};AESyncState state=SYNC_START;unsigned errortime=0;double error=0,rr=1;};
struct CAEStreamInfo{enum Type{STREAM_TYPE_TRUEHD,STREAM_TYPE_AC3};Type m_type=STREAM_TYPE_TRUEHD;int GetDuration()const{return 20;}};
struct Format{int m_dataFormat=AE_FMT_RAW,m_frameSize=1;CAEStreamInfo m_streamInfo;};
struct Packet{struct{int channels=1,sample_rate=48000;}config;int nb_samples=0,max_nb_samples=2048,bytes_per_sample=1,planes=1,pause_burst_ms=0;std::vector<uint8_t>storage=std::vector<uint8_t>(61440);uint8_t*data[1]{storage.data()};};
struct CSampleBuffer{std::unique_ptr<Packet>pkt=std::make_unique<Packet>();AE::TrueHDRestartTrace trueHDRestartTrace;double centerMixLevel=1,surroundMixLevel=1;int refCount=0,pkt_start_offset=0;double timestamp=0;};
struct CActiveAEBufferPool{std::deque<CSampleBuffer*>m_freeSamples;CSampleBuffer*GetFreeBuffer();void ReturnBuffer(CSampleBuffer*);};
@GET_FREE@
@RETURN_BUFFER@
struct CActiveAEStream;
struct MsgStreamSample{CSampleBuffer*buffer;CActiveAEStream*stream;};
struct CActiveAEDataProtocol{enum{STREAMBUFFER,STREAMSAMPLE};};
struct Message{int signal=CActiveAEDataProtocol::STREAMBUFFER;CSampleBuffer*buffer=nullptr;void*data=&buffer;void Release(){}};
struct Port{std::deque<Message*>incoming;std::vector<CSampleBuffer*>sent;
 bool ReceiveInMessage(Message**m){if(incoming.empty())return false;*m=incoming.front();incoming.pop_front();return true;}
 void SendOutMessage(int signal,void*data,unsigned){assert(signal==CActiveAEDataProtocol::STREAMSAMPLE);sent.push_back(static_cast<MsgStreamSample*>(data)->buffer);}};
struct Event{bool Wait(std::chrono::milliseconds){return false;}};
struct IAEStream{@EXT_DATA@;};
struct CActiveAEStream{using ExtData=IAEStream::ExtData;Format m_format;CSampleBuffer*m_currentBuffer=nullptr;bool m_streamIsFlushed=true;double m_lastPts=0,m_lastPtsJump=0;std::chrono::milliseconds m_errorInterval=1000ms;CCriticalSection m_statsLock;float m_bufferedTime=0;Port port;Port*m_streamPort=&port;Event m_inMsgEvent;unsigned freeBuffers=0;
 void RemapBuffer(){}void DecFreeBuffers(){--freeBuffers;}
 double GetDelay(){return .125;}CAESyncInfo GetSyncInfo(){return {};}
 unsigned int AddData(const uint8_t*const*,unsigned,unsigned,ExtData*);
};
@ADD_DATA@
struct DVDAudioFrame{uint8_t*data[1]{};unsigned nb_frames=61440,framesOut=0;double pts=12000000,duration=20000,centerMixLevel=1,surroundMixLevel=1;bool hasDownmix=false,passthrough=true;Format format;AE::TrueHDRestartTrace trueHDRestartTrace;};
struct Clock{double GetAbsoluteClock(){return 123456;}};
void usleep(unsigned){}
struct CAudioSinkAE{bool m_bAbort=false;CCriticalSection m_critSection;CActiveAEStream*m_pAudioStream=nullptr;unsigned m_syncErrorTime=0,m_addPacketUnlockTime=0;double m_syncError=0,m_resampleRatio=1,m_playingPts=0,m_playingFramePts=0,m_timeOfPts=0;Clock clock;Clock*m_pClock=&clock;
 double GetDelay(){return m_pAudioStream->GetDelay()*DVD_TIME_BASE;}
 unsigned AddPackets(const DVDAudioFrame&);
};
@ADD_PACKETS@
std::ofstream result;
template<class T>void record(const T&v){result.write(reinterpret_cast<const char*>(&v),sizeof(v));}
int main(int argc,char**argv){assert(argc==2);result.open(argv[1],std::ios::binary);
 constexpr bool traced=@TRACED@;
 unsigned cases=0;
 for(bool debug:{false,true})for(bool truehd:{false,true})for(unsigned initialOffset:{0U,37U}) {
  CServiceBroker::log.enabled=debug;CLog::records.clear();
  std::vector<uint8_t>bytes(61440);for(size_t i=0;i<bytes.size();++i)bytes[i]=(i*37+(i>>5))&255;bytes[32]=0xf8;bytes[33]=0x72;bytes[34]=0x6f;bytes[35]=0xba;
  const auto before=bytes;
  AE::CTrueHDRestartTrace producer;producer.Reset(debug);auto tag=producer.Next(bytes.data(),bytes.size());
  producer.Reset(debug); // old in-flight tag must retain its epoch
  CActiveAEStream stream;stream.m_format.m_streamInfo.m_type=truehd?CAEStreamInfo::STREAM_TYPE_TRUEHD:CAEStreamInfo::STREAM_TYPE_AC3;
  std::vector<std::unique_ptr<CSampleBuffer>>buffers;std::vector<std::unique_ptr<Message>>messages;
  CActiveAEBufferPool pool;
  for(int n=0;n<32;++n){buffers.push_back(std::make_unique<CSampleBuffer>());auto*b=buffers.back().get();b->trueHDRestartTrace=tag;b->pkt->nb_samples=99;b->pkt->pause_burst_ms=20;pool.m_freeSamples.push_back(b);auto*free=pool.GetFreeBuffer();assert(free==b);if constexpr(traced)assert(!free->trueHDRestartTrace);messages.push_back(std::make_unique<Message>());messages.back()->buffer=free;stream.port.incoming.push_back(messages.back().get());++stream.freeBuffers;}
  DVDAudioFrame frame;frame.data[0]=bytes.data();frame.framesOut=initialOffset;frame.format=stream.m_format;frame.trueHDRestartTrace=tag;
  CAudioSinkAE sink;sink.m_pAudioStream=&stream;
  const auto copied=sink.AddPackets(frame);assert(copied==bytes.size()-initialOffset&&bytes==before);record(copied);record(sink.m_playingPts);record(sink.m_playingFramePts);record(sink.m_timeOfPts);
  unsigned at=initialOffset;
  for(auto*b:stream.port.sent){const auto count=unsigned(b->pkt->nb_samples);assert(std::equal(b->pkt->data[0],b->pkt->data[0]+count,bytes.data()+at));record(count);result.write(reinterpret_cast<const char*>(b->pkt->data[0]),count);record(b->timestamp);record(b->pkt_start_offset);
   if constexpr(traced){if(debug&&truehd){assert(b->trueHDRestartTrace&&b->trueHDRestartTrace.epoch==tag.epoch&&b->trueHDRestartTrace.packet==tag.packet&&b->trueHDRestartTrace.offset==at&&b->trueHDRestartTrace.bytes==count);}else assert(!b->trueHDRestartTrace);}
   at+=count;b->trueHDRestartTrace=tag;pool.ReturnBuffer(b);if constexpr(traced)assert(!b->trueHDRestartTrace);assert(b->pkt->nb_samples==0&&b->pkt->pause_burst_ms==0);
  }
  assert(at==bytes.size());++cases;
 }
 std::cout<<cases<<" actual AudioSink/AE AddData/pool transfer cases PASS\n";
}
'''


def transfer(directory, ref=None, mutation=None, sanitize=False):
    directory.mkdir(parents=True, exist_ok=True)
    header = source(HEADER)
    (directory/"TrueHDRestartTrace.h").write_text(header)
    code = source(AE+"ActiveAEStream.cpp", ref)
    audio = source("xbmc/cores/VideoPlayer/AudioSinkAE.cpp", ref)
    pool = source(AE+"ActiveAEBuffer.cpp", ref)
    ext = block(source("xbmc/cores/AudioEngine/Interfaces/AEStream.h"), "  struct ExtData")
    traced = "trueHDRestartTrace" in code
    add = block(code, "unsigned int CActiveAEStream::AddData(")
    add_packets = block(audio, "unsigned int CAudioSinkAE::AddPackets(")
    get_free = block(pool, "CSampleBuffer* CActiveAEBufferPool::GetFreeBuffer(")
    returned = block(pool, "void CActiveAEBufferPool::ReturnBuffer(")
    if mutation == "missing-handoff":
        old = "ext.trueHDRestartTrace = audioframe.trueHDRestartTrace;"
        assert old in add_packets
        add_packets = add_packets.replace(old, "ext.trueHDRestartTrace = {};", 1)
    elif mutation == "stale-pool":
        old = "buffer->trueHDRestartTrace = {};"
        assert old in returned
        returned = returned.replace(old, "", 1)
    text = TRANSFER.replace("@GET_FREE@", get_free).replace("@RETURN_BUFFER@", returned).replace("@EXT_DATA@", ext).replace("@ADD_DATA@", add).replace("@ADD_PACKETS@", add_packets).replace("@TRACED@", "true" if traced else "false")
    (directory/"utils").mkdir(exist_ok=True)
    (directory/"utils/log.h").write_text(r'''
#pragma once
#include <sstream>
#include <string>
#include <vector>
constexpr int LOGDEBUG=0,LOGERROR=1,LOGINFO=2;
struct LogRecord{std::string message;std::vector<std::string>args;};
struct CLog{inline static std::vector<LogRecord>records;template<class T>static std::string text(T v){std::ostringstream s;s<<v;return s.str();}template<class...T>static void Log(int,const char*m,T...a){records.push_back({m,{text(a)...}});}};
''')
    return execute(directory, "transfer", text, sanitize=sanitize)
PIPELINE_EXTRA = r'''
#include <list>
static long long fakeNowMs=0;
namespace XbmcThreads{template<class T=void>struct EndTime{long long start=0,due=0;void Set(std::chrono::milliseconds n){start=fakeNowMs;due=start+n.count();}bool IsTimePast()const{return fakeNowMs>=due;}auto GetStartTime()const{return std::chrono::steady_clock::time_point(std::chrono::milliseconds(start));}};}
@SYNC_ERROR@;
struct CAESyncInfo{enum AESyncState{SYNC_OFF,SYNC_INSYNC,SYNC_START,SYNC_MUTE,SYNC_ADJUST};};
struct Clock{double GetClockSpeed(){return 1;}};
struct Buffers{Format m_inputFormat;std::deque<CSampleBuffer*>m_outputSamples,m_inputSamples;void SetRR(double,float){}};
'''
# The stream declaration below replaces only host boundaries, never decisions.
PIPELINE_STREAM = r'''
struct CActiveAEStream{Clock clock;Clock*m_pClock=&clock;Buffers buffers;Buffers*m_processingBuffers=&buffers;Format m_format;CAESyncInfo::AESyncState m_syncState=CAESyncInfo::SYNC_START;CSyncError m_syncError;bool m_resampleMode=false,m_paused=false,m_started=false;double m_resampleIntegral=0,m_lastSyncError=0;auto GetErrorInterval(){return 1000ms;}};
struct PausePool{Packet packet;CSampleBuffer buffer{&packet,nullptr};bool available=true;CSampleBuffer*GetFreeBuffer(){if(!available)return nullptr;packet=Packet{};buffer.trueHDRestartTrace={};return &buffer;}};
constexpr int MODE_RAW=0,MODE_PCM=1,MODE_TRANSCODE=2;
constexpr double TRUEHD_SYNC_ERROR_SCALE=0.45;
struct EngineStats{void UpdateStream(CActiveAEStream*){}void AddSamples(int,const std::list<CActiveAEStream*>&){}};
struct CActiveAE{int m_mode=MODE_RAW;Format m_sinkFormat,m_encoderFormat;struct{float atempoThreshold=0;}m_settings;PausePool pauses;PausePool*m_silenceBuffers=&pauses;EngineStats m_stats;std::list<CActiveAEStream*>m_streams;Buffers sinkBuffers;Buffers*m_sinkBuffers=&sinkBuffers;CSampleBuffer*SyncStream(CActiveAEStream*);void Step();};
@SYNC_PREFIX@
void CActiveAE::Step(){@RAW_STAGE@}
static void checkLog(const char*stage,const char*decision,const AE::TrueHDRestartTrace&tag){
 auto logs=logsSince(0,stage);assert(logs.size()==1);const auto&r=logs.front();assert(number(r,0)==tag.epoch&&number(r,1)==tag.packet);
 if(std::string(stage)=="TrueHD restart: stage=ae "){assert(r.args.at(4)==decision);}
}
int main(int argc,char**argv){assert(argc==2);result.open(argv[1],std::ios::binary);constexpr bool traced=@TRACED@;unsigned cases=0;
 for(bool debug:{false,true})for(int scenario=0;scenario<4;++scenario)for(int writePlan=0;writePlan<4;++writePlan){
  CServiceBroker::log.enabled=debug;CLog::records.clear();fakeNowMs=0;
  std::vector<uint8_t>bytes(65536);for(size_t i=0;i<bytes.size();++i)bytes[i]=(i*73+(i>>8)*19+(i>>4))&255;bytes[32]=0xf8;bytes[33]=0x72;bytes[34]=0x6f;bytes[35]=0xba;
  const auto original=bytes;AE::CTrueHDRestartTrace producer;producer.Reset(debug);auto tag=producer.Next(bytes.data(),61440);producer.Reset(debug);
  Packet packet;packet.data[0]=bytes.data();CSampleBuffer sourceBuffer{&packet,nullptr};sourceBuffer.trueHDRestartTrace=tag;
  CActiveAE engine;CActiveAEStream stream;engine.m_streams.push_back(&stream);stream.buffers.m_outputSamples.push_back(&sourceBuffer);
  if(scenario==1){stream.m_syncState=CAESyncInfo::SYNC_INSYNC;}
  if(scenario==2||scenario==3){stream.m_syncState=CAESyncInfo::SYNC_ADJUST;stream.m_syncError.Flush(100ms);stream.m_syncError.Correction(scenario==2?-27.706881:137.253985);stream.m_lastSyncError=scenario==2?-27.706881:137.253985;}
  std::vector<uint8_t>accepted;unsigned steps=0,insertions=0;std::string expected=scenario==0?"mute":scenario==2?"drop":"payload";
  while(!stream.buffers.m_outputSamples.empty()){
   assert(++steps<50);engine.Step();assert(engine.sinkBuffers.m_inputSamples.size()==1);auto*out=engine.sinkBuffers.m_inputSamples.front();engine.sinkBuffers.m_inputSamples.pop_front();
   if(out!=&sourceBuffer){++insertions;assert(scenario==3);if constexpr(traced)assert(!out->trueHDRestartTrace);assert(stream.buffers.m_outputSamples.front()==&sourceBuffer);}
   PCM pcm;if(writePlan==1)pcm.writes={17,281};else if(writePlan==2)pcm.writes={0,0,0,0,0};
   CAEBitstreamPacker packer;FakeSink sink;Stats stats;CActiveAESink active;active.m_packer=&packer;active.m_sink=&sink;active.m_stats=&stats;active.m_sinkFormat.m_frames=1440;active.m_swapState=CActiveAESink::SKIP_SWAP;sink.alsa.m_pcm=&pcm;sink.alsa.m_format=active.m_sinkFormat;
   if(writePlan==3){pcm.writes={17,0,0,0,0,0,0};} // accepted prefix then bounded sink retry failure
   auto before=CLog::records.size();auto returned=active.OutputSamples(out);record(returned);record(active.m_extError);recordBytes(pcm.accepted.data(),pcm.accepted.size());accepted.insert(accepted.end(),pcm.accepted.begin(),pcm.accepted.end());
   if constexpr(traced){auto logs=logsSince(before,"TrueHD restart: stage=sink_api ");const bool expect=debug&&out==&sourceBuffer;assert(logs.size()==size_t(expect));if(expect){auto&r=logs.front();assert(number(r,0)==tag.epoch&&number(r,1)==tag.packet);assert(r.args.at(4)==(expected=="payload"?"payload":expected=="mute"?"replacement_pause":"discard"));auto payload=number(r,8),taken=number(r,7);assert(payload==(expected=="payload"?taken:0));assert(pcm.accepted.size()==taken*16);assert(number(r,9)==(!active.m_extError));}}
  }
  assert(bytes==original);record(insertions);record(int(stream.m_syncState));
  if constexpr(traced){if(debug){checkLog("TrueHD restart: stage=ae ",expected.c_str(),tag);auto inserted=logsSince(0,"TrueHD restart: stage=ae_insert_pause ");assert(inserted.size()==size_t(scenario==3));if(scenario==3){assert(sourceBuffer.trueHDRestartTrace.insertedPauses==insertions);assert(sourceBuffer.trueHDRestartTrace.insertedPauseMs==20*insertions);}}else assert(logsSince(0,"TrueHD restart:").empty());}
  ++cases;
 }
 std::cout<<cases<<" actual RAW decision/sync/sink cases PASS\n";
}
'''


def pipeline(directory, ref=None, mutation=None, sanitize=False):
    directory.mkdir(parents=True, exist_ok=True)
    helper = load_delivery()
    # Reuse its established byte/ALSA/recovery adapters, not its assertions/main.
    original = helper.HARNESS.split("struct FakeMat", 1)[0]
    original = original.replace('#include "utils/log.h"', '#include "utils/log.h"\n#include "TrueHDRestartTrace.h"\n#include <cmath>')
    original = original.replace('if(r.message.rfind("TrueHD delivery",0)!=0)', 'if(r.message.rfind("TrueHD delivery",0)!=0 && r.message.rfind("TrueHD restart",0)!=0)')
    old = 'struct Packet {uint8_t*data[1]{};unsigned nb_samples=61440;int pause_burst_ms=0;};'
    new = 'struct Packet {uint8_t*data[1]{};int nb_samples=61440;int pause_burst_ms=0;int max_nb_samples=61440,planes=1,linesize=61440,bytes_per_sample=1;struct{int sample_rate=48000,channels=1;}config;};'
    assert old in original
    original = original.replace(old, new)
    original = original.replace('struct CSampleBuffer {Packet*pkt=nullptr;void*pool=nullptr;};', 'struct CSampleBuffer {Packet*pkt=nullptr;void*pool=nullptr;AE::TrueHDRestartTrace trueHDRestartTrace;CSampleBuffer(Packet*p,void*q):pkt(p),pool(q){}void Return(){}};')
    sink = source(AE+"ActiveAESink.cpp", ref)
    engine = source(AE+"ActiveAE.cpp", ref)
    output = block(sink, "unsigned int CActiveAESink::OutputSamples(")
    sync = block(engine, "CSampleBuffer* CActiveAE::SyncStream(")
    marker = '  if (!newerror || stream->m_syncState != CAESyncInfo::AESyncState::SYNC_INSYNC)'
    sync = sync[:sync.index(marker)]+'  return ret;\n}\n'
    raw = block(engine, '    // pass through\n    else')
    raw = raw[raw.index('{')+1:raw.rindex('}')]
    if mutation == "inserted-tag":
        old='buffer = SyncStream(*it);'
        assert old in raw
        raw=raw.replace(old,old+'\n          if (buffer) buffer->trueHDRestartTrace = restartInput->trueHDRestartTrace;',1)
    elif mutation == "mislabel-drop":
        old='restartInput->pkt->pause_burst_ms > 0 ? "mute" : "drop"'
        assert old in raw
        raw=raw.replace(old,'restartInput->pkt->pause_burst_ms > 0 ? "mute" : "payload"',1)
    traced='TrueHD restart: stage=ae ' in raw
    swap = block(sink, "void CActiveAESink::SwapInit(").replace("{", "{ (void)samples;", 1)
    original = original.replace('@OUTPUT@', output).replace('@SWAP@', swap)
    alsa=source("xbmc/cores/AudioEngine/Sinks/AESinkALSA.cpp", ref)
    original=original.replace('@ALSA@', block(alsa,"unsigned int CAESinkALSA::AddPackets(")).replace('@HANDLE_ERROR@',block(alsa,"void CAESinkALSA::HandleError("))
    original=original.replace('@TRACE_AVAILABLE@','false')
    pre=PIPELINE_EXTRA
    pre=pre.replace('@SYNC_ERROR@',block(source(AE+"ActiveAEStream.h",ref),'class CSyncError'))
    extra=PIPELINE_STREAM.replace('@SYNC_PREFIX@',sync).replace('@RAW_STAGE@',raw).replace('@TRACED@','true' if traced else 'false')
    # LOGAUDIO is a component argument in the actual code, ignored by this logger.
    text=original+'\nconstexpr int LOGAUDIO=3;\n'+pre+extra
    (directory/'TrueHDRestartTrace.h').write_text(source(HEADER))
    (directory/'utils').mkdir(exist_ok=True)
    (directory/'utils/log.h').write_text(r'''
#pragma once
#include <cstring>
#include <vector>
#include <string>
#include <sstream>
constexpr int LOGDEBUG=0,LOGERROR=1,LOGINFO=2;
struct LogRecord{std::string message;std::vector<std::string>args;};
void observeLog(const LogRecord&);
struct CLog{inline static std::vector<LogRecord>records;template<class T>static std::string text(T v){std::ostringstream s;s<<v;return s.str();}template<class...T>static void Log(int,const char*m,T...a){LogRecord r{m,{text(a)...}};records.push_back(r);observeLog(r);}template<class...T>static void Log(int,int,const char*m,T...a){Log(LOGDEBUG,m,a...);}};
''')
    (directory/'utils/StringUtils.h').write_text('#include <string>\nstruct StringUtils{static void ToLower(std::string&) {}};\n')
    (directory/'AEChannelInfo.h').write_text('#pragma once\nconstexpr int AE_CH_RAW=1;struct CAEChannelInfo{unsigned n=0;void operator+=(int){n++;}unsigned Count()const{return n;}};\n')
    (directory/'AEStreamInfo.h').write_text(r'''
#pragma once
struct CAEStreamInfo{enum Type{STREAM_TYPE_TRUEHD,STREAM_TYPE_AC3,STREAM_TYPE_EAC3,STREAM_TYPE_DTSHD,STREAM_TYPE_DTSHD_MA,STREAM_TYPE_DTSHD_CORE,STREAM_TYPE_DTS_512,STREAM_TYPE_DTS_1024,STREAM_TYPE_DTS_2048};Type m_type=STREAM_TYPE_TRUEHD;unsigned m_sampleRate=48000,m_repeat=1,m_dtsPeriod=512;bool m_dataIsLE=false;int GetDuration()const{return 20;}};
''')
    paths=[]
    for name in ['AEBitstreamPacker.cpp','AEBitstreamPacker.h','AEPackIEC61937.cpp','AEPackIEC61937.h']:
        (directory/name).write_text(source('xbmc/cores/AudioEngine/Utils/'+name,ref))
        if name.endswith('.cpp'):paths.append(directory/name)
    for name in ['Crc32.cpp','Crc32.h','EndianSwap.cpp','EndianSwap.h']:
        (directory/name).write_text(source('xbmc/utils/'+name,ref))
        if name.endswith('.cpp'):paths.append(directory/name)
    return execute(directory,'pipeline',text,paths,sanitize)
PRODUCER_MAIN = r'''
int main(int argc,char**argv){assert(argc==2);std::ofstream result(argv[1],std::ios::binary);constexpr bool traced=@TRACED@;unsigned cases=0;
 for(bool debug:{false,true})for(bool lav:{false,true})for(bool raw:{false,true})for(bool truehd:{false,true}){
  CServiceBroker::log.enabled=debug;CLog::records.clear();CDVDAudioCodecPassthrough codec;codec.m_lavStyleSyncEnabled=lav;codec.m_deviceIsRAW=raw;codec.m_format.m_streamInfo.m_type=truehd?CAEStreamInfo::STREAM_TYPE_TRUEHD:CAEStreamInfo::STREAM_TYPE_AC3;codec.Reset();
  auto epoch=codec.m_restartTrace.Epoch();const bool expected=debug&&lav&&!raw&&truehd;
  if constexpr(traced)assert(bool(epoch)==expected);
  codec.pending.resize(61440,0x11);codec.pending[32]=0xf8;codec.pending[33]=0x72;codec.pending[34]=0x6f;codec.pending[35]=0xba;
  for(unsigned n=1;n<=40;++n){codec.m_currentPts=12000000+(n-1)*20000;DVDAudioFrame frame;codec.GetData(frame);write(result,frame.nb_frames);write(result,frame.pts);write(result,frame.duration);write(result,frame.hasDiscontinuity);write(result,frame.discontinuityCorrection);result.write(reinterpret_cast<const char*>(frame.data[0]),frame.nb_frames);
   if constexpr(traced){assert(bool(frame.trueHDRestartTrace)==(expected&&n<=32));if(frame.trueHDRestartTrace){assert(frame.trueHDRestartTrace.epoch==epoch&&frame.trueHDRestartTrace.packet==n&&frame.trueHDRestartTrace.packetBytes==61440&&frame.trueHDRestartTrace.majorAtStart);}}
  }
  if constexpr(traced){assert(countLogs("TrueHD restart: stage=produce ")==size_t(expected?32:0));assert(countLogs("TrueHD restart: stage=reset ")==size_t(expected));}
  codec.pending.clear();DVDAudioFrame empty;empty.trueHDRestartTrace.epoch=999;codec.GetData(empty);if constexpr(traced)assert(!empty.trueHDRestartTrace);
  ++cases;
 }
 std::cout<<cases<<" actual codec Reset/GetData cases PASS\n";
}
'''


def producer(directory, ref=None, mutation=None, sanitize=False):
    directory.mkdir(parents=True, exist_ok=True)
    scaffold = types.ModuleType("restart_mat_fixture")
    scaffold.__file__=str(ROOT/"tools/test-truehd-mat-diagnostics.py")
    exec(compile(source("tools/test-truehd-mat-diagnostics.py",BASELINE_REF),scaffold.__file__,"exec"),scaffold.__dict__)
    text=scaffold.HARNESS.split('int main(int argc, char** argv) {',1)[0]
    text=text.replace('#include "PackerMAT.h"','#include "PackerMAT.h"\n#include "TrueHDRestartTrace.h"\nconstexpr double DVD_NOPTS_VALUE=-1e300;\nstruct Logging{bool enabled=true;bool IsLogLevelLogged(int)const{return enabled;}};\nstruct CServiceBroker{inline static Logging log;static Logging&GetLogging(){return log;}};')
    text=text.replace('  Format format;\n};','  Format format;\n  AE::TrueHDRestartTrace trueHDRestartTrace;\n};',1)
    text=text.replace('  bool m_lavStyleSyncEnabled = true, m_needsResync = false;', '  bool m_lavStyleSyncEnabled = true, m_needsResync = false, m_deviceIsRAW = false;\n  AE::CTrueHDRestartTrace m_restartTrace;\n  unsigned m_trueHDoffset=0,m_dataSize=0,m_bufferSize=0,m_backlogSize=0;\n  double m_nextPts=LOCAL_NOPTS,m_truehd_ptsCache=LOCAL_NOPTS;bool m_truehd_ptsCacheValid=false;\n  struct{void Reset(){}}m_parser;\n  void Reset();')
    codec=source('xbmc/cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecPassthrough.cpp',ref)
    get=block(codec,'void CDVDAudioCodecPassthrough::GetData(DVDAudioFrame &frame)')
    reset=block(codec,'void CDVDAudioCodecPassthrough::Reset()')
    traced='trueHDRestartTrace' in get
    if mutation=='missing-producer':
        old='frame.trueHDRestartTrace = m_restartTrace.Next(frame.data[0], frame.nb_frames);'
        assert old in get
        get=get.replace(old,'frame.trueHDRestartTrace = {};',1)
    text=text.replace('@GET_DATA@',get+'\n'+reset)
    text=text.replace('@DURATION@',block(source('xbmc/cores/AudioEngine/Utils/AEStreamInfo.cpp',ref),'double CAEStreamInfo::GetDuration() const'))
    text+=PRODUCER_MAIN.replace('@TRACED@','true' if traced else 'false')
    (directory/'TrueHDRestartTrace.h').write_text(source(HEADER))
    (directory/'utils').mkdir(exist_ok=True)
    (directory/'utils/log.h').write_text(r'''
#pragma once
#include <sstream>
#include <string>
#include <vector>
#include <cstring>
constexpr int LOGDEBUG=0,LOGERROR=1;
struct CLog{inline static std::vector<std::string>records;template<class...T>static void Log(int,const char*m,T...a){std::ostringstream s;s<<m;((s<<' '<<a),...);records.push_back(s.str());}};
''')
    (directory/'libavutil').mkdir(exist_ok=True)
    (directory/'libavutil/common.h').write_text('#define FFALIGN(x,a) (((x)+(a)-1)&~((a)-1))\n')
    (directory/'libavutil/intreadwrite.h').write_text('#define AV_RB16(p) (((unsigned)(p)[0]<<8)|(p)[1])\n#define AV_RB32(p) (((unsigned)AV_RB16(p)<<16)|AV_RB16((p)+2))\n')
    for suffix in ['h','cpp']:(directory/('PackerMAT.'+suffix)).write_text(source('xbmc/cores/AudioEngine/Utils/PackerMAT.'+suffix,ref))
    # These two unchanged public headers are the production codec's helpers.
    for rel in ['cores/VideoPlayer/DVDCodecs/Audio/FloatingAverage.h','cores/VideoPlayer/Interface/TimingConstants.h']:
        target=directory/rel;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source('xbmc/'+rel,ref))
    return execute(directory,'producer',text,[directory/'PackerMAT.cpp'],sanitize)

def verify(args, directory):
    baseline = subprocess.check_output(['git','rev-parse','--verify',args.baseline_ref+'^{commit}'],cwd=ROOT,text=True).strip()
    (directory/'TrueHDRestartTrace.h').write_text(source(HEADER))
    window_bytes, stdout=execute(directory,'window',WINDOW,sanitize=args.sanitize)
    assert window_bytes==b'1'
    print(stdout.strip(),flush=True)
    records=[]
    for name,fixture in [('producer',producer),('transfer',transfer),('pipeline',pipeline)]:
        previous,old_stdout=fixture(directory/(name+'-baseline'),baseline,sanitize=args.sanitize)
        actual,new_stdout=fixture(directory/(name+'-actual'),sanitize=args.sanitize)
        assert actual==previous, name+': payload/control behavior changed'
        records.append({'fixture':name,'bytes':len(actual),'sha256':hashlib.sha256(actual).hexdigest(),'parent_equal':True})
        print(new_stdout.strip(),flush=True)
        print(name+': '+str(len(actual))+' complete behavior bytes equal to '+baseline,flush=True)
    controls=[('missing-producer',producer),('missing-handoff',transfer),('stale-pool',transfer),('inserted-tag',pipeline),('mislabel-drop',pipeline)]
    rejected=[]
    for mutation,fixture in controls:
        try:
            fixture(directory/mutation,mutation=mutation,sanitize=args.sanitize)
        except RuntimeError as error:
            # Compilation failure or sanitizer crash is never a semantic rejection.
            assert 'Assertion' in str(error) and 'AddressSanitizer' not in str(error) and 'runtime error:' not in str(error),str(error)
            rejected.append(mutation);print('Rejected '+mutation,flush=True)
        else:
            raise AssertionError(mutation+' unexpectedly passed')
    for mutation,old,new in [('unbounded-window','PACKET_LIMIT = 32','PACKET_LIMIT = 64'),('wrong-slice-offset','result.offset = sourceOffset;','result.offset = 0;')]:
        child=directory/mutation;child.mkdir();header=source(HEADER);assert old in header;(child/'TrueHDRestartTrace.h').write_text(header.replace(old,new,1))
        try:
            execute(child,'window',WINDOW,sanitize=args.sanitize)
        except RuntimeError as error:
            assert 'Assertion' in str(error) and 'AddressSanitizer' not in str(error),str(error)
            rejected.append(mutation);print('Rejected '+mutation,flush=True)
        else:raise AssertionError(mutation+' unexpectedly passed')
    result={'baseline_ref':baseline,'production_header_sha256':hashlib.sha256(source(HEADER).encode()).hexdigest(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'behavior':records,'negative_controls':rejected,'sanitize':args.sanitize,'limits':['Host adapters for clocks, packet pools, API acceptance and logging; no real ALSA/HDMI receiver.', 'Producer fixture executes actual codec Reset/GetData with synthetic output bytes, not lossless decoding.', 'Pipeline executes actual AE RAW queue decision and sync acquisition prefix; subsequent in-sync servo is outside this fixture.', 'No physical timing neutrality, image inclusion or receiver acceptance is established.']}
    (directory/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref',default=BASELINE_REF)
    parser.add_argument('--output',type=Path,help='Retain generated fixtures in a new directory')
    parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True)
        verify(args,args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='truehd-restart-') as temporary:
            verify(args,Path(temporary))

if __name__=='__main__':
    main()
