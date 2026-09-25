/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "DVDAudioCodecFFmpeg.h"
#include "OmniphonyPcmStream.h"
#include "cores/AudioEngine/Interfaces/AEResample.h"

#include <cstdint>
#include <memory>
#include <vector>

class CProcessInfo;

/*!
 * \brief Planes this source will accept from the decoder.
 *
 * `DVDAudioFrame` carries `uint8_t* data[16]`, so sixteen is what Kodi itself
 * can pass onward: a planar stream wider than that is beyond the ordinary
 * decode path too, not only this one. Bounding the accepted layout here keeps
 * this source, its callers' arrays and the frame struct in agreement, and it
 * costs nothing real - the widest layout the renderer places is 7.1.4, at
 * twelve.
 */
constexpr int OMNI_PCM_MAX_PLANES = 16;

/*!
 * \brief An ffmpeg decoder that hands its output to the binaural renderer.
 *
 * The object path taps the stream before decode, because objects only exist
 * there. Everything else has to be decoded first, and that is what this is: an
 * ordinary CDVDAudioCodecFFmpeg whose PCM is converted to what the renderer's
 * PCM bridge accepts - interleaved, one label per channel, at the rate the
 * codec settled on - instead of being handed to the sink.
 *
 * It derives from the ffmpeg codec rather than owning one, for two reasons.
 * The layout accessor below needs the decoder's own AVCodecContext, which is
 * protected; and when the helper dies mid-stream the codec can hand this object
 * straight to its fallback, where it carries on as the plain ffmpeg decoder it
 * already is - no second decoder, and no packet replayed into it.
 */
class COmniphonyPcmSource : public CDVDAudioCodecFFmpeg
{
public:
  /*!
   * \brief \param rate the rate to convert to, which the codec chose and the
   * renderer was opened at. Not this component's decision: everything from the
   * engine's config to the format handed to ActiveAE has to agree on one
   * number, and the codec is where that number is settled.
   */
  COmniphonyPcmSource(CProcessInfo& processInfo, unsigned int rate);
  ~COmniphonyPcmSource() override;

  void Reset() override;

  /*!
   * \brief Serve a frame this source received but could not use, if there is
   * one; otherwise receive the next as the decoder normally would.
   *
   * This is the whole of the fallback contract. \ref Convert has to receive a
   * frame before it can see the layout ffmpeg settled on, so the frame that
   * proves a stream unrenderable has already left the decoder's queue. Letting
   * the ordinary path pull the *next* one would drop it, silently, at exactly
   * the moment the listener is switched over. Holding it here means the base's
   * `GetData(DVDAudioFrame&)` formats it as though nothing had happened.
   */
  int GetData(uint8_t** dst) override;

  /*!
   * \brief Take one decoded frame, converted for the bridge.
   *
   * \param pcm  cleared and filled with interleaved samples at the rate this
   *             source was constructed with, in the encoding \ref Header says
   * \param pts  the frame's timestamp in DVD time, or DVD_NOPTS_VALUE
   * \return false when the decoder has nothing ready, or when this stream
   *         cannot be rendered - see \ref Unsupported, which distinguishes the
   *         two. A frame is never partly converted: on failure \p pcm is empty.
   */
  bool Convert(std::vector<uint8_t>& pcm, double& pts);

  //! After decoder EOF, pull the resampler tail until pcm is empty. False
  //! reports a conversion error; Reset discards this tail instead.
  bool DrainResampler(std::vector<uint8_t>& pcm);

  /*!
   * \brief True once this stream has proved unrenderable, which is permanent.
   *
   * Set when the decoder's layout cannot be labelled or the resampler refuses
   * the conversion. The caller's answer is to decode this stream the ordinary
   * way; nothing here will start working later.
   */
  bool Unsupported() const { return m_unsupported; }

  /*!
   * \brief The header introducing the samples \ref Convert produces.
   *
   * Empty until the first frame has been converted, which is the earliest the
   * layout is known. Rebuilt whenever the stream's geometry changes.
   */
  const std::vector<uint8_t>& Header() const { return m_header; }

