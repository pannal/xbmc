#!/usr/bin/env python3
"""Actual AML screenshot capture/read methods with original-session admission.

Executes CaptureSource and registry methods, the complete ScreenshotSurfaceAML
Capture method and complete CaptureVideoFrame helper, using real AMLSession.
GL, GUI, context availability and /dev/amvideocap0 syscalls are recording stubs.
Decoder mutation uses the real session protocol, not actual reset/open ioctls.
ASan/UBSan and controlled concurrent reads establish host admission/lifetime
behavior, not real device capture, EGL validity, scheduling or TSAN evidence.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    codec = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
    hardware = (ROOT / 'xbmc/utils/ScreenshotAML.cpp').read_text()
    surface = (ROOT / 'xbmc/platform/linux/ScreenshotSurfaceAML.cpp').read_text()
    methods = '\n'.join(function(header, signature) for signature in [
        'struct CaptureSource', 'uint64_t      GetOperationEpoch()',
        'CAMLSession::Permit AcquirePresentation('])
    methods = methods.replace('}\nuint64_t', '};\nuint64_t')
    source = PRELUDE.replace('@CODEC_METHODS@', methods)
    source += '\n'.join(function(codec, signature) for signature in [
        'void CAMLCodec::SetCaptureSource(', 'CAMLCodec::CaptureSource CAMLCodec::GetCaptureSource()'])
    source += '\n' + '\n'.join(line for line in hardware.splitlines() if line.startswith('#define '))
    source += '\n#define open fakeOpen\n#define ioctl fakeIoctl\n#define pread fakePread\n#define close fakeClose\n'
    source += function(hardware, 'bool CScreenshotAML::CaptureVideoFrame(')
    source += '\n#undef open\n#undef ioctl\n#undef pread\n#undef close\n'
    source += function(surface, 'bool CScreenshotSurfaceAML::Capture()')
    return source + TESTS


PRELUDE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include "utils/ScreenshotAML.h"
#include <array>
#include <cassert>
#include <cstring>
#include <functional>
#include <future>
#include <limits>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
class CAMLCodec
{
public:
  CAMLSession m_session;
  @CODEC_METHODS@
  static CaptureSource GetCaptureSource();
  static void SetCaptureSource(const std::shared_ptr<CAMLCodec>&);
  static std::mutex s_captureMutex;
  static std::weak_ptr<CAMLCodec> s_captureCodec;
};
std::mutex CAMLCodec::s_captureMutex;
std::weak_ptr<CAMLCodec> CAMLCodec::s_captureCodec;
struct Hardware
{
  int opens{0}, reads{0}, closes{0}, ioctls{0};
  int openResult{7}, ioctlFailure{0}, width{0}, height{0};
  bool shortRead{false}, readFailure{false};
  std::function<void()> onRead;
} hardware;
int fakeOpen(const char* path,int flags,int mode)
{
  assert(std::strcmp(path,"/dev/amvideocap0")==0 && flags==O_RDWR && mode==0);
  ++hardware.opens; return hardware.openResult;
}
int fakeIoctl(int fd,unsigned long request,int value)
{
  assert(fd==7); ++hardware.ioctls;
  if(request==_IOW('V',0x02,int)) hardware.width=value;
  else { assert(request==_IOW('V',0x03,int)); hardware.height=value; }
  return hardware.ioctls==hardware.ioctlFailure ? -1 : 0;
}
ssize_t fakePread(int fd,void* output,size_t size,off_t offset)
{
  assert(fd==7 && offset==0 && size==static_cast<size_t>(hardware.width*hardware.height*3));
  ++hardware.reads;
  if(hardware.onRead) hardware.onRead();
  auto* bytes=static_cast<unsigned char*>(output);
  for(size_t i=0;i<size;++i) bytes[i]=70+i%3;
  if(hardware.readFailure) return -1;
  return hardware.shortRead ? static_cast<ssize_t>(size)-1 : size;
}
int fakeClose(int fd) { assert(fd==7); ++hardware.closes; return 0; }
struct RenderSystem
{
  bool ready{true}; uint64_t generation{1};
  bool CanRender() {return ready;}
  uint64_t CaptureRenderTarget() {return generation;}
  bool IsRenderTargetCurrent(uint64_t target) {return ready && target==generation;}
} renderSystem;
struct CWinSystemBase
{
  CCriticalSection graphics;
  int begins{0}, ends{0}, cancels{0}; bool begin{true}, end{true};
  CCriticalSection& GetGfxContext() {return graphics;}
  bool BeginGuiComposite() {++begins; return begin;}
  bool EndGuiComposite() {++ends; return end;}
  void CancelGuiComposite() {++cancels;}
} window;
struct WindowManager
{
  std::function<void()> onRender;
  void Render() {if(onRender) onRender();}
};
struct GUI {WindowManager windows; WindowManager& GetWindowManager(){return windows;}} gui;
struct CServiceBroker
{
  static CWinSystemBase* GetWinSystem(){return &window;}
  static RenderSystem* GetRenderSystem(){return &renderSystem;}
  static GUI* GetGUI(){return &gui;}
};
#define HAS_GLES 2
constexpr int GL_VIEWPORT=1,GL_RGBA=2,GL_UNSIGNED_BYTE=3;
using GLint=int; using GLvoid=void;
int glReads=0;
void glGetIntegerv(int name,int* viewport)
{ assert(name==GL_VIEWPORT); viewport[0]=viewport[1]=0; viewport[2]=2; viewport[3]=1; }
void glReadPixels(int x,int y,int width,int height,int format,int type,void* output)
{
  assert(x==0&&y==0&&width==2&&height==1&&format==GL_RGBA&&type==GL_UNSIGNED_BYTE);
  ++glReads;
  const std::array<unsigned char,8> pixels{1,2,3,0,4,5,6,0};
  std::memcpy(output,pixels.data(),pixels.size());
}
struct CScreenshotSurfaceAML
{
  int m_width{0},m_height{0},m_stride{0}; unsigned char* m_buffer{nullptr};
  ~CScreenshotSurfaceAML(){delete[] m_buffer;}
  bool Capture();
};
const std::array<unsigned char,8> guiOnly{3,2,1,0,6,5,4,0};
const std::array<unsigned char,8> withVideo{70,71,72,255,70,71,72,255};
void checkPixels(const CScreenshotSurfaceAML& surface,const std::array<unsigned char,8>& expected)
{
  assert(surface.m_width==2&&surface.m_height==1&&surface.m_stride==8&&surface.m_buffer);
  assert(std::equal(expected.begin(),expected.end(),surface.m_buffer));
}
void reset()
{
  hardware={}; gui.windows.onRender={}; glReads=0;
  renderSystem.ready=true; ++renderSystem.generation;
  window.begins=window.ends=window.cancels=0; window.begin=window.end=true;
  CAMLCodec::SetCaptureSource({});
}
void mutate(const std::shared_ptr<CAMLCodec>& codec,bool open)
{
  auto request=codec->m_session.Fence();
  assert(codec->m_session.BeginMutation(request));
  assert(codec->m_session.Complete(request,open));
}
std::shared_ptr<CAMLCodec> opened()
{
  auto codec=std::make_shared<CAMLCodec>(); mutate(codec,true);
  CAMLCodec::SetCaptureSource(codec); return codec;
}
'''

TESTS = r'''
void no_session_and_read_failures()
{
  reset();
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);
   assert(hardware.opens==0&&glReads==1);}
  auto codec=opened();
  for(int failure=0;failure<6;++failure)
  {
    reset(); CAMLCodec::SetCaptureSource(codec);
    hardware.openResult=failure==1?-1:7;
    hardware.ioctlFailure=failure==2?1:failure==3?2:0;
    hardware.shortRead=failure==4; hardware.readFailure=failure==5;
    CScreenshotSurfaceAML surface; assert(surface.Capture());
    checkPixels(surface,failure==0?withVideo:guiOnly);
    assert(hardware.opens==1&&hardware.closes==(failure==1?0:1));
    assert(hardware.reads==((failure==0||failure>=4)?1:0));
  }
  reset();
  {auto expired=opened();} // Weak registry does not manufacture a live session.
  CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);
  assert(hardware.opens==0);
}
void stale_session_during_gui()
{
  for(bool reopen:{false,true})
  {
    reset(); auto codec=opened();
    gui.windows.onRender=[&]{mutate(codec,reopen);};
    CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);
    assert(hardware.opens==0);
  }
  reset(); auto codec=opened(); auto pending=codec->m_session.Fence();
  CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);
  assert(hardware.opens==0);
  assert(codec->m_session.BeginMutation(pending)); assert(codec->m_session.Complete(pending,true));
}
void replacement_during_gui()
{
  // Even an otherwise live old allocation cannot authorize a read of the
  // global capture device after its published output identity changes.
  for(bool closeOriginal:{false,true})
  {
    reset(); auto original=opened(); std::weak_ptr<CAMLCodec> retained=original;
    auto replacement=std::make_shared<CAMLCodec>(); mutate(replacement,true);
    gui.windows.onRender=[&]{
      if(closeOriginal) mutate(original,false);
      CAMLCodec::SetCaptureSource(replacement); original.reset();
      assert(!retained.expired()); // CaptureSource pins its original allocation.
    };
    CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);
    assert(hardware.opens==0&&retained.expired());
    gui.windows.onRender={};
    CScreenshotSurfaceAML fresh; assert(fresh.Capture()); checkPixels(fresh,withVideo);
    assert(hardware.opens==1);
  }
}
void display_and_target_failure()
{
  reset(); auto codec=opened(); const auto epoch=codec->GetOperationEpoch();
  auto display=CAMLSession::FenceDisplay(); assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::FAILED));
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly); assert(hardware.opens==0);}
  display=CAMLSession::FenceDisplay(); assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::WAITING_FOR_RESET));
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly); assert(hardware.opens==0);}
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(codec->GetOperationEpoch()==epoch);
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,withVideo);}
  reset(); CAMLCodec::SetCaptureSource(codec); renderSystem.ready=false;
  {CScreenshotSurfaceAML surface; assert(!surface.Capture()); assert(glReads==0&&hardware.opens==0);}
  renderSystem.ready=true; gui.windows.onRender=[] {++renderSystem.generation;};
  {CScreenshotSurfaceAML surface; assert(!surface.Capture()); assert(glReads==0&&hardware.opens==0&&window.cancels==1);}
}
void display_requested_inside_read()
{
  reset(); auto codec=opened(); const auto epoch=codec->GetOperationEpoch();
  CAMLSession::DisplayRequest display;
  hardware.onRead=[&] {
    display=CAMLSession::FenceDisplay();
    assert(display && !CAMLSession::TryBeginDisplay(display));
    // A display callback cannot wait for this stack's own permit. It retains
    // the request, returns promptly, and continues after the read unwinds.
  };
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,withVideo);}
  hardware.onRead={};
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,guiOnly);}
  assert(hardware.reads==1 && CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(codec->GetOperationEpoch()==epoch);
  {CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,withVideo);}
  assert(hardware.reads==2);
}
void counted_read_vs_mutation()
{
  reset(); auto codec=opened();
  std::promise<void> reading,releaseRead;
  auto entered=reading.get_future(); auto release=releaseRead.get_future();
  hardware.onRead=[&] {reading.set_value(); assert(release.wait_for(5s)==std::future_status::ready);};
  std::thread decoder([&]{
    assert(entered.wait_for(5s)==std::future_status::ready);
    auto mutation=codec->m_session.Fence();
    assert(!codec->m_session.Wait(mutation,0ms));
    assert(!codec->m_session.BeginMutation(mutation));
    releaseRead.set_value();
    assert(codec->m_session.Wait(mutation,5s));
    assert(codec->m_session.BeginMutation(mutation));
    assert(codec->m_session.Complete(mutation,true));
  });
  CScreenshotSurfaceAML surface; assert(surface.Capture()); checkPixels(surface,withVideo);
  decoder.join(); assert(hardware.reads==1);
}
int main()
{
  no_session_and_read_failures(); stale_session_during_gui(); replacement_during_gui();
  display_and_target_failure(); display_requested_inside_read(); counted_read_vs_mutation(); reset();
}
'''


def run(source, negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-screenshot-admission-') as temporary:
        directory=Path(temporary)
        cpp=directory/'test.cpp'; binary=directory/'test'
        cpp.write_text(source)
        subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                        '-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer',
                        '-fno-pie','-no-pie','-I',str(ROOT/'xbmc'),str(cpp),'-o',str(binary)],check=True)
        result=subprocess.run([str(binary)],capture_output=negative,text=True,timeout=15)
        if negative:
            assert result.returncode!=0 and 'Assertion' in result.stderr,result.stderr
        else:
            result.check_returncode()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args(); source=harness(); run(source)
    print('AML direct screenshot: PASS (production capture/source/read helpers; real session; ASan/UBSan; GUI/GL/syscall stubs)')
    if args.negative_controls:
        controls=[
            ('capture ignores admission','if (permit)\n    CScreenshotAML::CaptureVideoFrame','if (true)\n    CScreenshotAML::CaptureVideoFrame'),
            ('stale source re-resolves current','auto permit = video.Acquire();','auto permit = CAMLCodec::GetCaptureSource().Acquire();'),
            ('replaced live source remains admitted','if (!codec || s_captureCodec.lock() != codec)','if (!codec)'),
            ('stale source refreshes epoch','codec->AcquirePresentation(epoch)','codec->AcquirePresentation(codec->GetOperationEpoch())'),
            ('permit ends before read','auto permit = video.Acquire();','const bool permit = static_cast<bool>(video.Acquire());'),
        ]
        for label,before,after in controls:
            assert before in source,label
            run(source.replace(before,after),negative=True)
            print('Rejected runtime negative control:',label)


if __name__=='__main__':
    main()
