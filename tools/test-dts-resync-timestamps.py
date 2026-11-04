#!/usr/bin/env python3
"""Replay production DTS timestamp handling across coordinated resync.

Uses the complete GENERAL_RESYNC handler, codec-recreation gate, codec GetData,
reset/resync methods and FloatingAverage. Parser output, clocks and audio sink
are controlled host boundaries; this does not verify encoded media or hardware.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLAYER = 'xbmc/cores/VideoPlayer/VideoPlayerAudio.cpp'
CODEC = 'xbmc/cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecPassthrough.cpp'
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PREFIX = r'''
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <memory>
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/FloatingAverage.h"
using namespace std::chrono_literals;
constexpr int LOGDEBUG=0, LOGAUDIO=1;
constexpr int AV_CODEC_ID_DTS=1, AV_CODEC_ID_TRUEHD=2, AV_CODEC_ID_AC3=3;
constexpr double LOCAL_NOPTS=-1.0, MAX_REASONABLE_PTS=86400000000.0;
bool IsValidPts(double pts) {return pts >= 0 && pts <= MAX_REASONABLE_PTS;}
struct CLog {template<class... T> static void Log(T...) {}};
struct CAEStreamInfo {
  enum DataType {STREAM_TYPE_DTSHD_MA, STREAM_TYPE_TRUEHD, STREAM_TYPE_AC3};
  DataType m_type=STREAM_TYPE_DTSHD_MA;
  double GetDuration() const {return m_type==STREAM_TYPE_TRUEHD ? 20.0 : 512000.0/48000.0;}
};
struct Format {CAEStreamInfo m_streamInfo; int m_sampleRate=48000;};
struct DVDAudioFrame {
  uint8_t* data[1]{}; int nb_frames=0, framesOut=0, planes=0, bits_per_sample=0;
  bool hasDiscontinuity=false, passthrough=false;
  bool hasTimestamp=true;
  double discontinuityCorrection=0, duration=0, pts=DVD_NOPTS_VALUE;
  Format format;
};
struct Parser {int resets=0; void Reset() {++resets;}};
struct Packer {int GetSamplesOffset() {return 0;} void Reset() {}};
struct Cache {double pts=0; void SetAudioPts(double value) {pts=value;}};
Cache cache;
struct CServiceBroker {static Cache& GetDataCacheCore() {return cache;}};
class CDVDAudioCodec {
public:
  Cache& m_dataCacheCore=cache;
  virtual ~CDVDAudioCodec()=default;
  virtual bool NeedPassthrough() const {return true;}
};
class CDVDAudioCodecPassthrough : public CDVDAudioCodec {
public:
  bool m_lavStyleSyncEnabled=true, m_needsResync=true, m_truehd_ptsCacheValid=false;
  double m_currentPts=LOCAL_NOPTS, m_nextPts=LOCAL_NOPTS, m_lastOutputPts=LOCAL_NOPTS;
  double m_internalClock=LOCAL_NOPTS, m_truehd_ptsCache=LOCAL_NOPTS;
  double m_jitterThreshold=100000;
  unsigned int m_trueHDoffset=0, m_dataSize=0, m_bufferSize=0, m_backlogSize=0;
  Format m_format;
  Parser m_parser;
  std::unique_ptr<Packer> m_packerMAT;
  AudioSync::CFloatingAverage<double,256> m_jitterTracker;
  bool IsLavStyleSyncEnabled() const {return m_lavStyleSyncEnabled;}
  int GetData(uint8_t**) {int size=m_dataSize; m_dataSize=0; return size;}
  void GetData(DVDAudioFrame&);
  void ResetLavSyncState();
  void SyncToResyncPts(double);
  void Reset();
  void Queue(double pts) {m_currentPts=pts; m_dataSize=128;}
};
struct Sink {
  double delay=309333.333333; int flushes=0, resumes=0;
  double GetDelay() const {return delay;}
  void Flush() {++flushes;}
  void Resume() {++resumes;}
};
struct CDVDMsgDouble {double m_value; explicit CDVDMsgDouble(double pts):m_value(pts){}};
struct IDVDStreamPlayer {enum {SYNC_STARTING, SYNC_INSYNC};};
struct Timer {void Set(std::chrono::milliseconds) {}};
struct Queue {int GetLevel() {return 10;}};
struct Clock {double value=0; double GetClock() {return value;}};
class Player {
public:
  std::unique_ptr<CDVDAudioCodec> m_pAudioCodec=std::make_unique<CDVDAudioCodecPassthrough>();
  Sink m_audioSink;
  Queue m_messageQueue;
  Timer m_syncTimer;
  int m_syncState=IDVDStreamPlayer::SYNC_STARTING, m_speed=DVD_PLAYSPEED_NORMAL;
  double m_audioClock=0, m_pcmOutputClock=LOCAL_NOPTS;
  bool m_paused=false, m_lavStylePcmSyncEnabled=false, m_pcmResyncTimestamp=false;
  bool m_awaitingReseekResync=true;
  struct {int codec=AV_CODEC_ID_DTS;} m_streaminfo;
  AudioSync::CFloatingAverage<double,256> m_pcmJitterTracker;
  auto& Codec() {return *static_cast<CDVDAudioCodecPassthrough*>(m_pAudioCodec.get());}
  void Resync(double coordinated);
  void AssignMissingPts(DVDAudioFrame& audioframe);
};
void near(double actual,double expected,const char* name) {
  if(std::abs(actual-expected)>0.001) {
    std::cerr<<name<<": expected "<<expected<<", got "<<actual<<'\n'; std::exit(1);
  }
}
void check(bool value,const char* name) {if(!value) {std::cerr<<name<<'\n'; std::exit(1);}}
'''

TESTS = r'''
int main() {
  // Both sides of the captured 100ms tolerance boundary must retain truthful
  // labels, including a parser frame already buffered when RESYNC arrives.
  for(double pullback : {99000.0,101670.0,-99000.0}) {
    Player player; auto& codec=player.Codec();
    constexpr double content=353119000;
    codec.m_internalClock=content; codec.m_needsResync=false;
    codec.Queue(content);
    player.m_audioClock=content;
    const double coordinated=content-pullback-player.m_audioSink.delay;
    player.Resync(coordinated);
    check(codec.m_dataSize==128 && codec.m_parser.resets==0,"resync must retain buffered content");
    near(player.m_audioClock,coordinated+player.m_audioSink.delay,"shared clock coordination retained");
    check(player.m_audioSink.flushes==0 && player.m_audioSink.resumes==1,"ordinary sink resume retained");
    check(!player.m_awaitingReseekResync,"reseek correction hold released");
    for(int i=0;i<1500;++i) {
      const double input=content+i*512000000.0/48000.0;
      if(i) codec.Queue(input);
      DVDAudioFrame frame; codec.GetData(frame);
      near(frame.pts,input,"DTS content label changed by coordinated resync");
      check(!frame.hasDiscontinuity,"resync invented a source discontinuity");
    }
  }
  // Empty/reset codecs acquire actual source PTS, never a playback-clock guess.
  Player fresh; fresh.Resync(352711000);
  check(fresh.Codec().m_needsResync,"fresh DTS must await source timestamp");
  DVDAudioFrame frame;
  fresh.Codec().Queue(LOCAL_NOPTS); fresh.Codec().GetData(frame);
  check(frame.pts==DVD_NOPTS_VALUE,"unanchored codec invented source PTS");
  fresh.AssignMissingPts(frame);
  near(frame.pts,fresh.m_audioClock,"unanchored player clock fallback retained");
  check(!frame.hasTimestamp && fresh.Codec().m_needsResync,"fallback became a source anchor");
  fresh.Codec().Queue(352811000);
  fresh.Codec().GetData(frame);
  near(frame.pts,352811000,"initial source anchor");
  fresh.Codec().Reset();
  check(fresh.Codec().m_parser.resets==1 && fresh.Codec().m_dataSize==0,"real reset clears parser");
  fresh.Resync(371855000); fresh.Codec().Queue(371957000); fresh.Codec().GetData(frame);
  near(frame.pts,371957000,"seek source anchor");
  // Missing PTS and ordinary jitter still use the duration chain; a genuine
  // large source discontinuity still escapes after the floating window fills.
  const double next=fresh.Codec().m_internalClock;
  fresh.Codec().Queue(LOCAL_NOPTS); fresh.Codec().GetData(frame);
  near(frame.pts,next,"missing PTS continues valid timeline");
  double jump=fresh.Codec().m_internalClock+500000;
  bool corrected=false;
  for(int i=0;i<300;++i) {
    fresh.Codec().Queue(jump+i*512000000.0/48000.0); fresh.Codec().GetData(frame);
    corrected |= frame.hasDiscontinuity;
  }
  check(corrected,"real source discontinuity recovery removed");
  near(frame.pts,jump+299*512000000.0/48000.0,"real source discontinuity recovered");
  // Preserve non-DTS LAV behavior, disabled Full, pause and forward-flush guards.
  for(int id : {AV_CODEC_ID_TRUEHD,AV_CODEC_ID_AC3}) {
    Player other; other.m_streaminfo.codec=id;
    other.Codec().m_internalClock=1000000; other.Codec().m_needsResync=false;
    other.Resync(500000);
    near(other.Codec().m_internalClock,500000+other.m_audioSink.delay,"non-DTS coordinated seed retained");
  }
  Player disabled; disabled.Codec().m_lavStyleSyncEnabled=false;
  disabled.Resync(1000000);
  check(disabled.Codec().m_needsResync,"disabled Full clock untouched");
  Player paused; paused.m_paused=true; paused.Resync(1000000);
  check(paused.m_audioSink.resumes==0,"paused sink resumed");
  Player forward; forward.Resync(10000000);
  check(forward.m_audioSink.flushes==1,"forward sink flush removed");
  // The companion codec-recreation gate must not pre-seed fresh DTS labels.
  Sink sink; Clock clock; clock.value=500000;
  CDVDAudioCodecPassthrough recreated;
  Recreate(true,IDVDStreamPlayer::SYNC_INSYNC,&clock,sink,&recreated,AV_CODEC_ID_DTS);
  check(recreated.m_needsResync,"DTS recreation acquired playback-clock estimate");
  recreated.Queue(1000000); recreated.GetData(frame);
  near(frame.pts,1000000,"DTS recreation source anchor");
  for(int id : {AV_CODEC_ID_TRUEHD,AV_CODEC_ID_AC3}) {
    CDVDAudioCodecPassthrough other;
    Recreate(true,IDVDStreamPlayer::SYNC_INSYNC,&clock,sink,&other,id);
    near(other.m_internalClock,clock.value+sink.delay,"non-DTS recreation seed retained");
  }
  std::cout<<"PASS: DTS buffered/initial/seek/recreation timestamps, both threshold sides, "
              "missing PTS, discontinuity recovery, non-DTS and sink guards\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', help='Git revision that must fail the corrected assertions')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    audio = (ROOT / PLAYER).read_text()
    if args.baseline:
        audio = subprocess.check_output(['git', 'show', args.baseline + ':' + PLAYER], cwd=ROOT, text=True)
    codec = (ROOT / CODEC).read_text()
    resync = extract(audio, 'else if (pMsg->IsType(CDVDMsg::GENERAL_RESYNC))')
    resync = resync[resync.index('{'):]
    recreate = extract(audio, 'if (enableLavFull &&')
    missing = extract(audio, 'if (audioframe.pts == DVD_NOPTS_VALUE)')
    methods = '\n'.join(extract(codec, name) for name in [
        'void CDVDAudioCodecPassthrough::GetData(DVDAudioFrame &frame)',
        'void CDVDAudioCodecPassthrough::Reset()',
        'void CDVDAudioCodecPassthrough::ResetLavSyncState()',
        'void CDVDAudioCodecPassthrough::SyncToResyncPts(double pts)'])
    body = ('void Player::AssignMissingPts(DVDAudioFrame& audioframe) {audioframe.hasTimestamp=true;\n' + missing + '\n}\n'
            'void Player::Resync(double coordinated) {auto pMsg=std::make_shared<CDVDMsgDouble>(coordinated);\n' + resync + '\n}\n'
            'void Recreate(bool enableLavFull,int m_syncState,Clock* m_pClock,Sink& m_audioSink,\n'
            'CDVDAudioCodecPassthrough* passthroughCodec,int codecId) {\n'
            'struct {int codec;} hints{codecId}; (void)hints;\n' + recreate + '\n}\n')

    def run(actual, reject=False):
        with tempfile.TemporaryDirectory(prefix='dts-resync-') as temp:
            cpp, binary = Path(temp) / 'test.cpp', Path(temp) / 'test'
            cpp.write_text(PREFIX + methods + actual + TESTS)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-g', '-O1', '-fno-pie', '-no-pie', '-fsanitize=address,undefined',
                            '-I' + str(ROOT / 'xbmc'), str(cpp), '-o', str(binary)], check=True)
            env = dict(os.environ, ASAN_OPTIONS=os.environ.get('ASAN_OPTIONS', 'detect_leaks=0:halt_on_error=1'),
                       UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
            result = subprocess.run([str(binary)], env=env, capture_output=True, text=True, timeout=30)
            assert 'Sanitizer' not in result.stderr and 'runtime error:' not in result.stderr, result.stderr
            if reject:
                assert result.returncode == 1 and 'DTS ' in result.stderr, result.stderr
                print('REJECTED:', result.stderr.strip())
            else:
                assert result.returncode == 0, result.stderr
                print(result.stdout, end='')

    run(body, reject=bool(args.baseline))
    if args.negative_controls:
        for name, target in [('resync rewrites DTS', resync), ('recreation pre-seeds DTS', recreate)]:
            guard = ' && m_streaminfo.codec != AV_CODEC_ID_DTS' if target == resync else ' && hints.codec != AV_CODEC_ID_DTS'
            assert target.count(guard) == 1, name
            run(body.replace(target, target.replace(guard, '', 1), 1), reject=True)
            print('Rejected negative control:', name)


if __name__ == '__main__':
    main()
