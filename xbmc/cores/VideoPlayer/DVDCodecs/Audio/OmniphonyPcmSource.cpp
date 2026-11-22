/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyPcmSource.h"

#include "cores/AudioEngine/AEResampleFactory.h"
#include "cores/AudioEngine/Utils/AEChannelData.h"
#include "cores/AudioEngine/Utils/AEUtil.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "utils/log.h"

#include <algorithm>

extern "C"
{
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
}

namespace
{
/*!
 * \brief What the samples are sent as.
 *
 * Float is what the resampler produces most directly and what the renderer
 * ultimately wants, so it costs the fewest passes over the audio. It gives up
 * nothing above full scale either: the bridge scales it into the engine's
 * 24-bit convention without clamping, and that convention sits inside an i32
 * with eight bits to spare, so a decoder's overshoot reaches the level set in
 * the render configuration intact rather than being clipped ahead of it.
 * Switching to I32Scaled24 would be a one-line change, precisely because the
 * header says which encoding it is.
 */
constexpr OmniphonyPcmEncoding OMNI_PCM_ENCODING = OmniphonyPcmEncoding::Float32;

// Receive wider FFmpeg layouts before deciding whether the bridge can render them.
constexpr size_t RECEIVE_PLANES = AE_CH_MAX;
} // namespace

// The bound this source accepts must never exceed what a DVDAudioFrame can
// carry, because a retained frame is handed back through one. Stated to the
// compiler rather than to a reader: if that array is ever resized, whichever
// direction it goes, this stops building instead of quietly overrunning.
static_assert(OMNI_PCM_MAX_PLANES <=
                  static_cast<int>(sizeof(DVDAudioFrame::data) / sizeof(DVDAudioFrame::data[0])),
              "OMNI_PCM_MAX_PLANES exceeds the planes DVDAudioFrame can carry");
static_assert(OMNI_PCM_MAX_PLANES <= static_cast<int>(RECEIVE_PLANES),
              "the receive array must hold every layout that can be accepted");

COmniphonyPcmSource::COmniphonyPcmSource(CProcessInfo& processInfo, unsigned int rate)
  : CDVDAudioCodecFFmpeg(processInfo), m_rate(static_cast<int>(rate))
{
}

COmniphonyPcmSource::~COmniphonyPcmSource() = default;

void COmniphonyPcmSource::Reset()
{
  CDVDAudioCodecFFmpeg::Reset();

  // Drop the resampler rather than carry it across the seek. IAEResample has
  // no discard/reset operation, so buffered samples would come out afterwards as
  // though it belonged to the new position - a fraction of a second of the old
  // one, at the very moment the listener is told the film has moved. ActiveAE
  // has the same problem and answers it the same way, destroying and rebuilding
  // in CActiveAEBufferPoolResample::Flush.
  //
  // The next converted frame rebuilds it, and that rebuild re-arms the header,
  // which the bridge needs again anyway: it is reset alongside this and returns
  // to expecting one.
  m_resampler.reset();
  m_headerPending = false;

  // A frame held for the fallback belongs to where the film used to be. The
  // decoder was just flushed, so its planes point at nothing worth keeping.
  m_retainedBytes = 0;
}

bool COmniphonyPcmSource::NativeLayout(uint64_t& mask, int& channels) const
{
  if (!m_pFrame)
    return false;

  const AVChannelLayout& layout = m_pFrame->ch_layout;
  if (layout.order != AV_CHANNEL_ORDER_NATIVE)
    return false;

  const int count = layout.nb_channels;
  if (count <= 0 || count > OMNI_PCM_MAX_PLANES)
    return false;

  // Only meaningful once the order is known to be native: for any other order
  // this union member is not a mask.
  const uint64_t m = layout.u.mask;
  int bits = 0;
  for (int i = 0; i < 64; ++i)
  {
    if (m & (static_cast<uint64_t>(1) << i))
      ++bits;
  }
  if (bits != count)
    return false;

  mask = m;
  channels = count;
  return true;
}

void COmniphonyPcmSource::OpenLayout(uint64_t& mask, int& channels) const
{
  mask = 0;
  channels = 0;
  if (!m_pCodecContext)
    return;

  const AVChannelLayout& layout = m_pCodecContext->ch_layout;
  channels = layout.nb_channels > 0 ? layout.nb_channels : 0;
  // The same union hazard NativeLayout guards: for any order but native this
  // member is a pointer, not a mask. A count on its own is still worth having.
  if (layout.order == AV_CHANNEL_ORDER_NATIVE)
    mask = layout.u.mask;
}

