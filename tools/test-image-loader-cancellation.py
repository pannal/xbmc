#!/usr/bin/env python3
"""Execute actual image-loader/job cancellation methods against threaded host fixtures.

The decoder, broker, GL work and queue admission are fixtures. Full production
DoWork, ShouldCancel, CancelJob, OnJobProgress, OnJobComplete (job and large
texture manager) execute unchanged. Demand-aware sources use the complete current
manager fixture and retain historical eager-upload witnesses separately.
No GPU, target lifetime or performance claim.
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

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'bfa0b0f91d06971e0d47d1aa1e44837ef01cdf98'

SPEC = importlib.util.spec_from_file_location('upload_extract', ROOT / 'tools/test-aml-texture-upload-context.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)

PRELUDE = r'''
#include <algorithm>
#include <atomic>
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
struct CCriticalSection {
 std::recursive_mutex mutex;
 void lock(){mutex.lock();++heldLocks;}
 void unlock(){--heldLocks;mutex.unlock();}
};
static void Check(bool ok,const char* text){if(!ok)throw std::runtime_error(text);}
constexpr int LOGDEBUG=0, LOGERROR=1;
struct CLog {template<class... T>static void Log(int,const char*,T&&...) {}};
struct State {
 bool cacheHit=true,useCache=true,needsChecking=false,async=true;
 bool directFails=false,cacheFails=false,emptyPath=false;
 int blockDecode=0;
 std::atomic<int> decodes{0},uploads{0},refreshes{0},destroyed{0},destroyedWithLock{0};
 std::mutex mutex;std::condition_variable cv;bool entered=false,released=false;
 void DecodeBarrier(int call){
  if(call!=blockDecode)return;
  std::unique_lock<std::mutex> lock(mutex);entered=true;cv.notify_all();
  Check(cv.wait_for(lock,3s,[&]{return released;}),"fixture: decode barrier release timeout");
 }
 void AwaitDecode(){std::unique_lock<std::mutex> lock(mutex);Check(cv.wait_for(lock,3s,[&]{return entered;}),"fixture: decode entry timeout");}
 void ReleaseDecode(){std::lock_guard<std::mutex> lock(mutex);released=true;cv.notify_all();}
};
static thread_local State* currentState=nullptr;
struct CTexture {
 State* state;
 explicit CTexture(State* s):state(s){}
 ~CTexture(){++state->destroyed;if(heldLocks)++state->destroyedWithLock;}
 void LoadToGPUAsync(const std::atomic_bool* =nullptr){++state->uploads;}
 static std::unique_ptr<CTexture> LoadFromFile(const std::string&,int,int){
  auto& s=*currentState;const int n=++s.decodes;s.DecodeBarrier(n);
  return s.directFails?nullptr:std::make_unique<CTexture>(&s);
 }
};
struct FakeTextureManager {std::string GetTexturePath(const std::string& p){return currentState->emptyPath?"":p;}};
struct FakeGUI {FakeTextureManager manager;FakeTextureManager& GetTextureManager(){return manager;}};
struct FakeCache {
 std::string CheckCachedImage(const std::string& path,bool& checking){checking=currentState->needsChecking;return currentState->cacheHit?path:"";}
 void BackgroundCacheImage(const std::string&){++currentState->refreshes;}
 void CacheImage(const std::string&,std::unique_ptr<CTexture>* out){
  auto& s=*currentState;const int n=++s.decodes;s.DecodeBarrier(n);
  if(!s.cacheFails)*out=std::make_unique<CTexture>(&s);
 }
};
struct FakeGraphics {int GetWidth()const{return 3840;}int GetHeight()const{return 2160;}};
struct FakeWindow {FakeGraphics gfx;FakeGraphics& GetGfxContext(){return gfx;}};
struct FakeAdvanced {bool m_guiAsyncTextureUpload=true;};
struct CSettings {static constexpr auto SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES="lookandfeel.delaybackgroundimages";bool GetBool(const std::string&)const{return true;}};
struct FakeSettings {FakeAdvanced* GetAdvancedSettings(){static thread_local FakeAdvanced a;a.m_guiAsyncTextureUpload=currentState->async;return &a;}
 CSettings* GetSettings(){static CSettings s;return &s;}};
struct CServiceBroker {
 static FakeGUI* GetGUI(){static FakeGUI g;return &g;}
 static FakeCache* GetTextureCache(){static FakeCache c;return &c;}
 static FakeWindow* GetWinSystem(){static FakeWindow w;return &w;}
 static FakeSettings* GetSettingsComponent(){static FakeSettings s;return &s;}
};
class CJob;
struct IJobCallback {
 virtual ~IJobCallback()=default;
 virtual void OnJobComplete(unsigned int,bool,CJob*)=0;
 virtual void OnJobProgress(unsigned int,unsigned int,unsigned int,const CJob*){}
};
class CJobManager;
class CJob {
 friend class CJobManager;
 CJobManager* m_callback=nullptr;
public:
 enum PRIORITY{PRIORITY_LOW_PAUSABLE=0,PRIORITY_LOW,PRIORITY_NORMAL,PRIORITY_HIGH,PRIORITY_DEDICATED};
 virtual ~CJob()=default;
 virtual bool DoWork()=0;
 virtual const char* GetType()const{return "ImageLoader";}
 virtual bool ShouldCancel(unsigned int,unsigned int)const;
};
struct CImageLoader : CJob {
 std::string m_path;bool m_use_cache;
 std::unique_ptr<CTexture> m_texture;
 std::shared_ptr<std::atomic_bool> m_cancelled;
 CImageLoader(std::string path,bool useCache):m_path(std::move(path)),m_use_cache(useCache){}
 bool DoWork()override;
};
struct CWorkItem {
 CJob* m_job;unsigned int m_id;IJobCallback* m_callback;
 bool operator==(unsigned int id)const{return id==m_id;}
 bool operator==(const CJob* job)const{return job==m_job;}
 void FreeJob(){delete m_job;}
};
class CJobManager {
public:
 using JobQueue=std::vector<CWorkItem>;using Processing=std::vector<CWorkItem>;
 mutable CCriticalSection m_section;JobQueue m_jobQueue[5];Processing m_processing;
 unsigned int next=1;
 unsigned int Queue(CJob* job,IJobCallback* callback){std::unique_lock<CCriticalSection> lock(m_section);const auto id=next++;m_jobQueue[CJob::PRIORITY_NORMAL].push_back({job,id,callback});return id;}
 CJob* Start(unsigned int id){
  std::unique_lock<CCriticalSection> lock(m_section);auto& q=m_jobQueue[CJob::PRIORITY_NORMAL];auto i=std::find(q.begin(),q.end(),id);Check(i!=q.end(),"fixture: missing queued job");auto item=*i;q.erase(i);m_processing.push_back(item);item.m_job->m_callback=this;return item.m_job;
 }
 void CancelJob(unsigned int);
 bool OnJobProgress(unsigned int,unsigned int,const CJob*)const;
 void OnJobComplete(bool,CJob*);
};
class CGUILargeTextureManager : public IJobCallback {
public:
 struct CLargeTexture {
  std::string path;std::unique_ptr<CTexture> texture;
  explicit CLargeTexture(std::string p):path(std::move(p)){}
  const std::string& GetPath()const{return path;}
  void SetTexture(std::unique_ptr<CTexture> t){Check(!texture,"fixture: duplicate publication");texture=std::move(t);}
 };
 using queueIterator=std::vector<std::pair<unsigned int,CLargeTexture*>>::iterator;
 CCriticalSection m_listSection;
 std::vector<std::pair<unsigned int,CLargeTexture*>> m_queued;
 std::vector<CLargeTexture*> m_allocated;
 std::unordered_map<std::string,CLargeTexture*> m_queuedLookup,m_allocatedLookup;
 void OnJobComplete(unsigned int,bool,CJob*)override;
 void Register(unsigned int id,const std::string& path){std::unique_lock<CCriticalSection> l(m_listSection);auto* image=new CLargeTexture(path);m_queued.push_back({id,image});m_queuedLookup[path]=image;}
 // Fixture consumer release; the real JobManager cancellation contract below executes.
 void Drop(unsigned int id){std::unique_lock<CCriticalSection> l(m_listSection);auto i=std::find_if(m_queued.begin(),m_queued.end(),[&](const auto& item){return item.first==id;});Check(i!=m_queued.end(),"fixture: missing image queue entry");m_queuedLookup.erase(i->second->path);delete i->second;m_queued.erase(i);}
 ~CGUILargeTextureManager(){for(auto* image:m_allocated)delete image;for(auto item:m_queued)delete item.second;}
};
'''

MAIN = r'''
struct Run {
 CJobManager jobs;CGUILargeTextureManager images;
 unsigned int Queue(State& state,const std::string& path="image-A"){auto* job=new CImageLoader(path,state.useCache);const auto id=jobs.Queue(job,&images);images.Register(id,path);return id;}
 void Cancel(unsigned int id){images.Drop(id);jobs.CancelJob(id);}
 void Execute(CJob* job,State& state){currentState=&state;const bool ok=job->DoWork();jobs.OnJobComplete(ok,job);currentState=nullptr;}
};
static int checks=0;
static void Expect(bool ok,const char* text){++checks;Check(ok,text);}
static void Ordinary(bool async,int route,bool failure=false,bool checking=false){
 State s;s.async=async;s.needsChecking=checking;s.cacheHit=route!=1;s.useCache=route!=2;
 s.directFails=route==3||failure;s.cacheFails=failure;
 {Run r;auto id=r.Queue(s);r.Execute(r.jobs.Start(id),s);
  Expect(r.images.m_queued.empty(),"ordinary: queue drained");Expect(r.images.m_allocated.size()==1,"ordinary: completion retained entry");
  auto* image=r.images.m_allocated[0];
  Expect(bool(image->texture)!=failure,"ordinary: texture publication matches decoder result");
  Expect(s.uploads==int(async&&!failure),"ordinary: ON uploads once / OFF no worker upload");
  Expect(s.refreshes==int(checking&&!failure&&route!=1),"ordinary: validation refresh preserved");
  Expect(s.decodes==(route==3||failure?2:1),"ordinary: exact direct/fallback branch traversal");
 }
 Expect(s.destroyed==int(!failure),"ordinary: texture owned once through completion");Expect(!s.destroyedWithLock,"ordinary: texture destruction outside callback locks");
}
static void CancelDuringDecode(bool fallback,bool checking,bool useCache=true){
 State s;s.useCache=useCache;s.cacheHit=!fallback;s.needsChecking=checking;s.blockDecode=1;
 Run r;auto id=r.Queue(s);CJob* job=r.jobs.Start(id);std::exception_ptr error;
 std::thread worker([&]{try{r.Execute(job,s);}catch(...){error=std::current_exception();}});
 s.AwaitDecode();r.Cancel(id);s.ReleaseDecode();worker.join();if(error)std::rethrow_exception(error);
 Expect(!s.uploads,"cancel-during-decode: obsolete job must not upload to GPU");
 Expect(!s.refreshes,"cancel-during-decode: obsolete job must not queue validation refresh");
 Expect(r.images.m_allocated.empty()&&r.images.m_queued.empty(),"cancel-during-decode: no publication");
 Expect(s.destroyed==1&&!s.destroyedWithLock,"cancel-during-decode: job owns decoded texture until safe deletion");
}
static void CancelBeforeWork(){
 State s;Run r;auto id=r.Queue(s);auto* job=r.jobs.Start(id);r.Cancel(id);r.Execute(job,s);
 Expect(!s.decodes&&!s.uploads&&!s.refreshes,"cancel-before-work: entry check avoids decode/upload");
 Expect(r.images.m_allocated.empty(),"cancel-before-work: no publication");
}
static void CancelQueued(){State s;Run r;auto id=r.Queue(s);r.Cancel(id);Expect(r.jobs.m_jobQueue[CJob::PRIORITY_NORMAL].empty(),"cancel-queued: removed before execution");Expect(!s.decodes&&!s.uploads,"cancel-queued: no work");}
static void OldAndNewSamePath(){
 State oldState,newState;oldState.blockDecode=1;Run r;auto oldId=r.Queue(oldState);auto* old=r.jobs.Start(oldId);std::exception_ptr error;
 std::thread worker([&]{try{r.Execute(old,oldState);}catch(...){error=std::current_exception();}});
 oldState.AwaitDecode();r.Cancel(oldId);
 auto newId=r.Queue(newState);Expect(oldId!=newId,"same-path: job identity differs");auto* newer=r.jobs.Start(newId);
 oldState.ReleaseDecode();worker.join();if(error)std::rethrow_exception(error);
 Expect(!oldState.uploads,"same-path: cancelled old decode does not upload");
 Expect(r.images.m_queuedLookup.size()==1&&r.images.m_queued[0].first==newId,"same-path: old completion cannot remove new queue entry");
 r.Execute(newer,newState);
 Expect(newState.uploads==1&&r.images.m_allocatedLookup.size()==1,"same-path: new job publishes once");
 Expect(r.images.m_allocatedLookup.at("image-A")->texture->state==&newState,"same-path: published texture belongs to new job");
 Expect(oldState.destroyed==1&&!oldState.destroyedWithLock,"same-path: obsolete ownership safe");
}
static void CopiedCallbackDoesNotPublishOldID(){
 State oldState,newState;Run r;auto oldId=r.Queue(oldState);auto* old=static_cast<CImageLoader*>(r.jobs.Start(oldId));
 // Model the real OnJobComplete unlock/copy boundary: the captured callback
 // can still run after cancellation. Execute actual large-manager callback.
 old->m_texture=std::make_unique<CTexture>(&oldState);r.Cancel(oldId);
 auto newId=r.Queue(newState);r.images.OnJobComplete(oldId,true,old);
 Expect(r.images.m_queued.size()==1&&r.images.m_queued[0].first==newId,"copied callback: old ID preserves newer same-path job");
 Expect(old->m_texture!=nullptr&&r.images.m_allocated.empty(),"copied callback: obsolete texture not published");
 r.jobs.OnJobComplete(true,old);r.Execute(r.jobs.Start(newId),newState);
 Expect(oldState.destroyed==1&&!oldState.destroyedWithLock,"copied callback: obsolete destruction safe");
}
static void FailureAndEmptyPath(){
 State s;s.emptyPath=true;Run r;auto id=r.Queue(s);r.Execute(r.jobs.Start(id),s);Expect(!s.decodes&&!s.uploads,"empty path: no decode/upload");
 State direct;direct.useCache=false;direct.directFails=true;Run d;id=d.Queue(direct);d.Execute(d.jobs.Start(id),direct);Expect(direct.decodes==1&&!direct.uploads,"no-cache decode failure does not fall back");
}
int main(int argc,char** argv){
 try{
  const std::string scenario=argc>1?argv[1]:"all";
  if(scenario=="witness-direct"){CancelDuringDecode(false,true);}
  else if(scenario=="witness-cache"){CancelDuringDecode(true,false);}
  else if(scenario=="entry"){CancelBeforeWork();}
  else{
   for(bool async:{false,true})for(int route=0;route<4;++route)Ordinary(async,route);
   Ordinary(true,0,false,true);Ordinary(false,0,false,true);Ordinary(true,0,true);Ordinary(false,0,true);
   CancelDuringDecode(false,false);CancelDuringDecode(false,true);CancelDuringDecode(true,false);CancelDuringDecode(false,false,false);
   CancelBeforeWork();CancelQueued();OldAndNewSamePath();CopiedCallbackDoesNotPublishOldID();FailureAndEmptyPath();
  }
  std::printf("PASS %s %d checks; decoder/GL fixtures, actual cancellation and publication methods\n",scenario.c_str(),checks);return 0;
 }catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


def read_sources(ref=None):
    names = ['xbmc/GUILargeTextureManager.cpp', 'xbmc/GUILargeTextureManager.h', 'xbmc/utils/JobManager.cpp']
    return {name: subprocess.check_output(['git', 'show', f'{ref}:{name}'], cwd=ROOT, text=True)
            if ref else (ROOT/name).read_text() for name in names}


def make_source(files, work=None):
    large, jobs = files['xbmc/GUILargeTextureManager.cpp'], files['xbmc/utils/JobManager.cpp']
    if 'class CTextureUploadJob' in large:
        # Demand-aware uploads moved out of the decode job. Reuse the complete
        # actual manager/job fixture rather than retaining a mirrored old state
        # machine. Historical eager-upload witnesses below still use the old
        # fixture unchanged. Cooperative decode cancellation must still return
        # failure even when cancelling cannot save any further GPU work.
        spec = importlib.util.spec_from_file_location('demand_fixture', ROOT / 'tools/test-image-upload-demand.py')
        demand = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demand)
        current = dict(files)
        if work is not None:
            original = EXTRACT.function(large, 'bool CImageLoader::DoWork()')
            current['xbmc/GUILargeTextureManager.cpp'] = large.replace(original, work, 1)
        return demand.source(current)
    methods = [work or EXTRACT.function(large, 'bool CImageLoader::DoWork()')]
    methods.extend(EXTRACT.function(jobs, sig) for sig in [
        'bool CJob::ShouldCancel(', 'void CJobManager::CancelJob(',
        'bool CJobManager::OnJobProgress(', 'void CJobManager::OnJobComplete('])
    methods.append(EXTRACT.function(large, 'void CGUILargeTextureManager::OnJobComplete('))
    return PRELUDE + '\n\n'.join(methods) + MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default=BASELINE)
    parser.add_argument('--out', '--out-dir', dest='out_dir', type=Path,
                        help='retain generated sources, compiler commands and results here')
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = Path(tempfile.mkdtemp(prefix='kodi-image-loader-cancel-'))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    files = read_sources(); work = EXTRACT.function(files['xbmc/GUILargeTextureManager.cpp'], 'bool CImageLoader::DoWork()')
    records = [{'sanitizers_requested':args.sanitize,'environment':{key:os.environ[key] for key in ('ASAN_OPTIONS','UBSAN_OPTIONS','LSAN_OPTIONS') if key in os.environ}}]

    def build(name, source, sanitize=False):
        cpp=args.out_dir/(name+'.cpp'); binary=args.out_dir/name;cpp.write_text(source)
        cmd=['g++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-pthread']
        if sanitize:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
        cmd += [str(cpp),'-o',str(binary)]
        result=subprocess.run(cmd,text=True,capture_output=True)
        (args.out_dir/(name+'-compile.log')).write_text(result.stdout+result.stderr)
        records.append({'build':name,'command':cmd,'returncode':result.returncode})
        if result.returncode:raise AssertionError(f'{name} compile failed: {result.stderr}')
        return binary

    def run(binary, scenario='all', success=True, witness=None):
        cmd=[str(binary),scenario];result=subprocess.run(cmd,text=True,capture_output=True,timeout=15,
            env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
        log=result.stdout+result.stderr;(args.out_dir/(binary.name+'-'+scenario+'.log')).write_text(log)
        records.append({'run':binary.name,'scenario':scenario,'returncode':result.returncode,'expected_success':success,'output':log})
        assert (result.returncode==0)==success,(binary.name,scenario,log)
        if not success:
            assert result.returncode==1 and 'FAIL ' in log,(binary.name,scenario,log)
            if witness is not None:
                assert result.stderr == 'FAIL ' + witness + '\n', (binary.name, scenario, log)
                assert not any(token in log for token in ('AddressSanitizer', 'UndefinedBehaviorSanitizer', 'runtime error:', 'DEADLYSIGNAL')), log
        print(binary.name,scenario,result.returncode,log.strip())

    try:
        baseline=build('baseline',make_source(read_sources(args.baseline_ref)))
        run(baseline,'witness-direct',False,'cancel-during-decode: obsolete job must not upload to GPU');run(baseline,'witness-cache',False,'cancel-during-decode: obsolete job must not upload to GPU')
        actual=build('actual',make_source(files),args.sanitize);run(actual)
        pattern=r'if\s*\(ShouldCancel\([^;{}]*?\)\)\s*(?:\{\s*)?return false;\s*(?:\})?'
        checkpoints=list(re.finditer(pattern,work))
        assert len(checkpoints)==3,f'Expected three cooperative checkpoints, got {len(checkpoints)}'
        for index,scenario in enumerate(['entry','witness-direct','witness-cache']):
            m=checkpoints[index];mutated=work[:m.start()]+work[m.end():]
            binary=build('missing-'+scenario,make_source(files,mutated))
            demand_aware = 'class CTextureUploadJob' in files['xbmc/GUILargeTextureManager.cpp']
            witness = ('entry cancellation prevents decode and upload' if scenario == 'entry' else
                       'cancelled decode returns failure at cooperative checkpoint') if demand_aware else None
            run(binary,scenario,False,witness)
        # Move the direct-branch guard behind its actual side effect. Historical
        # loaders upload eagerly; demand-aware loaders still schedule validation
        # refreshes. Returning false afterward cannot undo either operation.
        m=checkpoints[1];guard=m.group();mutated=work[:m.start()]+work[m.end():]
        assert 'ShouldCancel(0, 0)' in guard
        guard = '      if (ShouldCancel(0, 0))\n      {\n        return false;\n      }'
        demand_aware = 'class CTextureUploadJob' in files['xbmc/GUILargeTextureManager.cpp']
        call = re.search(r'BackgroundCacheImage\([^;]*\);' if demand_aware else
                         r'm_texture->LoadToGPUAsync\([^;]*\);', mutated)
        assert call, 'actual guarded side-effect call missing from direct branch'
        at = call.end()
        mutated=mutated[:at]+'\n'+guard+mutated[at:]
        name = 'late-after-refresh' if demand_aware else 'late-after-gpu'
        binary=build(name,make_source(files,mutated));run(binary,'witness-direct',False,
            'cancelled decode avoids refresh and upload' if demand_aware else None)
    finally:
        print('Artifacts:', args.out_dir)
        (args.out_dir/'commands-results.json').write_text(json.dumps(records,indent=2)+'\n')
        (args.out_dir/'provenance.json').write_text(json.dumps({'baseline_ref':args.baseline_ref,'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'production_file_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'limits':'Actual method execution with fixture decoder/GL, broker, admission and consumer release. No real driver, graphics-resource lifecycle, image decoder, production ReleaseImage or device performance validation. ThreadSanitizer not run.'},indent=2)+'\n')


if __name__=='__main__':main()
