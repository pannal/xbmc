#!/usr/bin/env python3
"""Execute actual JSON-RPC/atomic request/main-pump late display diagnostics.

Reuses the unchanged idle-wake fixture and assertions. Actual RPC handler,
request/consume helpers, recorder and main pump execute with recording clock,
renderer, sysfs and EGL boundaries. Schema/dispatch checks establish binding,
not a running JSON-RPC server or network authorization. No device or cure claim.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('idle_trace_fixture',ROOT/'tools/test-idle-wake-diagnostics.py')
IDLE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(IDLE)
RPC='xbmc/interfaces/json-rpc/ApplicationOperations.cpp';DECL='xbmc/interfaces/json-rpc/ApplicationOperations.h';REGISTRY='xbmc/interfaces/json-rpc/JSONServiceDescription.cpp';SCHEMA='xbmc/interfaces/json-rpc/schema/methods.json';STATUS='xbmc/interfaces/json-rpc/JSONRPCUtils.h'
FILES=(*IDLE.FILES,RPC,DECL,REGISTRY,SCHEMA,STATUS)
RPC_PRELUDE=r'''
#include <thread>
struct CVariant {std::string value;CVariant&operator=(const char*text){value=text;return *this;}};
namespace JSONRPC {
@STATUS@
class ITransportLayer {};class IClient {};
struct CApplicationOperations {
@DECL@
};
}
using namespace JSONRPC;
@RPC@
'''
TESTS=r'''
static std::pair<JSONRPC_STATUS,std::string>CallRPC(){CVariant params,result;auto code=CApplicationOperations::TraceDisplay("Application.TraceDisplay",nullptr,nullptr,params,result);return {code,result.value};}
static void ManualReset(bool enabled){PLAYBACK_DIAGNOSTICS::ConsumeDisplayTraceRequest();Reset(enabled);window=nullptr;fixture::nativeReadDelay=0;}
#ifdef HAS_LIBAMCODEC
static std::size_t LogIndex(const std::string&value,const std::string&scope=""){for(std::size_t i=0;i<fixture::logs.size();++i)if(fixture::logs[i].find(value)!=std::string::npos&&fixture::logs[i].find(scope)!=std::string::npos)return i;return fixture::logs.size();}
static void LateWorkerRequest(){using namespace PLAYBACK_DIAGNOSTICS;ManualReset(true);CWinSystemAmlogicGLESContext aml;window=&aml;endDisplay.Generation(7);endDisplay.Begin(fixtureNow,"wake");const auto oldID=endDisplay.Id();fixtureNow+=90000000;
 const auto clocks=fixtureClockReads,reads=fixture::reads.size();std::pair<JSONRPC_STATUS,std::string>response;
 std::thread worker([&]{response=CallRPC();});worker.join();Check(response.first==OK&&response.second=="requested","late RPC returns requested, not started acknowledgement");
 Check(endDisplay.Id()==oldID&&fixture::logs.empty(),"worker RPC never mutates main-owned trace before pump");Check(fixtureClockReads==clocks,"worker request performs no diagnostic timestamp read");Check(fixture::reads.size()==reads,"worker request performs no native reads");
 fixtureNow+=7000000;fixture::nativeReadDelay=700000;aml_end_display_diagnostics_pump();fixture::nativeReadDelay=0;const auto start=fixtureNow;
 Check(endDisplay.Id()==oldID+1&&endDisplay.Active(start)&&endDisplay.Active(start+EndDisplayTrace::WINDOW_US-1),"manual budget starts after main status work at fresh timestamp");
 Check(HasLog("t_us="+std::to_string(start)+" trace=")&&HasLog("event=begin manual"),"manual begin receipt is emitted before a future render can stall");
 const std::string oldTrace=" trace="+std::to_string(oldID)+" ",newTrace=" trace="+std::to_string(oldID+1)+" ";
 const auto oldSnapshot=LogIndex("output=timeout-no-gui-swap ",oldTrace),oldSummary=LogIndex("event=summary timeout",oldTrace),manualBegin=LogIndex("event=begin manual",newTrace);
 Check(oldSnapshot<oldSummary&&oldSummary<manualBegin&&manualBegin<fixture::logs.size(),"expired trace snapshot and summary precede new manual begin");
 Check(aml.targetCaptures==1&&CountLog("output=first-pump ")==0,"manual first snapshot waits for following main pump");
 aml_end_display_diagnostics_pump();Check(aml.targetCaptures==2&&HasLog("can_render=false")&&HasLog("current_target_valid=false"),"late manual trace snapshots current rejected render state");
 fixtureNow=start+EndDisplayTrace::WINDOW_US;aml_end_display_diagnostics_pump();
 Check(!endDisplay.Active(fixtureNow)&&endDisplay.Id()==oldID+1&&CountLog("event=summary timeout")==2&&CountLog("event=begin manual")==1&&HasLog("swap_attempts=0"),"render-off manual trace expires once without latent rearm");
 const auto finalReads=fixture::reads.size();for(int i=0;i<20;++i)aml_end_display_diagnostics_pump();Check(fixture::reads.size()==finalReads&&CountLog("event=begin manual")==1,"completed manual trace does not keep observing or rearm");++cases;
}
static void DisabledRequest(){using namespace PLAYBACK_DIAGNOSTICS;ManualReset(false);const auto id=endDisplay.Id(),clocks=fixtureClockReads,reads=fixture::reads.size();std::pair<JSONRPC_STATUS,std::string>response;
 std::thread worker([&]{response=CallRPC();});worker.join();Check(response.first==OK&&response.second=="disabled"&&!ConsumeDisplayTraceRequest(),"disabled RPC reports disabled without pending work");Check(endDisplay.Id()==id&&fixtureClockReads==clocks&&fixture::reads.size()==reads,"disabled request touches no recorder clock or native state");
 SetEnabled(true);response=CallRPC();Check(response.second=="requested","enabled RPC can queue a request before level change");SetEnabled(false);const auto offClock=fixtureClockReads,offReads=fixture::reads.size();aml_end_display_diagnostics_pump();Check(fixtureClockReads==offClock&&fixture::reads.size()==offReads,"disabled pump clears request without clock or native reads");
 SetEnabled(true);aml_end_display_diagnostics_pump();Check(endDisplay.Id()==id&&!endDisplay.Active(fixtureNow)&&!HasLog("event=begin manual"),"disabled pump clears queued requests before reenable");++cases;
}
static void ActiveAtEntryExpiresDuringSnapshot(){using namespace PLAYBACK_DIAGNOSTICS;ManualReset(true);CWinSystemAmlogicGLESContext aml;window=&aml;endDisplay.Begin(fixtureNow,"wake");const auto id=endDisplay.Id();Check(CallRPC().second=="requested","active-at-entry request accepted as request only");fixture::nativeReadDelay=700000;aml_end_display_diagnostics_pump();fixture::nativeReadDelay=0;Check(endDisplay.Id()==id&&CountLog("event=begin manual")==0,"active-at-entry request stays ignored when snapshot crosses deadline");aml_end_display_diagnostics_pump();Check(!endDisplay.Active(fixtureNow)&&CountLog("event=summary timeout")==1&&CountLog("event=begin manual")==0,"cross-deadline active request leaves no delayed manual window");++cases;}
static void ActiveBurst(){using namespace PLAYBACK_DIAGNOSTICS;ManualReset(true);CWinSystemAmlogicGLESContext aml;window=&aml;const auto id=endDisplay.Id(),clocks=fixtureClockReads;std::atomic<unsigned>accepted{0};std::vector<std::thread>workers;
 for(int t=0;t<4;++t){workers.emplace_back([&]{for(int n=0;n<128;++n){auto result=CallRPC();if(result.first==OK&&result.second=="requested")++accepted;}});}
 for(auto&t:workers){t.join();}
 Check(accepted==512&&endDisplay.Id()==id&&fixtureClockReads==clocks&&fixture::reads.empty(),"concurrent RPC burst is only a coalesced atomic request");aml_end_display_diagnostics_pump();const auto start=fixtureNow;Check(endDisplay.Id()==id+1&&CountLog("event=begin manual")==1,"concurrent requests admit one main-owned manual trace");
 for(int n=1;n<=10;++n){fixtureNow=start+static_cast<uint64_t>(n)*400000;auto result=CallRPC();Check(result.second=="requested","active request remains a request acknowledgement");aml_end_display_diagnostics_pump();Check(endDisplay.Id()==id+1&&CountLog("event=begin manual")==1,"active requests cannot replace or extend the running trace");}
 fixtureNow=start+EndDisplayTrace::WINDOW_US;aml_end_display_diagnostics_pump();Check(!endDisplay.Active(fixtureNow)&&endDisplay.Id()==id+1&&CountLog("event=summary timeout")==1,"active coalesced requests do not extend original deadline");
 for(int n=0;n<20;++n){aml_end_display_diagnostics_pump();}
 Check(CountLog("event=begin manual")==1,"active request burst cannot leave a latent rearm");
 Check(EndDisplayTrace::WINDOW_US==5000000&&EndDisplayTrace::DETAIL_LIMIT==32&&EndDisplayTrace::QUEUE_LIMIT==64,"manual requests preserve recorder time detail and queue bounds");++cases;
}
#else
static void GenericRequest(){using namespace PLAYBACK_DIAGNOSTICS;for(bool enabled:{false,true}){ManualReset(enabled);const auto id=endDisplay.Id(),clocks=fixtureClockReads;std::pair<JSONRPC_STATUS,std::string>response;std::thread worker([&]{response=CallRPC();});worker.join();Check(response.first==OK&&response.second=="unsupported"&&!ConsumeDisplayTraceRequest(),"non-AML RPC is unsupported without pending work");Check(endDisplay.Id()==id&&fixtureClockReads==clocks&&fixture::reads.empty(),"non-AML RPC performs no diagnostic work");}++cases;}
#endif
int main(int,char**argv){try{char*base[]={argv[0]};const int baseStatus=IdleBaseMain(1,base);if(baseStatus!=0)return baseStatus;
#ifdef HAS_LIBAMCODEC
 LateWorkerRequest();DisabledRequest();ActiveBurst();ActiveAtEntryExpiresDuringSnapshot();
#else
 GenericRequest();
#endif
 std::printf("PASS actual manual RPC/atomic request/main pump with preserved idle/swap assertions (%u cases)\n",cases);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def source(files):
    generated=IDLE.source(files)
    generated=generated.replace('static std::vector<std::string>logs,reads,restoration;','static std::vector<std::string>logs,reads,restoration;\nstatic uint64_t nativeReadDelay=0;')
    generated=generated.replace('  fixture::reads.push_back(path);','  PLAYBACK_DIAGNOSTICS::fixtureNow+=fixture::nativeReadDelay;\n  fixture::reads.push_back(path);')
    assert generated.count('int main(int argc,char**argv)')==1
    generated=generated.replace('int main(int argc,char**argv)','int IdleBaseMain(int argc,char**argv)',1)
    status=IDLE.EX.function(files[STATUS],'  enum JSONRPC_STATUS')+';'
    declaration=re.search(r'static\s+JSONRPC_STATUS\s+TraceDisplay\s*\([^;]+\);',files[DECL]).group()
    rpc=IDLE.EX.function(files[RPC],'JSONRPC_STATUS CApplicationOperations::TraceDisplay(')
    return generated+RPC_PRELUDE.replace('@STATUS@',status).replace('@DECL@',declaration).replace('@RPC@',rpc)+TESTS

def need(value,message):
    if not value:raise AssertionError(message)
def schema_check(files):
    methods=json.loads(files[SCHEMA]);method=methods.get('Application.TraceDisplay')
    need(method is not None,'manual RPC schema entry exists')
    need(method.get('permission')=='ControlGUI','manual RPC requires ControlGUI permission')
    need(method.get('type')=='method' and method.get('transport')=='Response' and method.get('params')==[],'manual RPC is response method with no parameters')
    returns=method.get('returns',{})
    need(isinstance(returns,dict) and returns.get('type')=='string' and set(returns.get('enum',[]))=={'requested','disabled','unsupported'},'manual RPC exposes only requested disabled unsupported results')
    need(len(re.findall(r'\{\s*"Application\.TraceDisplay"\s*,\s*CApplicationOperations::TraceDisplay\s*\}',files[REGISTRY]))==1,'manual RPC dispatch maps exactly once to actual handler')
    need(len(re.findall(r'static\s+JSONRPC_STATUS\s+TraceDisplay\s*\(',files[DECL]))==1,'manual RPC has one matching handler declaration')

def edit(files,path,signature,old,new):
    changed=dict(files);body=IDLE.EX.function(files[path],signature);assert body.count(old)==1,(signature,old);changed[path]=files[path].replace(body,body.replace(old,new,1),1);return changed

def mutations(files):
    rpc=IDLE.EX.function(files[RPC],'JSONRPC_STATUS CApplicationOperations::TraceDisplay(')
    request='PLAYBACK_DIAGNOSTICS::RequestDisplayTrace()'
    yield 'worker-arms-recorder',edit(files,RPC,'JSONRPC_STATUS CApplicationOperations::TraceDisplay(',request,
        '(PLAYBACK_DIAGNOSTICS::endDisplay.Begin(PLAYBACK_DIAGNOSTICS::NowUs(), "manual"), PLAYBACK_DIAGNOSTICS::RequestDisplayTrace())'),True,'worker RPC never mutates main-owned trace before pump'
    header=files[IDLE.TRACE];request_body=IDLE.EX.function(header,'inline bool RequestDisplayTrace()');at=request_body.index('{')+1
    changed=dict(files);changed[IDLE.TRACE]=header.replace(request_body,request_body[:at]+'\n  (void)NowUs();'+request_body[at:],1)
    yield 'producer-reads-clock',changed,True,'worker request performs no diagnostic timestamp read'
    yield 'producer-reads-native',edit(files,RPC,'JSONRPC_STATUS CApplicationOperations::TraceDisplay(',request,
        '(CSysfsPath{"/sys/class/amhdmitx/amhdmitx0/hpd_state"}.Get<std::string>(), PLAYBACK_DIAGNOSTICS::RequestDisplayTrace())'),True,'worker request performs no native reads'
    yield 'disabled-request-admitted',edit(files,IDLE.TRACE,'inline bool RequestDisplayTrace()',
        'if (!Enabled())','if (false)'),True,'disabled RPC reports disabled without pending work'
    yield 'request-not-consumed',edit(files,IDLE.TRACE,'inline bool ConsumeDisplayTraceRequest()',
        'displayTraceRequested.exchange(false, std::memory_order_relaxed)','displayTraceRequested.load(std::memory_order_relaxed)'),True,'render-off manual trace expires once without latent rearm'
    yield 'disabled-pump-retains-request',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        '    PLAYBACK_DIAGNOSTICS::ConsumeDisplayTraceRequest();',''),True,'disabled pump clears queued requests before reenable'
    yield 'active-trace-restarted',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        'requested && !wasActive','requested && ((void)wasActive, true)'),True,'active requests cannot replace or extend the running trace'
    yield 'active-rechecked-after-slow-snapshot',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        'requested && !wasActive','requested && ((void)wasActive, !trace.Active(PLAYBACK_DIAGNOSTICS::NowUs()))'),True,'active-at-entry request stays ignored when snapshot crosses deadline'
    yield 'stale-pre-snapshot-start-time',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        'trace.Begin(PLAYBACK_DIAGNOSTICS::NowUs(), "manual");','trace.Begin(now, "manual");'),True,'manual budget starts after main status work at fresh timestamp'
    yield 'old-expiry-snapshot-skipped',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        'const char* reason = trace.SnapshotReason(now)','const char* reason = requested ? nullptr : trace.SnapshotReason(now)'),True,'expired trace snapshot and summary precede new manual begin'
    yield 'missing-manual-begin-receipt',edit(files,IDLE.AML,'void aml_end_display_diagnostics_pump()',
        '    trace.Drain(emit);',''),True,'manual begin receipt is emitted before a future render can stall'
    yield 'non-AML-request-admitted',edit(files,RPC,'JSONRPC_STATUS CApplicationOperations::TraceDisplay(',
        '#ifdef HAS_LIBAMCODEC','#if 1'),False,'non-AML RPC is unsupported without pending work'
    yield 'default-EGL-error-consumed',edit(files,IDLE.EGL,'bool CEGLContextUtils::TrySwapBuffers(',
        '  return success;','  if (!diagnostics) eglGetError();\n  return success;'),True,'default swap caller retains EGL error ownership'

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output','--out',dest='out',type=Path);parser.add_argument('--sanitize',action='store_true');args=parser.parse_args();out=args.out or Path(tempfile.mkdtemp(prefix='manual-display-trace-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in FILES};schema_check(files);schema_controls=[]
    for name,witness in [('permission','manual RPC requires ControlGUI permission'),('dispatch','manual RPC dispatch maps exactly once to actual handler')]:
        changed=dict(files)
        if name=='permission':
            obj=json.loads(files[SCHEMA]);obj['Application.TraceDisplay']['permission']='ReadData';changed[SCHEMA]=json.dumps(obj)
        else:changed[REGISTRY]=files[REGISTRY].replace('CApplicationOperations::TraceDisplay','CApplicationOperations::Quit',1)
        try:schema_check(changed)
        except AssertionError as error:need(str(error)==witness,'schema control failed for wrong reason');schema_controls.append({'name':name,'witness':str(error),'accepted':True})
        else:raise AssertionError('schema control survived: '+name)
    script_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();records=[]
    variants=[('actual-AML',files,True,None),('actual-generic',files,False,None),*mutations(files)]
    for name,selected,aml,witness in variants:
        local=out/name;local.mkdir(exist_ok=True);(local/'utils').mkdir(exist_ok=True)
        clock=selected[IDLE.CLOCK];body=IDLE.EX.function(clock,'inline uint64_t NowUs()');clock=clock.replace(body,'inline uint64_t fixtureNow=100,fixtureClockReads=0;\ninline uint64_t NowUs(){++fixtureClockReads;return fixtureNow;}')
        (local/'utils/PlaybackDiagnostics.h').write_text(clock);(local/'utils/PlaybackEndDiagnostics.h').write_text(selected[IDLE.TRACE]);cpp=local/'check.cpp';binary=local/'check';cpp.write_text(source(selected))
        cmd=['g++','-std=c++17','-O1','-g','-pthread','-Wall','-Wextra','-Werror','-Wno-unused-parameter']
        if args.sanitize:cmd+=['-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
        if aml:cmd+=['-DHAS_LIBAMCODEC']
        cmd+=['-I'+str(local),'-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(binary)];c=subprocess.run(cmd,capture_output=True,text=True);(local/'compile.log').write_text(c.stdout+c.stderr);need(c.returncode==0,c.stderr)
        r=subprocess.run([str(binary)],capture_output=True,text=True,timeout=30,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'});(local/'stdout.txt').write_text(r.stdout);(local/'stderr.txt').write_text(r.stderr)
        accepted=(r.returncode==0 and not r.stderr) if witness is None else (r.returncode==1 and r.stderr=='FAIL '+witness+'\n' and not any(t in r.stdout+r.stderr for t in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'name':name,'compile_argv':cmd,'compile_exit':c.returncode,'exit':r.returncode,'witness':witness,'accepted':accepted,'generated_sha256':hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out/'receipt.json').write_text(json.dumps({'script_sha256':script_hash,'idle_fixture_sha256':hashlib.sha256((ROOT/'tools/test-idle-wake-diagnostics.py').read_bytes()).hexdigest(),'source_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'schema_controls':schema_controls,'results':records},indent=2)+'\n');need(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==script_hash,'test script changed during run');need(accepted,str((name,r.returncode,r.stdout,r.stderr)))
        print(r.stdout.strip() if witness is None else 'PASS strict semantic control '+name,flush=True)
if __name__=='__main__':main()
