#!/usr/bin/env python3
"""Production CAMLCodec::ResetInternal() against a slow subtitle-probe restart.

Extracts the whole ResetInternal body and runs it with recording doubles for the
amcodec library, packet helpers and probe lifetime. The probe restart (which
first joins the previous probe, possibly blocked in a VFS read) advances a fake
clock past the no-output watchdog interval. The watchdog must measure from the
completed reset, so the restarted decoder gets a full interval. Host decision
test only, not a device measurement.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    level = 0
    for index in range(source.index('{', start), len(source)):
        level += (source[index] == '{') - (source[index] == '}')
        if level == 0:
            return source[start:index + 1]
    raise ValueError(signature)


def body(source, signature):
    block = function(source, signature)
    return block[block.index('{') + 1:-1]


PREFIX = r'''
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>
struct CLog {template<class... T> static void Log(T&&...) {}};
constexpr int LOGDEBUG=0, DVD_PLAYSPEED_NORMAL=1000, TRICKMODE_NONE=0;
constexpr double DVD_NOPTS_VALUE=-1e30;
enum class StreamHdrType {HDR_TYPE_NONE, HDR_TYPE_DOLBYVISION};
// Fake wall clock: production reads std::chrono::system_clock::now().
struct FakeClock {
  static inline std::chrono::system_clock::time_point value{std::chrono::seconds(1000)};
  static std::chrono::system_clock::time_point now() {return value;}
  static void Advance(std::chrono::milliseconds d) {value+=d;}
};
struct VCodec {int cntl_handle=7;};
struct Packet {VCodec* codec=nullptr;};
struct Private {VCodec vcodec; Packet am_pkt;};
struct Dll {
  int resets=0;
  void codec_set_cntl_mode(VCodec*,int) {}
  void codec_pause(VCodec*) {}
  void codec_reset(VCodec*) {++resets; FakeClock::Advance(std::chrono::milliseconds(400));}
  void codec_set_video_delay_limited_ms(VCodec*,int) {}
};
static std::chrono::milliseconds probeRetireDelay{0};
static int probeStarts=0;
void dumpfile_close(Private*) {}
void dumpfile_open(Private*) {}
void am_packet_release(Packet*) {}
void am_packet_init(Packet*) {}
void pre_header_feeding(Private*,Packet*) {}
void aml_dv_backend_invalidate(void*) {}
uint64_t aml_dv_backend_epoch() {return 1;}
bool aml_subtitle_active_area_configure(int,int,bool,bool,int) {return true;}
// Stops and joins the previous probe worker before starting the next one.
void aml_dv_detect_active_area_start() {++probeStarts; FakeClock::Advance(probeRetireDelay);}
struct CAMLCodec {
  bool m_abort=true, m_opened=true;
  int m_speed=DVD_PLAYSPEED_NORMAL;
  Dll dll; Dll* m_dll=&dll; Private priv; Private* am_private=&priv;
  void* m_dvSession=nullptr; uint64_t m_dvBackendEpoch=0; double m_dvBackendSampleTime=1;
  double m_cur_pts=1, m_last_pts=1, m_prev_last_pts=1;
  std::vector<int> m_reorderQueue{1};
  int m_repairExcursionRun=1, m_outPtsRingPos=1, m_outPtsRingCount=1, m_state=1;
  bool m_stream_eof=true, m_buffer_level_ready=true, m_no_data_since_reset=false;
  struct {int frames=1;} m_stillFrameDrain;
  struct {StreamHdrType hdrType=StreamHdrType::HDR_TYPE_NONE; int width=3840, height=2160;
          struct {int dv_profile=0;} dovi; int subtitleProbeSource=0;} m_hints;
  StreamHdrType m_originalSourceHdrType=StreamHdrType::HDR_TYPE_NONE;
  std::chrono::system_clock::time_point m_tp_last_frame{};
  int m_decoder_timeout=5;
  void HoldVideo(bool) {}
  bool VideoRestartHoldWanted() {return false;}
  void SetPollDevice(int) {}
  void SetSpeedInternal(int) {}
  void ResetInternal();
  // The watchdog decision in GetPicture(): no frame within m_decoder_timeout.
  bool TimedOut() const {
    return FakeClock::now() - m_tp_last_frame > std::chrono::seconds(m_decoder_timeout);
  }
};
'''

TESTS = r'''
int main() {
  // A probe retirement longer than the watchdog interval must not turn the
  // first post-reset poll into a timeout (an unfair second escalation step).
  for (int delay : {0, 6000, 20000}) {
    probeRetireDelay = std::chrono::milliseconds(delay);
    CAMLCodec codec;
    codec.m_tp_last_frame = FakeClock::now();
    codec.ResetInternal();
    assert(codec.dll.resets == 1 && probeStarts > 0);
    assert(!codec.TimedOut() && "watchdog must start after all reset work");
    FakeClock::Advance(std::chrono::seconds(codec.m_decoder_timeout));
    assert(!codec.TimedOut());
    FakeClock::Advance(std::chrono::milliseconds(1));
    assert(codec.TimedOut() && "a full interval after the reset still times out");
  }
  // A closed decoder returns early and leaves the watchdog untouched.
  CAMLCodec closed; closed.m_opened = false;
  closed.m_tp_last_frame = FakeClock::now() - std::chrono::seconds(9);
  closed.ResetInternal();
  assert(closed.TimedOut() && closed.dll.resets == 0);
  std::puts("PASS: aml-reset-watchdog");
}
'''


def harness(root):
    aml = (root / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    reset = body(aml, 'void CAMLCodec::ResetInternal()')
    assert 'aml_dv_detect_active_area_start();' in reset
    reset = reset.replace('std::chrono::system_clock::now()', 'FakeClock::now()')
    return PREFIX + 'void CAMLCodec::ResetInternal() {' + reset + '}\n' + TESTS


def run(source, directory, label, expect_failure=False):
    path = directory / (label + '.cpp')
    path.write_text(source)
    exe = directory / label
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(path), '-o', str(exe)],
                   check=True)
    result = subprocess.run([str(exe)], text=True, capture_output=True)
    if expect_failure:
        if result.returncode == 0:
            raise AssertionError('negative control was not rejected: ' + label)
        print('REJECTED:', label)
    elif result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    else:
        print(result.stdout, end='')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    source = harness(args.root)
    with tempfile.TemporaryDirectory(prefix='aml-reset-watchdog-') as tmp:
        directory = Path(tmp)
        run(source, directory, 'aml-reset-watchdog')
        if args.negative_controls:
            stamp = '  m_tp_last_frame = FakeClock::now();\n'
            assert stamp in source
            early = source.replace(stamp, '', 1).replace(
                '  SetSpeedInternal(m_speed);\n', stamp + '  SetSpeedInternal(m_speed);\n', 1)
            run(early, directory, 'stamp-before-probe-restart', expect_failure=True)
            run(source.replace(stamp, '', 1), directory, 'no-stamp-after-reset', expect_failure=True)


if __name__ == '__main__':
    main()
