#!/usr/bin/env python3
"""Execute production detector sample admission, luma/edge calculations, consensus
and exact-PTS metadata lookup. FFmpeg/VFS/threads are tested separately; this is
synthetic host sample validation, not software decoding or device acceptance.
"""
from pathlib import Path
import argparse
import os
import re
import signal
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def source(aml=None):
    if aml is None:
        aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    scan = function(aml, 'static void DetectActiveAreaFromFile(')
    admit = scan[scan.index('        if (frame->width != codedWidth'):scan.index('        lastWidth = frame->width;')]
    access_start = scan.index('        const int stride = frame->linesize[0];')
    access_end = scan.index('        CLog::Log(LOGDEBUG, "DetectActiveArea: sample at ', access_start)
    access = scan[access_start:access_end]
    edges_start = scan.index('      /* Re-derive frame accessors')
    edges_end = scan.index('      samples_top[validSamples] = sTop;', edges_start)
    edges = scan[edges_start:edges_end]
    if 'spatial[validSamples] = ' in edges:
        start = edges.index('      spatial[validSamples] = ')
        end = edges.index('      sTop = measurements[0].value;', start)
        edges = edges[:start] + edges[end:]
    code = PREFIX
    code += 'bool AdmitFrame(Frame* frame,Source* source){int codedWidth=source->width,codedHeight=source->height;auto& state=s_detectState;\n' + admit + 'return true;cleanup:return false;}\n'
    code += 'uint32_t Contrast(Frame* frame){auto& state=s_detectState;int lastWidth=frame->width,lastHeight=frame->height;{\n'
    code += access + '(void)sampleW;(void)sampleStartX;return contrast;}cleanup:return 0;}\n'
    typed = 'struct DetectEdgeMeasurement' in aml
    if typed:
        code += aml[aml.index('enum class DetectEdgeReason'):aml.index('enum class DetectAxisConfidence')]
        code += 'template<typename Luma>\n' + function(aml, 'static DetectEdgeMeasurement detect_refine_edge(') + '\n'
        code += '#define TYPED_EDGE_EVIDENCE 1\nstd::vector<DetectEdgeMeasurement> edgeMeasurements;\n'
        if 'static DetectEdgeMeasurement detect_subtitle_edge(' in aml:
            code += 'template<typename Luma>\n' + function(aml, 'static DetectEdgeMeasurement detect_subtitle_edge(') + '\n'
            code += '#define SUBTITLE_EDGE_ESTIMATE 1\nstd::vector<DetectEdgeMeasurement> subtitleEdgeMeasurements;\n'
    elif 'static uint16_t detect_refine_edge(' in aml:
        code += 'template<typename Luma>\n' + function(aml, 'static uint16_t detect_refine_edge(') + '\n'
    code += 'Result Edges(Frame* frame){int lastWidth=frame->width,lastHeight=frame->height;\n'
    if typed:
        code += '(void)detect_edge_reason;(void)detect_edge_lanes;\n'
    code += edges
    if typed:
        code += 'edgeMeasurements.assign(std::begin(measurements),std::end(measurements));\n'
        if 'static DetectEdgeMeasurement detect_subtitle_edge(' in aml:
            code += 'subtitleEdgeMeasurements.assign(std::begin(subtitleMeasurements),std::end(subtitleMeasurements));\n'
    code += 'return {sTop,sBottom,sLeft,sRight' + (',rejectedSpatial' if typed else '') + '};}\n'
    code += 'constexpr uint16_t Unobserved=' + ('UINT16_MAX' if typed else '0') + ';\n'
    code += re.search(r'enum class DetectAxisConfidence\s*\{.*?\};', aml, re.S).group() + '\n'
    if typed:
        code += function(aml, 'struct DetectPairEvidence') + ';\n'
        code += function(aml, 'static DetectPairEvidence detect_pair_evidence(') + '\n'
    code += function(aml, 'static DetectAxisConfidence detect_axis_consensus(') + '\n'
    code += function(aml, 'static bool detect_samples_consensus(') + '\n'
    if 'static bool detect_subtitle_consensus(' in aml:
        code += function(aml, 'static bool detect_subtitle_consensus(') + '\n'
        code += 'Result SubtitleEdges(Frame* frame){Edges(frame);auto& m=subtitleEdgeMeasurements;return {m[0].value,m[1].value,m[2].value,m[3].value,std::any_of(m.begin(),m.end(),[](const auto& e){return e.RejectedSpatialEvidence();})};}\n'
    if 'struct DetectSpatialSample' in aml:
        code += function(aml, 'struct DetectSpatialSample') + ';\n'
        code += 'template<class Luma>\n' + function(aml, 'static DetectSpatialSample detect_spatial_sample(') + '\n'
        code += function(aml, 'static bool detect_tiered_consensus(') + '\n'
        if 'static bool detect_strict_positive_axis(' in aml:
            code += function(aml, 'static bool detect_strict_positive_axis(') + '\n'
            code += '#define STRICT_POSITIVE_AXIS 1\n'
        code += '#define TIERED_SPATIAL_EVIDENCE 1\n'
    code += re.search(r'static const uint32_t s_commonAR\[\] = \{.*?\};', aml, re.S).group() + '\n'
    final_start = scan.index('    /* Require enough usable samples')
    publication = scan.index('\n  if (source->nativeDV &&', final_start)
    # End at the enclosing scan scope before native publication, independent of comments.
    final = scan[final_start:scan.rindex('\n  }', final_start, publication)]
    code += 'bool Select(const uint16_t* samples_top,const uint16_t* samples_bottom,const uint16_t* samples_left,const uint16_t* samples_right,int validSamples,int lastWidth,int lastHeight,DetectResult& result,const bool* rejected=nullptr){\n'
    code += 'auto& state=s_detectState;bool known[7]{};const bool* samples_rejected=rejected?rejected:known;(void)samples_rejected;\n'
    if 'static bool detect_subtitle_consensus(' in aml:
        code += '(void)detect_subtitle_consensus;\n'
    code += 'constexpr int numSeeks=7;uint16_t detTop=0,detBottom=0,detLeft=0,detRight=0;{\n'
    if 'struct DetectSpatialSample' in aml:
        code += '(void)detect_tiered_consensus;constexpr bool spatialAccepted=false;constexpr bool spatialInvariant=false,authoredPositive=false;bool nativeHighConfidence=false;bool vertical_rejected[14]{},horizontal_rejected[14]{};DetectResult subtitleConsensus{};\n'
        if 'static bool detect_strict_positive_axis(' in aml:
            code += '(void)detect_strict_positive_axis;constexpr bool positiveAxisOnly=false;DetectResult positiveAxes{};\n'
    code += final + '\n(void)nativeHighConfidence;}result={detTop,detBottom,detLeft,detRight};return true;cleanup:return false;}\n'
    cache = (ROOT / 'xbmc/cores/DataCacheCore.cpp').read_text()
    code += function(cache, 'DOVIFrameMetadata CDataCacheCore::GetVideoDoViFrameMetadata(double pts)') + '\n'
    code += function(cache, 'void CDataCacheCore::ClearVideoDoViFrameMetadata()')
    # Retain production throttle/coverage/format/VFS and injection wiring.
    assert 'const bool throttle = !source->standalone && (!source->nativeDV || detect_throttle_enabled());' in scan
    assert 'fmtCtx->flags |= AVFMT_FLAG_CUSTOM_IO;' in scan
    assert 'fmtCtx->interrupt_callback.opaque = &input;' in scan
    assert 'av_freep(&avioCtx->buffer);' in scan and 'av_free(avioBuf);' in scan
    assert 'detect_samples_consensus(samples_top, samples_bottom, samples_left, samples_right,' in scan
    assert 'validSamples < minUsable' in scan and 'contrast < minContrast' in scan
    return code + TESTS


PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <mutex>
#include <string>
#include <vector>
#include "utils/AgedMap.h"
using CCriticalSection=std::recursive_mutex;
constexpr int AV_PIX_FMT_YUV420P=0,AV_PIX_FMT_YUVJ420P=1,AV_PIX_FMT_YUV422P=2,
 AV_PIX_FMT_YUVJ422P=3,AV_PIX_FMT_YUV444P=4,AV_PIX_FMT_YUVJ444P=5,
 AV_PIX_FMT_NV12=6,AV_PIX_FMT_NV21=7,AV_PIX_FMT_YUV420P10LE=8,AV_PIX_FMT_P010LE=9,
 AV_PIX_FMT_YUV420P10BE=10,AV_PIX_FMT_RGB24=11;
constexpr int DV_DETECT_SKIPPED=4,DV_DETECT_SKIP_IMAX=5,LOGINFO=0;
struct CLog{template<class... T>static void Log(T...) {}};
std::atomic<int> s_detectState{0};
struct Frame {int width=128,height=72,format=0;int linesize[1]{128};uint8_t* data[1]{};};
struct Source {int width=128,height=72;};
struct DetectResult {uint16_t top,bottom,left,right;};
struct Result {uint16_t top,bottom,left,right;bool rejected{false};};
struct DOVIFrameMetadata {double pts=0;bool has_level5_metadata=false;int top=0;};
struct CDataCacheCore {
 CCriticalSection m_videoPlayerSection;
 struct Info {AgedMap<uint64_t,DOVIFrameMetadata> doviFrameMetadataMap;} m_playerVideoInfo;
 DOVIFrameMetadata GetVideoDoViFrameMetadata(double);void ClearVideoDoViFrameMetadata();
};
'''

TESTS = r'''
int main(){
 Source source;Frame frame;std::vector<uint8_t> image(128*72,16);frame.data[0]=image.data();
 for(int y=8;y<64;++y)for(int x=0;x<128;++x)image[y*128+x]=120;
 assert(AdmitFrame(&frame,&source));assert(Contrast(&frame)>10);
 auto edges=Edges(&frame);assert(edges.top==8&&edges.bottom==8&&edges.left==Unobserved&&edges.right==Unobserved);
 uint16_t top[7]={8,8,8,8,8,8,8},bottom[7]={8,8,8,8,8,8,8},left[7]={},right[7]={};
 DetectResult result{};
 assert(detect_samples_consensus(top,bottom,left,right,7,result)&&result.top==8&&result.bottom==8);
 assert(detect_samples_consensus(top,bottom,left,right,6,result));
 assert(!detect_samples_consensus(top,bottom,left,right,5,result));
 assert(!detect_samples_consensus(top,bottom,left,right,0,result));
 assert(!detect_samples_consensus(top,bottom,left,right,15,result));
#ifdef TIERED_SPATIAL_EVIDENCE
 auto y=[&](int row,int col){return static_cast<uint32_t>(image[row*128+col]);};
 auto spatial=detect_spatial_sample(y,128,72);
 assert(spatial.edge[0]==8&&spatial.edge[1]==8&&spatial.edge[2]==0&&spatial.edge[3]==0&&spatial.strong);
 DetectSpatialSample scouts[14];
 for(auto& sample:scouts)sample=spatial;
 bool invariant=false;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&invariant&&result.top==8&&result.bottom==8);
