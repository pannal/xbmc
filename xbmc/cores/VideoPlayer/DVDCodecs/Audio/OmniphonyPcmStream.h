/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>
#include <string>
#include <vector>

/*!
 * \brief Codec value sent to the PCM bridge as `input_codec`.
 *
 * The OPCM header deliberately remains only geometry and samples. The original
 * compressed codec travels through the engine's existing OPEN/configuration
 * path instead, where the bridge uses it to select Dolby, DTS or plain-PCM
 * placement policy before the first frame.
 */
const char* OmniphonyPcmCodecId(int codecId);

/*!
 * \brief The OPCM wire format: how Kodi describes decoded PCM to the renderer.
 *
 * The renderer does not decode. It is fed by a bridge plugin, and the bridge
 * this describes - libpcm_bridge.so - exists because Kodi already decoded the
 * stream and therefore already knows what every channel is. The header says so
 * directly: one label byte per channel, in the order the samples are
 * interleaved.
 *
 * That is the whole reason this is not a WAV header. A WAV header carries a
 * channel count and, at best, a WAVE_FORMAT_EXTENSIBLE dwChannelMask - and a
 * mask is a lossy round trip in both directions. Its bit order is the
 * interleave order, so the host must re-sort its layout to match; positions
 * with no mask bit cannot be expressed; and one position the renderer does not
 * name (TOP_BACK_CENTER) has nowhere to go at all. Six channels through a plain
 * WAV header are labelled the same whether the source used side or back
 * surrounds, which is audibly wrong and silent about it.
 *
 * \verbatim
 * offset  size  field
 * 0       4     magic "OPCM"
 * 4       2     version, currently 1                    (u16 LE)
 * 6       2     channel count N, 1..=OMNI_PCM_MAX_CHANNELS  (u16 LE)
 * 8       4     sample rate in Hz                       (u32 LE)
 * 12      1     sample encoding, see OmniphonyPcmEncoding
 * 13      1     reserved, must be zero
 * 14      N     one channel label per channel, interleave order
 * \endverbatim
 *
 * No length field follows the header: the stream runs until the bridge is
 * reset, which returns it to awaiting a fresh header. That is also how a format
 * change is announced - reset, then describe the new geometry.
 *
 * These values are the bridge's ABI. They mirror `RChannelLabel` in
 * omniphony-renderer/bridge_api and the parser in pcm_bridge/src/header.rs;
 * neither side may be changed alone.
 */

//! \brief How the samples following the header are encoded.
enum class OmniphonyPcmEncoding : uint8_t
{
  /*!
   * \brief Signed 24-bit scaled into an int32, the renderer's own convention.
   *
   * Passed through untouched by the bridge.
   */
  I32Scaled24 = 0,
  //! \brief 32-bit float, nominally -1.0 to 1.0. The bridge scales it without
  //! clamping, so what lies above 0 dBFS reaches the renderer's level intact.
  Float32 = 1,
};

//! \brief Bytes before the label array.
constexpr size_t OMNI_PCM_FIXED_HEADER_LEN = 14;

//! \brief The only header version the bridge understands.
constexpr uint16_t OMNI_PCM_VERSION = 1;

//! \brief Upper bound on the channel count, matching the bridge's own.
constexpr int OMNI_PCM_MAX_CHANNELS = 64;

/*!
 * \brief Label every channel of an FFmpeg native channel-layout mask.
 *
 * The bits of a native mask are the interleave order - FFmpeg orders channels
 * by ascending bit - so walking the mask upwards and emitting one label per set
 * bit produces exactly the order the samples arrive in. Nothing is sorted and
 * nothing is inferred from the count.
 *
 * \param mask      the layout mask, valid only for AV_CHANNEL_ORDER_NATIVE
 * \param channels  the decoded channel count, which must equal the number of
 *                  set bits - a disagreement means the mask does not describe
 *                  this stream and nothing can be said about its channels
 * \param labels    filled with \p channels label bytes on success, untouched
 *                  otherwise
 * \return false if the count disagrees, the count is out of range, or any set
 *         bit names a position the renderer cannot place. Every failure is a
 *         reason to decode the stream the ordinary way instead: a channel put
 *         in the wrong place is worse than one not spatialised at all.
 */
bool OmniphonyPcmChannelLabels(uint64_t mask, int channels, std::vector<uint8_t>& labels);

/*!
 * \brief Name the labelled channels, for the player process screen.
 *
 * Produces the abbreviations Kodi already uses everywhere else -
 * `FL, FR, FC, LFE, SL, SR` - in the order \p labels carries, which is the
 * order the samples interleave. Reading the labels rather than the mask is
 * deliberate: this row says what the renderer was handed, so it should be built
 * from the same bytes the renderer was handed.
 *
 * The names match CAEChannelInfo::GetChName wherever both know a position, and
 * the separator matches its operator std::string(). They are spelled out here
 * rather than borrowed so this component keeps its one dependency, FFmpeg's
 * channel constants; the renderer names five positions Kodi's enum does not
 * (the wides, the surround-directs and the second LFE), so a translation table
 * would have been needed in either direction.
 *
 * One channel of front centre is "Mono", because that is what a listener calls
 * it. Any other single channel is named, since a lone LFE or a lone surround is
 * not mono, it is one channel of something.
 *
 * \return the description, or empty if \p labels is empty or names a position
 *         that does not exist - neither of which this codebase can produce, the
 *         labels having come from OmniphonyPcmChannelLabels.
 */
