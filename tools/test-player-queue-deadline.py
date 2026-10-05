#!/usr/bin/env python3
"""Exercise production queue deadlines with real Kodi events and producer threads.

Reuse the lifecycle fixture's message/metadata stubs and complete Put/Get/Flush
methods, replacing its threading stubs with real CEvent/CCriticalSection. Test
filtered packet wakeups, lifecycle rechecking, priority/FIFO and abort delivery.
--baseline REV runs the deadline regression against the queue source at REV.
No demuxer, decoder, player clock, display hardware or addon is executed.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lifecycle', ROOT / 'tools/test-player-lifecycle.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def harness(baseline=None):
    source = fixture.harness(ROOT).split('struct CVideoPlayerAudio {')[0]
    source = source.replace('using CCriticalSection = std::mutex;',
                            '#include "threads/Event.h"\n#include <thread>\n#include <cstdio>')
    source = source.replace(
        'struct Event { void Set() {} void Reset() {} bool Wait(std::chrono::milliseconds) { return false; } };', '')
    source = source.replace('CCriticalSection m_section; Event m_hEvent;',
                            'CCriticalSection m_section; CEvent m_hEvent{true};')
    source = source.replace('bool m_bInitialized=true, m_bAbortRequest=false, m_drain=false;',
                            'bool m_bInitialized=true, m_drain=false; std::atomic<bool> m_bAbortRequest{false};')
    if baseline:
        queue = subprocess.check_output(
            ['git', 'show', f'{baseline}:xbmc/cores/VideoPlayer/DVDMessageQueue.cpp'],
            cwd=ROOT, text=True)
        current = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessageQueue.cpp').read_text()
        source = source.replace(fixture.block(current, 'MsgQueueReturnCode CDVDMessageQueue::Get('),
                                fixture.block(queue, 'MsgQueueReturnCode CDVDMessageQueue::Get('))
    return source + TESTS


TESTS = r'''
static auto message(CDVDMsg::Message type) { return std::make_shared<CDVDMsg>(type); }
static int receive(CDVDMessageQueue& q, int mode=0, int priority=0) {
  std::shared_ptr<CDVDMsg> msg;
  const auto result=q.Get(msg,0ms,priority,mode);
  return result==MSGQ_OK ? 100+msg->type : result;
}
// The producer stays active far longer than the read budget. With the old Get,
// every excluded packet restarts the budget and completion follows the producer.
static void deadline(int lifecycle, int priority) {
  CDVDMessageQueue q;
  std::atomic<bool> running{true};
  std::atomic<int> produced{0};
  std::thread producer([&] {
    const auto end=std::chrono::steady_clock::now()+1200ms;
    do {
      q.Put(std::make_shared<CDVDMsgDemuxerPacket>(produced++));
      std::this_thread::sleep_for(1ms);
    } while (std::chrono::steady_clock::now()<end);
    running=false;
  });
  while(produced==0) std::this_thread::yield();
  std::shared_ptr<CDVDMsg> msg;
  const auto start=std::chrono::steady_clock::now();
  const auto result=q.Get(msg,40ms,priority,lifecycle);
  const auto elapsed=std::chrono::steady_clock::now()-start;
  const bool producerActive=running;
  producer.join();
  std::fprintf(stderr,"filtered Get mode=%d: %.1fms, producer_active=%d\n",
               lifecycle,std::chrono::duration<double,std::milli>(elapsed).count(),producerActive);
  assert(result==MSGQ_TIMEOUT && !msg);
  assert(elapsed>=30ms && elapsed<400ms && producerActive);
  // A timed-out filtered read must not consume input or change its priority.
  assert(priority==(lifecycle ? 0 : 1));
  assert(q.m_iDataSize==produced*7);
  for(int id=0;id<produced;++id) {
    priority=0;
    assert(q.Get(msg,0ms,priority)==MSGQ_OK);
    assert(std::static_pointer_cast<CDVDMsgDemuxerPacket>(msg)->id==id);
  }
  assert(q.m_iDataSize==0 && receive(q)==MSGQ_TIMEOUT);
}
static void startup_recheck() {
  CDVDMessageQueue q;
  std::atomic<bool> blocked{true}, producerActive{true};
  std::thread producer([&] {
    const auto start=std::chrono::steady_clock::now();
    int id=0;
    do {
      q.Put(std::make_shared<CDVDMsgDemuxerPacket>(id++));
      if(std::chrono::steady_clock::now()-start>=75ms) blocked=false;
      std::this_thread::sleep_for(1ms);
    } while(std::chrono::steady_clock::now()-start<1200ms);
    producerActive=false;
  });
  const auto start=std::chrono::steady_clock::now();
  int retries=0; bool resumedDuringProduction=false; std::shared_ptr<CDVDMsg> msg;
  // Model the caller's lifecycle observation once per Get, exactly as the video
  // loop does. Release of the display fence does not itself enqueue a message.
  do {
    const bool lifecyclePending=blocked;
    int priority=0;
    const auto result=q.Get(msg,10ms,priority,lifecyclePending);
    if(result==MSGQ_OK) {
      assert(!lifecyclePending);
      resumedDuringProduction=producerActive;
      break;
    }
    assert(result==MSGQ_TIMEOUT);
    ++retries;
  } while(std::chrono::steady_clock::now()-start<2s);
  const auto elapsed=std::chrono::steady_clock::now()-start;
  producer.join();
  assert(msg && resumedDuringProduction && elapsed<400ms && retries>=2);
  assert(std::static_pointer_cast<CDVDMsgDemuxerPacket>(msg)->id==0);
}
static void allowed_and_abort() {
  for(int mode : {0,1,2}) {
    CDVDMessageQueue q;
    q.Put(std::make_shared<CDVDMsgDemuxerPacket>(0));
    std::thread producer([&] {
      std::this_thread::sleep_for(20ms);
      q.Put(message(mode==2 ? CDVDMsg::PLAYER_STARTED : CDVDMsg::GENERAL_PAUSE),1);
    });
    std::shared_ptr<CDVDMsg> msg; int priority=mode==0 ? 1 : 0;
    const auto result=q.Get(msg,1s,priority,mode);
    producer.join();
    assert(result==MSGQ_OK && priority==1);
    assert(msg->IsType(mode==2 ? CDVDMsg::PLAYER_STARTED : CDVDMsg::GENERAL_PAUSE));
    assert(q.m_iDataSize==7);
    q.Put(message(mode==2 ? CDVDMsg::PLAYER_ABORT : CDVDMsg::GENERAL_RESYNC));
    if(mode) assert(receive(q,mode)==100+(mode==2 ? CDVDMsg::PLAYER_ABORT : CDVDMsg::GENERAL_RESYNC));
    assert(receive(q)==100+CDVDMsg::DEMUXER_PACKET);
  }
  CDVDMessageQueue q;
  std::thread aborter([&] {
    std::this_thread::sleep_for(20ms);
    std::unique_lock<CCriticalSection> lock(q.m_section);
    q.m_bAbortRequest=true; q.m_hEvent.Set(); // Production Abort's exact body.
  });
  std::shared_ptr<CDVDMsg> msg; int priority=0;
  const auto start=std::chrono::steady_clock::now();
  const auto result=q.Get(msg,1s,priority,1);
  const auto elapsed=std::chrono::steady_clock::now()-start;
  aborter.join();
  assert(result==MSGQ_ABORT && !msg && elapsed<400ms);
}
int main() {
  deadline(1,0); deadline(2,0); deadline(0,1);
  startup_recheck(); allowed_and_abort();
  std::puts("PASS: bounded filtered reads, startup recheck, retained FIFO, allowed controls and abort");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='player-queue-deadline-') as temporary:
        out = Path(temporary)
        cpp = out / 'test.cpp'
        cpp.write_text(harness(args.baseline))
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                        '-DTARGET_POSIX', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-no-pie', '-I', str(ROOT / 'xbmc'), str(cpp),
                        str(ROOT / 'xbmc/threads/Event.cpp'),
                        str(ROOT / 'xbmc/platform/posix/threads/RecursiveMutex.cpp'),
                        '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=20)


if __name__ == '__main__':
    main()
