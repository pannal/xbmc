#!/usr/bin/env python3
"""Execute production detector sample admission, luma/edge calculations, consensus
and exact-PTS metadata lookup. FFmpeg/VFS/threads are tested separately; this is
synthetic host sample validation, not software decoding or device acceptance.
"""
from pathlib import Path
import argparse
import os
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def source():
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
    code += 'Result Edges(Frame* frame){int lastWidth=frame->width,lastHeight=frame->height;\n'
    code += edges + 'return {sTop,sBottom,sLeft,sRight};}\n'
    code += function(aml, 'static bool detect_samples_stable(') + '\n'
    cache = (ROOT / 'xbmc/cores/DataCacheCore.cpp').read_text()
    code += function(cache, 'DOVIFrameMetadata CDataCacheCore::GetVideoDoViFrameMetadata(double pts)') + '\n'
    code += function(cache, 'void CDataCacheCore::ClearVideoDoViFrameMetadata()')
    # Retain production throttle/coverage/format/VFS and injection wiring.
    assert 'const bool throttle = !source->nativeDV || detect_throttle_enabled();' in scan
    assert 'fmtCtx->flags |= AVFMT_FLAG_CUSTOM_IO;' in scan
    assert 'fmtCtx->interrupt_callback.opaque = const_cast<CAMLNativeWorker::Run*>(&run);' in scan
    assert 'av_freep(&avioCtx->buffer);' in scan and 'av_free(avioBuf);' in scan
    assert 'detect_samples_stable(samples_top, samples_bottom, samples_left, samples_right, validSamples)' in scan
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
#include <vector>
#include "utils/AgedMap.h"
using CCriticalSection=std::recursive_mutex;
constexpr int AV_PIX_FMT_YUV420P=0,AV_PIX_FMT_YUVJ420P=1,AV_PIX_FMT_YUV422P=2,
 AV_PIX_FMT_YUVJ422P=3,AV_PIX_FMT_YUV444P=4,AV_PIX_FMT_YUVJ444P=5,
 AV_PIX_FMT_NV12=6,AV_PIX_FMT_NV21=7,AV_PIX_FMT_YUV420P10LE=8,AV_PIX_FMT_P010LE=9,
 AV_PIX_FMT_YUV420P10BE=10,AV_PIX_FMT_RGB24=11;
constexpr int DV_DETECT_SKIPPED=4;
std::atomic<int> s_detectState{0};
struct Frame {int width=128,height=72,format=0;int linesize[1]{128};uint8_t* data[1]{};};
struct Source {int width=128,height=72;};
struct Result {uint16_t top,bottom,left,right;};
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
 auto edges=Edges(&frame);assert(edges.top==8&&edges.bottom==8&&edges.left==0&&edges.right==0);
 uint16_t top[7]={8,8,8,8,8,8,8},bottom[7]={8,8,8,8,8,8,8},left[7]={},right[7]={};
 assert(detect_samples_stable(top,bottom,left,right,7));assert(detect_samples_stable(top,bottom,left,right,6));
 assert(!detect_samples_stable(top,bottom,left,right,5));
 // Insufficient contrast never contributes a usable sample; dark-scene coverage is rejected.
 std::fill(image.begin(),image.end(),16);assert(Contrast(&frame)<10);
 // Variable aspect ratios are rejected even when both have significant nonzero bars.
 top[6]=bottom[6]=16;assert(!detect_samples_stable(top,bottom,left,right,7));
 top[6]=bottom[6]=0;assert(!detect_samples_stable(top,bottom,left,right,7));
 top[6]=bottom[6]=8;left[6]=20;assert(!detect_samples_stable(top,bottom,left,right,7));left[6]=0;
 // A caption in an otherwise black border changes actual sampled edges, rejecting stability.
 for(int y=8;y<64;++y)for(int x=0;x<128;++x)image[y*128+x]=120;
 for(int x=32;x<96;++x)image[70*128+x]=200;
 edges=Edges(&frame);assert(edges.bottom==1);bottom[6]=edges.bottom;
 assert(!detect_samples_stable(top,bottom,left,right,7));bottom[6]=8;
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
 std::cout<<"PASS: production probe pixel admission/contrast/edges/stability and exact-PTS metadata freshness\n";
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
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
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
            ('mixed aspect accepted', 'if (*range.second - *range.first > 5)', 'if (false && *range.second - *range.first > 5)'),
            ('dark coverage accepted', 'if (count < 6 || count > 7)', 'if (count < 1 || count > 7)'),
            ('latest metadata borrowed', '.find(static_cast<uint64_t>(pts))', '.findOrLatest(static_cast<uint64_t>(pts))'),
            ('source dimensions ignored', 'frame->width != source->width || frame->height != source->height ||', '(frame->width != source->width && false) || (frame->height != source->height && false) ||'),
        ]:
            assert old in code
            run(code.replace(old, new, 1), True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
