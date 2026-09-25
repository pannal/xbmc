#!/usr/bin/env python3
"""Host tests of extracted BD-menu player policy methods.

Uses deterministic clock/player stubs and the actual production method bodies.
Checks navigation policy and ownership decisions, not decoder, HDMI or thread
behavior. Requires Python 3 and g++; ASan/UBSan run by default.
"""
import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def main():
    player = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    video = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    queue = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessageQueue.h').read_text()
    constants = player[player.index('constexpr double MENU_DOMAIN_RAMP_RATE'):
                       player.index('\n}', player.index('constexpr double MENU_DOMAIN_RAMP_RATE'))]
    methods = '\n'.join(function(player, 'void CVideoPlayer::' + name)
                        for name in ('CheckBetterStream(', 'UpdateMenuDomainQueueDepth(',
                                     'DrainStreamsAtBoundary('))
    still = player[player.index('    case BD_EVENT_STILL_TIME:', player.index('int CVideoPlayer::OnDiscNavResult')):]
    still = still[:still.index('    case BD_EVENT_MENU_ERROR:')]
    methods += '\nvoid CVideoPlayer::HandleStill(int iMessage, int* pData) { switch(iMessage) {\n' + still + '\n} }\n'
    methods += function(video, 'void CVideoPlayerVideo::ProcessOverlays(')
    methods = methods.replace('std::chrono::steady_clock', 'TestClock')
    methods += ('\nstruct QueueFullness {\n  int dataLevel=0, timeLevel=0; bool m_timeBound=false;\n'
                '  int GetLevel(bool data_level) const {return data_level ? dataLevel : timeLevel;}\n  '
                + function(queue, 'bool IsFull() const') + '\n};\n')
    source = PRELUDE + constants + '\n' + methods + TESTS
    with tempfile.TemporaryDirectory(prefix='bd-menu-player-') as temporary:
        cpp = pathlib.Path(temporary) / 'test.cpp'
        binary = pathlib.Path(temporary) / 'test'
        cpp.write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(ROOT / 'xbmc'), str(cpp), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print('BD menu player policy: PASS (ASan/UBSan; deterministic host stubs)')


