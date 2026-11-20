#!/usr/bin/env python3
"""Execute the actual wake-on-access wait loop with a read interrupt.

WaitCondition and ProgressDialogHelper are extracted from WakeOnAccess.cpp and
run without a dialog (as on a job thread): a wait ends early when the interrupt
fires, waits to its timeout without one, and ends at once when the condition
holds. CFileFactory::CreateLoader, CFile::Open and CFile::Stat passing the
interrupt to WakeUpHost are not covered here.
"""
import argparse
import importlib.util
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
struct CVariant {template<class T> explicit CVariant(const T&){}};
struct CGUIDialogProgress {void SetHeading(const CVariant&){}void SetLine(int,const CVariant&){}void Close(){}void SetPercentage(int){}
 bool IsActive()const{return true;}void Open(){}bool IsCanceled()const{return false;}void Progress(){}};
constexpr int WINDOW_DIALOG_PROGRESS=0;
struct FakeWindowManager {template<class T> T* GetWindow(int){return nullptr;}};
struct CGUIComponent {FakeWindowManager wm;FakeWindowManager& GetWindowManager(){return wm;}};
struct FakeMessenger {bool IsProcessThread()const{return false;}};
struct CServiceBroker {static FakeMessenger* GetAppMessenger(){static FakeMessenger m;return &m;}static CGUIComponent* GetGUI(){return nullptr;}};
constexpr int LOGINFO=0,LOGWARNING=1;
struct CLog {template<class...T>static void Log(int,const char*,T&&...){}};
// WakeUpHost(hostName, ...) with fixture entries, waits and cooldown.
class CWakeOnAccess {
public:
 struct WakeUpEntry {std::string friendlyName="nas";};
 bool enabled=true,wakeResult=false;int touched=0,woken=0;std::function<void()> during;
 bool IsEnabled()const{return enabled;}
 bool FindOrTouchHostEntry(const std::string&,bool,WakeUpEntry&){return true;}
 void TouchHostEntry(const std::string&,bool){++touched;}
 bool WakeUpHost(const WakeUpEntry&,const std::function<bool()>&){++woken;if(during)during();return wakeResult;}
 bool WakeUpHost(const std::string& hostName,const std::string& customMessage,bool upnpMode,const std::function<bool()>& interrupt);
};
namespace XbmcThreads {template<class D=std::chrono::milliseconds> struct EndTime {
 std::chrono::steady_clock::time_point end;explicit EndTime(D d):end(std::chrono::steady_clock::now()+d){}
 bool IsTimePast()const{return std::chrono::steady_clock::now()>=end;}
 D GetTimeLeft()const{return std::chrono::duration_cast<D>(std::max(end-std::chrono::steady_clock::now(),std::chrono::steady_clock::duration::zero()));}};}
namespace KODI {namespace TIME {template<class D> void Sleep(D d){std::this_thread::sleep_for(d);}}}
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
 {CWakeOnAccess wake;std::atomic<bool> gone{false};wake.during=[&]{gone=true;};
  Check(!wake.WakeUpHost("nas","test",false,[&]{return gone.load();})&&wake.woken==1&&wake.touched==0,"an interrupted wake starts no cooldown");}
 {CWakeOnAccess wake;Check(!wake.WakeUpHost("nas","test",false,{})&&wake.touched==1,"a failed wake still starts the cooldown, as before");}
 {CWakeOnAccess wake;wake.wakeResult=true;Check(wake.WakeUpHost("nas","test",false,[]{return false;})&&wake.touched==1,"a wake still starts the cooldown");}
 std::printf("PASS %u checks; actual ProgressDialogHelper wait loop (no dialog) and WakeUpHost cooldown\n",checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def extract_class(text, name):
    m = re.search(r'^class ' + name + r'\b.*?^\};\n', text, re.S | re.M)
    assert m, name
    return m.group(0)


def source(text, mutation=None):
    body = (extract_class(text, 'WaitCondition') + extract_class(text, 'NestDetect') + 'int NestDetect::m_nest = 0;\n' +
            extract_class(text, 'ProgressDialogHelper') +
            re.search(r'^bool CWakeOnAccess::WakeUpHost\(const std::string& hostName,\n.*?^\}\n', text, re.S | re.M).group(0))
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
             ('cooldown-after-interrupt', ('    if (!ret && interrupt && interrupt())\n      return false;\n', ''),
              'an interrupted wake starts no cooldown')]


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
        c = subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(exe)],
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
