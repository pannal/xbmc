#!/usr/bin/env python3
"""Verify Kodi's decoded PCM frames using native FFmpeg PCM decoders and ASan/UBSan.

Uses pkg-config for libavcodec and libavutil. With a standalone native FFmpeg
build, pass its include flags and link flags with --ffmpeg-cflags and --ffmpeg-libs.
The real decode, layout and frame formatting paths run with logging/settings stand-ins.
"""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def function(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PREFIX = r'''
#include "cores/AudioEngine/Utils/AEAudioFormat.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
extern "C"
{
#include <libavcodec/avcodec.h>
#include <libavutil/downmix_info.h>
#include <libavutil/intreadwrite.h>
#include <libavutil/mem.h>
}
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <iterator>
#include <vector>
constexpr int LOGINFO = 0, LOGERROR = 1;
struct CLog
{
  template <class... A> static void Log(A &&...) {}
};
struct Settings
{
  Settings *GetSettings() { return this; }
  bool GetBool(const char *) { return false; }
};
struct CServiceBroker
{
  static Settings *GetSettingsComponent()
  {
    static Settings s;
    return &s;
  }
};

struct CAEUtil
{
  static unsigned int DataFormatToBits(AEDataFormat);
  static CAEChannelInfo GetAEChannelLayout(uint64_t);
  static uint64_t GetAVChannelLayout(const CAEChannelInfo &);
  static AVChannel GetAVChannel(AEChannel);
};
'''

DECODER = r'''
struct CDVDAudioCodecFFmpeg {
  void GetData(DVDAudioFrame&);
  int GetData(uint8_t**, size_t);
  AEDataFormat GetDataFormat();
  CAEChannelInfo GetChannelMap();
  void BuildChannelMap();
  int GetSampleRate();
  AVMatrixEncoding GetMatrixEncoding();
  AVAudioServiceType GetAudioServiceType();
  int GetProfile();
'''

MAIN = r'''
enum class Order { Native, Reversed, Unnamed };

static void run(bool planar, uint64_t mask, unsigned int samples, Order order = Order::Native)
{
  const AVCodec *codec =
      avcodec_find_decoder(planar ? AV_CODEC_ID_PCM_S16LE_PLANAR : AV_CODEC_ID_PCM_S16LE);
  assert(codec);
  CDVDAudioCodecFFmpeg decoder{};
  decoder.m_pCodecContext = avcodec_alloc_context3(codec);
  decoder.m_pFrame = av_frame_alloc();
  assert(decoder.m_pCodecContext && decoder.m_pFrame);
  AVChannelLayout &layout = decoder.m_pCodecContext->ch_layout;
  assert(av_channel_layout_from_mask(&layout, mask) == 0);
  if (order != Order::Native)
  {
    AVChannelLayout ordered{};
    ordered.order = AV_CHANNEL_ORDER_CUSTOM;
    ordered.nb_channels = layout.nb_channels;
    ordered.u.map =
        static_cast<AVChannelCustom *>(av_calloc(ordered.nb_channels, sizeof(*ordered.u.map)));
    assert(ordered.u.map);
    for (int i = 0; i < layout.nb_channels; i++)
      ordered.u.map[i].id =
          order == Order::Unnamed ? AV_CHAN_UNKNOWN
                                  : av_channel_layout_channel_from_index(&layout, layout.nb_channels - i - 1);
    av_channel_layout_uninit(&layout);
    layout = ordered;
  }
  decoder.m_pCodecContext->sample_rate = 48000;
  decoder.m_pCodecContext->pkt_timebase = {1, 1000000};
  assert(avcodec_open2(decoder.m_pCodecContext, codec, nullptr) == 0);
  int count = layout.nb_channels;
  AVPacket *packet = av_packet_alloc();
  assert(packet && av_new_packet(packet, samples * count * 2) == 0);
  packet->pts = 700000;
  auto sample = [](unsigned t, unsigned c) { return static_cast<int16_t>(1 + 32 * t + c); };
  for (unsigned t = 0; t < samples; t++)
    for (int c = 0; c < count; c++)
    {
      const unsigned at = planar ? c * samples + t : t * count + c;
      int16_t value = sample(t, c);
      AV_WL16(packet->data + 2 * at, value);
    }
  assert(avcodec_send_packet(decoder.m_pCodecContext, packet) == 0);
  av_packet_free(&packet);
  DVDAudioFrame output{};
  decoder.GetData(output);
  const uint64_t known = mask & ~AV_CH_LOW_FREQUENCY_2;
  assert(output.nb_frames == samples);
  assert(order == Order::Native || decoder.m_pFrame->ch_layout.order == AV_CHANNEL_ORDER_CUSTOM);
  const unsigned mapped = output.format.m_channelLayout.Count();
  assert(CAEUtil::GetAVChannelLayout(output.format.m_channelLayout) == known);
  assert(mapped > 0 && mapped <= static_cast<unsigned>(count));
  assert(output.planes == (planar ? mapped : 1) && output.framesize == mapped * 2);
  assert(output.format.m_sampleRate == 48000 && output.bits_per_sample == 16);
  assert(output.pts == 700000 && std::abs(output.duration - samples * 1000000.0 / 48000) < 1.e-7);
  for (unsigned channel = 0; channel < mapped; channel++)
  {
    // With unnamed speakers Kodi uses the default layout for the channel count.
    const int src = order == Order::Unnamed ? static_cast<int>(channel)
        : av_channel_layout_index_from_channel(
              &layout, CAEUtil::GetAVChannel(output.format.m_channelLayout[channel]));
    assert(src >= 0);
    for (unsigned t = 0; t < samples; t++)
    {
      int16_t value;
      const uint8_t *p =
          planar ? output.data[channel] + t * 2 : output.data[0] + (t * mapped + channel) * 2;
      memcpy(&value, p, 2);
      assert(value == sample(t, src));
    }
  }
  avcodec_free_context(&decoder.m_pCodecContext);
  av_frame_free(&decoder.m_pFrame);
  std::cout << (planar ? "planar" : "packed") << (order == Order::Native ? " native" : " custom") << " channels "
            << count << " -> " << mapped << ", " << samples << " sample frames checked\n";
}
int main()
{
  assert((avcodec_version() >> 16) == LIBAVCODEC_VERSION_MAJOR);
  assert((avutil_version() >> 16) == LIBAVUTIL_VERSION_MAJOR);
  constexpr uint64_t sevenTwoThree = AV_CH_LAYOUT_7POINT1 | AV_CH_TOP_FRONT_LEFT |
                                     AV_CH_TOP_FRONT_RIGHT | AV_CH_TOP_BACK_CENTER |
                                     AV_CH_LOW_FREQUENCY_2;
  for (bool planar : {false, true})
  {
    for (unsigned frames : {1u, 17u})
    {
      run(planar, AV_CH_LAYOUT_STEREO, frames);
      run(planar, sevenTwoThree, frames, Order::Reversed);
      run(planar, AV_CH_LAYOUT_STEREO, frames, Order::Reversed);
      run(planar, AV_CH_LAYOUT_STEREO, frames, Order::Unnamed);
      run(planar, AV_CH_LAYOUT_7POINT1, frames);
      run(planar, sevenTwoThree, frames);
    }
  }
}
'''


def fixture(root):
    audio = root / 'xbmc/cores/VideoPlayer/DVDCodecs/Audio'
    header = (audio / 'DVDAudioCodecFFmpeg.h').read_text()
    decoder = (audio / 'DVDAudioCodecFFmpeg.cpp').read_text()
    frame = (audio / 'DVDAudioCodec.h').read_text()
    util = (root / 'xbmc/cores/AudioEngine/Utils/AEUtil.cpp').read_text()
    frame_type = frame[frame.index('typedef struct stDVDAudioFrame'):
                       frame.index('} DVDAudioFrame;') + len('} DVDAudioFrame;')]
    members = header[header.index('  AEAudioFormat m_format;'):]
    bodies = [function(util, name) for name in (
        'unsigned int CAEUtil::DataFormatToBits', 'CAEChannelInfo CAEUtil::GetAEChannelLayout',
        'uint64_t CAEUtil::GetAVChannelLayout', 'enum AVChannel CAEUtil::GetAVChannel')]
    bodies.extend(function(decoder, name) for name in (
        'static unsigned count_bits', 'void CDVDAudioCodecFFmpeg::BuildChannelMap',
        'CAEChannelInfo CDVDAudioCodecFFmpeg::GetChannelMap',
        'enum AEDataFormat CDVDAudioCodecFFmpeg::GetDataFormat',
        'int CDVDAudioCodecFFmpeg::GetSampleRate',
        'enum AVMatrixEncoding CDVDAudioCodecFFmpeg::GetMatrixEncoding',
        'enum AVAudioServiceType CDVDAudioCodecFFmpeg::GetAudioServiceType',
        'int CDVDAudioCodecFFmpeg::GetProfile'))
    # Both GetData overloads and the private plane-copy helper between them.
    start = decoder.index('void CDVDAudioCodecFFmpeg::GetData(DVDAudioFrame')
    stop = decoder.index('void CDVDAudioCodecFFmpeg::ApplyDrcScale', start)
    return '\n'.join([PREFIX, frame_type, DECODER, members, *bodies, decoder[start:stop], MAIN])


def ffmpeg_flags(override, mode):
    if override is not None:
        return shlex.split(override)
    return shlex.split(subprocess.check_output(
        ['pkg-config', mode, 'libavcodec', 'libavutil'], text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--ffmpeg-cflags')
    parser.add_argument('--ffmpeg-libs')
    args = parser.parse_args()
    root = args.root.resolve()
    cflags = ffmpeg_flags(args.ffmpeg_cflags, '--cflags')
    libs = ffmpeg_flags(args.ffmpeg_libs, '--libs')
    with tempfile.TemporaryDirectory(prefix='kodi-decoded-pcm-') as tmp:
        source = Path(tmp) / 'test.cpp'
        exe = Path(tmp) / 'test'
        source.write_text(fixture(root))
        subprocess.run([
            *shlex.split(os.environ.get('CXX', 'c++')), '-std=c++17', '-g', '-O1',
            '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer',
            '-fno-pie', '-no-pie', '-I', str(root / 'xbmc'), *cflags, str(source),
            str(root / 'xbmc/cores/AudioEngine/Utils/AEChannelInfo.cpp'), *libs, '-o', str(exe),
        ], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
