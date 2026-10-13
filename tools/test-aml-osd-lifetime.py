#!/usr/bin/env python3
"""Focused production PM4K OSD publication/lifetime regression.

Extracts the complete production OSD method, actual activity/cache declarations,
and exact open/close publication statements. GUI/property/sysfs are recording
fixtures; std::atomic and the two host threads are real. One test scheduling hook
is inserted after the production PM4K condition to exercise close before the
property dereference. This is a deterministic source interleaving witness, not a
CE/device reproduction. Baseline must fail UBSan; fixed source and behavior cases
must pass. No builds of Kodi or changes to source trees.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess

SOURCE = 'xbmc/utils/AMLUtils.cpp'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def publications(source, signature):
    body = function(source, signature)
    # Extract actual publication effects, rather than duplicate their semantics.
    lines = [line.strip() for line in body.splitlines()
             if re.match(r's_pm4k(?:Active|Home)\s*(?:=|\.)', line.strip())]
    assert lines, signature
    return '\n'.join(lines)


def parts(source):
    declarations = '\n'.join(re.findall(r'^static [^\n;]+ s_pm4k(?:Active|Home)[^\n]*;',
                                       source, flags=re.M))
    assert 's_pm4kActive' in declarations
    method = function(source, 'void aml_dv_set_xbmc_osd()')
    condition = method.index('if (s_pm4kActive')
    brace = method.index('{', condition)
    method = method[:brace + 1] + '\n    ScheduleClose();' + method[brace + 1:]
    return declarations, publications(source, 'void aml_dv_open('), \
        publications(source, 'void aml_dv_close()'), method


PRELUDE = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
constexpr int WINDOW_HOME=10000, WINDOW_VIDEO_MENU=12901;
struct Property {std::string text;std::string asString() const {return text;}};
struct CGUIWindow {
  std::map<std::string,std::string> values;
  Property GetProperty(const char* name) {return {values[name]};}
} ownedHome;
struct WindowManager {
  CGUIWindow* currentHome=&ownedHome;bool dialogs=false,videoMenu=false;
  CGUIWindow* GetWindow(int id) {assert(id==WINDOW_HOME);return currentHome;}
  bool HasVisibleDialog() const {return dialogs;}
  bool IsWindowVisible(int id) const {assert(id==WINDOW_VIDEO_MENU);return videoMenu;}
} wm;
struct GUI {WindowManager& GetWindowManager(){return wm;}} gui;
struct Cache {bool avChange=false;bool GetAVChangeExtended()const{return avChange;}} cache;
struct CServiceBroker {
  static GUI* GetGUI(){return &gui;}
  static Cache& GetDataCacheCore(){return cache;}
};
std::atomic_bool s_discMenuVisible{false};
int s_lastOsd=-1,emittedOsd=-1;
struct CSysfsPath {
  CSysfsPath(const char* path,int value) {
    assert(std::string(path)=="/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd");
    emittedOsd=value;
  }
};
void aml_dv_dump_state(const char*){}
std::mutex scheduleMutex;std::condition_variable scheduleChanged;
bool scheduled=false;int phase=0;
void ScheduleClose(){
  if(!scheduled)return;
  std::unique_lock<std::mutex> lock(scheduleMutex);
  phase=1;scheduleChanged.notify_all();
  scheduleChanged.wait(lock,[]{return phase==2;});
}
'''

TESTS = r'''
void Reset(){
  wm.currentHome=&ownedHome;wm.dialogs=false;wm.videoMenu=false;cache.avChange=false;
  ownedHome.values={{"script.plex.osd_active","0"},{"script.plex.is_active","1"},
    {"skin.codeclogosonscreen","0"},{"skin.codeclogososd","0"}};
  s_discMenuVisible.store(false);s_lastOsd=-1;emittedOsd=-1;
  Publish(false,&ownedHome);scheduled=false;phase=0;
}
void Expect(int value){s_lastOsd=-1;emittedOsd=-1;aml_dv_set_xbmc_osd();assert(emittedOsd==value);}
int main(int argc,char** argv){
  const bool onlySchedule=argc==2&&std::string(argv[1])=="schedule";
  if(!onlySchedule){
    // PM4K property has priority over the invisible/other generic dialogs.
    Reset();Publish(true,&ownedHome);wm.dialogs=true;Expect(0);
    ownedHome.values["script.plex.osd_active"]="1";Expect(1);
    // Activity can be stale for one frame, but no Home must never be dereferenced.
    Reset();wm.currentHome=nullptr;Publish(true,nullptr);wm.dialogs=true;Expect(1);
    Reset();wm.currentHome=nullptr;Publish(true,nullptr);Expect(0);
    // Completed close returns to the generic OSD path.
    Reset();Publish(true,&ownedHome);wm.dialogs=true;ClosePublication();Expect(1);
    // Existing generic OSD behavior and skin ownership of AVChange are preserved.
    Reset();Expect(0);wm.dialogs=true;Expect(1);
    Reset();wm.videoMenu=true;Expect(1);
    Reset();ownedHome.values["skin.codeclogosonscreen"]="1";Expect(1);
    Reset();cache.avChange=true;Expect(1);
    ownedHome.values["skin.codeclogososd"]="1";Expect(0);
    wm.dialogs=true;Expect(1);
    // Disc menus remain OSD even when PM4K hides its own OSD.
    Reset();Publish(true,&ownedHome);s_discMenuVisible.store(true);Expect(1);
    Reset();s_discMenuVisible.store(true);Expect(1);
  }
  // Close occurs after condition selection. Owned builtin Home remains alive.
  // Original source reloads its nulled cache; corrected source holds GUI's local.
  Reset();Publish(true,&ownedHome);ownedHome.values["script.plex.osd_active"]="1";
  scheduled=true;
  std::thread closer([]{
    std::unique_lock<std::mutex> lock(scheduleMutex);
    scheduleChanged.wait(lock,[]{return phase==1;});
    ClosePublication();phase=2;scheduleChanged.notify_all();
  });
  Expect(1);closer.join();scheduled=false;
  // Next GUI frame sees the closed publication and ordinary empty GUI state.
  Expect(0);
  std::cout<<"PASS production OSD/open-close publication, scheduled close and policy cases\n";
}
'''


def generate(source, mutation=None):
    declarations, opened, closed, method = parts(source)
    if mutation == 'cached-pointer':
        condition = method[method.index('if (s_pm4kActive'):]
        match = re.search(r'&&\s*(\w+)\s*\)', condition)
        assert match, 'cannot locate local Home condition'
        local = match[1]
        assert local != 's_pm4kHome'
        brace = method.index('{', method.index('if (s_pm4kActive'))
        end = method.index('\n  else', brace)
        branch = method[method.index('if (s_pm4kActive'):end]
        branch = re.sub(r'\b' + re.escape(local) + r'\b', 's_pm4kHome', branch)
        method = method[:method.index('if (s_pm4kActive')] + branch + method[end:]
        declarations += '\nstatic CGUIWindow* s_pm4kHome=nullptr;'
        opened = 's_pm4kHome=pm4kHome;\n' + opened
        closed += '\ns_pm4kHome=nullptr;'
    elif mutation == 'missing-home-guard':
        condition = method.index('if (s_pm4kActive')
        end = method.index('{',condition)
        text = method[condition:end]
        changed, count = re.subn(r'\s*&&\s*\w+\s*(?=\))','',text)
        assert count == 1, text
        method = method[:condition] + changed + method[end:]
    elif mutation == 'keep-active-after-close':
        closed = ''
    elif mutation == 'lose-disc-menu':
        method, count = re.subn(r'\s*\|\|\s*s_discMenuVisible\.load\([^)]*\)', '', method)
        assert count == 1
    elif mutation == 'ignore-pm4k-property':
        assert '"script.plex.osd_active"' in method
        method = method.replace('"script.plex.osd_active"','"script.plex.is_active"')
    code = PRELUDE + declarations
    code += '\nvoid Publish(bool pm4kActive,CGUIWindow* pm4kHome){\n' + opened + '\n}\n'
    code += '\nvoid ClosePublication(){\n' + closed + '\n}\n' + method + '\n' + TESTS
    return code


def run_case(root, out, name, source, mutation=None, schedule_only=False):
    cpp, executable = out / (name + '.cpp'), out / name
    cpp.write_text(generate(source, mutation))
    argv = [os.environ.get('CXX','g++'), '-std=c++17','-O2','-Wall','-Wextra',
            '-Werror','-Wno-unused-parameter','-pthread','-fsanitize=undefined',
            '-fno-sanitize-recover=undefined',str(cpp),'-o',str(executable)]
    compiled = subprocess.run(argv,cwd=out,capture_output=True,text=True,timeout=60)
    (out / (name + '.compile.log')).write_text(compiled.stdout+compiled.stderr)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(executable), *(['schedule'] if schedule_only else [])],
                            cwd=out,capture_output=True,text=True,timeout=20)
    (out / (name + '.run.log')).write_text(result.stdout+result.stderr)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,default=Path('/tmp/aml-osd-lifetime'))
    parser.add_argument('--baseline',default='2a8fd121f87e41f257ead0f5421e3de974b21a9d')
    parser.add_argument('--baseline-only',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    baseline=subprocess.run(['git','show',args.baseline+':'+SOURCE],cwd=args.root,
                            capture_output=True,text=True,check=True).stdout
    original=run_case(args.root,args.output,'baseline',baseline,schedule_only=True)
    assert original.returncode != 0 and 'null pointer' in original.stderr and 'CGUIWindow' in original.stderr, \
        (original.returncode,original.stdout,original.stderr)
    print('Baseline FAIL reproduced: scheduled close invalidates cached pointer',flush=True)
    if args.baseline_only:return
    current=(args.root/SOURCE).read_text()
    declarations=parts(current)[0]
    assert re.search(r'static std::atomic(?:_bool|<\s*bool\s*>)\s+s_pm4kActive',declarations), \
        'activity publication must use actual std::atomic<bool>'
    assert 's_pm4kHome' not in declarations, 'cross-thread GUI pointer cache remains'
    fixed=run_case(args.root,args.output,'fixed',current)
    assert fixed.returncode==0,(fixed.returncode,fixed.stdout,fixed.stderr)
    print(fixed.stdout.strip(),flush=True)
    controls=['cached-pointer','missing-home-guard','keep-active-after-close',
              'lose-disc-menu','ignore-pm4k-property']
    for control in controls:
        bad=run_case(args.root,args.output,'negative-'+control,current,control)
        assert bad.returncode!=0,'destructive control survived: '+control
        print('Rejected behavioral control: '+control,flush=True)
    print('AML OSD lifetime: PASS; baseline fails, fixed passes, five controls rejected; '
          'host fixtures, not CE/GPU/device verification')

if __name__=='__main__':main()
