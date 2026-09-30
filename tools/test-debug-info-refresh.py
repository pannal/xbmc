#!/usr/bin/env python3
"""Production debug-info event refresh/lifecycle with modeled ASS, draw and time."""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
function=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
PRELUDE=r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
long long tick=10000;
auto FakeNow(){return std::chrono::steady_clock::time_point(std::chrono::milliseconds(tick));}
constexpr int NO_SUBTITLE_ID=-1,LOGERROR=1;
struct CLog{template<class... T>static void Log(T&&...){} };
int adds=0,flushes=0,draws=0;bool failInit=false,failAdd=false;
std::vector<std::string> events;
struct CSubtitlesAdapter {
 bool Initialize(){return !failInit;}
 std::shared_ptr<int> CreateOverlay(){return std::make_shared<int>(1);}
 void FlushSubtitles(){++flushes;events.clear();}
 int AddSubtitle(std::string& text,double begin,double end){
  assert(begin==0. && end==5000000.);++adds;
  if(text.empty())return NO_SUBTITLE_ID;
  events.push_back(text);text+="processed";return failAdd?NO_SUBTITLE_ID:1;
 }
};
struct CRect{};
struct OverlayRenderer{
 void Flush(){}
 void AddOverlay(std::shared_ptr<int>,double,int){}
 void SetVideoRect(CRect&,CRect&,CRect&){}
 void Render(int){++draws;}
};
struct DEBUG_INFO_PLAYER {std::string audio="audio",video="video",player="player",vsync="vsync";};
struct DEBUG_INFO_VIDEO {std::string videoSource="source",metaPrim="prim",metaLight="light",shader="shader",render="render";};
struct DEBUG_INFO_RENDER {std::string renderFlags="flags",videoOutput="output";};
struct CDebugRenderer{
 std::vector<std::string> m_cachedLines;
 std::chrono::steady_clock::time_point m_nextInfoUpdate{};
 CSubtitlesAdapter* m_adapter=nullptr;std::atomic_bool m_isInitialized{false};
 std::shared_ptr<int> m_overlay;OverlayRenderer m_overlayRenderer;
 void Initialize();void Dispose();void Flush();void Render(CRect&,CRect&,CRect&);
 void SetInfo(DEBUG_INFO_PLAYER&);void SetInfo(DEBUG_INFO_VIDEO&,DEBUG_INFO_RENDER&);
 void SetInfo(std::vector<std::string>);
};
'''
TESTS=r'''
void retained_draw_and_changes(){
 tick=10000;adds=flushes=draws=0;CDebugRenderer debug;debug.Initialize();DEBUG_INFO_PLAYER p;
 CRect src,dst,view;debug.SetInfo(p);assert(adds==4&&flushes==1);
 for(int i=0;i<1000;++i){debug.SetInfo(p);debug.Render(src,dst,view);}
 assert(adds==4&&flushes==1&&draws==1000);
 tick=10100;debug.SetInfo(p);assert(adds==4); // original text cached before mutable PostProcess
 tick=10101;p.audio="new";debug.SetInfo(p);assert(adds==4);
 tick=10199;p.audio="latest";debug.SetInfo(p);assert(adds==4);
 tick=10200;debug.SetInfo(p);assert(adds==8&&events.front()=="latest");
 tick=10300;DEBUG_INFO_VIDEO v;DEBUG_INFO_RENDER r;debug.SetInfo(v,r);assert(events.size()==7&&adds==15);
 debug.Flush();assert(events.empty());debug.SetInfo(v,r);assert(events.size()==7&&adds==22);
 debug.Dispose();assert(!debug.m_isInitialized&&debug.m_cachedLines.empty());
 debug.SetInfo(p);assert(adds==22);debug.Initialize();debug.SetInfo(p);assert(adds==26&&events.size()==4);
 debug.Dispose();
}
void empty_lines(){
 tick=22000;adds=flushes=0;CDebugRenderer debug;debug.Initialize();
 DEBUG_INFO_VIDEO v;v.videoSource=v.metaPrim=v.metaLight=v.shader=v.render="";
 DEBUG_INFO_RENDER r;debug.SetInfo(v,r);assert(adds==2&&flushes==1&&events.size()==2);
 tick=22100;debug.SetInfo(v,r);assert(adds==2&&flushes==1);
 DEBUG_INFO_PLAYER p;p.audio=p.video=p.player=p.vsync="";
 tick=22200;debug.SetInfo(p);assert(events.empty()&&adds==2&&flushes==2);
 tick=22300;debug.SetInfo(p);assert(flushes==2);
 debug.Dispose();
}
void frequent_changes_and_failed_add(){
 tick=20000;adds=flushes=0;CDebugRenderer debug;debug.Initialize();DEBUG_INFO_PLAYER p;
 for(int i=0;i<1000;++i){tick=20000+i;p.video=std::to_string(i);debug.SetInfo(p);}
 assert(flushes==10&&adds==40); // queries/draw not throttled by this helper
 tick=21000;failAdd=true;debug.SetInfo(p);assert(debug.m_cachedLines.empty());
 failAdd=false;tick=21100;debug.SetInfo(p);assert(!debug.m_cachedLines.empty());
 debug.Dispose();failInit=true;debug.Initialize();assert(!debug.m_isInitialized&&debug.m_adapter==nullptr);
 failInit=false;debug.Initialize();debug.SetInfo(p);assert(!debug.m_cachedLines.empty());debug.Dispose();
}
int main(){retained_draw_and_changes();empty_lines();frequent_changes_and_failed_add();
 std::cout<<"PASS bounded latest-text refresh, unchanged ASS reuse, retained draw, mode/flush/reopen and failed adapter recovery\n";}
