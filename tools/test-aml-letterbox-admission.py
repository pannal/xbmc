#!/usr/bin/env python3
"""Run the production letterbox watcher with real session/worker admission.

Cached metadata, settings and sysfs are recording substitutes. No-op sampling
must leave decoder/presentation admission open; actual writes retain exclusion.
"""
import argparse
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-aml-native-workers.py'))
function, run = fixture['function'], fixture['run']
WATCH = 'static void _auto_letterbox_watch_run('
PATH = 'xbmc/utils/AMLUtils.cpp'

TESTS = r'''
void watcher_cached_samples_do_not_fence(){
 CAMLSession s;init(s);aml_dv_set_active_area_geometry(3840,1600,true);
 std::promise<void> sampled;auto complete=sampled.get_future();int samples=0;
 sampleHook=[&]{
   assert(s.AcquireDecoder()); // real session must remain available during cache reads
   if(++samples==2)sampled.set_value();
 };
 aml_dv_auto_letterbox_watch_start();
 assert(complete.wait_for(2s)==std::future_status::ready);
 aml_dv_auto_letterbox_watch_stop();sampleHook={};assert(watchWrites==0&&s_autoLbAdditive);
}
void watcher_rechecks_before_write(bool manual){
 CAMLSession s;init(s);aml_dv_set_active_area_geometry(3840,1600,true);
 cache.meta={true,280,280,0,0}; // impossible, threshold two
 std::promise<void> sampled;auto complete=sampled.get_future();int samples=0;
 sampleHook=[&]{
   if(++samples==2){
     // The copy returned for sample two still carries the previous metadata.
     // Policy changes before native admission must be observed before writing.
     if(manual)manualOverride=true;else cache.meta={};
     sampled.set_value();
   }
   if(samples==3){assert(!s.AcquireDecoder());} // final recheck owns exclusion
 };
 aml_dv_auto_letterbox_watch_start();assert(complete.wait_for(2s)==std::future_status::ready);
 // Let the retained worker finish its post-sample check before cancelling.
 std::this_thread::sleep_for(50ms);
 aml_dv_auto_letterbox_watch_stop();sampleHook={};manualOverride=false;
 assert(watchWrites==0&&s_autoLbAdditive);
 if(!manual)assert(samples==3);
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = fixture['source']()
    if args.baseline:
        baseline = subprocess.check_output(['git', 'show', f'{args.baseline}:{PATH}'], cwd=ROOT, text=True)
        current = (ROOT / PATH).read_text()
        code = code.replace(function(current, WATCH), function(baseline, WATCH))
    code = code.replace('struct Cache{', 'std::function<void()> sampleHook;std::atomic<bool> manualOverride{false};\nstruct Cache{')
    code = code.replace('Metadata GetVideoDoViFrameMetadata(){return meta;}',
                        'Metadata GetVideoDoViFrameMetadata(){auto snapshot=meta;if(sampleHook)sampleHook();return snapshot;}')
    code = code.replace('bool aml_dv_l5_override_active(){return false;}',
                        'bool aml_dv_l5_override_active(){return manualOverride;}')
    code = code.replace('int main(){ready();', TESTS + '\nint main(){ready();watcher_cached_samples_do_not_fence();watcher_rechecks_before_write(false);watcher_rechecks_before_write(true);')
    run(code)
    if args.negative_controls:
        for name, old, new in [
            ('cached sampling fences playback',
             '    if (!CServiceBroker::IsServiceManagerUp())',
             '    CAMLNativeTransaction sampleFence; if (!run.Admit(sampleFence)) return;\n    if (!CServiceBroker::IsServiceManagerUp())'),
            ('manual override recheck omitted',
             '!run.Admit(native) || !CServiceBroker::IsServiceManagerUp() ||\n        !aml_dv_auto_letterbox_active()',
             '!run.Admit(native) || !CServiceBroker::IsServiceManagerUp()'),
            ('metadata recheck omitted',
             'w, h, src, reason) != verdict)', 'w, h, src, reason) != verdict && false)'),
        ]:
            assert code.count(old) == 1, name
            run(code.replace(old, new), negative=True)
            print('REJECTED:', name)


if __name__ == '__main__':
    main()
