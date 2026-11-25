#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the production AFBC presenter with recorded session, dma-buf and GL ownership.

Exercises creation through BeginFrame with two/three buffers and setup failures.
The mock session owns each allocation independently of its exported fd and import.
This establishes host cleanup behavior, not driver allocation or display behavior.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#pragma once
#include <algorithm>
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>

using GLuint = unsigned int;
using GLenum = unsigned int;
using GLint = int;
using GLsizei = int;
using EGLDisplay = void*;
using EGLSyncKHR = void*;
using EGLTimeKHR = uint64_t;
using EGLint = int;
constexpr EGLDisplay EGL_NO_DISPLAY = nullptr;
constexpr EGLSyncKHR EGL_NO_SYNC_KHR = nullptr;
constexpr EGLint EGL_SYNC_FENCE_KHR = 1;
constexpr EGLint EGL_CONDITION_SATISFIED_KHR = 2;
constexpr GLenum GL_NO_ERROR = 0;
constexpr GLenum GL_EXTENSIONS = 1;
constexpr GLenum GL_RENDERBUFFER = 2;
constexpr GLenum GL_DEPTH_COMPONENT16 = 3;
constexpr GLenum GL_TEXTURE_2D = 4;
constexpr GLenum GL_TEXTURE_MIN_FILTER = 5;
constexpr GLenum GL_TEXTURE_MAG_FILTER = 6;
constexpr GLenum GL_NEAREST = 7;
constexpr GLenum GL_TEXTURE_WRAP_S = 8;
constexpr GLenum GL_TEXTURE_WRAP_T = 9;
constexpr GLenum GL_CLAMP_TO_EDGE = 10;
constexpr GLenum GL_FRAMEBUFFER = 11;
constexpr GLenum GL_COLOR_ATTACHMENT0 = 12;
constexpr GLenum GL_DEPTH_ATTACHMENT = 13;
constexpr GLenum GL_STENCIL_ATTACHMENT = 14;
constexpr GLenum GL_FRAMEBUFFER_COMPLETE = 15;
constexpr GLenum GL_FRAMEBUFFER_BINDING = 16;
using PFNEGLCREATESYNCKHRPROC = EGLSyncKHR (*)(EGLDisplay, EGLint, const EGLint*);
using PFNEGLCLIENTWAITSYNCKHRPROC = EGLint (*)(EGLDisplay, EGLSyncKHR, EGLint, EGLTimeKHR);
using PFNEGLDESTROYSYNCKHRPROC = EGLint (*)(EGLDisplay, EGLSyncKHR);

enum class Failure { NONE, ALLOCATE, IMPORT, BIND, FRAMEBUFFER, GL_ERROR };
struct Allocation
{
  bool session = true;
  bool exported = true;
  unsigned int imports = 0;
};
struct Fixture
{
  Failure failure = Failure::NONE;
  int failIndex = 2;
  bool failReplacementAllocation = false;
  bool failReplacementSession = false;
  int nextFd = 100;
  int sessionsOpened = 0;
  int sessionRequests = 0;
  int currentIndex = -1;
  GLuint nextObject = 1;
  GLuint framebuffer = 0;
  GLenum error = GL_NO_ERROR;
  std::map<int, std::vector<size_t>> sessions;
  std::map<int, size_t> exported;
  std::set<int> framebuffers;
  std::vector<Allocation> allocations;
  std::set<GLuint> textures, fbos, renderbuffers;

  size_t LiveAllocations() const
  {
    return std::count_if(allocations.begin(), allocations.end(), [](const Allocation& value) {
      return value.session || value.exported || value.imports;
    });
  }
  bool Fails(Failure stage) const
  {
    return sessionsOpened == 1 && currentIndex == failIndex && failure == stage;
  }
  void AssertClean() const
  {
    assert(LiveAllocations() == 0);
    assert(sessions.empty() && exported.empty() && framebuffers.empty());
    assert(textures.empty() && fbos.empty() && renderbuffers.empty());
  }
} fixture;

