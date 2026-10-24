#!/usr/bin/env python3
"""Host regression for the AML shared texture upload context.

Extracts the production upload-context methods of CEGLContextUtils
(xbmc/utils/EGLUtils.cpp), the CWinSystemAmlogicGLESContext hooks and
CTexture::LoadToGPUAsync, and runs them on real threads against a recording
EGL fixture. The fixture enforces the EGL rules the code relies on: a context
is current on one thread at a time, a context is not destroyed while another
thread has it current, and eglMakeCurrent without a surface needs
EGL_KHR_surfaceless_context.

Cases: surfaceless binding, the 1x1 pbuffer fallback, no usable surface (the
render thread keeps uploading, with no error logged per texture), concurrent
loaders, DestroyContext while a loader uploads, and binding after destruction.
The baseline must fail, the fixed source must pass (also under TSan), and five
destructive source mutations must be rejected. Host fixtures only: no Mali
driver, GPU or CE device behaviour is verified here.
"""
import argparse
from pathlib import Path
import re
import subprocess

EGL_SOURCE = 'xbmc/utils/EGLUtils.cpp'
EGL_HEADER = 'xbmc/utils/EGLUtils.h'
AML_SOURCE = 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp'
TEXTURE_SOURCE = 'xbmc/guilib/Texture.cpp'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def block(source, signature):
    text = function(source, signature)
    return text + ';'


EGL_METHODS = ['bool CEGLContextUtils::CreateContext(', 'void CEGLContextUtils::DestroyContext()',
               'bool CEGLContextUtils::BindTextureUploadContext()',
               'bool CEGLContextUtils::UnbindTextureUploadContext()',
               'bool CEGLContextUtils::HasContext()']
FIXED_EGL_METHODS = ['bool CEGLContextUtils::CreateTextureUploadSurface()',
                     'bool CEGLContextUtils::HasTextureUploadContext() const']
AML_METHODS = ['bool CWinSystemAmlogicGLESContext::BindTextureUploadContext()',
               'bool CWinSystemAmlogicGLESContext::UnbindTextureUploadContext()',
               'bool CWinSystemAmlogicGLESContext::HasContext()']