'''
def harness():
    path='xbmc/cores/VideoPlayer/VideoRenderers/DebugRenderer.cpp';source=(ROOT/path).read_text()
    methods='\n'.join(function(source,s) for s in ['void CDebugRenderer::Initialize()', 'void CDebugRenderer::Dispose()',
       'void CDebugRenderer::SetInfo(DEBUG_INFO_PLAYER& info)', 'void CDebugRenderer::SetInfo(DEBUG_INFO_VIDEO& video, DEBUG_INFO_RENDER& render)',
       'void CDebugRenderer::SetInfo(std::vector<std::string> lines)', 'void CDebugRenderer::Render(', 'void CDebugRenderer::Flush()'])
    return PRELUDE+methods.replace('std::chrono::steady_clock::now()', 'FakeNow()')+TESTS

def run(source,negative=False):
    with tempfile.TemporaryDirectory(prefix='debug-info-') as temp:
        out=Path(temp);(out/'test.cpp').write_text(source)
        cmd=[os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-fno-pie','-no-pie',str(out/'test.cpp'),'-o',str(out/'test')]
        if not negative:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
        subprocess.run(cmd,check=True)
        result=subprocess.run([str(out/'test')],text=True,capture_output=negative,check=not negative,timeout=20)
        if negative:assert result.returncode!=0 and 'Assertion' in result.stderr,result.stderr

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args();source=harness()
    if args.negative_controls:
        controls=[('remove refresh deadline','if (now < m_nextInfoUpdate)','if (false)'),
                  ('replace identical ASS events','if (lines == m_cachedLines)','if (false)'),
                  ('cache mutated text','for (auto line : lines)','for (auto& line : lines)'),
                  ('retain flushed text cache','void CDebugRenderer::Flush()\n{\n  m_cachedLines.clear();','void CDebugRenderer::Flush()\n{'),
                  ('cache failed additions','if (added)','if (added || !added)'),
                  ('treat empty text as failed event','!line.empty() && m_adapter->AddSubtitle','m_adapter->AddSubtitle'),
                  ('skip retained draw','m_overlayRenderer.Render(0);','/* omitted */')]
        for name,before,after in controls:
            assert before in source;run(source.replace(before,after,1),True);print('Rejected runtime negative control:',name)
    else:run(source);print('Debug info refresh: PASS (production methods; ASan/UBSan; modeled ASS/draw/time)')
if __name__=='__main__':main()
