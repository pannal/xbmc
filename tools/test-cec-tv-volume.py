#!/usr/bin/env python3
"""Exercise production CEC routing, discovery, queue and key mapping on host adapters.

Native libCEC, app volume, manager, thread lifecycle and clock are explicit test
adapters. Production method bodies and volume state declarations are extracted
unchanged except for a deterministic steady-clock provider. This does not test
physical HDMI/TV acceptance. Semantic mutations must fail the same assertions.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CEC_INCLUDE = Path(os.environ.get('CEC_INCLUDE', '/usr/include/libcec'))


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#include <cectypes.h>
#include "input/remote/IRRemote.h"
using namespace CEC;
using namespace std::chrono_literals;
using CCriticalSection=std::recursive_mutex;
constexpr int LOGDEBUG=0, LOGERROR=1, LOGWARNING=2, FEATURE_CEC=1;
constexpr int ACTION_MUTE=1,ACTION_VOLUME_UP=2,ACTION_VOLUME_DOWN=3;
constexpr int VOLUME_REFRESH_TIMEOUT=100,VOLUME_CHANGE_TIMEOUT=250;
std::function<void()> beforeMappedKey;
struct CLog { template<class... T> static void Log(int,const char* format,T&&...) {
 if(std::string(format).find("received key")!=std::string::npos && beforeMappedKey)beforeMappedKey();
} };
struct TestClock {
 static inline std::atomic<int> ms{1000};
 static auto now() { return std::chrono::steady_clock::time_point(std::chrono::milliseconds(ms.load())); }
};
struct Gate {
 std::mutex m; std::condition_variable cv; bool entered=false,released=false;
 void pause() { std::unique_lock<std::mutex> l(m);entered=true;cv.notify_all();assert(cv.wait_for(l,3s,[&]{return released;})); }
 void await() { std::unique_lock<std::mutex> l(m);assert(cv.wait_for(l,3s,[&]{return entered;})); }
 void release() { std::lock_guard<std::mutex> l(m);released=true;cv.notify_all(); }
};
struct Native {
 using Tx=std::tuple<char,int,int>;
 std::vector<Tx> traffic;
 std::vector<cec_device_type> probes;
 bool tv=true,avr=false,pressOK=true,releaseOK=true;
 std::function<void()> onPress,onRelease,onProbe;
 bool SendKeypress(cec_logical_address target,cec_user_control_code key,bool) {
  traffic.emplace_back('P',target,key); if(onPress)onPress(); return pressOK;
 }
 bool SendKeyRelease(cec_logical_address target,bool) {
  traffic.emplace_back('R',target,0);if(onRelease)onRelease();return releaseOK;
 }
 bool IsActiveDeviceType(cec_device_type type) {
  probes.push_back(type);if(onProbe)onProbe();return type==CEC_DEVICE_TYPE_TV?tv:avr;
 }
 std::string GetDeviceOSDName(cec_logical_address target) { return target==CECDEVICE_TV?"TV":"AVR"; }
};
struct CAction { int id; float amount=1.0f; int GetID() const{return id;} float GetAmount()const{return amount;} };
struct CApplicationVolumeHandling {
 static constexpr float VOLUME_MAXIMUM=1.0f;
 float gain=0.7f;bool m_muted=true;bool& muted=m_muted;int changes=0;
 void SetMute(bool mute);void SetLocalMute(bool mute);void ToggleMute();void Mute();void UnMute();
 void VolumeChanged(){++changes;}void SetVolume(float value,bool){gain=value;++changes;}
};
struct Components {
 std::shared_ptr<CApplicationVolumeHandling> app=std::make_shared<CApplicationVolumeHandling>();
 template<class T> auto GetComponent(){return app;}
};
struct IAE {bool muted=true;void SetMute(bool value){muted=value;}};
struct Messenger {
 std::mutex mutex;std::queue<std::function<void()>> pending;
 void PostCallback(std::function<void()> cb){std::lock_guard<std::mutex> lock(mutex);pending.push(std::move(cb));}
 void Drain(){while(true){std::function<void()> cb;{std::lock_guard<std::mutex> lock(mutex);if(pending.empty())return;cb=std::move(pending.front());pending.pop();}cb();}}
};
namespace PERIPHERALS {class CPeripherals;}
struct CServiceBroker {
 static Components& GetAppComponents(){static Components c;return c;}
 static Messenger* GetAppMessenger(){static Messenger m;return &m;}
 static IAE* GetActiveAE(){static IAE ae;return &ae;}
 static PERIPHERALS::CPeripherals& GetPeripherals();
};
namespace PERIPHERALS {
struct CecButtonPress { int iButton;unsigned int iDuration; };
enum CecVolumeChange { VOLUME_CHANGE_NONE,VOLUME_CHANGE_UP,VOLUME_CHANGE_DOWN,VOLUME_CHANGE_MUTE };
class CPeripheralCecAdapterUpdateThread;
class CPeripheralCecAdapter:public std::enable_shared_from_this<CPeripheralCecAdapter> {
public:
 Native native;Native* m_cecAdapter=&native;
 CCriticalSection m_critSection,m_lifecycleSection;
 std::atomic<uint64_t> m_sourceEventGeneration{7};
 std::atomic<bool> m_bStop{false},m_retired{false};
 std::vector<CecButtonPress> m_buttonQueue;CecButtonPress m_currentButton{};
 bool m_bHasButton=false,m_bIsReady=true;
 std::chrono::steady_clock::time_point m_lastCecKeypressTime{};
 struct Config {unsigned int iButtonRepeatRateMs=0;} m_configuration;
 CPeripheralCecAdapterUpdateThread* m_queryThread=nullptr;
 /*STATE*/
 CPeripheralCecAdapter(){m_lastChange=VOLUME_CHANGE_NONE;m_bIsMuted=false;}
 bool IsRunning()const{return !m_bStop;}
 bool HasAudioControl();bool HasAudioControlLocked()const;
 void SetTvVolumeControl(bool);void InvalidateVolumeControl();void ResetVolumeControl();
 void OnVolumeControlConnected(cec_logical_address,uint64_t,uint64_t);
 bool ReleaseVolumeKey(std::unique_lock<CCriticalSection>&);void ProcessVolumeChange(bool stopping=false);
 bool QueueVolumeChange(CecVolumeChange);bool VolumeUp();bool VolumeDown();bool ToggleMute();bool IsMuted();
 bool SuppressCecVolumeKey(cec_user_control_code)const;
 void PushCecKeypress(const cec_keypress&);void PushCecKeypress(const CecButtonPress&);
 void GetNextKey();int GetButton();unsigned int GetHoldTime();void ResetButton();
};
class CPeripheralCecAdapterUpdateThread {
public:
 CPeripheralCecAdapter* m_adapter;std::atomic<bool> m_audioControlRefreshPending{false};
 explicit CPeripheralCecAdapterUpdateThread(CPeripheralCecAdapter* a):m_adapter(a){}
 void RefreshAudioControl();std::string UpdateAudioSystemStatus();
};
using PeripheralVector=std::vector<std::shared_ptr<CPeripheralCecAdapter>>;
class CPeripherals {
public:
 bool supported=true;PeripheralVector devices;
 bool SupportsCEC(){return supported;}
 bool GetPeripheralsWithFeature(PeripheralVector& out,int){out=devices;return !out.empty();}
 bool OnAction(const CAction&);bool ToggleMute();bool Mute(){return ToggleMute();}bool UnMute(){return ToggleMute();}
};
}
using namespace PERIPHERALS;
CPeripherals* currentManager=nullptr;
CPeripherals& CServiceBroker::GetPeripherals(){assert(currentManager);return *currentManager;}
'''
TEST = r'''
int checks=0;
void check(bool value,const char* label){++checks;if(!value){std::cerr<<"FAIL "<<label<<"\n";std::abort();}}
struct Fixture {
 std::shared_ptr<CPeripheralCecAdapter> a=std::make_shared<CPeripheralCecAdapter>();
 CPeripheralCecAdapterUpdateThread q{a.get()};CPeripherals p;
 Fixture(){TestClock::ms=1000;a->m_queryThread=&q;a->m_volumeEnabled=true;p.devices={a};currentManager=&p;
 CServiceBroker::GetAppComponents().app=std::make_shared<CApplicationVolumeHandling>();CServiceBroker::GetActiveAE()->muted=true;}
 ~Fixture(){currentManager=nullptr;}
 void tv(){a->SetTvVolumeControl(true);q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();}
 void avr(){a->native.avr=true;q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();}
 void step(int ms=0){TestClock::ms+=ms;a->ProcessVolumeChange();}
 bool up(){return p.OnAction({ACTION_VOLUME_UP});}
 bool down(){return p.OnAction({ACTION_VOLUME_DOWN});}
 bool mute(){return p.OnAction({ACTION_MUTE});}
 void expect(std::initializer_list<Native::Tx> tx,const char* label){check(a->native.traffic==std::vector<Native::Tx>(tx),label);}
};
int main(){
 {Fixture f;f.q.UpdateAudioSystemStatus();check(!f.a->m_tvVolumeControl,"default policy");
 check(!f.up()&&!f.down()&&!f.mute(),"TV-only default local fallback");
 check(f.a->native.probes==std::vector<cec_device_type>{CEC_DEVICE_TYPE_AUDIO_SYSTEM},"default amplifier discovery");
 f.step();f.expect({},"default no TV traffic");}
 {Fixture f;f.avr();check(f.a->HasAudioControl(),"AVR availability");
 auto app=CServiceBroker::GetAppComponents().app;check(app->gain==1.0f&&!app->muted,"AVR normalizes local output");
 check(f.up(),"AVR route action consumed");f.step();f.step(251);
 f.expect({{'P',5,0x41},{'R',5,0}},"default AVR press and release");
 check(f.mute(),"AVR mute admitted");f.step();check(f.a->IsMuted(),"legacy AVR mute estimate");}
 {Fixture f;f.tv();check(f.q.m_audioControlRefreshPending,"setting requests discovery");
 check(f.a->native.probes==std::vector<cec_device_type>{CEC_DEVICE_TYPE_TV},"TV-only discovery");
 auto app=CServiceBroker::GetAppComponents().app;check(app->gain==1.0f&&!app->muted&&app->changes==2,"TV normalizes local gain and mute before routing");
 check(f.up(),"TV route consumed");f.step();f.step(251);
 f.expect({{'P',0,0x41},{'R',0,0}},"TV short press release");}
 {Fixture f;f.tv();f.up();f.step();f.step(50);f.up();f.step();
 f.step(51);f.up();f.step();f.step(101);f.up();f.step();f.step(251);
 f.expect({{'P',0,0x41},{'P',0,0x41},{'P',0,0x41},{'R',0,0}},"held repeats release once");}
 {Fixture f;f.tv();f.up();f.step();check(f.down(),"direction admitted");f.step(20);
 check(f.mute(),"TV mute admitted");f.step(20);check(!f.a->IsMuted(),"TV status remains unknown");
 f.step(120);f.mute();f.step();f.step(251);
 f.expect({{'P',0,0x41},{'R',0,0},{'P',0,0x42},{'R',0,0},{'P',0,0x43},{'R',0,0}},"direction and mute paired; held mute once");}
 {Fixture f;f.tv();f.up();f.step();f.down();f.a->SetTvVolumeControl(false);f.avr();f.mute();f.step(120);
 f.expect({{'P',0,0x41},{'R',0,0},{'P',5,0x43}},"setting change releases old TV and discards queued down");
 check(CServiceBroker::GetAppComponents().app->gain==1.0f,"default restored AVR normalization");}
 {Fixture f;f.tv();f.up();f.step();f.a->SetTvVolumeControl(false);f.a->SetTvVolumeControl(true);
 f.q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();f.up();f.step(120);
 f.expect({{'P',0,0x41},{'R',0,0},{'P',0,0x41}},"same-recipient new generation still releases");}
 {Fixture f;f.tv();f.up();f.a->SetTvVolumeControl(false);f.q.UpdateAudioSystemStatus();f.step(300);
 f.expect({},"queued press never survives target switch");check(!f.up(),"no AVR on restoration falls back local");}
 {Fixture f;f.a->native.tv=false;f.tv();check(!f.up()&&!f.mute(),"missing TV local fallback");f.step();f.expect({},"missing TV no send");}
 {Fixture f;f.tv();f.up();f.a->m_volumeEnabled=false;f.a->InvalidateVolumeControl();check(!f.up(),"disabled admission");
 f.step();f.expect({},"disabled queued work cleared");}
 {Fixture f;f.tv();f.up();f.step();f.a->m_volumeEnabled=false;f.a->InvalidateVolumeControl();f.step(120);
 f.expect({{'P',0,0x41},{'R',0,0}},"disabled release saved recipient");}
 {Fixture f;f.tv();f.up();f.step();f.down();f.a->ProcessVolumeChange(true);
 f.expect({{'P',0,0x41},{'R',0,0}},"shutdown flushes work and releases held key");check(!f.up(),"shutdown closes admission");}
 {Fixture f;f.tv();f.up();f.a->m_volumeConnectionLost=true;check(!f.down(),"loss admission fence");f.step();
 f.expect({},"loss drops queued command");f.a->m_volumeConnectionLost=false;f.q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();f.step();
 f.expect({},"reconnect never replays queued command");}
 {Fixture f;f.tv();f.up();f.step();f.a->m_volumeConnectionLost=true;f.step();f.a->m_volumeConnectionLost=false;
 f.q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();f.down();f.step();f.step(251);
 f.expect({{'P',0,0x41},{'P',0,0x42},{'R',0,0}},"no old release enters reconnected session");}
 {Fixture f;f.tv();f.a->native.pressOK=false;f.mute();f.step();
 f.expect({{'P',0,0x43},{'R',0,0}},"failed press attempts release");check(!f.up()&&!f.a->IsMuted(),"failed send local fallback and no mute claim");}
 {Fixture f;f.tv();f.up();f.step();f.a->native.releaseOK=false;f.down();f.step(120);
 check(!f.up(),"failed release closes admission");f.a->native.releaseOK=true;f.step(120);
 f.expect({{'P',0,0x41},{'R',0,0},{'R',0,0}},"failed release retries only saved recipient; no next press");}
 {Fixture f;f.tv();f.up();f.step();f.a->native.releaseOK=false;
 for(int i=0;i<10;i++)f.step(300);
 check(f.a->native.traffic.size()==4,"failed release retries bounded to three");check(!f.up(),"release exhaustion uses local fallback");
 f.a->ProcessVolumeChange(true);check(f.a->native.traffic.size()==5,"shutdown final release attempt after exhaustion");}
 {Fixture f;f.tv();f.up();f.step();f.a->native.releaseOK=false;
 for(int i=0;i<3;i++){f.step(300);}f.a->ProcessVolumeChange(true);
 f.a->ResetVolumeControl();f.a->native.traffic.clear();f.a->native.releaseOK=true;f.a->m_volumeEnabled=true;
 f.q.UpdateAudioSystemStatus();CServiceBroker::GetAppMessenger()->Drain();check(f.up(),"new enabled native session recovers from exhausted release");f.step();f.step(251);
 f.expect({{'P',0,0x41},{'R',0,0}},"new session never releases old native recipient");}
 {Fixture f;f.tv();f.a->native.onPress=[&]{f.a->m_volumeConnectionLost=true;};f.up();f.step();
 check(!f.down(),"synchronous callback closes admission without lock deadlock");f.step(300);
 f.expect({{'P',0,0x41}},"no native send after connection-loss callback");}
 {Fixture f;f.tv();f.a->m_retired=true;check(!f.up()&&!f.mute(),"retired admission");}
 {Fixture f;f.tv();f.a->m_bStop=true;check(!f.up()&&!f.mute(),"stopped admission");}
 {Fixture f;f.a->SetTvVolumeControl(true);f.q.UpdateAudioSystemStatus();
 check(!f.a->HasAudioControl(),"TV routing waits for main normalization");CServiceBroker::GetAppMessenger()->Drain();
 check(f.a->HasAudioControl()&&CServiceBroker::GetAppComponents().app->gain==1.0f&&!CServiceBroker::GetAppComponents().app->muted,"TV publishes only normalized output");}
 {Fixture f;f.a->native.tv=false;f.tv();auto app=CServiceBroker::GetAppComponents().app;
 check(app->gain==0.7f&&app->muted&&app->changes==0,"unavailable target does not normalize local output");}
 {Fixture f;f.tv();f.a->native.pressOK=false;f.up();f.step();
 check(CServiceBroker::GetAppComponents().app->gain==1.0f&&!CServiceBroker::GetAppComponents().app->muted,"send failure fallback keeps normalized level");}
 {Fixture f;f.tv();f.p.supported=false;check(!f.up()&&!f.mute(),"CEC unsupported manager fallback");}
 {Fixture f;f.tv();for(auto key:{CEC_USER_CONTROL_CODE_VOLUME_UP,CEC_USER_CONTROL_CODE_VOLUME_DOWN,
 CEC_USER_CONTROL_CODE_MUTE,CEC_USER_CONTROL_CODE_MUTE_FUNCTION,CEC_USER_CONTROL_CODE_RESTORE_VOLUME_FUNCTION}) {
 cec_keypress k;k.keycode=key;k.duration=0;f.a->PushCecKeypress(k);k.duration=500;f.a->PushCecKeypress(k);}
 check(f.a->GetButton()==0,"incoming volume press repeat release suppressed in TV mode");
 cec_keypress k;k.keycode=CEC_USER_CONTROL_CODE_UP;k.duration=0;f.a->PushCecKeypress(k);
 check(f.a->GetButton()==XINPUT_IR_REMOTE_UP,"incoming navigation preserved");}
 {Fixture f;cec_keypress k;k.keycode=CEC_USER_CONTROL_CODE_VOLUME_UP;k.duration=0;f.a->PushCecKeypress(k);
 check(f.a->GetButton()==XINPUT_IR_REMOTE_VOLUME_PLUS,"default incoming volume mapping unchanged");
 f.a->SetTvVolumeControl(true);check(f.a->GetButton()==0,"policy switch clears already mapped volume input");}
 {Fixture f;Gate key;beforeMappedKey=[&]{key.pause();};
 cec_keypress k;k.keycode=CEC_USER_CONTROL_CODE_VOLUME_UP;k.duration=0;
 std::thread callback([&]{f.a->PushCecKeypress(k);});key.await();f.tv();key.release();callback.join();beforeMappedKey={};
 check(f.a->GetButton()==0,"callback that passed raw guard before opt-in cannot echo after queue clear");}
 {Fixture f;f.tv();f.up();f.step();f.a->native.tv=false;f.q.UpdateAudioSystemStatus();f.step(120);
 check(!f.up(),"discovered missing target falls back");f.expect({{'P',0,0x41},{'R',0,0}},"missing target releases prior recipient");}
 {Fixture f;f.tv();Gate probe;std::atomic<int> probes{0};
 f.a->native.onProbe=[&]{if(probes.fetch_add(1)==0)probe.pause();};
 std::thread discovery([&]{f.q.UpdateAudioSystemStatus();});probe.await();f.a->SetTvVolumeControl(false);f.avr();
 check(f.a->m_volumeTarget==CECDEVICE_AUDIOSYSTEM,"new AVR route established before stale TV discovery returns");
 probe.release();discovery.join();CServiceBroker::GetAppMessenger()->Drain();
 check(f.a->HasAudioControl()&&f.a->m_volumeTarget==CECDEVICE_AUDIOSYSTEM,"stale discovery cannot destroy or replace newer route");
 check(CServiceBroker::GetAppComponents().app->changes==4,"stale TV discovery cannot repeat normalization");}
 {Fixture f;f.tv();Gate send;f.a->native.onPress=[&]{send.pause();};f.up();
 std::thread worker([&]{f.step();});send.await();std::atomic<bool> switched{false};
 std::thread settings([&]{f.a->SetTvVolumeControl(false);switched=true;});
 // Main producers and settings must stay responsive while native transport is blocked.
 auto deadline=std::chrono::steady_clock::now()+1s;
 while(!switched && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
 check(switched,"settings remain responsive during native send");
 send.release();worker.join();settings.join();f.a->native.onPress={};f.step(120);
 f.expect({{'P',0,0x41},{'R',0,0}},"concurrent switch releases admitted recipient");}
 {Fixture f;f.a->native.avr=true;f.q.UpdateAudioSystemStatus();
 check(!f.a->HasAudioControl(),"AVR waits for main local normalization");f.a->SetTvVolumeControl(true);
 CServiceBroker::GetAppMessenger()->Drain();check(CServiceBroker::GetAppComponents().app->changes==0,"stale main normalization rejected after TV selection");}
 {Fixture f;f.a->native.avr=true;f.q.UpdateAudioSystemStatus();f.a->native.avr=false;f.q.UpdateAudioSystemStatus();
 CServiceBroker::GetAppMessenger()->Drain();
 check(!f.a->HasAudioControl(),"new absent discovery cancels queued AVR publication");
 check(CServiceBroker::GetAppComponents().app->changes==0,"new absent discovery cancels queued local normalization");}
 {Fixture f;f.a->native.avr=true;f.q.UpdateAudioSystemStatus();f.a->m_retired=true;
 CServiceBroker::GetAppMessenger()->Drain();check(CServiceBroker::GetAppComponents().app->changes==0,"retired main normalization rejected");}
 {Fixture f;f.a->native.avr=true;f.q.UpdateAudioSystemStatus();++f.a->m_sourceEventGeneration;
 CServiceBroker::GetAppMessenger()->Drain();check(CServiceBroker::GetAppComponents().app->changes==0,"reconnected main normalization rejected");}
 {Fixture f;f.a->native.avr=true;f.q.UpdateAudioSystemStatus();f.p.devices.clear();f.a.reset();
 CServiceBroker::GetAppMessenger()->Drain();check(CServiceBroker::GetAppComponents().app->changes==0,"destroyed main normalization weak ownership");}
 {Fixture f;f.tv();auto other=std::make_shared<CPeripheralCecAdapter>();other->m_volumeEnabled=true;other->m_volumeTarget=CECDEVICE_AUDIOSYSTEM;
 f.p.devices.push_back(other);CServiceBroker::GetAppComponents().app->m_muted=true;
 f.a->SetTvVolumeControl(false);f.avr();
 check(other->m_volumeChangeQueue.empty(),"AVR local unmute cannot toggle another adapter");
 check(!CServiceBroker::GetActiveAE()->muted,"local AE actually unmuted");}
 {Fixture f;f.tv();auto app=CServiceBroker::GetAppComponents().app;app->SetLocalMute(false);
 check(!app->muted&&!CServiceBroker::GetActiveAE()->muted,"local-only API updates app and AE");
 f.step();f.expect({},"local-only API sends no TV key");app->SetMute(true);f.step();
 f.expect({{'P',0,0x43}},"ordinary SetMute retains peripheral routing");}
 {Fixture f;f.avr();Gate send;f.a->native.onPress=[&]{send.pause();};f.mute();
 std::thread worker([&]{f.step();});send.await();std::atomic<bool> produced{false};
 std::thread producer([&]{f.down();produced=true;});auto deadline=std::chrono::steady_clock::now()+1s;
 while(!produced && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
 check(produced,"volume producer remains responsive during native send");producer.join();f.a->SetTvVolumeControl(false);f.avr();
 send.release();worker.join();f.a->native.onPress={};check(!f.a->IsMuted()&&!f.a->m_bIsMuted,"old AVR mute result cannot alter new AVR status");f.step(120);
 f.expect({{'P',5,0x43},{'R',5,0}},"old-generation send releases saved AVR recipient after policy refresh");}
 {Fixture f;f.avr();f.up();f.step();f.down();Gate release;
 f.a->native.onRelease=[&]{release.pause();};std::thread worker([&]{f.step(20);});release.await();f.tv();
 release.release();worker.join();f.a->native.onRelease={};
 f.expect({{'P',5,0x41},{'R',5,0}},"direction queued before target change cannot press new target after release");
 f.mute();f.step();f.expect({{'P',5,0x41},{'R',5,0},{'P',0,0x43}},"fresh-generation command uses new target");}
 {Fixture f;f.tv();for(int i=0;i<200;i++) {if(i%2)f.up();else f.down();}
 check(f.a->m_volumeChangeQueue.size()<=32,"bounded alternating producer backlog");}
 std::cout<<"PASS "<<checks<<" CEC routing/queue/discovery/feedback/lifecycle assertions\n";
}
'''


def source_text():
    adapter = (ROOT / 'xbmc/peripherals/devices/PeripheralCecAdapter.cpp').read_text()
    header = (ROOT / 'xbmc/peripherals/devices/PeripheralCecAdapter.h').read_text()
    peripherals = (ROOT / 'xbmc/peripherals/Peripherals.cpp').read_text()
    methods = [
        'bool CPeripheralCecAdapter::HasAudioControl(',
        'bool CPeripheralCecAdapter::HasAudioControlLocked(',
        'void CPeripheralCecAdapter::InvalidateVolumeControl(',
        'void CPeripheralCecAdapter::ResetVolumeControl(',
        'void CPeripheralCecAdapter::SetTvVolumeControl(',
        'void CPeripheralCecAdapter::OnVolumeControlConnected(',
        'bool CPeripheralCecAdapter::ReleaseVolumeKey(',
        'void CPeripheralCecAdapter::ProcessVolumeChange(',
        'bool CPeripheralCecAdapter::QueueVolumeChange(',
        'bool CPeripheralCecAdapter::VolumeUp(',
        'bool CPeripheralCecAdapter::VolumeDown(',
        'bool CPeripheralCecAdapter::ToggleMute(',
        'bool CPeripheralCecAdapter::IsMuted(',
        'bool CPeripheralCecAdapter::SuppressCecVolumeKey(',
        'void CPeripheralCecAdapter::PushCecKeypress(const cec_keypress&',
        'void CPeripheralCecAdapter::PushCecKeypress(const CecButtonPress&',
        'void CPeripheralCecAdapter::GetNextKey(',
        'int CPeripheralCecAdapter::GetButton(',
        'unsigned int CPeripheralCecAdapter::GetHoldTime(',
        'void CPeripheralCecAdapter::ResetButton(',
        'void CPeripheralCecAdapterUpdateThread::RefreshAudioControl(',
        'std::string CPeripheralCecAdapterUpdateThread::UpdateAudioSystemStatus(',
    ]
    state = header[header.index('  std::queue<CecVolumeChange>'):header.index('  bool m_bGoingToStandby;')]
    bodies = '\n'.join(block(adapter, m) for m in methods)
    app = (ROOT / 'xbmc/application/ApplicationVolumeHandling.cpp').read_text()
    for method in ['SetLocalMute(bool', 'SetMute(bool', 'ToggleMute(void', 'Mute()', 'UnMute()']:
        bodies += '\n' + block(app, 'void CApplicationVolumeHandling::' + method)
    bodies += '\n' + block(peripherals, 'bool CPeripherals::OnAction(')
    bodies += '\n' + block(peripherals, 'bool CPeripherals::ToggleMute(')
    # Lifecycle integration guards supplement runtime tests; native/thread owners
    # are adapters here and are separately exercised by test-cec-adapter-lifetime.py.
    process = block(adapter, 'void CPeripheralCecAdapter::Process(')
    assert process.index('StopThread(true)') < process.index('ProcessVolumeChange(true)') < process.index('m_cecAdapter->Close()')
    reset = block(adapter, 'void CPeripheralCecAdapter::ResetMembers(')
    assert 'ResetVolumeControl()' in reset and 'm_buttonQueue.clear()' in reset
    initialize = block(adapter, 'bool CPeripheralCecAdapter::InitialiseFeature(')
    assert initialize.index('StopThread(true)') < initialize.index('CECDestroy(m_cecAdapter)') < initialize.index('ResetVolumeControl()') < initialize.index('m_cecAdapter = CECInitialise(')
    assert initialize.index('CECDestroy(m_cecAdapter)') < initialize.index('m_buttonQueue.clear()') < initialize.index('m_cecAdapter = CECInitialise(')
    setting = block(adapter, 'void CPeripheralCecAdapter::OnSettingChanged(')
    assert 'SetTvVolumeControl(GetSettingBool("volume_control_tv"))' in setting
    assert 'm_volumeEnabled = false' in setting and 'InvalidateVolumeControl()' in setting
    alert = block(adapter, 'void CPeripheralCecAdapter::CecAlert(')
    assert alert.index('m_volumeConnectionLost = true') < alert.index('ReopenConnection(true)')
    assert 'm_volumeSection' not in block(adapter, 'void CPeripheralCecAdapter::CecKeyPress(')
    return PRELUDE.replace('/*STATE*/', state) + bodies.replace('std::chrono::steady_clock::now()', 'TestClock::now()') + TEST


CONTROLS = {
    'wrong-TV-recipient': ('m_pressedVolumeTarget = m_volumeTarget;', 'm_pressedVolumeTarget = CECDEVICE_AUDIOSYSTEM;'),
    'wrong-release-recipient': ('SendKeyRelease(target, false)', 'SendKeyRelease(m_volumeTarget, false)'),
    'no-generation-release': ('m_pressedVolumeGeneration != m_volumeGeneration', 'false'),
    'retain-stale-queue': ('m_volumeChangeQueue = {};', '/* unsafe: retain queue */'),
    'repeat-mute-toggle': ('change == VOLUME_CHANGE_MUTE || sincePress.count()', 'false || sincePress.count()'),
    'claim-TV-mute-status': ('!m_tvVolumeControl && HasAudioControlLocked() && m_bIsMuted', 'HasAudioControlLocked()'),
    'race-incoming-volume': ('if (m_tvVolumeControl &&\n      (key.iButton', 'if (false &&\n      (key.iButton'),
    'publish-stale-discovery': ('generation != m_adapter->m_volumeGeneration ||', '(false && generation != m_adapter->m_volumeGeneration) ||'),
    'accept-stopped': ('!m_volumeConnectionLost && !m_bStop', '!m_volumeConnectionLost && true'),
    'accept-after-loss': ('!m_volumeConnectionLost && !m_bStop', 'true && !m_bStop'),
    'ignore-send-failure': ('if (!sent)', 'if (false)'),
    'stale-direction-after-release': (' || generation != m_volumeGeneration)', ')'),
    'stale-mute-result': (' && generation == m_volumeGeneration)', ')'),
    'block-GUI-on-native-send': [('lock.unlock();\n  const bool sent', '/* unsafe: hold lock */\n  const bool sent'), ('lock.lock();\n  CLog::Log(sent', '/* unsafe: already locked */\n  CLog::Log(sent')],
    'unbounded-release-retry': ('if (!stopping && m_volumeReleaseFailures >= 3)', 'if (false)'),
    'retain-exhausted-release-on-enable': ('  m_volumeReleaseFailures = 0;\n  m_lastChange', '  /* unsafe: retain release exhaustion */\n  m_lastChange'),
    'skip-TV-local-normalization': ('  if (present)\n', '  if (present && !tv)\n'),
    'stale-main-normalization': ('if (!current())', 'if (false)'),
    'retain-superseded-normalization': (' || m_adapter->m_audioNormalizationPending)', ')'),
    'route-local-unmute-to-peripheral': ('appVolume->SetLocalMute(false)', 'appVolume->SetMute(false)'),
    'consume-local-fallback': ('if (action.GetID() == ACTION_VOLUME_UP ? cecDevice->VolumeUp() : cecDevice->VolumeDown())', 'if (true)'),
}


def run(out, controls, cec_include):
    source = source_text()
    reports = []
    for name, change in [('candidate', None)] + (list(CONTROLS.items()) if controls else []):
        text = source
        if change:
            for old, new in (change if isinstance(change, list) else [change]):
                assert old in text, (name, old)
                text = text.replace(old, new)
        cpp = out / (name + '.cpp')
        cpp.write_text(text)
        argv = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-variable', '-pthread', '-fsanitize=address,undefined',
                '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                '-I' + str(ROOT / 'xbmc'), '-I' + str(cec_include), str(cpp), '-o', str(out / name)]
        (out / (name + '.argv.json')).write_text(json.dumps(argv, indent=2) + '\n')
        compiled = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        (out / (name + '.compile.log')).write_text(compiled.stdout + compiled.stderr)
        assert compiled.returncode == 0, name + ': ' + compiled.stderr
        p = subprocess.run([str(out / name)], capture_output=True, text=True, timeout=15,
                           env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1'})
        (out / (name + '.run.log')).write_text(p.stdout + p.stderr)
        if change:
            assert p.returncode != 0 and 'FAIL ' in p.stderr, name + ' did not fail semantically: ' + p.stdout + p.stderr
            reports.append(name + ': rejected (' + p.stderr.strip() + ')')
        else:
            assert p.returncode == 0, p.stdout + p.stderr
            reports.append(p.stdout.strip())
        print(reports[-1], flush=True)
    (out / 'results.json').write_text(json.dumps(reports, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--controls', action='store_true')
    parser.add_argument('--cec-include', type=Path, default=CEC_INCLUDE)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        run(args.output, args.controls, args.cec_include)
    else:
        with tempfile.TemporaryDirectory(prefix='cec-tv-volume-') as temporary:
            run(Path(temporary), args.controls, args.cec_include)
