#!/usr/bin/env python3
"""Compile and exercise the production BD-menu seam consumer method.

The surrounding demux/device is stubbed; this checks state and error paths,
not Kodi scheduling, decoding or HDMI output. Requires g++ and sanitizers.
Temporary build outputs are automatically removed.
"""
from pathlib import Path
import tempfile
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT/'xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp').read_text()
method = source[source.index('void CDVDDemuxFFmpeg::ApplySeamTimeOffset'):source.index('double CDVDDemuxFFmpeg::ConvertTimestamp')]
header = r'''
#include <cassert>
#include <cmath>
#include <cstdint>
#include <map>
#include <memory>
#include "xbmc/cores/VideoPlayer/Interface/TimingConstants.h"
struct DemuxPacket { double pts; double dts; };
struct CDVDInputStream {
  virtual ~CDVDInputStream() = default;
  struct IMenus { virtual ~IMenus() = default; virtual bool GetSeamTimeOffsets(int&,double&,double&) = 0; };
};
struct Menu : CDVDInputStream, CDVDInputStream::IMenus {
  int generation=0; double current=0, previous=0; bool available=true;
  bool GetSeamTimeOffsets(int& g,double& c,double& p) override {
    g=generation; c=current; p=previous; return available;
  }
};
struct CDVDDemuxFFmpeg {
  void* m_pSSIF = nullptr;
  std::shared_ptr<CDVDInputStream> m_pInput;
  struct SeamStreamState { int generation=0; double lastCorrected=DVD_NOPTS_VALUE; };
  std::map<int,SeamStreamState> m_seamStreamState;
  void ApplySeamTimeOffset(DemuxPacket*,int);
};
'''
test = r'''
int main() {
  CDVDDemuxFFmpeg d;
  auto menu=std::make_shared<Menu>(); d.m_pInput=menu;
  auto packet=[&](int id,double raw,double expected) {
    DemuxPacket p{raw*DVD_TIME_BASE+40000,raw*DVD_TIME_BASE};
    d.ApplySeamTimeOffset(&p,id);
    assert(std::fabs(p.dts-expected*DVD_TIME_BASE)<0.01);
    assert(std::fabs(p.pts-p.dts-40000)<0.01);
  };
  packet(0,999,999); packet(1,999,999);
  for(int generation=1;generation<=3;++generation) {
    menu->generation=generation; menu->current=generation*1000; menu->previous=(generation-1)*1000;
    packet(0,999.5,generation*1000-0.5); // buffered old clip video
    packet(0,0,generation*1000);       // first new clip video
    packet(1,999.8,generation*1000-0.2); // audio crosses independently
    packet(1,0,generation*1000);
    packet(0,999,generation*1000+999); packet(1,999,generation*1000+999);
  }
  DemuxPacket absent{DVD_NOPTS_VALUE,DVD_NOPTS_VALUE};
  d.ApplySeamTimeOffset(&absent,2);
  assert(absent.pts==DVD_NOPTS_VALUE && absent.dts==DVD_NOPTS_VALUE);
  assert(d.m_seamStreamState.count(2)==0);
  DemuxPacket ptsOnly{2*DVD_TIME_BASE,DVD_NOPTS_VALUE};
  d.ApplySeamTimeOffset(&ptsOnly,2);
  assert(ptsOnly.pts==3002.0*DVD_TIME_BASE && ptsOnly.dts==DVD_NOPTS_VALUE);
  menu->generation=6; menu->current=6000; menu->previous=5000;
  packet(0,0,6000); // skipped generations cannot use obsolete per-stream history
  d.m_seamStreamState.clear();
  packet(0,25,6025); // reopened/flushed stream adopts current offset
  menu->generation=7; menu->current=menu->previous=0;
  packet(0,50,50); // explicit seek reset
  menu->current=999;
  d.m_pSSIF=&d; packet(0,1,1); d.m_pSSIF=nullptr; // MVC unchanged
  menu->available=false; packet(0,2,2); // provider rejects correction
  d.m_pInput=std::make_shared<CDVDInputStream>(); packet(0,3,3); // ordinary input unchanged
}
'''
with tempfile.TemporaryDirectory(prefix="bd-seam-") as tmp:
    out=Path(tmp)/"test.cpp"
    out.write_text(header+method+test)
    subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    '-I',str(ROOT),str(out),'-o',str(out.with_suffix(''))],check=True)
    subprocess.run([str(out.with_suffix(''))],check=True)
    print("PASS: repeated seams, staggered audio/video, seeks and non-menu/MVC bypass")
