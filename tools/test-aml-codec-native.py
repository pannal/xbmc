#!/usr/bin/env python3
"""Production codec lifecycle under real all-session native/display admission.

Reuse only the lifecycle fixture's recording device scaffolding and extracted
methods. These cases test the new outer boundary, not actual driver/DV effects.
"""
import argparse
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
fixture=runpy.run_path(str(ROOT/'tools/test-aml-lifecycle.py'))
TESTS=r'''
static void OpenSession(CAMLSession& session){auto r=session.Fence();assert(session.BeginMutation(r));assert(session.Complete(r,true));}
static void AcrossSessions(){
  CAMLSession other;OpenSession(other);CAMLCodec codec;assert(codec.OpenDecoder());codec.trace.clear();
  auto held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());assert(*held);
  const auto epoch=codec.m_session.Epoch();
  assert(!codec.Reset());auto token=codec.m_nativeLifecycleRequest;assert(token);
  for(int i=0;i<4;++i){assert(!codec.ContinueLifecycle());assert(codec.m_nativeLifecycleRequest==token);}
  assert(codec.trace.empty() && codec.m_session.Epoch()==epoch);
  CAMLSession newcomer;assert(!newcomer.BeginMutation(newcomer.Fence()));
  auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
  held.reset();assert(CAMLSession::TryBeginDisplay(display));
  assert(!CAMLSession::TryBeginNative(token));
  assert(!codec.ContinueLifecycle() && codec.trace.empty());
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(codec.ContinueLifecycle());assert((codec.trace==std::vector<std::string>{"reset"}));
  assert(token->phase==CAMLSession::NativeRequest::Phase::COMPLETED);
  assert(!codec.m_nativeLifecycleRequest && codec.m_session.Epoch()==epoch+1);
  assert(other.AcquireDecoder());
  assert(!CAMLSession::EndNative(token) && !CAMLSession::CancelNative(token));
}
static void ExactNestedSession(){
  CAMLSession first,second;OpenSession(first);OpenSession(second);
  auto a=first.Fence(),b=second.Fence();auto token=CAMLSession::FenceNative(a.identity);
  assert(CAMLSession::TryBeginNative(token));
  assert(!second.BeginMutation(b,token)); // exact outer target, not merely current thread
  auto foreign=std::async(std::launch::async,[&]{
    assert(!first.BeginMutation(a,token));assert(!CAMLSession::EndNative(token));
  });foreign.get();
  assert(!first.BeginMutation(a)); // no implicit borrowing of another native scope
  assert(first.BeginMutation(a,token));assert(first.Complete(a,true));
  assert(CAMLSession::EndNative(token));assert(!first.BeginMutation(first.Fence(),token));
  assert(second.BeginMutation(b));assert(second.Complete(b,true));
}
static void NestedEffectsAndFailure(){
  CAMLSession other;OpenSession(other);CAMLCodec codec;
  auto effects=[&]{
    assert(codec.m_nativeLifecycleRequest);
    assert(codec.m_nativeLifecycleRequest->phase==CAMLSession::NativeRequest::Phase::ACTIVE);
    assert(!CAMLSession::CancelNative(codec.m_nativeLifecycleRequest));
    assert(!other.AcquireDecoder());assert(!CAMLSession::FenceNative());
    auto display=CAMLSession::FenceDisplay();assert(!CAMLSession::TryBeginDisplay(display));
    assert(CAMLSession::CancelDisplay(display));
  };
  codec.onOpen=effects;codec.onClose=effects;
  assert(codec.OpenDecoder());assert(codec.ReopenDecoder());
  assert((codec.trace==std::vector<std::string>{"open","close","open"}));
  codec.openSucceeds=false;codec.trace.clear();
  assert(!codec.ReopenDecoder());assert(codec.LifecycleFailed());
  assert((codec.trace==std::vector<std::string>{"close","open","close"}));
  assert(!codec.m_nativeLifecycleRequest && !codec.deviceAcquired && !codec.m_decoderNeedsClose);
  assert(other.AcquireDecoder());
  codec.openSucceeds=true;codec.onOpen=[] {throw 9;};
  try{codec.OpenDecoder();assert(false);}catch(int){}
  assert(codec.LifecycleFailed() && !codec.LifecyclePending() && !codec.m_opened);
  assert(!codec.m_nativeLifecycleRequest && other.AcquireDecoder());
  auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  // Throw after partial resources were acquired: explicit Close owns cleanup.
  codec.onOpen={};assert(codec.OpenDecoder());codec.onClose=[] {throw 7;};
  try{codec.CloseDecoder();assert(false);}catch(int){}
  assert(codec.m_decoderNeedsClose && codec.deviceAcquired && codec.LifecycleFailed());
  codec.onClose=effects;assert(codec.CloseDecoder());assert(!codec.deviceAcquired);
}
static void ReplacementAndTransfer(){
  CAMLSession other;OpenSession(other);CAMLCodec codec;assert(codec.OpenDecoder());codec.trace.clear();
  auto held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  assert(!codec.ReopenDecoder());auto obsolete=codec.m_nativeLifecycleRequest;
  assert(!codec.CloseDecoder());auto replacement=codec.m_nativeLifecycleRequest;
  assert(obsolete!=replacement && obsolete->phase==CAMLSession::NativeRequest::Phase::CANCELLED);
  assert(!CAMLSession::TryBeginNative(obsolete) && !CAMLSession::EndNative(obsolete));
  held.reset();assert(codec.ContinueLifecycle());assert((codec.trace==std::vector<std::string>{"close"}));
  // Same operation on a serialized new owner after the previous thread joined.
  codec.trace.clear();assert(codec.OpenDecoder());codec.trace.clear();
  held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  std::shared_ptr<CAMLSession::NativeRequest> oldOwner;
  std::thread owner([&]{assert(!codec.CloseDecoder());oldOwner=codec.m_nativeLifecycleRequest;});owner.join();
  assert(!codec.ContinueLifecycle()); // observer cannot take the departed owner's token
  assert(!codec.CloseDecoder());auto current=codec.m_nativeLifecycleRequest;
  assert(current!=oldOwner && current->owner==std::this_thread::get_id());
  assert(oldOwner->phase==CAMLSession::NativeRequest::Phase::CANCELLED);
  held.reset();assert(codec.ContinueLifecycle());assert((codec.trace==std::vector<std::string>{"close"}));
}
static void CompetingNativeAndStaleSession(){
  CAMLCodec codec;assert(codec.OpenDecoder());codec.trace.clear();
  auto announced=CAMLSession::FenceNative();assert(CAMLSession::TryBeginNative(announced));
  assert(!codec.Reset() && !codec.m_nativeLifecycleRequest && codec.trace.empty());
  assert(CAMLSession::EndNative(announced));assert(codec.ContinueLifecycle());
  CAMLSession other;OpenSession(other);auto held=std::make_unique<CAMLSession::Permit>(other.AcquireDecoder());
  codec.trace.clear();assert(!codec.Reset());auto wrong=other.Fence();
  auto exact=codec.m_lifecycleRequest;codec.m_lifecycleRequest=wrong;held.reset();
  assert(!codec.ContinueLifecycle());assert(codec.trace.empty() && !codec.m_nativeLifecycleRequest);
  codec.m_lifecycleRequest=exact;assert(codec.ContinueLifecycle());
}
static void DeferredSessionCancellation(){
  for(auto operation:{CAMLCodec::Lifecycle::CLOSE,CAMLCodec::Lifecycle::REOPEN}){
    CAMLCodec codec;assert(codec.OpenDecoder());
    codec.m_dvSession=std::make_shared<const int>(0);s_dvPlaybackSession=codec.m_dvSession;
    auto permit=std::make_unique<CAMLSession::Permit>(codec.m_session.AcquireDecoder());
    assert(!codec.BeginLifecycle(operation));assert(!s_dvPlaybackSession);
    permit.reset();assert(codec.ContinueLifecycle());assert(codec.CloseDecoder());
  }
  CAMLCodec codec;assert(codec.OpenDecoder());codec.m_dvSession=std::make_shared<const int>(0);
  auto replacement=std::make_shared<const int>(1);s_dvPlaybackSession=replacement;
  assert(codec.CloseDecoder());assert(s_dvPlaybackSession==replacement);s_dvPlaybackSession.reset();
}
int main(){DeferredSessionCancellation();AcrossSessions();ExactNestedSession();NestedEffectsAndFailure();ReplacementAndTransfer();CompetingNativeAndStaleSession();}
'''


