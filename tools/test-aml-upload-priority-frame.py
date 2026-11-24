#!/usr/bin/env python3
"""The Amlogic window system's frame hook selects the upload priority.

Compiles the production CWinSystemAmlogicGLESContext::BeginAFBCScanoutFrame and
AbandonAFBCScanoutFrame against a recording presenter and EGL context: uploads
may use the high-priority context only during AFBC frames with three display
buffers, never for two buffers, linear frames, abandoned frames or when the
primary context is not current. Mutations are negative controls.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp'


def function(source, signature):
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 0
    for i in range(brace, len(source)):
        depth += {'{': 1, '}': -1}.get(source[i], 0)
        if depth == 0:
            return source[start:i + 1] + '\n'
    raise ValueError(signature)


PRELUDE = r"""
#include <cstdio>
#include <memory>
#include <string>
typedef int EGLint;
typedef void* EGLDisplay;
typedef void* EGLConfig;
#define EGL_DEPTH_SIZE 0x3025
#define EGL_STENCIL_SIZE 0x3026
static int eglGetConfigAttrib(EGLDisplay, EGLConfig, EGLint, EGLint* value) { *value = 0; return 1; }
#define LOGINFO 1
struct CLog { template <typename... A> static void Log(int, const char*, A&&...) {} };
constexpr int DIRTYREGION_SOLVER_FILL_VIEWPORT_ALWAYS = 0;
constexpr int DIRTYREGION_SOLVER_FILL_VIEWPORT_ON_CHANGE = 3;
constexpr int WINDOW_VISUALISATION = 1, WINDOW_SCREENSAVER = 2, WINDOW_FULLSCREEN_GAME = 3;
struct CAdvancedSettings { int m_guiAlgorithmDirtyRegions = 3; bool m_guiAFBCScanout = true; };
struct CSettingsComponent
{
  std::shared_ptr<CAdvancedSettings> advanced = std::make_shared<CAdvancedSettings>();
  std::shared_ptr<CAdvancedSettings> GetAdvancedSettings() { return advanced; }
};
struct CGUIWindowManager { bool IsWindowActive(int) const { return false; } };
struct CGUIComponent { CGUIWindowManager manager; CGUIWindowManager& GetWindowManager() { return manager; } };
static CSettingsComponent g_settings;
static CGUIComponent g_gui;
struct CServiceBroker
{
  static CSettingsComponent* GetSettingsComponent() { return &g_settings; }
  static CGUIComponent* GetGUI() { return &g_gui; }
};
struct CTimeUtils { static unsigned int GetFrameTime() { return 0; } };
struct CGUIExternalRendering { static bool Recent(unsigned int) { return false; } };
enum RENDER_STEREO_MODE { RENDER_STEREO_MODE_OFF, RENDER_STEREO_MODE_SPLIT };
struct CGraphicContext { RENDER_STEREO_MODE GetStereoMode() const { return RENDER_STEREO_MODE_OFF; } };
struct Presenter
{
  bool begin = true;
  unsigned int buffers = 3;
  unsigned int present = 1;
  unsigned int Present() { return present; }
  bool Failed() const { return false; }
  bool BeginFrame(const std::string&, int, int, int, int) { return begin; }
  void BeginLinearFrame() {}
  unsigned int Buffers() const { return buffers; }
};
struct EGLContextUtils
{
  int calls = 0;
  bool high = false;
  EGLDisplay GetEGLDisplay() const { return nullptr; }
  EGLConfig GetEGLConfig() const { return nullptr; }
  void SetTextureUploadHighPriority(bool value) { high = value; ++calls; }
};
enum class MenuRoute { NONE, MENU };
struct GuiAB { enum class Phase { IDLE, RUNNING }; Phase phase = Phase::IDLE; };
class CWinSystemAmlogicGLESContext
{
public:
  void BeginAFBCScanoutFrame();
  void AbandonAFBCScanoutFrame();
  void PresentAFBCForFixture();
  void MarkGuiDirty() {}
  bool primary = true;
  bool IsPrimaryContextCurrent() const { return primary; }
  bool HasPendingKernelSwitch() const { return false; }
  CGraphicContext gfx;
  CGraphicContext& GetGfxContext() { return gfx; }
  bool m_afbcFrame = false;
  bool m_afbcDirtyRegionsReported = false;
  int m_vncGuiViewer = -1;
  Presenter m_afbcPresenter;
  MenuRoute m_menuRoute = MenuRoute::NONE;
  GuiAB m_guiAB;
  int m_nWidth = 3840, m_nHeight = 2160;
  EGLContextUtils m_pGLContext;
  std::string m_framebuffer_name = "fb0";
};
"""

MAIN = r"""
static int failures = 0;
static void Check(bool value, const char* what)
{
  if (!value)
  {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  }
}
int main()
{
  CWinSystemAmlogicGLESContext w;
  w.BeginAFBCScanoutFrame();
  Check(w.m_afbcFrame && w.m_pGLContext.high, "three buffers: high-priority uploads");
  w.m_afbcPresenter.buffers = 2;
  w.BeginAFBCScanoutFrame();
  Check(w.m_afbcFrame && !w.m_pGLContext.high, "two buffers: medium-priority uploads");
  w.m_afbcPresenter.buffers = 3;
  w.BeginAFBCScanoutFrame();
  Check(w.m_pGLContext.high, "three buffers again: high");
  w.PresentAFBCForFixture();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "successful present: medium before next frame");
  w.m_afbcPresenter.begin = false;
  w.BeginAFBCScanoutFrame();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "presenter refused the frame (linear): medium");
  w.m_afbcPresenter.begin = true;
  w.BeginAFBCScanoutFrame();
  w.m_vncGuiViewer = 1;
  w.BeginAFBCScanoutFrame();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "linear frame (VNC viewer): medium");
  w.m_vncGuiViewer = -1;
  w.BeginAFBCScanoutFrame();
  w.primary = false;
  w.BeginAFBCScanoutFrame();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "primary context not current: medium");
  w.primary = true;
  w.BeginAFBCScanoutFrame();
  Check(w.m_pGLContext.high, "AFBC frame: high");
  w.AbandonAFBCScanoutFrame();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "abandoned frame: medium");
  w.m_pGLContext.high = true;
  w.AbandonAFBCScanoutFrame();
  Check(!w.m_pGLContext.high, "skipped frame without active target: medium");
  w.BeginAFBCScanoutFrame();
  w.m_afbcPresenter.present = 0;
  w.PresentAFBCForFixture();
  Check(!w.m_afbcFrame && !w.m_pGLContext.high, "rejected present: medium");
  return failures ? 1 : 0;
}
"""


def build(source):
    parts = [PRELUDE]
    for signature in ('void CWinSystemAmlogicGLESContext::AbandonAFBCScanoutFrame()',
                      'void CWinSystemAmlogicGLESContext::BeginAFBCScanoutFrame()'):
        parts.append(function(source, signature))
    start = source.index('  const bool afbc = m_afbcFrame;')
    end = source.index('  else\n  {\n    if (m_afbcPresenter.Shown())', start)
    parts.append('void CWinSystemAmlogicGLESContext::PresentAFBCForFixture()\n{\n' + source[start:end] + '}')
    parts.append(MAIN)
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    source = (ROOT / SOURCE).read_text()
    cases = {'actual': (source, None)}
    mutations = {
        'always-high': ('m_pGLContext.SetTextureUploadHighPriority(m_afbcFrame && m_afbcPresenter.Buffers() >= 3);',
                        'm_pGLContext.SetTextureUploadHighPriority(true);', 'two buffers: medium-priority uploads'),
        'ignore-buffers': ('m_pGLContext.SetTextureUploadHighPriority(m_afbcFrame && m_afbcPresenter.Buffers() >= 3);',
                           'm_pGLContext.SetTextureUploadHighPriority(m_afbcFrame);', 'two buffers: medium-priority uploads'),
        'stale-on-early-return': ('  {\n    m_pGLContext.SetTextureUploadHighPriority(false);\n    return;\n  }',
                                  '  {\n    return;\n  }', 'primary context not current: medium'),
        'stale-on-abandon': ('  m_pGLContext.SetTextureUploadHighPriority(false);\n  if (!m_afbcFrame)\n    return;',
                             '  if (!m_afbcFrame)\n    return;\n  m_pGLContext.SetTextureUploadHighPriority(false);',
                             'skipped frame without active target: medium'),
        'stale-on-success': ('    m_pGLContext.SetTextureUploadHighPriority(false);\n    presented = afbcFrame != 0;',
                             '    m_pGLContext.SetTextureUploadHighPriority(afbcFrame != 0);\n    presented = afbcFrame != 0;',
                             'successful present: medium before next frame'),
        'stale-on-rejection': ('    m_pGLContext.SetTextureUploadHighPriority(false);\n    presented = afbcFrame != 0;',
                               '    m_pGLContext.SetTextureUploadHighPriority(afbcFrame == 0);\n    presented = afbcFrame != 0;',
                               'rejected present: medium'),
    }
    for name, (before, after, witness) in mutations.items():
        assert source.count(before) == 1, name
        cases[name] = (source.replace(before, after), witness)
    with tempfile.TemporaryDirectory(prefix='aml-upload-priority-frame-') as directory:
        for name, (text, witness) in cases.items():
            cpp, binary = Path(directory) / (name + '.cpp'), Path(directory) / name
            cpp.write_text(build(text))
            command = [os.environ.get('CXX', 'c++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wno-unused-parameter', str(cpp), '-o', str(binary)]
            if args.sanitize:
                command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
            subprocess.run(command, check=True)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            accepted = run.returncode == 0 if witness is None else run.returncode == 1 and witness in run.stderr
            print(f"{name}: {'accepted' if accepted else 'REJECTED'}" + (f' (witness: {witness})' if witness else ''))
            if not accepted:
                sys.exit(f'{name}: {run.stderr}')
    print('PASS upload priority frame hook: high only for AFBC frames with three buffers')


if __name__ == '__main__':
    main()
