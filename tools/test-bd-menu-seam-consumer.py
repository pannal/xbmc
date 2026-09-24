#!/usr/bin/env python3
"""Exercise production seam read-tagging/correction with ASan and UBSan.

Stubs provide native offsets, AVIO positions and TS/PES packet positions. They
model sparse selected PG display/clear packets and dense advancing A/V, not a
full demuxer or disc. --baseline REV --sparse-only reproduces the prior defect.
"""
from pathlib import Path
import argparse
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline')
parser.add_argument('--sparse-only', action='store_true')
args = parser.parse_args()
path = 'xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp'
source = (subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=ROOT, text=True)
          if args.baseline else (ROOT / path).read_text())
start = ('void CDVDDemuxFFmpeg::RecordSeamRead' if 'void CDVDDemuxFFmpeg::RecordSeamRead' in source
         else 'void CDVDDemuxFFmpeg::ApplySeamTimeOffset')
methods = source[source.index(start):source.index('double CDVDDemuxFFmpeg::ConvertTimestamp')]
methods += source[source.index('static int dvd_file_read('):source.index('void CDVDDemuxFFmpeg::MarkBroken()')]
if 'void CDVDDemuxFFmpeg::RecordSeamRead' not in methods:
    methods += '\nvoid CDVDDemuxFFmpeg::RecordSeamRead(int) {}\n'
