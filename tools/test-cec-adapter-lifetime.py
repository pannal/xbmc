#!/usr/bin/env python3
"""Bounded corrected CEC ownership schedules using extracted production methods.

Native adapter, query worker, manager and initialization are explicit host
adapters. Executes actual teardown/removal/reopen/job bodies; separately checks
initialization/settings admission in source. No faulty baseline or controls.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include <iostream>
using CCriticalSection=std::recursive_mutex;
constexpr int EXITCODE_RESTARTAPP=1, FEATURE_CEC=2;
struct Gate {
 std::mutex mutex; std::condition_variable cv; bool entered=false,released=false;
 void pause() { std::unique_lock<std::mutex> l(mutex); entered=true; cv.notify_all(); assert(cv.wait_for(l,std::chrono::seconds(3),[&]{return released;})); }
 void await() { std::unique_lock<std::mutex> l(mutex); assert(cv.wait_for(l,std::chrono::seconds(3),[&]{return entered;})); }
 void release() { std::lock_guard<std::mutex> l(mutex); released=true; cv.notify_all(); }
};
std::atomic<int> nativeDestroyed{0},queryDestroyed{0},initializations{0};
struct Native { ~Native() { ++nativeDestroyed; } };
struct Query { ~Query() { ++queryDestroyed; } };
void DrainCecCallbacks(Native*) {}
void CECDestroy(Native* n) { delete n; }
struct CJob { enum {PRIORITY_NORMAL}; virtual ~CJob()=default; virtual bool DoWork()=0; };
namespace PERIPHERALS { class CPeripheralCecAdapterReopenJob; }
struct CApplicationActionListeners { template<class T> void UnregisterActionListener(T*) {} };
struct Components { template<class T> std::shared_ptr<T> GetComponent() { return std::make_shared<T>(); } };
struct Announcements { template<class T> void RemoveAnnouncer(T*) {} };
struct Jobs { std::unique_ptr<CJob> job; void AddJob(CJob* j,void*,int) { job.reset(j); } };
struct CServiceBroker {
 static auto GetAnnouncementManager() { return std::make_shared<Announcements>(); }
 static Components& GetAppComponents() { static Components c; return c; }
 static Jobs* GetJobManager() { static Jobs j; return &j; }
};
namespace PERIPHERALS {
class CPeripheralCecAdapter:public std::enable_shared_from_this<CPeripheralCecAdapter> {
public:
 Native* m_cecAdapter=new Native; Query* m_queryThread=new Query;
 CCriticalSection m_lifecycleSection,m_critSection;
 std::atomic<bool> m_retired{false},m_bStop{false},m_bDeviceRemoved{false}; bool m_deinitialized=false;
 std::atomic<uint64_t> m_sourceEventGeneration{7}; int m_iExitCode=0;
 Gate* stopGate=nullptr;
 ~CPeripheralCecAdapter(); void Deinitialize(); void OnDeviceRemoved(); void RetireCecCallbacks();
 bool ReopenConnection(bool=false,uint64_t=0);
 void StopThread(bool wait=true) { m_bStop=true; if(wait && stopGate) { auto gate=stopGate; stopGate=nullptr; gate->pause(); } }
 void ResetMembers() { RetireCecCallbacks(); CECDestroy(m_cecAdapter); m_cecAdapter=nullptr; }
 bool InitialiseFeature(int) { std::unique_lock<CCriticalSection> l(m_lifecycleSection); if(m_retired.load()) return false; ++initializations; m_cecAdapter=new Native; m_queryThread=new Query; m_bStop=false; return true; }
};
}
using namespace PERIPHERALS;
'''
TEST = r'''
int main() {
 { auto a=std::make_shared<CPeripheralCecAdapter>(); int n=nativeDestroyed,q=queryDestroyed;
   a->OnDeviceRemoved(); assert(a->m_bDeviceRemoved && a->m_retired && a->m_deinitialized);
   assert(!a->m_cecAdapter && !a->m_queryThread); a->Deinitialize(); a.reset();
   assert(nativeDestroyed==n+1 && queryDestroyed==q+1); }
 { auto a=std::make_shared<CPeripheralCecAdapter>(); auto weak=a->weak_from_this();
   CPeripheralCecAdapterReopenJob job(weak,7); a.reset(); assert(weak.expired()); assert(!job.DoWork()); }
 { auto a=std::make_shared<CPeripheralCecAdapter>(); CPeripheralCecAdapterReopenJob stale(a,6);
   assert(!stale.DoWork()); assert(initializations==0);
   int n=nativeDestroyed,q=queryDestroyed;
   assert(a->ReopenConnection(true)); auto job=std::move(CServiceBroker::GetJobManager()->job);
   assert(job->DoWork()); assert(initializations==1);
   assert(nativeDestroyed==n+1 && queryDestroyed==q+1);
   assert(!job->DoWork()); // same session token is now stale
   a->OnDeviceRemoved(); assert(!a->ReopenConnection(true)); }
 { auto a=std::make_shared<CPeripheralCecAdapter>(); Gate stop; a->stopGate=&stop;
   bool reopened=true; int starts=initializations;
   CPeripheralCecAdapterReopenJob job(a,a->m_sourceEventGeneration.load());
   std::thread reopen([&]{ reopened=job.DoWork(); }); stop.await();
   std::atomic<bool> retired{false};
   std::thread remove([&]{ a->OnDeviceRemoved(); retired=true; });
   auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
   while(!a->m_retired.load() && std::chrono::steady_clock::now()<deadline) std::this_thread::yield();
   assert(a->m_retired && !retired && a->m_cecAdapter);
   stop.release(); reopen.join(); remove.join();
   assert(!reopened && retired && initializations==starts);
   assert(a->m_bDeviceRemoved && a->m_deinitialized && !a->m_cecAdapter && !a->m_queryThread); }
 std::cout<<"PASS CEC removal/destructor/repeated retirement, weak queued job, session rejection and synchronized executing-reopen retirement\n";
}
'''


def run(out):
    source = (ROOT / 'xbmc/peripherals/devices/PeripheralCecAdapter.cpp').read_text()
    header = (ROOT / 'xbmc/peripherals/devices/PeripheralCecAdapter.h').read_text()
    for marker in ['bool CPeripheralCecAdapter::InitialiseFeature(',
                   'void CPeripheralCecAdapter::OnSettingChanged(',
                   'void CPeripheralCecAdapter::OnCecSourceActivated(',
                   'bool CPeripheralCecAdapter::ToggleDeviceState(']:
        method = block(source, marker)
        assert 'lifecycleLock(m_lifecycleSection)' in method and 'm_retired.load(' in method
    assert '~CPeripheralCecAdapter() override;' in header
    methods = ['CPeripheralCecAdapter::~CPeripheralCecAdapter()',
               'void CPeripheralCecAdapter::Deinitialize(',
               'void CPeripheralCecAdapter::RetireCecCallbacks(',
               'void CPeripheralCecAdapter::OnDeviceRemoved(',
               'class PERIPHERALS::CPeripheralCecAdapterReopenJob',
               'bool CPeripheralCecAdapter::ReopenConnection(']
    excerpts = [block(source, marker) + (';' if marker.startswith('class ') else '')
                for marker in methods]
    cpp = out / 'cec-adapter.cpp'
    cpp.write_text(PRELUDE + '\n'.join(excerpts) + TEST)
    argv = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
            '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
            '-fno-pie', '-no-pie', str(cpp), '-o', str(out / 'cec-adapter')]
    (out / 'argv.json').write_text(json.dumps(argv, indent=2) + '\n')
    compiled = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    (out / 'compile.log').write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(out / 'cec-adapter')], capture_output=True, text=True, timeout=15,
                            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                                 'UBSAN_OPTIONS': 'halt_on_error=1'})
    (out / 'run.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        run(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='cec-adapter-') as temporary:
            run(Path(temporary))
