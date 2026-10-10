#!/usr/bin/env python3
"""Compile real Blu-ray session/cache plus extracted logical close and service fences.
Host libbluray/filesystem/player stand-ins expose ownership and scheduling only.
Native Java behavior, device I/O, linker acceptance and stop latency remain unverified.
"""
import argparse, hashlib, json, os, pathlib, re, resource, subprocess, sys
P = pathlib.Path
REL = 'xbmc/cores/VideoPlayer/DVDInputStreams/'

def function(text, signature):
    start = text.index(signature)
    brace = text.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]

LIB = r'''
#pragma once
#include <cstdint>
#include <string>
struct CBlurayDiscSession;
struct BLURAY { CBlurayDiscSession* context{}; bool expectStream{}; const char* expectedRoot{}; const std::string* rootHandle{}; };
struct BD_OVERLAY {};
struct bd_argb_overlay_s {};
extern "C" BLURAY* bd_init();
extern "C" void bd_close(BLURAY*);
extern "C" void bd_register_overlay_proc(BLURAY*, void*, void (*)(void*, const BD_OVERLAY*));
extern "C" void bd_register_argb_overlay_proc(BLURAY*, void*, void (*)(void*, const bd_argb_overlay_s*), void*);
'''
INPUT = r'''
#pragma once
#include "BlurayDiscSession.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
extern std::atomic<int> dvReleases;
extern std::atomic_bool dvHold;
inline void aml_dv_set_disc_hold(bool hold) { dvHold = hold; if (!hold) ++dvReleases; }
struct FakePlayer { std::atomic<int> overlayCalls{0}; };
class CDVDInputStreamBluray {
public:
 ~CDVDInputStreamBluray() { Close(); }
 void Close(); void Abort(); bool PostNativeGate(int phase);
 void OverlayCallback(const BD_OVERLAY*) { callback(); }
 void OverlayCallbackARGB(const bd_argb_overlay_s*) { callback(); }
 void callback() {
   ++player.overlayCalls;
   std::unique_lock lock(callbackMutex);
   callbackEntered = true; callbackCV.notify_all();
   callbackCV.wait(lock, [&]{return !blockCallback;});
 }
 bool CloseMVCDemux() { ++mvcCleanups; return true; }
 void OverlayClose() { ++overlayCleanups; }
 void ReplaceTitleInfo(void*) { ++titleCleanups; }
 void FreePrevTitleInfo() { ++prevCleanups; }
 std::shared_ptr<CBlurayDiscSession> m_session;
 BLURAY* m_bd{}; std::atomic_bool m_aborted{false}; bool m_navmode{true}, m_dvDiscHold{true};
 enum Hold { HOLD_NONE, HOLD_EXIT, HOLD_DATA }; Hold m_hold{HOLD_NONE};
 bool m_crossPlaylistPending{true}, m_videoCompatBoundary{true}, m_naturalChainBoundary{true};
 bool m_currentTitleIsBdj{true}, m_atTitleEnd{true}, m_bdStillActive{true};
 std::chrono::steady_clock::time_point m_endOfTitleSpinStart{};
 int mvcCleanups{},overlayCleanups{},titleCleanups{},prevCleanups{};
 FakePlayer player;
 std::mutex callbackMutex; std::condition_variable callbackCV;
 bool callbackEntered{},blockCallback{};
};
'''
FILE = r'''
#pragma once
#include <atomic>
#include <cstdint>
#include <condition_variable>
#include <cstring>
#include <mutex>
class CFileItem {};
extern std::atomic<int> liveStreams;
extern std::mutex ioGateMutex;
extern std::condition_variable ioGate;
extern bool blockIO, ioEntered, releaseIO, blockOpen, openEntered, releaseOpen;
class CDVDInputStreamFile {
public:
 CDVDInputStreamFile(CFileItem&, int) { ++liveStreams; }
 ~CDVDInputStreamFile() { --liveStreams; }
 bool Open() { std::unique_lock lock(ioGateMutex);if(blockOpen){openEntered=true;ioGate.notify_all();ioGate.wait(lock,[]{return releaseOpen;});}return true; }
 int64_t GetLength() { return 32768; }
 int64_t Seek(int64_t offset, int) { position=offset; return offset; }
 int Read(uint8_t* b,int n) {
   { std::unique_lock lock(ioGateMutex);
     if (blockIO && position>=2048) { ioEntered=true; ioGate.notify_all(); ioGate.wait(lock,[]{return releaseIO;}); }
   }
   for (int i=0;i<n;++i) b[i]=static_cast<uint8_t>((position+i)%251);
   position+=n; return n;
 }
 int64_t position{};
};
'''
FIXTURE = r'''
#include "DVDInputStreamBluray.h"
#include "DVDInputStreamFile.h"
#include "utils/ScopeGuard.h"
#include <cassert>
#include <cstdio>
#include <future>
#include <thread>
using namespace std::chrono_literals;
namespace KODI { namespace TIME { inline void Sleep(std::chrono::milliseconds d){std::this_thread::sleep_for(d);} }}
@CALLBACKS@
std::atomic<int> dvReleases{0},liveStreams{0},nativeCloses{0},nativeInits{0};
std::atomic_bool dvHold{true},servicesAlive{true},initFailure{false};
std::mutex ioGateMutex; std::condition_variable ioGate;
bool blockIO=false,ioEntered=false,releaseIO=false,blockOpen=false,openEntered=false,releaseOpen=false;
std::mutex closeMutex; std::condition_variable closeCV;
bool blockClose=false,closeEntered=false,releaseClose=false,probeFallback=false;
std::atomic<int> probeCalls{0};
template<class Predicate> bool waitFor(Predicate p) {
 auto until=std::chrono::steady_clock::now()+2s;
 while (!p()) { if(std::chrono::steady_clock::now()>=until)return false; std::this_thread::sleep_for(1ms); }
 return true;
}
extern "C" BLURAY* bd_init() { ++nativeInits; return initFailure ? nullptr : new BLURAY; }
extern "C" void bd_register_overlay_proc(BLURAY*,void*,void (*)(void*,const BD_OVERLAY*)) {}
extern "C" void bd_register_argb_overlay_proc(BLURAY*,void*,void (*)(void*,const bd_argb_overlay_s*),void*) {}
extern "C" void bd_close(BLURAY* bd) {
 assert(servicesAlive);
 if(bd->context) {
   assert(bd->rootHandle && *bd->rootHandle==bd->expectedRoot);
   if(bd->expectStream)assert(liveStreams>0);
   uint8_t b[2048]{}; assert(read_blocks(bd->context,b,0,1)==-1);
   bluray_overlay_cb(bd->context,nullptr); bluray_overlay_argb_cb(bd->context,nullptr);
 }
 if(probeFallback) { assert(!CBlurayDiscSession::Shutdown()); ++probeCalls; }
 { std::unique_lock lock(closeMutex); closeEntered=true;closeCV.notify_all();
   closeCV.wait(lock,[]{return !blockClose||releaseClose;}); }
 assert(servicesAlive); ++nativeCloses;delete bd;
}
void setCloseBlock(bool block) {
 std::lock_guard lock(closeMutex);blockClose=block;closeEntered=false;releaseClose=false;
}
void releaseNative() { std::lock_guard lock(closeMutex);releaseClose=true;closeCV.notify_all(); }
bool entered() { std::lock_guard lock(closeMutex);return closeEntered; }
void settledShutdown() { assert(waitFor([]{return CBlurayDiscSession::Shutdown();})); }
std::shared_ptr<CBlurayDiscSession> attach(CDVDInputStreamBluray& input,bool cache=false) {
 auto s=std::make_shared<CBlurayDiscSession>(&input,1);
 input.m_session=s;input.m_bd=s->OpenNative();assert(input.m_bd);
 s->root="bluray-fixture-root";input.m_bd->context=s.get();input.m_bd->expectedRoot="bluray-fixture-root";input.m_bd->rootHandle=&s->root;
 input.m_bd->expectStream=true;
 CFileItem item;CBlurayIsoCache::Config config;config.enabled=cache;config.pageSize=2048;config.maxBytes=8192;config.forwardPrefetchPages=cache?1:0;
 assert(s->OpenStream(item,config));return s;
}
// Actual input Close/Abort and post-native Open abort guards are inserted here.
@INPUT_METHODS@
bool CDVDInputStreamBluray::PostNativeGate(int phase) {
 switch(phase){
 case 0: {
@OPEN_NATIVE_GUARD@
}break;
 case 1: {
@OPEN_PLAY_GUARD@
}m_hold=HOLD_DATA;break;
 case 2: {
@OPEN_SELECT_GUARD@
}break;
 case 3: {
@OPEN_FINAL_GUARD@
}break;
 default: assert(false);
 }return true;
}

struct PlayerStub { bool ready=true;int closes=0;bool ClosePlayer(bool){++closes;return ready;} };
using CApplicationPlayer=PlayerStub;
struct ApplicationFixture {
 PlayerStub player;std::optional<int> m_pendingStop;
 template<class T> PlayerStub* GetComponent(){return &player;}
 std::atomic<int> servicesDestroyed{0};
 bool Create(){ @CREATE_GATE@ return true; }
 bool Cleanup(){ @CLEANUP_GATE@ ++servicesDestroyed;return true; }
 bool Stop(int exitCode){ @STOP_GATE@ ++servicesDestroyed;return true; }
};

int main() {
 assert(CBlurayDiscSession::Initialize());
 // Cancellation before stream creation prevents late backend setup.
 {auto cancelled=std::make_shared<CBlurayDiscSession>(nullptr,0);cancelled->Cancel();CFileItem item;CBlurayIsoCache::Config config;assert(!cancelled->OpenStream(item,config));}
 // Actual input Abort cancels the independently owned read path without releasing it.
 {CDVDInputStreamBluray input;input.m_dvDiscHold=false;auto s=std::make_shared<CBlurayDiscSession>(&input,0);input.m_session=s;CFileItem item;CBlurayIsoCache::Config cfg;cfg.enabled=false;assert(s->OpenStream(item,cfg));input.Abort();uint8_t b[2048];assert(input.m_aborted&&input.m_session&&s->ReadBlocks(b,0,1)==-1);input.Close();s.reset();}
 // Actual indexed/ARGB owner gates wait in-flight input callbacks.
 {
  CDVDInputStreamBluray owner;owner.m_dvDiscHold=false;
  auto s=std::make_shared<CBlurayDiscSession>(&owner,2);
  {std::lock_guard lock(owner.callbackMutex);owner.blockCallback=true;}
  std::thread overlay([&]{s->Overlay(nullptr);});
  assert(waitFor([&]{std::lock_guard lock(owner.callbackMutex);return owner.callbackEntered;}));
  std::atomic_bool detached=false;std::thread detach([&]{s->DetachOwner();detached=true;});
  std::this_thread::sleep_for(30ms);assert(!detached);
  {std::lock_guard lock(owner.callbackMutex);owner.blockCallback=false;owner.callbackCV.notify_all();}
  overlay.join();detach.join();assert(detached);
  int before=owner.player.overlayCalls;s->Overlay(nullptr);s->OverlayARGB(nullptr);assert(owner.player.overlayCalls==before);
 }
 // Actual logical Close gates callbacks before mutating navigation/MVC/title/overlay state.
 {
  CDVDInputStreamBluray input;input.m_dvDiscHold=false;
  auto session=std::make_shared<CBlurayDiscSession>(&input,20);input.m_session=session;
  {std::lock_guard lock(input.callbackMutex);input.blockCallback=true;}
  std::thread callback([&]{session->Overlay(nullptr);});
  assert(waitFor([&]{std::lock_guard lock(input.callbackMutex);return input.callbackEntered;}));
  std::atomic_bool closed=false;std::thread closer([&]{input.Close();closed=true;});
  assert(waitFor([&]{return !std::atomic_load(&input.m_session);}));
  std::this_thread::sleep_for(30ms);assert(!closed&&input.m_navmode&&input.mvcCleanups==0&&input.overlayCleanups==0&&input.titleCleanups==0);
  {std::lock_guard lock(input.callbackMutex);input.blockCallback=false;input.callbackCV.notify_all();}
  callback.join();closer.join();assert(closed&&!input.m_navmode&&input.mvcCleanups==1&&input.overlayCleanups==1&&input.titleCleanups==1);
  session.reset();
 }
 // Abort during a blocked real OpenStream backend setup is noticed on return.
 {
  CDVDInputStreamBluray input;input.m_dvDiscHold=false;
  auto session=std::make_shared<CBlurayDiscSession>(&input,21);input.m_session=session;
  {std::lock_guard lock(ioGateMutex);blockOpen=true;openEntered=false;releaseOpen=false;}
  CFileItem item;CBlurayIsoCache::Config cfg;cfg.enabled=false;bool opened=true;
  std::thread opener([&]{opened=session->OpenStream(item,cfg);});
  assert(waitFor([]{std::lock_guard lock(ioGateMutex);return openEntered;}));input.Abort();
  {std::lock_guard lock(ioGateMutex);releaseOpen=true;ioGate.notify_all();}
  opener.join();assert(!opened&&input.m_aborted&&input.m_session);input.Close();session.reset();assert(liveStreams==0);blockOpen=false;
 }
 // Logical close returns while native teardown owns callback root/stream.
 {
  auto input=std::make_unique<CDVDInputStreamBluray>();
  auto s=attach(*input);std::weak_ptr<CBlurayDiscSession> weak=s;
  uint8_t b[2048];assert(s->ReadBlocks(b,0,1)==1);for(int i=0;i<2048;++i)assert(b[i]==i%251);
  s->Overlay(nullptr);assert(input->player.overlayCalls==1);
  auto contender=std::make_shared<CBlurayDiscSession>(nullptr,30);std::atomic_bool contenderDone=false;BLURAY* contenderNative=nullptr;
  std::thread contenderThread([&]{contenderNative=contender->OpenNative();contenderDone=true;});
  std::this_thread::sleep_for(80ms);assert(!contenderDone&&nativeInits==1);
  contender->Cancel();assert(waitFor([&]{return contenderDone.load();}));contenderThread.join();assert(!contenderNative);CBlurayDiscSession::Retire(contender);contender.reset();
  setCloseBlock(true);std::atomic_bool returned=false;
  std::thread logical([&]{input->Close();returned=true;});
  assert(waitFor([&]{return returned.load();}));logical.join();
  assert(waitFor(entered));assert(!input->m_bd&&!input->m_session&&input->m_aborted);
  assert(input->m_hold==CDVDInputStreamBluray::HOLD_EXIT&&!input->m_navmode);
  assert(input->overlayCleanups==1&&input->titleCleanups==1&&input->prevCleanups==1);
  assert(dvReleases==1&&!dvHold);input->Close();assert(dvReleases==1);
  int oldCalls=input->player.overlayCalls;s->Overlay(nullptr);s->OverlayARGB(nullptr);assert(input->player.overlayCalls==oldCalls);
  input.reset();s.reset();assert(!weak.expired()&&liveStreams==1);
  // Shared Java lease denies overlap; cancellation fails admission.
  auto next=std::make_shared<CBlurayDiscSession>(nullptr,3);std::atomic_bool openDone=false;
  BLURAY* nextNative=nullptr;std::thread opener([&]{nextNative=next->OpenNative();openDone=true;});
  std::this_thread::sleep_for(80ms);assert(!openDone&&nativeInits==1);
  next->Cancel();assert(waitFor([&]{return openDone.load();}));opener.join();assert(!nextNative);next.reset();
  ApplicationFixture app;assert(!app.Stop(17)&&app.m_pendingStop==17&&app.servicesDestroyed==0);
  std::atomic_bool cleanupDone=false;std::thread cleanupThread([&]{assert(app.Cleanup());cleanupDone=true;});
  std::this_thread::sleep_for(30ms);assert(!cleanupDone&&app.servicesDestroyed==0);assert(!CBlurayDiscSession::Initialize());
  dvHold=true; // A separate newer non-Blu-ray playback policy is never released by native close.
  releaseNative();assert(waitFor([&]{return cleanupDone.load();}));cleanupThread.join();settledShutdown();assert(weak.expired()&&liveStreams==0&&nativeCloses==1);
  assert(dvHold&&dvReleases==1&&app.servicesDestroyed==1);
 }
 // Admission rejection retains ownership and synchronous fallback runs outside queue lock.
 {
  assert(CBlurayDiscSession::Initialize());CDVDInputStreamBluray input;input.m_dvDiscHold=false;
  auto s=attach(input);input.m_session.reset();input.m_bd=nullptr;
  assert(!CBlurayDiscSession::Shutdown());setCloseBlock(false);probeFallback=true;
  std::atomic_bool done=false;std::thread retire([&]{CBlurayDiscSession::Retire(s);done=true;});
  assert(waitFor([&]{return done.load();}));retire.join();probeFallback=false;
  assert(probeCalls==1&&nativeCloses==2);s->CloseNative();s.reset();assert(nativeCloses==2&&liveStreams==0);
  settledShutdown();
 }
 // Actual cache prefetch may be stuck in backend; cancellation is nonblocking and owning retirement waits.
 {
  assert(CBlurayDiscSession::Initialize());auto input=std::make_unique<CDVDInputStreamBluray>();input->m_dvDiscHold=false;
  auto s=attach(*input,true);std::weak_ptr<CBlurayDiscSession> weak=s;
  {std::lock_guard lock(ioGateMutex);blockIO=true;ioEntered=false;releaseIO=false;}
  uint8_t b[2048];assert(s->ReadBlocks(b,0,1)==1);
  assert(waitFor([]{std::lock_guard lock(ioGateMutex);return ioEntered;}));
  std::atomic_bool done=false;std::thread logical([&]{input->Close();done=true;});
  assert(waitFor([&]{return done.load();}));logical.join();input.reset();s.reset();
  assert(!weak.expired()&&liveStreams==1&&nativeCloses==2);assert(!CBlurayDiscSession::Shutdown());
  {std::lock_guard lock(ioGateMutex);releaseIO=true;ioGate.notify_all();}
  settledShutdown();assert(weak.expired()&&liveStreams==0&&nativeCloses==3);
  blockIO=false;
 }
 // Failed native initialization still retires its admission lease.
 {
  ApplicationFixture app;assert(app.Create());initFailure=true;
  auto s=std::make_shared<CBlurayDiscSession>(nullptr,4);assert(!s->OpenNative());
  assert(!CBlurayDiscSession::Shutdown());s->CloseNative();s.reset();settledShutdown();initFailure=false;
 }
 // The production failed-Open scope guard retires the partially opened context.
 {
  assert(CBlurayDiscSession::Initialize());CDVDInputStreamBluray input;input.m_dvDiscHold=false;
  setCloseBlock(false);
  { @FAILURE_GUARD@ input.m_bd=session->OpenNative();assert(input.m_bd); }
  settledShutdown();assert(!input.m_session&&!input.m_bd&&nativeCloses==4);
 }
 // Abort that arrives before session publication is honored by actual Open prelude.
 {assert(CBlurayDiscSession::Initialize());CDVDInputStreamBluray input;input.m_dvDiscHold=false;input.Abort();{@FAILURE_GUARD@ assert(!session->OpenNative());}assert(!input.m_session&&!input.m_bd);settledShutdown();}
 // Player retirement guards direct Cleanup before closing native admission.
 {
  assert(CBlurayDiscSession::Initialize());ApplicationFixture app;app.player.ready=false;
  assert(!app.Cleanup()&&app.player.closes==1&&app.servicesDestroyed==0);
  // A lease can still be acquired: the short-circuit must not fence admission early.
  auto s=std::make_shared<CBlurayDiscSession>(nullptr,5);assert(s->OpenNative());s->CloseNative();s.reset();
  app.player.ready=true;assert(app.Cleanup());settledShutdown();
 }
 // Actual post-native Open abort checks retire context and never report success.
 for(int phase=0;phase<4;++phase){
  assert(CBlurayDiscSession::Initialize());CDVDInputStreamBluray input;input.m_dvDiscHold=false;
  std::atomic_bool nativeEntered=false,nativeReturned=false,done=false;bool opened=true;
  std::thread opener([&]{
   {@FAILURE_GUARD@ input.m_bd=session->OpenNative();assert(input.m_bd);nativeEntered=true;
    assert(waitFor([&]{return nativeReturned.load();}));opened=input.PostNativeGate(phase);if(opened)failedOpen.release();}
   done=true;
  });
  assert(waitFor([&]{return nativeEntered.load();}));input.Abort();nativeReturned=true;
  assert(waitFor([&]{return done.load();}));opener.join();
  assert(!opened&&!input.m_session&&!input.m_bd&&input.m_hold==CDVDInputStreamBluray::HOLD_EXIT);settledShutdown();
 }
 // A foreground positioned read (prefetch disabled) retains its backend while close waits the actual I/O lock.
 {
  assert(CBlurayDiscSession::Initialize());auto input=std::make_unique<CDVDInputStreamBluray>();input->m_dvDiscHold=false;
  auto session=attach(*input,false);std::weak_ptr<CBlurayDiscSession> weak=session;
  {std::lock_guard lock(ioGateMutex);blockIO=true;ioEntered=false;releaseIO=false;}
  std::atomic_bool readDone=false;int readResult=0;
  std::thread reader([session,&readDone,&readResult]{uint8_t b[2048];readResult=session->ReadBlocks(b,1,1);readDone=true;});
  assert(waitFor([]{std::lock_guard lock(ioGateMutex);return ioEntered;}));setCloseBlock(false);
  std::atomic_bool closeDone=false;std::thread closer([&]{input->Close();closeDone=true;});
  assert(waitFor([&]{return closeDone.load();}));closer.join();input.reset();session.reset();
  assert(waitFor(entered));std::this_thread::sleep_for(30ms);
  assert(!readDone&&!weak.expired()&&liveStreams==1);assert(!CBlurayDiscSession::Shutdown());
  {std::lock_guard lock(ioGateMutex);releaseIO=true;ioGate.notify_all();}
  assert(waitFor([&]{return readDone.load();}));reader.join();assert(readResult==-1);
  settledShutdown();assert(weak.expired()&&liveStreams==0);blockIO=false;
 }
 servicesAlive=false;
 std::puts("Blu-ray owning close/session/cache/application fences: PASS");
}
'''

