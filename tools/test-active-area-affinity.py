#!/usr/bin/env python3
"""Exercise production CPU selection/syscall boundaries; target speed is unverified."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_STUB = r'''
#pragma once
struct CSettings {
 static constexpr int SETTING_SUBTITLES_ISOLATEACTIVEAREA=1;
 static inline bool enabled=false;
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
constexpr int LOGDEBUG=0,LOGINFO=1,LOGERROR=2;
struct CLog {
 static inline int lastLevel=-1;
 template<class... T>static void Log(int level,const char* format,T&&...){
  lastLevel=level;size_t fields=0;for(auto p=format;*p;++p)fields+=*p=='{';
  assert(fields==sizeof...(T));
 }
};
'''
TEST = r'''
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <cassert>
#include <atomic>
#include <cerrno>
#include <future>
#include <set>
#include <thread>
#include "ServiceBroker.h"
int mode=0;
std::atomic<int> sets{0},reads{0};
std::set<int> available{0,1,2,3,4,5},livePlayback{2,3,4,5};
thread_local std::set<int> effective;
int FakeSet(pid_t tid,size_t,const cpu_set_t* mask){
 assert(tid==0);++sets;if(mode==1){errno=EPERM;return -1;}
 effective.clear();for(int cpu=0;cpu<CPU_SETSIZE;++cpu)if(CPU_ISSET(cpu,mask))effective.insert(cpu);
 return 0;
}
int FakeGet(pid_t tid,size_t,cpu_set_t* mask){
 ++reads;CPU_ZERO(mask);
 if((tid==0&&mode==4)||(tid==getpid()&&mode==5)){errno=EINVAL;return -1;}
 auto cpus=tid==0?effective:(tid==getpid()?available:livePlayback);
 if(tid==0&&mode==2)cpus={0};
 if(tid==0&&mode==3)cpus={0,2};
 for(int cpu:cpus)CPU_SET(cpu,mask);return 0;
}
#define sched_setaffinity FakeSet
#define sched_getaffinity FakeGet
#include "threads/PerformanceCores.cpp"
int main(){
 using namespace PERFORMANCE_CORES;
 assert(DetectionRequested({0,1,2,3,4,5},{2,3,4,5})==std::set<int>({0,1}));
 assert(DetectionRequested({2,3},{2,3}).empty());
 const auto noSets=sets.load(),noReads=reads.load();
 assert(ApplyDetectionCurrentThread());assert(sets==noSets&&reads==noReads);
#if defined(TARGET_LINUX) && defined(HAS_LIBAMCODEC)
 CSettings::enabled=true;
 assert(ApplyDetectionCurrentThread()&&effective==std::set<int>({0,1}));
 for(mode=1;mode<=5;++mode){
  const bool ok=ApplyDetectionCurrentThread();assert(ok==(mode==2));
  assert(CLog::lastLevel==(ok?LOGINFO:LOGERROR));
 }
 mode=0;available={2,3,4,5};const auto before=sets.load();
 assert(!ApplyDetectionCurrentThread()&&sets==before);
 available={0,1,2,3,4,5,6,7};
 assert(ApplyDetectionCurrentThread()&&effective==std::set<int>({0,1,6,7}));
 // The actual registration lifetime is tied to the playback thread's lifetime.
 std::promise<void> ready,finish;auto wait=finish.get_future();
 std::thread playback([&]{ApplyCurrentThread("fixture-playback",1);ready.set_value();wait.wait();});
 ready.get_future().wait();assert(s_playbackThreads.size()==1);
 livePlayback={0,2,3,4,5};
 assert(ApplyDetectionCurrentThread()&&effective==std::set<int>({1,6,7}));
 livePlayback={0,1,2,3,4,5,6,7};
 assert(!ApplyDetectionCurrentThread());
 finish.set_value();playback.join();assert(s_playbackThreads.empty());
#else
 CSettings::enabled=true;assert(ApplyDetectionCurrentThread());assert(sets==0&&reads==0);
#endif
}
'''

def main():
    with tempfile.TemporaryDirectory(prefix='active-area-affinity-') as directory:
        work = Path(directory)
        for name, text in [('ServiceBroker.h', SETTINGS_STUB),
                           ('settings/Settings.h', '#pragma once\n#include "ServiceBroker.h"\n'),
                           ('settings/SettingsComponent.h', '#pragma once\n#include "ServiceBroker.h"\n'),
                           ('utils/log.h', LOG_STUB)]:
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (work / 'test.cpp').write_text(TEST)
        for target in (True, False):
            flags = ['-DTARGET_LINUX=1', '-DHAS_LIBAMCODEC=1'] if target else []
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-Wno-misleading-indentation', '-pthread', '-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', *flags,
                            '-I', str(work), '-I', str(ROOT/'xbmc'), str(work/'test.cpp'),
                            '-o', str(work/'test')], check=True)
            subprocess.run([str(work/'test')], check=True, timeout=30,
                           env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
    print('PASS: default-off bypass, dynamic available/playback masks, isolation failure, live thread registration, target/non-target boundaries')

if __name__ == '__main__':
    main()
