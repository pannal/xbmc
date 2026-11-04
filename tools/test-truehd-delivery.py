#!/usr/bin/env python3
"""Run actual sink methods with controlled ALSA/clock boundaries.

Compares baseline/work complete calls, bytes, offsets, returns and recoveries.
Checks mandatory --diagnostic-ref CRCs against an independent bitwise CRC over
the bytes the fake ALSA API actually accepts, even after working-source tracing
is removed. Does not establish real ALSA or HDMI delivery.
"""
import argparse
from pathlib import Path
import subprocess
import os
import shutil
import hashlib
import json
import tempfile

DIAGNOSTIC_BASE = '9a90ccab8904ef1cf98b18bc7d1348cf469f33f2'

def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <climits>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fstream>
#include <iterator>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include "AEStreamInfo.h"
#include "AEBitstreamPacker.h"
#include "Crc32.h"
#include "utils/log.h"
#include "EndianSwap.h"
using namespace std::chrono_literals;
std::ofstream result;
template<class T> void record(const T& v){result.write(reinterpret_cast<const char*>(&v),sizeof(v));}
void recordText(const std::string&s){record(s.size());result.write(s.data(),s.size());}
void recordBytes(const uint8_t*p,size_t n){record(n);result.write(reinterpret_cast<const char*>(p),n);}
void observeLog(const LogRecord& r){if(r.message.rfind("TrueHD delivery",0)!=0){recordText("log");recordText(r.message);for(auto&s:r.args)recordText(s);}}
std::vector<uint8_t> expectedCarrier(const std::vector<uint8_t>&input,bool swap){std::vector<uint8_t>v(61440);const uint8_t header[8]={0x72,0xf8,0x1f,0x4e,0x16,0,0xf0,0xef};std::copy(header,header+8,v.begin());for(size_t i=8;i<61440;i+=2){v[i]=input[i+1];v[i+1]=input[i];}if(swap)for(size_t i=0;i<v.size();i+=2)std::swap(v[i],v[i+1]);return v;}
uint32_t oracle(const uint8_t* p,size_t n){uint32_t c=0xffffffff;for(size_t i=0;i<n;i++){c^=uint32_t(p[i])<<24;for(int bit=0;bit<8;bit++)c=(c<<1)^((c&0x80000000)?0x04c11db7:0);}return c;}
struct Logging {bool enabled=true;bool IsLogLevelLogged(int)const{return enabled;}};
struct CServiceBroker {inline static Logging log;static Logging&GetLogging(){return log;}};
struct Format {int m_dataFormat=0;unsigned m_frameSize=16,m_frames=1440,m_sampleRate=192000;CAEStreamInfo m_streamInfo;};
constexpr int AE_FMT_RAW=0,AE_FMT_S16NE=1,AE_FMT_S16BE=2;
struct AEDelayStatus {double delay=0.125;void SetDelay(double d){delay=d;}};
struct PCM {int state=1;std::deque<int>writes,recover,resume;int prepareResult=0;uint8_t* root=nullptr;size_t size=0;unsigned frameSize=16;std::vector<uint8_t>accepted;};
constexpr int SND_PCM_STATE_PREPARED=1;
int snd_pcm_writei(PCM*p,void*b,unsigned amount){auto*bytes=static_cast<uint8_t*>(b);assert(bytes>=p->root&&bytes+amount*p->frameSize<=p->root+p->size);int ret=p->writes.empty()?int(amount):p->writes.front();if(!p->writes.empty())p->writes.pop_front();assert(ret<=int(amount));recordText("write");record(size_t(bytes-p->root));record(amount);record(ret);recordBytes(bytes,amount*p->frameSize);if(ret>0)p->accepted.insert(p->accepted.end(),bytes,bytes+ret*p->frameSize);return ret;}
const char*snd_strerror(int){return "controlled ALSA error";}
int snd_pcm_recover(PCM*p,int err,int silent){int r=p->recover.empty()?0:p->recover.front();if(!p->recover.empty())p->recover.pop_front();recordText("recover");record(err);record(silent);record(r);if(r==0)p->state=SND_PCM_STATE_PREPARED;return r;}
int snd_pcm_state(PCM*p){recordText("state");record(p->state);return p->state;}
int snd_pcm_start(PCM*p){recordText("start");p->state=2;return 0;}
int snd_pcm_prepare(PCM*p){recordText("prepare");record(p->prepareResult);p->state=SND_PCM_STATE_PREPARED;return p->prepareResult;}
int snd_pcm_resume(PCM*p){int r=p->resume.empty()?0:p->resume.front();if(!p->resume.empty())p->resume.pop_front();recordText("resume");record(r);return r;}
struct CThread {static void Sleep(std::chrono::milliseconds n){recordText("sleep");record(n.count());}};
namespace KODI::TIME {void Sleep(std::chrono::milliseconds n){CThread::Sleep(n);}}
struct CAESinkALSA {PCM*m_pcm=nullptr;Format m_format;bool m_passthrough=true,m_fragmented=false;unsigned m_originalPeriodSize=233;unsigned AddPackets(uint8_t**,unsigned,unsigned);void HandleError(const char*,int);};
@ALSA@
@HANDLE_ERROR@
size_t cases=0,traceRecords=0;
constexpr bool traceAvailable=@TRACE_AVAILABLE@;
void require(bool pass,const char*why){if(!pass){std::cerr<<"ASSERT case "<<cases<<": "<<why<<'\n';std::abort();}}
std::vector<LogRecord> logsSince(size_t at,const char*prefix){std::vector<LogRecord>v;for(;at<CLog::records.size();at++)if(CLog::records[at].message.rfind(prefix,0)==0)v.push_back(CLog::records[at]);return v;}
uint64_t number(const LogRecord&r,size_t at){return std::stoull(r.args.at(at));}
unsigned checkedAdd(CAESinkALSA&alsa,uint8_t**data,unsigned frames,unsigned offset){auto begin=CLog::records.size();auto accepted=alsa.m_pcm?alsa.m_pcm->accepted.size():0;auto ret=alsa.AddPackets(data,frames,offset);auto logs=logsSince(begin,"TrueHD delivery ALSA:");bool expect=traceAvailable&&CServiceBroker::log.enabled&&alsa.m_pcm&&alsa.m_passthrough&&alsa.m_format.m_streamInfo.m_type==CAEStreamInfo::STREAM_TYPE_TRUEHD;require(logs.size()==size_t(expect),"ALSA trace gating");if(expect){auto&r=logs.front();auto&p=*alsa.m_pcm;require(number(r,0)==offset&&number(r,1)==frames&&number(r,2)==ret&&number(r,3)==alsa.m_format.m_frameSize,"ALSA trace accounting");require(p.accepted.size()-accepted==ret*alsa.m_format.m_frameSize,"API acceptance frame accounting");auto expected=oracle(p.accepted.data()+accepted,p.accepted.size()-accepted);require(number(r,4)==expected,"ALSA CRC differs from accepted fragments");traceRecords++;}return ret;}
struct FakeSink {CAESinkALSA alsa;unsigned AddPackets(uint8_t**data,unsigned frames,unsigned offset){if(alsa.m_pcm){alsa.m_pcm->root=data[0];alsa.m_pcm->size=65536;}recordText("add");record(frames);record(offset);auto r=checkedAdd(alsa,data,frames,offset);recordText("add-return");record(r);return r;}void GetDelay(AEDelayStatus&s){recordText("delay");s.delay=.125;}void AddPause(int ms){recordText("pause");record(ms);}};
struct Stats {void UpdateSinkDelay(const AEDelayStatus&s,int packets){recordText("stats");record(s.delay);record(packets);}};
struct Packet {uint8_t*data[1]{};unsigned nb_samples=61440;int pause_burst_ms=0;};
struct CSampleBuffer {Packet*pkt=nullptr;void*pool=nullptr;};
struct CAEUtil {static bool S16NeedsByteSwap(int,int output){return output==AE_FMT_S16BE;}};
struct CActiveAESink {enum SwapState{SKIP_SWAP,NEED_BYTESWAP,CHECK_SWAP};Format m_requestedFormat,m_sinkFormat;bool m_needIecPack=true,m_extStreaming=false,m_extError=false;SwapState m_swapState=SKIP_SWAP;CAEBitstreamPacker*m_packer=nullptr;FakeSink*m_sink=nullptr;Stats*m_stats=nullptr;unsigned OutputSamples(CSampleBuffer*);void SwapInit(CSampleBuffer*);};
@OUTPUT@
@SWAP@
struct FakeMat {std::vector<uint8_t>v;bool PackTrueHD(const uint8_t*,int){return true;}std::vector<uint8_t>GetOutputFrame(){return v;}uint64_t GetOutputFrameNumber(){return 17;}bool HadDiscontinuity(){return false;}};
struct Codec {FakeMat*m_packerMAT=nullptr;uint8_t*m_buffer=nullptr;unsigned m_dataSize=0;std::vector<uint8_t>m_trueHDBuffer;bool m_lavStyleSyncEnabled=false,m_truehd_ptsCacheValid=false;double m_currentPts=12,m_truehd_ptsCache=11;void run(){@CODEC_BRANCH@}};
constexpr unsigned TRUEHD_BUF_SIZE=61440;constexpr double LOCAL_NOPTS=-1;
void script(PCM&p,int plan){switch(plan){case 1:p.writes={17,281};break;case 2:break;case 3:p.writes={0};break;case 4:p.writes={0,0,0,0,0};break;case 5:p.writes={-EPIPE};p.recover={0};break;case 6:p.writes={-EPIPE};p.recover={-EPIPE};break;case 7:p.writes={-EPIPE,-EPIPE};p.recover={-EPIPE};break;case 8:p.writes={-ESTRPIPE};p.recover={-ESTRPIPE};p.resume={-EAGAIN,0};break;default:break;}}
int main(int argc,char**argv){assert(argc==2);result.open(argv[1],std::ios::binary);const auto*known=reinterpret_cast<const uint8_t*>("123456789");require(oracle(known,9)==0x0376e6e7&&Crc32::Compute(known,9)==0x0376e6e7,"CRC known answer");
 for(bool debug:{false,true})for(int mode=0;mode<5;mode++)for(int swap=0;swap<4;swap++)for(int plan=0;plan<9;plan++){
 ++cases;CLog::records.clear();CServiceBroker::log.enabled=debug;record(cases);std::vector<uint8_t>input(65536);for(size_t i=0;i<input.size();i++)input[i]=(i*73+(i>>8)*19+(i>>4))&255;
 PCM pcm;script(pcm,plan);CAEBitstreamPacker packer;FakeSink sink;Stats stats;Packet packet;packet.data[0]=input.data();CSampleBuffer sample{&packet,cases%2?&packet:nullptr};CActiveAESink active;active.m_packer=&packer;active.m_sink=&sink;active.m_stats=&stats;active.m_sinkFormat.m_dataFormat=swap==3?AE_FMT_S16BE:AE_FMT_S16NE;active.m_swapState=swap==0?CActiveAESink::SKIP_SWAP:swap==1?CActiveAESink::NEED_BYTESWAP:CActiveAESink::CHECK_SWAP;sink.alsa.m_pcm=&pcm;sink.alsa.m_fragmented=plan==2;pcm.frameSize=16;
 if(mode==1){active.m_sinkFormat.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_AC3;packet.nb_samples=64;}else if(mode==2){active.m_requestedFormat.m_dataFormat=AE_FMT_S16NE;packet.nb_samples=1000;active.m_sinkFormat.m_frameSize=4;pcm.frameSize=4;sink.alsa.m_passthrough=false;}else if(mode==3){packet.nb_samples=0;packet.pause_burst_ms=20;}else if(mode==4){active.m_needIecPack=false;packet.nb_samples=0;packet.pause_burst_ms=20;}
 sink.alsa.m_format=active.m_sinkFormat;auto before=input;auto r=active.OutputSamples(&sample);record(r);record(active.m_extError);record(active.m_swapState);recordBytes(input.data(),input.size());recordBytes(pcm.accepted.data(),pcm.accepted.size());require(input==before,"OutputSamples modified original input");if(mode==0){auto expected=expectedCarrier(input,swap==1||swap==3);require(std::equal(expected.begin(),expected.end(),packer.GetBuffer()),"IEC preamble/payload/byte-order oracle");}auto logs=logsSince(0,"TrueHD delivery sink:");bool expect=traceAvailable&&debug&&mode==0;require(logs.size()==size_t(expect),"sink trace gating");if(expect){auto&l=logs.front();require(number(l,0)==oracle(input.data()+8,61432),"sink payload CRC range");require(number(l,1)==61440&&number(l,3)==3840&&number(l,4)==16,"sink carrier accounting");require(number(l,2)==oracle(packer.GetBuffer(),61440),"sink CRC precedes final byte order");traceRecords++;}
 }
 for(bool debug:{false,true})for(bool truehd:{false,true})for(bool passthrough:{false,true})for(int plan=0;plan<9;plan++){
 ++cases;CLog::records.clear();CServiceBroker::log.enabled=debug;record(cases);std::vector<uint8_t>data(65536);for(size_t i=0;i<data.size();i++)data[i]=(i*29+(i>>5))&255;PCM pcm;script(pcm,plan);pcm.root=data.data();pcm.size=data.size();CAESinkALSA alsa;alsa.m_pcm=&pcm;alsa.m_fragmented=plan==2;alsa.m_passthrough=passthrough;if(!truehd)alsa.m_format.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_AC3;uint8_t*pointer=data.data();auto ret=checkedAdd(alsa,&pointer,800,23);record(ret);recordBytes(pcm.accepted.data(),pcm.accepted.size());}
 // Missing sink returns immediately without diagnostics or writes.
 {++cases;CLog::records.clear();CAESinkALSA alsa;uint8_t*none=nullptr;require(checkedAdd(alsa,&none,10,2)==INT_MAX,"missing ALSA sink return");}
 for(bool debug:{false,true})for(bool lav:{false,true}){
 ++cases;CLog::records.clear();CServiceBroker::log.enabled=debug;FakeMat mat;mat.v.resize(61440);for(size_t i=0;i<mat.v.size();i++)mat.v[i]=(i*31+(i>>8))&255;Codec codec;codec.m_packerMAT=&mat;codec.m_lavStyleSyncEnabled=lav;codec.m_truehd_ptsCacheValid=lav;codec.run();record(codec.m_dataSize);record(codec.m_currentPts);record(codec.m_truehd_ptsCache);record(codec.m_truehd_ptsCacheValid);recordBytes(codec.m_trueHDBuffer.data(),codec.m_trueHDBuffer.size());auto logs=logsSince(0,"TrueHD delivery codec:");require(logs.size()==size_t(traceAvailable&&debug),"codec trace gating");if(traceAvailable&&debug){auto&l=logs.front();require(number(l,0)==17&&number(l,1)==61440,"codec trace identity");require(number(l,2)==oracle(mat.v.data()+8,61432),"codec CRC includes reserved header");traceRecords++;}}
 std::cout<<cases<<" cases; "<<traceRecords<<" CRC trace assertions; complete behavior recorded\n";
}
'''


def source(root,path,ref):
    return subprocess.check_output(['git','show',f'{ref}:{path}'],cwd=root,text=True) if ref else (root/path).read_text()


def build(root,directory,ref=None,mutation=None,sanitize=False,require_diagnostics=False):
    directory.mkdir(parents=True,exist_ok=True)
    read=lambda p:source(root,p,ref)
    ae='xbmc/cores/AudioEngine/'
    output=block(read(ae+'Engines/ActiveAE/ActiveAESink.cpp'),'unsigned int CActiveAESink::OutputSamples(')
    alsa=block(read(ae+'Sinks/AESinkALSA.cpp'),'unsigned int CAESinkALSA::AddPackets(')
    codec=block(read('xbmc/cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecPassthrough.cpp'),'if (m_packerMAT->PackTrueHD(')
    if require_diagnostics:
        assert 'TrueHD delivery sink:' in output and 'TrueHD delivery other:' in output, 'sink traces missing'
        assert 'TrueHD delivery ALSA:' in alsa, 'ALSA trace missing'
        assert 'TrueHD delivery codec:' in codec, 'codec trace missing'
    if mutation=='wrong-offset':
        old='buffer = data[0]+offset*m_format.m_frameSize + frames_written*m_format.m_frameSize;'
        assert old in alsa
        alsa=alsa.replace(old,'buffer = data[0] + frames_written*m_format.m_frameSize;')
    elif mutation=='missing-fragment':
        old='acceptedCrc.Compute(static_cast<const char*>(buffer), ret * m_format.m_frameSize);'
        assert old in alsa
        alsa=alsa.replace(old,'if (frames_written == 0) '+old)
    elif mutation=='pre-swap-crc':
        old='Crc32::Compute(buffer[0], totalFrames * m_sinkFormat.m_frameSize)'
        assert old in output
        output=output.replace('  uint8_t **buffer', '  uint32_t beforeSwapCrc=0;\n  uint8_t **buffer',1)
        output=output.replace('      switch(m_swapState)','      beforeSwapCrc=Crc32::Compute(buffer[0], size);\n      switch(m_swapState)',1)
        output=output.replace(old,'beforeSwapCrc')
    elif mutation=='codec-header':
        old='Crc32::Compute(m_trueHDBuffer.data() + 8, m_trueHDBuffer.size() - 8)'
        assert old in codec
        codec=codec.replace(old,'Crc32::Compute(m_trueHDBuffer.data(), m_trueHDBuffer.size())')
    trace='TrueHD delivery sink:' in output
    harness=HARNESS.replace('@OUTPUT@',output).replace('@ALSA@',alsa).replace('@CODEC_BRANCH@',codec)
    harness=harness.replace('@HANDLE_ERROR@',block(read(ae+'Sinks/AESinkALSA.cpp'),'void CAESinkALSA::HandleError('))
    harness=harness.replace('@SWAP@',block(read(ae+'Engines/ActiveAE/ActiveAESink.cpp'),'void CActiveAESink::SwapInit('))
    harness=harness.replace('@TRACE_AVAILABLE@','true' if trace else 'false')
    # Constants are visible to the actual codec branch definition.
    harness=harness.replace('struct FakeMat','constexpr unsigned TRUEHD_BUF_SIZE=61440;constexpr double LOCAL_NOPTS=-1;\nstruct FakeMat',1)
    harness=harness.replace('constexpr unsigned TRUEHD_BUF_SIZE=61440;constexpr double LOCAL_NOPTS=-1;\nvoid script','void script')
    (directory/'main.cpp').write_text(harness)
    for p in ['AEBitstreamPacker.cpp','AEBitstreamPacker.h','AEPackIEC61937.cpp','AEPackIEC61937.h']:(directory/p).write_text(read(ae+'Utils/'+p))
    for p in ['Crc32.cpp','Crc32.h','EndianSwap.cpp','EndianSwap.h']:(directory/p).write_text(read('xbmc/utils/'+p))
    (directory/'utils').mkdir(exist_ok=True)
    (directory/'utils/StringUtils.h').write_text('#include <string>\nstruct StringUtils {static void ToLower(std::string&) {}};\n')
    (directory/'utils/log.h').write_text(r'''
