#!/usr/bin/env python3
"""Exercise layout-owned selection caching against the unconditional production baseline.

Extracts the real layout declaration/methods and SetState -> SetSelected ->
SetColor chain. Control ownership, XML/info/time and render submission are host
fixtures. This is a behavior/traversal test, not a device frame-time benchmark.
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

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'ee7e5fcb7165631fe7d2781a7a60a3ff6b7d0f04'
SPEC = importlib.util.spec_from_file_location('extract_gui', ROOT / 'tools/test-aml-texture-upload-context.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
FILES = ['xbmc/guilib/GUIListItemLayout.h', 'xbmc/guilib/GUIListItemLayout.cpp',
         'xbmc/guilib/GUIListGroup.cpp', 'xbmc/guilib/GUIListLabel.cpp', 'xbmc/guilib/GUILabel.cpp']
METHODS = ['CGUIListItemLayout::CGUIListItemLayout()',
           'CGUIListItemLayout::CGUIListItemLayout(const CGUIListItemLayout& from)',
           'CGUIListItemLayout::CGUIListItemLayout(const CGUIListItemLayout& from, CGUIControl* control)',
           'void CGUIListItemLayout::Process(', 'void CGUIListItemLayout::Render(',
           'void CGUIListItemLayout::LoadControl(', 'void CGUIListItemLayout::LoadLayout(',
           'void CGUIListItemLayout::CreateListControlLayouts(',
           'void CGUIListItemLayout::FreeResources(']

PRELUDE = r'''
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
static int checks=0, typeReads=0, failInvalid=0, failAdd=0;
static bool failFree=false;
static uint64_t digest=1469598103934665603ULL;
static void Check(bool ok,const char* text){++checks;if(!ok)throw std::runtime_error(text);}
static void Mix(uint64_t n){digest^=n;digest*=1099511628211ULL;}
struct CPoint{};struct CRect{CRect(float,float,float,float){}};
enum ORIENTATION{HORIZONTAL,VERTICAL};enum ANIMATION_TYPE{ANIM_NONE};
using CDirtyRegionList=std::vector<int>;
struct CGUIListItem{bool selected=false,playing=false;virtual ~CGUIListItem()=default;virtual bool IsFileItem()const{return false;}bool IsSelected()const{return selected;}};
struct CFileItem:CGUIListItem{CFileItem()=default;explicit CFileItem(const CGUIListItem& x){selected=x.selected;playing=x.playing;}bool IsFileItem()const override{return true;}};
namespace INFO{constexpr int DEFAULT_CONTEXT=0;struct Info{bool Get(int){return true;}};using InfoPtr=std::shared_ptr<Info>;}
namespace KODI{namespace GUILIB{namespace GUIINFO{
struct CGUIInfoBool{bool value=false;void Update(int,const CGUIListItem* i){value=i->playing;}void Parse(const char*,int){}operator bool()const{return value;}};
struct CGUIInfoLabel{CGUIInfoLabel(const char*,const char*,int){}};
}}}
namespace GUIINFO=KODI::GUILIB::GUIINFO;
namespace XbmcThreads{template<class T=std::chrono::milliseconds>struct EndTime{
 bool past=false;static T Max(){return T::max();}void Set(std::chrono::milliseconds){past=false;}bool IsTimePast()const{return past;}
};}
struct CLabelInfo{float offsetX=0,offsetY=0;};struct CTextureInfo{explicit CTextureInfo(const char* = "") {}};
struct CGUILabel{enum COLOR{COLOR_TEXT,COLOR_SELECTED};COLOR m_color=COLOR_TEXT;bool SetColor(COLOR);};
class CGUIControl{
public:
 enum TYPE{GUICONTROL_LISTGROUP,GUICONTROL_LISTLABEL,GUICONTROL_IMAGE};enum GUISCROLLVALUE{FOCUS};
 explicit CGUIControl(TYPE t):kind(t){}virtual ~CGUIControl()=default;
 TYPE kind;bool invalid=false;int tint=0,processed=0;
 TYPE GetControlType()const{++typeReads;return kind;}
 bool IsGroup()const{return kind==GUICONTROL_LISTGROUP;}
 virtual CGUIControl* Clone()const=0;
 virtual void SetInvalid(){invalid=true;if(failInvalid && --failInvalid==0)throw std::runtime_error("injected invalidation");}
 virtual void Tick(unsigned,CDirtyRegionList&)=0;
 virtual void Snapshot(){Mix(kind);Mix(invalid);Mix(tint);Mix(processed);}
};
class CGUIListLabel:public CGUIControl{
public:
 CGUILabel m_label;
 CGUIListLabel():CGUIControl(GUICONTROL_LISTLABEL){}
 CGUIListLabel(int,int,float,float,float,float,const CLabelInfo&,const GUIINFO::CGUIInfoLabel&,GUISCROLLVALUE):CGUIListLabel(){}
 CGUIControl* Clone()const override{return new CGUIListLabel(*this);}
 void SetSelected(bool);
 void Tick(unsigned time,CDirtyRegionList& dirty)override{++processed;tint=int(time%7);if(invalid)dirty.push_back(int(m_label.m_color));invalid=false;}
 void Snapshot()override{CGUIControl::Snapshot();Mix(m_label.m_color);}
};
class CGUIControlGroup:public CGUIControl{
public:
 using iControls=std::vector<CGUIControl*>::iterator;std::vector<CGUIControl*> m_children;
 CGUIControlGroup():CGUIControl(GUICONTROL_LISTGROUP){}
 CGUIControlGroup(const CGUIControlGroup& from):CGUIControl(from){for(auto* c:from.m_children)m_children.push_back(c->Clone());}
 ~CGUIControlGroup(){for(auto* c:m_children)delete c;}
 void AddControl(CGUIControl* c){m_children.push_back(c);if(failAdd && --failAdd==0)throw std::runtime_error("injected add");}
 float GetXPosition()const{return 0;}float GetYPosition()const{return 0;}float GetWidth()const{return 100;}float GetHeight()const{return 40;}
 void Tick(unsigned time,CDirtyRegionList& dirty)override{++processed;for(auto* c:m_children)c->Tick(time,dirty);}
 void Snapshot()override{CGUIControl::Snapshot();Mix(m_children.size());for(auto* c:m_children)c->Snapshot();}
};
class CGUIListGroup:public CGUIControlGroup{
public:
 int visibility=0,process=0,render=0,infos=0,freed=0;std::function<void(CGUIListGroup&)> onInfo;
 CGUIListGroup(int=0,int=0,float=0,float=0,float=0,float=0){}
 CGUIListGroup(const CGUIListGroup& from):CGUIControlGroup(from){}
 CGUIControl* Clone()const override{return new CGUIListGroup(*this);}
 void SetState(bool,bool);
 void SetPushUpdates(bool){}void SetParentControl(CGUIControl*){}void AllocResources(){}
 void SetWidth(float){}void SetHeight(float){}int GetParentID()const{return 0;}
 void SetInvalid()override{for(auto* c:m_children)c->SetInvalid();}
 void UpdateInfo(const CGUIListItem*){++infos;if(onInfo)onInfo(*this);}
 void UpdateVisibility(const CGUIListItem*){++visibility;}
 void DoProcess(unsigned time,CDirtyRegionList& dirty){++process;Tick(time,dirty);}
 void DoRender(){++render;Snapshot();}
 void FreeResources(bool){++freed;if(failFree){failFree=false;throw std::runtime_error("injected free");}}
};
class CGUIImage:public CGUIControl{
public:
 CGUIImage(int,int,float,float,float,float,const CTextureInfo&):CGUIControl(GUICONTROL_IMAGE){}
 CGUIControl* Clone()const override{return new CGUIImage(*this);}
 void SetVisibleCondition(const std::string&){}void SetInfo(const GUIINFO::CGUIInfoLabel&){}void SetAspectRatio(int){}
 void Tick(unsigned time,CDirtyRegionList&)override{++processed;tint=int(time%7);invalid=false;}
};
struct CAspectRatio{enum{AR_KEEP};};
struct TiXmlElement{
 bool group=false;float width=100,height=40;unsigned interval=0;std::vector<std::unique_ptr<TiXmlElement>> children;TiXmlElement* next=nullptr;
 TiXmlElement* Add(bool isGroup=false){if(!children.empty())children.back()->next=nullptr;auto x=std::make_unique<TiXmlElement>();x->group=isGroup;auto* p=x.get();if(!children.empty())children.back()->next=p;children.push_back(std::move(x));return p;}
 void QueryFloatAttribute(const char* name,float* out){*out=std::string(name)=="width"?width:height;}
 void QueryUnsignedAttribute(const char*,unsigned* out){*out=interval;}
 const char* Attribute(const char*)const{return nullptr;}
 TiXmlElement* FirstChildElement(const char*){return children.empty()?nullptr:children[0].get();}
 TiXmlElement* NextSiblingElement(const char*){return next;}
};
struct CGUIControlFactory{CGUIControl* Create(int,const CRect&,TiXmlElement* e,bool){return e->group?static_cast<CGUIControl*>(new CGUIListGroup()):new CGUIListLabel();}};
struct FakeInfo{INFO::InfoPtr Register(const char*,int){return std::make_shared<INFO::Info>();}};
struct FakeGUI{FakeInfo& GetInfoManager(){static FakeInfo x;return x;}};
struct CServiceBroker{static FakeGUI* GetGUI(){static FakeGUI x;return &x;}};
'''

MAIN = r'''
static std::vector<CGUIListLabel*> Labels(CGUIControlGroup& g){
 std::vector<CGUIListLabel*> out;
 for(auto* c:g.m_children){if(auto* l=dynamic_cast<CGUIListLabel*>(c))out.push_back(l);else if(auto* nested=dynamic_cast<CGUIControlGroup*>(c)){auto sub=Labels(*nested);out.insert(out.end(),sub.begin(),sub.end());}}
 return out;
}
static void Populate(CGUIListItemLayout& x){
 x.m_group.AddControl(new CGUIListLabel());auto* nested=new CGUIListGroup();nested->AddControl(new CGUIListLabel());x.m_group.AddControl(nested);
}
static void Frame(CGUIListItemLayout& x,CFileItem& item,unsigned time){
 CDirtyRegionList dirty;int v=x.m_group.visibility,p=x.m_group.process,r=x.m_group.render;
 x.Process(&item,0,time,dirty);x.Render(&item,0);
 Check(x.m_group.visibility==v+1&&x.m_group.process==p+1&&x.m_group.render==r+1,"visibility/process/render run every frame");
 for(auto* label:Labels(x.m_group)){Check(label->m_label.m_color==((item.selected||item.playing)?CGUILabel::COLOR_SELECTED:CGUILabel::COLOR_TEXT),"selected label matches selected-or-playing");Check(label->tint==int(time%7),"dynamic color runs every frame");}
 Mix(time);Mix(dirty.size());for(int d:dirty)Mix(d);Mix(x.m_group.visibility);Mix(x.m_group.process);Mix(x.m_group.render);
}
static void ExpectThrow(const std::function<void()>& f){bool threw=false;try{f();}catch(const std::runtime_error& e){threw=std::string(e.what()).find("injected ")==0;}Check(threw,"fixture exception reached its intended stage");}
static void ProcessThrow(CGUIListItemLayout& x,CFileItem& item){CDirtyRegionList dirty;ExpectThrow([&]{x.Process(&item,0,999,dirty);});}
static void Load(CGUIListItemLayout& x,bool focused=false){TiXmlElement xml;auto* nested=xml.Add(true);nested->Add();x.LoadLayout(&xml,0,focused,100,40);}
static void Create(CGUIListItemLayout& x){CLabelInfo a,b;CTextureInfo t;x.CreateListControlLayouts(100,40,true,a,b,t,t,40,10,10,"a","b");}
static void Stable(bool witness,bool selected){
 CGUIListItemLayout x;Populate(x);CFileItem item;item.selected=selected;Frame(x,item,1);
 const int before=typeReads;for(unsigned t=2;t<202;++t)Frame(x,item,t);
 const int repeated=typeReads-before;
 if(witness)Check(repeated==0,"stable frames must avoid redundant selection traversal");
 std::printf("stable selected=%d repeated-frame control-type reads=%d (200 frames, 2 labels + nested group)\n",selected,repeated);
}
static void Changes(){
 CGUIListItemLayout x;Populate(x);CFileItem i;
 // The info path must not hide a missing comparison in the selection cache:
 // playing remains false and only the first frame performs an info refresh.
 Frame(x,i,290);i.selected=true;Frame(x,i,291);i.selected=false;Frame(x,i,292);
 Check(x.m_group.infos==1,"plain selection transitions do not refresh info");
 // Playing changes become visible through the normal invalidated/timed info path.
 for(unsigned n=0;n<64;++n){i.selected=(n&1);i.playing=(n&2);x.m_infoUpdateTimeout.past=true;Frame(x,i,300+n);}
 // Selection changes do not require an info refresh; cached playing remains true.
 i.selected=false;i.playing=true;x.m_infoUpdateTimeout.past=true;Frame(x,i,400);
 i.selected=true;Frame(x,i,401);i.selected=false;Frame(x,i,402);
}
static void Info(bool timeout,bool throwing){
 CGUIListItemLayout x;Populate(x);CFileItem i;i.selected=true;Frame(x,i,500);
 x.m_group.onInfo=[&](CGUIListGroup&){if(throwing)throw std::runtime_error("injected info");};
 if(timeout)x.m_infoUpdateTimeout.past=true;else x.SetInvalid();
 if(throwing){ProcessThrow(x,i);x.m_group.onInfo={};x.m_infoUpdateTimeout.past=false;}
 if(!throwing)Check(x.m_group.infos==1,"info update occurs in Process, not before it");
 Frame(x,i,501);Check(x.m_group.infos==2,"invalidated/timed info callback occurs exactly once");
}
static void Lifecycle(const std::string& kind,bool throwing){
 CGUIListItemLayout x;Populate(x);CFileItem i;i.selected=true;Frame(x,i,600);
 if(kind=="load"){
  if(throwing){failAdd=2;ExpectThrow([&]{Load(x,true);});}else Load(x,true);
  Check(x.m_focused,"focused reload retains its new focused flag");
 }else if(kind=="create"){
  if(throwing){failAdd=4;ExpectThrow([&]{Create(x);});}else Create(x);
 }else {failFree=throwing;if(throwing)ExpectThrow([&]{x.FreeResources();});else x.FreeResources();}
 if(kind=="free")Check(x.m_group.freed==1,"resource boundary forwards exactly once");
 Frame(x,i,601);
}
static void Clone(){
 CGUIListItemLayout source;Populate(source);CFileItem i;i.selected=true;Frame(source,i,700);
 CGUIListItemLayout a(source),b(source,nullptr);
 // Keep the real default m_invalidated=true. This also clears cache through
 // Process, so copying a cached value alone has no unique visual witness.
 for(auto* x:{&a,&b}){
  Check(Labels(x->m_group).size()==Labels(source.m_group).size(),"clone retains control tree");
  Check(Labels(x->m_group)[0]!=Labels(source.m_group)[0],"clone owns independent labels");
  Frame(*x,i,701);Check(x->m_group.infos==1,"clone first frame performs normal info update");
 }
}
static void PartialTraversal(bool rollback){
 CGUIListItemLayout x;Populate(x);CFileItem i;i.selected=false;Frame(x,i,800);
 i.selected=true;failInvalid=1;ProcessThrow(x,i);failInvalid=0;
 i.selected=!rollback;Frame(x,i,801);
}
int main(int argc,char** argv){try{
 std::string mode=argc>1?argv[1]:"all";
 if(mode=="walk"){Stable(true,false);Stable(true,true);}
 else if(mode=="clone")Clone();
 else if(mode=="rollback")PartialTraversal(true);
 else if(mode=="retry")PartialTraversal(false);
 else if(mode=="invalid-info")Info(false,true);
 else if(mode=="timeout-info")Info(true,true);
 else if(mode=="load")Lifecycle("load",true);
 else if(mode=="create")Lifecycle("create",true);
 else if(mode=="free")Lifecycle("free",true);
 else if(mode=="changes")Changes();
 else {
  Stable(false,false);Stable(false,true);Changes();Info(false,false);Info(true,false);Info(false,true);Info(true,true);
  for(const auto* op:{"load","create","free"}){Lifecycle(op,false);Lifecycle(op,true);}
  Clone();PartialTraversal(true);PartialTraversal(false);
 }
 std::printf("PASS %s checks=%d digest=%llu\n",mode.c_str(),checks,static_cast<unsigned long long>(digest));return 0;
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def function(source, signature):
    return EXTRACT.function(source, signature)


def generate(files, overrides=None, baseline=False):
    overrides = overrides or {}
    header = EXTRACT.block(files[FILES[0]], 'class CGUIListItemLayout final').replace('protected:', 'public:')
    # The historical methods run in the same declaration, with its extra unused
    # optional field. That keeps fixtures identical; it makes no old-ABI claim.
    result = PRELUDE + '\n' + header + '\n'
    for signature in METHODS:
        result += overrides.get(signature, function(files[FILES[1]], signature)) + '\n'
    result += function(files[FILES[2]], 'void CGUIListGroup::SetState(') + '\n'
    result += function(files[FILES[3]], 'void CGUIListLabel::SetSelected(') + '\n'
    result += function(files[FILES[4]], 'bool CGUILabel::SetColor(') + '\n'
    return result + '\n' + MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='gui-selection-'))
    out.mkdir(parents=True, exist_ok=True)
    files = {p: (ROOT / p).read_text() for p in FILES}
    records = []
    env = os.environ.copy()
    if args.sanitize:
        # Host LSan is unavailable in the ptrace-restricted environment. Honor
        # other caller options, while making this required limit explicit.
        env['ASAN_OPTIONS'] = env.get('ASAN_OPTIONS', '') + ':detect_leaks=0'

    def build(name, changes=None, baseline=False):
        cpp, exe = out / (name + '.cpp'), out / name
        cpp.write_text(generate(files, changes, baseline))
        cmd = ['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter']
        if args.sanitize:
            cmd += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie']
        cmd += [str(cpp), '-o', str(exe)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        (out / (name + '.compile.log')).write_text(p.stdout + p.stderr)
        records.append({'compile': cmd, 'exit': p.returncode})
        if p.returncode:
            raise RuntimeError(p.stderr)
        return exe

    def run(exe, mode, fail=None):
        p = subprocess.run([str(exe), mode], capture_output=True, text=True, timeout=15, env=env)
        text = p.stdout + p.stderr
        (out / (exe.name + '-' + mode + '.log')).write_text(text)
        accepted = p.returncode == 0 if fail is None else p.returncode == 1 and ('FAIL ' + fail) in text
        records.append({'run': [str(exe), mode], 'exit': p.returncode, 'accepted': accepted,
                        'semantic_failure': fail, 'output': text})
        print(exe.name, mode, p.returncode, text.strip())
        if not accepted:
            raise RuntimeError((exe.name, mode, p.returncode, text))
        return re.search(r'digest=(\d+)', text).group(1) if fail is None else None

    try:
        candidate = build('actual')
        actual_digest = run(candidate, 'all')
        run(candidate, 'walk')
        old = subprocess.check_output(['git', 'show', BASELINE + ':' + FILES[1]], cwd=ROOT, text=True)
        historical = {sig: function(old, sig) for sig in METHODS}
        baseline = build('historical-ee7-methods', historical, baseline=True)
        old_digest = run(baseline, 'all')
        if old_digest != actual_digest:
            raise RuntimeError(('historical label/dirty/render behavior differs', actual_digest, old_digest))
        run(baseline, 'walk', 'stable frames must avoid redundant selection traversal')

        process_sig = 'void CGUIListItemLayout::Process('
        process = function(files[FILES[1]], process_sig)
        resets = list(re.finditer(re.escape('m_lastSelected.reset();'), process))
        if len(resets) != 3:
            raise RuntimeError('Expected three independently owned Process resets')
        mutants = []
        match = resets[2]
        mutants.append(('missing-partial-reset', {process_sig: process[:match.start()] + process[match.end():]},
                        'rollback', 'selected label matches selected-or-playing'))
        for op, sig in [('load', 'void CGUIListItemLayout::LoadLayout('),
                         ('create', 'void CGUIListItemLayout::CreateListControlLayouts(')]:
            body = function(files[FILES[1]], sig)
            mutants.append(('missing-' + op + '-reset', {sig: body.replace('m_lastSelected.reset();', '', 1)}, op,
                            'selected label matches selected-or-playing'))
        ordered = 'm_group.SetState(selected, m_focused);\n    m_lastSelected = selected;'
        if ordered not in process:
            raise RuntimeError('Expected publish-after-traversal sequence')
        mutants.append(('early-publication', {process_sig: process.replace(ordered,
                        'm_lastSelected = selected;\n    m_group.SetState(selected, m_focused);')},
                        'retry', 'selected label matches selected-or-playing'))
        mutants.append(('missing-selection-comparison', {process_sig: process.replace(
                        '!m_lastSelected || *m_lastSelected != selected', '!m_lastSelected')},
                        'changes', 'selected label matches selected-or-playing'))
        mutants.append(('wrong-playing-predicate', {process_sig: process.replace('item->IsSelected() || m_isPlaying',
                        'item->IsSelected() && m_isPlaying')}, 'changes', 'selected label matches selected-or-playing'))
        mutants.append(('skipped-visibility', {process_sig: process.replace('m_group.UpdateVisibility(item);', '')},
                        'all', 'visibility/process/render run every frame'))
        mutants.append(('skipped-process', {process_sig: process.replace('m_group.DoProcess(currentTime, dirtyregions);', '')},
                        'all', 'visibility/process/render run every frame'))
        for name, changes, mode, witness in mutants:
            if generate(files, changes) == generate(files):
                raise RuntimeError(('mutation did not change production methods', name))
            run(build(name, changes), mode, witness)
    finally:
        (out / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
        (out / 'provenance.json').write_text(json.dumps({
            'baseline': BASELINE, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256': {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()},
            'asan_options': env.get('ASAN_OPTIONS'),
            'limits': 'Clone/info/free functional coverage retains normal invalidation and preserved label categories; their conservative cache resets are not independently tested by implementation-mirroring assertions or fabricated color changes. Partial traversal and partial info/free/add exceptions are deliberately injected defensive-contract stress, not observed current failures. Actual layout declaration (protected exposed), constructors/Process/Render/load/create/free and recursive SetState/SetSelected/SetColor. XML/factory/info/time/control ownership/dynamic-color/render fixtures; no full skin engine, actual GPU, concurrency or device frame-time claim. Historical methods use current compatible declaration with unused cache field; operation witness counts control-type reads, not time.'}, indent=2) + '\n')
        print('Artifacts:', out)


if __name__ == '__main__':
    main()
