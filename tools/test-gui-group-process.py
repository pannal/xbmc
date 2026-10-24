#!/usr/bin/env python3
"""Execute complete production Group/ListGroup and base Process methods.

Compare the original and current methods using child/visibility/dirty-region and
affine graphics-context fixtures. This checks bounds, marker, ordering, origins
and list-item reset, while counting discarded projections. It does not execute
real animation, info providers, rendering matrices, GPU work or device pacing.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

FILES = ['xbmc/guilib/GUIControl.cpp','xbmc/guilib/GUIControlGroup.cpp',
         'xbmc/guilib/GUIListGroup.cpp']
SIGNATURES = ['void CGUIControl::Process(', 'void CGUIControlGroup::Process(',
              'void CGUIListGroup::Process(']
BASELINE = '0aef062ae7d1d2c9002f35fb5d11ab3bb5fcdd21'


def method(source, signature):
    start=source.index(signature)
    masked=re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                  lambda match:' '*len(match.group()),source)
    brace=masked.index('{',start);end=brace+1;depth=1
    while depth:
        depth+=(masked[end]=='{')-(masked[end]=='}');end+=1
    return source[start:end]


FIXTURE=r'''
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
static unsigned checks=0;
static void Check(bool value,const char* description)
{++checks;if(!value)throw std::runtime_error(description);}
struct CPoint {float x=0,y=0;CPoint()=default;CPoint(float a,float b):x(a),y(b){}};
struct CRect
{
 float x1=0,y1=0,x2=0,y2=0;
 CRect()=default;CRect(float a,float b,float c,float d):x1(a),y1(b),x2(c),y2(d){}
 bool Empty()const{return x2<=x1||y2<=y1;}
 void Union(const CRect& other)
 {
  if(other.Empty())return;
  if(Empty()){*this=other;return;}
  x1=std::min(x1,other.x1);y1=std::min(y1,other.y1);
  x2=std::max(x2,other.x2);y2=std::max(y2,other.y2);
 }
};
using CDirtyRegionList=std::vector<CRect>;
struct CGUIListItem {int id;};
struct Graphics
{
 std::array<float,6> transform{1,0,0,1,0,0};
 CPoint origin;std::vector<CPoint> origins;
 unsigned projections=0,leafVisits=0;
 std::vector<std::string> events;
 void SetOrigin(float x,float y){origins.push_back(origin);origin.x+=x;origin.y+=y;}
 void RestoreOrigin(){Check(!origins.empty(),"restore has matching origin");origin=origins.back();origins.pop_back();}
 CRect GenerateAABB(const CRect& rect)
 {
  ++projections;CRect result;bool first=true;
  for(float x:{rect.x1,rect.x2})for(float y:{rect.y1,rect.y2})
  {
   const float xx=x+origin.x,yy=y+origin.y;
   const float px=transform[0]*xx+transform[1]*yy+transform[4];
   const float py=transform[2]*xx+transform[3]*yy+transform[5];
   if(first){result=CRect(px,py,px,py);first=false;}
   else{result.x1=std::min(result.x1,px);result.y1=std::min(result.y1,py);
        result.x2=std::max(result.x2,px);result.y2=std::max(result.y2,py);}
  }
  return result;
 }
};
static Graphics gfx;
struct Window {Graphics& GetGfxContext(){return gfx;}};
struct CServiceBroker {static Window* GetWinSystem(){static Window window;return &window;}};
struct CGUIControl
{
 int id=0;float m_posX=0,m_posY=0;
 bool visible=true,nextVisible=true,dirtyHidden=false,m_hasProcessed=false;
 CRect box{1,2,12,14},m_renderRegion;
 virtual ~CGUIControl()=default;
 virtual bool Group()const{return false;}
 virtual void Process(unsigned int,CDirtyRegionList&);
 virtual void UpdateVisibility(const CGUIListItem* item)
 {
  gfx.events.push_back("V"+std::to_string(id)+":"+std::to_string(item?item->id:0));
  visible=nextVisible;
 }
 void DoProcess(unsigned int time,CDirtyRegionList& dirty)
 {
  gfx.events.push_back("D"+std::to_string(id));
  if(visible){if(!Group())++gfx.leafVisits;Process(time,dirty);}
  else if(dirtyHidden)dirty.push_back(m_renderRegion);
 }
 bool IsVisible()const{return visible;}
 CPoint GetPosition()const{return {m_posX,m_posY};}
 CRect CalcRenderRegion()const{return box;}
 CRect GetRenderRegion()const{return m_renderRegion;}
};
struct CGUIControlGroup:CGUIControl
{
 using iControls=std::vector<CGUIControl*>::iterator;
 std::vector<CGUIControl*> m_children;
 bool Group()const override{return true;}
 void Process(unsigned int,CDirtyRegionList&)override;
};
struct CGUIListGroup:CGUIControlGroup
{
 const CGUIListItem* m_item=nullptr;
 void UpdateVisibility(const CGUIListItem* item)override
 {CGUIControl::UpdateVisibility(item);m_item=item;}
 void Process(unsigned int,CDirtyRegionList&)override;
};
@METHODS@
static uint64_t digest=1469598103934665603ULL;
static void Hash(uint64_t value){digest^=value;digest*=1099511628211ULL;}
static void HashRect(const CRect& rect)
{for(float value:{rect.x1,rect.y1,rect.x2,rect.y2}){uint32_t bits;std::memcpy(&bits,&value,4);Hash(bits);}}
static void HashControl(CGUIControl& control)
{
 Hash(control.id);Hash(control.m_hasProcessed);HashRect(control.m_renderRegion);
 if(control.Group())Check(control.m_hasProcessed,"group processing marker retained");
 if(auto* list=dynamic_cast<CGUIListGroup*>(&control))
 {Check(list->m_item==nullptr,"list item reset after processing");Hash(list->m_item==nullptr);}
 if(auto* group=dynamic_cast<CGUIControlGroup*>(&control))
  for(auto* child:group->m_children)HashControl(*child);
}
static void Scenario(bool listRoot,bool nested,unsigned layout,const std::array<float,6>& transform,unsigned extra)
{
 gfx=Graphics{};gfx.transform=transform;gfx.origin={3,-4};
 std::unique_ptr<CGUIControlGroup> root=listRoot?std::unique_ptr<CGUIControlGroup>(new CGUIListGroup):std::make_unique<CGUIControlGroup>();
 root->id=1;root->m_posX=11;root->m_posY=-7;
 CGUIControl a,b,c;a.id=2;b.id=3;c.id=5;
 a.box=CRect(-8,2,12,20);b.box=CRect(30,-11,45,3);c.box=CRect(6,8,16,23);
 CGUIControlGroup subGroup;CGUIListGroup subList;
 CGUIControlGroup* sub=listRoot?&subGroup:&subList;
 sub->id=4;sub->m_posX=-12;sub->m_posY=15;sub->m_children={&c};
 if(layout!=3){root->m_children={&a,&b};if(nested)root->m_children.push_back(sub);}
 const unsigned groupCount=1+(nested&&layout!=3?1:0);
 CGUIListItem item{99};
 for(unsigned frame=0;frame<2;++frame)
 {
  for(auto* control:std::vector<CGUIControl*>{root.get(),&a,&b,&c,sub})control->m_hasProcessed=false;
  if(auto* list=dynamic_cast<CGUIListGroup*>(root.get()))list->m_item=&item;
  a.visible=false;a.nextVisible=layout!=1;
  b.visible=true;b.nextVisible=layout==0;b.dirtyHidden=layout==2;
  b.m_renderRegion=CRect(500,600,510,610);
  c.nextVisible=layout!=1;c.dirtyHidden=false;
  if(frame==1&&layout==2){a.nextVisible=false;a.dirtyHidden=true;a.m_renderRegion=CRect(-60,-40,-30,-10);}
  const unsigned projections=gfx.projections,leaves=gfx.leafVisits;
  CDirtyRegionList dirty;root->Process(100+frame,dirty);
  Check(gfx.origins.empty()&&gfx.origin.x==3&&gfx.origin.y==-4,"all origins restored");
  Check(gfx.projections-projections==(gfx.leafVisits-leaves)+groupCount*extra,
        "only discarded group projections removed");
  if(layout==3)Check(root->m_renderRegion.Empty(),"empty group retains empty child bounds");
  HashControl(*root);Hash(dirty.size());for(const auto& rect:dirty)HashRect(rect);
  for(const auto& event:gfx.events){for(unsigned char ch:event)Hash(ch);Hash(0xff);}
  gfx.events.clear();
 }
}
int main(int argc,char** argv)
{
 try
 {
  Check(argc==2,"expected redundant projection count required");
  const unsigned extra=static_cast<unsigned>(std::stoul(argv[1]));unsigned scenarios=0;
  const std::array<float,6> transforms[]={{1,0,0,1,0,0},{2,0,0,1.5f,20,-30},
                                       {0,-1,1,0,5,8},{1,.25f,-.5f,1,-7,11}};
  for(bool list:{false,true})for(bool nested:{false,true})for(unsigned layout=0;layout<4;++layout)
   for(const auto& transform:transforms){Scenario(list,nested,layout,transform,extra);++scenarios;}
  std::cout<<"scenarios="<<scenarios<<" checks="<<checks<<" digest="<<digest<<'\n';return 0;
 }
 catch(const std::exception& error){std::cerr<<"FAIL "<<error.what()<<'\n';return 1;}
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline-ref',default=BASELINE)
    parser.add_argument('--cxx',default=os.environ.get('CXX','c++'))
    parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args()
    current=[method((args.repo/path).read_text(),signature) for path,signature in zip(FILES,SIGNATURES)]
    baseline=[method(subprocess.check_output(['git','show',args.baseline_ref+':'+path],cwd=args.repo,text=True),signature)
              for path,signature in zip(FILES,SIGNATURES)]
    with tempfile.TemporaryDirectory(prefix='gui-group-process-') as directory:
        work=Path(directory)
        def run(label,methods,extra):
            source=work/(label+'.cpp');source.write_text(FIXTURE.replace('@METHODS@','\n'.join(methods)))
            binary=source.with_suffix('')
            command=shlex.split(args.cxx)+['-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                    '-Wno-unused-parameter',str(source),'-o',str(binary)]
            if args.sanitize:command+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
            subprocess.run(command,check=True)
            return subprocess.run([str(binary),str(extra)],capture_output=True,text=True,
                    env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
        original=run('baseline',baseline,1);actual=run('current',current,0)
        assert original.returncode==actual.returncode==0,(original.stdout,original.stderr,actual.stdout,actual.stderr)
        expected=original.stdout.split('digest=')[1].strip()
        assert actual.stdout.split('digest=')[1].strip()==expected,('semantic output changed',original.stdout,actual.stdout)
        print('PASS baseline:',original.stdout.strip())
        print('PASS current:',actual.stdout.strip())
        controls={
            'processed-marker':(1,'m_hasProcessed = true;','m_hasProcessed = false;'),
            'origin-restore':(1,'CServiceBroker::GetWinSystem()->GetGfxContext().RestoreOrigin();',''),
            'hidden-dirty-bounds':(1,'control->IsVisible() || (oldDirty != dirtyregions.size())',
                                     'control->IsVisible() || (oldDirty != dirtyregions.size() && false)'),
            'list-item-reset':(2,'m_item = NULL;',''),
            'visibility-order':(1,'    control->UpdateVisibility(nullptr);',''),
        }
        for name,(index,old,new) in controls.items():
            changed=list(current);assert changed[index].count(old)==1,(name,old)
            changed[index]=changed[index].replace(old,new,1)
            if name=='visibility-order':
                changed[index]=changed[index].replace('    control->DoProcess(currentTime, dirtyregions);',
                      '    control->DoProcess(currentTime, dirtyregions);\n    control->UpdateVisibility(nullptr);',1)
            result=run(name,changed,0)
            assert result.returncode!=0 or result.stdout.split('digest=')[1].strip()!=expected,('Mutation survived',name)
            print('Rejected mutation:',name)
    print('Limits: child/visibility/dirty and affine-context fixtures; no real animation, info providers, GPU or device-performance acceptance.')


if __name__=='__main__':main()