#ifdef STRICT_POSITIVE_AXIS
 uint16_t axisFirst[14],axisSecond[14],axisBorder=0;bool axisRejected[14]{};
 std::fill_n(axisFirst,14,8);std::fill_n(axisSecond,14,8);
 // A confirmed letterbox may crop vertically while an unrelated dark object
 // leaves the horizontal axis unresolved. No horizontal crop is inferred.
 for(auto& sample:scouts){sample.edge[2]=UINT16_MAX;sample.edge[3]=400;}
 assert(detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder)&&axisBorder==8);
 axisRejected[3]=true;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 axisRejected[3]=false;scouts[3].edge[0]=scouts[3].edge[1]=0;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 scouts[3].edge[0]=UINT16_MAX;scouts[3].edge[1]=8;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 scouts[3].edge[0]=8;axisFirst[3]=0;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 axisFirst[3]=UINT16_MAX;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 axisFirst[3]=8;scouts[3].edge[0]=scouts[3].edge[1]=20;
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
 for(auto& sample:scouts)sample=spatial;
#endif
 // Proposed-prefix guards distinguish dark picture beyond the bar from
 // a caption inside it. The same strict implementation validates both cases.
 std::vector<uint8_t> guardedPicture(128*72,16);
 for(int row=8;row<25;++row)for(int col=0;col<128;++col)guardedPicture[row*128+col]=120;
 auto guardedY=[&](int row,int col){return static_cast<uint32_t>(guardedPicture[row*128+col]);};
 auto withinBar=detect_refine_edge(guardedY,128,72,true,false,8,true);
 auto intoPicture=detect_refine_edge(guardedY,128,72,true,false,32,true);
 assert(withinBar.value==8&&!withinBar.RejectedSpatialEvidence());
 assert(intoPicture.value==UINT16_MAX&&intoPicture.RejectedSpatialEvidence());
 for(int row=2;row<4;++row)for(int col=0;col<128;++col)guardedPicture[row*128+col]=100;
 auto captionInBar=detect_refine_edge(guardedY,128,72,true,false,8,true);
 assert(captionInBar.value==UINT16_MAX&&captionInBar.RejectedSpatialEvidence());
 // One strongly supported full-frame scene blocks native cropping, while text
 // keeps the common interior envelope. A rare strong mode is also retained.
 scouts[13]=DetectSpatialSample{};std::fill_n(scouts[13].edge,4,0);scouts[13].strong=true;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==8);
 for(int i=0;i<13;++i)scouts[i]=scouts[13];scouts[13]=spatial;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==0);
 // Intermittent small raster darkness cannot override known full-frame edges;
 // a strongly supported larger rare framing mode still protects text.
 scouts[13].edge[0]=scouts[13].edge[1]=20;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==20);
 // A single weak dark-object hypothesis cannot add an inset to full-frame text.
 scouts[13].strong=false;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==0);
 scouts[12].edge[0]=UINT16_MAX;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==0);
