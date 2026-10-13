#!/usr/bin/env python3
"""Bounded corrected-path checks of extracted production methods.

Uses native host threads. Interpreter, logging, file lookup, events and platform
adapter are explicit substitutes; this is not embedded-Python or CE runtime proof.
There is no baseline, fault injection, mutation, stress or exhaustion entrypoint.
"""
from pathlib import Path
import os
import runpy
import subprocess
import argparse
import tempfile

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <future>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
class ILanguageInvoker;class CScriptInvocationManager;
namespace ADDON{struct IAddon{std::string LibPath() const{return "unused.py";}};using AddonPtr=std::shared_ptr<IAddon>;}
constexpr int LOGDEBUG=0,LOGERROR=1;
struct CLog{template<class...T>static void Log(int,const char*,const T&...){};};
struct CEvent{
 bool value;std::mutex mutex;std::condition_variable changed;
 explicit CEvent(bool=false,bool signaled=false):value(signaled){}
 void Set(){std::lock_guard<std::mutex> lock(mutex);value=true;changed.notify_all();}
 void Reset(){std::lock_guard<std::mutex> lock(mutex);value=false;}
 bool Signaled(){std::lock_guard<std::mutex> lock(mutex);return value;}
 void Wait(){std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[&]{return value;});}
 template<class R,class P>bool Wait(std::chrono::duration<R,P> d){
  std::unique_lock<std::mutex> lock(mutex);return changed.wait_for(lock,d,[&]{return value;});
 }
};
namespace XbmcThreads{struct CEventGroup{CEventGroup(std::initializer_list<CEvent*>){} const CEvent* wait(){return nullptr;}template<class T>const CEvent* wait(T){return nullptr;}};}
namespace XbmcCommons{struct UncheckedException{void LogThrowMessage(const char*)const{}};}
struct CSingleExit{CCriticalSection& mutex;explicit CSingleExit(CCriticalSection& m):mutex(m){mutex.unlock();}~CSingleExit(){mutex.lock();}};
struct CFileUtils{static bool Exists(const std::string& s,bool=false){return s!="missing.py";}};
struct URIUtils{static std::string GetExtension(const std::string&){return ".py";}};
struct StringUtils{static void ToLower(std::string&){};};
namespace KODI::TIME{template<class T>void Sleep(T value){std::this_thread::sleep_for(value);}}
'''

PLATFORM = r'''
struct IRunnable{virtual ~IRunnable()=default;virtual void Run()=0;};
struct IThreadImpl{
 static std::unique_ptr<IThreadImpl> CreateThreadImpl(std::thread::native_handle_type){return std::make_unique<IThreadImpl>();}
 void SetThreadInfo(const std::string&){}
 bool SetPriority(const ThreadPriority&){return true;}
};
'''

TESTS = r'''
template<class F>void Await(F ready){
 auto until=std::chrono::steady_clock::now()+2s;
 while(!ready()){assert(std::chrono::steady_clock::now()<until);std::this_thread::sleep_for(1ms);}
}
struct Handler:ILanguageInvocationHandler{
 std::atomic<int> starts{0},ends{0},executions{0};
 ILanguageInvoker* CreateInvoker()override;
 void OnScriptStarted(ILanguageInvoker*)override{++starts;}
 void OnExecutionEnded(ILanguageInvoker*)override{++ends;}
};
struct FakeInvoker:ILanguageInvoker{
 Handler& handler;std::atomic<int> ready{0},entered{0};
 CEvent gate{false,true};std::vector<std::vector<std::string>> calls;
 explicit FakeInvoker(Handler& h):ILanguageInvoker(&h),handler(h){}
 bool execute(const std::string&,const std::vector<std::string>& args)override{
  setState(InvokerStateRunning);++handler.executions;++entered;gate.Wait();
  calls.push_back(args);setState(InvokerStateScriptDone);++ready;return true;
 }
 bool stop(bool)override{gate.Set();return true;}
 void onExecutionDone()override{setState(InvokerStateExecutionDone);ILanguageInvoker::onExecutionDone();}
};
ILanguageInvoker* Handler::CreateInvoker(){return new FakeInvoker(*this);}
void Setup(CScriptInvocationManager& manager,Handler& handler){manager.m_invocationHandlers[".py"]=&handler;}
std::shared_ptr<FakeInvoker> Inner(const CLanguageInvokerThreadPtr& thread){return std::static_pointer_cast<FakeInvoker>(thread->GetInvoker());}
void Idle(const CLanguageInvokerThreadPtr& thread,const std::string& script){Await([&]{return thread->Reuseable(script);});}
void Retire(CScriptInvocationManager& manager){manager.StopRunningScripts(true);manager.Process();assert(manager.m_scripts.empty());assert(manager.m_scriptPaths.empty());}

