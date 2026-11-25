#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run actual converter-retirement EGL teardown against a recording fixture."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root/'xbmc/utils/EGLUtils.cpp').read_text()
start = source.index('void CEGLContextUtils::DestroyContext()')
end = source.index('\n// The upload context', start)
method = source[start:end]
code = r'''
#include <cassert>
#include <atomic>
#include <mutex>
#include <vector>
#define TARGET_LINUX 1
#define HAS_LIBAMCODEC 1
#define HAS_GLES 2
using EGLContext=int;using EGLSurface=int;using EGLDisplay=int;
constexpr int EGL_NO_CONTEXT=0,EGL_NO_SURFACE=0,EGL_TRUE=1;
using CCriticalSection=std::recursive_mutex;
static bool failBind=false,hasCache=true;
static int current=1,deletedContext=0,releases=0,currentAtRelease=0;
static std::vector<int> order;
int eglMakeCurrent(int,int,int,int context){
 if(context && failBind)return 0;
 current=context;order.push_back(10+context);return EGL_TRUE;
}
int eglDestroyContext(int,int context){order.push_back(20+context);deletedContext++;return 1;}
int eglDestroySurface(int,int){order.push_back(30);return 1;}
struct CAMLAFBCTexture{
 static bool HasConversionResources(int context){assert(context==2);return hasCache;}
 static void ReleaseConversionResources(int context){
  assert(context==2);assert(!deletedContext);releases++;
  currentAtRelease=current;order.push_back(40);
 }
};
struct CEGLContextUtils{
 std::atomic_bool m_textureUploadClosing{false};
 CCriticalSection m_textureUploadLock;
 bool m_textureUploadDisabled=false,m_textureUploadPoisoned=false;
 int m_eglDisplay=1,m_eglContext=1,m_eglUploadContext=2,m_eglUploadSurface=3;
 void DestroyContext();
};
'''+method+r'''
int main(){
 for(int mode=0;mode<4;mode++){
  CEGLContextUtils c;failBind=mode==1;c.m_textureUploadPoisoned=mode==2;hasCache=mode!=3;
  current=1;deletedContext=releases=currentAtRelease=0;order.clear();
  c.DestroyContext();
  assert(releases==(hasCache?1:0) && deletedContext==2);
  if(hasCache)assert(currentAtRelease==(mode==0?2:1));
  else assert((order==std::vector<int>{10,21,22,30})); // No extra bind without AFBC.

  assert(c.m_eglUploadContext==0 && c.m_eglContext==0 && c.m_eglUploadSurface==0);
  assert(c.m_textureUploadClosing && c.m_textureUploadDisabled);
 }
}
'''
with tempfile.TemporaryDirectory(prefix='afbc-context-retire-') as tmp:
    p=Path(tmp)/'test.cpp';p.write_text(code);exe=Path(tmp)/'test'
    subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(p),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
print('PASS actual EGL retirement: release before destruction; exact bound context; failed bind/poison metadata retirement; no extra bind without AFBC')
