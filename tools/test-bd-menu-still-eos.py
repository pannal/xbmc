#!/usr/bin/env python3
"""Compile and exercise the production BD-menu still eos method.

The surrounding demux/device is stubbed; this checks state and error paths,
not Kodi scheduling, decoding or HDMI output. Requires g++ and sanitizers.
Temporary build outputs are automatically removed.
"""
from pathlib import Path
import tempfile
import subprocess

ROOT = Path(__file__).resolve().parents[1]
root=ROOT
source=(root/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
header=(root/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
method=source[source.index('bool CAMLCodec::DrainHevcStill('):source.index('CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(')]
state=header[header.index('  struct StillFrameDrain'):header.index('  // Green-flash mask state')]
preamble=r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <fcntl.h>
#include <unistd.h>
#include <utility>
#include <vector>
constexpr int AV_CODEC_ID_HEVC=1;
constexpr int LOGWARNING=1;
struct CLog { template<typename... A> static void Log(A... ) {} };
struct Codec { int handle; };
struct FakeDll {
  std::deque<std::pair<int,int>> results;
  std::vector<uint8_t> bytes;
  unsigned int calls=0;
  int codec_write(Codec* codec, void* data, int size) {
    assert(fcntl(codec->handle,F_GETFL)&O_NONBLOCK);
    ++calls;
    auto result=results.empty()?std::pair<int,int>{size,0}:results.front();
    if(!results.empty())results.pop_front();
    errno=result.second;
    if(result.first>0) {
      auto* b=static_cast<uint8_t*>(data);
      bytes.insert(bytes.end(),b,b+std::min(result.first,size));
    }
    return result.first;
  }
};
class CAMLCodec {
public:
  struct { bool stills=true; int codec=AV_CODEC_ID_HEVC; } m_hints;
  std::atomic_bool m_abort{false};
  struct Private {Codec vcodec;} privateStorage;
  Private* am_private=&privateStorage;
  FakeDll dllStorage;
  FakeDll* m_dll=&dllStorage;
  int fd[2];
  CAMLCodec() {assert(pipe(fd)==0);privateStorage.vcodec.handle=fd[1];}
  ~CAMLCodec(){close(fd[0]);close(fd[1]);}
  bool DrainHevcStill(float);
'''
tests=r'''
int main() {
  using State=CAMLCodec::StillFrameDrain::State;
  { CAMLCodec c;c.m_hints.stills=false;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==0); }
  { CAMLCodec c;c.m_hints.codec=2;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==0); }
  { CAMLCodec c;c.m_stillFrameDrain.pictureEmitted=true;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==0); }
  { CAMLCodec c;assert(!c.DrainHevcStill(1));assert(c.m_dll->calls==0); }
  { CAMLCodec c;c.m_abort=true;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==0); }
  { CAMLCodec c;c.m_dll->results={{2,0},{0,0},{-1,EAGAIN},{-1,EINTR},{4,0}};
    for(int i=0;i<5;++i){assert(c.DrainHevcStill(0));assert(!(fcntl(c.fd[1],F_GETFL)&O_NONBLOCK));}
    assert(c.m_dll->bytes==std::vector<uint8_t>({0,0,0,1,0x48,1}));
    assert(c.m_stillFrameDrain.state==State::WAITING);assert(c.DrainHevcStill(0));assert(c.m_dll->calls==5);
    c.m_stillFrameDrain.deadline=std::chrono::steady_clock::now();
    assert(!c.DrainHevcStill(0));assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==5);
  }
  for(auto result:{std::pair<int,int>{0,0},{-1,EAGAIN},{-1,EINTR}}) {
    CAMLCodec c;for(int i=0;i<32;++i)c.m_dll->results.push_back(result);
    for(int i=0;i<32;++i)assert(c.DrainHevcStill(0));
    assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==32);assert(!c.DrainHevcStill(0));
  }
  { CAMLCodec c;c.m_dll->results={{0,0}};assert(c.DrainHevcStill(0));
    c.m_stillFrameDrain.deadline=std::chrono::steady_clock::now();
    assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==1);
  }
  { CAMLCodec c;c.m_dll->results={{-1,EIO}};assert(!c.DrainHevcStill(0));assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==1); }
  { CAMLCodec c;c.m_dll->results={{2,0}};assert(c.DrainHevcStill(0));c.m_abort=true;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==1); }
  { CAMLCodec c;c.am_private->vcodec.handle=-1;assert(!c.DrainHevcStill(0));assert(c.m_dll->calls==0); }
  { CAMLCodec c;assert(fcntl(c.fd[1],F_SETFL,O_NONBLOCK)==0);assert(c.DrainHevcStill(0));assert(fcntl(c.fd[1],F_GETFL)&O_NONBLOCK); }
  std::puts("PASS: 14 HEVC still EOS scenarios (actual extracted production method)");
}
'''
with tempfile.TemporaryDirectory(prefix="bd-still-eos-") as tmp:
    out=Path(tmp)/"test.cpp"
    out.write_text(preamble+state+'};\n'+method+tests)
    subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    str(out),'-o',str(out.with_suffix(''))],check=True)
    subprocess.run([str(out.with_suffix(''))],check=True)
