#!/usr/bin/env python3
"""Bounded corrected-path checks of ThreadMessage and extracted messenger methods.

Uses the production header and enqueue/dispatch/cleanup code. Host adapters only
supply events, graphics/services, logging and non-callback receivers. Never invokes
an invalid legacy payload or a faulty baseline. No device or full-link claim.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <cassert>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <queue>
#include <stdexcept>
#include <thread>
#include <iostream>
#include "messaging/ThreadMessage.h"
class CEvent { public: void Set() {} void Wait() {} explicit CEvent(bool=true) {} };
using CCriticalSection = std::recursive_mutex;
struct CThread { static auto GetCurrentThreadId() { return std::this_thread::get_id(); } };
struct CWinSystemBase { CCriticalSection mutex; auto& GetGfxContext() { return mutex; } };
struct CServiceBroker { static CWinSystemBase* GetWinSystem() { return nullptr; } };
struct CSingleExit { explicit CSingleExit(CCriticalSection&) {} };
constexpr int LOGERROR=0, TMSG_CALLBACK=7, TMSG_GUI_MESSAGE=8, TMSG_MASK_MESSAGE=65535;
struct CLog { template<class... T> static void Log(T&&...) {} template<class... T> static void LogF(T&&...) {} };
namespace KODI { namespace MESSAGING {
struct ThreadMessageCallback { void (*callback)(void*); void* userptr; };
struct IMessageTarget { virtual void OnApplicationMessage(ThreadMessage*) = 0; };
class CApplicationMessenger {
public:
 void PostCallback(std::function<void()>);
 void Cleanup(); void ProcessMessages(); void ProcessWindowMessages();
 int SendMsg(ThreadMessage&&, bool); void ProcessMessage(ThreadMessage*);
 std::queue<ThreadMessage*> m_vecMessages, m_vecWindowMessages;
 std::map<int,IMessageTarget*> m_mapTargets;
 CCriticalSection m_critSection;
 std::thread::id m_guiThreadId, m_processThreadId;
 bool m_bStop=false;
 ~CApplicationMessenger() { Cleanup(); }
};
'''
TEST = r'''
} }
using namespace KODI::MESSAGING;
int main() {
 int calls=0;
 auto owner=std::make_shared<int>(17); std::weak_ptr<int> weak=owner;
 ThreadMessage original(TMSG_CALLBACK); original.callback=[owner,&calls] { assert(*owner==17); ++calls; };
 ThreadMessage copied(original), assigned; assigned=original;
 ThreadMessage moved(std::move(copied)), moveAssigned; moveAssigned=std::move(assigned);
 auto copyAssign=[](ThreadMessage& a,const ThreadMessage& b) { a=b; };
 auto moveAssign=[](ThreadMessage& a,ThreadMessage& b) { a=std::move(b); };
 copyAssign(original,original); moveAssign(moveAssigned,moveAssigned);
 assert(original.callback && moved.callback && moveAssigned.callback);
 moved.callback(); moveAssigned.callback(); original.callback(); assert(calls==3);
 original.callback={}; moved.callback={}; moveAssigned.callback={}; owner.reset(); assert(weak.expired());
 CApplicationMessenger messenger;
 owner=std::make_shared<int>(23); weak=owner;
 messenger.PostCallback([owner,&calls] { assert(*owner==23); ++calls; });
 owner.reset(); assert(!weak.expired() && calls==3);
 messenger.ProcessMessages(); assert(weak.expired() && calls==4);
 owner=std::make_shared<int>(31); weak=owner;
 messenger.PostCallback([owner,&calls] { ++calls; }); owner.reset();
 assert(!weak.expired()); messenger.Cleanup(); assert(weak.expired() && calls==4);
 ThreadMessageCallback legacy{[](void* raw) { ++*static_cast<int*>(raw); },&calls};
 messenger.SendMsg(ThreadMessage(TMSG_CALLBACK,-1,-1,&legacy),false);
 messenger.ProcessMessages(); assert(calls==5);
 messenger.PostCallback([] { throw std::runtime_error("ordinary callback error"); });
 messenger.PostCallback([&calls] { ++calls; }); messenger.ProcessMessages(); assert(calls==6);
 messenger.m_bStop=true; owner=std::make_shared<int>(41); weak=owner;
 messenger.PostCallback([owner,&calls] { ++calls; }); owner.reset(); assert(weak.expired() && calls==6);
 messenger.m_bStop=false; owner=std::make_shared<int>(43); weak=owner;
 ThreadMessage window(TMSG_GUI_MESSAGE); window.callback=[owner] {}; owner.reset();
 messenger.SendMsg(std::move(window),false); assert(!weak.expired());
 messenger.Cleanup(); assert(weak.expired());
 std::cout << "PASS callback transfers/self-assignment, actual queue dispatch, legacy dispatch, exception continuation, cleanup and rejection ownership\n";
}
'''


def run(out):
    source = (ROOT / 'xbmc/messaging/ApplicationMessenger.cpp').read_text()
    methods = ['void CApplicationMessenger::Cleanup()',
               'int CApplicationMessenger::SendMsg(ThreadMessage&&',
               'void CApplicationMessenger::PostCallback(',
               'void CApplicationMessenger::ProcessMessages()',
               'void CApplicationMessenger::ProcessMessage(ThreadMessage *',
               'void CApplicationMessenger::ProcessWindowMessages()']
    cpp = out / 'callback.cpp'
    cpp.write_text(PRELUDE + '\n'.join(block(source, name) for name in methods) + TEST)
    argv = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
            '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
            '-I', str(ROOT / 'xbmc'), str(cpp), '-o', str(out / 'callback')]
    (out / 'argv.json').write_text(json.dumps(argv, indent=2) + '\n')
    compiled = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    (out / 'compile.log').write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(out / 'callback')], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                                 'UBSAN_OPTIONS': 'halt_on_error=1'})
    (out / 'run.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        run(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='messenger-callback-') as temporary:
            run(Path(temporary))
