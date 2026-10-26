#!/usr/bin/env python3
"""Exercise actual scroll-aware decode admission composed with private GPU demand.

Uses the full production large-texture manager and JobManager cancellation/
completion methods via test-image-upload-demand. Decoder, queue, clock, settings
and GL are fixtures. IsBackgroundSized executes separately with a scaled 4K
context, and the actual control/animation Animate methods with a recording
manager. No actual GUI, image decoder, EGL/GL or device timing is established.
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
SPEC=importlib.util.spec_from_file_location('demand_defer_fixture',ROOT/'tools/test-image-upload-demand.py')
DEMAND=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(DEMAND)
LARGE='xbmc/GUILargeTextureManager.cpp';TEXTURE='xbmc/guilib/GUITexture.cpp';WINDOWS='xbmc/guilib/GUIWindowManager.cpp'
CONTROL='xbmc/guilib/GUIControl.cpp';GROUPLIST='xbmc/guilib/GUIControlGroupList.cpp';EFFECT='xbmc/guilib/VisibleEffect.cpp';EFFECT_H='xbmc/guilib/VisibleEffect.h'
MAIN=r'''
static void Ask(Harness&h,const std::string&path,bool background=true,bool cache=true,bool first=true){CTextureArray array;Check(h.images.GetImage(path,array,first,cache,background),"request admission remains pending");}
static void QuietAndScroll(){State s;Harness h(s);Ask(h,"A");Check(h.jobs.Queued()==0&&h.images.m_queued.front().first==0,"background request waits for end of its frame");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"quiet frame starts background decoder");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"started decoder is admitted once");++cases;
 State t;Harness q(t);Ask(q,"B");q.images.SetScrolling();q.images.ProcessDeferred();Check(q.jobs.Queued()==0,"late container scroll still defers background");for(int i=0;i<30;++i){frameTime+=16;q.images.SetScrolling();q.images.ProcessDeferred();}Check(q.jobs.Queued()==0,"background waits throughout scrolling");q.images.ProcessDeferred();Check(q.jobs.Queued()==1,"first quiet frame after scrolling starts decoder");++cases;}
static void ForegroundAndSharing(){State s;Harness h(s);h.images.SetScrolling();Ask(h,"poster",false);Check(h.jobs.Queued()==1,"foreground starts immediately during scrolling");++cases;
 State t;Harness q(t);Ask(q,"A");auto*image=q.images.m_queued.front().second;auto token=image->GetCancellationToken();Ask(q,"A");q.images.SetScrolling();q.images.ProcessDeferred();Check(q.jobs.Queued()==0&&image->m_refCount==2,"background consumers share one deferred entry");q.images.ProcessDeferred();Check(q.jobs.Queued()==1&&q.images.m_queued.front().second==image&&image->GetCancellationToken()==token,"background consumers eventually share one decoder/token");++cases;}
static void MixedUrgency(){State s;Harness h(s);Ask(h,"A",true,false);auto*image=h.images.m_queued.front().second;auto token=image->GetCancellationToken();h.images.SetScrolling();Ask(h,"A",false,true);
 Check(h.jobs.Queued()==1&&h.images.m_queued.front().first!=0,"foreground consumer promotes shared deferred request");
 auto*loader=static_cast<CImageLoader*>(h.jobs.m_jobQueue[CJob::PRIORITY_NORMAL].front().m_job);
 Check(image->m_refCount==2&&h.images.m_queued.front().second==image&&image->GetCancellationToken()==token&&loader->m_cancelled==token,"promotion preserves both references and request token");
 Check(!image->m_useCache&&!loader->m_use_cache,"promotion preserves first consumer cache policy");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"foreground promotion schedules one decoder");h.images.ReleaseImage("A",true);Check(!token->load()&&image->m_refCount==1,"first mixed-consumer release keeps request live");h.images.ReleaseImage("A",true);Check(token->load()&&h.jobs.Queued()==0,"last mixed-consumer release cancels actual decoder");++cases;
 State t;Harness q(t);Ask(q,"B",false);unsigned id=q.images.m_queued.front().first;Ask(q,"B",true);q.images.SetScrolling();q.images.ProcessDeferred();Check(q.jobs.Queued()==1&&q.images.m_queued.front().first==id&&q.images.m_queued.front().second->m_refCount==2,"later background consumer cannot defer active foreground decoder");++cases;}
static void DeferredRelease(){State s;Harness h(s);Ask(h,"A");auto token=h.images.m_queued.front().second->GetCancellationToken();unsigned locks=h.jobs.m_section.lockCount;h.images.ReleaseImage("A",true);
 Check(h.jobs.m_section.lockCount==locks,"released deferred request never calls CancelJob zero");Check(h.images.m_queued.empty()&&token->load(),"released deferred request retires token and entry");h.images.ProcessDeferred();Check(h.jobs.Queued()==0,"released deferred request never starts");++cases;}
static void PerRequestTimeout(){State s;Harness h(s);frameTime=100;Ask(h,"A");frameTime=2900;Ask(h,"B");h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==0,"both requests remain below own timeout");frameTime=3100;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()>=1&&h.images.m_queued[0].first!=0,"request starts at three seconds despite scrolling");Check(h.jobs.Queued()==1&&h.images.m_queued[1].first==0,"each background request has its own three-second timeout");frameTime=5899;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"later request waits until exact own timeout");frameTime=5900;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==2,"request starts at three seconds despite scrolling");++cases;}
static void PollAndDrawDemand(){State s;Harness h(s);Ask(h,"A");Ask(h,"A",false,false,false);Check(h.jobs.Queued()==0&&h.images.m_queued.front().second->m_refCount==1,"non-first foreground poll cannot promote or add reference");h.images.RequestImageUpload("A",true);h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==0,"draw demand cannot bypass deferred decoder scheduling");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"quiet frame admits demanded decoder");h.Next();CTextureArray array;Check(h.Get(array)&&!array.size()&&s.uploads==0&&h.jobs.Queued()==0,"completed background decode waits for the end of its frame");h.images.ProcessDeferred();Check(h.Get(array)&&!array.size()&&s.uploads==0&&h.jobs.Queued()==1,"deferred decode keeps private upload publication barrier");h.Next();Check(h.Get(array)&&array.size()==1&&s.uploads==1&&s.releases==1,"deferred demand publishes after separate upload completion");array.Free();++cases;}
static void Rejection(){for(int mode=0;mode<3;++mode){State s;Harness h(s);h.jobs.reject=true;if(mode==0)Ask(h,"A",false);else{Ask(h,"A",true,false);if(mode==1)h.images.ProcessDeferred();else Ask(h,"A",false,true);}CTextureArray array;
 Check(h.images.m_queued.empty()&&h.jobs.Queued()==0&&!h.Get(array)&&!array.size(),"rejected immediate/deferred/promoted decoder becomes load failure, not stranded");
 auto*image=h.images.m_allocatedLookup.at("A");Check(image->m_refCount==(mode==2?2u:1u),"queue rejection preserves consumer reference count");if(mode==2)Check(!image->m_useCache,"rejected promotion retains original cache policy");++cases;}}
static void Publish(Harness&h,const std::string&path){Ask(h,path);h.images.ProcessDeferred();h.Next();h.images.ProcessDeferred();h.images.RequestImageUpload(path,true);h.Next();CTextureArray array;
 Check(h.images.GetImage(path,array,false)&&array.size()==1,"background image publishes after quiet frames");array.Free();}
static void HeldReuse(){State s;Harness h(s);Publish(h,"A");h.images.ReleaseImage("A",false);CTextureArray a;h.images.SetScrolling();
 Check(h.images.GetImage("A",a,true,true,true)&&!a.size(),"recently released background image is held during motion");
 h.images.ProcessDeferred();Check(h.images.GetImage("A",a,false,true,true)&&!a.size(),"held image stays hidden while motion continues");
 h.images.ProcessDeferred();Check(h.images.GetImage("A",a,false,true,true)&&a.size()==1&&s.decodes==1&&s.uploads==1,"held image is shown after motion without reloading");a.Free();
 h.images.ReleaseImage("A",false);CTextureArray b;Check(h.images.GetImage("A",b,true,true,true)&&!b.size(),"reuse at rest waits for the end of its frame");
 h.images.ProcessDeferred();Check(h.images.GetImage("A",b,false,true,true)&&b.size()==1,"reuse at rest is shown on the next frame");b.Free();++cases;}
static void ForegroundReuse(){State s;Harness h(s);Publish(h,"A");h.images.ReleaseImage("A",false);CTextureArray a;h.images.SetScrolling();
 Check(h.images.GetImage("A",a,true,true,false)&&a.size()==1,"foreground reuse is never held");
 // A consumer that is not drawing, such as a hidden view's image, keeps the image loaded.
 CTextureArray b;Check(h.images.GetImage("A",b,true,true,true)&&!b.size(),"background consumer is held while another consumer keeps the image");
 CTextureArray f;Check(h.images.GetImage("A",f,false)&&f.size()==1,"foreground poll is not held");f.Free();
 h.images.ProcessDeferred();Check(h.images.GetImage("A",b,false,true,true)&&!b.size(),"kept image stays held while motion continues");
 h.images.ProcessDeferred();Check(h.images.GetImage("A",b,false,true,true)&&b.size()==1&&s.decodes==1,"kept image is shown after motion without reloading");a.Free();b.Free();++cases;
 // A foreground consumer waits for an upload when a background consumer arrives during motion.
 State t;Harness q(t);Ask(q,"B",false);q.Next();CTextureArray c;q.images.SetScrolling();Check(q.images.GetImage("B",c,true,true,true)&&!c.size(),"recently released background image is held during motion");
 q.images.RequestImageUpload("B",true);Check(q.jobs.Queued()==0,"held image is not uploaded during motion");
 q.images.RequestImageUpload("B",false);Check(q.jobs.Queued()==1,"foreground demand uploads a held image");q.Next();
 CTextureArray d;Check(q.images.GetImage("B",d,false)&&d.size()==1,"foreground poll is not held");
 Check(q.images.GetImage("B",c,false,true,true)&&!c.size(),"background consumer stays held after a foreground upload");
 q.images.ProcessDeferred();q.images.ProcessDeferred();Check(q.images.GetImage("B",c,false,true,true)&&c.size()==1&&t.uploads==1,"background consumer is served after motion without another upload");c.Free();d.Free();++cases;}
static void CompletionHold(){State s;Harness h(s);frameTime=100;Ask(h,"A");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"quiet frame starts background decoder");
 h.images.SetScrolling();h.Next();Check(h.images.m_allocatedLookup.at("A")->m_held,"decode finished during motion is held");
 h.images.RequestImageUpload("A",true);h.images.ProcessDeferred();Check(h.jobs.Queued()==0&&s.uploads==0,"held image is not uploaded during motion");
 frameTime=3099;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==0,"held image waits within its request timeout");
 frameTime=3100;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"held image keeps its request's three-second timeout");
 h.Next();CTextureArray a;Check(h.Get(a)&&a.size()==1&&s.uploads==1,"held image publishes after its upload");a.Free();++cases;
 State t;Harness q(t);Ask(q,"B",false);q.images.SetScrolling();q.Next();Check(!q.images.m_allocatedLookup.at("B")->m_held,"foreground decode is never held");++cases;}
static void SharedDecode(){for(bool async:{true,false})for(int order=0;order<2;++order){State s;s.async=async;Harness h(s);
 // Both consumers request the same image in one frame, in either order.
 if(order==0){Ask(h,"A");Ask(h,"A",false);}else{Ask(h,"A",false);Ask(h,"A");}
 Check(h.jobs.Queued()==1,"shared request starts one foreground decode");h.images.ProcessDeferred();h.images.SetScrolling();h.Next();
 Check(h.images.m_allocatedLookup.at("A")->m_held,"shared decode finished during motion is held for its background consumer");
 h.images.RequestImageUpload("A",false);if(async)h.Next();CTextureArray f,b;
 Check(h.images.GetImage("A",f,false)&&f.size()==1,"foreground consumer of a shared decode is served during motion");
 Check(h.images.GetImage("A",b,false,true,true)&&!b.size(),"background consumer of a shared decode waits for motion to stop");
 h.images.ProcessDeferred();h.images.ProcessDeferred();Check(h.images.GetImage("A",b,false,true,true)&&b.size()==1&&s.decodes==1,"background consumer of a shared decode is served after motion");f.Free();b.Free();++cases;}
 State s;Harness h(s);frameTime=100;Ask(h,"A",false);frameTime=2000;Ask(h,"A");h.images.SetScrolling();h.Next();
 frameTime=4999;h.images.SetScrolling();h.images.ProcessDeferred();Check(h.images.m_allocatedLookup.at("A")->m_held,"joining background consumer times out from its own request");
 frameTime=5000;h.images.SetScrolling();h.images.ProcessDeferred();Check(!h.images.m_allocatedLookup.at("A")->m_held,"joining background consumer times out after three seconds");++cases;}
static void SettingToggle(){for(bool on:{false,true}){const char*text=on?"enabled setting delays":"disabled setting does not delay";
 State s;s.delayBackgrounds=on;Harness h(s);h.images.SetScrolling();Ask(h,"A");Check((h.jobs.Queued()==0)==on,text);h.images.ReleaseImage("A",true);h.images.ProcessDeferred();
  Publish(h,"B");h.images.ReleaseImage("B",false);CTextureArray a;h.images.SetScrolling();Check(h.images.GetImage("B",a,true,true,true)&&(a.size()==0)==on,text);a.Free();++cases;}}
static void MotionReport(){{State s;Harness h(s);Check(!h.images.IsBackgroundMotion(),"still GUI reports no background motion");
  h.images.SetScrolling();Check(h.images.IsBackgroundMotion(),"motion is reported within its frame");h.images.ProcessDeferred();
  Check(h.images.IsBackgroundMotion(),"motion is reported through the next frame");h.images.ProcessDeferred();
  Check(!h.images.IsBackgroundMotion(),"motion report ends after a still frame");++cases;}
 {State s;s.delayBackgrounds=false;Harness h(s);h.images.SetScrolling();Check(!h.images.IsBackgroundMotion(),"disabled setting reports no background motion");
  h.images.ProcessDeferred();Check(!h.images.IsBackgroundMotion(),"disabled setting reports no background motion");++cases;}}
int main(){try{QuietAndScroll();ForegroundAndSharing();MixedUrgency();DeferredRelease();PerRequestTimeout();PollAndDrawDemand();Rejection();HeldReuse();ForegroundReuse();CompletionHold();SharedDecode();SettingToggle();MotionReport();std::printf("PASS %u cases %u checks; actual deferred manager/demand composition; host fixtures\n",cases,checks);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def manager_source(files):
    generated=DEMAND.source(files)
    assert generated.endswith(DEMAND.TESTS)
    generated=generated[:-len(DEMAND.TESTS)]
    # Observes entry to the actual CancelJob through its real lock, without
    # replacing or instrumenting that production method.
    generated=generated.replace('struct CCriticalSection {std::recursive_mutex mutex;void lock(){mutex.lock();++heldLocks;}',
        'struct CCriticalSection {std::recursive_mutex mutex;unsigned lockCount=0;void lock(){mutex.lock();++heldLocks;++lockCount;}')
    helpers=DEMAND.TESTS[:DEMAND.TESTS.index('static void UndemandedAndCoalesced()')]
    return generated+helpers+MAIN

SIZE_SOURCE=r'''
#include <cstdio>
#include <string>
struct CGraphicContext {float ScaleFinalXCoord(float x,float)const{return 2*x+10;}float ScaleFinalYCoord(float,float y)const{return 2*y-4;}int GetWidth()const{return 3840;}int GetHeight()const{return 2160;}};
struct CWinSystem {CGraphicContext gfx;CGraphicContext&GetGfxContext(){return gfx;}};
struct CServiceBroker {static CWinSystem*GetWinSystem(){static CWinSystem w;return &w;}};
@FUNCTION@
int main(){
#define CHECK(x,msg) if(!(x)){std::fprintf(stderr,"FAIL %s\n",msg);return 1;}
 CHECK(IsBackgroundSized(0,0,1920,1080),"full-screen fanart is a background");
 CHECK(IsBackgroundSized(100,50,1920,540),"half-screen image is a background");
 CHECK(!IsBackgroundSized(0,0,1920,530),"image short of half screen is not background");
 CHECK(!IsBackgroundSized(101,154,476,716),"info poster is not background");
 CHECK(!IsBackgroundSized(0,0,300,445),"wall poster is not background");
 std::puts("PASS 5 background size checks");return 0;
}
'''

ANIMATION_SOURCE=r'''
#pragma GCC diagnostic ignored "-Wunused-parameter"
#include <cstdio>
#include <stdexcept>
#include <vector>
static unsigned checks=0,cases=0,reports=0;
static void Check(bool ok,const char*text){++checks;if(!ok)throw std::runtime_error(text);}
enum ANIMATION_PROCESS { ANIM_PROCESS_NONE = 0, ANIM_PROCESS_NORMAL, ANIM_PROCESS_REVERSE };
enum ANIMATION_STATE { ANIM_STATE_NONE = 0, ANIM_STATE_DELAYED, ANIM_STATE_IN_PROCESS, ANIM_STATE_APPLIED };
enum ANIMATION_TYPE { ANIM_TYPE_NONE = 0, ANIM_TYPE_CONDITIONAL = 5 };
enum GUIVISIBLE { HIDDEN = 0, DELAYED, VISIBLE };
struct CPoint {float x,y;CPoint(float a=0,float b=0):x(a),y(b){}};
struct TransformMatrix {void Reset(){}};
class CAnimation {public:
 enum ANIM_REPEAT { ANIM_REPEAT_NONE = 0, ANIM_REPEAT_PULSE, ANIM_REPEAT_LOOP };
 ANIMATION_TYPE m_type=ANIM_TYPE_CONDITIONAL;ANIM_REPEAT m_repeatAnim=ANIM_REPEAT_NONE;bool m_lastCondition=true;
 ANIMATION_PROCESS m_queuedProcess=ANIM_PROCESS_NONE,m_currentProcess=ANIM_PROCESS_NONE;ANIMATION_STATE m_currentState=ANIM_STATE_NONE;
 unsigned int m_start=0,m_length=300,m_delay=0,m_amount=0;
 void Animate(unsigned int time, bool startAnim);
 ANIMATION_TYPE GetType()const{return m_type;}ANIMATION_STATE GetState()const{return m_currentState;}ANIMATION_PROCESS GetProcess()const{return m_currentProcess;}
 void RenderAnimation(TransformMatrix&,const CPoint&){}
 @REPEATING@
};
@CANIMATE@
struct LargeManager {void SetScrolling(){++reports;}};
struct GUI {LargeManager large;LargeManager&GetLargeTextureManager(){return large;}};
struct CServiceBroker {static GUI*GetGUI(){static GUI g;return &g;}};
class CGUIControl {public:
 GUIVISIBLE m_visible=VISIBLE;TransformMatrix m_transform;std::vector<CAnimation>m_animations;
 bool HasProcessed()const{return true;}void UpdateStates(ANIMATION_TYPE,ANIMATION_PROCESS,ANIMATION_STATE){}
 float GetXPosition()const{return 0;}float GetYPosition()const{return 0;}float GetWidth()const{return 10;}float GetHeight()const{return 10;}
 bool Animate(unsigned int currentTime);
};
@ANIMATE@
static unsigned Frame(CGUIControl&c,unsigned time){unsigned before=reports;c.Animate(time);return reports-before;}
static CGUIControl Start(CAnimation::ANIM_REPEAT repeat=CAnimation::ANIM_REPEAT_NONE,unsigned delay=0,ANIMATION_PROCESS process=ANIM_PROCESS_NORMAL){
 CGUIControl c;CAnimation a;a.m_repeatAnim=repeat;a.m_delay=delay;a.m_queuedProcess=process;c.m_animations.push_back(a);return c;}
int main(){try{
 {auto c=Start();Check(Frame(c,1000)==1,"running animation reports motion");Check(Frame(c,1299)==1,"running animation reports every frame");
  Check(Frame(c,1300)==0,"finished animation stops reporting");Check(Frame(c,5000)==0,"finished animation stops reporting");++cases;}
 {auto c=Start(CAnimation::ANIM_REPEAT_NONE,300);Check(Frame(c,1000)==0,"delay alone is not motion");Check(Frame(c,1300)==1,"delayed animation reports once it moves");++cases;}
 {auto c=Start(CAnimation::ANIM_REPEAT_NONE,0,ANIM_PROCESS_REVERSE);Check(Frame(c,1000)==1,"reverse animation reports motion");Check(Frame(c,1300)==0,"finished animation stops reporting");++cases;}
 for(auto repeat:{CAnimation::ANIM_REPEAT_LOOP,CAnimation::ANIM_REPEAT_PULSE}){auto c=Start(repeat);for(unsigned t=1000;t<4000;t+=50)Check(Frame(c,t)==0,"repeating animation is not motion");++cases;}
 {auto c=Start(CAnimation::ANIM_REPEAT_LOOP);CAnimation a;a.m_queuedProcess=ANIM_PROCESS_NORMAL;c.m_animations.push_back(a);Check(Frame(c,1000)==1,"a moving animation reports beside a repeating one");++cases;}
 std::printf("PASS %u cases %u checks; actual control/animation Animate; recording manager\n",cases,checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def animation_source(files):
    header=files[EFFECT_H];repeating=DEMAND.EXTRACT.function(header,'inline bool IsRepeating() const')
    return (ANIMATION_SOURCE.replace('@REPEATING@',repeating)
            .replace('@CANIMATE@',DEMAND.EXTRACT.function(files[EFFECT],'void CAnimation::Animate('))
            .replace('@ANIMATE@',DEMAND.EXTRACT.function(files[CONTROL],'bool CGUIControl::Animate(')))

def size_source(files):return SIZE_SOURCE.replace('@FUNCTION@',DEMAND.EXTRACT.function(files[TEXTURE],'bool IsBackgroundSized('))
def edit(files,path,signature,old,new):
    changed=dict(files);body=DEMAND.EXTRACT.function(files[path],signature);assert body.count(old)==1,(signature,old);changed[path]=files[path].replace(body,body.replace(old,new,1),1);return changed

def mutations(files):
    definitions=[
      ('no-defer',LARGE,'void CGUILargeTextureManager::QueueImage(','if (defer)','if (false)','background request waits for end of its frame'),
      ('sticky-scrolling',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','m_scrolling.exchange(false)','m_scrolling.load()','first quiet frame after scrolling starts decoder'),
      ('no-timeout',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','scrolling && now - image->m_deferredSince < MAX_DEFER_TIME','scrolling && ((void)now, (void)image, true)','request starts at three seconds despite scrolling'),
      ('shared-global-timeout',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','else if (waits(it->second))','else if (waits(m_queued.front().second))','each background request has its own three-second timeout'),
      ('reuse-not-held',LARGE,'bool CGUILargeTextureManager::GetImage(','          image->m_held = true;\n','','recently released background image is held during motion'),
      ('held-handed-out',LARGE,'bool CGUILargeTextureManager::GetImage(','    if (image->m_held && background)\n      return true;\n','','recently released background image is held during motion'),
      ('hold-blocks-foreground-poll',LARGE,'bool CGUILargeTextureManager::GetImage(','    if (image->m_held && background)','    if (image->m_held)','foreground poll is not held'),
      ('hold-needs-sole-consumer',LARGE,'bool CGUILargeTextureManager::GetImage(','      if (defer)\n      {\n        // The image may','      if (defer && image->m_refCount == 1)\n      {\n        // The image may','background consumer is held while another consumer keeps the image'),
      ('background-join-not-recorded',LARGE,'void CGUILargeTextureManager::QueueImage(','if (defer && !it->second->m_background)','if (false)','shared decode finished during motion is held for its background consumer'),
      ('foreground-join-clears-background',LARGE,'void CGUILargeTextureManager::QueueImage(','    else if (!defer)\n    {\n','    else if (!defer)\n    {\n      it->second->m_background = false;\n','shared decode finished during motion is held for its background consumer'),
      ('join-keeps-foreground-timeout',LARGE,'void CGUILargeTextureManager::QueueImage(','      it->second->m_deferredSince = CTimeUtils::GetFrameTime();\n','','joining background consumer times out from its own request'),
      ('completion-not-held',LARGE,'void CGUILargeTextureManager::OnJobComplete(','if (success && image->m_background)','if (false)','completed background decode waits for the end of its frame'),
      ('foreground-completion-held',LARGE,'void CGUILargeTextureManager::OnJobComplete(','if (success && image->m_background)','if (success)','foreground decode is never held'),
      ('upload-ignores-hold',LARGE,'void CGUILargeTextureManager::QueueUpload(',' ||\n      (image->m_held && !image->m_foregroundDemand)','','completed background decode waits for the end of its frame'),
      ('hold-blocks-foreground-upload',LARGE,'void CGUILargeTextureManager::QueueUpload(','(image->m_held && !image->m_foregroundDemand)','image->m_held','foreground demand uploads a held image'),
      ('allocated-demand-unclassified',LARGE,'void CGUILargeTextureManager::RequestImageUpload(','allocated->second->m_foregroundDemand |= !background;','allocated->second->m_foregroundDemand |= true;','held image is not uploaded during motion'),
      ('queued-demand-unclassified',LARGE,'void CGUILargeTextureManager::RequestImageUpload(','queued->second->m_foregroundDemand |= !background;','queued->second->m_foregroundDemand |= true;','completed background decode waits for the end of its frame'),
      ('hold-never-released',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','      image->m_held = false;\n      QueueUpload(image);','      (void)image;','deferred decode keeps private upload publication barrier'),
      ('setting-ignored-when-off',LARGE,'bool CGUILargeTextureManager::DeferBackgrounds(','  return CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(\n      CSettings::SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES);','  return true;','disabled setting does not delay'),
      ('setting-ignored-when-on',LARGE,'bool CGUILargeTextureManager::DeferBackgrounds(','  return CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(\n      CSettings::SETTING_LOOKANDFEEL_DELAYBACKGROUNDIMAGES);','  return false;','background request waits for end of its frame'),
      ('motion-previous-frame-ignored',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','  m_moved = scrolling;\n','','motion is reported through the next frame'),
      ('motion-report-sticky',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','  m_moved = scrolling;','  m_moved = m_moved || scrolling;','motion report ends after a still frame'),
      ('motion-ignores-setting',LARGE,'bool CGUILargeTextureManager::IsBackgroundMotion(','return (m_scrolling || m_moved) && DeferBackgrounds();','return m_scrolling || m_moved;','disabled setting reports no background motion'),
      ('repeating-is-motion',CONTROL,'bool CGUIControl::Animate(',' && !anim.IsRepeating()','','repeating animation is not motion'),
      ('applied-is-motion',CONTROL,'bool CGUIControl::Animate(','anim.GetState() == ANIM_STATE_IN_PROCESS','anim.GetProcess() != ANIM_PROCESS_NONE','finished animation stops reporting'),
      ('delay-is-motion',CONTROL,'bool CGUIControl::Animate(','anim.GetState() == ANIM_STATE_IN_PROCESS','(anim.GetState() == ANIM_STATE_IN_PROCESS || anim.GetState() == ANIM_STATE_DELAYED)','delay alone is not motion'),
      ('unreported-animation',CONTROL,'bool CGUIControl::Animate(','if (moving)','if (moving && false)','running animation reports motion'),
      ('missing-foreground-promotion',LARGE,'void CGUILargeTextureManager::QueueImage(','if (!defer)','if (false)','foreground consumer promotes shared deferred request'),
      ('overwrite-cache-policy',LARGE,'void CGUILargeTextureManager::QueueImage(','    it->second->AddRef();','    it->second->AddRef(); it->second->m_useCache=useCache;','promotion preserves first consumer cache policy'),
      ('rejected-decode-stranded',LARGE,'CGUILargeTextureManager::queueIterator CGUILargeTextureManager::StartQueuedImage(','if (it->first == 0)','if (false)','rejected immediate/deferred/promoted decoder becomes load failure, not stranded'),
      ('cancel-job-zero',LARGE,'void CGUILargeTextureManager::ReleaseImage(','          if (it->first != 0)\n','', 'released deferred request never calls CancelJob zero'),
      ('quarter-screen',TEXTURE,'bool IsBackgroundSized(','return 2 * screenWidth','return 4 * screenWidth','image short of half screen is not background'),
      ('full-screen-only',TEXTURE,'bool IsBackgroundSized(','return 2 * screenWidth','return 1 * screenWidth','half-screen image is a background')]
    for name,path,signature,old,new,witness in definitions:yield name,edit(files,path,signature,old,new),witness,path

def callsites(files):
    alloc=DEMAND.EXTRACT.function(files[TEXTURE],'bool CGUITexture::AllocResources()')
    assert re.search(r'GetImage\(\s*m_info\.filename, texture, !IsAllocated\(\), m_use_cache,\s*IsBackgroundRequest\(\)\)',alloc)
    process=DEMAND.EXTRACT.function(files[WINDOWS],'void CGUIWindowManager::Process(')
    assert process.index('window->DoProcess(currentTime, m_dirtyregions);\n  }')<process.index('GetLargeTextureManager().ProcessDeferred();')<process.index('AssignDepth')
    group=DEMAND.EXTRACT.function(files[GROUPLIST],'void CGUIControlGroupList::Process(')
    assert group.index('m_scroller.Update(currentTime)')<group.index('if (m_scroller.IsScrolling())\n    CServiceBroker::GetGUI()->GetLargeTextureManager().SetScrolling();')<group.index('UpdateVisibility')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output','--out',dest='out',type=Path);parser.add_argument('--sanitize',action='store_true');args=parser.parse_args()
    out=args.out or Path(tempfile.mkdtemp(prefix='gui-background-defer-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in (*DEMAND.FILES,TEXTURE,WINDOWS,CONTROL,EFFECT,EFFECT_H,GROUPLIST)};callsites(files);records=[]
    variants=[('manager',files,None,LARGE),('sizes',files,None,TEXTURE),('animation',files,None,CONTROL),*mutations(files)]
    builders={LARGE:manager_source,TEXTURE:size_source,CONTROL:animation_source}
    for name,selected,witness,path in variants:
        cpp=out/(name+'.cpp');binary=out/name;cpp.write_text(builders[path](selected))
        cmd=['g++','-std=c++17','-O1','-g','-pthread','-Wall','-Wextra','-Werror']
        if args.sanitize:cmd+=['-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
        cmd+=[str(cpp),'-o',str(binary)];c=subprocess.run(cmd,capture_output=True,text=True);(out/(name+'-compile.log')).write_text(c.stdout+c.stderr);assert c.returncode==0,c.stderr
        r=subprocess.run([str(binary)],capture_output=True,text=True,timeout=30,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
        (out/(name+'-stdout.txt')).write_text(r.stdout);(out/(name+'-stderr.txt')).write_text(r.stderr)
        accepted=(r.returncode==0 and not r.stderr) if witness is None else (r.returncode==1 and r.stderr=='FAIL '+witness+'\n' and not any(t in r.stdout+r.stderr for t in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'name':name,'compile_argv':cmd,'compile_exit':c.returncode,'exit':r.returncode,'witness':witness,'accepted':accepted,'generated_sha256':hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out/'receipt.json').write_text(json.dumps({'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'dependency_sha256':hashlib.sha256((ROOT/'tools/test-image-upload-demand.py').read_bytes()).hexdigest(),'source_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'results':records},indent=2)+'\n');assert accepted,(name,r.returncode,r.stdout,r.stderr)
        print(r.stdout.strip() if witness is None else 'PASS semantic control '+name,flush=True)
if __name__=='__main__':main()
