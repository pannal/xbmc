#!/usr/bin/env python3
"""Concurrent production presenter/adapter/buffer admission; native calls are recording stubs.
No CE compilation, real driver, GUI/GL or device claim. Uses the actual unselected
submission queue, owner handoffs and CAMLVideoBuffer consumption implementation.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

def main():
    base = ROOT / 'xbmc/cores/VideoPlayer'
    header = (base / 'DVDCodecs/Video/DVDVideoCodecAmlogic.h').read_text()
    source = (base / 'DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    adapter = (base / 'VideoRenderers/HwDecRender/AMLPresenterSession.h').read_text()
    methods = '\n'.join(extract(source, signature) for signature in [
        'void CAMLVideoBuffer::Set(', 'CAMLSession::Permit CAMLVideoBuffer::AcquirePresentation()',
        'void CAMLVideoBuffer::Commit(', 'bool CAMLVideoBuffer::Submit(', 'void CAMLVideoBuffer::ApplyGeometry(',
        'void CAMLVideoBuffer::Poll(', 'bool CAMLVideoBuffer::Drop()'])
    code = PRELUDE + extract(header, 'class CAMLVideoBuffer :') + ';\n' + methods
    code += PICTURE + extract(adapter, 'class CAMLPresenterSession\n') + ';\n'
    rm = (base / 'VideoRenderers/RenderManager.cpp').read_text()
    rh = (base / 'VideoRenderers/RenderManager.h').read_text()
    reservation = extract(rh, 'class BufferReservation') + ';'
    code += ROUTE.replace('@RESERVATION@', reservation)
    for signature in ['CRenderManager::BufferReservation::~BufferReservation()',
                      'CRenderManager::BufferReservation::BufferReservation(',
                      'CRenderManager::BufferReservation& CRenderManager::BufferReservation::operator=(',
                      'void CRenderManager::CancelReservation(']:
        code += extract(rm, signature) + '\n'
    # Execute the unchanged async branches, including the real entry locking and
    # reservation checks. Legacy branches run in test-render-slot-publication.py.
    for signature, fallback in [('int CRenderManager::WaitForBuffer(', 'return -1;'),
                                ('bool CRenderManager::AddVideoPicture(', 'return false;')]:
        body = extract(rm, signature)
        code += body[:body.index('#endif') + len('#endif')] + '\n' + fallback + '\n}\n'
    renderer = (base / 'VideoRenderers/HwDecRender/RendererAML.cpp').read_text()
    renderer_header = (base / 'VideoRenderers/HwDecRender/RendererAML.h').read_text()
    code += SYNC_RENDERER.replace('@RESUME@', extract(renderer_header, 'void ResumeIndependentPresentation('))
    code += extract(renderer, 'void CRendererAML::RenderUpdate(')
    code += extract(renderer, 'bool CRendererAML::RenderUpdateVideo(')
    code += extract(rm, 'void CRenderManager::LogAMLPresenter(')
    code += TESTS + ROUTE_TESTS
    with tempfile.TemporaryDirectory(prefix='aml-presenter-') as name:
        out = Path(name)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-DHAS_LIBAMCODEC=1', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=30)
    print('AML presenter: PASS (production executor/adapter/buffers; concurrent producer; '
          'main absent beyond pool capacity; ASan/UBSan; native/clock stubs)')

PRELUDE = r'''
#include "cores/VideoPlayer/VideoRenderers/AMLPresenter.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <cassert>
#include <future>
#include <string>
#include <iostream>
constexpr int LOGINFO=1;
struct CLog{template<class... T> static void Log(int,const char* format,T&&...) {
  size_t count=0;for(std::string f=format;f.find("{}")!=std::string::npos;f=f.substr(f.find("{}")+2))++count;
  assert(count==sizeof...(T));
}};
namespace PERFORMANCE_CORES {
  void ApplyCurrentThread(const char*,uint64_t) {}
  int CurrentCpu(){return -1;}
}
using namespace std::chrono_literals;
struct CRect {};
std::atomic<int> buffers{0}, peak{0};
struct CVideoBuffer {
  explicit CVideoBuffer(int) { int n=++buffers; int old=peak; while(old<n&&!peak.compare_exchange_weak(old,n)) {} }
  virtual ~CVideoBuffer() { --buffers; }
  std::atomic<int> refs{1};
  void Acquire() {++refs;}
  void Release();
};
class CAMLCodec {
public:
  const std::thread::id mainThread=std::this_thread::get_id();
  CAMLSession session;
  std::atomic<int> releases{0}, drops{0}, polls{0}, geometry{0};
  std::atomic<int> qbufResult{0};
  std::shared_ptr<const void> lifetime;
  CAMLCodec() {auto r=session.Fence(); assert(session.BeginMutation(r)); assert(session.Complete(r,true));}
  CAMLSession::DiagnosticSnapshot GetDiagnostics() const {return session.Diagnostics();}
  uint64_t GetOperationEpoch() const {return session.Epoch();}
  bool IsOperationInvalidated(uint64_t epoch) const {return session.IsInvalidated(epoch);}
  CAMLSession::Permit AcquirePresentation(uint64_t epoch,bool retirement=false) {return session.Acquire(epoch,retirement);}
  CAMLSession::Permit AcquireMainControl(uint64_t epoch,bool wait=false) {return session.AcquireControl(epoch,wait);}
  bool IsPresentationPermit(const CAMLSession::Permit& permit,uint64_t epoch) const {return session.Matches(permit,epoch);}
  CAMLSession::OwnerTransfer RequestPresentationOwner(std::thread::id owner) {return session.RequestOwner(owner);}
  bool AcceptPresentationOwner(const CAMLSession::OwnerTransfer& r,bool wait=false) {return session.AcceptOwner(r,wait);}
  bool CancelPresentationOwner(const CAMLSession::OwnerTransfer& r) {return session.CancelOwner(r);}
  void RetainProcessInfo(std::shared_ptr<const void> value) {lifetime=std::move(value);}
  int ReleaseFrame(uint32_t,uint64_t,const CAMLSession::Permit& permit,bool drop=false) {
    assert(!permit.IsControl() && session.Matches(permit,session.Epoch()));
    session.RecordQbuf(drop,qbufResult);
    if(drop) ++drops; else {assert(!permit.IsRetirement()); ++releases;}
    return qbufResult;
  }
  void SetVideoRect(const CRect&,const CRect&,uint64_t,const CAMLSession::Permit& permit) {
    assert(std::this_thread::get_id()==mainThread && !permit.IsRetirement());
    assert(session.Matches(permit,session.Epoch())); ++geometry;
  }
  void RefreshDecoderRate(const CAMLSession::Permit&) {}
  int PollFrame(const CAMLSession::Permit& permit) {
    assert(!permit.IsControl()&&!permit.IsRetirement()&&session.Matches(permit,session.Epoch()));
    ++polls; return 1;
  }
};
'''
PICTURE = r'''
void CVideoBuffer::Release() {
  if(--refs==0) {assert(static_cast<CAMLVideoBuffer*>(this)->Drop()); delete this;}
}
struct VideoPicture {
  CVideoBuffer* videoBuffer{nullptr}; double pts{0}; int iFlags{0};
  ~VideoPicture() {if(videoBuffer) videoBuffer->Release();}
  void CopyRef(const VideoPicture& other) {videoBuffer=other.videoBuffer; pts=other.pts; if(videoBuffer) videoBuffer->Acquire();}
};
enum EFIELDSYNC {FS_NONE,FS_TOP,FS_BOT};
namespace OVERLAY {struct CRenderer {using OverlayBatch=std::vector<int>;};}
struct CDVDClock {
  std::chrono::steady_clock::time_point start=std::chrono::steady_clock::now();
  double GetClock() const {return std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-start).count();}
  double GetClockSpeed() const {return 1;}
  bool GetClockInfo(int& missed,double& speed,double& refresh) {missed=0;speed=1;refresh=1000;return false;}
  void SetVsyncAdjust(double) {}
  PLAYBACK_DIAGNOSTICS::ClockHistory::Report GetDiagnosticEvents(uint64_t) {return {};}
};
'''
SYNC_RENDERER = r'''
struct CRendererAML {
  struct {CVideoBuffer* videoBuffer=nullptr;} m_buffers[1];
  struct PreparedVideoGeometry {CRect source,destination;};
  PreparedVideoGeometry PrepareVideoLayer(){return {};}
  std::shared_ptr<CAMLCodec> m_pollCodec;
  uint64_t m_pollEpoch=0;
  int m_prevVPts=-1;
  bool m_resumeControl=false;
  @RESUME@
  void RenderUpdate(int,int,bool,unsigned int,unsigned int);
  bool RenderUpdateVideo(int,int,bool,unsigned int,unsigned int);
};
'''
TESTS = r'''
template<class F> void Await(F ready) {
  const auto end=std::chrono::steady_clock::now()+3s;
  while(!ready()) {assert(std::chrono::steady_clock::now()<end); std::this_thread::sleep_for(1ms);}
}
void Put(CAMLPresenterSession& session,const std::shared_ptr<CAMLCodec>& codec,CDVDClock& clock,
         int pts,int menu=0,CAMLPresenter::Method method=CAMLPresenter::Method::SINGLE,double age=0) {
  auto slot=session.queue->Reserve(2s); assert(slot);
  VideoPicture picture;
  auto* buffer=new CAMLVideoBuffer(pts);
  buffer->Set(codec,pts,40,static_cast<uint32_t>(pts),1);
  picture.videoBuffer=buffer; picture.pts=clock.GetClock()-age;
  auto content=std::make_shared<CAMLPresenterSession::OverlayObservation>();
  content->overlays.push_back(menu); content->method=method;
  auto frame=std::make_shared<CAMLPresenterSession::Frame>(picture,content);
  frame->force=false; // normal clock-driven selection; Show(true) enables it
  assert(session.queue->Publish(slot,frame));
}
void Acknowledge(CAMLPresenterSession& session) {
  Await([&] {return bool(session.queue->PendingControl().frame);});
  Await([&] {return session.ApplyControl({},{});});
}
void IndependentProgress() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,4,std::make_shared<int>(1));
  session.Authorize();
  session.SetTiming(1000,25,0); session.queue->Show(true);
  Put(session,codec,clock,1,1); Acknowledge(session);
  assert(session.queue->WaitIdle(1s));
  const auto beforeGap=session.queue->Diagnostics().sample;
  const int geometry=codec->geometry;
  const int before=codec->releases;
  auto producer=std::async(std::launch::async,[&] {
    for(int i=2;i<=129;++i) Put(session,codec,clock,i,i);
  });
  // No main processing/control effects while >32 poolfuls are selected and
  // retired. Main retains only its older CPU observation, never a video lease.
  const auto old=session.queue->Observe();
  assert(producer.wait_for(4s)==std::future_status::ready); producer.get();
  Await([&] {return codec->releases>=before+128;});
  assert(codec->geometry==geometry);
  assert(session.queue->WaitIdle(1s));
  const auto afterGap=session.queue->Diagnostics();
  const auto progress=afterGap.sample.progress.Since(beforeGap.progress);
  assert(progress.published==128 && progress.qbufAttempts==128 && progress.completed==128);
  assert(progress.retired==128 && progress.polls>=128);
  assert(afterGap.sample.progress.retired+afterGap.outstanding==129);
  const auto admission=codec->GetDiagnostics();
  assert(admission.qbufCalls==129 && admission.dropCalls==0 && admission.qbufErrors==0);
  assert(session.queue->Outstanding()<=4 && peak<=5);
  auto observation=session.queue->Observe();
  auto menu=std::static_pointer_cast<const CAMLPresenterSession::OverlayObservation>(observation.payload);
  assert(menu && menu->overlays.front()==129);
  {
    auto capture=codec->AcquireMainControl(codec->GetOperationEpoch(),true);
    assert(capture && capture.IsControl());
    const int polls=codec->polls;
    std::this_thread::sleep_for(10ms);
    assert(codec->polls==polls);
    assert(!codec->AcquirePresentation(codec->GetOperationEpoch()));
    assert(!codec->session.AcquireDecoder());
  }
  assert(session.Stop());
  assert(codec->AcquirePresentation(codec->GetOperationEpoch()));
  auto frames=session.queue->TakeFrames(); frames.clear(); assert(buffers==0);
}
void ControlsAndEpochs() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,4,std::make_shared<int>(2));
  session.Authorize();
  session.SetTiming(1000,25,0); session.queue->Show(true);
  codec->qbufResult=-1;
  Put(session,codec,clock,100);
  Await([&] {return bool(session.queue->PendingControl().frame);});
  auto old=session.queue->PendingControl();
  assert(codec->releases==1 && codec->polls==0);
  session.queue->SetControl(2);
  assert(!session.queue->CompleteControl(old));
  Await([&] {return session.queue->PendingControl().generation==2;});
  Acknowledge(session); Await([&] {return codec->polls>0;});
  assert(codec->releases==1); // failed QBUF is not retried
  Put(session,codec,clock,100); // duplicate OMX PTS: no QBUF, later drop
  Put(session,codec,clock,101,3,CAMLPresenter::Method::BOB);
  Await([&] {return codec->releases==2 && codec->drops>=1;});
  session.queue->SetControl(3);
  Await([&] {return bool(session.queue->PendingControl().frame);});
  const auto epoch=codec->GetOperationEpoch();
  auto reset=codec->session.Fence();
  Await([&] {return codec->session.BeginMutation(reset);});
  assert(codec->session.Complete(reset,true));
  assert(!codec->AcquirePresentation(epoch));
  Put(session,codec,clock,102,4,CAMLPresenter::Method::BLEND);
  Await([&] {auto p=session.queue->PendingControl(); return p.frame&&p.frame->epoch!=epoch;});
  Acknowledge(session); Await([&] {return codec->releases==3;});
  assert(session.Stop());
  old={}; auto frames=session.queue->TakeFrames(); frames.clear(); assert(buffers==0);
}
void HeldBobDrain() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,2,std::make_shared<int>(3));
  session.Authorize();
  session.SetTiming(1000,25,0); session.queue->Show(true);
  Put(session,codec,clock,199,0,CAMLPresenter::Method::BOB);
  Acknowledge(session);
  Await([&] {return session.queue->Stats().queued==0;});
  const int polls=codec->polls;
  for(int n=0;n<150;++n) {
    assert(session.queue->Stats().queued==0);
    std::this_thread::sleep_for(1ms);
  }
  assert(codec->polls>polls+4);
  assert(session.Stop());
  auto frames=session.queue->TakeFrames();frames.clear();assert(buffers==0);
}
void PendingHandoffAndDrain() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,2,std::make_shared<int>(3));
  session.Authorize();
  session.SetTiming(1000,25,0); session.queue->Show(true);
  Put(session,codec,clock,201);
  Await([&] {return bool(session.queue->PendingControl().frame);});
  std::this_thread::sleep_for(125ms); // exceeds the EOF quiet timer
  assert(session.queue->Stats().queued==1 && codec->polls==0);
  assert(session.Stop());
  auto frames=session.queue->TakeFrames();
  assert(frames.size()==1);
  auto frame=std::static_pointer_cast<CAMLPresenterSession::Frame>(frames.front());
  CRendererAML renderer;
  renderer.m_buffers[0].videoBuffer=frame->buffer;
  renderer.ResumeIndependentPresentation(session.queue->PreviousPts());
  // A transient native fence must retain the control continuation.
  auto native=CAMLSession::FenceNative();assert(native);
  Await([&] {return CAMLSession::TryBeginNative(native);});
  renderer.RenderUpdate(0,-1,false,0,255);
  assert(codec->geometry==0 && codec->polls==0);
  assert(CAMLSession::EndNative(native));
  renderer.RenderUpdate(0,-1,false,0,255);
  assert(codec->releases==1 && codec->geometry==1 && codec->polls==1);
  renderer.RenderUpdate(0,-1,false,0,255);
  assert(codec->releases==1 && codec->geometry==1 && codec->polls==2);
  frame.reset();frames.clear();assert(buffers==0);
}
void CancelAfterSubmit() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,2,std::make_shared<int>(3));
  session.Authorize();
  session.SetTiming(1000,25,0); session.queue->Show(true);
  Put(session,codec,clock,200);
  Await([&] {return bool(session.queue->PendingControl().frame);});
  auto request=session.queue->PendingControl();
  assert(session.Stop()); // no main control processing needed to stop/join
  assert(!session.queue->CompleteControl(request));
  assert(codec->releases==1 && codec->polls==0 && codec->geometry==0);
  request={}; auto frames=session.queue->TakeFrames(); frames.clear();
  assert(codec->drops==0 && buffers==0); // submitted is not pending-return
  assert(codec->AcquirePresentation(codec->GetOperationEpoch()));
}
void LateFramesAndNativeFence() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  std::unique_ptr<CAMLPresenterSession> session;
  {
    auto previousOwner=codec->AcquirePresentation(codec->GetOperationEpoch());
    session=std::make_unique<CAMLPresenterSession>(codec,clock,4,std::make_shared<int>(4));
    session->Authorize();
    session->SetTiming(1000,25,0); session->queue->Show(true);
    for(int n=1;n<=4;++n) Put(*session,codec,clock,n,n,CAMLPresenter::Method::SINGLE,200000);
  }
  Acknowledge(*session);
  Await([&] {return codec->releases==2;});
  assert(codec->drops==2 && session->queue->Skipped()==2);
  assert(session->queue->WaitIdle(1s));
  const auto native=CAMLSession::FenceNative(); assert(native);
  Await([&] {return CAMLSession::TryBeginNative(native);});
  const int releases=codec->releases, polls=codec->polls;
  auto producer=std::async(std::launch::async,[&] {for(int n=5;n<=20;++n) Put(*session,codec,clock,n,n);});
  std::this_thread::sleep_for(10ms);
  assert(codec->releases==releases && codec->polls==polls);
  assert(session->queue->Outstanding()<=4);
  assert(CAMLSession::EndNative(native));
  assert(producer.wait_for(3s)==std::future_status::ready); producer.get();
  assert(session->Stop()); auto frames=session->queue->TakeFrames(); frames.clear(); assert(buffers==0);
}
void UnapprovedStartup() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CAMLPresenterSession session(codec,clock,2,std::make_shared<int>(0));
  session.queue->Show(true);Put(session,codec,clock,900);
  assert(session.RequiresNativeGuiWait());
  std::this_thread::sleep_for(20ms);
  const auto owner=codec->GetDiagnostics();
  assert(owner.mainOwner && owner.transferring && codec->polls==0 && codec->releases==0);
  assert(session.Stop());assert(!session.RequiresNativeGuiWait());
  auto frames=session.queue->TakeFrames();assert(frames.size()==1);frames.clear();
  assert(buffers==0 && codec->AcquirePresentation(codec->GetOperationEpoch()));
}
void RouteProgress();
int main() {
  UnapprovedStartup(); RouteProgress(); IndependentProgress(); ControlsAndEpochs(); HeldBobDrain(); PendingHandoffAndDrain(); CancelAfterSubmit(); LateFramesAndNativeFence();
  std::cout<<"production presenter scenarios passed\n";
}
'''
ROUTE = r'''
#include <array>
using CCriticalSection=std::recursive_mutex;
constexpr int DVP_FLAG_INTERLACED=1, DVP_FLAG_TOP_FIELD_FIRST=2;
enum EINTERLACEMETHOD {VS_INTERLACEMETHOD_NONE,VS_INTERLACEMETHOD_RENDER_BLEND,VS_INTERLACEMETHOD_RENDER_BOB};
class CRenderManager {
public:
  uint64_t m_diagnosticId=1;
  void LogAMLPresenter(const char*,bool=false);
  @RESERVATION@
  std::shared_ptr<CAMLPresenterSession> m_amlPresenter;
  CCriticalSection m_presentlock;
  std::array<uint64_t,16> m_reservations{};
  std::deque<int> m_free;
  struct {void notifyAll() {}} m_presentevent;
  void CancelReservation(BufferReservation&);
  int WaitForBuffer(BufferReservation&,volatile std::atomic_bool&,std::chrono::milliseconds);
  bool AddVideoPicture(BufferReservation&,const VideoPicture&,OVERLAY::CRenderer::OverlayBatch,
                       volatile std::atomic_bool&,EINTERLACEMETHOD,bool);
};
'''
ROUTE_TESTS = r'''
void RouteProgress() {
  auto codec=std::make_shared<CAMLCodec>(); CDVDClock clock;
  CRenderManager manager;
  manager.m_amlPresenter=std::make_shared<CAMLPresenterSession>(codec,clock,4,std::make_shared<int>(0));
  auto session=manager.m_amlPresenter;
  session->Authorize();
  session->SetTiming(1000,25,0); session->queue->Show(true);
  auto publish=[&](int n) {
    std::atomic_bool stop{false};
    CRenderManager::BufferReservation reservation;
    assert(manager.WaitForBuffer(reservation,stop,2s)>=0);
    VideoPicture picture; picture.pts=clock.GetClock();
    auto* buffer=new CAMLVideoBuffer(n); buffer->Set(codec,n,40,static_cast<uint32_t>(n),1);
    picture.videoBuffer=buffer;
    assert(manager.AddVideoPicture(reservation,picture,{n},stop,VS_INTERLACEMETHOD_NONE,false));
  };
  publish(1); Acknowledge(*session);
  manager.LogAMLPresenter("test-start",true);
  auto producer=std::async(std::launch::async,[&] {for(int i=2;i<=129;++i) publish(i);});
  assert(producer.wait_for(4s)==std::future_status::ready); producer.get();
  Await([&] {return codec->releases==129;});
  assert(session->queue->WaitIdle(1s));
  manager.LogAMLPresenter("test-complete",true);
  assert(session->queue->Stats().queued==0); // current frame cannot block EOS/drain
  CRenderManager::BufferReservation old;
  std::atomic_bool stop{false};
  assert(manager.WaitForBuffer(old,stop,1s)>=0);
  assert(session->Stop());
  auto frames=session->queue->TakeFrames(); frames.clear();
  manager.m_amlPresenter=std::make_shared<CAMLPresenterSession>(codec,clock,4,std::make_shared<int>(0));
  VideoPicture stale;
  assert(!manager.AddVideoPicture(old,stale,{},stop,VS_INTERLACEMETHOD_NONE,false));
  old=CRenderManager::BufferReservation{};
  assert(manager.m_amlPresenter->queue->Outstanding()==0);
  assert(manager.m_amlPresenter->Stop()); manager.m_amlPresenter.reset();
  assert(buffers==0);
}
'''
if __name__ == '__main__':
    main()
