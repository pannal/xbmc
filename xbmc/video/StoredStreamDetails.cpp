/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "StoredStreamDetails.h"

#include "utils/StringUtils.h"

namespace VIDEO
{
namespace
{
/*! The HDR type the player reports for one the scan may have refined. The scan probes a frame of
 a plain HDR10 stream and records HDR10+ when it finds the dynamic metadata; the player only
 reads the container, which declares HDR10 either way.
 */
std::string GetPlainHdrType(const std::string& hdrType)
{
  if (StringUtils::EqualsNoCase(hdrType, "hdr10plus"))
    return "hdr10";

  return hdrType;
}

//! Whether a stored audio row sits where the stream is: the same track, whatever it is named now.
bool LinesUp(const AudioStreamRow& stored, const AudioStreamRow& stream)
{
  return StringUtils::EqualsNoCase(GetPlainAudioCodec(stored.codec),
                                   GetPlainAudioCodec(stream.codec)) &&
         stored.channels == stream.channels &&
         StringUtils::EqualsNoCase(stored.language, stream.language);
}

//! Whether what another build recorded about a stored row still describes the stream.
bool StillDescribes(const AudioStreamRow& stored, const AudioStreamRow& stream)
{
  return StringUtils::EqualsNoCase(stored.codec, stream.codec) ||
         StringUtils::EqualsNoCase(stored.codec, GetPlainAudioCodec(stream.codec));
}
} // unnamed namespace

const std::vector<SplitAudioCodec>& GetSplitAudioCodecs()
{
  static const std::vector<SplitAudioCodec> codecs = {{"truehd", "Dolby Atmos", "truehd_atmos"},
                                                      {"eac3", "Dolby Atmos", "eac3_ddp_atmos"},
                                                      {"dtshd_ma", "DTS:X", "dtshd_ma_x"},
                                                      {"dtshd_ma", "DTS:X IMAX", "dtshd_ma_x_imax"},
                                                      {"dca", "DTS-ES", "dts_es"},
                                                      {"dca", "DTS 96/24", "dts_96_24"},
                                                      {"dca", "DTS Express", "dts_express"},
                                                      {"aac", "AAC-LC", "aac_lc"},
                                                      {"aac", "HE-AAC", "he_aac"},
                                                      {"aac", "HE-AAC v2", "he_aac_v2"},
                                                      {"aac", "AAC-SSR", "aac_ssr"},
                                                      {"aac", "AAC-LTP", "aac_ltp"}};

  return codecs;
}

std::string GetPlainAudioCodec(const std::string& codec)
{
  for (const auto& split : GetSplitAudioCodecs())
  {
    if (StringUtils::EqualsNoCase(codec, split.extended))
      return split.plain;
  }

  return codec;
}

std::vector<AudioStreamRow> PlanAudioRows(const std::vector<AudioStreamRow>& stored,
                                          const std::vector<AudioStreamRow>& streams)
{
  std::vector<AudioStreamRow> rows;
  rows.reserve(streams.size());
  for (const auto& stream : streams)
  {
    AudioStreamRow row = stream;
    row.otherColumns.clear();
    rows.emplace_back(std::move(row));
  }

  // The table has no key, so the stored rows can only be told apart by their order. What they
  // carry follows them only while every one of them still sits where its stream does.
  if (stored.size() != streams.size())
    return rows;

  for (size_t i = 0; i < streams.size(); ++i)
  {
    if (!LinesUp(stored[i], streams[i]))
      return rows;
  }

  for (size_t i = 0; i < streams.size(); ++i)
  {
    if (StillDescribes(stored[i], streams[i]))
      rows[i].otherColumns = stored[i].otherColumns;
  }

  return rows;
}

HdrFields CompleteHdrFields(const std::vector<StoredVideoStream>& stored,
                            const std::string& codec,
                            int width,
                            int height,
                            HdrFields reported)
{
  const StoredVideoStream* match = nullptr;
  for (const auto& candidate : stored)
  {
    if (!StringUtils::EqualsNoCase(candidate.codec, codec) || candidate.width != width ||
        candidate.height != height ||
        !StringUtils::EqualsNoCase(GetPlainHdrType(candidate.hdrType),
                                   GetPlainHdrType(reported.hdrType)))
    {
      continue;
    }

    if (match &&
        (!StringUtils::EqualsNoCase(match->hdrType, candidate.hdrType) ||
         match->hdrTypeAlt != candidate.hdrTypeAlt || match->dvProfile != candidate.dvProfile))
    {
      return reported; // they disagree, and nothing says which one this stream is
    }

    match = &candidate;
  }

  if (!match)
    return reported;

  // A refinement the scan found stands over the plainer type reported; a type reported as
  // something fuller than what was stored is left as reported.
  if (StringUtils::EqualsNoCase(reported.hdrType, GetPlainHdrType(match->hdrType)))
    reported.hdrType = match->hdrType;
  if (reported.hdrTypeAlt.empty())
    reported.hdrTypeAlt = match->hdrTypeAlt;
  if (reported.dvProfile.empty())
    reported.dvProfile = match->dvProfile;

  return reported;
}

bool IsPlainColumnName(const std::string& name)
{
  if (name.empty() || (name.front() >= '0' && name.front() <= '9'))
    return false;

  for (const char c : name)
  {
    const bool letter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
    const bool digit = c >= '0' && c <= '9';
    if (!letter && !digit && c != '_')
      return false;
  }

  return true;
}
} // namespace VIDEO
