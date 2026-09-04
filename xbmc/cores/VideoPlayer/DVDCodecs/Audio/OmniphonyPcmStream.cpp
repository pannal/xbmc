/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyPcmStream.h"

extern "C"
{
#include <libavcodec/codec_id.h>
#include <libavutil/channel_layout.h>
}

#include <algorithm>
#include <cmath>

namespace
{
//! \brief The label values the renderer's bridge_api defines for fixed speaker
//! positions. Kept as literals rather than an enum of our own: they cross a
//! process and a language boundary, so the numbers themselves are the contract.
enum OmniphonyLabel : uint8_t
{
  OMNI_L = 0,
  OMNI_R = 1,
  OMNI_C = 2,
  OMNI_LFE = 3,
  OMNI_LS = 4, //!< side left
  OMNI_RS = 5, //!< side right
  OMNI_TFL = 6,
  OMNI_TFR = 7,
  OMNI_TSL = 8,
  OMNI_TSR = 9,
  OMNI_TBL = 10,
  OMNI_TBR = 11,
  OMNI_LSC = 12, //!< front left of centre
  OMNI_RSC = 13, //!< front right of centre
  OMNI_LB = 14, //!< back left
  OMNI_RB = 15, //!< back right
  OMNI_CB = 16, //!< back centre
  OMNI_TC = 17,
  OMNI_LSD = 18, //!< surround direct left
  OMNI_RSD = 19, //!< surround direct right
  OMNI_LW = 20, //!< wide left
  OMNI_RW = 21, //!< wide right
  OMNI_TFC = 22,
  OMNI_LFE2 = 23,
};

/*!
 * \brief One FFmpeg channel bit and the renderer position it is.
 *
 * Every label the renderer defines for a fixed speaker is reachable from a
 * mask bit, so the only unmappable positions are FFmpeg's own: TOP_BACK_CENTER,
 * which the renderer does not name; STEREO_LEFT/RIGHT, which mark a downmix
 * rather than a speaker; and the BOTTOM_* family. A stream carrying one of
 * those falls back to ordinary decoding rather than being placed approximately.
 */
struct LabelMapping
{
  uint64_t bit;
  uint8_t label;
};

constexpr LabelMapping MAPPINGS[] = {
    {AV_CH_FRONT_LEFT, OMNI_L},
    {AV_CH_FRONT_RIGHT, OMNI_R},
    {AV_CH_FRONT_CENTER, OMNI_C},
    {AV_CH_LOW_FREQUENCY, OMNI_LFE},
    {AV_CH_BACK_LEFT, OMNI_LB},
    {AV_CH_BACK_RIGHT, OMNI_RB},
    {AV_CH_FRONT_LEFT_OF_CENTER, OMNI_LSC},
    {AV_CH_FRONT_RIGHT_OF_CENTER, OMNI_RSC},
    {AV_CH_BACK_CENTER, OMNI_CB},
    {AV_CH_SIDE_LEFT, OMNI_LS},
    {AV_CH_SIDE_RIGHT, OMNI_RS},
    {AV_CH_TOP_CENTER, OMNI_TC},
    {AV_CH_TOP_FRONT_LEFT, OMNI_TFL},
    {AV_CH_TOP_FRONT_CENTER, OMNI_TFC},
    {AV_CH_TOP_FRONT_RIGHT, OMNI_TFR},
    {AV_CH_TOP_BACK_LEFT, OMNI_TBL},
    // AV_CH_TOP_BACK_CENTER has no counterpart and is deliberately absent.
    {AV_CH_TOP_BACK_RIGHT, OMNI_TBR},
    {AV_CH_WIDE_LEFT, OMNI_LW},
    {AV_CH_WIDE_RIGHT, OMNI_RW},
    {AV_CH_SURROUND_DIRECT_LEFT, OMNI_LSD},
    {AV_CH_SURROUND_DIRECT_RIGHT, OMNI_RSD},
    {AV_CH_LOW_FREQUENCY_2, OMNI_LFE2},
    {AV_CH_TOP_SIDE_LEFT, OMNI_TSL},
    {AV_CH_TOP_SIDE_RIGHT, OMNI_TSR},
};

/*!
 * \brief What each label is called on screen, indexed by the label itself.
 *
 * Kodi's own abbreviations wherever its channel enum has the position, so a
 * listener reading "FL, FR, FC, LFE, SL, SR" here sees the same words the
 * audio settings and the rest of the process screen use. The five the enum
 * does not name - the wides, the surround-directs and the second LFE - follow
 * the same shape.
 */
constexpr const char* LABEL_NAMES[] = {
    "FL", // OMNI_L
    "FR", // OMNI_R
    "FC", // OMNI_C
    "LFE", // OMNI_LFE
    "SL", // OMNI_LS
    "SR", // OMNI_RS
    "TFL", // OMNI_TFL
    "TFR", // OMNI_TFR
    "TSL", // OMNI_TSL
    "TSR", // OMNI_TSR
    "TBL", // OMNI_TBL
    "TBR", // OMNI_TBR
    "FLOC", // OMNI_LSC
    "FROC", // OMNI_RSC
    "BL", // OMNI_LB
    "BR", // OMNI_RB
    "BC", // OMNI_CB
    "TC", // OMNI_TC
    "SDL", // OMNI_LSD
    "SDR", // OMNI_RSD
    "WL", // OMNI_LW
    "WR", // OMNI_RW
    "TFC", // OMNI_TFC
    "LFE2", // OMNI_LFE2
};

static_assert(sizeof(LABEL_NAMES) / sizeof(LABEL_NAMES[0]) == OMNI_LFE2 + 1,
              "every label the wire format can carry needs a name");

//! \brief The renderer position for one mask bit, or -1 if it has none.
int LabelForBit(uint64_t bit)
{
  for (const auto& m : MAPPINGS)
  {
    if (m.bit == bit)
      return m.label;
  }
  return -1;
}

void PushU16(std::vector<uint8_t>& out, uint16_t v)
{
  out.push_back(static_cast<uint8_t>(v & 0xFF));
  out.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

void PushU32(std::vector<uint8_t>& out, uint32_t v)
{
  out.push_back(static_cast<uint8_t>(v & 0xFF));
  out.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
  out.push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
  out.push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}
} // namespace

const char* OmniphonyPcmCodecId(int codecId)
{
  switch (codecId)
  {
    case AV_CODEC_ID_AC3:
      return "ac3";
    case AV_CODEC_ID_EAC3:
      return "eac3";
    case AV_CODEC_ID_TRUEHD:
      return "truehd";
    case AV_CODEC_ID_DTS:
      return "dts";
    default:
      return "pcm";
  }
}

bool OmniphonyPcmChannelLabels(uint64_t mask, int channels, std::vector<uint8_t>& labels)
{
  if (channels <= 0 || channels > OMNI_PCM_MAX_CHANNELS)
    return false;

  // A mask whose population count differs from the decoded channel count is not
  // describing this stream. That is exactly the case CDVDAudioCodecFFmpeg's own
  // BuildChannelMap papers over by falling back to a default layout, and a
  // default layout is a guess at where the sound is.
  std::vector<uint8_t> mapped;
  mapped.reserve(static_cast<size_t>(channels));

  // Ascending bit order is interleave order for a native layout, so this walk
  // produces the labels in the order the samples arrive.
  for (int bit = 0; bit < 64; ++bit)
  {
    const uint64_t flag = static_cast<uint64_t>(1) << bit;
    if (!(mask & flag))
      continue;

    const int label = LabelForBit(flag);
    if (label < 0)
      return false;

    if (mapped.size() == static_cast<size_t>(channels))
      return false; // more set bits than channels; the mask is not this stream's

    mapped.push_back(static_cast<uint8_t>(label));
  }

  if (mapped.size() != static_cast<size_t>(channels))
    return false;

  labels.swap(mapped);
  return true;
}

std::string OmniphonyPcmDescribe(const std::vector<uint8_t>& labels)
{
  if (labels.empty())
    return {};

  // One front-centre channel is what everyone calls mono. A lone LFE or a lone
  // surround is not, so the test is the label rather than the count.
  if (labels.size() == 1 && labels[0] == OMNI_C)
    return "Mono";

  std::string out;
  for (const uint8_t label : labels)
  {
    if (label >= sizeof(LABEL_NAMES) / sizeof(LABEL_NAMES[0]))
      return {};
    if (!out.empty())
      out += ", ";
    out += LABEL_NAMES[label];
  }
  return out;
}

OmniphonyLevelMatch OmniphonyPcmLevelMatch(uint64_t mask, int channels)
{
  OmniphonyLevelMatch match{OMNI_NORMALIZED_DOWNMIX_DB, 0.0};
  if (channels <= 0)
    return match;

  // Everything a fold has to sum into the front pair. The front pair itself
  // passes through, and the low-frequency channels are dropped rather than
  // summed unless a listener has asked for them - see the header.
  constexpr uint64_t PASSES_THROUGH =
      AV_CH_FRONT_LEFT | AV_CH_FRONT_RIGHT | AV_CH_LOW_FREQUENCY | AV_CH_LOW_FREQUENCY_2;

  // Two conditions, and both are needed.
  //
  // There must be more channels than the two being folded into, which is Kodi's
  // own test for whether it is folding at all - a mono source is widened to the
  // pair rather than folded into it, and nothing is summed with anything.
  //
  // And what those extra channels are has to matter, which a count cannot say:
  // 2.1 is three channels but its third is the LFE, dropped rather than summed,
  // so the front pair comes through as it went in. With no mask there is only
  // the count, and a count above two at least means something is folded, even
  // if not what.
  const bool folds = channels > 2 && (mask ? (mask & ~PASSES_THROUGH) != 0 : true);

  /*
   * A layout Kodi would not fold takes the reference correction unchanged,
   * which is what `match` already holds.
   *
   * This half of the rule was settled by listening. Following Kodi
   * exactly - no normalisation where Kodi normalises nothing - is defensible
   * per stream and indefensible across a library: Kodi's own stereo output is
   * 9.5 dB hotter than its own 5.1 fold, so inheriting that put a stereo album
   * 14 dB above a 5.1 film at one setting, and mono 17 above it. One level
   * control has to mean one loudness or it is not a level control, so an
   * unfolded layout is brought to where the reference sits instead.
   *
   * What it costs is that turning binaural on makes a stereo track quieter
   * than Kodi plays it with binaural off. That is the honest price of
   * the choice, and it is the direction a listener can undo with the setting.
   */
  if (!folds)
    return match;

  match.downmixDb = OMNI_NORMALIZED_DOWNMIX_DB;

  /*
   * Never a boost, only an attenuation for layouts wider than the reference.
   *
   * At or above the reference the ratio applies as it stands, so 5.1 corrects
   * by nothing and 7.1 and 7.1.4 are attenuated by it. Below it the ratio
   * would open up: a three or four channel fold would come out 1.8 to 3 dB
   * above 5.1 on the theory that fewer sources sum to less. The
   * theory is sound and the number is not - the header records 4.1 dB measured
   * against 4.8 predicted on noise, and only 3.2 dB on a real 5.1 balance -
   * and by ear the boost is plainly wrong. Clamping it keeps the part that was
   * measured and drops the part that was extrapolated.
   */
  match.summingDb = std::min(0.0, 10.0 * std::log10(OMNI_MATCH_REFERENCE_CHANNELS / channels));
  return match;
}

std::vector<uint8_t> OmniphonyPcmHeader(const std::vector<uint8_t>& labels,
                                        uint32_t sampleRate,
                                        OmniphonyPcmEncoding encoding)
{
  if (labels.empty() || labels.size() > static_cast<size_t>(OMNI_PCM_MAX_CHANNELS) ||
      sampleRate == 0)
    return {};

  std::vector<uint8_t> header;
  header.reserve(OMNI_PCM_FIXED_HEADER_LEN + labels.size());

  header.push_back('O');
  header.push_back('P');
  header.push_back('C');
  header.push_back('M');
  PushU16(header, OMNI_PCM_VERSION);
  PushU16(header, static_cast<uint16_t>(labels.size()));
  PushU32(header, sampleRate);
  header.push_back(static_cast<uint8_t>(encoding));
  header.push_back(0); // reserved
  header.insert(header.end(), labels.begin(), labels.end());

  return header;
}
