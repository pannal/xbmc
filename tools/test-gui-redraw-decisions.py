#!/usr/bin/env python3
"""Execute actual input/dirty/redraw/idle blocks with owned recording fixtures.

Uses the real opt-in timing header; window lifetimes, geometry, dirty solver and
backend rendering are fixtures. A stack-owned record installs the real header
gates/counters directly; file startup/writer tests belong to the separate tool.
The 432 baseline supplies behavior compatibility,
not observation expectations. No device, driver, input-latency or GPU-time proof.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '432c915e6d8781d9e24813386dcedecb129f08c1'
WM = 'xbmc/guilib/GUIWindowManager.cpp'
GLES = 'xbmc/rendering/gles/RenderSystemGLES.cpp'
GROUP = 'xbmc/guilib/GUIControlGroup.cpp'
HEADER = 'xbmc/utils/GUIFrameTiming.h'


def block(text, signature):
    start = text.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda m: ' ' * len(m.group()), text)
    brace = masked.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return text[start:end]


PRE = r'''
#include "utils/GUIFrameTiming.h"
#include <algorithm>
#include <array>
#include <cassert>
#include <chrono>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
using namespace std::chrono_literals;
static unsigned checks=0;static uint64_t digest=1469598103934665603ULL;
void Check(bool value,const char* text){++checks;if(!value)throw std::runtime_error(text);}
void Mix(uint64_t v){digest^=v;digest*=1099511628211ULL;}
constexpr bool historical=HISTORICAL;
using GUI_FRAME_TIMING::Counter;
static GUI_FRAME_TIMING::Header timingHeader{};
static GUI_FRAME_TIMING::Record firstRecord{},otherRecord{};
static void Timing(bool enabled=true,bool frame=true)
{
 using namespace GUI_FRAME_TIMING;firstRecord={};otherRecord={};timingHeader={};
 firstRecord.frame=11;otherRecord.frame=12;detail::enabled=enabled;detail::owner=true;
 detail::current=frame?&firstRecord:nullptr;detail::header=&timingHeader;
 detail::frameDepth=frame?1:0;
}
uint64_t CountOf(Counter c){return firstRecord.counters[static_cast<unsigned>(c)];}
int64_t Signed(Counter c){return static_cast<int64_t>(CountOf(c));}
static unsigned destroyedActions=0,focusReads=0,selectionReads=0,itemReads=0;
enum {ACTION_MOVE_UP=1,ACTION_MOVE_DOWN=2,ACTION_MOVE_LEFT=3,ACTION_MOVE_RIGHT=4,ACTION_OTHER=99,
      TMSG_GUI_ACTION=12,WINDOW_INVALID=-1,LOGWARNING=1,
      DIRTYREGION_SOLVER_FILL_VIEWPORT_ALWAYS=0,DIRTYREGION_SOLVER_FILL_VIEWPORT_ON_CHANGE=3};
struct CAction {int id;explicit CAction(int n):id(n){}int GetID()const{return id;}
 ~CAction(){++destroyedActions;}};
struct ThreadMessage {int dwMessage=TMSG_GUI_ACTION,param1=WINDOW_INVALID;void* lpVoid=nullptr;};
struct CGUIControl {
 int id;bool focused;explicit CGUIControl(int n,bool f=true):id(n),focused(f){}
 virtual ~CGUIControl()=default;int GetID()const{return id;}bool HasFocus()const{return focused;}
 virtual bool IsContainer()const{return false;}
};
struct IGUIContainer:CGUIControl {using CGUIControl::CGUIControl;bool IsContainer()const override{return true;}};
struct CGUIBaseContainer:IGUIContainer {
 using IGUIContainer::IGUIContainer;int selected=0;bool hasItem=true;
 const void* GetListItem(int)const{++itemReads;return hasItem?this:nullptr;}
 virtual int GetSelectedItem()const{++selectionReads;return selected;}
};
struct VirtualContainer:CGUIBaseContainer {using CGUIBaseContainer::CGUIBaseContainer;
 int GetSelectedItem()const override{++selectionReads;return selected+100;}};
struct EPGContainer:IGUIContainer {using IGUIContainer::IGUIContainer;};
struct CGUIControlGroup:CGUIControl {
 using CGUIControl::CGUIControl;using LookupMap=std::multimap<int,CGUIControl*>;
 int m_focusedControl=0;LookupMap lookup;std::vector<CGUIControl*> m_children;
 auto GetLookupControls(int id)const{++focusReads;return lookup.equal_range(id);}
 CGUIControl* GetFocusedControl()const;
};
struct CGUIWindow:CGUIControlGroup {
 using CGUIControlGroup::CGUIControlGroup;std::vector<std::unique_ptr<CGUIControl>> owned;
 unsigned calls=0;std::function<bool(const CAction&)> handler;
 bool OnAction(const CAction& a){++calls;return handler?handler(a):false;}
 template<class T> T* Add(int id,bool focus=true){auto item=std::make_unique<T>(id,focus);
 T* raw=item.get();lookup.emplace(id,raw);m_children.push_back(raw);owned.push_back(std::move(item));
 if(focus)m_focusedControl=id;return raw;}
 void Clear(){lookup.clear();m_children.clear();owned.clear();m_focusedControl=0;}
};
struct FakeApplication {unsigned calls=0;std::function<bool(const CAction&)> handler;
 bool OnAction(const CAction& a){++calls;return handler?handler(a):false;}} g_application;
struct CLog {template<class... T>static void Log(T&&...){} };
struct Region {int id=0;bool empty=false;bool IsEmpty()const{return empty;}
 bool operator==(const Region& x)const{return id==x.id&&empty==x.empty;}};
using CDirtyRegionList=std::vector<Region>;
struct Tracker {
 CDirtyRegionList marked,solved;unsigned markCalls=0;int cleanArg=0;bool expire=false;
 void MarkDirtyRegion(const Region& r){++markCalls;if(!r.IsEmpty())marked.push_back(r);}
 const CDirtyRegionList& GetMarkedRegions()const{return marked;}
 void CleanMarkedRegions(int n){cleanArg=n;if(expire)marked.clear();}
 CDirtyRegionList GetDirtyRegions(){return solved;}
};
struct Graphics {std::vector<int> scissors;unsigned resets=0,next=0,visualResolution=0;
 void NextControlBoundsFrame(){++next;}void SetScissors(const Region& r){scissors.push_back(r.id);}
 void ResetScissors(){++resets;}int GetResInfo(){return 0;}
 void SetRenderingResolution(int,bool){++visualResolution;}};
struct FakeWindowSystem {Graphics gfx;int age=2;CDirtyRegionList dirty;
 Graphics& GetGfxContext(){return gfx;}int GetBufferAge(){return age;}
 void SetDirtyRegions(const CDirtyRegionList& r){dirty=r;}};
struct Advanced {bool m_guiVisualizeDirtyRegions=false;int m_guiAlgorithmDirtyRegions=3;};
struct FakeSettings {Advanced advanced;Advanced* GetAdvancedSettings(){return &advanced;}};
struct FakeMessenger {bool IsProcessThread(){return true;}};
static FakeWindowSystem windowSystem;static FakeSettings settings;static FakeMessenger messenger;
struct CServiceBroker {static FakeWindowSystem* GetWinSystem(){return &windowSystem;}
 static FakeSettings* GetSettingsComponent(){return &settings;}
 static FakeMessenger* GetAppMessenger(){return &messenger;}};
struct CSingleExit {explicit CSingleExit(Graphics&){} };
struct CGUITexture {static unsigned quads;static void DrawQuad(const Region&,unsigned){++quads;}};
unsigned CGUITexture::quads=0;
struct CGUIWindowManager {
 std::map<int,std::unique_ptr<CGUIWindow>> windows;int active=5;
 unsigned windowReads=0,activeReads=0,passes=0,published=0;
 Tracker m_tracker;CDirtyRegionList m_dirtyregions;
 CGUIWindow* GetWindow(int n){++windowReads;auto it=windows.find(n);return it==windows.end()?nullptr:it->second.get();}
 int GetActiveWindowOrDialog(){++activeReads;return active;}
 CGUIWindow* AddWindow(int n){auto w=std::make_unique<CGUIWindow>(n);auto* raw=w.get();windows[n]=std::move(w);return raw;}
 void Add(CGUIWindow*){} // Mirror the real member name so unqualified timing Add cannot compile.
 void OnApplicationMessage(ThreadMessage*);bool Render();void ForwardDirty();
 void RenderPass(){++passes;}void PublishRenderBounds(){++published;}
 void Send(int action,int target=WINDOW_INVALID){ThreadMessage m{TMSG_GUI_ACTION,target,new CAction(action)};OnApplicationMessage(&m);}
};
enum class PresentResult {TARGET_INVALID,UNREPORTED,SKIPPED};
static std::vector<int> sleeps;
namespace KODI::TIME {void Sleep(std::chrono::milliseconds d){sleeps.push_back(d.count());}}
struct CRenderSystemGLES {
 bool can=true;unsigned vsync=0,presents=0;bool lastRendered=false;PresentResult m_presentResult{};
 bool CanRender(){return can;}void SetVSync(bool b){if(b)++vsync;}
 void PresentRenderImpl(bool b){++presents;lastRendered=b;m_presentResult=PresentResult::SKIPPED;}
 void PresentRender(bool,bool);
};
'''

TESTS = r'''
static void Reset(){Timing();destroyedActions=focusReads=selectionReads=itemReads=0;g_application={};}
static void NoTraversal(const char* mode)
{
 Reset();CGUIWindowManager w;auto* win=w.AddWindow(5);win->Add<CGUIBaseContainer>(101);
 g_application.handler=[](const CAction&){return true;};
 if(std::string(mode)=="disabled")GUI_FRAME_TIMING::detail::enabled=false;
 if(std::string(mode)=="noframe")GUI_FRAME_TIMING::detail::current=nullptr;
 if(std::string(mode)=="worker") {std::thread t([&]{w.Send(ACTION_MOVE_DOWN);});t.join();}
 else w.Send(ACTION_MOVE_DOWN);
 Check(g_application.calls==1,"inactive dispatch exactly once");
 Check(destroyedActions==1,"action deleted exactly once");
 Check(w.activeReads==0&&w.windowReads==0&&focusReads==0&&itemReads==0&&selectionReads==0,
       "inactive observer must not traverse");
 Check(std::all_of(std::begin(firstRecord.counters),std::end(firstRecord.counters),[](auto n){return n==0;}),"inactive counters untouched");
 Mix(g_application.calls);Mix(destroyedActions);
}
static void Navigation()
{
 Reset();CGUIWindowManager w;auto* old=w.AddWindow(5);old->Add<CGUIBaseContainer>(101);
 g_application.handler=[&](const CAction&){w.windows.erase(5);w.active=6;
   auto* replacement=w.AddWindow(6);replacement->Add<CGUIBaseContainer>(202)->selected=7;return true;};
 w.Send(ACTION_MOVE_DOWN);
 Check(g_application.calls==1,"handler must run exactly once");
 Check(destroyedActions==1,"action deleted exactly once");
 if(!historical){Check(Signed(Counter::MessageNavAfterWindow)==6&&Signed(Counter::MessageNavAfterControl)==202&&
 Signed(Counter::MessageNavAfterSelection)==7,"fresh target snapshot after replacement");
 Check(CountOf(Counter::MessageNavTargetChanged)==1&&CountOf(Counter::MessageNavHandled)==1,"target and handled recorded");
 Check(Signed(Counter::MessageNavBeforeWindow)==5&&Signed(Counter::MessageNavBeforeControl)==101&&
 Signed(Counter::MessageNavBeforeSelection)==0&&Signed(Counter::MessageNavRequestedWindow)==-1&&
 Signed(Counter::MessageNavLastAction)==ACTION_MOVE_DOWN,"before snapshot and numeric metadata");}
 Mix(w.active);Mix(destroyedActions);
 // Same window, truly deleted control; no stale owner or pointer may survive.
 Reset();CGUIWindowManager controls;auto* win=controls.AddWindow(5);win->Add<CGUIBaseContainer>(101);
 g_application.handler=[&](const CAction&){win->Clear();win->Add<VirtualContainer>(303)->selected=9;return false;};
 controls.Send(ACTION_MOVE_UP);
 Check(g_application.calls==1&&destroyedActions==1,"false handler exactly once");
 if(!historical)Check(Signed(Counter::MessageNavAfterControl)==303&&Signed(Counter::MessageNavAfterSelection)==109&&
 CountOf(Counter::MessageNavHandled)==0,"deleted control re-resolved with virtual selection");
 Mix(controls.active);Mix(destroyedActions);
 // Duplicate IDs select the focused instance through the actual group method.
 Reset();CGUIWindowManager duplicate;auto* dw=duplicate.AddWindow(5);
 dw->Add<CGUIBaseContainer>(101,false)->selected=40;auto* selected=dw->Add<CGUIBaseContainer>(101,true);selected->selected=2;
 g_application.handler=[&](const CAction& action){selected->selected+=action.GetID()==ACTION_MOVE_DOWN?1:-1;return true;};
 duplicate.Send(ACTION_MOVE_DOWN);duplicate.Send(ACTION_MOVE_UP);
 Check(selected->selected==2&&g_application.calls==2&&destroyedActions==2,"batched navigation behavior");
 if(!historical){Check(Signed(Counter::MessageNavBeforeSelection)==3&&Signed(Counter::MessageNavAfterSelection)==2,"last pair uses focused duplicate");
 Check(CountOf(Counter::MessageNavCandidateChanged)==2,"count every batched selection transition");
 Check(CountOf(Counter::MessageNavStarted)==2&&CountOf(Counter::MessageNavCompleted)==2,"count every delivered message");}
 Mix(selected->selected);Mix(destroyedActions);
 for(int kind=0;kind<3;++kind){Reset();CGUIWindowManager unknown;auto* uw=unknown.AddWindow(5);
 if(kind==0)uw->Add<EPGContainer>(101);if(kind==1)uw->Add<CGUIBaseContainer>(101)->hasItem=false;
 if(kind==2)uw->Add<CGUIControl>(101);g_application.handler=[](const CAction&){return false;};
 unknown.Send(ACTION_MOVE_RIGHT);Check(g_application.calls==1&&destroyedActions==1,"unsupported dispatch unaffected");
 if(!historical)Check(CountOf(Counter::MessageNavUnknownSelection)==1&&Signed(Counter::MessageNavAfterSelection)==-1,
 "unsupported or empty selection stays unknown");Mix(destroyedActions);}
 Reset();CGUIWindowManager explicitTarget;auto* ew=explicitTarget.AddWindow(8);ew->Add<CGUIBaseContainer>(55)->selected=4;
 ew->handler=[](const CAction&){return false;};explicitTarget.Send(ACTION_MOVE_LEFT,8);
 Check(ew->calls==1&&g_application.calls==0&&destroyedActions==1,"explicit handler exactly once");
 if(!historical)Check(Signed(Counter::MessageNavRequestedWindow)==8&&CountOf(Counter::MessageNavHandled)==0,"explicit result observation");
 Mix(ew->calls);Mix(g_application.calls);
 Reset();CGUIWindowManager missing;missing.Send(ACTION_MOVE_LEFT,999);
 Check(g_application.calls==0&&destroyedActions==1,"missing target deletion unaffected");
 if(!historical)Check(CountOf(Counter::MessageNavCompleted)==1&&CountOf(Counter::MessageNavHandled)==0&&
 CountOf(Counter::MessageNavUnknownSelection)==1,"missing target is completed unknown");Mix(destroyedActions);
 Reset();CGUIWindowManager null;ThreadMessage m;null.OnApplicationMessage(&m);
 Check(!destroyedActions&&!null.windowReads&&!g_application.calls,"null payload untouched");
 if(!historical)Check(!CountOf(Counter::MessageNavStarted),"null payload unobserved");
 Reset();CGUIWindowManager other;g_application.handler=[](const CAction&){return false;};other.Send(ACTION_OTHER);
 Check(g_application.calls==1&&destroyedActions==1&&!other.activeReads,"non-navigation dispatch unaffected");
 if(!historical)Check(!CountOf(Counter::MessageNavStarted),"non-navigation not counted");Mix(destroyedActions);
}
static void Reentrant()
{
 Reset();CGUIWindowManager w;w.AddWindow(5)->Add<CGUIBaseContainer>(101);
 g_application.handler=[](const CAction&){GUI_FRAME_TIMING::detail::current=&otherRecord;return true;};
 w.Send(ACTION_MOVE_DOWN);
 Check(g_application.calls==1&&destroyedActions==1,"reentrant delivery behavior");
 if(!historical){Check(CountOf(Counter::MessageNavStarted)==1&&!CountOf(Counter::MessageNavCompleted),"changed frame rejects completion");
 Check(std::all_of(std::begin(otherRecord.counters),std::end(otherRecord.counters),[](auto n){return n==0;}),"no completion in unrelated frame");}
 Mix(destroyedActions);
 Reset();CGUIWindowManager nested;nested.AddWindow(5)->Add<CGUIBaseContainer>(101);
 g_application.handler=[](const CAction&){GUI_FRAME_TIMING::Frame inner;return true;};nested.Send(ACTION_MOVE_DOWN);
 Check((firstRecord.flags&GUI_FRAME_TIMING::NestedFrame)&&firstRecord.nestedFrames==1,"nested ambiguity must be flagged");
 if(!historical)Check(CountOf(Counter::MessageNavCompleted)==1,"nested same token retains flagged observation");
 Mix(firstRecord.nestedFrames);
 Reset();CGUIWindowManager throwing;throwing.AddWindow(5)->Add<CGUIBaseContainer>(101);
 g_application.handler=[](const CAction&)->bool{throw std::runtime_error("original handler failure");};
 ThreadMessage m{TMSG_GUI_ACTION,WINDOW_INVALID,new CAction(ACTION_MOVE_DOWN)};bool caught=false;
 try{throwing.OnApplicationMessage(&m);}catch(const std::runtime_error& e){caught=std::string(e.what())=="original handler failure";}
 delete static_cast<CAction*>(m.lpVoid); // Existing source does not own cleanup across a throwing handler.
 Check(caught&&g_application.calls==1&&destroyedActions==1,"handler exception unchanged");
 if(!historical)Check(CountOf(Counter::MessageNavStarted)==1&&!CountOf(Counter::MessageNavCompleted),"throw remains started not completed");
 Mix(caught);Mix(destroyedActions);
}
static void Redraw()
{
 for(bool enabled:{false,true})for(int algorithm:{0,1,3})for(int age:{0,2})for(bool visual:{false,true})for(int shape=0;shape<3;++shape)
 {
  Timing(enabled);windowSystem={};settings={};CGUITexture::quads=0;
  windowSystem.age=age;settings.advanced.m_guiAlgorithmDirtyRegions=algorithm;settings.advanced.m_guiVisualizeDirtyRegions=visual;
  CGUIWindowManager w;w.m_tracker.marked={{9,false},{10,false}};w.m_tracker.expire=shape==2;
  if(shape==1)w.m_tracker.solved={{1,false},{2,true},{3,false}};
  if(shape==2)w.m_tracker.solved={{8,true}};
  const bool result=w.Render();const bool forced=visual||age==0||algorithm==0;
  const unsigned expected=forced?1:(algorithm==3?(shape!=0?1:0):(shape==1?2:0));
  Check(w.passes==expected&&result==(expected!=0),"redraw policy matches432 baseline");
  Check(w.m_tracker.cleanArg==(visual?21:(age?age+1:10)),"aging policy unchanged");
  Check(windowSystem.gfx.next==1&&w.published==1,"render bookkeeping unchanged");
  Check(windowSystem.gfx.resets==(!forced&&algorithm!=3?1u:0u),"scissor reset policy unchanged");
  if(!forced&&algorithm!=3)Check(windowSystem.gfx.scissors==(shape==1?std::vector<int>{1,3}:std::vector<int>{}),"empty tiles never draw");
  if(!historical&&enabled){Check(CountOf(Counter::RenderCalls)==1&&CountOf(Counter::MarkedBeforeAge)==2&&
   CountOf(Counter::MarkedAfterAge)==(shape==2?0u:2u)&&CountOf(Counter::SolvedRegions)==w.m_tracker.solved.size(),"dirty observation follows actual aging");
   Check(Signed(Counter::EffectiveBufferAge)==(visual?20:age)&&Signed(Counter::DirtyAlgorithm)==algorithm,"age and algorithm metadata are actual inputs");
   Check(CountOf(Counter::RenderForced)==(forced?1u:0u)&&CountOf(Counter::RenderDirty)==(forced?0u:expected)&&
   CountOf(Counter::RenderClean)==(expected?0u:1u),"decision reason follows actual branch");}
  if(!enabled)Check(std::all_of(std::begin(firstRecord.counters),std::end(firstRecord.counters),[](auto n){return n==0;}),"disabled render counters untouched");
  Mix(w.passes);Mix(result);Mix(w.m_tracker.cleanArg);Mix(windowSystem.gfx.resets);Mix(CGUITexture::quads);
 }
 for(bool enabled:{false,true}){Timing(enabled);CGUIWindowManager w;w.m_dirtyregions={{1,false},{2,true},{3,false}};w.ForwardDirty();
 Check(w.m_tracker.markCalls==3&&w.m_tracker.marked==CDirtyRegionList({{1,false},{3,false}})&&w.published==1,"dirty forwarding policy unchanged");
 if(!historical&&enabled)Check(CountOf(Counter::WindowProcessCalls)==1&&CountOf(Counter::DirtyEmitted)==2,"only nonempty emissions counted");Mix(w.m_tracker.markCalls);}
 for(bool enabled:{false,true})for(bool can:{false,true})for(bool rendered:{false,true})for(bool video:{false,true})
 {Timing(enabled);sleeps.clear();CRenderSystemGLES r;r.can=can;r.PresentRender(rendered,video);
 const bool sleep=can&&!rendered&&!video;
 Check(sleeps==(sleep?std::vector<int>{40}:std::vector<int>{}),"idle40 policy unchanged");
 Check(r.presents==(can?1u:0u)&&r.vsync==(can?1u:0u),"presentation behavior unchanged");
 if(!historical&&enabled)Check(CountOf(Counter::Idle40ms)==(sleep?1u:0u),"idle counter follows original wait");
 Mix(r.presents);Mix(r.vsync);Mix(sleeps.size());if(sleep)Mix(sleeps[0]);}
}
int main(){try{NoTraversal("disabled");NoTraversal("noframe");NoTraversal("worker");Navigation();Reentrant();Redraw();
 std::cout<<"PASS "<<checks<<" checks digest "<<digest<<'\n';return 0;
 }catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
'''


def source(files, historical=False):
    wm = files[WM]
    action = block(wm, 'case TMSG_GUI_ACTION:')
    dispatch = 'void CGUIWindowManager::OnApplicationMessage(ThreadMessage* pMsg) {switch(pMsg->dwMessage){' + action + ' break;default:break;}}'
    process = block(wm, 'void CGUIWindowManager::Process(')
    start = process.index('  const bool observeDirty') if '  const bool observeDirty' in process else process.index('  for (auto& itr : m_dirtyregions)')
    tail = process[start:process.rindex('  PublishRenderBounds();')+len('  PublishRenderBounds();')]
    forward = 'void CGUIWindowManager::ForwardDirty() {\n' + tail + '\n}'
    return PRE + '\n' + block(files[GROUP], 'CGUIControl *CGUIControlGroup::GetFocusedControl() const') + '\n' + dispatch + '\n' + forward + '\n' + block(wm, 'bool CGUIWindowManager::Render()') + '\n' + block(files[GLES], 'void CRenderSystemGLES::PresentRender(') + '\n' + TESTS


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='gui-redraw-decisions-'))
    out.mkdir(parents=True, exist_ok=True)
    files = {f: (ROOT/f).read_text() for f in [WM, GLES, GROUP]}
    baseline = {f: subprocess.check_output(['git', 'show', BASELINE+':'+f], cwd=ROOT, text=True) for f in files}
    cases = [('actual', files, False, None), ('baseline432', baseline, True, None)]
    mutations = [
        ('stale-snapshot', WM, 'const auto after = snapshot();', 'const auto after = before;', 'fresh target snapshot after replacement'),
        ('dispatch-twice', WM, 'handled = g_application.OnAction(*action);', '{ handled = g_application.OnAction(*action); handled = g_application.OnAction(*action); }', 'inactive dispatch exactly once'),
        ('drop-action-delete', WM, '      delete action;', '      (void)action;', 'action deleted exactly once'),
        ('disabled-traversal', WM, 'const bool observe = frame != 0 &&', 'const bool observe = true &&', 'inactive observer must not traverse'),
        ('missing-selection-count', WM, 'GUI_FRAME_TIMING::Count(Counter::MessageNavCandidateChanged);', '(void)0;', 'count every batched selection transition'),
        ('first-message-only', WM, 'else if (sameTarget && before[2] != after[2])', 'else if (sameTarget && before[2] != after[2] && CountOf(Counter::MessageNavCandidateChanged) == 0)', 'count every batched selection transition'),
        ('dropped-handled', WM, 'Add(Counter::MessageNavHandled, handled ? 1 : 0);', 'Add(Counter::MessageNavHandled, handled ? 0 : 0);', 'target and handled recorded'),
        ('missing-frame-guard', WM, 'if (observe && GUI_FRAME_TIMING::ActiveFrame() == frame)', 'if (observe)', 'no completion in unrelated frame'),
        ('empty-selection', WM, 'container && container->GetListItem(0)', 'container', 'unsupported or empty selection stays unknown'),
        ('wrong-duplicate', GROUP, 'if (i->second->HasFocus())', 'if (true)', 'last pair uses focused duplicate'),
        ('force-redraw', WM, 'visualizeDirtyRegions || bufferAge == 0 ||', 'true || visualizeDirtyRegions || bufferAge == 0 ||', 'redraw policy matches432 baseline'),
        ('draw-empty-tiles', WM, 'if (i.IsEmpty())', 'if (false)', 'redraw policy matches432 baseline'),
        ('missing-scissor-reset', WM, 'CServiceBroker::GetWinSystem()->GetGfxContext().ResetScissors();', '(void)0;', 'scissor reset policy unchanged'),
        ('drop-forward', WM, 'm_tracker.MarkDirtyRegion(itr);', '(void)itr;', 'dirty forwarding policy unchanged'),
        ('changed-idle-wait', GLES, 'KODI::TIME::Sleep(40ms);', 'KODI::TIME::Sleep(10ms);', 'idle40 policy unchanged'),
    ]
    for name, path, old, new, witness in mutations:
        changed = dict(files);changed[path] = replace_once(changed[path], old, new)
        cases.append((name, changed, False, witness))
    results = {}; digests = {}
    try:
        for name, selected, historical, witness in cases:
            cpp, binary = out/(name+'.cpp'), out/name
            generated = source(selected, historical)
            cpp.write_text(generated)
            command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation', '-pthread', '-DHAS_LIBAMCODEC', '-DHISTORICAL='+str(int(historical)), '-I'+str(ROOT/'xbmc'), str(cpp), '-o', str(binary)]
            if args.sanitize: command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
            built = subprocess.run(command, capture_output=True, text=True)
            (out/(name+'.compile.log')).write_text(built.stdout+built.stderr)
            if built.returncode: raise RuntimeError(name+' compile failed: '+built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0'})
            (out/(name+'.stdout')).write_text(run.stdout);(out/(name+'.stderr')).write_text(run.stderr)
            accepted = run.returncode == 0 if witness is None else run.returncode == 1 and 'FAIL: '+witness in run.stderr
            results[name] = {'accepted':accepted, 'exit_code':run.returncode, 'expected_witness':witness, 'command':command,
                             'generated_sha256':hashlib.sha256(generated.encode()).hexdigest()}
            if not accepted: raise RuntimeError(name+': '+run.stdout+run.stderr)
            if witness is None:
                digests[name]=re.search(r'digest (\d+)',run.stdout).group(1)
                print(name,run.stdout.strip(),flush=True)
            else: print('Rejected',name,'with',witness,flush=True)
        assert digests['actual']==digests['baseline432'],digests
    finally:
        (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        (out/'provenance.json').write_text(json.dumps({'baseline':BASELINE,'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in [WM,GLES,GROUP,HEADER]},
          'record_setup':'Real header ActiveFrame/Add/Set/Count; stack-owned record installs main owner/active state directly. Separate writer tool owns initialization/mmap/format acceptance.',
          'scope':'Actual TMSG case, whole WM Render/GLES PresentRender, actual Process dirty-forward tail and GetFocusedControl; actual timing header; fixture action routing/backend/tracker/geometry/window resources. No complete application, real scheduler, EGL/GPU, latency or device acceptance.'},indent=2)+'\n')
    print('PASS matching432 behavior digest and',len(mutations),'specific semantic mutants; artifacts:',out)


if __name__ == '__main__':
    main()
