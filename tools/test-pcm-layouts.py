#!/usr/bin/env python3
"""Execute production PCM layout/negotiation/ALSA/rematrix seams on the host.

AEChannelInfo and OmniphonyPcmStream are compiled intact. Other functions are
extracted verbatim; settings, sink/device discovery and logging are stand-ins.
Rematrix uses the host libswresample, not CE playback or physical speaker output.
FFmpeg public headers can be selected with --ffmpeg-include; they must expose the
AVChannelLayout API compatible with the host shared libraries.
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

AUDIO = Path('xbmc/cores/AudioEngine')
CHANNEL = AUDIO / 'Utils/AEChannelInfo.cpp'
DATA = AUDIO / 'Utils/AEChannelData.h'
UTIL = AUDIO / 'Utils/AEUtil.cpp'
ACTIVE = AUDIO / 'Engines/ActiveAE/ActiveAE.cpp'
ALSA = AUDIO / 'Sinks/AESinkALSA.cpp'
RESAMPLE = AUDIO / 'Engines/ActiveAE/ActiveAEResampleFFMPEG.cpp'
RESAMPLE_H = AUDIO / 'Engines/ActiveAE/ActiveAEResampleFFMPEG.h'
OMNI = Path('xbmc/cores/VideoPlayer/DVDCodecs/Audio/OmniphonyPcmStream.cpp')
SETTINGS_H = Path('xbmc/settings/Settings.h')


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def one(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError(f'mutation/extraction anchor is not unique: {old!r}')
    return source.replace(old, new)


PREFIX = r'''
#include "cores/AudioEngine/Utils/AEChannelInfo.h"
#include "cores/AudioEngine/Interfaces/AE.h"
#include "cores/AudioEngine/Utils/AEDeviceInfo.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyPcmStream.h"
extern "C" {
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libavcodec/codec_id.h>
#include <libswresample/swresample.h>
#include <alsa/asoundlib.h>
}
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <vector>
using namespace std::chrono_literals;
constexpr int LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3, LOGAUDIO=4;
struct CLog { template<class...T> static void Log(int,const char*,T&&...) {} };
struct CAEUtil {
 static CAEChannelInfo GuessChLayout(unsigned int);
 static const char* GetStdChLayoutName(AEStdChLayout);
 static uint64_t GetAVChannelLayout(const CAEChannelInfo&);
 static CAEChannelInfo GetAEChannelLayout(uint64_t);
 static AVChannel GetAVChannel(AEChannel);
 static int GetAVChannelIndex(AEChannel,uint64_t);
};
struct CSettings {
 @SETTING_IDS@
 std::map<std::string,int> ints;
 std::map<std::string,bool> bools;
 std::map<std::string,double> numbers;
 std::string GetString(const char*) const {return "test-device";}
 int GetInt(const char* id) const {auto it=ints.find(id);return it==ints.end()?0:it->second;}
 bool GetBool(const char* id) const {auto it=bools.find(id);return it!=bools.end()&&it->second;}
 double GetNumber(const char* id) const {auto it=numbers.find(id);return it==numbers.end()?0:it->second;}
};
struct SettingsComponent {
 std::shared_ptr<CSettings> settings=std::make_shared<CSettings>();
 std::shared_ptr<CSettings> GetSettings() {return settings;}
};
struct Logging {bool CanLogComponent(int) const{return false;}};
struct CServiceBroker {
 static SettingsComponent* GetSettingsComponent(){static SettingsComponent c;return &c;}
 static Logging& GetLogging(){static Logging l;return l;}
};
struct AudioSettings {
 int channels=1,config=AE_CONFIG_AUTO;unsigned int samplerate=48000;
 bool passthrough=false,ac3passthrough=false,ac3transcode=false,eac3passthrough=false,stereoupmix=false;
 std::string device="test-device",passthroughdevice="test-passthrough";
};
struct Sink {
 AEDeviceType deviceType=AE_DEVTYPE_HDMI;
 bool passthroughDevice=true, supports=true;
 AEDeviceType GetDeviceType(const std::string&) const{return deviceType;}
 bool SupportsFormat(const std::string&,const AEAudioFormat&) const{return supports;}
 bool HasPassthroughDevice() const{return passthroughDevice;}
};
struct Stats {double water=0;double GetWaterLevel()const{return water;}};
struct CActiveAE {
 enum {MODE_PCM=0,MODE_RAW=1,MODE_TRANSCODE=2};
 int m_mode=MODE_PCM;
 Sink m_sink;
 Stats m_stats;
 AudioSettings m_settings;
 std::vector<int> m_streams{1};
 std::chrono::milliseconds m_extKeepConfig{0};
 AEAudioFormat m_internalFormat;
 void ApplySettingsToFormat(AEAudioFormat&,const AudioSettings&,int* mode=nullptr);
 bool IsSettingVisible(const std::string&);
};
std::vector<snd_pcm_chmap_query_t*> supportedMaps;
snd_pcm_chmap_t* actualMap=nullptr;
snd_pcm_chmap_t* copyMap(const snd_pcm_chmap_t* src) {
 if(!src)return nullptr;
 auto n=sizeof(snd_pcm_chmap_t)+src->channels*sizeof(unsigned int);
 auto dst=static_cast<snd_pcm_chmap_t*>(malloc(n));memcpy(dst,src,n);return dst;
}
extern "C" snd_pcm_chmap_query_t** snd_pcm_query_chmaps(snd_pcm_t*) {
 if(supportedMaps.empty())return nullptr;
 auto out=static_cast<snd_pcm_chmap_query_t**>(calloc(supportedMaps.size()+1,sizeof(void*)));
 for(size_t i=0;i<supportedMaps.size();++i){
   auto n=sizeof(snd_pcm_chmap_query_t)+supportedMaps[i]->map.channels*sizeof(unsigned int);
   out[i]=static_cast<snd_pcm_chmap_query_t*>(malloc(n));memcpy(out[i],supportedMaps[i],n);
 }
 return out;
}
extern "C" void snd_pcm_free_chmaps(snd_pcm_chmap_query_t** maps) {
 if(!maps)return;for(auto p=maps;*p;++p)free(*p);free(maps);
}
extern "C" snd_pcm_chmap_t* snd_pcm_get_chmap(snd_pcm_t*) {return copyMap(actualMap);}
struct CAESinkALSA {
 snd_pcm_t* m_pcm=nullptr;
 CAEChannelInfo GetChannelLayoutRaw(const AEAudioFormat&);
 CAEChannelInfo GetChannelLayoutLegacy(const AEAudioFormat&,unsigned int,unsigned int);
 CAEChannelInfo GetChannelLayout(const AEAudioFormat&,unsigned int);
 static AEChannel ALSAChannelToAEChannel(unsigned int);
 static unsigned int AEChannelToALSAChannel(AEChannel);
 static CAEChannelInfo ALSAchmapToAEChannelMap(snd_pcm_chmap_t*);
 static snd_pcm_chmap_t* AEChannelMapToALSAchmap(const CAEChannelInfo&);
 static snd_pcm_chmap_t* CopyALSAchmap(snd_pcm_chmap_t*);
 static unsigned int ALSAchmapActiveCount(const snd_pcm_chmap_t&);
 static CAEChannelInfo GetAlternateLayoutForm(const CAEChannelInfo&);
 snd_pcm_chmap_t* SelectALSAChannelMap(const CAEChannelInfo&);
 static std::string ALSAchmapToString(snd_pcm_chmap_t*){return "fixture-map";}
};
struct CActiveAEResampleFFMPEG {
 @RESAMPLER_MEMBERS@
 CActiveAEResampleFFMPEG();~CActiveAEResampleFFMPEG();
 bool Init(SampleConfig,SampleConfig,bool,bool,double,double,CAEChannelInfo*,AEQuality,bool,float);
};
'''

MAIN = r'''
CAEChannelInfo expect(std::initializer_list<AEChannel> channels){
 CAEChannelInfo result;for(auto ch:channels)result+=ch;return result;
}
void equal(const CAEChannelInfo& a,const CAEChannelInfo& b){assert(a==b);}
AEAudioFormat fmt(const CAEChannelInfo& info){
 AEAudioFormat out;out.m_channelLayout=info;out.m_dataFormat=AE_FMT_FLOAT;out.m_sampleRate=44100;return out;
}
void closeMaps(){for(auto p:supportedMaps)free(p);supportedMaps.clear();free(actualMap);actualMap=nullptr;}
void addMap(std::initializer_list<unsigned> channels,int type=SND_CHMAP_TYPE_FIXED){
 auto bytes=sizeof(snd_pcm_chmap_query_t)+channels.size()*sizeof(unsigned);
 auto map=static_cast<snd_pcm_chmap_query_t*>(calloc(1,bytes));map->type=static_cast<snd_pcm_chmap_type>(type);
 map->map.channels=channels.size();std::copy(channels.begin(),channels.end(),map->map.pos);supportedMaps.push_back(map);
}
SampleConfig cfg(const CAEChannelInfo& info){
 return {AV_SAMPLE_FMT_FLT,CAEUtil::GetAVChannelLayout(info),static_cast<int>(info.Count()),48000,32,32};
}
std::vector<float> pulse(const CAEChannelInfo& source,const CAEChannelInfo& destination,AEChannel channel,
                        bool upmix=false,float sublevel=0.0f,bool remap=false,int lfemixto=0){
 CServiceBroker::GetSettingsComponent()->settings->ints[CSettings::SETTING_AUDIOOUTPUT_LFEMIXTO]=lfemixto;
 CActiveAEResampleFFMPEG r;auto src=cfg(source),dst=cfg(destination);auto order=destination;
 assert(r.Init(dst,src,upmix,false,M_SQRT1_2,M_SQRT1_2,remap?&order:nullptr,AE_QUALITY_MID,false,sublevel));
 constexpr int samples=16;
 std::vector<float> input(samples*source.Count(),0.0f),output(samples*destination.Count(),-999.0f);
 int index=CAEUtil::GetAVChannelIndex(channel,src.channel_layout);assert(index>=0);
 for(int i=0;i<samples;++i)input[i*source.Count()+index]=0.25f;
 uint8_t* in=reinterpret_cast<uint8_t*>(input.data());uint8_t* out=reinterpret_cast<uint8_t*>(output.data());
 assert(swr_convert(r.m_pContext,&out,samples,const_cast<const uint8_t**>(&in),samples)==samples);
 output.resize(destination.Count());return output;
}
void near(float actual,float expected){assert(std::abs(actual-expected)<0.00001f);}
int main(){
 const auto six0=expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_BL,AE_CH_BR,AE_CH_BC});
 const auto six1=expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_LFE,AE_CH_BL,AE_CH_BR,AE_CH_BC});
 const CAEChannelInfo old[]{AE_CH_LAYOUT_1_0,AE_CH_LAYOUT_2_0,AE_CH_LAYOUT_2_1,AE_CH_LAYOUT_3_0,
  AE_CH_LAYOUT_3_1,AE_CH_LAYOUT_4_0,AE_CH_LAYOUT_4_1,AE_CH_LAYOUT_5_0,AE_CH_LAYOUT_5_1,AE_CH_LAYOUT_7_0,AE_CH_LAYOUT_7_1};
 const CAEChannelInfo saved[]{
  expect({AE_CH_FL,AE_CH_FR}),expect({AE_CH_FL,AE_CH_FR,AE_CH_LFE}),expect({AE_CH_FL,AE_CH_FR,AE_CH_FC}),
  expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_LFE}),expect({AE_CH_FL,AE_CH_FR,AE_CH_BL,AE_CH_BR}),
  expect({AE_CH_FL,AE_CH_FR,AE_CH_BL,AE_CH_BR,AE_CH_LFE}),expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_BL,AE_CH_BR}),
  expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_LFE,AE_CH_BL,AE_CH_BR}),
  expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_BL,AE_CH_BR,AE_CH_SL,AE_CH_SR}),
  expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_LFE,AE_CH_BL,AE_CH_BR,AE_CH_SL,AE_CH_SR})};
 const char* names[]{"1.0","2.0","2.1","3.0","3.1","4.0","4.1","5.0","5.1","7.0","7.1","6.0","6.1"};
 assert(AE_CH_LAYOUT_6_0==11&&AE_CH_LAYOUT_6_1==12&&AE_CH_LAYOUT_MAX==13);
 for(int i=0;i<11;++i){auto enumLayout=static_cast<AEStdChLayout>(i);equal(CAEChannelInfo(enumLayout),old[i]);
   assert(std::string(CAEUtil::GetStdChLayoutName(enumLayout))==names[i]);if(i>0)equal(old[i],saved[i-1]);}
 equal(CAEChannelInfo(AE_CH_LAYOUT_6_0),six0);equal(CAEChannelInfo(AE_CH_LAYOUT_6_1),six1);
 assert(CAEUtil::GetStdChLayoutName(AE_CH_LAYOUT_6_0)!=nullptr);
 assert(CAEUtil::GetStdChLayoutName(AE_CH_LAYOUT_6_1)!=nullptr);
 assert(std::string(CAEUtil::GetStdChLayoutName(AE_CH_LAYOUT_6_0))=="6.0");
 assert(std::string(CAEUtil::GetStdChLayoutName(AE_CH_LAYOUT_6_1))=="6.1");
 assert(std::string(CAEUtil::GetStdChLayoutName(AE_CH_LAYOUT_INVALID))=="UNKNOWN");
 equal(CAEUtil::GuessChLayout(6),saved[7]);equal(CAEUtil::GuessChLayout(7),saved[8]);
 assert(CAEUtil::GuessChLayout(0).Count()==0&&CAEUtil::GuessChLayout(9).Count()==0);
 const uint64_t back6=AV_CH_FRONT_LEFT|AV_CH_FRONT_RIGHT|AV_CH_FRONT_CENTER|AV_CH_BACK_LEFT|AV_CH_BACK_RIGHT|AV_CH_BACK_CENTER;
 const uint64_t back61=back6|AV_CH_LOW_FREQUENCY;
 assert(CAEUtil::GetAVChannelLayout(six0)==back6&&CAEUtil::GetAVChannelLayout(six1)==back61);
 assert(back6!=AV_CH_LAYOUT_6POINT0&&back61!=AV_CH_LAYOUT_6POINT1);
 equal(CAEUtil::GetAEChannelLayout(back6),six0);equal(CAEUtil::GetAEChannelLayout(back61),six1);
 assert(CAEUtil::GetAVChannelIndex(AE_CH_BC,back6)==5&&CAEUtil::GetAVChannelIndex(AE_CH_BC,back61)==6);
 std::vector<uint8_t> labels;
 assert(OmniphonyPcmChannelLabels(back6,6,labels));assert((labels==std::vector<uint8_t>{0,1,2,14,15,16}));
 assert(OmniphonyPcmChannelLabels(back61,7,labels));assert((labels==std::vector<uint8_t>{0,1,2,3,14,15,16}));
 assert(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_6POINT1,7,labels));assert((labels==std::vector<uint8_t>{0,1,2,3,16,4,5}));
 assert(OmniphonyPcmChannelLabels(AV_CH_LAYOUT_STEREO,2,labels));assert((labels==std::vector<uint8_t>{0,1}));
 assert(std::string(OmniphonyPcmCodecId(AV_CODEC_ID_TRUEHD))=="truehd");
 assert(std::string(OmniphonyPcmCodecId(AV_CODEC_ID_EAC3))=="eac3");
 CActiveAE engine;AudioSettings s;int mode=-1;
 s.config=AE_CONFIG_FIXED;engine.m_settings=s;
 for(int i=1;i<=12;++i){s.channels=i;auto f=fmt(saved[9]);engine.ApplySettingsToFormat(f,s,&mode);
   equal(f.m_channelLayout,i<=10?saved[i-1]:(i==11?six0:six1));assert(mode==CActiveAE::MODE_PCM&&f.m_sampleRate==48000);}
 for(int config:{AE_CONFIG_AUTO,AE_CONFIG_MATCH}){
   s.config=config;engine.m_settings=s;
   for(int value:{11,12}){
     s.channels=value;
     auto f=fmt(value==11?six0:six1);engine.ApplySettingsToFormat(f,s);equal(f.m_channelLayout,value==11?six0:six1);
     auto side=fmt(expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_LFE,AE_CH_SL,AE_CH_SR}));
     engine.ApplySettingsToFormat(side,s);assert(side.m_channelLayout.HasChannel(AE_CH_BL)&&side.m_channelLayout.HasChannel(AE_CH_BR));
     assert(!side.m_channelLayout.HasChannel(AE_CH_SL)&&!side.m_channelLayout.HasChannel(AE_CH_BC));
     assert(side.m_channelLayout.HasChannel(AE_CH_LFE)==(value==12));
     auto stereo=fmt(AE_CH_LAYOUT_2_0);engine.ApplySettingsToFormat(stereo,s);equal(stereo.m_channelLayout,CAEChannelInfo(AE_CH_LAYOUT_2_0));
     s.stereoupmix=true;auto up=fmt(AE_CH_LAYOUT_2_0);engine.ApplySettingsToFormat(up,s);equal(up.m_channelLayout,value==11?six0:six1);s.stereoupmix=false;
   }
 }
 s.config=AE_CONFIG_AUTO;engine.m_settings=s;engine.m_internalFormat=fmt(saved[9]);engine.m_extKeepConfig=1ms;
 auto kept=fmt(six1);engine.ApplySettingsToFormat(kept,s);equal(kept.m_channelLayout,saved[9]);engine.m_extKeepConfig=0ms;
 engine.m_stats.water=1;auto stereo=fmt(AE_CH_LAYOUT_2_0);engine.ApplySettingsToFormat(stereo,s);equal(stereo.m_channelLayout,saved[9]);engine.m_stats.water=0;
 for(int v:{11,12}){s.channels=v;auto f=fmt(v==11?six0:six1);f.m_dataFormat=AE_FMT_RAW;f.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_TRUEHD;
   engine.ApplySettingsToFormat(f,s,&mode);assert(mode==CActiveAE::MODE_RAW&&f.m_dataFormat==AE_FMT_RAW&&f.m_sampleRate==44100);equal(f.m_channelLayout,v==11?six0:six1);}
 engine.m_sink.deviceType=AE_DEVTYPE_IEC958;s.channels=12;s.samplerate=48000;engine.m_settings=s;
 auto spdif=fmt(six1);spdif.m_sampleRate=96000;engine.ApplySettingsToFormat(spdif,s);equal(spdif.m_channelLayout,CAEChannelInfo(AE_CH_LAYOUT_2_0));assert(spdif.m_sampleRate==48000);
 assert(!engine.IsSettingVisible(CSettings::SETTING_AUDIOOUTPUT_CHANNELS));
 for(auto dev:{AE_DEVTYPE_PCM,AE_DEVTYPE_HDMI}){engine.m_sink.deviceType=dev;assert(engine.IsSettingVisible(CSettings::SETTING_AUDIOOUTPUT_CHANNELS));}
 s.channels=1;s.passthrough=true;s.ac3passthrough=true;s.ac3transcode=true;s.eac3passthrough=true;engine.m_settings=s;
 auto trans=fmt(saved[7]);engine.ApplySettingsToFormat(trans,s,&mode);assert(mode==CActiveAE::MODE_TRANSCODE&&trans.m_streamInfo.m_type==CAEStreamInfo::STREAM_TYPE_EAC3);
 engine.m_sink.supports=false;trans=fmt(saved[7]);engine.ApplySettingsToFormat(trans,s,&mode);assert(trans.m_streamInfo.m_type==CAEStreamInfo::STREAM_TYPE_AC3);engine.m_sink.supports=true;
 s.channels=11;trans=fmt(saved[7]);engine.ApplySettingsToFormat(trans,s,&mode);assert(mode==CActiveAE::MODE_PCM);
 CAESinkALSA sink;
 assert(sink.AEChannelToALSAChannel(AE_CH_BC)==SND_CHMAP_RC&&sink.ALSAChannelToAEChannel(SND_CHMAP_RC)==AE_CH_BC);
 addMap({SND_CHMAP_FL,SND_CHMAP_FR});addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_LFE,SND_CHMAP_FC});
 addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_LFE,SND_CHMAP_FC,SND_CHMAP_RL,SND_CHMAP_RR});
 addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_LFE,SND_CHMAP_FC,SND_CHMAP_RL,SND_CHMAP_RR,SND_CHMAP_RLC,SND_CHMAP_RRC});
 for(auto req:{six0,six1}){
   auto physical=sink.SelectALSAChannelMap(req);assert(physical&&physical->channels==6);
   equal(sink.ALSAchmapToAEChannelMap(physical),expect({AE_CH_FL,AE_CH_FR,AE_CH_LFE,AE_CH_FC,AE_CH_BL,AE_CH_BR}));
   actualMap=copyMap(physical);auto f=fmt(req);auto actual=sink.GetChannelLayout(f,6);assert(!actual.HasChannel(AE_CH_BC));
   auto resolved=req;resolved.ResolveChannels(actual);assert(!resolved.HasChannel(AE_CH_BC)&&resolved.HasChannel(AE_CH_BL)&&resolved.HasChannel(AE_CH_BR));
   assert(resolved.HasChannel(AE_CH_LFE)==req.HasChannel(AE_CH_LFE));free(physical);free(actualMap);actualMap=nullptr;
 }
 auto orphan=expect({AE_CH_FL,AE_CH_FR,AE_CH_FC,AE_CH_BC});orphan.ResolveChannels(saved[7]);assert(orphan.HasChannel(AE_CH_BL)&&orphan.HasChannel(AE_CH_BR));
 closeMaps();
 addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_FC,SND_CHMAP_RL,SND_CHMAP_RR,SND_CHMAP_RC});
 auto exact=sink.SelectALSAChannelMap(six0);assert(exact);equal(sink.ALSAchmapToAEChannelMap(exact),six0);free(exact);closeMaps();
 addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_FC,SND_CHMAP_LFE,SND_CHMAP_RL,SND_CHMAP_RR,SND_CHMAP_RC},SND_CHMAP_TYPE_VAR);
 exact=sink.SelectALSAChannelMap(six1);assert(exact);equal(sink.ALSAchmapToAEChannelMap(exact),six1);free(exact);closeMaps();
 // Fixed map in a padded container keeps its NA slot: existing PR79 matching.
 addMap({SND_CHMAP_FL,SND_CHMAP_FR,SND_CHMAP_NA,SND_CHMAP_FC,SND_CHMAP_RL,SND_CHMAP_RR,SND_CHMAP_RC});
 exact=sink.SelectALSAChannelMap(six0);assert(exact&&exact->channels==7&&sink.ALSAchmapActiveCount(*exact)==6);free(exact);closeMaps();
 // Native G12B 6.x keeps canonical slots inside the full eight-slot transport.
 for(bool lfe:{false,true}){
   auto req=lfe?six1:six0;
   addMap({SND_CHMAP_FL,SND_CHMAP_FR,lfe?SND_CHMAP_LFE:SND_CHMAP_NA,SND_CHMAP_FC,
           SND_CHMAP_RL,SND_CHMAP_RR,SND_CHMAP_RC,SND_CHMAP_NA});
   auto selected=sink.SelectALSAChannelMap(req);
   assert(selected&&selected->channels==8&&sink.ALSAchmapActiveCount(*selected)==req.Count());
   assert(selected->pos[6]==SND_CHMAP_RC&&selected->pos[7]==SND_CHMAP_NA);
   assert(selected->pos[2]==(lfe?SND_CHMAP_LFE:SND_CHMAP_NA));
   actualMap=copyMap(selected);
   auto packed=sink.GetChannelLayout(fmt(req),8);
   assert(packed.Count()==8&&packed[6]==AE_CH_BC&&packed[7]==AE_CH_UNKNOWN1);
   assert(packed[2]==(lfe?AE_CH_LFE:AE_CH_UNKNOWN1));
   for(unsigned source=0;source<req.Count();++source){
     auto out=pulse(req,packed,req[source],false,0.0f,true);
     assert(out.size()==8);
     for(unsigned slot=0;slot<out.size();++slot)
       near(out[slot],packed[slot]==req[source]?0.25f:0.0f);
   }
   // Kernel maps change only when present: pre-native-map fallback above remains.
   free(selected);closeMaps();
 }
 assert(sink.GetChannelLayoutLegacy(fmt(six0),2,8).Count()==5);
 assert(sink.GetChannelLayoutLegacy(fmt(six1),2,8).Count()==6);
 auto raw=fmt(six1);raw.m_dataFormat=AE_FMT_RAW;raw.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_TRUEHD;assert(sink.GetChannelLayoutRaw(raw).Count()==8);
 raw.m_streamInfo.m_type=CAEStreamInfo::STREAM_TYPE_EAC3;assert(sink.GetChannelLayoutRaw(raw).Count()==2);
 for(auto req:{six0,six1}){
   auto out=pulse(req,req,AE_CH_BC);for(unsigned i=0;i<req.Count();++i)near(out[i],req[i]==AE_CH_BC?0.25f:0.0f);
   auto reordered=expect({AE_CH_BC,AE_CH_FR,AE_CH_FL,AE_CH_FC,AE_CH_BL,AE_CH_BR});
   if(req.HasChannel(AE_CH_LFE))reordered+=AE_CH_LFE;
   out=pulse(req,reordered,AE_CH_BC,false,0.0f,true);near(out[0],0.25f);for(unsigned i=1;i<out.size();++i)near(out[i],0.0f);
   out=pulse(req,saved[7],AE_CH_BC);near(out[4],0.25f*M_SQRT1_2);near(out[5],0.25f*M_SQRT1_2);near(out[3],0.0f);
   out=pulse(AE_CH_LAYOUT_2_0,req,AE_CH_FL,true);int bc=CAEUtil::GetAVChannelIndex(AE_CH_BC,CAEUtil::GetAVChannelLayout(req));near(out[bc],0.0f);
 }
 // Existing LFE-to-front custom matrix keeps present BC when removing LFE.
 auto custom=pulse(six1,six0,AE_CH_BC,false,0.7f,false,1);near(custom[5],0.25f);
 auto noLfe=pulse(six0,saved[7],AE_CH_BC);near(noLfe[3],0.0f);
 // Retained limitation: optional manual LFE-to-front matrix omits orphan BC.
 // Compare its silence against the default auto matrix for both missing-BC targets.
 for(auto dst:{saved[6],saved[0]}){
   auto manual=pulse(six1,dst,AE_CH_BC,false,0.7f,false,1);
   for(float sample:manual)near(sample,0.0f);
   auto automatic=pulse(six1,dst,AE_CH_BC);
   float energy=0.0f;for(float sample:automatic)energy+=std::abs(sample);assert(energy>0.0f);
 }
 std::cout<<"PCM layouts, saved choices, configuration, ALSA and host swresample assertions passed\n";
}
'''


def generate(sources):
    active=sources[ACTIVE]
    functions=[block(sources[UTIL], x) for x in (
        'CAEChannelInfo CAEUtil::GuessChLayout(', 'const char* CAEUtil::GetStdChLayoutName(',
        'uint64_t CAEUtil::GetAVChannelLayout(', 'CAEChannelInfo CAEUtil::GetAEChannelLayout(',
        'enum AVChannel CAEUtil::GetAVChannel(', 'int CAEUtil::GetAVChannelIndex(')]
    functions += [block(active,'void CActiveAE::ApplySettingsToFormat('),block(active,'bool CActiveAE::IsSettingVisible(')]
    alsa=sources[ALSA]
    arrays=alsa[alsa.index('#define ALSA_MAX_CHANNELS'):alsa.index('enum AMLDeviceType')]
    functions += [arrays]
    functions += [block(alsa,x) for x in (
        'inline CAEChannelInfo CAESinkALSA::GetChannelLayoutRaw(',
        'inline CAEChannelInfo CAESinkALSA::GetChannelLayoutLegacy(',
        'inline CAEChannelInfo CAESinkALSA::GetChannelLayout(',
        'AEChannel CAESinkALSA::ALSAChannelToAEChannel(',
        'unsigned int CAESinkALSA::AEChannelToALSAChannel(',
        'CAEChannelInfo CAESinkALSA::ALSAchmapToAEChannelMap(',
        'snd_pcm_chmap_t* CAESinkALSA::AEChannelMapToALSAchmap(',
        'snd_pcm_chmap_t* CAESinkALSA::CopyALSAchmap(',
        'unsigned int CAESinkALSA::ALSAchmapActiveCount(',
        'CAEChannelInfo CAESinkALSA::GetAlternateLayoutForm(',
        'snd_pcm_chmap_t* CAESinkALSA::SelectALSAChannelMap(')]
    functions += [block(sources[RESAMPLE],x) for x in (
        'CActiveAEResampleFFMPEG::CActiveAEResampleFFMPEG()',
        'CActiveAEResampleFFMPEG::~CActiveAEResampleFFMPEG()',
        'bool CActiveAEResampleFFMPEG::Init(')]
    settings=set(re.findall(r'CSettings::(SETTING_\w+)', '\n'.join(functions)))
    declarations=[]
    for name in sorted(settings):
        match=re.search(r'  static constexpr auto '+name+r' = .*?;',sources[SETTINGS_H])
        if not match:raise RuntimeError('Missing real settings declaration '+name)
        declarations.append(match.group(0))
    members=sources[RESAMPLE_H].split('protected:',1)[1].split('};',1)[0]
    prefix=PREFIX.replace('@SETTING_IDS@','\n'.join(declarations)).replace('@RESAMPLER_MEMBERS@',members)
    return prefix+'\n'+'\n'.join(functions)+'\n'+MAIN


def headers(candidate):
    if candidate:
        out=Path(candidate)
        if not (out/'libavutil/channel_layout.h').is_file():raise RuntimeError('FFmpeg public headers missing')
        return out
    for out in (Path('/usr/include'),Path('/usr/include/x86_64-linux-gnu')):
        if (out/'libavutil/channel_layout.h').is_file():return out
    raise RuntimeError('Pass --ffmpeg-include with compatible FFmpeg/ALSA public headers')


def mutations():
    row0='{AE_CH_FL, AE_CH_FR, AE_CH_FC, AE_CH_BL, AE_CH_BR, AE_CH_BC, AE_CH_NULL}'
    row1='{AE_CH_FL, AE_CH_FR, AE_CH_FC, AE_CH_LFE, AE_CH_BL, AE_CH_BR, AE_CH_BC,\n       AE_CH_NULL}'
    # Handle either clang-format line width without changing the mutation intent.
    return [
        ('6.0-wrong-lfe',CHANNEL,row0,row0.replace('AE_CH_BC','AE_CH_LFE')),
        ('6.0-missing-bc',CHANNEL,row0,row0.replace('AE_CH_BC','AE_CH_SR')),
        ('6.0-side-surrounds',CHANNEL,row0,row0.replace('AE_CH_BL','AE_CH_SL').replace('AE_CH_BR','AE_CH_SR')),
        ('6.1-side-surrounds',CHANNEL,row1,row1.replace('AE_CH_BL','AE_CH_SL').replace('AE_CH_BR','AE_CH_SR')),
        ('stale-empty-layout-row',CHANNEL,row1,'{AE_CH_NULL}'),
        ('shifted-layout-ordinals',DATA,'  AE_CH_LAYOUT_6_0,\n  AE_CH_LAYOUT_6_1,\n\n  AE_CH_LAYOUT_MAX','  AE_CH_LAYOUT_6_0 = 12,\n  AE_CH_LAYOUT_6_1 = 11,\n\n  AE_CH_LAYOUT_MAX = 13'),
        ('saved-5.1-shifted',ACTIVE,'case  8: stdLayout = AE_CH_LAYOUT_5_1;','case  8: stdLayout = AE_CH_LAYOUT_6_1;'),
        ('new-6.0-wrong-case',ACTIVE,'case 11: stdLayout = AE_CH_LAYOUT_6_0;','case 11: stdLayout = AE_CH_LAYOUT_5_1;'),
        ('new-6.1-wrong-case',ACTIVE,'case 12: stdLayout = AE_CH_LAYOUT_6_1;','case 12: stdLayout = AE_CH_LAYOUT_6_0;'),
        ('six-count-guess-shifted',UTIL,'case 6: result = AE_CH_LAYOUT_5_1;','case 6: result = AE_CH_LAYOUT_6_0;'),
        ('seven-count-guess-shifted',UTIL,'case 7: result = AE_CH_LAYOUT_7_0;','case 7: result = AE_CH_LAYOUT_6_1;'),
        ('stale-null-layout-name',UTIL,'"6.0", "6.1"','"6.0"'),
        ('new-layout-name-swapped',UTIL,'"6.0", "6.1"','"6.1", "6.0"'),
        ('bc-mask-is-lfe',UTIL,'if (info.HasChannel(AE_CH_BC))   channelLayout |= AV_CH_BACK_CENTER;','if (info.HasChannel(AE_CH_BC))   channelLayout |= AV_CH_LOW_FREQUENCY;'),
        ('bc-remap-index-is-lfe',UTIL,'case AE_CH_BC:\n      return AV_CHAN_BACK_CENTER;','case AE_CH_BC:\n      return AV_CHAN_LOW_FREQUENCY;'),
        ('alsa-bc-is-lfe',ALSA,'case SND_CHMAP_RC:   aeChannel = AE_CH_BC;','case SND_CHMAP_RC:   aeChannel = AE_CH_LFE;'),
        ('alsa-bc-is-unknown',ALSA,'case AE_CH_BC:    alsaChannel = SND_CHMAP_RC;','case AE_CH_BC:    alsaChannel = SND_CHMAP_UNKNOWN;'),
        ('fallback-drops-orphan-bc',CHANNEL,'if (srcHasBC && !dstHasBC)','if (false && srcHasBC && !dstHasBC)'),
        ('upmix-fills-bc',RESAMPLE,'case AV_CHAN_BACK_LEFT:\n        case AV_CHAN_SIDE_LEFT:','case AV_CHAN_BACK_LEFT:\n        case AV_CHAN_BACK_CENTER:\n        case AV_CHAN_SIDE_LEFT:'),
    ]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--logs',type=Path)
    ap.add_argument('--ffmpeg-include',type=Path)
    ap.add_argument('--negative-controls',action='store_true')
    args=ap.parse_args();root=args.root.resolve()
    log=args.logs.resolve() if args.logs else Path(tempfile.mkdtemp(prefix='pcm-layouts-'))
    log.mkdir(parents=True,exist_ok=True)
    includes=headers(args.ffmpeg_include)
    files=[CHANNEL,DATA,UTIL,ACTIVE,ALSA,RESAMPLE,RESAMPLE_H,OMNI,SETTINGS_H,
           AUDIO/'Utils/AEChannelInfo.h',AUDIO/'Interfaces/AE.h',AUDIO/'Utils/AEAudioFormat.h',AUDIO/'Utils/AEStreamInfo.h',AUDIO/'Utils/AEDeviceInfo.h',AUDIO/'Utils/AEPackIEC61937.h',Path('xbmc/cores/VideoPlayer/DVDCodecs/Audio/OmniphonyPcmStream.h')]
    sources={p:(root/p).read_text() for p in files}
    libs=[ctypes.util.find_library(x) for x in ('swresample','avutil')]
    if not all(libs):raise RuntimeError('Host libswresample/libavutil missing')
    avlib=ctypes.CDLL(libs[1])
    avlib.av_version_info.restype=ctypes.c_char_p
    host_version=avlib.av_version_info().decode()
    report={'root':str(root),'headers':str(includes),'host_libraries':libs,'host_ffmpeg_version':host_version,
            'public_header_sha256':{str(p):hashlib.sha256((includes/p).read_bytes()).hexdigest()
                                   for p in (Path('libavutil/channel_layout.h'),Path('libswresample/swresample.h'))},
            'source_sha256':{str(p):hashlib.sha256((root/p).read_bytes()).hexdigest() for p in files},
            'limits':['Host source seams, service/ALSA stand-ins; no device delivery or GUI/persistence',
                      'Host FFmpeg ABI/runtime, not CE playback or linkage; sanitizer leak check disabled',
                      'Existing BC-silent stereo upmix and orphan-BC LFE-front omission preserved'],
            'cases':[]}
    cases=[('positive',sources)]
    if args.negative_controls:
        for name,path,old,new in mutations():
            if name in ('6.1-side-surrounds','stale-empty-layout-row') and old not in sources[path]:
                old=old.replace(',\n       AE_CH_NULL}', ', AE_CH_NULL}')
                new=new.replace(',\n       AE_CH_NULL}', ', AE_CH_NULL}')
            mutated=dict(sources);mutated[path]=one(mutated[path],old,new);cases.append((name,mutated))
    for name,case in cases:
        work=log/name;work.mkdir(exist_ok=True)
        # Header/source copies isolate mutation controls from the real checkout.
        for path in (CHANNEL,DATA,OMNI,*[p for p in files if p.suffix=='.h']):
            dst=work/path;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_text(case[path])
        cpp=work/'fixture.cpp';cpp.write_text(generate(case));exe=work/'fixture'
        cmd=['c++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-misleading-indentation',
             '-Wno-deprecated-declarations','-fsanitize=address,undefined','-fno-omit-frame-pointer',
             '-I',str(work/'xbmc'),'-I',str(root/'xbmc'),'-idirafter',str(includes),str(cpp),str(work/CHANNEL),str(work/OMNI),
             *['-l:'+lib for lib in libs],'-o',str(exe)]
        compiled=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        (work/'compile.log').write_text(compiled.stdout);(work/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
        entry={'name':name,'compile_exit':compiled.returncode};report['cases'].append(entry)
        if compiled.returncode:
            (log/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            raise RuntimeError(f'{name} did not compile; see {work}/compile.log')
        env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',UBSAN_OPTIONS='halt_on_error=1')
        run=subprocess.run([str(exe)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env)
        (work/'run.log').write_text(run.stdout);entry['run_exit']=run.returncode
        if name=='positive':ok=run.returncode==0
        else:ok=run.returncode==-signal.SIGABRT and 'Assertion' in run.stdout and 'failed' in run.stdout and 'AddressSanitizer' not in run.stdout and 'runtime error:' not in run.stdout
        entry['passed']=ok
        if not ok:
            (log/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            raise RuntimeError(f'{name} failed its expected runtime result; see {work}/run.log')
        print(f'{name}: PASS')
    report['passed']=True;(log/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'production positive and {len(cases)-1} compiled mutation controls passed; {log}/report.json')


if __name__=='__main__':main()
