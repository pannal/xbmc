#!/usr/bin/env python3
"""Host checks of production slot admission/publication/retirement methods.

Extracts RenderManager methods, the reservation declaration, overlay batch
attachment, ProcessOverlays, and GLES reference handling. Uses real Kodi menu
image/group classes. Clocks, GUI/device services and renderer capabilities are
controlled stubs, not GPU, libbluray delivery or device playback verification.
Requires Python 3 and g++; runs with ASan/UBSan and deterministic wait callbacks.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block(source, start):
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda m: ' ' * len(m.group()), source)
    opening = masked.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def function(source, signature):
    return block(source, source.index(signature))


def main():
    path = ROOT / 'xbmc/cores/VideoPlayer'
    rm = (path / 'VideoRenderers/RenderManager.cpp').read_text()
    header = (path / 'VideoRenderers/RenderManager.h').read_text()
    ov = (path / 'VideoRenderers/OverlayRenderer.cpp').read_text()
    oh = (path / 'VideoRenderers/OverlayRenderer.h').read_text()
    vp = (path / 'VideoPlayerVideo.cpp').read_text()
    gles = (path / 'VideoRenderers/LinuxRendererGLES.cpp').read_text()

    # The behavioral fixture exercises invalidation directly and through Flush/
    # DiscardBuffer. Check the real configure/init wiring too, without pretending
    # to emulate platform renderer creation or a display mode change.
    for name in ['bool CRenderManager::Configure(const VideoPicture&',
                 'bool CRenderManager::Configure()', 'void CRenderManager::PreInit()',
                 'void CRenderManager::UnInit()']:
        method = function(rm, name)
        assert method.index('lock2(m_presentlock)') < method.index('InvalidateReservations();')
    output = function(vp, 'CVideoPlayerVideo::EOutputState CVideoPlayerVideo::OutputPicture(')
    assert output.index('WaitForBuffer(reservation,') < output.index('auto overlays = ProcessOverlays(')
    assert output.index('auto overlays = ProcessOverlays(') < output.index('AddVideoPicture(reservation,')
    assert 'void CRenderManager::AddOverlay(' not in rm

    declaration = function(header, 'class BufferReservation') + ';'
    element = function(oh, 'struct SElement') + ';'
    source = PRELUDE.replace('@ELEMENT@', element).replace('@RESERVATION@', declaration)
    source += '\n'.join(function(rm, name) for name in [
        'CRenderManager::BufferReservation::~BufferReservation()',
        'CRenderManager::BufferReservation::BufferReservation(',
        'CRenderManager::BufferReservation& CRenderManager::BufferReservation::operator=(',
        'void CRenderManager::ReserveBuffer(', 'void CRenderManager::CancelReservation(',
        'void CRenderManager::InvalidateReservations()', 'void CRenderManager::RetireBuffer(',
        'bool CRenderManager::AddVideoPicture(', 'int CRenderManager::WaitForBuffer(',
        'void CRenderManager::DiscardBuffer()', 'void CRenderManager::ProcessPresentationQueue()',
        'bool CRenderManager::Flush('])
    source += '\n' + function(ov, 'void CRenderer::SetOverlays(')
    source += '\n' + function(ov, 'void CRenderer::Release(std::vector<SElement>&')
    source += '\n' + function(ov, 'void CRenderer::Release(int idx)')
    source += '\n' + function(gles, 'void CLinuxRendererGLES::AddVideoPicture(')
    source += '\n' + function(gles, 'void CLinuxRendererGLES::ReleaseBuffer(')
    source += '\n' + function(vp, 'OVERLAY::CRenderer::OverlayBatch CVideoPlayerVideo::ProcessOverlays(')
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='render-slot-test-') as temporary:
        out = Path(temporary)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                        '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Slot publication: PASS (production methods, ASan/UBSan; host stubs)')


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
constexpr int NUM_BUFFERS=5, LOGWARNING=1, LOGERROR=2, LOGDEBUG=3;
constexpr int DVP_FLAG_INTERLACED=1, DVP_FLAG_TOP_FIELD_FIRST=2;
constexpr int TMSG_RENDERER_FLUSH=1;
enum EINTERLACEMETHOD {VS_INTERLACEMETHOD_NONE, VS_INTERLACEMETHOD_RENDER_BLEND,
                      VS_INTERLACEMETHOD_RENDER_BOB, OTHER_METHOD};
struct CLog {template<class... T> static void Log(T&&...) {} template<class... T> static void LogF(T&&...) {}};
long long nowMs=0;
namespace XbmcThreads {
template<class = void> struct EndTime {
  long long end=0;
  EndTime()=default;
  explicit EndTime(std::chrono::milliseconds d) {Set(d);}
  void Set(std::chrono::milliseconds d) {end=nowMs+d.count();}
  bool IsTimePast() const {return nowMs>=end;}
};
}
struct Condition {
  std::function<void()> hook;
  std::vector<std::chrono::milliseconds> waits;
  int notifications=0;
  void notifyAll() {++notifications;}
  void wait(std::unique_lock<CCriticalSection>& lock,std::chrono::milliseconds duration) {
    waits.push_back(duration);nowMs+=duration.count();lock.unlock();
    if(hook)hook();
    lock.lock();
  }
};
struct Event {bool signaled=false;void Reset(){signaled=false;}void Set(){signaled=true;}
  bool Wait(std::chrono::milliseconds){return signaled;}};
struct CSingleExit {template<class T> explicit CSingleExit(T&) {}};
struct App {bool active=true;bool GetRenderGUI() const{return active;}} g_application;
struct Messenger {bool main=true;int posts=0;bool IsProcessThread(){return main;}void PostMsg(int){++posts;}};
struct Window {int gfx=0;int& GetGfxContext(){return gfx;}};
struct CServiceBroker {
  static Messenger* GetAppMessenger(){static Messenger m;return &m;}
  static Window* GetWinSystem(){static Window w;return &w;}
};
struct Clock {double GetClock() const{return 1.0;}};
struct RefBuffer {int refs=1;void Acquire(){++refs;}void Release(){assert(refs>0);--refs;}};
struct Light {int MaxCLL=0;};
struct VideoPicture {
  double pts=100;int iFlags=0,m_3dSubtitleDepth=7;
  RefBuffer* videoBuffer=nullptr;
  int color_primaries=1,color_space=2,color_transfer=3,color_range=1,colorBits=10;
  bool hasDisplayMetadata=false,hasLightMetadata=false;
  int displayMetadata=0;Light lightMetadata;
};
struct CLinuxRendererGLES {
  struct CPictureBuffer {
    RefBuffer* videoBuffer=nullptr;bool loaded=true;
    int m_srcPrimaries=0,m_srcColSpace=0,m_srcColTransfer=0,m_srcBits=0;
    bool m_srcFullRange=false,hasDisplayMetadata=false,hasLightMetadata=false;
    int displayMetadata=0;Light lightMetadata;
  };
  CPictureBuffer m_buffers[NUM_BUFFERS];
  void AddVideoPicture(const VideoPicture&,int);
  void ReleaseBuffer(int);
};
const auto mainThread = std::this_thread::get_id();
struct Renderer : CLinuxRendererGLES {
  bool saveResult=false,need=false,doublePass=false,clearOnSave=false;
  bool lastSaveRequest=false;int adds=0,releases=0;
  std::function<void(int)> onAdd,onRelease;
  void AddVideoPicture(const VideoPicture& p,int i) {
    ++adds;CLinuxRendererGLES::AddVideoPicture(p,i);if(onAdd)onAdd(i);
  }
  void ReleaseBuffer(int i) {
    assert(std::this_thread::get_id()==mainThread);
    if(onRelease)onRelease(i);
    ++releases;CLinuxRendererGLES::ReleaseBuffer(i);
  }
  bool NeedBuffer(int) {assert(std::this_thread::get_id()==mainThread);return need;}
  bool WantsDoublePass() {return doublePass;}
  bool Flush(bool save) {
    assert(std::this_thread::get_id()==mainThread);lastSaveRequest=save;
    if(!saveResult || clearOnSave) for(int i=0;i<NUM_BUFFERS;++i)ReleaseBuffer(i);
    return saveResult;
  }
};
namespace OVERLAY {
class CRenderer {
public:
  @ELEMENT@
  using OverlayBatch=std::vector<SElement>;
  CCriticalSection m_section;
  std::vector<SElement> m_buffers[NUM_BUFFERS];
  void SetOverlays(OverlayBatch,int);
  void Release(std::vector<SElement>&);
  void Release(int);
  void Flush(){for(int i=0;i<NUM_BUFFERS;++i)Release(i);}
};
}
using namespace OVERLAY;
class CRenderManager {
public:
  @RESERVATION@
  enum EPRESENTSTEP {PRESENT_IDLE,PRESENT_FLIP,PRESENT_FRAME,PRESENT_FRAME2,PRESENT_READY};
  enum EFIELDSYNC {FS_NONE,FS_TOP,FS_BOT};
  enum EPRESENTMETHOD {PRESENT_METHOD_SINGLE,PRESENT_METHOD_BLEND,PRESENT_METHOD_BOB};
  struct SPresent {double pts=0;EFIELDSYNC presentfield=FS_NONE;EPRESENTMETHOD presentmethod=PRESENT_METHOD_SINGLE;};
  SPresent m_Queue[NUM_BUFFERS];
  std::array<uint64_t,NUM_BUFFERS> m_reservations{};
  uint64_t m_nextReservation=0,m_reservationEpoch=0;
  CCriticalSection m_statelock,m_presentlock,m_datalock;
  std::deque<int> m_free,m_queued,m_discard;
  Renderer renderer;Renderer* m_pRenderer=&renderer;
  OVERLAY::CRenderer m_overlays,m_debugRenderer;
  Clock m_dvdClock;Condition m_presentevent;Event m_flushEvent;
  XbmcThreads::EndTime<> m_presentTimer;
  EPRESENTSTEP m_presentstep=PRESENT_IDLE;
  bool m_bRenderGUI=true,m_forceNext=false,m_presentstarted=false;
  int m_QueueSize=NUM_BUFFERS,m_presentsource=0,m_presentsourcePast=-1;
  CRenderManager(){for(int i=0;i<NUM_BUFFERS;++i)m_free.push_back(i);}
  ~CRenderManager(){for(int i=0;i<NUM_BUFFERS;++i)renderer.ReleaseBuffer(i);}
  void ReserveBuffer(BufferReservation&);
  void CancelReservation(BufferReservation&);
  void InvalidateReservations();
  void RetireBuffer(int);
  bool AddVideoPicture(BufferReservation&,const VideoPicture&,OVERLAY::CRenderer::OverlayBatch,
                       volatile std::atomic_bool&,EINTERLACEMETHOD,bool);
  int WaitForBuffer(BufferReservation&,volatile std::atomic_bool&,std::chrono::milliseconds=100ms);
  void DiscardBuffer();
  void ProcessPresentationQueue();
  bool Flush(bool,bool);
  void PrepareNextRender() {} // scheduler formulas are unchanged and outside this fixture
};
struct CDVDOverlayLibass : CDVDOverlay {
  CDVDOverlayLibass():CDVDOverlay(DVDOVERLAY_TYPE_TEXT){}
  CDVDOverlayLibass* GetLibassHandler(){return this;}
  bool EventActive(double) const{return true;}
};
struct OverlayContainer : CCriticalSection {
  VecOverlays items;
  void CleanUp(double) {} // event delivery/container expiry covered by BD navigation harness
  VecOverlays* GetOverlays(){return &items;}
};
struct IDVDStreamPlayer {enum {SYNC_INSYNC};};
struct CVideoPlayerVideo {
  double m_iSubtitleDelay=0;bool m_bRenderSubs=false;
  int m_syncState=IDVDStreamPlayer::SYNC_INSYNC;
  OverlayContainer container;OverlayContainer* m_pOverlayContainer=&container;
  OVERLAY::CRenderer::OverlayBatch ProcessOverlays(const VideoPicture*,double);
};
'''

TESTS = r'''
using Reservation=CRenderManager::BufferReservation;
using Batch=OVERLAY::CRenderer::OverlayBatch;
VideoPicture picture(RefBuffer& ref,double pts=100) {VideoPicture p;p.videoBuffer=&ref;p.pts=pts;return p;}
bool submit(CRenderManager& r,Reservation& slot,const VideoPicture& p,Batch batch={},bool wait=false) {
  static std::atomic_bool stop=false;
  return r.AddVideoPicture(slot,p,std::move(batch),stop,VS_INTERLACEMETHOD_NONE,wait);
}
std::shared_ptr<CDVDOverlayImage> menu(bool bdj,uint32_t color) {
  auto image=std::make_shared<CDVDOverlayImage>();image->SetDiscMenuOverlay(true);
  image->m_isPqMenuGraphics=true;image->m_isHdrPq=true;
  image->width=image->height=image->source_width=image->source_height=1;
  if(bdj) {image->linesize=4;image->pixels.resize(4);memcpy(image->pixels.data(),&color,4);}
  else {image->linesize=1;image->pixels={0};image->palette={color};image->pqMenuPalette={color};}
  image->m_menuVisible=image->HasVisiblePixels();return image;
}
int main() {
  std::atomic_bool stop=false;
  // Cancel/move/assignment only return unpublished capacity; no renderer callback.
  { CRenderManager r;int free=r.m_free.size();
    {Reservation a;r.WaitForBuffer(a,stop);Reservation b(std::move(a));
     Reservation c;r.WaitForBuffer(c,stop);c=std::move(b);auto& alias=c;c=std::move(alias);
     assert(r.m_free.size()==static_cast<size_t>(free-1));}
    assert(r.m_free.size()==static_cast<size_t>(free));assert(r.renderer.adds==0&&r.renderer.releases==0);
    {Reservation a;r.WaitForBuffer(a,stop);std::thread producer([token=std::move(a)](){});producer.join();}
    assert(r.m_free.size()==NUM_BUFFERS&&r.renderer.releases==0);
  }
  // A stale token cannot publish or cancel a new reservation of the same slot.
  {RefBuffer ref;CRenderManager r;auto p=picture(ref);Reservation old;
   r.WaitForBuffer(old,stop);r.InvalidateReservations();Reservation fresh;r.WaitForBuffer(fresh,stop);
   assert(!submit(r,old,p));old=Reservation{};assert(r.m_free.size()==NUM_BUFFERS-1);
   assert(submit(r,fresh,p));assert(!submit(r,fresh,p));assert(r.renderer.adds==1);}
  // Destructive flush cancels reservations; saved-buffer flush retains actual queue membership.
  for(bool request:{false,true})for(bool result:{false,true}) {
    RefBuffer ref;CRenderManager r;Reservation published;r.WaitForBuffer(published,stop);
    auto p=picture(ref);assert(submit(r,published,p,{{12,menu(false,0xff123456)}}));
    Reservation old;r.WaitForBuffer(old,stop);r.m_presentstarted=true;r.m_presentsource=0;
    r.m_presentsourcePast=3;r.m_discard.push_back(4);r.m_free.erase(std::find(r.m_free.begin(),r.m_free.end(),4));
    const auto queued=r.m_queued;const auto discarded=r.m_discard;r.renderer.saveResult=result;
    assert(r.Flush(true,request));assert(r.renderer.lastSaveRequest==request);
    assert(!submit(r,old,p));old=Reservation{};
    for(auto& b:r.m_overlays.m_buffers)assert(b.empty());
    if(result) {assert(r.m_queued==queued&&r.m_discard==discarded&&r.m_presentstarted&&r.m_presentsourcePast==3);assert(ref.refs==2);}
    else {assert(r.m_queued.empty()&&r.m_discard.empty()&&!r.m_presentstarted&&r.m_presentsourcePast==-1);assert(ref.refs==1);assert(r.m_free.size()==NUM_BUFFERS);}
  }
  // Preserve AML's distinct Flush(true) behavior: buffers reset but queue retained.
  {RefBuffer ref;CRenderManager r;Reservation s;r.WaitForBuffer(s,stop);assert(submit(r,s,picture(ref)));
   r.renderer.saveResult=true;r.renderer.clearOnSave=true;assert(r.Flush(true,true));
   assert(ref.refs==1&&r.m_queued.size()==1);}
  // GUI inactive never waits for capacity; only the existing bounded pacing wait.
  for(bool freeSlot:{false,true}) {
    RefBuffer ref;CRenderManager r;g_application.active=false;
    if(!freeSlot)r.m_free.clear();
    Reservation s;
    assert(r.WaitForBuffer(s,stop,500ms)==0);assert(r.m_presentevent.waits.size()==1);
    assert(r.m_presentevent.waits[0]<=20ms);assert(submit(r,s,picture(ref))==freeSlot);
    g_application.active=true;
  }
  // A full queue timeout/abort does not acquire a slot; wakeup invalidation cannot
  // admit an old output request into a newly configured/flushed queue.
  {RefBuffer ref;CRenderManager r;r.m_free.clear();Reservation s;
   auto start=nowMs;assert(r.WaitForBuffer(s,stop,100ms)==-1);assert(nowMs-start==100);
   stop=true;assert(r.WaitForBuffer(s,stop,100ms)==-1);stop=false;
   r.m_presentevent.hook=[&]{r.InvalidateReservations();r.m_free={0};};
   assert(r.WaitForBuffer(s,stop)==-1);assert(!submit(r,s,picture(ref)));assert(r.m_free.size()==1);}
  {RefBuffer ref;CRenderManager r;Reservation s;g_application.active=false;
   r.m_presentevent.hook=[&]{r.InvalidateReservations();r.m_queued={4};};
   assert(r.WaitForBuffer(s,stop)==0);assert(!submit(r,s,picture(ref)));assert(r.m_queued==std::deque<int>{4});
   g_application.active=true;}
  // Publication is visible atomically to a consumer taking the presentation lock.
  {RefBuffer ref;CRenderManager r;Reservation s;r.WaitForBuffer(s,stop);auto image=menu(true,0xffabcdef);auto second=menu(false,0xff654321);
   std::thread consumer;
   r.renderer.onAdd=[&](int idx){consumer=std::thread([&,idx]{std::unique_lock<CCriticalSection> lock(r.m_presentlock);
     assert(r.m_queued==std::deque<int>{idx});assert(r.m_Queue[idx].pts==321);
     assert(r.m_overlays.m_buffers[idx].size()==2&&r.m_overlays.m_buffers[idx][0].overlay_dvd==image);
     assert(r.m_overlays.m_buffers[idx][1].overlay_dvd==second&&r.m_overlays.m_buffers[idx][1].pts==318);});};
   assert(submit(r,s,picture(ref,321),{{319,image},{318,second}}));consumer.join();}
  // Startup publication is accepted before waiting: forced presentation, timeout
  // and abort afterward all keep the accepted frame and clear m_forceNext.
  for(int outcome=0;outcome<4;++outcome) {
    RefBuffer ref;CRenderManager r;Reservation s;r.WaitForBuffer(s,stop);auto start=nowMs;
    r.m_presentevent.hook=[&]{assert(r.renderer.adds==1&&r.m_queued.size()==1&&r.m_forceNext);
      if(outcome==0)r.m_presentstep=CRenderManager::PRESENT_FRAME;
      if(outcome==1)stop=true;};
    if(outcome==3)stop=true; // Existing AddVideoPicture accepts then observes abort in its wait.
    assert(r.AddVideoPicture(s,picture(ref),{},stop,VS_INTERLACEMETHOD_NONE,true));
    assert(!r.m_forceNext&&r.m_queued.size()==1&&nowMs-start<=200);stop=false;
  }
  // Software/GLES buffers are acquired once; retained NeedBuffer slots are not
  // returned early. GUI-inactive override still retires video before overlays/free.
  {RefBuffer ref;CRenderManager r;Reservation s;r.WaitForBuffer(s,stop);auto p=picture(ref);
   p.iFlags=DVP_FLAG_INTERLACED|DVP_FLAG_TOP_FIELD_FIRST;
   assert(r.AddVideoPicture(s,p,{{100,menu(false,0xff101010)}},stop,VS_INTERLACEMETHOD_RENDER_BOB,false));
   assert(ref.refs==2&&!r.renderer.m_buffers[0].loaded);
   assert(r.m_Queue[0].presentmethod==CRenderManager::PRESENT_METHOD_BOB&&r.m_Queue[0].presentfield==CRenderManager::FS_TOP);
   r.DiscardBuffer();r.renderer.need=true;r.ProcessPresentationQueue();assert(r.m_discard.size()==1&&ref.refs==2);
   r.renderer.onRelease=[&](int i){assert(std::find(r.m_free.begin(),r.m_free.end(),i)==r.m_free.end());
     assert(!r.m_overlays.m_buffers[i].empty());};
   r.m_bRenderGUI=false;r.ProcessPresentationQueue();r.renderer.onRelease={};
   assert(r.m_discard.empty()&&ref.refs==1&&r.m_overlays.m_buffers[0].empty());
   assert(r.m_free.size()==NUM_BUFFERS);}
  // Repeated PTS and the initial source index do not substitute for reservation
  // identity. Retire/reuse a slot many times without duplicate free-list entries.
  {RefBuffer ref;CRenderManager r;
   for(int i=0;i<30;++i){Reservation s;r.WaitForBuffer(s,stop);assert(submit(r,s,picture(ref,777)));
     r.DiscardBuffer();r.ProcessPresentationQueue();auto free=r.m_free;std::sort(free.begin(),free.end());
     assert(std::adjacent_find(free.begin(),free.end())==free.end());assert(ref.refs==1);}}
  // HDMV indexed palettes and BD-J ARGB canvases: persistent menu batches keep
  // their own picture/PTS, replacement does not mutate an already published frame,
  // and an empty batch for clear/hide cannot inherit the previous slot's menu.
  for(bool bdj:{false,true}) {
    RefBuffer ref;CRenderManager r;CVideoPlayerVideo v;auto p=picture(ref,-500000);
    auto image=menu(bdj,0xff123456);auto group=std::make_shared<CDVDOverlayGroup>();group->SetDiscMenuOverlay(true);
    group->iPTSStartTime=1000;group->iPTSStopTime=1001;group->m_overlays={image};v.container.items={group};
    auto batch=v.ProcessOverlays(&p,p.pts);assert(batch.size()==1&&batch[0].overlay_dvd==image);
    assert(v.ProcessOverlays(&p,9999999).size()==1);assert(group->m_3dSubtitleDepth==0);
    Reservation first;r.WaitForBuffer(first,stop);assert(submit(r,first,p,batch));int a=r.m_queued.back();
    auto replacement=std::static_pointer_cast<CDVDOverlayImage>(image->Clone());
    if(bdj){uint32_t c=0xffabcdef;memcpy(replacement->pixels.data(),&c,4);}else{replacement->palette[0]=0xffabcdef;replacement->pqMenuPalette[0]=0xffabcdef;}
    group->m_overlays={replacement};p.pts=200;Reservation second;r.WaitForBuffer(second,stop);
    assert(submit(r,second,p,v.ProcessOverlays(&p,p.pts)));int b=r.m_queued.back();
    assert(a!=b&&r.m_Queue[a].pts==-500000&&r.m_Queue[b].pts==200);
    assert(r.m_overlays.m_buffers[a][0].overlay_dvd==image&&r.m_overlays.m_buffers[b][0].overlay_dvd==replacement);
    assert(image->pixels!=replacement->pixels||image->palette!=replacement->palette);
    // Flush/reconfigure cancellation with live old menu slots must not install
    // a batch on any other slot, nor mutate a retained picture's menu reference.
    Reservation stale;r.WaitForBuffer(stale,stop);r.InvalidateReservations();
    Reservation fresh;r.WaitForBuffer(fresh,stop);assert(!submit(r,stale,p,batch));
    stale=Reservation{};assert(r.m_overlays.m_buffers[a][0].overlay_dvd==image);
    assert(submit(r,fresh,p,{}));assert(r.m_overlays.m_buffers[r.m_queued.back()].empty());
    group->m_overlays.clear();auto cleared=v.ProcessOverlays(&p,201);assert(cleared.empty());
    r.DiscardBuffer();r.ProcessPresentationQueue();Reservation clear;r.WaitForBuffer(clear,stop);
    assert(submit(r,clear,p,cleared));assert(r.m_overlays.m_buffers[r.m_queued.back()].empty());
    v.container.items.clear();assert(v.ProcessOverlays(&p,202).empty()); // HIDE's empty composition
    for(bool configure:{false,true}) {Reservation old;r.WaitForBuffer(old,stop);
      if(configure)r.InvalidateReservations();else r.Flush(true,false);
      assert(!submit(r,old,p,batch));for(auto& e:r.m_overlays.m_buffers)assert(e.empty());}
    auto transparent=menu(bdj,0);assert(!transparent->m_menuVisible); // no false PQ activation
  }
  // Ordinary/forced overlay timestamps and empty batches preserve selection policy.
  {CVideoPlayerVideo v;VideoPicture p;v.m_bRenderSubs=true;v.m_iSubtitleDelay=10;
   auto sub=std::make_shared<CDVDOverlayImage>();sub->iPTSStartTime=100;sub->iPTSStopTime=200;v.container.items={sub};
   assert(v.ProcessOverlays(&p,109).empty());auto b=v.ProcessOverlays(&p,110);assert(b.size()==1&&b[0].pts==100);
   sub->bForced=true;b=v.ProcessOverlays(&p,110);assert(b.size()==1&&b[0].pts==110);}
}
'''

if __name__ == '__main__':
    main()
