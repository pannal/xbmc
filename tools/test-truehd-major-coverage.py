#!/usr/bin/env python3
"""Verify source-AU major tracing against real MAT copies and host libavutil CRC.

Synthetic AUs exercise provenance, header integrity and byte mapping, not audio
decoding. The production packer is compiled unchanged; private state is exposed
only to place an AU at every header split without a huge media fixture.
"""
import argparse
import ctypes.util
import importlib.util
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "xbmc/cores/AudioEngine/Utils"
HARNESS = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iostream>
#include <map>
#include <string>
#include <vector>
#include "utils/log.h"
#define private public
#include "PackerMAT.h"
#undef private
extern "C" {
int av_crc_init(uint32_t*, int, int, unsigned int, int);
uint32_t av_crc(const uint32_t*, uint32_t, const uint8_t*, size_t);
}
using Bytes=std::vector<uint8_t>;
static unsigned checks=0;
static uint32_t table[1024];
void check(bool value) { ++checks; assert(value); }
void checksum(Bytes& f) {
  const unsigned h=28+((f[29]&1)?2+2*(f[30]>>4):0);
  auto crc=av_crc(table,0,f.data()+4,h-4);
  crc^=(f[h+1]<<8)|f[h]; f[h+2]=crc;f[h+3]=crc>>8;
}
Bytes major(int extension=-1, unsigned salt=0, unsigned bytes=256) {
  Bytes f(bytes);f[0]=f.size()>>9;f[1]=f.size()/2;
  f[4]=0xf8;f[5]=0x72;f[6]=0x6f;f[7]=0xba;
  f[12]=0xb7;f[13]=0x52;f[20]=0x10;
  if(extension>=0){f[29]=1;f[30]=extension<<4;}
  for(unsigned i=40;i<200;++i) f[i]=static_cast<uint8_t>(i*37+salt);
  checksum(f);return f;
}
unsigned num(const LogRecord& r,unsigned n){return std::stoul(r.args.at(n));}
std::vector<LogRecord> majors(){std::vector<LogRecord> out;for(const auto&r:CLog::records)if(r.message.find("stage=major ")!=std::string::npos)out.push_back(r);return out;}
void compare(const LogRecord& r, const Bytes& au, CPackerMAT& p,
             const std::vector<Bytes>& emitted) {
  check(num(r,0)==71 && num(r,2)==au.size());
  check(num(r,4)==1 && num(r,5)==1 && num(r,6)==1);
  Bytes recovered;
  for(unsigned index:{8u,11u}) {
    const auto packet=num(r,index),offset=num(r,index+1),bytes=num(r,index+2);
    if(!bytes){check(packet==0);continue;}
    check(offset>=28 && offset+bytes<=61416);
    check(offset+bytes<=30716 || offset>=30728);
    const auto& mat=packet<=emitted.size()?emitted.at(packet-1):p.m_buffer;
    check(packet<=emitted.size()+1 && offset+bytes<=mat.size());
    recovered.insert(recovered.end(),mat.begin()+offset,mat.begin()+offset+bytes);
  }
  check(recovered==au);
}
void positioned(unsigned position,int extension,unsigned bytes=256) {
  auto input=major(extension,0,bytes);const auto original=input;
  CPackerMAT p;p.SetLavStyleEnabled(true);p.WriteHeader();p.m_bufferCount=position;
  p.SetRestartTrace(71);CLog::records.clear();p.PackTrueHD(input.data(),input.size());
  std::vector<Bytes> frames;for(;;){auto f=p.GetOutputFrame();if(f.empty())break;frames.push_back(std::move(f));}
  auto records=majors();check(records.size()==1);compare(records[0],input,p,frames);
  check(input==original);
  const auto& r=records[0];check(num(r,8)==1 && num(r,9)==position);
  if(position+input.size()>30716 && position<30716){check(num(r,11)==1 && num(r,12)==30728);}
  if(position+input.size()>61416){check(num(r,11)==2 && num(r,12)==28);}
}
void observer() {
  AE::CTrueHDMajorTrace t;t.Reset(0);auto f=major();t.Begin(f.data(),f.size());check(!t.Append(28,f.size()));
  for(int ext=-1;ext<=15;++ext)for(unsigned salt=0;salt<32;++salt){
    auto good=major(ext,salt);t.Reset(71);t.Begin(good.data(),good.size());
    check(t.StructureValid() && t.CRCValid());
    for(unsigned i:{12u,20u,28u}){auto bad=good;bad[i]^=8;t.Begin(bad.data(),bad.size());check(!t.CRCValid());}
    auto shortAu=good;shortAu[1]--;t.Begin(shortAu.data(),shortAu.size());check(!t.StructureValid());
    good[8]=0xf0;checksum(good);t.Begin(good.data(),good.size());check(!t.StructureValid());
  }
  for(unsigned n=0;n<64;++n){t.Reset(71);t.Begin(f.data(),n);check(!t.CRCValid());}
  t.Reset(71);for(unsigned n=1;n<256;++n)t.Flush();
  t.Begin(f.data(),f.size());check(!t.Append(61411,5));t.Flush();check(t.Append(28,f.size()-5));
  check(t.Complete() && t.Spans()[0].packet==256 && t.Spans()[1].packet==257);
  t.Begin(f.data(),f.size());check(!t.Append(28,f.size()));
  for(unsigned n=0;n<1000;++n){t.Flush();check(t.Dequeue()<=257);}check(t.Packet()==257);
  t.Reset(72);t.Begin(f.data(),f.size());check(t.Append(28,f.size()) && t.Spans()[0].packet==1);
  t.Reset(73);t.Begin(f.data(),f.size());t.Append(28,4);t.Reset(74);check(!t.Append(28,f.size()-4));
}
void longStream() {
  CPackerMAT p;p.SetLavStyleEnabled(true);p.SetRestartTrace(71);CLog::records.clear();
  std::vector<Bytes> frames,source;
  for(unsigned n=0;n<7000;++n){
    Bytes au=n%120==0?major(int(n%16),n):Bytes(64);
    au[0]=(au.size()>>9)&15;au[1]=au.size()/2;au[2]=(n*40)>>8;au[3]=n*40;
    if(n%120==0)checksum(au);
    // A complete, CRC-valid fake header inside a minor AU must not be observed.
    if(n==7){au.resize(512);au[0]=1;au[1]=0;auto fake=major();std::copy(fake.begin(),fake.end(),au.begin()+24);}
    source.push_back(au);p.PackTrueHD(au.data(),au.size());
    for(;;){auto f=p.GetOutputFrame();if(f.empty())break;frames.push_back(std::move(f));}
  }
  check(frames.size()>256);
  const auto records=majors();check(records.size()>40);
  for(const auto&r:records){auto au=num(r,1);check((au-1)%120==0);compare(r,source.at(au-1),p,frames);check(num(r,8)<=256);}
  unsigned dequeued=0;for(const auto&r:CLog::records)if(r.message.find("stage=mat_dequeue ")!=std::string::npos)check(num(r,1)==++dequeued);
  check(dequeued==256);
  std::cout<<records.size()<<" genuine major AUs mapped; embedded CRC-valid header ignored; "<<frames.size()<<" MAT outputs\n";
}
void recovery(bool lav) {
  CPackerMAT p;p.SetLavStyleEnabled(lav);p.SetRestartTrace(71);CLog::records.clear();
  auto f=major();p.PackTrueHD(f.data(),f.size());check(majors().size()==1);
  f[2]=0x70;f[3]=0;check(!p.PackTrueHD(f.data(),f.size()));
  check(p.m_restartTrace.Epoch()==0);
  unsigned gaps=0;for(const auto&r:CLog::records)if(r.message.find("stage=major_gap ")!=std::string::npos){++gaps;check(num(r,0)==71 && num(r,1)==1);}
  check(gaps==1);f[2]=f[3]=0;p.PackTrueHD(f.data(),f.size());check(majors().size()==1);
}
int main(int argc,char**argv) {
  assert(argc==2);check(av_crc_init(table,0,16,0x2d,sizeof(table))==0);
  observer();unsigned splits=0;
  for(int ext=-1;ext<=15;++ext){
    const unsigned header=32+(ext<0?0:2+2*ext);
    positioned(28,ext);positioned(100,ext);positioned(40000,ext);
    for(unsigned cut=1;cut<header;++cut){positioned(30716-cut,ext);positioned(61416-cut,ext);splits+=2;}
  }
  for(unsigned cut:{1u,63u,64u,65u,4095u,8189u}){
    positioned(30716-cut,15,8190);positioned(61416-cut,15,8190);
  }
  recovery(false);recovery(true);longStream();
  std::ofstream out(argv[1],std::ios::binary);out.put('1');
  std::cout<<checks<<" assertions; "<<splits<<" header split positions; real libavutil CRC oracle PASS\n";
}
'''


def run(directory, sanitize, mutation=None):
    directory.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("restart_fixture", ROOT / "tools/test-truehd-restart.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    for name in ["PackerMAT.h", "PackerMAT.cpp", "TrueHDRestartTrace.h"]:
        text = (UTILS / name).read_text()
        if mutation == "missing-middle" and name == "PackerMAT.cpp":
            old = "      TraceAudioData(nBytesBefore);"
            assert text.count(old) == 2
            text = text.replace(old, "      (void)nBytesBefore;", 1)
        if mutation == "accept-bad-crc" and name == "TrueHDRestartTrace.h":
            old = "m_crcValid = crc == ((data[majorBytes + 3] << 8) | data[majorBytes + 2]);"
            assert old in text
            text = text.replace(old, "m_crcValid = true; (void)crc;", 1)
        if mutation == "keep-stale-mapping" and name == "PackerMAT.cpp":
            assert text.count("      StopRestartTrace();") == 2
            text = text.replace("      StopRestartTrace();", "", 1)
        (directory / name).write_text(text)
    (directory / "utils").mkdir(exist_ok=True)
    (directory / "utils/log.h").write_text(r'''
