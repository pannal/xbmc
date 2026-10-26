#!/usr/bin/env python3
"""Run actual idle-wake arm, bounded trace/pump and AML observers on host fixtures.

Actual OnWake, display-state formatter, PresentRenderImpl, TrySwapBuffers,
status observers and unchanged EndDisplayTrace execute. Clock, restoration,
window/admission/target, logger, sysfs and EGL calls are fixtures. No device,
real suspend, kernel handler, physical signal or cure is established.
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
SPEC=importlib.util.spec_from_file_location('wake_method_extract',ROOT/'tools/test-aml-texture-upload-context.py')
EX=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(EX)
POWER='xbmc/powermanagement/PowerManager.cpp';AML='xbmc/utils/AMLUtils.cpp';WINDOW='xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp';EGL='xbmc/utils/EGLUtils.cpp';BASE='xbmc/windowing/WinSystem.h';TRACE='xbmc/utils/PlaybackEndDiagnostics.h';CLOCK='xbmc/utils/PlaybackDiagnostics.h'
FILES=(POWER,AML,WINDOW,EGL,BASE,TRACE,CLOCK)
PRELUDE=r'''
#include "utils/PlaybackEndDiagnostics.h"
#include "rendering/RenderResource.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
static unsigned checks=0,cases=0;
static void Check(bool value,const std::string&reason){++checks;if(!value)throw std::runtime_error(reason);}
namespace fixture {
static std::vector<std::string>logs,reads,restoration;
static bool announcedActive=false;static uint64_t announcedAt=0;
static int swaps=0,errorReads=0,applySwitches=0;static bool swapAccepted=true;
static std::string text(bool value){return value?"true":"false";}
static std::string text(const std::string&s){return s;}static std::string text(const char*s){return s?s:"null";}
template<class T>static std::string text(const T&v){std::ostringstream s;s<<v;return s.str();}
template<class...T>static std::string Format(const std::string&pattern,const T&...values){
 std::vector<std::string>args{text(values)...};std::string out;std::size_t pos=0,i=0;
 for(;;){auto a=pattern.find('{',pos);if(a==std::string::npos){out+=pattern.substr(pos);break;}auto b=pattern.find('}',a);Check(b!=std::string::npos&&i<args.size(),"fixture format argument mismatch");out+=pattern.substr(pos,a-pos)+args[i++];pos=b+1;}Check(i==args.size(),"fixture extra format argument");return out;
}
}
namespace fmt {template<class...T>static std::string format(const std::string&s,const T&...v){return fixture::Format(s,v...);}}
namespace StringUtils {template<class...T>static std::string Format(const std::string&s,const T&...v){return fixture::Format(s,v...);}}
constexpr int LOGDEBUG=0,LOGINFO=1,LOGWARNING=2;
struct CLog {template<class...T>static void Log(int level,const char*pattern,const T&...v){if(level==LOGDEBUG&&!PLAYBACK_DIAGNOSTICS::Enabled())return;fixture::logs.push_back(fixture::Format(pattern,v...));}};
struct CSysfsPath {
 std::string path;explicit CSysfsPath(const char*p):path(p){}explicit CSysfsPath(std::string p):path(std::move(p)){}
 bool Exists()const{return true;}
 template<class T>std::optional<T>Get()const{
  const auto name=path.substr(path.find_last_of('/')+1);
  if(name=="rxsense_state"||name=="attr"||name=="rawedid"||name=="cedst_count"||name=="debug"||name.rfind("hdcp",0)==0)
   throw std::runtime_error("unsafe diagnostic getter: "+path);
  fixture::reads.push_back(path);
  if(name=="blank")return T("");
  if(name=="disp_mode")return T("VIC:97\n");
  if(name=="mode"&&path=="/sys/class/display/mode")return T("2160p60hz\n");
  if(name=="sink_type")return T("sink\n");
  return T("1\n");
 }
};
namespace GUI_FRAME_TIMING {enum class Region{EGLSwap};struct Scope{explicit Scope(Region){}};}
using EGLint=int;constexpr int EGL_SUCCESS=0x3000,EGL_NO_DISPLAY=0,EGL_NO_SURFACE=0,EGL_TRUE=1;
static int eglSwapBuffers(int,int){++fixture::swaps;return fixture::swapAccepted?EGL_TRUE:0;}
static int eglGetError(){++fixture::errorReads;return 0x300d;}
struct CEGLContextUtils {struct SwapDiagnostics{bool attempted=false;EGLint error=EGL_SUCCESS;};int m_eglDisplay=1,m_eglSurface=1;bool TrySwapBuffers(SwapDiagnostics*diagnostics=nullptr);};
using CCriticalSection=std::recursive_mutex;
struct IDispResource {virtual~IDispResource()=default;virtual void OnResetDisplay(){}};
struct CWinSystemBase {virtual~CWinSystemBase()=default;virtual uint64_t GetDisplayGeneration()const{return 0;}
@BASE_STATE@
};
struct FakeGraphics {int GetHDRType()const{return 2;}};
struct Life {uint64_t serial=7;bool ready=false,pending=true;uint64_t Serial()const{return serial;}bool Ready()const{return ready;}bool Pending()const{return pending;}void Resume(){ready=true;pending=false;}};
struct Timer {bool past=false;bool IsTimePast()const{return past;}auto GetStartTime()const{return PLAYBACK_DIAGNOSTICS::origin+std::chrono::microseconds(100);}auto GetInitialTimeoutValue()const{return std::chrono::microseconds(200);}};
struct CWinSystemAmlogicGLESContext: CWinSystemBase {
 Life m_displayLifecycle;Timer m_dispResetTimer;bool m_displayGeometryReady=false,m_delayDispReset=false,admitted=false,targetValid=false;
 std::atomic_bool m_hdrRefreshPending{false};int m_hdrType=1;mutable int targetCaptures=0;FakeGraphics gfx;
 std::shared_ptr<const uint8_t>identity=std::make_shared<const uint8_t>(1);
 CCriticalSection m_resourceSection;std::vector<IDispResource*>m_resources;CEGLContextUtils m_pGLContext;PresentResult m_presentResult=PresentResult::NOT_ATTEMPTED;
 RenderTargetToken CaptureRenderTarget()const{++targetCaptures;return {identity,m_displayLifecycle.serial};}
 bool IsRenderTargetCurrent(const RenderTargetToken&t)const{return bool(t.identity)&&targetValid&&t.identity==identity&&t.generation==m_displayLifecycle.serial;}
 bool CanRender()const{return admitted;}const FakeGraphics&GetGfxContext()const{return gfx;}
 void RefreshHDRCapabilities(){}void GuiABPresented(bool){}void GuiABFinish(const char*){}bool GuiABDraw(){return false;}void ApplyPendingKernelSwitch(){++fixture::applySwitches;}
 uint64_t GetDisplayGeneration()const override{return m_displayLifecycle.Serial();}
 std::string GetDisplayDiagnosticState()const override;void ObserveEndDisplay(const char*,bool=false);void PresentRenderImpl(bool);
};
static CWinSystemBase*window=nullptr;
static void aml_hdr10plus_vsif_hold(bool){}static void aml_native_gui_osd_present(bool){}
enum class AMLHDMILinkSample{SKIPPED,UNCHANGED,CHANGED};
static int s_lastOsd=-1,s_lastSubtitles=-1,s_dvModeCached=0;
static bool aml_linux_force_422=false,vs10_conversion=false,s_dvPlaybackActive=false;
static void Step(const char*event,uint64_t duration){fixture::restoration.push_back(event);PLAYBACK_DIAGNOSTICS::fixtureNow+=duration;}
struct CApplicationPowerHandling {void ResetShutdownTimers(){Step("timers",100);}void ResetScreenSaver(){}};
struct CGUIDialogBusyNoCancel {void Close(bool){Step("dialog",100);}};
constexpr int WINDOW_DIALOG_BUSY_NOCANCEL=1;
struct Components {template<class T>std::shared_ptr<T>GetComponent(){return std::make_shared<T>();}};
struct WindowManager {template<class T>T*GetWindow(int){static T dialog;return &dialog;}};
struct GUI {WindowManager wm;WindowManager&GetWindowManager(){return wm;}};
struct Network {void WaitForNet(){Step("network",6000000);}};
struct Audio {void Resume(){Step("audio",1000000);}};
struct Application {void UpdateLibraries(){Step("libraries",100);}};static Application g_application;
struct Weather {void Refresh(){Step("weather",100);}};
struct PVR {void OnWake(){Step("pvr",100);}};
namespace ANNOUNCEMENT{enum E{System};}
struct Announcements {void Announce(ANNOUNCEMENT::E,const char*event){Check(std::string(event)=="OnWake","actual wake announcement retained");fixture::restoration.push_back("announce");fixture::announcedAt=PLAYBACK_DIAGNOSTICS::NowUs();fixture::announcedActive=PLAYBACK_DIAGNOSTICS::endDisplay.Active(fixture::announcedAt);}};
struct CServiceBroker {
 static Network&GetNetwork(){static Network n;return n;}static Components&GetAppComponents(){static Components c;return c;}
 static GUI*GetGUI(){static GUI g;return &g;}static Audio*GetActiveAE(){static Audio a;return &a;}
 static Weather&GetWeatherManager(){static Weather w;return w;}static PVR&GetPVRManager(){static PVR p;return p;}
 static Announcements*GetAnnouncementManager(){static Announcements a;return &a;}static CWinSystemBase*GetWinSystem(){return window;}
};
struct CPowerManager {void OnWake();void RestorePlayerState(){Step("player",2000000);}};
@METHODS@
'''
TESTS=r'''
static void Reset(bool enabled){using namespace PLAYBACK_DIAGNOSTICS;SetEnabled(enabled);endDisplay.Cancel();fixtureNow=100;fixture::logs.clear();fixture::reads.clear();fixture::restoration.clear();fixture::announcedActive=false;fixture::announcedAt=0;fixture::swaps=fixture::errorReads=fixture::applySwitches=0;fixture::swapAccepted=true;}
static std::size_t CountLog(const std::string&text){return std::count_if(fixture::logs.begin(),fixture::logs.end(),[&](const auto&line){return line.find(text)!=std::string::npos;});}
static bool HasLog(const std::string&text){return CountLog(text)>0;}
static void Wake(){using namespace PLAYBACK_DIAGNOSTICS;Reset(true);CPowerManager power;power.OnWake();Check(fixtureNow>EndDisplayTrace::WINDOW_US,"fixture restoration exceeds trace budget");
 const std::vector<std::string>expected={"network","timers","dialog","audio","libraries","weather","pvr","player","announce"};Check(fixture::restoration==expected,"wake restoration and announcement ordering unchanged");
 std::vector<EndDisplayTrace::Entry>events;endDisplay.Drain([&](const auto&e){events.push_back(e);});
#ifdef HAS_LIBAMCODEC
 Check(fixture::announcedActive&&events.size()==1&&std::string(events[0].kind)=="begin"&&events[0].detail=="wake"&&events[0].at==fixtureNow,"idle wake arms a fresh post-restoration trace");
 Check(endDisplay.Active(fixtureNow+EndDisplayTrace::WINDOW_US-1),"wake retains full five-second post-restoration budget");
#else
 Check(!fixture::announcedActive&&events.empty(),"non-AML wake does not arm platform trace");
#endif
 Reset(false);const auto offReads=fixtureClockReads;power.OnWake();events.clear();endDisplay.Drain([&](const auto&e){events.push_back(e);});Check(!fixture::announcedActive&&events.empty(),"disabled debug does not arm wake trace");Check(fixtureClockReads==offReads+1,"disabled wake performs no diagnostic timestamp read");++cases;
}
static void Observers(){using namespace PLAYBACK_DIAGNOSTICS;Reset(false);aml_dv_dump_state("disabled-debug");const auto count=fixture::reads.size();Check(count>0&&!HasLog("aml_dv_dump_state"),"suppressed logger still evaluates audited DV getters");aml_hdmi_link_probe("disabled-debug");Check(fixture::reads.size()>count,"ordinary link observer reads audited cached fields");const auto before=fixture::reads.size();aml_end_display_diagnostics_pump();Check(fixture::reads.size()==before,"disabled pump performs no native reads");SetEnabled(true);aml_dv_dump_state("enabled-debug");Check(HasLog("aml_dv_dump_state"),"enabled DV observer still emits audited snapshot");++cases;}
static void IdlePump(){using namespace PLAYBACK_DIAGNOSTICS;Reset(true);CWinSystemAmlogicGLESContext aml;window=&aml;endDisplay.Generation(1);endDisplay.Begin(fixtureNow,"wake");aml_end_display_diagnostics_pump();Check(endDisplay.Display()==7,"pump observes current display generation without a present");Check(aml.targetCaptures==1&&HasLog("can_render=false")&&HasLog("current_target_valid=false")&&HasLog("geometry=false")&&HasLog("pending=true"),"rejected render still captures admission and target state");Check(HasLog("ready=false")&&HasLog("delay=false")&&HasLog("fb_blank=unobserved"),"snapshot qualifies readiness delay and empty blank getter");
 const auto reads=fixture::reads.size();for(int i=0;i<1000;++i){fixtureNow+=1;aml_end_display_diagnostics_pump();}Check(aml.targetCaptures==1&&fixture::reads.size()==reads,"bounded snapshots perform no repeated admission getters");
 fixtureNow=100+EndDisplayTrace::WINDOW_US;aml_end_display_diagnostics_pump();Check(CountLog("event=summary")==1&&HasLog("swap_attempts=0")&&HasLog("first_gui_swap_us=0")&&HasLog("timeout-no-gui-swap"),"idle timeout emits exactly one zero-attempt summary");
 const auto finalReads=fixture::reads.size();for(int i=0;i<20;++i)aml_end_display_diagnostics_pump();Check(aml.targetCaptures==2&&fixture::reads.size()==finalReads&&CountLog("event=summary")==1,"expired idle trace cannot keep querying or emit repeated summaries");
 CWinSystemBase generic;window=&generic;endDisplay.Begin(++fixtureNow,"wake");aml_end_display_diagnostics_pump();Check(HasLog("unavailable"),"generic window diagnostics honestly unavailable");++cases;
}
static void StateGetter(){Reset(true);CWinSystemAmlogicGLESContext aml;auto first=aml.GetDisplayDiagnosticState();Check(first.find("current_target_valid=false")!=std::string::npos&&first.find("ready=false")!=std::string::npos&&first.find("geometry=false")!=std::string::npos,"state getter preserves rejected readiness and target validity");aml.targetValid=aml.admitted=aml.m_displayGeometryReady=aml.m_delayDispReset=true;aml.m_displayLifecycle.ready=true;aml.m_displayLifecycle.pending=false;auto next=aml.GetDisplayDiagnosticState();Check(next.find("current_target_valid=true")!=std::string::npos&&next.find("ready=true")!=std::string::npos&&next.find("delay=true")!=std::string::npos&&next.find("deadline_us=300")!=std::string::npos&&next.find("hdr=2 cached_hdr=1")!=std::string::npos,"state getter distinguishes ready geometry target and delayed reset");++cases;}
static void PresentOutcomes(){using namespace PLAYBACK_DIAGNOSTICS;Reset(true);CWinSystemAmlogicGLESContext aml;window=&aml;endDisplay.Generation(7);endDisplay.Begin(fixtureNow,"wake");aml_end_display_diagnostics_pump();
 aml.PresentRenderImpl(false);Check(aml.m_presentResult==PresentResult::SKIPPED&&fixture::swaps==0&&fixture::errorReads==0,"skipped present is distinct and never swaps");
 aml.PresentRenderImpl(true);Check(aml.m_presentResult==PresentResult::TARGET_INVALID&&fixture::swaps==0,"invalid render target never attempts EGL swap");
 aml.targetValid=true;fixture::swapAccepted=false;aml.PresentRenderImpl(true);Check(aml.m_presentResult==PresentResult::SWAP_FAILED&&fixture::swaps==1&&fixture::errorReads==1&&fixture::applySwitches==0,"failed swap records one owned EGL error and applies no pending switch");
 fixture::swapAccepted=true;aml.PresentRenderImpl(true);Check(aml.m_presentResult==PresentResult::SWAP_ACCEPTED&&fixture::swaps==2&&fixture::errorReads==1&&fixture::applySwitches==1,"accepted swap remains distinct from failed and skipped");
 endDisplay.Frame(++fixtureNow,7,static_cast<int>(RenderAttemptStatus::COMMANDS_COMPLETED),true,static_cast<int>(PresentResult::SWAP_ACCEPTED),10000,false,true,false,[]{return std::string("fixture draw");});aml_end_display_diagnostics_pump();
 aml.m_displayLifecycle.serial=8;fixtureNow=100+EndDisplayTrace::WINDOW_US;aml_end_display_diagnostics_pump();Check(CountLog(" output=")==3,"pump emits at most three generation-aware snapshots");Check(HasLog("swap_attempts=2")&&HasLog("swap_failures=1")&&HasLog("last_egl_error=12301"),"summary retains distinct swap attempt and failure evidence");
 Reset(false);fixture::swapAccepted=false;CEGLContextUtils gl;Check(!gl.TrySwapBuffers()&&fixture::errorReads==0,"default swap caller retains EGL error ownership");++cases;
}
int main(int argc,char**argv){try{if(argc>1&&std::string(argv[1])=="link-on"){Reset(true);aml_hdmi_link_probe("enabled-debug");Check(!fixture::reads.empty(),"enabled ordinary link observer reads audited fields");++cases;}else{Wake();Observers();IdlePump();StateGetter();PresentOutcomes();}std::printf("PASS %u cases %u checks; actual idle-wake/pump/observers/present/swap; host fixtures\n",cases,checks);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def selected_methods(files):
    signatures=[(POWER,'void CPowerManager::OnWake()'),(EGL,'bool CEGLContextUtils::TrySwapBuffers('),
       (WINDOW,'std::string CWinSystemAmlogicGLESContext::GetDisplayDiagnosticState() const'),
       (WINDOW,'void CWinSystemAmlogicGLESContext::ObserveEndDisplay('),
       (AML,'void aml_end_display_diagnostics_pump()'),(AML,'void aml_dv_dump_state(const char* tag)\n{'),
       (AML,'AMLHDMILinkSample aml_hdmi_link_probe('),(WINDOW,'void CWinSystemAmlogicGLESContext::PresentRenderImpl(')]
    return '\n\n'.join(EX.function(files[p],sig) for p,sig in signatures)
def source(files):
    base=EX.function(files[BASE],'  virtual std::string GetDisplayDiagnosticState() const')
    return PRELUDE.replace('@BASE_STATE@',base).replace('@METHODS@',selected_methods(files))+TESTS

def edited(files,path,signature,old,new):
    changed=dict(files);body=EX.function(files[path],signature);assert body.count(old)==1,(signature,old);changed[path]=files[path].replace(body,body.replace(old,new,1),1);return changed

def mutations(files):
    wake=EX.function(files[POWER],'void CPowerManager::OnWake()')
    arm=re.search(r'#ifdef HAS_LIBAMCODEC\n[\s\S]*?#endif\n',wake).group()
    noarm=dict(files);noarm[POWER]=files[POWER].replace(wake,wake.replace(arm,''),1)
    yield 'wake-arm-omitted',noarm,True,'idle wake arms a fresh post-restoration trace',None
    early=wake.replace(arm,'');at=early.index('{')+1;early=early[:at]+'\n'+arm+early[at:];changed=dict(files);changed[POWER]=files[POWER].replace(wake,early,1)
    yield 'wake-arm-before-restoration',changed,True,'idle wake arms a fresh post-restoration trace',None
    yield 'wake-disabled-timestamp',edited(files,POWER,'void CPowerManager::OnWake()',
        'if (PLAYBACK_DIAGNOSTICS::Enabled())','if (true)'),True,'disabled wake performs no diagnostic timestamp read',None
    unguarded=dict(files);unguarded[POWER]=files[POWER].replace(wake,wake.replace(arm,arm.replace('#ifdef HAS_LIBAMCODEC\n','').replace('#endif\n','')),1)
    yield 'wake-missing-platform-guard',unguarded,False,'non-AML wake does not arm platform trace',None
    yield 'missing-idle-admission-snapshot',edited(files,AML,'void aml_end_display_diagnostics_pump()',
        'window ? window->GetDisplayDiagnosticState() : "unavailable"','std::string("unavailable")'),True,'rejected render still captures admission and target state',None
    yield 'missing-idle-generation',edited(files,AML,'void aml_end_display_diagnostics_pump()',
        'trace.Generation(window->GetDisplayGeneration());','(void)window;'),True,'pump observes current display generation without a present',None
    yield 'unbounded-idle-snapshots',edited(files,AML,'void aml_end_display_diagnostics_pump()',
        'trace.SnapshotReason(now)','"mutant-unbounded"'),True,'bounded snapshots perform no repeated admission getters',None
    yield 'missing-zero-attempt-expiry',edited(files,AML,'void aml_end_display_diagnostics_pump()',
        '  trace.Expire(now);',''),True,'idle timeout emits exactly one zero-attempt summary',None
    yield 'lazy-attr-in-pump',edited(files,AML,'void aml_end_display_diagnostics_pump()',
        'read("/sys/class/amhdmitx/amhdmitx0/config")','read("/sys/class/amhdmitx/amhdmitx0/attr")'),True,'unsafe diagnostic getter: /sys/class/amhdmitx/amhdmitx0/attr',None
    yield 'active-rxsense-in-suppressed-dv-dump',edited(files,AML,'void aml_dv_dump_state(const char* tag)\n{',
        '"unobserved(active-getter)"','rd("/sys/class/amhdmitx/amhdmitx0/rxsense_state")'),True,'unsafe diagnostic getter: /sys/class/amhdmitx/amhdmitx0/rxsense_state',None
    yield 'lazy-attr-in-suppressed-dv-dump',edited(files,AML,'void aml_dv_dump_state(const char* tag)\n{',
        '"unobserved(lazy-getter)"','rd("/sys/class/amhdmitx/amhdmitx0/attr")'),True,'unsafe diagnostic getter: /sys/class/amhdmitx/amhdmitx0/attr',None
    probe=EX.function(files[AML],'AMLHDMILinkSample aml_hdmi_link_probe(');at=probe.index('{')+1
    bad=probe[:at]+'\n  if (PLAYBACK_DIAGNOSTICS::Enabled()) (void)CSysfsPath{"/sys/class/amhdmitx/amhdmitx0/rxsense_state"}.Get<std::string>();\n'+probe[at:]
    changed=dict(files);changed[AML]=files[AML].replace(probe,bad,1)
    yield 'active-rxsense-hidden-behind-debug',changed,True,'unsafe diagnostic getter: /sys/class/amhdmitx/amhdmitx0/rxsense_state','link-on'
    yield 'default-swap-error-consumed',edited(files,EGL,'bool CEGLContextUtils::TrySwapBuffers(',
        '  return success;','  if (!diagnostics) eglGetError();\n  return success;'),True,'default swap caller retains EGL error ownership',None


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output','--out',dest='out',type=Path);parser.add_argument('--sanitize',action='store_true');args=parser.parse_args();out=args.out or Path(tempfile.mkdtemp(prefix='idle-wake-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in FILES};header=files[CLOCK];clock=EX.function(header,'inline uint64_t NowUs()');header=header.replace(clock,'inline uint64_t fixtureNow=100,fixtureClockReads=0;\ninline uint64_t NowUs(){++fixtureClockReads;return fixtureNow;}')
    (out/'utils').mkdir(exist_ok=True);(out/'utils/PlaybackDiagnostics.h').write_text(header);(out/'utils/PlaybackEndDiagnostics.h').write_text(files[TRACE]);records=[]
    variants=[('actual-aml',files,True,None,None),('actual-generic',files,False,None,None),('actual-link-debug',files,True,None,'link-on'),*mutations(files)]
    script_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for name,selected,aml,witness,mode in variants:
        cpp=out/(name+'.cpp');binary=out/name;cpp.write_text(source(selected));cmd=['g++','-std=c++17','-O1','-g','-pthread','-Wall','-Wextra','-Werror']
        if args.sanitize:cmd+=['-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
        if aml:cmd+=['-DHAS_LIBAMCODEC']
        cmd+=['-I'+str(out),'-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(binary)]
        c=subprocess.run(cmd,capture_output=True,text=True);(out/(name+'-compile.log')).write_text(c.stdout+c.stderr);assert c.returncode==0,c.stderr
        command=[str(binary)]+([mode] if mode else [])
        r=subprocess.run(command,capture_output=True,text=True,timeout=30,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
        (out/(name+'-stdout.txt')).write_text(r.stdout);(out/(name+'-stderr.txt')).write_text(r.stderr)
        accepted=(r.returncode==0 and not r.stderr) if witness is None else (r.returncode==1 and r.stderr=='FAIL '+witness+'\n' and not any(token in r.stdout+r.stderr for token in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'name':name,'compile_argv':cmd,'compile_exit':c.returncode,'run_argv':command,'exit':r.returncode,'witness':witness,'accepted':accepted,'generated_sha256':hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out/'receipt.json').write_text(json.dumps({'script_sha256':script_hash,'source_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'clock_fixture':'Actual diagnostic clock body replaced by controlled fixtureNow and counter; all remaining header code unchanged.','results':records},indent=2)+'\n')
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==script_hash,'Script changed during run'
        assert accepted,(name,r.returncode,r.stdout,r.stderr)
        print(r.stdout.strip() if witness is None else 'PASS strict semantic control '+name,flush=True)
if __name__=='__main__':main()
