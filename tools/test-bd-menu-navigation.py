#!/usr/bin/env python3
"""Exercise BD navigation/overlay logic with ASan and UBSan.

Uses source-extracted production functions with real Kodi image and geometry
classes. Supply the include directory from the paired libbluray 1.5 installation:
  python3 tools/test-bd-menu-navigation.py --libbluray-include /path/usr/include
This is a host behavioral check; it does not emulate libbluray or a device.
"""
import argparse
from pathlib import Path
import os
import shlex
import subprocess
import tempfile

import re

def masked(s):
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',lambda m:re.sub(r'[^\n]',' ',m.group()),s)

def balance(s,start,opening='{',closing='}'):
    m=masked(s);depth=0
    for i in range(start,len(s)):
        if m[i]==opening:depth+=1
        elif m[i]==closing:
            depth-=1
            if depth==0:return i+1
    raise ValueError('unbalanced')

def span(s,name):
    hit=re.search(r'(?m)^[^\n;{}]*\b'+re.escape(name)+r'\s*\(',s)
    if not hit:raise ValueError('missing '+name)
    start=hit.start();body=masked(s).find('{',hit.end());return start,balance(s,body)

def get(s,name):
    a,b=span(s,name);return s[a:b]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--libbluray-include", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
workspace = tempfile.TemporaryDirectory(prefix="kodi-bd-menu-test-")
out = Path(workspace.name)
s = (root / "xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamBluray.cpp").read_text()
(out / "test-include").mkdir()
(out / "test-include/PlatformDefs.h").write_text("#pragma once\n")
preamble=r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <list>
#include <limits>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
#include <iostream>
#include <libbluray/bluray.h>
#include <libbluray/overlay.h>
#include "cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h"
#include "utils/Geometry.h"
#define HAVE_LIBBLURAY_BDJ 1
constexpr int LOGDEBUG=0, LOGWARNING=1, BD_EVENT_MENU_OVERLAY=1000;
struct CLog {template<class...T>static void Log(T&&...) {}};
constexpr int64_t END_OF_TITLE_SPIN_TIMEOUT_MS=5000;
static uint32_t build_rgba(const BD_PG_PALETTE_ENTRY& e,bool bt2020) { return e.Y+(bt2020?1000u:0u); }
bool g_pgsHdrToSdr=true;
#define PIXEL_ASHIFT 24
#define PIXEL_RSHIFT 16
#define PIXEL_GSHIFT 8
#define PIXEL_BSHIFT 0
namespace real {
@@REAL_PALETTE@@
}
struct Player {
 std::shared_ptr<CDVDOverlay> last;
 void OnDiscNavResult(void* p,int) { last=*static_cast<std::shared_ptr<CDVDOverlay>*>(p); }
};
struct FakeStream {
 std::vector<int> reads;size_t pos=0;bool clamp=false;
 int64_t Seek(int64_t v,int) {return clamp ? 0:v;}
 int Read(uint8_t* p,int size){int n=pos<reads.size()?reads[pos++]:0;if(n>0&&n<=size)memset(p,7,n);return n;}
};
class CDVDInputStreamBluray {
public:
 using SOverlay=std::shared_ptr<CDVDOverlayImage>;
 using SOverlays=std::list<SOverlay>;
 struct SPlane {SOverlays o;int w=0,h=0;};
 std::recursive_mutex m_overlayLock;
 SPlane m_planes[2];Player* m_player;
 std::atomic_bool m_atTitleEnd=false;bool m_menu=false,m_naturalChainBoundary=false,m_crossPlaylistPending=false,m_bMVCPlayback=false;
 bool m_hasMenuOverlay=false,m_hasOverlay=false,m_overlayCloseDeferred=false;
 std::atomic_bool m_pqAuthoredGraphics=false;
 bool TagGraphicsAsPq() const;
 std::shared_ptr<CDVDOverlay> m_pendingOverlayGroup;
 std::atomic<std::thread::id> m_readingThread{};
 int m_lastReadEvent=BD_EVENT_NONE;
 enum EHold {HOLD_NONE,HOLD_STILL};int m_hold=HOLD_NONE;
 bool IsInMenu(){return m_menu;}
 bool IsNaturalChainBoundaryInFlight()const{return !m_bMVCPlayback&&(m_naturalChainBoundary||m_crossPlaylistPending||m_atTitleEnd);}
 void OverlayClose(bool deferrable=false,int closingPlane=-1);
 static void OverlayInit(SPlane&,int,int);
 static void OverlayClear(SPlane&,int,int,int,int);
 void OverlayFlush(int64_t);
 void DeliverParkedOverlayIfDue();
 void OverlayCallback(const BD_OVERLAY*);
 void OverlayCallbackARGB(const BD_ARGB_OVERLAY*);
 std::mutex m_seamOffsetMutex;int m_seamGeneration=0;double m_seamTimeOffset=0,m_seamTimeOffsetPrev=0;bool m_seamlessPlayItem=false;
 void UpdateSeamTimeOffset(uint64_t,uint64_t);void ResetSeamTimeOffset(const char*);
 static bool AreClipVideoStreamsCompatible(const BLURAY_CLIP_INFO*,const BLURAY_CLIP_INFO*);
 static bool AreClipPgStreamsEqual(const BLURAY_CLIP_INFO*,const BLURAY_CLIP_INFO*);
 bool IsClipCodecCompatible(const BLURAY_CLIP_INFO*,const BLURAY_CLIP_INFO*)const;
 void Reenter(uint64_t previousOut,uint64_t nextIn);
 std::atomic_bool m_aborted=false;std::mutex m_readBlocksLock;std::unique_ptr<FakeStream>m_pstream;
 int ReadBlocks(uint8_t*,int,int);
};
'''
real_palette=get(s,'clamp')+'\n'+get(s,'build_rgba')
tag=get(s,'CDVDInputStreamBluray::TagGraphicsAsPq')
tag=re.sub(r'CServiceBroker::GetSettingsComponent\(\)->GetSettings\(\)->GetBool\(\s*CSettings::SETTING_SUBTITLES_PGSHDRTOSDR\)','g_pgsHdrToSdr',tag)
assert 'g_pgsHdrToSdr' in tag
functions=[tag,get(s,'EndOfTitleReadStalled')]+[get(s,'CDVDInputStreamBluray::'+name) for name in ['OverlayClose','OverlayInit','OverlayClear','OverlayFlush','DeliverParkedOverlayIfDue','OverlayCallback','OverlayCallbackARGB','ReadBlocks','UpdateSeamTimeOffset','ResetSeamTimeOffset','AreClipVideoStreamsCompatible','AreClipPgStreamsEqual','IsClipCodecCompatible']]
a=s.index('if (m_atTitleEnd.exchange(false))');b=balance(s,s.index('{',a))
functions.append('void CDVDInputStreamBluray::Reenter(uint64_t previousOut,uint64_t nextIn) {'+s[a:b]+'}')
tests=r'''
int main(){
 { // Real palette conversion: the SDR matrix is unchanged; BT.2020 matches ffmpeg-003's PGS path.
   auto px=[](uint32_t v,int shift){return int((v>>shift)&0xff);};
   BD_PG_PALETTE_ENTRY white{235,128,128,255},black{16,128,128,255},red{};red.Y=64;red.Cb=100;red.Cr=200;red.T=128;
   for(bool bt2020:{false,true}){
     assert(real::build_rgba(white,bt2020)==0xffffffffu&&real::build_rgba(black,bt2020)==0xff000000u);
   }
   uint32_t sdr=real::build_rgba(red,false),hdr=real::build_rgba(red,true);
   assert(px(sdr,24)==128&&px(sdr,16)==171&&px(sdr,8)==8&&px(sdr,0)==0); // BT.601 as before
   assert(px(hdr,24)==128&&px(hdr,16)==177&&px(hdr,8)==14&&px(hdr,0)==0); // BT.2020 NCL
 }
 using Clock=std::chrono::steady_clock;using namespace std::chrono_literals;
 auto origin=Clock::time_point{}+1s,since=Clock::time_point{};
 assert(!EndOfTitleReadStalled(BD_EVENT_END_OF_TITLE,0,false,false,origin,since));
 for(int i=1;i<5000;++i) assert(!EndOfTitleReadStalled(i%2?BD_EVENT_NONE:BD_EVENT_END_OF_TITLE,0,false,false,origin+std::chrono::milliseconds(i),since));
 assert(EndOfTitleReadStalled(BD_EVENT_NONE,0,false,false,origin+5s,since));
 assert(!EndOfTitleReadStalled(BD_EVENT_IDLE,0,false,false,origin+100s,since));
 assert(!EndOfTitleReadStalled(BD_EVENT_NONE,0,true,false,origin+200s,since));
 assert(!EndOfTitleReadStalled(BD_EVENT_END_OF_TITLE,0,false,false,origin+201s,since));
 assert(!EndOfTitleReadStalled(BD_EVENT_PLAYITEM,0,false,true,origin+202s,since));
 assert(!EndOfTitleReadStalled(BD_EVENT_NONE,1,false,false,origin+1000s,since));
 // A repeated announcement without actual navigation progress cannot reset it.
 assert(!EndOfTitleReadStalled(BD_EVENT_END_OF_TITLE,0,false,false,origin+1001s,since));
 assert(EndOfTitleReadStalled(BD_EVENT_PLAYLIST,0,false,false,origin+1006s,since));
 Player player;CDVDInputStreamBluray b;b.m_player=&player;
 BD_OVERLAY ov{};ov.plane=BD_OVERLAY_IG;ov.cmd=BD_OVERLAY_INIT;ov.w=4;ov.h=2;b.OverlayCallback(&ov);
 BD_PG_PALETTE_ENTRY pal[256]{};pal[1].Y=21;pal[2].Y=22;
 BD_PG_RLE_ELEM runs[]={{4,1},{0,0},{4,2}};
 ov.cmd=BD_OVERLAY_DRAW;ov.palette=pal;ov.img=runs;b.OverlayCallback(&ov);
 assert(b.m_planes[1].o.size()==1);auto image=b.m_planes[1].o.front();
 assert(image->IsDiscMenuOverlay()&&image->pixels==std::vector<uint8_t>({1,1,1,1,2,2,2,2}));
 assert(!image->m_isHdrPq); // SDR disc graphics keep the SDR path
 ov.cmd=BD_OVERLAY_FLUSH;b.OverlayCallback(&ov);assert(player.last&&player.last->IsDiscMenuOverlay());
 // Palette-only DRAW is valid with no rectangle and cannot mutate an in-flight image.
 pal[1].Y=99;ov.cmd=BD_OVERLAY_DRAW;ov.palette_update_flag=1;ov.w=ov.h=0;ov.img=nullptr;b.OverlayCallback(&ov);
 assert(b.m_planes[1].o.front()!=image&&image->palette[1]==21&&b.m_planes[1].o.front()->palette[1]==99);
 ov.palette_update_flag=0;ov.w=4;ov.h=2;ov.img=runs;
 BD_PG_RLE_ELEM malformed[]={{4,1},{0,0},{0,0},{4,2}};ov.img=malformed;
 auto valid=b.m_planes[1].o.front();b.OverlayCallback(&ov);assert(b.m_planes[1].o.front()==valid);
 BD_PG_RLE_ELEM overflow[]={{9,1}};ov.img=overflow;b.OverlayCallback(&ov);assert(b.m_planes[1].o.front()==valid);
 // PQ-authored IG: tagged, and its palette converted with the BT.2020 matrix to match.
 b.m_pqAuthoredGraphics=true;ov.img=runs;b.OverlayCallback(&ov);
 auto tagged=b.m_planes[1].o.front();
 assert(tagged!=valid&&tagged->m_isHdrPq&&tagged->palette[1]==1099);
 // A palette-only update keeps each image on the matrix its tag was drawn with.
 ov.palette_update_flag=1;ov.w=ov.h=0;ov.img=nullptr;b.OverlayCallback(&ov);
 assert(b.m_planes[1].o.front()->m_isHdrPq&&b.m_planes[1].o.front()->palette[1]==1099);
 ov.palette_update_flag=0;ov.w=4;ov.h=2;ov.img=runs;
 // The shared PGS HDR switch off: no IG tagging, untagged matrix.
 g_pgsHdrToSdr=false;b.OverlayCallback(&ov);
 assert(!b.m_planes[1].o.front()->m_isHdrPq&&b.m_planes[1].o.front()->palette[1]==99);
 g_pgsHdrToSdr=true;b.m_pqAuthoredGraphics=false;
 // PG children retain subtitle identity even in a disc-composition envelope.
 ov.plane=BD_OVERLAY_PG;ov.cmd=BD_OVERLAY_INIT;b.OverlayCallback(&ov);ov.cmd=BD_OVERLAY_DRAW;ov.img=runs;b.OverlayCallback(&ov);
 assert(!b.m_planes[0].o.front()->IsDiscMenuOverlay());
 // PG is never tagged, even in a PQ regime: it stays on its own path and matrix.
 b.m_pqAuthoredGraphics=true;b.OverlayCallback(&ov);
 assert(!b.m_planes[0].o.front()->m_isHdrPq&&b.m_planes[0].o.front()->palette[1]<1000);
 b.m_pqAuthoredGraphics=false;
 ov.cmd=BD_OVERLAY_CLOSE;b.OverlayCallback(&ov);assert(b.m_planes[0].o.empty()&&!b.m_planes[1].o.empty());
 // The last dirty row ends at the canvas allocation; a stride*h memcpy overreads.
 BD_ARGB_OVERLAY argb{};argb.plane=1;argb.cmd=BD_ARGB_OVERLAY_INIT;argb.w=4;argb.h=2;b.OverlayCallbackARGB(&argb);
 auto canvas=std::make_unique<uint32_t[]>(8);for(int i=0;i<8;++i)canvas[i]=i+1;
 argb.cmd=BD_ARGB_OVERLAY_DRAW;argb.x=2;argb.w=2;argb.stride=4;argb.argb=canvas.get()+2;b.OverlayCallbackARGB(&argb);
 auto rgba=b.m_planes[1].o.front();assert(rgba->linesize==8&&rgba->pixels.size()==16&&!rgba->m_isHdrPq);
 // BD-J graphics stay untagged even in a PQ regime until the Xlet's graphics range is known.
 b.m_pqAuthoredGraphics=true;b.OverlayCallbackARGB(&argb);assert(!b.m_planes[1].o.front()->m_isHdrPq);
 b.m_pqAuthoredGraphics=false;
 b.OverlayCallbackARGB(&argb);rgba=b.m_planes[1].o.front();
 uint32_t copied[4];memcpy(copied,rgba->pixels.data(),16);assert(copied[0]==3&&copied[1]==4&&copied[2]==7&&copied[3]==8);
 b.m_readingThread=std::this_thread::get_id();b.OverlayFlush(-1);assert(b.m_pendingOverlayGroup);
 b.m_atTitleEnd=true;b.DeliverParkedOverlayIfDue();assert(b.m_pendingOverlayGroup);
 b.m_atTitleEnd=false;b.DeliverParkedOverlayIfDue();assert(!b.m_pendingOverlayGroup);
 b.m_readingThread=std::thread::id{};b.OverlayClose();assert(!b.m_hasOverlay&&!b.m_hasMenuOverlay);
 // Exercise actual offset producer and source-extracted duplicate-reentry gate.
 for(int i=1;i<=3;++i){b.m_atTitleEnd=true;b.Reenter(135000,90000);assert(b.m_seamGeneration==i&&b.m_seamTimeOffset==i*0.5);b.Reenter(135000,90000);assert(b.m_seamGeneration==i&&b.m_seamTimeOffset==i*0.5);}
 b.UpdateSeamTimeOffset(180000,90000);assert(b.m_seamTimeOffset==2.5&&b.m_seamTimeOffsetPrev==1.5);
 b.UpdateSeamTimeOffset(90000,90000);assert(b.m_seamGeneration==4);
 b.ResetSeamTimeOffset("seek");assert(b.m_seamTimeOffset==0&&b.m_seamTimeOffsetPrev==0&&b.m_seamGeneration==5);
 BLURAY_CLIP_INFO ca{},cb{};BLURAY_STREAM_INFO va{},vb{},aa{},ab{};ca.video_stream_count=cb.video_stream_count=1;ca.audio_stream_count=cb.audio_stream_count=1;ca.video_streams=&va;cb.video_streams=&vb;ca.audio_streams=&aa;cb.audio_streams=&ab;
 assert(b.IsClipCodecCompatible(&ca,&cb));vb.dynamic_range_type=1;assert(!b.IsClipCodecCompatible(&ca,&cb));vb.dynamic_range_type=0;vb.color_space=1;assert(!b.IsClipCodecCompatible(&ca,&cb));vb.color_space=0;ab.pid=2;assert(!b.IsClipCodecCompatible(&ca,&cb));ab.pid=0;cb.pg_stream_count=1;assert(!b.IsClipCodecCompatible(&ca,&cb));
 uint8_t block[4096];b.m_pstream=std::make_unique<FakeStream>();b.m_pstream->reads={100,1948,2048};assert(b.ReadBlocks(block,1,2)==2);
 b.m_pstream->pos=0;b.m_pstream->reads={-1};assert(b.ReadBlocks(block,1,1)==-1);
 b.m_pstream->pos=0;b.m_pstream->reads={100,0};assert(b.ReadBlocks(block,1,1)==0);
 b.m_pstream->clamp=true;assert(b.ReadBlocks(block,1,1)==-1);
 b.m_aborted=true;assert(b.ReadBlocks(block,1,1)==-1);
 std::cout<<"BD-menu watchdog, short-loop seams, stream compatibility, graphics ownership/bounds and I/O: PASS\n";
}
'''
source=out/'navigation-behavior.cpp';source.write_text(preamble.replace('@@REAL_PALETTE@@',real_palette)+'\n'.join(functions)+tests)
cmd=shlex.split(os.environ.get('CXX', 'g++')) + ['-std=c++17','-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(out/'test-include'),'-I'+str(root/'xbmc'),'-I'+str(args.libbluray_include.resolve()),str(source),'-o',str(out/'navigation-behavior')]
result=subprocess.run(cmd,capture_output=True,text=True);(out/'navigation-behavior-build.log').write_text(result.stdout+result.stderr);assert result.returncode==0,result.stderr
result=subprocess.run([str(out/'navigation-behavior')],capture_output=True,text=True);(out/'navigation-behavior.log').write_text(result.stdout+result.stderr);print(result.stdout+result.stderr);assert result.returncode==0