int MockOpen(const char*, int, ...)
{
  const int fd = fixture.nextFd++;
  fixture.framebuffers.insert(fd);
  return fd;
}
int MockIoctl(int fd, unsigned long request, ...);
int MockClose(int fd)
{
  const auto session = fixture.sessions.find(fd);
  if (session != fixture.sessions.end())
  {
    for (size_t index : session->second)
      fixture.allocations[index].session = false;
    fixture.sessions.erase(session);
    return 0;
  }
  const auto buffer = fixture.exported.find(fd);
  if (buffer != fixture.exported.end())
  {
    fixture.allocations[buffer->second].exported = false;
    fixture.exported.erase(buffer);
    return 0;
  }
  assert(fixture.framebuffers.erase(fd) == 1);
  return 0;
}

class CAMLAFBCImport
{
public:
  enum class Layout { WIDE_BLOCK };
  ~CAMLAFBCImport() { Release(); }
  static bool Supported() { return true; }
  bool Import(int fd, unsigned int, unsigned int, Layout)
  {
    if (fixture.Fails(Failure::IMPORT))
      return false;
    m_allocation = static_cast<int>(fixture.exported.at(fd));
    ++fixture.allocations[m_allocation].imports;
    return true;
  }
  bool BindTexture() const { return !fixture.Fails(Failure::BIND); }
  void Release()
  {
    if (m_allocation < 0)
      return;
    assert(fixture.allocations[m_allocation].imports > 0);
    --fixture.allocations[m_allocation].imports;
    m_allocation = -1;
  }
private:
  int m_allocation = -1;
};
struct CFrameBufferObject
{
  static inline GLuint current = 0;
  static void SetDefaultFramebuffer(GLuint name) { current = name; }
  static GLuint GetDefaultFramebuffer() { return current; }
};
struct CLog
{
  template<class... Args> static void Log(int, const char*, const Args&...) {}
};
constexpr int LOGWARNING = 1;
constexpr int LOGINFO = 2;
struct StringUtils
{
  template<class... Args> static std::string Format(const char* text, const Args&...) { return text; }
};

GLenum glGetError() { const auto error = fixture.error; fixture.error = 0; return error; }
const unsigned char* glGetString(GLenum)
{
  return reinterpret_cast<const unsigned char*>("GL_OES_packed_depth_stencil");
}
void GenObjects(GLsizei count, GLuint* names, std::set<GLuint>& objects)
{
  for (int i = 0; i < count; ++i)
  {
    names[i] = fixture.nextObject++;
    objects.insert(names[i]);
  }
}
void DeleteObjects(GLsizei count, const GLuint* names, std::set<GLuint>& objects)
{
  for (int i = 0; i < count; ++i)
    assert(objects.erase(names[i]) == 1);
}
void glGenRenderbuffers(GLsizei count, GLuint* names) { GenObjects(count, names, fixture.renderbuffers); }
void glDeleteRenderbuffers(GLsizei count, const GLuint* names) { DeleteObjects(count, names, fixture.renderbuffers); }
void glBindRenderbuffer(GLenum, GLuint) {}
void glRenderbufferStorage(GLenum, GLenum, int, int) {}
void glGenTextures(GLsizei count, GLuint* names) { GenObjects(count, names, fixture.textures); }
void glDeleteTextures(GLsizei count, const GLuint* names) { DeleteObjects(count, names, fixture.textures); }
void glBindTexture(GLenum, GLuint) {}
void glTexParameteri(GLenum, GLenum, GLint) {}
void glGenFramebuffers(GLsizei count, GLuint* names) { GenObjects(count, names, fixture.fbos); }
void glDeleteFramebuffers(GLsizei count, const GLuint* names) { DeleteObjects(count, names, fixture.fbos); }
void glBindFramebuffer(GLenum, GLuint name)
{
  assert(name == 0 || fixture.fbos.count(name));
  fixture.framebuffer = name;
}
void glFramebufferTexture2D(GLenum, GLenum, GLenum, GLuint, GLint) {}
void glFramebufferRenderbuffer(GLenum, GLenum, GLenum, GLuint) {}
GLenum glCheckFramebufferStatus(GLenum)
{
  if (fixture.Fails(Failure::GL_ERROR))
    fixture.error = 99;
  return fixture.Fails(Failure::FRAMEBUFFER) ? 0 : GL_FRAMEBUFFER_COMPLETE;
}
void glGetIntegerv(GLenum, GLint* value) { *value = fixture.framebuffer; }
void glFinish() {}
void glFlush() {}
EGLDisplay eglGetCurrentDisplay() { return reinterpret_cast<EGLDisplay>(1); }
EGLSyncKHR CreateSync(EGLDisplay, EGLint, const EGLint*) { assert(false); return nullptr; }
EGLint WaitSync(EGLDisplay, EGLSyncKHR, EGLint, EGLTimeKHR) { assert(false); return 0; }
EGLint DestroySync(EGLDisplay, EGLSyncKHR) { assert(false); return 0; }
using EGLFunction = void (*)();
EGLFunction eglGetProcAddress(const char* name)
{
  if (std::strcmp(name, "eglCreateSyncKHR") == 0)
    return reinterpret_cast<EGLFunction>(CreateSync);
  if (std::strcmp(name, "eglClientWaitSyncKHR") == 0)
    return reinterpret_cast<EGLFunction>(WaitSync);
  if (std::strcmp(name, "eglDestroySyncKHR") == 0)
    return reinterpret_cast<EGLFunction>(DestroySync);
  return nullptr;
}
'''

TEST = r'''
#include "fixture.h"
#define open MockOpen
#define close MockClose
#define ioctl MockIoctl
#include "AMLAFBCPresenter.cpp"
#undef open
#undef close
#undef ioctl