PRELUDE = r'''
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <vector>
using namespace std::chrono_literals;
#define HAVE_LIBBLURAY 1
#define STREAM_SOURCE_MASK(x) (x)
constexpr int STREAM_VIDEO=1, STREAM_AUDIO=2, STREAM_SUBTITLE=3, STREAM_SOURCE_DEMUX=1;
constexpr int DVDSTREAM_TYPE_BLURAY=1, LOGDEBUG=0, DVDSTATE_NORMAL=0, DVDSTATE_STILL=1;
constexpr int BD_EVENT_STILL_TIME=1, BD_EVENT_STILL=2;
struct TestClock {
  using duration=std::chrono::nanoseconds;
  using rep=duration::rep; using period=duration::period;
  using time_point=std::chrono::time_point<TestClock>;
  static constexpr bool is_steady=true;
  static inline duration ticks=1s;
  static time_point now() { return time_point(ticks); }
};
void advance(std::chrono::milliseconds elapsed) { TestClock::ticks+=elapsed; }
std::function<void()> onSleep;
struct CThread { static void Sleep(std::chrono::milliseconds elapsed) { advance(elapsed); if(onSleep) onSleep(); } };
namespace XbmcThreads {
template<class T=void> struct EndTime {
  TestClock::time_point end;
  explicit EndTime(std::chrono::milliseconds duration) { Set(duration); }
  void Set(std::chrono::milliseconds duration) { end=TestClock::now()+duration; }
  bool IsTimePast() const { return TestClock::now()>=end; }
};
}
struct CLog { template<class... T> static void Log(T&&...) {} };
struct Advanced { float m_videoMenuDomainQueueTimeSize=1.0f; } advanced;
struct SettingsComponent { Advanced* GetAdvancedSettings() {return &advanced;} } settings;
struct CServiceBroker { static SettingsComponent* GetSettingsComponent() {return &settings;} };
struct CDVDInputStream {
  bool bd=false;
  virtual ~CDVDInputStream()=default;
  bool IsStreamType(int) const {return bd;}
};
struct CDVDInputStreamBluray : CDVDInputStream {
  bool domain=true, data=true;
  CDVDInputStreamBluray() {bd=true;}
  bool IsMenuDomainSegment() const {return domain;}
  bool IsReadInDataPhase() const {return data;}
};
struct CDVDMsg { enum {VIDEO_DRAIN}; explicit CDVDMsg(int) {} };
struct IDVDStreamPlayer {
  enum { SYNC_STARTING, SYNC_INSYNC };
  bool stalled=true;
  bool IsStalled() const {return stalled;}
};
struct TestStream : IDVDStreamPlayer {
  double queue=0, limit=16, pts=0, sink=0, outputDelay=0;
  int limitWrites=0, drainMessages=0;
  bool data=false, eos=true, accepts=true, timeBound=false;
  void SetMaxTimeSize(double value, bool bound=false) {limit=value; timeBound=bound; ++limitWrites;}
  double GetQueueTimeSize() const {return queue;}
  double GetCurrentPts() const {return pts;}
  double GetSinkDelay() const {return sink;}
  double GetOutputDelay() const {return outputDelay;}
  bool HasData() const {return data;}
  bool IsEOS() const {return eos;}
  bool AcceptsData() const {return accepts;}
  void SendMessage(std::shared_ptr<CDVDMsg>,int) {++drainMessages;}
};
struct CCurrentStream {
  int type=STREAM_VIDEO,id=1,source=STREAM_SOURCE_DEMUX,player=0;
  int syncState=IDVDStreamPlayer::SYNC_INSYNC;
  int64_t demuxerId=10;
  void* stream=nullptr;
};
struct CDemuxStream {
  int type=STREAM_VIDEO,source=STREAM_SOURCE_DEMUX,uniqueId=1,dvdNavId=-1;
  int64_t demuxerId=20;
  bool disabled=false;
};
struct SelectionStream { int id=1; int64_t demuxerId=20; };
struct PredicateAudioFilter { PredicateAudioFilter(int,bool) {} };
struct SelectionStreams {
  std::vector<SelectionStream> preferred{{1,20}};
  std::vector<SelectionStream> Get(int,PredicateAudioFilter) const {return preferred;}
};
struct VideoSettings {int m_AudioStream=0;};
struct ProcessInfo { VideoSettings GetVideoSettings() const {return {};} };
struct Messenger {bool pending=false; bool HasMessages() const {return pending;}};
struct RenderManager {
  int queued=0;
  void GetStats(int&,double&,int& value,int&) {value=queued;}
};
struct CVideoPlayer {
  bool m_bdAudioReuse=false,m_bdVideoReuse=false,m_bAbortRequest=false,m_HasVideo=true;
  int m_playSpeed=DVD_PLAYSPEED_NORMAL;
  CCurrentStream m_CurrentVideo, m_CurrentAudio;
  struct {bool videoOnly=false,preferStereo=false;} m_playerOptions;
  struct {int iSelectedAudioStream=-1,state=DVDSTATE_NORMAL;
    std::chrono::milliseconds iDVDStillTime{0}; TestClock::time_point iDVDStillStartTime{};} m_dvd;
  bool m_bdTimedStill=false;
  TestStream audio, video;
  TestStream* m_VideoPlayerAudio=&audio;
  TestStream* m_VideoPlayerVideo=&video;
  std::shared_ptr<CDVDInputStream> m_pInputStream=std::make_shared<CDVDInputStream>();
  SelectionStreams m_SelectionStreams;
  ProcessInfo info; ProcessInfo* m_processInfo=&info;
  Messenger m_messenger; RenderManager m_renderManager;
  double m_messageQueueTimeSize=16,m_menuDomainRampCap=0;
  bool m_menuDomainLowLatency=false,m_menuDomainClampPending=false;
  bool m_menuDomainSegment=false,m_menuDomainFillPending=false;
  TestClock::time_point m_menuDomainRampLast{},m_menuDomainEvalLast{},m_menuDomainStarveStart{};
  bool valid=false,better=true; int opens=0,closes=0,validChecks=0;
  CVideoPlayer() {m_CurrentAudio.type=STREAM_AUDIO;}
  IDVDStreamPlayer* GetStreamPlayer(int) {return &video;}
  bool IsValidStream(const CCurrentStream&) {++validChecks;return valid;}
  bool IsBetterStream(const CCurrentStream&,CDemuxStream*) {return better;}
  void CloseStream(CCurrentStream& current,bool) {++closes;current.id=-1;}
  bool OpenStream(CCurrentStream& current,int64_t demux,int id,int source) {
    ++opens;current.id=id;current.demuxerId=demux;current.source=source;
    if(current.type==STREAM_AUDIO)m_bdAudioReuse=false;else m_bdVideoReuse=false;
    return true;
  }
  void CheckBetterStream(CCurrentStream&,CDemuxStream*);
  void UpdateMenuDomainQueueDepth(bool);
  void DrainStreamsAtBoundary();
  void HandleStill(int,int*);
};
using CCriticalSection=std::recursive_mutex;
constexpr int DVDOVERLAY_TYPE_GROUP=1;
struct CDVDOverlay {
  virtual ~CDVDOverlay()=default;
  bool menu=false,bForced=false; int type=0,m_3dSubtitleDepth=0;
  double iPTSStartTime=0,iPTSStopTime=0;
  bool IsDiscMenuOverlay() const {return menu;}
  bool IsOverlayType(int value) const {return type==value;}
};
using VecOverlays=std::vector<std::shared_ptr<CDVDOverlay>>;
struct CDVDOverlayGroup : CDVDOverlay {VecOverlays m_overlays;};
struct CDVDOverlayLibass : CDVDOverlay {
  bool active=true; CDVDOverlayLibass* GetLibassHandler() {return this;}
  bool EventActive(double) const {return active;}
};
struct OverlayContainer : CCriticalSection {
  VecOverlays items;
  void CleanUp(double) {}
  VecOverlays* GetOverlays() {return &items;}
};
struct VideoPicture {int m_3dSubtitleDepth=7;};
struct OverlayRender {
  VecOverlays rendered;
  void AddOverlay(std::shared_ptr<CDVDOverlay> value,double) {rendered.push_back(value);}
};
struct CVideoPlayerVideo {
  double m_iSubtitleDelay=0; bool m_bRenderSubs=true;
  int m_syncState=IDVDStreamPlayer::SYNC_INSYNC;
  OverlayContainer container; OverlayContainer* m_pOverlayContainer=&container;
  OverlayRender m_renderManager;
  void ProcessOverlays(const VideoPicture*,double);
};
'''

