#!/usr/bin/env python3
"""Host replay of actual GUI texture admission, polling, culling and fade methods.

Compiles production texture allocation/process/render/geometry methods, image
Process/Render/fade methods, control DoRender and real rectangle clip functions
against owned manager/render boundaries. No manager/job/GPU implementation is
substituted as evidence of backend correctness: those boundaries only script
publication and record demand/draw/defer-classification calls. This proves frontend CPU behavior,
not pixels, upload-context correctness, runtime performance or device acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

FILES = ('xbmc/guilib/GUITexture.cpp', 'xbmc/guilib/GUITexture.h',
         'xbmc/guilib/GUIImage.cpp', 'xbmc/guilib/GUIControl.cpp',
         'xbmc/windowing/GraphicContext.cpp', 'xbmc/utils/Geometry.h', 'xbmc/guilib/GUIImage.h')


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda m: ' ' * len(m.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include "utils/Geometry.h"
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <optional>
#include <stack>
#include <stdexcept>
#include <string>
#include <vector>
namespace UTILS { namespace COLOR { using Color=unsigned; } }
namespace MathUtils { static int round_int(double x){return static_cast<int>(std::round(x));} }
namespace StringUtils { static bool EndsWithNoCase(const std::string&s,const char*p){return s.size()>=4&&s.substr(s.size()-4)==p;} }
using CDirtyRegionList=std::vector<int>;
struct CGUIListItem{};
struct InfoColor {bool valid=false;unsigned color=0xffffffff;explicit operator bool()const{return valid;}
 explicit operator unsigned()const{return color;}bool Update(const CGUIListItem*){return false;}bool HasInfo()const{return valid;}};
@ASPECT@
struct CTextureInfo {bool useLarge=true;CRect border;bool m_infill=true;int orientation=0;InfoColor diffuseColor;
 std::string filename="large",diffuse;};
struct CTexture {bool alpha=false;bool HasAlpha()const{return alpha;}};
struct CTextureArray {std::vector<std::shared_ptr<CTexture>>m_textures;std::vector<unsigned>m_delays;
 unsigned m_width=100,m_height=50,m_texWidth=128,m_texHeight=64;int m_orientation=0,m_loops=0;
 bool m_texCoordsArePixels=false;unsigned size()const{return m_textures.size();}
 void Reset(){m_textures.clear();m_delays.clear();}};
static CTextureArray Published(bool alpha=false){CTextureArray a;auto t=std::make_shared<CTexture>();t->alpha=alpha;a.m_textures.push_back(t);a.m_delays.push_back(100);return a;}
struct LargeManager {struct Entry{CTextureArray data;bool failed=false;unsigned refs=0;};std::map<std::string,Entry>entries;
 struct Request{std::string path;bool first,defer;};std::vector<Request>requests;
 std::vector<std::string>demands;std::vector<bool>backgroundDemands;unsigned gets=0;bool motion=false;
 bool IsBackgroundMotion()const{return motion;}
 bool GetImage(const std::string&p,CTextureArray&t,bool first,bool,bool defer=false){requests.push_back({p,first,defer});++gets;auto&e=entries[p];if(first)++e.refs;t=e.data;return !e.failed;}
 void RequestImageUpload(const std::string&p,bool background=false){demands.push_back(p);backgroundDemands.push_back(background);}
 void ReleaseImage(const std::string&p,bool){auto&e=entries[p];if(e.refs)--e.refs;}
 void Publish(const std::string&p,bool alpha=false){entries[p].data=Published(alpha);}
 unsigned Demand(const std::string&p)const{return static_cast<unsigned>(std::count(demands.begin(),demands.end(),p));}};
struct NormalManager {bool CanLoad(const std::string&p){return p=="normal";}CTextureArray Load(const std::string&p,bool bundle=false){return !bundle&&p=="normal"?Published():CTextureArray{};}
 void ReleaseTexture(const std::string&,bool){}};
enum RENDER_ORDER {RENDER_ORDER_ALL_BACK_TO_FRONT,RENDER_ORDER_FRONT_TO_BACK,RENDER_ORDER_BACK_TO_FRONT};
constexpr int RENDER_STEREO_MODE_MONO=0,RENDER_STEREO_MODE_OFF=1;
struct TransformMatrix {void InverseTransformPosition(float&,float&)const{}};
struct RESOLUTION_INFO {int iWidth=0,iHeight=0;};
struct CGraphicContext {std::stack<CRect>m_clipRegions;std::stack<CPoint>m_origins;CRect scissors{0,0,100,100};
 RENDER_ORDER order=RENDER_ORDER_ALL_BACK_TO_FRONT;unsigned mergedAlpha=255;unsigned captured=0;
 float scaleX=1,scaleY=1,offsetX=0,offsetY=0;int screenWidth=100,screenHeight=100;
 bool SetClipRegion(float,float,float,float);void RestoreClipRegion();void ClipRect(CRect&,CRect&,CRect*=nullptr);
 unsigned MergeColor(unsigned c){return (((mergedAlpha*(c>>24))/255)<<24)|(c&0xffffff);}
 unsigned MergeAlpha(unsigned c){return MergeColor(c);}float GetScalingPixelRatio(){return 1;}float GetFPS(){return 60;}
 float GetTransformDepth(int=0){return .5f;}float GetNormalizedDepth(int d){return d*.01f;}RENDER_ORDER GetRenderOrder(){return order;}
 float ScaleFinalXCoord(float x,float)const{return x*scaleX+offsetX;}float ScaleFinalYCoord(float,float y)const{return y*scaleY+offsetY;}float ScaleFinalZCoord(float,float)const{return .5f;}
 void CaptureControlQuad(float*,float*,float*){++captured;}const CRect&GetScissors(){return scissors;}
 int GetStereoMode(){return RENDER_STEREO_MODE_OFF;}void SetTransform(const TransformMatrix&){}void RemoveTransform(){}
 void SetCameraPosition(const CPoint&){}void RestoreCameraPosition(){}void SetStereoFactor(float){}void RestoreStereoFactor(){}
 CRect GenerateAABB(const CRect&r){return r;}int GetWidth()const{return screenWidth;}int GetHeight()const{return screenHeight;}
 void BeginControlBounds(CRect*){}void EndControlBounds(){}unsigned GetControlBoundsFrame(){return 1;}
 void GetGUIScaling(const RESOLUTION_INFO&,float&x,float&y,TransformMatrix*){x=y=1;}};
struct Window {CGraphicContext gfx;CGraphicContext&GetGfxContext(){return gfx;}};
struct GUI {LargeManager large;NormalManager normal;LargeManager&GetLargeTextureManager(){return large;}NormalManager&GetTextureManager(){return normal;}};
static Window window;static GUI gui;
struct CServiceBroker {static Window*GetWinSystem(){return &window;}static GUI*GetGUI(){return &gui;}};
class CGUITexture {public:
 CGUITexture(float x=10,float y=10,float w=40,float h=40){m_posX=x;m_posY=y;m_width=w;m_height=h;m_vertex=CRect(x,y,x+w,y+h);}
 CGUITexture(const CGUITexture&);virtual~CGUITexture()=default;CGUITexture*Clone()const{return new CGUITexture(*this);}
 bool AllocateOnDemand();bool IsBackgroundRequest()const;bool Process(unsigned);bool AllocResources();bool CalculateSize();void FreeResources(bool immediately=false);
 bool ReadyToRender()const;bool SetVisible(bool);bool SetAlpha(unsigned char);bool SetDiffuseColor(unsigned,const CGUIListItem*item=nullptr);
 bool SetPosition(float,float);bool SetWidth(float);bool SetHeight(float);bool SetFileName(const std::string&);void SetUseCache(bool);
 void ResetAnimState();bool UpdateAnimFrame(unsigned);int GetOrientation()const;static void OrientateTexture(CRect&,float,float,int);
 void Render(int32_t depthOffset=0,int32_t overrideDepth=-1);void Render(float,float,float,float,float,float,float,float,float,float);
 void SetInvalid(){m_invalid=true;}bool IsAllocated()const{return m_isAllocated!=NO;}bool FailedToAlloc()const{return m_isAllocated==NORMAL_FAILED||m_isAllocated==LARGE_FAILED;}
 const std::string&GetFileName()const{return m_info.filename;}const InfoColor&GetDiffuseColor()const{return m_info.diffuseColor;}
 float GetXPosition()const{return m_posX;}float GetYPosition()const{return m_posY;}float GetWidth()const{return m_width;}float GetHeight()const{return m_height;}
 const CRect&GetRenderRect()const{return m_vertex;}
 static void DrawQuad(const CRect&,unsigned){}void Allocate(){++allocations;}void Free(){}
 void Begin(unsigned){++begins;}void End(){++ends;}void Draw(float*,float*,float*,const CRect&,const CRect&,int){++draws;}
 bool m_visible=true;unsigned m_diffuseColor=0xffffffff;float m_posX=10,m_posY=10,m_width=40,m_height=40,m_depth=0;
 CRect m_vertex{10,10,50,50};bool m_invalid=true,m_use_cache=true;std::optional<bool>m_lastReadyState;unsigned char m_alpha=255;
 float m_frameWidth=0,m_frameHeight=0,m_texCoordsScaleU=1,m_texCoordsScaleV=1;int m_currentLoop=0;unsigned m_currentFrame=0,m_lasttime=0;
 float m_diffuseU=1,m_diffuseV=1,m_diffuseScaleU=1,m_diffuseScaleV=1;CPoint m_diffuseOffset;bool m_allocateDynamically=false;
 enum ALLOCATE_TYPE {NO=0,NORMAL,LARGE,NORMAL_FAILED,LARGE_FAILED};ALLOCATE_TYPE m_isAllocated=NO;bool m_continuation=false;
 CTextureInfo m_info;CAspectRatio m_aspect;CTextureArray m_diffuse,m_texture;unsigned begins=0,ends=0,draws=0,allocations=0;};
#define GUIPROFILER_RENDER_BEGIN(x) ((void)0)
#define GUIPROFILER_RENDER_END(x) ((void)0)
class CGUIControl {public:virtual~CGUIControl()=default;virtual void Render(){}virtual CRect CalcRenderRegion()const{return CRect(x,y,x+w,y+h);}
 virtual void Process(unsigned,CDirtyRegionList&){m_renderRegion=CalcRenderRegion();}
 void DoRender();bool IsVisible()const{return visible;}bool IsControlRenderable(){return true;}
 void MarkDirtyRegion(){++dirty;}bool visible=true,m_isCulled=false,m_hasCamera=false;float m_stereo=0;CPoint m_camera;
 TransformMatrix m_cachedTransform;unsigned m_hitColor=0xffffffff;CRect m_hitRect,m_renderRegion{10,10,50,50};
 struct Bounds{bool valid=false;unsigned frame=0;CRect bounds;TransformMatrix reference;int screenWidth=0,screenHeight=0;}m_renderedBounds;
 float x=10,y=10,w=40,h=40;unsigned dirty=0;};
struct ImageInfo {std::string fallback;const std::string&GetFallback()const{return fallback;}};
class CGUIImage:public CGUIControl {public:
 @FADING@
 CGUIImage(){m_texture=std::make_unique<CGUITexture>();}~CGUIImage(){for(auto*p:m_fadingTextures)delete p;}
 void Process(unsigned,CDirtyRegionList&)override;void Render()override;CRect CalcRenderRegion()const override;
 bool ProcessFading(CFadingTexture*,unsigned,unsigned,bool&);unsigned char GetFadeLevel(unsigned)const;
 void UpdateDiffuseColor(const CGUIListItem*){}std::unique_ptr<CGUITexture>m_texture;std::vector<CFadingTexture*>m_fadingTextures;
 ImageInfo m_info;std::string m_currentFallback;unsigned m_crossFadeTime=100,m_currentFadeTime=0,m_lastRenderTime=0;
 unsigned m_diffuseColor=0xffffffff,m_lastDiffuseColor=0xffffffff;};
static unsigned checks=0;static void Check(bool ok,const char*message){++checks;if(!ok)throw std::runtime_error(message);}
static void Reset(){window=Window{};gui=GUI{};}
'''

TESTS = r'''
static void Pending(CGUITexture&t){Check(t.Process(100),"first allocation changes state");Check(!t.ReadyToRender(),"pending remains unready");}
static void Admission(){
 Reset();CGUITexture prefetched;Pending(prefetched);for(unsigned t=116;t<300;t+=16)prefetched.Process(t);
 Check(gui.large.demands.empty(),"processed prefetched item does not admit");
 window.gfx.m_clipRegions.push(CRect(100,100,200,200));prefetched.Render();
 Check(gui.large.demands.empty(),"fully clipped pending does not admit");Check(!prefetched.begins&&!prefetched.draws,"pending never reaches renderer");
 window.gfx.m_clipRegions.pop();window.gfx.m_clipRegions.push(CRect(0,0,30,30));prefetched.SetAlpha(0);prefetched.Render();
 Check(gui.large.Demand("large")==1,"zero-alpha future fade target admits");
 Check(!prefetched.begins,"admission never synchronously begins upload");prefetched.SetVisible(false);prefetched.Render();
 Check(gui.large.Demand("large")==1,"hidden texture does not admit");
 Reset();CGUITexture moved(150,150,40,40);Pending(moved);window.gfx.m_clipRegions.push(CRect(0,0,100,100));moved.Render();Check(gui.large.demands.empty(),"initial offscreen pending does not admit");
 moved.SetPosition(20,25);Check(moved.Process(116),"moving pending dirties");Check(moved.m_vertex==CRect(20,25,60,65),"pending moved bounds follow declared geometry");moved.Render();
 Check(gui.large.Demand("large")==1,"moving into clip admits");moved.SetPosition(200,200);moved.Process(132);moved.Render();Check(gui.large.Demand("large")==1,"moving outside clip stops admission calls");
}
static void OuterCullAndAutosize(){
 Reset();CGUIImage image;CDirtyRegionList dirty;image.Process(100,dirty);image.m_isCulled=true;image.DoRender();
 Check(gui.large.demands.empty(),"outer culled control never admits");image.m_isCulled=false;image.visible=false;image.DoRender();Check(gui.large.demands.empty(),"outer hidden control never admits");
 image.visible=true;image.m_texture->SetPosition(150,150);image.x=image.y=150;image.Process(116,dirty);image.DoRender();
 Check(gui.large.demands.empty(),"outer scissor rejects offscreen declared bounds");
 for(int axis=0;axis<3;++axis){Reset();CGUIImage autoImage;autoImage.m_texture=std::make_unique<CGUITexture>(10,10,axis==1?40:0,axis==0?40:0);autoImage.w=axis==1?40:0;autoImage.h=axis==0?40:0;
  autoImage.Process(100,dirty);Check(gui.large.Demand("large")>=1,"unknown autosize admits before outer cull");Check(!autoImage.m_texture->begins,"autosize pending never renders synchronously");
  autoImage.DoRender();gui.large.Publish("large");const unsigned before=autoImage.dirty;autoImage.Process(116,dirty);
  Check(autoImage.m_texture->ReadyToRender()&&autoImage.dirty>before,"autosize completion wakes idle image");}
}
static void PublicationAndFades(){
 {Reset();CGUIImage idle;idle.m_crossFadeTime=0;CDirtyRegionList d;idle.Process(100,d);idle.Process(116,d);const unsigned before=idle.dirty;gui.large.Publish("large");idle.Process(132,d);Check(idle.dirty>before,"idle publication dirties without input");}
 Reset();CGUIImage image;CDirtyRegionList dirty;image.Process(100,dirty);image.DoRender();const unsigned pendingDirty=image.dirty;
 image.Process(116,dirty);image.Process(132,dirty);Check(image.dirty==pendingDirty,"idle pending does not dirty forever");
 CGUITexture oldSource;oldSource.m_info.filename="old";gui.large.Publish("old");oldSource.AllocResources();auto*old=new CGUIImage::CFadingTexture(&oldSource,100);oldSource.FreeResources();image.m_fadingTextures.push_back(old);
 image.Process(148,dirty);Check(image.m_currentFadeTime==0&&old->m_fadeTime==100,"old image retained while incoming upload pending");
 Check(image.m_texture->m_alpha==0,"incoming pending fade starts alpha zero");image.DoRender();Check(gui.large.Demand("large")>=1,"pending fade admits despite zero alpha");
 gui.large.Publish("large");const unsigned before=image.dirty;image.Process(164,dirty);
 Check(image.m_texture->ReadyToRender()&&image.dirty>before,"idle publication dirties without input");
 image.Process(180,dirty);Check(image.m_currentFadeTime>0&&image.m_texture->m_alpha>0,"published incoming resumes original fade");Check(old->m_fadeTime<100,"old fade resumes only after publication");
 image.DoRender();Check(image.m_texture->begins&&image.m_texture->draws,"published image reaches normal renderer");
 const unsigned requests=gui.large.Demand("large");image.DoRender();Check(gui.large.Demand("large")==requests,"ready image does not request upload again");
 Reset();CGUITexture normal;normal.m_info.filename="normal";normal.m_info.useLarge=false;Check(normal.Process(100)&&normal.ReadyToRender(),"normal synchronous texture remains ready");normal.Render();Check(normal.draws&&gui.large.demands.empty(),"normal path preserves draws without GPU demand API");
}
static void Copies(){
 Reset();CGUITexture source;Pending(source);auto copy=std::unique_ptr<CGUITexture>(source.Clone());
 Check(!copy->IsAllocated(),"copy starts with independent allocation state");copy->AllocResources();Check(gui.large.entries["large"].refs==2,"copy acquires independent manager reference");
 copy->FreeResources();Check(gui.large.entries["large"].refs==1,"copy release preserves another consumer");source.Render();Check(gui.large.Demand("large")==1,"remaining consumer still requests GPU admission");
}
static void DemandClassification(){
 Reset();CGUITexture background(0,0,100,100);Pending(background);background.Render();CGUITexture poster(0,0,30,50);poster.m_info.filename="poster";poster.AllocResources();poster.Render();
 Check(gui.large.demands.size()==2&&gui.large.backgroundDemands[0]&&!gui.large.backgroundDemands[1],"draw demand reports its consumer type");
 Check(gui.large.requests.back().path=="poster"&&!gui.large.requests.back().defer,"poster poll is not a background request");
}
static void BackgroundCopies(){ Reset();CGUITexture shown(0,0,100,100);shown.m_info.filename="shown";gui.large.Publish("shown");shown.AllocResources();auto fade=std::unique_ptr<CGUITexture>(shown.Clone());fade->SetFileName("next");gui.large.requests.clear();fade->AllocResources();
 Check(gui.large.requests.size()==1&&gui.large.requests[0].path=="next"&&gui.large.requests[0].defer,"copy loading a new file is a new background request");
 Reset();CGUITexture fresh(0,0,100,100);fresh.m_info.filename="fresh";auto unallocated=std::unique_ptr<CGUITexture>(fresh.Clone());unallocated->AllocResources();
 Check(gui.large.requests.size()==1&&gui.large.requests[0].defer,"copy of an unallocated background texture is a new background request");
}
static void Replacement(){
 Reset();CGUIImage image;CDirtyRegionList dirty;gui.large.Publish("large");image.m_texture->m_aspect.ratio=CAspectRatio::AR_KEEP;image.m_texture->m_width=40;image.m_texture->m_height=80;image.w=40;image.h=80;
 image.Process(100,dirty);Check(image.m_texture->ReadyToRender(),"old fitted texture ready");const CRect oldVertex=image.m_texture->m_vertex;
 Check(oldVertex!=CRect(10,10,50,90),"fixture has actual old aspect-fitted bounds");
 // The old fitted middle lies outside this narrow top strip, but declared bounds intersect it.
 window.gfx.scissors=CRect(0,10,100,15);image.m_texture->SetFileName("replacement");
 Check(!image.m_texture->m_invalid,"filename replacement begins with previously valid geometry");image.Process(116,dirty);
 Check(image.m_texture->m_vertex==CRect(10,10,50,90),"replacement pending refreshes outer cull bounds");image.DoRender();
 Check(gui.large.Demand("replacement")==1,"replacement declared geometry reaches demand despite old aspect fit");
 // Render must also use declared geometry if called before the pending Process recalculates it.
 image.m_texture->m_vertex=oldVertex;window.gfx.m_clipRegions.push(CRect(0,10,100,15));image.m_texture->Render();
 Check(gui.large.Demand("replacement")==2,"pending render uses declared bounds independently of stale fitted vertex");
}
static void BackgroundClassification(){
 Reset();Check(IsBackgroundSized(0,0,100,50),"half-screen boundary is background");
 Check(!IsBackgroundSized(0,0,99.99f,50),"below half-screen remains foreground");Check(IsBackgroundSized(0,0,100.01f,50),"above half-screen is background");
 window.gfx.scaleX=window.gfx.scaleY=2;window.gfx.screenWidth=window.gfx.screenHeight=200;window.gfx.offsetX=1000;window.gfx.offsetY=-500;
 Check(IsBackgroundSized(20,30,100,50),"background classification uses final screen scaling");Check(!IsBackgroundSized(20,30,40,60),"scaled poster remains foreground");
 Check(!IsBackgroundSized(0,0,0,200)&&!IsBackgroundSized(0,0,200,0),"unknown autosize is not a declared background");
 Reset();CGUITexture background(0,0,100,50);Pending(background);
 Check(gui.large.requests.size()==1&&gui.large.requests[0].first&&gui.large.requests[0].defer,"first large background requests scroll deferral");
 background.Process(116);Check(gui.large.requests.size()==2&&!gui.large.requests[1].first,"background polling does not acquire another reference");
 // Record classification at this boundary; manager tests own promotion policy.
 Reset();CGUITexture poster(0,0,30,50);Pending(poster);Check(gui.large.requests.size()==1&&gui.large.requests[0].first&&!gui.large.requests[0].defer,"first poster remains foreground");
 Reset();CGUITexture autosize(0,0,0,50);Pending(autosize);Check(!gui.large.requests[0].defer&&gui.large.Demand("large")==1,"unknown autosize keeps foreground fallback progress");
}
static void DeferredIncomingFade(){
 Reset();CGUIImage image;image.m_texture=std::make_unique<CGUITexture>(0,0,100,100);image.x=image.y=0;image.w=image.h=100;image.m_lastRenderTime=100;
 CGUITexture oldSource(0,0,100,100);oldSource.m_info.filename="old";gui.large.Publish("old");oldSource.AllocResources();
 image.m_fadingTextures.push_back(new CGUIImage::CFadingTexture(&oldSource,100));oldSource.FreeResources();CDirtyRegionList dirty;
 image.Process(116,dirty);image.DoRender();const auto incoming=std::find_if(gui.large.requests.begin(),gui.large.requests.end(),[](const LargeManager::Request&r){return r.path=="large"&&r.first;});Check(incoming!=gui.large.requests.end()&&incoming->defer,"incoming background uses deferred classification");
 const auto copies=std::count_if(gui.large.requests.begin(),gui.large.requests.end(),[](const LargeManager::Request&r){return r.path=="old"&&r.first;});
 const auto deferredCopies=std::count_if(gui.large.requests.begin(),gui.large.requests.end(),[](const LargeManager::Request&r){return r.path=="old"&&r.first&&r.defer;});
 Check(copies==2&&deferredCopies==1,"outgoing fade copy is not a new background request");
 for(unsigned time=132;time<=2116;time+=16)image.Process(time,dirty);
 Check(image.m_currentFadeTime==0&&image.m_texture->m_alpha==0,"long deferred decode preserves zero-alpha incoming fade");
 Check(image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_fadeTime==100&&image.m_fadingTextures[0]->m_texture->ReadyToRender(),"long deferred decode retains ready outgoing image");
 gui.large.Publish("large");const unsigned before=image.dirty;image.Process(2132,dirty);
 Check(image.m_texture->ReadyToRender()&&image.dirty>before,"long deferred completion wakes idle fade");
 Check(image.m_currentFadeTime>0&&image.m_currentFadeTime<image.m_crossFadeTime,"deferred fade resumes at ordinary frame step");
 Check(image.m_fadingTextures[0]->m_fadeTime<100,"deferred old fade starts only after publication");
}
static CGUIImage*FadingImage(bool background,bool published){auto*image=new CGUIImage;if(background){image->m_texture=std::make_unique<CGUITexture>(0,0,100,100);image->x=image->y=0;image->w=image->h=100;}
 image->m_lastRenderTime=100;if(published)gui.large.Publish("large");
 for(const char*path:{"a","b"}){CGUITexture source(image->x,image->y,image->w,image->h);source.m_info.filename=path;gui.large.Publish(path);source.AllocResources();
  image->m_fadingTextures.push_back(new CGUIImage::CFadingTexture(&source,path[0]=='a'?40:50));source.FreeResources();}
 return image;}
static void MotionEndsFade(){CDirtyRegionList dirty;
 {Reset();std::unique_ptr<CGUIImage>image(FadingImage(true,true));image->Process(116,dirty);
  Check(image->m_fadingTextures.size()==2&&image->m_currentFadeTime==16,"background fade runs while the GUI is still");
  gui.large.motion=true;const unsigned before=image->dirty;image->Process(132,dirty);
  Check(image->m_fadingTextures.empty()&&image->m_currentFadeTime==100&&image->m_texture->m_alpha==image->GetFadeLevel(100)&&image->dirty>before,"motion ends a background crossfade");
  Check(!gui.large.entries["a"].refs&&!gui.large.entries["b"].refs,"ended fade releases its outgoing copies");
  image->Process(148,dirty);Check(image->m_currentFadeTime==100&&image->m_fadingTextures.empty(),"ended fade stays ended");}
 {Reset();std::unique_ptr<CGUIImage>image(FadingImage(true,false));gui.large.motion=true;image->Process(116,dirty);
  Check(image->m_fadingTextures.size()==1&&image->m_fadingTextures[0]->m_texture->GetFileName()=="b"&&image->m_fadingTextures[0]->m_fadeTime==100&&
        image->m_fadingTextures[0]->m_texture->m_alpha==image->GetFadeLevel(100),"pending incoming keeps the outgoing image during motion");
  Check(image->m_currentFadeTime==0&&image->m_texture->m_alpha==0,"pending incoming does not skip its own fade");
  gui.large.motion=false;gui.large.Publish("large");image->Process(132,dirty);image->Process(148,dirty);
  Check(image->m_currentFadeTime>0&&image->m_currentFadeTime<100&&image->m_fadingTextures.size()==1,"incoming fades in once motion stops");}
 {Reset();std::unique_ptr<CGUIImage>image(FadingImage(false,true));gui.large.motion=true;image->Process(116,dirty);
  Check(image->m_fadingTextures.size()==2&&image->m_currentFadeTime==16,"poster fade continues during motion");}
}
static void AddCopy(CGUIImage&image,const std::string&path,unsigned fade){CGUITexture source(image.x,image.y,image.w,image.h);source.m_info.filename=path;if(!path.empty())gui.large.Publish(path);
 source.AllocResources();image.m_fadingTextures.push_back(new CGUIImage::CFadingTexture(&source,fade));source.FreeResources();}
static void Background(CGUIImage&image,const std::string&incoming){image.m_texture=std::make_unique<CGUITexture>(0,0,100,100);image.m_texture->m_info.filename=incoming;image.x=image.y=0;image.w=image.h=100;image.m_lastRenderTime=100;}
static void MotionEmptyIncoming(){CDirtyRegionList dirty;
 {Reset();CGUIImage image;Background(image,"");AddCopy(image,"a",100);gui.large.motion=true;image.Process(116,dirty);image.Process(132,dirty);
  Check(image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_fadeTime==100&&image.m_fadingTextures[0]->m_texture->m_alpha==image.GetFadeLevel(100),"empty incoming keeps the outgoing image during motion");
  gui.large.motion=false;image.Process(148,dirty);
  Check(image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_fadeTime==84,"outgoing image fades to blank at the ordinary step once motion stops");
  for(unsigned time=164;time<=320;time+=16){image.Process(time,dirty);}Check(image.m_fadingTextures.empty()&&!gui.large.entries["a"].refs,"faded outgoing image is released");}
 {Reset();CGUIImage image;Background(image,"");AddCopy(image,"a",40);AddCopy(image,"b",50);gui.large.motion=true;image.Process(116,dirty);
  Check(image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_texture->GetFileName()=="b"&&image.m_fadingTextures[0]->m_fadeTime==100&&!gui.large.entries["a"].refs,
        "switching mid-fade to an empty image keeps the last outgoing image");}
 {Reset();CGUIImage image;Background(image,"large");AddCopy(image,"a",100);AddCopy(image,"",100);gui.large.motion=true;image.Process(116,dirty);
  Check(image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_texture->GetFileName()=="a"&&image.m_fadingTextures[0]->m_fadeTime==100,"passing an empty image keeps the last visible outgoing image");
  gui.large.motion=false;gui.large.Publish("large");image.Process(132,dirty);image.Process(148,dirty);
  Check(image.m_currentFadeTime>0&&image.m_currentFadeTime<100&&image.m_fadingTextures.size()==1&&image.m_fadingTextures[0]->m_fadeTime<100,"crossfade from the kept image runs once motion stops");}
}
int main(){try{Admission();OuterCullAndAutosize();PublicationAndFades();Copies();Replacement();BackgroundClassification();DeferredIncomingFade();BackgroundCopies();DemandClassification();MotionEndsFade();MotionEmptyIncoming();std::printf("PASS %u frontend checks\n",checks);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}}
'''


def source_code(root, sources):
    texture, header, image, control, gfx = [sources[p] for p in FILES[:5]]
    aspect = header[header.index('#define ASPECT_ALIGN_CENTER'):header.index('class CTextureInfo')]
    methods = [function(texture,'bool IsBackgroundSized(')]
    methods += [function(texture, sig) for sig in (
        'CGUITexture::CGUITexture(const CGUITexture& right)', 'bool CGUITexture::IsBackgroundRequest(', 'bool CGUITexture::AllocateOnDemand(',
        'bool CGUITexture::Process(', 'void CGUITexture::Render(int32_t', 'void CGUITexture::Render(float',
        'bool CGUITexture::AllocResources(', 'bool CGUITexture::CalculateSize(', 'void CGUITexture::FreeResources(',
        'bool CGUITexture::ReadyToRender(', 'void CGUITexture::ResetAnimState(', 'bool CGUITexture::UpdateAnimFrame(',
        'bool CGUITexture::SetPosition(', 'bool CGUITexture::SetWidth(', 'bool CGUITexture::SetHeight(',
        'bool CGUITexture::SetVisible(', 'bool CGUITexture::SetAlpha(', 'bool CGUITexture::SetDiffuseColor(',
        'bool CGUITexture::SetFileName(', 'void CGUITexture::SetUseCache(', 'int CGUITexture::GetOrientation(',
        'void CGUITexture::OrientateTexture(')]
    methods += [function(gfx, sig) for sig in ('bool CGraphicContext::SetClipRegion(', 'void CGraphicContext::RestoreClipRegion(', 'void CGraphicContext::ClipRect(')]
    methods += [function(image, sig) for sig in ('void CGUIImage::Process(', 'void CGUIImage::Render(', 'bool CGUIImage::ProcessFading(', 'unsigned char CGUIImage::GetFadeLevel(', 'CRect CGUIImage::CalcRenderRegion(')]
    methods += [function(control, 'void CGUIControl::DoRender(')]
    fading=function(sources['xbmc/guilib/GUIImage.h'], 'class CFadingTexture')+';'
    return PRELUDE.replace('@ASPECT@', aspect).replace('@FADING@', fading).replace('CRect*=nullptr','CRect* = nullptr')+'\n'+'\n\n'.join(methods)+'\n'+TESTS


def semantic_failure(result, witness):
    diagnostic = result.stdout+result.stderr
    return (result.returncode == 1 and any(line.startswith('FAIL: ') and witness in line for line in result.stderr.splitlines())
            and not any(x in diagnostic for x in ('AddressSanitizer','UndefinedBehaviorSanitizer','LeakSanitizer','runtime error:','DEADLYSIGNAL','Sanitizer CHECK failed')))


def check_semantic_classifier():
    witness='designated semantic witness'
    good='FAIL: '+witness+'\n'
    def result(status=1, stdout='', stderr=good):
        return subprocess.CompletedProcess([],status,stdout,stderr)
    assert semantic_failure(result(),witness)
    for status in (0,2,-6,-11,139):
        assert not semantic_failure(result(status),witness)
    for stderr in ('','FAIL: another assertion\n','FAIL: another assertion\n'+witness+'\n'):
        assert not semantic_failure(result(stderr=stderr),witness)
    for marker in ('AddressSanitizer','UndefinedBehaviorSanitizer','LeakSanitizer','runtime error:','DEADLYSIGNAL','Sanitizer CHECK failed'):
        assert not semantic_failure(result(stderr=good+marker),witness)
        assert not semantic_failure(result(stdout=marker),witness)


def compile_run(root, out, name, code):
    cpp=out/(name+'.cpp');cpp.write_text(code);exe=out/name
    result=subprocess.run(['g++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-pie','-no-pie','-I'+str(root/'xbmc'),str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=120)
    (out/(name+'.compile.log')).write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(name+' compile failed: '+result.stderr)
    run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=60,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
    (out/(name+'.stdout')).write_text(run.stdout);(out/(name+'.stderr')).write_text(run.stderr);return run


def mutations(code):
    pairs=[('clip-bypassed','if (!vertex.IsEmpty())','if (true)','fully clipped pending does not admit'),
           ('alpha-gated','if (!vertex.IsEmpty())','if (m_alpha && !vertex.IsEmpty())','zero-alpha future fade target admits'),
           ('stale-pending-bounds','m_vertex = vertex;','(void)vertex;','pending moved bounds follow declared geometry'),
           ('autosize-not-admitted','if (m_width == 0 || m_height == 0)','if (false)','unknown autosize admits before outer cull'),
           ('outer-cull-bypassed','if (IsVisible() && !m_isCulled)','if (IsVisible())','outer culled control never admits'),
           ('outer-scissor-bypassed','if (IsControlRenderable() &&','if (false && IsControlRenderable() &&','outer scissor rejects offscreen declared bounds'),
           ('replacement-not-invalidated','if (m_isAllocated != LARGE)\n          m_invalid = true;','if (false)\n          m_invalid = true;','replacement pending refreshes outer cull bounds'),
           ('pending-render-stale-fit','CRect vertex(m_posX, m_posY, m_posX + m_width, m_posY + m_height);\n      CRect textureCoords','CRect vertex = m_vertex;\n      CRect textureCoords','pending render uses declared bounds independently of stale fitted vertex')]
    for name,old,new,witness in pairs:
        assert code.count(old)==1, name+' mutation anchor not unique'
        yield name,code.replace(old,new,1),witness
    changes=[('background-boundary-exclusive','return 2 * screenWidth * screenHeight >=','return 2 * screenWidth * screenHeight >','half-screen boundary is background'),
             ('background-ignores-screen-scaling','gfx.ScaleFinalXCoord(posX + width, posY) - gfx.ScaleFinalXCoord(posX, posY)','width','background classification uses final screen scaling'),
             ('background-callsite-disabled','IsBackgroundSized(m_posX, m_posY, m_width, m_height)','false','first large background requests scroll deferral'),
             ('foreground-callsite-misclassified','IsBackgroundSized(m_posX, m_posY, m_width, m_height)','true','first poster remains foreground'),
             ('fade-copy-deferred','return !m_continuation && IsBackgroundSized','return IsBackgroundSized','outgoing fade copy is not a new background request'),
             ('demand-not-classified','RequestImageUpload(\n            m_info.filename, IsBackgroundRequest());','RequestImageUpload(\n            m_info.filename);','draw demand reports its consumer type'),
             ('copy-continuation-sticky','  m_continuation = false;\n','','copy loading a new file is a new background request'),
             ('unallocated-copy-continues','m_continuation = right.IsAllocated();','m_continuation = true;','copy of an unallocated background texture is a new background request'),
             ('deferred-fade-clock-stopped','m_lastRenderTime = currentTime;','if (m_texture->ReadyToRender()) m_lastRenderTime = currentTime;','deferred fade resumes at ordinary frame step'),
             ('motion-fade-continues','GetLargeTextureManager().IsBackgroundMotion())','GetLargeTextureManager().IsBackgroundMotion() && false)','motion ends a background crossfade'),
             ('motion-ends-foreground-fade','        m_texture->IsBackgroundRequest() &&\n','','poster fade continues during motion'),
             ('motion-drops-outgoing','      if (!ready)\n      {\n        const auto visible','      if (false)\n      {\n        const auto visible','pending incoming keeps the outgoing image during motion'),
             ('motion-dims-kept-image','        kept->m_fadeTime = m_crossFadeTime;\n','        (void)0;\n','pending incoming keeps the outgoing image during motion'),
             ('motion-empty-is-ready','const bool ready = m_texture->ReadyToRender();','const bool ready = m_texture->ReadyToRender() || m_texture->GetFileName().empty();','empty incoming keeps the outgoing image during motion'),
             ('motion-keeps-empty-copy','{ return !texture->m_texture->GetFileName().empty(); }','{ return texture != nullptr; }','passing an empty image keeps the last visible outgoing image'),
             ('held-outgoing-fades','      if (!holdOutgoing && (m_texture->ReadyToRender() || m_texture->GetFileName().empty()))','      if ((holdOutgoing || !holdOutgoing) && (m_texture->ReadyToRender() || m_texture->GetFileName().empty()))','empty incoming keeps the outgoing image during motion'),
             ('motion-skips-pending-fade','      if (ready)\n        m_currentFadeTime = m_crossFadeTime;','      m_currentFadeTime = m_crossFadeTime;','pending incoming does not skip its own fade')]
    for name,old,new,witness in changes:
        assert code.count(old)==1,name+' mutation anchor not unique'
        yield name,code.replace(old,new,1),witness
    old='m_lastReadyState = ready;\n      changed = true;'
    assert code.count(old)==1
    candidate=code.replace(old,'m_lastReadyState = ready;\n      changed = !ready;',1)
    old='m_texture = texture;\n\n        changed = true;'
    assert candidate.count(old)==1
    yield 'publication-never-dirties',candidate.replace(old,'m_texture = texture;',1),'idle publication dirties without input'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--out',type=Path,default=Path('/tmp/kodi-gui-upload-demand-tests'));a=p.parse_args();root=a.root.resolve();out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    check_semantic_classifier();sources={f:(root/f).read_text() for f in FILES};code=source_code(root,sources);good=compile_run(root,out,'production',code)
    assert good.returncode==0 and good.stdout.startswith('PASS '),(good.returncode,good.stdout,good.stderr)
    controls=[]
    for name,mutated,witness in mutations(code):
        result=compile_run(root,out,name,mutated);assert semantic_failure(result,witness),(name,result.returncode,result.stdout,result.stderr)
        controls.append({'name':name,'result':'REJECTED','exit':result.returncode,'witness':witness})
    receipt={'result':'PASS','production':good.stdout.strip(),'semantic_controls':controls,'sources':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in sources},'extracted_fixture_sha256':hashlib.sha256(code.encode()).hexdigest(),'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'limits':'Actual frontend methods with owned manager/publication/render boundaries. No real GL, image pixels, JobManager/manager lifetime, device timing or CE target compiler acceptance. ASan/UBSan with leak detection disabled; no TSan/leak claim.'}
    (out/'results.json').write_text(json.dumps(receipt,indent=2)+'\n');print(good.stdout.strip());print(len(controls),'semantic controls rejected')
if __name__=='__main__':main()
