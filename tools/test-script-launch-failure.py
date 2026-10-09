#!/usr/bin/env python3
"""Production CThread/invoker/manager fault injection with native host threads.

Only the thread allocation boundary, language interpreter and platform adapters
are recording substitutes. GUI script dispatch is represented by ExecuteAsync;
no real Python interpreter, GUI, CE scheduler or device exhaustion is simulated.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
FILES = ['xbmc/threads/Thread.h', 'xbmc/threads/Thread.cpp',
         'xbmc/interfaces/generic/ILanguageInvoker.h', 'xbmc/interfaces/generic/ILanguageInvoker.cpp',
         'xbmc/interfaces/generic/ILanguageInvocationHandler.h',
         'xbmc/interfaces/generic/LanguageInvokerThread.h', 'xbmc/interfaces/generic/LanguageInvokerThread.cpp',
         'xbmc/interfaces/generic/ScriptInvocationManager.h', 'xbmc/interfaces/generic/ScriptInvocationManager.cpp']
BASELINE = '6a19454cab0cccf91bd838aff59a93477b79e36a'


def program(baseline=False):
    source = {p: subprocess.check_output(['git', 'show', BASELINE + ':' + p], cwd=ROOT, text=True)
              if baseline else (ROOT / p).read_text() for p in FILES}
    th = source[FILES[0]]
    tc = source[FILES[1]]
    ih = source[FILES[2]]
    ic = source[FILES[3]]
    lh = source[FILES[5]]
    lc = source[FILES[6]]
    mh = source[FILES[7]]
    mc = source[FILES[8]]
    code = PRELUDE
    code += function(th, 'enum class ThreadPriority') + ';\n'
    code += PLATFORM + function(th, 'class CThread\n').replace('protected:', 'public:').replace('private:', 'public:') + ';\n'
    code += 'static thread_local CThread* currentThread;\n'
    for signature in ['CThread::CThread(const char*', 'CThread::CThread(IRunnable*', 'CThread::~CThread()',
                      'void CThread::Create(', 'bool CThread::IsRunning()', 'bool CThread::SetPriority(',
                      'bool CThread::IsAutoDelete()', 'void CThread::StopThread(', 'void CThread::Process()',
                      'bool CThread::IsCurrentThread()', 'bool CThread::Join(std::chrono::milliseconds',
                      'void CThread::Action()']:
        body = function(tc, signature)
        if signature == 'void CThread::Create(':
            body = body.replace('new std::thread(', 'LaunchThread(')
            body = body.replace('std::make_shared<std::thread>(', 'LaunchSharedThread(')
        code += body + '\n'
    if 'bool CThread::Join(const std::shared_ptr' in tc:
        code += function(tc, 'bool CThread::Join(const std::shared_ptr') + '\n'
    code += ih[ih.index('typedef enum'):ih.index('class ILanguageInvoker\n')]
    code += function(source[FILES[4]], 'class ILanguageInvocationHandler\n') + ';\n'
    code += function(ih, 'class ILanguageInvoker\n') + ';\nusing LanguageInvokerPtr=std::shared_ptr<ILanguageInvoker>;\n'
    code += function(lh, 'class CLanguageInvokerThread :').replace('protected CThread', 'public CThread').replace('protected:', 'public:').replace('private:', 'public:') + ';\n'
    code += 'using CLanguageInvokerThreadPtr=std::shared_ptr<CLanguageInvokerThread>;\n'
    code += function(mh, 'class CScriptInvocationManager\n').replace('protected:', 'public:').replace('private:', 'public:') + ';\n'
    for signature in ['ILanguageInvoker::ILanguageInvoker(', 'ILanguageInvoker::~ILanguageInvoker()',
                      'bool ILanguageInvoker::Execute(', 'bool ILanguageInvoker::Stop(',
                      'void ILanguageInvoker::onExecutionDone()', 'void ILanguageInvoker::onExecutionFailed()',
                      'void ILanguageInvoker::setState(']:
        if signature == 'ILanguageInvoker::~ILanguageInvoker()':
            code += 'ILanguageInvoker::~ILanguageInvoker()=default;\n'
        else:
            code += function(ic, signature) + '\n'
    # Supply unchanged virtual helpers too, preserving the production handler contract.
    for signature in ['bool ILanguageInvoker::IsStopping()', 'void ILanguageInvoker::AbortNotification()',
                      'void ILanguageInvoker::pulseGlobalEvent()', 'bool ILanguageInvoker::onExecutionInitialized()',
                      'void ILanguageInvoker::onExecutionFinalized()']:
        code += function(ic, signature) + '\n'
    for signature in ['CLanguageInvokerThread::CLanguageInvokerThread(', 'CLanguageInvokerThread::~CLanguageInvokerThread()',
                      'InvokerState CLanguageInvokerThread::GetState()', 'void CLanguageInvokerThread::Release()',
                      'bool CLanguageInvokerThread::execute(', 'bool CLanguageInvokerThread::stop(',
                      'void CLanguageInvokerThread::OnStartup()', 'void CLanguageInvokerThread::Process()',
                      'void CLanguageInvokerThread::OnExit()', 'void CLanguageInvokerThread::OnException()']:
        code += function(lc, signature) + '\n'
    for signature in ['void CLanguageInvokerThread::OnLaunchFailed()']:
        if signature in lc:
            code += function(lc, signature) + '\n'
    for signature in ['CScriptInvocationManager::~CScriptInvocationManager()', 'void CScriptInvocationManager::Process()',
                      'void CScriptInvocationManager::Uninitialize()', 'int CScriptInvocationManager::GetReusablePluginHandle(',
                      'LanguageInvokerPtr CScriptInvocationManager::GetLanguageInvoker(',
                      'int CScriptInvocationManager::ExecuteAsync(', 'int CScriptInvocationManager::ExecuteSync(',
                      'bool CScriptInvocationManager::Stop(int', 'bool CScriptInvocationManager::IsRunning(int',
                      'bool CScriptInvocationManager::IsRunning(const std::string&',
                      'void CScriptInvocationManager::OnExecutionDone(',
                      'CScriptInvocationManager::LanguageInvokerThread CScriptInvocationManager::getInvokerThread(']:
        first = mc.find(signature)
        code += function(mc, signature) + '\n'
        if signature.startswith('int CScriptInvocationManager::Execute'):
            # ExecuteAsync/Sync each have two production overloads.
            code += function(mc[mc.find(signature, first+len(signature)):], signature) + '\n'
    for signature in ['void CScriptInvocationManager::RemoveFailedInvocation(', 'void CScriptInvocationManager::RemoveScriptPath(']:
        if signature in mc:
            code += function(mc, signature) + '\n'
    tests = TESTS
    if baseline:
        tests = tests.replace("auto completion=worker->m_future;", "auto completion=worker->m_future.share();")
    return code + tests


def compile_test(out, code):
    (out / 'test.cpp').write_text(code)
    subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined',
                    '-fno-omit-frame-pointer', str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)


def expect_failure(binary, mode, marker, timeout=4, baseline=False):
    # Keep leak detection enabled. Baseline overflow remains recoverable so its
    # independently demonstrated wrapper leak can also be observed.
    env = {**os.environ, 'UBSAN_OPTIONS': 'halt_on_error=0' if baseline else 'halt_on_error=1'}
    try:
        result = subprocess.run([str(binary), mode], capture_output=True, text=True, timeout=timeout, env=env)
        assert marker != 'timeout' and result.returncode != 0 and marker in result.stderr, (mode, result.stderr)
        print(mode + ': rejected as expected', flush=True)
        print(result.stderr[-1800:] if baseline else result.stderr.splitlines()[-1], flush=True)
    except subprocess.TimeoutExpired:
        assert marker == 'timeout', mode
        print(mode + ': timed out at the missing completion/startup signal as expected', flush=True)


def main(baseline=False, controls=False):
    with tempfile.TemporaryDirectory(prefix='script-launch-') as name:
        out = Path(name)
        code = program(baseline)
        compile_test(out, code)
        if baseline:
            for mode, marker in [('gui-failure', 'std::system_error'), ('destructor-failure', 'timeout'),
                                 ('retirement', 'LeakSanitizer'), ('path-retirement', 'manager.IsRunning(second)')]:
                expect_failure(out / 'test', mode, marker, baseline=True)
        else:
            subprocess.run([str(out / 'test')], check=True, timeout=30,
                           env={**os.environ, 'UBSAN_OPTIONS': 'halt_on_error=1'})
        if controls:
            assert not baseline
            mutations = [
                ('startup rollback', 'm_StopEvent.Set();\n    m_StartEvent.Set();',
                 'm_StopEvent.Set();', 'destructor-failure', 'timeout'),
                ('optional launch catch', 'OnLaunchFailed();\n      return false;',
                 'throw;', 'gui-failure', 'std::system_error'),
                ('manager cleanup', 'if (!invokerThread->Execute(script, arguments))',
                 'if ((invokerThread->Execute(script, arguments), false))', 'gui-failure', 'manager.ExecuteAsync'),
                ('interpreter isolation', 'm_invoker->setState(InvokerStateFailed);',
                 'm_invoker->onExecutionFailed();', 'gui-failure', 'worker-only interpreter teardown'),
                ('path ownership', 'RemoveScriptPath(it.script, it.thread->GetId());',
                 'm_scriptPaths.erase(it.script);', 'path-retirement', 'manager.IsRunning(second)'),
                ('completion ownership', 'completion.wait_for(duration)',
                 'm_future.wait_for(duration)', 'pinned-retirement', 'waiting.wait_for'),
                ('stop ownership', 'if (m_thread == lthread)',
                 'if (true)', 'pinned-retirement', 'terminate called'),
                ('wrapper retirement', 'return std::make_shared<std::thread>(std::forward<T>(args)...);',
                 'return std::shared_ptr<std::thread>(new std::thread(std::forward<T>(args)...), [](std::thread*){});',
                 'retirement', 'LeakSanitizer'),
            ]
            for label, old, new, mode, marker in mutations:
                assert old in code, label
                # Change only the named contract, never the assertions.
                compile_test(out, code.replace(old, new))
                print('Negative control: ' + label, flush=True)
                expect_failure(out / 'test', mode, marker)
    print('Script launch: ' + ('BASELINE REPRODUCED' if baseline else 'PASS') +
          ' (production thread/invocation methods; native host threads; interpreter/platform stubs; ASan/UBSan)', flush=True)


PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <future>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
class ILanguageInvoker;class CScriptInvocationManager;
namespace ADDON{struct IAddon{};using AddonPtr=std::shared_ptr<IAddon>;}
std::atomic<int> failCode{0}, launches{0};
thread_local std::function<void()> beforeFailedLaunch,afterJoinUnlock;
std::mutex logMutex;std::vector<std::string> messages;
constexpr int LOGDEBUG=0,LOGERROR=1;
template<class T>std::string Text(const T& value){return std::to_string(value);}
std::string Text(const std::string& value){return value;}
std::string Text(const char* value){return value;}
std::string Text(std::thread::id value){return "thread";}
struct CLog {
 template<class...T>static void Log(int,const char* format,const T&... values){
  std::string line=format;((line+="|"+Text(values)),...);
  std::lock_guard<std::mutex> lock(logMutex);messages.push_back(line);
 }
};
struct CEvent {
 bool value;std::mutex mutex;std::condition_variable changed;
 explicit CEvent(bool=false,bool signaled=false):value(signaled){}
 void Set(){std::lock_guard<std::mutex> lock(mutex);value=true;changed.notify_all();}
 void Reset(){std::lock_guard<std::mutex> lock(mutex);value=false;}
 bool Signaled(){std::lock_guard<std::mutex> lock(mutex);return value;}
 void Wait(){std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[&]{return value;});}
 template<class R,class P>bool Wait(std::chrono::duration<R,P> duration){
  std::unique_lock<std::mutex> lock(mutex);return changed.wait_for(lock,duration,[&]{return value;});
 }
};
namespace XbmcThreads{struct CEventGroup{CEventGroup(std::initializer_list<CEvent*>){} const CEvent* wait(){return nullptr;}template<class T>const CEvent* wait(T){return nullptr;}};}
namespace XbmcCommons{struct UncheckedException{void LogThrowMessage(const char*)const{}};}
struct CSingleExit{CCriticalSection& mutex;explicit CSingleExit(CCriticalSection& m):mutex(m){mutex.unlock();if(afterJoinUnlock)afterJoinUnlock();}~CSingleExit(){mutex.lock();}};
struct CFileUtils{static bool Exists(const std::string& s,bool=false){return s!="missing.py";}};
struct URIUtils{static std::string GetExtension(const std::string&){return ".py";}};
struct StringUtils{static void ToLower(std::string&){};};
namespace KODI::TIME{template<class T>void Sleep(T value){std::this_thread::sleep_for(value);}}
template<class...T>std::thread* LaunchThread(T&&...args){
 ++launches;if(int code=failCode.exchange(0)){
  if(auto hook=std::exchange(beforeFailedLaunch,{}))hook();
  if(code==-1)throw std::bad_alloc();
  throw std::system_error(code,std::generic_category(),"injected launch");
 }
 return new std::thread(std::forward<T>(args)...);
}
template<class...T>std::shared_ptr<std::thread> LaunchSharedThread(T&&...args){
 ++launches;if(int code=failCode.exchange(0)){
  if(auto hook=std::exchange(beforeFailedLaunch,{}))hook();
  if(code==-1)throw std::bad_alloc();
  throw std::system_error(code,std::generic_category(),"injected launch");
 }
 return std::make_shared<std::thread>(std::forward<T>(args)...);
}
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
 Handler& handler;std::atomic<int> ready{0};
 explicit FakeInvoker(Handler& h):ILanguageInvoker(&h),handler(h){}
 bool execute(const std::string&,const std::vector<std::string>&)override{
  setState(InvokerStateRunning);++handler.executions;setState(InvokerStateScriptDone);++ready;return true;
 }
 bool stop(bool)override{return true;}
 void onExecutionDone()override{setState(InvokerStateExecutionDone);ILanguageInvoker::onExecutionDone();}
 void onExecutionFailed()override{assert(false&&"worker-only interpreter teardown called on failed launch");}
};
ILanguageInvoker* Handler::CreateInvoker(){return new FakeInvoker(*this);}
void Setup(CScriptInvocationManager& manager,Handler& handler){manager.m_invocationHandlers[".py"]=&handler;}
struct Worker:CThread{
 CEvent finish;std::atomic<int> ran{0};
 Worker():CThread("worker"){}
 void Process()override{++ran;finish.Wait();}
};
void GUIFailure(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);failCode=11;
 assert(manager.ExecuteAsync("optional.py",ADDON::AddonPtr(),{},true,73)==-1);
 assert(manager.m_scripts.empty()&&manager.m_scriptPaths.empty()&&!manager.m_lastInvokerThread&&manager.m_lastPluginHandle==-1);
 assert(handler.starts==0&&handler.ends==0&&handler.executions==0);
}
void DestructorFailure(){Worker worker;failCode=12;try{worker.Create();assert(false);}catch(const std::system_error&){} }
void Retirement(){for(int i=0;i<16;++i){Worker worker;worker.Create();worker.finish.Set();worker.StopThread();assert(!worker.IsRunning());}}
void RawFailureAndRetry(){
 Worker worker;failCode=1;try{worker.Create();assert(false);}catch(const std::system_error& e){assert(e.code().value()==1);}
 assert(!worker.IsRunning()&&worker.m_StartEvent.Signaled()&&worker.m_StopEvent.Signaled());
 worker.Create();Await([&]{return worker.ran==1;});worker.finish.Set();worker.StopThread();
}
void FailureAndRetry(){
 for(int code:{1,11,12,-1}){
  Handler handler;CScriptInvocationManager manager;Setup(manager,handler);failCode=code;
  auto invoker=std::make_shared<FakeInvoker>(handler);
  assert(manager.ExecuteAsync("optional.py",invoker,{}, {},true,73)==-1);
  assert(invoker->GetState()==InvokerStateFailed&&invoker->GetId()==-1);
  {std::lock_guard<std::mutex> lock(logMutex);
   assert(std::any_of(messages.begin(),messages.end(),[&](const auto& line){
    if(line.find("Cannot launch script")==std::string::npos||line.find("|optional.py|")==std::string::npos)return false;
    return code==-1?line.find("std::bad_alloc")!=std::string::npos:line.find("|"+std::to_string(code)+"|generic|")!=std::string::npos;
   }));
  }
  assert(manager.m_scripts.empty()&&manager.m_scriptPaths.empty()&&!manager.m_lastInvokerThread);
  auto id=manager.ExecuteAsync("optional.py");assert(id>=0);
  Await([&]{return !manager.IsRunning(id);});manager.Process();
  assert(handler.starts==1&&handler.ends==1&&handler.executions==1);
  failCode=code;assert(manager.ExecuteSync("optional.py")==-1);
  assert(manager.m_scripts.empty()&&!manager.IsRunning("optional.py"));
 }
}
void SuccessReuse(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 int before=launches;int id=manager.ExecuteAsync("reused.py",{}, {},true,44);assert(id>=0);
 auto thread=manager.m_lastInvokerThread;
 auto invoker=std::static_pointer_cast<FakeInvoker>(thread->GetInvoker());
 Await([&]{return invoker->ready==1;});{std::lock_guard<std::mutex> lock(thread->m_mutex);}
 assert(manager.GetReusablePluginHandle("reused.py")==44);
 auto reused=manager.GetLanguageInvoker("reused.py");assert(reused==invoker);
 assert(manager.ExecuteAsync("reused.py",reused,{}, {},true,44)==id);
 Await([&]{return invoker->ready==2;});{std::lock_guard<std::mutex> lock(thread->m_mutex);}
 assert(launches==before+1&&handler.starts==2&&handler.executions==2);
 thread->Stop(true);assert(!thread->CThread::IsRunning());
 // Explicit invoker overload can enter its existing cached-thread branch even
 // after that native thread retired. A failed restart must clean this branch too.
 failCode=11;assert(manager.ExecuteAsync("reused.py",reused)==-1);
 assert(!manager.m_lastInvokerThread&&manager.m_scripts.empty()&&manager.m_scriptPaths.empty());
 assert(reused->GetState()==InvokerStateFailed);
}
void ParallelPaths(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 auto first=manager.ExecuteAsync("same.py",{}, {},true);assert(first>=0);
 auto live=manager.m_lastInvokerThread;auto old=std::static_pointer_cast<FakeInvoker>(live->GetInvoker());
 Await([&]{return old->ready==1;});{std::lock_guard<std::mutex> lock(live->m_mutex);}
 // A second launch with this path must not remove the surviving first path entry.
 auto other=std::make_shared<FakeInvoker>(handler);failCode=12;
 assert(manager.ExecuteAsync("same.py",other)==-1);
 assert(manager.m_scriptPaths.at("same.py")==first&&manager.m_scripts.count(first)==1);
 live->Stop(true);manager.Process();
}
void PathRetirement(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 auto first=manager.ExecuteAsync("shared.py",std::make_shared<FakeInvoker>(handler),{}, {},true);
 auto firstThread=manager.m_lastInvokerThread;auto firstInvoker=std::static_pointer_cast<FakeInvoker>(firstThread->GetInvoker());
 Await([&]{return firstInvoker->ready==1;});{std::lock_guard<std::mutex> lock(firstThread->m_mutex);}
 auto second=manager.ExecuteAsync("shared.py",std::make_shared<FakeInvoker>(handler),{}, {},true);
 auto secondThread=manager.m_lastInvokerThread;auto secondInvoker=std::static_pointer_cast<FakeInvoker>(secondThread->GetInvoker());
 Await([&]{return secondInvoker->ready==1;});{std::lock_guard<std::mutex> lock(secondThread->m_mutex);}
 assert(first!=second);firstThread->Stop(true);manager.Process();
 assert(manager.IsRunning(second)&&manager.IsRunning("shared.py")&&manager.m_scriptPaths.at("shared.py")==second);
 secondThread->Stop(true);manager.Process();
}
void ReentrantFailure(){
 Handler handler;CScriptInvocationManager manager;Setup(manager,handler);
 int later=-1;CLanguageInvokerThreadPtr retained;
 beforeFailedLaunch=[&]{
  later=manager.ExecuteAsync("overlap.py",std::make_shared<FakeInvoker>(handler),{}, {},true,91);
  retained=manager.m_lastInvokerThread;
 };
 failCode=11;assert(manager.ExecuteAsync("overlap.py",std::make_shared<FakeInvoker>(handler),{}, {},true,73)==-1);
 assert(later>=0&&manager.m_lastInvokerThread==retained&&manager.m_lastPluginHandle==91);
 assert(manager.m_scripts.size()==1&&manager.IsRunning(later)&&manager.m_scriptPaths.at("overlap.py")==later);
 retained->Stop(true);manager.Process();
}
void PinnedRetirement(){
 for(bool stop:{false,true}){
  Worker worker;worker.Create();Await([&]{return worker.ran==1;});
  CEvent entered,proceed;
  auto waiting=std::async(std::launch::async,[&]{
   afterJoinUnlock=[&]{entered.Set();proceed.Wait();};
   if(stop){worker.StopThread();return true;}return worker.Join(2s);
  });
  assert(entered.Wait(1s));worker.finish.Set();Await([&]{return !worker.IsRunning();});
  worker.StopThread();worker.finish.Reset();worker.Create();Await([&]{return worker.ran==2;});
  proceed.Set();assert(waiting.wait_for(1s)==std::future_status::ready&&waiting.get());
  assert(worker.IsRunning());worker.finish.Set();worker.StopThread();
 }
 Worker worker;worker.Create();assert(!worker.Join(1ms));worker.finish.Set();assert(worker.Join(1s));worker.StopThread();
}
void AutoDelete(){
 struct AutoWorker: CThread {
  CEvent& gate;std::atomic<int>& destroyed;
  AutoWorker(CEvent& g,std::atomic<int>& d):CThread("auto"),gate(g),destroyed(d){}
  ~AutoWorker()override{++destroyed;}
  void Process()override{gate.Wait();}
 };
 CEvent gate;std::atomic<int> destroyed{0};auto* worker=new AutoWorker(gate,destroyed);
 worker->Create(true);
 // Pin the existing completion before the auto-deleting worker can exit.
 auto completion=worker->m_future;gate.Set();assert(completion.wait_for(2s)==std::future_status::ready);
 assert(destroyed==1);
 worker=new AutoWorker(gate,destroyed);failCode=11;
 try{worker->Create(true);assert(false);}catch(const std::system_error&){}
 delete worker;assert(destroyed==2);
}
void ConcurrentRetirement(){
 for(int i=0;i<20;++i){
  Worker worker;worker.Create();
  CEvent joined;
  auto join=std::async(std::launch::async,[&]{afterJoinUnlock=[&]{joined.Set();};return worker.Join(2s);});
  assert(joined.Wait(1s));
  auto stop=std::async(std::launch::async,[&]{worker.StopThread();});
  worker.finish.Set();assert(join.get());stop.get();assert(!worker.IsRunning());
 }
}
int main(int argc,char**argv){
 if(argc>1){std::string mode=argv[1];if(mode=="gui-failure")GUIFailure();else if(mode=="destructor-failure")DestructorFailure();else if(mode=="retirement")Retirement();else if(mode=="path-retirement")PathRetirement();else if(mode=="pinned-retirement")PinnedRetirement();return 0;}
 GUIFailure();DestructorFailure();RawFailureAndRetry();Retirement();FailureAndRetry();SuccessReuse();ParallelPaths();PathRetirement();ReentrantFailure();PinnedRetirement();AutoDelete();ConcurrentRetirement();
 std::cout<<"script launch scenarios passed\n";
}
'''
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--controls', action='store_true')
    args = parser.parse_args()
    main(args.baseline, args.controls)
