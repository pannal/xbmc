#!/usr/bin/env python3
"""Execute the production close and destructor of PAPlayer and CExternalPlayer.

The application retires a player after the close completes, and creates a newer
player. A thread that still holds the retired player destroys the player later.
The destructor of a closed player must keep the shared cache of the newer player;
the destructor of an unclosed player still closes the player.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PRELUDE = r'''
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <string>
using namespace std::chrono_literals;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "Assertion failed: %s at line %d\n", #c, __LINE__); std::abort(); } } while (0)

struct CDataCacheCore {
  float speed = 1.0f;
  int resets = 0;
  // the production reset restores the default state of every player
  void Reset() { speed = 1.0f; ++resets; }
};
struct CActiveAE { void KeepConfiguration(int) {} };
struct CServiceBroker {
  static CDataCacheCore& GetDataCacheCore() { static CDataCacheCore c; return c; }
  static CActiveAE* GetActiveAE() { static CActiveAE a; return &a; }
};
struct CApplication {
  struct ScopedFrameMoveGuardExit { explicit ScopedFrameMoveGuardExit(CApplication&) {} };
};
CApplication g_application;
struct CCriticalSection : std::recursive_mutex {};
struct CEvent { bool Wait(std::chrono::milliseconds) { return true; } };
struct CGUIDialog {
  bool active = true;
  bool IsActive() const { return active; }
  void Close() { active = false; }
};
int g_stops = 0;

class PAPlayer {
public:
  ~PAPlayer();
  bool CloseFile(bool reopen = false);
  void SoftStop(bool, bool) {}
  void CloseAllStreams(bool) {}
  void StopThread(bool) { ++g_stops; }
  bool m_isPaused = false;
  int m_jobCounter = 0;
  CCriticalSection m_streamsLock;
  CEvent m_jobEvent;
  bool m_closed = false;
};

class CExternalPlayer {
public:
  ~CExternalPlayer();
  bool CloseFile(bool reopen = false);
  bool m_bAbortRequest = false;
  bool m_bIsPlaying = true;
  CGUIDialog* m_dialog = nullptr;
};

@PRODUCTION@

template<class Player> void closed_player() {
  auto& cache = CServiceBroker::GetDataCacheCore();
  int resets = 0;
  {
    Player p;
    CHECK(p.CloseFile());  // the application completes the close and retires the player
    resets = cache.resets;
    cache.speed = 0.0f;    // the newer player pauses
    CHECK(p.CloseFile());  // a repeated close
  }                        // the late destructor
  CHECK(cache.speed == 0.0f && cache.resets == resets);
  cache.speed = 1.0f;
}

template<class Player> void unclosed_player() {
  auto& cache = CServiceBroker::GetDataCacheCore();
  const int resets = cache.resets;
  { Player p; }
  CHECK(cache.resets == resets + 1);
}

void pa_closed() { closed_player<PAPlayer>(); }
void pa_unclosed() {
  const int stops = g_stops;
  unclosed_player<PAPlayer>();
  CHECK(g_stops == stops + 1);  // the destructor stops the thread of the player
}
void external_closed() { closed_player<CExternalPlayer>(); }
void external_unclosed() {
  CGUIDialog dialog;
  {
    CExternalPlayer p;
    p.m_dialog = &dialog;
  }
  CHECK(!dialog.active);  // the destructor closes the dialog of the player
  unclosed_player<CExternalPlayer>();
}

#define CASES(X) X(pa_closed) X(pa_unclosed) X(external_closed) X(external_unclosed)
int main(int argc, char** argv) {
#define ENTRY(name) if (std::string(argv[1]) == #name) { name(); return 0; }
  if (argc > 1)
    CASES(ENTRY)
  return 2;
}
'''


def production():
    pa = (ROOT / 'xbmc/cores/paplayer/PAPlayer.cpp').read_text()
    external = (ROOT / 'xbmc/cores/ExternalPlayer/ExternalPlayer.cpp').read_text()
    body = '\n'.join([function(pa, 'PAPlayer::~PAPlayer()'), function(pa, 'bool PAPlayer::CloseFile('),
                      function(external, 'CExternalPlayer::~CExternalPlayer()'),
                      function(external, 'bool CExternalPlayer::CloseFile(')])
    return PRELUDE.replace('@PRODUCTION@', body)


# Each control removes one new check from the production text. A rig that still
# passes would not prove the check.
CONTROLS = [
    ('a repeated PAPlayer close resets the cache', '  if (m_closed)\n    return true;\n', ''),
    ('a completed PAPlayer close is not recorded', '  m_closed = true;\n', ''),
    ('the PAPlayer destructor does not close', 'PAPlayer::~PAPlayer()\n{\n  CloseFile();\n', 'PAPlayer::~PAPlayer()\n{\n'),
    ('a repeated external close resets the cache', '  if (m_bAbortRequest)\n    return true;\n', ''),
    ('the external destructor does not close', 'CExternalPlayer::~CExternalPlayer()\n{\n  CloseFile();\n',
     'CExternalPlayer::~CExternalPlayer()\n{\n'),
]
CASES = ['pa_closed', 'pa_unclosed', 'external_closed', 'external_unclosed']


def run(code, expect_failure=False, label=''):
    with tempfile.TemporaryDirectory(prefix='player-close-cache-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        failures = {}
        for name in CASES:
            result = subprocess.run([str(out / 'test'), name], capture_output=True, text=True, timeout=20)
            if result.returncode != 0:
                assert any(sign in result.stderr for sign in ('Assertion', 'runtime error:', 'AddressSanitizer')), \
                    result.stderr
                failures[name] = result.stderr.strip().splitlines()[-1]
        if expect_failure:
            assert failures, 'negative control passed: ' + label
            print('Negative control rejected at runtime:', label)
        else:
            print('case results:', 'PASS' if not failures else 'FAIL')
        for name, message in failures.items():
            print('  FAIL', name, '->', message)
        assert expect_failure or not failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = production()
    run(code)
    header = (ROOT / 'xbmc/cores/paplayer/PAPlayer.h').read_text()
    assert 'bool m_closed = false;' in header, 'PAPlayer must declare m_closed'
    if args.negative_controls:
        for label, before, after in CONTROLS:
            assert before in code, label
            run(code.replace(before, after, 1), True, label)
    print('Player close cache: PASS (production PAPlayer and CExternalPlayer close; ASan/UBSan)')


if __name__ == '__main__':
    main()
