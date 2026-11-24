#!/usr/bin/env python3
"""The Amlogic window system's frame hook selects the upload priority.

Compiles the production AML render admission and AFBC frame hooks with the
production presenter callback, a recording EGL context and a mock presenter.
Menu routing is stubbed out. Successful handoffs keep upload eligibility and
rejected render entry, linear, rejected present and abandoned frames clear it.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'xbmc/windowing/amlogic/WinSystemAmlogicGLESContext'


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
#include <functional>
#include <memory>
#include <string>
#include <utility>
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
struct CAMLAFBCPresenter
{
  explicit CAMLAFBCPresenter(std::function<void(bool)> callback = {})
    : setUploadEligibility(std::move(callback))
  {
  }

  bool begin = true;
  unsigned int buffers = 3;
  unsigned int present = 1;
  unsigned int Present() { return present; }
  bool Failed() const { return false; }
  std::function<void(bool)> setUploadEligibility;
  bool BeginFrame(const std::string&, int, int, int, int)
  {
    if (begin && setUploadEligibility)
      setUploadEligibility(buffers >= 3);
    return begin;
  }
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
struct VideoMuteController
{
  unsigned int HandoffGeneration() const { return 0; }
};
static VideoMuteController aml_video_mute_controller() { return {}; }
class CRenderSystemGLES
{
public:
  bool primary = true;
  bool IsPrimaryContextCurrent() const { return primary; }
  bool CanRender() const { return IsPrimaryContextCurrent(); }
  bool BeginRender() { return CanRender(); }
};
class CWinSystemAmlogicGLESContext : public CRenderSystemGLES
{
public:
  bool BeginRender();
  void BeginAFBCScanoutFrame();
  void AbandonAFBCScanoutFrame();
  void PresentAFBCForFixture();
  void MarkGuiDirty() {}
  void TakeAFBCOutcome() {}
  void GuiABBegin() {}
  void GuiABFinish(const char*) {}
  unsigned int m_videoMuteHandoff = 0;
  bool HasPendingKernelSwitch() const { return false; }
  CGraphicContext gfx;
  CGraphicContext& GetGfxContext() { return gfx; }
  bool m_afbcFrame = false;
  bool m_afbcDirtyRegionsReported = false;
  int m_vncGuiViewer = -1;
  MenuRoute m_menuRoute = MenuRoute::NONE;
  GuiAB m_guiAB;
  int m_nWidth = 3840, m_nHeight = 2160;
  EGLContextUtils m_pGLContext;
@PRESENTER_MEMBER@
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
  Check(!w.m_afbcFrame && w.m_pGLContext.high, "successful present: still eligible");
  const int callsAfterPresent = w.m_pGLContext.calls;
  Check(w.BeginRender() && w.m_pGLContext.high &&
            w.m_pGLContext.calls == callsAfterPresent + 1,
        "healthy render entry: no priority reset before the next AFBC frame");
  w.PresentAFBCForFixture();
  w.primary = false;
  Check(!w.BeginRender() && !w.m_afbcFrame && !w.m_pGLContext.high,
        "rejected render entry after successful present: medium");
  w.primary = true;
  Check(w.BeginRender() && w.m_afbcFrame && w.m_pGLContext.high,
        "render entry after recovery: high");
  w.PresentAFBCForFixture();
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


def presenter_member(header):
    declaration = '  CAMLAFBCPresenter m_afbcPresenter'
    start = header.index(declaration)
    semicolon = header.index(';', start)
    if '{' in header[start:semicolon]:
        return function(header, declaration).rstrip() + ';'
    return header[start:semicolon + 1]


def build(source, header):
    parts = [PRELUDE.replace('@PRESENTER_MEMBER@', presenter_member(header))]
    begin = function(source, 'bool CWinSystemAmlogicGLESContext::BeginRender()')
    # Keep render admission and frame setup verbatim, skipping unrelated menu routing.
    parts.append(begin[:begin.index('  // Outside fullscreen video')] +
                 begin[begin.index('  BeginAFBCScanoutFrame();'):])
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
    source = SOURCE.with_suffix('.cpp').read_text()
    header = SOURCE.with_suffix('.h').read_text()
    cases = {'actual': (source, header, None)}
    mutations = {
        'stale-on-render-entry-rejection': (
            '  if (!CanRender())\n  {\n    m_pGLContext.SetTextureUploadHighPriority(false);\n'
            '    return CRenderSystemGLES::BeginRender();\n  }',
            '  if (!CanRender())\n    return CRenderSystemGLES::BeginRender();',
            'rejected render entry after successful present: medium'),
        'stale-on-early-return': ('  {\n    m_pGLContext.SetTextureUploadHighPriority(false);\n    return;\n  }',
                                  '  {\n    return;\n  }', 'primary context not current: medium'),
        'stale-on-abandon': ('  m_pGLContext.SetTextureUploadHighPriority(false);\n  if (!m_afbcFrame)\n    return;',
                             '  if (!m_afbcFrame)\n    return;\n  m_pGLContext.SetTextureUploadHighPriority(false);',
                             'skipped frame without active target: medium'),
        'clear-on-success': ('    presented = afbcFrame != 0;',
                             '    m_pGLContext.SetTextureUploadHighPriority(false);\n    presented = afbcFrame != 0;',
                             'successful present: still eligible'),
        'stale-on-linear': ('    m_pGLContext.SetTextureUploadHighPriority(false);\n    m_afbcPresenter.BeginLinearFrame();',
                            '    m_afbcPresenter.BeginLinearFrame();',
                            'presenter refused the frame (linear): medium'),
        'stale-on-rejection': ('      m_pGLContext.SetTextureUploadHighPriority(false);\n      MarkGuiDirty();',
                               '      MarkGuiDirty();', 'rejected present: medium'),
    }
    for name, (before, after, witness) in mutations.items():
        assert source.count(before) == 1, name
        cases[name] = (source.replace(before, after), header, witness)
    cases['disconnected-presenter'] = (
        source, header.replace(presenter_member(header), '  CAMLAFBCPresenter m_afbcPresenter;'),
        'three buffers: high-priority uploads')
    with tempfile.TemporaryDirectory(prefix='aml-upload-priority-frame-') as directory:
        for name, (text, window_header, witness) in cases.items():
            cpp, binary = Path(directory) / (name + '.cpp'), Path(directory) / name
            cpp.write_text(build(text, window_header))
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
    print('PASS upload priority frame hook: healthy handoffs preserve eligibility and fallback clears it')


if __name__ == '__main__':
    main()