def build_source(root, changes=None):
    files={name:(root/(REL+name)).read_text() for name in ['BlurayDiscSession.cpp','BlurayDiscSession.h','BlurayIsoCache.cpp','BlurayIsoCache.h']}
    bluray=(root/(REL+'DVDInputStreamBluray.cpp')).read_text();app=(root/'xbmc/application/Application.cpp').read_text()
    if changes:
        key,old,new=changes
        if key=='input':
            assert old in bluray,(key,old);bluray=bluray.replace(old,new,1)
        elif key=='application':
            assert old in app,(key,old);app=app.replace(old,new,1)
        else:
            assert old in files[key],(key,old);files[key]=files[key].replace(old,new,1)
    methods=function(bluray,'void CDVDInputStreamBluray::Close()')+'\n'+function(bluray,'void CDVDInputStreamBluray::Abort()')
    create=re.search(r'#ifdef HAVE_LIBBLURAY\n(.*?)#endif',function(app,'bool CApplication::Create()'),re.S).group(1)
    cleanup=function(app,'bool CApplication::Cleanup()')
    cleanup=re.search(r'    if \(!GetComponent<CApplicationPlayer>\(\)->ClosePlayer\(true\).*?#endif',cleanup,re.S).group().removesuffix('#endif')
    stop=function(app,'bool CApplication::Stop(int exitCode)')
    stop=re.search(r'#ifdef HAVE_LIBBLURAY\n(.*?)#endif',stop,re.S).group(1)
    guard=re.search(r'  const auto session = std::make_shared<CBlurayDiscSession>\(this, m_diagnosticOpen\);.*?failedOpen\(.*?\}, this\);',bluray,re.S).group()
    guard=guard.replace('this, m_diagnosticOpen','&input, 6').replace('&m_session','&input.m_session').replace('}, this);','}, &input);').replace('if (m_aborted)', 'if (input.m_aborted)')
    callbacks='\n'.join(function(bluray,signature) for signature in ['static int read_blocks(', 'static void bluray_overlay_cb(', 'void  bluray_overlay_argb_cb('])
    guards={
      '@OPEN_NATIVE_GUARD@':re.search(r'(if \((?:m_aborted|false)\)\s*return false;)\s*bd_get_event\(m_bd, nullptr\);',bluray).group(1),
      '@OPEN_PLAY_GUARD@':re.search(r'(if \((?:m_aborted|false)\)\s*return false;)\s*if\(playResult <= 0\)',bluray).group(1),
      '@OPEN_SELECT_GUARD@':re.search(r'(if \((?:m_aborted|false)\)\s*return false;)\s*// Keep DV output',bluray).group(1),
      '@OPEN_FINAL_GUARD@':re.search(r'OpenNextStream\(\);\s*(if \((?:m_aborted|false)\)\s*return false;)',bluray).group(1),
    }
    fixture=FIXTURE.replace('@CALLBACKS@',callbacks).replace('@INPUT_METHODS@',methods).replace('@CREATE_GATE@',create).replace('@CLEANUP_GATE@',cleanup).replace('@STOP_GATE@',stop).replace('@FAILURE_GUARD@',guard)
    for marker,guardText in guards.items(): fixture=fixture.replace(marker,guardText)
    files.update({'fixture.cpp':'#include <optional>\n'+fixture,'DVDInputStreamBluray.h':INPUT,'DVDInputStreamFile.h':FILE,'libbluray/bluray.h':LIB,'libbluray/overlay.h':'#pragma once\n#include "bluray.h"\n','filesystem/File.h':'#pragma once\nnamespace XFILE { enum { READ_TRUNCATED=1, READ_BITRATE=2, READ_CHUNKED=4, READ_NO_CACHE=8 }; }\n','utils/log.h':'#pragma once\n#define LOGINFO 1\nclass CLog { public: template<class... T> static void Log(T&&...){} };\n','utils/PlaybackDiagnostics.h':'#pragma once\nnamespace PLAYBACK_DIAGNOSTICS { inline long NowUs(){return 0;} }\n','utils/ScopeGuard.h':(root/'xbmc/utils/ScopeGuard.h').read_text()})
    return files

