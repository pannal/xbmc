#!/usr/bin/env python3
"""Run production AML capture conversion with real threads and injected EAGAIN.

Checks both public entry points against their serial output, including failures
at every worker launch and joining before return. FFmpeg scaling and logging are
stand-ins; video frames use the direct NV12 path. No device or HDR validation.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

STUBS = {
    'utils/log.h': '''#pragma once
constexpr int LOGERROR = 0;
struct CLog { template<typename... T> static void Log(T&&...) {} };
''',
    'libavutil/pixfmt.h': '''#pragma once
enum AVPixelFormat { AV_PIX_FMT_NV12, AV_PIX_FMT_YUV444P16LE };
''',
    'libswscale/swscale.h': '''#pragma once
#include <stdint.h>
#include <stdlib.h>
struct SwsContext {};
#define SWS_BILINEAR 2
static inline SwsContext* sws_getContext(int, int, AVPixelFormat, int, int,
                                       AVPixelFormat, int, void*, void*, void*)
{ abort(); }
static inline int sws_scale(SwsContext*, const uint8_t* const*, const int*, int,
                            int, uint8_t* const*, const int*) { abort(); }
static inline void sws_freeContext(SwsContext*) { abort(); }
''',
}

TESTS = r'''
#include "utils/AMLCaptureConvert.h"
#include "utils/AMLVideoCapture.h"
#include <algorithm>
#include <array>
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <exception>
#include <pthread.h>
#include <thread>
#include <vector>

unsigned int concurrency = 4;
int attempts = 0, joins = 0, failAt = 0;
// Keep the scheduling cases deterministic even on a single-core test host.
unsigned int std::thread::hardware_concurrency() noexcept { return concurrency; }

extern "C" int pthread_create(pthread_t* t, const pthread_attr_t* a,
                              void* (*f)(void*), void* arg)
{
  using Create = int (*)(pthread_t*, const pthread_attr_t*, void* (*)(void*), void*);
  static auto create = reinterpret_cast<Create>(dlsym(RTLD_NEXT, "pthread_create"));
  assert(create);
  if (++attempts == failAt)
    return EAGAIN;
  return create(t, a, f, arg);
}

extern "C" int pthread_join(pthread_t t, void** result)
{
  using Join = int (*)(pthread_t, void**);
  static auto join = reinterpret_cast<Join>(dlsym(RTLD_NEXT, "pthread_join"));
  assert(join);
  const int rc = join(t, result);
  if (!rc)
    ++joins;
  return rc;
}

void reset(unsigned int cores, int fail)
{
  concurrency = cores;
  attempts = joins = 0;
  failAt = fail;
}

void checkWorkers(int expected)
{
  assert(attempts == (failAt ? failAt : expected));
  assert(joins == (failAt ? failAt - 1 : expected));
}

std::vector<uint8_t> gui(int width, int height, int stride)
{
  std::vector<uint8_t> pixels(stride * height, 0xA5);
  for (int y = 0; y < height; ++y)
    for (int x = 0; x < width; ++x)
    {
      auto* p = pixels.data() + y * stride + x * 4;
      p[3] = (x * 53 + y * 71) % 256;
      p[0] = p[3] / 2;
      p[1] = p[3] / 3;
      p[2] = p[3] / 4;
    }
  return pixels;
}

void scale(int height, unsigned int cores, int expected)
{
  const auto input = gui(3, 5, 12);
  std::vector<uint8_t> reference(7 * height * 4, 0xA5);
  reset(1, 0);
  assert(AML_CAPTURE_CONVERT::ScaleGui(input.data(), 3, 5, reference.data(), 7, height));
  checkWorkers(0);
  for (int fail = 0; fail <= expected; ++fail)
  {
    std::vector<uint8_t> result(reference.size(), 0xA5);
    reset(cores, fail);
    assert(AML_CAPTURE_CONVERT::ScaleGui(input.data(), 3, 5, result.data(), 7, height));
    checkWorkers(expected);
    assert(result == reference);
  }
}

void compose(int height, unsigned int cores, int expected)
{
  AML_VIDEO_CAPTURE::Frame frame;
  frame.x = frame.y = 1;
  frame.width = frame.frameWidth = 4;
  frame.height = frame.frameHeight = height;
  frame.displayWidth = 6;
  frame.displayHeight = height + 2;
  frame.stride = 4;
  frame.nv12.resize(4 * height + 4 * ((height + 1) / 2), 128);
  for (int y = 0; y < height; ++y)
    for (int x = 0; x < 4; ++x)
      frame.nv12[y * 4 + x] = 16 + (y * 31 + x * 17) % 220;
  const int stride = 6 * 4 + 8;
  const auto input = gui(6, height + 2, stride);
  auto reference = input;
  reset(1, 0);
  assert(AML_CAPTURE_CONVERT::ComposeUnder(frame, reference.data(), 6, height + 2, stride));
  checkWorkers(0);
  for (int y = 0; y < height + 2; ++y)
    for (int x = 0; x < stride; ++x)
    {
      const bool inside = y >= 1 && y <= height && x >= 4 && x < 20;
      if (!inside)
        assert(reference[y * stride + x] == input[y * stride + x]);
      else if (x % 4 == 3)
        assert(reference[y * stride + x] == 255);
    }
  for (int fail = 0; fail <= expected; ++fail)
  {
    auto result = input;
    reset(cores, fail);
    assert(AML_CAPTURE_CONVERT::ComposeUnder(frame, result.data(), 6, height + 2, stride));
    checkWorkers(expected);
    assert(result == reference);
  }
}

int main()
{
  std::set_terminate([] {
    std::fprintf(stderr, "Unexpected terminate after launch %d (failure at %d)\n", attempts, failAt);
    std::_Exit(2);
  });
  for (auto run : {scale, compose})
  {
    run(7, 4, 3); // uneven row partitions, fail each of the three launches
    run(8, 4, 3);
    run(2, 4, 1); // fewer rows than cores
    run(1, 4, 0);
    run(7, 0, 0); // unknown hardware concurrency
    run(7, 1, 0);
    run(7, 2, 1);
    run(7, 3, 2);
    run(7, 32, 3); // worker count remains capped
  }
  std::puts("ScaleGui and ComposeUnder: serial/parallel pixels, launch failures and joins PASS");
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'xbmc/utils/AMLCaptureConvert.cpp',
                        help='Alternate production source for a negative control')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='aml-capture-workers-') as temporary:
        directory = Path(temporary)
        for name, contents in STUBS.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        test = directory / 'test.cpp'
        test.write_text(TESTS)
        binary = directory / 'test'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(directory), '-I', str(ROOT / 'xbmc'),
                        '-I', str(ROOT / 'xbmc/utils'), str(args.source), str(test),
                        '-ldl', '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)


if __name__ == '__main__':
    main()
