#!/usr/bin/env python3
"""Exercise actual image request ownership, lease cancellation and publication.

Uses real threads/mutexes and extracted production classes/methods, with fixture
broker, job admission, decoder and GL/binding. No device or GPU timing claim.
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
SPEC = importlib.util.spec_from_file_location('loader_tests', ROOT / 'tools/test-image-loader-cancellation.py')
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)
BASELINE = '42e507705412e947efd3d1df9790c46bdc94aab8'
FILES = ['xbmc/GUILargeTextureManager.cpp', 'xbmc/GUILargeTextureManager.h',
         'xbmc/guilib/Texture.cpp', 'xbmc/utils/JobManager.cpp',
         'xbmc/guilib/TextureManager.cpp', 'xbmc/guilib/TextureManager.h']


def sources():
    return {p: (ROOT / p).read_text() for p in FILES}


def method(text, name):
    return BASE.EXTRACT.function(text, name)


def declaration(text, name):
    return method(text, name) + ';'


MAIN = r'''
static int checks=0;
static void Expect(bool ok,const char* text){++checks;Check(ok,text);}
struct Run {
 CJobManager jobs;CGUILargeTextureManager images;
 Run(){activeJobs=&jobs;}
 ~Run(){
  activeJobs=&jobs;
  while(!images.m_queued.empty())images.ReleaseImage(images.m_queued.front().second->GetPath(),true);
  while(!images.m_allocated.empty()){
   auto* image=images.m_allocated.front();
   if(image->m_refCount)images.ReleaseImage(image->GetPath(),true);else images.CleanupUnusedImages(true);
  }
  for(auto& queue:jobs.m_jobQueue)for(auto item:queue)item.FreeJob();
  for(auto item:jobs.m_processing)item.FreeJob();
  activeJobs=nullptr;
 }
 unsigned Queue(State& state,const std::string& path="image-A"){
  images.QueueImage(path,state.useCache);
  return images.m_queued.back().first;
 }
 void Execute(CJob* job,State& state){
  currentState=&state;bool success=false;
  try{success=job->DoWork();}catch(const std::runtime_error&){++state.errors;}
  jobs.OnJobComplete(success,job);currentState=nullptr;
 }
 void Cancel(const std::string& path="image-A"){images.ReleaseImage(path,true);}
};
static void WaitBind(State& s){
 const auto end=std::chrono::steady_clock::now()+3s;
 while(!s.bindEntered){Check(std::chrono::steady_clock::now()<end,"fixture bind timeout");std::this_thread::yield();}
}
static void ReleaseReferencesAndQueuedCancellation(){
 State s;Run r;auto id=r.Queue(s);auto* image=r.images.m_queued[0].second;
 auto token=image->GetCancellationToken();std::weak_ptr<std::atomic_bool> weak=token;
 auto* job=static_cast<CImageLoader*>(r.jobs.m_jobQueue[CJob::PRIORITY_NORMAL][0].m_job);
 Expect(token && !token->load(),"request token starts false");
 Expect(job->m_cancelled==token,"QueueImage passes the exact request token to its job");
 r.images.QueueImage("image-A",true);
 Expect(r.images.m_queued.size()==1&&image->m_refCount==2,"same-path consumers share one job/request");
 r.Cancel();Expect(!token->load()&&r.images.m_queued.size()==1,"first consumer release must not cancel shared request");
 token.reset();Expect(weak.use_count()==2,"request and queued job are the only token owners");
 r.Cancel();Expect(r.images.m_queued.empty()&&r.jobs.m_jobQueue[CJob::PRIORITY_NORMAL].empty(),"last queued release removes request and job");
 Expect(weak.expired(),"queued token retires after both production owners");
 Expect(r.jobs.next==id+1,"two consumers did not allocate another job");
}
static void LeaseWaitCancellation(bool fallback=false,bool failRelease=false,bool throwRelease=false){
 State s;s.cacheHit=!fallback;s.failRelease=failRelease;s.throwRelease=throwRelease;
 Run r;auto id=r.Queue(s);auto token=r.images.m_queued[0].second->GetCancellationToken();std::weak_ptr<std::atomic_bool> weak=token;
 auto* job=r.jobs.Start(id);auto* window=CServiceBroker::GetWinSystem();window->lease.lock();
 std::exception_ptr error;
 std::thread worker([&]{try{r.Execute(job,s);}catch(...){error=std::current_exception();}});
 WaitBind(s);r.Cancel();const bool marked=token->load();
 token.reset();const bool jobKeepsToken=!weak.expired();
 window->lease.unlock();worker.join();if(error)std::rethrow_exception(error);
 Expect(forbiddenLocks==0,"no manager lock may be acquired under upload lease");
 Expect(s.uploads==0&&s.syncs==0,"cancelled lease waiter must not upload or sync");
 Expect(marked,"last release marks token while job waits for lease");
 Expect(jobKeepsToken,"running job keeps cancelled token alive after request deletion");
 Expect(s.binds==1&&s.releases==1,"cancelled acquired lease releases exactly once");
 Expect(s.errors==int(failRelease||throwRelease),"cancelled lease release failures propagate");
 Expect(r.images.m_allocated.empty()&&r.images.m_queued.empty(),"cancelled waiter does not publish");
 Expect(weak.expired()&&s.destroyed==1&&!s.destroyedWithLock,"cancelled job/token safely retire outside callback locks");
}
static void FirstReleaseKeepsLiveWaiter(){
 State s;Run r;auto id=r.Queue(s);r.images.QueueImage("image-A",true);
 auto token=r.images.m_queued[0].second->GetCancellationToken();auto* job=r.jobs.Start(id);
 auto* window=CServiceBroker::GetWinSystem();window->lease.lock();std::thread worker([&]{r.Execute(job,s);});
 WaitBind(s);r.Cancel();Expect(!token->load(),"one remaining consumer keeps waiter live");
 window->lease.unlock();worker.join();
 Expect(s.uploads==1&&s.syncs==1&&s.releases==1,"shared live waiter uploads and releases normally");
 Expect(r.images.m_allocated.size()==1&&r.images.m_allocated[0]->GetTexture().size()==1,"shared live waiter publishes");
 r.Cancel();Expect(token->load()&&s.destroyed==1,"allocated last release retires only its completed request");
}
static void BeforeBindAndDefault(){
 State s;currentState=&s;CTexture texture(&s);std::atomic_bool cancelled{true};
 texture.LoadToGPUAsync(&cancelled);
 Expect(!s.binds&&!s.uploads&&!s.releases,"pre-cancelled request never attempts bind");
 texture.LoadToGPUAsync();
 Expect(s.binds==1&&s.uploads==1&&s.syncs==1&&s.releases==1,"null token retains ordinary upload behavior");
 s.bindFails=true;texture.LoadToGPUAsync();
 Expect(s.binds==2&&s.uploads==1&&s.releases==1,"bind failure retains render-thread fallback");
 currentState=nullptr;
}
static void SamePathAndCopiedCallback(){
 State oldState,newState;Run r;auto oldID=r.Queue(oldState);auto oldToken=r.images.m_queued[0].second->GetCancellationToken();
 auto* old=static_cast<CImageLoader*>(r.jobs.Start(oldID));old->m_texture=std::make_unique<CTexture>(&oldState);
 // Real JobManager can copy this callback before cancellation. Its invocation
 // is delayed here until actual consumer release and replacement admission.
 auto callback=r.jobs.m_processing[0].m_callback;r.Cancel();auto newID=r.Queue(newState);
 auto newToken=r.images.m_queued[0].second->GetCancellationToken();
 Expect(oldToken!=newToken&&oldToken->load()&&!newToken->load(),"same-path replacement owns an independent live token");
 callback->OnJobComplete(oldID,true,old);
 Expect(r.images.m_allocated.empty()&&r.images.m_queued[0].first==newID&&old->m_texture,"copied old callback cannot publish/remove replacement");
 r.jobs.OnJobComplete(true,old);r.Execute(r.jobs.Start(newID),newState);
 Expect(newState.uploads==1&&r.images.m_allocatedLookup.at("image-A")->GetTexture().size()==1,"replacement uploads and publishes once");
 Expect(oldState.destroyed==1&&!oldState.destroyedWithLock,"copied callback retains safe old texture ownership");
}
static void CancelActiveUpload(){
 State s;s.blockDecode=100;Run r;auto id=r.Queue(s);auto* job=r.jobs.Start(id);
 std::thread worker([&]{r.Execute(job,s);});s.AwaitDecode();r.Cancel();s.ReleaseDecode();worker.join();
 Expect(s.uploads==1&&s.syncs==1&&s.releases==1,"already active upload finishes and releases safely");
 Expect(r.images.m_allocated.empty()&&s.destroyed==1,"active cancelled upload does not publish");
}
static void FailedDirectDecode(){
 State s;s.directFails=true;s.blockDecode=1;Run r;auto id=r.Queue(s);auto* job=r.jobs.Start(id);
 std::thread worker([&]{r.Execute(job,s);});s.AwaitDecode();r.Cancel();s.ReleaseDecode();worker.join();
 Expect(s.decodes==1&&!s.uploads&&r.images.m_allocated.empty(),"cancelled failed direct decode must not start fallback");
}
static void OrdinaryRoutes(){
 for(bool async:{false,true})for(int route=0;route<4;++route){
  State s;s.async=async;s.cacheHit=route!=1;s.useCache=route!=2;s.directFails=route==3;
  Run r;auto id=r.Queue(s);r.Execute(r.jobs.Start(id),s);
  Expect(s.uploads==int(async)&&s.syncs==int(async),"ordinary OFF/ON upload behavior preserved");
  Expect(r.images.m_allocated.size()==1&&r.images.m_allocated[0]->GetTexture().size()==1,"ordinary successful publication preserved");
 }
}
int main(int argc,char** argv){try{
 const std::string mode=argc>1?argv[1]:"all";
 if(mode=="lease")LeaseWaitCancellation();
 else if(mode=="lease-cache")LeaseWaitCancellation(true);
 else if(mode=="refs")ReleaseReferencesAndQueuedCancellation();
 else if(mode=="prebind")BeforeBindAndDefault();
 else if(mode=="failed-decode")FailedDirectDecode();
 else if(mode=="failed-release")LeaseWaitCancellation(false,true);
 else {
  ReleaseReferencesAndQueuedCancellation();LeaseWaitCancellation();LeaseWaitCancellation(true);
  LeaseWaitCancellation(false,true);LeaseWaitCancellation(false,false,true);
  FirstReleaseKeepsLiveWaiter();BeforeBindAndDefault();SamePathAndCopiedCallback();
  CancelActiveUpload();FailedDirectDecode();OrdinaryRoutes();
 }
 std::printf("PASS %s %d checks; actual QueueImage/ReleaseImage/request ownership and texture upload; binding/decoder/GL fixtures\n",mode.c_str(),checks);return 0;
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''



def current_generate(files, overrides=None):
    """Real current manager plus actual CTexture upload lease, with owned GL/window."""
    overrides = overrides or {}
    spec = importlib.util.spec_from_file_location('demand_request_fixture', ROOT/'tools/test-image-upload-demand.py')
    demand = importlib.util.module_from_spec(spec);spec.loader.exec_module(demand)
    selected = dict(files)
    for signature, replacement in overrides.items():
        path = 'xbmc/guilib/Texture.cpp' if signature.startswith('void CTexture::') else 'xbmc/GUILargeTextureManager.cpp'
        original = method(selected[path], signature)
        selected[path] = selected[path].replace(original, replacement, 1)
    pre = demand.PRELUDE.replace('static unsigned checks=0,cases=0;', 'static unsigned checks=0;')
    pre = pre.replace('static thread_local int heldLocks=0;', 'static thread_local int heldLocks=0; static thread_local bool leaseHeld=false; static std::atomic<int> forbiddenLocks{0};')
    pre = pre.replace('void lock(){mutex.lock();++heldLocks;}', 'void lock(){if(leaseHeld){++forbiddenLocks;throw std::runtime_error("fixture lock under upload lease");}mutex.lock();++heldLocks;}')
    pre = pre.replace(' bool uploadUnsupported=false,throwUpload=false;', ''' bool failRelease=false,throwRelease=false,bindFails=false;
 std::atomic<bool>bindEntered{false};std::atomic<int>binds{0},syncs{0},errors{0},destroyedWithLock{0};
 bool uploadUnsupported=false,throwUpload=false;''')
    pre=pre.replace('~CTexture(){++state->destroyed;if(uploaded&&heldLocks)++state->gpuDestroyedWithLock;}',
        '~CTexture(){++state->destroyed;if(heldLocks)++state->destroyedWithLock;if(uploaded&&heldLocks)++state->gpuDestroyedWithLock;}')
    # The window deliberately grants the lease to isolate actual CTexture's
    # post-bind cancellation. Native locked admission has a separate suite.
    old = method(pre, ' void LoadToGPUAsync(')
    pre = pre.replace(old, ''' void LoadToGPUAsync(const std::atomic_bool*cancelled=nullptr);
 void LoadToGPU(){++state->uploads;uploaded=true;state->Decode(100);}
 void SyncGPU(){++state->syncs;}''',1)
    window = method(pre, 'struct FakeWindow')+';'
    pre = pre.replace(window, '''struct FakeWindow {
 FakeGraphics gfx;std::mutex lease;
 FakeGraphics&GetGfxContext(){return gfx;}bool HasContext(){return false;}
 bool BindTextureUploadContext(const std::atomic_bool* =nullptr){auto&s=*currentState;++s.binds;s.bindEntered=true;if(s.bindFails)return false;lease.lock();leaseHeld=true;++s.leases;return true;}
 bool UnbindTextureUploadContext(){auto&s=*currentState;++s.releases;--s.leases;leaseHeld=false;lease.unlock();if(s.throwRelease)throw std::runtime_error("fixture release throw");return !s.failRelease;}
};using CWinSystemBase=FakeWindow;''',1)
    array_stub=method(pre,'struct CTextureArray')+';'
    pre=pre.replace(array_stub,declaration(files['xbmc/guilib/TextureManager.h'],'class CTextureArray'),1)
    pre=pre.replace(' unsigned GetHeight()const{', ' unsigned GetTextureWidth()const{return GetWidth();} unsigned GetTextureHeight()const{return GetHeight();} int GetOrientation()const{return 0;}\n unsigned GetHeight()const{',1)
    pre=pre.replace('struct FakeGraphics {','struct FakeGraphics : CCriticalSection {',1)
    arrays='\n'.join(method(files['xbmc/guilib/TextureManager.cpp'],signature) for signature in
        ['CTextureArray::CTextureArray()', 'unsigned int CTextureArray::size() const',
         'void CTextureArray::Reset()', 'void CTextureArray::Add(', 'void CTextureArray::Set(', 'void CTextureArray::Free()'])
    arrays+='\nCTextureArray::~CTextureArray() = default;\n'
    generated=demand.source(selected)
    assert generated.startswith(demand.PRELUDE) and generated.endswith(demand.TESTS)
    generated=pre+generated[len(demand.PRELUDE):-len(demand.TESTS)]
    upload=method(selected['xbmc/guilib/Texture.cpp'],'void CTexture::LoadToGPUAsync(')
    main=MAIN
    main=main.replace('static int checks=0;', 'static int requestChecks=0;').replace('++checks;', '++requestChecks;').replace('mode.c_str(),checks)', 'mode.c_str(),requestChecks)')
    main=main.replace('  images.QueueImage(path,state.useCache);', '  currentState=&state;images.QueueImage(path,state.useCache);')
    main=main.replace(' void Execute(CJob* job,State& state){', ''' unsigned QueueUpload(State&state,const std::string&path="image-A"){
  auto id=Queue(state,path);Execute(jobs.Start(id),state);currentState=&state;
  images.RequestImageUpload(path);return images.m_allocatedLookup.at(path)->m_uploadJobID;
 }
 void Execute(CJob* job,State& state){''')
    main=main.replace('while(!s.bindEntered)', 'while(!s.bindEntered)')
    main=main.replace('s.AwaitDecode()', 's.decodeBarrier.Await()').replace('s.ReleaseDecode()', 's.decodeBarrier.Release()')
    # Decode and upload now have distinct admission/publication phases.
    for a,b in [('static void LeaseWaitCancellation(', 'static void FirstReleaseKeepsLiveWaiter()'),
                ('static void FirstReleaseKeepsLiveWaiter()', 'static void BeforeBindAndDefault()'),
                ('static void CancelActiveUpload()', 'static void FailedDirectDecode()')]:
        start=main.index(a);end=main.index(b,start+len(a));part=main[start:end]
        part=part.replace('r.Queue(s)', 'r.QueueUpload(s)').replace('r.images.m_queued[0].second->GetCancellationToken()', 'r.images.m_allocatedLookup.at("image-A")->GetCancellationToken()')
        if 'FirstReleaseKeepsLiveWaiter' in a:
            part=part.replace('r.images.QueueImage("image-A",true);', 'CTextureArray acquired;r.images.GetImage("image-A",acquired,true);')
        main=main[:start]+part+main[end:]
    main=main.replace('r.Execute(r.jobs.Start(newID),newState);', '''r.Execute(r.jobs.Start(newID),newState);currentState=&newState;r.images.RequestImageUpload("image-A");
 r.Execute(r.jobs.Start(r.images.m_allocatedLookup.at("image-A")->m_uploadJobID),newState);''')
    main=main.replace('r.Execute(r.jobs.Start(id),s);\n  Expect(s.uploads==int(async)', '''r.Execute(r.jobs.Start(id),s);
  if(async){currentState=&s;r.images.RequestImageUpload("image-A");r.Execute(r.jobs.Start(r.images.m_allocatedLookup.at("image-A")->m_uploadJobID),s);}
  Expect(s.uploads==int(async)''')
    # Demand fixture owns queue cleanup; no duplicate FreeJob on teardown.
    main=main.replace('  for(auto& queue:jobs.m_jobQueue)for(auto item:queue)item.FreeJob();\n  for(auto item:jobs.m_processing)item.FreeJob();\n', '')
    return generated+arrays+upload+'\n'+main

def generate(files, overrides=None):
    if "class CTextureUploadJob" in files["xbmc/GUILargeTextureManager.cpp"]:
        return current_generate(files, overrides)
    overrides = overrides or {}
    large = files['xbmc/GUILargeTextureManager.cpp']
    header = files['xbmc/GUILargeTextureManager.h']
    pre = BASE.PRELUDE
    pre = pre.replace('#include <algorithm>', '#include <algorithm>\n#include <cassert>')
    pre = pre.replace('static thread_local int heldLocks=0;', 'static thread_local int heldLocks=0; static thread_local bool leaseHeld=false; static std::atomic<int> forbiddenLocks{0};')
    pre = pre.replace('void lock(){mutex.lock();++heldLocks;}', 'void lock(){if(leaseHeld){++forbiddenLocks;throw std::runtime_error("fixture lock under upload lease");}mutex.lock();++heldLocks;}')
    pre = pre.replace(' int blockDecode=0;', ''' int blockDecode=0;
 bool failRelease=false,throwRelease=false,bindFails=false;
 std::atomic<bool> bindEntered{false};
 std::atomic<int> binds{0},releases{0},syncs{0},errors{0},leases{0};''')
    pre = pre.replace('static thread_local State* currentState=nullptr;',
                      'static thread_local State* currentState=nullptr;\nclass CJobManager; static CJobManager* activeJobs=nullptr;')
    pre = pre.replace(' void LoadToGPUAsync(const std::atomic_bool* =nullptr){++state->uploads;}', ''' void LoadToGPUAsync(const std::atomic_bool* cancelled=nullptr);
 void LoadToGPU(){++state->uploads;state->DecodeBarrier(100);}
 void SyncGPU(){++state->syncs;}
 unsigned GetWidth()const{return 64;} unsigned GetHeight()const{return 32;}
 unsigned GetTextureWidth()const{return 64;} unsigned GetTextureHeight()const{return 32;}
 int GetOrientation()const{return 0;}''')
    pre = pre.replace('struct FakeGraphics {', 'struct FakeGraphics : CCriticalSection {')
    pre = pre.replace('struct FakeWindow {FakeGraphics gfx;FakeGraphics& GetGfxContext(){return gfx;}};', '''struct FakeWindow {
 FakeGraphics gfx;std::mutex lease;
 FakeGraphics& GetGfxContext(){return gfx;} bool HasContext(){return false;}
 // Deliberately admit a lease to isolate CTexture's post-bind cancellation and
 // release paths. The real locked admission gate has its own threaded tests.
 bool BindTextureUploadContext(const std::atomic_bool* =nullptr){auto& s=*currentState;++s.binds;s.bindEntered=true;if(s.bindFails)return false;lease.lock();leaseHeld=true;++s.leases;return true;}
 bool UnbindTextureUploadContext(){auto& s=*currentState;++s.releases;--s.leases;leaseHeld=false;lease.unlock();if(s.throwRelease)throw std::runtime_error("fixture release throw");return !s.failRelease;}
};using CWinSystemBase=FakeWindow;''')
    pre = pre.replace(' static FakeGUI* GetGUI()', ' static CJobManager* GetJobManager(){return activeJobs;}\n static FakeGUI* GetGUI()')
    a = pre.index('struct CImageLoader : CJob {')
    b = pre.index('struct CWorkItem {', a)
    pre = pre[:a] + declaration(header, 'class CImageLoader : public CJob').replace('private:', 'public:') + '\n' + pre[b:]
    pre = pre.replace(' unsigned int Queue(CJob* job,IJobCallback* callback)',
                      ' unsigned int AddJob(CJob* job,IJobCallback* callback,CJob::PRIORITY){return Queue(job,callback);}\n unsigned int Queue(CJob* job,IJobCallback* callback)')
    a = pre.index('class CGUILargeTextureManager : public IJobCallback {')
    pre = pre[:a] + declaration(files['xbmc/guilib/TextureManager.h'], 'class CTextureArray') + '\n' + declaration(header, 'class CGUILargeTextureManager : public IJobCallback').replace('private:', 'public:') + '\n'
    pre += 'struct CTimeUtils {static unsigned GetFrameTime(){return 100;}};\n'
    methods = []
    for signature in ['CImageLoader::CImageLoader(', 'bool CImageLoader::DoWork()',
                      'CGUILargeTextureManager::CLargeTexture::CLargeTexture(',
                      'CGUILargeTextureManager::CLargeTexture::~CLargeTexture()',
                      'void CGUILargeTextureManager::CLargeTexture::AddRef()',
                      'bool CGUILargeTextureManager::CLargeTexture::DecrRef(',
                      'bool CGUILargeTextureManager::CLargeTexture::DeleteIfRequired(',
                      'void CGUILargeTextureManager::CLargeTexture::SetTexture(',
                      'void CGUILargeTextureManager::CleanupUnusedImages(',
                      'bool CGUILargeTextureManager::GetImage(',
                      'void CGUILargeTextureManager::ReleaseImage(',
                      'void CGUILargeTextureManager::QueueImage(',
                      'void CGUILargeTextureManager::OnJobComplete(']:
        methods.append(overrides.get(signature) or method(large, signature))
    for signature in ['CImageLoader::~CImageLoader()', 'CGUILargeTextureManager::CGUILargeTextureManager()',
                      'CGUILargeTextureManager::~CGUILargeTextureManager()']:
        default = re.search(re.escape(signature) + r'\s*=\s*default;', large)
        assert default, signature
        methods.append(default.group())
    for signature in ['bool CJob::ShouldCancel(', 'void CJobManager::CancelJob(',
                      'bool CJobManager::OnJobProgress(', 'void CJobManager::OnJobComplete(']:
        methods.append(method(files['xbmc/utils/JobManager.cpp'], signature))
    signature = 'void CTexture::LoadToGPUAsync('
    methods.append(overrides.get(signature) or method(files['xbmc/guilib/Texture.cpp'], signature))
    arrays = files['xbmc/guilib/TextureManager.cpp']
    for signature in ['CTextureArray::CTextureArray()', 'unsigned int CTextureArray::size() const',
                      'void CTextureArray::Reset()', 'void CTextureArray::Add(',
                      'void CTextureArray::Set(', 'void CTextureArray::Free()']:
        methods.append(method(arrays, signature))
    methods.append('CTextureArray::~CTextureArray() = default;')
    return pre + '\n\n'.join(methods) + MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='image-request-cancel-'))
    out.mkdir(parents=True, exist_ok=True)
    files = sources()
    records = []

    def build(name, overrides=None, selected=None):
        source = generate(files if selected is None else selected, overrides)
        cpp, binary = out / (name + '.cpp'), out / name
        cpp.write_text(source)
        command = ['g++', '-std=c++17', '-O1', '-g', '-pthread', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter']
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie']
        command += [str(cpp), '-o', str(binary)]
        c = subprocess.run(command, capture_output=True, text=True)
        (out / (name + '.compile.log')).write_text(c.stdout + c.stderr)
        records.append({'compile': command, 'exit': c.returncode})
        if c.returncode:
            raise RuntimeError(c.stderr)
        return binary

    def run(binary, case, witness=None):
        r = subprocess.run([str(binary), case], capture_output=True, text=True, timeout=15,
            env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
        text = r.stdout + r.stderr
        (out / (binary.name + '-' + case + '.log')).write_text(text)
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n' and not any(t in text for t in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'run': [str(binary), case], 'exit': r.returncode,
                        'expected_semantic_failure': witness, 'accepted': accepted, 'output': text})
        if not accepted:
            raise RuntimeError((binary.name, case, r.returncode, text))
        print(binary.name, case, r.returncode, text.strip())

    try:
        run(build('actual'), 'all')
        old = subprocess.check_output(['git', 'show', BASELINE + ':xbmc/GUILargeTextureManager.cpp'], cwd=ROOT, text=True)
        legacy = {sig: method(old, sig) for sig in ['void CGUILargeTextureManager::QueueImage(',
                  'CGUILargeTextureManager::CLargeTexture::~CLargeTexture()', 'bool CImageLoader::DoWork()']}
        # Current declarations/constructor keep the old methods source-compatible;
        # the historical queue supplies no token and the destructor never marks it.
        historical = {p: subprocess.check_output(['git','show','f5bb418483e6f20e84a7d18f1822c67e4ca321ba:'+p],cwd=ROOT,text=True) for p in FILES}
        baseline = build('baseline-request-methods', legacy, historical)
        run(baseline, 'lease', 'cancelled lease waiter must not upload or sync')
        run(baseline, 'failed-decode', 'cancelled failed direct decode must not start fallback')
        upload = method(files['xbmc/guilib/Texture.cpp'], 'void CTexture::LoadToGPUAsync(')
        large = files['xbmc/GUILargeTextureManager.cpp']
        queue = method(large, 'void CGUILargeTextureManager::QueueImage(')
        destructor = method(large, 'CGUILargeTextureManager::CLargeTexture::~CLargeTexture()')
        work = method(large, 'bool CImageLoader::DoWork()')
        demand_aware = 'class CTextureUploadJob' in large
        gpu_signature = '  bool DoWork() override' if demand_aware else 'bool CImageLoader::DoWork()'
        gpu_work = method(large, gpu_signature)
        calls = list(re.finditer(re.escape('LoadToGPUAsync(m_cancelled.get())'), gpu_work))
        assert len(calls) == (1 if demand_aware else 2)
        cache_call = calls[-1]
        missing_cache = gpu_work[:cache_call.start()] + 'LoadToGPUAsync()' + gpu_work[cache_call.end():]
        token_signature = ('CGUILargeTextureManager::queueIterator CGUILargeTextureManager::StartQueuedImage('
                           if '::StartQueuedImage(' in large else 'void CGUILargeTextureManager::QueueImage(')
        token_work = method(large, token_signature)
        decr = method(large, 'bool CGUILargeTextureManager::CLargeTexture::DecrRef(')
        cases = [
            ('job-lock-under-lease', {'void CTexture::LoadToGPUAsync(': upload.replace('} guard{window};', '} guard{window};\n  CServiceBroker::GetJobManager()->OnJobProgress(0, 0, nullptr);')}, 'lease', 'no manager lock may be acquired under upload lease'),
            ('missing-loader-direct-token', {gpu_signature: gpu_work.replace('LoadToGPUAsync(m_cancelled.get())', 'LoadToGPUAsync()', 1)}, 'lease', 'cancelled lease waiter must not upload or sync'),
            ('missing-loader-cache-token', {gpu_signature: missing_cache}, 'lease-cache', 'cancelled lease waiter must not upload or sync'),
            ('premature-shared-cancel', {'bool CGUILargeTextureManager::CLargeTexture::DecrRef(': decr.replace('m_refCount--;', 'm_refCount--; m_cancelled->store(true);')}, 'refs', 'first consumer release must not cancel shared request'),
            ('missing-token-transfer', {token_signature: token_work.replace('image->GetCancellationToken()', '{}')}, 'refs', 'QueueImage passes the exact request token to its job'),
            ('missing-token-store', {'CGUILargeTextureManager::CLargeTexture::~CLargeTexture()': re.sub(r'm_cancelled->store\([^;]+;', '', destructor)}, 'lease', 'cancelled lease waiter must not upload or sync'),
            ('missing-prebind-check', {'void CTexture::LoadToGPUAsync(': upload.replace('  if (cancelled && cancelled->load(std::memory_order_acquire))\n    return;\n', '')}, 'prebind', 'pre-cancelled request never attempts bind'),
            ('missing-postbind-check', {'void CTexture::LoadToGPUAsync(': upload.replace('if (!cancelled || !cancelled->load(std::memory_order_acquire))', 'if (true)')}, 'lease', 'cancelled lease waiter must not upload or sync'),
            ('ignored-release-failure', {'void CTexture::LoadToGPUAsync(': upload.replace('if (!guard.Release())', 'if ((guard.Release(), false))')}, 'failed-release', 'cancelled lease release failures propagate'),
        ]
        for name, changes, case, witness in cases:
            assert generate(files, changes) != generate(files), name
            run(build(name, changes), case, witness)
    finally:
        (out / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
        (out / 'provenance.json').write_text(json.dumps({
            'baseline': BASELINE, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256': {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()},
            'limits': 'Extracted production methods/classes (private exposed for inspection), fixture broker/admission/binding/GL/decoder. No real EGL, full worker scheduler, shutdown orchestration or device validation.'}, indent=2) + '\n')
        print('Artifacts:', out)


if __name__ == '__main__':
    main()
