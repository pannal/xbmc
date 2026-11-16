#!/usr/bin/env python3
"""Real production ActorProtocol queues/late replies around an in-flight write.

Runs production pause preservation, sink ReturnBuffers/PAUSE, FlushEngine and
RunStages entry barrier on two OS threads with the complete ActorProtocol header
and implementation. A condition-variable CEvent adapter bounds protocol waits
to 100ms; the worker's transport write is an explicit gated test boundary.
This is not ALSA, full ActiveAE::Process, real AddPackets abort, or receiver proof.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "paused", ROOT / "tools/test-activeae-paused-packets.py")
paused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paused)
AE = ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE"


def harness(engine, sink):
    source = paused.harness(engine, sink).split("int main()")[0]
    header = (ROOT / "xbmc/utils/ActorProtocol.h").read_text()
    protocol = (ROOT / "xbmc/utils/ActorProtocol.cpp").read_text()
    # Keep every production queue/message method, replacing only include wiring.
    header = header.replace('#include "threads/CriticalSection.h"', "").replace("#pragma once", "")
    protocol = re.sub(r'^#include "[^"]+"$', "", protocol, flags=re.M)
    insertion = source.index("struct Port;")
    ending = source.index("struct CActiveAESink {", insertion)
    source = source[:insertion] + EVENT + header + "\n" + protocol + r'''
using Message=Actor::Message;
struct Port:Actor::Protocol {Port():Actor::Protocol("test-data"){}};
''' + source[ending:]
    control = paused.block(source, "struct Control") + ";"
    source = source.replace(control, CONTROL)
    return source + TESTS


EVENT = r'''
#include <atomic>
#include <condition_variable>
#include <functional>
#include <thread>
#include <algorithm>
using CCriticalSection=std::recursive_mutex;
class CEvent {
  std::mutex mutex;
  std::condition_variable changed;
  bool signaled=false;
public:
  void Set(){std::lock_guard<std::mutex> lock(mutex);signaled=true;changed.notify_all();}
  void Reset(){std::lock_guard<std::mutex> lock(mutex);signaled=false;}
  bool Wait(std::chrono::milliseconds timeout){
    std::unique_lock<std::mutex> lock(mutex);
    bool result=changed.wait_for(lock,std::min(timeout,100ms),[&]{return signaled;});
    if(result)signaled=false;
    return result;
  }
};
'''

CONTROL = r'''
struct Control:Actor::Protocol {
  Control(CActiveAESink*):Actor::Protocol("test-control"){}
  std::atomic<int> timeouts{0};
  bool SendOutMessageSync(int signal,Message** reply,std::chrono::seconds timeout){
    bool result=Actor::Protocol::SendOutMessageSync(signal,reply,timeout);
    if(!result)++timeouts;
    return result;
  }
};
'''

TESTS = r'''
struct Worker {
  CActiveAE& engine;
  std::mutex mutex;
  std::condition_variable changed;
  bool writing=false,releaseWrite=false,ending=false;
  std::thread thread;
  int pauseReplies=0,flushReplies=0,writes=0;
  std::vector<int> controls;
  explicit Worker(CActiveAE& value):engine(value),thread([this]{run();}){}
  void run(){
    Message* inflight=nullptr;
    assert(engine.sink.m_dataPort.ReceiveOutMessage(&inflight));
    {
      std::unique_lock<std::mutex> lock(mutex);writing=true;changed.notify_all();
      assert(changed.wait_for(lock,10s,[&]{return releaseWrite;}));
    }
    // The sample left the queue before PAUSE, so it was already submitted.
    ++writes;
    inflight->Reply(CSinkDataProtocol::RETURNSAMPLE,inflight->data,sizeof(CSampleBuffer*));
    inflight->Release();
    for(;;){
      Message* msg=nullptr;
      if(engine.m_sink.m_controlPort.ReceiveOutMessage(&msg)){
        controls.push_back(msg->signal);
        if(msg->signal==CSinkControlProtocol::PAUSE){
          engine.sink.Pause(msg);++pauseReplies;
        }else if(msg->signal==CSinkControlProtocol::FLUSH){
          engine.sink.ReturnBuffers();++flushReplies;
          msg->Reply(CSinkControlProtocol::ACC);
        }else {
          assert(msg->signal==CSinkControlProtocol::STREAMING||
                 msg->signal==CSinkControlProtocol::PAUSED);
        }
        msg->Release();
      }else {
        std::unique_lock<std::mutex> lock(mutex);
        if(ending)break;
        changed.wait_for(lock,1ms);
      }
    }
  }
  void WaitForWrite(){
    std::unique_lock<std::mutex> lock(mutex);
    assert(changed.wait_for(lock,10s,[&]{return writing;}));
  }
  void ReleaseWrite(){
    std::lock_guard<std::mutex> lock(mutex);releaseWrite=true;changed.notify_all();
  }
  void Join(){
    {std::lock_guard<std::mutex> lock(mutex);ending=true;changed.notify_all();}
    thread.join();
  }
};
int main(){
  for(bool cancel:{false,true})for(int iteration=0;iteration<3;++iteration){
    CActiveAE engine;CActiveAEStream stream;
    CSampleBuffer inflight{1},pending{2};
    auto* a=&inflight;auto* b=&pending;
    engine.sink.m_dataPort.SendOutMessage(CSinkDataProtocol::SAMPLE,&a,sizeof(a));
    engine.sink.m_dataPort.SendOutMessage(CSinkDataProtocol::SAMPLE,&b,sizeof(b));
    engine.m_stats.m_bufferedSamples=2;
    engine.m_stats.m_sinkDelay.delay=.125;
    Worker worker(engine);worker.WaitForWrite();
    engine.Pause(&stream); // Both PAUSE and fallback FLUSH time out behind the write.
    assert(engine.m_sink.m_controlPort.timeouts==2);
    assert(engine.m_sinkFlushPending&&engine.m_extError);
    assert(engine.m_stats.resets==0&&engine.m_stats.m_bufferedSamples==2);
    assert(inflight.returns==0&&pending.returns==0);
    assert(!engine.RunStages()&&engine.submitted==0);
    // Cancel queued timed-out controls while the worker still owns the in-flight
    // write. Protocol::Purge must not invent an ACK or release its sample.
    if(cancel)engine.m_sink.m_controlPort.Purge();
    assert(engine.m_sinkFlushPending&&engine.m_stats.resets==0);
    worker.ReleaseWrite();
    assert(engine.FlushEngine()); // Genuine later ACK after the write and returns.
    assert(!engine.m_sinkFlushPending&&engine.m_stats.resets==1);
    assert(engine.m_stats.m_bufferedSamples==0);
    assert(inflight.returns==1&&pending.returns==1);
    assert(stream.buffers.m_outputSamples.empty()); // no replay after failed pause
    assert(engine.RunStages()&&engine.submitted==1);
    worker.Join();
    assert(worker.writes==1);
    assert(worker.pauseReplies==(cancel?0:1));
    assert(worker.flushReplies==(cancel?1:2));
    Message* ghost=nullptr;
    assert(!engine.m_sink.m_controlPort.ReceiveInMessage(&ghost));
    assert(!engine.m_sink.m_controlPort.ReceiveOutMessage(&ghost));
  }
  std::cout<<"PASS real ActorProtocol queues/two threads: in-flight write, PAUSE and FLUSH timeouts, late ACK, canceled pending controls, exactly-once returns and barrier accounting (6 schedules)\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    engine, sink = (AE / "ActiveAE.cpp").read_text(), (AE / "ActiveAESink.cpp").read_text()
    source = harness(engine, sink)
    variants = [("production", source)]
    if args.negative_controls:
        for label, old, new in (
            ("write-before-barrier", "if (m_sinkFlushPending)\n    return false;",
             "if (false && m_sinkFlushPending)\n    return false;"),
            ("reset-before-ack", "m_extError = true;\n    return false;",
             "m_stats.Reset(m_sinkFormat.m_sampleRate,m_mode==MODE_PCM);m_extError = true;\n    return false;"),
            ("late-sample-return-leak", "buffer->Return();", "(void)buffer;"),
        ):
            assert old in source, label
            variants.append((label, source.replace(old, new)))
    with tempfile.TemporaryDirectory(prefix="ae-threaded-barrier-") as temp:
        for label, text in variants:
            cpp, exe = Path(temp) / (label + ".cpp"), Path(temp) / label
            cpp.write_text(text)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-pthread", "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            if label == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            else:
                assert result.returncode and "Assertion" in result.stderr, (label, result.stderr)
                print("REJECTED:", label)


if __name__ == "__main__":
    main()
