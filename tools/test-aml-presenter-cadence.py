#!/usr/bin/env python3
"""Run actual presenter scheduling/selection with deterministic clock/wait substitutes.

No OS scheduling, native driver, audio-clock discipline or target/HDMI proof.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''

#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <cassert>
#include <iostream>
#include <cmath>
using namespace std::chrono_literals;
struct TestClock {
 static inline std::chrono::steady_clock::time_point now{};
 static auto Now(){return now;}
 static double Us(){return std::chrono::duration<double,std::micro>(now.time_since_epoch()).count();}
 static void Add(double us){now+=std::chrono::nanoseconds(std::llround(us*1000));}
};
static double oversleep=100;
static std::function<void()> onWait;
static std::function<void(std::chrono::steady_clock::time_point)> inspectDeadline;
static bool stalled=false;
static double stallAt=0;
static uint64_t currentEpoch=1;
struct TestCondition {
 void notify_all(){}
 template<class L,class P> void wait(L&,P p){assert(p());}
 template<class L,class D,class P> bool wait_for(L&,D,P p){return p();}
 template<class L,class T,class P> bool wait_until(L& lock,T target,P p){
  lock.unlock();if(inspectDeadline)inspectDeadline(target);if(onWait)onWait();
  if(TestClock::now<target)TestClock::now=target;
  TestClock::Add(oversleep);lock.lock();return p();
 }
};
#include "Presenter.h"
struct Frame:CAMLPresenter::Frame {
 bool submitted=false;
 Submission Submit(int&) override {TestClock::Add(10);if(epoch!=currentEpoch)return Submission::INVALIDATED;if(submitted)return Submission::REUSED;submitted=true;return Submission::SUBMITTED;}
 bool Poll() override {
  if(stallAt>0&&!stalled&&TestClock::Us()>=stallAt){TestClock::Add(250000);stalled=true;}
  TestClock::Add(5);return true;
 }
 bool Retire() override {TestClock::Add(2);return true;}
};

void steady(double refresh, bool sync, CAMLPresenter::Method method, bool reset=false, bool stall=false)
{
 TestClock::now={};int baseline=-1;uint64_t produced=0;CAMLPresenter* q=nullptr;
 currentEpoch=1;double seekOffset=0;
 stalled=false;stallAt=stall?60000000:0;bool resetDone=false,refreshDone=false;
 bool checkReset=false,afterStall=false;double tickBegan=0;
 int recoverySkips=-1;
 const double framePeriod=1e6*1001/24000;
 CAMLPresenter::Hooks h;h.start=[] {return true;};h.finish=[]{};h.adjustClock=[](double){};
 h.timing=[&]{
  tickBegan=TestClock::Us();TestClock::Add(5);const double now=TestClock::Us();
  if(reset&&!resetDone&&now>=20000000){
   q->Discard();++currentEpoch;seekOffset=30000000;
   produced=static_cast<uint64_t>(std::ceil((now+seekOffset)/framePeriod));
   q->ResetClock();resetDone=true;checkReset=true;
  }
  if(reset&&!refreshDone&&now>=40000000){refresh=24000.0/1001;refreshDone=true;checkReset=true;}
  while(produced*framePeriod<now+seekOffset+5*framePeriod){
   auto frame=std::make_shared<Frame>();frame->pts=produced++*framePeriod;frame->epoch=currentEpoch;frame->method=method;
   const auto id=q->Reserve(0ms);assert(id);assert(q->Publish(id,frame));
  }
  assert(q->Outstanding()<=16);
  if(now>=2000000&&baseline<0)baseline=q->Skipped();
  if(stall&&now>=62000000&&recoverySkips<0)recoverySkips=q->Skipped();
  if(now>=180000000)q->m_stop=true;
  return CAMLPresenter::Timing{now+seekOffset,1,refresh,0,sync};
 };
 CAMLPresenter presenter(16,h);q=&presenter;
 onWait=[&]{auto c=q->PendingControl();if(c.frame)assert(q->CompleteControl(c));};
 inspectDeadline=[&](auto target){
  const double targetUs=std::chrono::duration<double,std::micro>(target.time_since_epoch()).count();
  if(checkReset){assert(std::abs(targetUs-tickBegan-1e6/refresh)<0.002);checkReset=false;}
  if(stalled&&afterStall)assert(targetUs>tickBegan); // no expired timer slots replayed at entry
  if(stalled)afterStall=true;
 };
 presenter.Show(true);presenter.Authorized();presenter.Run();
 assert(presenter.State()==CAMLPresenter::Phase::STOPPED);
 auto stats=presenter.Diagnostics();
 auto skips=presenter.TakeSkipEpisodes();
 assert(presenter.TakeSkipEpisodes().empty());
 if(!reset&&!stall){
  std::cout<<"refresh="<<refresh<<" sync="<<sync<<" method="<<int(method)
           <<" steady_skipped="<<presenter.Skipped()-baseline<<" wakeMax="<<stats.wakeLateMaxUs<<"\n";
  assert(presenter.Skipped()==baseline);
  assert(stats.missedDeadlines==0&&stats.wakeLateMaxUs==100);
 }
 if(stall){
  assert(stalled&&stats.missedDeadlines>=4&&stats.loopGapMaxUs>=250000);
  assert(presenter.Skipped()>baseline&&presenter.Skipped()==recoverySkips);
  bool found=false;
  for(const auto& event:skips){
   if(event.loopGapUs>=250000){
    assert(event.render-event.pts>62000&&event.queued>2&&event.count>0);
    assert(event.epoch==1&&event.timing.refresh==refresh&&event.wakeLateUs>200000);found=true;
   }
  }
  assert(found);
 }
 if(reset){assert(resetDone&&refreshDone&&stats.missedDeadlines==0&&presenter.Observe().epoch==2);}
 presenter.TakeFrames();onWait={};inspectDeadline={};
}
void bounded_events()
{
 CAMLPresenter::Hooks h;h.adjustClock=[](double){};
 CAMLPresenter q(16,h);q.Show(true);
 // Exercise the production late-selection branch, independently of Run's clock.
 for(int i=0;i<12;++i){
  q.m_queue.clear();
  for(int j=0;j<6;++j){auto f=std::make_shared<Frame>();f->pts=j*41708;f->epoch=i+1;q.m_queue.push_back(f);}
  q.Select({1000000,1,24000.0/1001,0,false});
 }
 const auto events=q.TakeSkipEpisodes();
 assert(events.size()==8&&q.m_lostSkipEpisodes==4);
 assert(events.front().serial==5&&events.back().serial==12);
 for(const auto& e:events)assert(e.count==4&&e.queued==6&&e.render-e.pts==1000000&&e.serial==e.epoch);
 assert(q.TakeSkipEpisodes().empty());q.TakeFrames();
}
int main(){
 steady(24000.0/1001,false,CAMLPresenter::Method::SINGLE);
 steady(24000.0/1001,true,CAMLPresenter::Method::SINGLE);
 steady(48000.0/1001,true,CAMLPresenter::Method::SINGLE);
 steady(48000.0/1001,true,CAMLPresenter::Method::BOB);
 steady(60000.0/1001,false,CAMLPresenter::Method::SINGLE);
 steady(60,false,CAMLPresenter::Method::SINGLE);
 steady(60,false,CAMLPresenter::Method::SINGLE,true);
 steady(24000.0/1001,true,CAMLPresenter::Method::SINGLE,false,true);
 bounded_events();
 std::cout<<"PASS cadence/reset/refresh/stall recovery and bounded skip events\n";
}
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--negative-control',action='store_true')
    args=parser.parse_args()
    original=(ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h').read_text()
    # Only the platform time/wait backend and private test access are replaced.
    source=original.replace('std::chrono::steady_clock::now()', 'TestClock::Now()')
    source=source.replace('std::condition_variable m_changed;', 'TestCondition m_changed;')
    source=source.replace('\nprivate:', '\npublic:')
    with tempfile.TemporaryDirectory(prefix='presenter-cadence-') as tmp:
        out=Path(tmp);(out/'test.cpp').write_text(HARNESS)
        def run(header, negative=False):
            (out/'Presenter.h').write_text(header)
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread',
                            '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(ROOT/'xbmc'),
                            '-I'+str(out),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            result=subprocess.run([str(out/'test')],capture_output=negative,text=True,timeout=20)
            if negative:
                assert result.returncode!=0 and 'presenter.Skipped()==baseline' in result.stderr, result
                print('Rejected negative control: rebasing every deadline accumulates steady-playback skips')
            else:
                result.check_returncode()
        run(source)
        if args.negative_control:
            needle='          nextTick += period;'
            assert source.count(needle)==1
            run(source.replace(needle,'          nextTick = iterationStart; // restored deadline drift\n'+needle),True)


if __name__=='__main__':
    main()