int MockIoctl(int fd, unsigned long request, ...)
{
  va_list args;
  va_start(args, request);
  void* data = va_arg(args, void*);
  va_end(args);
  if (request == GET_SESSION)
  {
    assert(fixture.framebuffers.count(fd));
    ++fixture.sessionRequests;
    if (!fixture.sessions.empty() || (fixture.failReplacementSession && fixture.sessionRequests == 2))
    {
      errno = EBUSY;
      return -1;
    }
    assert(fixture.LiveAllocations() == 0);
    ++fixture.sessionsOpened;
    const int session = fixture.nextFd++;
    fixture.sessions.emplace(session, std::vector<size_t>{});
    *static_cast<int32_t*>(data) = session;
    return 0;
  }
  assert(request == SESSION_ALLOC);
  auto& requestBuffer = *static_cast<ScanoutBuffer*>(data);
  auto& buffers = fixture.sessions.at(fd);
  assert(requestBuffer.index == buffers.size());
  fixture.currentIndex = requestBuffer.index;
  if (fixture.Fails(Failure::ALLOCATE) ||
      (fixture.failReplacementAllocation && fixture.sessionsOpened == 2 && requestBuffer.index == 1))
  {
    errno = ENOMEM;
    return -1;
  }
  const size_t allocation = fixture.allocations.size();
  fixture.allocations.emplace_back();
  buffers.push_back(allocation);
  requestBuffer.fd = fixture.nextFd++;
  requestBuffer.size = requestBuffer.width * requestBuffer.height * 4;
  fixture.exported.emplace(requestBuffer.fd, allocation);
  return 0;
}