#pragma once
#include <cstring>
#include <vector>
#include <string>
#include <sstream>
constexpr int LOGDEBUG=0,LOGERROR=1,LOGINFO=2;
struct LogRecord{std::string message;std::vector<std::string>args;};
void observeLog(const LogRecord&);
struct CLog {inline static std::vector<LogRecord>records;template<class T>static std::string text(T value){std::ostringstream out;out<<value;return out.str();}template<class...T>static void Log(int,const char*message,T...args){LogRecord r{message,{text(args)...}};records.push_back(r);observeLog(r);}};
''')
    (directory/'AEChannelInfo.h').write_text('#pragma once\nconstexpr int AE_CH_RAW=1;struct CAEChannelInfo{unsigned n=0;void operator+=(int){n++;}unsigned Count()const{return n;}};\n')
    (directory/'AEStreamInfo.h').write_text(r'''
#pragma once
struct CAEStreamInfo{enum Type {STREAM_TYPE_TRUEHD,STREAM_TYPE_AC3,STREAM_TYPE_EAC3,STREAM_TYPE_DTSHD,STREAM_TYPE_DTSHD_MA,STREAM_TYPE_DTSHD_CORE,STREAM_TYPE_DTS_512,STREAM_TYPE_DTS_1024,STREAM_TYPE_DTS_2048};Type m_type=STREAM_TYPE_TRUEHD;unsigned m_sampleRate=48000,m_repeat=1,m_dtsPeriod=512;bool m_dataIsLE=false;};
''')
    cmd=['g++','-std=c++17','-O1','-g','-DTARGET_POSIX','-I'+str(directory),str(directory/'main.cpp'),str(directory/'AEBitstreamPacker.cpp'),str(directory/'AEPackIEC61937.cpp'),str(directory/'Crc32.cpp'),str(directory/'EndianSwap.cpp'),'-o',str(directory/'run')]
    if sanitize:cmd[1:1]=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
    subprocess.run(cmd,check=True,capture_output=True,text=True)
    result=directory/'behavior.bin'
    r=subprocess.run([str(directory/'run'),str(result)],capture_output=True,text=True,env=os.environ.copy())
    (directory/'stdout.txt').write_text(r.stdout);(directory/'stderr.txt').write_text(r.stderr)
    return r,result



def catalog(root,directory,media,destination,sanitize=False):
    raw = media.read_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    assert len(raw) >= 8 and raw[4:8] == bytes.fromhex('f8726fba'), 'expected raw TrueHD starting at major sync'
    offset, expected_aus = 0, 0
    while offset < len(raw):
        assert offset + 8 <= len(raw), 'truncated AU header'
        length = (((raw[offset] & 15) << 8) | raw[offset + 1]) * 2
        assert length >= 8 and offset + length <= len(raw), 'truncated AU'
        offset += length
        expected_aus += 1
    target=directory/'catalog-build';shutil.copytree(directory/'actual',target,dirs_exist_ok=True)
    text=(target/'main.cpp').read_text();text=text[:text.index('int main(int argc,char**argv)')]
    text+=r'\n#include "PackerMAT.h"\n'.replace('\\n','\n')
    text+=r"""
int main(int argc,char**argv){assert(argc==3);std::ifstream f(argv[1],std::ios::binary);std::vector<uint8_t>raw((std::istreambuf_iterator<char>(f)),{});std::ofstream out(argv[2]);out<<"[\n";CPackerMAT mat;mat.SetLavStyleEnabled(true);CServiceBroker::log.enabled=false;size_t at=0,aus=0,count=0;while(at<raw.size()){assert(at+8<=raw.size());size_t n=(((raw[at]&15)<<8)|raw[at+1])*2;assert(n>=8&&at+n<=raw.size());mat.PackTrueHD(raw.data()+at,n);for(;;){auto input=mat.GetOutputFrame();if(input.empty())break;assert(input.size()==61440);uint32_t payload=Crc32::Compute(input.data()+8,input.size()-8);uint32_t carrier[2]{};for(int swapped=0;swapped<2;swapped++){PCM pcm;CAEBitstreamPacker packer;FakeSink sink;Stats stats;Packet packet;packet.nb_samples=input.size();packet.data[0]=input.data();CSampleBuffer sample{&packet,nullptr};CActiveAESink active;active.m_packer=&packer;active.m_sink=&sink;active.m_stats=&stats;active.m_sinkFormat.m_frames=3840;active.m_swapState=swapped?CActiveAESink::NEED_BYTESWAP:CActiveAESink::SKIP_SWAP;sink.alsa.m_pcm=&pcm;sink.alsa.m_format=active.m_sinkFormat;require(active.OutputSamples(&sample)==125&&!active.m_extError,"catalog delivery result");auto expected=expectedCarrier(input,swapped);require(pcm.accepted==expected,"catalog full IEC carrier differs from independent vector");carrier[swapped]=Crc32::Compute(pcm.accepted.data(),pcm.accepted.size());}if(count)out<<",\n";out<<"{\"mat\":"<<++count<<",\"payload_crc\":\""<<std::hex<<std::setfill('0')<<std::setw(8)<<payload<<"\",\"carrier_crc\":\""<<std::setw(8)<<carrier[0]<<"\",\"swapped_carrier_crc\":\""<<std::setw(8)<<carrier[1]<<"\"}"<<std::dec;}at+=n;aus++;}out<<"\n]\n";assert(aus>0&&count>0);std::cout<<aus<<" AUs; "<<count<<" MATs; both complete carrier byte orders independently verified\n";}
"""
    (target/'main.cpp').write_text(text)
    for ext in ['h','cpp']:(target/f'PackerMAT.{ext}').write_text((root/f'xbmc/cores/AudioEngine/Utils/PackerMAT.{ext}').read_text())
    (target/'libavutil').mkdir(exist_ok=True)
    (target/'libavutil/common.h').write_text('#define FFALIGN(x,a) (((x)+(a)-1)&~((a)-1))\n')
    (target/'libavutil/intreadwrite.h').write_text('#define AV_RB16(p) (((unsigned)(p)[0]<<8)|(p)[1])\n#define AV_RB32(p) (((unsigned)AV_RB16(p)<<16)|AV_RB16((p)+2))\n')
    command=['g++','-std=c++17','-O1','-g','-DTARGET_POSIX','-I'+str(target),*[str(target/x) for x in ['main.cpp','AEBitstreamPacker.cpp','AEPackIEC61937.cpp','Crc32.cpp','EndianSwap.cpp','PackerMAT.cpp']],'-o',str(target/'catalog')]
    if sanitize:command[1:1]=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
    subprocess.run(command,check=True,capture_output=True,text=True)
    executed=subprocess.run([str(target/'catalog'),str(media),str(target/'records.json')],capture_output=True,text=True)
    (target/'stdout.txt').write_text(executed.stdout);(target/'stderr.txt').write_text(executed.stderr)
    assert executed.returncode==0,executed.stderr
    records=json.loads((target/'records.json').read_text())
    assert int(executed.stdout.split()[0]) == expected_aus
    artifact={'input_sha256':raw_sha,'input_bytes':len(raw),'input_aus':expected_aus,'records':records}
    destination.write_text(json.dumps(artifact,indent=2)+'\n');print(executed.stdout.strip());print('Catalog',destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline-ref', default=DIAGNOSTIC_BASE,
                        help='full-output baseline Git revision (default: pinned pre-cleanup source)')
    parser.add_argument('--diagnostic-ref', default=DIAGNOSTIC_BASE,
                        help='instrumented Git revision for mandatory diagnostic assertions/controls')
    parser.add_argument('--output', type=Path, help='retain generated fixtures in this directory')
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--media', type=Path, help='optional complete raw TrueHD for a source catalog')
    parser.add_argument('--catalog', type=Path, help='output JSON for --media')
    args = parser.parse_args()
    if args.media and not args.catalog:
        parser.error('--media requires --catalog')
    if args.catalog:
        args.catalog = args.catalog.resolve()
    if args.output:
        run(args)
    else:
        with tempfile.TemporaryDirectory(prefix='truehd-delivery-') as temporary:
            args.output = Path(temporary)
            run(args)


def run(a):
    resolve=lambda ref:subprocess.check_output(['git','rev-parse','--verify',ref+'^{commit}'],cwd=a.root,text=True).strip()
    baseline,diagnostic=resolve(a.baseline_ref),resolve(a.diagnostic_ref)
    for name,ref in [('baseline',baseline),('actual',None)]:
        r,artifact=build(a.root,a.output/name,ref=ref,sanitize=a.sanitize)
        if r.returncode:raise RuntimeError(f'{name}: {r.stderr}')
        print(name,r.stdout.strip())
    first=(a.output/'baseline/behavior.bin').read_bytes();second=(a.output/'actual/behavior.bin').read_bytes();assert first==second,'complete bytes/calls/offsets/returns/recovery changed'
    print(f'Baseline {baseline}: {len(first)} behavior bytes identical')
    r,artifact=build(a.root,a.output/'diagnostic',ref=diagnostic,sanitize=a.sanitize,require_diagnostics=True)
    if r.returncode:raise RuntimeError(f'diagnostic: {r.stderr}')
    assert artifact.read_bytes()==first,'diagnostic source differs in complete behavior'
    print('diagnostic',diagnostic,r.stdout.strip())
    for mutant in ['wrong-offset','missing-fragment','pre-swap-crc','codec-header']:
        diagnostic_mutant=mutant!='wrong-offset'
        r,artifact=build(a.root,a.output/mutant,ref=diagnostic if diagnostic_mutant else None,
                         mutation=mutant,sanitize=a.sanitize,require_diagnostics=diagnostic_mutant)
        if mutant == 'wrong-offset':
            assert r.returncode == 0 and artifact.read_bytes() != first, (mutant,r.stdout,r.stderr)
            print('Rejected',mutant,'complete byte/call/offset baseline mismatch')
        else:
            assert r.returncode and 'ASSERT' in r.stderr,(mutant,r.stdout,r.stderr)
            print('Rejected',mutant, r.stderr.strip().splitlines()[-1])

    if a.media:
        assert a.catalog, '--media needs --catalog'
        catalog(a.root,a.output,a.media,a.catalog,a.sanitize)


if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as e:
        print(e.stderr);raise
