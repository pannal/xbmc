#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The high-priority upload context's converter cache is retired with it.

Runs the actual CEGLContextUtils::DestroyContext and BindTextureUploadContext
against a recording fixture: teardown releases the AFBC converter objects of
both upload contexts, each while bound, before any context is destroyed; a
high-priority context that cannot be bound drops its converter bookkeeping
before it is destroyed (a later context could reuse the handle) and the upload
falls back to the medium-priority context. Mutations are negative controls.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]


def methods(source):
    parts = []
    for signature, end in (('void CEGLContextUtils::DestroyContext()', '\n// The upload context'),
                           ('bool CEGLContextUtils::BindTextureUploadContext(', '\nbool CEGLContextUtils::UnbindTextureUploadContext()')):
        start = source.index(signature)
        parts.append(source[start:source.index(end, start)])
    return '\n'.join(parts)


FIXTURE = r"""
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <set>
#include <vector>
#define TARGET_LINUX 1
#define HAS_LIBAMCODEC 1
#define HAS_GLES 2
using EGLContext=int;using EGLSurface=int;using EGLDisplay=int;using EGLBoolean=int;
constexpr int EGL_NO_CONTEXT=0,EGL_NO_SURFACE=0,EGL_NO_DISPLAY=0,EGL_TRUE=1;
using CCriticalSection=std::recursive_mutex;
#define LOGWARNING 2
struct CLog{template<typename...A>static void LogF(int,const char*,A&&...){}};
static std::set<int> live,unbindable;
static int current=0;
static std::vector<int> deletedFramebuffers,deletedPrograms,destroyedSurfaces;
static std::map<int,int> expectedSurfaces;
static EGLContext eglGetCurrentContext(){return current;}
static void glDeleteProgram(int program){deletedPrograms.push_back(program);}
struct ConversionResources
{
 int fbo,program;
 void Delete(){deletedFramebuffers.push_back(fbo);glDeleteProgram(program);}
};
static std::mutex conversionLock;
static std::map<EGLContext,ConversionResources> conversionResources;
static void Fail(const char* what){std::fprintf(stderr,"FAIL: %s\n",what);std::exit(1);}
int eglGetError(){return 0x3002;}
EGLBoolean eglMakeCurrent(int,int surface,int,int context){
 if(context&&(unbindable.count(context)||!live.count(context)))return 0;
 if(expectedSurfaces.count(context)&&expectedSurfaces.at(context)!=surface)
  Fail("teardown: each converter binds its own surface");
 current=context;return EGL_TRUE;
}
EGLBoolean eglDestroyContext(int,int context){
 if(conversionResources.count(context))Fail("context destroyed while its converter cache remains");
 if(!live.erase(context))Fail("destroy of an unknown context");
 return EGL_TRUE;
}
int eglDestroySurface(int,int surface){destroyedSurfaces.push_back(surface);return EGL_TRUE;}
struct CAMLAFBCTexture{
 static bool HasConversionResources(int context){return conversionResources.count(context)!=0;}
 static void ReleaseConversionResources(EGLContext context,EGLContext sharedContext=EGL_NO_CONTEXT);
};
struct CEGLContextUtils{
 bool BindTextureUploadContext(const std::atomic_bool* cancelled=nullptr);
 void DestroyContext();
 EGLSurface CreateTextureUploadPbuffer(){return EGL_NO_SURFACE;}
 std::atomic_bool m_textureUploadClosing{false};
 CCriticalSection m_textureUploadLock;
 bool m_textureUploadDisabled=false,m_textureUploadPoisoned=false;
 int m_eglDisplay=1,m_eglContext=1,m_eglUploadContext=2,m_eglUploadContextHigh=4,m_eglUploadSurface=0,m_eglUploadSurfaceHigh=0;
 std::atomic<bool> m_textureUploadHighPriority{false};
 inline static thread_local CEGLContextUtils* m_currentUploadContext=nullptr;
};
"""

