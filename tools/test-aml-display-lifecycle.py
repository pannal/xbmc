#!/usr/bin/env python3
"""Production AML display admission, deferred pool returns and output reservation.

Uses the real AMLSession header, buffer Drop, every pool method, base CVideoBuffer
Acquire/Release and wrapper GetPicture. Codec device operations/output, display
mutation and wrapper ContinueLifecycle are controlled stubs. ASan/UBSan exercises
ownership and deterministic host interleavings; no driver/EGL/device claim.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
SESSION = 'cores/VideoPlayer/DVDCodecs/Video/AMLSession.h'


def harness():
    header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.h').read_text()
    source = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    base = (ROOT / 'xbmc/cores/VideoPlayer/Buffers/VideoBuffer.cpp').read_text()
    classes = '\n'.join(function(header, signature).replace('private:', 'public:') + ';'
                        for signature in ['class CAMLVideoBuffer :', 'class CAMLVideoBufferPool :'])
    methods = '\n'.join(function(source, signature) for signature in [
        'CAMLVideoBufferPool::~CAMLVideoBufferPool()', 'CVideoBuffer* CAMLVideoBufferPool::Get()',
        'void CAMLVideoBuffer::Set(', 'CAMLSession::Permit CAMLVideoBuffer::AcquirePresentation()',
        'void CAMLVideoBuffer::Commit(', 'void CAMLVideoBuffer::Poll(', 'bool CAMLVideoBuffer::Drop()',
        'void CAMLVideoBufferPool::Return(', 'void CAMLVideoBufferPool::QueueReturns(',
        'bool CAMLVideoBufferPool::HasPendingReturns()',
        'void CAMLVideoBufferPool::ProcessPendingReturns()',
        'void CAMLVideoBufferPool::ProcessReturns()'])
    methods += '\n' + '\n'.join(function(base, signature) for signature in [
        'CVideoBuffer::CVideoBuffer(int', 'void CVideoBuffer::Acquire()',
        'void CVideoBuffer::Acquire(std::shared_ptr', 'void CVideoBuffer::Release()'])
    wrapper = function(source, 'CDVDVideoCodec::VCReturn CDVDVideoCodecAmlogic::GetPicture(')
    return PRELUDE + classes + STATICS + methods + WRAPPER + wrapper + TESTS


def run(source, header, negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-display-') as temporary:
        out = Path(temporary)
        include = out / SESSION
        include.parent.mkdir(parents=True)
        include.write_text(header)
        (out / 'test.cpp').write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                   '-pthread', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                   str(out / 'test.cpp'), '-o', str(out / 'test')]
        if not negative:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        subprocess.run(command, check=True)
        result = subprocess.run([str(out / 'test')], capture_output=negative,
                                text=True, timeout=20, check=not negative)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source, header = harness(), (ROOT / 'xbmc' / SESSION).read_text()
    if args.negative_controls:
        controls = [
            ('display ignores active permits', 'header', 'ready && !state.active &&', 'ready &&'),
            ('display permits retirement', 'header', 'm_state->displayFenced || epoch',
             '(m_state->displayFenced && !retirement) || epoch'),
            ('display rejection tombstones return', 'source',
             'if (m_codec->IsOperationInvalidated(m_operationEpoch))',
             'if (true || m_codec->IsOperationInvalidated(m_operationEpoch))'),
            ('old pools exceed global budget', 'source',
             'if (outstanding >= MAX_OUTSTANDING_BUFFERS)', 'if (false && outstanding >= MAX_OUTSTANDING_BUFFERS)'),
            ('pool rejection leaks global token', 'source',
             '--s_outstandingBuffers;\n      return nullptr;', '(void)s_outstandingBuffers;\n      return nullptr;'),
            ('late enqueue retains empty pool', 'source',
             'if (pool->HasPendingReturns() &&', 'if (true &&'),
            ('player picture reference leaks', 'source',
             '    if (pVideoPicture->videoBuffer)\n      pVideoPicture->videoBuffer->Release();\n', ''),
            ('pool exceeds bounded capacity', 'source', 'if (m_videoBuffers.size() >= MAX_BUFFERS)',
             'if (false && m_videoBuffers.size() >= MAX_BUFFERS)'),
            ('failed-context cancellation reopens', 'header',
             'state.displayFenced = !s_cancelDisplayReady;', 'state.displayFenced = false;'),
            ('deferred return publishes capacity', 'source',
             'm_pendingReturns.insert(id);', 'm_freeBuffers.push_back(id);'),
        ]
        for name, target, before, after in controls:
            original = header if target == 'header' else source
            assert before in original, name
            changed = original.replace(before, after, 1)
            run(changed if target == 'source' else source,
                changed if target == 'header' else header, negative=True)
            print('Rejected runtime negative control:', name)
    else:
        run(source, header)
        print('AML display lifecycle: PASS (real session/buffer/pool/GetPicture; ASan/UBSan; '
              'recording codec and modeled native display mutation)')


PRELUDE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <atomic>
#include <cassert>
#include <cmath>
#include <functional>
#include <future>
#include <set>
#include <string>
#include <thread>
using namespace std::chrono_literals;
constexpr int LOGDEBUG=0;
struct CLog {template<class... T>static void Log(T&&...) {}};
struct CRect {};
struct CCriticalSection {
  std::mutex mutex;
  std::atomic<size_t> owner{0};
  void lock(){mutex.lock();owner=std::hash<std::thread::id>{}(std::this_thread::get_id());}
  void unlock(){owner=0;mutex.unlock();}
  bool HeldHere()const{return owner==std::hash<std::thread::id>{}(std::this_thread::get_id());}
};
class CVideoBuffer;
class IVideoBufferPool:public std::enable_shared_from_this<IVideoBufferPool> {
public:
  virtual ~IVideoBufferPool()=default;
  virtual CVideoBuffer* Get()=0;
  virtual void Return(int)=0;
  std::shared_ptr<IVideoBufferPool> GetPtr(){return shared_from_this();}
};
class CVideoBuffer {
public:
  explicit CVideoBuffer(int);
  virtual ~CVideoBuffer()=default;
  void Acquire();void Acquire(std::shared_ptr<IVideoBufferPool>);void Release();
  int GetId()const{return m_id;}
  int m_id;
  std::atomic<int> m_refCount{0};
  std::shared_ptr<IVideoBufferPool> m_pool;
};
struct VideoPicture {
  CVideoBuffer* videoBuffer{nullptr};
  double pts{0};int iWidth{1920},iHeight{1080},iDisplayWidth{1920},iDisplayHeight{1080};
  // As production: SetParams releases the buffer the picture still holds.
  void SetParams(const VideoPicture& picture){if(videoBuffer)videoBuffer->Release();videoBuffer=nullptr;pts=picture.pts;}
};
struct CDVDVideoCodec {enum VCReturn {VC_NONE,VC_ERROR,VC_BUFFER,VC_PICTURE};};
class CAMLCodec {
public:
  CAMLSession session;
  std::vector<uint32_t> returned;
  std::function<void()> onReturn;
  uint64_t generation{1};
  int dequeues{0};
  CDVDVideoCodec::VCReturn output{CDVDVideoCodec::VC_PICTURE};
  explicit CAMLCodec(bool open=true){if(open)Open();}
  void Open(){auto request=session.Fence();assert(session.BeginMutation(request));assert(session.Complete(request,true));}
  uint64_t GetOperationEpoch()const{return session.Epoch();}
  uint64_t GetPresentationGeneration()const{return generation;}
  CAMLSession::Permit AcquirePresentation(uint64_t epoch,bool retirement=false){return session.Acquire(epoch,retirement);}
  bool IsOperationInvalidated(uint64_t epoch)const{return session.IsInvalidated(epoch);}
  bool IsPresentationPermit(const CAMLSession::Permit& permit,uint64_t epoch)const{return session.Matches(permit,epoch);}
  int ReleaseFrame(uint32_t index,uint64_t gen,const CAMLSession::Permit& permit,bool drop=false){
    assert(drop && gen==generation && session.Matches(permit,session.Epoch()));
    assert(!session.DisplayBlocked());
    if(onReturn)onReturn();
    returned.push_back(index);return 0;
  }
  void SetVideoRect(const CRect&,const CRect&,uint64_t,const CAMLSession::Permit&){}
  void PollFrame(const CAMLSession::Permit&){}
  CDVDVideoCodec::VCReturn GetPicture(VideoPicture&){++dequeues;return output;}
  int GetOMXPts()const{return 1;}int GetAmlDuration()const{return 40;}
  uint32_t GetBufferIndex()const{return dequeues;}
};
'''
STATICS = r'''
std::atomic<size_t> CAMLVideoBufferPool::s_outstandingBuffers{0};
std::mutex CAMLVideoBufferPool::s_returnMutex;
bool CAMLVideoBufferPool::s_processingReturns{false};
std::vector<std::shared_ptr<CAMLVideoBufferPool>> CAMLVideoBufferPool::s_returnPools;
'''
WRAPPER = r'''
class CDVDVideoCodecAmlogic:public CDVDVideoCodec {
public:
  std::shared_ptr<CAMLCodec> m_Codec;
  std::shared_ptr<CAMLVideoBufferPool> m_videoBufferPool;
  VideoPicture m_videobuffer;
  struct Sequence{float ratio{1.0f};};
  Sequence *m_mpeg2_sequence{nullptr},*m_h264_sequence{nullptr};
  double m_mpeg2_sequence_pts{0},m_h264_sequence_pts{0};
  float m_aspect_ratio{1.0f};struct{bool forced_aspect{false};}m_hints;
  bool ContinueLifecycle(){return true;}
  VCReturn GetPicture(VideoPicture*);
};
'''
TESTS = r'''
using Phase=CAMLSession::DisplayPhase;
void Set(CAMLVideoBuffer* buffer,const std::shared_ptr<CAMLCodec>& codec,uint32_t index){
  buffer->Set(codec,1,40,index,codec->generation);
}
void OwnPermitAndDecoder() {
  CAMLSession session;auto opening=session.Fence();
  assert(session.BeginMutation(opening)&&session.Complete(opening,true));
  const auto epoch=session.Epoch();
  CAMLSession::DisplayRequest display;
  {
    auto permit=session.Acquire(epoch);assert(permit);
    display=CAMLSession::FenceDisplay();assert(display);
    // Never waits for a permit held by this very caller.
    assert(!CAMLSession::TryBeginDisplay(display));
    assert(!session.Acquire(epoch)&&!session.Acquire(epoch,true)&&!session.AcquireDecoder());
    assert(!session.IsInvalidated(epoch));
  }
  assert(CAMLSession::TryBeginDisplay(display));
  auto reset=session.Fence();assert(!session.BeginMutation(reset));
  assert(CAMLSession::EndDisplay(display,Phase::READY));
  assert(session.Epoch()==epoch);
  assert(!session.Acquire(epoch)); // Display completion cannot undo decoder fencing.
  assert(session.BeginMutation(reset)&&session.Complete(reset,true));
  {
    auto decoder=session.AcquireDecoder();assert(decoder);
    display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
  }
  assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,Phase::READY));
}
void RegistrationAndStale() {
  auto display=CAMLSession::FenceDisplay();
  CAMLSession pending;
  auto open=pending.Fence();assert(pending.BeginMutation(open));assert(pending.Complete(open,true));
  assert(pending.DisplayBlocked()&&!pending.AcquireDecoder());
  assert(CAMLSession::TryBeginDisplay(display));
  CAMLSession during;
  auto next=during.Fence();assert(!during.BeginMutation(next));
  assert(during.DisplayBlocked());
  std::thread foreign([&]{assert(!CAMLSession::EndDisplay(display,Phase::READY));});foreign.join();
  assert(CAMLSession::EndDisplay(display,Phase::WAITING_FOR_RESET));
  assert(pending.DisplayBlocked()&&during.DisplayBlocked());
  assert(during.BeginMutation(next)&&during.Complete(next,true));
  assert(CAMLSession::EndDisplay(display,Phase::READY));
  assert(pending.AcquireDecoder()&&during.AcquireDecoder());
  auto old=CAMLSession::FenceDisplay(),current=CAMLSession::FenceDisplay();
  assert(!CAMLSession::CancelDisplay(old)&&!CAMLSession::TryBeginDisplay(old));
  assert(CAMLSession::TryBeginDisplay(current));
  assert(!CAMLSession::EndDisplay(old,Phase::READY));
  assert(CAMLSession::EndDisplay(current,Phase::READY));
}
void DeferredPressure() {
  auto codec=std::make_shared<CAMLCodec>();auto pool=std::make_shared<CAMLVideoBufferPool>();
  std::vector<CAMLVideoBuffer*> acquired;
  for(size_t i=0;i<CAMLVideoBufferPool::MAX_BUFFERS;++i){
    auto* buffer=static_cast<CAMLVideoBuffer*>(pool->Get());assert(buffer);Set(buffer,codec,i);acquired.push_back(buffer);
  }
  assert(!pool->Get());
  assert(CAMLVideoBufferPool::s_outstandingBuffers==CAMLVideoBufferPool::MAX_BUFFERS);
  CDVDVideoCodecAmlogic wrapper;wrapper.m_Codec=codec;wrapper.m_videoBufferPool=pool;VideoPicture output;
  assert(wrapper.GetPicture(&output)==CDVDVideoCodec::VC_NONE&&codec->dequeues==0);
  const auto epoch=codec->GetOperationEpoch();
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  for(auto* buffer:acquired)buffer->Release();
  assert(pool->m_pendingReturns.size()==CAMLVideoBufferPool::MAX_BUFFERS&&pool->m_freeBuffers.empty());
  assert(CAMLVideoBufferPool::s_returnPools.size()==1&&!pool->Get());
  CAMLVideoBufferPool::ProcessReturns();CAMLVideoBufferPool::ProcessReturns();
  assert(codec->returned.empty()&&pool->m_freeBuffers.empty());
  assert(CAMLSession::EndDisplay(display,Phase::READY));assert(codec->GetOperationEpoch()==epoch);
  codec->onReturn=[&]{assert(!pool->m_criticalSection.HeldHere());assert(pool->m_freeBuffers.size()<CAMLVideoBufferPool::MAX_BUFFERS);};
  CAMLVideoBufferPool::ProcessReturns();
  assert(codec->returned.size()==CAMLVideoBufferPool::MAX_BUFFERS);
  assert(pool->m_freeBuffers.size()==CAMLVideoBufferPool::MAX_BUFFERS&&pool->m_pendingReturns.empty());
  assert(CAMLVideoBufferPool::s_returnPools.empty());
  CAMLVideoBufferPool::ProcessReturns();assert(codec->returned.size()==CAMLVideoBufferPool::MAX_BUFFERS);
  codec->onReturn={};
  assert(wrapper.GetPicture(&output)==CDVDVideoCodec::VC_PICTURE&&codec->dequeues==1);
  output.videoBuffer->Release();output.videoBuffer=nullptr;
  assert(codec->returned.size()==CAMLVideoBufferPool::MAX_BUFFERS+1);
  const auto freeCount=pool->m_freeBuffers.size();codec->output=CDVDVideoCodec::VC_BUFFER;
  assert(wrapper.GetPicture(&output)==CDVDVideoCodec::VC_BUFFER&&codec->dequeues==2);
  assert(pool->m_freeBuffers.size()==freeCount);
}
// CVideoPlayerVideo keeps m_picture after output (still-frame repeats) and
// passes it back to GetPicture. Each new picture must release the previous
// one, or the bounded pool runs dry after MAX_BUFFERS frames and output stops.
void PlayerKeepsLastPicture() {
  auto codec=std::make_shared<CAMLCodec>();auto pool=std::make_shared<CAMLVideoBufferPool>();
  CDVDVideoCodecAmlogic wrapper;wrapper.m_Codec=codec;wrapper.m_videoBufferPool=pool;
  VideoPicture picture;
  for(size_t i=0;i<3*CAMLVideoBufferPool::MAX_BUFFERS;++i){
    assert(wrapper.GetPicture(&picture)==CDVDVideoCodec::VC_PICTURE);
    assert(picture.videoBuffer&&CAMLVideoBufferPool::s_outstandingBuffers==1);
  }
  assert(pool->m_videoBuffers.size()<=2);
  assert(codec->returned.size()==3*CAMLVideoBufferPool::MAX_BUFFERS-1);
  picture.videoBuffer->Release();picture.videoBuffer=nullptr;
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
}
void ResetOvertakes(bool close) {
  auto codec=std::make_shared<CAMLCodec>();auto pool=std::make_shared<CAMLVideoBufferPool>();
  auto* buffer=static_cast<CAMLVideoBuffer*>(pool->Get());Set(buffer,codec,7);
  auto display=CAMLSession::FenceDisplay();buffer->Release();
  assert(pool->m_pendingReturns.size()==1&&codec->returned.empty());
  auto reset=codec->session.Fence();assert(codec->session.BeginMutation(reset));
  assert(!CAMLSession::TryBeginDisplay(display));
  CAMLVideoBufferPool::ProcessReturns(); // Reset owns invalidation; memory may now retire.
  assert(codec->returned.empty()&&pool->m_freeBuffers.size()==1&&pool->m_pendingReturns.empty());
  assert(codec->session.Complete(reset,!close));
  assert(CAMLSession::TryBeginDisplay(display));assert(CAMLSession::EndDisplay(display,Phase::READY));
  CAMLVideoBufferPool::ProcessReturns();assert(codec->returned.empty()&&pool->m_freeBuffers.size()==1);
  assert(bool(codec->AcquirePresentation(codec->GetOperationEpoch()))==!close);
}
void FailedBindAndCancel() {
  auto codec=std::make_shared<CAMLCodec>();
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,Phase::FAILED));
  assert(codec->session.DisplayBlocked());
  auto retry=CAMLSession::FenceDisplay();assert(CAMLSession::CancelDisplay(retry));
  // Cancelling a retry cannot establish a valid context after the earlier failure.
  assert(codec->session.DisplayBlocked()&&!codec->AcquirePresentation(codec->GetOperationEpoch(),true));
  auto repaired=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(repaired));
  assert(!CAMLSession::EndDisplay(display,Phase::READY));
  assert(CAMLSession::EndDisplay(repaired,Phase::READY));
}
void ConcurrentAndReentrantPump() {
  auto codec=std::make_shared<CAMLCodec>();auto pool=std::make_shared<CAMLVideoBufferPool>();
  auto* buffer=static_cast<CAMLVideoBuffer*>(pool->Get());Set(buffer,codec,9);
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  buffer->Release();assert(CAMLSession::EndDisplay(display,Phase::READY));
  std::promise<void> entered,release;
  auto enteredFuture=entered.get_future(),releaseFuture=release.get_future();
  codec->onReturn=[&]{
    assert(!pool->m_criticalSection.HeldHere());
    // Reentry cannot claim/publish the buffer currently undergoing QBUF.
    CAMLVideoBufferPool::ProcessReturns();
    assert(pool->m_freeBuffers.empty());
    entered.set_value();releaseFuture.wait();
  };
  std::thread pump([]{CAMLVideoBufferPool::ProcessReturns();});
  assert(enteredFuture.wait_for(5s)==std::future_status::ready);
  CAMLVideoBufferPool::ProcessReturns(); // A second executor cannot duplicate publication.
  assert(pool->m_freeBuffers.empty());
  // A decoder allocation may grow the pointer vector while the pump holds its
  // detached original-buffer pointer. This is an ASan ownership check, not TSAN.
  auto* additional=pool->Get();assert(additional&&additional!=buffer);
  release.set_value();pump.join();codec->onReturn={};
  assert(codec->returned==std::vector<uint32_t>{9});
  assert(pool->m_freeBuffers==std::vector<int>{buffer->GetId()});
  additional->Release();
  assert(pool->m_freeBuffers.size()==2&&CAMLVideoBufferPool::s_returnPools.empty());
}
void MultiplePoolBudgetAndLateEnqueue() {
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
  auto codec=std::make_shared<CAMLCodec>();
  std::vector<std::shared_ptr<CAMLVideoBufferPool>> pools;
  std::vector<std::weak_ptr<CAMLVideoBufferPool>> weak;
  std::vector<CAMLVideoBuffer*> buffers;
  for(size_t i=0;i<CAMLVideoBufferPool::MAX_OUTSTANDING_BUFFERS;++i){
    auto pool=std::make_shared<CAMLVideoBufferPool>();
    auto* buffer=static_cast<CAMLVideoBuffer*>(pool->Get());assert(buffer);Set(buffer,codec,i);
    weak.push_back(pool);pools.push_back(pool);buffers.push_back(buffer);
  }
  auto excess=std::make_shared<CAMLVideoBufferPool>();
  assert(!excess->Get()&&excess->m_videoBuffers.empty());
  assert(CAMLVideoBufferPool::s_outstandingBuffers==CAMLVideoBufferPool::MAX_OUTSTANDING_BUFFERS);
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  for(auto* buffer:buffers)buffer->Release();
  auto latePool=pools.front();
  pools.clear();
  assert(CAMLVideoBufferPool::s_returnPools.size()==CAMLVideoBufferPool::MAX_OUTSTANDING_BUFFERS);
  for(const auto& pool:weak)assert(!pool.expired());
  assert(!excess->Get()); // Pressure survives user pool release and absent main pumps.
  CAMLVideoBufferPool::ProcessReturns();assert(!excess->Get());
  assert(CAMLSession::EndDisplay(display,Phase::READY));
  CAMLVideoBufferPool::ProcessReturns();
  assert(codec->returned.size()==CAMLVideoBufferPool::MAX_OUTSTANDING_BUFFERS);
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0&&CAMLVideoBufferPool::s_returnPools.empty());
  // Execute the late QueueReturns step from the Return-versus-pump race after
  // that pool's obligation has already completed. It must not retain an empty pool.
  CAMLVideoBufferPool::QueueReturns(latePool);
  assert(CAMLVideoBufferPool::s_returnPools.empty());latePool.reset();
  for(const auto& pool:weak)assert(pool.expired());
  auto* fresh=excess->Get();assert(fresh&&CAMLVideoBufferPool::s_outstandingBuffers==1);
  fresh->Release();assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
}
void ConcurrentGlobalReservations() {
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
  constexpr size_t workers=8;
  std::vector<std::shared_ptr<CAMLVideoBufferPool>> pools;
  std::vector<std::vector<CVideoBuffer*>> buffers(workers);
  for(size_t i=0;i<workers;++i)pools.push_back(std::make_shared<CAMLVideoBufferPool>());
  std::promise<void> release;auto start=release.get_future().share();
  std::vector<std::thread> threads;
  for(size_t i=0;i<workers;++i)threads.emplace_back([&,i]{
    start.wait();
    for(size_t j=0;j<CAMLVideoBufferPool::MAX_BUFFERS;++j){
      if(auto* buffer=pools[i]->Get())buffers[i].push_back(buffer);
    }
  });
  release.set_value();for(auto& thread:threads)thread.join();
  size_t count=0;for(const auto& list:buffers)count+=list.size();
  assert(count==CAMLVideoBufferPool::MAX_OUTSTANDING_BUFFERS&&CAMLVideoBufferPool::s_outstandingBuffers==count);
  for(auto& list:buffers)for(auto* buffer:list)buffer->Release();
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
}
void LifetimePin() {
  auto codec=std::make_shared<CAMLCodec>();auto pool=std::make_shared<CAMLVideoBufferPool>();
  std::weak_ptr<CAMLVideoBufferPool> weak=pool;
  auto* buffer=static_cast<CAMLVideoBuffer*>(pool->Get());Set(buffer,codec,8);
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  buffer->Release();pool.reset();assert(!weak.expired());
  CAMLVideoBufferPool::ProcessReturns();assert(!weak.expired()&&codec->returned.empty());
  assert(CAMLSession::EndDisplay(display,Phase::READY));
  CAMLVideoBufferPool::ProcessReturns();assert(weak.expired()&&codec->returned==std::vector<uint32_t>{8});
}
int main(){
  OwnPermitAndDecoder();RegistrationAndStale();DeferredPressure();PlayerKeepsLastPicture();
  ResetOvertakes(false);ResetOvertakes(true);FailedBindAndCancel();LifetimePin();
  ConcurrentAndReentrantPump();MultiplePoolBudgetAndLateEnqueue();ConcurrentGlobalReservations();
  assert(CAMLVideoBufferPool::s_outstandingBuffers==0);
}
'''

if __name__ == '__main__':
    main()
