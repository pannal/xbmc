#!/usr/bin/env python3
"""Compile the complete production TextureBase.cpp against explicit host stubs.

Copies TextureBase.cpp, TextureBase.h and TextureFormats.h unchanged, and extracts
the actual CRenderSystemBase::SupportsNPOT body. Only the service locator, render
capability/max-size interface, logging and aligned allocator are substituted.
This checks CPU allocation/padding/clamping; it does not run GL or measure a GUI.
The unsupported XB_FMT_DXT5_YCoCg mapping and odd-width RGB edge replication
are outside this regression; RGB odd-width allocation is covered.
"""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile


RENDER = r'''
#pragma once
#include <cstdint>
class CRenderSystemBase {
public:
  virtual ~CRenderSystemBase() = default;
  virtual bool SupportsNPOT(bool dxt) const;
  virtual uint32_t GetMaxTextureSize() const { return 4096; }
};
'''
SERVICE = r'''
#pragma once
#include "rendering/RenderSystem.h"
class CServiceBroker {
public:
  static CRenderSystemBase* GetRenderSystem();
};
'''
MEMORY = r'''
#pragma once
#include <cstddef>
namespace KODI { namespace MEMORY {
void* AlignedMalloc(size_t bytes, size_t alignment);
void AlignedFree(void* pointer);
} }
'''
LOG = r'''
#pragma once
constexpr int LOGERROR = 1;
class CLog {
public:
  template<typename... Args> static void Log(int, const char*, Args&&...) {}
};
'''