PRELUDE = r'''
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <future>
#include <initializer_list>
#include <map>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

// ---- EGL fixture -------------------------------------------------------
typedef int EGLint;
typedef unsigned EGLBoolean;
typedef unsigned EGLenum;
typedef void* EGLDisplay;
typedef void* EGLContext;
typedef void* EGLSurface;
typedef void* EGLConfig;
typedef void (*PFNEGLSETDAMAGEREGIONKHRPROC)();
#define EGL_TRUE 1u
#define EGL_FALSE 0u
#define EGL_NONE 0x3038
#define EGL_SUCCESS 0x3000
#define EGL_BAD_ACCESS 0x3002
#define EGL_BAD_CONTEXT 0x3006
#define EGL_BAD_MATCH 0x3009
#define EGL_WIDTH 0x3057
#define EGL_HEIGHT 0x3056
#define EGL_CONTEXT_PRIORITY_LEVEL_IMG 0x3100
#define EGL_CONTEXT_PRIORITY_HIGH_IMG 0x3101
#define EGL_CONTEXT_PRIORITY_MEDIUM_IMG 0x3102
#define EGL_CONTEXT_FLAGS_KHR 0x30FC
#define EGL_CONTEXT_OPENGL_DEBUG_BIT_KHR 0x1
#define EGL_NO_DISPLAY ((EGLDisplay)0)
#define EGL_NO_CONTEXT ((EGLContext)0)
#define EGL_NO_SURFACE ((EGLSurface)0)
#define EGL_NO_CONFIG_KHR ((EGLConfig)0)

namespace fixture
{
std::mutex lock;
std::set<std::string> extensions;
bool pbuffer = true;
std::map<std::thread::id, EGLContext> current;
std::map<std::thread::id, EGLSurface> currentSurface;
std::set<EGLContext> live;
std::set<EGLSurface> surfaces;
std::vector<std::string> violations;
uintptr_t next = 0x100;
thread_local EGLint error = EGL_SUCCESS;
void Violation(const std::string& what)
{
  violations.push_back(what);
}
bool CurrentElsewhere(EGLContext context)
{
  for (const auto& entry : current)
    if (entry.second == context && entry.first != std::this_thread::get_id())
      return true;
  return false;
}
} // namespace fixture

EGLContext eglCreateContext(EGLDisplay, EGLConfig, EGLContext share, const EGLint*)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  if (share != EGL_NO_CONTEXT && !fixture::live.count(share))
    fixture::Violation("share with a dead context");
  EGLContext context = reinterpret_cast<EGLContext>(fixture::next += 0x10);
  fixture::live.insert(context);
  return context;
}
EGLBoolean eglDestroyContext(EGLDisplay, EGLContext context)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  if (!fixture::live.erase(context))
    fixture::Violation("destroy of an unknown context");
  if (fixture::CurrentElsewhere(context))
    fixture::Violation("context destroyed while current on another thread");
  return EGL_TRUE;
}
EGLBoolean eglMakeCurrent(EGLDisplay, EGLSurface draw, EGLSurface read, EGLContext context)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  const auto self = std::this_thread::get_id();
  if (context == EGL_NO_CONTEXT)
  {
    fixture::current.erase(self);
    fixture::currentSurface.erase(self);
    return EGL_TRUE;
  }
  if (!fixture::live.count(context))
  {
    fixture::Violation("bind of a destroyed context");
    fixture::error = EGL_BAD_CONTEXT;
    return EGL_FALSE;
  }
  if (draw != read)
    fixture::Violation("draw and read surfaces differ");
  if (draw == EGL_NO_SURFACE && !fixture::extensions.count("EGL_KHR_surfaceless_context"))
  {
    fixture::error = EGL_BAD_MATCH;
    return EGL_FALSE;
  }
  if (draw != EGL_NO_SURFACE && !fixture::surfaces.count(draw))
    fixture::Violation("bind to a destroyed surface");
  if (fixture::CurrentElsewhere(context))
  {
    fixture::Violation("context current on two threads");
    fixture::error = EGL_BAD_ACCESS;
    return EGL_FALSE;
  }
  fixture::current[self] = context;
  fixture::currentSurface[self] = draw;
  return EGL_TRUE;
}
EGLContext eglGetCurrentContext()
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  const auto entry = fixture::current.find(std::this_thread::get_id());
  return entry == fixture::current.end() ? EGL_NO_CONTEXT : entry->second;
}
EGLSurface eglCreatePbufferSurface(EGLDisplay, EGLConfig, const EGLint* attribs)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  int width = -1, height = -1;
  for (const EGLint* a = attribs; *a != EGL_NONE; a += 2)
  {
    if (a[0] == EGL_WIDTH)
      width = a[1];
    if (a[0] == EGL_HEIGHT)
      height = a[1];
  }
  if (width != 1 || height != 1)
    fixture::Violation("upload pbuffer is not 1x1");
  if (!fixture::pbuffer)
  {
    fixture::error = EGL_BAD_MATCH;
    return EGL_NO_SURFACE;
  }
  EGLSurface surface = reinterpret_cast<EGLSurface>(fixture::next += 0x10);
  fixture::surfaces.insert(surface);
  return surface;
}
EGLBoolean eglDestroySurface(EGLDisplay, EGLSurface surface)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  if (!fixture::surfaces.erase(surface))
    fixture::Violation("destroy of an unknown surface");
  for (const auto& entry : fixture::currentSurface)
    if (entry.second == surface && entry.first != std::this_thread::get_id())
      fixture::Violation("surface destroyed while current on another thread");
  return EGL_TRUE;
}
EGLint eglGetError()
{
  EGLint value = fixture::error;
  fixture::error = EGL_SUCCESS;
  return value;
}
EGLBoolean eglQueryContext(EGLDisplay, EGLContext, EGLint, EGLint* value)
{
  *value = EGL_CONTEXT_PRIORITY_HIGH_IMG;
  return EGL_TRUE;
}
void (*eglGetProcAddress(const char*))()
{
  return nullptr;
}

struct CEGLUtils
{
  static bool HasExtension(EGLDisplay, std::string const& name)
  {
    std::lock_guard<std::mutex> lock(fixture::lock);
    return fixture::extensions.count(name) != 0;
  }
  static void Log(int, std::string const&) {}
};

// ---- Kodi fixture ------------------------------------------------------
enum { LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR };
std::atomic<int> errorLogs{0};
struct CLog
{
  template<typename... Args>
  static void Log(int level, const char*, Args&&...)
  {
    if (level == LOGERROR)
      ++errorLogs;
  }
  template<typename... Args>
  static void LogF(int level, const char*, Args&&...)
  {
    if (level == LOGERROR)
      ++errorLogs;
  }
};
using CCriticalSection = std::recursive_mutex;
'''

