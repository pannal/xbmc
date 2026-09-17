/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "StreamUtils.h"

extern "C"
{
#include <libavcodec/avcodec.h>
#include <libavcodec/defs.h>
}

namespace
{
/*
 * The DTS:X alternate-profile syncwords that carry an object declaration are
 * 0xF14000D0 through 0xF14000D4, and ffmpeg reports the low nibble of whichever
 * one it saw. Those five values are the whole range a declaration can arrive in.
 */
constexpr int DTSX_SYNCWORD_NIBBLE_MIN = 0;
constexpr int DTSX_SYNCWORD_NIBBLE_MAX = 4;
constexpr int DTSX_LEVEL_NIBBLE_MASK = 0x0F;

/*
 * Above the nibble goes what the element's own payload says about the channel
 * sets it carries: 0x10 that the byte naming them was read, 0x20 that the
 * height quartet is among them. These are dca.h's DCA_X_LEVEL_SETS_READ and
 * DCA_X_LEVEL_HEIGHTS, which ffmpeg does not install.
 */
constexpr int DTSX_LEVEL_SETS_READ = 0x10;
constexpr int DTSX_LEVEL_HEIGHTS = 0x20;
constexpr int DTSX_LEVEL_KNOWN_BITS =
    DTSX_LEVEL_NIBBLE_MASK | DTSX_LEVEL_SETS_READ | DTSX_LEVEL_HEIGHTS;

// The heights DTS:X puts over its bed when it puts any: four, top front and top
// back pairs, in every presentation read so far.
constexpr int DTSX_HEIGHT_CHANNELS = 4;
} // unnamed namespace

int StreamUtils::GetCodecPriority(const std::string &codec)
{
  /*
   * Technically flac, truehd, and dtshd_ma are equivalently good as they're all lossless. However,
   * ffmpeg can't decode dtshd_ma losslessy yet.
   */
  if (codec == "truehd_atmos") // Dolby TrueHD with Atmos
    return 11;
  if (codec == "dtshd_ma_x_imax") // DTS:X IMAX Enhanced
    return 10;
  if (codec == "dtshd_ma_x") // DTS:X
    return 9;
  if (codec == "flac") // Lossless FLAC
    return 8;
  if (codec == "truehd") // Dolby TrueHD
    return 7;
  if (codec == "dtshd_ma") // DTS-HD Master Audio (previously known as DTS++)
    return 6;
  if (codec == "dtshd_hra") // DTS-HD High Resolution Audio
    return 5;
  if (codec == "eac3_ddp_atmos") // Dolby Digital Plus with Atmos
    return 4;
  if (codec == "eac3") // Dolby Digital Plus
    return 3;
  if (codec == "dca") // DTS
    return 2;
  if (codec == "ac3") // Dolby Digital
    return 1;
  return 0;
}

std::string StreamUtils::GetCodecName(int codecId, int profile)
{
  std::string codecName;

  if (codecId == AV_CODEC_ID_DTS)
  {
    if (profile == AV_PROFILE_DTS_HD_MA)
      codecName = "dtshd_ma";
    else if (profile == AV_PROFILE_DTS_HD_MA_X)
      codecName = "dtshd_ma_x";
    else if (profile == AV_PROFILE_DTS_HD_MA_X_IMAX)
      codecName = "dtshd_ma_x_imax";
    else if (profile == AV_PROFILE_DTS_HD_HRA)
      codecName = "dtshd_hra";
    else
      codecName = "dca";

    return codecName;
  }

  if (codecId == AV_CODEC_ID_AAC)
  {
    switch (profile)
    {
      case AV_PROFILE_AAC_LOW:
      case AV_PROFILE_MPEG2_AAC_LOW:
        codecName = "aac_lc";
        break;
      case AV_PROFILE_AAC_HE:
      case AV_PROFILE_MPEG2_AAC_HE:
        codecName = "he_aac";
        break;
      case AV_PROFILE_AAC_HE_V2:
        codecName = "he_aac_v2";
        break;
      case AV_PROFILE_AAC_SSR:
        codecName = "aac_ssr";
        break;
      case AV_PROFILE_AAC_LTP:
        codecName = "aac_ltp";
        break;
      default:
        codecName = "aac";
    }
    return codecName;
  }

  if (codecId == AV_CODEC_ID_EAC3 && profile == AV_PROFILE_EAC3_DDP_ATMOS)
    return "eac3_ddp_atmos";

  if (codecId == AV_CODEC_ID_TRUEHD && profile == AV_PROFILE_TRUEHD_ATMOS)
    return "truehd_atmos";

  const AVCodec* codec = avcodec_find_decoder(static_cast<AVCodecID>(codecId));
  if (codec)
    codecName = avcodec_get_name(codec->id);

  return codecName;
}

bool StreamUtils::IsDTSXProfile(int profile)
{
  return profile == AV_PROFILE_DTS_HD_MA_X || profile == AV_PROFILE_DTS_HD_MA_X_IMAX;
}

int StreamUtils::GetDTSXObjectCount(int profile, int level)
{
  // The level says which variant, the profile says of what, and the level means
  // nothing on its own: every codec is free to report whatever it likes there,
  // and an Auro-3D carrier will report its layout in the very same field.
  if (!IsDTSXProfile(profile))
    return -1;

  // Outside that range the stream carried no object declaration - the older
  // 0x02000850 form, or an ffmpeg without this tree's dca_xll patch, which
  // leaves the level at the AV_LEVEL_UNKNOWN it starts at. Nor is a level with
  // bits set that the patch never sets.
  if (level < 0 || (level & ~DTSX_LEVEL_KNOWN_BITS))
    return -1;

  const int nibble = level & DTSX_LEVEL_NIBBLE_MASK;
  if (nibble < DTSX_SYNCWORD_NIBBLE_MIN || nibble > DTSX_SYNCWORD_NIBBLE_MAX)
    return -1;

  // The nibble is the declaration count less one, as transmitted.
  return nibble + 1;
}

int StreamUtils::GetDTSXHeightCount(int profile, int level)
{
  if (!IsDTSXProfile(profile))
    return -1;

  // No object element at all: the 0x02000850 form, very nearly every DTS:X
  // release, whose matrix puts the two height pairs over the bed.
  if (level == AV_LEVEL_UNKNOWN)
    return DTSX_HEIGHT_CHANNELS;

  // An element, but no word on what it carries. The same object count arrives
  // over 7.1.4 and over a 5.1 with no heights at all, so it is left unsaid.
  if (GetDTSXObjectCount(profile, level) < 0 || !(level & DTSX_LEVEL_SETS_READ))
    return -1;

  return (level & DTSX_LEVEL_HEIGHTS) ? DTSX_HEIGHT_CHANNELS : 0;
}
