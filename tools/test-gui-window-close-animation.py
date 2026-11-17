#!/usr/bin/env python3
"""Execute the production CGUIWindow close/init/animation methods with a stub group.

Extracts AfterRender, Close_Internal, Close, OnInitWindow, OnDeinitWindow,
CheckAnimation, IsAnimating, Animate and DisableAnimations from GUIWindow.cpp and
runs them over a minimal animation model (queued -> running -> finished after its
length). Scenarios follow the built-in Dim/Black screensaver lifecycle: window
manager DeInitialize() (disable animations, force close, free) and reuse without
SetDefaults(), plus animations disabled while a close animation is queued or
running. The baseline (the source before this fix) must fail the reuse scenario,
proving the harness sees the stuck close. No real skin, rendering or device run.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'xbmc/guilib/GUIWindow.cpp'
BASELINE = 'e304db550ae03855a7946963dd97a8ffde8bbbb3'
SIGNATURES = ['void CGUIWindow::AfterRender()', 'void CGUIWindow::Close_Internal(',
              'void CGUIWindow::Close(', 'void CGUIWindow::OnInitWindow()',
              'void CGUIWindow::OnDeinitWindow(', 'bool CGUIWindow::CheckAnimation(',
              'bool CGUIWindow::IsAnimating(', 'bool CGUIWindow::Animate(',
              'void CGUIWindow::DisableAnimations()']


def method(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    brace = masked.index('{', start)
    end = brace + 1
    depth = 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    # default arguments are written as /*= x*/ comments in the definitions
    return source[start:end]


FIXTURE = r'''
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
static unsigned checks = 0;
static void Check(bool value, const std::string& what)
{ ++checks; if (!value) throw std::runtime_error(what); }

enum ANIMATION_TYPE { ANIM_TYPE_WINDOW_OPEN = 1, ANIM_TYPE_WINDOW_CLOSE = -1 };
enum { SOUND_INIT = 1, SOUND_DEINIT = 2 };
enum { WINDOW_FULLSCREEN_VIDEO = 12005, WINDOW_FULLSCREEN_GAME = 12911 };
enum { GUI_MSG_WINDOW_INIT = 1, GUI_MSG_WINDOW_DEINIT = 2 };
enum { TMSG_GUI_WINDOW_CLOSE = 1 };
enum { LOGDEBUG = 0 };
using CCriticalSection = std::recursive_mutex;
struct CSingleExit { explicit CSingleExit(CCriticalSection&) {} };
struct CGUIMessage
{
  int msg, sender, control, param1;
  CGUIMessage(int m, int s, int c, int p1 = 0) : msg(m), sender(s), control(c), param1(p1) {}
  int GetMessage() const { return msg; }
  int GetParam1() const { return param1; }
};
struct CGfx { CCriticalSection lock; void lock_() {} } ;
struct CWinSystem { CCriticalSection ctx; CCriticalSection& GetGfxContext() { return ctx; } };
struct CAudioManager { unsigned played = 0; void PlayWindowSound(int, int) { ++played; } };
struct CGUI { CAudioManager audio; CAudioManager& GetAudioManager() { return audio; } };
struct CMessenger
{
  bool IsProcessThread() const { return true; }
  void SendMsg(int, int, int, void*) { throw std::runtime_error("unexpected SendMsg"); }
  void PostMsg(int, int, int, void*) { throw std::runtime_error("unexpected PostMsg"); }
};
struct CServiceBroker
{
  static CWinSystem* GetWinSystem() { static CWinSystem w; return &w; }
  static CGUI* GetGUI() { static CGUI g; return &g; }
  static CMessenger* GetAppMessenger() { static CMessenger m; return &m; }
};
struct CTransform { void Reset() {} };
struct CChild { void UpdateVisibility(void*) {} };

// Minimal CAnimation model: queued -> running (from the next Animate) -> finished.
struct Anim { int type; unsigned length; bool queued = false, running = false; unsigned start = 0; };

class CGUIControlGroup
{
public:
  virtual ~CGUIControlGroup() = default;
  std::vector<Anim> m_animations;
  std::vector<CChild*> m_children;
  CTransform m_transform;
  virtual bool CheckAnimation(ANIMATION_TYPE) { return true; }
  bool HasAnimation(ANIMATION_TYPE type)
  { for (auto& a : m_animations) if (a.type == type) return true; return false; }
  void QueueAnimation(ANIMATION_TYPE type)
  {
    if (!CheckAnimation(type)) return;
    for (auto& a : m_animations) if (a.type == type) { a.queued = true; a.running = false; }
  }
  void ResetAnimations() { for (auto& a : m_animations) a.queued = a.running = false; }
  virtual bool IsAnimating(ANIMATION_TYPE type)
  { for (auto& a : m_animations) if (a.type == type && (a.queued || a.running)) return true; return false; }
  virtual bool Animate(unsigned now)
  {
    bool changed = false;
    for (auto& a : m_animations)
    {
      if (a.queued) { a.queued = false; a.running = true; a.start = now; changed = true; }
      else if (a.running && now - a.start >= a.length) { a.running = false; changed = true; }
    }
    return changed;
  }
};

class CGUIWindow : public CGUIControlGroup
{
public:
  bool m_active = false, m_closing = false, m_hasProcessed = false, m_bAllocated = true;
  bool m_animationsEnabled = true, m_manualRunActions = false;
  unsigned deinits = 0, inits = 0;
  int GetID() const { return 97; }
  bool HasProcessed() const { return m_hasProcessed; }
  bool IsSoundEnabled() const { return true; }
  void SetInitialVisibility() {}
  void RestoreControlStates() {}
  void SaveControlStates() {}
  void RunLoadActions() {}
  void RunUnloadActions() {}
  void FreeResources(bool) {}
  void Process(unsigned now) { Animate(now); m_hasProcessed = true; }
  bool OnMessage(CGUIMessage& message)
  {
    if (message.GetMessage() == GUI_MSG_WINDOW_INIT) { ++inits; OnInitWindow(); return true; }
    if (message.GetMessage() == GUI_MSG_WINDOW_DEINIT) { ++deinits; OnDeinitWindow(message.GetParam1()); return true; }
    return false;
  }
  void AfterRender();
  void Close_Internal(bool forceClose = false, int nextWindowID = 0, bool enableSound = true);
  void Close(bool forceClose = false, int nextWindowID = 0, bool enableSound = true, bool bWait = true);
  void OnInitWindow();
  void OnDeinitWindow(int nextWindowID);
  bool CheckAnimation(ANIMATION_TYPE animType) override;
  bool IsAnimating(ANIMATION_TYPE animType) override;
  bool Animate(unsigned int currentTime) override;
  void DisableAnimations();
};

// The built-in Dim window: no XML, programmatic 1000 ms fades.
struct CDimWindow : CGUIWindow
{
  CDimWindow()
  {
    m_animations.push_back({ANIM_TYPE_WINDOW_OPEN, 1000});
    m_animations.push_back({ANIM_TYPE_WINDOW_CLOSE, 1000});
  }
};
'''

DRIVER = r'''
static unsigned now = 1000;
static void Frame(CGUIWindow& w, unsigned step = 16) { now += step; w.Process(now); w.AfterRender(); }
static void Open(CGUIWindow& w) { CGUIMessage init(GUI_MSG_WINDOW_INIT, 0, 0); w.OnMessage(init); }
// CGUIWindowManager::DeInitialize() on an active window
static void DeInitialize(CGUIWindow& w) { w.DisableAnimations(); w.Close(true); w.FreeResources(true); }
static void RunFrames(CGUIWindow& w, unsigned ms) { for (unsigned t = 0; t < ms; t += 16) Frame(w); }

static void Normal()
{
  CDimWindow w; Open(w); RunFrames(w, 1200);
  w.Close(); Frame(w);
  Check(w.m_active && w.m_closing, "normal: close animation runs (still on screen mid-fade)");
  RunFrames(w, 1100);
  Check(!w.m_active && w.deinits == 1, "normal: window deinitialised after the 1000 ms fade");
}

static void ReuseAfterDeInitialize()
{
  CDimWindow w; Open(w); RunFrames(w, 1200);
  DeInitialize(w);
  Check(!w.m_active && w.deinits == 1, "reuse: DeInitialize force-closed the window");
  Open(w); RunFrames(w, 1200);
  Check(w.m_animationsEnabled, "reuse: reopened window has animations enabled");
  w.Close(); Frame(w);
  Check(w.m_active, "reuse: close fades (not instant) after reuse");
  RunFrames(w, 1100);
  Check(!w.m_active && w.deinits == 2, "reuse: second close completes (no stuck black overlay)");
}

static void DisabledWhileQueued()
{
  CDimWindow w; Open(w); RunFrames(w, 1200);
  w.Close();
  w.DisableAnimations();
  Frame(w);
  Check(!w.m_active && !w.m_closing, "queued: close completes once animations are disabled");
}

static void DisabledWhileRunning()
{
  CDimWindow w; Open(w); RunFrames(w, 1200);
  w.Close(); RunFrames(w, 300);
  Check(w.m_active && w.m_closing, "running: fade in progress");
  w.DisableAnimations();
  Frame(w);
  Check(!w.m_active && !w.m_closing && w.deinits == 1, "running: close completes once, when disabled");
  RunFrames(w, 1200);
  Check(w.deinits == 1, "running: no second deinit");
}

static void CloseWhileDisabled()
{
  CDimWindow w; Open(w); RunFrames(w, 1200);
  w.DisableAnimations();
  w.Close();
  Frame(w);
  Check(!w.m_active && !w.m_closing, "disabled: ordinary close does not wait for an animation that cannot run");
}

int main()
{
  int failed = 0;
  for (auto test : {Normal, ReuseAfterDeInitialize, DisabledWhileQueued, DisabledWhileRunning,
                    CloseWhileDisabled})
  {
    try { test(); }
    catch (const std::exception& e) { std::cout << "FAIL: " << e.what() << "\n"; ++failed; }
  }
  std::cout << checks << " checks, " << failed << " failed scenarios\n";
  return failed ? 1 : 0;
}
'''


SCENARIO_FAILED = 1  # the driver's own exit code; sanitizers are given different ones
SANITIZER_ENV = {'ASAN_OPTIONS': 'exitcode=86', 'UBSAN_OPTIONS': 'exitcode=87:print_stacktrace=1'}
REUSE_FAILURE = 'FAIL: reuse: reopened window has animations enabled'


def build_and_run(source, label):
    """Returns (exit code, stdout); anything on stderr or a crash is reported as such."""
    code = FIXTURE
    for signature in SIGNATURES:
        code += method(source, signature) + '\n'
    code += DRIVER
    with tempfile.TemporaryDirectory() as tmp:
        cpp = os.path.join(tmp, 'test.cpp')
        exe = os.path.join(tmp, 'test')
        Path(cpp).write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Werror',
                        '-Wno-unused-variable', '-fsanitize=address,undefined',
                        '-fno-sanitize-recover=all', cpp, '-o', exe], check=True)
        result = subprocess.run([exe], capture_output=True, text=True,
                                env={**os.environ, **SANITIZER_ENV})
    print(f'[{label}] exit {result.returncode}\n{result.stdout}', end='')
    if result.stderr:
        print(f'[{label}] stderr:\n{result.stderr}', end='')
    if result.returncode not in (0, SCENARIO_FAILED) or result.stderr:
        raise SystemExit(f'{label}: crashed or sanitizer error (exit {result.returncode}), '
                         'not a scenario result')
    return result.returncode, result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default=BASELINE,
                        help='git revision whose GUIWindow.cpp must fail (negative control)')
    args = parser.parse_args()
    current = (ROOT / SOURCE).read_text()
    if build_and_run(current, 'current')[0] != 0:
        raise SystemExit('current source failed')
    baseline = subprocess.run(['git', '-C', str(ROOT), 'show', f'{args.baseline}:{SOURCE}'],
                              check=True, capture_output=True, text=True).stdout
    status, output = build_and_run(baseline, f'baseline {args.baseline}')
    if status == 0:
        raise SystemExit('negative control passed: the harness does not detect the stuck close')
    if REUSE_FAILURE not in output.splitlines():
        raise SystemExit('negative control failed without the stuck reuse close')
    print('OK: current passes, baseline fails on the stuck reuse close')


if __name__ == '__main__':
    main()
