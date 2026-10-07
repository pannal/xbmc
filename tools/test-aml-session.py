#!/usr/bin/env python3
"""Exercise actual AML session admission and extracted buffer consumption/Return.

The session header and CAMLVideoBuffer implementation are production code. Codec
ioctls, geometry and polling are recording stubs; decoder reset/open are modeled
by the real session mutation protocol, not the actual decoder entry points.
Deterministic host threads exercise permit and claim races under ASan/UBSan.
Counting/race cases move main-acquired permits to fake consumers; this tests token
lifetime, not production authorization to execute presentation on another thread.
Separate ownership cases enforce that normal admission is acquired by its owner.
This does not validate real driver timing, player dispatch or device playback.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.h').read_text()
    source = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    buffer = function(header, 'class CAMLVideoBuffer :') + ';'
    methods = '\n'.join(function(source, signature) for signature in [
        'void CAMLVideoBuffer::Set(', 'CAMLSession::Permit CAMLVideoBuffer::AcquirePresentation()',
        'void CAMLVideoBuffer::Commit(', 'bool CAMLVideoBuffer::Submit(',
        'void CAMLVideoBuffer::ApplyGeometry(', 'bool CAMLVideoBuffer::Poll(',
        'bool CAMLVideoBuffer::Drop()', 'void CAMLVideoBufferPool::Return('])
    return PRELUDE + buffer + POOL + methods + TESTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    if args.negative_controls:
        negative_controls()
        return
    with tempfile.TemporaryDirectory(prefix='aml-session-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(harness())
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('AML session: PASS (production admission, buffer consumption and pool Return; '
          'ASan/UBSan; recording codec, modeled lifecycle mutation)')


PRELUDE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <atomic>
#include <cassert>
#include <functional>
#include <string>
#include <set>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
struct CRect {};
struct CVideoBuffer { explicit CVideoBuffer(int) {} };
struct CCriticalSection {
  std::mutex mutex;
  bool held{false};
  void lock() {mutex.lock(); held=true;}
  void unlock() {held=false; mutex.unlock();}
};
struct Latch {
  std::mutex mutex;
  std::condition_variable ready;
  bool entered{false}, released{false};
  void Block() {
    std::unique_lock<std::mutex> lock(mutex);
    entered=true; ready.notify_all();
    ready.wait(lock,[&] {return released;});
  }
  void Entered() {
    std::unique_lock<std::mutex> lock(mutex);
    assert(ready.wait_for(lock,5s,[&] {return entered;}));
  }
  void Release() {
    std::lock_guard<std::mutex> lock(mutex);
    released=true; ready.notify_all();
  }
};
class CAMLCodec {
public:
  CAMLSession session;
  uint64_t generation{1};
  std::mutex traceMutex;
  std::vector<std::string> trace;
  std::function<void()> onRelease, onGeometry, onPoll;
  int qbufResult{0};
  explicit CAMLCodec(std::thread::id owner=std::this_thread::get_id()) : session(owner) {
    const auto request=session.Fence();
    assert(session.BeginMutation(request));
    assert(session.Complete(request,true));
  }
  uint64_t GetOperationEpoch() const {return session.Epoch();}
  bool IsOperationInvalidated(uint64_t epoch) const {return session.IsInvalidated(epoch);}
  CAMLSession::Permit AcquirePresentation(uint64_t epoch,bool retirement=false) {
    return session.Acquire(epoch,retirement);
  }
  bool IsPresentationPermit(const CAMLSession::Permit& permit,uint64_t epoch) const {
    return session.Matches(permit,epoch);
  }
  void Record(const char* value) {
    std::lock_guard<std::mutex> lock(traceMutex); trace.emplace_back(value);
  }
  int ReleaseFrame(uint32_t,uint64_t gen,const CAMLSession::Permit& permit,bool drop=false) {
    assert(gen==generation && session.Matches(permit,GetOperationEpoch()));
    if(onRelease) onRelease();
    Record(drop ? "drop" : "release"); return qbufResult;
  }
  void SetVideoRect(const CRect&,const CRect&,uint64_t gen,const CAMLSession::Permit& permit) {
    assert(gen==generation && session.Matches(permit,GetOperationEpoch()));
    Record("geometry"); if(onGeometry) onGeometry();
  }
  int PollFrame(const CAMLSession::Permit& permit) {
    assert(session.Matches(permit,GetOperationEpoch()));
    Record("poll"); if(onPoll) onPoll(); return 1;
  }
};
'''
POOL = r'''
class CAMLVideoBufferPool:public std::enable_shared_from_this<CAMLVideoBufferPool> {
public:
  static inline std::atomic<size_t> s_outstandingBuffers{0};
  CCriticalSection m_criticalSection;
  std::vector<CAMLVideoBuffer*> m_videoBuffers;
  std::vector<int> m_freeBuffers;
  std::set<int> m_pendingReturns;
  // These original normal-return tests must never defer. The dedicated display
  // fixture executes real QueueReturns/ProcessReturns with retained real pools.
  static void QueueReturns(const std::shared_ptr<CAMLVideoBufferPool>&){assert(false);}
  void Return(int);
};
'''
TESTS = r'''
void Set(CAMLVideoBuffer& buffer,const std::shared_ptr<CAMLCodec>& codec,int pts=100) {
  buffer.Set(codec,pts,40,7,codec->generation);
}
void PresentationOwner() {
  auto codec=std::make_shared<CAMLCodec>();
  const auto epoch=codec->GetOperationEpoch();
  assert(codec->AcquirePresentation(epoch));
  std::thread wrong([&] {
    assert(!codec->AcquirePresentation(epoch));
    assert(codec->AcquirePresentation(epoch,true));
  });
  wrong.join();
  auto request=codec->session.Fence();
  assert(codec->session.BeginMutation(request));
  assert(codec->session.Complete(request,true));
  std::thread late([&] {
    assert(!codec->AcquirePresentation(epoch));
    assert(!codec->AcquirePresentation(codec->GetOperationEpoch()));
    assert(!codec->AcquirePresentation(epoch,true));
    assert(codec->AcquirePresentation(codec->GetOperationEpoch(),true));
  });
  late.join();

  // Codec creation and presentation ownership can be different threads.
  Latch start;
  std::shared_ptr<CAMLCodec> assigned;
  std::thread owner([&] {
    start.Block();
    assert(assigned->AcquirePresentation(assigned->GetOperationEpoch()));
  });
  start.Entered();
  assigned=std::make_shared<CAMLCodec>(owner.get_id());
  assert(!assigned->AcquirePresentation(assigned->GetOperationEpoch()));
  assert(assigned->AcquirePresentation(assigned->GetOperationEpoch(),true));
  start.Release(); owner.join();
}
void RequestIdentity() {
  CAMLSession first,second;
  auto a=first.Fence(),b=second.Fence();
  assert(!second.Wait(a,0ms));
  assert(!second.BeginMutation(a));
  assert(second.BeginMutation(b));
  assert(!second.Complete(a,true));
  assert(second.Complete(b,true));
  auto old=second.Fence(),fresh=second.Fence();
  assert(!second.BeginMutation(old));
  assert(second.BeginMutation(fresh));
  assert(!second.Complete(old,true));
  assert(second.Complete(fresh,true));
  assert(!second.Complete(fresh,true));
}
void EpochAndTimeout() {
  auto codec=std::make_shared<CAMLCodec>();
  const auto epoch=codec->GetOperationEpoch(),generation=codec->generation;
  CAMLSession::Request request;
  {
    auto permit=codec->AcquirePresentation(epoch);
    assert(permit);
    auto moved=std::move(permit);
    assert(!permit && moved);
    request=codec->session.Fence();
    assert(!codec->AcquirePresentation(epoch));
    assert(!codec->session.Wait(request,0ms));
    assert(!codec->session.BeginMutation(request));
    // Retirement still works after normal admission is fenced.
    auto retirement=codec->AcquirePresentation(epoch,true);
    assert(retirement);
    assert(!codec->session.BeginMutation(request));
  }
  assert(codec->session.Wait(request,0ms));
  // Timing out never reopened admission, even after the blocker later exits.
  assert(!codec->AcquirePresentation(epoch));
  assert(codec->session.BeginMutation(request));
  assert(!codec->AcquirePresentation(epoch,true));
  assert(codec->session.Complete(request,true));
  assert(codec->generation==generation); // Reset does not change open generation.
  assert(codec->GetOperationEpoch()==epoch+1);
  assert(!codec->AcquirePresentation(epoch,true));
  assert(codec->AcquirePresentation(epoch+1));
}
void BufferPolicy() {
  auto codec=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer buffer(0); Set(buffer,codec);
  int previous=100;
  {
    auto permit=buffer.AcquirePresentation();
    buffer.Commit(permit,{}, {},previous);
    buffer.Poll(permit);
  }
  assert((codec->trace==std::vector<std::string>{"poll"}));
  buffer.Drop(); buffer.Drop();
  assert((codec->trace==std::vector<std::string>{"poll","drop"}));
  Set(buffer,codec,200); codec->trace.clear(); previous=100;
  codec->qbufResult=-1;
  CAMLSession::Request midway;
  codec->onGeometry=[&] {
    assert(previous==100);
    midway=codec->session.Fence();
    assert(!codec->session.BeginMutation(midway));
  };
  codec->onPoll=[&] {assert(previous==200);};
  {
    auto permit=buffer.AcquirePresentation();
    buffer.Commit(permit,{}, {},previous);
    buffer.Poll(permit);
  }
  buffer.Drop();
  assert((codec->trace==std::vector<std::string>{"release","geometry","poll"}));
}
void PermitBeforeClaim() {
  auto codec=std::make_shared<CAMLCodec>();
  auto foreign=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer buffer(0); Set(buffer,codec);
  int previous=-1;
  CAMLSession::Permit absent;
  buffer.Commit(absent,{}, {},previous);
  {
    auto permit=foreign->AcquirePresentation(foreign->GetOperationEpoch());
    buffer.Commit(permit,{}, {},previous);
  }
  {
    auto retirement=codec->AcquirePresentation(codec->GetOperationEpoch(),true);
    buffer.Commit(retirement,{}, {},previous);
  }
  assert(codec->trace.empty() && previous==-1);
  buffer.Drop();
  assert((codec->trace==std::vector<std::string>{"drop"}));
  Set(buffer,codec); codec->trace.clear();
  auto request=codec->session.Fence();
  assert(!buffer.AcquirePresentation());
  buffer.Drop(); // Retirement while fenced must perform the required QBUF.
  assert((codec->trace==std::vector<std::string>{"drop"}));
  assert(codec->session.BeginMutation(request));
  assert(codec->session.Complete(request,true));
  buffer.Drop();
  assert(codec->trace.size()==1);
}
void RetireOldEpoch() {
  auto codec=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer old(0); Set(old,codec);
  auto request=codec->session.Fence();
  assert(codec->session.BeginMutation(request));
  assert(codec->session.Complete(request,true));
  old.Drop(); assert(codec->trace.empty());
  Set(old,codec); old.Drop();
  assert((codec->trace==std::vector<std::string>{"drop"}));
  auto close=codec->session.Fence();
  assert(codec->session.BeginMutation(close));
  assert(codec->session.Complete(close,false));
  assert(!codec->AcquirePresentation(codec->GetOperationEpoch(),true));
}
void Race(bool commitWins) {
  auto codec=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer buffer(0); Set(buffer,codec);
  int previous=-1;
  Latch device;
  codec->onRelease=[&] {device.Block();};
  // Main obtains normal admission; moving it here models only in-flight counts.
  auto firstPermit=commitWins ? buffer.AcquirePresentation() : CAMLSession::Permit{};
  auto winner=std::thread([&,permit=std::move(firstPermit)] {
    if(commitWins) {
      buffer.Commit(permit,{}, {},previous); buffer.Poll(permit);
    } else buffer.Drop();
  });
  device.Entered();
  // The losing claimant cannot produce a second device return.
  auto secondPermit=commitWins ? CAMLSession::Permit{} : buffer.AcquirePresentation();
  auto loser=std::thread([&,permit=std::move(secondPermit)] {
    if(commitWins) buffer.Drop();
    else {
      buffer.Commit(permit,{}, {},previous);
    }
  });
  loser.join();
  auto request=codec->session.Fence();
  assert(!codec->session.Wait(request,0ms));
  assert(!codec->session.BeginMutation(request));
  device.Release(); winner.join();
  assert(codec->session.Wait(request,0ms));
  assert(codec->session.BeginMutation(request));
  assert(codec->session.Complete(request,true));
  assert(previous==(commitWins ? 100 : -1));
  assert(codec->trace==(commitWins ? std::vector<std::string>{"release","geometry","poll"}
                                 : std::vector<std::string>{"drop"}));
}
void PollPinsMutation() {
  auto codec=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer buffer(0); Set(buffer,codec);
  Latch poll; int previous=-1;
  codec->onPoll=[&] {poll.Block();};
  auto admitted=buffer.AcquirePresentation();
  std::thread presenter([&,permit=std::move(admitted)] {
    buffer.Commit(permit,{}, {},previous); buffer.Poll(permit);
  });
  poll.Entered();
  auto request=codec->session.Fence();
  assert(!codec->session.BeginMutation(request));
  assert(!codec->session.Wait(request,0ms));
  poll.Release(); presenter.join();
  assert(codec->session.Wait(request,0ms));
  assert(codec->session.BeginMutation(request));
  assert(codec->session.Complete(request,true));
}
void PoolUnlock() {
  auto codec=std::make_shared<CAMLCodec>();
  CAMLVideoBuffer buffer(0); Set(buffer,codec);
  CAMLVideoBufferPool pool; pool.m_videoBuffers.push_back(&buffer);
  codec->onRelease=[&] {
    assert(!pool.m_criticalSection.held);
    assert(pool.m_freeBuffers.empty());
  };
  ++CAMLVideoBufferPool::s_outstandingBuffers; // This fixture manually seeds one acquired ID.
  pool.Return(0);
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
  assert((pool.m_freeBuffers==std::vector<int>{0}));
  assert((codec->trace==std::vector<std::string>{"drop"}));
}
int main() {
  PresentationOwner(); RequestIdentity(); EpochAndTimeout(); BufferPolicy(); PermitBeforeClaim();
  RetireOldEpoch(); Race(true); Race(false); PollPinsMutation(); PoolUnlock();
}
'''

def negative_controls():
    original = harness()
    original_header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLSession.h').read_text()
    controls = [
        ('wrong presentation owner admitted', 'header',
         '(!retirement && std::this_thread::get_id() != m_state->owner) ||', 'false ||'),
        ('foreign request accepted', 'header',
         'request.identity == m_state && ', ''),
        ('mutation skips live counts', 'header',
         'm_state->active || m_state->retiring || m_state->mutating || m_state->displayActive ||',
         'm_state->mutating || m_state->displayActive ||'),
        ('foreign permit claims buffer', 'source',
         '!m_codec->IsPresentationPermit(permit, m_operationEpoch)', '!permit'),
        ('retirement permit presents', 'source',
         'permit.IsRetirement() || permit.IsControl() || previousPts', 'previousPts'),
        ('required drop claim skipped', 'source',
         'if (m_consumption.compare_exchange_strong(expected, Consumption::CLAIMED))',
         'if (false && m_consumption.compare_exchange_strong(expected, Consumption::CLAIMED))'),
        ('pool lock held across QBUF', 'source',
         'buffer = m_videoBuffers[id];\n  }\n  const bool returned = buffer->Drop();',
         'buffer = m_videoBuffers[id];\n    buffer->Drop();\n  }\n  const bool returned = buffer->Drop();'),
    ]
    with tempfile.TemporaryDirectory(prefix='aml-session-negative-') as temporary:
        out = Path(temporary)
        header = out / 'cores/VideoPlayer/DVDCodecs/Video/AMLSession.h'
        header.parent.mkdir(parents=True)
        for name, target, before, after in controls:
            source, text = original, original_header
            assert before in (text if target == 'header' else source), name
            if target == 'header':
                text = text.replace(before, after, 1)
            else:
                source = source.replace(before, after, 1)
            header.write_text(text)
            (out / 'test.cpp').write_text(source)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                            '-Werror', '-pthread', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                            str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
            result = subprocess.run([str(out / 'test')], capture_output=True, text=True, timeout=15)
            assert result.returncode != 0 and 'Assertion' in result.stderr, (name, result.stderr)
            print('Rejected runtime negative control:', name)


if __name__ == '__main__':
    main()