MUTANTS=[
 ('skip-overlay-barrier',('BlurayDiscSession.cpp','  std::lock_guard lock(m_overlayMutex);\n  m_owner = nullptr;', '  m_owner = nullptr;')),
 ('cleanup-before-owner-barrier',('input','    session->DetachOwner();', '    m_navmode=false;CloseMVCDemux();session->DetachOwner();')),
 ('keep-overlay-owner',('BlurayDiscSession.cpp','m_owner = nullptr;','/* keep old owner */')),
 ('omit-cancel',('BlurayDiscSession.cpp','m_cancelled = true;','/* no cancellation */')),
 ('premature-root-free',('BlurayDiscSession.cpp','bd_close(m_bd);','root.clear(); bd_close(m_bd);')),
 ('omit-foreground-read-lock',('BlurayDiscSession.cpp','    std::lock_guard lock(m_ioMutex);\n    m_stream.reset();','    m_stream.reset();')),
 ('premature-stream-free',('BlurayDiscSession.cpp','bd_close(m_bd);','m_stream.reset(); bd_close(m_bd);')),
 ('skip-native-close',('BlurayDiscSession.cpp','bd_close(m_bd);','/* leaked native close */')),
 ('ignore-java-lease',('BlurayDiscSession.cpp','while (m_accepting && !cancelled && (m_leased || m_active || m_pending != nullptr))','while (m_accepting && !cancelled && (m_active || m_pending != nullptr))')),
 ('skip-lease-release',('BlurayDiscSession.cpp','CloseQueue().Release();','/* unreleased lease */')),
 ('shutdown-false-completion',('BlurayDiscSession.cpp','if (m_leased || m_active || m_pending != nullptr)\n      return false;','if (m_leased || m_active || m_pending != nullptr)\n      return true;')),
 ('sync-retirement',('BlurayDiscSession.cpp','!session->m_bd || !CloseQueue().Submit(session)','true')),
 ('omit-input-abort-cancel',('input','    session->Cancel();\n}\n\nbool CDVDInputStreamBluray::IsEOF()', '    (void)session;\n}\n\nbool CDVDInputStreamBluray::IsEOF()')),
 ('omit-logical-dv-release',('input','aml_dv_set_disc_hold(false);','/* no release */')),
 ('omit-failed-open-retirement',('input','input->Close();','(void)input;')),
 ('retain-input-session',('input','std::atomic_exchange(&m_session, std::shared_ptr<CBlurayDiscSession>{})','std::atomic_load(&m_session)')),
 ('omit-create-initialization',('application','if (!CBlurayDiscSession::Initialize())','if (false)')),
 ('omit-stop-native-fence',('application','if (!CBlurayDiscSession::Shutdown())','if (false)')),
 ('omit-abort-before-publication',('input','if (m_aborted)\n    session->Cancel();','if (false)\n    session->Cancel();')),
 ('omit-post-native-open-abort',('input','if (m_aborted)\n    return false;\n  bd_get_event(m_bd, nullptr);','if (false)\n    return false;\n  bd_get_event(m_bd, nullptr);')),
 ('omit-post-play-abort',('input','if (m_aborted)\n      return false;\n    if(playResult <= 0)','if (false)\n      return false;\n    if(playResult <= 0)')),
 ('omit-post-select-abort',('input','if (m_aborted)\n    return false;\n\n  // Keep DV output','if (false)\n    return false;\n\n  // Keep DV output')),
 ('omit-final-open-abort',('input','OpenNextStream();\n  if (m_aborted)','OpenNextStream();\n  if (false)')),
 ('omit-direct-cleanup-fence',('application','while (!CBlurayDiscSession::Shutdown())','while (false)')),
]

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=P,default=P(__file__).resolve().parents[1]);ap.add_argument('--logs',type=P,default=P('/tmp/bluray-close-regression'));ap.add_argument('--negative-controls',action='store_true');ap.add_argument('--sanitizers',action='store_true');args=ap.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    args.logs.mkdir(parents=True,exist_ok=True);results=[]
    cases=[('positive',None)]+(MUTANTS if args.negative_controls else [])
    for name,change in cases:
        d=args.logs/name;d.mkdir(parents=True,exist_ok=True)
        try: files=build_source(args.root,change)
        except (AssertionError,AttributeError) as e: print(f'{name}: extraction failure {e}',file=sys.stderr);return 1
        for path,data in files.items():
            p=d/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
        cmd=[os.environ.get('CXX','g++'),'-std=c++17','-pthread','-g','-O1','-Wall','-Wextra','-Werror','-DHAVE_LIBBLURAY','-DHAVE_LIBBLURAY_BDJ','-I',str(d),str(d/'BlurayDiscSession.cpp'),str(d/'BlurayIsoCache.cpp'),str(d/'fixture.cpp'),'-o',str(d/'fixture')]
        if args.sanitizers: cmd[1:1]=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
        compiled=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT);(d/'compile.log').write_text(compiled.stdout)
        if compiled.returncode: print(f'{name}: COMPILE FAILED; {d}/compile.log',file=sys.stderr);return 1
        try: ran=subprocess.run([str(d/'fixture')],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=20,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:abort_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
        except subprocess.TimeoutExpired as e: (d/'run.log').write_text(str(e));print(f'{name}: timeout is not a valid negative control',file=sys.stderr);return 1
        (d/'run.log').write_text(ran.stdout)
        passed=ran.returncode==0 if change is None else ran.returncode==-6 and 'Assertion' in ran.stdout and 'AddressSanitizer' not in ran.stdout and 'runtime error:' not in ran.stdout
        results.append({'case':name,'compiled':True,'compile_command':cmd,'returncode':ran.returncode,'passed':passed,'mutant':change})
        print(f'{name}: {"PASS" if passed else "FAIL"}',flush=True)
        if not passed: print(ran.stdout,file=sys.stderr);return 1
    hashes={path:hashlib.sha256((args.root/path).read_bytes()).hexdigest() for path in [REL+'BlurayDiscSession.cpp',REL+'BlurayDiscSession.h',REL+'BlurayIsoCache.cpp',REL+'BlurayIsoCache.h',REL+'DVDInputStreamBluray.cpp','xbmc/application/Application.cpp','xbmc/utils/ScopeGuard.h']}
    (args.logs/'report.json').write_text(json.dumps({'sanitizers':args.sanitizers,'compiler_version':subprocess.check_output([os.environ.get('CXX','g++'),'--version'],text=True).splitlines()[0],'kind':'production session/queue/cache; extracted logical close/abort/failure guard and application gates; explicit host stand-ins','source_sha256':hashes,'results':results,'limitations':['native Java/network/device services substituted','host compiler is not CE compiler','timeouts/sanitizer crashes not accepted as negative controls']},indent=2)+'\n')
    return 0
if __name__=='__main__':sys.exit(main())