HARNESS = r'''
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <string>
#include <vector>
#include "TextureBase.h"
#include "ServiceBroker.h"
#include "utils/MemUtils.h"

namespace {
struct Allocation { unsigned char* raw; size_t bytes; };
std::map<void*, Allocation> allocations;
unsigned checks = 0, failures = 0, cases = 0;
constexpr size_t guard = 32;
void Check(bool condition, const std::string& description) {
  ++checks;
  if (!condition) { ++failures; std::cerr << "FAIL " << description << '\n'; }
}
void CheckGuards(void* p) {
  const auto& a = allocations.at(p);
  for (size_t i=0; i<guard; ++i)
    if (a.raw[i] != 0xa5 || a.raw[guard+a.bytes+i] != 0x5a) {
      std::cerr << "allocation guard overwritten\n"; std::abort();
    }
}
enum class Caps { BASE, ALL, NONE };
struct Renderer : CRenderSystemBase {
  Caps caps = Caps::BASE;
  uint32_t maximum = 4096;
  mutable std::vector<bool> compressedArguments;
  bool SupportsNPOT(bool dxt) const override {
    compressedArguments.push_back(dxt);
    if (caps == Caps::ALL) return true;
    if (caps == Caps::NONE) return false;
    return CRenderSystemBase::SupportsNPOT(dxt);
  }
  uint32_t GetMaxTextureSize() const override { return maximum; }
} renderer;
struct Texture : CTextureBase {
  using CTextureBase::GetPitch;
  using CTextureBase::GetRows;
  KD_TEX_FMT Format() const { return m_textureFormat; }
  ~Texture() { KODI::MEMORY::AlignedFree(m_pixels); }
};
struct Format { XB_FMT xb; const char* name; bool compressed; unsigned bytes; };
const Format formats[] = {
 {XB_FMT_A8R8G8B8,"BGRA",false,4}, {XB_FMT_RGBA8,"RGBA",false,4},
 {XB_FMT_RGB8,"RGB",false,3}, {XB_FMT_A8,"A8",false,1},
 {XB_FMT_DXT1,"DXT1",true,8}, {XB_FMT_DXT3,"DXT3",true,16},
 {XB_FMT_DXT5,"DXT5",true,16}
};
// Independent arithmetic oracle, deliberately not production PadPow2/GetPitch.
unsigned PowerTwo(unsigned n) { unsigned p=1; if (!n) return 0; while(p<n) p*=2; return p; }
unsigned Round(unsigned n,unsigned block) { return (n+block-1)/block*block; }
unsigned Pitch(unsigned w,const Format& f) {
  return f.compressed ? ((w+3)/4)*f.bytes : Round(w*f.bytes, f.bytes==3?4:1);
}
unsigned Rows(unsigned h,const Format& f) { return f.compressed ? (h+3)/4 : h; }
void AllocationCase(const Format& f,Caps caps,unsigned w,unsigned h,unsigned max) {
  ++cases;
  renderer.caps=caps;renderer.maximum=max;renderer.compressedArguments.clear();
  Texture t;t.Allocate(w,h,f.xb);
  const bool npot = caps==Caps::ALL || (caps==Caps::BASE && !f.compressed);
  unsigned tw=Round(npot?w:PowerTwo(w), f.compressed?4:16);
  unsigned th=f.compressed?Round(npot?h:PowerTwo(h),4):(npot?h:PowerTwo(h));
  tw=std::min(tw,max);th=std::min(th,max);
  const std::string tag=std::string(f.name)+" "+std::to_string(w)+"x"+std::to_string(h)+
      " caps="+std::to_string(static_cast<int>(caps))+" max="+std::to_string(max);
  Check(t.GetTextureWidth()==tw,tag+" texture width expected "+std::to_string(tw)+
        " got "+std::to_string(t.GetTextureWidth()));
  Check(t.GetTextureHeight()==th,tag+" texture height expected "+std::to_string(th)+
        " got "+std::to_string(t.GetTextureHeight()));
  Check(t.GetWidth()==std::min(w,tw) && t.GetHeight()==std::min(h,th),tag+" active dimensions");
  Check(t.GetOriginalWidth()==w && t.GetOriginalHeight()==h,tag+" original dimensions");
  Check(renderer.compressedArguments.size()==1 && renderer.compressedArguments[0]==f.compressed,
        tag+" SupportsNPOT compressed-argument contract");
  Check(t.GetPitch()==Pitch(tw,f) && t.GetRows()==Rows(th,f),tag+" staging pitch/rows");
  const size_t expectedBytes=static_cast<size_t>(Pitch(tw,f))*Rows(th,f);
  Check((t.GetPixels()!=nullptr)==(expectedBytes!=0),tag+" zero/nonzero allocation");
  if (t.GetPixels()) {
    Check(allocations.at(t.GetPixels()).bytes==expectedBytes,tag+" allocation size");
    Check(reinterpret_cast<uintptr_t>(t.GetPixels())%32==0,tag+" alignment");
    CheckGuards(t.GetPixels());
  }
}
void ClampCase(const Format& f,unsigned w,unsigned h) {
  ++cases;renderer.caps=Caps::BASE;renderer.maximum=4096;
  Texture t;t.Allocate(w,h,f.xb);
  const unsigned imageBlocks=f.compressed?(t.GetWidth()+3)/4:t.GetWidth();
  const unsigned imageRows=Rows(t.GetHeight(),f);
  const unsigned pitch=t.GetPitch(), rows=t.GetRows();
  std::memset(t.GetPixels(),0xcc,allocations.at(t.GetPixels()).bytes);
  for (unsigned y=0;y<imageRows;++y)
    for(unsigned x=0;x<imageBlocks;++x)
      for(unsigned b=0;b<f.bytes;++b)
        t.GetPixels()[y*pitch+x*f.bytes+b]=static_cast<unsigned char>((17*y+23*x+b)%251);
  t.ClampToEdge();
  bool all=true;
  for(unsigned y=0;y<rows;++y)
    for(unsigned x=0;x<pitch/f.bytes;++x)
      for(unsigned b=0;b<f.bytes;++b) {
        const auto expected=static_cast<unsigned char>((17*std::min(y,imageRows-1)+
                            23*std::min(x,imageBlocks-1)+b)%251);
        all &= t.GetPixels()[y*pitch+x*f.bytes+b]==expected;
      }
  Check(all,std::string(f.name)+" last pixel/block and last row replicated");
  CheckGuards(t.GetPixels());
}
}
namespace KODI { namespace MEMORY {
void* AlignedMalloc(size_t bytes,size_t alignment) {
  if(bytes>64*1024*1024) {std::cerr<<"test allocation unexpectedly large\n";std::abort();}
  void* raw=nullptr;
  if(posix_memalign(&raw,alignment,bytes+2*guard))return nullptr;
  auto* p=static_cast<unsigned char*>(raw);
  std::memset(p,0xa5,guard);std::memset(p+guard,0xcc,bytes);std::memset(p+guard+bytes,0x5a,guard);
  allocations[p+guard]={p,bytes};return p+guard;
}
void AlignedFree(void* pointer) {
  if(!pointer)return;
  CheckGuards(pointer);auto a=allocations.at(pointer);
  allocations.erase(pointer);std::free(a.raw);
}
} }
CRenderSystemBase* CServiceBroker::GetRenderSystem() { return &renderer; }

int main() {
  for(const auto& f:formats) {
    for(const auto caps:{Caps::BASE,Caps::ALL,Caps::NONE}) {
      for(const auto& dims:std::vector<std::pair<unsigned,unsigned>>{{1,1},{17,3},{35,19},{21,17}})
        AllocationCase(f,caps,dims.first,dims.second,4096);
      AllocationCase(f,caps,91,65,64);
      AllocationCase(f,caps,1000000,1000000,64);
      AllocationCase(f,caps,35,19,0);
      AllocationCase(f,caps,0,19,4096);
      AllocationCase(f,caps,35,0,4096);
    }
    // RGB row padding is a separate inherited edge case: use a 4-pixel-aligned
    // active width here; odd-width RGB allocations remain covered above.
    ClampCase(f,f.bytes==3&&!f.compressed?36:35,19);
  }
  AllocationCase(formats[0],Caps::BASE,1920,1080,4096);
  AllocationCase(formats[0],Caps::BASE,3326,1871,4096);
  // Reallocation must release the previous allocation and retain valid guards.
  { Texture t;renderer.caps=Caps::ALL;renderer.maximum=4096;
    t.Allocate(35,19,XB_FMT_RGBA8);t.Allocate(17,3,XB_FMT_RGBA8);
    Check(allocations.size()==1,"reallocation releases previous staging allocation"); }
  for(unsigned n:{0u,1u,2u,3u,15u,16u,17u,1023u,1024u,1025u})
    Check(CTextureBase::PadPow2(n)==PowerTwo(n),"PadPow2 boundary "+std::to_string(n));
  Check(allocations.empty(),"all staging allocations released by test owners");
  std::cout<<cases<<" allocation/clamp cases, "<<checks<<" checks, "<<failures<<" failures\n";
  std::cout<<"No GL/driver/device or performance validation; unsupported YCoCg is excluded.\n";
  return failures?1:0;
}
'''


