#!/usr/bin/env python3
"""Execute the actual large-texture manager with uncached internet images.

The complete GUILargeTextureManager declarations and definitions and the actual
JobManager cancellation/completion methods run against the decoder, queue and
network fixtures of test-image-upload-demand. Paths starting with "http" are
internet images; the network fixture sees each fetch's path and read interrupt.
Covers: decode jobs hand downloads off; at most two downloads, at low priority;
a released download keeps filling the disk cache until an image waits for its
slot, then the most recently started released one stops, and its slot returns
when its job ends; a new request takes over its image's running download;
waiting downloads start no decode; shutdown interrupts.
No real network, curl or GPU; no timing or performance claim.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('download_lane_demand', ROOT / 'tools/test-image-upload-demand.py')
DEMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMAND)
EXTRACT = DEMAND.EXTRACT

TESTS = r'''
#include <set>
struct Harness {
 State&s;CJobManager jobs;CGUILargeTextureManager images;
 explicit Harness(State&state):s(state){activeJobs=&jobs;currentState=&s;frameTime=0;}
 ~Harness(){
  activeJobs=&jobs;currentState=&s;
  while(!images.m_queued.empty()){const auto path=images.m_queued.front().second->GetPath();images.ReleaseImage(path,true);}
  while(!images.m_allocated.empty()){
   auto*image=images.m_allocated.front();const auto path=image->GetPath();
   if(image->m_refCount)images.ReleaseImage(path,true);else images.CleanupUnusedImages(true);
  }
  activeJobs=nullptr;currentState=nullptr;
 }
 void Ask(const std::string&path,bool useCache=true){CTextureArray a;Check(images.GetImage(path,a,true,useCache)&&!a.size(),"fixture: new request pending");}
 unsigned JobOf(const std::string&path){for(const auto&e:images.m_queued)if(e.second->GetPath()==path)return e.first;return 0;}
 bool Work(CJob*job){currentState=&s;bool ok=false;try{ok=job->DoWork();}catch(const std::exception&){}jobs.OnJobComplete(ok,job);return ok;}
 bool Run(const std::string&path){const unsigned id=JobOf(path);Check(id!=0,"fixture: job for path");return Work(jobs.Start(id));}
 bool Published(const std::string&path){auto i=images.m_allocatedLookup.find(path);return i!=images.m_allocatedLookup.end()&&(i->second->m_pendingTexture||i->second->GetTexture().size());}
 CGUILargeTextureManager::CLargeTexture::Download StateOf(const std::string&path){return images.m_queuedLookup.at(path)->m_downloadState;}
};
using Download=CGUILargeTextureManager::CLargeTexture::Download;
static constexpr int LOW=CJob::PRIORITY_LOW,NORMAL=CJob::PRIORITY_NORMAL;
template<class P> static bool Within(P p,std::chrono::milliseconds d){const auto end=std::chrono::steady_clock::now()+d;while(!p()){if(std::chrono::steady_clock::now()>end)return false;std::this_thread::sleep_for(1ms);}return true;}

// Fetches that read until their interrupt fires (then, if asked, wait for `let`) or until `finish`.
struct Net {
 std::mutex m;std::set<std::string> started,seen;
 std::atomic<bool> finish{false},hold{false},let{false},timedOut{false};
 bool Started(const std::string&p){std::lock_guard<std::mutex>l(m);return started.count(p)>0;}
 bool Seen(const std::string&p){std::lock_guard<std::mutex>l(m);return seen.count(p)>0;}
 std::function<bool(const std::string&,const std::function<bool()>&)> Blocking(){
  return [this](const std::string&p,const std::function<bool()>&interrupt){
   if(!interrupt)return true;
   {std::lock_guard<std::mutex>l(m);started.insert(p);}
   const auto end=std::chrono::steady_clock::now()+2s;
   while(true){
    if(interrupt()){{std::lock_guard<std::mutex>l(m);seen.insert(p);}if(hold)Within([&]{return let.load();},2s);return false;}
    if(finish)return true;
    if(std::chrono::steady_clock::now()>end){timedOut=true;return false;}
    std::this_thread::sleep_for(1ms);
   }};
 }
};
// Runs a job on its own thread; always joined, after letting every fetch end.
struct Worker {
 Net&net;std::thread t;std::exception_ptr error;
 template<class F> Worker(Net&n,F f):net(n),t([this,f]{try{f();}catch(...){error=std::current_exception();}}){}
 void Join(){if(t.joinable())t.join();if(error)std::rethrow_exception(error);}
 ~Worker(){net.let=true;net.finish=true;if(t.joinable())t.join();}
};
static void Wait(bool ok,const char*text){Check(ok,text);}

static void HandOff(){State s;s.cacheHit=false;Harness h(s);h.Ask("http://a");
 Check(!h.Run("http://a")&&s.fills==0,"decode job never fetches an internet image");
 Check(h.jobs.added[LOW]==1&&h.StateOf("http://a")==Download::RUNNING,"uncached internet image moves to a low-priority download job");
 Check(h.Run("http://a")&&s.fills==1&&s.interruptibleFills==1,"download fills the cache with a read interrupt");
 Check(h.Published("http://a")&&h.images.m_downloadJobs.empty(),"download publishes and returns its slot");
 h.images.RequestImageUpload("http://a");Check(h.jobs.added[CJob::PRIORITY_HIGH]==1,"downloaded image uploads like a decoded one");++cases;}
static void CachedInternet(){State s;Harness h(s);h.Ask("http://a");
 Check(h.Run("http://a")&&h.jobs.added[LOW]==0&&s.fills==0,"cached internet image decodes without a download");++cases;}
static void LocalUncached(){State s;s.cacheHit=false;Harness h(s);h.Ask("A");
 Check(h.Run("A")&&s.fills==1&&s.interruptibleFills==0&&h.jobs.added[LOW]==0,"local uncached image keeps the decode path");++cases;}
static void NoCacheInternet(){State s;s.useCache=false;int interrupted=0;
 s.fetch=[&](const std::string&,const std::function<bool()>&interrupt){if(interrupt)++interrupted;return true;};Harness h(s);h.Ask("http://a",false);
 Check(!h.Run("http://a")&&interrupted==0,"decode job never fetches an internet image");
 Check(h.Run("http://a")&&interrupted==1&&s.fills==0,"uncached-mode download reads with a read interrupt");++cases;}
static void Cap(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 Check(h.jobs.added[LOW]==2&&h.images.m_downloadQueue.size()==1&&h.StateOf("http://c")==Download::WAITING,"at most two downloads");
 Check(h.jobs.added[NORMAL]==3,"one decode job per image");
 h.Run("http://a");Check(h.jobs.added[LOW]==3&&h.StateOf("http://c")==Download::RUNNING,"a finished download admits the next");++cases;}
static void ReleaseKeepsReading(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 auto*job=h.jobs.Start(h.JobOf("http://a"));Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);
 Check(!Within([&]{return net.Seen("http://a");},200ms),"a released download keeps reading while no image waits");
 Check(h.images.m_downloadJobs.size()==1,"a released download keeps its slot");
 net.finish=true;w.Join();
 Check(s.fills==1&&!h.images.m_allocatedLookup.count("http://a")&&h.images.m_downloadJobs.empty(),"a released download fills the disk cache, publishes nothing and returns its slot");++cases;}
static void StopOnDemand(){State s;s.cacheHit=false;Net net;net.hold=true;s.fetch=net.Blocking();Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 for(const auto&d:h.images.m_downloadJobs)Check(!d.stop->load(),"a download still shown is never stopped");
 auto*a=h.jobs.Start(h.JobOf("http://a"));auto*b=h.jobs.Start(h.JobOf("http://b"));Worker wa(net,[&]{h.Work(a);}),wb(net,[&]{h.Work(b);});
 Wait(Within([&]{return net.Started("http://a")&&net.Started("http://b");},2s),"fixture: fetches did not start");
 h.images.ReleaseImage("http://a",true);
 Within([&]{return net.Seen("http://a")||net.Seen("http://b")||net.timedOut;},3s);
 Check(!net.Seen("http://b"),"a download still shown is never stopped");
 Check(net.Seen("http://a")&&!net.timedOut,"a released download stops when an image waits for its slot");
 Check(h.jobs.added[LOW]==2,"a stopped download keeps its slot until its job ends");
 h.Ask("http://a");h.Run("http://a");Check(h.StateOf("http://a")==Download::WAITING&&h.jobs.added[LOW]==2,"a stopped download is not taken over");
 net.let=true;wa.Join();
 Check(h.jobs.added[LOW]==3&&h.StateOf("http://c")==Download::RUNNING,"the waiting image gets the stopped download's slot");
 net.finish=true;wb.Join();Check(h.Published("http://b"),"the shown download publishes");++cases;}
static void NewestStopsFirst(){State s;s.cacheHit=false;Net net;net.hold=true;s.fetch=net.Blocking();Harness h(s);
 for(auto p:{"http://a","http://b"}){h.Ask(p);h.Run(p);}
 auto*a=h.jobs.Start(h.JobOf("http://a"));auto*b=h.jobs.Start(h.JobOf("http://b"));Worker wa(net,[&]{h.Work(a);}),wb(net,[&]{h.Work(b);});
 Wait(Within([&]{return net.Started("http://a")&&net.Started("http://b");},2s),"fixture: fetches did not start");
 h.images.ReleaseImage("http://a",true);h.images.ReleaseImage("http://b",true);
 Check(!Within([&]{return net.Seen("http://a")||net.Seen("http://b");},200ms),"a released download keeps reading while no image waits");
 h.Ask("http://c");h.Run("http://c");
 Within([&]{return net.Seen("http://a")||net.Seen("http://b")||net.timedOut;},3s);
 Check(net.Seen("http://b")&&!net.Seen("http://a"),"the most recently started released download stops first");
 net.let=true;wb.Join();Check(h.StateOf("http://c")==Download::RUNNING,"the waiting image gets the stopped download's slot");
 net.finish=true;wa.Join();Check(s.decodes==1&&!h.images.m_allocatedLookup.count("http://a")&&h.images.m_downloadJobs.size()==1,"the older released download completes into the disk cache");++cases;}
static void TakeOver(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 const unsigned id=h.JobOf("http://a");auto*job=h.jobs.Start(id);Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);h.Ask("http://a");h.Run("http://a");
 Check(h.jobs.added[LOW]==1&&h.JobOf("http://a")==id&&h.StateOf("http://a")==Download::RUNNING,"a renewed request takes over its running download");
 net.finish=true;w.Join();Check(h.Published("http://a")&&h.images.m_downloadJobs.empty(),"the taken-over download publishes");++cases;}
static void StoppedBeforeStart(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 const unsigned id=h.JobOf("http://a");h.images.ReleaseImage("http://a",true);
 Check(h.jobs.Queued()==2,"a released download keeps its job and callback");
 const int fills=s.fills;h.Work(h.jobs.Start(id));Check(s.fills==fills,"a download stopped before it starts makes no request");
 Check(h.StateOf("http://c")==Download::RUNNING,"its slot returns when its job ends");++cases;}
static void ReleaseWaiting(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 h.images.ReleaseImage("http://c",true);Check(h.images.m_downloadQueue.empty(),"released waiting image leaves the download queue");
 h.Run("http://a");Check(h.jobs.added[LOW]==2,"released waiting image never downloads");++cases;}
static void JoinWaiting(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 CTextureArray a;Check(h.images.GetImage("http://c",a,true,true),"second consumer joins");
 Check(h.jobs.added[NORMAL]==3,"a new consumer of a waiting download starts no decode");
 h.images.m_hasDeferred=true;h.images.ProcessDeferred();
 Check(h.jobs.added[NORMAL]==3&&h.StateOf("http://c")==Download::WAITING,"the deferred scan skips waiting downloads");
 h.images.ReleaseImage("http://c",true);Check(h.images.m_queuedLookup.count("http://c")&&h.images.m_downloadQueue.size()==1,"one release of two keeps the waiting download");++cases;}
static void Shutdown(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 const unsigned id=h.JobOf("http://a");auto*job=h.jobs.Start(id);Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.jobs.CancelJob(id);Within([&]{return net.Seen("http://a")||net.timedOut;},3s);
 Check(net.Seen("http://a")&&!net.timedOut,"job-manager cancellation interrupts a download");
 w.Join();++cases;}
static void Rejected(){State s;s.cacheHit=false;Harness h(s);h.Ask("http://a");h.jobs.reject=true;h.Run("http://a");
 CTextureArray a;Check(!h.images.GetImage("http://a",a,false)&&h.images.m_queued.empty()&&h.images.m_downloadJobs.empty(),"rejected download becomes a load failure");++cases;}
static FakeCache& Cache(){return *CServiceBroker::GetTextureCache();}
static void WrappedInternet(){State s;s.cacheHit=false;Harness h(s);const std::string p="image://http://a|size=thumb";h.Ask(p);
 Check(!h.Run(p)&&s.fills==0&&h.jobs.added[LOW]==1,"a transformed internet image downloads in its own job");++cases;}
static void CachedElsewhere(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 Check(Cache().observer!=nullptr&&h.StateOf("http://c")==Download::WAITING,"fixture: waiting with an observer");
 Cache().observer("http://other");Check(h.StateOf("http://c")==Download::WAITING,"another image's report leaves a waiting download");
 Cache().observer("http://c");
 Check(h.StateOf("http://c")==Download::NONE&&h.images.m_downloadQueue.empty()&&h.jobs.added[NORMAL]==4,"a waiting image cached by another request decodes at once");
 s.cacheHit=true;Check(h.Run("http://c")&&h.Published("http://c")&&h.jobs.added[LOW]==2,"it decodes from the cache without a download");++cases;}
static void ReportAfterManager(){State s;s.cacheHit=false;std::function<void(const std::string&)> observer;
 {Harness h(s);for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}observer=Cache().observer;}
 observer("http://c");Check(true,"a report after the manager is gone is ignored");++cases;}
static void TakeOverPolicy(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 const unsigned id=h.JobOf("http://a");auto*job=h.jobs.Start(id);Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);h.Ask("http://a",false);h.Run("http://a");
 Check(h.JobOf("http://a")!=id&&h.jobs.added[LOW]==2,"a request with another cache policy does not take over");
 net.finish=true;w.Join();++cases;}
static void CachedBeforeHandOff(){State s;s.cacheHit=false;Harness h(s);h.Ask("http://a");
 auto*job=h.jobs.Start(h.JobOf("http://a"));currentState=&s;const bool ok=job->DoWork();
 Cache().observer("http://a");h.jobs.OnJobComplete(ok,job);
 Check(h.jobs.added[LOW]==0&&h.jobs.added[NORMAL]==2&&h.StateOf("http://a")==Download::NONE,"an image cached between its decode job's check and the hand-off decodes at once");++cases;}
static void ReportBeforeCheck(){State s;s.cacheHit=false;Harness h(s);h.Ask("http://a");Cache().observer("http://a");h.Run("http://a");
 Check(h.jobs.added[LOW]==1&&h.StateOf("http://a")==Download::RUNNING,"a report before the decode job's check does not skip the download");++cases;}
static void ReportsLost(){State s;s.cacheHit=false;Harness h(s);h.Ask("http://a");
 auto*job=h.jobs.Start(h.JobOf("http://a"));currentState=&s;const bool ok=job->DoWork();
 for(int i=0;i<70;++i){Cache().observer("http://other"+std::to_string(i));}
 h.jobs.OnJobComplete(ok,job);
 Check(h.jobs.added[LOW]==0&&h.jobs.added[NORMAL]==2,"with reports lost since the check, the cache is checked again");++cases;}
static void DropNotStarted(){State s;s.cacheHit=false;Harness h(s);
 for(auto p:{"http://a","http://b","http://c"}){h.Ask(p);h.Run(p);}
 const unsigned old=h.JobOf("http://a");Cache().observer("http://a");
 Check(h.StateOf("http://a")==Download::NONE&&h.jobs.added[NORMAL]==4&&h.StateOf("http://c")==Download::RUNNING&&h.jobs.added[LOW]==3,
  "a download not yet running whose image was cached elsewhere is dropped and its slot reused");
 const int fills=s.fills;h.Work(h.jobs.Start(old));Check(s.fills==fills,"a dropped download makes no request");++cases;}
static void KeepStarted(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 auto*job=h.jobs.Start(h.JobOf("http://a"));Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 Cache().observer("http://a");Check(h.images.m_downloadJobs.size()==1&&h.StateOf("http://a")==Download::RUNNING,"a running download is not dropped");
 net.finish=true;w.Join();Check(h.Published("http://a"),"the running download publishes");++cases;}
static void TakeOverPlacement(bool before){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 const auto running=h.images.m_queuedLookup.at("http://a")->m_decodePlacement;
 auto*job=h.jobs.Start(h.JobOf("http://a"));Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);h.Ask("http://a");
 if(before)h.images.RequestImageUpload("http://a");
 h.Run("http://a");Check(h.JobOf("http://a")!=0&&h.jobs.added[LOW]==1,"fixture: took over");
 if(!before){Check(!running->needed,"fixture: not needed yet");h.images.RequestImageUpload("http://a");}
 Check(running->needed,before?"a foreground request before the take-over reaches the running download":"a foreground request reaches the download it took over");
 net.finish=true;w.Join();++cases;}
static void CachedBeatsTakeOver(){State s;s.cacheHit=false;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a");h.Run("http://a");
 const unsigned old=h.JobOf("http://a");auto*job=h.jobs.Start(old);Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);h.Ask("http://a");
 auto*decode=h.jobs.Start(h.JobOf("http://a"));currentState=&s;const bool ok=decode->DoWork();
 Cache().observer("http://a");h.jobs.OnJobComplete(ok,decode);
 Check(h.JobOf("http://a")!=old&&h.StateOf("http://a")==Download::NONE,"a newer cache report wins over taking a download over");
 net.finish=true;w.Join();++cases;}
static void CachelessDropped(){State s;Harness h(s);h.Ask("http://a",false);h.Run("http://a");const unsigned id=h.JobOf("http://a");
 h.images.ReleaseImage("http://a",true);Check(h.images.m_downloadJobs.empty(),"a cache-less download is dropped with its last consumer");
 int fetched=0;s.fetch=[&](const std::string&,const std::function<bool()>&){++fetched;return true;};
 h.Work(h.jobs.Start(id));Check(fetched==0,"a dropped cache-less download makes no request");++cases;}
static void CachelessStopped(){State s;Net net;s.fetch=net.Blocking();Harness h(s);h.Ask("http://a",false);h.Run("http://a");
 auto*job=h.jobs.Start(h.JobOf("http://a"));Worker w(net,[&]{h.Work(job);});Wait(Within([&]{return net.Started("http://a");},2s),"fixture: fetch did not start");
 h.images.ReleaseImage("http://a",true);
 Check(Within([&]{return net.Seen("http://a");},1s),"a running cache-less download stops with its last consumer");w.Join();++cases;}
int main(){try{
 HandOff();CachedInternet();LocalUncached();NoCacheInternet();Cap();ReleaseKeepsReading();StopOnDemand();NewestStopsFirst();TakeOver();
 StoppedBeforeStart();ReleaseWaiting();JoinWaiting();Shutdown();Rejected();
 WrappedInternet();CachedElsewhere();ReportAfterManager();TakeOverPolicy();
 CachedBeforeHandOff();ReportBeforeCheck();ReportsLost();DropNotStarted();KeepStarted();TakeOverPlacement(false);TakeOverPlacement(true);CachedBeatsTakeOver();CachelessDropped();CachelessStopped();
 std::printf("PASS %u cases %u checks; actual manager/jobs, decoder/network fixtures\n",cases,checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def source(files, mutation=None):
    generated = DEMAND.source(files, mutation)
    assert generated.endswith(DEMAND.TESTS)
    return generated[:-len(DEMAND.TESTS)] + TESTS


def mutations():
    def edit(signature, old, new):
        def mutate(body):
            function = EXTRACT.function(body, signature)
            assert function.count(old) == 1, (signature, old)
            return body.replace(function, function.replace(old, new, 1), 1)
        return mutate
    work, start = 'bool CImageLoader::DoWork()', 'void CGUILargeTextureManager::StartNextDownload()'
    release, done = 'void CGUILargeTextureManager::ReleaseImage(', 'void CGUILargeTextureManager::OnJobComplete('
    yield ('decode-fetches', edit(work, '  if (internet && !m_download)\n', '  if (false)\n'),
           'decode job never fetches an internet image')
    yield ('no-cap', edit(start, 'if (m_downloadJobs.size() >= MAX_DOWNLOADS)', 'if (false)'), 'at most two downloads')
    yield ('normal-priority', edit(start, 'CJob::PRIORITY_LOW', 'CJob::PRIORITY_NORMAL'),
           'uncached internet image moves to a low-priority download job')
    yield ('stop-on-release', edit(release, '              download->wanted = false;\n',
           '              download->wanted = false, download->stop->store(true);\n'),
           'a released download keeps reading while no image waits')
    yield ('cancel-running-download', edit(release, '          else if (it->first != 0)', '          if (it->first != 0)'),
           'a released download keeps reading while no image waits')
    yield ('stop-shown', edit(start, '        if (!d->wanted && !d->stop->load())', '        if (!d->stop->load())'),
           'a download still shown is never stopped')
    yield ('no-stop', edit(start, '          d->stop->store(true, std::memory_order_release);\n', ''),
           'a released download stops when an image waits for its slot')
    yield ('no-stop-on-release', edit(release, '    if (released)\n      StartNextDownload();\n', '    (void)released;\n'),
           'a released download stops when an image waits for its slot')
    yield ('oldest-first', edit(start, 'm_downloadJobs.rbegin();\n           d != m_downloadJobs.rend()',
           'm_downloadJobs.begin();\n           d != m_downloadJobs.end()'),
           'the most recently started released download stops first')
    yield ('no-take-over', edit(done, '        if (running != m_downloadJobs.end())\n', '        if (false)\n'),
           'a renewed request takes over its running download')
    yield ('take-over-stopped', edit(done, '!d.wanted && !d.stop->load() && d.path', '!d.wanted && d.path'),
           'a stopped download is not taken over')
    yield ('shutdown-ignored', edit(work, ' || ShouldCancel(0, 0); };', '; };'), 'job-manager cancellation interrupts a download')
    yield ('no-read-interrupt', edit(work, '&m_texture, nullptr, interrupt);', '&m_texture, nullptr);'),
           'download fills the cache with a read interrupt')
    yield ('request-after-stop', edit(work,
           '    // Stopped while it waited for a worker: not even a request to the server.\n    if (interrupt())\n      return false;\n', ''),
           'a download stopped before it starts makes no request')
    yield ('waiting-restarts-decode', edit('CGUILargeTextureManager::queueIterator CGUILargeTextureManager::StartQueuedImage(',
           '  if (image->m_downloadState != CLargeTexture::Download::NONE)\n    return ++it;\n', ''),
           'a new consumer of a waiting download starts no decode')
    yield ('plain-only', edit(work, 'CTextureCacheJob::IsInterruptibleDownload(texturePath)',
           'texturePath.rfind("http", 0) == 0'), 'a transformed internet image downloads in its own job')
    yield ('cached-ignored', edit('void CGUILargeTextureManager::OnImageCached(',
           '    if (it != m_queued.end() && it->first == 0)\n      StartQueuedImage(it);\n', '    (void)it;\n'),
           'a waiting image cached by another request decodes at once')
    yield ('no-cache-url', edit(work, '      m_cacheURL = CTextureUtils::UnwrapImageURL(texturePath);\n', ''),
           'a waiting image cached by another request decodes at once')
    yield ('take-over-any-policy', edit(done, ' &&\n                     d.useCache == image->m_useCache', ''),
           'a request with another cache policy does not take over')
    cached = 'bool CGUILargeTextureManager::CachedSince('
    yield ('no-recheck', edit(cached, '  if (url.empty() || m_recentCached.empty())', '  if (true)'),
           "an image cached between its decode job's check and the hand-off decodes at once")
    yield ('any-report', edit(cached, 'report.first > generation && ', ''),
           "a report before the decode job's check does not skip the download")
    yield ('lost-ignored', edit(cached, '  if (m_recentCached.front().first > generation + 1)\n    return true;\n', ''),
           'with reports lost since the check, the cache is checked again')
    yield ('snapshot-late', edit(work, '    m_checkedGeneration = m_cachedGeneration->load();\n',
           '    m_checkedGeneration = 0;\n'), "a report before the decode job's check does not skip the download")
    yield ('no-drop', edit('void CGUILargeTextureManager::OnImageCached(',
           '        !download->start->compare_exchange_strong(queued, DownloadStart::DROPPED))', '        ((void)queued, true))'),
           'a download not yet running whose image was cached elsewhere is dropped and its slot reused')
    yield ('drop-started', edit('void CGUILargeTextureManager::OnImageCached(',
           '        !download->start->compare_exchange_strong(queued, DownloadStart::DROPPED))',
           '        ((void)queued, download->start->store(DownloadStart::DROPPED), false))'),
           'a running download is not dropped')
    yield ('no-start-claim', edit(work, '    if (!m_start->compare_exchange_strong(queued, DownloadStart::STARTED))\n      return false;\n',
           '    (void)queued;\n'), 'a dropped download makes no request')
    yield ('placement-kept', edit(done, '          image->m_decodePlacement = running->placement;\n', ''),
           'a foreground request reaches the download it took over')
    yield ('placement-not-carried', edit(done, '          if (image->m_decodePlacement->IsNeededNow())\n            running->placement->NeededNow();\n', ''),
           'a foreground request before the take-over reaches the running download')
    def check_after_takeover(body):
        function = EXTRACT.function(body, done)
        block = ('        if (CachedSince(loader->m_checkedGeneration, image->m_cacheURL))\n        {\n'
                 '          // Cached by another request after the job\'s check: decode it from the cache,\n'
                 '          // also rather than take over a download that may already have failed.\n'
                 '          StartQueuedImage(it);\n          return;\n        }\n')
        anchor = '        image->m_downloadState = CLargeTexture::Download::WAITING;\n'
        assert function.count(block) == 1 and function.count(anchor) == 1
        moved = function.replace(block, '', 1).replace(anchor, block + anchor, 1)
        return body.replace(function, moved, 1)
    yield ('take-over-first', check_after_takeover, 'a newer cache report wins over taking a download over')
    yield ('cacheless-kept', edit(release, '              if (!download->useCache)\n', '              if (false)\n'),
           'a cache-less download is dropped with its last consumer')
    yield ('cacheless-running-kept', edit(release, '                else\n                  download->stop->store(true, std::memory_order_release);\n', ''),
           'a running cache-less download stops with its last consumer')
    yield ('no-admit-on-end', edit(done, '  if (downloadEnded)\n    StartNextDownload();\n', ''), 'a finished download admits the next')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', '--out-dir', dest='out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='image-download-lane-'))
    out.mkdir(parents=True, exist_ok=True)
    files = {p: (ROOT / p).read_text() for p in DEMAND.FILES}
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'}
    records = []
    for name, mutation, witness in [('current', None, None), *mutations()]:
        cpp = out / (name + '.cpp')
        cpp.write_text(source(files, mutation))
        exe = out / name
        cmd = ['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pthread']
        if args.sanitize:
            cmd += ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
        cmd += [str(cpp), '-o', str(exe)]
        c = subprocess.run(cmd, capture_output=True, text=True)
        (out / (name + '-compile.log')).write_text(c.stdout + c.stderr)
        assert c.returncode == 0, c.stderr
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120, env=env)
        (out / (name + '-stdout.txt')).write_text(r.stdout)
        (out / (name + '-stderr.txt')).write_text(r.stderr)
        sanitizer = any(x in r.stdout + r.stderr for x in ('AddressSanitizer', 'UndefinedBehaviorSanitizer', 'runtime error:', 'DEADLYSIGNAL'))
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (
            r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n' and not sanitizer)
        records.append({'name': name, 'compile_argv': cmd, 'exit': r.returncode, 'witness': witness,
                        'accepted': accepted, 'generated_sha256': hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out / 'receipt.json').write_text(json.dumps({'script_sha256': script_hash,
            'sources': {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()},
            'sanitize': args.sanitize, 'results': records}, indent=2) + '\n')
        assert accepted, (name, r.stdout, r.stderr)
        if witness is None:
            print(r.stdout, end='', flush=True)
        else:
            print('PASS semantic control ' + name + ': ' + witness, flush=True)


if __name__ == '__main__':
    main()