MAIN = r"""
int main(){
 // teardown: both upload contexts' converters released while bound, before any destruction
 live={1,2,4};conversionResources={{2,{20,21}},{4,{40,41}}};current=0;
 expectedSurfaces={{2,3},{4,5}};
 {CEGLContextUtils c;c.m_eglUploadSurface=3;c.m_eglUploadSurfaceHigh=5;c.DestroyContext();
  if(!conversionResources.empty())Fail("teardown: converter caches remain");
  if(deletedFramebuffers!=std::vector<int>{20,40} || deletedPrograms!=std::vector<int>{21,41})
   Fail("teardown: each converter released while its context was bound");
  if(!live.empty()||c.m_eglUploadContextHigh!=0||c.m_eglUploadContext!=0)Fail("teardown: contexts destroyed");}
 if(destroyedSurfaces!=std::vector<int>{3,5})Fail("teardown: both upload surfaces destroyed");
 expectedSurfaces.clear();destroyedSurfaces.clear();
 // bind: the high context worked before (has a cache) but cannot be bound now
 live={1,2,4};conversionResources={{4,{40,41}}};unbindable={4};current=0;
 deletedFramebuffers.clear();deletedPrograms.clear();
 {CEGLContextUtils c;c.m_textureUploadHighPriority=true;c.m_eglUploadSurfaceHigh=5;
  if(!c.BindTextureUploadContext())Fail("fallback: medium-priority upload context bound");
  if(current!=2)Fail("fallback: medium-priority upload context current");
  if(conversionResources.count(4))Fail("fallback: converter bookkeeping of the unbindable context dropped");
  if(deletedPrograms!=std::vector<int>{41} || !deletedFramebuffers.empty())
   Fail("fallback: only the shared program is deleted from medium");
  if(live.count(4)||c.m_eglUploadContextHigh!=0)Fail("fallback: unbindable high-priority context retired");
  if(destroyedSurfaces!=std::vector<int>{5} || c.m_eglUploadSurfaceHigh!=0)
   Fail("fallback: high pbuffer retired with its context");
  c.m_currentUploadContext=nullptr;c.m_textureUploadLock.unlock();}
 // bind: flagged and bindable
 live={1,2,4};conversionResources.clear();unbindable.clear();current=0;
 {CEGLContextUtils c;c.m_textureUploadHighPriority=true;
  if(!c.BindTextureUploadContext()||current!=4)Fail("flagged: high-priority upload context current");
  c.m_currentUploadContext=nullptr;c.m_textureUploadLock.unlock();}
 conversionResources={{4,{40,41}}};current=99;
 deletedFramebuffers.clear();deletedPrograms.clear();
 CAMLAFBCTexture::ReleaseConversionResources(4,2);
 if(!deletedPrograms.empty() || !deletedFramebuffers.empty())
  Fail("unrelated context must not delete any converter objects");
 live={1,2,4};conversionResources={{4,{40,41}}};unbindable={2,4};current=0;
 {CEGLContextUtils c;c.m_textureUploadHighPriority=true;
  if(c.BindTextureUploadContext())Fail("no upload context bound");
  if(!live.count(4) || !conversionResources.count(4))Fail("owner retained for teardown");
  c.DestroyContext();
  if(!live.empty() || !conversionResources.empty())Fail("failed share-group teardown");}
 std::puts("ok");
}
"""


def run(source, texture, witness, tmp, name):
    p = Path(tmp) / (name + '.cpp'); exe = Path(tmp) / name
    start = texture.index('void CAMLAFBCTexture::ReleaseConversionResources(')
    end = texture.index('\nbool CAMLAFBCTexture::Allocate(', start)
    p.write_text(FIXTURE + methods(source) + texture[start:end] + MAIN)
    subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    str(p), '-o', str(exe)], check=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    ok = r.returncode == 0 if witness is None else r.returncode != 0 and witness in r.stderr
    print(f"{name}: {'accepted' if ok else 'REJECTED'}" + (f' (witness: {witness})' if witness else ''))
    if not ok:
        sys.exit(r.stderr or r.stdout)


source = (root / 'xbmc/utils/EGLUtils.cpp').read_text()
texture = (root / 'xbmc/guilib/AMLAFBCTexture.cpp').read_text()
mutations = {
    'keep-fallback-cache': ('      CAMLAFBCTexture::ReleaseConversionResources(m_eglUploadContextHigh, m_eglUploadContext);\n', '',
                            'context destroyed while its converter cache remains'),
    'teardown-medium-only': ('  for (EGLContext upload : {m_eglUploadContext, m_eglUploadContextHigh})',
                             '  for (EGLContext upload : {m_eglUploadContext})', 'converter cache remains'),
}
with tempfile.TemporaryDirectory(prefix='afbc-upload-high-retire-') as tmp:
    run(source, texture, None, tmp, 'actual')
    for name, (before, after, witness) in mutations.items():
        assert source.count(before) == 1, name
        run(source.replace(before, after), texture, witness, tmp, name)
    run(source, texture.replace('    glDeleteProgram(it->second.program);', '    (void)it;'),
        'fallback: only the shared program is deleted from medium', tmp, 'leak-shared-program')
    run(source, texture.replace('current == sharedContext', 'sharedContext != EGL_NO_CONTEXT'),
        'unrelated context must not delete any converter objects', tmp, 'delete-from-unrelated-context')
print('PASS high-priority upload context retirement: converters released before destruction, '
      'unbindable context retired with its bookkeeping, fallback to medium')
