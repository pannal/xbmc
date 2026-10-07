#!/usr/bin/env python3
"""Production presenter pacing with deterministic time and modeled driver IRQs.

The model sets sticky readiness and drains queued frames at each IRQ, as the
amvideo freerun path does. It is not an ISR/scanout or reporter reproduction.
"""
from pathlib import Path
import argparse
import subprocess,tempfile,os
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--negative-control', action='store_true')
args=parser.parse_args()
presenter=(root/'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h').read_text().replace('std::chrono::steady_clock::now()', 'TestClock::Now()').replace('std::condition_variable m_changed;', 'TestCondition m_changed;').replace('\nprivate:', '\npublic:')
if args.negative_control:
 presenter=presenter.replace('if (nativePaced)', 'if (false && nativePaced)')
poll = """
static unsigned int amvideo_poll(file*, poll_table*) {
 if (atomic_read(&trickmode_framedone)) {
  atomic_set(&trickmode_framedone, 0);
  return POLLOUT | POLLWRNORM;
 }
 return 0;
}
"""
harness=r'''
#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <cassert>
#include <iostream>
#include <cmath>
#include <vector>
#include <algorithm>
using namespace std::chrono_literals;
static constexpr double period=1000000.0/24;
static double nextIrq=0, irqPhase=400, oversleep=100;
static int pending=0, ticks=0, trickmode_framedone=0;
static bool jitter=false, nativePacing=false, transition=false, unavailable=false;
static double iterationUs=0;
static int fallbackWaits=0;
static std::vector<int> drains;
static std::vector<double> qbuf;
// These kernel waitqueue/atomic standins only substitute the OS primitives.
struct file{};struct poll_table{};
#define POLLOUT 4
#define POLLWRNORM 256
static int atomic_read(int* p){return *p;}
static void atomic_set(int* p,int value){*p=value;}
''' + poll + r'''
struct TestClock {
 static inline std::chrono::steady_clock::time_point now{};
 static auto Now(){return now;}
 static double Us(){return std::chrono::duration<double,std::micro>(now.time_since_epoch()).count();}
 static void Until(double target){
  // MODELED IRQ: source sets sticky done before the FREERUN_NODUR drain.
  // Acquisition occurs at this event time; this is not extracted full ISR code.
  while(nextIrq<=target){trickmode_framedone=1;drains.push_back(pending);pending=0;nextIrq+=period;}
  now=std::chrono::steady_clock::time_point(std::chrono::nanoseconds(std::llround(target*1000)));
 }
 static void Add(double us){Until(Us()+us);}
};
static std::function<void()> onWait;
struct TestCondition{
 void notify_all(){}
 template<class L,class P> void wait(L&,P p){assert(p());}
 template<class L,class D,class P> bool wait_for(L&,D,P p){return p();}
 template<class L,class T,class P> bool wait_until(L&lock,T target,P p){
  lock.unlock();if(onWait)onWait();
  if(unavailable){
   ++fallbackWaits;
   const double delta=std::chrono::duration<double,std::micro>(target.time_since_epoch()).count()-iterationUs;
   assert(delta>40000&&delta<42000); // first fallback cannot inherit unused native deadlines
  }
  double targetUs=std::chrono::duration<double,std::micro>(target.time_since_epoch()).count();
  oversleep=jitter&&ticks++%2?700:100;
  TestClock::Until(std::max(TestClock::Us(),targetUs)+oversleep);
  lock.lock();return p();
 }
};
#include "Presenter.h"
struct Frame:CAMLPresenter::Frame {
 bool submitted=false;
 Submission Submit(int&) override {
  if(submitted)return Submission::REUSED;
  submitted=true;qbuf.push_back(TestClock::Us());pending++;return Submission::SUBMITTED;
 }
 bool PollPaces()const override{return nativePacing&&!unavailable;}
 bool Poll()override{
  if(unavailable)return true; // syscall error/no fd: admitted pass, no native pacing
  if(!amvideo_poll(nullptr,nullptr)){
   // Real poll has50ms timeout. One24Hz IRQ is always sooner in this fixture.
   assert(nextIrq-TestClock::Us()<50000);
   TestClock::Until(nextIrq);
   assert(amvideo_poll(nullptr,nullptr));
  }
  return true; // actual adapter reports admitted polling, not physicalreceipt
 }
 bool Retire()override{return true;}
};
static void run(bool phaseJitter,double phase,bool sync,bool native,bool change=false){
 nativePacing=native;transition=change;unavailable=false;fallbackWaits=0;
 TestClock::now={};nextIrq=phase;irqPhase=phase;pending=ticks=trickmode_framedone=0;
 jitter=phaseJitter;drains.clear();qbuf.clear();
 CAMLPresenter*q=nullptr;unsigned produced=0;int baseline=-1;constexpr double runTime=20*1000000;
 CAMLPresenter::Hooks h;h.start=[]{return true;};h.finish=[]{};h.adjustClock=[](double){};
 h.timing=[&]{
  // Small per-iteration work remains when no timer is used.
  TestClock::Add(50);
  double now=TestClock::Us();iterationUs=now-50;
  unavailable=transition&&now>=5000000&&now<7000000;
  while(produced*period<now+5*period){
   auto f=std::make_shared<Frame>();f->pts=produced++*period;f->epoch=1;
   auto id=q->Reserve(0ms);assert(id);assert(q->Publish(id,f));
  }
  if(now>=2000000&&baseline<0)baseline=q->Skipped();
  if(now>=runTime)q->m_stop=true;
  return CAMLPresenter::Timing{now,1,24,0,sync};
 };
 CAMLPresenter presenter(16,h);q=&presenter;
 onWait=[&]{auto c=q->PendingControl();if(c.frame)assert(q->CompleteControl(c));};
 presenter.Show(true);presenter.Authorized();presenter.Run();
 auto d=presenter.Diagnostics();assert(d.missedDeadlines==0);if(!transition)assert(presenter.Skipped()==baseline);
 // Exclude startup and final10IRQs: never classify unfinished tail as an empty slot.
 assert(drains.size()>400);unsigned empty=0,doubles=0,singles=0;
 for(size_t i=10;i+10<drains.size();++i){
  assert(drains[i]>=0&&drains[i]<=2);
  empty+=drains[i]==0;doubles+=drains[i]==2;singles+=drains[i]==1;
 }
 double lo=1e9,hi=0;for(size_t i=10;i+10<qbuf.size();++i){double dt=qbuf[i]-qbuf[i-1];lo=std::min(lo,dt);hi=std::max(hi,dt);}
 if(!transition)assert(lo>41000&&hi<43000);
 if(!native&&phaseJitter&&phase==400){assert(empty>100&&doubles>100);assert(std::abs(int(empty)-int(doubles))<=1);}
 else if(!transition)assert(empty==0&&doubles==0);
 if(transition){
  assert(fallbackWaits>=45&&fallbackWaits<=50);
  for(size_t i=250;i+10<drains.size();++i)assert(drains[i]==1);
 }
 std::cout<<"sync="<<sync<<" jitter="<<phaseJitter<<" IRQphase_us="<<phase<<" QBUF_us="<<lo<<".."<<hi
 <<" native="<<native<<" fallback="<<fallbackWaits<<" interior IRQ0/1/2="<<empty<<"/"<<singles<<"/"<<doubles<<" missed="<<d.missedDeadlines<<" steady_skipped="<<(presenter.Skipped()-baseline)<<"\n";
 presenter.TakeFrames();onWait={};
}
static void last_pass_fallback(bool blocked) {
 TestClock::now={};nextIrq=400;pending=0;unavailable=false;
 struct TwoPass : CAMLPresenter::Frame {
  int polls=0; bool block;
  explicit TwoPass(bool value):block(value){method=CAMLPresenter::Method::BLEND;epoch=1;}
  Submission Submit(int&)override{return Submission::REUSED;}
  bool Poll()override{++polls;assert(polls<=2);return polls==1||!block;}
  bool PollPaces()const override{return polls==1||block;} // stale true after denied second call
  bool Retire()override{return true;}
 };
 CAMLPresenter* q=nullptr;CAMLPresenter::Hooks h;
 h.start=[]{return true;};h.finish=[]{};h.adjustClock=[](double){};
 h.timing=[]{return CAMLPresenter::Timing{TestClock::Us(),1,24,0,false};};
 CAMLPresenter presenter(4,h);q=&presenter;
 auto frame=std::make_shared<TwoPass>(blocked);
 auto id=q->Reserve(0ms);assert(id&&q->Publish(id,frame));
 int waits=0;
 onWait=[&]{
  ++waits;
  auto control=q->PendingControl();if(control.frame)assert(q->CompleteControl(control));
  if(frame->polls==2)q->m_stop=true;
  assert(waits<4);
 };
 presenter.Show(true);presenter.Authorized();presenter.Run();
 assert(waits==2&&frame->polls==2);
 presenter.TakeFrames();onWait={};
 std::cout<<"PASS BLEND second pass "<<(blocked?"denied":"unpaced")<<" restores timer fallback\n";
}
int main(){PLAYBACK_DIAGNOSTICS::SetEnabled(true);last_pass_fallback(true);last_pass_fallback(false);for(bool sync:{false,true}){for(bool native:{false,true}){run(false,400,sync,native);run(true,10000,sync,native);run(true,400,sync,native);}run(true,400,sync,true,true);}}
'''
with tempfile.TemporaryDirectory(prefix='presenter-irq-') as temp:
 out=Path(temp);(out/'Presenter.h').write_text(presenter);(out/'test.cpp').write_text(harness)
 subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(root/'xbmc'),'-I'+str(out),str(out/'test.cpp'),'-o',str(out/'test')],check=True)
 result=subprocess.run([str(out/'test')],capture_output=args.negative_control,text=True,timeout=20,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
 if args.negative_control:
  assert result.returncode!=0 and 'empty==0&&doubles==0' in result.stderr, result
  print('REJECTED extra timer between usable native polls: alternating empty/double IRQ acquisition')
 else: result.check_returncode()
