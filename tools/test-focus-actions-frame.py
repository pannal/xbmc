#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Execute the production FrameMove input statements with a recording GUI.

Input focuses a Home widget row that still holds its remembered item. The row's
focus actions restore the previous row's column with two Control.Move actions,
queued as thread messages and evaluated when they run, as Kodi does. The frame
must process controls with the restored item, never the remembered one.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
static void Check(bool ok, const char* message)
{
  if (!ok) { std::fprintf(stderr, "FAIL %s\n", message); std::exit(1); }
}
struct Row { int selected; int Position() const { return selected; } };
static Row upper{1}, lower{0};       // B is selected above; the lower row remembers C.
static int listposition = 0;        // Window(home).Property(listposition)
static int processedSelection = -1;
struct WindowManager
{
  std::deque<std::function<void()>> thread;
  int GetActiveWindowOrDialog() const { return 10000; }
  void SendThreadMessage(std::function<void()> message) { thread.push_back(std::move(message)); }
  void DispatchThreadMessages()
  {
    while (!thread.empty())
    {
      auto message = std::move(thread.front());
      thread.pop_front();
      message();
    }
  }
  void Process() { processedSelection = lower.selected; }
};
static WindowManager windows;
struct GUI { WindowManager& GetWindowManager() { return windows; } };
struct InputManager
{
  void Process(int window, float)
  {
    Check(window == 10000, "input goes to the active window");
    // Down from B: the window unfocuses the upper row and focuses the lower
    // one, which keeps its remembered item. Their actions are queued; $INFO is
    // resolved when each action runs.
    windows.SendThreadMessage([] { listposition = upper.Position(); });
    windows.SendThreadMessage([] { lower.selected -= lower.Position(); });
    windows.SendThreadMessage([] { lower.selected += listposition; });
  }
};
struct CServiceBroker
{
  static GUI* GetGUI() { static GUI gui; return &gui; }
  static InputManager& GetInputManager() { static InputManager input; return input; }
};
static unsigned portEvents = 0;
static void HandlePortEvents() { ++portEvents; }
static void FrameMoveInput(float frameTime)
{
@STATEMENTS@
}
int main()
{
  FrameMoveInput(0.016f);
  Check(portEvents == 1, "port events are handled once");
  windows.Process();
  Check(processedSelection == 1, "the frame processes the restored column, not the remembered item");
  Check(windows.thread.empty(), "focus actions queued by input have run");
  std::puts("PASS focus actions queued by input run before the frame processes controls");
  return 0;
}
'''


def statements(source):
    start = source.index('    HandlePortEvents();')
    end = source.index('GetWindowManager().DispatchThreadMessages();', start)
    end = source.index('\n', end) + 1
    return source[start:end]


def run(directory, name, code):
    cpp = directory / (name + '.cpp')
    cpp.write_text(code)
    binary = directory / name
    subprocess.run(['c++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(cpp), '-o',
                    str(binary)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True,
                          env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})


def main():
    source = (ROOT / 'xbmc/application/Application.cpp').read_text()
    block = statements(source)
    dispatch = '    CServiceBroker::GetGUI()->GetWindowManager().DispatchThreadMessages();\n'
    assert block.count(dispatch) == 1
    controls = {
        'no-dispatch': block.replace(dispatch, ''),
        'dispatch-before-input': dispatch + block.replace(dispatch, ''),
    }
    with tempfile.TemporaryDirectory(prefix='focus-actions-frame-') as temporary:
        directory = Path(temporary)
        result = run(directory, 'production', FIXTURE.replace('@STATEMENTS@', block))
        if result.returncode != 0:
            sys.exit('production failed: ' + result.stdout + result.stderr)
        print(result.stdout.strip())
        for name, text in controls.items():
            result = run(directory, name, FIXTURE.replace('@STATEMENTS@', text))
            if result.returncode != 1 or 'the frame processes the restored column' not in result.stderr:
                sys.exit('control survived: ' + name + ' ' + result.stderr)
            print('PASS semantic control ' + name)


if __name__ == '__main__':
    main()
