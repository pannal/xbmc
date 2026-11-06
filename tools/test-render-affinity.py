#!/usr/bin/env python3
"""Exercise production GUI render-thread placement with fake CPU masks and sysfs; device speed is unverified."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_STUB = r'''
#pragma once
struct CSettings {
 static constexpr int SETTING_SUBTITLES_ISOLATEACTIVEAREA=1;
 static inline bool enabled=true;
 bool GetBool(int) const {return enabled;}
};
struct SettingsComponent {
 CSettings* GetSettings(){static CSettings settings;return &settings;}
};
struct CServiceBroker {
 static SettingsComponent* GetSettingsComponent(){static SettingsComponent component;return &component;}
};
'''
LOG_STUB = r'''
#pragma once
#include <cassert>
#include <string>
constexpr int LOGDEBUG=0,LOGINFO=1,LOGERROR=2;
struct CLog {
 static inline int lastLevel=-1,count=0;static inline std::string lastFormat;
 template<class... T>static void Log(int level,const char* format,T&&...){
  ++count;lastLevel=level;lastFormat=format;size_t fields=0;for(auto p=format;*p;++p)fields+=*p=='{';
  assert(fields==sizeof...(T));
 }
};
'''
TEST = r'''
#include <sched.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <fstream>
#include <set>
#include <string>
#include <thread>
#include "ServiceBroker.h"
int mode=0;
std::atomic<int> sets{0};
std::set<int> mainMask{0,1,2,3,4,5};
thread_local std::set<int> effective{0,1,2,3,4,5};
int FakeSet(pid_t tid,size_t,const cpu_set_t* mask){
 assert(tid==0);++sets;if(mode==1){errno=EPERM;return -1;}
 if(mode==6&&!CPU_ISSET(0,mask)&&!CPU_ISSET(1,mask)){errno=EINVAL;return -1;}  // fast cluster offline
 effective.clear();for(int cpu=0;cpu<CPU_SETSIZE;++cpu)if(CPU_ISSET(cpu,mask))effective.insert(cpu);
 if(mode==2&&effective==std::set<int>({2,3,4,5}))effective={2,3};  // silent kernel narrowing
 return 0;
}
int FakeGet(pid_t tid,size_t,cpu_set_t* mask){
 CPU_ZERO(mask);
 if(tid==0&&mode==3){errno=EINVAL;return -1;}
 const auto& cpus=tid==0?effective:mainMask;
 for(int cpu:cpus)CPU_SET(cpu,mask);return 0;
}
#define sched_setaffinity FakeSet
#define sched_getaffinity FakeGet
#include "threads/PerformanceCores.cpp"
static std::string root;
static void Frequencies(const std::map<int,uint64_t>& khz){
 const std::string clear="rm -rf '"+root+"'/cpu*";assert(system(clear.c_str())==0);
 for(const auto& [cpu,value]:khz){
  const std::string dir=root+"/cpu"+std::to_string(cpu)+"/cpufreq";
  assert(system(("mkdir -p '"+dir+"'").c_str())==0);
  std::ofstream(dir+"/cpuinfo_max_freq")<<value<<"\n";
 }
}
[[maybe_unused]] static void Reset(){
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
 s_renderThread.store(0);{std::lock_guard<std::mutex> lock(s_renderMutex);s_processCpus.clear();}
#endif
 mode=0;effective={0,1,2,3,4,5};mainMask={0,1,2,3,4,5};
}
int main(){
 using namespace PERFORMANCE_CORES;
 const std::map<int,uint64_t> s922x{{0,1800000},{1,1800000},{2,2208000},{3,2208000},{4,2208000},{5,2208000}};
 // Pure topology decision.
 assert(FastClusterRequested({0,1,2,3,4,5},s922x));
 assert(!FastClusterRequested({0,1,2,3},{{0,1908000},{1,1908000},{2,1908000},{3,1908000}}));  // quad core
 assert(!FastClusterRequested({0,1,2,3,4,5},{{0,2208000},{1,2208000},{2,2208000},{3,2208000},{4,2208000},{5,2208000}}));
 assert(!FastClusterRequested({0,1,2,3,4,5},{{0,1800000},{1,1800000},{2,2208000},{3,2208000},{4,2208000}}));  // missing cpu5
 assert(!FastClusterRequested({0,1,2,3,4,5},{{0,1800000},{1,2208000},{2,2208000},{3,2208000},{4,2208000},{5,2208000}}));
 assert(!FastClusterRequested({0,1,2,3,4},s922x));   // Kodi may not use cpu5
 assert(!FastClusterRequested({2,3,4,5},s922x));     // nothing slower to avoid
 assert(FastClusterRequested({1,2,3,4,5},s922x));
 char dir[]="/tmp/render-affinity-XXXXXX";assert(mkdtemp(dir));root=dir;
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
 s_cpuRoot=root.c_str();
 // Fast cluster: only the calling (render) thread is pinned and recorded.
 Reset();Frequencies(s922x);ApplyRenderThread();
 assert(effective==std::set<int>({2,3,4,5})&&CLog::lastLevel==LOGINFO&&InheritsRenderAffinity());
 assert(ProcessCpus()==std::set<int>({0,1,2,3,4,5}));
 {const int before=sets;ApplyRenderThread();assert(sets==before);}  // idempotent
 // Other threads are not the render thread; a child restores Kodi's CPUs.
 {bool inherits=true;std::set<int> child,grandchild={9};const auto cpus=ChildAffinity();
  assert(cpus==std::set<int>({0,1,2,3,4,5}));
  std::thread t([&]{effective={2,3,4,5};inherits=InheritsRenderAffinity();RestoreChildAffinity(cpus);child=effective;
                    grandchild=ChildAffinity();});
  t.join();assert(!inherits&&child==std::set<int>({0,1,2,3,4,5})&&grandchild.empty());}
 // Detection uses Kodi's CPUs from before pinning, not the pinned main mask.
 mainMask={2,3,4,5};
 {std::set<int> result;std::thread t([&]{assert(ApplyDetectionCurrentThread());result=effective;});t.join();
  assert(result==std::set<int>({0,1}));}
 // Maintenance: every 256th call checks the mask; drift to the slow CPUs is undone.
 {
  auto Calls=[](int n){for(int i=0;i<n;++i)MaintainRenderThread();};
  // Align the internal counter: run until a check happens on an unchanged mask.
  const int aligned=sets;Calls(256);assert(sets==aligned);  // pinned and unchanged: no set
  effective={0,1};                                          // the kernel moved it (fast cluster went offline)
  {const int before=sets,logs=CLog::count;Calls(255);assert(sets==before&&CLog::count==logs);}
  mode=6;                                                   // still offline: wait, log once
  {const int logs=CLog::count;Calls(1);assert(effective==std::set<int>({0,1})&&CLog::count==logs+1&&
     CLog::lastFormat.find("waiting")!=std::string::npos);
   Calls(256*3);assert(CLog::count==logs+1&&effective==std::set<int>({0,1}));}
  mode=0;                                                   // back online: restored and logged once
  {const int logs=CLog::count;Calls(256);assert(effective==std::set<int>({2,3,4,5})&&CLog::count==logs+1&&
     CLog::lastFormat.find("restored")!=std::string::npos);
   const int before=sets;Calls(256*4);assert(sets==before&&CLog::count==logs+1);}
  // A second suspend cycle waits and restores again, logging each transition once.
  {effective={0,1};mode=6;const int logs=CLog::count;Calls(256*2);
   assert(CLog::count==logs+1&&CLog::lastFormat.find("waiting")!=std::string::npos);
   mode=0;Calls(256);assert(effective==std::set<int>({2,3,4,5})&&CLog::count==logs+2);}
  // A widened mask is narrowed again.
  effective={0,1,2,3,4,5};Calls(256);assert(effective==std::set<int>({2,3,4,5}));
  // Another thread calling it changes nothing.
  {std::set<int> m;std::thread t([&]{effective={0,1};for(int i=0;i<512;++i)MaintainRenderThread();m=effective;});t.join();
   assert(m==std::set<int>({0,1}));}
 }
 // Uniform or unreadable topologies leave the thread alone.
 for(const auto& khz:{std::map<int,uint64_t>{{0,1908000},{1,1908000},{2,1908000},{3,1908000},{4,1908000},{5,1908000}},
                      std::map<int,uint64_t>{}}){
  Reset();Frequencies(khz);const int before=sets;ApplyRenderThread();
  assert(sets==before&&effective==std::set<int>({0,1,2,3,4,5})&&!InheritsRenderAffinity());
  assert(CLog::lastLevel==LOGINFO);
  {const int b=sets;for(int i=0;i<512;++i)MaintainRenderThread();assert(sets==b);}
  {std::set<int> result;std::thread t([&]{assert(ApplyDetectionCurrentThread());result=effective;});t.join();
   assert(result==std::set<int>({0,1}));}  // unchanged behaviour while unpinned
 }
 // A restricted Kodi (taskset 0-4) is not narrowed further.
 Reset();Frequencies(s922x);effective={0,1,2,3,4};ApplyRenderThread();
 assert(effective==std::set<int>({0,1,2,3,4})&&!InheritsRenderAffinity());
 // Set failure, silent narrowing and an unreadable result restore Kodi's CPUs and stay unpinned.
 // Mode 4: the set succeeds, then the verification read fails.
 for(int failure:{1,2,4}){
  Reset();Frequencies(s922x);mode=failure;
  ApplyRenderThread();
  assert(!InheritsRenderAffinity()&&CLog::lastLevel==LOGERROR);
  if(failure!=1)assert(effective==std::set<int>({0,1,2,3,4,5}));
  mainMask={2,3,4,5};
  {bool ok=true;std::thread t([&]{ok=ApplyDetectionCurrentThread();});t.join();assert(!ok);}  // live main mask again
 }
#else
 Frequencies(s922x);const int before=sets;ApplyRenderThread();
 assert(sets==before&&!InheritsRenderAffinity()&&ChildAffinity().empty());RestoreChildAffinity({0,1});assert(sets==before);
 for(int i=0;i<512;++i)MaintainRenderThread();assert(sets==before);
#endif
 assert(system(("rm -rf '"+root+"'").c_str())==0);
}
'''

def main():
    test = TEST.replace(
        " if(tid==0&&mode==3){errno=EINVAL;return -1;}",
        " if(tid==0&&mode==3){errno=EINVAL;return -1;}\n"
        " if(tid==0&&mode==5){errno=EINVAL;return -1;}")
    test = test.replace(
        " if(mode==2&&effective==std::set<int>({2,3,4,5}))effective={2,3};  // silent kernel narrowing",
        " if(mode==2&&effective==std::set<int>({2,3,4,5}))effective={2,3};  // silent kernel narrowing\n"
        " if(mode==4)mode=5;  // the read after this set fails")
    with tempfile.TemporaryDirectory(prefix='render-affinity-') as directory:
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
        (work / 'test.cpp').write_text(test)
        for target in (True, False):
            flags = ['-DTARGET_LINUX=1', '-DHAS_LIBAMCODEC=1'] if target else []
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-Wno-misleading-indentation', '-Wno-unused-variable', '-pthread',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                            *flags, '-I', str(work), '-I', str(ROOT/'xbmc'), str(work/'test.cpp'),
                            '-o', str(work/'test')], check=True)
            subprocess.run([str(work/'test')], check=True, timeout=60,
                           env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
    print('PASS: fast-cluster topology rules, render pin and idempotence, child restore, detection with pinned main, '
          'maintenance after drift, offline wait and restore, uniform/unreadable/restricted topologies, set failure, silent narrowing, unreadable result, non-target no-op')

if __name__ == '__main__':
    main()
