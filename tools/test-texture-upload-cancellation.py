#!/usr/bin/env python3
"""Run actual upload-bind methods with recording EGL and threaded lock fixtures.

Exercises cancellation before, during and after lease acquisition, plus every
platform forwarder. No real driver, device timing or complete platform build.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'upload_priority', ROOT / 'tools/test-aml-texture-upload-priority.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)
h, d = p.h, p.d
X11 = 'xbmc/windowing/X11/GLContextEGL.cpp'
WRAPPERS = {
    'CWinSystemGbmEGLContext': 'xbmc/windowing/gbm/WinSystemGbmEGLContext',
    'CWinSystemWaylandEGLContext': 'xbmc/windowing/wayland/WinSystemWaylandEGLContext',
    'CWinSystemX11GLContext': 'xbmc/windowing/X11/WinSystemX11GLContext',
    'CWinSystemX11GLESContext': 'xbmc/windowing/X11/WinSystemX11GLESContext',
}

TESTS = r'''
template<class Context> static void Contended(Context& context)
{
 lockContended=false;
 std::atomic_bool held{false},release{false},cancelled{false};
 std::thread owner([&]{
   Check(context.BindTextureUploadContext(),"owner binds");held=true;
   Await([&]{return release.load();},"owner release deadline");
   Check(context.UnbindTextureUploadContext(),"owner releases");
 });
 Await([&]{return held.load();},"owner acquisition deadline");
 bool bound=false;
 std::thread waiter([&]{
   bound=context.BindTextureUploadContext(&cancelled);
   if(bound)Check(context.UnbindTextureUploadContext(),"unexpected waiter releases");
 });
 Await([&]{return lockContended.load();},"waiter actually contends");
 cancelled.store(true,std::memory_order_release);release=true;
 owner.join();waiter.join();
 Check(!bound,"cancelled mutex waiter must not bind");
 Check(fixture::bindCalls==1&&fixture::unbindCalls==1,
       "cancelled mutex waiter performs no empty EGL bind/release");
 cancelled=false;
 Check(context.BindTextureUploadContext(&cancelled),"cancel does not disable context or retain lock");
 Check(context.UnbindTextureUploadContext(),"fresh lease releases");
}
static void Cancellation()
{
 Reset({"EGL_KHR_surfaceless_context"},false);
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"cancellation context creation");
 CTexture active;active.hold=true;
 std::thread owner([&]{active.LoadToGPUAsync();});
 Await([&]{return active.uploading.load();},"texture owner acquisition");
 lockContended=false;
 CTexture obsolete;std::atomic_bool cancelled{false};
 std::thread waiter([&]{obsolete.LoadToGPUAsync(&cancelled);});
 Await([&]{return lockContended.load();},"texture waiter actually contends");
 cancelled.store(true,std::memory_order_release);active.hold=false;
 owner.join();waiter.join();
 Check(!obsolete.uploaded&&!obsolete.synced,"cancelled texture skips GL work");
 Check(fixture::bindCalls==1&&fixture::unbindCalls==1,"texture token reaches locked bind gate");
 win.m_pGLContext.DestroyContext();Violations("texture cancellation");

 Reset({"EGL_KHR_surfaceless_context"},false);
 Check(win.m_pGLContext.CreateContext({}),"direct cancellation creation");
 Contended(win.m_pGLContext);
 win.m_pGLContext.DestroyContext();Violations("direct cancellation");

 Reset({"EGL_KHR_surfaceless_context"},false);
 Check(win.m_pGLContext.CreateContext({}),"late cancellation creation");
 cancelled=false;fixture::cancelOnBind=&cancelled;
 CTexture late;std::thread lateWorker([&]{late.LoadToGPUAsync(&cancelled);});lateWorker.join();
 fixture::cancelOnBind=nullptr;
 Check(cancelled&&!late.uploaded&&!late.synced,"post-bind cancellation still skips upload/sync");
 Check(fixture::bindCalls==1&&fixture::unbindCalls==1,"post-bind cancellation releases lease");
 CTexture fresh;UploadOnLoader(fresh);
 Check(fresh.uploaded&&fresh.synced,"post-bind cancellation preserves later upload");
 win.m_pGLContext.DestroyContext();Violations("late cancellation");

 Reset({"EGL_KHR_surfaceless_context"},false);
 Check(win.m_pGLContext.CreateContext({}),"X11 cancellation creation");
 CGLContextEGL x11;x11.m_eglDisplay=win.m_pGLContext.m_eglDisplay;
 x11.m_eglUploadContext=win.m_pGLContext.m_eglUploadContext;
 Contended(x11);
 cancelled=true;
 CWinSystemBase unsupported;
 Check(!unsupported.BindTextureUploadContext(&cancelled),"unsupported context retains fallback");
 CWinSystemX11GLContext gl;gl.m_pGLContext=&x11;
 CWinSystemX11GLESContext gles;gles.m_pGLContext=&x11;
 bool bound=static_cast<CWinSystemBase&>(gl).BindTextureUploadContext(&cancelled);
 Check(!bound,"X11 GL forwards token");if(bound)x11.UnbindTextureUploadContext();
 bound=static_cast<CWinSystemBase&>(gles).BindTextureUploadContext(&cancelled);
 Check(!bound,"X11 GLES forwards token");if(bound)x11.UnbindTextureUploadContext();
 gl.m_pGLContext=nullptr;gles.m_pGLContext=nullptr;
 Check(!gl.BindTextureUploadContext()&&!gles.BindTextureUploadContext(),"X11 unavailable fallback");
 win.m_pGLContext.DestroyContext();Violations("X11 cancellation");

 CWinSystemGbmEGLContext gbm;CWinSystemWaylandEGLContext wayland;
 for(auto* context:{&gbm.m_eglContext,&wayland.m_eglContext})
 {
   Reset({"EGL_KHR_surfaceless_context"},false);
   Check(context->CreateContext({}),"forwarder context creation");
   const unsigned before=fixture::bindCalls;
   CWinSystemBase& window=context==&gbm.m_eglContext?static_cast<CWinSystemBase&>(gbm):
                                                               static_cast<CWinSystemBase&>(wayland);
   bound=window.BindTextureUploadContext(&cancelled);
   Check(!bound,"EGL forwarder preserves cancellation");if(bound)context->UnbindTextureUploadContext();
   Check(fixture::bindCalls==before,"EGL forwarder avoids obsolete bind");
   context->DestroyContext();Violations("EGL forwarder cancellation");
 }
 std::puts("PASS contended texture/CEGL/X11, late cancellation and platform forwarding");
}
'''


def generate(files):
    source = p.generate(files)
    source = h.replace(source, 'class CCriticalSection\n{',
                       'static std::atomic_bool lockContended{false};\nclass CCriticalSection\n{')
    source = h.replace(source, 'while (!mutex.try_lock())\n  {',
                       'while (!mutex.try_lock())\n  {\n   lockContended=true;')
    source = h.replace(source, 'bool pbuffer = true;',
                       'std::atomic_bool* cancelOnBind=nullptr;\nbool pbuffer = true;')
    source = h.replace(source, '  ++fixture::bindCalls;',
                       '  ++fixture::bindCalls;\n  if(fixture::cancelOnBind)fixture::cancelOnBind->store(true);')
    extra = r'''
class CGLContextEGL
{
public:
 EGLDisplay m_eglDisplay=EGL_NO_DISPLAY;
 EGLContext m_eglUploadContext=EGL_NO_CONTEXT;
 CCriticalSection m_textureUploadLock;
 bool BindTextureUploadContext(const std::atomic_bool* cancelled=nullptr);
 bool UnbindTextureUploadContext();
};
'''
    for cls, path in WRAPPERS.items():
        header = (ROOT / (path + '.h')).read_text()
        assert 'BindTextureUploadContext(const std::atomic_bool* cancelled = nullptr) override;' in header
        field = 'CGLContextEGL* m_pGLContext=nullptr;' if '/X11/' in path else 'CEGLContextUtils m_eglContext;'
        extra += f'class {cls} : public CWinSystemBase {{public: {field}\n'
        extra += 'bool BindTextureUploadContext(const std::atomic_bool* cancelled=nullptr) override;};\n'
        extra += d.function(files[path + '.cpp'], 'bool ' + cls + '::BindTextureUploadContext(') + '\n'
    for method in ['BindTextureUploadContext(', 'UnbindTextureUploadContext()']:
        extra += d.function(files[X11], 'bool CGLContextEGL::' + method) + '\n'
    source = h.replace(source, 'int main(){try {', extra + TESTS + '\nint main(){try {\n Cancellation();')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    paths = p.PATHS + [X11] + [v + '.cpp' for v in WRAPPERS.values()]
    files = {path: (ROOT / path).read_text() for path in paths}
    cases = {'actual': files}
    gate = '  if (cancelled && cancelled->load(std::memory_order_acquire))\n    return false;\n'
    changed = dict(files)
    changed[d.EGL_SOURCE] = h.replace(changed[d.EGL_SOURCE], gate, '')
    cases['missing-gate'] = changed
    changed = dict(files)
    body = d.function(changed[d.EGL_SOURCE], 'bool CEGLContextUtils::BindTextureUploadContext(')
    moved = h.replace(body, gate, '')
    moved = h.replace(moved, '  std::unique_lock<CCriticalSection> lock(m_textureUploadLock);',
                      gate + '  std::unique_lock<CCriticalSection> lock(m_textureUploadLock);')
    changed[d.EGL_SOURCE] = h.replace(changed[d.EGL_SOURCE], body, moved)
    cases['pre-lock-only'] = changed
    drops = {'texture': (d.TEXTURE_SOURCE, 'window->BindTextureUploadContext(cancelled)'),
             'AML': (d.AML_SOURCE, 'm_pGLContext.BindTextureUploadContext(cancelled)')}
    drops.update({cls: (path + '.cpp', 'BindTextureUploadContext(cancelled)') for cls, path in WRAPPERS.items()})
    for label, (path, call) in drops.items():
        changed = dict(files)
        changed[path] = h.replace(changed[path], call, call.replace('(cancelled)', '()'))
        cases['drop-' + label] = changed
    changed = dict(files)
    changed[d.TEXTURE_SOURCE] = h.replace(changed[d.TEXTURE_SOURCE],
        'if (!cancelled || !cancelled->load(std::memory_order_acquire))', 'if (true)')
    cases['missing-post-bind-check'] = changed
    changed = dict(files)
    changed[X11] = h.replace(changed[X11],
        '  if (cancelled && cancelled->load(std::memory_order_acquire))\n  {\n'
        '    m_textureUploadLock.unlock();\n    return false;\n  }\n', '')
    cases['missing-X11-gate'] = changed
    x11_gate = ('  if (cancelled && cancelled->load(std::memory_order_acquire))\n  {\n'
                '    m_textureUploadLock.unlock();\n    return false;\n  }\n')
    changed = dict(files)
    changed[X11] = h.replace(changed[X11], x11_gate, '')
    changed[X11] = h.replace(changed[X11], '  m_textureUploadLock.lock();',
                            gate + '  m_textureUploadLock.lock();')
    cases['X11-pre-lock-only'] = changed
    changed = dict(files)
    changed[X11] = h.replace(changed[X11], x11_gate,
                             x11_gate.replace('    m_textureUploadLock.unlock();\n', ''))
    cases['missing-X11-unlock'] = changed
    witnesses = {name: 'texture token reaches locked bind gate' for name in
                 ['missing-gate', 'pre-lock-only', 'drop-texture', 'drop-AML']}
    witnesses.update({'drop-CWinSystemGbmEGLContext': 'EGL forwarder preserves cancellation',
                      'drop-CWinSystemWaylandEGLContext': 'EGL forwarder preserves cancellation',
                      'drop-CWinSystemX11GLContext': 'X11 GL forwards token',
                      'drop-CWinSystemX11GLESContext': 'X11 GLES forwards token',
                      'missing-post-bind-check': 'post-bind cancellation still skips upload/sync',
                      'missing-X11-gate': 'cancelled mutex waiter must not bind',
                      'X11-pre-lock-only': 'cancelled mutex waiter must not bind',
                      'missing-X11-unlock': 'fixture: upload lock did not retire'})
    results = {}
    for label, selected in cases.items():
        source = generate(selected)
        cpp, binary = args.output / (label + '.cpp'), args.output / label
        cpp.write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra',
                   '-Werror', '-Wno-unused-parameter', '-pthread', '-DHAS_LIBAMCODEC', str(cpp), '-o', str(binary)]
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
        subprocess.run(command, check=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                             env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        (args.output / (label + '.stdout')).write_text(run.stdout)
        (args.output / (label + '.stderr')).write_text(run.stderr)
        witness = witnesses.get(label)
        accepted = (run.returncode == 0 if witness is None else
                    run.returncode == 1 and 'FAIL: ' + witness in run.stderr)
        results[label] = {'exit_code': run.returncode, 'accepted': accepted,
                          'expected_witness': witness,
                          'source_sha256': hashlib.sha256(source.encode()).hexdigest()}
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        if not accepted:
            raise RuntimeError(f'{label}: {run.stdout}\n{run.stderr}')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
