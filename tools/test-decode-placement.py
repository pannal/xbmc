#!/usr/bin/env python3
"""Exercise production image-decode placement with per-thread fake CPU masks and sysfs; device speed is unverified."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_STUB = r'''
#pragma once
struct CSettings {static constexpr int SETTING_SUBTITLES_ISOLATEACTIVEAREA=1;bool GetBool(int) const {return false;}};
struct SettingsComponent {CSettings* GetSettings(){static CSettings settings;return &settings;}};
struct CServiceBroker {static SettingsComponent* GetSettingsComponent(){static SettingsComponent c;return &c;}};
'''
LOG_STUB = r'''
#pragma once
#include <atomic>
#include <cassert>
#include <mutex>
#include <string>
constexpr int LOGDEBUG=0,LOGINFO=1,LOGERROR=2;
struct CLog {
 static inline std::atomic<int> infos{0},errors{0};
 template<class... T>static void Log(int level,const char* format,T&&...){
  size_t fields=0;for(auto p=format;*p;++p)fields+=*p=='{';assert(fields==sizeof...(T));
  (level==LOGERROR?errors:infos)++;
 }
};
'''
TEST = r'''
#include <sched.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <condition_variable>
#include <fstream>
#include <map>
#include <mutex>
#include <random>
#include <set>
#include <string>
#include <thread>
#include <vector>
#include "ServiceBroker.h"
// Per-thread masks keyed by kernel thread id; tid 0 is the calling thread.
std::mutex masksMutex;std::map<pid_t,std::set<int>> masks;std::atomic<int> sets{0},failSet{0};
static pid_t Tid(){return static_cast<pid_t>(syscall(SYS_gettid));}
static std::set<int>& MaskOf(pid_t tid){auto it=masks.find(tid);if(it==masks.end())it=masks.emplace(tid,std::set<int>{0,1,2,3,4,5}).first;return it->second;}
int FakeSet(pid_t tid,size_t,const cpu_set_t* mask){
 std::lock_guard<std::mutex> lock(masksMutex);++sets;if(failSet){errno=EPERM;return -1;}
 auto& m=MaskOf(tid?tid:Tid());m.clear();for(int cpu=0;cpu<CPU_SETSIZE;++cpu)if(CPU_ISSET(cpu,mask))m.insert(cpu);return 0;
}
int FakeGet(pid_t tid,size_t,cpu_set_t* mask){
 std::lock_guard<std::mutex> lock(masksMutex);CPU_ZERO(mask);for(int cpu:MaskOf(tid?tid:Tid()))CPU_SET(cpu,mask);return 0;
}
#define sched_setaffinity FakeSet
#define sched_getaffinity FakeGet
#include "threads/PerformanceCores.cpp"
static std::set<int> Mine(){std::lock_guard<std::mutex> lock(masksMutex);return MaskOf(Tid());}
static void SetMine(std::set<int> m){std::lock_guard<std::mutex> lock(masksMutex);MaskOf(Tid())=std::move(m);}
static std::string root;
static void WriteFrequencies(const std::map<int,uint64_t>& khz){
 assert(system(("rm -rf '"+root+"'/cpu*").c_str())==0);
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
 {std::lock_guard<std::mutex> lock(s_frequencyMutex);s_frequencies.clear();}
#endif
 for(const auto& [cpu,value]:khz){const std::string dir=root+"/cpu"+std::to_string(cpu)+"/cpufreq";
  assert(system(("mkdir -p '"+dir+"'").c_str())==0);std::ofstream(dir+"/cpuinfo_max_freq")<<value<<"\n";}
}
// Run body on a fresh worker thread whose mask starts as given.
template<class F>static void OnWorker(std::set<int> start,F body){std::thread([&]{SetMine(start);body();}).join();}
using PERFORMANCE_CORES::CDecodePlacement;
int main(){
 const std::set<int> all{0,1,2,3,4,5},slow{0,1};
 const std::map<int,uint64_t> s922x{{0,1800000},{1,1800000},{2,2208000},{3,2208000},{4,2208000},{5,2208000}};
 char dir[]="/tmp/decode-placement-XXXXXX";assert(mkdtemp(dir));root=dir;WriteFrequencies(s922x);
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
 s_cpuRoot=root.c_str();
 // A decode nothing draws runs on the slow cluster and gives the CPUs back.
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();
  {CDecodePlacement::Scope scope(p);assert(Mine()==slow);}assert(Mine()==all);});
 assert(CLog::infos==1);  // announced once
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();{CDecodePlacement::Scope scope(p);assert(Mine()==slow);}});
 assert(CLog::infos==1);
 // Needed before the decode starts: never restricted.
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();p->NeededNow();const int before=sets;
  {CDecodePlacement::Scope scope(p);assert(Mine()==all);}assert(Mine()==all&&sets==before);});
 // Needed while decoding: widened from the consumer's thread at once; the worker sets nothing more.
 {
  auto p=std::make_shared<CDecodePlacement>();std::mutex m;std::condition_variable cv;int stage=0;std::set<int> during,after;int setsAtWiden=0;
  std::thread worker([&]{SetMine(all);{CDecodePlacement::Scope scope(p);
    {std::unique_lock<std::mutex> l(m);stage=1;cv.notify_all();cv.wait(l,[&]{return stage==2;});}
    during=Mine();setsAtWiden=sets;}
   after=Mine();
   std::lock_guard<std::mutex> l(m);stage=3;cv.notify_all();});
  {std::unique_lock<std::mutex> l(m);cv.wait(l,[&]{return stage==1;});}
  p->NeededNow();p->NeededNow();  // repeated per frame
  {std::lock_guard<std::mutex> l(m);stage=2;cv.notify_all();}
  worker.join();
  assert(during==all&&after==all&&sets==setsAtWiden);
 }
 // A null placement, a uniform or restricted topology, and a slow-only worker are left alone.
 OnWorker(all,[&]{const int b=sets;{CDecodePlacement::Scope scope(nullptr);}assert(sets==b&&Mine()==all);});
 OnWorker({0,1,2,3,4},[&]{auto p=std::make_shared<CDecodePlacement>();{CDecodePlacement::Scope scope(p);assert(Mine()==std::set<int>({0,1,2,3,4}));}});
 OnWorker(slow,[&]{auto p=std::make_shared<CDecodePlacement>();{CDecodePlacement::Scope scope(p);assert(Mine()==slow);}assert(Mine()==slow);});
 OnWorker({2,3,4,5},[&]{auto p=std::make_shared<CDecodePlacement>();{CDecodePlacement::Scope scope(p);assert(Mine()==std::set<int>({2,3,4,5}));}});
 WriteFrequencies({{0,1908000},{1,1908000},{2,1908000},{3,1908000},{4,1908000},{5,1908000}});
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();const int b=sets;{CDecodePlacement::Scope scope(p);assert(Mine()==all);}assert(sets==b);});
 WriteFrequencies({});
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();{CDecodePlacement::Scope scope(p);assert(Mine()==all);}});
 WriteFrequencies(s922x);
 // A failed restriction is not undone and not widened later.
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();failSet=1;const int b=sets;
  {CDecodePlacement::Scope scope(p);assert(Mine()==all);p->NeededNow();}failSet=0;assert(sets==b+1&&Mine()==all);});
 // A failed widening keeps the restriction recorded: a later call retries, and
 // otherwise the worker restores its CPUs itself when the decode returns.
 {
  auto p=std::make_shared<CDecodePlacement>();std::mutex m;std::condition_variable cv;int stage=0;std::set<int> during,after;
  std::thread worker([&]{SetMine(all);{CDecodePlacement::Scope scope(p);
    {std::unique_lock<std::mutex> l(m);stage=1;cv.notify_all();cv.wait(l,[&]{return stage==2;});}
    during=Mine();}
   after=Mine();});
  {std::unique_lock<std::mutex> l(m);cv.wait(l,[&]{return stage==1;});}
  const int e=CLog::errors;failSet=1;p->NeededNow();failSet=0;assert(CLog::errors==e+1);
  {std::lock_guard<std::mutex> l(m);stage=2;cv.notify_all();}
  worker.join();
  assert(during==slow&&after==all);
 }
 {
  auto p=std::make_shared<CDecodePlacement>();std::mutex m;std::condition_variable cv;int stage=0;std::set<int> during;
  std::thread worker([&]{SetMine(all);{CDecodePlacement::Scope scope(p);
    {std::unique_lock<std::mutex> l(m);stage=1;cv.notify_all();cv.wait(l,[&]{return stage==2;});}
    during=Mine();}});
  {std::unique_lock<std::mutex> l(m);cv.wait(l,[&]{return stage==1;});}
  failSet=1;p->NeededNow();failSet=0;p->NeededNow();  // the next frame's request widens it
  {std::lock_guard<std::mutex> l(m);stage=2;cv.notify_all();}
  worker.join();
  assert(during==all);
 }
 // Threads a restricted worker creates take its own CPUs; outside a decode,
 // or once the decode was needed before it started, they keep the inherited mask.
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();assert(PERFORMANCE_CORES::ChildAffinity().empty());
  {CDecodePlacement::Scope scope(p);assert(Mine()==slow&&PERFORMANCE_CORES::ChildAffinity()==all);
   std::set<int> child;std::thread([&]{SetMine(slow);PERFORMANCE_CORES::RestoreChildAffinity(all);child=Mine();}).join();
   assert(child==all);}
  assert(PERFORMANCE_CORES::ChildAffinity().empty());});
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();p->NeededNow();
  {CDecodePlacement::Scope scope(p);assert(PERFORMANCE_CORES::ChildAffinity().empty());}});
 // A failed restore is reported, never thrown.
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();const int e=CLog::errors;
  {CDecodePlacement::Scope scope(p);assert(Mine()==slow);failSet=1;}failSet=0;assert(CLog::errors==e+1);});
 // A worker's CPUs return, also when it was never the render thread's child.
 OnWorker({1,2,3,4,5},[&]{auto p=std::make_shared<CDecodePlacement>();
  {CDecodePlacement::Scope scope(p);assert(Mine()==std::set<int>({1}));}assert(Mine()==std::set<int>({1,2,3,4,5}));});
 // Stress: concurrent decodes, some needed mid-way from another thread; every worker ends unrestricted.
 {
  std::vector<std::shared_ptr<CDecodePlacement>> ps;for(int i=0;i<64;++i)ps.push_back(std::make_shared<CDecodePlacement>());
  std::atomic<bool> stop{false};std::atomic<int> wrong{0};
  std::thread consumer([&]{std::mt19937 r(1);while(!stop)ps[r()%ps.size()]->NeededNow();});
  std::vector<std::thread> workers;
  for(int w=0;w<4;++w)workers.emplace_back([&,w]{SetMine(all);for(int i=w;i<(int)ps.size();i+=4){
    {CDecodePlacement::Scope scope(ps[i]);const auto m=Mine();if(m!=slow&&m!=all)++wrong;}
    if(Mine()!=all)++wrong;}});
  for(auto& t:workers)
   t.join();
  stop=true;consumer.join();assert(wrong==0);
 }
#else
 OnWorker(all,[&]{auto p=std::make_shared<CDecodePlacement>();const int b=sets;
  {CDecodePlacement::Scope scope(p);p->NeededNow();assert(PERFORMANCE_CORES::ChildAffinity().empty());}
  assert(sets==b&&Mine()==all);});
#endif
 assert(system(("rm -rf '"+root+"'").c_str())==0);
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='decode-placement-') as directory:
        work = Path(directory)
        for name, text in [('ServiceBroker.h', SETTINGS_STUB),
                           ('settings/Settings.h', '#pragma once\n#include "ServiceBroker.h"\n'),
                           ('settings/SettingsComponent.h', '#pragma once\n#include "ServiceBroker.h"\n'),
                           ('utils/log.h', LOG_STUB),
                           ('utils/PlaybackDiagnostics.h',
                            '#pragma once\n#include <cstdint>\nnamespace PLAYBACK_DIAGNOSTICS{inline uint64_t NowUs(){return 0;}}\n')]:
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (work / 'test.cpp').write_text(TEST)
        builds = [(['-DTARGET_LINUX=1', '-DHAS_LIBAMCODEC=1'], 'address,undefined'),
                  (['-DTARGET_LINUX=1', '-DHAS_LIBAMCODEC=1'], 'thread'),
                  ([], 'address,undefined')]
        for flags, sanitizer in builds:
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-g', '-O1', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-variable', '-Wno-unused-but-set-variable', '-pthread',
                            '-fsanitize=' + sanitizer, '-fno-omit-frame-pointer', *flags,
                            '-I', str(work), '-I', str(ROOT / 'xbmc'), str(work / 'test.cpp'),
                            '-o', str(work / 'test')], check=True)
            subprocess.run(['setarch', os.uname().machine, '-R', str(work / 'test')], check=True, timeout=120,
                           env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'TSAN_OPTIONS': 'halt_on_error=1'})
    print('PASS: unneeded decode on the slow cluster and restored, announce once, needed before/while decoding, '
          'failed widening retried or restored by the worker, children of a restricted worker take its CPUs, '
          'null/uniform/missing/restricted/slow-only/fast-only workers, set and restore failures, '
          'concurrent stress under ASan/UBSan and TSan, non-target no-op')


if __name__ == '__main__':
    main()
