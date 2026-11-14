#!/usr/bin/env python3
"""Which capture an AML screenshot's video takes: vdin1, amvideocap, or none.

Compiles the complete AMLVideoCapture.cpp against recording stand-ins for the
video sysfs state, /dev/vdin1 and its ioctls and mmap, and checks Capture()'s
result for each state. Only confirmed hidden video is NO_VIDEO; video state the
video layer does not report, a missing vdin1 or one without the frame memory
ioctl is UNAVAILABLE (amvideocap); a busy vdin1 or a failed vdin1 capture is
FAILED. ASan/UBSan; no real VDIN, decoding or timing.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def harness(directory):
    source = (ROOT / 'xbmc/utils/AMLVideoCapture.cpp').read_text()
    utils = (ROOT / 'xbmc/utils/AMLUtils.h').read_text()
    defines = '\n'.join(line for line in utils.splitlines()
                        if re.match(r'#define DOLBY_VISION_(OUTPUT_MODE|LL)_', line))
    for name in ['video/video_state', 'display/vinfo']:
        path = '"/sys/class/' + name + '"'
        assert path in source, path
        source = source.replace(path, '"' + str(directory / name.replace('/', '_')) + '"')
    source = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
    prelude = PRELUDE.replace('@DEFINES@', defines).replace('@DIRECTORY@', str(directory))
    return prelude + '\n#define open fakeOpen\n#define close fakeClose\n#define ioctl fakeIoctl\n' \
        '#define mmap fakeMmap\n#define munmap fakeMunmap\n' + source + '\n' + TESTS


PRELUDE = r'''
#include "utils/AMLVideoCapture.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <optional>
#include <string>
#include <thread>
#include <vector>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
@DEFINES@
constexpr int LOGDEBUG=0, LOGINFO=1, LOGWARNING=2;
struct CLog {template<class... T> static void Log(T&&...) {}};
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_HDR10,HDR_TYPE_HDR10PLUS,HDR_TYPE_DOLBYVISION,HDR_TYPE_HLG};
struct HDRStaticMetadataInfo {bool has_mdcv_metadata=false; uint32_t max_lum=0; bool has_cll_metadata=false; uint16_t max_cll=0;};
struct DataCache
{
  StreamHdrType GetVideoSourceHdrType() {return StreamHdrType::HDR_TYPE_NONE;}
  HDRStaticMetadataInfo GetVideoHDRStaticMetadataInfo() {return {};}
} dataCache;
struct CServiceBroker {static DataCache& GetDataCacheCore() {return dataCache;}};
std::map<std::string,std::string> sysfs;
class CSysfsPath
{
public:
  explicit CSysfsPath(const std::string& path) : m_path(path) {}
  bool Exists() {return sysfs.count(m_path)!=0;}
  template<typename T> std::optional<T> Get();
private:
  std::string m_path;
};
template<> std::optional<std::string> CSysfsPath::Get() {return Exists() ? std::optional<std::string>(sysfs[m_path]) : std::nullopt;}
template<> std::optional<unsigned int> CSysfsPath::Get() {return Exists() ? std::optional<unsigned int>(std::stoul(sysfs[m_path])) : std::nullopt;}
void writeFile(const std::string& name,const char* text)
{
  const std::string path=std::string("@DIRECTORY@/")+name;
  if(text) std::ofstream(path)<<text; else std::remove(path.c_str());
}
constexpr int FD=9;
constexpr uint64_t MEM_START=0x10000000;
struct Vdin
{
  int openErrno=0, memInfoErrno=0, startErrno=0, frames=2;
  bool started=false;
  int opens=0, closes=0, memInfos=0, starts=0, stops=0, gets=0, puts=0, maps=0, unmaps=0;
  std::vector<uint8_t> memory=std::vector<uint8_t>(4096);
} vdin;
int fakeOpen(const char* path,int flags)
{
  assert(std::strcmp(path,"/dev/vdin1")==0 && (flags&O_RDWR));
  ++vdin.opens;
  if(vdin.openErrno) {errno=vdin.openErrno; return -1;}
  return FD;
}
int fakeClose(int fd) {assert(fd==FD); ++vdin.closes; return 0;}
int fakeIoctl(int fd,unsigned long request,...);
void* fakeMmap(void* address,size_t length,int protection,int flags,int fd,off_t offset)
{
  assert(!address && protection==PROT_READ && flags==MAP_SHARED && fd==FD && vdin.started);
  assert(offset>=0 && static_cast<size_t>(offset)+length<=vdin.memory.size());
  ++vdin.maps; return vdin.memory.data()+offset;
}
int fakeMunmap(void*,size_t) {++vdin.unmaps; return 0;}
'''

TESTS = r'''
int fakeIoctl(int fd,unsigned long request,...)
{
  using namespace AML_VIDEO_CAPTURE;
  assert(fd==FD);
  va_list list; va_start(list,request);
  void* argument=request==TVIN_IOC_S_VDIN_V4L2STOP ? nullptr : va_arg(list,void*);
  va_end(list);
  if(request==TVIN_IOC_G_MEM_INFO)
  {
    ++vdin.memInfos;
    if(vdin.memInfoErrno) {errno=vdin.memInfoErrno; return -1;}
    // Allocated when a session starts.
    auto* info=static_cast<VdinMemInfo*>(argument);
    info->start=vdin.started ? MEM_START : 0; info->size=vdin.started ? vdin.memory.size() : 0;
    return 0;
  }
  if(request==TVIN_IOC_S_VDIN_V4L2START)
  {
    ++vdin.starts; assert(!vdin.started);
    const auto* param=static_cast<const VdinV4l2Param*>(argument);
    assert(param->width==4 && param->height==2 && param->mode==1);
    assert(param->bitOrder==1); // plain memory order, not the VPP's byte-reversed words
    if(vdin.startErrno) {errno=vdin.startErrno; return -1;}
    vdin.started=true; return 0;
  }
  if(request==TVIN_IOC_S_VDIN_V4L2STOP) {++vdin.stops; assert(vdin.started); vdin.started=false; return 0;}
  if(request==TVIN_IOC_G_BUF)
  {
    assert(vdin.started);
    if(vdin.frames==0) {errno=EAGAIN; return -1;}
    --vdin.frames; ++vdin.gets;
    auto* buffer=static_cast<VdinBuffer*>(argument);
    *buffer={}; buffer->width=4; buffer->height=2; buffer->stride=4; buffer->bufferFormat=1;
    buffer->physY=MEM_START+64; buffer->physU=MEM_START+128;
    return 0;
  }
  assert(request==TVIN_IOC_P_BUF); ++vdin.puts; return 0;
}
using AML_VIDEO_CAPTURE::Result;
void state(const char* disabled,const char* window)
{
  sysfs.clear(); vdin=Vdin{};
  if(disabled) sysfs["/sys/class/video/disable_video"]=disabled;
  writeFile("video_video_state",window);
  writeFile("display_vinfo","    width: 4\n    height: 2\n");
  for(size_t i=0;i<vdin.memory.size();++i) vdin.memory[i]=static_cast<uint8_t>(i);
}
const char* SHOWN="VPP_hsc_startp 0x0\nVPP_hsc_endp 0x3\nVPP_vsc_startp 0x0\nVPP_vsc_endp 0x1\n";
Result capture(AML_VIDEO_CAPTURE::Frame& frame) {return AML_VIDEO_CAPTURE::Capture(frame);}
Result capture() {AML_VIDEO_CAPTURE::Frame frame; return capture(frame);}
int main()
{
  // The video layer's own state decides, before vdin1 is opened.
  state(nullptr,SHOWN); assert(capture()==Result::UNAVAILABLE && vdin.opens==0);
  state("1",SHOWN); assert(capture()==Result::NO_VIDEO && vdin.opens==0);
  state("0",nullptr); assert(capture()==Result::UNAVAILABLE && vdin.opens==0);
  state("0","VPP_hsc_startp 0x0\nVPP_hsc_endp 0x3\n"); assert(capture()==Result::UNAVAILABLE && vdin.opens==0);
  state("0","VPP_hsc_startp 0x3\nVPP_hsc_endp 0x0\nVPP_vsc_startp 0x0\nVPP_vsc_endp 0x1\n");
  assert(capture()==Result::NO_VIDEO && vdin.opens==0);
  // Busy stays a failure; no vdin1, or one too old to map, is amvideocap's.
  state("0",SHOWN); vdin.openErrno=EBUSY; assert(capture()==Result::FAILED && vdin.opens==1);
  state("0",SHOWN); vdin.openErrno=ENOENT; assert(capture()==Result::UNAVAILABLE && vdin.opens==1);
  state("0",SHOWN); vdin.memInfoErrno=ENOTTY;
  assert(capture()==Result::UNAVAILABLE && vdin.closes==1 && vdin.starts==0);
  // A vdin1 that has the interface but gives no frame is a real failure.
  state("0",SHOWN); vdin.startErrno=ENOMEM;
  assert(capture()==Result::FAILED && vdin.starts==1 && vdin.stops==0 && vdin.closes==1);
  state("0",SHOWN); vdin.frames=0;
  assert(capture()==Result::FAILED && vdin.stops==1 && vdin.closes==1 && vdin.maps==0);
  state("0",SHOWN);
  {
    AML_VIDEO_CAPTURE::Frame frame; assert(capture(frame)==Result::OK);
    assert(frame.width==4 && frame.height==2 && frame.displayWidth==4 && frame.displayHeight==2);
    assert(frame.frameWidth==4 && frame.frameHeight==2 && frame.stride==4 && frame.nv12.size()==12);
    for(int i=0;i<8;++i) assert(frame.nv12[i]==64+i);
    for(int i=0;i<4;++i) assert(frame.nv12[8+i]==128+i);
    assert(frame.signal==AML_VIDEO_CAPTURE::Signal::SDR);
    // Every frame taken is returned, and the session stopped and closed.
    assert(vdin.gets==2 && vdin.puts==2 && vdin.stops==1 && vdin.closes==1 && vdin.maps==vdin.unmaps);
  }
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='aml-video-capture-') as temporary:
        directory = Path(temporary)
        (directory / 'test.cpp').write_text(harness(directory))
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(directory / 'test.cpp'), '-o', str(directory / 'test')], check=True)
        subprocess.run([str(directory / 'test')], check=True, timeout=30)
    print('AML video capture fallback: PASS (complete AMLVideoCapture.cpp; sysfs/vdin1 stand-ins; ASan/UBSan)')


if __name__ == '__main__':
    main()