CLASSES = r'''
class CEGLContextUtils
{
public:
  bool CreateContext(CEGLAttributesVec contextAttribs);
  void DestroyContext();
  bool BindTextureUploadContext();
  bool UnbindTextureUploadContext();
  bool HasContext();
  bool HasTextureUploadContext() const;

  EGLDisplay m_eglDisplay{reinterpret_cast<EGLDisplay>(0x1)};
  EGLContext m_eglContext{EGL_NO_CONTEXT};
  EGLConfig m_eglConfig{reinterpret_cast<EGLConfig>(0x2)};
  EGLContext m_eglUploadContext{EGL_NO_CONTEXT};
  EGLSurface m_eglUploadSurface{EGL_NO_SURFACE};
  mutable CCriticalSection m_textureUploadLock;
  PFNEGLSETDAMAGEREGIONKHRPROC m_eglSetDamageRegionKHR{nullptr};
  bool m_partialUpdateSupport{false};
  bool m_bufferAgeSupport{false};

private:
  bool CreateTextureUploadSurface();
};

class CWinSystemBase
{
public:
  virtual ~CWinSystemBase() = default;
  virtual bool BindTextureUploadContext() { return false; }
  virtual bool UnbindTextureUploadContext() { return false; }
  virtual bool HasContext() { return false; }
};

class CWinSystemAmlogicGLESContext : public CWinSystemBase
{
public:
#ifndef BASELINE
  bool BindTextureUploadContext() override;
  bool UnbindTextureUploadContext() override;
  bool HasContext() override;
#endif
  CEGLContextUtils m_pGLContext;
};

CWinSystemAmlogicGLESContext* g_winSystem = nullptr;
struct CServiceBroker
{
  static CWinSystemBase* GetWinSystem() { return g_winSystem; }
};

// The texture's GL work: LoadToGPU blocks while `hold` is set, so a test can
// act while an upload is in progress.
struct CTexture
{
  std::atomic<bool> hold{false};
  std::atomic<bool> uploading{false};
  std::atomic<bool> uploaded{false};
  std::atomic<bool> synced{false};
  EGLContext uploadContext{EGL_NO_CONTEXT};
  void LoadToGPU()
  {
    uploadContext = eglGetCurrentContext();
    uploading = true;
    while (hold)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    uploaded = uploadContext != EGL_NO_CONTEXT;
  }
  void SyncGPU() { synced = uploaded.load(); }
  void LoadToGPUAsync();
};
'''

