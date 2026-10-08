#!/usr/bin/env python3
"""Production presenter transitions with deterministic clock/thread interleavings.

Models main-thread control acknowledgement and a seek crossing the clock read.
Does not model the decoder, kernel acquisition, HDMI or device acceptance.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--negative-controls', action='store_true')
args = parser.parse_args()
header = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h').read_text()
header = (header.replace('std::chrono::steady_clock::now()', 'Clock::Now()')
          .replace('std::condition_variable m_changed;', 'Condition m_changed;')
          .replace('\nprivate:', '\npublic:'))
HARNESS = r'''
#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <cassert>
#include <iostream>
using namespace std::chrono_literals;
struct Clock {
  static inline std::chrono::steady_clock::time_point now{};
  static auto Now() { return now; }
  static double Us() {
    return std::chrono::duration<double, std::micro>(now.time_since_epoch()).count();
  }
};
static std::function<void()> service;
static std::function<void()> beforeWait;
struct Condition {
  void notify_all() {}
  template<class L, class P> void wait(L&, P p) { assert(p()); }
  template<class L, class D, class P> bool wait_for(L&, D, P p) { return p(); }
  template<class L, class T, class P> bool wait_until(L& lock, T deadline, P p) {
    if (beforeWait) {
      auto callback = std::move(beforeWait); beforeWait = {};
      lock.unlock(); callback(); lock.lock();
    }
    if (p()) return true;
    lock.unlock();
    if (service) service();
    lock.lock();
    if (p()) return true;
    Clock::now = deadline;
    return p();
  }
};
#include "Presenter.h"
static unsigned currentEpoch;
struct Frame : CAMLPresenter::Frame {
  bool submitted = false;
  std::function<void()> poll;
  Submission Submit(int&) override {
    if (epoch != currentEpoch) return Submission::INVALIDATED;
    if (submitted) return Submission::REUSED;
    submitted = true;
    return Submission::SUBMITTED;
  }
  bool Poll() override { if (poll) poll(); return true; }
  bool Retire() override { return true; }
};
static void control(bool delayed, bool missing, bool stale, bool early = false) {
  Clock::now = {}; currentEpoch = 1;
  CAMLPresenter::Hooks hooks;
  hooks.start = [] { return true; }; hooks.finish = [] {};
  hooks.adjustClock = [](double) {};
  hooks.timing = [] { return CAMLPresenter::Timing{Clock::Us(), 1, 24, 0, false}; };
  CAMLPresenter q(4, hooks);
  auto frame = std::make_shared<Frame>(); frame->epoch = currentEpoch;
  double pollUs = -1;
  frame->poll = [&] { pollUs = Clock::Us(); q.m_stop = true; };
  auto serial = q.Reserve(0ms); assert(serial && q.Publish(serial, frame));
  unsigned waits = 0;
  service = [&] {
    ++waits; assert(waits <= 3);
    auto request = q.PendingControl(); assert(request.frame);
    if (missing) {
      assert(pollUs == -1);
      if (waits == 3) q.m_stop = true;
    } else {
      if (delayed) Clock::now += 5ms;
      if (stale && waits == 1) {
        q.SetControl(request.generation + 1);
        assert(!q.CompleteControl(request));
      } else assert(q.CompleteControl(request));
    }
  };
  if (early) { beforeWait = service; service = {}; }
  q.Show(true); q.Authorized(); q.Run();
  if (missing) assert(waits == 3 && pollUs == -1 && Clock::Us() > 83000);
  else if (stale) assert(waits == 2 && pollUs >= 41666 && pollUs < 42000);
  else assert(waits == 1 && pollUs == (delayed ? 5000 : 0));
  q.TakeFrames(); service = {}; beforeWait = {};
  std::cout << "PASS control delayed=" << delayed << " missing=" << missing
            << " stale=" << stale << " receipt_before_wait=" << early
            << " first_poll_us=" << pollUs << "\n";
}
static void seek(bool duringSample) {
  Clock::now = {}; currentEpoch = 1;
  CAMLPresenter* q = nullptr; unsigned calls = 0;
  double offset = 10000000; bool changed = false;
  CAMLPresenter::Hooks hooks;
  hooks.start = [] { return true; }; hooks.finish = [] {};
  hooks.adjustClock = [](double) {};
  auto publish = [&](double pts) {
    auto frame = std::make_shared<Frame>(); frame->epoch = currentEpoch; frame->pts = pts;
    auto serial = q->Reserve(0ms); assert(serial && q->Publish(serial, frame));
  };
  auto transition = [&] {
    const double phase = q->m_syncOffset, error = q->m_error;
    const int samples = q->m_samples;
    q->Show(false); q->Discard(); ++currentEpoch; offset -= 6000000;
    for (int i = 0; i < 5; ++i) publish(offset + Clock::Us() + i * 1e6 / 24);
    q->Show(true); changed = true;
    assert(q->m_syncOffset == phase && q->m_samples == samples && q->m_error == error);
  };
  hooks.timing = [&] {
    if (calls == 4 && !duringSample) transition();
    double sampled = offset + Clock::Us();
    if (calls == 0) for (int i = 0; i < 5; ++i) publish(sampled + i * 1e6 / 24);
    // Model the worker being descheduled after GetClock, while another thread
    // completes flush, publishes the new epoch and makes it visible on resync.
    if (calls == 4 && duringSample) transition();
    if (++calls == 10) q->m_stop = true;
    return CAMLPresenter::Timing{sampled, 1, 24, 0, false};
  };
  CAMLPresenter presenter(16, hooks); q = &presenter;
  q->m_syncOffset = 12345; q->m_samples = 7; q->m_error = 9000;
  service = [&] { auto c = q->PendingControl(); if (c.frame) assert(q->CompleteControl(c)); };
  presenter.Show(true); presenter.Authorized(); presenter.Run();
  assert(changed && presenter.Skipped() == 0 && presenter.Observe().epoch == 2);
  presenter.TakeFrames(); service = {};
  std::cout << "PASS seek crossing timing read=" << duringSample << " no stale-clock skips\n";
}
static void recovery() {
  Clock::now = {}; currentEpoch = 1;
  CAMLPresenter::Hooks hooks;
  hooks.start = [] { return true; }; hooks.finish = [] {};
  hooks.adjustClock = [](double) {};
  // Observed first-frame/master offset and display latency, with an illustrative
  // IRQ phase. This demonstrates causality, not exact replay of a reporter seek.
  hooks.timing = [] { return CAMLPresenter::Timing{Clock::Us() - 190000, 1,
                                                 24000.0 / 1001, 255000, false}; };
  CAMLPresenter q(16, hooks);
  struct Paced : Frame { bool PollPaces() const override { return true; } };
  auto first = std::make_shared<Paced>(); first->epoch = 1; first->force = true;
  first->poll = [] { Clock::now = std::max(Clock::now,
                                         std::chrono::steady_clock::time_point(15ms)); };
  auto serial = q.Reserve(0ms); assert(serial && q.Publish(serial, first));
  bool acknowledged = false;
  service = [&] {
    assert(!acknowledged); acknowledged = true;
    Clock::now += 5ms;
    auto control = q.PendingControl(); assert(q.CompleteControl(control));
    for (int i = 1; i <= 5; ++i) {
      auto f = std::make_shared<Paced>(); f->epoch = 1; f->pts = i * 1e6 * 1001 / 24000;
      f->poll = [&] { q.m_stop = true; };
      auto id = q.Reserve(0ms); assert(id && q.Publish(id, f));
    }
    q.Show(true);
  };
  q.Authorized(); q.Run();
  assert(acknowledged && q.Skipped() == 0);
  q.TakeFrames(); service = {};
  std::cout << "PASS first post-resync queue avoids control-delay catch-up skip\n";
}
int main(int argc, char** argv) {
  if (argc > 1 && std::string(argv[1]) == "recovery") recovery();
  else if (argc > 1) { seek(false); seek(true); }
  else { control(false, false, false); control(true, false, false);
         control(false, true, false); control(false, false, true);
         control(false, false, false, true); recovery(); }
}
'''


def run(source, variant, mode, failure=None):
    with tempfile.TemporaryDirectory(prefix='presenter-transition-') as tmp:
        out = Path(tmp)
        (out / 'Presenter.h').write_text(source)
        (out / 'test.cpp').write_text(HARNESS)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I' + str(ROOT / 'xbmc'), '-I' + str(out),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test'), *mode], text=True, capture_output=True,
                                timeout=20, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        if failure:
            assert result.returncode != 0 and failure in result.stderr, (variant, result)
            print('REJECTED ' + variant)
        else:
            print(result.stdout, end='')
            if result.returncode:
                print(result.stderr)
            result.check_returncode()


run(header, 'production control', [])
run(header, 'production seek', ['seek'])
if args.negative_controls:
    needle = '(waitingForControl && !m_pending.frame)'
    assert header.count(needle) == 1
    run(header.replace(needle, '(false && waitingForControl && !m_pending.frame)'),
        'ignored control acknowledgement', [], 'waits == 1 && pollUs')
    run(header.replace(needle, '(false && waitingForControl && !m_pending.frame)'),
        'control delay causes a recovery skip', ['recovery'], 'q.Skipped() == 0')
    needle = 'if (timingGeneration != m_timingGeneration)'
    assert header.count(needle) == 1
    run(header.replace(needle, 'if (false && timingGeneration != m_timingGeneration)'),
        'stale timing across seek', ['seek'], 'presenter.Skipped() == 0')
