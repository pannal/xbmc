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
void paused_selection()
{
 int adjustments=0;
 CAMLPresenter::Hooks h;h.adjustClock=[&](double){++adjustments;};
 CAMLPresenter q(4,h);q.Show(true);
 auto held=std::make_shared<Frame>();held->pts=0;held->presented=true;
 auto next=std::make_shared<Frame>();next->pts=41708;
 q.Restore(held,{next},-1);
 for(int i=0;i<40;++i)q.Select({1000000,1,24000.0/1001,0,true,true});
 assert(q.m_current==held&&q.m_queue.size()==1);
 assert(adjustments==0); // paused selection must not discipline the clock
 next->force=true;
 q.Select({1000000,1,24000.0/1001,0,true,true});
 assert(q.m_current==next&&q.m_queue.empty());
 q.TakeFrames();

 CAMLPresenter stepping(4,h);stepping.Show(true);
 auto stepHeld=std::make_shared<Frame>();stepHeld->pts=0;
 auto stepNext=std::make_shared<Frame>();stepNext->pts=41708;
 auto future=std::make_shared<Frame>();future->pts=83416;
 stepping.Restore(stepHeld,{stepNext,future},-1);
 const int beforeStep=adjustments;
 stepping.Select({41708,1,24000.0/1001,0,true,true,true,1});
 assert(stepping.m_current==stepNext); // actual frame advance need not set Frame::force
 for(int i=0;i<40;++i)stepping.Select({41708,1,24000.0/1001,0,true,true,true,1});
 assert(stepping.m_current==stepNext&&adjustments==beforeStep);
 stepping.TakeFrames();

 CAMLPresenter initial(4,h);initial.Show(true);
 auto first=std::make_shared<Frame>();first->pts=0;
 initial.Restore(nullptr,{first},-1);
 initial.Select({0,1,24000.0/1001,0,false,true});
 assert(initial.m_current==first); // permit a paused startup/replacement frame
 auto draining=std::make_shared<Frame>();draining->pts=41708;
 initial.m_queue.push_back(draining);
 initial.m_forceNext=true;
 initial.Select({0,1,24000.0/1001,0,false,true});
 assert(initial.m_current==draining); // preserve explicit queue draining
 initial.TakeFrames();
}
void step_credits()
{
 const double period=1e6*1001/24000;
 for(double latency:{0.,60000.,100000.})for(double offset:{0.,-60000.}){
  CAMLPresenter::Hooks h;h.adjustClock=[](double){assert(false);};
  CAMLPresenter q(8,h);q.Show(true);q.m_syncOffset=offset;
  auto held=std::make_shared<Frame>();held->pts=0;held->presented=true;
  std::vector<std::shared_ptr<CAMLPresenter::Frame>> frames;
  for(int i=1;i<=6;++i){auto f=std::make_shared<Frame>();f->pts=i*period;frames.push_back(f);}
  q.Restore(held,frames,-1);
  q.Select({0,1,24000.0/1001,latency,true,true,true,0});
  assert(q.m_current==held); // the flag precedes the actual clock advance
  q.Select({period,1,24000.0/1001,latency,true,true,true,1});
  assert(q.m_current==frames[0]&&q.Skipped()==0); // latency must not drop the requested frame
  for(int i=0;i<40;++i)q.Select({period,1,24000.0/1001,latency,true,true,true,1});
  assert(q.m_current==frames[0]);
  for(int i=0;i<40;++i)q.Select({3*period,1,24000.0/1001,latency,true,true,true,3});
  assert(q.m_current==frames[2]&&q.Skipped()==0); // two queued single-step requests
  q.TakeFrames();
 }
 CAMLPresenter::Hooks h;h.adjustClock=[](double){};
 CAMLPresenter q(8,h);q.Show(true);
 auto held=std::make_shared<Frame>();held->presented=true;
 q.Restore(held,{},-1);
 q.Select({0,1,24000.0/1001,0,false,false,false,17});
 assert(held->advanceCount==17); // baseline is recorded even with an empty queue
 q.Select({0,1,24000.0/1001,0,false,true,true,17});
 q.Select({3*period,1,24000.0/1001,0,false,true,true,20});
 assert(held->advanceCount==17); // FrameAdvance(3) before picture delivery
 std::vector<std::shared_ptr<Frame>> frames;
 for(int i=1;i<=3;++i){
  auto f=std::make_shared<Frame>();f->pts=i*period;frames.push_back(f);q.m_queue.push_back(f);
  q.Select({3*period,1,24000.0/1001,0,false,true,true,20});
  assert(q.m_current==f&&f->advanceCount==17+i);
 }
 for(int i=0;i<40;++i)q.Select({3*period,1,24000.0/1001,0,false,true,true,20});
 assert(q.m_current==frames[2]);
 q.ResetClock();q.Discard();
 auto next=std::make_shared<Frame>();next->pts=4*period;q.m_queue.push_back(next);
 q.Select({3*period,1,24000.0/1001,0,false,true,true,20});
 assert(q.m_current==frames[2]); // timing resets do not authorize another step
 q.Select({4*period,1,24000.0/1001,0,false,true,true,21});
 assert(q.m_current==next&&next->advanceCount==21);
 q.TakeFrames();
}
int main(){
  PLAYBACK_DIAGNOSTICS::SetEnabled(true);
 paused_selection();
 step_credits();
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
                assert result.returncode!=0 and 'Assertion' in result.stderr, result
                print('Rejected negative control')
            else:
                result.check_returncode()
        run(source)
        if args.negative_control:
            needle='          nextTick += period;'
            assert source.count(needle)==1
            run(source.replace(needle,'          nextTick = iterationStart; // restored deadline drift\n'+needle),True)
            run(source.replace('timing.advanceCount != *consumed', 'true'),True)
            run(source.replace('!pausedStep && diff > 62000', 'diff > 62000'),True)


if __name__=='__main__':
    main()