MAIN = r'''
static int failures = 0;
static void Check(bool value, const char* message)
{
  if (!value)
  {
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
  }
}

static void Reset(std::set<std::string> extensions, bool pbuffer)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  fixture::extensions = std::move(extensions);
  fixture::pbuffer = pbuffer;
  fixture::current.clear();
  fixture::currentSurface.clear();
  fixture::live.clear();
  fixture::surfaces.clear();
  fixture::violations.clear();
  errorLogs = 0;
}

static void Violations(const char* name)
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  for (const auto& violation : fixture::violations)
    std::fprintf(stderr, "FAIL: %s: %s\n", name, violation.c_str());
  failures += static_cast<int>(fixture::violations.size());
}

static bool LiveResources()
{
  std::lock_guard<std::mutex> lock(fixture::lock);
  return !fixture::live.empty() || !fixture::surfaces.empty();
}

// Upload on a loader thread through the production hook.
static void UploadOnLoader(CTexture& texture)
{
  std::thread([&texture] { texture.LoadToGPUAsync(); }).join();
}

static void Surfaceless()
{
  Reset({"EGL_KHR_surfaceless_context"}, false);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "surfaceless: create context");
  CTexture texture;
  UploadOnLoader(texture);
  Check(texture.uploaded, "surfaceless: texture uploaded on the loader thread");
  Check(texture.synced, "surfaceless: upload finished before the loader returns");
  Check(texture.uploadContext == win.m_pGLContext.m_eglUploadContext,
        "surfaceless: upload used the shared upload context");
  Check(win.m_pGLContext.m_eglUploadSurface == EGL_NO_SURFACE, "surfaceless: no pbuffer created");
  {
    std::lock_guard<std::mutex> lock(fixture::lock);
    Check(fixture::current.empty(), "surfaceless: loader released the upload context");
  }
  win.m_pGLContext.DestroyContext();
  Check(!LiveResources(), "surfaceless: everything destroyed");
  Check(errorLogs == 0, "surfaceless: no error logged");
  Violations("surfaceless");
}

static void PbufferFallback()
{
  Reset({}, true);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "pbuffer: create context");
  Check(win.m_pGLContext.m_eglUploadSurface != EGL_NO_SURFACE, "pbuffer: upload pbuffer created");
  CTexture texture;
  UploadOnLoader(texture);
  Check(texture.uploaded, "pbuffer: texture uploaded on the loader thread");
  win.m_pGLContext.DestroyContext();
  Check(!LiveResources(), "pbuffer: context and pbuffer destroyed");
  Check(errorLogs == 0, "pbuffer: no error logged");
  Violations("pbuffer");
}

static void NoSurface()
{
  Reset({}, false);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "no surface: primary context still created");
#ifndef BASELINE
  Check(!win.m_pGLContext.HasTextureUploadContext(), "no surface: no upload context reported");
#endif
  for (int i = 0; i < 3; ++i)
  {
    CTexture texture;
    UploadOnLoader(texture);
    Check(!texture.uploading, "no surface: render thread keeps the upload");
  }
  Check(errorLogs == 0, "no surface: no error logged per texture");
  win.m_pGLContext.DestroyContext();
  Check(!LiveResources(), "no surface: everything destroyed");
  Violations("no surface");
}

static void ConcurrentLoaders()
{
  Reset({"EGL_KHR_surfaceless_context"}, false);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "concurrent: create context");
  std::vector<CTexture> textures(16);
  std::vector<std::thread> loaders;
  for (auto& texture : textures)
    loaders.emplace_back([&texture] { texture.LoadToGPUAsync(); });
  for (auto& loader : loaders)
    loader.join();
  bool all = true;
  for (auto& texture : textures)
    all = all && texture.uploaded;
  Check(all, "concurrent: every loader uploaded");
  win.m_pGLContext.DestroyContext();
  Violations("concurrent");
}

static void DestroyDuringUpload()
{
  Reset({"EGL_KHR_surfaceless_context"}, false);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "destroy: create context");
  // the loader binds through CEGLContextUtils directly: the baseline has no AML hook
  CTexture texture;
  texture.hold = true;
  std::atomic<bool> bound{false};
  std::thread loader([&] {
    bound = win.m_pGLContext.BindTextureUploadContext();
    if (!bound)
    {
      texture.uploading = true;
      return;
    }
    texture.LoadToGPU();
    win.m_pGLContext.UnbindTextureUploadContext();
  });
  while (!texture.uploading)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  std::atomic<bool> destroyed{false};
  std::thread shutdown([&] {
    win.m_pGLContext.DestroyContext();
    destroyed = true;
  });
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  Check(bound, "destroy: loader bound the upload context");
  Check(!destroyed, "destroy: waits for the upload in progress");
  texture.hold = false;
  loader.join();
  shutdown.join();
  Check(destroyed && !LiveResources(), "destroy: completes after the upload");

  // binding after destruction fails without keeping the lock
  CTexture late;
  UploadOnLoader(late);
  Check(!late.uploading, "destroy: no upload after destruction");
  auto relock = std::async(std::launch::async, [&] {
    std::unique_lock<CCriticalSection> lock(win.m_pGLContext.m_textureUploadLock);
  });
  Check(relock.wait_for(std::chrono::seconds(2)) == std::future_status::ready,
        "destroy: upload lock released after a failed bind");
  Violations("destroy");
}

static void PrimaryThread()
{
  Reset({"EGL_KHR_surfaceless_context"}, false);
  CWinSystemAmlogicGLESContext win;
  g_winSystem = &win;
  Check(win.m_pGLContext.CreateContext({}), "primary: create context");
  EGLSurface window = reinterpret_cast<EGLSurface>(0x9);
  {
    std::lock_guard<std::mutex> lock(fixture::lock);
    fixture::surfaces.insert(window);
  }
  eglMakeCurrent(win.m_pGLContext.m_eglDisplay, window, window, win.m_pGLContext.m_eglContext);
  CTexture texture;
  texture.LoadToGPUAsync();
  Check(!texture.uploading, "primary: render thread does not rebind to the upload context");
  Check(eglGetCurrentContext() == win.m_pGLContext.m_eglContext, "primary: context untouched");
  eglMakeCurrent(win.m_pGLContext.m_eglDisplay, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
  win.m_pGLContext.DestroyContext();
  Violations("primary");
}

int main()
{
  Surfaceless();
  PbufferFallback();
  NoSurface();
  ConcurrentLoaders();
  DestroyDuringUpload();
  PrimaryThread();
  if (failures)
    return 1;
  std::printf("6 cases: surfaceless, pbuffer, no surface, concurrent loaders, "
              "destroy during upload, primary thread\n");
  return 0;
}
'''