  /*!
   * \brief Whether the bridge still needs to be sent \ref Header.
   *
   * The bridge parses one header, then streams until it is reset; a reset
   * returns it to expecting another. So this is armed by the first conversion
   * and by every reset, and the caller clears it once the header has gone out.
   */
  bool HeaderPending() const { return m_headerPending; }
  void TakeHeader() { m_headerPending = false; }

  /*!
   * \brief The channel labels inside \ref Header, for describing the stream.
   *
   * Kept rather than parsed back out of the header, so the bytes the renderer
   * is sent and the words the screen shows cannot say different things. Empty
   * before the first frame, and emptied when the stream is given up on.
   */
  const std::vector<uint8_t>& Labels() const { return m_labels; }

  //! \brief Channels in the converted output, or 0 before the first frame.
  int Channels() const { return m_channels; }

  /*!
   * \brief The layout ffmpeg was opened with, before anything is decoded.
   *
   * The level the renderer is configured with has to be chosen at open, and
   * this is the best that is known then. It is not merely the demuxer's hint:
   * CDVDAudioCodecFFmpeg::Open takes the hint's mask when there is one and
   * falls back to av_channel_layout_default for the hinted count when there is
   * not, so what the decoder ends up configured with is at least as good as the
   * hint and sometimes better.
   *
   * Still a hint, though. The decoder may correct it once it has seen the
   * stream, and nothing rewrites the engine's config when it does.
   *
   * \param mask      set to the layout mask, or 0 if the order is not native
   * \param channels  set to the channel count, or 0 if it is not known
   */
  void OpenLayout(uint64_t& mask, int& channels) const;

  //! \brief Bytes in one converted sample-frame, or 0 before the first frame.
  size_t FrameSize() const { return static_cast<size_t>(m_channels) * sizeof(float); }

  //! \brief The rate Convert() delivers at.
  int Rate() const { return m_rate; }

private:
  /*!
   * \brief The decoder's layout, if every channel of it can be placed.
   *
   * Three things have to hold, and the first is why this exists at all rather
   * than reusing GetChannelMap(): the layout must be AV_CHANNEL_ORDER_NATIVE,
   * because `u.mask` shares storage with a pointer and reading it for a custom
   * layout reads that pointer as a mask; the set bits must number exactly the
   * decoded channels, or the mask is not describing this stream; and every bit
   * must name a position the renderer can place.
   *
   * GetChannelMap() satisfies none of the three - it reads `u.mask`
   * unconditionally, substitutes a default layout when the count disagrees, and
   * folds the top-back channels onto the back pair.
   */
  bool NativeLayout(uint64_t& mask, int& channels) const;

  //! \brief (Re)build the resampler and the header for the current geometry.
  bool Configure(uint64_t mask, int channels, AVSampleFormat fmt, int sampleRate);

  //! \brief Hold a received frame for \ref GetData to serve, if the ordinary
  //! path could carry it. A wider one is left dropped rather than written past
  //! the sixteen pointers every caller of GetData actually offers.
  void Retain(uint8_t* const* planes, int bytes);

  //! \brief Give up on this stream, once, with a reason in the log.
  void GiveUp(const char* why);

  //! \brief Planes of a frame received but not usable, and its byte count.
  //! Served once, by \ref GetData, so the ordinary path still delivers it.
  uint8_t* m_retainedPlanes[OMNI_PCM_MAX_PLANES]{};
  int m_retainedBytes{0};

  //! The rate everything is converted to - see the constructor.
  const int m_rate;

  std::unique_ptr<ActiveAE::IAEResample> m_resampler;
  std::vector<uint8_t> m_labels;
  std::vector<uint8_t> m_header;
  bool m_headerPending{false};
  bool m_unsupported{false};

  //! The geometry the resampler was built for; a change rebuilds it.
  uint64_t m_mask{0};
  int m_channels{0};
  AVSampleFormat m_srcFmt{AV_SAMPLE_FMT_NONE};
  int m_srcRate{0};
};
