#!/usr/bin/env python3
"""Execute the full production scanner, metadata crop proof and AVIO callbacks.

FFmpeg/VFS endpoints supply deterministic container headers and sample frames;
the actual CAMLNativeWorker owns blocked/cancelled scanner lifetime. This proves
header-only admission and cleanup, not real container parsing, decoding or device
behavior. Header expiry is injected at the backend boundary using the production
monotonic deadline, without changing its configured ten-second budget.
"""
import argparse
import json
import hashlib
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']


def source():
    global SOURCE_SHA
    aml = (ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    SOURCE_SHA = hashlib.sha256(aml.encode()).hexdigest()
    scan = function(aml, 'static void DetectActiveAreaFromFile(')
    assert 'source->metadataDeadline.Start(std::chrono::seconds(10));' in scan
    assert scan.index('source->metadataDeadline.Start(') < scan.index('file.Open(')
    helpers = aml[aml.index('enum class DetectEdgeReason'):aml.index('static void DetectActiveAreaFromFile(')]
    callbacks = function(aml, 'struct DetectInput')+';\n'+'\n'.join(function(aml, name) for name in (
        'static bool detect_input_deadline(', 'static int detect_input_interrupt_cb(',
        'static int detect_avio_read(', 'static int64_t detect_avio_seek('))
    constants = re.search(r'static const uint32_t s_commonAR\[\] = \{.*?\};', aml, re.S).group()
    crop = function(aml, 'static bool detect_coded_crop(')
    return PREFIX+callbacks+ENDPOINTS+crop+'\n'+helpers+'\n'+constants+'\n'+scan+TEST


PREFIX = r'''
#include "utils/DetectorAdmission.h"
#include "utils/AMLNativeWorker.h"
#include <future>
#include <condition_variable>
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstring>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
constexpr int DV_DETECT_RUNNING=0,DV_DETECT_SKIPPED=1,DV_DETECT_FAILED=2,DV_DETECT_TIMED_OUT=3,DV_DETECT_SKIP_IMAX=4,DV_DETECT_SKIP_NON16X9=5,LOGINFO=0,LOGWARNING=1,LOGDEBUG=2;
constexpr int AVERROR_EXIT=-99,AVERROR_EOF=-1,AVSEEK_SIZE=0x10000,AVSEEK_FORCE=0x20000;
constexpr int AVMEDIA_TYPE_VIDEO=1,AV_DISPOSITION_ATTACHED_PIC=1,AVFMTCTX_NOHEADER=1,AVFMT_FLAG_CUSTOM_IO=1,AVDISCARD_NONKEY=1,AVDISCARD_ALL=2,AVSEEK_FLAG_BACKWARD=1;
constexpr int AV_PIX_FMT_YUV420P=0,AV_PIX_FMT_YUVJ420P=1,AV_PIX_FMT_YUV422P=2,AV_PIX_FMT_YUVJ422P=3,AV_PIX_FMT_YUV444P=4,AV_PIX_FMT_YUVJ444P=5,AV_PIX_FMT_NV12=6,AV_PIX_FMT_NV21=7,AV_PIX_FMT_YUV420P10LE=8,AV_PIX_FMT_P010LE=9;
struct CLog{template<class...T>static void Log(T...) {}};

struct DetectResult{uint16_t top=0,bottom=0,left=0,right=0;bool highConfidence=false;};
struct DetectSource{
 std::string path="fixture.mkv";int width=128,height=72;bool nativeDV=false,standalone=true;
 int streamIndex=-1,codecHint=173,scannedWidth=0,scannedHeight=0,scannedStream=0,scannedCodec=0;bool uniqueStream=false;
 std::atomic<int>completedPositions{0},scanState{DV_DETECT_RUNNING};std::atomic<bool>timedOut{false};
 std::shared_ptr<DETECTOR_ADMISSION::PlaybackPolicy>policy=std::make_shared<DETECTOR_ADMISSION::PlaybackPolicy>(false,1);
 DETECTOR_ADMISSION::PositionDeadline positionDeadline,metadataDeadline;
 std::atomic<bool>metadataTimedOut{false},preCropped{false};double scannedAspect=0.0;
 std::shared_ptr<std::atomic<bool>>superseded=std::make_shared<std::atomic<bool>>(false);
 std::shared_ptr<const DetectResult> result,subtitleResult,retained,retainedSubtitle;
};
std::shared_ptr<DetectSource>s_detectSource;std::mutex s_detectSourceMutex;
std::atomic<bool>s_detectThrottleActive=false,s_detectCacheStarved=false,s_detectStable=false;
std::atomic<int>s_detectState=DV_DETECT_RUNNING;
struct Metadata{bool has_level5_metadata=false,level5_detected=false;int level5_active_area_top_offset=0,level5_active_area_bottom_offset=0,level5_active_area_left_offset=0,level5_active_area_right_offset=0;};
struct CServiceBroker{static CServiceBroker&GetDataCacheCore(){static CServiceBroker x;return x;}Metadata GetVideoDoViFrameMetadata(){return {};}};
struct CSettings{static constexpr int SETTING_SUBTITLES_DETECTACTIVEAREA=1;};
struct Settings{bool GetBool(int){return true;}};Settings*settings(){static Settings s;return &s;}
bool aml_dv_detect_active_area_enabled(){return true;}
struct CURL{explicit CURL(const std::string&){}std::string GetFileName(){return "fixture.mkv";}};
struct URIUtils{static std::string GetFileName(const std::string&p){return p;}};
struct StringUtils{static bool StartsWithNoCase(const std::string&s,const std::string&p){return s.rfind(p,0)==0;}};
bool detect_check_interrupted(void*p,const std::function<bool()>&stop){return static_cast<CAMLNativeWorker::Run*>(p)->Cancelled()||(stop&&stop());}
bool detect_wait_for_startup_hold(const CAMLNativeWorker::Run&){return true;}
bool detect_throttle_enabled(){return false;}constexpr int kDetectCacheTargetPct=80,kDetectCacheFloorPct=50;
int detect_wait_for_cache(const CAMLNativeWorker::Run&,int,int){return 100;}
struct Rational{int num=0,den=1;};
struct Scenario{
 int seeks=0,reads=0,sends=0,receives=0,decoders=0,frames=0,findInfo=0,opens=0;
 int published=0,subtitle=0,clears=0,closes=0,formatFrees=0,avioFrees=0,decoderFrees=0,frameFrees=0;
 int headerSleepMs=0;bool cancelHeader=false,expireHeader=false,blockHeader=false,blockOpen=false,expireOpen=false;
 int headerCalls=0;
 std::promise<void>*entered=nullptr;std::shared_future<void>release;
}g;
namespace XFILE {constexpr int READ_NO_CACHE=1,IOCTRL_READ_INTERRUPT=64,IOCTRL_SET_RETRY=16;struct CFile{
 std::function<bool()>callback;bool Open(const std::string&,int){
 ++g.opens;
 if(g.blockOpen){g.entered->set_value();g.release.wait();}
 if(g.expireOpen){s_detectSource->metadataDeadline.Start(10ms);std::this_thread::sleep_for(30ms);}
 return true;
 }
 int Read(void*,int){++g.reads;return 1;}
 int64_t Seek(int64_t p,int){return p;}int64_t GetLength(){return 1000000;}
 int IoControl(int action,void*value){if(action==IOCTRL_READ_INTERRUPT){if(value)callback=*static_cast<std::function<bool()>*>(value);else{callback={};++g.clears;}}return 0;}
 void Close(){++g.closes;assert(!callback);}
};}
'''

ENDPOINTS = r'''

struct AVCodecParameters{int codec_type=AVMEDIA_TYPE_VIDEO,codec_id=173,width=3832,height=1600;Rational sample_aspect_ratio{1,1};};
struct AVStream{AVCodecParameters*codecpar=nullptr;int disposition=0;Rational sample_aspect_ratio{1,1};struct{int num=1,den=1;}time_base;};
struct AVIOContext{uint8_t*buffer;void*opaque;int(*read)(void*,uint8_t*,int);int64_t(*seek)(void*,int64_t,int);};
struct AVFormatContext{AVIOContext*pb=nullptr;int flags=0,ctx_flags=0;struct{int(*callback)(void*)=nullptr;void*opaque=nullptr;}interrupt_callback;struct Format{const char*name="matroska";}*iformat;AVStream**streams=nullptr;unsigned nb_streams=1;int64_t duration=1000000;};
struct AVCodec{};struct AVCodecContext{int width=128,height=72,codec_id=173,thread_count=0,skip_frame=0,skip_loop_filter=0;Rational sample_aspect_ratio;};
struct AVPacket{int stream_index=0;};struct AVFrame{int width=128,height=72,format=AV_PIX_FMT_YUV420P,linesize[8]{128};uint8_t*data[8]{};int64_t best_effort_timestamp=0;};
int headerFlags=0;AVFormatContext* active=nullptr;std::vector<uint8_t>image;
std::vector<AVCodecParameters>parameters;std::vector<AVStream>streams;std::vector<AVStream*>streamPointers;int decodedWidth=128,decodedHeight=72;
void*av_malloc(size_t n){return std::malloc(n);}void av_free(void*p){std::free(p);}void av_freep(uint8_t**p){std::free(*p);*p=nullptr;}
AVIOContext*avio_alloc_context(uint8_t*b,int,int,void*opaque,int(*read)(void*,uint8_t*,int),void*,int64_t(*seek)(void*,int64_t,int)){return new AVIOContext{b,opaque,read,seek};}
void avio_context_free(AVIOContext**p){++g.avioFrees;delete*p;*p=nullptr;}
AVFormatContext*avformat_alloc_context(){return active=new AVFormatContext{};}
int avformat_open_input(AVFormatContext**p,const char*,void*,void*){
 ++g.headerCalls;(*p)->ctx_flags=headerFlags;static AVFormatContext::Format fmt;(*p)->streams=streamPointers.data();(*p)->nb_streams=streamPointers.size();(*p)->iformat=&fmt;
 if(g.entered&&!g.blockOpen)g.entered->set_value();
 if(g.blockHeader)g.release.wait();
 if(g.expireHeader)s_detectSource->metadataDeadline.Start(0ms);
 if(g.headerSleepMs)std::this_thread::sleep_for(std::chrono::milliseconds(g.headerSleepMs));
 if(g.cancelHeader)s_detectSource->superseded->store(true);
 return 0;
}
int avformat_find_stream_info(AVFormatContext*,void*){++g.findInfo;return 0;}
const AVCodec*avcodec_find_decoder(int id){static AVCodec codec;return id>0?&codec:nullptr;}
AVCodecContext*avcodec_alloc_context3(const AVCodec*){++g.decoders;return new AVCodecContext;}
void avcodec_parameters_to_context(AVCodecContext*c,AVCodecParameters*p){c->width=p->width;c->height=p->height;c->codec_id=p->codec_id;c->sample_aspect_ratio=p->sample_aspect_ratio;decodedWidth=c->width;decodedHeight=c->height;}
int avcodec_open2(AVCodecContext*,const AVCodec*,void*){return 0;}
AVFrame*av_frame_alloc(){++g.frames;return new AVFrame;}
void av_init_packet(AVPacket*){}void av_packet_unref(AVPacket*){}void avcodec_flush_buffers(AVCodecContext*){}
int av_seek_frame(AVFormatContext*fmt,int,int64_t p,int){++g.seeks;return static_cast<int>(fmt->pb->seek(fmt->pb->opaque,p,0));}
int av_read_frame(AVFormatContext*fmt,AVPacket*p){uint8_t b=0;p->stream_index=s_detectSource->streamIndex<0?0:s_detectSource->streamIndex;int r=fmt->pb->read(fmt->pb->opaque,&b,1);return r<0?r:0;}
int avcodec_send_packet(AVCodecContext*,AVPacket*){++g.sends;return 0;}
int avcodec_receive_frame(AVCodecContext*,AVFrame*frame){
 ++g.receives;frame->width=decodedWidth;frame->height=decodedHeight;frame->linesize[0]=decodedWidth;
 image.assign(size_t(decodedWidth)*decodedHeight,uint8_t(16+g.seeks));
 for(int y=decodedHeight/8;y<decodedHeight*7/8;++y)for(int x=0;x<decodedWidth;++x)image[size_t(y)*decodedWidth+x]=uint8_t(100+g.seeks);
 frame->data[0]=image.data();frame->best_effort_timestamp=g.seeks;return 0;
}
void av_frame_free(AVFrame**p){++g.frameFrees;delete*p;*p=nullptr;}void avcodec_free_context(AVCodecContext**p){++g.decoderFrees;delete*p;*p=nullptr;}void avformat_close_input(AVFormatContext**p){++g.formatFrees;delete*p;*p=nullptr;active=nullptr;}
void detect_publish(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>&source,uint16_t t,uint16_t b,uint16_t l,uint16_t r,bool high){++g.published;source->result=std::make_shared<DetectResult>(DetectResult{t,b,l,r,high});}
void detect_publish_subtitle(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>&source,const DetectResult&result){++g.subtitle;source->subtitleResult=std::make_shared<DetectResult>(result);}
'''

TEST = r'''
void prepare(int count=1){
 headerFlags=0;g=Scenario{};parameters.assign(count,AVCodecParameters{});streams.assign(count,AVStream{});streamPointers.clear();
 for(int i=0;i<count;++i){streams[i].codecpar=&parameters[i];streamPointers.push_back(&streams[i]);}
 s_detectSource=std::make_shared<DetectSource>();s_detectSource->width=s_detectSource->height=0;s_detectSource->codecHint=0;
 s_detectState=DV_DETECT_RUNNING;s_detectCacheStarved=false;s_detectThrottleActive=false;
}
void scan(){CAMLNativeWorker::Run run(s_detectSource->superseded);DetectActiveAreaFromFile(s_detectSource,run);}
void clean(bool allocated=true){
 assert(g.opens==1&&g.clears==1&&g.closes==1&&g.formatFrees==int(allocated)&&g.avioFrees==int(allocated)&&!active);
 assert(g.decoderFrees==g.decoders&&g.frameFrees==g.frames);
 assert(!s_detectSource->metadataDeadline.Expired()&&!s_detectSource->positionDeadline.Expired());
}
void cropped(int width=3832,int height=1600,int index=0,bool unique=true){
 scan();assert(s_detectSource->preCropped&&s_detectSource->scanState==DV_DETECT_SKIP_NON16X9);
 assert(s_detectSource->scannedWidth==width&&s_detectSource->scannedHeight==height);
 assert(s_detectSource->scannedStream==index&&s_detectSource->scannedCodec==173);
 assert(s_detectSource->uniqueStream==unique&&s_detectSource->completedPositions==0);
 assert(std::abs(s_detectSource->scannedAspect-static_cast<double>(width)/height)<1e-10);
 assert(!s_detectSource->result&&!s_detectSource->subtitleResult);
 assert(g.findInfo==0&&g.decoders==0&&g.frames==0&&g.seeks==0&&g.reads==0&&g.receives==0);
 assert(g.published==0&&g.subtitle==0&&!s_detectSource->timedOut);clean();
}
void sampled(){
 scan();assert(!s_detectSource->preCropped&&!s_detectSource->timedOut);
 assert(g.findInfo==1&&g.decoders==1&&g.frames==1&&g.receives>=14);
 assert(g.seeks>=14&&s_detectSource->completedPositions==14);clean();
}
int main(){
 // The reported encode: no authored RPU metadata enters this isolated source.
 // Prove actual early exit precedes decoder creation/find-info/sample AVIO work.
 prepare();cropped();
 // A dynamic stream table cannot establish unambiguous default selection or
 // a unique-stream cache record, even when the current header has one video.
 prepare();headerFlags=AVFMTCTX_NOHEADER;scan();assert(!s_detectSource->preCropped);
 assert(s_detectSource->scanState==DV_DETECT_SKIPPED&&g.decoders==0);clean();
 prepare();headerFlags=AVFMTCTX_NOHEADER;s_detectSource->streamIndex=0;cropped(3832,1600,0,false);
 // Explicit square SAR from either established container field is sufficient.
 prepare();streams[0].sample_aspect_ratio={0,1};cropped();
 prepare();parameters[0].sample_aspect_ratio={0,1};cropped();
 prepare();streams[0].sample_aspect_ratio={2,2};parameters[0].sample_aspect_ratio={2,2};cropped();
 // Unknown, anamorphic and contradictory metadata must run the real scanner.
 prepare();streams[0].sample_aspect_ratio={0,1};parameters[0].sample_aspect_ratio={0,1};sampled();
 prepare();streams[0].sample_aspect_ratio={2,1};parameters[0].sample_aspect_ratio={2,1};sampled();
 prepare();streams[0].sample_aspect_ratio={1,1};parameters[0].sample_aspect_ratio={2,1};sampled();
 prepare();streams[0].sample_aspect_ratio={2,1};parameters[0].sample_aspect_ratio={1,1};sampled();
 // Full coded canvas is not proof of pre-cropping.
 prepare();parameters[0].width=1920;parameters[0].height=1080;sampled();
 // Invalid geometry is rejected by the actual metadata proof helper.
 prepare();parameters[0].width=0;AVFormatContext context{};context.streams=streamPointers.data();context.nb_streams=1;
 assert(!detect_metadata_crop(s_detectSource,&context)&&!s_detectSource->preCropped);
 parameters[0].width=65536;assert(!detect_metadata_crop(s_detectSource,&context));
 parameters[0].width=3832;parameters[0].height=0;assert(!detect_metadata_crop(s_detectSource,&context));
 prepare();parameters[0].codec_id=0;context.streams=streamPointers.data();assert(!detect_metadata_crop(s_detectSource,&context));
 // Attached artwork is not a competing movie stream. Absolute selected indexes
 // still include attachments in their numbering.
 prepare(2);streams[0].disposition=AV_DISPOSITION_ATTACHED_PIC;cropped(3832,1600,1,true);
 prepare(2);streams[0].disposition=AV_DISPOSITION_ATTACHED_PIC;s_detectSource->streamIndex=0;
 scan();assert(!s_detectSource->preCropped&&g.findInfo==1&&g.decoders==0&&g.seeks==0);clean();
 // Unselected multi-video remains ambiguous; explicit selection returns only
 // that stream's dimensions and cannot seed unique-stream cache discovery.
 prepare(2);scan();assert(!s_detectSource->preCropped&&s_detectSource->scanState==DV_DETECT_SKIPPED);
 assert(g.findInfo==1&&g.decoders==0&&g.frames==0&&g.seeks==0);clean();
 prepare(2);parameters[1].width=3840;parameters[1].height=1606;s_detectSource->streamIndex=1;
 cropped(3840,1606,1,false);
 prepare(2);s_detectSource->streamIndex=4;scan();assert(!s_detectSource->preCropped&&g.decoders==0&&g.seeks==0);clean();
 // Cancellation is checked after the synchronous header backend returns, before
 // geometry can become a successful no-scan result.
 prepare();g.cancelHeader=true;scan();assert(s_detectSource->superseded->load());
 assert(!s_detectSource->preCropped&&!s_detectSource->result&&g.decoders==0&&g.findInfo==0&&g.seeks==0);clean();
 // Exercise the production deadline and post-call gate with a real elapsed
 // backend delay. Expiry is injected after the unchanged ten-second timer was
 // armed, avoiding ten seconds per negative control. No hard VFS preemption is claimed.
 prepare();g.expireHeader=true;g.headerSleepMs=25;const auto before=std::chrono::steady_clock::now();scan();
 assert(std::chrono::steady_clock::now()-before>=25ms);
 assert(s_detectSource->metadataTimedOut&&s_detectSource->timedOut&&s_detectSource->scanState==DV_DETECT_TIMED_OUT);
 assert(!s_detectSource->preCropped&&!s_detectSource->result&&!s_detectSource->subtitleResult);
 assert(g.decoders==0&&g.findInfo==0&&g.frames==0&&g.seeks==0);clean();
 // Expiry inside a synchronous VFS Open must be observed immediately after it
 // returns, before creating another backend operation or publishing metadata.
 prepare();g.expireOpen=true;const auto opened=std::chrono::steady_clock::now();scan();
 assert(std::chrono::steady_clock::now()-opened>=30ms);
 assert(s_detectSource->metadataTimedOut&&s_detectSource->timedOut&&s_detectSource->scanState==DV_DETECT_TIMED_OUT);
 assert(!s_detectSource->preCropped&&g.headerCalls==0&&g.findInfo==0&&g.decoders==0&&g.seeks==0);clean(false);
 // Real worker retirement retains blocked backend ownership. No replacement or
 // cleanup acknowledgement is possible until the original backend returns.
 for(bool open:{false,true}){
 prepare();g.blockHeader=!open;g.blockOpen=open;std::promise<void>entered,release;g.entered=&entered;g.release=release.get_future().share();
 CAMLNativeWorker worker;auto source=s_detectSource;
 assert(worker.Start([source](const CAMLNativeWorker::Run&run){DetectActiveAreaFromFile(source,run);},source->superseded));
 assert(entered.get_future().wait_for(2s)==std::future_status::ready);
 const auto retired=std::chrono::steady_clock::now();assert(!worker.Retire());
 assert(std::chrono::steady_clock::now()-retired<100ms);
 assert(g.closes==0&&g.formatFrees==0&&g.avioFrees==0);
 release.set_value();const auto limit=std::chrono::steady_clock::now()+2s;
 while(!worker.Retire()){assert(std::chrono::steady_clock::now()<limit);std::this_thread::sleep_for(1ms);}
 assert(worker.Closed()&&!worker.Failure()&&!worker.Start([](const auto&){}));
 assert(!source->preCropped&&g.decoders==0&&g.frames==0&&g.seeks==0);clean(!open);
 }
 std::cout<<"PASS: actual metadata proof + full scanner/AVIO + native worker: square-SAR no-scan, conservative fallthrough, selected streams/artwork, cancellation/deadline and owned blocked cleanup\n";
}
'''


def compile_run(code, out, name, negative=False):
    cpp=out/(name+'.cpp');exe=out/name;cpp.write_text(code)
    argv=[os.environ.get('CXX','g++'),'-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
          '-Wno-unused-function','-Wno-unused-variable','-Wno-missing-field-initializers',
          '-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',
          '-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(exe)]
    built=subprocess.run(argv,capture_output=True,text=True)
    (out/(name+'-compile.log')).write_text(built.stdout+built.stderr)
    assert built.returncode==0,built.stderr
    result=subprocess.run([str(exe)],capture_output=True,text=True,timeout=45,
                          env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
    (out/(name+'-result.log')).write_text(result.stdout+result.stderr)
    if negative:
        assert result.returncode!=0 and 'Assertion' in result.stderr, result.stdout+result.stderr
        print('REJECTED:',name)
    else:
        assert result.returncode==0,result.stdout+result.stderr
        print(result.stdout.strip())
    return {'case':name,'negative':negative,'exit':result.returncode,'compiler_argv':argv,'production_sha256':SOURCE_SHA,
            'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='detector-metadata-') as temporary:
        out=args.output or Path(temporary);out.mkdir(parents=True,exist_ok=True)
        code=source();receipts=[compile_run(code,out,'production')]
        if args.negative_controls:
            controls=[
                ('no-early-crop-bypass','if (detect_metadata_crop(source, fmtCtx))','if (false)'),
                ('dynamic-stream-table-as-unique', '(source->streamIndex < 0 && (context->ctx_flags & AVFMTCTX_NOHEADER))', 'false'),
                ('ignore-SAR-proof',
                 'sar.num <= 0 || sar.den <= 0 || sar.num != sar.den ||\n      (parameterSAR.num > 0 && parameterSAR.den > 0 && parameterSAR.num != parameterSAR.den) ||',
                 '((void)sar, (void)parameterSAR, false) ||'),
                ('artwork-as-video',
                 'if (stream->codecpar->codec_type != AVMEDIA_TYPE_VIDEO ||\n        (stream->disposition & AV_DISPOSITION_ATTACHED_PIC))',
                 'if (stream->codecpar->codec_type != AVMEDIA_TYPE_VIDEO)'),
                ('ambiguous-multi-video-first-wins','if (selected >= 0)\n      return false;',
                 'if (selected >= 0)\n      break;'),
                ('ignore-post-header-cancellation',
                 'avformat_open_input(&fmtCtx, probeName.c_str(), nullptr, nullptr) < 0 ||\n      detect_input_interrupt_cb(&input)',
                 'avformat_open_input(&fmtCtx, probeName.c_str(), nullptr, nullptr) < 0'),
                ('ignore-header-deadline','if (input.source->metadataDeadline.Expired())','if (false)'),
            ]
            for name,old,new in controls:
                assert old in code,name
                receipts.append(compile_run(code.replace(old,new,1),out,name,negative=True))
        (out/'verification.json').write_text(json.dumps(receipts,indent=2))


if __name__=='__main__':
    main()