def sources(egl, header, aml, texture, baseline):
    attributes = re.search(r'class CEGLAttributesVec\n\{[\s\S]*?\n\};\n', header).group()
    parts = [PRELUDE, attributes, CLASSES]
    methods = EGL_METHODS + ([] if baseline else FIXED_EGL_METHODS)
    parts += [function(egl, signature) for signature in methods]
    if baseline:
        # the baseline has neither: keep the class shape, never used to pass
        parts.append('bool CEGLContextUtils::CreateTextureUploadSurface() { return true; }')
        parts.append('bool CEGLContextUtils::HasTextureUploadContext() const { return false; }')
    else:
        parts += [function(aml, signature) for signature in AML_METHODS]
    parts.append(function(texture, 'void CTexture::LoadToGPUAsync()'))
    parts.append(MAIN)
    return '\n'.join(parts)


def run_case(out, name, cpp, baseline=False, sanitizer=None):
    path = out / (name + '.cpp')
    path.write_text(cpp)
    executable = out / name
    argv = ['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
            '-Wno-unused-parameter', '-pthread', '-DHAS_LIBAMCODEC']
    if baseline:
        argv.append('-DBASELINE')
    if sanitizer:
        argv.append('-fsanitize=' + sanitizer)
    argv += [str(path), '-o', str(executable)]
    compiled = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if compiled.returncode:
        raise RuntimeError(name + ' did not compile:\n' + compiled.stderr)
    return subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)


