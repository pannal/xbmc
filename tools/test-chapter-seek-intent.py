#!/usr/bin/env python3
"""Production chapter routing, intent retirement and receipt ordering checks.

Executes the chapter prefix of Seek, whole SeekChapter/GetPreviousChapter, and
final locked publication fragment of UpdatePlayState. Recording queue, chapter
clock and lifecycle adapters are explicit. The additional complete chapter
handler uses extracted production queue/flush/receipt methods from the existing
lifecycle fixture; demux, codec device completion and callbacks are recording
adapters. This is not actual device playback or a VFS timing test.
Negative controls must compile and fail behavioral assertions under ASan/UBSan.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness(parent):
    seek = function(parent, 'void CVideoPlayer::Seek(bool bPlus,')
    prefix = seek[:seek.index('\n  int64_t seekTarget;')]
    methods = prefix + '\n  ++timedSeeks;\n}\n'
    methods += '\n'.join(function(parent, signature) for signature in (
        'int CVideoPlayer::SeekChapter(', 'int CVideoPlayer::GetPreviousChapter('))
    update = function(parent, 'void CVideoPlayer::UpdatePlayState(')
    # Keep the actual final state lock and retirement statements, avoiding the
    # unrelated GUI/clock/cache-statistics providers earlier in UpdatePlayState.
    tail = update[update.rindex('  std::unique_lock<CCriticalSection> lock(m_StateSection);'):]
    methods += '\nvoid CVideoPlayer::Publish(State state) {\n' + tail
    return PRELUDE + methods + TESTS


PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
using CCriticalSection = std::recursive_mutex;
constexpr int MSGQ_OK=1, MSGQ_NOT_INITIALIZED=-1, CACHESTATE_DONE=0, CACHESTATE_FULL=1;
struct CDVDMsg {enum {PLAYER_SEEK_CHAPTER=1};};
struct CDVDMsgPlayerSeekChapter {int chapter; explicit CDVDMsgPlayerSeekChapter(int c):chapter(c){}};
struct Messenger {
  bool accept=true; int queued=0; std::vector<int> requests; std::function<void()> beforePut;
  int Put(std::shared_ptr<CDVDMsgPlayerSeekChapter> msg) {
    if(beforePut) beforePut();
    if(!accept) return MSGQ_NOT_INITIALIZED;
    requests.push_back(msg->chapter); ++queued; return MSGQ_OK;
  }
  int GetPacketCount(int) const {return queued;}
};
struct State {bool canseek=true; int chapter=7;};
struct IDVDStreamPlayer {enum {SYNC_STARTING,SYNC_WAITSYNC,SYNC_INSYNC};};
struct CurrentStream {int id=0,syncState=IDVDStreamPlayer::SYNC_INSYNC;};
class CVideoPlayer {
public:
  State m_State; CCriticalSection m_StateSection; Messenger m_messenger;
  CurrentStream m_CurrentVideo,m_CurrentAudio;
  std::atomic<int> m_chapterSeekTarget{0}; int m_caching=CACHESTATE_DONE;
  bool lifecycle=false, playerThread=true; int64_t time=3636289; int count=12,timedSeeks=0;
  int GetChapter(){return m_State.chapter;} int GetChapterCount(){return count;}
  int64_t GetTime(){return time;} int64_t GetChapterPos(int c){return c==7 ? 3633 : c*400;}
  bool IsCurrentThread() const{return playerThread;}
  bool ParentLifecyclePending() const{return lifecycle;}
  bool OtherThreadCanLock() {
    bool available=false;
    std::thread t([&]{available=m_StateSection.try_lock();if(available)m_StateSection.unlock();});
    t.join();return available;
  }
  void SynchronizeDemuxer(){assert(OtherThreadCanLock());}
  void Seek(bool,bool,bool); int SeekChapter(int); int GetPreviousChapter(); void Publish(State);
  void Back(){Seek(false,true,true);} void Next(){Seek(true,true,true);}
  void Caught(int chapter){m_messenger.queued=0;m_caching=CACHESTATE_DONE;lifecycle=false;Publish({true,chapter});}
};
'''

TESTS = r'''
int main(){
  // Exact reporter state: chapter 7 within its five-second grace, both actions
  // arrive before demux catches up. Original code requests 6 twice.
  {CVideoPlayer p;p.Back();p.Back();assert((p.m_messenger.requests==std::vector<int>{6,5}));}
  {CVideoPlayer p;p.Next();p.Next();assert((p.m_messenger.requests==std::vector<int>{8,9}));}
  {CVideoPlayer p;p.Back();p.Next();assert((p.m_messenger.requests==std::vector<int>{6,7}));}
  {CVideoPlayer p;p.Next();p.Back();assert((p.m_messenger.requests==std::vector<int>{8,7}));}
  // Ordinary Back after the grace restarts the current chapter. A second
  // pending Back then moves to the previous one, including while paused.
  {CVideoPlayer p;p.time=3640000;p.Back();p.Back();assert((p.m_messenger.requests==std::vector<int>{7,6}));}
  {CVideoPlayer p;p.m_State.chapter=1;p.time=400001;p.Back();p.Back();assert((p.m_messenger.requests==std::vector<int>{0,0}));assert(p.m_chapterSeekTarget==1);}
  {CVideoPlayer p;p.m_State.chapter=11;p.Next();p.Next();assert((p.m_messenger.requests==std::vector<int>{12}));assert(p.timedSeeks==1);}
  // No retirement during the blocked HTTP source seek, child flush or before
  // the marker. A queued newer request protects even an old state snapshot.
  {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.m_caching=CACHESTATE_FULL;
   p.Publish({true,7});assert(p.m_chapterSeekTarget==6);
   p.m_caching=CACHESTATE_DONE;p.lifecycle=true;p.Publish({true,7});assert(p.m_chapterSeekTarget==6);
   p.lifecycle=false;p.m_messenger.queued=1;p.Publish({true,7});assert(p.m_chapterSeekTarget==6);
   p.m_messenger.queued=0;p.Publish({true,5});assert(p.m_chapterSeekTarget==6);
   p.Publish({true,6});assert(p.m_chapterSeekTarget==0);
   p.time=2401000;p.Back();assert(p.m_messenger.requests.back()==5);}
  // Completed intent cannot bias subsequent natural chapter progress or the
  // five-second restart grace. Tiny chapters may skip the exact target sample.
  {CVideoPlayer p;p.Back();p.Caught(6);p.Publish({true,8});p.time=3210000;
   p.Back();assert(p.m_messenger.requests.back()==8);}
  {CVideoPlayer p;p.Next();p.Caught(9);assert(p.m_chapterSeekTarget==0);}
  // CACHESTATE_DONE alone is insufficient: a pause/speed request or cache
  // timeout can publish it before the newly flushed A/V startup resynchronizes
  // the old clock. Backward intent must survive that old chapter sample.
  for(int sync:{IDVDStreamPlayer::SYNC_STARTING,IDVDStreamPlayer::SYNC_WAITSYNC}) {
    {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.m_CurrentVideo.syncState=sync;
     p.Publish({true,7});assert(p.m_chapterSeekTarget==6 && "video_startup_retains_backward_intent");}
    {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.m_CurrentAudio.syncState=sync;
     p.Publish({true,7});assert(p.m_chapterSeekTarget==6 && "audio_startup_retains_backward_intent");}
    {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.m_CurrentVideo.id=-1;p.m_CurrentAudio.syncState=sync;
     p.Publish({true,7});assert(p.m_chapterSeekTarget==6 && "audio_only_startup_retains_intent");}
    {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.m_CurrentAudio.id=-1;p.m_CurrentVideo.syncState=sync;
     p.Publish({true,7});assert(p.m_chapterSeekTarget==6 && "video_only_startup_retains_intent");}
  }
  // Absent streams do not impose a startup obligation. The present stream
  // must be synchronized before successful intent can retire.
  for(bool audioOnly:{false,true}) {
    CVideoPlayer p;p.Back();p.m_messenger.queued=0;
    auto& absent=audioOnly?p.m_CurrentVideo:p.m_CurrentAudio;
    absent.id=-1;absent.syncState=IDVDStreamPlayer::SYNC_STARTING;
    p.Publish({true,6});assert(p.m_chapterSeekTarget==0 && "absent_stream_not_required");
  }
  // Abandoned/rejected demux seek retires when normal playback resumes.
  {CVideoPlayer p;p.Back();p.Caught(7);assert(p.m_chapterSeekTarget==0);}
  // Public clock refresh must not inspect the player-owned cache/lifecycle
  // state to retire intent. Its later player-thread publication may do so.
  {CVideoPlayer p;p.Back();p.m_messenger.queued=0;p.playerThread=false;
   p.Publish({true,7});assert(p.m_chapterSeekTarget==6);
   p.playerThread=true;p.Publish({true,7});assert(p.m_chapterSeekTarget==0);}
  // Failed queue insertion does not manufacture an intent or replace an
  // existing one. Publication/queue insertion hold the same state lock;
  // SynchronizeDemuxer checks that it has been released before waiting.
  {CVideoPlayer p;p.m_messenger.accept=false;p.Back();assert(p.m_chapterSeekTarget==0);}
  {CVideoPlayer p;p.Back();p.m_messenger.accept=false;p.Back();assert(p.m_chapterSeekTarget==6);}
  {CVideoPlayer p;p.m_messenger.beforePut=[&]{assert(!p.OtherThreadCanLock());};
   p.Back();p.Publish({true,7});assert(p.m_chapterSeekTarget==6);
   p.Caught(6);assert(p.m_chapterSeekTarget==0);}
}
'''


def run(source, out, name, failure=False, assertion=None):
    cpp=out/(name+'.cpp'); exe=out/name; cpp.write_text(source)
    subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                    '-Wno-unused-parameter','-pthread','-fsanitize=address,undefined',
                    '-fno-omit-frame-pointer','-no-pie',str(cpp),'-o',str(exe)],check=True)
    result=subprocess.run([str(exe)],capture_output=True,text=True)
    if failure:
        assert result.returncode==-6 and 'Assertion' in result.stderr, result.stderr
        assert 'Sanitizer' not in result.stderr and 'runtime error:' not in result.stderr
        if assertion:
            assert assertion in result.stderr, result.stderr
    elif result.returncode:
        raise RuntimeError(result.stderr)
    print(name+': PASS'+(' (behavioral rejection)' if failure else ''))


def chapter_handler_harness(parent):
    """Extend the existing actual queue/flush fixture without changing its tests.

    The complete production chapter handler, request publication and retirement
    fragment run against real extracted Put/Get/FlushBuffers/ContinueParentLifecycle.
    The demux seek, codec device completion, clock and callbacks are recording
    adapters. The first source seek itself queues the second Back, matching the
    capture's arrival order; two requests queued before dispatch may coalesce.
    """
    lifecycle = runpy.run_path(str(ROOT / 'tools/test-player-lifecycle.py'))
    source = lifecycle['harness']()
    messages = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
    message = function(messages, 'class CDVDMsgPlayerSeekChapter') + ';'
    source = source.replace('PLAYER_ABORT, PLAYER_SEEK,', 'PLAYER_ABORT, PLAYER_SEEK_CHAPTER, PLAYER_SEEK,', 1)
    source = source.replace('struct CDVDMsgGeneralSynchronize : CDVDMsg {',
                            message + '\nstruct CDVDMsgGeneralSynchronize : CDVDMsg {', 1)
    source = source.replace('struct IPlayerCallback { int starts=0; void OnAVStarted(int) {++starts;} };', r'''
struct IPlayerCallback {
  int starts=0; std::vector<int> chapters;
  void OnAVStarted(int) {++starts;}
  void OnPlayBackSeekChapter(int chapter) {chapters.push_back(chapter);}
};
constexpr int DVDSTREAM_TYPE_BLURAY=1;
struct CDVDInputStream {
  struct IChapter {bool accepted=false; int calls=0; bool SeekChapter(int) {++calls;return accepted;}};
};
''', 1)
    source = source.replace('struct ProcessInfo { double MinTempoPlatform()',
                            'struct ProcessInfo { std::vector<int> seekOffsets; void SeekFinished(int offset) {seekOffsets.push_back(offset);} double MinTempoPlatform()', 1)
    source = source.replace('struct Input {bool realtime=false; bool IsRealtime() const {return realtime;} } input;',
                            'struct Input {bool realtime=false,bluray=false; bool IsStreamType(int) const {return bluray;} CDVDInputStream::IChapter* chapter=nullptr; CDVDInputStream::IChapter* GetIChapter() {return chapter;} bool IsRealtime() const {return realtime;} } input;', 1)
    source = source.replace('struct Demux {int speed=0; void SetSpeed(int v) {speed=v;} } demux;', r'''
  struct Demux {
    int speed=0; bool accepted=true; std::vector<int> requests;
    std::function<void()> duringSeek;
    void SetSpeed(int v) {speed=v;}
    bool SeekChapter(int chapter, double* start, double* time=nullptr) {
      requests.push_back(chapter); const bool result=accepted;
      auto call=std::move(duringSeek); duringSeek={}; if(call) call();
      if(result) {
        *start=chapter==6 ? 2960165000.0 : chapter*400.0*DVD_TIME_BASE;
        if(time)*time=chapter==6 ? 2400123.456 : chapter*400000.0;
      }
      return result;
    }
  } demux;
''', 1)
    source = source.replace('struct {bool streamsReady=false; double time_offset=0;} m_State;',
                            'struct State {bool streamsReady=false; double time_offset=0; bool canseek=true; int chapter=7;}; State m_State;', 1)
    source = source.replace('void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); ++cacheChanges;}',
                            'int m_caching=CACHESTATE_DONE; void SetCaching(int value) {assert(value==CACHESTATE_FLUSH || value==CACHESTATE_DONE); m_caching=value; ++cacheChanges;}', 1)
    chapter = function(parent, 'else if (pMsg->IsType(CDVDMsg::PLAYER_SEEK_CHAPTER))')
    source = source.replace('      else ++other;', '      ' + chapter + '\n      else ++other;', 1)
    source = source.replace('  CDVDMessageQueue queue;\n', r'''
  CDVDMessageQueue queue;
  CDVDMessageQueue& m_messenger=queue;
  CCriticalSection m_StateSection; std::atomic<int> m_chapterSeekTarget{0};
  int64_t time=3636289; int count=12,timedSeeks=0,synchronizes=0;
  int GetChapter() {return m_State.chapter;} int GetChapterCount() {return count;}
  int64_t GetTime() {return time;} int64_t GetChapterPos(int c) {return c==7 ? 3633 : c*400;}
  bool IsCurrentThread() const {return true;}
  void SynchronizeDemuxer() {++synchronizes;}
  CVideoSeekQueueState GetVideoSeekQueueState();
  void Seek(bool,bool,bool); int SeekChapter(int); int GetPreviousChapter(); void Publish(State);
  void Back() {Seek(false,true,true);} void Next() {Seek(true,true,true);}
''', 1)
    seek = function(parent, 'void CVideoPlayer::Seek(bool bPlus,')
    methods = seek[:seek.index('\n  int64_t seekTarget;')] + '\n  ++timedSeeks;\n}\n'
    methods += '\n'.join(function(parent, signature) for signature in (
        'int CVideoPlayer::SeekChapter(', 'int CVideoPlayer::GetPreviousChapter(',
        'CVideoSeekQueueState CVideoPlayer::GetVideoSeekQueueState('))
    update = function(parent, 'void CVideoPlayer::UpdatePlayState(')
    tail = update[update.rindex('  std::unique_lock<CCriticalSection> lock(m_StateSection);'):]
    methods += '\nvoid CVideoPlayer::Publish(State state) {\n' + tail
    old_main = function(source, 'int main()')
    source = source.replace(old_main, methods.replace('CVideoPlayer::', 'Parent::') +
                            '\n' + CHAPTER_HANDLER_TESTS + '\nint main() {\nchapter_handlers();' +
                            old_main[old_main.index('{')+1:-1] +
                            '\n}\n', 1)
    return source


CHAPTER_HANDLER_TESTS = r'''
static void chapter_handlers() {
  // Ordinary file/provider completion keeps its prior landing-based UI offset.
  {CVideoPlayerVideo video; Parent p(video);p.Back();p.HandleMessages();
   complete(video);assert(p.ContinueParentLifecycle());
   assert(std::find(p.m_processInfo->seekOffsets.begin(),p.m_processInfo->seekOffsets.end(),
                    2960165-3636289)!=p.m_processInfo->seekOffsets.end() && "generic_callback_keeps_landing_offset");}
  // A rejected forward target has not been reached and must not remain the
  // base of subsequent Back/Next. This fails even when published chapter=7.
  {CVideoPlayerVideo video; Parent p(video); p.demux.accepted=false;
   p.Next(); p.HandleMessages(); assert(p.m_chapterSeekTarget==0 && "failed_forward_cleared");
   assert(!p.ParentLifecyclePending() && p.m_callback.chapters.empty());
   p.Back(); p.HandleMessages(); assert(p.demux.requests.back()==6);
   assert(p.m_chapterSeekTarget==0 && "failed_backward_cleared");}
  // Normalized chapter zero denotes chapter one. Failure cleanup uses the
  // same normalization as actual SeekChapter publication.
  {CVideoPlayerVideo video; Parent p(video); p.demux.accepted=false;
   p.SeekChapter(0); p.HandleMessages(); assert(p.m_chapterSeekTarget==0 && "failed_normalized_cleared");}
  // A new same-target request arriving inside source Seek still owns intent.
  // The old failure must not erase it before the next accepted seek executes.
  {CVideoPlayerVideo video; Parent p(video); p.demux.accepted=false;
   p.demux.duringSeek=[&]{p.SeekChapter(8);p.demux.accepted=true;};
   p.Next(); p.HandleMessages(); assert((p.demux.requests==std::vector<int>{8,8}));
   assert(p.m_chapterSeekTarget==8 && "queued_same_intent_retained");
   assert(p.ParentLifecyclePending() && p.m_callback.chapters.empty());}
  {CVideoPlayerVideo video; Parent p(video); p.demux.accepted=false;
   p.demux.duringSeek=[&]{p.SeekChapter(9);p.demux.accepted=true;};
   p.Next(); p.HandleMessages(); assert((p.demux.requests==std::vector<int>{8,9}));
   assert(p.m_chapterSeekTarget==9 && "queued_newer_intent_retained");}
  // The alternate input-stream chapter API may accept when the demux rejects.
  {CVideoPlayerVideo video; Parent p(video); p.demux.accepted=false;
   CDVDInputStream::IChapter inputChapter; inputChapter.accepted=true;
   p.input.chapter=&inputChapter; p.Next(); p.HandleMessages();
   assert(inputChapter.calls==1 && p.ParentLifecyclePending());
   assert(p.m_chapterSeekTarget==8 && "input_accepted_intent_retained");}
  // A successful forward flush receipt completes before the chapter marker.
  // Completion alone does not retire the still-unreached intent.
  {CVideoPlayerVideo video; Parent p(video); p.Next(); p.HandleMessages();
   assert(p.m_chapterSeekTarget==8 && "accepted_intent_retained");
   assert(!p.ContinueParentLifecycle() && p.m_callback.chapters.empty() && "accepted_callback_waits_receipt");
   complete(video); assert(p.ContinueParentLifecycle());
   assert((p.m_callback.chapters==std::vector<int>{8}));
   p.SetCaching(CACHESTATE_DONE); auto state=p.m_State;state.chapter=7;p.Publish(state);
   assert(p.m_chapterSeekTarget==8 && "pre_marker_intent_retained");
   state.chapter=8;p.Publish(state);
   assert(p.m_chapterSeekTarget==8 && "receipt_before_startup_keeps_intent");
   p.m_CurrentVideo.syncState=p.m_CurrentAudio.syncState=IDVDStreamPlayer::SYNC_INSYNC;
   p.Publish(state);assert(p.m_chapterSeekTarget==0);}
  // Exact arrival order: chapter 6 is already in source Seek when Back queues
  // chapter 5. Actual parent queue mode 2 holds it until the first native
  // flush receipt completes; both accurate targets and callbacks are serial.
  {CVideoPlayerVideo video; Parent p(video); p.input.bluray=true; p.demux.duringSeek=[&]{p.Back();};
   p.Back(); p.HandleMessages();
   assert((p.demux.requests==std::vector<int>{6}) && "second_seek_waits_first_receipt");
   assert(p.queue.GetPacketCount(CDVDMsg::PLAYER_SEEK_CHAPTER)==1);
   assert(p.m_chapterSeekTarget==5 && p.m_callback.chapters.empty());
   assert(p.m_CurrentVideo.startpts==2960165000.0 && "first_seek_accurate");
   const auto first=video.GetFlushRequest(); assert(first && !p.ContinueParentLifecycle());
   p.HandleMessages(); assert((p.demux.requests==std::vector<int>{6}));
   complete(video); assert(p.ContinueParentLifecycle());
   assert((p.m_callback.chapters==std::vector<int>{6}) && "first_callback_after_receipt");
   assert(std::find(p.m_processInfo->seekOffsets.begin(),p.m_processInfo->seekOffsets.end(),
                    2400123-3636289)!=p.m_processInfo->seekOffsets.end() && "callback_uses_title_marker_not_decoder_pts");
   assert(p.m_videoRecoveryStartTime==2400123.456 && "bd_recovery_keeps_title_target");
   p.SetCaching(CACHESTATE_DONE);p.Publish(p.m_State);
   assert(p.m_chapterSeekTarget==5 && "queued_second_intent_not_retired");
   p.HandleMessages(); assert((p.demux.requests==std::vector<int>{6,5}));
   const auto second=video.GetFlushRequest(); assert(second && second!=first);
   assert(first->state==CVideoFlushRequest::State::COMPLETED);
   assert(second->state==CVideoFlushRequest::State::PENDING);
   assert(p.m_CurrentVideo.startpts==2000000000.0 && "second_seek_accurate");
   assert((p.m_callback.chapters==std::vector<int>{6}));
   assert(!p.ContinueParentLifecycle());complete(video);assert(p.ContinueParentLifecycle());
   assert((p.m_callback.chapters==std::vector<int>{6,5}) && "second_callback_after_receipt");
   assert(video.m_pVideoCodec->resets==2);
   p.m_CurrentVideo.syncState=p.m_CurrentAudio.syncState=IDVDStreamPlayer::SYNC_INSYNC;
   p.SetCaching(CACHESTATE_DONE);auto state=p.m_State;state.chapter=5;p.Publish(state);
   assert(p.m_chapterSeekTarget==0);}
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    parser.add_argument('--source',type=Path,help='alternative complete VideoPlayer.cpp (before witness)')
    args=parser.parse_args()
    parent=(args.source or ROOT/'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    source=harness(parent)
    with tempfile.TemporaryDirectory(prefix='chapter-seek-intent-') as tmp:
        out=Path(tmp);run(source,out,'chapter-intent')
        handler_source=chapter_handler_harness(parent)
        run(handler_source,out,'chapter-handler-lifecycle')
        if args.negative_controls:
            controls=[
                ('ignore-backward-intent','if (pending > 0)','if (pending > GetChapter())'),
                ('ignore-mixed-forward-intent','pending > 0 ? pending : GetChapter()','std::max(GetChapter(), pending)'),
                ('keep-completed-intent','m_chapterSeekTarget.compare_exchange_strong(pendingChapter, 0);','(void)pendingChapter;'),
                ('retire-during-source-wait','m_caching == CACHESTATE_DONE','true'),
                ('retire-during-child-flush','!ParentLifecyclePending()','true'),
                ('retire-queued-intent','m_messenger.GetPacketCount(CDVDMsg::PLAYER_SEEK_CHAPTER) == 0','true'),
                ('retire-before-marker','state.chapter >= pendingChapter','true'),
                ('retire-from-ui-thread','IsCurrentThread() &&',''),
                ('publish-rejected-intent','if (m_messenger.Put(std::make_shared<CDVDMsgPlayerSeekChapter>(iChapter)) == MSGQ_OK)',
                 'if ((m_messenger.Put(std::make_shared<CDVDMsgPlayerSeekChapter>(iChapter)), true))'),
                ('unlocked-request-publication','std::unique_lock<CCriticalSection> lock(m_StateSection);',''),
            ]
            for name,old,new in controls:
                assert old in source,name
                # The request lock is first; retain the state retirement lock.
                run(source.replace(old,new,1),out,name,True)
            startup_controls=[
                ('retire-before-video-sync',
                 '(m_CurrentVideo.id < 0 || m_CurrentVideo.syncState == IDVDStreamPlayer::SYNC_INSYNC)',
                 'video_startup_retains_backward_intent'),
                ('retire-before-audio-sync',
                 '(m_CurrentAudio.id < 0 || m_CurrentAudio.syncState == IDVDStreamPlayer::SYNC_INSYNC)',
                 'audio_startup_retains_backward_intent'),
            ]
            for name,old,assertion in startup_controls:
                assert old in source,name
                run(source.replace(old,'true',1),out,name,True,assertion)
            handler_controls=[
                ('recovery-keeps-old-origin','m_videoRecoveryStartTime = chapterTime;','m_videoRecoveryStartTime = m_videoRecoveryStartTime;', 'bd_recovery_keeps_title_target'),
                ('native-ui-offset-for-files','const int64_t offset = blurayChapter','const int64_t offset = true','generic_callback_keeps_landing_offset'),
                ('raw-origin-in-ui-offset','static_cast<int64_t>(chapterTime) - beforeSeek','static_cast<int64_t>(chapterTime) + 4199000 - beforeSeek', 'callback_uses_title_marker_not_decoder_pts'),
                ('keep-rejected-forward-intent','m_chapterSeekTarget.compare_exchange_strong(failedChapter, 0);','(void)failedChapter;', 'failed_forward_cleared'),
                ('erase-queued-same-failure','if (m_messenger.GetPacketCount(CDVDMsg::PLAYER_SEEK_CHAPTER) == 0)','if (true)', 'queued_same_intent_retained'),
                ('erase-accepted-intent','if (!chapterSeekAccepted)','if (chapterSeekAccepted || !chapterSeekAccepted)', 'queued_same_intent_retained'),
                ('unnormalized-failed-intent','int failedChapter = std::max(msg.GetChapter(), 1);','int failedChapter = msg.GetChapter();', 'failed_normalized_cleared'),
                ('dispatch-chapter-during-flush','ParentLifecyclePending() ? 2 : 0','0', 'second_seek_waits_first_receipt'),
                ('chapter-flush-inaccurate','FlushBuffers(start, true, true, [this, start, blurayChapter, chapterTime, chapter = msg.GetChapter()]','FlushBuffers(start, false, true, [this, start, blurayChapter, chapterTime, chapter = msg.GetChapter()]', 'first_seek_accurate'),
                ('chapter-callback-before-receipt','if (request && request->state == CVideoFlushRequest::State::PENDING)','if (false && request && request->state == CVideoFlushRequest::State::PENDING)', 'accepted_callback_waits_receipt'),
            ]
            for name,old,new,assertion in handler_controls:
                assert old in handler_source,name
                run(handler_source.replace(old,new,1),out,name,True,assertion)


if __name__=='__main__':main()
