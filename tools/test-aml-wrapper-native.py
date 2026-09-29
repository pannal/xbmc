#!/usr/bin/env python3
"""Run admitted wrapper Close, its actual CD/CS restoration policy and destructor body.

Uses real session/native gates, registration receipts and extracted lifecycle
methods. Settings storage/callback effects, sysfs and decoder internals record
behavior; this is not device or full-settings-stack verification.
"""
import argparse
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
fixture=runpy.run_path(str(ROOT/'tools/test-aml-lifecycle.py'))
function=fixture['function']
SUPPORT=r'''
#include "settings/lib/SettingCallbackRegistration.h"
std::atomic<bool> allowEffects{true};
std::atomic<int> resetEffects{0};
std::function<void()> effectHook;
std::vector<std::string> order;
void NativeEffect(const std::string& effect){
  assert(allowEffects);
  auto display=CAMLSession::FenceDisplay();
  assert(!CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::CancelDisplay(display));
  ++resetEffects;order.push_back(effect);if(effectHook)effectHook();
}
enum DV_MODE{DV_MODE_ON,DV_MODE_ON_DEMAND};
enum class StreamHdrType{HDR_TYPE_HDR10,HDR_TYPE_SDR};
constexpr int DOLBY_VISION_OUTPUT_MODE_IPT=1;
DV_MODE mode=DV_MODE_ON;
bool aml_linux_force_422=true,vs10_conversion_reset_hdr10=true;
DV_MODE aml_dv_mode(){return mode;}
void aml_dv_on(int output){assert(output==DOLBY_VISION_OUTPUT_MODE_IPT);NativeEffect("dv-on");}
struct CSysfsPath{CSysfsPath(const char*,bool value){assert(!value);NativeEffect("kernel422");}};
struct Advanced {
  bool limit=true,force=true;int limitPrev=2,forcePrev=1;
  bool GetLimitCD(){return limit;}bool GetForceCS(){return force;}
  int GetLimitCDPrevVal(){return limitPrev;}int GetForceCSPrevVal(){return forcePrev;}
  void SetLimitCD(bool v){limit=v;}void SetForceCS(bool v){force=v;}
  void SetLimitCDPrevVal(int v){limitPrev=v;}void SetForceCSPrevVal(int v){forcePrev=v;}
} advanced;
struct Registry {
  std::shared_ptr<CSettingCallbackRegistration> record;
  std::atomic<bool> revoked{false};
  auto RevokeCallback(ISettingCallback*){if(record)record->Revoke();revoked=true;return record;}
} registry;
struct CSettings {
  static constexpr int SETTING_COREELEC_AMLOGIC_LIMIT_CD=1,SETTING_COREELEC_AMLOGIC_FORCE_CS=2;
  int cd=3,cs=3;
  void SetInt(int key,int value){if(key==1)cd=value;else cs=value;NativeEffect(key==1?"cd":"cs");}
  auto* GetSettingsManager(){return &registry;}
} settingsInstance;
CSettings* settings(){return &settingsInstance;}
struct Services {
  auto* GetSettings(){return settings();}auto* GetAdvancedSettings(){return &advanced;}
  StreamHdrType GetVideoHdrType(){return StreamHdrType::HDR_TYPE_HDR10;}
};
struct CServiceBroker {
  static auto* GetSettingsComponent(){static Services services;return &services;}
  static auto& GetDataCacheCore(){return *GetSettingsComponent();}
};
@RESET@
'''
TESTS=r'''
void resetPolicy(DV_MODE value=DV_MODE_ON){
  allowEffects=true;resetEffects=0;effectHook={};order.clear();advanced={};
  mode=value;aml_linux_force_422=true;vs10_conversion_reset_hdr10=true;
}
void open(CAMLSession& session){auto r=session.Fence();assert(session.BeginMutation(r));assert(session.Complete(r,true));}
void WrapperClose(bool core){
  resetPolicy();CAMLSession other;open(other);
  CDVDVideoCodecAmlogic wrapper;auto original=wrapper.m_Codec;
  if(core)assert(original->OpenDecoder());else{wrapper.m_Codec.reset();original.reset();}
  auto held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());assert(*held);
  const auto expectedPool=wrapper.m_videoBufferPool;
  std::thread::id owner;
  effectHook=[&]{assert(std::this_thread::get_id()==owner);
    assert(wrapper.m_videoBufferPool==expectedPool);
    assert(wrapper.m_Codec==original);
    if(core){assert(original->m_nativeLifecycleRequest);
      assert(original->m_nativeLifecycleRequest->phase==CAMLSession::NativeRequest::Phase::ACTIVE);
      // A nested same-close retry must not attach a second restoration prefix.
      assert(!original->CloseDecoder([]{assert(false);}));}
  };
  if(core)original->onClose=[&]{
    assert(!wrapper.m_videoBufferPool);assert((order==std::vector<std::string>{"cd","cs","kernel422","dv-on"}));
    assert(wrapper.m_Codec==original);order.push_back("close");};
  allowEffects=false;
  auto close=std::async(std::launch::async,[&]{owner=std::this_thread::get_id();wrapper.Close();});
  assert(close.wait_for(20ms)==std::future_status::timeout && resetEffects==0);
  allowEffects=true;held.reset();close.get();effectHook={};
  assert(!wrapper.m_Codec && !wrapper.m_videoBufferPool);
  assert(settings()->cd==2 && settings()->cs==1 && !advanced.limit && !advanced.force);
  assert(!advanced.limitPrev && !advanced.forcePrev && !aml_linux_force_422 && !vs10_conversion_reset_hdr10);
  assert(resetEffects==4);
  if(core){original->onClose={};assert(original->CloseDecoder());} // no orphan nested prefix
}
void NoCoreDisplayAndPolicy(){
  resetPolicy(DV_MODE_ON_DEMAND);CDVDVideoCodecAmlogic wrapper;wrapper.m_Codec.reset();
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  allowEffects=false;
  auto close=std::async(std::launch::async,[&]{wrapper.Close();});
  assert(close.wait_for(20ms)==std::future_status::timeout && resetEffects==0);
  allowEffects=true;assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));close.get();
  assert((order==std::vector<std::string>{"cd","cs","kernel422"}));
  assert(!vs10_conversion_reset_hdr10 && !wrapper.m_videoBufferPool);
}
void PrefixIdentity(){
  CAMLSession other;open(other);CAMLCodec codec;assert(codec.OpenDecoder());int first=0,second=0;
  auto held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  assert(!codec.CloseDecoder([&]{++first;}));
  assert(!codec.CloseDecoder([&]{++second;}));assert(first==0 && second==0);
  held.reset();assert(codec.ContinueLifecycle());assert(first==1 && second==0);
  // Superseding an unstarted Close cancels its dependent prefix.
  assert(codec.OpenDecoder());held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  assert(!codec.CloseDecoder([&]{++first;}));assert(!codec.Reset());
  held.reset();assert(codec.ContinueLifecycle());assert(first==1);
  // Same-operation transfer after the old owner joined preserves its prefix.
  held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  std::thread prior([&]{assert(!codec.CloseDecoder([&]{++first;}));});prior.join();
  assert(!codec.CloseDecoder([&]{++second;}));held.reset();assert(codec.ContinueLifecycle());
  assert(first==2 && second==0);
}
void Exceptions(bool core){
  resetPolicy();CDVDVideoCodecAmlogic wrapper;
  if(core)assert(wrapper.m_Codec->OpenDecoder());else wrapper.m_Codec.reset();
  effectHook=[] {throw 5;};
  try{wrapper.Close();assert(false);}catch(int){}
  effectHook={};assert(wrapper.m_videoBufferPool);
  auto token=CAMLSession::FenceNative();assert(token && CAMLSession::TryBeginNative(token));
  assert(CAMLSession::EndNative(token));
  if(core){assert(wrapper.m_Codec->LifecycleFailed());
    assert(!wrapper.m_Codec->m_beforeClose); // consumed, not replayed by continuation
    assert(!wrapper.m_Codec->ContinueLifecycle());
    assert(wrapper.m_Codec->CloseDecoder());}
}
void CallbackDrain(){
  resetPolicy();CDVDVideoCodecAmlogic wrapper;assert(wrapper.m_Codec->OpenDecoder());
  wrapper.m_settingsCallbackRegistered=true;registry.revoked=false;
  registry.record=std::make_shared<CSettingCallbackRegistration>(&wrapper);
  auto copy=registry.record;
  auto lease=std::make_unique<CSettingCallbackRegistration::Lease>(copy->Acquire());assert(*lease);
  allowEffects=false;
  auto destruction=std::async(std::launch::async,[&]{wrapper.RetireAndClose();});
  auto deadline=std::chrono::steady_clock::now()+2s;
  while(!registry.revoked){assert(std::chrono::steady_clock::now()<deadline);std::this_thread::yield();}
  assert(!copy->Acquire() && !copy->Drained());
  assert(destruction.wait_for(20ms)==std::future_status::timeout && resetEffects==0);
  allowEffects=true;lease.reset();destruction.get();assert(copy->Drained() && !wrapper.m_Codec);
  registry.record.reset();
}
int main(){WrapperClose(true);WrapperClose(false);NoCoreDisplayAndPolicy();PrefixIdentity();Exceptions(true);Exceptions(false);CallbackDrain();}
'''