MUTATIONS = {
    # DestroyContext does not wait for an upload in progress
    'destroy-without-lock': (EGL_SOURCE,
        r'(void CEGLContextUtils::DestroyContext\(\)\n\{\n[^\n]*\n)\s*std::unique_lock<CCriticalSection> lock\(m_textureUploadLock\);',
        r'\1'),
    # the upload context is bound without the pbuffer
    'bind-without-surface': (EGL_SOURCE,
        r'eglMakeCurrent\(m_eglDisplay, m_eglUploadSurface, m_eglUploadSurface, m_eglUploadContext\)',
        'eglMakeCurrent(m_eglDisplay, EGL_NO_SURFACE, EGL_NO_SURFACE, m_eglUploadContext)'),
    # an upload context without a surface is kept
    'keep-unbindable-context': (EGL_SOURCE,
        r'else if \(!CreateTextureUploadSurface\(\)\)', 'else if (!CreateTextureUploadSurface() && false)'),
    # the AML hook binds without asking first: one error logged per texture
    'ungated-aml-bind': (AML_SOURCE,
        r'  if \(!m_pGLContext\.HasTextureUploadContext\(\)\)\n    return false;\n', ''),
    # the AML hook is not wired to the upload context
    'unwired-aml-bind': (AML_SOURCE,
        r'return m_pGLContext\.BindTextureUploadContext\(\);', 'return false;'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('/tmp/aml-texture-upload-context'))
    parser.add_argument('--baseline', default='0aef062ae7d1d2c9002f35fb5d11ab3bb5fcdd21')
    parser.add_argument('--no-tsan', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def show(rev, path):
        return subprocess.run(['git', 'show', rev + ':' + path], cwd=args.root,
                              capture_output=True, text=True, check=True).stdout

    files = [EGL_SOURCE, EGL_HEADER, AML_SOURCE, TEXTURE_SOURCE]
    old = [show(args.baseline, path) for path in files]
    original = run_case(args.output, 'baseline', sources(*old, baseline=True), baseline=True)
    assert original.returncode != 0, original.stdout
    assert 'surfaceless: texture uploaded on the loader thread' in original.stderr, original.stderr
    assert 'context destroyed while current on another thread' in original.stderr, original.stderr
    print('Baseline FAIL reproduced: AML never uploads off the render thread; '
          'DestroyContext races an upload in progress', flush=True)

    current = {path: (args.root / path).read_text() for path in files}
    header = current[EGL_HEADER]
    assert 'bool HasTextureUploadContext() const;' in header, 'header lacks HasTextureUploadContext'
    assert 'EGLSurface m_eglUploadSurface{EGL_NO_SURFACE};' in header, 'header lacks the upload surface'
    fixed_cpp = sources(*(current[path] for path in files), baseline=False)
    fixed = run_case(args.output, 'fixed', fixed_cpp)
    assert fixed.returncode == 0, (fixed.stdout, fixed.stderr)
    print('Fixed PASS: ' + fixed.stdout.strip(), flush=True)
    if not args.no_tsan:
        tsan = run_case(args.output, 'fixed-tsan', fixed_cpp, sanitizer='thread')
        assert tsan.returncode == 0 and 'ThreadSanitizer' not in tsan.stderr, tsan.stderr
        print('Fixed PASS under ThreadSanitizer', flush=True)

    for name, (path, pattern, replacement) in MUTATIONS.items():
        mutated = dict(current)
        mutated[path], count = re.subn(pattern, replacement, current[path])
        assert count == 1, 'mutation did not apply: ' + name
        bad = run_case(args.output, 'negative-' + name,
                       sources(*(mutated[p] for p in files), baseline=False))
        assert bad.returncode != 0, 'destructive mutation survived: ' + name
        print('Rejected mutation: ' + name, flush=True)
    print('AML texture upload context: PASS; baseline fails, fixed passes, %d mutations '
          'rejected; host fixtures, not Mali/GPU/device verification' % len(MUTATIONS))


if __name__ == '__main__':
    main()