def extract_supports_npot(source):
    signature = "bool CRenderSystemBase::SupportsNPOT(bool dxt) const"
    start = source.index(signature)
    brace = source.index("{", start)
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    with tempfile.TemporaryDirectory(prefix="test-texture-npot-") as directory:
        work = Path(directory)
        for target, source in {
            "TextureBase.cpp": "xbmc/guilib/TextureBase.cpp",
            "TextureBase.h": "xbmc/guilib/TextureBase.h",
            "guilib/TextureFormats.h": "xbmc/guilib/TextureFormats.h",
        }.items():
            dest = work / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / source, dest)
        for target, content in {
            "rendering/RenderSystem.h": RENDER,
            "ServiceBroker.h": SERVICE,
            "utils/MemUtils.h": MEMORY,
            "utils/log.h": LOG,
            "commons/ilog.h": "#pragma once\n",
        }.items():
            dest = work / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        (work / "SupportsNPOT.cpp").write_text(
            '#include "rendering/RenderSystem.h"\n'
            + extract_supports_npot((repo / "xbmc/rendering/RenderSystem.cpp").read_text())
            + "\n"
        )
        (work / "main.cpp").write_text(HARNESS)
        command = shlex.split(args.cxx) + [
            "-std=c++17", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
            "-I", str(work), str(work / "TextureBase.cpp"),
            str(work / "SupportsNPOT.cpp"), str(work / "main.cpp"),
            "-o", str(work / "test-texture-npot"),
        ]
        if args.sanitize:
            command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        subprocess.run(command, check=True)
        completed = subprocess.run([str(work / "test-texture-npot")], check=False)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
