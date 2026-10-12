#!/usr/bin/env python3
"""Run entire actual CAMLCodec::GetPicture with recording hardware/hold boundaries.

Default assertions describe corrected behavior and deliberately fail original
source. --baseline replays a Git revision's complete method against those same
assertions. --observe-original proves the original bad flags/hold observations.
Full OutputPicture and PLAYER_STARTED gate use recording renderer/clock boundaries.
No actual QBUF, DV library, device or lifecycle concurrency is exercised.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CODEC = 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp'
DEFAULT = ROOT / CODEC


def method(source):
    marker = 'CDVDVideoCodec::VCReturn CAMLCodec::GetPicture(VideoPicture& videoPicture)'
    start = source.index(marker)
    begin = source.index('{', start)
    depth = 0
    for end in range(begin, len(source)):
        if source[end] == '{':
            depth += 1
        elif source[end] == '}':
            depth -= 1
            if depth == 0:
                return source[start:end + 1]
    raise RuntimeError('unterminated actual method')


PREFIX = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include "cores/VideoPlayer/Interface/TimingConstants.h"
constexpr int UNIT_FREQ = 96000;
constexpr int STREAM_TYPE_STREAM = 1;
constexpr int AV_CODEC_ID_HEVC = 1, AV_CODEC_ID_VC1 = 2, AV_CODEC_ID_WMV3 = 3;
constexpr int DVP_FLAG_DROPPED = 1, DVP_FLAG_INTERLACED = 2, DVP_FLAG_STREAM_CORRUPTION = 4;
constexpr int MIN_DECODER_VIDEO_RATE = 1, MAX_DECODER_VIDEO_RATE = 96000;
constexpr int LOGERROR = 1, LOGWARNING = 2, LOGDEBUG = 3, LOGVIDEO = 4;
struct CLog { template<typename... T> static void Log(T&&...) {} };
enum class MPEG2OutputMode { UNKNOWN, INTERLACED_FIELD, PROGRESSIVE };
struct VideoPicture
{
  int iFlags = 0;
  MPEG2OutputMode mpeg2OutputMode = MPEG2OutputMode::UNKNOWN;
  double iDuration = 0, dts = DVD_NOPTS_VALUE, pts = DVD_NOPTS_VALUE;
  std::shared_ptr<const void> amlDVSession;
  std::string stereoMode;
};
struct CDVDVideoCodec
{
  enum VCReturn { VC_NONE, VC_ERROR, VC_PICTURE, VC_EOF, VC_FLUSHED,
                  VC_FLUSHED_TIMEOUT, VC_BUFFER };
};
struct vdec_info { int frame_dur = 4004; int ratio_control = 0; };
struct Private
{
  struct { int dec_mode = 0; } gcodec;
  int vcodec = 0;
  std::atomic<int> video_rate{4004};
};
struct Dll
{
  int codec_get_vdec_info(int*, vdec_info* info) { *info = {}; return 0; }
};
struct Cache
{
  void GetPlayTimes(time_t& start, int64_t& current, int64_t& min, int64_t& max)
  { start = 0; current = min = max = 0; }
};
struct ProcessInfo
{
  struct { bool m_StereoInvert = false; } settings;
  void SetVideoDAR(double) {}
  auto& GetVideoSettings() { return settings; }
};
void aml_dv_backend_sample(std::shared_ptr<const void>&, uint64_t&, uint64_t&) {}
struct Session { bool AcquireDecoder() { return true; } };
class CAMLCodec
{
public:
  struct Frame { uint64_t pts; uint32_t index; bool interlaced; };
  std::deque<Frame> incoming, m_reorderQueue;
  Private storage;
  Private* am_private = &storage;
  Dll dll;
  Dll* m_dll = &dll;
  Cache m_dataCacheCore;
  ProcessInfo m_processInfo;
  Session m_session;
  struct { bool stills = false; int codec = AV_CODEC_ID_HEVC;
           double aspect = 1.78; std::string stereo_mode; } m_hints;
  struct { bool pictureEmitted = false; } m_stillFrameDrain;
  bool m_opened = true, m_dvBackendPaused = false, m_stream_eof = false;
  bool m_drain = false, m_no_data_since_reset = false, m_buffer_level_ready = true;
  bool m_keepMPEG2Progressive = false, m_repairTimestamps = false;
  float m_minimum_buffer_level = 10.0f;
  int m_speed = DVD_PLAYSPEED_NORMAL, m_decoder_drain_timeout = 5, m_decoder_timeout = 5;
  std::shared_ptr<const void> m_dvSession;
  uint64_t m_dvBackendEpoch = 0, m_dvBackendSampleTime = 0;
  uint64_t m_prev_last_pts = static_cast<uint64_t>(DVD_NOPTS_VALUE);
  uint64_t m_last_pts = static_cast<uint64_t>(DVD_NOPTS_VALUE);
  uint64_t m_cur_pts = static_cast<uint64_t>(DVD_NOPTS_VALUE);
  uint32_t m_bufferIndex = 0;
  std::array<uint64_t, 8> m_outPtsRing{};
  size_t m_outPtsRingPos = 0, m_outPtsRingCount = 0, m_repairExcursionRun = 0;
  // Decoder-owned target; device and hold calls below are recording stubs.
  double m_prerollTarget = DVD_NOPTS_VALUE;
  std::chrono::system_clock::time_point m_tp_last_frame = std::chrono::system_clock::now();
  std::chrono::system_clock::time_point m_tp_drain_start = m_tp_last_frame;
  std::vector<uint64_t> holdDecodedPts;
  int holdPolls = 0, dequeues = 0;
  bool LifecyclePending() { return false; }
  bool ContinueLifecycle() { return true; }
  float GetBufferLevel() { return 20.0f; }
  void CheckVideoHold() { ++holdPolls; }
  // Recording stub: observes whether production admits this output to the
  // decoded-hold boundary. It does not implement route/provider/hold policy.
  void VideoHoldDecoded() { holdDecodedPts.push_back(m_cur_pts); }
  bool DrainHevcStill(float) { return false; }
  int DequeueBuffer()
  {
    if (incoming.empty()) return EAGAIN;
    m_reorderQueue.push_back(incoming.front());
    incoming.pop_front();
    ++dequeues;
    return 0;
  }
  void SetPrerollTarget(double target);
  CDVDVideoCodec::VCReturn GetPicture(VideoPicture&);
};
void require(bool condition, const char* assertion)
{
  if (!condition)
  {
    std::cerr << "ASSERTION: " << assertion << '\n';
    std::exit(1);
  }
}
'''

ADMISSION = r'''
using namespace std::chrono_literals;
constexpr int VideoPlayer_VIDEO=2, CODEC_INTERLACED=1;
namespace MathUtils {bool FloatEquals(double a,double b,double t){return std::abs(a-b)<t;}}
namespace PLAYBACK_DIAGNOSTICS {bool Enabled(){return false;}int64_t NowUs(){return 0;}}
enum class EINTERLACEMETHOD {VS_INTERLACEMETHOD_NONE};
struct SStartMsg {uint64_t epoch=0;int player=0;double cachetime=0,cachetotal=0,timestamp=0;};
struct CDVDMsg {enum {PLAYER_AVCHANGE,PLAYER_STARTED};explicit CDVDMsg(int){}virtual ~CDVDMsg()=default;};
template<class T>struct CDVDMsgType:CDVDMsg {T value;CDVDMsgType(int t,T v):CDVDMsg(t),value(v){}};
struct CSysfsPath {explicit CSysfsPath(const char*){}bool Exists(){return false;}};
struct IDVDStreamPlayer {enum ESyncState {SYNC_STARTING,SYNC_WAITSYNC,SYNC_INSYNC};};
struct CRenderManager {
  struct BufferReservation {};
  int published=0,waits=0;
  bool Configure(const VideoPicture&,float,int,int,unsigned){return true;}
  int WaitForBuffer(BufferReservation&,bool&,std::chrono::milliseconds){++waits;return 1;}
  bool AddVideoPicture(BufferReservation&,const VideoPicture&,int,bool&,EINTERLACEMETHOD,bool){++published;return true;}
  double GetDelay(){return 0;}
};
struct CVideoPlayerVideo:IDVDStreamPlayer {
  enum EOutputState {OUTPUT_NORMAL,OUTPUT_AGAIN,OUTPUT_ABORT,OUTPUT_DROPPED};
  bool m_bAbortOutput=false,m_bFpsInvalid=false,m_stalled=false;
  double m_fFrameRate=24000./1001.;int m_speed=1000;
  uint64_t m_syncEpoch=7;ESyncState m_syncState=SYNC_STARTING;
  EOutputState m_outputSate=OUTPUT_NORMAL;VideoPicture m_picture;
  struct Info {
    std::string stereo;
    struct Settings {int m_Orientation=0;EINTERLACEMETHOD m_InterlaceMethod=EINTERLACEMETHOD::VS_INTERLACEMETHOD_NONE;} settings;
    std::string GetVideoStereoMode(){return stereo;}void SetVideoStereoMode(std::string v){stereo=v;}
    bool GetVideoInterlaced(){return false;}bool IsVideoHwDecoder(){return true;}
    Settings& GetVideoSettings(){return settings;}
    bool Supports(EINTERLACEMETHOD){return true;}
    EINTERLACEMETHOD GetDeinterlacingMethodDefault(){return EINTERLACEMETHOD::VS_INTERLACEMETHOD_NONE;}
  } m_processInfo;
  struct {int orientation=0,hdrType=0,codec=AV_CODEC_ID_HEVC,codecOptions=0;}m_hints;
  struct Codec {unsigned GetAllowedReferences(){return 0;}}codec;
  Codec* m_pVideoCodec=&codec;
  struct {void Add(double){}}m_ptsTracker;
  struct {void AddOutputDropGain(double,int){}}m_droppingStats;
  struct Clock {void UpdateFramerate(double){}double GetClock(double& t,bool){t=0;return 3633254000.;}}clock;
  Clock* m_pClock=&clock;
  struct {struct Counter{void Add(int64_t){}}capacity,publish;int capacityRejected=0;}m_diagnostics;
  struct Parent {int starts=0;void Put(std::shared_ptr<CDVDMsg> msg){if(auto p=std::dynamic_pointer_cast<CDVDMsgType<SStartMsg>>(msg)){++starts;assert(p->value.epoch==7);}}}m_messageParent;
  CRenderManager m_renderManager;
  void CalcFrameRate(){}int ProcessOverlays(const VideoPicture*,double){return 0;}
  void LogSyncTransition(const char*,double){}
  EOutputState OutputPicture(const VideoPicture*);
  void Accept(const VideoPicture& p) {
    m_picture=p;double pts=p.pts+41708;bool hasTimestamp=true;
    m_outputSate=OutputPicture(&m_picture);
    @START@
  }
};
@OUTPUT@
'''

TESTS = r'''
int main(int argc, char**)
{
  const bool observeOriginal = argc > 1;
  const auto target = uint64_t{3633254000};
  CAMLCodec codec;
  codec.SetPrerollTarget(static_cast<double>(target));
  codec.incoming = {{3626373000, 308, false}, {3626414000, 309, false},
                    {target, 310, false}, {3626456000, 311, false},
                    {target + 41708, 312, false}};
  VideoPicture picture;
  CVideoPlayerVideo player;
  const std::array<bool, 5> expectedDropped{true, true, false, true, false};
  for (size_t i = 0; i < expectedDropped.size(); ++i)
  {
    // Input's mutable wrapper flags: DROP for the first packet, cleared before
    // the delayed second preroll output; no assumed packet-output association.
    picture.iFlags = i == 0 ? DVP_FLAG_DROPPED : 0;
    const auto priorHoldCalls = codec.holdDecodedPts.size();
    const auto result = codec.GetPicture(picture);
    require(result == CDVDVideoCodec::VC_PICTURE, "actual_getpicture_returns_picture");
    require(codec.m_bufferIndex == 308 + i, "native_index_fifo_preserved");
    require(codec.dequeues == static_cast<int>(i + 1), "dequeue_bookkeeping_preserved");
    const bool dropped = picture.iFlags & DVP_FLAG_DROPPED;
    const bool admittedToHold = codec.holdDecodedPts.size() > priorHoldCalls;
    std::cout << "frame=" << i << " index=" << codec.m_bufferIndex
              << " pts=" << picture.pts << " dropped=" << dropped
              << " decoded_hold_admission=" << admittedToHold << std::endl;
    if (!observeOriginal) {
      const int beforePublished=player.m_renderManager.published;
      const int beforeWaits=player.m_renderManager.waits;
      player.Accept(picture);
      require(player.m_renderManager.published==beforePublished+!expectedDropped[i], "preroll_not_published_to_either_presenter");
      require(player.m_renderManager.waits==beforeWaits+!expectedDropped[i], "preroll_does_not_reserve_renderer_slot");
      require(player.m_messageParent.starts==(i<2?0:1), "startup_waits_for_target_frame");
    }
    if (observeOriginal)
    {
      require(!dropped, "original_erases_packet_drop_and_admits_all_frames");
      require(admittedToHold, "original_preroll_reaches_decoded_hold");
    }
    else
    {
      require(dropped == expectedDropped[i], "decoded_preroll_is_dropped_independent_of_input_flag");
      require(admittedToHold == !expectedDropped[i], "preroll_cannot_admit_decoded_hold");
    }
  }
  require(codec.holdPolls == 5, "hold_timeout_poll_preserved");
  require(codec.m_prerollTarget == static_cast<double>(target), "target_survives_first_eligible_output");
  if (!observeOriginal)
  {
    // Repeated/paused chapter seeks, exact target, and a later reordered preroll.
    for (const auto floor : {uint64_t{4181510000}, uint64_t{1000000000}, uint64_t{8000000000}})
    {
      codec.SetPrerollTarget(static_cast<double>(floor));
      codec.m_speed = DVD_PLAYSPEED_PAUSE;
      codec.m_hints.stills = true;
      codec.m_drain = true;
      codec.m_buffer_level_ready = false; // drained HEVC still can also yield a picture
      codec.incoming = {{floor - 100000, 500, true}, {floor, 501, true},
                        {floor - 40000, 502, true}};
      codec.m_keepMPEG2Progressive = true;
      for (int i=0; i<3; ++i)
      {
        const auto holds = codec.holdDecodedPts.size();
        require(codec.GetPicture(picture) == CDVDVideoCodec::VC_PICTURE, "paused_still_preroll_decodes");
        require(bool(picture.iFlags & DVP_FLAG_DROPPED) == (i != 1), "replacement_target_applies");
        require(bool(picture.iFlags & DVP_FLAG_INTERLACED), "dropped_interlace_flags_preserved");
        require(codec.holdDecodedPts.size() == holds + (i == 1), "only_eligible_still_admits_hold");
      }
    }
    // Reverse scan may legitimately visit output before the preceding chapter.
    codec.m_speed = -1000;
    codec.SetPrerollTarget(8000000000.);
    codec.incoming = {{7990000000, 599, false}};
    require(codec.GetPicture(picture) == CDVDVideoCodec::VC_PICTURE &&
            !(picture.iFlags & DVP_FLAG_DROPPED), "reverse_scan_does_not_inherit_forward_floor");
    codec.SetPrerollTarget(DVD_NOPTS_VALUE);
    codec.incoming = {{42, 600, false}};
    require(codec.GetPicture(picture) == CDVDVideoCodec::VC_PICTURE &&
            !(picture.iFlags & DVP_FLAG_DROPPED), "nonaccurate_seek_clears_old_floor");
    CAMLCodec ordinary;
    ordinary.incoming = {{3626373000, 401, true}};
    ordinary.m_keepMPEG2Progressive = true;
    VideoPicture p;
    require(ordinary.GetPicture(p) == CDVDVideoCodec::VC_PICTURE, "ordinary_output_preserved");
    require(!(p.iFlags & DVP_FLAG_DROPPED), "no_target_means_no_preroll_drop");
    require(p.iFlags & DVP_FLAG_INTERLACED, "interlace_flag_preserved");
    require(ordinary.holdDecodedPts.size() == 1, "ordinary_hold_admission_preserved");
  }
  std::cout << (observeOriginal ? "ORIGINAL BAD BOUNDARY REPRODUCED" : "NATIVE GETPICTURE PREROLL CHECKS PASS") << std::endl;
}
'''


def run(actual, setter, admission, observe=False, reject=False):
    with tempfile.TemporaryDirectory(prefix='aml-preroll-') as temp:
        out = Path(temp)
        cpp, binary = out / 'fixture.cpp', out / 'fixture'
        cpp.write_text(PREFIX + '\n' + setter + '\n' + actual + '\n' + admission + '\n' + TESTS)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-g', '-O1', '-fno-omit-frame-pointer', '-no-pie',
                        '-fsanitize=address,undefined', '-I'+str(ROOT / 'xbmc'), str(cpp), '-o', str(binary)], check=True)
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',
                   UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        result = subprocess.run([str(binary)] + (['observe-original'] if observe else []),
                                env=env, capture_output=True, text=True, timeout=20)
        if reject:
            assert result.returncode == 1 and 'ASSERTION:' in result.stderr, result.stderr
            assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr
            assert 'LeakSanitizer' not in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stderr
        print(result.stdout, end='')
        if reject:
            print('REJECTED:', result.stderr.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', help='Git revision whose complete GetPicture must fail corrected checks')
    parser.add_argument('--observe-original', action='store_true')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = DEFAULT.read_text()
    if args.baseline:
        source = subprocess.check_output(['git', 'show', args.baseline + ':' + CODEC], cwd=ROOT, text=True)
    actual = method(source)
    header = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
    # Run the actual production setter, not a second implementation of the policy.
    import runpy
    extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
    setter = extract(header, 'void SetPrerollTarget(').replace('void SetPrerollTarget(', 'void CAMLCodec::SetPrerollTarget(')
    owner=(ROOT / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    start=extract(owner, 'if (m_syncState == IDVDStreamPlayer::SYNC_STARTING &&')
    output=extract(owner, 'CVideoPlayerVideo::EOutputState CVideoPlayerVideo::OutputPicture(')
    admission=ADMISSION.replace('@START@',start).replace('@OUTPUT@',output)
    run(actual, setter, admission, observe=args.observe_original, reject=bool(args.baseline) and not args.observe_original)
    if args.negative_controls:
        controls = [
            ('reverse scan inherits floor', 'm_speed >= DVD_PLAYSPEED_PAUSE &&', ''),
            ('disabled decoded floor', 'videoPicture.pts < m_prerollTarget', 'false'),
            ('eligible output clears target', '      VideoHoldDecoded();',
             '      VideoHoldDecoded(); m_prerollTarget = DVD_NOPTS_VALUE;'),
            ('preroll admits unsupported hold', '      videoPicture.iFlags |= DVP_FLAG_DROPPED;',
             '      videoPicture.iFlags |= DVP_FLAG_DROPPED; VideoHoldDecoded();'),
        ]
        for name, old, new in controls:
            assert actual.count(old) == 1, name
            run(actual.replace(old, new, 1), setter, admission, reject=True)
            print('Rejected negative control:', name)
    print('PASS: native GetPicture/setter and OutputPicture/start admission; recording hardware/renderer/hold boundaries')


if __name__ == '__main__':
    main()