void StartupAndSequentialReuse(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 int id=manager.ExecuteAsync("reuse.py",ADDON::AddonPtr(),{"first"},true,44);assert(id>=0);
 auto thread=manager.m_lastInvokerThread;auto inner=Inner(thread);Idle(thread,"reuse.py");
 assert(thread->m_StartEvent.Signaled()&&inner->GetId()==id&&inner->ready==1);
 assert(manager.GetReusablePluginHandle("reuse.py")==44);
 assert(manager.ExecuteAsync("reuse.py",ADDON::AddonPtr(),{"second"},true,44)==id);
 Idle(thread,"reuse.py");assert(inner->ready==2&&handler.starts==2&&handler.executions==2);
 assert(inner->calls==std::vector<std::vector<std::string>>({{"first"},{"second"}}));
 assert(manager.m_scripts.size()==1&&manager.m_lastInvokerThread==thread);
 assert(manager.Stop(id,true));assert(!thread->CThread::IsRunning());manager.Process();
 assert(handler.ends==1&&!manager.IsRunning(id)&&!manager.IsRunning("reuse.py"));
 std::cout<<"PASS successful startup receipt, argument dispatch, sequential interpreter reuse, Stop(true) and path cleanup\n";
}

void ExclusiveClaimAndOrdinaryCalls(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 int id=manager.ExecuteAsync("kept.py",ADDON::AddonPtr(),{},true,51);assert(id>=0);
 auto kept=manager.m_lastInvokerThread;Idle(kept,"kept.py");
 CEvent start;
 auto first=std::async(std::launch::async,[&]{start.Wait();return manager.GetReusablePluginHandle("kept.py");});
 auto second=std::async(std::launch::async,[&]{start.Wait();return manager.GetReusablePluginHandle("kept.py");});
 start.Set();assert(first.wait_for(2s)==std::future_status::ready&&second.wait_for(2s)==std::future_status::ready);
 int a=first.get(),b=second.get();assert((a==51&&b==-1)||(b==51&&a==-1));
 int ordinary=manager.ExecuteAsync("ordinary.py",ADDON::AddonPtr(),{},false,52);assert(ordinary>=0&&ordinary!=id);
 Await([&]{return !manager.IsRunning(ordinary);});assert(manager.m_lastInvokerThread==kept);
 int overlap=manager.ExecuteAsync("kept.py",ADDON::AddonPtr(),{},true,53);assert(overlap>=0&&overlap!=id);
 Await([&]{return !manager.IsRunning(overlap);});assert(manager.m_lastInvokerThread==kept&&Inner(kept)->ready==1);
 assert(manager.ExecuteAsync("kept.py",ADDON::AddonPtr(),{},true,51)==id);Idle(kept,"kept.py");
 assert(Inner(kept)->ready==2);Retire(manager);assert(handler.executions==4&&handler.ends==3);
 std::cout<<"PASS exactly one of two callers claims kept handle, ordinary calls preserve reservation, claimant reuses original\n";
}