void COmniphonyPcmSource::Retain(uint8_t* const* planes, int bytes)
{
  const int channels = m_pFrame->ch_layout.nb_channels;
  const auto format = static_cast<AVSampleFormat>(m_pFrame->format);
  const int planesUsed = av_sample_fmt_is_planar(format) ? channels : 1;
  if (bytes <= 0 || planesUsed <= 0 || planesUsed > AE_CH_MAX)
    return;

  std::copy_n(planes, planesUsed, m_retainedPlanes);
  m_retainedPlaneCount = planesUsed;
  m_retainedBytes = bytes;
}

int COmniphonyPcmSource::GetData(uint8_t** dst, size_t capacity)
{
  if (m_retainedBytes > 0)
  {
    if (capacity < m_retainedPlaneCount)
      return AVERROR(ENOSPC);
    std::copy_n(m_retainedPlanes, m_retainedPlaneCount, dst);
    const int bytes = m_retainedBytes;
    m_retainedBytes = 0;
    return bytes;
  }
  return CDVDAudioCodecFFmpeg::GetData(dst, capacity);
}

void COmniphonyPcmSource::GiveUp(const char* why)
{
  if (m_unsupported)
    return;

  m_unsupported = true;
  m_resampler.reset();
  m_labels.clear();
  m_header.clear();
  m_headerPending = false;
  CLog::Log(LOGINFO, "COmniphonyPcmSource: not rendering this stream binaurally: {}", why);
}

bool COmniphonyPcmSource::Configure(uint64_t mask, int channels, AVSampleFormat fmt, int sampleRate)
{
  std::vector<uint8_t> labels;
  if (!OmniphonyPcmChannelLabels(mask, channels, labels))
  {
    GiveUp("its channel layout has a position the renderer cannot place");
    return false;
  }

  auto resampler = ActiveAE::CAEResampleFactory::Create();
  if (!resampler)
  {
    GiveUp("no resampler could be created");
    return false;
  }

  SampleConfig src{};
  src.fmt = fmt;
  src.channel_layout = mask;
  src.channels = channels;
  src.sample_rate = sampleRate;
  src.bits_per_sample = static_cast<int>(CAEUtil::DataFormatToUsedBits(GetDataFormat()));
  src.dither_bits = static_cast<int>(CAEUtil::DataFormatToDitherBits(GetDataFormat()));

  // Same layout out as in. That identity is what makes this conversion safe to
  // do here rather than in the renderer: with no remap layout, no upmix, and a
  // destination that still carries every source channel, ActiveAEResampleFFMPEG
  // builds no rematrix at all - so the centre boost, the surround mix and the
  // downmix normalisation it would otherwise apply are all unreachable, and the
  // audio arrives at the renderer at exactly the level it left the decoder.
  SampleConfig dst = src;
  dst.fmt = AV_SAMPLE_FMT_FLT;
  dst.sample_rate = m_rate;
  dst.bits_per_sample = static_cast<int>(CAEUtil::DataFormatToUsedBits(AE_FMT_FLOAT));
  dst.dither_bits = static_cast<int>(CAEUtil::DataFormatToDitherBits(AE_FMT_FLOAT));

  if (!resampler->Init(dst, src,
                       false, // upmix: never - the layout is unchanged
                       false, // normalize: there is no matrix to normalise
                       1.0, // centerMix: unreachable without a matrix
                       1.0, // surroundMix: likewise
                       nullptr, // remapLayout: a remap would build a matrix
                       AE_QUALITY_HIGH,
                       false, // force_resample
                       0.0f)) // sublevel: 0 leaves lfe_mix_level unset entirely
  {
    GiveUp("the resampler refused the conversion");
    return false;
  }

  m_header = OmniphonyPcmHeader(labels, static_cast<uint32_t>(m_rate), OMNI_PCM_ENCODING);
  if (m_header.empty())
  {
    GiveUp("the stream header could not be built");
    return false;
  }
  m_labels = std::move(labels);

  // Said once per geometry, not once per rebuild: every seek rebuilds this, and
  // a line per seek would bury the one that reports an actual format change.
  const bool changed =
      (mask != m_mask || channels != m_channels || fmt != m_srcFmt || sampleRate != m_srcRate);

  m_resampler = std::move(resampler);
  m_headerPending = true;
  m_mask = mask;
  m_channels = channels;
  m_srcFmt = fmt;
  m_srcRate = sampleRate;

  if (changed)
    CLog::Log(LOGINFO, "COmniphonyPcmSource: {} channels, {} Hz -> {} Hz for the renderer",
              channels, sampleRate, m_rate);
  return true;
}

