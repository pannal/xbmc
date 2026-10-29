#!/usr/bin/env python3
"""Execute actual demand-aware image manager and worker jobs with host fixtures.

Full large-texture manager declarations/definitions and actual JobManager
cancellation/completion methods run against owned decoder, queue and upload
fixtures. GPU work/context release are controlled barriers, not a real driver.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('method_extract',ROOT/'tools/test-aml-texture-upload-context.py')
EXTRACT=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
FILES=('xbmc/GUILargeTextureManager.cpp','xbmc/GUILargeTextureManager.h','xbmc/utils/JobManager.cpp')
PRELUDE=r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <exception>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
static thread_local int heldLocks=0;
static unsigned checks=0,cases=0;
static void Check(bool ok,const char* text){++checks;if(!ok)throw std::runtime_error(text);}
struct CCriticalSection {std::recursive_mutex mutex;void lock(){mutex.lock();++heldLocks;}void unlock(){--heldLocks;mutex.unlock();}};
constexpr int LOGDEBUG=0,LOGERROR=1;
struct CLog {template<class...T>static void Log(int,const char*,T&&...) {}};
struct Barrier {
 std::mutex mutex;std::condition_variable cv;bool entered=false,released=false;
 void Wait(){std::unique_lock<std::mutex>l(mutex);entered=true;cv.notify_all();Check(cv.wait_for(l,3s,[&]{return released;}),"fixture barrier release timeout");}
 void Await(){std::unique_lock<std::mutex>l(mutex);Check(cv.wait_for(l,3s,[&]{return entered;}),"fixture barrier entry timeout");}
 void Release(){std::lock_guard<std::mutex>l(mutex);released=true;cv.notify_all();}
};
struct State {
 bool cacheHit=true,useCache=true,needsChecking=false,async=true,directFails=false,cacheFails=false,emptyPath=false;
 bool uploadUnsupported=false,throwUpload=false;int blockDecode=0;bool blockUpload=false,blockAfterUpload=false;
 std::atomic<int>decodes{0},uploads{0},refreshes{0},destroyed{0},gpuDestroyedWithLock{0},leases{0},releases{0};
 // Background delay setting, on so that deferral is exercised; the product default is off.
 bool delayBackgrounds=true;
 Barrier decodeBarrier,uploadBarrier;
 void Decode(int n){if(n==blockDecode)decodeBarrier.Wait();}
};
static thread_local State*currentState=nullptr;
struct CTexture {
 State*state;bool uploaded=false;
 explicit CTexture(State*s):state(s){}
 ~CTexture(){++state->destroyed;if(uploaded&&heldLocks)++state->gpuDestroyedWithLock;}
 unsigned GetWidth()const{Check(state->leases==0,"metadata read after upload lease release");return 64;}
 unsigned GetHeight()const{Check(state->leases==0,"metadata read after upload lease release");return 32;}
 void LoadToGPUAsync(const std::atomic_bool* cancelled=nullptr){
  Check(heldLocks==0,"upload work outside manager/job locks");
  if(cancelled&&cancelled->load())return;
  if(state->uploadUnsupported)return;
  ++state->leases;
  if(state->blockUpload)state->uploadBarrier.Wait();
  if(cancelled&&cancelled->load()){--state->leases;++state->releases;return;}
  ++state->uploads;uploaded=true;
  if(state->blockAfterUpload)state->uploadBarrier.Wait();
  --state->leases;++state->releases;
  if(state->throwUpload)throw std::runtime_error("upload fixture partial failure");
 }
 static std::unique_ptr<CTexture>LoadFromFile(const std::string&,int,int){auto&s=*currentState;int n=++s.decodes;s.Decode(n);return s.directFails?nullptr:std::make_unique<CTexture>(&s);}
};
struct CTextureArray {
 std::vector<std::shared_ptr<CTexture>>m_textures;unsigned m_width=0,m_height=0;
 std::size_t size()const{return m_textures.size();}
 void Set(std::shared_ptr<CTexture>texture,unsigned w,unsigned h){m_textures.clear();m_width=w;m_height=h;m_textures.push_back(std::move(texture));}
 void Set(std::unique_ptr<CTexture>texture,unsigned w,unsigned h){Set(std::shared_ptr<CTexture>(std::move(texture)),w,h);}
 void Free(){m_textures.clear();m_width=m_height=0;}
};
class CJob;struct IJobCallback {virtual~IJobCallback()=default;virtual void OnJobComplete(unsigned,bool,CJob*)=0;virtual void OnJobProgress(unsigned,unsigned,unsigned,const CJob*){}};
class CJobManager;
class CJob {friend class CJobManager;CJobManager*m_callback=nullptr;public:
 enum PRIORITY{PRIORITY_LOW_PAUSABLE=0,PRIORITY_LOW,PRIORITY_NORMAL,PRIORITY_HIGH,PRIORITY_DEDICATED};
 virtual~CJob()=default;virtual bool DoWork()=0;virtual const char*GetType()const{return "FixtureJob";}virtual bool ShouldCancel(unsigned,unsigned)const;};
struct CWorkItem {CJob*m_job;unsigned m_id;IJobCallback*m_callback;bool operator==(unsigned id)const{return m_id==id;}bool operator==(const CJob*j)const{return m_job==j;}void FreeJob(){delete m_job;}};
class CJobManager {public:
 using JobQueue=std::vector<CWorkItem>;using Processing=std::vector<CWorkItem>;
 mutable CCriticalSection m_section;JobQueue m_jobQueue[5];Processing m_processing;unsigned next=1;bool reject=false;int added[5]{};
 unsigned AddJob(CJob*job,IJobCallback*callback,CJob::PRIORITY priority){
  std::unique_lock<CCriticalSection>l(m_section);++added[priority];if(reject){delete job;return 0;}
  unsigned id=next++;m_jobQueue[priority].push_back({job,id,callback});return id;}
 CJob*Start(unsigned id){std::unique_lock<CCriticalSection>l(m_section);for(auto&q:m_jobQueue){auto i=std::find(q.begin(),q.end(),id);if(i!=q.end()){auto item=*i;q.erase(i);m_processing.push_back(item);item.m_job->m_callback=this;return item.m_job;}}throw std::runtime_error("fixture queued job missing");}
 unsigned First()const{for(const auto&q:m_jobQueue)if(!q.empty())return q.front().m_id;return 0;}
 unsigned Queued()const{unsigned n=0;for(const auto&q:m_jobQueue)n+=q.size();return n;}
 void CancelJob(unsigned);bool OnJobProgress(unsigned,unsigned,const CJob*)const;void OnJobComplete(bool,CJob*);
 ~CJobManager(){for(auto&q:m_jobQueue)for(auto item:q)item.FreeJob();for(auto item:m_processing)item.FreeJob();}
};
static CJobManager*activeJobs=nullptr;
class CGUILargeTextureManager;
struct FakeTextureManager {std::string GetTexturePath(const std::string&p){return currentState->emptyPath?"":p;}};
struct FakeGUI {FakeTextureManager manager;FakeTextureManager&GetTextureManager(){return manager;}};
struct FakeCache {
 std::string CheckCachedImage(const std::string&p,bool&checking){checking=currentState->needsChecking;return currentState->cacheHit?p:"";}
 void BackgroundCacheImage(const std::string&){++currentState->refreshes;}
 void CacheImage(const std::string&,std::unique_ptr<CTexture>*out){auto&s=*currentState;int n=++s.decodes;s.Decode(n);if(!s.cacheFails)*out=std::make_unique<CTexture>(&s);}
};
struct FakeGraphics {int GetWidth()const{return 3840;}int GetHeight()const{return 2160;}};
struct FakeWindow {FakeGraphics gfx;FakeGraphics&GetGfxContext(){return gfx;}};
struct FakeAdvanced {bool m_guiAsyncTextureUpload=true;};
struct CSettings {static constexpr auto SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES="lookandfeel.delaybackgroundimages";
 bool GetBool(const std::string&id)const{Check(id==SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES,"only the background delay setting is read");return currentState->delayBackgrounds;}};
struct FakeSettings {FakeAdvanced*GetAdvancedSettings(){static thread_local FakeAdvanced a;a.m_guiAsyncTextureUpload=currentState->async;return &a;}
 CSettings*GetSettings(){static CSettings s;return &s;}};
struct CServiceBroker {
 static FakeGUI*GetGUI(){static FakeGUI g;return &g;}static FakeCache*GetTextureCache(){static FakeCache c;return &c;}
 static FakeWindow*GetWinSystem(){static FakeWindow w;return &w;}static FakeSettings*GetSettingsComponent(){static FakeSettings s;return &s;}
 static CJobManager*GetJobManager(){return activeJobs;}
};
static unsigned frameTime=0;struct CTimeUtils {static unsigned GetFrameTime(){return frameTime;}};
'''


def source(files, mutation=None):
    header=files[FILES[1]]
    header=re.sub(r'^\s*#(?:include|pragma)[^\n]*\n','',header,flags=re.M).replace('private:','public:')
    body=re.sub(r'^\s*#include[^\n]*\n','',files[FILES[0]],flags=re.M)
    if mutation:body=mutation(body)
    jobs='\n\n'.join(EXTRACT.function(files[FILES[2]],s) for s in ('bool CJob::ShouldCancel(','void CJobManager::CancelJob(','bool CJobManager::OnJobProgress(','void CJobManager::OnJobComplete('))
    return PRELUDE+header+'\n'+body+'\n'+jobs+'\n'+TESTS

TESTS=r'''
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
 bool Get(CTextureArray&array,bool first=false,const std::string&path="A"){return images.GetImage(path,array,first,s.useCache);}
 void Request(const std::string&path="A"){images.RequestImageUpload(path);}
 CJob*Start(unsigned id=0){return jobs.Start(id?id:jobs.First());}
 bool Work(CJob*job,State*state=nullptr){currentState=state?state:&s;bool ok=false;try{ok=job->DoWork();}catch(const std::exception&){}jobs.OnJobComplete(ok,job);return ok;}
 bool Next(){return Work(Start());}
 void Init(){CTextureArray array;Check(Get(array,true)&&!array.size(),"new request pending without publication");}
};
static void UndemandedAndCoalesced(){State s;Harness h(s);h.Init();Check(h.Next(),"decode succeeds");CTextureArray array;
 Check(s.decodes==1&&s.uploads==0,"prefetch decode performs no eager GPU upload");
 Check(h.Get(array)&&!array.size(),"private decoded pending is not failed or published");
 Check(h.jobs.Queued()==0,"undemanded decoded entry queues no upload");
 for(int i=0;i<20;++i){Check(h.Get(array,true),"same-path consumer acquires pending");h.Request();}
 Check(h.jobs.Queued()==1&&h.jobs.added[CJob::PRIORITY_HIGH]==1,"many consumers coalesce one upload promotion");
 Check(!array.size(),"promotion admission preserves publication barrier");
 Check(h.Next(),"upload job succeeds");Check(h.Get(array)&&array.size()==1,"completed upload publishes ready array");
 Check(s.uploads==1&&s.releases==1&&s.leases==0,"publish follows one completed upload context release");
 for(int i=0;i<20;++i){h.Request();}
 Check(h.jobs.Queued()==0,"ready texture never requeues upload");array.Free();++cases;
}
static void DemandBeforeDecode(){State s;Harness h(s);h.Init();h.Request();h.Request();
 Check(h.jobs.Queued()==1,"demand while decoding does not duplicate decode");h.Next();
 Check(s.uploads==0,"decode job remains CPU only after early demand");
 Check(h.jobs.Queued()==1&&h.jobs.added[CJob::PRIORITY_HIGH]==1,"decode callback rechecks earlier demand");
 h.Next();CTextureArray array;Check(h.Get(array)&&array.size()==1&&s.uploads==1,"early demand eventually publishes once");array.Free();++cases;
}
static void OffAndMissing(){State s;s.async=false;Harness h(s);h.Request("missing");Check(!h.jobs.Queued()&&h.images.m_queued.empty(),"demand cannot create missing request");h.Init();h.Request();h.Next();CTextureArray array;
 Check(h.Get(array)&&array.size()==1&&s.uploads==0,"OFF preserves decoded CPU publication");h.Request();Check(!h.jobs.Queued(),"OFF never queues async promotion");array.Free();++cases;
}
static void DecodeRoutes(){for(bool async:{false,true})for(int route=0;route<4;++route)for(bool checking:{false,true}){State s;s.async=async;s.cacheHit=route!=1;s.useCache=route!=2;s.directFails=route==3;s.needsChecking=checking;Harness h(s);h.Init();h.Next();CTextureArray array;
 Check(h.Get(array)&&bool(array.size())!=async&&s.uploads==0,"decode routes preserve OFF ready/ON private pending state");Check(s.decodes==(route==3?2:1),"exact direct/cache fallback branch traversal");
 Check(s.refreshes==int(checking&&route==0),"cache validation refresh remains CPU-side");h.Request();if(async)h.Next();Check(s.uploads==int(async),"each successful route preserves OFF or promotes once");array.Free();++cases;}}

static void DemandDuringDecode(){State s;s.blockDecode=1;Harness h(s);h.Init();auto*job=h.Start();std::exception_ptr error;
 std::thread t([&]{try{h.Work(job);}catch(...){error=std::current_exception();}});s.decodeBarrier.Await();h.Request();s.decodeBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(s.uploads==0&&h.jobs.Queued()==1,"demand arriving during decode is rechecked at completion");h.Next();Check(s.uploads==1,"decode-race demand promotes exactly once");++cases;
}
static void PublicationBlocked(){State s;s.blockUpload=true;Harness h(s);h.Init();h.Next();h.Request();auto*job=h.Start();std::exception_ptr error;
 std::thread t([&]{try{h.Work(job);}catch(...){error=std::current_exception();}});s.uploadBarrier.Await();CTextureArray array;
 Check(h.Get(array)&&!array.size(),"blocked worker never publishes mutable texture");h.Request();Check(!h.jobs.Queued(),"running promotion not duplicated");
 s.uploadBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(h.Get(array)&&array.size()==1&&s.releases==1,"idle completion becomes ready after lease release");array.Free();++cases;
}
static void GraceAndReacquire(){State s;Harness h(s);h.Init();h.Next();h.images.ReleaseImage("A",false);frameTime=1999;h.images.CleanupUnusedImages();
 Check(h.images.m_allocated.size()==1,"private pending entry keeps original two-second grace");CTextureArray array;Check(h.Get(array,true)&&!array.size(),"pending grace reacquisition preserves entry");h.Request();h.Next();Check(h.Get(array)&&array.size()==1&&s.decodes==1&&s.uploads==1,"reacquired pending texture promotes without re-decode");array.Free();++cases;
}
static void QueuedUploadGraceExpiry(){State s;Harness h(s);h.Init();h.Next();h.Request();h.images.ReleaseImage("A",false);frameTime=1999;h.images.CleanupUnusedImages();
 Check(h.images.m_allocated.size()==1&&h.jobs.Queued()==1,"last-ref queued upload survives original cache grace");
 CTextureArray array;Check(h.Get(array,true)&&!array.size(),"queued upload grace reacquisition keeps entry");h.Request();Check(h.jobs.Queued()==1,"reacquired queued upload remains coalesced");
 h.images.ReleaseImage("A",false);frameTime=4000;h.images.CleanupUnusedImages();
 Check(h.images.m_allocated.empty()&&h.jobs.Queued()==0&&s.uploads==0&&s.destroyed==1,"expired cache entry cancels queued promotion and retires private texture");++cases;
}
static void CancelQueuedUpload(){State s;Harness h(s);h.Init();h.Next();h.Request();Check(h.jobs.Queued()==1,"upload queued before cancellation");h.images.ReleaseImage("A",true);
 Check(h.jobs.Queued()==0&&h.images.m_allocated.empty(),"final immediate release cancels queued upload");Check(s.uploads==0&&s.destroyed==1,"queued cancellation has no upload and retires private texture");++cases;
}
static void CancelRunningUpload(){State s;s.blockUpload=true;Harness h(s);h.Init();h.Next();h.Request();auto*job=h.Start();std::exception_ptr error;
 std::thread t([&]{try{h.Work(job);}catch(...){error=std::current_exception();}});s.uploadBarrier.Await();h.images.ReleaseImage("A",true);s.uploadBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(s.uploads==0&&s.leases==0&&s.releases==1,"cancelled lease-waiting upload avoids native mutation and releases lease");
 Check(h.images.m_allocated.empty()&&s.destroyed==1&&!s.gpuDestroyedWithLock,"running upload owns texture through cancellation");++cases;
}
static void CancelMutatingUpload(){State s;s.blockAfterUpload=true;Harness h(s);h.Init();h.Next();h.Request();auto*job=h.Start();std::exception_ptr error;bool workerResult=true;
 std::thread t([&]{try{workerResult=h.Work(job);}catch(...){error=std::current_exception();}});s.uploadBarrier.Await();
 Check(s.uploads==1&&s.leases==1&&s.destroyed==0,"active native upload retains private texture");h.images.ReleaseImage("A",true);Check(s.destroyed==0,"running job strongly owns cancelled native texture");s.uploadBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(!workerResult,"cancelled active upload reports failure after GPU mutation");
 Check(h.images.m_allocated.empty()&&s.leases==0&&s.releases==1&&s.destroyed==1&&!s.gpuDestroyedWithLock,"mutating cancellation releases context and retires without publication under locks");++cases;
}
static void StaleUploadCallback(){State s;Harness h(s);h.Init();h.Next();h.Request();unsigned oldID=h.jobs.First();auto*old=h.Start();
 // Worker has completed but JobManager's already copied callback has not run.
 Check(old->DoWork()&&s.uploads==1,"old upload completes before callback/cancel race");
 h.images.ReleaseImage("A",true);h.Init();h.Next();h.Request();unsigned newID=h.jobs.First();Check(newID!=oldID,"new same-path upload has distinct job ID");
 h.images.OnJobComplete(oldID,true,old);CTextureArray array;
 Check(h.Get(array)&&!array.size()&&h.images.m_allocatedLookup.at("A")->m_uploadJobID==newID,"stale upload callback cannot publish into replacement path");
 h.jobs.OnJobComplete(false,old);h.Next();Check(h.Get(array)&&array.size()==1&&s.uploads==2,"replacement publishes only after own upload");
 array.Free();Check(!s.gpuDestroyedWithLock,"stale uploaded texture retires outside callback/global locks");++cases;
}
static void OldAndNewDecode(){State oldState,newState;oldState.blockDecode=1;Harness h(oldState);h.Init();unsigned oldID=h.jobs.First();auto*old=h.Start();std::exception_ptr error;bool oldResult=true;
 std::thread t([&]{try{oldResult=h.Work(old);}catch(...){error=std::current_exception();}});oldState.decodeBarrier.Await();h.images.ReleaseImage("A",true);h.Init();unsigned newID=h.jobs.First();
 oldState.decodeBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(!oldResult&&oldState.uploads==0&&oldState.destroyed==1,"cancelled old decoder owns and retires obsolete result");
 Check(h.images.m_queued.size()==1&&h.images.m_queued.front().first==newID&&newID!=oldID,"old decode completion preserves newer same-path generation");
 h.Work(h.Start(),&newState);h.Request();h.Next();CTextureArray array;Check(h.Get(array)&&array.size()==1&&array.m_textures[0]->state==&newState&&newState.uploads==1,"replacement decode publishes only its own texture");array.Free();++cases;
}
static void CopiedDecodeCallback(){State oldState,newState;Harness h(oldState);h.Init();unsigned oldID=h.jobs.First();auto*old=static_cast<CImageLoader*>(h.Start());old->m_texture=std::make_unique<CTexture>(&oldState);
 h.images.ReleaseImage("A",true);h.Init();unsigned newID=h.jobs.First();h.images.OnJobComplete(oldID,true,old);
 Check(h.images.m_queued.size()==1&&h.images.m_queued.front().first==newID&&h.images.m_allocated.empty()&&old->m_texture,"copied decode callback cannot publish obsolete generation");
 h.jobs.OnJobComplete(true,old);h.Work(h.Start(),&newState);h.Request();h.Next();CTextureArray array;Check(h.Get(array)&&array.size()==1&&array.m_textures[0]->state==&newState&&oldState.destroyed==1,"copied obsolete decode texture retires outside publication");array.Free();++cases;
}
static void Fallbacks(){for(int kind=0;kind<3;++kind){State s;s.uploadUnsupported=kind==1;s.throwUpload=kind==2;Harness h(s);h.Init();h.Next();if(kind==0)h.jobs.reject=true;h.Request();if(kind!=0)h.Next();CTextureArray array;
 bool ok=h.Get(array);if(kind<2){Check(ok&&array.size()==1,"rejected or unsupported upload retains CPU fallback");h.Request();Check(!h.jobs.Queued(),"CPU fallback cannot requeue every frame");}
 else Check(!ok&&!array.size(),"partial upload failure is failed, never CPU-published");
 array.Free();Check(!s.gpuDestroyedWithLock,"failed GPU-bearing texture destruction outside callback/global locks");++cases;}}
static void CancelDecode(bool fallback,bool checking){State s;s.cacheHit=!fallback;s.needsChecking=checking;s.blockDecode=1;Harness h(s);h.Init();auto*job=h.Start();std::exception_ptr error;bool workerResult=true;
 std::thread t([&]{try{workerResult=h.Work(job);}catch(...){error=std::current_exception();}});s.decodeBarrier.Await();h.images.ReleaseImage("A",true);s.decodeBarrier.Release();t.join();if(error)std::rethrow_exception(error);
 Check(!workerResult,"cancelled decode returns failure at cooperative checkpoint");Check(s.decodes==1&&s.uploads==0&&s.refreshes==0,"cancelled decode avoids refresh and upload");Check(h.images.m_allocated.empty()&&h.images.m_queued.empty()&&s.destroyed==1,"cancelled decode never publishes");++cases;}
static void ShutdownCancelledStartedUpload(){State s;Harness h(s);h.Init();h.Next();h.Request();unsigned id=h.jobs.First();auto*job=h.Start();h.jobs.CancelJob(id);
 Check(!h.Work(job)&&s.uploads==0,"job-manager cancellation prevents started upload work");CTextureArray array;Check(h.Get(array)&&!array.size(),"suppressed shutdown callback never publishes");++cases;}
static void AddJobZeroDecode(){State s;Harness h(s);h.jobs.reject=true;CTextureArray array;h.Get(array,true);Check(h.jobs.Queued()==0&&h.images.m_queued.empty(),"decode queue rejection leaves no jobID-zero pending request");++cases;}
static void CancelBeforeWork(){State s;Harness h(s);h.Init();auto*job=h.Start();h.images.ReleaseImage("A",true);Check(!h.Work(job)&&s.decodes==0&&s.uploads==0,"entry cancellation prevents decode and upload");++cases;}
static void CancelQueuedDecode(){State s;Harness h(s);h.Init();h.images.ReleaseImage("A",true);Check(!h.jobs.Queued()&&s.decodes==0&&s.uploads==0,"queued decode cancellation prevents all work");++cases;}
static void DecodeFailureAndEmpty(){for(int kind=0;kind<3;++kind){State s;s.emptyPath=kind==0;s.directFails=kind>0;s.cacheFails=true;s.useCache=kind!=2;Harness h(s);h.Init();Check(!h.Next(),"failed decode reports failure");CTextureArray array;Check(!h.Get(array)&&!array.size()&&!s.uploads,"failed decode never publishes or uploads");Check(s.decodes==(kind==0?0:kind==1?2:1),"failed decode preserves exact fallback traversal");++cases;}}
int main(int argc,char**argv){try{
 const std::string scenario=argc>1?argv[1]:"all";
 if(scenario=="entry")CancelBeforeWork();
 else if(scenario=="witness-direct")CancelDecode(false,true);
 else if(scenario=="witness-cache")CancelDecode(true,false);
 else {UndemandedAndCoalesced();DemandBeforeDecode();OffAndMissing();DecodeRoutes();DemandDuringDecode();PublicationBlocked();GraceAndReacquire();QueuedUploadGraceExpiry();CancelQueuedUpload();CancelRunningUpload();CancelMutatingUpload();StaleUploadCallback();OldAndNewDecode();CopiedDecodeCallback();Fallbacks();CancelDecode(false,true);CancelDecode(true,false);CancelBeforeWork();CancelQueuedDecode();DecodeFailureAndEmpty();ShutdownCancelledStartedUpload();AddJobZeroDecode();}
 std::printf("PASS %u cases %u checks; actual demand manager/jobs; decoder/GL/admission fixtures\n",cases,checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}

'''


def mutations(files):
    def edit(signature, old, new):
        def mutate(body):
            function=EXTRACT.function(body,signature)
            assert function.count(old)==1,(signature,old)
            return body.replace(function,function.replace(old,new,1),1)
        return mutate
    yield ('eager-decode-upload',edit('bool CImageLoader::DoWork()',
        '      return true;', '      m_texture->LoadToGPUAsync(m_cancelled.get());\n      return true;'),
        'prefetch decode performs no eager GPU upload')
    yield ('undemanded-admission',edit('void CGUILargeTextureManager::QueueUpload(',
        '!image->m_gpuDemand || ', ''), 'undemanded decoded entry queues no upload')
    yield ('duplicate-promotion',edit('void CGUILargeTextureManager::QueueUpload(',
        ' || image->m_uploadJobID != 0', ''), 'many consumers coalesce one upload promotion')
    yield ('early-publication',edit('void CGUILargeTextureManager::QueueUpload(',
        '  if (image->m_uploadJobID == 0)',
        '  if (image->m_uploadJobID != 0) image->SetTexture(image->m_pendingTexture);\n  if (image->m_uploadJobID == 0)'),
        'promotion admission preserves publication barrier')
    yield ('missing-completion-demand-recheck',edit('void CGUILargeTextureManager::OnJobComplete(',
        '      QueueUpload(image);', '      (void)image;'), 'decode callback rechecks earlier demand')
    yield ('stale-id-publication',edit('void CGUILargeTextureManager::OnJobComplete(',
        'image->m_uploadJobID == 0 || image->m_uploadJobID != jobID', 'image->m_uploadJobID == 0'),
        'stale upload callback cannot publish into replacement path')
    yield ('rejected-upload-stranded',edit('void CGUILargeTextureManager::QueueUpload(',
        '    image->SetTexture(std::move(image->m_pendingTexture));', '    (void)image;'),
        'rejected or unsupported upload retains CPU fallback')
    yield ('pending-is-failure',edit('bool CGUILargeTextureManager::GetImage(',
        ' || image->m_pendingTexture != nullptr', ''), 'private decoded pending is not failed or published')
    yield ('upload-job-cancellation-omitted',edit('  bool DoWork() override',
        '    if (ShouldCancel(0, 0) || m_cancelled->load(std::memory_order_acquire))\n      return false;', ''),
        'job-manager cancellation prevents started upload work')
    yield ('upload-final-cancellation-omitted',edit('  bool DoWork() override',
        '    return !m_cancelled->load(std::memory_order_acquire);', '    return true;'),
        'cancelled active upload reports failure after GPU mutation')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out','--out-dir',dest='out',type=Path)
    parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args();out=args.out or Path(tempfile.mkdtemp(prefix='image-upload-demand-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in FILES}
    script_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    records=[]
    env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'}
    variants=[('current',None,None),*mutations(files)]
    for name,mutation,witness in variants:
        cpp=out/(name+'.cpp');cpp.write_text(source(files,mutation));exe=out/name
        cmd=['g++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-pthread']
        if args.sanitize:cmd+=['-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
        cmd+=[str(cpp),'-o',str(exe)]
        c=subprocess.run(cmd,capture_output=True,text=True);(out/(name+'-compile.log')).write_text(c.stdout+c.stderr)
        assert c.returncode==0,c.stderr
        r=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30,env=env)
        (out/(name+'-stdout.txt')).write_text(r.stdout);(out/(name+'-stderr.txt')).write_text(r.stderr)
        accepted=(r.returncode==0 and not r.stderr) if witness is None else (r.returncode==1 and r.stderr=='FAIL '+witness+'\n' and not any(x in r.stdout+r.stderr for x in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'name':name,'compile_argv':cmd,'compile_exit':c.returncode,'exit':r.returncode,'witness':witness,'accepted':accepted,'generated_sha256':hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out/'receipt.json').write_text(json.dumps({'script_sha256':script_hash,'sources':{p:hashlib.sha256(v.encode()).hexdigest() for p,v in files.items()},'sanitize':args.sanitize,'asan_leak_detection':False,'results':records},indent=2)+'\n')
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==script_hash,'Test script changed during run'
        assert accepted,(name,r.stdout,r.stderr)
        if witness is None:print(r.stdout,end='',flush=True)
        else:print('PASS semantic control '+name+': '+witness,flush=True)

if __name__=='__main__':main()
