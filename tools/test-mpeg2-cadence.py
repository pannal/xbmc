#!/usr/bin/env python3
"""Exercise production MPEG cadence and FFmpeg output-mode publication.

No image/module build or device acceptance. Host ASan/UBSan tests include actual
FFmpeg-decoded synthetic MPEG-2 headers and full/half BWDIF output when installed.
Negative controls mutate temporary production copies and must fail runtime asserts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / 'xbmc/cores/VideoPlayer/MPEG2Cadence.h'
CODEC = ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecFFmpeg.cpp'


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def source_mode():
    source = CODEC.read_text()
    begin = source.index('  pVideoPicture->mpeg2OutputMode = MPEG2OutputMode::UNKNOWN;')
    end = source.index('\n  pVideoPicture->iFlags |=', begin)
    # Execute the production block, with raw input/filter/hardware services only.
    return r'''
struct Picture {MPEG2OutputMode mpeg2OutputMode=MPEG2OutputMode::UNKNOWN;};
constexpr int AV_CODEC_ID_MPEG1VIDEO=1,AV_CODEC_ID_MPEG2VIDEO=2,AV_CODEC_ID_H264=3;
struct Codec {
 bool m_pHardware=false,m_interlaced=false,m_pFilterGraph=true;std::string m_filters;
 struct Hints {int codec=AV_CODEC_ID_MPEG2VIDEO;} m_hints;
 void Params(Picture* pVideoPicture){
''' + source[begin:end] + r'''
 }
};
void modeTests(){
 Codec codec;Picture pic;
 codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::PROGRESSIVE);
 codec.m_interlaced=true;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::INTERLACED_FRAME);
 codec.m_filters="bwdif=1:-1:1";codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::INTERLACED_FIELD);
 // Queued filter output keeps its actual opened route even if latest raw input was progressive.
 codec.m_interlaced=false;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::INTERLACED_FIELD);
 codec.m_filters="bwdif=0:-1:1";codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::INTERLACED_FRAME);
 codec.m_pFilterGraph=false;codec.m_filters="bwdif=1:-1:1";codec.m_interlaced=true;
 codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::INTERLACED_FRAME);
 codec.m_interlaced=false;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::PROGRESSIVE);
 codec.m_filters="";codec.m_pFilterGraph=true;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::PROGRESSIVE);
 codec.m_pHardware=true;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::UNKNOWN);
 codec.m_pHardware=false;codec.m_hints.codec=AV_CODEC_ID_H264;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::UNKNOWN);
 codec.m_hints.codec=AV_CODEC_ID_MPEG1VIDEO;codec.Params(&pic);assert(pic.mpeg2OutputMode==MPEG2OutputMode::PROGRESSIVE);
}
'''


def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def player_source():
    source = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    reset = function(source, 'void CVideoPlayerVideo::ResetMPEG2Cadence()')
    update = function(source, 'void CVideoPlayerVideo::UpdateMPEG2Cadence(')
    observe = function(source, 'if (!bPacketDrop && pPacket->pData')
    decoder = function(source, 'bool CVideoPlayerVideo::ProcessDecoderOutput(')
    assert decoder.index('GetPicture(&m_picture)') < decoder.index('m_mpeg2Cadence')
    # The flush branch also handles the Amlogic no-output timeout (VC_FLUSHED_TIMEOUT).
    recovery = '\n'.join(function(decoder, signature) for signature in (
        'if (decoderState == CDVDVideoCodec::VC_FLUSHED ||\n      decoderState == CDVDVideoCodec::VC_FLUSHED_TIMEOUT)',
        'if (decoderState == CDVDVideoCodec::VC_REOPEN)'))
    completion = function(source, 'if (!lifecyclePending && m_pendingRecoveryDiscard)')
    guard_start = source.index('bool skipHalving =')
    guard = source[guard_start:source.index(';', guard_start) + 1]
    recovery_header = (ROOT / 'xbmc/cores/VideoPlayer/DecoderFlushRecovery.h').read_text()
    return r'''
#include <atomic>
#include <memory>
#include <deque>
#include <optional>
''' + recovery_header.replace('#pragma once', '') + r'''
constexpr int CODEC_INTERLACED=64;
constexpr double MAXFRAMERATEDIFF=0.01;
constexpr double DVD_TIME_BASE=1000000;
struct CDVDMsg{};struct CDVDMsgDemuxerPacket:CDVDMsg{};
struct CDVDVideoCodec {enum State{VC_FLUSHED,VC_REOPEN,VC_FLUSHED_TIMEOUT};};
constexpr int DVD_PLAYSPEED_NORMAL=1000,DVD_PLAYSPEED_PAUSE=0;
struct IDVDStreamPlayer {enum ESyncState {SYNC_STARTING,SYNC_WAITSYNC,SYNC_INSYNC};};
constexpr int LOGDEBUG=0;
struct CLog {template<class... Args> static void Log(Args...){}};
struct DemuxPacket {const void* pData=(void*)1;int iSize=10;double duration=2*field;};
static double clockMs;
double MPEG2NowMs(){return clockMs;}
struct CVideoPlayerVideo {
 struct Hints {int codec=AV_CODEC_ID_MPEG2VIDEO,codecOptions=CODEC_INTERLACED;bool fpsrate_doubled=true;}m_hints;
 struct Info {bool hw=false,interlaced=true;float fps=0;int changes=0;
  bool IsVideoHwDecoder(){return hw;}bool GetVideoInterlaced(){return interlaced;}
  void SetVideoFps(float f){fps=f;++changes;}void SetVideoInterlaced(bool i){interlaced=i;}
  float GetNewSpeed() const {return 1.0f;}
 }m_processInfo;
 // No-output recovery state (decoder flush branch); cadence is the subject here.
 CDecoderFlushRecovery m_decoderFlushRecovery;CVideoRecoveryGeneration m_recoveryGeneration;
 std::optional<uint64_t> m_pendingNoOutputRecovery;uint64_t m_pendingNoOutputEpoch=0;
 std::atomic<uint64_t> m_syncRequest{0};int m_speed=DVD_PLAYSPEED_NORMAL;
 int m_syncState=IDVDStreamPlayer::SYNC_INSYNC;bool m_paused=false,m_stalled=false;
 struct {bool IsFull() const {return false;}} m_messageQueue;int published=0;
 void PublishNoOutputRecovery(){++published;}
 struct Tracker {int flushes=0;void Flush(){++flushes;}}m_ptsTracker;
 struct Picture {Mode mpeg2OutputMode=Mode::UNKNOWN;double iDuration=0;}m_picture;
 CMPEG2Cadence m_mpeg2Cadence;double m_mpeg2SourceRate=rate,m_fFrameRate=rate;
 bool m_mpeg2RateChanged=false;std::weak_ptr<CDVDMsg> m_mpeg2LastPacket;int resets=0;
 void ResetFrameRateCalc(){++resets;}
 struct FakeCodec {bool pending=false;int resets=0,reopens=0;void Reset(){++resets;}void Reopen(){++reopens;}bool LifecyclePending(){return pending;}};
 std::unique_ptr<FakeCodec> m_pVideoCodec=std::make_unique<FakeCodec>();
 struct Message {std::shared_ptr<CDVDMsg> message;};std::deque<Message> m_packets;
 struct Renderer {int discards=0;void DiscardBuffer(){++discards;}}m_renderManager;
 bool m_pendingRecoveryDiscard=false;int sent=0;
 void SendMessage(std::shared_ptr<CDVDMsg>,int){++sent;}
 void ResetMPEG2Cadence();void UpdateMPEG2Cadence(double& frametime);
 bool Recovery(CDVDVideoCodec::State decoderState,double& frametime){
''' + recovery + r'''
  return false;
 }
 void CompleteRecovery(bool lifecyclePending,double& frametime){
''' + completion + r'''
 }

 void Packet(const std::shared_ptr<CDVDMsg>& pMsg,DemuxPacket* pPacket,bool bPacketDrop){
''' + observe + r'''
 }
 bool Guard(double calculated){
''' + guard + r'''
 return skipHalving;
 }
};
''' + reset + '\n' + update + r'''
void recoveryTests(){
 for(auto state:{CDVDVideoCodec::VC_FLUSHED,CDVDVideoCodec::VC_REOPEN}){
  CVideoPlayerVideo p;clockMs=0;p.ResetMPEG2Cadence();filmPackets(p.m_mpeg2Cadence);
  p.m_picture.mpeg2OutputMode=Mode::PROGRESSIVE;double frameTime=field;clockMs=40;p.UpdateMPEG2Cadence(frameTime);assert(p.m_mpeg2Cadence.Film());
  p.m_packets.push_back({std::make_shared<CDVDMsgDemuxerPacket>()});
  p.m_pVideoCodec->pending=true;p.Recovery(state,frameTime);
  assert(p.sent==1&&p.m_packets.empty()&&p.m_pendingRecoveryDiscard&&!p.m_renderManager.discards&&p.m_mpeg2Cadence.Film());
  int before=p.resets;p.CompleteRecovery(true,frameTime);p.CompleteRecovery(true,frameTime);assert(p.resets==before&&p.m_mpeg2Cadence.Film());
  p.m_pVideoCodec->pending=false;clockMs=1000;p.CompleteRecovery(false,frameTime);
  assert(!p.m_pendingRecoveryDiscard&&p.m_renderManager.discards==1&&!p.m_mpeg2Cadence.Film());near(p.m_fFrameRate,rate);near(frameTime,field);
  before=p.resets;p.CompleteRecovery(false,frameTime);assert(p.resets==before&&p.m_renderManager.discards==1);
  filmPackets(p.m_mpeg2Cadence,1010);clockMs=1050;p.UpdateMPEG2Cadence(frameTime);assert(p.m_mpeg2Cadence.Film());
  p.Recovery(state,frameTime);assert(!p.m_pendingRecoveryDiscard&&p.m_renderManager.discards==2&&!p.m_mpeg2Cadence.Film());near(p.m_fFrameRate,rate);
  assert(state==CDVDVideoCodec::VC_FLUSHED?p.m_pVideoCodec->resets==2:p.m_pVideoCodec->reopens==2);
 }
}
void playerTests(){
 CVideoPlayerVideo p;clockMs=0;p.ResetMPEG2Cadence();assert(p.m_mpeg2Cadence.Eligible());
 auto same=std::make_shared<CDVDMsg>();DemuxPacket packet;
 for(int i=0;i<20;++i){clockMs=i;p.Packet(same,&packet,false);}
 p.m_picture.mpeg2OutputMode=Mode::INTERLACED_FRAME;double frameTime=field;clockMs=499;
 p.UpdateMPEG2Cadence(frameTime);near(p.m_fFrameRate,rate);
 // Distinct dropped/empty messages do not supply the missing second observation.
 packet.pData=nullptr;p.Packet(std::make_shared<CDVDMsg>(),&packet,false);packet.pData=(void*)1;
 p.Packet(std::make_shared<CDVDMsg>(),&packet,true);clockMs=500;p.UpdateMPEG2Cadence(frameTime);near(p.m_fFrameRate,rate);
 p.ResetMPEG2Cadence();clockMs=501;p.Packet(same,&packet,false);
 for(int i=0;i<5;++i){clockMs=502+i;p.Packet(std::make_shared<CDVDMsg>(),&packet,false);}
 p.UpdateMPEG2Cadence(frameTime);near(p.m_fFrameRate,30000.0/1001.0);near(frameTime,2*field);
 assert(p.m_processInfo.interlaced&&p.m_mpeg2RateChanged&&p.m_ptsTracker.flushes==1&&p.resets==1);
 assert(!p.Guard(p.m_fFrameRate/2));
 p.m_mpeg2LastPacket=same;p.ResetMPEG2Cadence();near(p.m_fFrameRate,rate);assert(!p.m_mpeg2RateChanged&&p.m_mpeg2LastPacket.expired());
 assert(p.m_processInfo.interlaced&&p.m_ptsTracker.flushes==2&&p.resets==2);
 assert(p.Guard(rate/2));
 clockMs=510;p.ResetMPEG2Cadence();for(int i=0;i<4;++i){packet.duration=(i%2?3:2)*field;clockMs=510+i*10;p.Packet(std::make_shared<CDVDMsg>(),&packet,false);}
 p.m_picture.mpeg2OutputMode=Mode::PROGRESSIVE;p.UpdateMPEG2Cadence(frameTime);
 near(p.m_fFrameRate,24000.0/1001.0);assert(!p.m_processInfo.interlaced&&p.m_mpeg2Cadence.Film());
 clockMs=560;p.ResetMPEG2Cadence();near(p.m_fFrameRate,rate);assert(p.m_processInfo.interlaced&&!p.m_mpeg2Cadence.Film());
 // Hardware, unrelated codecs and PAL leave software probe and halve guard intact.
 p.m_processInfo.hw=true;p.ResetMPEG2Cadence();assert(!p.m_mpeg2Cadence.Eligible()&&p.Guard(rate/2));
 p.m_picture.mpeg2OutputMode=Mode::UNKNOWN;p.UpdateMPEG2Cadence(frameTime);near(p.m_fFrameRate,rate);
 p.m_processInfo.hw=false;p.m_hints.codec=AV_CODEC_ID_H264;p.ResetMPEG2Cadence();assert(!p.m_mpeg2Cadence.Eligible()&&p.Guard(rate/2));
 p.m_hints.codec=AV_CODEC_ID_MPEG2VIDEO;p.m_mpeg2SourceRate=p.m_fFrameRate=50;p.ResetMPEG2Cadence();assert(!p.m_mpeg2Cadence.Eligible()&&p.Guard(25));
 p.m_mpeg2SourceRate=p.m_fFrameRate=rate;p.m_hints.fpsrate_doubled=false;p.ResetMPEG2Cadence();twoFields(p.m_mpeg2Cadence);
 p.m_picture.mpeg2OutputMode=Mode::INTERLACED_FRAME;clockMs=570;p.UpdateMPEG2Cadence(frameTime);near(p.m_fFrameRate,rate);
}
'''


def demux_source():
    source = (ROOT / 'xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp').read_text()
    start = source.index('        st->bFpsRateDoubled = false;')
    end = source.index('        // mkvmerge derives DefaultDuration', start)
    return r'''
struct Rational {int num=0,den=1;};
constexpr int AV_FIELD_UNKNOWN=0,AV_FIELD_PROGRESSIVE=1,AV_FIELD_TT=2;
struct Parameters {int codec_id=AV_CODEC_ID_MPEG2VIDEO,field_order=AV_FIELD_UNKNOWN;Rational framerate{30000,1001};};
struct Stream {Rational avg_frame_rate{30000,1001},r_frame_rate{30000,1001},time_base{1,90000};Parameters par;Parameters* codecpar=&par;};
struct Demux {bool bFpsRateDoubled=true,bUnknownIP=true,bInterlaced=true;int iFpsRate=123,iFpsScale=7;};
void policy(Stream* pStream,Demux* st){float fps=0;Rational r_frame_rate=pStream->r_frame_rate;
''' + source[start:end] + r'''
}
double fps(const Demux& d){return double(d.iFpsRate)/d.iFpsScale;}
void demuxTests(){
 Stream stream;Demux out;stream.par.field_order=AV_FIELD_TT;
 policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&out.bFpsRateDoubled);
 stream.time_base={1001,60000};policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.time_base={1,90000};stream.avg_frame_rate={60000,1001};policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.field_order=AV_FIELD_UNKNOWN;stream.avg_frame_rate={30000,1001};stream.r_frame_rate={60000,1001};
 policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 // Film avg can conceal the sequence/base field cadence; native provenance remains false.
 stream.avg_frame_rate={24000,1001};policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.r_frame_rate={120000,2002};policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.framerate={30001,1001};policy(&stream,&out);near(fps(out),24000.0/1001);assert(!out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.framerate={30000,1001};stream.par.codec_id=AV_CODEC_ID_H264;policy(&stream,&out);near(fps(out),24000.0/1001);assert(!out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.codec_id=AV_CODEC_ID_MPEG1VIDEO;policy(&stream,&out);near(fps(out),rate);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.codec_id=AV_CODEC_ID_MPEG2VIDEO;stream.avg_frame_rate={20,1};stream.r_frame_rate={50,1};stream.par.framerate={25,1};
 policy(&stream,&out);near(fps(out),20);assert(!out.bInterlaced&&!out.bFpsRateDoubled);
 stream.avg_frame_rate={25,1};policy(&stream,&out);near(fps(out),50);assert(out.bInterlaced&&!out.bFpsRateDoubled);
 stream.avg_frame_rate={24000,1001};stream.r_frame_rate={60000,1001};stream.par.framerate={0,0};
 policy(&stream,&out);near(fps(out),24000.0/1001);assert(!out.bInterlaced&&!out.bFpsRateDoubled);
 stream.par.framerate={30000,1001};stream.r_frame_rate={60000,0};policy(&stream,&out);near(fps(out),24000.0/1001);assert(!out.bInterlaced&&!out.bFpsRateDoubled);
}
'''


TESTS = r'''
#include "MPEG2Cadence.h"
#include <cassert>
#include <limits>
#include <cstdio>
#include <string>
#include <vector>
using Mode=MPEG2OutputMode;
constexpr double rate=60000.0/1001.0,timeBase=1000000.0,field=timeBase/rate;
void nearAt(double a,double b,int line){if(std::abs(a-b)>=1e-6)std::fprintf(stderr,"line %d: got %.12g expected %.12g\n",line,a,b);assert(std::abs(a-b)<1e-6);}
#define near(a,b) nearAt(a,b,__LINE__)
void twoFields(CMPEG2Cadence& c){for(int i=0;i<6;++i)c.Observe(2*field,timeBase,i*10);}
void filmPackets(CMPEG2Cadence& c,double now=0){for(int i=0;i<4;++i)c.Observe((i%2?3:2)*field,timeBase,now+i*10);}
void helperTests(){
 CMPEG2Cadence c;
 for(double invalid:{0.0,24.0,25.0,50.0,55.0,61.0,120.0,std::numeric_limits<double>::infinity()}){
  c.Reset(true,invalid,true,0);assert(!c.Eligible());filmPackets(c);assert(!c.Film()&&!c.FrameOutput());
 }
 c.Reset(false,rate,true,0);filmPackets(c);near(c.Update(Mode::PROGRESSIVE,30),rate);
 c.Reset(true,rate,true,0);assert(c.Eligible());
 c.Observe(2*field,timeBase,0);c.Observe(3*field,timeBase,10);
 near(c.Update(Mode::PROGRESSIVE,10),rate);assert(!c.Film());
 c.Observe(2*field,timeBase,20);near(c.Update(Mode::PROGRESSIVE,20),rate);
 c.Observe(3*field,timeBase,30);near(c.Update(Mode::PROGRESSIVE,30),24000.0/1001.0);assert(c.Film()&&!c.FrameOutput());
 const double filmTime=timeBase/(24000.0/1001.0);
 auto t=c.PictureTiming(filmTime,0.5,true,true);near(t.duration,filmTime);near(t.offset,field/2);
 t=c.PictureTiming(filmTime,0.5,true,false);near(t.duration,filmTime);near(t.offset,0);
 double synthetic=0;for(int i=0;i<24;++i){t=c.PictureTiming(filmTime,i%2?0.5:0,true,false);near(t.offset,0);synthetic+=t.duration;}
 near(synthetic,1001000.0);
 t=c.PictureTiming(2*field,0.5,false,true);near(t.duration,3*field);near(t.offset,field);
 t=c.PictureTiming(2*field,0.5,false,false);near(t.duration,3*field);near(t.offset,field);
 for(Mode mode:{Mode::INTERLACED_FRAME,Mode::INTERLACED_FIELD,Mode::UNKNOWN}){
  c.Reset(true,rate,true,0);filmPackets(c);near(c.Update(mode,40),rate);assert(!c.Film());
 }
 c.Reset(true,rate,true,0);twoFields(c);near(c.Update(Mode::INTERLACED_FRAME,60),30000.0/1001.0);assert(c.FrameOutput());
 // Genuine progressive29.97 in an interlaced sequence also emits complete frames.
 c.Reset(true,rate,true,0);twoFields(c);near(c.Update(Mode::PROGRESSIVE,60),30000.0/1001.0);
 c.Reset(true,rate,true,0);twoFields(c);near(c.Update(Mode::INTERLACED_FIELD,60),rate);assert(!c.FrameOutput());
 c.Reset(true,rate,false,0);twoFields(c);near(c.Update(Mode::INTERLACED_FRAME,60),rate);
 // Timeout is exact and short evidence has no film verdict; no decoder gate exists.
 c.Reset(true,rate,true,0);c.Observe(2*field,timeBase,0);c.Observe(2*field,timeBase,10);
 near(c.Update(Mode::INTERLACED_FRAME,499),rate);near(c.Update(Mode::INTERLACED_FRAME,500),30000.0/1001.0);
 c.Reset(true,rate,true,0);c.Observe(2*field,timeBase,0);c.Drain();near(c.Update(Mode::INTERLACED_FRAME,10),rate);
 c.Reset(true,rate,true,0);c.Observe(2*field,timeBase,0);c.Observe(2*field,timeBase,10);c.Drain();near(c.Update(Mode::INTERLACED_FRAME,10),30000.0/1001.0);
 // Invalid observations cannot become two-/three-field evidence.
 for(double invalid:{0.0,-1.0,field,4*field,std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity()}){
  c.Reset(true,rate,true,0);for(int i=0;i<6;++i)c.Observe(invalid,timeBase,i);near(c.Update(Mode::PROGRESSIVE,6),rate);assert(!c.Film()&&!c.FrameOutput());
 }
 for(double invalidBase:{0.0,std::numeric_limits<double>::quiet_NaN()}){
  c.Reset(true,rate,true,0);for(int i=0;i<6;++i)c.Observe(2*field,invalidBase,i);near(c.Update(Mode::INTERLACED_FRAME,6),rate);
 }
 c.Reset(true,60.0,true,0);for(int i=0;i<6;++i)c.Observe(5.0,120.0,i);near(c.Update(Mode::PROGRESSIVE,6),60.0);
 c.Reset(true,rate,true,0);for(int i=0;i<6;++i)c.Observe(3*field,timeBase,i);near(c.Update(Mode::PROGRESSIVE,6),rate);assert(!c.Film()&&!c.FrameOutput());
 // Mixed film/video must discard its film verdict and earn it again.
 c.Reset(true,rate,true,0);filmPackets(c);c.Update(Mode::PROGRESSIVE,40);assert(c.Film());
 c.Observe(2*field,timeBase,50);c.Observe(2*field,timeBase,60);c.Observe(2*field,timeBase,70);
 near(c.Update(Mode::PROGRESSIVE,70),rate);assert(!c.Film());
 filmPackets(c,80);near(c.Update(Mode::PROGRESSIVE,120),24000.0/1001.0);assert(c.Film());
 near(c.Update(Mode::INTERLACED_FIELD,121),rate);assert(!c.Film());
 // Long same-progressive-mode video section must not permanently exhaust probing.
 c.Reset(true,rate,true,0);filmPackets(c);c.Update(Mode::PROGRESSIVE,40);assert(c.Film());
 for(int i=0;i<12;++i){c.Observe(2*field,timeBase,50+i*10);c.Update(Mode::PROGRESSIVE,50+i*10);}
 assert(!c.Film());
 for(int i=0;i<12;++i){c.Observe((i%2?3:2)*field,timeBase,200+i*10);c.Update(Mode::PROGRESSIVE,200+i*10);}
 assert(c.Film());near(c.Update(Mode::PROGRESSIVE,320),24000.0/1001.0);
 c.Reset(true,rate,true,200);assert(!c.Film()&&!c.FrameOutput());near(c.Update(Mode::PROGRESSIVE,200),rate);
 filmPackets(c,210);c.Update(Mode::PROGRESSIVE,250);assert(c.Film());
 c.Reset(false,rate,false,260);assert(!c.Film()&&!c.FrameOutput());near(c.Update(Mode::UNKNOWN,260),rate);
 c.Reset(true,rate,true,300);twoFields(c);c.Update(Mode::INTERLACED_FRAME,360);assert(c.FrameOutput());
 c.Reset(true,rate,false,400);assert(!c.FrameOutput());twoFields(c);near(c.Update(Mode::INTERLACED_FRAME,460),rate);
}
'''


def soft_headers(data):
    """Encode real MPEG pictures first, then set synthetic legal soft-RFF headers.

    ISO MPEG-2 sequence rate becomes30000/1001, progressive_sequence=0;
    progressive_frame=1 and repeat_first_field alternates. No pixels are changed.
    FFprobe must independently recover the2/3 durations and repeat/interlace bits.
    """
    data = bytearray(data)
    count = 0

    def bit(start, index, value):
        at = start + index // 8
        mask = 1 << (7 - index % 8)
        data[at] = data[at] | mask if value else data[at] & ~mask

    for i in range(len(data) - 8):
        if data[i:i + 4] == b'\x00\x00\x01\xb3':
            data[i + 7] = (data[i + 7] & 0xf0) | 4
        if data[i:i + 4] == b'\x00\x00\x01\xb5':
            kind = data[i + 4] >> 4
            if kind == 1:
                bit(i + 4, 12, 0)
            elif kind == 8:
                bit(i + 4, 30, count % 2)
                bit(i + 4, 32, 1)
                count += 1
    assert count == 24
    return data


def media_tests(out):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        print('SKIP: FFmpeg/FFprobe unavailable; no actual sample/filter acceptance')
        return '', {'available': False}
    version = command(['ffmpeg', '-version']).stdout.splitlines()[0]
    common = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
    base, film, video = out / 'film-base.m2v', out / 'soft-film.m2v', out / 'interlaced.m2v'
    command(common + ['-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=24000/1001',
                      '-frames:v', '24', '-c:v', 'mpeg2video', '-g', '1', '-bf', '0', '-f', 'mpeg2video', str(base)])
    film.write_bytes(soft_headers(base.read_bytes()))
    command(common + ['-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=60000/1001',
                      '-vf', 'tinterlace=mode=interleave_top', '-frames:v', '24', '-flags', '+ilme+ildct',
                      '-top', '1', '-c:v', 'mpeg2video', '-g', '1', '-bf', '0', '-f', 'mpeg2video', str(video)])
    parsed = {}
    for name, path in [('film', film), ('video', video)]:
        parsed[name] = json.loads(command(['ffprobe', '-v', 'error', '-show_frames', '-show_packets',
                     '-show_entries', 'frame=interlaced_frame,repeat_pict,pkt_duration_time:packet=duration_time',
                     '-of', 'json', str(path)]).stdout)['packets_and_frames']
    frames = [[f for f in parsed[name] if f['type'] == 'frame'] for name in ['film', 'video']]
    assert len(frames[0]) == len(frames[1]) == 24
    assert all(not f['interlaced_frame'] for f in frames[0])
    assert [f['repeat_pict'] for f in frames[0]] == [i % 2 for i in range(24)]
    assert all(f['interlaced_frame'] and not f['repeat_pict'] for f in frames[1])
    durations = [[float(f['duration_time']) * 1e6 for f in parsed[name] if f['type'] == 'packet'] for name in ['film', 'video']]
    assert len(durations[0]) == len(durations[1]) == 24
    # Oracles from independently decoded sample duration and output count.
    assert all(abs(d / (1e6 * 1001 / 60000) - (2 + i % 2)) < .001 for i, d in enumerate(durations[0]))
    assert all(abs(d / (1e6 * 1001 / 60000) - 2) < .001 for d in durations[1])
    counts = {}
    for mode, expected in [(1, 48), (0, 24)]:
        md5 = out / f'bwdif-{mode}.md5'
        command(common + ['-i', str(video), '-vf', f'bwdif={mode}:-1:1', '-f', 'framemd5', str(md5)])
        counts[str(mode)] = len([line for line in md5.read_text().splitlines() if line and not line.startswith('#')])
        assert counts[str(mode)] == expected
    data = ','.join(format(d, '.12g') for d in durations[0])
    return r'''
void realSamples(){
 CMPEG2Cadence c;c.Reset(true,rate,true,0);
 const double packets[]={@DATA@};
 for(int i=0;i<4;++i)c.Observe(packets[i],timeBase,i*10);
 near(c.Update(Mode::PROGRESSIVE,40),24000.0/1001.0);assert(c.Film());
 const double frameTime=timeBase/(24000.0/1001.0);
 for(int i=0;i<24;++i){auto timing=c.PictureTiming(frameTime,i%2?0.5:0,true,true);
  near(timing.duration,frameTime);near(timing.offset,i%2?field/2:0);}
 c.Reset(true,rate,true,0);twoFields(c);near(c.Update(Mode::INTERLACED_FIELD,60),rate);
 c.Reset(true,rate,true,0);twoFields(c);near(c.Update(Mode::INTERLACED_FRAME,60),30000.0/1001.0);
}
'''.replace('@DATA@', data), {'available': True, 'version': version, 'film_frames': 24,
                            'video_frames': 24, 'bwdif_frame_counts': counts,
                            'sample_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [film, video]}}


def compile_run(out, header, code, name, negative=False):
    (out / 'MPEG2Cadence.h').write_text(header)
    source, binary = out / (name + '.cpp'), out / name
    source.write_text(code)
    command([os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
             '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
             '-I', str(out), str(source), '-o', str(binary)])
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
    if negative:
        assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    header = HEADER.read_text()
    with tempfile.TemporaryDirectory(prefix='mpeg2-cadence-') as tmp:
        out = Path(tmp)
        media, record = media_tests(out)
        mode = source_mode()
        integration = player_source()
        demux = demux_source()
        code = TESTS + mode + integration + demux + media + '\nint main(){helperTests();modeTests();playerTests();recoveryTests();demuxTests();' + ('realSamples();' if media else '') + '}\n'
        compile_run(out, header, code, 'cadence')
        print('PASS: production MPEG cadence, repeat timing, mixed/reset/drain, retry/recovery and actual FFmpeg mode publication; ASan/UBSan')
        if record['available']:
            print('PASS: decoded synthetic soft-RFF/interlaced MPEG-2, 24 pictures each; BWDIF full48/half24')
        rejected = []
        if args.negative_controls:
            mutations = {
                'film rate scaling': ('m_film ? 0.4 :', 'm_film ? 1.0 :'),
                'film repeat counted twice': ('return {duration, timestamp', 'return {duration * (1.0 + repeat), timestamp'),
                'missing timestamp repeat phase': ('timestamp && repeat > 0.0', '(timestamp || !timestamp) && repeat > 0.0'),
                'field output halved': ('mode == MPEG2OutputMode::INTERLACED_FRAME ||', 'mode == MPEG2OutputMode::INTERLACED_FIELD || mode == MPEG2OutputMode::INTERLACED_FRAME ||'),
                'source provenance ignored': ('m_frameCandidate = m_doubled &&', 'm_frameCandidate ='),
                'timeout delayed': ('nowMs - m_startedMs >= 500.0', 'nowMs - m_startedMs >= 501.0'),
                'PAL probe expansion': ('fieldRate > 55.0', 'fieldRate > 49.0'),
                'one alternation film verdict': ('m_alternations >= 3', 'm_alternations >= 1'),
                'same-mode film never reacquired': ('if (!m_remaining && !m_film && m_mode == MPEG2OutputMode::PROGRESSIVE && count == 3)', 'if (false && !m_remaining && !m_film && m_mode == MPEG2OutputMode::PROGRESSIVE && count == 3)'),
                'mixed cadence stays film': ('m_contradictions >= 2', 'm_contradictions >= 100'),
                'reset keeps prior film': ('m_film = m_filmCandidate = m_frameOutput = m_frameCandidate = false;', 'm_frameOutput = m_frameCandidate = false;'),
                'drain evidence unresolved': ('void Drain() { FinishProbe(); }', 'void Drain() {}'),
            }
            for name, (old, new) in mutations.items():
                assert old in header, name
                compile_run(out, header.replace(old, new, 1), code, 'mutant', negative=True)
                rejected.append(name)
                print('REJECTED:', name)
        if args.negative_controls:
            for name, old, new in [
                ('retry packet evidence duplicated', ' && m_mpeg2LastPacket.lock() != pMsg', ''),
                ('dropped packet used as evidence', '!bPacketDrop && ', '(bPacketDrop || !bPacketDrop) && '),
                ('recovery completion leaves cadence stale', 'm_pendingRecoveryDiscard = false;\n      ResetMPEG2Cadence();', 'm_pendingRecoveryDiscard = false;'),
                ('reset keeps prior effective rate', 'm_fFrameRate = m_mpeg2SourceRate;', ''),
                ('reset keeps last packet identity', 'm_mpeg2LastPacket.reset();', ''),
                ('MPEG exception weakens all codecs', 'bool skipHalving = (m_hints.codecOptions & CODEC_INTERLACED)', 'bool skipHalving = false && (m_hints.codecOptions & CODEC_INTERLACED)'),
            ]:
                assert old in integration, name
                mutant = code.replace(integration, integration.replace(old, new, 1), 1)
                compile_run(out, header, mutant, 'integration-mutant', negative=True)
                rejected.append(name)
                print('REJECTED:', name)
        if args.negative_controls:
            for name, old, new in [
                ('failed filter claimed field output', 'm_pFilterGraph && m_filters.compare(0, 8,', 'm_filters.compare(0, 8,'),
                ('hardware published software mode', '!m_pHardware && ', '(m_pHardware || !m_pHardware) && '),
                ('unrelated codec published MPEG mode', 'm_hints.codec == AV_CODEC_ID_MPEG1VIDEO ||', 'm_hints.codec >= 0 ||'),
                ('full BWDIF tagged frame output', 'pVideoPicture->mpeg2OutputMode = MPEG2OutputMode::INTERLACED_FIELD;', 'pVideoPicture->mpeg2OutputMode = MPEG2OutputMode::INTERLACED_FRAME;'),
            ]:
                assert old in mode, name
                mutant = code.replace(mode, mode.replace(old, new, 1), 1)
                compile_run(out, header, mutant, 'mode-mutant', negative=True)
                rejected.append(name)
                print('REJECTED:', name)
        if args.negative_controls:
            for name, old, new in [
                ('demux provenance missing', 'st->bFpsRateDoubled = true;', 'st->bFpsRateDoubled = false;'),
                ('native rate falsely doubled', 'st->bFpsRateDoubled = false;', 'st->bFpsRateDoubled = true;'),
                ('sequence fallback missing', 'else if ((pStream->codecpar->codec_id', 'else if (false && (pStream->codecpar->codec_id'),
                ('sequence fallback expands non-MPEG', 'pStream->codecpar->codec_id == AV_CODEC_ID_MPEG1VIDEO ||', 'pStream->codecpar->codec_id >= 0 ||'),
                ('sequence fallback expands PAL', '/ r_frame_rate.den > 55.0', '/ r_frame_rate.den > 45.0'),
                ('sequence ratio accepts mismatch', '* pStream->codecpar->framerate.den ==', '* pStream->codecpar->framerate.den <='),
            ]:
                assert old in demux, name
                mutant = code.replace(demux, demux.replace(old, new, 1), 1)
                compile_run(out, header, mutant, 'demux-mutant', negative=True)
                rejected.append(name)
                print('REJECTED:', name)
        if args.report:
            args.report.write_text(json.dumps({'header_sha256': hashlib.sha256(header.encode()).hexdigest(),
                                              'media': record, 'negative_controls': rejected,
                                              'limits': 'Host decoder/tool version; production helper/mode/demux/Reset/Update/dedup/recovery blocks tested, not Kodi playback, CE runtime or physical deinterlacing.'}, indent=2) + '\n')


if __name__ == '__main__':
    main()
