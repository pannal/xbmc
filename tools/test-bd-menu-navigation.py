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
#include <queue>
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
constexpr uint32_t MAX_PLAYLIST_ID=99999;
#define EMPTY_QUEUE(x) { while(!x.empty()) x.pop(); }
// Title tables are heap objects here, so ASan reports a double free or a leak.
static int liveTitles=0;
static BLURAY_TITLE_INFO* NewTitle(uint32_t playlist,uint32_t clips) {
 auto* t=new BLURAY_TITLE_INFO{};t->playlist=playlist;t->clip_count=clips;t->clips=new BLURAY_CLIP_INFO[clips]{};
 auto* v=new BLURAY_STREAM_INFO[clips]{};auto* a=new BLURAY_STREAM_INFO[clips]{};
 for(uint32_t i=0;i<clips;++i){t->clips[i].video_stream_count=1;t->clips[i].video_streams=&v[i];t->clips[i].audio_stream_count=1;t->clips[i].audio_streams=&a[i];t->clips[i].in_time=i*1000;t->clips[i].out_time=i*1000+900;a[i].pid=0x1100+playlist%16;v[i].coding_type=playlist>=900?0x24:0x1b;}
 ++liveTitles;return t;
}
void bd_free_title_info(BLURAY_TITLE_INFO* t) {delete[] t->clips[0].video_streams;delete[] t->clips[0].audio_streams;delete[] t->clips;delete t;--liveTitles;}
static std::queue<BD_EVENT> queuedEvents;
int bd_get_event(BLURAY*,BD_EVENT* e) {if(queuedEvents.empty())return 0;*e=queuedEvents.front();queuedEvents.pop();return 1;}
struct CDVDInputStream {enum ENextStream {NEXTSTREAM_NONE,NEXTSTREAM_OPEN,NEXTSTREAM_RETRY};};
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
class CDVDInputStreamBluray : public CDVDInputStream {
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
 void UpdateGraphicsRegime();
 std::shared_ptr<CDVDOverlay> m_pendingOverlayGroup;
 std::atomic<std::thread::id> m_readingThread{};
 int m_lastReadEvent=BD_EVENT_NONE;
 enum EHold {HOLD_NONE,HOLD_HELD,HOLD_DATA,HOLD_STILL,HOLD_ERROR,HOLD_EXIT};int m_hold=HOLD_NONE;
 // Clip-table ownership across title, playlist and reopen boundaries.
 BLURAY* m_bd=nullptr;std::atomic_bool m_navmode=true;std::mutex m_clipTableMutex;
 BLURAY_TITLE_INFO* m_titleInfo=nullptr;BLURAY_CLIP_INFO* m_clip=nullptr;BLURAY_CLIP_INFO* m_nMVCClip=nullptr; // header types
 uint64_t m_titleGeneration=0;std::queue<int> m_clipQueue;uint32_t m_playlist=MAX_PLAYLIST_ID+1,m_angle=0,m_titleNumber=0;
 BLURAY_TITLE_INFO* m_prevTitleInfo=nullptr;const BLURAY_CLIP_INFO* m_prevClip=nullptr;uint32_t m_prevPlaylist=MAX_PLAYLIST_ID+1;
 int m_restoredBoundaryClip=-1;bool m_prevWasMVC=false,m_prevFlipEyes=false,m_prevTitleOnly=false,m_wrapSeekExempt=false,m_videoCompatBoundary=false;
 std::atomic_bool m_bFlipEyes=false,m_discontinuityFlush=false;BD_EVENT m_event{};
 void ReplaceTitleInfo(BLURAY_TITLE_INFO*);void FreePrevTitleInfo();void StashBoundaryClip(bool titleOnly=false);bool RestoreTitleOnlyStash();void RestoreTitleOnlyStashForEvent();int lastAudioPid=0;
 ENextStream NextStream();int HoldGate(int result);void DataRead();
 // Stand-in for ProcessEvent(): each case repeats what the production case does to the
 // clip table (the script checks those cases in the source).
 void ProcessEvent() {
  RestoreTitleOnlyStashForEvent(); // production helper, called first as in the real ProcessEvent()
  const uint32_t v=m_event.param;
  switch(m_event.event){
   case BD_EVENT_AUDIO_STREAM:{std::lock_guard l(m_clipTableMutex);lastAudioPid=(m_titleInfo&&m_clip&&m_clip->audio_stream_count>v-1)?m_clip->audio_streams[v-1].pid:-1;break;}
   case BD_EVENT_TITLE:m_titleNumber=v;break;
   case BD_EVENT_PLAYLIST:{bool re;{std::lock_guard l(m_clipTableMutex);re=v==m_playlist&&m_titleInfo;}if(!re){m_playlist=v;ReplaceTitleInfo(NewTitle(v,4));}break;}
   case BD_EVENT_PLAYLIST_STOP:ReplaceTitleInfo(nullptr);break;
   case BD_EVENT_ANGLE:{bool re;{std::lock_guard l(m_clipTableMutex);re=v==m_angle&&m_titleInfo;}m_angle=v;if(!re&&m_playlist<=MAX_PLAYLIST_ID)ReplaceTitleInfo(NewTitle(m_playlist,4));break;}
   case BD_EVENT_PLAYITEM:{std::lock_guard l(m_clipTableMutex);m_clip=(m_titleInfo&&v<m_titleInfo->clip_count)?&m_titleInfo->clips[v]:nullptr;break;}
   default:break;
  }
  m_event.event=BD_EVENT_NONE;
 }
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
functions+=[get(s,'CDVDInputStreamBluray::'+name) for name in ['ReplaceTitleInfo','UpdateGraphicsRegime','FreePrevTitleInfo','StashBoundaryClip','RestoreTitleOnlyStash','RestoreTitleOnlyStashForEvent','NextStream']]
# The hold gate and the data-received step of Read(), verbatim.
a=s.index('      /* Check for holding events */');g=s.index('switch(m_event.event)',a);g=s[g:balance(s,s.index('{',g))]
functions.append('int CDVDInputStreamBluray::HoldGate(int result) {'+g+'\n return -1000;}')
a=s.index('      if(result > 0)',a);functions.append('void CDVDInputStreamBluray::DataRead() {int result=1;'+s[a:balance(s,s.index('{',a))]+'}')
# The ProcessEvent() stand-in must match what each production case does to the clip table.
pe=get(s,'CDVDInputStreamBluray::ProcessEvent')
def case(name):
    i=pe.index('  case '+name+':');j=pe.index('\n  case ',i+1);return pe[i:j]
for name,must,never in [('BD_EVENT_TITLE',[],['ReplaceTitleInfo','StashBoundaryClip','FreePrevTitleInfo','m_titleInfo =']),
                        ('BD_EVENT_SEEK',[],['ReplaceTitleInfo','StashBoundaryClip','FreePrevTitleInfo']),
                        ('BD_EVENT_PLAYLIST_STOP',['ReplaceTitleInfo(nullptr)'],[]),
                        ('BD_EVENT_ANGLE',['angleReannounce = m_event.param == m_angle && m_titleInfo','ReplaceTitleInfo(bd_get_playlist_info(m_bd, m_playlist, m_angle))'],[]),
                        ('BD_EVENT_PLAYLIST',['playlistReannounce = m_event.param == m_playlist && m_titleInfo','m_playlist = m_event.param;','ProcessItem(m_playlist)'],[])]:
    body=case(name)
    for m in must: assert m in body,(name,m)
    for m in never: assert m not in body,(name,m)
assert 'ReplaceTitleInfo(bd_get_playlist_info(m_bd, playitem, m_angle))' in get(s,'CDVDInputStreamBluray::ProcessItem')
# The restore trigger runs before the event switch, and the stream cases read the table.
assert re.search(r'ProcessEvent\(\) \{\s*RestoreTitleOnlyStashForEvent\(\);\s*int pid = -1, ret;\s*switch \(m_event.event\)',pe)
assert 'pid = m_clip->audio_streams[m_event.param - 1].pid;' in case('BD_EVENT_AUDIO_STREAM')
assert 'pid = m_clip->pg_streams[m_event.param - 1].pid;' in case('BD_EVENT_PG_TEXTST_STREAM')
assert 'm_clip = (m_titleInfo && m_event.param < m_titleInfo->clip_count)' in case('BD_EVENT_PLAYITEM')
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
 // Title metadata lifetime (M3GAN 2.0, #118): Top Menu changes the title while playlist
 // 801 plays on. The title change holds and the player reopens through NextStream();
 // data follows, then later play items must continue on the kept clip table.
 {CDVDInputStreamBluray m;
  auto open=[&](uint32_t playlist){m.m_event={BD_EVENT_PLAYLIST,playlist};m.ProcessEvent();m.m_event={BD_EVENT_PLAYITEM,0};m.ProcessEvent();m.m_hold=m.HOLD_NONE;};
  auto titleChange=[&](std::vector<BD_EVENT> queued){ // Read() gate, then the reopen
   m.m_event={BD_EVENT_TITLE,0};assert(m.HoldGate(0)==0&&m.m_hold==m.HOLD_HELD&&!m.m_titleInfo);
   for(auto e:queued)queuedEvents.push(e);
   assert(m.NextStream()==m.NEXTSTREAM_OPEN&&m.m_hold==m.HOLD_DATA&&queuedEvents.empty());
   m.DataRead();assert(m.m_hold==m.HOLD_NONE);
  };
  auto playItem=[&](uint32_t item){m.m_event={BD_EVENT_PLAYITEM,item};int r=m.HoldGate(0);if(r==-1000&&m.m_event.event!=BD_EVENT_NONE)m.ProcessEvent();return r;};
  open(801);auto* table=m.m_titleInfo;
  titleChange({{BD_EVENT_TITLE,0}});
  assert(m.m_titleInfo==table&&m.m_clip==&table->clips[0]&&!m.m_prevTitleInfo&&liveTitles==1);
  assert(playItem(1)==-1000&&m.m_seamlessPlayItem&&m.m_clip==&table->clips[1]);
  titleChange({}); // repeated TITLE 0 (12:32:05 in the log)
  assert(m.m_titleInfo==table&&m.m_clip==&table->clips[1]);
  m.m_seamlessPlayItem=false;assert(playItem(2)==-1000&&m.m_seamlessPlayItem);
  m.m_seamlessPlayItem=false;assert(playItem(3)==-1000&&m.m_seamlessPlayItem&&liveTitles==1);
  // An out-of-range play item still tears down, without touching memory past the table.
  assert(playItem(9)==0&&m.m_hold==m.HOLD_HELD);m.m_hold=m.HOLD_NONE;
  // A seek stays inside the playlist (libbluray _seek_internal), so the table is kept.
  m.m_event={BD_EVENT_PLAYITEM,0};m.ProcessEvent();
  titleChange({{BD_EVENT_SEEK,0}});assert(m.m_titleInfo==table&&liveTitles==1);
  // A queued playlist change installs its own table; the stash is freed.
  titleChange({{BD_EVENT_PLAYLIST,802},{BD_EVENT_PLAYITEM,0}});
  assert(m.m_titleInfo!=table&&m.m_titleInfo->playlist==802&&m.m_playlist==802&&!m.m_prevTitleInfo&&liveTitles==1);
  // A playlist change after the reopen goes through the ordinary playlist boundary.
  table=m.m_titleInfo;titleChange({});assert(m.m_titleInfo==table);
  m.m_event={BD_EVENT_PLAYLIST,803};assert(m.HoldGate(0)==0&&!m.m_titleInfo&&!m.m_prevTitleOnly);
  assert(m.NextStream()==m.NEXTSTREAM_OPEN&&m.m_titleInfo&&m.m_titleInfo->playlist==803);
  assert(!m.m_prevTitleInfo&&liveTitles==1);m.DataRead();
  m.m_event={BD_EVENT_PLAYITEM,0};m.ProcessEvent();
  // Queued events read the kept table in order (pannal review of #68): a queued play item
  // keeps its clip, and the next boundary is measured from that clip, not the stashed one.
  {CDVDInputStreamBluray q;table=nullptr;
   q.m_event={BD_EVENT_PLAYLIST,801};q.ProcessEvent();q.m_event={BD_EVENT_PLAYITEM,0};q.ProcessEvent();q.m_hold=q.HOLD_NONE;
   auto* t801=q.m_titleInfo;q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   queuedEvents.push({BD_EVENT_PLAYITEM,2});queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo==t801&&q.m_clip==&t801->clips[2]);
   assert(q.lastAudioPid==0x1101); // resolved on the kept 801 table, not an empty one (-1)
   q.DataRead();q.m_atTitleEnd=true;
   CDVDInputStreamBluray ref;ref.UpdateSeamTimeOffset(t801->clips[2].out_time,t801->clips[3].in_time);
   q.m_event={BD_EVENT_PLAYITEM,3};assert(q.HoldGate(0)==-1000&&q.m_seamlessPlayItem);
   assert(q.m_seamTimeOffset==ref.m_seamTimeOffset); // offset from clip 2's out time
   // A stream selection queued first (no play item) also resolves on the kept table.
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);q.lastAudioPid=0;
   queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.lastAudioPid==0x1101&&q.m_clip==&t801->clips[3]);
   q.DataRead();
   // A same-number playlist or angle re-announcement keeps the kept table.
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   queuedEvents.push({BD_EVENT_PLAYLIST,801});queuedEvents.push({BD_EVENT_ANGLE,0});queuedEvents.push({BD_EVENT_PLAYITEM,1});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo==t801&&q.m_clip==&t801->clips[1]&&liveTitles==2);
   // A different playlist queued first still replaces it; later selections land on the new table.
   q.DataRead();q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   queuedEvents.push({BD_EVENT_PLAYLIST,802});queuedEvents.push({BD_EVENT_PLAYITEM,1});queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});
   q.m_videoCompatBoundary=false;
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo->playlist==802&&q.m_clip==&q.m_titleInfo->clips[1]);
   assert(q.m_videoCompatBoundary); // the stash survived for the playlist boundary check
   assert(q.lastAudioPid==0x1102&&!q.m_prevTitleInfo&&liveTitles==2);
   // pannal's ordering: a selection restores the stash early, then a different playlist
   // follows in the same queue. The boundary check still sees the old clip.
   q.DataRead();q.m_event={BD_EVENT_PLAYITEM,2};q.ProcessEvent();auto* t802=q.m_titleInfo;
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);q.m_videoCompatBoundary=false;
   queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});queuedEvents.push({BD_EVENT_PLAYLIST,803});
   queuedEvents.push({BD_EVENT_PLAYITEM,1});queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo->playlist==803&&q.m_clip==&q.m_titleInfo->clips[1]);
   assert(q.m_videoCompatBoundary&&q.lastAudioPid==0x1103&&!q.m_prevTitleInfo&&q.m_restoredBoundaryClip<0&&liveTitles==2);
   // The same with a queued play item on the old playlist first: the snapshot is still the
   // clip that was playing at the title change, and an incompatible switch says so.
   q.DataRead();q.m_event={BD_EVENT_PLAYITEM,0};q.ProcessEvent();(void)t802;
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);q.m_videoCompatBoundary=true;
   queuedEvents.push({BD_EVENT_PLAYITEM,3});queuedEvents.push({BD_EVENT_PLAYLIST,900});queuedEvents.push({BD_EVENT_PLAYITEM,0});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo->playlist==900&&!q.m_videoCompatBoundary);
   assert(!q.m_prevTitleInfo&&liveTitles==2);
   // A stop or a new angle after an early restore frees the snapshot with the reopen.
   q.DataRead();q.m_event={BD_EVENT_PLAYITEM,0};q.ProcessEvent();
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});queuedEvents.push({BD_EVENT_PLAYLIST_STOP,0});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&!q.m_titleInfo&&!q.m_prevTitleInfo&&liveTitles==1);
   q.m_event={BD_EVENT_PLAYLIST,901};q.ProcessEvent();q.m_event={BD_EVENT_PLAYITEM,0};q.ProcessEvent();q.DataRead();
   auto* t901=q.m_titleInfo;q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   queuedEvents.push({BD_EVENT_AUDIO_STREAM,1});queuedEvents.push({BD_EVENT_ANGLE,1});
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo&&q.m_titleInfo!=t901&&!q.m_prevTitleInfo&&liveTitles==2);
   // After the reopen, the snapshot marker is gone: a later stop frees the kept table.
   q.ReplaceTitleInfo(nullptr);q.FreePrevTitleInfo();assert(liveTitles==1);
   q.m_event={BD_EVENT_PLAYLIST,902};q.ProcessEvent();q.m_event={BD_EVENT_PLAYITEM,0};q.ProcessEvent();q.DataRead();
   q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
   assert(q.NextStream()==q.NEXTSTREAM_OPEN&&q.m_titleInfo&&q.m_restoredBoundaryClip<0&&liveTitles==2);
   q.m_event={BD_EVENT_PLAYLIST_STOP,0};q.ProcessEvent();assert(!q.m_titleInfo&&!q.m_prevTitleInfo&&liveTitles==1);
  }
  // A stop ends the playlist: nothing is restored, and a later play item tears down as before.
  titleChange({{BD_EVENT_PLAYLIST_STOP,0}});assert(!m.m_titleInfo&&!m.m_prevTitleInfo&&liveTitles==0);
  assert(playItem(1)==0);m.m_hold=m.HOLD_NONE;
  // An angle change reloads the table for the new angle instead.
  open(804);table=m.m_titleInfo;titleChange({{BD_EVENT_ANGLE,1}});
  assert(m.m_titleInfo&&m.m_titleInfo!=table&&m.m_angle==1&&!m.m_prevTitleInfo&&liveTitles==1);
  // MVC keeps today's reopen; a title change at title end holds nothing.
  m.m_event={BD_EVENT_PLAYITEM,0};m.ProcessEvent();m.m_bMVCPlayback=true;
  titleChange({});assert(!m.m_titleInfo&&liveTitles==0);m.m_bMVCPlayback=false;
  open(805);table=m.m_titleInfo;m.m_atTitleEnd=true;m.m_event={BD_EVENT_TITLE,1};
  assert(m.HoldGate(0)==-1000&&m.m_titleInfo==table);m.m_atTitleEnd=false;
  m.ReplaceTitleInfo(nullptr);m.FreePrevTitleInfo();assert(liveTitles==0);
 }
 // The combined HDR/menu-lifetime path keeps its regime across a title-only
 // gap, then recalculates it when a genuinely different playlist replaces it.
 for (uint8_t range : {BLURAY_DYNAMIC_RANGE_HDR10, BLURAY_DYNAMIC_RANGE_DOLBY_VISION}) {
  CDVDInputStreamBluray q;q.m_playlist=801;
  auto* hdr=NewTitle(801,4);hdr->clips[0].video_streams[0].dynamic_range_type=range;
  q.ReplaceTitleInfo(hdr);q.m_clip=&hdr->clips[0];assert(q.m_pqAuthoredGraphics);
  q.m_event={BD_EVENT_TITLE,0};assert(q.HoldGate(0)==0);
  assert(!q.m_titleInfo && q.m_pqAuthoredGraphics);
  assert(q.NextStream()==q.NEXTSTREAM_OPEN && q.m_titleInfo==hdr && q.m_pqAuthoredGraphics);
  q.ReplaceTitleInfo(NewTitle(802,4));assert(!q.m_pqAuthoredGraphics);
  q.ReplaceTitleInfo(nullptr);q.FreePrevTitleInfo();assert(liveTitles==0);
 }
 std::cout<<"BD-menu watchdog, short-loop seams, stream compatibility, graphics ownership/bounds, title metadata lifetime and I/O: PASS\n";
}
'''
source=out/'navigation-behavior.cpp';source.write_text(preamble.replace('@@REAL_PALETTE@@',real_palette)+'\n'.join(functions)+tests)
cmd=shlex.split(os.environ.get('CXX', 'g++')) + ['-std=c++17','-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(out/'test-include'),'-I'+str(root/'xbmc'),'-I'+str(args.libbluray_include.resolve()),str(source),'-o',str(out/'navigation-behavior')]
result=subprocess.run(cmd,capture_output=True,text=True);(out/'navigation-behavior-build.log').write_text(result.stdout+result.stderr);assert result.returncode==0,result.stderr
result=subprocess.run([str(out/'navigation-behavior')],capture_output=True,text=True);(out/'navigation-behavior.log').write_text(result.stdout+result.stderr);print(result.stdout+result.stderr);assert result.returncode==0
