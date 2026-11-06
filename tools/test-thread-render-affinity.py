#!/usr/bin/env python3
"""Production CThread::Create: a thread started by the pinned render thread restores Kodi's CPUs.

The CThread methods are compiled from the source with the fixture of
test-script-launch-failure.py; only the PERFORMANCE_CORES calls record.
"""
from pathlib import Path
import os
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-script-launch-failure.py'))
function = fixture['function']
STUB = ('namespace PERFORMANCE_CORES{inline std::set<int> ChildAffinity(){return {};}'
        'inline void RestoreChildAffinity(const std::set<int>&){}}\n')
RECORDING = r'''
namespace PERFORMANCE_CORES{
 // The creator's role: the pinned render thread, or a worker restricted for a decode.
 thread_local std::set<int> creatorRestore;std::atomic<int> asked{0};
 std::mutex mutex;std::vector<std::pair<std::thread::id,std::set<int>>> restored;
 inline std::set<int> ChildAffinity(){++asked;return creatorRestore;}
 inline void RestoreChildAffinity(const std::set<int>& cpus){std::lock_guard<std::mutex> lock(mutex);restored.emplace_back(std::this_thread::get_id(),cpus);}
}
'''
TEST = r'''
struct Probe:CThread{
 std::thread::id id;bool restoredFirst{false};std::promise<void> done;
 Probe():CThread("probe"){}
 void Process() override{
  id=std::this_thread::get_id();
  {std::lock_guard<std::mutex> lock(PERFORMANCE_CORES::mutex);
   for(const auto& r:PERFORMANCE_CORES::restored)restoredFirst|=r.first==id;}
  done.set_value();
 }
};
static void Run(Probe& p){auto f=p.done.get_future();p.Create();f.wait();p.StopThread(true);}
int main(){
 using namespace PERFORMANCE_CORES;
 // An ordinary creator: the new thread keeps its inherited mask.
 {Probe p;Run(p);assert(restored.empty()&&!p.restoredFirst&&asked==1);}
 // The pinned render thread: Kodi's CPUs are restored on the new thread before Process.
 creatorRestore={0,1,2,3,4,5};
 {Probe p;Run(p);assert(restored.size()==1&&restored[0].first==p.id&&restored[0].first!=std::this_thread::get_id()&&
   restored[0].second==std::set<int>({0,1,2,3,4,5})&&p.restoredFirst);}
 // Only the creator's own role counts: a thread started elsewhere is unchanged.
 restored.clear();
 {std::thread other([]{Probe p;Run(p);});other.join();assert(restored.empty());}
 // A worker restricted for a decode starts a thread, such as another job worker:
 // the child takes the worker's own CPUs, not the slow cluster it inherits.
 {std::thread worker([]{creatorRestore={1,2,3,4,5};Probe p;Run(p);creatorRestore.clear();
   assert(restored.size()==1&&restored[0].first==p.id&&restored[0].second==std::set<int>({1,2,3,4,5})&&p.restoredFirst);});
  worker.join();}
 creatorRestore.clear();
}
'''


def main():
    tc = (ROOT / 'xbmc/threads/Thread.cpp').read_text()
    th = (ROOT / 'xbmc/threads/Thread.h').read_text()
    platform = fixture['PLATFORM']
    assert STUB in platform, 'fixture stub changed'
    code = fixture['PRELUDE'] + function(th, 'enum class ThreadPriority') + ';\n'
    code += platform.replace(STUB, RECORDING)
    code += function(th, 'class CThread\n').replace('protected:', 'public:').replace('private:', 'public:') + ';\n'
    code += 'static thread_local CThread* currentThread;\n'
    for signature in ['CThread::CThread(const char*', 'CThread::CThread(IRunnable*', 'CThread::~CThread()',
                      'void CThread::Create(', 'bool CThread::IsRunning()', 'bool CThread::SetPriority(',
                      'bool CThread::IsAutoDelete()', 'void CThread::StopThread(', 'void CThread::Process()',
                      'bool CThread::IsCurrentThread()', 'bool CThread::Join(std::chrono::milliseconds',
                      'void CThread::Action()']:
        body = function(tc, signature)
        if signature == 'void CThread::Create(':
            assert 'ChildAffinity' in body and 'RestoreChildAffinity' in body
            body = body.replace('std::make_shared<std::thread>(', 'LaunchSharedThread(')
        code += body + '\n'
    if 'bool CThread::Join(const std::shared_ptr' in tc:
        code += function(tc, 'bool CThread::Join(const std::shared_ptr') + '\n'
    code += TEST
    with tempfile.TemporaryDirectory(prefix='thread-render-affinity-') as directory:
        work = Path(directory)
        (work / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-variable', '-pthread',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        str(work / 'test.cpp'), '-o', str(work / 'test')], check=True)
        subprocess.run([str(work / 'test')], check=True, timeout=60)
    print('PASS production CThread::Create: children of the render thread and of a decode-restricted worker '
          'restore their creator CPUs on their own thread before Process; other creators keep inherited placement')


if __name__ == '__main__':
    main()