header = r'''
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <map>
#include <memory>
#include "xbmc/cores/VideoPlayer/Interface/TimingConstants.h"
constexpr int LOGDEBUG=0;
struct CLog { template<typename... T> static void Log(T&&...) {} };
struct DemuxPacket { double pts; double dts; };
struct CDVDInputStream {
  virtual ~CDVDInputStream() = default;
  virtual int Read(uint8_t*, int size) { return size; }
  struct IMenus { virtual ~IMenus() = default;
    virtual bool GetSeamTimeOffsets(int&,double&,double&) = 0; };
};
struct Menu : CDVDInputStream, CDVDInputStream::IMenus {
  int generation=0; double current=0, previous=0; bool available=true;
  bool GetSeamTimeOffsets(int& g,double& c,double& p) override {
    g=generation; c=current; p=previous; return available;
  }
};
struct AVIOContext { int64_t pos=0; };
struct CDVDDemuxFFmpeg {
  void* m_pSSIF = nullptr;
  bool m_brokenFileDetected=false;
  int64_t m_sourceReadBytes=0;
  std::shared_ptr<CDVDInputStream> m_pInput;
  // Legacy state is included solely so --baseline compiles the old method.
  struct SeamStreamState { int generation=0; double lastCorrected=DVD_NOPTS_VALUE; };
  std::map<int,SeamStreamState> m_seamStreamState;
  std::map<int64_t,double> m_seamReadOffsets;
  int64_t m_seamReadEnd=0;
  AVIOContext io;
  AVIOContext* m_ioContext=&io;
  struct { struct { int64_t pos=-1; } pkt; } m_pkt;
  void RecordSeamRead(int);
  void ApplySeamTimeOffset(DemuxPacket*,int);
};
constexpr int AVERROR_EXIT=-1, AVERROR_EOF=-2;
static bool interrupt_cb(void*) {return false;}
'''
tests = r'''
struct Fixture {
  CDVDDemuxFFmpeg d;
  std::shared_ptr<Menu> menu=std::make_shared<Menu>();
  Fixture() {d.m_pInput=menu;}
  void read(int64_t start,int bytes,int generation,double offset) {
    menu->previous=menu->current;menu->generation=generation;menu->current=offset;
    d.io.pos=start;
    assert(dvd_file_read(&d,nullptr,bytes)==bytes);
    d.io.pos+=bytes;
  }
  void packet(int stream,int64_t pos,double raw,double expected,bool pg=false) {
    DemuxPacket p{raw*DVD_TIME_BASE,pg?DVD_NOPTS_VALUE:raw*DVD_TIME_BASE-40000};
    d.m_pkt.pkt.pos=pos;d.ApplySeamTimeOffset(&p,stream);
    if (std::fabs(p.pts-expected*DVD_TIME_BASE)>0.01) {
      std::fprintf(stderr,"stream %d raw %.2f: expected %.2f, got %.2f\n",
                   stream,raw,expected,p.pts/DVD_TIME_BASE);std::abort();
    }
    if(pg)assert(p.dts==DVD_NOPTS_VALUE);
    else assert(std::fabs(p.pts-p.dts-40000)<0.01);
  }
};
void sparse() {
  Fixture f;
  // Sparse selected PG display + clear; video/audio keep advancing near clip end.
  for(int loop=0;loop<4;++loop) {
    const int64_t base=loop*1920;
    f.read(base,1920,loop,loop);
    f.packet(0,base+4,0.0,loop);
    f.packet(2,base+196,0.1,loop+0.1,true); // displayset
    f.packet(2,base+388,0.2,loop+0.2,true); // clear displayset
    f.packet(0,base+1540,0.8,loop+0.8);
    f.packet(1,base+1732,0.9,loop+0.9);
  }
}
void remaining() {
  Fixture f;
  f.read(0,1920,0,0);
  f.packet(0,1540,0.8,0.8);f.packet(2,196,0.1,0.1,true);
  f.read(1920,1920,1,1);
  f.packet(0,1924,0,1); // new video emitted before old sparse PG
  f.packet(2,388,0.2,0.2,true);
  f.packet(1,1732,0.9,0.9); // audio crosses separately
  f.packet(1,2116,0.1,1.1);
  f.packet(2,2308,0.2,1.2,true);
  // Later read-ahead must not erase older ownership even across many seams.
  for(int loop=2;loop<12;++loop)f.read(loop*1920,1920,loop,loop);
  f.packet(2,580,0.3,0.3,true);
  f.packet(2,11*1920+196,0.1,11.1,true);
  // Repeated reads within a generation coalesce; no entry per packet/read.
  const auto size=f.d.m_seamReadOffsets.size();
  f.read(12*1920,1920,11,11);assert(f.d.m_seamReadOffsets.size()==size);
  // NOPTS remains NOPTS. Unknown ownership preserves data but not invented time.
  DemuxPacket absent{DVD_NOPTS_VALUE,DVD_NOPTS_VALUE};
  f.d.m_pkt.pkt.pos=196;f.d.ApplySeamTimeOffset(&absent,3);
  assert(absent.pts==DVD_NOPTS_VALUE && absent.dts==DVD_NOPTS_VALUE);
  for(int64_t pos : {-1LL,999999LL}) {
    DemuxPacket p{100,50};f.d.m_pkt.pkt.pos=pos;f.d.ApplySeamTimeOffset(&p,0);
    assert(p.pts==DVD_NOPTS_VALUE && p.dts==DVD_NOPTS_VALUE);
  }
  // Flush drops parser packets but AVIO's next read position need not be zero.
  f.d.m_seamReadOffsets.clear();f.d.m_seamReadEnd=0;
  f.read(50000,1920,12,0);f.packet(2,50196,0.1,0.1,true);
  f.packet(0,-1,0.1,0.1); // a unique read offset is unambiguous
  f.read(51920,1920,13,-2);f.packet(0,51924,4,2); // signed offsets
  // Ordinary and MVC inputs remain outside native seam correction.
  Fixture ordinary;ordinary.d.m_pInput=std::make_shared<CDVDInputStream>();
  ordinary.read(0,1920,1,99);ordinary.packet(0,4,1,1);
  Fixture mvc;mvc.d.m_pSSIF=&mvc;mvc.read(0,1920,1,99);mvc.packet(0,4,1,1);
  Fixture noProvider;noProvider.menu->available=false;
  noProvider.read(0,1920,1,99);noProvider.packet(0,4,1,1);
  // Absolute byte ownership on a backward read replaces superseded ranges.
  Fixture seek;seek.read(0,1920,0,0);seek.read(1920,1920,1,1);
  seek.read(1920,1920,2,0);seek.packet(2,2116,0.1,0.1,true);
}
int main(int argc,char**) {sparse();if(argc==1)remaining();
  std::puts("PASS: sparse PG display/clear, buffered old packets, byte epochs and bypass paths");}
'''
with tempfile.TemporaryDirectory(prefix='bd-seam-') as tmp:
    out = Path(tmp) / 'test.cpp'
    out.write_text(header + methods + tests)
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    '-I', str(ROOT), str(out), '-o', str(out.with_suffix(''))], check=True)
    subprocess.run([str(out.with_suffix(''))] + (['sparse'] if args.sparse_only else []), check=True)
