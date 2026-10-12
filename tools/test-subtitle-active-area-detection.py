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
    admit = scan[scan.index('        if (frame->width != source->width'):scan.index('        lastWidth = frame->width;')]
    access_start = scan.index('        const int stride = frame->linesize[0];')
    access_end = scan.index('        CLog::Log(LOGDEBUG, "DetectActiveArea: sample at ', access_start)
    access = scan[access_start:access_end]
    edges_start = scan.index('      /* Re-derive frame accessors')
    edges_end = scan.index('      samples_top[validSamples] = sTop;', edges_start)
    edges = scan[edges_start:edges_end]
    code = PREFIX
    code += 'bool AdmitFrame(Frame* frame,Source* source){\n' + admit + 'return true;cleanup:return false;}\n'
    code += 'uint32_t Contrast(Frame* frame){int lastWidth=frame->width,lastHeight=frame->height;{\n'
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
    code += re.search(r'static const uint32_t s_commonAR\[\] = \{.*?\};', aml, re.S).group() + '\n'
    final = scan[scan.index('    /* Require enough usable samples'):scan.index('\n  }\n\n  detect_publish(')]
    code += 'bool Select(const uint16_t* samples_top,const uint16_t* samples_bottom,const uint16_t* samples_left,const uint16_t* samples_right,int validSamples,int lastWidth,int lastHeight,DetectResult& result,const bool* rejected=nullptr){\n'
    code += 'bool known[7]{};const bool* samples_rejected=rejected?rejected:known;(void)samples_rejected;\n'
    if 'static bool detect_subtitle_consensus(' in aml:
        code += '(void)detect_subtitle_consensus;\n'
    code += 'constexpr int numSeeks=7;uint16_t detTop=0,detBottom=0,detLeft=0,detRight=0;{\n'
    code += final + '\n}result={detTop,detBottom,detLeft,detRight};return true;cleanup:return false;}\n'
    cache = (ROOT / 'xbmc/cores/DataCacheCore.cpp').read_text()
    code += function(cache, 'DOVIFrameMetadata CDataCacheCore::GetVideoDoViFrameMetadata(double pts)') + '\n'
    code += function(cache, 'void CDataCacheCore::ClearVideoDoViFrameMetadata()')
    # Retain production throttle/coverage/format/VFS and injection wiring.
    assert 'const bool throttle = !source->nativeDV || detect_throttle_enabled();' in scan
    assert 'fmtCtx->flags |= AVFMT_FLAG_CUSTOM_IO;' in scan
    assert 'fmtCtx->interrupt_callback.opaque = const_cast<CAMLNativeWorker::Run*>(&run);' in scan
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
 assert(!detect_samples_consensus(top,bottom,left,right,8,result));
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
 for(auto& value:ten)value<<=6;frame.format=AV_PIX_FMT_P010LE;
 assert(AdmitFrame(&frame,&source)&&Contrast(&frame)>10);edges=Edges(&frame);assert(edges.top==8&&edges.bottom==8);
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
            ('dark coverage accepted', 'if (count < 6 || count > 7)', 'if (count < 1 || count > 7)'),
            ('latest metadata borrowed', '.find(static_cast<uint64_t>(pts))', '.findOrLatest(static_cast<uint64_t>(pts))'),
            ('source dimensions ignored', 'frame->width != source->width || frame->height != source->height ||', '(frame->width != source->width && false) || (frame->height != source->height && false) ||'),
        ]:
            assert old in code
            run(code.replace(old, new, 1), True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