bool COmniphonyPcmSource::Convert(std::vector<uint8_t>& pcm, double& pts)
{
  pcm.clear();
  pts = DVD_NOPTS_VALUE;

  if (m_unsupported)
    return false;

  // Use the decoded frame layout when configuring the bridge.
  uint8_t* planes[RECEIVE_PLANES]{};
  const int bytes = GetData(planes, std::size(planes));
  if (bytes == AVERROR(ENOSPC))
    GiveUp("decoded layout exceeds the receive plane capacity");
  if (bytes <= 0 || !m_pFrame || !m_pCodecContext)
    return false;

  uint64_t mask = 0;
  int channels = 0;
  if (!NativeLayout(mask, channels))
  {
    Retain(planes, bytes);
    GiveUp("ffmpeg reports no usable channel layout for it");
    return false;
  }

  const auto fmt = static_cast<AVSampleFormat>(m_pFrame->format);
  const int rate = m_pCodecContext->sample_rate;
  if (rate <= 0)
  {
    // Giving up rather than returning empty-handed. A decoder that has produced
    // a frame but reports no sample rate will go on reporting none, and simply
    // returning false leaves the caller staging nothing for the rest of the
    // film - no audio, and nothing that looks like a failure either. The frame
    // is retained on the way out, like every other refusal here.
    Retain(planes, bytes);
    GiveUp("ffmpeg reports no sample rate for it");
    return false;
  }

  // A mid-stream change of any of these is a different conversion. Rebuilding
  // re-arms the header too, which is what tells the caller to reset the bridge
  // and describe the new geometry before sending more samples.
  if (!m_resampler || mask != m_mask || channels != m_channels || fmt != m_srcFmt ||
      rate != m_srcRate)
  {
    if (!Configure(mask, channels, fmt, rate))
    {
      // Configure has already given up; the frame it was called for is still
      // ours to hand back rather than drop on the way to the fallback.
      Retain(planes, bytes);
      return false;
    }
  }

  const int srcFrames = m_pFrame->nb_samples;
  if (srcFrames <= 0)
    return true; // a frame that decoded to nothing is not an error

  // Include samples held by earlier calls as well as this frame's converted
  // length. Both terms are rounded up in destination frames; no assumed filter
  // delay or fixed amount of spare space is needed.
  const int dstFrames = m_resampler->CalcDstSampleCount(srcFrames, m_rate, rate) +
                        m_resampler->GetBufferedSamples();
  pcm.resize(static_cast<size_t>(dstFrames) * FrameSize());

  uint8_t* dst[16]{};
  dst[0] = pcm.data();
  const int written = m_resampler->Resample(dst, dstFrames, planes, srcFrames, 1.0);
  if (written < 0)
  {
    // Retained like the other refusals. The resampler consumed this frame's
    // samples into output that is about to be thrown away, so the planes still
    // hold everything the ordinary path needs to deliver it - and without this
    // the listener loses a frame at the very moment they are switched across.
    Retain(planes, bytes);
    GiveUp("the resampler failed mid-stream");
    pcm.clear();
    return false;
  }

  pcm.resize(static_cast<size_t>(written) * FrameSize());

  // Mirrors CDVDAudioCodecFFmpeg::GetData, so a block timed here and one timed
  // there describe the same instant.
  const int64_t bpts = m_pFrame->best_effort_timestamp;
  if (bpts != AV_NOPTS_VALUE)
    pts = static_cast<double>(bpts) * DVD_TIME_BASE / AV_TIME_BASE;

  return true;
}

bool COmniphonyPcmSource::DrainResampler(std::vector<uint8_t>& pcm)
{
  pcm.clear();
  if (m_unsupported)
    return false;
  if (!m_resampler)
    return true;

  // A null input flushes delayed samples. Pull bounded chunks until an empty
  // result; the caller sends each one before the helper receives OP_FLUSH.
  const int dstFrames = m_rate / 100;
  pcm.resize(static_cast<size_t>(dstFrames) * FrameSize());
  uint8_t* dst[1]{pcm.data()};
  const int written = m_resampler->Resample(dst, dstFrames, nullptr, 0, 1.0);
  if (written < 0)
  {
    GiveUp("the resampler failed at end of stream");
    pcm.clear();
    return false;
  }
  pcm.resize(static_cast<size_t>(written) * FrameSize());
  return true;
}
