#!/usr/bin/env python3
"""Actual singleton/sysfs selection and owner controller with recording sysfs.
No physical output claims. Each scenario runs in a new process. --baseline REF
replaces only AMLUtils' factory and must fail the dedicated-slot recovery test.
"""
import argparse, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILE='xbmc/utils/AMLUtils.cpp'
def function(s,marker):
 a=s.index(marker);i=s.index('{',a)+1;depth=1
 while depth:depth+=(s[i]=='{')-(s[i]=='}');i+=1
 return s[a:i]
PRE=r'''
#include "utils/AMLVideoMute.h"
#include <cassert>
#include <string>
#include <stdexcept>
#include <vector>
#include <iostream>
static bool dedicated=true,kodiSlot=true,hdr=false,probeFails=false,writeFails=false;
static unsigned refs=0,probes=0,attempts=0;
static std::vector<std::pair<std::string,int>> writes;
constexpr int LOGDEBUG=0,LOGERROR=1;
struct CLog{template<class... T>static void Log(T&&...) {}};
struct CSysfsPath{
 std::string path;
 explicit CSysfsPath(const char* p):path(p){}
 CSysfsPath(const char* p,int value):path(p){
  ++attempts;if(writeFails)throw std::runtime_error("sysfs write");
  writes.emplace_back(path,value);
  if(path.find("vid_mute_kodi")!=std::string::npos){assert(dedicated);kodiSlot=value!=0;}
  else if(value)++refs;else if(refs)--refs;
 }
 bool Exists(){++probes;if(probeFails)throw std::runtime_error("probe");return dedicated;}
};
'''
TEST=r'''
int main(int argc,char** argv){
 assert(argc==2);std::string scenario=argv[1];
 const bool modern=scenario=="dedicated"||scenario=="mixed"||scenario=="write-failure";
 dedicated=modern;kodiSlot=modern;hdr=scenario=="mixed";
 refs=(scenario=="mixed"||!modern)?2:0;
 probeFails=scenario=="probe-failure";writeFails=scenario=="write-failure";
 auto& c=aml_video_mute_controller();
 if(writeFails){auto hold=c.Acquire();c.Release(hold);assert(attempts==3);return 0;}
 if(modern){assert(!kodiSlot && "new process clears only stale Kodi slot");assert(writes.size()==1);}
 else {assert(writes.empty()&&refs==2 && "legacy startup must not drain anonymous owners");}
 assert(probes==1);
 if(scenario=="mixed")assert(refs==2&&hdr && "other owner classes survive Kodi recovery");
 const auto before=writes.size();auto a=c.Acquire();assert(writes.size()==before+1);
 auto b=aml_video_mute_controller().Acquire();assert(probes==1&&writes.size()==before+1);
 assert(modern?kodiSlot:refs==3);
 c.Release(a);assert(writes.size()==before+1);
 c.Release(b);assert(writes.size()==before+2);
 assert(modern?!kodiSlot:refs==2);
 if(scenario=="mixed")assert(refs==2&&hdr);
 // A handoff is not an extra kernel reference or a new process initialization.
 a=c.Acquire();auto count=writes.size();
 c.Handoff(a,CAMLVideoMute::Clock::now()+std::chrono::seconds(6));
 auto& same=aml_video_mute_controller();assert(&same==&c&&writes.size()==count);
 same.CancelHandoff();assert(writes.size()==count+1);
 if(modern)assert(!kodiSlot);else assert(refs==2);
 std::cout<<"PASS: "<<scenario<<" production singleton recovery, route selection and active hold preservation\n";
}
'''
def main():
 p=argparse.ArgumentParser();p.add_argument('--baseline');p.add_argument('--negative-controls',action='store_true');args=p.parse_args()
 s=subprocess.check_output(['git','show',args.baseline+':'+FILE],cwd=ROOT,text=True) if args.baseline else (ROOT/FILE).read_text()
 factory=function(s,'CAMLVideoMute& aml_video_mute_controller()')
 def run(factory,negative=None):
  with tempfile.TemporaryDirectory(prefix='aml-mute-startup-') as tmp:
   d=Path(tmp);(d/'test.cpp').write_text(PRE+factory+TEST)
   subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie','-I',str(ROOT/'xbmc'),str(d/'test.cpp'),'-o',str(d/'test')],check=True)
   for scenario in [negative] if negative else ['dedicated','mixed','legacy','probe-failure','write-failure']:
    r=subprocess.run([str(d/'test'),scenario],capture_output=True,text=True,timeout=20)
    if negative:assert r.returncode and 'Assertion' in r.stderr,r.stdout+r.stderr
    else:assert not r.returncode,r.stdout+r.stderr;print(r.stdout.strip())
 run(factory)
 if args.negative_controls:
  for label,old,new,case in [
   ('no process-start reset','}, dedicated);','}, false);','dedicated'),
   ('reset anonymous counter','}, dedicated);','}, true);','legacy'),
   ('dedicated writes go to shared count','CSysfsPath(dedicated ?','CSysfsPath(false ?','mixed'),
   ('reset live owner on every access','  return controller;','  if (dedicated) CSysfsPath("/sys/class/amhdmitx/amhdmitx0/vid_mute_kodi", 0);\n  return controller;','dedicated'),
  ]:
   assert old in factory,label;run(factory.replace(old,new),case);print('REJECTED:',label)
if __name__=='__main__':main()
