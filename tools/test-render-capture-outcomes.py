#!/usr/bin/env python3
"""Exercise production capture outcomes with controlled syscall/GL/service stand-ins.

Extracts complete AML video capture, AML/GLES RenderCapture and AML/GLES screenshot
Capture methods. Checks bytes, failure states, composition cancellation, stale
readback suppression and geometry/matrix restoration. ASan/UBSan do not prove GPU
or driver behavior, nor detect reads of uninitialized memory; differing initialized
nonblend inputs verify output independence instead.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    aml = (ROOT / 'xbmc/utils/ScreenshotAML.cpp').read_text()
    defines = '\n'.join(line for line in aml.splitlines() if line.startswith('#define '))
    source = PRELUDE + '\n' + defines + '\n'
    source += function(aml, 'bool CScreenshotAML::CaptureVideoFrame(')
    source += '\n#undef open\n#undef ioctl\n#undef pread\n#undef close\n'
    for path, signature in [
        ('cores/VideoPlayer/VideoRenderers/HwDecRender/RendererAML.cpp', 'bool CRendererAML::RenderCapture('),
        ('cores/VideoPlayer/VideoRenderers/LinuxRendererGLES.cpp', 'bool CLinuxRendererGLES::RenderCapture('),
        ('platform/linux/ScreenshotSurfaceAML.cpp', 'bool CScreenshotSurfaceAML::Capture()'),
        ('rendering/gles/ScreenshotSurfaceGLES.cpp', 'bool CScreenshotSurfaceGLES::Capture()'),
    ]:
        source += '\n' + function((ROOT / 'xbmc' / path).read_text(), signature)
    return source + TESTS


def main():
    with tempfile.TemporaryDirectory(prefix='render-capture-') as temporary:
        directory = Path(temporary)
        (directory / 'test.cpp').write_text(harness())
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', '-I', str(ROOT / 'xbmc'),
                        str(directory / 'test.cpp'), '-o', str(directory / 'test')], check=True)
        subprocess.run([str(directory / 'test')], check=True)
    print('Render capture outcomes: PASS (five production methods; ASan/UBSan; GPU/driver/services stubbed)')


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <vector>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "utils/Geometry.h"
#include "utils/ScreenshotAML.h"
#include "cores/VideoPlayer/VideoRenderers/RenderFlags.h"
#define HAS_GLES 2
constexpr int CAPTURESTATE_WORKING=1, CAPTURESTATE_DONE=2, CAPTURESTATE_FAILED=3;
constexpr int GL_BLEND=1, GL_VIEWPORT=2, GL_RGBA=3, GL_UNSIGNED_BYTE=4;
using GLint=int;
using GLvoid=void;
using CCriticalSection=std::recursive_mutex;
struct Hardware {
  int openResult=7, failIoctl=0, ioctlCalls=0, opens=0, closes=0, reads=0;
  int width=0, height=0;
  bool shortRead=false, readError=false;
} hardware;
int fakeOpen(const char* path,int flags,int mode) {
  assert(std::strcmp(path,"/dev/amvideocap0")==0 && flags==O_RDWR && mode==0);
  ++hardware.opens; return hardware.openResult;
}
int fakeIoctl(int fd,unsigned long request,int value) {
  assert(fd==7); ++hardware.ioctlCalls;
  if (hardware.ioctlCalls==1) {
    assert(request==_IOW('V',0x02,int));hardware.width=value;
  } else {
    assert(request==_IOW('V',0x03,int));hardware.height=value;
  }
  return hardware.failIoctl==hardware.ioctlCalls ? -1 : 0;
}
ssize_t fakePread(int fd,void* buffer,size_t size,off_t offset) {
  assert(fd==7 && offset==0); ++hardware.reads;
  assert(size==static_cast<size_t>(hardware.width*hardware.height*3));
  auto* bytes=static_cast<unsigned char*>(buffer);
  for(int y=0;y<hardware.height;++y)
    for(int x=0;x<hardware.width;++x)
      for(int c=0;c<3;++c) bytes[(y*hardware.width+x)*3+c]=10+y*30+x*3+c;
  if(hardware.readError) return -1;
  return hardware.shortRead ? static_cast<ssize_t>(size)-1 : static_cast<ssize_t>(size);
}
int fakeClose(int fd) { assert(fd==7); ++hardware.closes; return 0; }
struct RenderSystem {
  bool valid=true; unsigned generation=1;
  bool CanRender() const { return valid; }
  unsigned CaptureRenderTarget() const { return generation; }
  bool IsRenderTargetCurrent(unsigned target) const { return valid && target==generation; }
} renderSystem;
struct Gfx : CCriticalSection { int GetHeight() const { return 1080; } };
struct CWinSystemBase {
  Gfx gfx; bool begin=true,end=true; int begins=0,ends=0,cancels=0;
  Gfx& GetGfxContext() { return gfx; }
  bool BeginGuiComposite() { ++begins; return begin; }
  bool EndGuiComposite() { ++ends; return end; }
  void CancelGuiComposite() { ++cancels; }
} window;
struct WindowManager {
  int renders=0; bool stale=false;
  void Render() { ++renders; if(stale) ++renderSystem.generation; }
};
struct CGUIComponent { WindowManager manager; WindowManager& GetWindowManager(){return manager;} } gui;
struct CServiceBroker {
  static inline RenderSystem* renderer=&renderSystem;
  static inline CWinSystemBase* win=&window;
  static inline CGUIComponent* guiPtr=&gui;
  static RenderSystem* GetRenderSystem(){return renderer;}
  static CWinSystemBase* GetWinSystem(){return win;}
  static CGUIComponent* GetGUI(){return guiPtr;}
};
struct Matrix {
  int value=17, loads=0,pops=0,popLoads=0; std::vector<int> stack;
  Matrix* operator->(){return this;}
  void Push(){stack.push_back(value);}
  void Translatef(float,float,float){value+=3;}
  void Scalef(float,float,float){value+=5;}
  void Load(){++loads;}
  void Pop(){assert(!stack.empty());value=stack.back();stack.pop_back();++pops;}
  void PopLoad(){Pop();++popLoads;}
} glMatrixModview;
int readbacks=0,disables=0;
void glDisable(int what){assert(what==GL_BLEND);++disables;}
void glGetIntegerv(int what,int* viewport){assert(what==GL_VIEWPORT);viewport[0]=viewport[1]=0;viewport[2]=viewport[3]=2;}
void glReadPixels(int x,int y,int width,int height,int format,int type,void* buffer){
  assert(x==0 && y>=0 && format==GL_RGBA && type==GL_UNSIGNED_BYTE && buffer);
  ++readbacks;auto* bytes=static_cast<unsigned char*>(buffer);
  for(int row=0;row<height;++row)for(int col=0;col<width;++col){
    const int i=(row*width+col)*4;bytes[i]=10+row*40+col*4;
    bytes[i+1]=bytes[i]+1;bytes[i+2]=bytes[i]+2;bytes[i+3]=255;
  }
}
struct CRenderCapture {
  unsigned width=2,height=2;int begins=0,ends=0,state=0;
  std::array<unsigned char,16> bytes{};bool nullBuffer=false;
  unsigned GetWidth(){return width;} unsigned GetHeight(){return height;}
  void* GetRenderBuffer(){return nullBuffer ? nullptr : bytes.data();}
  void BeginRender(){++begins;state=CAPTURESTATE_WORKING;}
  void EndRender(){++ends;state=CAPTURESTATE_DONE;}
  void SetState(int value){state=value;}
};
struct CRendererAML { bool RenderCapture(int,CRenderCapture*); };
struct CLinuxRendererGLES {
  bool m_bValidated=true,rendered=true,stale=false,invalid=false;
  int renders=0,dirty=0;
  CRect m_destRect{5,6,105,206};std::array<int,4> rotated{3,7,11,13},saved{};
  void saveRotatedCoords(){saved=rotated;}
  void syncDestRectToRotatedPoints(){rotated={0,0,2,2};}
  void restoreRotatedCoords(){rotated=saved;}
  void MarkDirty(){++dirty;}
  bool Render(int flags,int index){
    assert(flags==RENDER_FLAG_NOOSD && index==5);++renders;
    assert(m_destRect==CRect(0,0,2,2));assert(glMatrixModview.value==25);
    if(stale)++renderSystem.generation;
    if(invalid)renderSystem.valid=false;
    return rendered;
  }
  bool RenderCapture(int,CRenderCapture*);
};
struct Surface {
  int m_width=0,m_height=0,m_stride=0;unsigned char* m_buffer=nullptr;
  ~Surface(){delete[] m_buffer;}
};
struct CScreenshotSurfaceAML : Surface { bool Capture(); };
struct CScreenshotSurfaceGLES : Surface { bool Capture(); };
void reset(){
  hardware={};renderSystem={};gui={};window.begin=window.end=true;
  window.begins=window.ends=window.cancels=0;
  CServiceBroker::renderer=&renderSystem;CServiceBroker::win=&window;CServiceBroker::guiPtr=&gui;
  glMatrixModview={};readbacks=disables=0;
}
#define open fakeOpen
#define ioctl fakeIoctl
#define pread fakePread
#define close fakeClose
'''

TESTS = r'''
void testHardware(){
  reset();std::array<unsigned char,16> a{},b{};b.fill(231);
  assert(!CScreenshotAML::CaptureVideoFrame(nullptr,2,2,false));
  assert(!CScreenshotAML::CaptureVideoFrame(a.data(),0,2,false));
  assert(!CScreenshotAML::CaptureVideoFrame(a.data(),2,-1,false));
  assert(!CScreenshotAML::CaptureVideoFrame(a.data(),std::numeric_limits<int>::max(),2,false));
  assert(!CScreenshotAML::CaptureVideoFrame(a.data(),32,std::numeric_limits<int>::max(),false));
  assert(hardware.opens==0);
  for(int failure=0;failure<5;++failure){
    reset();a.fill(93);const auto before=a;
    hardware.openResult=failure==0 ? -1 : 7;
    hardware.failIoctl=failure==1 ? 1 : failure==2 ? 2 : 0;
    hardware.shortRead=failure==3;hardware.readError=failure==4;
    assert(!CScreenshotAML::CaptureVideoFrame(a.data(),2,2,false));assert(a==before);
    assert(hardware.closes==(failure==0 ? 0 : 1));
    assert(hardware.reads==(failure>=3 ? 1 : 0));
  }
  reset();assert(CScreenshotAML::CaptureVideoFrame(a.data(),2,2,false));
  assert(hardware.width==32 && hardware.height==2 && hardware.closes==1);
  reset();assert(CScreenshotAML::CaptureVideoFrame(b.data(),2,2,false));assert(a==b);
  const std::array<unsigned char,16> expected{10,11,12,255,13,14,15,255,40,41,42,255,43,44,45,255};
  assert(a==expected);
  reset();a={110,111,112,0,113,114,115,255,140,141,142,128,143,144,145,64};
  const auto original=a;
  assert(CScreenshotAML::CaptureVideoFrame(a.data(),2,2,true));
  for(int i=0;i<4;++i){
    const float alpha=original[i*4+3]/255.0f;
    for(int c=0;c<3;++c)
      assert(a[i*4+c]==static_cast<unsigned char>(alpha*original[i*4+c]+(1-alpha)*expected[i*4+c]));
    assert(a[i*4+3]==255);
  }
}
void testRenderers(){
  reset();CRendererAML aml;assert(!aml.RenderCapture(0,nullptr));
  for(int failure=0;failure<4;++failure){
    reset();CRenderCapture capture;capture.bytes.fill(99);const auto previous=capture.bytes;
    if(failure==0)hardware.openResult=-1;
    if(failure==1)hardware.shortRead=true;
    if(failure==2)capture.nullBuffer=true;
    const bool result=aml.RenderCapture(0,&capture);
    assert(result==(failure==3));assert(capture.begins==1);
    assert(capture.state==(result ? CAPTURESTATE_DONE : CAPTURESTATE_FAILED));
    assert(capture.ends==(result ? 1 : 0));
    if(!result)assert(capture.bytes==previous);
  }
  reset();CLinuxRendererGLES renderer;assert(!renderer.RenderCapture(5,nullptr));
  for(int mode=0;mode<7;++mode){
    reset();CLinuxRendererGLES subject;CRenderCapture capture;capture.bytes.fill(66);
    const auto before=capture.bytes;const CRect original=subject.m_destRect;const auto rotated=subject.rotated;
    if(mode==0)subject.m_bValidated=false;
    if(mode==1)renderSystem.valid=false;
    if(mode==2)CServiceBroker::renderer=nullptr;
    if(mode==3)subject.rendered=false;
    if(mode==4)subject.stale=true;
    if(mode==5)subject.invalid=true;
    const bool result=subject.RenderCapture(5,&capture);
    assert(result==(mode==6));assert(capture.state==(result ? CAPTURESTATE_DONE : CAPTURESTATE_FAILED));
    assert(readbacks==(result ? 1 : 0));assert(capture.ends==(result ? 1 : 0));
    assert(subject.m_destRect==original && subject.rotated==rotated);
    assert(glMatrixModview.value==17 && glMatrixModview.stack.empty());
    assert(glMatrixModview.pops==(mode>=3 ? 1 : 0));
    assert(glMatrixModview.popLoads==((mode==3 || mode==6) ? 1 : 0));
    if(!result)assert(capture.bytes==before);
    else assert(capture.bytes[0]==12 && capture.bytes[1]==11 && capture.bytes[2]==10 && capture.bytes[3]==255);
  }
}
template<class T>void testScreenshot(){
  for(int mode=0;mode<6;++mode){
    reset();T surface;
    if(mode==0)CServiceBroker::renderer=nullptr;
    if(mode==1)renderSystem.valid=false;
    if(mode==2)window.begin=false;
    if(mode==3)gui.manager.stale=true;
    if(mode==4)window.end=false;
    hardware.openResult=-1; // AML's GUI-only fallback must remain successful.
    const bool result=surface.Capture();assert(result==(mode==5));
    assert(readbacks==(result ? 1 : 0));
    assert(window.cancels==((mode==3 || mode==4) ? 1 : 0));
    assert(window.ends==((mode==4 || mode==5) ? 1 : 0));
    if(result){
      assert(surface.m_width==2 && surface.m_height==2 && surface.m_stride==8);
      const std::array<unsigned char,16> expected{52,51,50,255,56,55,54,255,12,11,10,255,16,15,14,255};
      assert(std::equal(expected.begin(),expected.end(),surface.m_buffer));
    }else assert(!surface.m_buffer);
  }
}
int main(){
  testHardware();testRenderers();testScreenshot<CScreenshotSurfaceAML>();testScreenshot<CScreenshotSurfaceGLES>();
  reset();CScreenshotSurfaceGLES noWindow;CServiceBroker::win=nullptr;assert(!noWindow.Capture());
  reset();CScreenshotSurfaceGLES noGui;CServiceBroker::guiPtr=nullptr;assert(!noGui.Capture());
}
'''

if __name__ == '__main__':
    main()
