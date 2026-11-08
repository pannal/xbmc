#!/usr/bin/env python3
"""Production CAMLCodec::SetInputEnded() against the real AML session admission.

Extracts the whole SetInputEnded body and runs the body with the production
AMLSession.h and a recording amcodec double. The end of stream is a decoder
hardware call, so the call must hold a decoder permit like AddData, GetPicture
and SetSpeed. A denied attempt must leave the end of the input pending, so the
next drain step (ContinueDrain sends the drain control on each step) retries.
Host admission test only, not a device measurement.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ADMISSION = '''  if (LifecyclePending() && !ContinueLifecycle())
    return;
  auto operation = m_session.AcquireDecoder();
  if (!operation)
    return;
'''


def function(source, signature):
    start = source.index(signature)
    level = 0
    for index in range(source.index('{', start), len(source)):
        level += (source[index] == '{') - (source[index] == '}')
        if level == 0:
            return source[start:index + 1]
    raise ValueError(signature)


PREFIX = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <cassert>
#include <chrono>
#include <cstdio>
#include <vector>
struct CLog {template<class... T> static void Log(T&&...) {}};
constexpr int LOGDEBUG=0;
struct VCodec {};
struct Private {VCodec vcodec;};
struct CAMLCodec;
struct Dll {
  CAMLCodec* codec=nullptr;
  std::vector<int> eos;
  bool displayBeganDuringEos=false;
  void codec_set_eos(VCodec*, int value);
};
struct CAMLCodec {
  CAMLSession m_session;
  Dll dll; Dll* m_dll=&dll; Private priv; Private* am_private=&priv;
  bool m_opened=true, m_inputEnded=false;
  bool lifecyclePending=false, lifecycleContinues=true;
  std::chrono::system_clock::time_point m_tp_input_ended{}, m_tp_drain_start{};
  CAMLCodec() {
    dll.codec=this;
    const auto request=m_session.Fence();
    assert(m_session.BeginMutation(request));
    assert(m_session.Complete(request,true));
  }
  bool LifecyclePending() const {return lifecyclePending;}
  bool ContinueLifecycle() {if (lifecycleContinues) lifecyclePending=false; return lifecycleContinues;}
  void SetInputEnded();
};
// A display mutation must not begin while the decoder executes the end of stream.
void Dll::codec_set_eos(VCodec*, int value) {
  const auto display=CAMLSession::FenceDisplay();
  assert(display);
  displayBeganDuringEos=CAMLSession::TryBeginDisplay(display);
  if (displayBeganDuringEos)
    assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  else
    assert(CAMLSession::CancelDisplay(display));
  eos.push_back(value);
}
'''

TESTS = r'''
int main() {
  {
    // Admitted: the end of stream runs under the decoder permit, once.
    CAMLCodec codec;
    codec.SetInputEnded();
    assert(codec.dll.eos == std::vector<int>{1} && codec.m_inputEnded);
    assert(!codec.dll.displayBeganDuringEos && "display mutation overlapped the end of stream");
    assert(codec.m_tp_input_ended != std::chrono::system_clock::time_point{});
    assert(codec.m_tp_drain_start == codec.m_tp_input_ended);
    codec.SetInputEnded();
    assert(codec.dll.eos.size() == 1);
  }
  for (bool mutating : {false, true}) {
    // A pending or active display mutation denies decoder admission. The end of
    // the input stays pending, and the next drain step sends the end of stream.
    CAMLCodec codec;
    const auto display=CAMLSession::FenceDisplay();
    assert(display);
    if (mutating)
      assert(CAMLSession::TryBeginDisplay(display));
    assert(!codec.m_session.AcquireDecoder());
    codec.SetInputEnded();
    assert(codec.dll.eos.empty() && "unadmitted end of stream");
    assert(!codec.m_inputEnded && "denied end of stream was committed");
    codec.SetInputEnded();
    assert(codec.dll.eos.empty() && !codec.m_inputEnded);
    if (mutating)
      assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
    else
      assert(CAMLSession::CancelDisplay(display));
    codec.SetInputEnded();
    assert(codec.dll.eos == std::vector<int>{1} && codec.m_inputEnded);
  }
  {
    // A decoder lifecycle that cannot continue denies the end of stream.
    CAMLCodec codec;
    codec.lifecyclePending=true; codec.lifecycleContinues=false;
    codec.SetInputEnded();
    assert(codec.dll.eos.empty() && !codec.m_inputEnded);
    codec.lifecycleContinues=true;
    codec.SetInputEnded();
    assert(codec.dll.eos == std::vector<int>{1} && codec.m_inputEnded);
  }
  {
    // A closed decoder gets no end of stream.
    CAMLCodec codec; codec.m_opened=false;
    codec.SetInputEnded();
    assert(codec.dll.eos.empty() && !codec.m_inputEnded);
  }
  std::puts("PASS: aml-input-ended");
}
'''


def harness(root):
    aml = (root / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    return PREFIX + function(aml, 'void CAMLCodec::SetInputEnded()') + '\n' + TESTS


def run(source, directory, label, root, expect_failure=False):
    path = directory / (label + '.cpp')
    path.write_text(source)
    exe = directory / label
    subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                    '-fno-omit-frame-pointer', '-I', str(root / 'xbmc'), str(path), '-o', str(exe)],
                   check=True)
    result = subprocess.run([str(exe)], text=True, capture_output=True)
    if expect_failure:
        if result.returncode == 0:
            raise AssertionError('negative control was not rejected: ' + label)
        print('REJECTED:', label)
    elif result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    else:
        print(result.stdout, end='')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness(args.root)
    with tempfile.TemporaryDirectory(prefix='aml-input-ended-') as tmp:
        directory = Path(tmp)
        run(source, directory, 'aml-input-ended', args.root)
        if args.negative_controls:
            assert ADMISSION in source
            run(source.replace(ADMISSION, '', 1), directory, 'no-decoder-admission', args.root,
                expect_failure=True)
            run(source.replace('  if (!operation)\n    return;\n', '', 1), directory,
                'denied-admission-runs', args.root, expect_failure=True)


if __name__ == '__main__':
    main()
