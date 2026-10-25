#!/usr/bin/env python3
"""Run the complete production multi-source scheduler with host boundary doubles.

The production header and cpp are compiled with only includes/access control
adapted. Input, demux, factory, logging and packet allocation are substitutes;
native FFmpeg, VFS, player threads and device playback are not executed.
--baseline REV exercises the same semantic cases on that revision.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = 'xbmc/cores/VideoPlayer/DVDDemuxers/'

STUBS = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <queue>
#include <sstream>
#include <string>
#include <utility>
#include <vector>
#include "cores/VideoPlayer/Interface/DemuxPacket.h"
enum class StreamFlags {FLAG_NONE};
constexpr int STREAM_VIDEO=1, STREAM_AUDIO=2, LOGDEBUG=0;
struct CDemuxStream { int type=0; StreamFlags flags=StreamFlags::FLAG_NONE; std::string language; };
struct CDVDDemux {
  int64_t id=0;
  virtual ~CDVDDemux()=default;
  virtual void Abort() {} virtual void Flush() {}
  virtual void EnableStream(int64_t,int,bool) {}
  virtual bool Reset() {return true;}
  virtual bool SeekTime(double,bool,double*) {return true;}
  virtual DemuxPacket* Read() {return nullptr;}
  virtual int GetNrOfStreams() const {return 0;}
  virtual CDemuxStream* GetStream(int64_t,int) const {return nullptr;}
  virtual std::vector<CDemuxStream*> GetStreams() const {return {};}
  virtual std::string GetStreamCodecName(int64_t,int) {return "";}
  virtual int GetStreamLength() {return 0;}
  int64_t GetDemuxerId() const {return id;}
  std::string GetFileName() const {return "fixture";}
};
struct CDVDInputStream {
  bool eof=false; CDVDDemux* factory=nullptr;
  virtual ~CDVDInputStream()=default;
  bool IsEOF() const {return eof;}
  std::string GetFileName() const {return "fixture";}
};
using InputStreamPtr=std::shared_ptr<CDVDInputStream>;
struct InputStreamMultiStreams: CDVDInputStream {std::vector<InputStreamPtr> m_InputStreams;};
struct CDVDFactoryDemuxer {static CDVDDemux* CreateDemuxer(const InputStreamPtr& input) {
  return std::exchange(input->factory,nullptr);
}};
struct ExternalStreamInfo {int flag=0;std::string language;};
struct CUtil {static ExternalStreamInfo GetExternalStreamDetailsFromFilename(
  const std::string&,const std::string&) {return {};}};
struct CURL {static std::string GetRedacted(const std::string& s) {return s;}};
struct CLog {
  inline static bool enabled=false;
  inline static std::vector<std::string> lines;
  static CLog& GetInstance() {static CLog instance;return instance;}
  static bool IsLogLevelLogged(int) {return enabled;}
  template<class... T> static void Log(int,const char* format,T&&... args) {
    if(!enabled)return;
    std::ostringstream line;line<<format;
    ((line<<' '<<args),...);lines.push_back(line.str());
  }
};
struct CServiceBroker {static CLog& GetLogging() {return CLog::GetInstance();}};
struct FakeDemux: CDVDDemux {
  int calls=0; bool resetOk=true,seekOk=true; double seekPts=DVD_NOPTS_VALUE;
  bool sawBackwards=false; double sawTime=0;
  std::function<DemuxPacket*(int)> next;
  DemuxPacket* Read() override {return next(++calls);}
  bool Reset() override {return resetOk;}
  bool SeekTime(double time,bool backwards,double* pts) override {
    sawTime=time;sawBackwards=backwards;if(pts)*pts=seekPts;return seekOk;
  }
};
static DemuxPacket* packet(double dts=DVD_NOPTS_VALUE,double pts=DVD_NOPTS_VALUE,
                           int size=17,int stream=0) {
  auto* p=new DemuxPacket;p->dts=dts;p->pts=pts;p->iSize=size;p->iStreamId=stream;return p;
}
'''

TESTS = r'''
static std::shared_ptr<FakeDemux> add(CDemuxMultiSource& m,int id,double priority,
                                    std::function<DemuxPacket*(int)> next) {
  auto d=std::make_shared<FakeDemux>();d->id=id;d->next=std::move(next);
  m.m_demuxerMap[id]=d;m.m_DemuxerToInputStreamMap[d]=std::make_shared<CDVDInputStream>();
  m.m_demuxerQueue.emplace(priority,d);return d;
}
static void read(CDemuxMultiSource& m,int n) {while(n--)delete m.Read();}
static double nan() {return std::numeric_limits<double>::quiet_NaN();}
static double inf() {return std::numeric_limits<double>::infinity();}
static void recovery() {
  for(double invalid:{static_cast<double>(DVD_NOPTS_VALUE),nan(),inf(),-inf()}) {
    CDemuxMultiSource m;
    auto v=add(m,1,0,[=](int n){return n==1?packet(invalid,invalid,0,-1):packet(909000000+n*41708.0);});
    auto a=add(m,2,1,[](int n){return packet(909000000+n*32000.0);});
    read(m,1001);assert(v->calls>400&&a->calls>400);
  }
}
static void ordering() {
  // DTS preference, PTS fallback (including nonfinite DTS), negative timestamps.
  for(double dts:{static_cast<double>(DVD_NOPTS_VALUE),nan(),inf(),-inf()}) {
    CDemuxMultiSource m;
    auto a=add(m,1,-2,[=](int){return packet(dts,-1);});
    auto b=add(m,2,0,[](int){return packet(0);});
    read(m,5);assert(a->calls==5&&b->calls==0);
  }
  CDemuxMultiSource m;
  auto a=add(m,1,-1,[](int){return packet(10,-100);});
  auto b=add(m,2,0,[](int n){return packet(n);});
  read(m,5);assert(a->calls==1&&b->calls==4);
}
static void retry_return() {
  CDemuxMultiSource m;
  auto a=add(m,1,-1,[](int n){return n==1?packet():packet(1000);});
  auto b=add(m,2,0,[](int n){return packet(n);});
  read(m,2);assert(a->calls==2);
  read(m,20);assert(a->calls==2&&b->calls==20);
}
static void timestamped() {
  CDemuxMultiSource m;
  auto v=add(m,1,0,[](int n){return packet(909000000+(n-1)*41708.0);});
  auto a=add(m,2,1,[](int n){return packet(909000000+(n-1)*32000.0);});
  read(m,1001);assert(v->calls==435&&a->calls==566);
}
static void persistent() {
  // Two untimed peers must not shield a later timestamped third source.
  for(bool null:{false,true}) {
    CDemuxMultiSource m;
    auto a=add(m,1,-2,[=](int){return null?nullptr:packet();});
    auto b=add(m,2,-1,[=](int){return null?nullptr:packet();});
    auto c=add(m,3,1000,[](int n){return packet(1000+n);});
    int lastA=0,lastB=0,lastC=0;
    for(int i=0;i<1000;++i) {
      const int ac=a->calls,bc=b->calls,cc=c->calls;read(m,1);
      if(a->calls>ac)lastA=i;
      if(b->calls>bc)lastB=i;
      if(c->calls>cc)lastC=i;
      if(i>20)assert(i-lastA<=4&&i-lastB<=4&&i-lastC<=2);
    }
    assert(a->calls>=200&&b->calls>=200&&c->calls>=400);
  }
}
static void all_retry() {
  CDemuxMultiSource m;
  std::vector<std::shared_ptr<FakeDemux>> sources;
  for(int id=0;id<7;++id)sources.push_back(add(m,id,id,[](int){return packet();}));
  read(m,1000);
  for(const auto& d:sources)assert(d->calls>=135&&d->calls<=150);
}
static void solo() {
  CDemuxMultiSource m;
  auto a=add(m,1,0,[](int n){return n%2?nullptr:packet();});
  read(m,100);assert(a->calls==100);
}
static void payloads() {
  for(int stream:{-1,DMX_SPECIALID_STREAMCHANGE,0,5})for(int size:{0,17}) {
    CDemuxMultiSource m;DemuxPacket* original=nullptr;
    add(m,1,-1,[&](int){original=packet(DVD_NOPTS_VALUE,DVD_NOPTS_VALUE,size,stream);return original;});
    auto b=add(m,2,0,[](int n){return packet(n);});
    auto* result=m.Read();assert(result==original&&result->iSize==size&&result->iStreamId==stream);
    assert(result->dts==DVD_NOPTS_VALUE&&result->pts==DVD_NOPTS_VALUE);delete result;
    read(m,20);assert(b->calls>=9);
  }
  // A legitimate zero-size media/control packet with timestamps remains timed.
  for(int stream:{0,DMX_SPECIALID_STREAMCHANGE}) {
    CDemuxMultiSource m;
    auto a=add(m,1,-1,[=](int){return packet(50,50,0,stream);});
    auto b=add(m,2,0,[](int n){return packet(n);});
    read(m,10);assert(a->calls==1&&b->calls==9);
  }
}
static void eof() {
  CDemuxMultiSource m;
  auto a=add(m,1,-1,[](int){return nullptr;});
  auto b=add(m,2,0,[](int){return nullptr;});
  m.m_DemuxerToInputStreamMap[a]->eof=true;
  read(m,20);assert(a->calls==1&&b->calls==19);
  m.m_DemuxerToInputStreamMap[b]->eof=true;
  read(m,20);assert(a->calls==1&&b->calls==20);
  // Packet return still takes precedence over an input EOF hint.
  CDemuxMultiSource n;
  auto c=add(n,1,0,[](int){return packet();});
  n.m_DemuxerToInputStreamMap[c]->eof=true;read(n,10);assert(c->calls==10);
}
static void seek() {
  CDemuxMultiSource m;
  auto a=add(m,1,-1,[](int){return packet();});
  auto b=add(m,2,0,[](int n){return packet(n);});
  read(m,5);a->seekPts=nan();b->seekPts=0;
  assert(m.SeekTime(500,true));
  int ac=a->calls,bc=b->calls;read(m,20);
  assert(a->calls>ac&&b->calls>bc&&a->sawTime==500);
  // Seek removes failed sources and revives successfully seeked EOF sources.
  a->seekOk=false;b->seekPts=inf();double start=99;
  assert(m.SeekTime(600,false,&start));assert(std::isinf(start));
  ac=a->calls;bc=b->calls;read(m,20);assert(a->calls==ac&&b->calls==bc+20);
  b->seekOk=false;assert(!m.SeekTime(700,false,&start));
  bc=b->calls;read(m,5);assert(b->calls==bc);
}
static void reset() {
  CDemuxMultiSource m;
  auto a=add(m,1,-1,[](int){return nullptr;});
  auto b=add(m,2,0,[](int){return packet();});
  m.m_DemuxerToInputStreamMap[a]->eof=true;read(m,5);
  m.m_DemuxerToInputStreamMap[a]->eof=false;
  assert(m.Reset());int ac=a->calls,bc=b->calls;read(m,20);
  assert(a->calls>=ac+9&&b->calls>=bc+9);
  a->resetOk=false;assert(!m.Reset());ac=a->calls;bc=b->calls;
  read(m,20);assert(a->calls==ac&&b->calls==bc+20);
}
static void lifecycle() {
  CDemuxMultiSource m;
  auto input=std::make_shared<InputStreamMultiStreams>();
  auto child=std::make_shared<CDVDInputStream>();auto* d=new FakeDemux;d->id=8;
  d->next=[](int){return packet();};child->factory=d;input->m_InputStreams.push_back(child);
  assert(m.Open(input));read(m,10);assert(d->calls==10);
  auto empty=std::make_shared<InputStreamMultiStreams>();
  assert(!m.Open(empty));assert(m.Read()==nullptr);
}
static void diagnostics() {
  CLog::enabled=false;CLog::lines.clear();
  CDemuxMultiSource m;
  auto a=add(m,1,0,[](int){return packet();});read(m,100);
  assert(CLog::lines.empty());
  CLog::enabled=true;read(m,10000);
  // Immediate anomaly + periodic source snapshots, logarithmically bounded retries.
  assert(!CLog::lines.empty()&&CLog::lines.size()<35);
  assert(a->calls==10100);
  CLog::enabled=false;
}
int main(int argc,char** argv) {
  assert(argc==2);const std::string test=argv[1];
  if(test=="recovery")recovery();else if(test=="ordering")ordering();
  else if(test=="retry-return")retry_return();else if(test=="timestamped")timestamped();
  else if(test=="persistent")persistent();else if(test=="all-retry")all_retry();
  else if(test=="solo")solo();else if(test=="payloads")payloads();else if(test=="eof")eof();
  else if(test=="seek")seek();else if(test=="reset")reset();
  else if(test=="lifecycle")lifecycle();else if(test=="diagnostics")diagnostics();else assert(false);
  std::printf("PASS %s\n",argv[1]);
}
'''


def harness(baseline=None):
    def read(name):
        if baseline:
            return subprocess.check_output(
                ['git', 'show', f'{baseline}:{DIRECTORY}{name}'], cwd=ROOT, text=True)
        return (ROOT / DIRECTORY / name).read_text()
    header = read('DemuxMultiSource.h')
    source = read('DemuxMultiSource.cpp')
    # Keep complete method bodies and real comparator; only boundary declarations
    # and private access are adapted. Real DemuxPacket/TimingConstants are included.
    header = '\n'.join(line for line in header.splitlines()
                       if not line.startswith(('#include', '#pragma')))
    source = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
    header = header.replace('private:', 'public:').replace(' override', '')
    return STUBS + header + source + TESTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    cases = args.case or ['recovery', 'ordering', 'retry-return', 'timestamped', 'persistent', 'all-retry', 'solo',
                          'payloads', 'eof', 'seek', 'reset', 'lifecycle', 'diagnostics']
    with tempfile.TemporaryDirectory(prefix='p3i-demux-test-') as work:
        cpp = Path(work) / 'test.cpp'
        binary = Path(work) / 'test'
        cpp.write_text(harness(args.baseline))
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                        '-I' + str(ROOT / 'xbmc'), str(cpp), '-o', str(binary)], check=True)
        failed = []
        for case in cases:
            result = subprocess.run([str(binary), case],
                                    env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            if result.returncode:
                failed.append(case)
        if failed:
            raise SystemExit('FAILED: ' + ', '.join(failed))
    print(f'{len(cases)} production scheduler cases passed; boundary doubles, no native playback')


if __name__ == '__main__':
    main()