void RunCase(const char* parameter, Failure failure, int failIndex, bool replacementAllocation,
             bool replacementSession, unsigned int expectedBuffers, int expectedSessions,
             bool allowThirdBuffer = true)
{
  fixture.AssertClean();
  fixture = Fixture{};
  fixture.failure = failure;
  fixture.failIndex = failIndex;
  fixture.failReplacementAllocation = replacementAllocation;
  fixture.failReplacementSession = replacementSession;
  {
    CAMLAFBCPresenter presenter;
    presenter.SetQueuedFlipsParameter(parameter);
    assert(presenter.BeginFrame("/mock/fb0", 3840, 2160, 24, 8, allowThirdBuffer) ==
           (expectedBuffers > 0));
    assert(presenter.Buffers() == expectedBuffers);
    assert(fixture.LiveAllocations() == expectedBuffers);
    assert(fixture.sessionsOpened == expectedSessions);
    assert(fixture.exported.size() == expectedBuffers);
    assert(fixture.textures.size() == expectedBuffers);
    assert(fixture.fbos.size() == expectedBuffers);
    assert(fixture.sessions.size() == (expectedBuffers ? 1U : 0U));
    if (expectedBuffers)
    {
      assert(CFrameBufferObject::GetDefaultFramebuffer() != 0);
      presenter.BeginLinearFrame();
      assert(presenter.BeginFrame("/mock/fb0", 3840, 2160, 24, 8, allowThirdBuffer));
      assert(fixture.sessionsOpened == expectedSessions);
    }
    else
    {
      assert(!presenter.Failed()); // transient creation failures retain their retry policy
      assert(!presenter.BeginFrame("/mock/fb0", 3840, 2160, 24, 8, allowThirdBuffer));
      assert(fixture.sessionsOpened == expectedSessions); // backoff prevents an immediate retry
    }
    presenter.Destroy(true);
    fixture.AssertClean();
    assert(CFrameBufferObject::GetDefaultFramebuffer() == 0);
  }
  fixture.AssertClean();
}
int main(int argc, char** argv)
{
  assert(argc == 4);
  RunCase(argv[1], Failure::NONE, 2, false, false, 3, 1);
  RunCase(argv[1], Failure::NONE, 2, false, false, 2, 1, false); // forced two on a queued-flip kernel
  RunCase(argv[2], Failure::NONE, 2, false, false, 2, 1);
  RunCase(argv[3], Failure::NONE, 2, false, false, 2, 1);
  RunCase(argv[1], Failure::ALLOCATE, 2, false, false, 2, 1);
  for (Failure failure : {Failure::IMPORT, Failure::BIND, Failure::FRAMEBUFFER, Failure::GL_ERROR})
  {
    RunCase(argv[1], failure, 2, false, false, 2, 2);
    RunCase(argv[1], failure, 2, true, false, 0, 2);
    RunCase(argv[1], failure, 2, false, true, 0, 1);
  }
  for (int index : {0, 1})
    for (Failure failure : {Failure::ALLOCATE, Failure::IMPORT, Failure::BIND,
                            Failure::FRAMEBUFFER, Failure::GL_ERROR})
      RunCase(argv[1], failure, index, false, false, 0, 1);
  puts("PASS production presenter allocation fallback, independent ownership and partial teardown");
}
'''


def run(source, header, negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-afbc-allocation-') as temporary:
        out = Path(temporary)
        (out / 'fixture.h').write_text(FIXTURE)
        for name in ('guilib/AMLAFBCTexture.h', 'system_gl.h', 'EGL/egl.h', 'EGL/eglext.h',
                     'cores/VideoPlayer/VideoRenderers/FrameBufferObject.h',
                     'utils/StringUtils.h', 'utils/log.h'):
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "fixture.h"\n')
        (out / 'AMLAFBCPresenter.h').write_text(header)
        (out / 'AMLAFBCPresenter.cpp').write_text(source)
        (out / 'test.cpp').write_text(TEST)
        (out / 'yes').write_text('Y\n')
        (out / 'no').write_text('N\n')
        executable = out / 'test'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', '-I', str(out), str(out / 'test.cpp'),
                        '-o', str(executable)], check=True)
        result = subprocess.run([str(executable), str(out / 'yes'), str(out / 'no'),
                                 str(out / 'missing')], capture_output=negative, text=True,
                                timeout=20, env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr
        else:
            result.check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    directory = ROOT / 'xbmc/windowing/amlogic'
    source = (directory / 'AMLAFBCPresenter.cpp').read_text()
    header = (directory / 'AMLAFBCPresenter.h').read_text()
    run(source, header)
    if args.negative_controls:
        controls = [
            ('explicit two-buffer preference ignored',
             'const unsigned int wanted = queued && allowThirdBuffer ? MAX_BUFFERS : 2;',
             'const unsigned int wanted = (queued || allowThirdBuffer) && queued ? MAX_BUFFERS : 2;'),
            ('session ownership retained', '    close(m_session);', '    (void)m_session;'),
            ('exported ownership retained', '      close(buffer.fd);', '      (void)buffer.fd;'),
            ('import ownership retained', '    buffer.import.Release();', '    (void)buffer.import;'),
        ]
        for label, before, after in controls:
            assert before in source, label
            run(source.replace(before, after), header, negative=True)
            print('REJECTED:', label)
        pattern = r'Destroy\(true\);\s*return Create\(framebuffer, width, height, depthBits, stencilBits, false\);'
        assert len(re.findall(pattern, source)) == 1
        old_fallback = '''
      if (buffer.framebuffer) glDeleteFramebuffers(1, &buffer.framebuffer);
      if (buffer.texture) glDeleteTextures(1, &buffer.texture);
      buffer.framebuffer = buffer.texture = 0;
      buffer.import.Release();
      close(buffer.fd);
      buffer.fd = -1;
      break;'''
        run(re.sub(pattern, old_fallback, source), header, negative=True)
        print('REJECTED: third buffer retained by original session')


if __name__ == '__main__':
    main()
