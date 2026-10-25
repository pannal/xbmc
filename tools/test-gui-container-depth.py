#!/usr/bin/env python3
"""Replay complete container draw/depth methods against owned layout fixtures.

Pinned pre-change source supplies the draw compatibility baseline for base and wrapping
containers. Panels select their rows by the current scroll value and the scroll target with a
direction-independent preload since the panel row change, so their draws are checked against an
independent enumeration of those rows (row-major, focused item last, production position
arithmetic) instead of the historical draw. Current depth must visit exactly the layouts selected
by the draw enumeration, in back-to-front order. Graphics,
layout children/text fades, scrolling state and provider replacement are fixtures;
this is not full Kodi integration, GL execution or device performance evidence.
The historical full-list/both-layout depth behavior is a counterexample, not the
new correctness oracle. No production source is rewritten by this tool.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


FILES = ["xbmc/guilib/GUIBaseContainer.cpp", "xbmc/guilib/GUIPanelContainer.cpp",
         "xbmc/guilib/GUIWrappingListContainer.cpp", "xbmc/guilib/GUIBaseContainer.h"]
BASELINE = '36d89001ee02dcee3e436422ac17d0246c94a4b7'


def method(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    brace = masked.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


COMMON = r'''
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>
static unsigned long checks=0, cases=0;
static void Check(bool condition,const char* message)
{++checks;if(!condition)throw std::runtime_error(message);}
enum ORIENTATION {HORIZONTAL,VERTICAL};
enum {RENDER_ORDER_BACK_TO_FRONT,RENDER_ORDER_FRONT_TO_BACK,ANIM_TYPE_UNFOCUS};
struct CPoint
{float x=0,y=0;CPoint()=default;CPoint(float a,float b):x(a),y(b){}
 CPoint operator+(CPoint p)const{return {x+p.x,y+p.y};}};
struct Draw
{
 int item,kind,parent,pass;float x,y;
 bool operator==(const Draw& b)const
 {return std::tie(item,kind,parent,pass,x,y)==std::tie(b.item,b.kind,b.parent,b.pass,b.x,b.y);}
};
struct Visit
{
 int item;bool focused;float x,y;
 bool operator==(const Visit& b)const
 {return std::tie(item,focused,x,y)==std::tie(b.item,b.focused,b.x,b.y);}
};
struct Graphics
{
 bool admit=true;int pass=0,clipPush=0,clipPop=0;
 CPoint origin;std::vector<CPoint> origins;
 std::vector<Draw> draws;std::vector<Visit> visits;std::vector<int> depths;
 std::vector<std::tuple<float,float,float,float>> clips;
 bool SetClipRegion(float x,float y,float w,float h)
 {clips.emplace_back(x,y,w,h);if(admit)++clipPush;return admit;}
 void RestoreClipRegion(){++clipPop;}
 int GetRenderOrder()const{return pass;}
 void SetOrigin(float x,float y){origins.push_back(origin);origin.x+=x;origin.y+=y;}
 void RestoreOrigin(){Check(!origins.empty(),"origin paired");origin=origins.back();origins.pop_back();}
 void Clear(){draws.clear();visits.clear();depths.clear();clips.clear();clipPush=clipPop=0;
  origin={};origins.clear();}
};
static Graphics gfx;
struct Window {Graphics& GetGfxContext(){return gfx;}};
struct CServiceBroker {static Window* GetWinSystem(){static Window window;return &window;}};
struct CGUIListItem;
static std::function<void()> depthCallback;
static int throwDepth=-1;
static unsigned layoutLookups=0,depthCalls=0;
struct CGUIListItemLayout
{
 int id=0,kind=0;float width=11.25f,height=9.5f;bool unfocus=false;
 virtual ~CGUIListItemLayout()=default;
 float Size(ORIENTATION orientation)const{return orientation==VERTICAL?height:width;}
 bool IsAnimating(int type)const{return type==ANIM_TYPE_UNFOCUS&&unfocus;}
 virtual void AssignDepth(){gfx.depths.push_back(-999);}
 void Render(CGUIListItem*,int parent);
};
struct OwnedLayout:CGUIListItemLayout
{
 OwnedLayout(int item,int which){id=item;kind=which;}
 void AssignDepth()override
 {
  ++depthCalls;
  if(depthCallback){auto callback=std::move(depthCallback);depthCallback={};callback();}
  if(id==throwDepth)throw std::runtime_error("injected layout depth exception");
  // Virtual nested-group, text and outgoing-fade stand-ins, with variable leaf counts.
  gfx.depths.push_back(id*100+kind*10);
  gfx.depths.push_back(id*100+kind*10+1);
  if((id+kind)%3==0)gfx.depths.push_back(id*100+kind*10+2);
 }
};
struct CGUIListItem
{
 int id;std::unique_ptr<CGUIListItemLayout> normal,focused;
 explicit CGUIListItem(int value):id(value),normal(std::make_unique<OwnedLayout>(value,1)),
  focused(std::make_unique<OwnedLayout>(value,2)){}
 CGUIListItemLayout* GetLayout()const{++layoutLookups;return normal.get();}
 CGUIListItemLayout* GetFocusedLayout()const{++layoutLookups;return focused.get();}
};
void CGUIListItemLayout::Render(CGUIListItem* item,int parent)
{Check(item->id==id,"layout belongs to selected item");
 gfx.draws.push_back({id,kind,parent,gfx.pass,gfx.origin.x,gfx.origin.y});}
struct CGUIControl {virtual ~CGUIControl()=default;virtual void Render(){};};
struct Scroller
{float value=0;int direction=0;float GetValue()const{return value;}
 bool IsScrollingDown()const{return direction>0;}bool IsScrollingUp()const{return direction<0;}};
'''

DECLARATIONS = r'''
struct CGUIBaseContainer:CGUIControl
{
 @RENDERITEM@
 float m_posX=2.125f,m_posY=-3.75f,m_width=39.5f,m_height=31.25f;
 CPoint m_renderOffset{.75f,-.5f};ORIENTATION m_orientation=VERTICAL;
 CGUIListItemLayout* m_layout=nullptr;CGUIListItemLayout* m_focusedLayout=nullptr;
 Scroller m_scroller;int m_offset=0,m_cursor=0,m_cacheItems=0,m_parentID=37;
 bool m_bHasFocus=true;
 std::vector<std::shared_ptr<CGUIListItem>> m_items;
 mutable std::vector<RENDERITEM> m_renderItems;
 std::vector<RENDERITEM> m_depthItems;
 int GetOffset()const{return m_offset;}int GetCursor()const{return m_cursor;}
 int GetCacheCount()const{return m_cacheItems;}
 virtual int CorrectOffset(int,int)const;
 void GetCacheOffsets(int&,int&)const;
 void Render()override;
 virtual void RenderItem(float,float,CGUIListItem*,bool);
 virtual void AssignDepth();
 @NEW_DECL@
};
struct CGUIPanelContainer:CGUIBaseContainer
{
 int m_itemsPerRow=3;std::optional<int> m_cachedScrollOffset;std::optional<std::pair<int,int>> m_cachedRows;
 int CorrectOffset(int,int)const override;
 void Render()override;
 @PANEL_DECL@
};
struct CGUIWrappingListContainer:CGUIBaseContainer
{int CorrectOffset(int,int)const override;};
template<class C>struct Observed:C
{
 void RenderItem(float x,float y,CGUIListItem* item,bool focus)override
 {gfx.visits.push_back({item->id,focus,x,y});C::RenderItem(x,y,item,focus);}
};
'''

TESTS = r'''
struct State
{
 int orientation=0,offset=0,cursor=0,count=19,cache=0,direction=0,rows=3,missing=0;
 bool focus=true,clip=true,cached=false;float scroll=0;
};
static std::vector<std::shared_ptr<CGUIListItem>> Items(int count,int missing)
{
 std::vector<std::shared_ptr<CGUIListItem>> result;
 for(int i=0;i<count;++i)
 {
  auto item=std::make_shared<CGUIListItem>(i+1);
  if(missing&&(i%5==0))item->normal.reset();
  if(missing&&(i%7==0))item->focused.reset();
  if(item->focused)item->focused->unfocus=(i%4==1);
  result.push_back(item);
 }
 return result;
}
static OwnedLayout normalTemplate(10001,1),focusedTemplate(10001,2);
template<class C>void Configure(C& c,const State& s)
{
 c.m_layout=&normalTemplate;c.m_focusedLayout=&focusedTemplate;
 c.m_orientation=static_cast<ORIENTATION>(s.orientation);c.m_offset=s.offset;c.m_cursor=s.cursor;
 c.m_cacheItems=s.cache;c.m_scroller.value=s.scroll;c.m_scroller.direction=s.direction;
 c.m_bHasFocus=s.focus;c.m_items=Items(s.count,s.missing);
}
template<class C>void PanelConfigure(C& c,const State& s)
{
 Configure(c,s);c.m_itemsPerRow=s.rows;
 if(s.cached)
 {
  c.m_cachedScrollOffset=s.offset;
  // rows at rest at the target, unlike the rows of the fixture's scroll value
  const ORIENTATION o=c.m_orientation;const float size=c.m_layout->Size(o);int first,last;
  now::GetPanelRows(s.offset*size,s.offset*size,size,o==VERTICAL?c.m_height:c.m_width,
                    o==VERTICAL?c.m_renderOffset.y:c.m_renderOffset.x,s.cache/2,first,last);
  c.m_cachedRows=std::make_pair(first,last);
 }
}
static std::vector<int> ExpectedDepths(const std::vector<Draw>& draws);
// Independent panel enumeration: rows from the cached or the production row helper, items
// row-major, the physically focused item last, positions with the production arithmetic.
template<class C>std::vector<Visit> PanelVisits(const C& c)
{
 std::vector<Visit> visits;
 if(!c.m_layout||!c.m_focusedLayout)return visits;
 const ORIENTATION o=c.m_orientation;const float size=c.m_layout->Size(o);int first,last;
 if(c.m_cachedRows)std::tie(first,last)=*c.m_cachedRows;
 else now::GetPanelRows(c.m_scroller.GetValue(),c.GetOffset()*size,size,o==VERTICAL?c.m_height:c.m_width,
                        o==VERTICAL?c.m_renderOffset.y:c.m_renderOffset.x,c.m_cacheItems/2,first,last);
 const CPoint origin=CPoint(c.m_posX,c.m_posY)+c.m_renderOffset;
 float pos=o==VERTICAL?origin.y:origin.x;pos+=first*size-c.m_scroller.GetValue();
 const int count=static_cast<int>(c.m_items.size());std::optional<Visit> focused;
 for(int row=first;row<=last&&row*c.m_itemsPerRow<count;++row,pos+=size)
  for(int col=0;col<c.m_itemsPerRow;++col)
  {
   const int current=row*c.m_itemsPerRow+col;if(current<0)continue;if(current>=count)break;
   const bool focus=current==c.GetOffset()*c.m_itemsPerRow+c.GetCursor()&&c.m_bHasFocus;
   const float across=(o==VERTICAL?origin.x:origin.y)+col*c.m_layout->Size(o==VERTICAL?HORIZONTAL:VERTICAL);
   const Visit visit=o==VERTICAL?Visit{current+1,focus,across,pos}:Visit{current+1,focus,pos,across};
   if(focus)focused=visit;else visits.push_back(visit);
  }
 if(focused)visits.push_back(*focused);
 return visits;
}
template<class B,class C>void ComparePanel(B& baseline,C& current,const State& s)
{
 ++cases;
 const auto expected=PanelVisits(current);
 for(int pass:{RENDER_ORDER_BACK_TO_FRONT,RENDER_ORDER_FRONT_TO_BACK})
 {
  gfx.admit=s.clip;gfx.pass=pass;gfx.Clear();baseline.Render();
  const auto clips=gfx.clips;const int pushed=gfx.clipPush,popped=gfx.clipPop;
  gfx.Clear();current.Render();
  auto visits=expected;if(pass==RENDER_ORDER_FRONT_TO_BACK)std::reverse(visits.begin(),visits.end());
  if(!s.clip)visits.clear();
  Check(gfx.visits==visits,"panel item/focus/order/coordinates match the row enumeration");
  Check(gfx.clips==clips&&gfx.clipPush==pushed&&gfx.clipPop==popped,"dirty clip behavior unchanged");
  Check(gfx.origins.empty(),"current origins balanced");
 }
 gfx.admit=true;gfx.pass=RENDER_ORDER_BACK_TO_FRONT;gfx.Clear();current.Render();
 const auto canonicalDraws=gfx.draws;
 gfx.Clear();gfx.admit=s.clip;current.AssignDepth();
 Check(gfx.depths==ExpectedDepths(canonicalDraws),"fresh depth is exactly selected drawn layouts, in physical focus-last order");
 Check(gfx.clips.empty(),"depth enumeration is independent of dirty clip admission");
 Check(current.m_depthItems.empty(),"depth capacity cache retains no owners");
 gfx.admit=true;
}
static std::vector<int> ExpectedDepths(const std::vector<Draw>& draws)
{
 std::vector<int> expected;
 for(const auto& draw:draws)
 {expected.push_back(draw.item*100+draw.kind*10);expected.push_back(draw.item*100+draw.kind*10+1);
  if((draw.item+draw.kind)%3==0)expected.push_back(draw.item*100+draw.kind*10+2);}
 return expected;
}
template<class B,class C>void Compare(B& baseline,C& current,const State& s)
{
 ++cases;
 gfx.admit=s.clip;
 std::vector<Draw> canonicalDraws;
 for(int pass:{RENDER_ORDER_BACK_TO_FRONT,RENDER_ORDER_FRONT_TO_BACK})
 {
  gfx.pass=pass;gfx.Clear();baseline.Render();
  const auto draws=gfx.draws;const auto visits=gfx.visits;const auto clips=gfx.clips;
  const int pushed=gfx.clipPush,popped=gfx.clipPop;
  Check(gfx.origins.empty(),"baseline origins balanced");
  gfx.Clear();current.Render();
  Check(gfx.draws==draws,"draw layout/id/order/coordinates match historical draw");
  Check(gfx.visits==visits,"physical item/focus/order/coordinates match historical draw");
  Check(gfx.clips==clips&&gfx.clipPush==pushed&&gfx.clipPop==popped,"dirty clip behavior unchanged");
  Check(gfx.origins.empty(),"current origins balanced");
  if(pass==RENDER_ORDER_BACK_TO_FRONT)canonicalDraws=draws;
 }
 // Depth enumeration is not filtered by a transient dirty-tile clip admission.
 if(!s.clip){gfx.admit=true;gfx.pass=RENDER_ORDER_BACK_TO_FRONT;gfx.Clear();baseline.Render();canonicalDraws=gfx.draws;}
 gfx.Clear();gfx.admit=s.clip;current.AssignDepth();
 Check(gfx.depths==ExpectedDepths(canonicalDraws),"fresh depth is exactly selected drawn layouts, in physical focus-last order");
 Check(gfx.clips.empty(),"depth enumeration is independent of dirty clip admission");
 Check(current.m_depthItems.empty(),"depth capacity cache retains no owners");
 gfx.admit=true;
}
static void Matrix()
{
 focusedTemplate.width=16.75f;focusedTemplate.height=13.25f;
 for(int kind=0;kind<3;++kind)
 for(int orientation=0;orientation<2;++orientation)
 for(float scroll:{-20.25f,-.5f,0.0f,7.125f,29.75f,80.25f})
 for(int cache:{0,1,4,9})
 for(int direction:{-1,0,1})
 for(bool focus:{false,true})
 for(int missing:{0,1})
 {
  State s;s.orientation=orientation;s.scroll=scroll;s.cache=cache;s.direction=direction;
  s.focus=focus;s.missing=missing;s.offset=2;s.cursor=1;s.cached=(cache%2==1);
  if(kind==0){old::Observed<old::CGUIBaseContainer> b;now::Observed<now::CGUIBaseContainer> c;
   Configure(b,s);Configure(c,s);Compare(b,c,s);}
  else if(kind==1){old::Observed<old::CGUIPanelContainer> b;now::Observed<now::CGUIPanelContainer> c;
   PanelConfigure(b,s);PanelConfigure(c,s);ComparePanel(b,c,s);}
  else {s.count=3;old::Observed<old::CGUIWrappingListContainer> b;now::Observed<now::CGUIWrappingListContainer> c;
   Configure(b,s);Configure(c,s);Compare(b,c,s);}
 }
}
static void EdgesAndFreshness()
{
 old::Observed<old::CGUIPanelContainer> b;now::Observed<now::CGUIPanelContainer> c;
 State s;s.offset=2;s.cursor=1;s.scroll=19;s.rows=3;s.cache=4;
 PanelConfigure(b,s);PanelConfigure(c,s);
 gfx.pass=RENDER_ORDER_BACK_TO_FRONT;gfx.admit=true;gfx.Clear();b.Render();
 Check(!gfx.visits.empty()&&gfx.visits.back().item==8&&gfx.visits.back().focused,
       "panel physical row focus counterexample is actually rendered last");
 const auto intended=ExpectedDepths(gfx.draws);
 gfx.Clear();b.AssignDepth();
 Check(gfx.depths!=intended,"historical both-layout/all-item depth counterexample reproduced");
 Check(gfx.depths.back()/100==4&&intended.back()/100==8,
       "historical panel depth defers logical item4 instead of physical focused item8");
 ComparePanel(b,c,s);
 // First-frame depth, then stale Render list, scroll, provider replacement/reload.
 for(int frame=0;frame<5;++frame)
 {
  s.scroll=frame==0?0.0f:frame*11.75f;s.offset=frame;s.cursor=frame%3;
  s.count=frame==3?0:23-frame;s.missing=frame%2;s.clip=frame%2==0;
  PanelConfigure(b,s);PanelConfigure(c,s);
  gfx.Clear();gfx.admit=true;c.AssignDepth();const auto depths=gfx.depths;
  gfx.Clear();gfx.pass=RENDER_ORDER_BACK_TO_FRONT;c.Render();
  Check(depths==ExpectedDepths(gfx.draws),"depth is fresh without first Render or after reload/list replace");
  ComparePanel(b,c,s);
 }
 // Missing container layouts must not resurrect previous render-list entries.
 for(int missing=0;missing<2;++missing)
 {
  b.m_layout=c.m_layout=missing?&normalTemplate:nullptr;
  b.m_focusedLayout=c.m_focusedLayout=missing?nullptr:&focusedTemplate;
  ComparePanel(b,c,s);Check(c.m_depthItems.empty(),"missing templates clear depth owners");
 }
 // Multiple instances, fractional origins and wrap duplicate visits are independent.
 now::Observed<now::CGUIWrappingListContainer> a,d;
 State wrap;wrap.count=2;wrap.cache=12;wrap.scroll=-.25f;
 Configure(a,wrap);Configure(d,wrap);d.m_posX=700.5f;d.m_posY=-51.125f;
 gfx.Clear();a.AssignDepth();const auto aDepth=gfx.depths;
 Check(aDepth.size()>wrap.count*2u,"wrapped duplicates are retained, not deduplicated");
 gfx.Clear();d.AssignDepth();Check(gfx.depths==aDepth,"container scratch is per-instance");
 gfx.Clear();depthCallback=[&d]{d.AssignDepth();};a.AssignDepth();
 std::vector<int> doubled=aDepth;doubled.insert(doubled.end(),aDepth.begin(),aDepth.end());
 Check(gfx.depths==doubled,"virtual depth may visit another independent container");
 gfx.Clear();depthCallback=[&a]{a.AssignDepth();};a.AssignDepth();
 Check(gfx.depths==doubled&&a.m_depthItems.empty(),"same-container reentrant depth has independent owner scope");
}
static void OwnershipAndWork()
{
 now::Observed<now::CGUIBaseContainer> c;State s;s.count=1500;s.scroll=0;s.cache=4;
 Configure(c,s);gfx.Clear();c.AssignDepth();
 const auto capacity=c.m_depthItems.capacity();
 const auto* storage=c.m_depthItems.data();
 Check(capacity>0&&c.m_depthItems.empty(),"successful depth keeps capacity only");
 gfx.Clear();c.AssignDepth();
 Check(c.m_depthItems.capacity()==capacity&&c.m_depthItems.data()==storage,"steady depth storage reused");
 std::vector<std::weak_ptr<CGUIListItem>> weak(c.m_items.begin(),c.m_items.end());
 c.m_items.clear();for(auto& item:weak)Check(item.expired(),"depth scratch does not retain removed provider items");
 Configure(c,s);weak.assign(c.m_items.begin(),c.m_items.end());throwDepth=1;
 bool caught=false;try{c.AssignDepth();}catch(const std::runtime_error& error)
 {caught=std::string(error.what())=="injected layout depth exception";}
 throwDepth=-1;Check(caught,"injected exception traverses real depth method");
 Check(c.m_depthItems.empty(),"exception leaves no member scratch owners");
 c.m_items.clear();for(auto& item:weak)Check(item.expired(),"local scratch releases owners on exception");
 struct ThrowBuild:now::CGUIBaseContainer
 {
  void BuildRenderItems(std::vector<RENDERITEM>& items)const override
  {now::CGUIBaseContainer::BuildRenderItems(items);throw std::runtime_error("injected enumeration exception");}
 } throwing;
 Configure(throwing,s);weak.assign(throwing.m_items.begin(),throwing.m_items.end());caught=false;
 try{throwing.AssignDepth();}catch(const std::runtime_error& error)
 {caught=std::string(error.what())=="injected enumeration exception";}
 Check(caught&&throwing.m_depthItems.empty(),"enumeration throw leaves no member scratch owners");
 throwing.m_items.clear();for(auto& item:weak)Check(item.expired(),"enumeration throw releases local owners");
 Configure(c,s);weak.assign(c.m_items.begin(),c.m_items.end());
 depthCallback=[&c]{c.m_items.clear();};gfx.Clear();c.AssignDepth();
 Check(!gfx.depths.empty(),"local enumeration survives reentrant provider release");
 for(auto& item:weak)Check(item.expired(),"provider-release callback leaves no scratch owners");
 old::Observed<old::CGUIBaseContainer> b;Configure(b,s);Configure(c,s);
 depthCalls=0;layoutLookups=0;gfx.Clear();b.AssignDepth();const auto oldCalls=depthCalls;
 const auto oldLookups=layoutLookups;
 depthCalls=0;layoutLookups=0;gfx.Clear();c.AssignDepth();const auto newCalls=depthCalls;
 Check(oldCalls==3000&&newCalls>0&&newCalls<20,"far-cache witness removes full-list/both-layout virtual calls");
 Check(layoutLookups<oldLookups,"far-cache witness reduces lookup work, not merely leaf work");
 std::cout<<"WORK oldDepth="<<oldCalls<<" currentDepth="<<newCalls
          <<" retainedCapacity="<<capacity<<" (fixture operations, not timings)\n";
}
int main()
{
 try{Matrix();EdgesAndFreshness();OwnershipAndWork();
  std::cout<<"PASS "<<cases<<" scenarios "<<checks<<" checks\n";return 0;}
 catch(const std::exception& error){std::cerr<<"FAIL "<<error.what()<<"\n";return 1;}
}
'''


def sources(root, baseline):
    return ({path: subprocess.check_output(['git', 'show', f'{baseline}:{path}'], cwd=root, text=True)
             for path in FILES}, {path: (root / path).read_text() for path in FILES})


def variant(src, current):
    base, panel, wrap, header = [src[path] for path in FILES]
    renderitem = method(header, 'struct RENDERITEM') + ';'
    declarations = DECLARATIONS.replace('@RENDERITEM@', renderitem)
    declarations = declarations.replace('@NEW_DECL@',
        'virtual void BuildRenderItems(std::vector<RENDERITEM>&) const;\n'
        'CGUIListItemLayout* GetRenderLayout(CGUIListItem*,bool) const;' if current else '')
    declarations = declarations.replace('@PANEL_DECL@',
        'void BuildRenderItems(std::vector<RENDERITEM>&) const override;' if current else '')
    extracted = [method(base, signature) for signature in [
        'void CGUIBaseContainer::Render()', 'void CGUIBaseContainer::RenderItem(',
        'void CGUIBaseContainer::AssignDepth()', 'int CGUIBaseContainer::CorrectOffset(',
        'void CGUIBaseContainer::GetCacheOffsets(']]
    if current:
        extracted.append(method(panel, 'void GetPanelRows('))
    extracted += [method(panel, 'void CGUIPanelContainer::Render()'),
                  method(panel, 'int CGUIPanelContainer::CorrectOffset('),
                  method(wrap, 'int CGUIWrappingListContainer::CorrectOffset(')]
    if current:
        extracted += [method(base, 'void CGUIBaseContainer::BuildRenderItems('),
                      method(base, 'CGUIListItemLayout* CGUIBaseContainer::GetRenderLayout('),
                      method(panel, 'void CGUIPanelContainer::BuildRenderItems(')]
    return declarations + '\n' + '\n\n'.join(extracted)


def mutations(current):
    """Mutate extracted candidate methods only; require explicit semantic failure."""
    changes = {
        'panel-physical-focus': ('GetOffset() * m_itemsPerRow + GetCursor()', 'GetOffset() + GetCursor()'),
        'unfocus-selection': ('focusedLayout && focusedLayout->IsAnimating(ANIM_TYPE_UNFOCUS)', 'false'),
        'inactive-layout-depth': ('GetRenderLayout(renderItem.item.get(), renderItem.focused)',
                                  'renderItem.item->GetLayout()'),
        'nonvirtual-depth': ('layout->AssignDepth();', 'layout->CGUIListItemLayout::AssignDepth();'),
        'stale-depth-list': ('BuildRenderItems(renderItems);\n  for (const auto& renderItem : renderItems)',
                             'renderItems = m_renderItems;\n  for (const auto& renderItem : renderItems)'),
        'retain-depth-owners': ('renderItems.clear();\n  m_depthItems.swap(renderItems);',
                                'm_depthItems.swap(renderItems);'),
        'wrong-base-coordinate': ('RENDERITEM{origin.x, focusedPos, focusedItem, true}',
                                   'RENDERITEM{origin.x + 1, focusedPos, focusedItem, true}'),
        'reverse-depth-order': ('for (const auto& renderItem : renderItems)',
                                'std::reverse(renderItems.begin(), renderItems.end());\n  for (const auto& renderItem : renderItems)'),
        'drop-focused-item': ('if (focusedItem)', 'if (false && focusedItem)'),
        'ignore-panel-cached-rows': ('if (m_cachedRows)', 'if (false && m_cachedRows)'),
        'panel-preload-ignored': ('GetCacheCount() / 2, firstRow, lastRow);', '0, firstRow, lastRow);'),
        'omit-preload-tail': ('end += cacheAfter * m_layout->Size(m_orientation);', 'end += 0 * cacheAfter * m_layout->Size(m_orientation);'),
        'reallocate-depth-cache': ('renderItems.swap(m_depthItems);', 'renderItems.reserve(m_depthItems.capacity());'),
    }
    for name, (old, new) in changes.items():
        if old not in current:
            raise RuntimeError(f'mutation anchor absent: {name}')
        yield name, current.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--baseline', default=BASELINE,
                        help='historical draw implementation (pinned before depth refactor)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.out or Path(tempfile.mkdtemp(prefix='kodi-container-depth-'))
    out.mkdir(parents=True, exist_ok=True)
    baseline = subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=root, text=True).strip()
    checkout_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    old, now = sources(root, baseline)
    baseline_code, current_code = variant(old, False), variant(now, True)
    prefix = COMMON + '\nnamespace old {\n' + baseline_code + '\n}\nnamespace now {\n'
    suffix = '\n}\n' + TESTS
    env = os.environ.copy()
    if args.sanitize:
        env.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
        env.setdefault('UBSAN_OPTIONS', 'halt_on_error=1:print_stacktrace=1')
    records = []
    for name, code in [('current', current_code), *mutations(current_code)]:
        source, binary = out / f'{name}.cpp', out / name
        source.write_text(prefix + code + suffix)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                   '-Wno-sign-compare', '-fno-omit-frame-pointer']
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-pie', '-no-pie']
        command += [str(source), '-o', str(binary)]
        built = subprocess.run(command, capture_output=True, text=True)
        (out / f'{name}-compile.txt').write_text(built.stdout + built.stderr)
        record = {'name': name, 'compile_argv': command, 'compile_exit': built.returncode,
                  'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        if built.returncode:
            records.append(record)
            raise RuntimeError(f'{name} compile failed; see {out}')
        run = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
        (out / f'{name}-stdout.txt').write_text(run.stdout)
        (out / f'{name}-stderr.txt').write_text(run.stderr)
        record.update(run_argv=[str(binary)], exit=run.returncode,
                      accepted=run.returncode == 0 if name == 'current' else
                      run.returncode == 1 and run.stderr.startswith('FAIL '))
        records.append(record)
        if not record['accepted']:
            raise RuntimeError(f'{name} did not produce required semantic result; see {out}')
    receipt = {'baseline': baseline, 'checkout_head': checkout_head,
               'limits': __doc__, 'sanitize': args.sanitize,
               'sanitizer_env': {key: env.get(key) for key in ('ASAN_OPTIONS', 'UBSAN_OPTIONS')},
               'source_hashes': {path: hashlib.sha256(now[path].encode()).hexdigest() for path in FILES},
               'baseline_source_hashes': {path: hashlib.sha256(old[path].encode()).hexdigest() for path in FILES},
               'results': records}
    (out / 'results.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print((out / 'current-stdout.txt').read_text(), end='')
    print(f'PASS {len(records)-1} semantic mutations; receipts {out}')


if __name__ == '__main__':
    main()
