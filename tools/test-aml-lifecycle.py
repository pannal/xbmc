#!/usr/bin/env python3
"""Host production AML lifecycle/recovery and wrapper-cleanup checks.

Extracts actual public lifecycle methods, full AddData and wrapper Close/reset cleanup;
uses the real AMLSession and full PollFrame/SetPollDevice and SetSpeed methods.
Controlled write callbacks exercise display fencing inside an admitted AddData;
requested/applied speed and wrapper cleanup are checked across display fencing. The poll syscall
and sync event are recording stubs. Internal decoder Open/Reset/Close, packet writers,
FFmpeg allocation and platform settings are controlled stubs. Generation tests
model the documented successful-open increment; they do not execute real Open.
The real CloseInternal config-free/reset fragment is extracted; its remaining
device, hold and DV effects are stubs. No driver, message dispatcher, GL, player
clock or device timing is exercised.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    codec = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
    wrapper = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    methods = '\n'.join(function(codec, signature) for signature in [
        'bool CAMLCodec::OpenDecoder()', 'bool CAMLCodec::CloseDecoder(',
        'bool CAMLCodec::Reset()', 'bool CAMLCodec::ReopenDecoder()',
        'bool CAMLCodec::BeginLifecycle(', 'bool CAMLCodec::ContinueLifecycle()',
        'void CAMLCodec::WaitForLifecycle()', 'bool CAMLCodec::AddData(',
        'int CAMLCodec::PollFrame(', 'void CAMLCodec::SetPollDevice(',
        'void CAMLCodec::SetSpeed(', 'void CAMLCodec::SetSpeedInternal('])
    methods += '\n' + '\n'.join(function(wrapper, signature) for signature in [
        'void CDVDVideoCodecAmlogic::Close(', 'void CDVDVideoCodecAmlogic::Reset(',
        'bool CDVDVideoCodecAmlogic::LifecyclePending()',
        'bool CDVDVideoCodecAmlogic::LifecycleFailed()',
        'bool CDVDVideoCodecAmlogic::ContinueLifecycle()', 'void CDVDVideoCodecAmlogic::FinishReset()'])
    prelude = PRELUDE.replace('@FAILED_METHOD@', function(header, 'bool          LifecycleFailed()'))
    close = function(codec, 'void CAMLCodec::CloseDecoderInternal()')
    config_free = close[close.index('  free(am_private->vcodec.config);'):close.index('  // return tsync')]
    prelude = prelude.replace('@CONFIG_FREE@', config_free)
    prelude = prelude.replace('@DV_CANCEL@', function((ROOT / 'xbmc/utils/AMLUtils.cpp').read_text(), 'void aml_dv_cancel_deferred_session('))
    return prelude + methods + TESTS


def compile_run(source, negative=False):
    with tempfile.TemporaryDirectory(prefix='aml-lifecycle-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                   '-Werror', '-pthread', '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'),
                   '-o', str(out / 'test')]
        if not negative:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        subprocess.run(command, check=True)
        result = subprocess.run([str(out / 'test')], capture_output=negative,
                                text=True, timeout=20, check=not negative)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness()
    if args.negative_controls:
        controls = [
            ('speed marks applied before driver update', 'm_requestedSpeed = speed;',
             'm_requestedSpeed = speed; m_speed = speed;'),
            ('decoder ignores closed display admission', 'if (!operation)',
             'if (false && !operation)'),
            ('decoder permit ends before write', 'auto operation = m_session.AcquireDecoder();',
             'auto operation = [](CAMLSession& session) { auto held=session.AcquireDecoder(); '
             'return static_cast<bool>(held); }(m_session);'),
            ('mutation skips outer admission',
             'if (!CAMLSession::TryBeginNative(m_nativeLifecycleRequest))',
             'if (false && !CAMLSession::TryBeginNative(m_nativeLifecycleRequest))'),
            ('pending reset cleanup runs early',
             'm_Codec && !m_Codec->ContinueLifecycle()',
             'false && m_Codec && !m_Codec->ContinueLifecycle()'),
            ('internal loop recovery bypasses gate',
             '// Decoder got stuck; Reset\n    Reset();',
             '// Decoder got stuck; Reset\n    ResetInternal();'),
            ('failed open reports success', 'return success;', 'return success || true;'),
            ('partial open skips unwind', 'if (!success && m_decoderNeedsClose)',
             'if (false && !success && m_decoderNeedsClose)'),
            ('cleanup keeps freed config pointer', 'am_private->vcodec.config = nullptr;',
             '(void)am_private->vcodec.config;'),
            ('completed failure forgotten', 'if (m_lifecycleFailed || m_session.DisplayBlocked())',
             'if (m_session.DisplayBlocked())'),
            # The wrapper now also calls core ContinueLifecycle unconditionally;
            # removing its first guard is redundant, so inject false completion.
            ('wrapper reports terminal failure complete', 'if (LifecycleFailed())\n    return false;',
             'if (LifecycleFailed())\n    return true;'),
            ('poll accepts foreign session', '!m_session.Matches(permit, permit.Epoch())',
             'false'),
            ('poll changes timeout policy', 'poll(codec_poll_fd, 1, 50);', 'poll(codec_poll_fd, 1, 0);'),
            ('poll errors falsely pace', 'return pollResult == 0 ||', 'return true || pollResult == 0 ||'),
            ('poll ignores error event flags',
             '(codec_poll_fd[0].revents & (POLLERR | POLLHUP | POLLNVAL)) == 0', 'true'),
        ]
        for name, before, after in controls:
            assert before in source, name
            compile_run(source.replace(before, after, 1), negative=True)
            print('Rejected runtime negative control:', name)
    else:
        compile_run(source)
        print('AML lifecycle: PASS (production lifecycle, poll, speed, full AddData and wrapper cleanup; '
              'ASan/UBSan; stub decoder internals and packet/device interfaces)')


PRELUDE = r'''
#include "cores/VideoPlayer/DVDCodecs/Video/AMLSession.h"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <future>
#include <functional>
#include <list>
#include <poll.h>
#define poll fixture_poll
#include <string>
#include <tuple>
#include <vector>
using namespace std::chrono_literals;
constexpr int LOGDEBUG=0,LOGVIDEO=1,LOGERROR=2,LOGWARNING=3,LOGINFO=4,LOGAVTIMING=5;
constexpr const char* __MODULE_NAME__="fixture";
void aml_kodi_reset_cd_cs() {}
struct CLog {template<class... T> static void Log(T&&...) {}};
std::mutex pollSyncMutex;
struct PollCall {int fd;short events;int timeout;};
std::vector<PollCall> polls;
std::function<void()> pollHook;
int pollResult=1;
short pollEvents=POLLOUT;
int poll(pollfd* descriptors,nfds_t count,int timeout) {
  assert(count==1);
  polls.push_back({descriptors[0].fd,descriptors[0].events,timeout});
  if(pollHook) pollHook();
  descriptors[0].revents=pollEvents;
  return pollResult;
}
struct SyncEvent {std::atomic<int> signals{0};void Set(){++signals;}} g_aml_sync_event;
constexpr double DVD_TIME_BASE=1000000,DVD_NOPTS_VALUE=-1000000;
constexpr int PLAYER_SUCCESS=0,STREAM_TYPE_STREAM=1,STATE_HASPTS=1,KEYFRAME_PTS_ONLY=2;
constexpr uint64_t UINT64_0=0;
constexpr unsigned int RW_WAIT_TIME=1;
constexpr int DVD_PLAYSPEED_NORMAL=1000,DVD_PLAYSPEED_PAUSE=0;
constexpr int TRICKMODE_NONE=0,TRICKMODE_FFFB=1,TRICKMODE_I=2;
constexpr int VFORMAT_H264=4,VFORMAT_H264_4K2K=5;
void usleep(unsigned int) {} // Error-policy waits do not sleep in this fixture.
int calc_chunk_size(size_t size) {return static_cast<int>(size);}
struct Packet {uint8_t* data{}; size_t size{}; void* buf{};};
struct am_packet_t {
  uint8_t* data{}; size_t data_size{}; Packet avpkt;
  int newflag{},isvalid{},avduration{}; double avpts{},avdts{};
};
struct Private {
  int video_format=VFORMAT_H264;
  struct {void* config{nullptr}; int config_len{0};} vcodec;
  struct {int dec_mode{0}; uintptr_t param{0};} gcodec;
  struct {size_t size{0}; uint8_t* data{};} hdr_buf;
  am_packet_t am_pkt;
  enum class Write {SUCCESS,STUCK,ERROR};
  Write write{Write::SUCCESS}; int writes{0};
};
void av_buffer_unref(void**) {}
int av_grow_packet(Packet*,size_t) {assert(false && "extradata path is not exercised"); return -1;}
void set_header_info(Private*) {}
std::function<void()> writeHook;
int write_av_packet(Private* state,am_packet_t* packet) {
  if(writeHook) writeHook();
  ++state->writes;
  if(state->write==Private::Write::ERROR) return -1;
  if(state->write==Private::Write::SUCCESS) packet->isvalid=0;
  return PLAYER_SUCCESS;
}
struct Dll {
  std::vector<int> modes;
  template<class Codec> void codec_set_cntl_mode(Codec*,int mode) { modes.push_back(mode); }
};
std::shared_ptr<const void> s_dvPlaybackSession;
int subtitleInvalidations=0;
void aml_subtitle_active_area_invalidate(){++subtitleInvalidations;}
// Cache semantics are exercised with production bodies in test-dv-backend-info.py.
void aml_dv_backend_invalidate(const std::shared_ptr<const void>&) {}
void aml_dv_backend_pause(const std::shared_ptr<const void>&,bool) {}
@DV_CANCEL@
class CAMLCodec {
public:
  enum class Lifecycle {NONE,OPEN,RESET,REOPEN,CLOSE};
  CAMLSession m_session;
  std::shared_ptr<const void> m_dvSession;
  bool m_dvBackendPaused{false};
  int64_t m_dvBackendSampleTime{0};
  Lifecycle m_lifecycle{Lifecycle::NONE};
  CAMLSession::Request m_lifecycleRequest;
  std::shared_ptr<CAMLSession::NativeRequest> m_nativeLifecycleRequest;
  std::function<void()> m_beforeClose;
  bool m_lifecycleFailed{false};
  bool m_speedPending{false};
  int m_speed=DVD_PLAYSPEED_NORMAL,m_requestedSpeed=DVD_PLAYSPEED_NORMAL;
  Dll dll; Dll* m_dll=&dll;
  std::chrono::system_clock::time_point m_tp_last_frame;
  void SetSpeed(int); void SetSpeedInternal(int);
  @FAILED_METHOD@
  bool m_opened{false},openSucceeds{true},m_decoderNeedsClose{false};
  bool acquireDevice{true},deviceAcquired{false},holdActive{false},m_dvOpened{false};
  uint64_t generation{0};
  std::vector<std::string> trace;
  std::function<void()> onClose;
  int m_pollDevice{-1},nextDescriptor{42};
  void CheckVideoHold() {} // Restart receipts exercised by test-aml-restart-hold.py.
  int PollFrame(const CAMLSession::Permit&);
  void SetPollDevice(int);
  bool OpenDecoder(); bool CloseDecoder(std::function<void()> beforeClose = {}); bool Reset(); bool ReopenDecoder();
  bool BeginLifecycle(Lifecycle, std::function<void()> beforeClose = {}); bool ContinueLifecycle(); void WaitForLifecycle();
  bool LifecyclePending() const {return m_lifecycle!=Lifecycle::NONE;}
  void MutationOnly() {
    assert(!m_session.Acquire(m_session.Epoch(),true));
  }
  ~CAMLCodec() {free(storage.vcodec.config);}
  bool OpenDecoderInternal() {
    MutationOnly(); trace.push_back("open");
    if(!acquireDevice) return false;
    m_decoderNeedsClose=true;
    deviceAcquired=holdActive=m_dvOpened=true;
    assert(!storage.vcodec.config);
    storage.vcodec.config=malloc(16); storage.vcodec.config_len=16;
    m_opened=openSucceeds;
    if(m_opened) {++generation;SetPollDevice(nextDescriptor);}
    return m_opened;
  }
  void CloseDecoderInternal() {
    MutationOnly(); trace.push_back("close");
    if(onClose) onClose();
    SetPollDevice(-1);
    m_opened=false;
    @CONFIG_FREE@
    deviceAcquired=holdActive=m_dvOpened=false;
    m_decoderNeedsClose=false;
  }
  void ResetInternal() {
    MutationOnly(); trace.push_back("reset"); m_abort=false;
    if(m_opened) {SetPollDevice(-1);SetPollDevice(nextDescriptor);}
  }
  bool AddData(uint8_t*,size_t,double,double);
  float GetBufferLevel(int,int& data,int& free) {data=10; free=100; return 10.0f;}
  Private storage; Private* am_private{&storage};
  bool m_buffer_level_ready{false},m_decoder_bypass_buffer_ready{false};
  float m_decoder_stream_buffer{0.0f},m_decoder_buffer{0.0f};
  float m_minimum_buffer_level{},m_decoder_minimum_stream_buffer{},m_decoder_minimum_buffer{};
  bool m_no_data_since_reset{true},m_abort{false};
  struct {bool ptsinvalid{false};} m_hints;
  int m_state{0}; double m_cur_pts{DVD_NOPTS_VALUE};
  bool m_wrFailActive{false};
  std::chrono::steady_clock::time_point m_tpWrFailStart,m_tpWrFailLastReset;
};
constexpr int AML_G12B=1,AV_CODEC_ID_H264=27;
int cpu=AML_G12B;
int aml_get_cpufamily_id() {return cpu;}
enum class DOVIELType {NONE,TYPE_FEL};
struct Bitstream {int resets{0}; void ResetStartDecode() {++resets;}};
int freed=0;
namespace KODI {namespace MEMORY {
void AlignedFree(uint8_t* data) {++freed; delete[] data;}
}}
using DLDemuxPacket=std::tuple<uint8_t*,uint32_t,bool,double>;
class CDVDVideoCodecAmlogic {
public:
  std::shared_ptr<CAMLCodec> m_Codec{std::make_shared<CAMLCodec>()};
  struct {DOVIELType dovi_el_type{DOVIELType::NONE}; int codec{AV_CODEC_ID_H264};} m_hints;
  struct {float speed{1.0f}; float GetSpeed() const {return speed;}} m_dataCacheCore;
  bool m_resetCleanupPending{false};
  std::shared_ptr<int> m_videoBufferPool{std::make_shared<int>(1)};
  struct {int iFlags{1};} m_videobuffer;
  int *m_mpeg2_sequence{nullptr},*m_h264_sequence{nullptr},*m_bitparser{nullptr};
  bool m_opened{true};
  std::list<DLDemuxPacket> m_packages;
  int m_el_starvation_count{3},m_mpeg2_sequence_pts{100};
  bool m_has_keyframe{true};
  std::unique_ptr<Bitstream> m_bitstream{std::make_unique<Bitstream>()};
  void Close(); void Reset(); bool LifecyclePending() const; bool LifecycleFailed() const;
  bool ContinueLifecycle(); void FinishReset();
};
'''
TESTS = r'''
void SpeedDuringDisplay() {
  CAMLCodec codec; assert(codec.OpenDecoder());
  codec.SetSpeed(2000);
  assert(codec.m_speed==2000 && !codec.m_speedPending);
  assert((codec.dll.modes==std::vector<int>{TRICKMODE_FFFB}));
  codec.SetSpeed(2000); assert(codec.dll.modes.size()==1);
  const auto epoch=codec.m_session.Epoch();
  auto display=CAMLSession::FenceDisplay();
  codec.SetSpeed(DVD_PLAYSPEED_PAUSE);
  assert(codec.m_speed==2000 && codec.m_speedPending);
  assert(codec.dll.modes.size()==1 && !codec.ContinueLifecycle());
  assert(CAMLSession::TryBeginDisplay(display));
  codec.SetSpeed(DVD_PLAYSPEED_NORMAL); // Last requested speed survives, applied speed does not lie.
  assert(codec.m_speed==2000 && codec.m_speedPending && codec.dll.modes.size()==1);
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::WAITING_FOR_RESET));
  assert(!codec.ContinueLifecycle() && codec.dll.modes.size()==1);
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(codec.ContinueLifecycle() && !codec.m_speedPending);
  assert(codec.m_speed==DVD_PLAYSPEED_NORMAL && codec.m_session.Epoch()==epoch);
  assert((codec.dll.modes==std::vector<int>{TRICKMODE_FFFB,TRICKMODE_NONE}));
  codec.SetSpeed(DVD_PLAYSPEED_PAUSE);
  assert(codec.m_speed==DVD_PLAYSPEED_PAUSE && codec.dll.modes.size()==3);
  CAMLCodec unopened; unopened.SetSpeed(2000);
  assert(unopened.m_speed==2000 && !unopened.m_speedPending && unopened.dll.modes.empty());
}
void DecoderDisplayAdmission() {
  CAMLCodec codec; assert(codec.OpenDecoder());
  uint8_t packet[1]{};
  const auto epoch=codec.m_session.Epoch();
  auto display=CAMLSession::FenceDisplay();
  assert(!codec.AddData(packet,1,1,1) && codec.storage.writes==0);
  assert(CAMLSession::TryBeginDisplay(display));
  assert(!codec.AddData(packet,1,1,1) && codec.storage.writes==0);
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  bool inside=false;
  writeHook=[&] {
    inside=true;
    display=CAMLSession::FenceDisplay();
    // Admission cannot overlook an AddData already inside its driver write.
    assert(!CAMLSession::TryBeginDisplay(display));
  };
  assert(codec.AddData(packet,1,1,1)); writeHook={};
  assert(inside && codec.storage.writes==1);
  assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(codec.m_session.Epoch()==epoch);

  CDVDVideoCodecAmlogic wrapper;
  assert(wrapper.m_Codec->OpenDecoder());
  wrapper.m_packages.emplace_back(new uint8_t[1],1,false,1.0);
  const int initialFreed=freed;
  display=CAMLSession::FenceDisplay();
  wrapper.Reset(); // Reset may overtake a fenced but unstarted display request.
  assert(wrapper.m_resetCleanupPending && wrapper.m_packages.size()==1);
  assert(!wrapper.ContinueLifecycle() && freed==initialFreed);
  assert(CAMLSession::TryBeginDisplay(display));
  assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));
  assert(wrapper.ContinueLifecycle() && !wrapper.m_resetCleanupPending);
  assert(wrapper.m_packages.empty() && freed==initialFreed+1);
}
void PollSessionAndDescriptor() {
  CAMLCodec original,replacement;
  original.nextDescriptor=42; replacement.nextDescriptor=84;
  assert(original.OpenDecoder() && replacement.OpenDecoder());
  polls.clear(); g_aml_sync_event.signals=0;
  const auto oldEpoch=original.m_session.Epoch();
  {
    auto permit=original.m_session.Acquire(oldEpoch);
    assert(original.PollFrame(permit)==1);
    assert(polls.size()==1 && polls[0].fd==42 && polls[0].events==POLLOUT && polls[0].timeout==50);
    assert(g_aml_sync_event.signals==1);
    // Another live codec's descriptor cannot be reached through this permit.
    assert(replacement.PollFrame(permit)==0);
    CAMLSession::Permit empty;
    assert(original.PollFrame(empty)==0);
    assert(polls.size()==1 && g_aml_sync_event.signals==1);
    original.SetPollDevice(-1);
    assert(original.PollFrame(permit)==0);
    assert(polls.size()==1 && g_aml_sync_event.signals==1);
  }
  original.nextDescriptor=43;
  assert(original.Reset());
  auto stale=original.m_session.Acquire(oldEpoch);
  assert(!stale && original.PollFrame(stale)==0);
  assert(polls.size()==1 && g_aml_sync_event.signals==1);
  auto current=original.m_session.Acquire(original.m_session.Epoch());
  assert(original.PollFrame(current)==1);
  assert(polls.size()==2 && polls.back().fd==43 && g_aml_sync_event.signals==2);
}
void PollPacingResults() {
  CAMLCodec codec; assert(codec.OpenDecoder());
  auto permit=codec.m_session.Acquire(codec.m_session.Epoch());
  const int signals=g_aml_sync_event.signals;
  for (short events : {short(POLLERR), short(POLLHUP), short(POLLNVAL),
                       short(POLLOUT|POLLERR), short(POLLIN), short(0)}) {
    pollEvents=events;
    assert(codec.PollFrame(permit)==0);
  }
  pollResult=-1; pollEvents=POLLOUT;
  assert(codec.PollFrame(permit)==0); // Includes EINTR: no usable wait/edge.
  pollResult=0; pollEvents=0;
  assert(codec.PollFrame(permit)==1); // The real syscall waited its timeout.
  pollResult=1; pollEvents=POLLOUT;
  assert(codec.PollFrame(permit)==1);
  assert(g_aml_sync_event.signals==signals+9); // Existing wake semantics survive.
}
void PendingThroughActualPoll() {
  CAMLCodec codec; codec.nextDescriptor=100; assert(codec.OpenDecoder()); codec.trace.clear();
  polls.clear();g_aml_sync_event.signals=0;
  auto admitted=codec.m_session.Acquire(codec.m_session.Epoch());
  std::promise<void> entered,release;
  auto enteredFuture=entered.get_future(),releaseFuture=release.get_future();
  pollHook=[&] {entered.set_value();releaseFuture.wait();};
  // Normal admission occurs on the owner. Moving this token to a fake blocking
  // syscall executor tests permit lifetime, not permission for real worker GL.
  std::thread presenter([&,admitted=std::move(admitted)]() mutable {
    auto permit=std::move(admitted);
    assert(codec.PollFrame(permit)==1);
  });
  assert(enteredFuture.wait_for(5s)==std::future_status::ready);
  assert(polls.size()==1 && polls[0].fd==100 && polls[0].events==POLLOUT && polls[0].timeout==50);
  assert(g_aml_sync_event.signals==0);
  codec.nextDescriptor=101;
  assert(!codec.Reset() && codec.LifecyclePending());
  assert(!codec.ContinueLifecycle() && codec.trace.empty());
  assert(codec.m_pollDevice==100);
  assert(!codec.m_session.Wait(codec.m_lifecycleRequest,0ms));
  release.set_value();presenter.join();pollHook={};
  assert(g_aml_sync_event.signals==1);
  assert(codec.ContinueLifecycle() && codec.m_pollDevice==101);
  auto current=codec.m_session.Acquire(codec.m_session.Epoch());
  assert(codec.PollFrame(current)==1);
  assert(polls.size()==2 && polls.back().fd==101 && g_aml_sync_event.signals==2);
}
void FreshReset() {
  CAMLCodec codec;
  assert(codec.Reset()); // No first frame, open device, or SYNC_INSYNC required.
  assert(!codec.LifecyclePending() && !codec.m_opened);
  assert(!codec.m_session.Acquire(codec.m_session.Epoch()));
  codec.trace.clear();
  assert(codec.OpenDecoder());
  auto generation=codec.generation,epoch=codec.m_session.Epoch();
  assert(codec.Reset());
  assert(codec.generation==generation && codec.m_session.Epoch()==epoch+1);
  assert((codec.trace==std::vector<std::string>{"open","reset"}));
}
void PendingAndReopen() {
  CAMLCodec codec; assert(codec.OpenDecoder()); codec.trace.clear();
  const auto oldGeneration=codec.generation,oldEpoch=codec.m_session.Epoch();
  CAMLSession::Request request;
  {
    auto permit=codec.m_session.Acquire(oldEpoch); assert(permit);
    assert(!codec.Reset() && codec.LifecyclePending());
    request=codec.m_lifecycleRequest;
    assert(!codec.Reset()); // Retry keeps the same request identity/serial.
    assert(codec.m_lifecycleRequest.serial==request.serial);
    assert(!codec.ContinueLifecycle());
    assert(codec.trace.empty());
    assert(!codec.m_session.Acquire(oldEpoch));
    assert(codec.m_session.Acquire(oldEpoch,true));
  }
  assert(codec.ContinueLifecycle());
  assert(codec.generation==oldGeneration && codec.m_session.Epoch()==oldEpoch+1);
  assert((codec.trace==std::vector<std::string>{"reset"}));
  assert(!codec.m_session.Complete(request,true));
  codec.trace.clear();
  const auto requestSerial=codec.m_lifecycleRequest.serial;
  assert(codec.ReopenDecoder());
  assert(codec.generation==oldGeneration+1);
  assert((codec.trace==std::vector<std::string>{"close","open"}));
  assert(codec.m_lifecycleRequest.serial==requestSerial+1); // No recursive close/open fences.
}
void CloseSupersedes() {
  CAMLCodec codec; assert(codec.OpenDecoder()); codec.trace.clear();
  CAMLSession::Request abandoned;
  {
    auto permit=codec.m_session.Acquire(codec.m_session.Epoch());
    assert(!codec.Reset()); abandoned=codec.m_lifecycleRequest;
    assert(!codec.CloseDecoder());
    assert(codec.m_lifecycle==CAMLCodec::Lifecycle::CLOSE);
    assert(codec.m_lifecycleRequest.serial!=abandoned.serial);
  }
  assert(codec.ContinueLifecycle());
  assert((codec.trace==std::vector<std::string>{"close"}));
  assert(!codec.m_opened && !codec.m_session.Complete(abandoned,true));
  assert(!codec.m_session.Acquire(codec.m_session.Epoch(),true));
}
void FailedOpen() {
  CAMLCodec codec; codec.openSucceeds=false;
  assert(!codec.OpenDecoder());
  assert(!codec.LifecyclePending() && !codec.m_opened && codec.generation==0);
  assert(codec.LifecycleFailed());
  for(int i=0;i<3;++i) assert(!codec.ContinueLifecycle());
  assert((codec.trace==std::vector<std::string>{"open","close"}));
  assert(!codec.m_decoderNeedsClose && !codec.deviceAcquired && !codec.holdActive && !codec.m_dvOpened);
  assert(codec.storage.vcodec.config==nullptr && codec.storage.vcodec.config_len==0);
  assert(!codec.m_session.Acquire(codec.m_session.Epoch(),true));
  codec.openSucceeds=true; assert(codec.OpenDecoder());
  assert(!codec.LifecycleFailed());
  codec.openSucceeds=false; codec.trace.clear();
  assert(!codec.ReopenDecoder());
  assert(!codec.m_opened && !codec.LifecyclePending() && codec.generation==1);
  assert(codec.LifecycleFailed());
  for(int i=0;i<3;++i) assert(!codec.ContinueLifecycle());
  assert((codec.trace==std::vector<std::string>{"close","open","close"}));
  assert(!codec.m_session.Acquire(codec.m_session.Epoch(),true));
  assert(!codec.m_decoderNeedsClose && !codec.deviceAcquired && !codec.holdActive && !codec.m_dvOpened);
  assert(codec.storage.vcodec.config==nullptr && codec.storage.vcodec.config_len==0);
  const auto closeCount=codec.trace.size();
  assert(codec.CloseDecoder()); // No second cleanup of the already unwound partial open.
  assert(codec.trace.size()==closeCount);
  CAMLCodec early; early.acquireDevice=false;
  assert(!early.OpenDecoder() && early.LifecycleFailed());
  assert((early.trace==std::vector<std::string>{"open"}));
  assert(!early.m_decoderNeedsClose && !early.storage.vcodec.config);
}
void WrapperFailure(bool delayed) {
  CDVDVideoCodecAmlogic wrapper;
  assert(wrapper.m_Codec->OpenDecoder()); wrapper.m_Codec->trace.clear();
  wrapper.m_Codec->openSucceeds=false;
  cpu=AML_G12B+1; wrapper.m_hints.dovi_el_type=DOVIELType::TYPE_FEL;
  const int initialFreed=freed;
  wrapper.m_packages.emplace_back(new uint8_t[1],1,false,1.0);
  if(delayed) {
    {
      auto permit=wrapper.m_Codec->m_session.Acquire(wrapper.m_Codec->m_session.Epoch());
      wrapper.Reset();
      assert(wrapper.LifecyclePending() && !wrapper.LifecycleFailed());
      assert(wrapper.m_Codec->trace.empty());
    }
    assert(!wrapper.ContinueLifecycle());
  } else wrapper.Reset();
  assert(wrapper.LifecycleFailed());
  for(int i=0;i<3;++i) assert(!wrapper.ContinueLifecycle());
  assert(wrapper.m_packages.size()==1 && freed==initialFreed);
  assert(wrapper.m_resetCleanupPending && wrapper.m_bitstream->resets==0);
  assert(!wrapper.m_Codec->m_decoderNeedsClose && !wrapper.m_Codec->deviceAcquired);
  assert(!wrapper.m_Codec->holdActive && !wrapper.m_Codec->m_dvOpened);
  assert(!wrapper.m_Codec->storage.vcodec.config && wrapper.m_Codec->storage.vcodec.config_len==0);
  assert((wrapper.m_Codec->trace==std::vector<std::string>{"close","open","close"}));
  assert(!wrapper.m_Codec->m_session.Acquire(wrapper.m_Codec->m_session.Epoch(),true));
  // Only a new explicit operation may clear the recorded failure.
  wrapper.m_Codec->openSucceeds=true;
  wrapper.Reset();
  assert(!wrapper.LifecycleFailed() && !wrapper.LifecyclePending());
  assert(wrapper.m_packages.empty() && freed==initialFreed+1);
  assert(wrapper.m_bitstream->resets==1 && wrapper.m_Codec->generation==2);
  assert((wrapper.m_Codec->trace==std::vector<std::string>{"close","open","close","open"}));
  cpu=AML_G12B;
}
void CloseDeferredCleanup(bool failed) {
  CDVDVideoCodecAmlogic wrapper;
  assert(wrapper.m_Codec->OpenDecoder());
  wrapper.m_Codec->trace.clear();
  wrapper.m_mpeg2_sequence=new int(1);
  wrapper.m_h264_sequence=new int(2);
  wrapper.m_bitparser=new int(3);
  cpu=AML_G12B+1; wrapper.m_hints.dovi_el_type=DOVIELType::TYPE_FEL;
  wrapper.m_packages.emplace_back(new uint8_t[1],1,false,1.0);
  const int initialFreed=freed;
  if(failed) {
    wrapper.m_Codec->openSucceeds=false;
    wrapper.Reset(); assert(wrapper.LifecycleFailed());
  } else {
    auto permit=wrapper.m_Codec->m_session.Acquire(wrapper.m_Codec->m_session.Epoch());
    wrapper.Reset(); assert(wrapper.LifecyclePending());
    assert(wrapper.m_Codec->trace.empty());
  }
  assert(wrapper.m_resetCleanupPending && wrapper.m_packages.size()==1);
  assert(wrapper.m_bitstream->resets==0 && freed==initialFreed);
  auto original=wrapper.m_Codec;
  wrapper.Close();
  assert(!wrapper.m_Codec && !wrapper.m_videoBufferPool);
  assert(!wrapper.m_resetCleanupPending && !wrapper.LifecyclePending());
  assert(wrapper.m_packages.empty() && freed==initialFreed+1);
  assert(wrapper.m_bitstream->resets==1 && !wrapper.m_has_keyframe);
  assert(!wrapper.m_opened && wrapper.m_videobuffer.iFlags==0);
  assert(!wrapper.m_mpeg2_sequence && !wrapper.m_h264_sequence && !wrapper.m_bitparser);
  assert(!original->m_decoderNeedsClose && !original->m_opened);
  assert(original->trace==(failed ? std::vector<std::string>{"close","open","close"}
                                 : std::vector<std::string>{"close"}));
  assert(wrapper.ContinueLifecycle());
  assert(wrapper.m_bitstream->resets==1 && freed==initialFreed+1);
  // Reusing the wrapper must not replay deferred cleanup against the next codec.
  wrapper.m_Codec=std::make_shared<CAMLCodec>();
  assert(wrapper.m_Codec->OpenDecoder());
  assert(wrapper.ContinueLifecycle() && !wrapper.LifecyclePending());
  assert(wrapper.m_bitstream->resets==1 && freed==initialFreed+1);
  wrapper.Close();
  assert(freed==initialFreed+1);
  cpu=AML_G12B;
}
void WaitPinsOriginalSession() {
  CAMLCodec codec; assert(codec.OpenDecoder()); codec.trace.clear();
  auto permit=std::make_unique<CAMLSession::Permit>(codec.m_session.Acquire(codec.m_session.Epoch()));
  std::promise<void> fenced;
  auto ready=fenced.get_future();
  std::atomic<bool> done{false},released{false};
  codec.onClose=[&] {assert(released);};
  std::thread lifecycle([&] {
    assert(!codec.CloseDecoder()); fenced.set_value();
    codec.WaitForLifecycle(); done=true;
  });
  assert(ready.wait_for(5s)==std::future_status::ready);
  assert(!done);
  released=true; permit.reset();
  lifecycle.join(); assert(done && !codec.m_opened);
  assert((codec.trace==std::vector<std::string>{"close"}));
}
void WrapperCleanup(bool reopen) {
  CDVDVideoCodecAmlogic wrapper;
  assert(wrapper.m_Codec->OpenDecoder()); wrapper.m_Codec->trace.clear();
  cpu=reopen ? AML_G12B+1 : AML_G12B;
  wrapper.m_hints.dovi_el_type=DOVIELType::TYPE_FEL;
  const int initialFreed=freed;
  wrapper.m_packages.emplace_back(new uint8_t[1],1,false,1.0);
  wrapper.m_packages.emplace_back(new uint8_t[1],1,true,1.0);
  {
    auto permit=wrapper.m_Codec->m_session.Acquire(wrapper.m_Codec->m_session.Epoch());
    wrapper.Reset();
    assert(wrapper.LifecyclePending() && !wrapper.ContinueLifecycle());
    assert(wrapper.m_Codec->trace.empty());
    assert(wrapper.m_packages.size()==2 && freed==initialFreed);
    assert(wrapper.m_has_keyframe && wrapper.m_bitstream->resets==0);
  }
  assert(wrapper.ContinueLifecycle() && !wrapper.LifecyclePending());
  assert(wrapper.m_packages.empty() && freed==initialFreed+2);
  assert(!wrapper.m_has_keyframe && wrapper.m_bitstream->resets==1);
  assert(wrapper.m_el_starvation_count==0 && wrapper.m_mpeg2_sequence_pts==0);
  assert(wrapper.ContinueLifecycle() && wrapper.m_bitstream->resets==1);
  assert(wrapper.m_Codec->trace==(reopen ? std::vector<std::string>{"close","open"}
                                       : std::vector<std::string>{"reset"}));
  cpu=AML_G12B;
}
void InternalRecovery() {
  CAMLCodec codec; assert(codec.OpenDecoder()); codec.trace.clear();
  uint8_t packet[1]{};
  codec.storage.write=Private::Write::STUCK;
  {
    auto permit=codec.m_session.Acquire(codec.m_session.Epoch());
    assert(!codec.AddData(packet,1,1,1));
    assert(codec.storage.writes==100 && codec.LifecyclePending());
    const auto serial=codec.m_lifecycleRequest.serial;
    assert(codec.trace.empty());
    assert(!codec.AddData(packet,1,1,1)); // Pending recovery preserves packet replay.
    assert(codec.storage.writes==100 && codec.m_lifecycleRequest.serial==serial);
  }
  codec.storage.write=Private::Write::SUCCESS;
  assert(codec.AddData(packet,1,1,1));
  assert(!codec.LifecyclePending() && codec.storage.writes==101);
  assert((codec.trace==std::vector<std::string>{"reset"}));
  codec.trace.clear(); codec.storage.write=Private::Write::ERROR;
  codec.m_wrFailActive=true;
  codec.m_tpWrFailStart=std::chrono::steady_clock::now()-500ms;
  codec.m_tpWrFailLastReset=std::chrono::steady_clock::now()-300ms;
  {
    auto permit=codec.m_session.Acquire(codec.m_session.Epoch());
    assert(!codec.AddData(packet,1,1,1));
    assert(codec.LifecyclePending() && codec.trace.empty());
  }
  codec.storage.write=Private::Write::SUCCESS;
  assert(codec.AddData(packet,1,1,1));
  assert((codec.trace==std::vector<std::string>{"reset"}));
  // Existing persistent write-error drop remains separate from quiescence.
  codec.storage.write=Private::Write::ERROR; codec.m_wrFailActive=true;
  codec.m_tpWrFailStart=std::chrono::steady_clock::now()-2100ms;
  const auto before=codec.trace.size();
  assert(codec.AddData(packet,1,1,1));
  assert(!codec.LifecyclePending() && codec.trace.size()==before);
}
int main() {
  SpeedDuringDisplay(); DecoderDisplayAdmission();
  PollSessionAndDescriptor();
  PollPacingResults(); PendingThroughActualPoll();
  FreshReset(); PendingAndReopen(); CloseSupersedes(); FailedOpen();
  WaitPinsOriginalSession(); WrapperCleanup(false); WrapperCleanup(true); InternalRecovery();
  WrapperFailure(false); WrapperFailure(true);
  CloseDeferredCleanup(false); CloseDeferredCleanup(true);
}
'''

if __name__ == '__main__':
    main()