#pragma once
#include <cstring>
#include <sstream>
#include <string>
#include <vector>
constexpr int LOGDEBUG=0;
struct LogRecord{std::string message;std::vector<std::string>args;};
struct CLog{inline static std::vector<LogRecord>records;template<class T>static std::string text(T value){std::ostringstream out;out<<value;return out.str();}template<class...T>static void Log(int,const char*m,T...a){records.push_back({m,{text(a)...}});}};
''')
    (directory / "libavutil").mkdir(exist_ok=True)
    (directory / "libavutil/common.h").write_text("#define FFALIGN(x,a) (((x)+(a)-1)&~((a)-1))\n")
    (directory / "libavutil/intreadwrite.h").write_text("#define AV_RB16(p) (((unsigned)(p)[0]<<8)|(p)[1])\n#define AV_RB32(p) (((unsigned)AV_RB16(p)<<16)|AV_RB16((p)+2))\n")
    library = ctypes.util.find_library("avutil")
    if not library:
        raise RuntimeError("Host libavutil required for independent CRC oracle")
    data, stdout = helper.execute(directory, "major-coverage", HARNESS,
                                  [directory / "PackerMAT.cpp", "-l:" + library], sanitize)
    assert data == b"1"
    return stdout


def verify(directory, sanitize):
    print(run(directory / "actual", sanitize), end="", flush=True)
    rejected = []
    for mutation in ["missing-middle", "accept-bad-crc", "keep-stale-mapping"]:
        try:
            run(directory / mutation, sanitize, mutation)
        except RuntimeError as error:
            assert "Assertion" in str(error) and "AddressSanitizer" not in str(error) and "runtime error:" not in str(error), str(error)
            rejected.append(mutation)
            print("Rejected " + mutation, flush=True)
        else:
            raise AssertionError(mutation + " unexpectedly passed")
    (directory / "receipt.json").write_text(json.dumps({"sanitize": sanitize, "negative_controls": rejected,
        "limits": ["Synthetic AUs, no lossless decoding", "Host logger and forced initial packer position", "No physical HDMI/TV or image inclusion evidence"]}, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True)
        verify(args.output, args.sanitize)
    else:
        with tempfile.TemporaryDirectory(prefix="truehd-major-") as temporary:
            verify(Path(temporary), args.sanitize)


if __name__ == "__main__":
    main()
