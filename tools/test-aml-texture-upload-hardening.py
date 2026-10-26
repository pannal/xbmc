#!/usr/bin/env python3
"""Execute actual upload/release/publication methods with threaded host fixtures.

Extends the donor recording-EGL harness. GL upload/finish, image decoding and the
application's job-drain orchestration remain fixtures. The destructor fixture
records callback-lock ownership; the real CGLTexture deletion path is not run.
Unknown-release tests explicitly retire the recording driver's binding on its
own worker before cleanup; they do not validate Mali deferred destruction.
This is not GPU sharing, pixel, performance or whole-application acceptance.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('donor', HERE/'test-aml-texture-upload-context.py')
donor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(donor)
PUBLICATION = 'xbmc/GUILargeTextureManager.cpp'


def replace(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old,new,1)


MUTEX = r'''
class CCriticalSection
{
 std::recursive_mutex mutex;
 mutable std::mutex state;
 std::thread::id owner;
 unsigned depth=0;
public:
 void lock()
 {
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
  while (!mutex.try_lock())
  {
   if (std::chrono::steady_clock::now()>deadline)
     throw std::runtime_error("fixture: upload lock did not retire");
   std::this_thread::yield();
  }
  std::lock_guard<std::mutex> guard(state);
  owner=std::this_thread::get_id(); ++depth;
 }
 void unlock()
 {
  {
   std::lock_guard<std::mutex> guard(state);
   if (!depth || owner!=std::this_thread::get_id())
   {
    std::lock_guard<std::mutex> fixtureGuard(fixture::lock);
    fixture::Violation("mutex unlocked by a thread without ownership");
    return;
   }
   if (!--depth) owner={};
  }
  mutex.unlock();
 }
 bool HeldByCurrentThread() const
 { std::lock_guard<std::mutex> guard(state); return depth && owner==std::this_thread::get_id(); }
};
'''

EXTRA_EGL = r'''
EGLBoolean eglGetConfigAttrib(EGLDisplay,EGLConfig,EGLint attribute,EGLint* result)
{
 std::lock_guard<std::mutex> guard(fixture::lock);
 if (attribute!=EGL_SURFACE_TYPE || fixture::failConfigQuery) return EGL_FALSE;
 *result=fixture::configBits; return EGL_TRUE;
}
EGLBoolean eglReleaseThread()
{
 std::lock_guard<std::mutex> guard(fixture::lock);
 ++fixture::releaseCalls;
 fixture::releaseThread=std::this_thread::get_id();
 if (fixture::failRelease) { fixture::error=EGL_BAD_ACCESS; return EGL_FALSE; }
 fixture::current.erase(std::this_thread::get_id());
 fixture::currentSurface.erase(std::this_thread::get_id());
 return EGL_TRUE;
}
'''

PUBLICATION_CLASSES = r'''
struct CJob { virtual ~CJob()=default; };
struct CImageLoader : CJob { std::unique_ptr<CTexture> m_texture; };
struct CGUILargeTextureManager
{
 struct CLargeTexture
 {
  std::string path="art";
  std::shared_ptr<CTexture> texture,m_pendingTexture;
  bool m_asyncUpload=false,m_gpuDemand=false;
  unsigned m_uploadJobID=0;
  void SetTexture(std::shared_ptr<CTexture> value) { texture=std::move(value); }
  const std::string& GetPath() const { return path; }
 };
 using queueIterator=std::vector<std::pair<unsigned,CLargeTexture*>>::iterator;
 CCriticalSection m_listSection;
 std::vector<std::pair<unsigned,CLargeTexture*>> m_queued;
 std::vector<CLargeTexture*> m_allocated;
 std::unordered_map<std::string,CLargeTexture*> m_queuedLookup,m_allocatedLookup;
 // This suite tests the synchronous-publication branch after actual context
 // work. Demand-aware admission executes separately in test-image-upload-demand.
 void QueueUpload(CLargeTexture*) {}
 void OnJobComplete(unsigned,bool,CJob*);
};
'''

TESTS = r'''
template<class Predicate> static void Await(Predicate predicate,const char* what)
{
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 while (!predicate())
 {
  if (std::chrono::steady_clock::now()>deadline) throw std::runtime_error(what);
  std::this_thread::yield();
 }
}
static void Exceptions()
{
 for (bool sync : {false,true})
 {
  Reset({"EGL_KHR_surfaceless_context"},false);
  CWinSystemAmlogicGLESContext win;g_winSystem=&win;
  Check(win.m_pGLContext.CreateContext({}),"exception: create");
  CTexture texture;texture.throwLoad=!sync;texture.throwSync=sync;
  std::atomic<bool> caught{false},released{false};
  std::thread worker([&] {
   try { texture.LoadToGPUAsync(); } catch (const std::runtime_error&) { caught=true; }
   released=eglGetCurrentContext()==EGL_NO_CONTEXT;
   // Negative witnesses must retire their own binding before fixture cleanup.
   if (!released) win.m_pGLContext.UnbindTextureUploadContext();
  });worker.join();
  Check(caught && released,"exception: actual async method releases on throwing load/sync");
  win.m_pGLContext.DestroyContext();Violations("exception");
 }
}
static void Publication()
{
 for (bool success : {false,true})
 {
  CGUILargeTextureManager manager;CGUILargeTextureManager::CLargeTexture image;
  manager.m_queued.push_back({1,&image});manager.m_queuedLookup[image.path]=&image;
  CImageLoader job;job.m_texture=std::make_unique<CTexture>();
  bool underLock=false,destroyed=false;
  job.m_texture->destructionLock=&manager.m_listSection;
  job.m_texture->destroyedUnderLock=&underLock;job.m_texture->destroyed=&destroyed;
  manager.OnJobComplete(1,success,&job);
  Check(static_cast<bool>(image.texture)==success,"publication: failed job not published");
  Check(!destroyed && !underLock,"publication: no texture destructor under list lock");
  Check(static_cast<bool>(job.m_texture)!=success,"publication: failed job keeps texture until job deletion");
  job.m_texture.reset();image.texture.reset();
  Check(destroyed && !underLock,"publication: destruction occurs outside callback lock");
  Check(manager.m_queued.empty() && manager.m_queuedLookup.empty(),"publication: queue bookkeeping retained");
 }
}
static void ThrowingReleaseLog()
{
 for (unsigned phase : {0u,1u,2u})
 {
  Reset({"EGL_KHR_surfaceless_context"},false);CWinSystemAmlogicGLESContext win;g_winSystem=&win;
  Check(win.m_pGLContext.CreateContext({}),"log exception: create");
  {std::lock_guard<std::mutex> guard(fixture::lock);fixture::failUnbind=true;}
  fixture::throwWarning=true;std::string caught;
  std::atomic<bool> leaseReleased{false};
  std::thread worker([&]{CTexture texture;texture.throwLoad=phase==1;texture.throwSync=phase==2;
   try {texture.LoadToGPUAsync();}
   catch(const std::bad_alloc&){caught="injected release log failure";}
   catch(const std::exception& error){caught=error.what();}
   leaseReleased=eglGetCurrentContext()==EGL_NO_CONTEXT &&
     win.m_pGLContext.m_currentUploadContext==nullptr &&
     !win.m_pGLContext.m_textureUploadLock.HeldByCurrentThread();
   // A destructive control may strand bookkeeping after the driver detached.
   // Retire only its own fixture lease, without hiding the assertion above.
   fixture::throwWarning=false;
   if (win.m_pGLContext.m_currentUploadContext==&win.m_pGLContext)
     win.m_pGLContext.UnbindTextureUploadContext();
  });worker.join();
  const char* expected=phase==1?"injected LoadToGPU failure":phase==2?"injected SyncGPU failure":"injected release log failure";
  Check(caught==expected,"log exception: preserve original upload/sync failure");
  Check(leaseReleased,"log exception: detach/TLS/unlock precede throwing reporting");
  win.m_pGLContext.DestroyContext();Violations("log exception");
 }
}
static void BindFailureAndConfig()
{
 for (bool compatible : {false,true})
 {
  Reset({"EGL_KHR_surfaceless_context"},true);
  {std::lock_guard<std::mutex> guard(fixture::lock);fixture::configBits=compatible?EGL_WINDOW_BIT|EGL_PBUFFER_BIT:EGL_WINDOW_BIT;fixture::failBind=true;}
  CWinSystemAmlogicGLESContext win;g_winSystem=&win;
  Check(win.m_pGLContext.CreateContext({}),"bind failure: primary still created");
  for (int i=0;i<4;++i){CTexture texture;UploadOnLoader(texture);Check(!texture.uploading,"bind failure: untouched CPU fallback");}
  Check(fixture::bindCalls==(compatible?2u:1u),"bind failure: one attempt/fallback then disabled");
  Check(fixture::pbufferCalls==(compatible?1u:0u),"config: no pbuffer without selected config PBUFFER_BIT");
  Check(!win.m_pGLContext.HasTextureUploadContext(),"bind failure: unavailable after failure");
  win.m_pGLContext.DestroyContext();Violations("bind failure");
 }
 Reset({},true);
 {std::lock_guard<std::mutex> guard(fixture::lock);fixture::failConfigQuery=true;}
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"config query failure: primary created");
 Check(fixture::pbufferCalls==0 && !win.m_pGLContext.HasTextureUploadContext(),"config query failure: clean fallback");
 std::thread direct([&]{const bool bound=win.m_pGLContext.BindTextureUploadContext();
  Check(!bound,"unavailable: direct bind is rejected independently of AML precheck");
  if(bound)win.m_pGLContext.UnbindTextureUploadContext();});direct.join();
 win.m_pGLContext.DestroyContext();Violations("config query");
}
static void CancelledCompletion()
{
 Reset({"EGL_KHR_surfaceless_context"},false);
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"cancel: create");
 CGUILargeTextureManager manager;CGUILargeTextureManager::CLargeTexture image;
 manager.m_queued.push_back({9,&image});manager.m_queuedLookup[image.path]=&image;
 bool underLock=false,destroyed=false;
 CImageLoader job;job.m_texture=std::make_unique<CTexture>();job.m_texture->hold=true;
 job.m_texture->destructionLock=&manager.m_listSection;
 job.m_texture->destroyedUnderLock=&underLock;job.m_texture->destroyed=&destroyed;
 std::thread worker([&]{job.m_texture->LoadToGPUAsync();manager.OnJobComplete(9,true,&job);});
 Await([&]{return job.m_texture->uploading.load();},"cancel upload timeout");
 // Model ReleaseImage removing its queue entry. A callback already copied by
 // JobManager may still race cancellation and execute the actual method below.
 {std::unique_lock<CCriticalSection> lock(manager.m_listSection);
  manager.m_queued.clear();manager.m_queuedLookup.clear();}
 job.m_texture->hold=false;worker.join();
 Check(!image.texture && job.m_texture && job.m_texture->synced,"cancel: completed upload is not published after queue removal");
 Check(!destroyed,"cancel: job retains cancelled texture");job.m_texture.reset();
 Check(destroyed && !underLock,"cancel: destruction occurs outside callback lock");
 win.m_pGLContext.DestroyContext();Violations("cancel");
}
static void SurfacelessRetry()
{
 Reset({"EGL_KHR_surfaceless_context"},true);
 {std::lock_guard<std::mutex> guard(fixture::lock);fixture::failSurfaceless=true;}
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"retry: create");CTexture texture;UploadOnLoader(texture);
 Check(texture.uploaded && texture.synced && fixture::bindCalls==2 && fixture::pbufferCalls==1,"retry: actual pbuffer binding succeeds once");
 win.m_pGLContext.DestroyContext();Violations("retry");
}
static void ReleaseFailure()
{
 for (bool poisoned : {false,true})
 {
  Reset({"EGL_KHR_surfaceless_context"},false);
  CWinSystemAmlogicGLESContext win;g_winSystem=&win;Check(win.m_pGLContext.CreateContext({}),"release: create");
  {std::lock_guard<std::mutex> guard(fixture::lock);fixture::failUnbind=true;fixture::failRelease=poisoned;}
  std::atomic<bool> caught{false},knownReleased{false};std::thread::id workerId;
  std::thread worker([&] {
   workerId=std::this_thread::get_id();CTexture texture;
   try {texture.LoadToGPUAsync();}catch(const std::runtime_error&){caught=true;}
   knownReleased=eglGetCurrentContext()==EGL_NO_CONTEXT;
   // Clean an intentionally poisoned recording driver only on its owning thread.
   if (poisoned)
   {
    {std::lock_guard<std::mutex> guard(fixture::lock);fixture::failUnbind=false;fixture::failRelease=false;}
    eglReleaseThread();
   }
  });worker.join();
  Check(caught==poisoned && knownReleased!=poisoned,"release: report actual detach outcome");
  Check(fixture::releaseThread==workerId && fixture::releaseCalls==(poisoned?2u:1u),"release: fallback runs on binding worker");
  Check(!win.m_pGLContext.HasTextureUploadContext(),"release: failed make-current disables generation");
  win.m_pGLContext.DestroyContext();Check(win.m_pGLContext.CreateContext({}),"release: recreate primary");
  Check(win.m_pGLContext.HasTextureUploadContext()!=poisoned,"release: unknown detach poison survives recreation");
  win.m_pGLContext.DestroyContext();Violations("release");
 }
}
static void OwnerAndClose()
{
 Reset({"EGL_KHR_surfaceless_context"},false);
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;Check(win.m_pGLContext.CreateContext({}),"owner: create");
 CTexture active;active.hold=true;
 std::thread owner([&]{active.LoadToGPUAsync();});Await([&]{return active.uploading.load();},"owner bind timeout");
 Check(!win.m_pGLContext.UnbindTextureUploadContext(),"owner: foreign thread cannot release lease");
 Check(fixture::unbindCalls==0,"owner: foreign release never calls EGL");
 CTexture queued;std::atomic<bool> queuedStarted{false},closed{false};
 std::thread waiting([&]{queuedStarted=true;queued.LoadToGPUAsync();});Await([&]{return queuedStarted.load();},"queued timeout");
 std::thread closer([&]{win.m_pGLContext.CloseTextureUploadContext();closed=true;});
 Await([&]{return win.m_pGLContext.m_textureUploadClosing.load();},"close admission timeout");
 Check(!closed,"close: waits for current upload");active.hold=false;
 owner.join();closer.join();waiting.join();
 Check(!queued.uploading && !win.m_pGLContext.HasTextureUploadContext(),"close: queued upload rejected");
 CTexture late;UploadOnLoader(late);Check(!late.uploading,"close: late upload rejected");
 win.m_pGLContext.DestroyContext();Violations("owner/close");
}
static void NestedLease()
{
 Reset({"EGL_KHR_surfaceless_context"},false);CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"nested: create");
 std::thread worker([&]{Check(win.m_pGLContext.BindTextureUploadContext(),"nested: first bind");
  Check(!win.m_pGLContext.BindTextureUploadContext(),"nested: second lease rejected");
  Check(win.m_pGLContext.UnbindTextureUploadContext(),"nested: original lease releases");});worker.join();
 Check(fixture::bindCalls==1,"nested: single actual binding");win.m_pGLContext.DestroyContext();Violations("nested");
}
static void SurfaceReplacement()
{
 Reset({"EGL_KHR_surfaceless_context"},true);CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"surface: create");
 const EGLContext upload=win.m_pGLContext.m_eglUploadContext;
 {std::lock_guard<std::mutex> guard(fixture::lock);fixture::surfaces.insert(reinterpret_cast<EGLSurface>(0x999));}
 win.m_pGLContext.m_eglSurface=reinterpret_cast<EGLSurface>(0x999);
 CTexture active;active.hold=true;std::thread worker([&]{active.LoadToGPUAsync();});
 Await([&]{return active.uploading.load();},"surface bind timeout");
 win.m_pGLContext.DestroySurface();active.hold=false;worker.join();
 Check(active.uploaded && active.synced && win.m_pGLContext.m_eglUploadContext==upload,"surface: same upload namespace survives primary surface removal");
 win.m_pGLContext.DestroyContext();Violations("surface");
}
// Application job draining is an explicit fixture contract here. Actual upload
// and completion methods execute; the entire CApplication/CJobManager does not.
static void GenerationAfterCompletionDrain()
{
 Reset({"EGL_KHR_surfaceless_context"},false);CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 Check(win.m_pGLContext.CreateContext({}),"generation: create");const EGLContext old=win.m_pGLContext.m_eglContext;
 CGUILargeTextureManager manager;CGUILargeTextureManager::CLargeTexture image;
 manager.m_queued.push_back({7,&image});manager.m_queuedLookup[image.path]=&image;
 CImageLoader job;job.m_texture=std::make_unique<CTexture>();std::atomic<bool> awaitingCompletion{false},allowCompletion{false};
 std::thread worker([&]{job.m_texture->LoadToGPUAsync();awaitingCompletion=true;
  Await([&]{return allowCompletion.load();},"completion drain timeout");manager.OnJobComplete(7,true,&job);});
 Await([&]{return awaitingCompletion.load();},"generation upload timeout");
 win.m_pGLContext.CloseTextureUploadContext();Check(!image.texture,"generation: upload lease alone did not publish");
 allowCompletion=true;worker.join();Check(image.texture && image.texture->synced,"generation: completion drained before teardown");
 image.texture.reset();win.m_pGLContext.DestroyContext();Check(win.m_pGLContext.CreateContext({}),"generation: recreate");
 Check(win.m_pGLContext.m_eglContext!=old,"generation: fixture created a different namespace");
 CTexture fresh;UploadOnLoader(fresh);Check(fresh.uploaded && fresh.synced,"generation: new image uploads in fresh namespace");
 win.m_pGLContext.DestroyContext();Violations("generation");
}
'''


def build_source(files):
    prelude=donor.PRELUDE
    prelude=replace(prelude,'#include <vector>','#include <vector>\n#include <memory>\n#include <new>\n#include <unordered_map>')
    prelude=replace(prelude,'#define EGL_NONE 0x3038','#define EGL_NONE 0x3038\n#define EGL_SURFACE_TYPE 0x3033\n#define EGL_WINDOW_BIT 4\n#define EGL_PBUFFER_BIT 1')
    prelude=replace(prelude,'bool pbuffer = true;',r'''bool pbuffer = true;
bool failConfigQuery=false,failBind=false,failSurfaceless=false,failUnbind=false,failRelease=false;
std::atomic<bool> throwWarning{false};
EGLint configBits=EGL_WINDOW_BIT|EGL_PBUFFER_BIT;
unsigned bindCalls=0,unbindCalls=0,releaseCalls=0,pbufferCalls=0;
std::thread::id releaseThread;''')
    prelude=replace(prelude,'    fixture::current.erase(self);',r'''    ++fixture::unbindCalls;
    if (fixture::failUnbind) { fixture::error=EGL_BAD_ACCESS; return EGL_FALSE; }
    fixture::current.erase(self);''')
    prelude=replace(prelude,'  if (!fixture::live.count(context))',r'''  ++fixture::bindCalls;
  if (fixture::failBind || (fixture::failSurfaceless && draw==EGL_NO_SURFACE))
  { fixture::error=EGL_BAD_MATCH; return EGL_FALSE; }
  if (!fixture::live.count(context))''')
    prelude=replace(prelude,'  int width = -1, height = -1;',r'''  ++fixture::pbufferCalls;
  if (!(fixture::configBits&EGL_PBUFFER_BIT)) fixture::Violation("pbuffer requested from incompatible config");
  int width = -1, height = -1;''')
    prelude=replace(prelude,'using CCriticalSection = std::recursive_mutex;',MUTEX)
    prelude=replace(prelude,'  static void LogF(int level, const char*, Args&&...)\n  {',
        '  static void LogF(int level, const char*, Args&&...)\n  {\n    if (level==LOGWARNING && fixture::throwWarning) throw std::bad_alloc();')
    classes=donor.upload_classes(files[donor.EGL_SOURCE])
    if 'void CTexture::LoadToGPUAsync(const std::atomic_bool*' in files[donor.TEXTURE_SOURCE]:
        classes=replace(classes,'  void LoadToGPUAsync();',
                        '  void LoadToGPUAsync(const std::atomic_bool* cancelled=nullptr);')
    classes=replace(classes,'  bool HasTextureUploadContext() const;',r'''  bool HasTextureUploadContext() const;
  void CloseTextureUploadContext();
  bool CreateTextureUploadPbuffer();''')
    classes=replace(classes,'  mutable CCriticalSection m_textureUploadLock;',r'''  mutable CCriticalSection m_textureUploadLock;
  bool m_textureUploadDisabled=false,m_textureUploadPoisoned=false;
  std::atomic<bool> m_textureUploadClosing{false};
  inline static thread_local CEGLContextUtils* m_currentUploadContext=nullptr;
  EGLSurface m_eglSurface=EGL_NO_SURFACE;
  void DestroySurface();''')
    classes=replace(classes,'  EGLContext uploadContext{EGL_NO_CONTEXT};',r'''  EGLContext uploadContext{EGL_NO_CONTEXT};
  bool throwLoad=false,throwSync=false;
  CCriticalSection* destructionLock=nullptr;
  bool* destroyedUnderLock=nullptr;
  bool* destroyed=nullptr;
  ~CTexture()
  {
    if (destroyed) *destroyed=true;
    if (destroyedUnderLock && destructionLock && destructionLock->HeldByCurrentThread()) *destroyedUnderLock=true;
  }''')
    classes=replace(classes,'    uploading = true;','    uploading = true;\n    if (throwLoad) throw std::runtime_error("injected LoadToGPU failure");')
    classes=replace(classes,'  void SyncGPU() { synced = uploaded.load(); }','  void SyncGPU() { if (throwSync) throw std::runtime_error("injected SyncGPU failure"); synced = uploaded.load(); }')
    main=donor.MAIN[:donor.MAIN.index('int main()')]
    main=replace(main,'static int failures = 0;','static std::atomic<int> failures{0};')
    main=replace(main,'  errorLogs = 0;',r'''  errorLogs = 0;
  fixture::failConfigQuery=fixture::failBind=fixture::failSurfaceless=fixture::failUnbind=fixture::failRelease=false;
  fixture::configBits=EGL_WINDOW_BIT|EGL_PBUFFER_BIT;
  fixture::bindCalls=fixture::unbindCalls=fixture::releaseCalls=fixture::pbufferCalls=0;
  fixture::releaseThread={};''')
    attributes=re.search(r'class CEGLAttributesVec\n\{[\s\S]*?\n\};\n',files[donor.EGL_HEADER]).group()
    signatures=donor.EGL_METHODS+donor.FIXED_EGL_METHODS+[
        'bool CEGLContextUtils::CreateTextureUploadPbuffer()',
        'void CEGLContextUtils::CloseTextureUploadContext()',
        'void CEGLContextUtils::DestroySurface()']
    parts=[prelude,EXTRA_EGL,attributes,classes,PUBLICATION_CLASSES]
    parts += [donor.function(files[donor.EGL_SOURCE],signature) for signature in signatures]
    parts += [donor.function(files[donor.AML_SOURCE],signature) for signature in donor.AML_METHODS]
    parts += [donor.function(files[donor.TEXTURE_SOURCE],'void CTexture::LoadToGPUAsync('),
              donor.function(files[PUBLICATION],'void CGUILargeTextureManager::OnJobComplete('),main,TESTS]
    parts += [r'''int main(){try {
      Surfaceless();PbufferFallback();NoSurface();ConcurrentLoaders();DestroyDuringUpload();PrimaryThread();
      Exceptions();Publication();BindFailureAndConfig();SurfacelessRetry();ReleaseFailure();OwnerAndClose();
      NestedLease();SurfaceReplacement();GenerationAfterCompletionDrain();CancelledCompletion();ThrowingReleaseLog();
      if(failures)
        return 1;
      std::puts("PASS donor6 + hardening11 fixture groups");
      return 0;
    }catch(const std::exception& e){std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}}''']
    return '\n'.join(parts)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=HERE.parent)
    parser.add_argument('--cxx',default=os.environ.get('CXX','c++'))
    parser.add_argument('--sanitize',action='store_true')
    parser.add_argument('--tsan',action='store_true',
                        help='Attempt TSan; report known host startup failures as unavailable')
    parser.add_argument('--no-mutations',action='store_true')
    args=parser.parse_args()
    paths=[donor.EGL_SOURCE,donor.EGL_HEADER,donor.AML_SOURCE,donor.TEXTURE_SOURCE,PUBLICATION]
    files={path:(args.root/path).read_text() for path in paths}
    # These are narrow source-order checks, not execution of CApplication.
    application=(args.root/'xbmc/application/Application.cpp').read_text()
    stop=donor.function(application,'bool CApplication::Stop(')
    cleanup=donor.function(application,'bool CApplication::Cleanup()')
    create=donor.function(application,'bool CApplication::CreateGUI()')
    shutdown=donor.function(files[donor.AML_SOURCE],
        'bool CWinSystemAmlogicGLESContext::PrepareForShutdown()')
    assert 'GetJobManager()->CancelJobs();' in stop
    assert cleanup.index('m_pGUI.reset();') < cleanup.index('CServiceBroker::UnregisterWinSystem();')
    assert create.index('m_pWinSystem->DestroyWindowSystem()') < create.index('m_pGUI =')
    assert shutdown.index('CloseTextureUploadContext();') < shutdown.index('RetireNativeTransactions()')
    print('Source-order checks: existing Stop job drain and init-failure-before-GUI boundaries retained',flush=True)
    with tempfile.TemporaryDirectory(prefix='aml-upload-hardening-') as directory:
        work=Path(directory)
        def run(name,selected,sanitizer=None):
            source=work/(name+'.cpp');source.write_text(build_source(selected));binary=source.with_suffix('')
            command=shlex.split(args.cxx)+['-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                     '-pthread','-DHAS_LIBAMCODEC',str(source),'-o',str(binary)]
            if sanitizer:command+=['-fsanitize='+sanitizer,'-fno-omit-frame-pointer','-fno-pie','-no-pie']
            subprocess.run(command,check=True)
            return subprocess.run([str(binary)],capture_output=True,text=True,timeout=20,
                                  env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
        result=run('actual',files,'address,undefined' if args.sanitize else None)
        assert result.returncode==0,(result.stdout,result.stderr)
        print(result.stdout.strip(),flush=True)
        if args.tsan:
            result=run('tsan',files,'thread')
            if 'FATAL: ThreadSanitizer: unexpected memory mapping' in result.stderr or 'ThreadSanitizer: CHECK failed' in result.stderr:
                print('TSan unavailable in this host environment: '+result.stderr.splitlines()[0],flush=True)
            else:
                assert result.returncode==0 and 'WARNING: ThreadSanitizer' not in result.stderr,result.stderr
                print('PASS TSan',flush=True)
        if not args.no_mutations:
            mutations={
                'exception-cleanup':(donor.TEXTURE_SOURCE,'      if (bound)\n','      if (false && bound)\n'),
                'failed-publication':(PUBLICATION,'if (success)\n      {\n        if (image->m_asyncUpload)','if (true)\n      {\n        if (image->m_asyncUpload)'),
                'destroy-under-callback-lock':(PUBLICATION,'// Failed jobs retain their texture until job deletion outside this lock.','loader->m_texture.reset();\n      // Failed jobs retain their texture until job deletion outside this lock.'),
                'retry-per-texture':(donor.EGL_SOURCE,'    m_textureUploadDisabled = true;\n    CLog::LogF(LOGWARNING, "Disabling unavailable','    m_textureUploadDisabled = false;\n    CLog::LogF(LOGWARNING, "Disabling unavailable'),
                'wrong-owner':(donor.EGL_SOURCE,'  if (m_currentUploadContext != this)\n    return false;','  if (false && m_currentUploadContext != this)\n    return false;'),
                'release-fallback':(donor.EGL_SOURCE,'released = eglReleaseThread() == EGL_TRUE;','released = false;'),
                'pbuffer-config':(donor.EGL_SOURCE,'!(surfaceTypes & EGL_PBUFFER_BIT)','false'),
                'sticky-poison':(donor.EGL_SOURCE,'m_textureUploadDisabled = m_textureUploadPoisoned;','m_textureUploadPoisoned = false; m_textureUploadDisabled = false;'),
                'close-admission':(donor.EGL_SOURCE,'m_textureUploadClosing.store(true, std::memory_order_release);','m_textureUploadClosing.store(false, std::memory_order_release);'),
                'logging-before-release':(donor.EGL_SOURCE,'',''),
            }
            for name,(path,old,new) in mutations.items():
                selected=dict(files)
                if name=='logging-before-release':
                    body=donor.function(selected[path],'bool CEGLContextUtils::UnbindTextureUploadContext()')
                    start=body.index('  // Even logging allocation failures')
                    end=body.index('  return released;',start)
                    warning=body[start:end]
                    bad=body[:start]+body[end:]
                    bad=replace(bad,'  m_currentUploadContext = nullptr;',warning+'  m_currentUploadContext = nullptr;')
                    selected[path]=replace(selected[path],body,bad)
                elif name=='close-admission':
                    body=donor.function(selected[path],'void CEGLContextUtils::CloseTextureUploadContext()')
                    selected[path]=replace(selected[path],body,replace(body,old,new))
                else:selected[path]=replace(selected[path],old,new)
                result=run(name,selected)
                assert result.returncode!=0,('Mutation survived',name,result.stdout,result.stderr)
                print('Rejected mutation:',name,flush=True)
    print('Limits: recording EGL/GL/decode/job-drain fixtures; no GPU sharing, pixels, device timing or arbitrary full-context recreation acceptance.')


if __name__=='__main__':main()