#endif
#ifdef TIERED_SPATIAL_EVIDENCE
 // A rare asymmetric window has four proven black prefixes and sustained
 // picture within its orthogonal bounds. It protects text without native AR.
 std::vector<uint8_t> windowPixels(128*72,16);
 for(int row=12;row<56;++row)for(int col=20;col<105;++col)windowPixels[row*128+col]=100;
 auto windowY=[&](int row,int col){return static_cast<uint32_t>(windowPixels[row*128+col]);};
 auto boxed=detect_spatial_sample(windowY,128,72);
 assert(boxed.window&&boxed.edge[0]==12&&boxed.edge[1]==16&&boxed.edge[2]==20&&boxed.edge[3]==23);
 for(auto& scout:scouts){scout=DetectSpatialSample{};std::fill_n(scout.edge,4,0);scout.strong=true;}
 scouts[13]=boxed;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==12&&result.bottom==16&&result.left==20&&result.right==23);
 // Caption/picture in a proposed prefix contradicts the rectangular proof.
 for(int col=20;col<105;++col)windowPixels[4*128+col]=100;
 assert(!detect_spatial_sample(windowY,128,72).window);
 // Uniform raised grey mattes protect text but cannot establish native L5.
 std::vector<uint8_t> greyPixels(128*72,29);
 for(int row=12;row<60;++row)for(int col=0;col<128;++col)greyPixels[row*128+col]=100;
 auto greyY=[&](int row,int col){return static_cast<uint32_t>(greyPixels[row*128+col]);};
 auto grey=detect_spatial_sample(greyY,128,72);
 assert(grey.raised&&grey.edge[0]==12&&grey.edge[1]==12);
 for(auto& scout:scouts)scout=grey;
 assert(detect_tiered_consensus(scouts,14,result,invariant)&&!invariant&&result.top==12);
#ifdef STRICT_POSITIVE_AXIS
 std::fill_n(axisFirst,14,12);std::fill_n(axisSecond,14,12);
 assert(!detect_strict_positive_axis(axisFirst,axisSecond,axisRejected,scouts,14,0,axisBorder));
#endif
 // Patterned/unequal grey picture edges and a caption in the prefix veto it.
 greyPixels[12]=40;
 assert(!detect_spatial_sample(greyY,128,72).raised);
 greyPixels[12]=29;
 for(int row=60;row<72;++row)for(int col=0;col<128;++col)greyPixels[row*128+col]=35;
 assert(!detect_spatial_sample(greyY,128,72).raised);
 for(int row=60;row<72;++row)for(int col=0;col<128;++col)greyPixels[row*128+col]=29;
 for(int col=0;col<128;++col)greyPixels[5*128+col]=100;
 assert(!detect_spatial_sample(greyY,128,72).raised);
 std::fill(greyPixels.begin(),greyPixels.end(),29);
 assert(!detect_spatial_sample(greyY,128,72).raised);
 std::fill(greyPixels.begin(),greyPixels.end(),60);
 for(int row=12;row<60;++row)for(int col=0;col<128;++col)greyPixels[row*128+col]=100;
 assert(!detect_spatial_sample(greyY,128,72).raised);
 // Bright framing and a partial window cannot supply four black edges.
 for(int col=0;col<128;++col)windowPixels[col]=100;
 assert(!detect_spatial_sample(windowY,128,72).window);
