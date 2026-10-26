#!/usr/bin/env python3
"""Exercise actual scroll-aware decode admission composed with private GPU demand.

Uses the full production large-texture manager and JobManager cancellation/
completion methods via test-image-upload-demand. Decoder, queue, clock and GL
are fixtures. IsBackgroundSized executes separately with a scaled 4K context.
No actual GUI, image decoder, EGL/GL or device timing is established.
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
static void PollAndDrawDemand(){State s;Harness h(s);Ask(h,"A");Ask(h,"A",false,false,false);Check(h.jobs.Queued()==0&&h.images.m_queued.front().second->m_refCount==1,"non-first foreground poll cannot promote or add reference");h.Request();h.images.SetScrolling();h.images.ProcessDeferred();Check(h.jobs.Queued()==0,"draw demand cannot bypass deferred decoder scheduling");h.images.ProcessDeferred();Check(h.jobs.Queued()==1,"quiet frame admits demanded decoder");h.Next();CTextureArray array;Check(h.Get(array)&&!array.size()&&s.uploads==0&&h.jobs.Queued()==1,"deferred decode keeps private upload publication barrier");h.Next();Check(h.Get(array)&&array.size()==1&&s.uploads==1&&s.releases==1,"deferred demand publishes after separate upload completion");array.Free();++cases;}
static void Rejection(){for(int mode=0;mode<3;++mode){State s;Harness h(s);h.jobs.reject=true;if(mode==0)Ask(h,"A",false);else{Ask(h,"A",true,false);if(mode==1)h.images.ProcessDeferred();else Ask(h,"A",false,true);}CTextureArray array;
 Check(h.images.m_queued.empty()&&h.jobs.Queued()==0&&!h.Get(array)&&!array.size(),"rejected immediate/deferred/promoted decoder becomes load failure, not stranded");
 auto*image=h.images.m_allocatedLookup.at("A");Check(image->m_refCount==(mode==2?2u:1u),"queue rejection preserves consumer reference count");if(mode==2)Check(!image->m_useCache,"rejected promotion retains original cache policy");++cases;}}
int main(){try{QuietAndScroll();ForegroundAndSharing();MixedUrgency();DeferredRelease();PerRequestTimeout();PollAndDrawDemand();Rejection();std::printf("PASS %u cases %u checks; actual deferred manager/demand composition; host fixtures\n",cases,checks);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
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

def size_source(files):return SIZE_SOURCE.replace('@FUNCTION@',DEMAND.EXTRACT.function(files[TEXTURE],'bool IsBackgroundSized('))
def edit(files,path,signature,old,new):
    changed=dict(files);body=DEMAND.EXTRACT.function(files[path],signature);assert body.count(old)==1,(signature,old);changed[path]=files[path].replace(body,body.replace(old,new,1),1);return changed

def mutations(files):
    definitions=[
      ('no-defer',LARGE,'void CGUILargeTextureManager::QueueImage(','if (defer)','if (false)','background request waits for end of its frame'),
      ('sticky-scrolling',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','m_scrolling.exchange(false)','m_scrolling.load()','first quiet frame after scrolling starts decoder'),
      ('no-timeout',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','scrolling && now - it->second->m_deferredSince < MAX_DEFER_TIME','scrolling && ((void)now, true)','request starts at three seconds despite scrolling'),
      ('shared-global-timeout',LARGE,'void CGUILargeTextureManager::ProcessDeferred(','it->second->m_deferredSince','m_queued.front().second->m_deferredSince','each background request has its own three-second timeout'),
      ('missing-foreground-promotion',LARGE,'void CGUILargeTextureManager::QueueImage(','if (!defer)','if (false)','foreground consumer promotes shared deferred request'),
      ('overwrite-cache-policy',LARGE,'void CGUILargeTextureManager::QueueImage(','    it->second->AddRef();','    it->second->AddRef(); it->second->m_useCache=useCache;','promotion preserves first consumer cache policy'),
      ('rejected-decode-stranded',LARGE,'CGUILargeTextureManager::queueIterator CGUILargeTextureManager::StartQueuedImage(','if (it->first == 0)','if (false)','rejected immediate/deferred/promoted decoder becomes load failure, not stranded'),
      ('cancel-job-zero',LARGE,'void CGUILargeTextureManager::ReleaseImage(','          if (it->first != 0)\n','', 'released deferred request never calls CancelJob zero'),
      ('quarter-screen',TEXTURE,'bool IsBackgroundSized(','return 2 * screenWidth','return 4 * screenWidth','image short of half screen is not background'),
      ('full-screen-only',TEXTURE,'bool IsBackgroundSized(','return 2 * screenWidth','return 1 * screenWidth','half-screen image is a background')]
    for name,path,signature,old,new,witness in definitions:yield name,edit(files,path,signature,old,new),witness,path

def callsites(files):
    alloc=DEMAND.EXTRACT.function(files[TEXTURE],'bool CGUITexture::AllocResources()')
    assert re.search(r'GetImage\(\s*m_info\.filename, texture, !IsAllocated\(\), m_use_cache,\s*!IsAllocated\(\) && IsBackgroundSized\(m_posX, m_posY, m_width, m_height\)\)',alloc)
    process=DEMAND.EXTRACT.function(files[WINDOWS],'void CGUIWindowManager::Process(')
    assert process.index('window->DoProcess(currentTime, m_dirtyregions);\n  }')<process.index('GetLargeTextureManager().ProcessDeferred();')<process.index('AssignDepth')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output','--out',dest='out',type=Path);parser.add_argument('--sanitize',action='store_true');args=parser.parse_args()
    out=args.out or Path(tempfile.mkdtemp(prefix='gui-background-defer-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in (*DEMAND.FILES,TEXTURE,WINDOWS)};callsites(files);records=[]
    variants=[('manager',files,None,LARGE),('sizes',files,None,TEXTURE),*mutations(files)]
    for name,selected,witness,path in variants:
        cpp=out/(name+'.cpp');binary=out/name;cpp.write_text(manager_source(selected) if path==LARGE else size_source(selected))
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
