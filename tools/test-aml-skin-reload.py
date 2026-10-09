#!/usr/bin/env python3
"""Execute skin reload application guards and AML restoration with native/GUI stubs.

Runs production application Begin/Finish, renderer Flush, render-manager stop,
flush/restore and update, presenter/session and native buffer consumption. Actual
LoadSkin resource initialization/window activation is checked for wiring only.
No real skin, EGL, decoder, device or image-build verification is claimed.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-aml-presenter.py'))
function = fixture['extract']


def main(negative_controls=False):
    base = ROOT / 'xbmc/cores/VideoPlayer'
    buffer_header = (base / 'DVDCodecs/Video/DVDVideoCodecAmlogic.h').read_text()
    buffer_source = (base / 'DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    adapter = (base / 'VideoRenderers/HwDecRender/AMLPresenterSession.h').read_text()
    renderer = (base / 'VideoRenderers/HwDecRender/RendererAML.cpp').read_text()
    renderer_header = (base / 'VideoRenderers/HwDecRender/RendererAML.h').read_text()
    manager = (base / 'VideoRenderers/RenderManager.cpp').read_text()
    app = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    app_header = (ROOT / 'xbmc/application/ApplicationPlayer.h').read_text()
    skin = function((ROOT / 'xbmc/application/ApplicationSkinHandling.cpp').read_text(),
                    'bool CApplicationSkinHandling::LoadSkin(')
    # Wiring coverage only: failed loads use cancellation; successful completion
    # follows resource/window initialization. Each stale window restore is gated.
    assert skin.index('BeginSkinReload(reload)') < skin.index('UnloadSkin();')
    assert skin.index('FinishSkinReload(reload, false)') < skin.index('UnloadSkin();')
    assert skin.index('FinishSkinReload(reload, true)') > skin.index('// restore player')
    window = skin[skin.index('// restore active window'):skin.index('// restore player')]
    assert 'IsSkinReloadCurrent(reload)' in window
    assert 'CScopeGuard<int, 0, void(int)>' in skin
    player = (base / 'VideoPlayer.cpp').read_text()
    assert 'm_renderManager.FlushForSkinReload(restore)' in function(
        player, 'bool CVideoPlayer::FlushRendererForSkinReload(')
    prelude = fixture['PRELUDE'].replace('LOGDEBUG=0, LOGINFO=1', 'LOGDEBUG=0, LOGINFO=1, LOGERROR=2')
    prelude = prelude.replace('std::shared_ptr<const void> lifetime;', '''std::shared_ptr<const void> lifetime;
  bool refuseOwner=false;
  std::mutex eventsMutex;std::vector<std::pair<uint32_t,bool>> events;''')
    prelude = prelude.replace('RequestPresentationOwner(std::thread::id owner) {return session.RequestOwner(owner);}', 'RequestPresentationOwner(std::thread::id owner) {if(refuseOwner)return {};return session.RequestOwner(owner);}')
    prelude = prelude.replace('int ReleaseFrame(uint32_t,uint64_t,', 'int ReleaseFrame(uint32_t index,uint64_t,')
    prelude = prelude.replace('if(drop) ++drops;', '{std::lock_guard<std::mutex> lock(eventsMutex);events.emplace_back(index,drop);}\n    if(drop) ++drops;')
    prelude += '#include <array>\n#include <optional>\n#include "utils/ScopeGuard.h"\n#include "cores/VideoPlayer/VideoRenderers/RenderLifecycle.h"\n'
    code = prelude + function(buffer_header, 'class CAMLVideoBuffer :') + ';\n'
    for signature in ['void CAMLVideoBuffer::Set(', 'CAMLSession::Permit CAMLVideoBuffer::AcquirePresentation()',
                      'void CAMLVideoBuffer::Commit(', 'bool CAMLVideoBuffer::Submit(',
                      'void CAMLVideoBuffer::ApplyGeometry(', 'bool CAMLVideoBuffer::Poll(',
                      'bool CAMLVideoBuffer::Drop()']:
        code += function(buffer_source, signature) + '\n'
    picture = fixture['PICTURE'].replace('int iFlags{0};', 'int iFlags{0}; std::string stereoMode;')
    picture = picture.replace('void CopyRef(', 'void SetParams(const VideoPicture& other) {pts=other.pts; stereoMode=other.stereoMode;}\n  void CopyRef(')
    picture = picture.replace('using OverlayBatch=std::vector<int>;', '''using OverlayBatch=std::vector<int>;
      std::array<OverlayBatch,8> slots;
      void SetOverlays(OverlayBatch v,int n){slots[n]=std::move(v);}
      OverlayBatch GetOverlays(int n){return slots[n];}
      void Flush(){for(auto& s:slots)s.clear();}''')
    picture = picture.replace('double GetClockSpeed() const {return 1;}', 'std::atomic<double> speed{1}; double GetClockSpeed() const {return speed;}')
    code += picture + function(adapter, 'class CAMLPresenterSession\n') + ';\n'
    code += BOUNDARIES + RENDERER
    for signature in ['void ResumeIndependentPresentation(', 'std::shared_ptr<CAMLCodec> PresenterCodec()',
                      'CVideoBuffer* PresentationBuffer(', 'int PreviousPts()']:
        # These are inline methods in the production renderer header.
        code = code.replace('@'+signature.split('(')[0].split()[-1]+'@', function(renderer_header, signature))
    for signature in ['void CRendererAML::AddVideoPicture(', 'void CRendererAML::ReleaseBuffer(',
                      'void CRendererAML::Reset()', 'bool CRendererAML::Flush(',
                      'void CRendererAML::ReleasePresentationReferences()']:
        code += function(renderer, signature) + '\n'
    code += MANAGER
    for signature in ['bool CRenderManager::FlushOnMain(', 'bool CRenderManager::FlushForSkinReload(',
                      'void CRenderManager::StopAMLPresenter(', 'void CRenderManager::RestoreAMLPresenter(',
                      'bool CRenderManager::UpdateAMLPresenter()']:
        code += function(manager, signature) + '\n'
    code += APP.replace('@STATE@', function(app_header, 'struct SkinReloadState') + ';')
    for signature in ['bool CApplicationPlayer::IsSkinReloadCurrent(', 'bool CApplicationPlayer::BeginSkinReload(',
                      'void CApplicationPlayer::FinishSkinReload(']:
        code += function(app, signature) + '\n'
    code += TESTS
    with tempfile.TemporaryDirectory(prefix='aml-skin-reload-') as name:
        out = Path(name)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-DHAS_LIBAMCODEC=1', '-pthread',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True, timeout=30)
        if negative_controls:
            mutations = {
                'lying-save-buffers': ('if (!saveBuffers)\n    Reset();', 'Reset();'),
                'held-selection-skipped': ('bool advance = !m_current;', 'bool advance = true;'),
                'stale-player-restored': ('state.generation == m_openGeneration &&', 'true &&'),
                'stale-renderer-restored': ('flush != m_flushGeneration ||', 'false ||'),
            }
            for label, (before, after) in mutations.items():
                original_header = ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/AMLPresenter.h'
                mutated_code = code
                include = ROOT / 'xbmc'
                if label == 'held-selection-skipped':
                    target = out / 'include/cores/VideoPlayer/VideoRenderers/AMLPresenter.h'
                    target.parent.mkdir(parents=True, exist_ok=True)
                    text = original_header.read_text()
                    assert text.count(before) == 1
                    target.write_text(text.replace(before, after))
                    include = out / 'include'
                else:
                    assert code.count(before) == 1
                    mutated_code = code.replace(before, after)
                (out / 'negative.cpp').write_text(mutated_code)
                subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                                '-Werror', '-Wno-unused-parameter', '-DHAS_LIBAMCODEC=1', '-pthread',
                                '-I', str(include), '-I', str(ROOT / 'xbmc'),
                                str(out / 'negative.cpp'), '-o', str(out / 'negative')], check=True)
                failed = subprocess.run([str(out / 'negative')], capture_output=True, timeout=30)
                assert failed.returncode != 0, f'negative control survived: {label}'
                print(f'negative control rejected: {label}')
                if label == 'held-selection-skipped':
                    target.unlink()
    print('AML skin reload: PASS (production guards/flush/restore/queue/buffers; ASan/UBSan; '
          'native/clock/GUI stubs; LoadSkin wiring only)')


BOUNDARIES = r'''
using CCriticalSection=std::recursive_mutex;
constexpr int RENDER_STEREO_MODE_OFF=0;
struct Graphics:std::recursive_mutex {
  bool pq=true;int stereo=0;
  void SetTransferPQ(bool value){pq=value;}
  int GetStereoMode()const{return stereo;}
  float GetFPS()const{return 1000;}
};
struct CWinSystemBase {
  enum class NativeGuiWaitResult{READY,PENDING,FAILED};
  Graphics graphics;
  bool failWait=false,pendingWait=false;
  int starts=0,stops=0;
  Graphics& GetGfxContext(){return graphics;}
  NativeGuiWaitResult SetNativeGuiWait(bool wait){
    if(wait){++starts;return failWait?NativeGuiWaitResult::FAILED:pendingWait?NativeGuiWaitResult::PENDING:NativeGuiWaitResult::READY;}
    ++stops;return NativeGuiWaitResult::READY;
  }
};
struct CServiceBroker {
  static inline CWinSystemBase system;
  static CWinSystemBase* GetWinSystem(){return &system;}
};
struct CSingleExit{explicit CSingleExit(Graphics&) {}}; // recording GUI boundary; no real graphics lock
struct CBaseRenderer {
  virtual ~CBaseRenderer()=default;
  virtual bool Flush(bool)=0;
  virtual void AddVideoPicture(const VideoPicture&,int)=0;
};
'''
RENDERER = r'''
class CRendererAML:public CBaseRenderer {
public:
  static constexpr int m_numRenderBuffers=8;
  struct BUFFER{CVideoBuffer* videoBuffer=nullptr;} m_buffers[m_numRenderBuffers];
  std::shared_ptr<CAMLCodec> m_pollCodec;
  uint64_t m_pollEpoch=0;
  int m_prevVPts=-1;
  bool m_resumeControl=false;
  @ResumeIndependentPresentation@
  @PresenterCodec@
  @PresentationBuffer@
  @PreviousPts@
  uint64_t PrepareIndependentControl(CRect&,CRect&){return 1;}
  void Reset();
  bool Flush(bool) override;
  void ReleaseBuffer(int);
  void AddVideoPicture(const VideoPicture&,int) override;
  void ReleasePresentationReferences();
};
'''
MANAGER = r'''
class CRenderManager {
public:
  enum EPRESENTMETHOD{PRESENT_METHOD_SINGLE,PRESENT_METHOD_BLEND,PRESENT_METHOD_BOB};
  enum State{STATE_UNCONFIGURED,STATE_CONFIGURED};
  enum Step{PRESENT_IDLE,PRESENT_READY};
  CCriticalSection m_statelock,m_presentlock,m_datalock;
  uint64_t m_lifecycleGeneration=1,m_flushGeneration=0;
  bool m_closing=false,m_amlIndependentPresenter=true,m_showVideo=true;
  State m_renderState=STATE_CONFIGURED;
  Step m_presentstep=PRESENT_IDLE;
  CRendererAML renderer;
  CBaseRenderer* m_pRenderer=&renderer;
  std::shared_ptr<CAMLPresenterSession> m_amlPresenter;
  std::shared_ptr<const void> m_processInfoLifetime=std::make_shared<int>(1);
  CDVDClock m_dvdClock;
  std::shared_ptr<CRenderLifecycle> m_lifecycle=CRenderLifecycle::Create();
  VideoPicture m_picture;
  int m_QueueSize=8,m_QueueSkip=0,m_presentsource=0,m_presentsourcePast=-1;
  bool m_presentstarted=false;
  double m_presentpts=0;
  float m_fps=25;
  int m_latencyTweak=0,m_audioLatencyTweak=0,m_videoDelay=0;
  struct {double pts=0;EFIELDSYNC presentfield=FS_NONE;EPRESENTMETHOD presentmethod=PRESENT_METHOD_SINGLE;} m_Queue[8];
  std::deque<int> m_free,m_queued,m_discard;
  OVERLAY::CRenderer m_overlays;
  struct {void Flush(){}} m_debugRenderer;
  struct {void Reset(){}} m_clockSync;
  struct {void notifyAll(){}} m_presentevent;
  struct {bool sync=false;void UpdateClockSync(bool v){sync=v;}} port;
  decltype(port)* m_playerPort=&port;
  struct {double pts=0;void SetRenderPts(double value){pts=value;}} m_dataCacheCore;
  int invalidations=0;
  void InvalidateReservations(){++invalidations;}
  void ClearFrameSelection(){}
  void CancelDeferredDV(){} // unrelated DV boundary
  void ProcessLifecycleRequests(){m_lifecycle->Process();}
  void LogAMLPresenter(const char*,bool=false){}
  bool FlushOnMain(bool);
  bool FlushForSkinReload(std::function<void()>&);
  void StopAMLPresenter(bool);
  void RestoreAMLPresenter(uint64_t,uint64_t,const std::shared_ptr<CAMLCodec>&,uint64_t);
  bool UpdateAMLPresenter();
  ~CRenderManager(){StopAMLPresenter(false);renderer.Flush(false);}
};
'''
APP = r'''
struct IPlayer {
  bool playing=true,video=true,paused=false,flushOk=true;
  int pauses=0,restores=0;
  CRenderManager* manager=nullptr;
  std::function<void()> onPause,onFlush,onRestore;
  bool IsPlaying()const{return playing;}
  bool HasVideo()const{return video;}
  void Pause(){paused=!paused;++pauses;if(manager)manager->m_dvdClock.speed=paused?0:1;if(onPause)onPause();}
  bool FlushRendererForSkinReload(std::function<void()>& restore){
    bool result=flushOk;
    if(result&&manager)result=manager->FlushForSkinReload(restore);
    else if(result)restore=[this]{++restores;if(onRestore)onRestore();};
    if(onFlush)onFlush();
    return result;
  }
};
struct CApplicationPlayer {
  @STATE@
  std::shared_ptr<IPlayer> m_pPlayer,m_closingPlayer;
  bool m_shutdown=false,pending=false;
  uint64_t m_openGeneration=1;
  std::shared_ptr<IPlayer> GetInternal()const{return m_pPlayer;}
  bool HasPendingOpen()const{return pending;}
  bool IsPausedPlayback()const{return m_pPlayer&&m_pPlayer->paused;}
  bool IsSkinReloadCurrent(const SkinReloadState&)const;
  bool BeginSkinReload(SkinReloadState&);
  void FinishSkinReload(SkinReloadState&,bool);
};
'''
TESTS = r'''
std::vector<std::pair<uint32_t,bool>> Events(const std::shared_ptr<CAMLCodec>& codec){
  std::lock_guard<std::mutex> lock(codec->eventsMutex);return codec->events;
}
template<class F>void Await(F ready){
  const auto end=std::chrono::steady_clock::now()+3s;
  while(!ready()){assert(std::chrono::steady_clock::now()<end);std::this_thread::sleep_for(1ms);}
}
void Put(CRenderManager& manager,const std::shared_ptr<CAMLCodec>& codec,int n,bool main=false){
  VideoPicture picture;picture.pts=manager.m_dvdClock.GetClock()+(main?1000000:0);
  auto* buffer=new CAMLVideoBuffer(n);buffer->Set(codec,n,40,n,1);picture.videoBuffer=buffer;
  if(main){
    assert(!manager.m_free.empty());int i=manager.m_free.front();manager.m_free.pop_front();
    manager.renderer.AddVideoPicture(picture,i);manager.m_Queue[i].pts=picture.pts;
    manager.m_overlays.SetOverlays({n},i);manager.m_queued.push_back(i);
  }else{
    auto content=std::make_shared<CAMLPresenterSession::OverlayObservation>();content->overlays={n};
    auto frame=std::make_shared<CAMLPresenterSession::Frame>(picture,content);
    auto slot=manager.m_amlPresenter->queue->Reserve(1s);assert(slot);
    assert(manager.m_amlPresenter->queue->Publish(slot,frame));
  }
}
void Start(CRenderManager& manager,const std::shared_ptr<CAMLCodec>& codec){
  CServiceBroker::system.graphics.pq=true;
  manager.renderer.m_pollCodec=codec;manager.renderer.m_pollEpoch=codec->GetOperationEpoch();
  manager.m_amlPresenter=std::make_shared<CAMLPresenterSession>(codec,manager.m_dvdClock,8,manager.m_processInfoLifetime);
  manager.m_amlPresenter->queue->Show(true);manager.UpdateAMLPresenter();
  Put(manager,codec,1);
  Await([&]{return bool(manager.m_amlPresenter->queue->PendingControl().frame);});
  // Deliberately stop between submission and main-control acknowledgement.
  assert(codec->releases==1&&codec->polls==0);
}
void Reloads(bool paused){
  auto codec=std::make_shared<CAMLCodec>();codec->pollPacing=0;CRenderManager manager;Start(manager,codec);
  CApplicationPlayer app;app.m_pPlayer=std::make_shared<IPlayer>();auto player=app.m_pPlayer;
  player->manager=&manager;player->paused=paused;manager.m_dvdClock.speed=paused?0:1;
  for(int pass=0;pass<3;++pass){
    CApplicationPlayer::SkinReloadState reload;
    auto old=manager.m_amlPresenter;
    assert(app.BeginSkinReload(reload));assert(!manager.m_amlPresenter&&codec->GetDiagnostics().mainOwner);
    assert(manager.m_presentstarted&&manager.renderer.PresentationBuffer(manager.m_presentsource));
    assert(manager.m_overlays.GetOverlays(manager.m_presentsource)==std::vector<int>{1});
    assert(CServiceBroker::system.graphics.pq); // Flush(true) did not reset PQ
    if(pass==0){for(int n=2;n<=5;++n)Put(manager,codec,n,true);}
    app.FinishSkinReload(reload,true);
    assert(manager.m_amlPresenter&&manager.m_amlPresenter!=old&&player->paused==paused);
    assert(player->pauses==(paused?0:(pass+1)*2));
    // The imported selection must request control before selecting an overdue FIFO.
    Await([&]{return bool(manager.m_amlPresenter->queue->PendingControl().frame);});
    auto request=manager.m_amlPresenter->queue->PendingControl();
    auto frame=std::static_pointer_cast<CAMLPresenterSession::Frame>(request.frame);
    assert(frame->buffer->m_bufferIndex==1&&frame->buffer->WasSubmitted());
    assert(codec->releases==1&&codec->drops==0);
    auto current=manager.m_amlPresenter;
    app.FinishSkinReload(reload,true);assert(manager.m_amlPresenter==current); // one-shot
    request={};frame.reset();old.reset();
  }
  manager.m_dvdClock.speed=0;
  assert(manager.m_amlPresenter->ApplyControl({},{}));
  Await([&]{return codec->polls>0;});
  assert(codec->releases==1&&codec->drops==0); // paused held frame survives repeated reloads
  manager.m_dvdClock.speed=1;
  // Clock age is below skip threshold; drain the imported FIFO in order.
  Await([&]{return codec->releases==5;});
  assert(codec->drops==0);
  const std::vector<std::pair<uint32_t,bool>> expected={{1,false},{2,false},{3,false},{4,false},{5,false}};
  assert(Events(codec)==expected); // exact FIFO native indices, no duplicate return
  assert(manager.m_amlPresenter->Stop());auto frames=manager.m_amlPresenter->queue->TakeFrames();frames.clear();
}
void AppCancellation(){
  for(bool paused:{false,true})for(int trigger=0;trigger<10;++trigger){
    CApplicationPlayer app;auto old=std::make_shared<IPlayer>();app.m_pPlayer=old;old->paused=paused;
    CApplicationPlayer::SkinReloadState reload;
    auto replace=[&]{app.m_pPlayer=std::make_shared<IPlayer>();++app.m_openGeneration;};
    if(trigger==0)old->onPause=replace;
    if(trigger==1)old->onFlush=replace;
    if(trigger==2)old->flushOk=false;
    bool began=app.BeginSkinReload(reload);
    if(trigger==3)replace();
    if(trigger==4)++app.m_openGeneration; // same IPlayer reused, including failed Open
    if(trigger==5)app.m_closingPlayer=old;
    if(trigger==6)app.m_shutdown=true;
    if(trigger==7)app.pending=true;
    if(trigger==8)old->playing=false; // Stop during skin work
    if(trigger==9)old->onRestore=replace;
    app.FinishSkinReload(reload,trigger!=2);
    assert(old->restores==((trigger==9 || (paused&&trigger==0))?1:0));
    if(app.m_pPlayer!=old)assert(app.m_pPlayer->pauses==0&&app.m_pPlayer->restores==0);
    if(trigger==2){assert(!began&&old->paused==paused);}
  }
  for(bool success:{false,true}){
    CApplicationPlayer app;app.m_pPlayer=std::make_shared<IPlayer>();CApplicationPlayer::SkinReloadState reload;
    assert(app.BeginSkinReload(reload));assert(!app.BeginSkinReload(reload));app.FinishSkinReload(reload,success);
    assert(!app.m_pPlayer->paused&&app.m_pPlayer->pauses==2&&app.m_pPlayer->restores==int(success));
    app.FinishSkinReload(reload,true);assert(app.m_pPlayer->pauses==2);
    assert(!app.BeginSkinReload(reload));
  }
  for(bool replace:{false,true}){
    CApplicationPlayer app;auto old=std::make_shared<IPlayer>();app.m_pPlayer=old;
    old->onRestore=[&]{if(replace){app.m_pPlayer=std::make_shared<IPlayer>();++app.m_openGeneration;}throw std::runtime_error("restore");};
    CApplicationPlayer::SkinReloadState reload;assert(app.BeginSkinReload(reload));
    bool threw=false;try{app.FinishSkinReload(reload,true);}catch(const std::runtime_error&){threw=true;}
    assert(threw&&reload.completed);
    if(replace)assert(app.m_pPlayer->pauses==0);else assert(!old->paused&&old->pauses==2);
    app.FinishSkinReload(reload,true);assert(old->restores==1);
  }
  CApplicationPlayer app;CApplicationPlayer::SkinReloadState reload;assert(!app.BeginSkinReload(reload));
  app.FinishSkinReload(reload,true); // no original player
}
void Fallbacks(){
  for(int failure=0;failure<10;++failure){
    auto codec=std::make_shared<CAMLCodec>();CRenderManager manager;Start(manager,codec);
    std::function<void()> restore;assert(manager.FlushForSkinReload(restore)&&restore);
    auto* held=manager.renderer.PresentationBuffer(manager.m_presentsource);
    if(failure==0)CServiceBroker::system.failWait=true;
    if(failure==1)manager.m_closing=true;
    if(failure==2)++manager.m_lifecycleGeneration;
    if(failure==3)manager.FlushOnMain(true); // intervening generic flush/reconfigure
    if(failure==4)CServiceBroker::system.graphics.stereo=1;
    if(failure==5)manager.m_picture.stereoMode="split_vertical";
    if(failure==6)manager.m_amlIndependentPresenter=false;
    if(failure==7)manager.renderer.m_pollCodec=std::make_shared<CAMLCodec>();
    if(failure==8){auto r=codec->session.Fence();assert(codec->session.BeginMutation(r));assert(codec->session.Complete(r,true));}
    if(failure==9)codec->refuseOwner=true;
    restore();assert(!manager.m_amlPresenter);
    codec->refuseOwner=false;
    if(failure!=8)assert(manager.renderer.PresentationBuffer(manager.m_presentsource)==held);
    CServiceBroker::system.failWait=false;CServiceBroker::system.graphics.stereo=0;
    restore={};
  }
  // A genuine pre-existing fallback cannot mint restoration eligibility.
  auto codec=std::make_shared<CAMLCodec>();CRenderManager manager;Start(manager,codec);
  CServiceBroker::system.failWait=true;assert(!manager.UpdateAMLPresenter());
  CServiceBroker::system.failWait=false;std::function<void()> restore;
  assert(manager.FlushForSkinReload(restore)&&!restore&&!manager.m_amlPresenter);
  manager.m_closing=true;assert(!manager.FlushForSkinReload(restore)&&!restore);
}
void HeldSelectionBeforeOverdueQueue(){
  auto codec=std::make_shared<CAMLCodec>();CRenderManager manager;Start(manager,codec);
  std::function<void()> restore;assert(manager.FlushForSkinReload(restore));
  for(int n=2;n<=5;++n){Put(manager,codec,n,true);manager.m_Queue[manager.m_queued.back()].pts=-1000000+n;}
  manager.m_dvdClock.speed=0;
  restore();
  Await([&]{return bool(manager.m_amlPresenter->queue->PendingControl().frame);});
  auto held=std::static_pointer_cast<CAMLPresenterSession::Frame>(manager.m_amlPresenter->queue->PendingControl().frame);
  assert(held->buffer->m_bufferIndex==1&&codec->releases==1&&codec->drops==0);
}
void PendingAndHidden(){
  for(bool hidden:{false,true}){
    auto codec=std::make_shared<CAMLCodec>();codec->pollPacing=0;CRenderManager manager;Start(manager,codec);
    std::function<void()> restore;assert(manager.FlushForSkinReload(restore));
    for(int n=2;n<=5;++n)Put(manager,codec,n,true);
    if(hidden)manager.m_showVideo=false;
    CServiceBroker::system.pendingWait=true;restore();
    assert(manager.m_amlPresenter&&manager.m_amlPresenter->queue->State()==CAMLPresenter::Phase::STARTING);
    assert(codec->GetDiagnostics().transferring&&codec->releases==1&&codec->drops==0&&codec->polls==0);
    CServiceBroker::system.pendingWait=false;assert(manager.UpdateAMLPresenter());
    Await([&]{return bool(manager.m_amlPresenter->queue->PendingControl().frame);});
    auto held=std::static_pointer_cast<CAMLPresenterSession::Frame>(manager.m_amlPresenter->queue->PendingControl().frame);
    assert(held->buffer->m_bufferIndex==1&&codec->releases==1);
    if(hidden){
      Await([&]{return codec->drops==4;});
      const std::vector<std::pair<uint32_t,bool>> expected={{1,false},{2,true},{3,true},{4,true},{5,true}};
      assert(Events(codec)==expected); // hiding obeys existing cancel policy, exactly once
    }else assert(codec->drops==0);
  }
}
void FailedSkinLoad(){
  for(bool paused:{false,true})for(bool throws:{false,true}){
    auto codec=std::make_shared<CAMLCodec>();CRenderManager manager;Start(manager,codec);
    CApplicationPlayer app;auto player=std::make_shared<IPlayer>();app.m_pPlayer=player;
    player->manager=&manager;player->paused=paused;manager.m_dvdClock.speed=paused?0:1;
    auto load=[&]{
      CApplicationPlayer::SkinReloadState reload;
      KODI::UTILS::CScopeGuard<int,0,void(int)> cancel([&](int){app.FinishSkinReload(reload,false);},1);
      assert(app.BeginSkinReload(reload));
      assert(codec->GetDiagnostics().mainOwner&&!manager.m_amlPresenter);
      if(throws)throw std::runtime_error("skin resources");
      return false; // missing Home.xml follows the same production scope cancellation
    };
    try{assert(!load());}catch(const std::runtime_error&){assert(throws);}
    assert(player->paused==paused&&player->pauses==(paused?0:2));
    assert(!manager.m_amlPresenter&&codec->releases==1&&codec->drops==0);
    assert(manager.renderer.PresentationBuffer(manager.m_presentsource));
  }
}
void FailedSubmission(){
  auto codec=std::make_shared<CAMLCodec>();codec->qbufResult=-1;CRenderManager manager;Start(manager,codec);
  std::function<void()> restore;assert(manager.FlushForSkinReload(restore));restore();
  Await([&]{return bool(manager.m_amlPresenter->queue->PendingControl().frame);});
  assert(codec->releases==1&&codec->drops==0);
  assert(manager.m_amlPresenter->ApplyControl({},{}));Await([&]{return codec->polls>0;});
  assert(codec->releases==1&&codec->drops==0);
}
int main(){
  AppCancellation();assert(buffers==0);
  Reloads(false);assert(buffers==0);
  Reloads(true);assert(buffers==0);
  Fallbacks();assert(buffers==0);
  HeldSelectionBeforeOverdueQueue();assert(buffers==0);
  PendingAndHidden();assert(buffers==0);
  FailedSkinLoad();assert(buffers==0);
  FailedSubmission();assert(buffers==0);
  std::cout<<"skin reload scenarios passed\n";
}
'''
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    main(parser.parse_args().negative_controls)