TESTS = r'''
int main() {
  // A retained player must survive unrelated packets and rebind before old-ID validation.
  { CVideoPlayer p; p.m_bdAudioReuse=true; CDemuxStream s;
    p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==0 && p.closes==0 && p.m_bdAudioReuse);
    s.type=STREAM_AUDIO;s.dvdNavId=100;p.m_dvd.iSelectedAudioStream=101;
    p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==0 && p.closes==0 && p.m_bdAudioReuse);
    s.dvdNavId=101;p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==1 && p.closes==0 && p.validChecks==0);
    assert(p.m_CurrentAudio.demuxerId==20 && !p.m_bdAudioReuse);
  }
  // Without a navigator PID the saved/default candidate wins over first packet order.
  { CVideoPlayer p;p.m_bdAudioReuse=true;p.m_SelectionStreams.preferred={{9,20}};
    CDemuxStream s;s.type=STREAM_AUDIO;s.uniqueId=3;
    p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==0 && p.closes==0 && p.m_bdAudioReuse);
    s.uniqueId=9;p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==1 && p.m_CurrentAudio.id==9 && p.closes==0);
  }
  { CVideoPlayer p;p.m_bdVideoReuse=true;CDemuxStream s;
    p.CheckBetterStream(p.m_CurrentVideo,&s);assert(p.opens==1 && p.closes==0);
    assert(p.m_CurrentVideo.demuxerId==20 && !p.m_bdVideoReuse);
  }
  { CVideoPlayer p;p.m_bdAudioReuse=true;p.m_playerOptions.videoOnly=true;
    CDemuxStream s;s.type=STREAM_AUDIO;p.CheckBetterStream(p.m_CurrentAudio,&s);
    assert(p.opens==0 && p.closes==0);
  }
  { CVideoPlayer p;p.m_bdVideoReuse=true;CDemuxStream s;s.disabled=true;
    p.CheckBetterStream(p.m_CurrentVideo,&s);assert(p.opens==0 && p.closes==0);
    s.disabled=false;s.source=0;p.CheckBetterStream(p.m_CurrentVideo,&s);
    assert(p.opens==0 && p.closes==0);
  }
  // No reuse token preserves ordinary invalid-stream close and selection behavior.
  { CVideoPlayer p;CDemuxStream s;p.CheckBetterStream(p.m_CurrentVideo,&s);
    assert(p.closes==1 && p.opens==1 && p.validChecks==1);
  }
  { CVideoPlayer p;p.valid=true;p.better=false;CDemuxStream s;
    p.CheckBetterStream(p.m_CurrentVideo,&s);assert(p.closes==0 && p.opens==0);
  }
  // Ordinary playback never changes queue targets.
  { CVideoPlayer p;p.UpdateMenuDomainQueueDepth(false);
    assert(p.audio.limitWrites==0 && p.video.limitWrites==0);
  }
  { CVideoPlayer p;p.m_pInputStream=std::make_shared<CDVDInputStreamBluray>();
    advanced.m_videoMenuDomainQueueTimeSize=0;p.UpdateMenuDomainQueueDepth(true);
    assert(p.audio.limitWrites==0 && p.video.limitWrites==0);
    advanced.m_videoMenuDomainQueueTimeSize=1;
  }
  // Startup waits for stream sync and adequate queue fill, then clamps only the menu.
  { CVideoPlayer p;auto bd=std::make_shared<CDVDInputStreamBluray>();p.m_pInputStream=bd;
    p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;
    p.UpdateMenuDomainQueueDepth(true);assert(!p.m_menuDomainLowLatency);
    p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
    p.UpdateMenuDomainQueueDepth(false);assert(p.m_menuDomainFillPending && p.video.limit==16);
    p.video.queue=1.2;advance(250ms);p.UpdateMenuDomainQueueDepth(false);
    assert(!p.m_menuDomainFillPending && p.video.limit==1 && p.audio.limit==1);
    assert(p.video.timeBound && p.audio.timeBound); // the menu cap must limit read-ahead
    bd->domain=false;p.UpdateMenuDomainQueueDepth(false);
    assert(!p.m_menuDomainLowLatency && p.video.limit==16 && p.audio.limit==16);
    assert(!p.video.timeBound && !p.audio.timeBound);
  }
  // Existing read-ahead ramps down; sustained starvation releases until next segment.
  { CVideoPlayer p;p.m_pInputStream=std::make_shared<CDVDInputStreamBluray>();
    p.video.queue=4;p.UpdateMenuDomainQueueDepth(true);assert(p.video.limit==4.5 && p.video.timeBound);
    advance(1000ms);p.UpdateMenuDomainQueueDepth(false);assert(p.video.limit==3.75);
    p.video.queue=0;advance(1000ms);p.UpdateMenuDomainQueueDepth(false);assert(p.video.limit==1);
    advance(250ms);p.UpdateMenuDomainQueueDepth(false);assert(p.m_menuDomainLowLatency);
    advance(500ms);p.UpdateMenuDomainQueueDepth(false);
    assert(!p.m_menuDomainLowLatency && p.video.limit==16 && !p.video.timeBound);
    advance(500ms);p.UpdateMenuDomainQueueDepth(false);assert(!p.m_menuDomainLowLatency);
  }
  // Queue fullness: bytes always count; the time level counts only for a time-bound queue.
  { QueueFullness q;assert(!q.IsFull());
    q.dataLevel=100;assert(q.IsFull());q.m_timeBound=true;assert(q.IsFull());
    q.dataLevel=40;q.timeLevel=100;q.m_timeBound=false;assert(!q.IsFull());
    q.m_timeBound=true;assert(q.IsFull());q.timeLevel=99;assert(!q.IsFull());
  }
  // Draining must include decoder, renderer and sink output despite empty input queues.
  { CVideoPlayer p;p.video.eos=false;p.audio.sink=DVD_MSEC_TO_TIME(200);p.m_renderManager.queued=1;
    auto start=TestClock::now();onSleep=[&] {
      auto elapsed=TestClock::now()-start;
      if(elapsed>=50ms)p.video.eos=true;
      if(elapsed>=150ms)p.audio.sink=0;
      if(elapsed>=200ms)p.m_renderManager.queued=0;
    };
    p.DrainStreamsAtBoundary();onSleep={};
    assert(p.video.drainMessages==1 && TestClock::now()-start>=275ms);
  }
  { CVideoPlayer p;p.video.eos=false;auto start=TestClock::now();
    p.DrainStreamsAtBoundary();assert(TestClock::now()-start<=1525ms); // stalled decoder is bounded
  }
  { CVideoPlayer p;p.m_messenger.pending=true;auto start=TestClock::now();
    p.DrainStreamsAtBoundary();assert(TestClock::now()==start); // user/control work remains responsive
  }
  { CVideoPlayer p;p.m_playSpeed=0;p.DrainStreamsAtBoundary();assert(p.video.drainMessages==0);
    p.m_playSpeed=DVD_PLAYSPEED_NORMAL;p.m_bAbortRequest=true;p.DrainStreamsAtBoundary();
    assert(p.video.drainMessages==0);
  }
  { CVideoPlayer p;p.m_CurrentVideo.id=-1;p.m_CurrentAudio.id=-1;
    auto start=TestClock::now();p.DrainStreamsAtBoundary();
    assert(p.video.drainMessages==0 && TestClock::now()==start);
  }
  // A timed still after a generic still initializes once; duplicates cannot extend it.
  { CVideoPlayer p;int on=1,seconds=3,off=0;p.HandleStill(BD_EVENT_STILL,&on);
    assert(p.m_dvd.state==DVDSTATE_STILL && !p.m_bdTimedStill);
    p.m_CurrentVideo.stream=&p;p.video.outputDelay=DVD_MSEC_TO_TIME(250);
    p.HandleStill(BD_EVENT_STILL_TIME,&seconds);auto start=p.m_dvd.iDVDStillStartTime;
    assert(p.m_bdTimedStill && p.m_dvd.iDVDStillTime==3250ms);
    advance(1000ms);p.HandleStill(BD_EVENT_STILL_TIME,&seconds);
    assert(p.m_dvd.iDVDStillStartTime==start && p.m_dvd.iDVDStillTime==3250ms);
    p.HandleStill(BD_EVENT_STILL,&off);assert(!p.m_bdTimedStill && p.m_dvd.state==DVDSTATE_NORMAL);
    seconds=0;p.HandleStill(BD_EVENT_STILL_TIME,&seconds);
    assert(p.m_bdTimedStill && p.m_dvd.iDVDStillTime==0ms);
  }
  // Menu compositions remain visible with subtitles disabled and negative clip timestamps.
  { CVideoPlayerVideo v;v.m_bRenderSubs=false;VideoPicture picture;
    auto group=std::make_shared<CDVDOverlayGroup>();group->menu=true;group->type=DVDOVERLAY_TYPE_GROUP;
    group->iPTSStartTime=-1;auto image=std::make_shared<CDVDOverlay>();image->menu=true;
    group->m_overlays={image};v.container.items={group};v.ProcessOverlays(&picture,-500000);
    assert(v.m_renderManager.rendered.size()==1 && group->m_3dSubtitleDepth==0);
  }
  // Ordinary and forced subtitles still honor visibility and authored intervals.
  { CVideoPlayerVideo v;VideoPicture picture;auto sub=std::make_shared<CDVDOverlay>();
    sub->iPTSStartTime=100;sub->iPTSStopTime=200;v.container.items={sub};
    v.ProcessOverlays(&picture,99);assert(v.m_renderManager.rendered.empty());
    v.ProcessOverlays(&picture,100);assert(v.m_renderManager.rendered.size()==1);
    v.m_renderManager.rendered.clear();v.ProcessOverlays(&picture,200);assert(v.m_renderManager.rendered.empty());
    v.m_bRenderSubs=false;v.ProcessOverlays(&picture,150);assert(v.m_renderManager.rendered.empty());
    sub->bForced=true;v.ProcessOverlays(&picture,150);assert(v.m_renderManager.rendered.size()==1);
  }
}
'''

if __name__ == '__main__':
    main()
