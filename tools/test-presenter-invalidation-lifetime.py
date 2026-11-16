#!/usr/bin/env python3
"""Invalidated credit history does not invent steps at startup or after resume."""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
cadence = runpy.run_path(str(ROOT / "tools/test-aml-presenter-cadence.py"))
TEST = r"""
int main(){
 for(bool restore:{false,true})for(bool advance:{false,true}){
  TestClock::now={};currentEpoch=2;
  CAMLPresenter* presenter=nullptr;int selections=0,ticks=0;
  auto held=std::make_shared<Frame>();held->epoch=1;held->advanceCount=4;
  auto next=std::make_shared<Frame>();next->epoch=2;next->pts=0;
  CAMLPresenter::Hooks hooks;
  hooks.start=[]{return true;};hooks.finish=[]{};
  hooks.adjustClock=[](double){};
  hooks.timing=[&]{
   if(++ticks>12)presenter->m_stop=true;
   return CAMLPresenter::Timing{0,1,24,0,false,true,advance,4};
  };
  hooks.selected=[&](double){++selections;assert(presenter->m_current->advanceCount==4);};
  CAMLPresenter q(4,hooks);presenter=&q;q.Show(true);
  q.Restore(restore?held:nullptr,{next},-1);q.Authorized();
  onWait=[&]{auto control=q.PendingControl();if(control.frame)assert(q.CompleteControl(control));};
  q.Run();onWait={};assert(selections==1 && q.Skipped()==0);q.TakeFrames();
 }
 // Clock resumes before a replacement arrives: stale paused credits expire.
 {
  CAMLPresenter::Hooks hooks;hooks.adjustClock=[](double){};
  CAMLPresenter q(4,hooks);q.m_invalidatedAdvanceCount=1;
  q.Select({0,1,24,0,false,false,true,4});
  auto next=std::make_shared<Frame>();next->pts=0;q.m_queue.push_back(next);
  q.Show(true);q.Select({0,1,24,0,false,true,true,4});
  assert(q.m_current==next && next->advanceCount==4);
  q.Stop();q.TakeFrames();
 }
 std::puts("PASS: invalidation/startup without outstanding credits and resume expire stale history");
}
"""

if __name__ == "__main__":
    header = (ROOT / "xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h").read_text()
    header = header.replace("std::chrono::steady_clock::now()", "TestClock::Now()")
    header = header.replace("std::condition_variable m_changed;", "TestCondition m_changed;")
    header = header.replace("\nprivate:", "\npublic:")
    with tempfile.TemporaryDirectory(prefix="presenter-invalidation-lifetime-") as directory:
        out = Path(directory)
        (out / "Presenter.h").write_text(header)
        (out / "test.cpp").write_text(cadence["HARNESS"].split("void steady(")[0] + TEST)
        subprocess.run([os.environ.get("CXX", "g++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-pthread", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie",
                        "-I" + str(ROOT / "xbmc"), "-I" + str(out), str(out / "test.cpp"),
                        "-o", str(out / "test")], check=True)
        subprocess.run([str(out / "test")], check=True, timeout=20)
