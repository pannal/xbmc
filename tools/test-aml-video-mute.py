#!/usr/bin/env python3
"""Run the actual mute owner controller: handoff, frame identity, expiry, races.
Host callbacks only; no sysfs, HDMI or physical blackout acknowledgement.
"""
import argparse, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TEST=r'''
#include <cassert>
#include <thread>
#include <future>
#include <vector>
#include <iostream>
using namespace std::chrono_literals;
int main(){
 std::vector<bool>writes;CAMLVideoMute c([&](bool mute){writes.push_back(mute);});
 auto now=CAMLVideoMute::Clock::now();
 auto a=c.Acquire(),b=c.Acquire();assert(writes==std::vector<bool>{true});
 c.Release(a);assert(writes.size()==1);c.Release(b);assert(!writes.back());
 a=c.Acquire();auto beforeFrame=c.HandoffGeneration();assert(!beforeFrame);
 c.Handoff(a,now+6s);auto g=c.HandoffGeneration();assert(g&&writes.back());
 c.Presented(beforeFrame,true,true,false);assert(c.HandoffGeneration()==g);
 c.Presented(g,false,true,false);c.Presented(g,true,false,false);c.Presented(g,true,true,true);
 assert(c.HandoffGeneration()==g&&writes.back());
 c.Presented(g,true,true,false);assert(!c.HandoffGeneration()&&!writes.back());
 a=c.Acquire();c.Handoff(a,now+6s);g=c.HandoffGeneration();
 b=c.Acquire();auto count=writes.size();c.Release(a);c.Presented(g,true,true,false);c.Expire(now+30s);
 assert(writes.size()==count&&writes.back());c.Release(b);assert(!writes.back());
 a=c.Acquire();c.CancelHandoff();assert(writes.back());c.Handoff(a,now+6s);
 assert(!c.HandoffGeneration()&&!writes.back()); // unmasked start overtakes late close
 a=c.Acquire();b=c.Acquire();c.Handoff(b,now+6s);c.Handoff(a,now+6s);
 assert(!c.HandoffGeneration()&&!writes.back()); // older owner cannot become newest
 a=c.Acquire();c.Handoff(a,now+6s);g=c.HandoffGeneration();
 c.Expire(now+5999ms);assert(c.HandoffGeneration()==g);c.Expire(now+6s);
 assert(!c.HandoffGeneration()&&!writes.back());
 a=c.Acquire();c.Handoff(a,now+6s);g=c.HandoffGeneration();
 b=c.Acquire();c.Handoff(b,now+6s);assert(c.HandoffGeneration()!=g);
 c.Presented(g,true,true,false);assert(c.HandoffGeneration()&&writes.back());
 c.CancelHandoff();assert(!writes.back());
 // Real threads, with an old close held until its replacement owns the mute.
 for(int i=0;i<100;i++){
   a=c.Acquire();count=writes.size();std::promise<void> started;
   auto ready=started.get_future();
   std::thread old([&]{ready.wait();c.Handoff(a,now+6s);c.Release(a);});
   b=c.Acquire();started.set_value();old.join();
   assert(writes.size()==count&&writes.back());c.Release(b);assert(!writes.back());
 }
 std::cout<<"PASS: owner isolation, late close, accepted GUI frame identity, timeout, unmasked/shutdown cancel and threaded replacement\n";
}
'''
def main():
 p=argparse.ArgumentParser();p.add_argument('--negative-controls',action='store_true');args=p.parse_args()
 header=(ROOT/'xbmc/utils/AMLVideoMute.h').read_text().replace('#pragma once','')
 def run(code,negative=False):
  with tempfile.TemporaryDirectory(prefix='aml-mute-') as tmp:
   path=Path(tmp);(path/'test.cpp').write_text(code+TEST)
   cmd=['g++','-std=c++17','-pthread','-Wall','-Wextra','-Werror','-Wno-unused-parameter',str(path/'test.cpp'),'-o',str(path/'test')]
   if not negative:cmd+=['-fsanitize=address,undefined','-fno-pie','-no-pie']
   subprocess.run(cmd,check=True)
   r=subprocess.run([str(path/'test')],capture_output=True,text=True,timeout=20)
   if negative:assert r.returncode!=0 and 'Assertion' in r.stderr,r.stdout+r.stderr
   else:assert r.returncode==0,r.stdout+r.stderr;print(r.stdout.strip())
 run(header)
 if args.negative_controls:
  for label,old,new in [
   ('global unmute','m_owners.erase(owner);','m_owners.clear();'),
   ('old close supersedes replacement','it->second == m_generation','true'),
   ('stale frame release','m_handoff.load() == generation','true'),
   ('failed swap release','!accepted ||',''),
   ('unready display release','!ready ||',''),
   ('video frame releases close hold',' || fullscreenVideo',''),
   ('never expire','now >= m_deadline','false'),
   ('cancel active owners','m_handoff = 0;\n    Apply();','m_handoff = 0;\n    m_owners.clear();\n    Apply();'),
  ]:
   assert old in header,label
   run(header.replace(old,new),True);print('REJECTED:',label)
if __name__=='__main__':main()