#endif
 // Repeated completed reporter scan: paired vertical4/7; noisy horizontal has no veto.
 uint16_t rt[]={262,846,262,471,262,262,262},rb[]={527,262,262,262,262,262,262};
 uint16_t rl[]={5,714,0,499,0,896,128},rr[]={0,1041,532,0,523,941,0};
 assert(detect_samples_consensus(rt,rb,rl,rr,7,result));
 assert(result.top==262&&result.bottom==262&&result.left==0&&result.right==0);
 assert(Select(rt,rb,rl,rr,7,3840,2160,result));
 assert(result.top==263&&result.bottom==263&&result.left==0&&result.right==0);
 // Paired consensus and snap must be invariant under scheduling order.
 int order[]={0,1,2,3,4,5,6};
 do {
   uint16_t a[7],b[7],c[7],d[7];
   for(int i=0;i<7;++i){a[i]=rt[order[i]];b[i]=rb[order[i]];c[i]=rl[order[i]];d[i]=rr[order[i]];}
   assert(Select(a,b,c,d,7,3840,2160,result)&&result.top==263&&result.bottom==263&&result.left==0&&result.right==0);
   for(int i=0;i<7;++i)assert(a[i]==rt[order[i]]&&b[i]==rb[order[i]]&&c[i]==rl[order[i]]&&d[i]==rr[order[i]]);
 } while(std::next_permutation(order,order+7));
 // Same supported framing at1080p uses unchanged common-AR snapping.
 uint16_t ht[7],hb[7],hl[7],hr[7];
 for(int i=0;i<7;++i){ht[i]=rt[i]/2;hb[i]=rb[i]/2;hl[i]=rl[i]/2;hr[i]=rr[i]/2;}
 assert(Select(ht,hb,hl,hr,7,1920,1080,result)&&result.top==131&&result.bottom==131&&result.left==0&&result.right==0);
 // First6 of this same scan has paired3/6: strict majority is4, not3.
 assert(!detect_samples_consensus(rt,rb,rl,rr,6,result));
 assert(!Select(rt,rb,rl,rr,6,3840,2160,result));
 assert(!Select(rt,rb,rl,rr,5,3840,2160,result));
 // Separate independent majorities do not substitute for paired evidence.
 uint16_t disjointTop[]={262,262,262,262,262,500,500};
 uint16_t disjointBottom[]={262,262,500,500,500,262,262};
 uint16_t noisy[]={0,100,200,300,400,500,600};
 assert(!detect_samples_consensus(disjointTop,disjointBottom,noisy,noisy,7,result));
 // Ignore unilateral larger estimates on either axis, preserving symmetric counterpart.
 top[5]=30;bottom[6]=30;
 assert(detect_samples_consensus(top,bottom,left,right,7,result)&&result.top==8);
 top[5]=8;bottom[6]=8;
 // Five-pixel tolerance accepts symmetric near agreement, without sorting pairs.
 top[1]=10;bottom[2]=10;top[3]=bottom[3]=9;
 assert(detect_samples_consensus(top,bottom,left,right,7,result)&&result.top==8);
 top[1]=bottom[2]=top[3]=bottom[3]=8;
 // Smaller nonzero boundary or either zero endpoint defeats a stronger positive majority.
 top[6]=2;assert(!detect_samples_consensus(top,bottom,left,right,7,result));top[6]=8;
 bottom[6]=0;assert(!detect_samples_consensus(top,bottom,left,right,7,result));bottom[6]=8;
 // Pillarbox uses the same paired policy; vertical noise cannot veto a supported axis.
 uint16_t pt[]={0,100,200,300,400,500,600},pb[]={10,210,310,410,510,610,710};
 uint16_t pl[]={480,480,480,900,480,480,480},pr[]={480,480,480,480,480,480,900};
 assert(detect_samples_consensus(pt,pb,pl,pr,7,result)&&result.left==480&&result.right==480&&result.top==0);
 assert(Select(pt,pb,pl,pr,7,3840,2160,result)&&result.left==480&&result.right==480&&result.top==0);
 pl[6]=0;assert(!detect_samples_consensus(pt,pb,pl,pr,7,result));pl[6]=480;
 pl[5]=pr[5]=300;assert(!detect_samples_consensus(pt,pb,pl,pr,7,result));pl[5]=pr[5]=480;
 pl[5]=pr[5]=700;assert(!detect_samples_consensus(pt,pb,pl,pr,7,result));
 pr[5]=710;assert(!detect_samples_consensus(pt,pb,pl,pr,7,result));pl[5]=pr[5]=480;
 // Stable asymmetric framing cannot justify inventing a symmetric detected crop.
 uint16_t asym[]={30,30,30,30,30,30,30};
 assert(detect_samples_consensus(top,asym,left,right,7,result)&&result.top==0&&result.bottom==0);
 assert(detect_samples_consensus(top,left,left,right,7,result)&&result.top==0&&result.bottom==0);
 assert(detect_samples_consensus(left,right,left,right,7,result)&&result.top==0&&result.left==0);
 // Insufficient contrast never contributes a usable sample; dark-scene coverage is rejected.
 std::fill(image.begin(),image.end(),16);assert(Contrast(&frame)<10);
 // Variable aspect ratios are rejected even when both have significant nonzero bars.
 top[6]=bottom[6]=16;assert(!detect_samples_consensus(top,bottom,left,right,7,result));
 top[6]=16;bottom[6]=30;assert(!detect_samples_consensus(top,bottom,left,right,7,result));
 top[6]=bottom[6]=0;assert(!detect_samples_consensus(top,bottom,left,right,7,result));
 top[6]=bottom[6]=8;left[6]=20;
 // Former all-axis range assertion is intentionally corrected: unilateral horizontal darkness cannot reject vertical consensus.
 assert(detect_samples_consensus(top,bottom,left,right,7,result)&&result.top==8&&result.left==0);left[6]=0;
 // A caption in an otherwise black border changes actual sampled edges, rejecting stability.
 for(int y=8;y<64;++y)for(int x=0;x<128;++x)image[y*128+x]=120;
 for(int x=32;x<96;++x)image[70*128+x]=200;
 edges=Edges(&frame);assert(edges.bottom==1);bottom[6]=edges.bottom;
 assert(!detect_samples_consensus(top,bottom,left,right,7,result));bottom[6]=8;
 // Unsupported formats, resolution changes and truncated/negative strides are refused.
 frame.format=AV_PIX_FMT_RGB24;assert(!AdmitFrame(&frame,&source));
 frame.format=AV_PIX_FMT_YUV420P10BE;assert(!AdmitFrame(&frame,&source));
 frame.format=AV_PIX_FMT_YUV420P;source.width=1920;assert(!AdmitFrame(&frame,&source));source.width=128;
 frame.linesize[0]=-128;assert(Contrast(&frame)==0);frame.linesize[0]=32;assert(Contrast(&frame)==0);
 // Supported little-endian 10-bit luma follows the same edge geometry.
 std::vector<uint16_t> ten(128*72,16<<2);
 for(int y=8;y<64;++y)for(int x=0;x<128;++x)ten[y*128+x]=120<<2;
 frame.data[0]=reinterpret_cast<uint8_t*>(ten.data());frame.linesize[0]=256;frame.format=AV_PIX_FMT_YUV420P10LE;
 assert(AdmitFrame(&frame,&source)&&Contrast(&frame)>10);edges=Edges(&frame);assert(edges.top==8&&edges.bottom==8);