def source():
    full=fixture['harness']();assert full.endswith(fixture['TESTS'])
    code=full[:-len(fixture['TESTS'])].replace('void aml_kodi_reset_cd_cs() {}','void aml_kodi_reset_cd_cs();')
    support=SUPPORT.replace('@RESET@',function((ROOT/'xbmc/utils/AMLUtils.cpp').read_text(),'void aml_kodi_reset_cd_cs()'))
    code=code.replace('class CDVDVideoCodecAmlogic {',support+'\nclass CDVDVideoCodecAmlogic: public ISettingCallback {')
    code=code.replace('  void Close(); void Reset();','  bool m_settingsCallbackRegistered=false;\n  void RetireAndClose();\n  void Close(); void Reset();')
    destructor=function((ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text(),
        'CDVDVideoCodecAmlogic::~CDVDVideoCodecAmlogic()')
    destructor=destructor.replace('CDVDVideoCodecAmlogic::~CDVDVideoCodecAmlogic()', 'void CDVDVideoCodecAmlogic::RetireAndClose()')
    return code+destructor+TESTS


def run(code,negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-wrapper-native-') as tmp:
        out=Path(tmp);(out/'test.cpp').write_text(code)
        result=subprocess.run(['g++','-std=c++17','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer',
            '-fno-pie','-no-pie','-I',str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        result=subprocess.run([str(out/'test')],capture_output=True,text=True,timeout=15)
        if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result.stdout+result.stderr
        else:assert result.returncode==0,result.stdout+result.stderr


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code);print('PASS: wrapper restoration, core/no-core admission and callback drainage (ASan/UBSan)')
    if args.negative_controls:
        for name,old,new in [
            ('restore before core admission','    m_Codec->CloseDecoder(std::move(restore));','    restore();\n    m_Codec->CloseDecoder();'),
            ('drop retained close prefix','auto beforeClose = std::move(m_beforeClose);','auto beforeClose = [] {};'),
            ('let retry replace prefix','  if (m_lifecycle != operation)\n  {','  if (m_lifecycle != operation || beforeClose)\n  {'),
            ('skip no-core admission','      if (CAMLSession::TryBeginNative(request))','      if (true)'),
            ('leak no-core token on exception','      CAMLSession::EndNative(request);\n      throw;', '      throw;'),
            ('skip active callback drain','while (receipt && !receipt->Drained())','while (false && receipt && !receipt->Drained())'),
            ('restore DV in on-demand policy','      aml_dv_mode() == DV_MODE_ON)', '      true)'),
        ]:
            assert code.count(old)==1,name;run(code.replace(old,new),True);print('rejected:',name)


if __name__=='__main__':main()