void BusyIsolationAndClaimRelease(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 auto busy=std::make_shared<FakeInvoker>(handler);busy->gate.Reset();
 int id=manager.ExecuteAsync("busy.py",busy,{}, {},true,61);assert(id>=0);
 auto kept=manager.m_lastInvokerThread;Await([&]{return busy->entered==1;});
 assert(manager.GetReusablePluginHandle("busy.py")==-1);
 int other=manager.ExecuteAsync("busy.py",ADDON::AddonPtr(),{},true,62);assert(other>=0&&other!=id);
 Await([&]{return !manager.IsRunning(other);});assert(manager.m_lastInvokerThread==kept);
 assert(busy->entered==1&&busy->ready==0);busy->gate.Set();Idle(kept,"busy.py");
 for(const std::string& path:{std::string(),std::string("missing.py")}){
  assert(manager.GetReusablePluginHandle("busy.py")==61);
  assert(manager.ExecuteAsync(path,ADDON::AddonPtr(),{},true,61)==-1);
  assert(!manager.m_lastInvokerClaimed&&Inner(kept)->ready==1);
 }
 assert(manager.GetReusablePluginHandle("busy.py")==61);
 assert(manager.ExecuteAsync("busy.py",busy,{}, {},true,61)==-1);
 assert(!manager.m_lastInvokerClaimed&&manager.m_lastInvokerThread==kept);
 assert(manager.GetReusablePluginHandle("busy.py")==61);
 assert(manager.ExecuteAsync("busy.py",ADDON::AddonPtr(),{},true,61)==id);Idle(kept,"busy.py");
 Retire(manager);assert(handler.executions==3&&handler.ends==2);
 std::cout<<"PASS busy invoker isolation, empty/missing-file claim cleanup and duplicate-owned-invoker rejection\n";
}

void ReleaseCancelAndAcceptedRestart(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 auto released=std::make_shared<FakeInvoker>(handler);
 CLanguageInvokerThread releaseThread(released,&manager,true);releaseThread.SetId(81);
 releaseThread.Release();assert(releaseThread.Execute("release.py",{"first"}));
 Await([&]{return !releaseThread.CThread::IsRunning();});assert(released->ready==1&&releaseThread.Retired());
 assert(!releaseThread.Reuseable("release.py"));
 auto cancelled=std::make_shared<FakeInvoker>(handler);
 CLanguageInvokerThread cancelThread(cancelled,&manager,true);cancelThread.SetId(82);
 assert(!cancelThread.Stop(true));assert(!cancelThread.Execute("cancel.py",{}));
 assert(cancelled->entered==0&&cancelled->GetId()==-1&&cancelThread.Retired());
 auto restarted=std::make_shared<FakeInvoker>(handler);
 CLanguageInvokerThread restartThread(restarted,&manager,true);restartThread.SetId(83);
 assert(restartThread.Execute("restart.py",{"first"}));
 Await([&]{return restartThread.Reuseable("restart.py");});restarted->Reset();
 assert(restartThread.Execute("restart.py",{"second"}));restartThread.Release();
 Await([&]{return !restartThread.CThread::IsRunning();});assert(restarted->ready==2&&restartThread.Retired());
 assert(restarted->calls==std::vector<std::vector<std::string>>({{"first"},{"second"}}));
 assert(handler.starts==3&&handler.ends==2&&handler.executions==3);
 std::cout<<"PASS release before launch, cancellation before launch and accepted restart followed by release\n";
}