std::string OmniphonyPcmDescribe(const std::vector<uint8_t>& labels);

//! \brief How much quieter Kodi's own stereo fold is when it normalises.
//! Measured for 5.1 to stereo against the unnormalised fold of the same
//! material: 0.0999 against 0.2997 RMS, a factor of three.
constexpr double OMNI_NORMALIZED_DOWNMIX_DB = 9.5;

/*!
 * \brief The channel count the binaural level setting is calibrated against.
 *
 * Six, so that 5.1 - the layout the -3 dB default was chosen against, and the
 * one every profile in the field holds a number for - corrects by nothing. That
 * anchoring is the whole reason the summing term could be added at all: it
 * leaves 5.1 sounding exactly as it did and migrates nobody.
 *
 * \note The shape of the summing term is theory, not measurement. Incoherent
 * summing predicts that N equal sources sit 10*log10(N) dB above one, so
 * correcting a layout back to the reference asks for 10*log10(6/N), which is
 * what OmniphonyPcmLevelMatch computes. A measurement found +4.1 dB for six
 * noise channels, close to the predicted 4.8 - but only +3.2 dB on a real 5.1
 * film balance, because a film's surrounds sit well below its fronts and the
 * effective width is smaller than the channel count.
 *
 * Below the reference the ratio turns into a boost, which is extrapolation
 * rather than measurement, and a check by ear went against it: there the term
 * is not applied at all. What is applied is the half that was measured -
 * layouts at and above 5.1, where the ratio is an attenuation rather than a
 * boost. Retuning that half is still this one
 * constant, and moving it moves 5.1, which is the layout every profile in the
 * field holds a number for.
 */
constexpr double OMNI_MATCH_REFERENCE_CHANNELS = 6.0;

//! \brief The two corrections that keep one level setting meaning one loudness.
struct OmniphonyLevelMatch
{
  //! \brief dB to subtract, so that this layout sits where the reference one
  //! does. Named for Kodi's normalised fold, which is where the number came
  //! from and what it still matches on any layout Kodi folds; a layout Kodi
  //! leaves alone takes it too, because one level control has to mean one
  //! loudness across a library - see OmniphonyPcmLevelMatch.
  double downmixDb;
  //! \brief dB to add for a layout wider than the reference, which sums more
  //! sources into the two ears than the setting was calibrated against. Never
  //! positive: narrower layouts are not boosted - see
  //! OMNI_MATCH_REFERENCE_CHANNELS.
  double summingDb;
};

/*!
 * \brief Derive both corrections from the stream's own layout.
 *
 * This lives beside the wire format rather than in the codec because it is the
 * same knowledge - what the channels of this stream are - and because keeping
 * it free of Kodi's headers is what lets it be tested. Nothing here reaches the
 * renderer; the result is a gain the codec writes into the engine's config.
 *
 * \param mask      the FFmpeg native layout mask, or 0 if only a count is known
 * \param channels  the source channel count; 0 or less means nothing is known,
 *                  and the reference layout's answer is returned so that a
 *                  stream we cannot measure behaves as 5.1 always has
 *
 * \note Whether Kodi's own fold sums anything into the front pair still decides
 * which branch a layout takes, because it is what tells a real fold from a
 * pass-through: a source carrying only front left, front right and the
 * low-frequency channels has nothing to sum, since swresample mixes LFE into
 * the front pair only when a positive lfe_mix_level is set and
 * CActiveAEResampleFFMPEG sets it only when the sublevel it is given is above
 * zero. So stereo, mono and 2.1 are pass-throughs and a centre or a surround
 * makes the fold real. The test picks the branch, not whether a correction is
 * made: a pass-through is given the reference correction outright, having no
 * width of its own to scale by.
 *
 * \note The 9.5 dB itself is measured for 5.1 only. Narrower folds - quad, 3.0 -
 * normalise by less, so they are corrected by more than they should be.
 */
OmniphonyLevelMatch OmniphonyPcmLevelMatch(uint64_t mask, int channels);

/*!
 * \brief Build the header that introduces a stream of \p labels channels.
 *
 * Returns an empty vector if \p labels is empty or longer than the bridge
 * accepts, or if \p sampleRate is zero - each of which the bridge would refuse.
 */
std::vector<uint8_t> OmniphonyPcmHeader(const std::vector<uint8_t>& labels,
                                        uint32_t sampleRate,
                                        OmniphonyPcmEncoding encoding);
