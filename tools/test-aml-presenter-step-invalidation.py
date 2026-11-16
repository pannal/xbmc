#!/usr/bin/env python3
"""VIDEO-1: real Run/INVALIDATED/Select must retain unconsumed paused steps.

Reuses cadence's deterministic clock/wait and recording frame adapters. The
entire production presenter header executes; there is no replacement credit
algorithm or expected-failure inversion. Native decoder/IRQ is not exercised.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
cadence = runpy.run_path(str(ROOT / "tools/test-aml-presenter-cadence.py"))

TEST = r"""
int main() {
  TestClock::now={};currentEpoch=2;
  const double period=1e6*1001/24000;
  const int consumed=@CONSUMED@,requested=@REQUESTED@,direction=@DIRECTION@;
  auto held=std::make_shared<Frame>();
  held->pts=0;held->epoch=1;held->presented=true;held->advanceCount=consumed;
  std::vector<std::shared_ptr<CAMLPresenter::Frame>> queued;
  for(int i=1;i<=6;++i){
    auto frame=std::make_shared<Frame>();frame->pts=i*period;frame->epoch=2;
    queued.push_back(frame);
  }
  CAMLPresenter* presenter=nullptr;int ticks=0,selected=0;
  CAMLPresenter::Hooks hooks;
  hooks.start=[]{return true;};hooks.finish=[]{};
  hooks.adjustClock=[](double value){assert(value==0);};
  hooks.timing=[&]{
    if(++ticks>20)presenter->m_stop=true;
    return CAMLPresenter::Timing{4*period,1,24000.0/1001,100000,false,true,true,requested};
  };
  hooks.selected=[&](double){
    ++selected;
    assert(presenter->Skipped()==0 &&
           "VIDEO-1 invalidation re-enabled lateness dropping with outstanding paused steps");
    assert(presenter->m_current->advanceCount==consumed+direction*selected &&
           "VIDEO-1 replacement consumed all outstanding step credits");
    if(selected==3)presenter->m_stop=true;
  };
  CAMLPresenter q(8,hooks);presenter=&q;q.Show(true);
  q.Restore(held,queued,-1);q.Authorized();
  onWait=[&]{auto control=q.PendingControl();if(control.frame)assert(q.CompleteControl(control));};
  q.Run();onWait={};
  assert(q.State()==CAMLPresenter::Phase::STOPPED);
  assert(selected==3 && "VIDEO-1 invalidation lost requested unconsumed steps");
  assert(q.m_current==queued[2]);
  q.TakeFrames();
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direction", choices=["forward", "backward"], default="forward")
    args = parser.parse_args()
    source = (ROOT / "xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h").read_text()
    source = source.replace("std::chrono::steady_clock::now()", "TestClock::Now()")
    source = source.replace("std::condition_variable m_changed;", "TestCondition m_changed;")
    source = source.replace("\nprivate:", "\npublic:")
    prefix = cadence["HARNESS"].split("void steady(")[0]
    test = TEST.replace("@CONSUMED@", "1" if args.direction == "forward" else "4")
    test = test.replace("@REQUESTED@", "4" if args.direction == "forward" else "1")
    test = test.replace("@DIRECTION@", "1" if args.direction == "forward" else "-1")
    with tempfile.TemporaryDirectory(prefix="presenter-step-invalidation-") as directory:
        out = Path(directory)
        (out / "Presenter.h").write_text(source)
        (out / "test.cpp").write_text(prefix + test)
        subprocess.run([
            os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
            "-pthread", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie",
            "-I" + str(ROOT / "xbmc"), "-I" + str(out), str(out / "test.cpp"),
            "-o", str(out / "test"),
        ], check=True)
        subprocess.run([str(out / "test")], check=True, timeout=20)


if __name__ == "__main__":
    main()