struct HandleOwner{};
struct HandleProbe:CRunningScriptsHandler<HandleOwner>{
 using CRunningScriptsHandler::GetNewScriptHandle;using CRunningScriptsHandler::ReuseScriptHandle;
 using CRunningScriptsHandler::RemoveScriptHandle;using CRunningScriptsHandler::GetScriptFromHandle;
};
void HandleCleanup(){
 HandleOwner oldOwner,newOwner;int handle=HandleProbe::GetNewScriptHandle(&oldOwner);
 assert(HandleProbe::GetScriptFromHandle(handle)==&oldOwner);
 HandleProbe::ReuseScriptHandle(handle,&newOwner);HandleProbe::RemoveScriptHandle(handle,&oldOwner);
 assert(HandleProbe::GetScriptFromHandle(handle)==&newOwner);
 HandleProbe::RemoveScriptHandle(handle,&newOwner);assert(!HandleProbe::GetScriptFromHandle(handle));
 std::cout<<"PASS old script cleanup retains reused handle until its current owner removes it\n";
}
int main(){StartupAndSequentialReuse();ExclusiveClaimAndOrdinaryCalls();BusyIsolationAndClaimRelease();ReleaseCancelAndAcceptedRestart();HandleCleanup();}
'''


def generate():
    function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
    def read(path):
        return (ROOT / 'xbmc' / path).read_text()
    th, tc = read('threads/Thread.h'), read('threads/Thread.cpp')
    ih, ic = read('interfaces/generic/ILanguageInvoker.h'), read('interfaces/generic/ILanguageInvoker.cpp')
    lh, lc = read('interfaces/generic/LanguageInvokerThread.h'), read('interfaces/generic/LanguageInvokerThread.cpp')
    mh, mc = read('interfaces/generic/ScriptInvocationManager.h'), read('interfaces/generic/ScriptInvocationManager.cpp')
    assert 'TakeReusableThread' in mc, '#86 candidate has not been integrated yet'
    public = lambda value: value.replace('protected:', 'public:').replace('private:', 'public:')
    code = PRELUDE + function(th, 'enum class ThreadPriority') + ';\n' + PLATFORM
    code += public(function(th, 'class CThread\n')) + ';\nstatic thread_local CThread* currentThread;\n'
    for sig in ['CThread::CThread(const char*', 'CThread::CThread(IRunnable*', 'CThread::~CThread()',
                'void CThread::Create(', 'bool CThread::IsRunning()', 'bool CThread::SetPriority(',
                'bool CThread::IsAutoDelete()', 'void CThread::StopThread(', 'void CThread::Process()',
                'bool CThread::IsCurrentThread()', 'bool CThread::Join(std::chrono::milliseconds',
                'bool CThread::Join(const std::shared_ptr', 'void CThread::Action()']:
        code += function(tc, sig) + '\n'
    code += ih[ih.index('typedef enum'):ih.index('class ILanguageInvoker\n')]
    code += function(read('interfaces/generic/ILanguageInvocationHandler.h'), 'class ILanguageInvocationHandler\n') + ';\n'
    code += function(ih, 'class ILanguageInvoker\n') + ';\nusing LanguageInvokerPtr=std::shared_ptr<ILanguageInvoker>;\n'
    code += public(function(lh, 'class CLanguageInvokerThread :')).replace('protected CThread', 'public CThread') + ';\n'
    code += 'using CLanguageInvokerThreadPtr=std::shared_ptr<CLanguageInvokerThread>;\n'
    code += public(function(mh, 'class CScriptInvocationManager\n')) + ';\n'
    code += 'ILanguageInvoker::~ILanguageInvoker()=default;\n'
    for sig in ['ILanguageInvoker::ILanguageInvoker(', 'bool ILanguageInvoker::Execute(', 'bool ILanguageInvoker::Stop(',
                'bool ILanguageInvoker::IsActive()', 'bool ILanguageInvoker::IsRunning()', 'bool ILanguageInvoker::IsStopping()',
                'void ILanguageInvoker::onExecutionDone()', 'void ILanguageInvoker::onExecutionFailed()',
                'void ILanguageInvoker::setState(', 'void ILanguageInvoker::AbortNotification()',
                'void ILanguageInvoker::pulseGlobalEvent()', 'bool ILanguageInvoker::onExecutionInitialized()',
                'void ILanguageInvoker::onExecutionFinalized()']:
        code += function(ic, sig) + '\n'
    for sig in ['CLanguageInvokerThread::CLanguageInvokerThread(', 'CLanguageInvokerThread::~CLanguageInvokerThread()',
                'InvokerState CLanguageInvokerThread::GetState()', 'bool CLanguageInvokerThread::Reuseable(',
                'bool CLanguageInvokerThread::Keeps(', 'bool CLanguageInvokerThread::Retired()',
                'void CLanguageInvokerThread::Release()', 'bool CLanguageInvokerThread::execute(',
                'bool CLanguageInvokerThread::stop(', 'void CLanguageInvokerThread::OnStartup()',
                'void CLanguageInvokerThread::Process()', 'void CLanguageInvokerThread::OnExit()',
                'void CLanguageInvokerThread::OnException()', 'void CLanguageInvokerThread::OnLaunchFailed()']:
        code += function(lc, sig) + '\n'
    for sig in ['CScriptInvocationManager::~CScriptInvocationManager()', 'void CScriptInvocationManager::Process()',
                'void CScriptInvocationManager::Uninitialize()', 'int CScriptInvocationManager::GetReusablePluginHandle(',
                'LanguageInvokerPtr CScriptInvocationManager::GetLanguageInvoker(',
                'void CScriptInvocationManager::ReleaseLastInvokerThread()', 'bool CScriptInvocationManager::OwnsClaim(',
                'CLanguageInvokerThreadPtr CScriptInvocationManager::TakeReusableThread(',
                'int CScriptInvocationManager::ExecuteAsync(', 'int CScriptInvocationManager::ExecuteSync(',
                'bool CScriptInvocationManager::Stop(int', 'bool CScriptInvocationManager::Stop(const std::string',
                'void CScriptInvocationManager::StopRunningScripts(', 'bool CScriptInvocationManager::IsRunning(int',
                'bool CScriptInvocationManager::IsRunning(const std::string&',
                'void CScriptInvocationManager::OnExecutionDone(',
                'CScriptInvocationManager::LanguageInvokerThread CScriptInvocationManager::getInvokerThread(',
                'void CScriptInvocationManager::RemoveFailedInvocation(', 'void CScriptInvocationManager::RemoveScriptPath(']:
        first = mc.index(sig)
        code += function(mc, sig) + '\n'
        if sig.startswith('int CScriptInvocationManager::Execute'):
            code += function(mc[mc.index(sig, first+len(sig)):], sig) + '\n'
    # Only handle-table production behavior is exercised. Runner is an unused declaration boundary.
    code += 'struct CScriptRunner{void ExecuteScript();void GetAddon();void SetDone();void StartScript();bool RunScript(const ADDON::AddonPtr&,const std::string&,int,bool);};\n'
    handles = read('interfaces/generic/RunningScriptsHandler.h')
    code += handles[handles.index('template<class TScript>'):]
    return code + TESTS


def check(out):
    OUT = out
    OUT.mkdir(parents=True, exist_ok=True)
    code = generate()
    (OUT / 'bounded.cpp').write_text(code)
    argv = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
            '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
            '-fno-omit-frame-pointer', str(OUT / 'bounded.cpp'), '-o', str(OUT / 'bounded')]
    compile_result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    (OUT / 'compile.log').write_text(compile_result.stdout + compile_result.stderr)
    assert compile_result.returncode == 0, compile_result.stderr
    result = subprocess.run([str(OUT / 'bounded')], capture_output=True, text=True, timeout=20,
                            env={**os.environ, 'UBSAN_OPTIONS': 'halt_on_error=1'})
    (OUT / 'run.log').write_text(result.stdout + result.stderr)
    print(result.stdout, end='')
    assert result.returncode == 0, (result.returncode, result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Keep generated programs and verification logs here')
    args = parser.parse_args()
    if args.output:
        check(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='invoker-reuse-corrected-') as name:
            check(Path(name))


if __name__ == '__main__':
    main()