def source():
    full=fixture['harness']()
    assert full.endswith(fixture['TESTS'])
    code=full[:-len(fixture['TESTS'])]
    code=code.replace('std::function<void()> onClose;','std::function<void()> onClose,onOpen;')
    code=code.replace('MutationOnly(); trace.push_back("open");','MutationOnly(); if(onOpen)onOpen(); trace.push_back("open");')
    return code+TESTS


def run(code,patch=None,negative=False):
    with tempfile.TemporaryDirectory(prefix='codec-native-') as tmp:
        out=Path(tmp)
        if patch:
            p=out/'cores/VideoPlayer/DVDCodecs/Video/AMLSession.h';p.parent.mkdir(parents=True);p.write_text(patch)
        (out/'test.cpp').write_text(code)
        result=subprocess.run(['g++','-std=c++17','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer',
            '-fno-pie','-no-pie','-I',str(out),'-I',str(ROOT/'xbmc'),str(out/'test.cpp'),'-o',str(out/'test')],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        result=subprocess.run([str(out/'test')],capture_output=True,text=True,timeout=15)
        if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result.stdout+result.stderr
        else:assert result.returncode==0,result.stdout+result.stderr


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code);print('PASS: codec outer admission and exact nested session (ASan/UBSan)')
    if args.negative_controls:
        header=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLSession.h').read_text()
        for name,old,new in [
            ('ignore another active session','ForSessions([&](State& state) { ready = ready && !state.active && !state.retiring && !state.mutating; });', 'ForSessions([&](State& state) { ready = true; });'),
            ('borrow another session token','native->sessionIdentity == m_state &&','true &&'),
            ('mutate during display','request->phase != NativeRequest::Phase::PENDING || s_displayPhase == DisplayPhase::MUTATING','request->phase != NativeRequest::Phase::PENDING'),
        ]:
            assert header.count(old)==1,name;run(code,header.replace(old,new),True);print('rejected:',name)
        for name,old,new in [
            ('retain obsolete owner token','m_nativeLifecycleRequest->owner != std::this_thread::get_id()', 'false'),
            ('drop outer token before nested effects','  const Lifecycle operation = m_lifecycle;', '  CAMLSession::EndNative(m_nativeLifecycleRequest);\n  const Lifecycle operation = m_lifecycle;'),
            ('omit exception completion','    CAMLSession::EndNative(m_nativeLifecycleRequest);\n    m_nativeLifecycleRequest.reset();\n    throw;', '    throw;'),
        ]:
            assert code.count(old)==1,name;run(code.replace(old,new),negative=True);print('rejected:',name)


if __name__=='__main__':main()
