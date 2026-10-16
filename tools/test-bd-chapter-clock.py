#!/usr/bin/env python3
"""Run real chapter publication/stepping and Blu-ray-to-demux chapter routing.

Extracts whole UpdatePlayState, player getters/request methods, the chapter prefix
of Seek, real SPlayerState/IChapter and Blu-ray chapter/position methods. Native
library navigation, clocks, menus, cache/services and flush are recording host
boundaries. Chapter marks are synthetic: captured clocks/events are observations,
not a recovered MPLS table. This checks target selection, not disc/device output.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
VP = ROOT / 'xbmc/cores/VideoPlayer'


def harness(parent):
    inp = (VP / 'DVDInputStreams/DVDInputStream.h').read_text()
    bh = (VP / 'DVDInputStreams/DVDInputStreamBluray.h').read_text()
    bc = (VP / 'DVDInputStreams/DVDInputStreamBluray.cpp').read_text()
    demux = (VP / 'DVDDemuxers/DVDDemuxFFmpeg.cpp').read_text()
    header = (VP / 'VideoPlayer.h').read_text()
    # The actual base getter matters: do not silently model Blu-ray as exposing
    # an IChapter getter merely because its separate RTTI interface exists.
    assert 'GetIChapter()' not in bh
    base_getter = extract(inp, 'virtual IChapter* GetIChapter()')
    interfaces = '\n'.join(extract(inp, 'class ' + n) + ';' for n in
                           ('IDisplayTime', 'ITimes', 'IPosTime', 'IChapter'))
    inherited = bh[bh.index('class CDVDInputStreamBluray\n'):bh.index('public:', bh.index('class CDVDInputStreamBluray\n'))]
    bluray = inherited + 'public:\n' + BLURAY_BODY
    methods = '\n'.join(extract(bc, n) for n in (
        'int CDVDInputStreamBluray::GetChapterCount(',
        'int CDVDInputStreamBluray::GetChapter(',
        'int64_t CDVDInputStreamBluray::GetChapterPos(',
        'bool CDVDInputStreamBluray::SeekChapter(',
        'bool CDVDInputStreamBluray::SeekChapterWithResult(',
        'MenuType CDVDInputStreamBluray::GetSupportedMenuType(',
        'int CDVDInputStreamBluray::GetTotalTime(',
        'int CDVDInputStreamBluray::GetTime('))
    methods += '\n' + '\n'.join(extract(demux, n) for n in (
        'int CDVDDemuxFFmpeg::GetChapterCount(',
        'int64_t CDVDDemuxFFmpeg::GetChapterPos(',
        'bool CDVDDemuxFFmpeg::SeekChapter(', 'double CDVDDemuxFFmpeg::ConvertTimestamp('))
    seek = extract(parent, 'void CVideoPlayer::Seek(bool bPlus,')
    methods += '\n' + seek[:seek.index('\n  int64_t seekTarget;')] + '\n  ++timedSeeks;\n}\n'
    methods += '\n' + '\n'.join(extract(parent, n) for n in (
        'void CVideoPlayer::UpdatePlayState(', 'int CVideoPlayer::GetChapterCount(',
        'int CVideoPlayer::GetChapter(', 'int64_t CVideoPlayer::GetChapterPos(',
        'int64_t CVideoPlayer::GetTime(', 'int CVideoPlayer::GetPreviousChapter(',
        'int CVideoPlayer::SeekChapter('))
    state = extract(header, 'struct SPlayerState') + ';'
    message = extract((VP / 'DVDMessage.h').read_text(), 'class CDVDMsgPlayerSeekChapter') + ';'
    return (PRELUDE.replace('@INTERFACES@', interfaces).replace('@BASE_GETTER@', base_getter)
            .replace('@BLURAY@', bluray).replace('@STATE@', state).replace('@MESSAGE@', message)
            + methods + TESTS)


BLURAY_BODY = r'''
  std::mutex m_clipTableMutex; BLURAY native; BLURAY* m_bd=&native;
  BLURAY_TITLE_INFO title; BLURAY_TITLE_INFO* m_titleInfo=&title;
  std::vector<Chapter> marks;std::vector<Clip> clips;
  uint64_t m_titleGeneration=0;std::atomic_bool m_aborted{false};
  bool m_seamlessPlayItem=true, m_navmode=true, m_bMVCPlayback=false;
  std::atomic<unsigned> m_uoMask{0};
  std::shared_ptr<Session> m_session=std::make_shared<Session>();
  std::queue<int> m_clipQueue; int m_event=0, m_clipStartTime=0, m_dispTimeBeforeRead=1;
  CDVDInputStream::IDisplayTime* GetIDisplayTime() override {return this;}
  CDVDInputStream::IPosTime* GetIPosTime() override {return this;}
  bool PosTime(int) override {return false;}
  bool CanSeek() override {return !menu || seekable;}
  bool GetState(std::string& s) override {s="BD-J";return true;}
  MenuType GetSupportedMenuType() override;
  void GetChapterName(std::string&, int=-1) override {}
  int GetChapterCount() override; int GetChapter() override;
  int64_t GetChapterPos(int) override; bool SeekChapter(int) override;
  bool SeekChapterWithResult(int,CDVDInputStream::IChapter::SeekResult&) override;
  int GetTotalTime() override; int GetTime() override;
  void ProcessEvent() {++native.events;}
  void OpenNextStream() {} void SeekMVCDemux(int64_t) {}
  void SetMarks(const std::vector<int64_t>& seconds) {
    marks.clear(); for(auto s:seconds)marks.push_back({static_cast<uint64_t>(s)*90000});
    title.chapter_count=marks.size();title.chapters=marks.data();title.duration=13000*90000ULL;
    clips={{0,0,title.duration}};title.clip_count=1;title.clips=clips.data();
  }
};
'''

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <ctime>
#include <memory>
#include <limits>
#include <functional>
#include <mutex>
#include <queue>
#include <string>
#include <utility>
#include <vector>
using CCriticalSection=std::recursive_mutex;
constexpr double DVD_TIME_BASE=1000000, DVD_NOPTS_VALUE=-1.e30;
constexpr double DVD_MSEC_TO_TIME(double v){return v*1000;}
constexpr double DVD_SEC_TO_TIME(double v){return v*DVD_TIME_BASE;}
constexpr double DVD_TIME_TO_MSEC(double v){return v/1000;}
constexpr int CACHESTATE_DONE=0,CACHESTATE_FLUSH=1,CACHESTATE_PLAY=4,DVDSTATE_STILL=2;
constexpr int MSGQ_OK=1,MSGQ_NOT_INITIALIZED=-1,LOGDEBUG=0;
enum class MenuType {NONE,NATIVE};
struct IDVDStreamPlayer {enum {SYNC_STARTING,SYNC_WAITSYNC,SYNC_INSYNC};};
struct CDVDInputStream {
  @INTERFACES@
  struct IMenus {
    virtual ~IMenus()=default;
    bool menu=false,seekable=true;
    virtual bool CanSeek(){return seekable;}
    virtual bool GetState(std::string&){return true;}
    virtual MenuType GetSupportedMenuType(){return MenuType::NATIVE;}
  };
  struct IExtentionStream {virtual ~IExtentionStream()=default;};
  virtual ~CDVDInputStream()=default;
  @BASE_GETTER@
  virtual ITimes* GetITimes(){return nullptr;}
  virtual IDisplayTime* GetIDisplayTime(){return nullptr;}
  virtual IPosTime* GetIPosTime(){return nullptr;}
  virtual bool CanPause(){return true;} virtual bool IsRealtime(){return false;}
  template<class T> bool GetCacheStatus(T*){return false;}
  int64_t GetLength(){return 0;}
};
struct Chapter {uint64_t start;unsigned clip_ref=0;};
struct Clip {uint64_t start_time,in_time,out_time;};
struct BLURAY_TITLE_INFO {unsigned clip_count=0;Clip* clips=nullptr;unsigned chapter_count=0;Chapter* chapters=nullptr;uint64_t duration=0;};
struct BLURAY {unsigned chapter=0; bool fail=false; int events=0;std::vector<unsigned> requests;std::function<void()> duringSeek;};
constexpr unsigned BLURAY_UO_CHAPTER_SEARCH=1;
unsigned bd_get_current_chapter(BLURAY* p){return p->chapter;}
// Recording library boundary: real nav_chapter_search uses this zero-based
// chapter index directly. No media, access-point rounding or BD-J VM is emulated.
int64_t bd_seek_chapter(BLURAY* p,unsigned ch){p->requests.push_back(ch);if(p->fail)return -1;if(p->duringSeek)p->duringSeek();p->chapter=ch;return ch*1000;}
int bd_get_event(BLURAY*,int*){return 0;}
struct Session {int resets=0;void ResetAccessPattern(){++resets;}};
#define EMPTY_QUEUE(q) while(!(q).empty())(q).pop()
@BLURAY@
struct AVChapter {int64_t start=0;struct {int den=1,num=1;} time_base;};
template<class T> double av_q2d(T v){return double(v.num)/v.den;}
constexpr int64_t AV_NOPTS_VALUE=std::numeric_limits<int64_t>::min();
constexpr int AV_TIME_BASE=1000000;
struct Format {int64_t start_time=0;unsigned nb_chapters=0;AVChapter** chapters=nullptr;};
struct CLog {template<class...T>static void Log(int,const char*,T...) {}};
struct CDVDDemuxFFmpeg {
  std::shared_ptr<CDVDInputStream> m_pInput; Format* m_pFormatContext=nullptr;
  bool m_checkTransportStream=false,m_bSup=false;double m_startTime=0;
  double m_currentPts=0; int flushes=0,readerQueries=0;std::vector<int64_t> fileMarks;
  int GetChapterCount(); int64_t GetChapterPos(int); bool SeekChapter(int,double*,double* = nullptr);
  int GetChapter(){++readerQueries;return 70;}
  void GetChapterName(std::string& n,int ch){n=std::to_string(ch);}
  int GetStreamLength(){auto d=m_pInput?m_pInput->GetIDisplayTime():nullptr;return d?d->GetTotalTime():12000000;}
  void Flush(){++flushes;}
  bool SeekTime(double,bool,double*){assert(false && "unexpected_time_seek");return false;}
  double ConvertTimestamp(int64_t,int,int);
};
struct CacheCore {std::vector<std::pair<std::string,int64_t>> chapters;void SetChapters(const decltype(chapters)& c){chapters=c;}};
struct CServiceBroker {static CacheCore& GetDataCacheCore(){static CacheCore c;return c;}};
namespace XFILE {struct SCacheStatus {int64_t forward=0;};}
struct CDVDMsg {enum {PLAYER_SEEK_CHAPTER};explicit CDVDMsg(int){} virtual ~CDVDMsg()=default;};
@MESSAGE@
struct Messenger {
  bool accept=true;int queued=0;std::vector<int> requests;
  int Put(std::shared_ptr<CDVDMsgPlayerSeekChapter> m){if(!accept)return MSGQ_NOT_INITIALIZED;requests.push_back(m->GetChapter());++queued;return MSGQ_OK;}
  int GetPacketCount(int){return queued;}
};
struct Clock {double pts=0,absolute=1;double GetClock(bool){return pts;}double GetAbsoluteClock(){return absolute;}};
struct Stream {double dts=DVD_NOPTS_VALUE,startpts=DVD_NOPTS_VALUE;int id=-1,dispTime=0,syncState=IDVDStreamPlayer::SYNC_INSYNC;};
struct ProcessInfo {double publishedTime=0;void SetStateRealtime(bool){} void SetPlayTimes(time_t,double time,double,double){publishedTime=time;}};
struct Edl {bool cuts=false;int64_t cut=0;bool HasCuts(){return cuts;}int64_t GetTimeWithoutCuts(double v){return v-cut;}int64_t GetTotalCutTime(){return cut;}};
@STATE@
struct CVideoPlayer {
  SPlayerState m_State;mutable CCriticalSection m_StateSection;Clock m_clock;
  Stream m_CurrentVideo,m_CurrentAudio;CDVDDemuxFFmpeg* m_pDemuxer=nullptr;
  std::shared_ptr<CDVDInputStream> m_pInputStream;
  ProcessInfo process;ProcessInfo* m_processInfo=&process;Edl m_Edl;
  Messenger m_messenger;std::atomic<int> m_chapterSeekTarget{0};
  double m_demuxSeekBasePts=DVD_NOPTS_VALUE;int m_caching=CACHESTATE_DONE,m_messageQueueTimeSize=10;
  struct {int state=0;std::chrono::steady_clock::time_point iDVDStillStartTime;std::chrono::milliseconds iDVDStillTime{0};}m_dvd;
  bool lifecycle=false,playerThread=true;int timedSeeks=0;
  bool IsCurrentThread(){return playerThread;}bool ParentLifecyclePending(){return lifecycle;}
  bool IsInMenuInternal(){auto p=std::dynamic_pointer_cast<CDVDInputStream::IMenus>(m_pInputStream);return p&&p->menu;}
  void SynchronizeDemuxer(){} double GetQueueTime(){return 0;}
  struct CacheInfo {bool valid=false;double level=0,offset=0,time=0;};
  CacheInfo GetCachingTimes(){return {};}
  void UpdatePlayState(double);void Seek(bool,bool,bool);int SeekChapter(int);
  int GetChapterCount() const;int GetChapter() const;int64_t GetChapterPos(int) const;
  int64_t GetTime();int GetPreviousChapter();
  void Publish(){UpdatePlayState(0);}void Next(){Seek(true,true,true);}void Back(){Seek(false,true,true);}
};
'''

TESTS = r'''
struct Rig {
  CVideoPlayer p;std::shared_ptr<CDVDInputStreamBluray> bd=std::make_shared<CDVDInputStreamBluray>();CDVDDemuxFFmpeg demux;
  Rig(){bd->SetMarks({0,100,200,300,400,500,600,700,800,900});demux.m_pInput=bd;p.m_pDemuxer=&demux;p.m_pInputStream=bd;p.m_CurrentVideo.id=0;p.m_CurrentVideo.dts=4199000000.;p.m_CurrentVideo.dispTime=1;p.m_clock.pts=4199000000.;}
  void At(double clockMs,double packetMs,int displayMs){p.m_clock.pts=clockMs*1000;p.m_CurrentVideo.dts=packetMs*1000;p.m_CurrentVideo.dispTime=displayMs;p.Publish();}
  void Dispatch(){for(int c:p.m_messenger.requests){double start=DVD_NOPTS_VALUE;assert(demux.SeekChapter(c,&start));assert(start==bd->GetChapterPos(c)*DVD_TIME_BASE);}p.m_messenger.requests.clear();p.m_messenger.queued=0;}
};
struct Exposed : CDVDInputStream,CDVDInputStream::IChapter,CDVDInputStream::IDisplayTime {
  CDVDInputStream::IChapter* GetIChapter() override{return this;}CDVDInputStream::IDisplayTime* GetIDisplayTime() override{return this;}
  int GetChapter() override{return 3;}int GetChapterCount() override{return 3;}
  int64_t GetChapterPos(int c) override{return (c-1)*100;}void GetChapterName(std::string&,int) override{}
  bool SeekChapter(int) override{return true;}int GetTotalTime() override{return 300000;}
  int GetTime() override{return 1;}
};
struct TimesInput : CDVDInputStream,CDVDInputStream::ITimes {
  bool valid=true;CDVDInputStream::ITimes* GetITimes() override{return this;}
  bool GetTimes(Times& t) override {t={0,4000*DVD_TIME_BASE,4000*DVD_TIME_BASE,5000*DVD_TIME_BASE};return valid;}
};
int main(){
  // Measured first-key raw clock4211.377s and initial raw origin~4199s.
  // This explicit synthetic table models a valid long title; its marks are
  // NOT claimed to be Avatar's unavailable MPLS chapter table.
  {Rig r;std::vector<int64_t> marks;for(int i=0;i<80;++i)marks.push_back(i*160);
   r.bd->SetMarks(marks);r.bd->native.chapter=3; // reader has advanced ahead
   assert(r.bd->GetIChapter()==nullptr && "bluray_getter_is_not_rtti");
   assert(r.bd->GetChapter()==4 && "native_reader_is_deliberately_ahead");
   assert(r.demux.GetChapterCount()==80 && "demux_rtti_sees_chapters");
   r.At(4211377,4212000,13000);
   assert(r.p.GetChapter()==1 && "title_clock_not_raw_clip_clock");
   assert(r.p.GetTime()==12377 && r.p.process.publishedTime==12377);
   r.p.Next();assert(r.p.m_messenger.requests.back()==2 && "next_requests_adjacent_title_chapter");r.Dispatch();
   assert(r.bd->native.requests.back()==1 && r.bd->native.chapter==1 && r.demux.flushes==1 && "provider_receives_zero_based_two");
   assert(r.bd->m_session->resets==1 && "accepted_navigation_resets_access");
   r.At(4370500,4371000,161000);assert(r.p.GetChapter()==2 && r.p.m_chapterSeekTarget==0 && "normalized_seek_retires_intent");
   r.p.Next();assert(r.p.m_messenger.requests.back()==3);r.Dispatch();assert(r.bd->native.requests.back()==2);
   r.At(4530000,4531000,332000);assert(r.p.GetChapter()==3);
   r.p.Back();assert(r.p.m_messenger.requests.back()==3 && "back_after_grace_restarts_current");}
  // All four measured pre-key raw clocks, with explicit synthetic title-time
  // projections/table, cover the last-chapter time fallback and backward
  // direction error. The unavailable disc table/title samples are not inferred.
  const double rawClocks[]={4211377,8488566,12886861,13495510};
  const int titleClocks[]={12377,4293500,8693700,9302300};
  for(int i=0;i<4;++i){
   Rig r;std::vector<int64_t> marks;for(int c=0;c<76;++c)marks.push_back(c*150);
   r.bd->SetMarks(marks);r.At(rawClocks[i],rawClocks[i]+5000,titleClocks[i]+5000);
   const int chapter=titleClocks[i]/150000+1;
   assert(r.p.GetChapter()==chapter && "four_clock_title_selection");
   r.p.Next();assert(r.p.m_messenger.requests.back()==chapter+1 && r.p.timedSeeks==0 && "no_false_end_title_fallback");
   r.p.m_chapterSeekTarget=0;r.p.m_messenger.requests.clear();r.p.m_messenger.queued=0;
   r.p.Back();const int previous=titleClocks[i]<(chapter-1)*150000+5000?chapter-1:chapter;
   assert(r.p.m_messenger.requests.back()==previous && "back_never_jumps_forward");
  }
  // Equal title positions in different raw clips must choose equal chapters.
  for(double origin:{0.,4199000.,-100000.,9000000.}) {
   Rig r;r.bd->native.chapter=8;
   for(int title:{1,99999,100000,104999,105000,250000,899999}) {
    r.At(origin+title,origin+title+2000,title+2000);
    assert(r.p.GetChapter()==title/100000+1 && "clip_origin_invariant");
   }
   assert(r.demux.readerQueries==0 && "do_not_use_read_ahead_chapter");
  }
  // Active audio is the display-time fallback, including audio-only playback.
  {Rig r;r.p.m_CurrentVideo.id=-1;r.p.m_CurrentVideo.dts=DVD_NOPTS_VALUE;
   r.p.m_CurrentAudio.id=0;r.p.m_CurrentAudio.dts=4400000000.;r.p.m_CurrentAudio.dispTime=201000;
   r.p.m_clock.pts=4399500000.;r.p.Publish();assert(r.p.GetChapter()==3 && r.p.GetTime()==200500);}
  // A valid offset established earlier remains usable if no new packet DTS is
  // available; the clock must not silently return to clip-relative chapters.
  {Rig r;r.At(4299500,4300000,101000);r.p.m_CurrentVideo.dts=DVD_NOPTS_VALUE;
   r.p.m_clock.pts+=DVD_TIME_BASE;r.p.Publish();assert(r.p.GetChapter()==2);}
  // Pending Back/Next still steps from intent until startup is synchronized.
  {Rig r;r.At(4500000,4500000,301000);r.p.Back();r.p.Back();
   assert((r.p.m_messenger.requests==std::vector<int>{3,2}));
   r.p.m_messenger.queued=0;r.p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;
   r.p.Publish();assert(r.p.m_chapterSeekTarget==2);
   r.p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;r.p.Publish();assert(r.p.m_chapterSeekTarget==0);}
  {Rig r;r.At(4500000,4500000,301000);r.p.Next();r.p.Back();
   assert((r.p.m_messenger.requests==std::vector<int>{5,4}));}
  // Restart grace boundary is title time, including a paused/fixed clock.
  for(int ms:{104999,105000}){Rig r;r.At(4199000+ms,4199000+ms,ms);r.p.Back();
   assert(r.p.m_messenger.requests.back()==(ms<105000?1:2) && "title_time_restart_grace");}
  // Exposed input chapter providers retain precedence over clock inference.
  {CVideoPlayer p;CDVDDemuxFFmpeg d;auto in=std::make_shared<Exposed>();
   d.m_pInput=in;p.m_pDemuxer=&d;p.m_pInputStream=in;p.m_clock.pts=10*DVD_TIME_BASE;
   p.Publish();assert(p.GetChapter()==3 && "explicit_provider_precedence");}
  // Timeline-bearing input need not expose chapter navigation. ITimes beats
  // display inference, while a failed Times observation uses ordinary time.
  {Rig r;auto in=std::make_shared<TimesInput>();r.p.m_pInputStream=in;r.p.m_clock.pts=4250*DVD_TIME_BASE;
   r.p.Publish();assert(r.p.GetChapter()==3 && r.p.GetTime()==250000 && "times_normalized_before_chapter");
   in->valid=false;r.p.m_clock.pts=101*DVD_TIME_BASE;r.p.Publish();assert(r.p.GetChapter()==2 && r.p.GetTime()==101000);}
  // No input/display offset: ordinary FFmpeg clock-based chapters are retained.
  {Rig r;r.demux.m_pInput.reset();r.p.m_pInputStream.reset();Format f;AVChapter a,b;AVChapter* list[]={&a,&b};
   a.start=0;b.start=100;f.nb_chapters=2;f.chapters=list;r.demux.m_pFormatContext=&f;
   r.p.m_clock.pts=101*DVD_TIME_BASE;r.p.Publish();assert(r.p.GetChapter()==2 && r.demux.readerQueries==0);}
  // Native menu user-operation exclusion, including a seekable BD-J popup.
  {Rig r;r.bd->menu=true;r.bd->seekable=false;r.At(4200000,4200000,1000);
   assert(r.p.GetChapter()==0 && "nonseekable_menu_has_no_chapter");
   r.bd->seekable=true;r.p.Publish();assert(r.p.GetChapter()==1);}
  // Disc still duration is UI elapsed time, never a chapter-table position.
  {Rig r;r.At(4450000,4450000,251000);r.p.m_dvd.state=DVDSTATE_STILL;
   r.p.m_dvd.iDVDStillStartTime=std::chrono::steady_clock::now();r.p.m_dvd.iDVDStillTime=std::chrono::milliseconds(10000);
   r.p.Publish();assert(r.p.GetChapter()==3 && "still_duration_not_chapter_time");}
  // EDL maps display time after chapter selection, not the authored title table.
  {Rig r;r.p.m_Edl.cuts=true;r.p.m_Edl.cut=150000;r.At(4450000,4450000,251000);
   assert(r.p.GetChapter()==3 && r.p.GetTime()==101000 && "edl_does_not_rebase_chapter_marks");}
  // Empty tables and positions before their first marker have no chapter.
  {Rig r;r.bd->SetMarks({10,100});r.At(4199000,4199000,1);assert(r.p.GetChapter()==0);}
  {Rig r;r.bd->SetMarks({});r.p.Publish();assert(r.p.GetChapter()==0 && r.p.GetChapterCount()==0);}
  // UO-masked, invalid and failed native navigation retain provider checks.
  {Rig r;r.bd->m_uoMask=BLURAY_UO_CHAPTER_SEARCH;double start=DVD_NOPTS_VALUE;
   assert(!r.demux.SeekChapter(2,&start) && r.bd->native.requests.empty());
   r.bd->m_uoMask=0;assert(!r.demux.SeekChapter(99,&start) && r.bd->native.requests.empty());
   r.bd->native.fail=true;assert(!r.demux.SeekChapter(2,&start) && r.demux.flushes==0 && r.bd->m_session->resets==0);}
}
'''


def run(source, out, name, expected_assertion=None):
    cpp, exe = out / (name + '.cpp'), out / name
    cpp.write_text(source)
    subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-Wno-unused-parameter', '-pthread', '-no-pie',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    str(cpp), '-o', str(exe)], check=True)
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    (out / (name + '.log')).write_text(result.stdout + result.stderr)
    if expected_assertion:
        assert result.returncode == -6 and 'Assertion' in result.stderr, result.stderr
        assert expected_assertion in result.stderr, result.stderr
        assert 'Sanitizer' not in result.stderr and 'runtime error:' not in result.stderr
    elif result.returncode:
        raise RuntimeError(result.stderr)
    print(name + ': PASS' + (' (behavioral rejection)' if expected_assertion else ''), flush=True)
    return {'name': name, 'returncode': result.returncode, 'expected_assertion': expected_assertion}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='alternative complete VideoPlayer.cpp')
    parser.add_argument('--expect-original-failure', action='store_true')
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    parent = (args.source or VP / 'VideoPlayer.cpp').read_text()
    source = harness(parent)
    with tempfile.TemporaryDirectory(prefix='bd-chapter-clock-') as temporary:
        out = args.output or Path(temporary)
        out.mkdir(parents=True, exist_ok=True)
        results = [run(source, out, 'bd-chapter-clock',
                       'title_clock_not_raw_clip_clock' if args.expect_original_failure else None)]
        if args.negative_controls:
            controls = [
                ('omit-display-offset', 'state.time += state.time_offset * 1000 / DVD_TIME_BASE;',
                 ';', 'title_clock_not_raw_clip_clock'),
                ('still-ui-clock', 'if (chapterTime >= chapterStartMs)',
                 'if ((static_cast<void>(chapterTime), state.time) >= chapterStartMs)', 'still_duration_not_chapter_time'),
                ('reader-chapter', 'state.chapter = static_cast<int>(i) + 1;',
                 'state.chapter = m_pDemuxer->GetChapter();', 'title_clock_not_raw_clip_clock'),
                ('discard-provider-precedence', 'chaptersFromClock = false;',
                 'chaptersFromClock = true;', 'explicit_provider_precedence'),
                ('ignore-menu-restriction', 'IsInMenuInternal() && pMenu && !pMenu->CanSeek()',
                 'false', 'nonseekable_menu_has_no_chapter'),
                ('ignore-pending-back', 'if (pending > 0)', 'if (false)',
                 'r.p.m_messenger.requests==std::vector<int>{3,2}'),
                ('wrong-libbluray-index', 'bd_seek_chapter(m_bd, ch - 1)',
                 'bd_seek_chapter(m_bd, ch)', 'provider_receives_zero_based_two'),
                ('wrong-chapter-units', '.start / 90000;', '.start / 90;',
                 'start==bd->GetChapterPos(c)*DVD_TIME_BASE'),
            ]
            for name, old, new, assertion in controls:
                assert old in source, name
                results.append(run(source.replace(old, new), out, name, assertion))
        (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