#ifdef TIERED_SPATIAL_EVIDENCE
 auto tenY=[&](int row,int col){return static_cast<uint32_t>(ten[row*128+col]>>2);};
 spatial=detect_spatial_sample(tenY,128,72);assert(spatial.edge[0]==8&&spatial.edge[1]==8&&spatial.strong);
#endif
 for(auto& value:ten)value<<=6;frame.format=AV_PIX_FMT_P010LE;
 assert(AdmitFrame(&frame,&source)&&Contrast(&frame)>10);edges=Edges(&frame);assert(edges.top==8&&edges.bottom==8);
#ifdef TIERED_SPATIAL_EVIDENCE
 auto p010Y=[&](int row,int col){return static_cast<uint32_t>(ten[row*128+col]>>8);};
 spatial=detect_spatial_sample(p010Y,128,72);assert(spatial.edge[0]==8&&spatial.edge[1]==8&&spatial.strong);
#endif
 // Exact lookup cannot borrow latest metadata or survive explicit source/seek clearing.
 CDataCacheCore cache;cache.m_playerVideoInfo.doviFrameMetadataMap.insert(100,{100,true,8});
 cache.m_playerVideoInfo.doviFrameMetadataMap.insert(200,{200,true,16});
 assert(cache.GetVideoDoViFrameMetadata(100).top==8);
 assert(!cache.GetVideoDoViFrameMetadata(101).has_level5_metadata);
 assert(!cache.GetVideoDoViFrameMetadata(-1).has_level5_metadata);
 assert(!cache.GetVideoDoViFrameMetadata(std::numeric_limits<double>::infinity()).has_level5_metadata);
 assert(!cache.GetVideoDoViFrameMetadata(1e30).has_level5_metadata);
 cache.ClearVideoDoViFrameMetadata();assert(!cache.GetVideoDoViFrameMetadata(100).has_level5_metadata);
 std::cout<<"PASS: production probe pixels, paired confidence, scanner selection/AR snap and exact-PTS freshness\n";
}
'''


def run(code, negative=False):
    with tempfile.TemporaryDirectory(prefix='subtitle-active-area-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(code)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        '-I', str(ROOT / 'xbmc'), str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], text=True, capture_output=True,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=10)
        if negative:
            assert result.returncode == -signal.SIGABRT and 'Assertion' in result.stderr and 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = source()
    run(code)
    if args.negative_controls:
        for label, old, new in [
            ('half accepted as majority', 'if (bestSupport < count / 2 + 1)', 'if (bestSupport < (count + 1) / 2)'),
            ('smaller/full-frame accepted', 'if ((first[i] != UINT16_MAX && first[i] + tolerance < candidate) ||\n          (second[i] != UINT16_MAX && second[i] + tolerance < candidate))', 'if (false && ((first[i] != UINT16_MAX && first[i] + tolerance < candidate) || (second[i] != UINT16_MAX && second[i] + tolerance < candidate)))'),
            ('bilateral mixed aspect accepted', 'first[i] > candidate + tolerance && second[i] > candidate + tolerance)', 'false && first[i] > candidate + tolerance && second[i] > candidate + tolerance)'),
            ('joint evidence bypassed', 'std::abs(static_cast<int>(first[j]) - second[j]) <= tolerance &&\n          std::abs(static_cast<int>(first[j]) - value) <= tolerance &&\n          std::abs(static_cast<int>(second[j]) - value) <= tolerance)', 'std::abs(static_cast<int>(first[j]) - value) <= tolerance)'),
            ('unrelated horizontal noise vetoes picture', 'uint16_t vertical = 0, horizontal = 0;', 'if (*std::max_element(left,left+count)-*std::min_element(left,left+count)>5) return false; uint16_t vertical = 0, horizontal = 0;'),
            ('scanner discards consensus', 'detTop = consensus.top;', 'detTop = 0;'),
            ('snap bypassed', 'detTop = tb; detBottom = tb; detLeft = 0; detRight = 0;', 'detLeft = 0; detRight = 0; (void)tb;'),
            ('dark coverage accepted', 'if (count < 6 || count > 14)', 'if (count < 1 || count > 14)'),
            ('latest metadata borrowed', '.find(static_cast<uint64_t>(pts))', '.findOrLatest(static_cast<uint64_t>(pts))'),
            ('source dimensions ignored', 'frame->width != codedWidth || frame->height != codedHeight ||', '(frame->width != codedWidth && false) || (frame->height != codedHeight && false) ||'),
        ]:
            assert old in code
            run(code.replace(old, new, 1), True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
