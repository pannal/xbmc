#!/usr/bin/env python3
"""Run real diagnostic producers with counted clock reads and logging toggles.
Synthetic native/clock hooks establish observation overhead and presenter progress,
not target performance, scanout or physical playback quality.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
HARNESS = r'''
#include "utils/PlaybackEndDiagnostics.h"
#include "Presenter.h"
#include "commons/ilog.h"
#include <cassert>
#include <future>
#include <iostream>
using namespace std::chrono_literals;
namespace spdlog {
namespace level {enum level_enum {off,info,trace};const char* to_string_view(level_enum){return "level";}}
void set_level(level::level_enum) {}
}
struct CLog {
 int m_logLevel=LOG_LEVEL_DEBUG;
 struct Logger {spdlog::level::level_enum level(){return spdlog::level::info;}};
 std::shared_ptr<Logger> m_defaultLogger;
 template<class...T> void FormatAndLogInternal(T&&...){}
 void SetLogLevel(int);
};
@SET_LEVEL@
struct Frame:CAMLPresenter::Frame {
 std::atomic<int> polls{0};
 Submission Submit(int&)override{return Submission::SUBMITTED;}
 bool Poll()override{++polls;return true;}
 bool Retire()override{return true;}
};
template<class F> void await(F ready){
 const auto end=std::chrono::steady_clock::now()+2s;
 while(!ready()){assert(std::chrono::steady_clock::now()<end);std::this_thread::sleep_for(1ms);}
}
int main(){
 using namespace PLAYBACK_DIAGNOSTICS;
 CLog log;log.SetLogLevel(LOG_LEVEL_NORMAL);assert(!Enabled());
 log.SetLogLevel(LOG_LEVEL_MAX+1);assert(!Enabled());
 log.SetLogLevel(LOG_LEVEL_DEBUG);assert(Enabled());
 // Even the existing same-level early return must update observation state.
 log.m_defaultLogger=std::make_shared<CLog::Logger>();
 log.SetLogLevel(LOG_LEVEL_NORMAL);assert(!Enabled());
 ClockHistory history;const auto before=clockReads.load();
 history.Record(1,2,3);history.MarkCorrection(4,5);
 assert(history.Since(0).events.empty()&&clockReads==before);
 VideoStages stages;assert(clockReads==before);stages.BeginIteration(100,0);
 assert(stages.sinceUs==100&&stages.loopUs==100&&stages.maxLoopUs==0);
 EndDisplayTrace trace;int formatted=0;
 auto detail=[&]{++formatted;return std::string("detail");};
 trace.Begin(1,"off");trace.Record(2,"off",detail);
 assert(!trace.Active(2)&&trace.SnapshotReason(2)==nullptr&&formatted==0);
 size_t records=0;trace.Drain([&](const auto&){++records;});assert(records==0);
 log.SetLogLevel(LOG_LEVEL_DEBUG);
 history.Record(1,2,3);history.MarkCorrection(4,5);assert(clockReads>before);
 assert(history.Since(0).events.back().adjustment==5);
 trace.Begin(10,"on");trace.Record(11,"on",detail);assert(formatted==1);
 log.SetLogLevel(LOG_LEVEL_NORMAL);trace.Record(12,"off",detail);
 assert(formatted==1&&trace.SnapshotReason(12)==nullptr);trace.Cancel();
 log.SetLogLevel(LOG_LEVEL_DEBUG);assert(!trace.Active(13));
 history.Record(6,7,8);log.SetLogLevel(LOG_LEVEL_NORMAL);history.Record(9,10,11);
 log.SetLogLevel(LOG_LEVEL_DEBUG);history.MarkCorrection(12,13);assert(history.Since(0).events.empty());
 // Same frame selection and native passes with and without measurements.
 log.SetLogLevel(LOG_LEVEL_NORMAL);
 CAMLPresenter::Hooks hooks;std::atomic<int> cpuSamples{0};
 hooks.start=[]{return true;};hooks.finish=[]{};
 hooks.timing=[]{return CAMLPresenter::Timing{1000000,1,1000,0,false};};
 hooks.adjustClock=[](double){};hooks.sampleCpu=[&]{++cpuSamples;return 2;};
 CAMLPresenter queue(4,hooks);queue.Show(true);
 auto frame=std::make_shared<Frame>();frame->pts=0;frame->epoch=1;
 const auto serial=queue.Reserve(0ms);assert(serial&&queue.Publish(serial,frame));
 const auto idleReads=clockReads.load();queue.Launch();queue.Authorized();
 await([&]{return bool(queue.PendingControl().frame);});
 // Only the cadence clock runs: no diagnostic NowUs or CPU sampling while off.
 std::this_thread::sleep_for(5ms);assert(clockReads==idleReads&&cpuSamples==0);
 log.SetLogLevel(LOG_LEVEL_DEBUG);
 assert(queue.Diagnostics().pendingControlUs==0); // request began without a timestamp
 assert(queue.CompleteControl(queue.PendingControl()));
 await([&]{return frame->polls>0;});
 assert(clockReads>idleReads);
 log.SetLogLevel(LOG_LEVEL_NORMAL);std::this_thread::sleep_for(10ms);
 const auto stable=clockReads.load();std::this_thread::sleep_for(10ms);
 assert(clockReads==stable);queue.Stop();
 const auto progress=queue.Diagnostics().sample.progress;
 assert(progress.selected==1&&progress.completed==1&&progress.polls>0);
 auto leases=queue.TakeFrames();leases.clear();
 std::cout<<"PASS logger-level binding, zero off-mode timing reads, trace cancellation, mid-control toggles and native presenter progress\n";
}
'''


def main():
    header = (ROOT / 'xbmc/utils/PlaybackDiagnostics.h').read_text()
    header = header.replace('inline uint64_t NowUs()\n{',
                            'inline std::atomic<uint64_t> clockReads{0};\ninline uint64_t NowUs()\n{\n  ++clockReads;')
    presenter = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h').read_text()
    log = (ROOT / 'xbmc/utils/log.cpp').read_text()
    level = extract(log, 'void CLog::SetLogLevel(')
    with tempfile.TemporaryDirectory(prefix='diagnostic-level-') as tmp:
        path = Path(tmp)
        (path / 'utils').mkdir()
        (path / 'utils/PlaybackDiagnostics.h').write_text(header)
        (path / 'Presenter.h').write_text(presenter)
        cpp = path / 'check.cpp'
        exe = path / 'check'
        cpp.write_text(HARNESS.replace('@SET_LEVEL@', level))
        args = ['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                '-I' + str(path), '-I' + str(ROOT / 'xbmc'), str(cpp), '-o', str(exe)]
        subprocess.run(args, check=True)
        env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}
        subprocess.run([str(exe)], env=env, check=True, timeout=10)
        # Each negative control must compile and then fail a runtime assertion.
        mutations = {
            'always-collect': header.replace('return debugEnabled.load(std::memory_order_relaxed);', 'return true;'),
            'collect-off-operations': presenter.replace('if (PLAYBACK_DIAGNOSTICS::Enabled())\n      m_operationToken.store', 'if (true)\n      m_operationToken.store'),
            'invent-pending-age': presenter.replace('m_pending.frame && m_controlSinceUs ? now - m_controlSinceUs : 0', 'm_pending.frame ? now - m_controlSinceUs : 0'),
        }
        for name, mutant in mutations.items():
            (path / 'utils/PlaybackDiagnostics.h').write_text(mutant if name == 'always-collect' else header)
            (path / 'Presenter.h').write_text(mutant if name != 'always-collect' else presenter)
            subprocess.run(args, check=True)
            result = subprocess.run([str(exe)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            assert result.returncode != 0, name
            print('REJECTED', name)


if __name__ == '__main__':
    main()
