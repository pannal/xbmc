#!/usr/bin/env python3
"""Exercise the wake sequence and progress wait with local network and GUI adapters.

The production code decides when to resolve, ping, send a packet, wait for
services, and start a cooldown. The adapters count those operations and can
trigger cancellation between them. No network or Kodi installation is needed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'xbmc/network/WakeOnAccess.cpp'

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <functional>
#include <stdexcept>
#include <string>
#include <thread>
using namespace std::chrono_literals;
static unsigned checks=0;
static void Check(bool ok,const char*text){++checks;if(!ok)throw std::runtime_error(text);}
struct Environment
{
  bool processThread = false, dialogCanceled = false;
  bool ready = true, connected = true, knownHost = true;
  bool quickPing = false, online = true, send = true, interrupted = false;
  int lookups = 0, pings = 0, packets = 0;
  std::function<void()> onLookup, onPing, onSleep;
};
static Environment env;
static constexpr unsigned long INADDR_NONE = ~0UL;
static unsigned long HostToIP(const std::string&)
{
  ++env.lookups;
  if (env.onLookup)
    env.onLookup();
  return env.knownHost ? 1 : INADDR_NONE;
}
static unsigned long ntohl(unsigned long address) { return address; }
static std::string LookupUPnPHost(const std::string&) { return {}; }
#define LOCALIZED(id) std::to_string(id)
struct StringUtils { static std::string Format(const std::string&,const std::string& text){return text;} };
struct CVariant {template<class T> explicit CVariant(const T&){}};
struct CGUIDialogProgress {void SetHeading(const CVariant&){}void SetLine(int,const CVariant&){}void Close(){}void SetPercentage(int){}
 bool IsActive()const{return true;}void Open(){}bool IsCanceled()const{return env.dialogCanceled;}void Progress(){}};
constexpr int WINDOW_DIALOG_PROGRESS=0;
struct FakeWindowManager {template<class T> T* GetWindow(int){static T window;return &window;}};
struct CGUIComponent {FakeWindowManager wm;FakeWindowManager& GetWindowManager(){return wm;}};
struct FakeMessenger {bool IsProcessThread()const{return env.processThread;}};
struct FakeNetwork
{
  bool IsConnected() const { return env.connected; }
  bool HasInterfaceForIP(unsigned long) const { return env.ready; }
  bool PingHost(unsigned long, unsigned, unsigned timeout, unsigned) const
  {
    ++env.pings;
    if (env.onPing)
      env.onPing();
    return timeout == 500 ? env.quickPing : env.online;
  }
  bool WakeOnLan(const char*) const { ++env.packets; return env.send; }
};
struct CJob {virtual ~CJob()=default;virtual bool DoWork()=0;bool ShouldCancel(int,int){return false;}};
struct IJobCallback {virtual ~IJobCallback()=default;virtual void OnJobComplete(unsigned,bool,CJob*)=0;};
struct FakeJobs
{
  unsigned AddJob(CJob* job,IJobCallback* callback)
  {
    callback->OnJobComplete(1,env.online,job);
    delete job;
    return 1;
  }
  void CancelJob(unsigned){}
};
struct CApplicationPlayer {bool IsPlaying()const{return false;}};
struct CGUIDialogKaiToast {enum {Error}; static void QueueNotification(int,const std::string&,const std::string&){}};
struct FakeComponents {template<class T> T* GetComponent()const {static T c;return &c;}};
struct CServiceBroker
{
  static FakeMessenger* GetAppMessenger(){static FakeMessenger m;return &m;}
  static CGUIComponent* GetGUI(){static CGUIComponent gui;return &gui;}
  static FakeNetwork& GetNetwork(){static FakeNetwork net;return net;}
  static FakeJobs* GetJobManager(){static FakeJobs jobs;return &jobs;}
  static FakeComponents& GetAppComponents(){static FakeComponents c;return c;}
};
constexpr int LOGINFO=0,LOGWARNING=1,LOGERROR=2;
struct CLog {template<class...T>static void Log(int,const char*,T&&...){}};
class CWakeOnAccess
{
public:
  struct WakeUpEntry
  {
    std::string friendlyName="nas", host="nas", mac, upnpUuid;
    unsigned ping_port=0, ping_mode=0, wait_online1_sec=1, wait_online2_sec=0, wait_services_sec=0;
  } entry;
  unsigned m_netinit_sec=1,m_netsettle_ms=0;
  int touched=0,entries=0;
  bool IsEnabled()const{return true;}
  bool FindOrTouchHostEntry(const std::string&,bool,WakeUpEntry& out){++entries;out=entry;return true;}
  void TouchHostEntry(const std::string&,bool){++touched;}
  bool WakeUpHost(const WakeUpEntry&,const std::function<bool()>& interrupt = {});
  bool WakeUpHost(const std::string& hostName,const std::string& customMessage,bool upnpMode,const std::function<bool()>& interrupt);
};
namespace XbmcThreads {template<class D=std::chrono::milliseconds> struct EndTime {
 std::chrono::steady_clock::time_point end;explicit EndTime(D d=D::zero()):end(std::chrono::steady_clock::now()+d){}
 void Set(D d){end=std::chrono::steady_clock::now()+d;}
 bool IsTimePast()const{return std::chrono::steady_clock::now()>=end;}
 D GetTimeLeft()const{return std::chrono::duration_cast<D>(std::max(end-std::chrono::steady_clock::now(),std::chrono::steady_clock::duration::zero()));}};}
namespace KODI {namespace TIME {template<class D> void Sleep(D d){if(env.onSleep)env.onSleep();std::this_thread::sleep_for(d);}}}
'''

TESTS = r'''
struct Ready : WaitCondition {bool SuccessWaiting()const override{return true;}};
struct Counted : WaitCondition {mutable int calls=0;bool SuccessWaiting()const override{++calls;return false;}};
using Clock=std::chrono::steady_clock;
int main(){try{
 {std::atomic<bool> gone{false};ProgressDialogHelper dlg("wake",[&]{return gone.load();});
  std::thread later([&]{std::this_thread::sleep_for(100ms);gone=true;});
  const auto start=Clock::now();const auto r=dlg.ShowAndWait(WaitCondition(),5,"waiting");later.join();
  Check(r==ProgressDialogHelper::Canceled&&Clock::now()-start<1s,"a wake wait ends on the read interrupt");}
 {ProgressDialogHelper dlg("wake");const auto start=Clock::now();const auto r=dlg.ShowAndWait(WaitCondition(),1,"waiting");
  Check(r==ProgressDialogHelper::TimedOut&&Clock::now()-start>=900ms,"without an interrupt a wake wait runs to its timeout");}
 {std::atomic<bool> gone{false};ProgressDialogHelper dlg("wake",[&]{return gone.load();});
  Check(dlg.ShowAndWait(Ready(),5,"waiting")==ProgressDialogHelper::Success,"a host that answers ends the wait");}
 {ProgressDialogHelper dlg("wake",[]{return true;});Counted c;
  Check(dlg.ShowAndWait(c,5,"waiting")==ProgressDialogHelper::Canceled&&c.calls==0,"no readiness check, which may block in DNS, after the interrupt");}
 auto interrupt=[] { return env.interrupted; };
 {
   env={};
   CWakeOnAccess wake;
   env.interrupted=true;
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.lookups==0 &&
         env.pings==0 && env.packets==0 && wake.touched==0 && wake.entries==0,
         "a canceled open skips the cooldown lookup and all network work");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.ready=false;
   env.knownHost=false;
   env.onSleep=[] {env.interrupted=true;};
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.lookups==1 &&
         env.pings==0 && env.packets==0 && wake.touched==0,
         "an interrupted readiness wait skips the fallback DNS lookup");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.onLookup=[] {env.interrupted=true;};
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.lookups==1 &&
         env.pings==0 && env.packets==0 && wake.touched==0,
         "an interrupt during network readiness skips the quick ping");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.onLookup=[] {if(env.lookups==2)env.interrupted=true;};
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.lookups==2 &&
         env.pings==0 && env.packets==0 && wake.touched==0,
         "an interrupt during the quick DNS lookup skips the probe and packet");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.onPing=[] {env.interrupted=true;};
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.pings==1 &&
         env.packets==0 && wake.touched==0, "an interrupted quick ping sends no wake packet");
 }
 {
   env={};
   CWakeOnAccess wake;
   wake.m_netinit_sec=0;
   env.onLookup=[] {env.interrupted=true;};
   Check(!wake.WakeUpHost("nas","test",false,interrupt) && env.lookups==1 &&
         env.pings==0 && env.packets==0 && wake.touched==0,
         "an interrupt during the failing fallback lookup starts no cooldown");
 }
 {
   env={};
   CWakeOnAccess wake;
   wake.entry.wait_services_sec=1;
   env.onSleep=[] {env.interrupted=true;};
   auto consumeInterrupt=[] {bool value=env.interrupted;env.interrupted=false;return value;};
   Check(!wake.WakeUpHost("nas","test",false,consumeInterrupt) && env.packets==1 && wake.touched==0,
         "an interrupted service wait starts no cooldown");
 }
 {
   env={};
   CWakeOnAccess wake;
   wake.m_netinit_sec=0;
   env.knownHost=false;
   Check(wake.WakeUpHost("nas","test",false,{}) && env.lookups==3 &&
         env.pings==2 && env.packets==1 && wake.touched==1,
         "a network timeout with unresolved DNS still sends the wake packet");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.send=false;
   Check(!wake.WakeUpHost("nas","test",false,{}) && env.packets==1 && wake.touched==1,
         "a failed wake still starts the cooldown, as before");
 }
 {
   env={};
   CWakeOnAccess wake;
   env.processThread=true;
   env.dialogCanceled=true;
   env.ready=false;
   env.knownHost=false;
   wake.entry.wait_services_sec=1;
   Check(wake.WakeUpHost("nas","test",false,{}) && env.packets==1 && wake.touched==1,
         "GUI cancellation preserves the network fallback and service-wait result");
 }
 std::printf("PASS %u checks; production wake sequence, progress wait and cooldown\n",checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def extract_class(text, name):
    m = re.search(r'^class ' + name + r'\b.*?^\};\n', text, re.S | re.M)
    assert m, name
    return m.group(0)


def source(text, mutation=None):
    body = (extract_class(text, 'WaitCondition') + extract_class(text, 'NestDetect') + 'int NestDetect::m_nest = 0;\n' +
            extract_class(text, 'ProgressDialogHelper') +
            extract_class(text, 'NetworkStartWaiter') + extract_class(text, 'PingResponseWaiter') +
            re.search(r'^bool CWakeOnAccess::WakeUpHost\(const std::string& hostName,\n.*?^\}\n', text, re.S | re.M).group(0) +
            re.search(r'^bool CWakeOnAccess::WakeUpHost\(const WakeUpEntry&.*?^\}\n', text, re.S | re.M).group(0))
    if mutation:
        old, new = mutation
        assert body.count(old) == 1, old
        body = body.replace(old, new, 1)
    return PRELUDE + body + TESTS


MUTATIONS = [('interrupt-ignored', ('      // Before the check, which may block in a host name lookup.\n      if (m_interrupt && m_interrupt())\n        return Canceled;\n', ''),
              'a wake wait ends on the read interrupt'),
             ('check-first', ('      // Before the check, which may block in a host name lookup.\n      if (m_interrupt && m_interrupt())\n        return Canceled;\n\n      if (waitObj.SuccessWaiting())\n        return Success;\n',
                              '      if (waitObj.SuccessWaiting())\n        return Success;\n\n      if (m_interrupt && m_interrupt())\n        return Canceled;\n'),
              'no readiness check, which may block in DNS, after the interrupt'),
             ('cooldown-after-interrupt',
              ('    if (interrupt && checkInterrupt())\n      return false;\n', ''),
              'an interrupted readiness wait skips the fallback DNS lookup'),
             ('uncanceled-quick-ping',
              ('  if (interrupt && interrupt())\n    return false;\n\n  if (alreadyAwake)', '  if (alreadyAwake)'),
              'an interrupt during the quick DNS lookup skips the probe and packet'),
             ('missed-final-poll',
              ('    if (interrupt && checkInterrupt())', '    if (interrupted)'),
              'an interrupt during the failing fallback lookup starts no cooldown'),
             ('lost-interrupt',
              ('      if (!interrupted)\n        interrupted = interrupt();', '      interrupted = interrupt();'),
              'an interrupted service wait starts no cooldown')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', '--out-dir', dest='out', type=Path)
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='wake-on-access-interrupt-'))
    out.mkdir(parents=True, exist_ok=True)
    text = (ROOT / SOURCE).read_text()
    records = []
    for name, mutation, witness in [('current', None, None), *MUTATIONS]:
        cpp = out / (name + '.cpp')
        cpp.write_text(source(text, mutation))
        exe = out / name
        c = subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Wno-unused-parameter', '-Werror', '-pthread', str(cpp), '-o', str(exe)],
                           capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (
            r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n')
        records.append({'name': name, 'exit': r.returncode, 'witness': witness, 'accepted': accepted})
        (out / 'receipt.json').write_text(json.dumps({'results': records}, indent=2) + '\n')
        assert accepted, (name, r.stdout, r.stderr)
        print(r.stdout, end='') if witness is None else print('PASS semantic control ' + name + ': ' + witness)


if __name__ == '__main__':
    main()
