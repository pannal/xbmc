/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "video/StoredStreamDetails.h"

#include <set>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

using namespace VIDEO;

namespace
{
using OtherColumns = std::vector<std::pair<std::string, std::string>>;

// What a build that keeps codec and profile apart leaves on an audio row, as the setter reads it.
const OtherColumns objectColumns = {{"strAudioProfile", "'DTS-ES'"}, {"iAudioObjects", "'0'"}};

AudioStreamRow Audio(const std::string& codec,
                     int channels,
                     const std::string& language,
                     const OtherColumns& otherColumns = {})
{
  return {codec, channels, language, otherColumns};
}

void ExpectRow(const AudioStreamRow& row,
               const std::string& codec,
               int channels,
               const std::string& language,
               const OtherColumns& otherColumns)
{
  EXPECT_EQ(codec, row.codec);
  EXPECT_EQ(channels, row.channels);
  EXPECT_EQ(language, row.language);
  EXPECT_EQ(otherColumns, row.otherColumns);
}

void ExpectHdr(const HdrFields& hdr,
               const std::string& hdrType,
               const std::string& hdrTypeAlt,
               const std::string& dvProfile)
{
  EXPECT_EQ(hdrType, hdr.hdrType);
  EXPECT_EQ(hdrTypeAlt, hdr.hdrTypeAlt);
  EXPECT_EQ(dvProfile, hdr.dvProfile);
}
} // unnamed namespace

TEST(TestStoredStreamDetails, SplitCodecsCanBeRestored)
{
  // The startup migration matches on the plain name and the profile and writes the extended name,
  // so each pair must lead to one name and each name must be reached from one pair only.
  std::set<std::pair<std::string, std::string>> pairs;
  std::set<std::string> extendedNames;
  for (const auto& codec : GetSplitAudioCodecs())
  {
    EXPECT_TRUE(pairs.emplace(codec.plain, codec.profile).second) << codec.extended;
    EXPECT_TRUE(extendedNames.emplace(codec.extended).second) << codec.extended;
  }
  EXPECT_EQ(12u, extendedNames.size());
}

TEST(TestStoredStreamDetails, GetPlainAudioCodec)
{
  for (const auto& codec : GetSplitAudioCodecs())
    EXPECT_EQ(codec.plain, GetPlainAudioCodec(codec.extended)) << codec.extended;

  EXPECT_EQ("dca", GetPlainAudioCodec("DTS_ES"));
  EXPECT_EQ("dca", GetPlainAudioCodec("dca"));
  EXPECT_EQ("truehd", GetPlainAudioCodec("truehd"));
  EXPECT_EQ("dtshd_ma", GetPlainAudioCodec("dtshd_ma"));
  EXPECT_EQ("ac3", GetPlainAudioCodec("ac3"));
  EXPECT_EQ("", GetPlainAudioCodec(""));
}

TEST(TestStoredStreamDetails, NewlyIdentifiedCodecIsWritten)
{
  // A library written before a codec could be told apart holds only the plain name, and holds no
  // other build's columns. The name the file is identified as now must replace it.
  for (const auto& codec : GetSplitAudioCodecs())
  {
    SCOPED_TRACE(codec.extended);
    const auto rows =
        PlanAudioRows({Audio(codec.plain, 6, "eng")}, {Audio(codec.extended, 6, "eng")});
    ASSERT_EQ(1u, rows.size());
    ExpectRow(rows[0], codec.extended, 6, "eng", {});
  }
}

TEST(TestStoredStreamDetails, NewlyIdentifiedCodecKeepsOtherColumns)
{
  // The same, where another build left columns of its own on the row: they stay with it.
  for (const auto& codec : GetSplitAudioCodecs())
  {
    SCOPED_TRACE(codec.extended);
    const auto rows = PlanAudioRows({Audio(codec.plain, 6, "eng", objectColumns)},
                                    {Audio(codec.extended, 6, "eng")});
    ASSERT_EQ(1u, rows.size());
    ExpectRow(rows[0], codec.extended, 6, "eng", objectColumns);
  }
}

TEST(TestStoredStreamDetails, UnchangedCodecKeepsOtherColumns)
{
  const auto rows = PlanAudioRows(
      {Audio("truehd_atmos", 8, "eng", objectColumns), Audio("ac3", 6, "fre", objectColumns)},
      {Audio("truehd_atmos", 8, "eng"), Audio("ac3", 6, "fre")});
  ASSERT_EQ(2u, rows.size());
  ExpectRow(rows[0], "truehd_atmos", 8, "eng", objectColumns);
  ExpectRow(rows[1], "ac3", 6, "fre", objectColumns);
}

TEST(TestStoredStreamDetails, PlainerCodecIsWrittenWithoutOtherColumns)
{
  // A file replaced by one that no longer carries the extension: what was recorded about the old
  // stream does not describe the new one, and would lead the migration to rename it back.
  for (const auto& codec : GetSplitAudioCodecs())
  {
    SCOPED_TRACE(codec.extended);
    const auto rows = PlanAudioRows({Audio(codec.extended, 6, "eng", objectColumns)},
                                    {Audio(codec.plain, 6, "eng")});
    ASSERT_EQ(1u, rows.size());
    ExpectRow(rows[0], codec.plain, 6, "eng", {});
  }
}

TEST(TestStoredStreamDetails, DifferentProfileIsWrittenWithoutOtherColumns)
{
  const std::vector<std::pair<std::string, std::string>> transitions = {
      {"dts_es", "dts_96_24"},
      {"dts_96_24", "dts_express"},
      {"dtshd_ma_x", "dtshd_ma_x_imax"},
      {"he_aac", "aac_lc"}};
  for (const auto& [from, to] : transitions)
  {
    SCOPED_TRACE(from + " -> " + to);
    const auto rows = PlanAudioRows({Audio(from, 6, "eng", objectColumns)}, {Audio(to, 6, "eng")});
    ASSERT_EQ(1u, rows.size());
    ExpectRow(rows[0], to, 6, "eng", {});
  }
}

TEST(TestStoredStreamDetails, EachTrackIsJudgedOnItsOwn)
{
  const auto rows = PlanAudioRows(
      {Audio("dca", 6, "eng", objectColumns), Audio("truehd_atmos", 8, "eng", objectColumns),
       Audio("aac", 2, "jpn", objectColumns)},
      {Audio("dts_es", 6, "eng"), Audio("truehd", 8, "eng"), Audio("aac", 2, "jpn")});
  ASSERT_EQ(3u, rows.size());
  ExpectRow(rows[0], "dts_es", 6, "eng", objectColumns);
  ExpectRow(rows[1], "truehd", 8, "eng", {});
  ExpectRow(rows[2], "aac", 2, "jpn", objectColumns);
}

TEST(TestStoredStreamDetails, IdenticalTracksKeepTheirOwnColumns)
{
  const OtherColumns first = {{"iAudioObjects", "'11'"}};
  const OtherColumns second = {{"iAudioObjects", "'15'"}};
  const auto rows =
      PlanAudioRows({Audio("truehd", 8, "eng", first), Audio("truehd", 8, "eng", second)},
                    {Audio("truehd_atmos", 8, "eng"), Audio("truehd_atmos", 8, "eng")});
  ASSERT_EQ(2u, rows.size());
  ExpectRow(rows[0], "truehd_atmos", 8, "eng", first);
  ExpectRow(rows[1], "truehd_atmos", 8, "eng", second);
}

TEST(TestStoredStreamDetails, ChangedLayoutKeepsNoOtherColumns)
{
  // The table has no key, so once the rows no longer line up nothing says which is which.
  const std::vector<AudioStreamRow> stored = {Audio("dca", 6, "eng", objectColumns),
                                              Audio("ac3", 2, "fre", objectColumns)};
  const std::vector<std::pair<std::string, std::vector<AudioStreamRow>>> layouts = {
      {"track added", {Audio("dts_es", 6, "eng"), Audio("ac3", 2, "fre"), Audio("ac3", 2, "ger")}},
      {"track removed", {Audio("dts_es", 6, "eng")}},
      {"tracks swapped", {Audio("ac3", 2, "fre"), Audio("dts_es", 6, "eng")}},
      {"channels changed", {Audio("dts_es", 8, "eng"), Audio("ac3", 2, "fre")}},
      {"language changed", {Audio("dts_es", 6, "ger"), Audio("ac3", 2, "fre")}},
      {"codec changed", {Audio("dts_es", 6, "eng"), Audio("eac3", 2, "fre")}}};
  for (const auto& [change, streams] : layouts)
  {
    SCOPED_TRACE(change);
    const auto rows = PlanAudioRows(stored, streams);
    ASSERT_EQ(streams.size(), rows.size());
    for (size_t i = 0; i < streams.size(); ++i)
      ExpectRow(rows[i], streams[i].codec, streams[i].channels, streams[i].language, {});
  }
}

TEST(TestStoredStreamDetails, LanguageAndCodecAreComparedWithoutCase)
{
  const auto rows =
      PlanAudioRows({Audio("DCA", 6, "ENG", objectColumns)}, {Audio("dts_es", 6, "eng")});
  ASSERT_EQ(1u, rows.size());
  ExpectRow(rows[0], "dts_es", 6, "eng", objectColumns);
}

TEST(TestStoredStreamDetails, OnlyStoredColumnsAreCarried)
{
  EXPECT_TRUE(PlanAudioRows({}, {}).empty());

  const auto rows = PlanAudioRows({}, {Audio("dts_es", 6, "eng", objectColumns)});
  ASSERT_EQ(1u, rows.size());
  ExpectRow(rows[0], "dts_es", 6, "eng", {});
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsFillsWhatThePlayerCannotRead)
{
  const std::vector<StoredVideoStream> stored = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"}};
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision",
            "hdr10", "8.1");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsKeepsHdr10Plus)
{
  // The scan finds HDR10+ in a frame; the player sees only the HDR10 the container declares.
  const std::vector<StoredVideoStream> stored = {{"hevc", 3840, 2160, "hdr10plus", "", ""}};
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"hdr10", "", ""}), "hdr10plus", "", "");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsKeepsWhatWasReported)
{
  const std::vector<StoredVideoStream> dolbyVision = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"}};
  ExpectHdr(CompleteHdrFields(dolbyVision, "hevc", 3840, 2160, {"dolbyvision", "hlg", "8.4"}),
            "dolbyvision", "hlg", "8.4");

  const std::vector<StoredVideoStream> hdr10 = {{"hevc", 3840, 2160, "hdr10", "", ""}};
  ExpectHdr(CompleteHdrFields(hdr10, "hevc", 3840, 2160, {"hdr10plus", "", ""}), "hdr10plus", "",
            "");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsTellsSameSizeStreamsApartByType)
{
  const std::vector<StoredVideoStream> stored = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"},
      {"hevc", 3840, 2160, "hdr10plus", "", ""}};
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision",
            "hdr10", "8.1");
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"hdr10", "", ""}), "hdr10plus", "", "");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsTakesNothingFromDisagreeingStreams)
{
  const std::vector<StoredVideoStream> profiles = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"},
      {"hevc", 3840, 2160, "dolbyvision", "", "7.6"}};
  ExpectHdr(CompleteHdrFields(profiles, "hevc", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision",
            "", "");

  // One of two otherwise identical streams carries HDR10+: the player cannot say which it plays.
  const std::vector<StoredVideoStream> types = {{"hevc", 3840, 2160, "hdr10plus", "", ""},
                                                {"hevc", 3840, 2160, "hdr10", "", ""}};
  ExpectHdr(CompleteHdrFields(types, "hevc", 3840, 2160, {"hdr10", "", ""}), "hdr10", "", "");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsTakesFromAgreeingStreams)
{
  const std::vector<StoredVideoStream> stored = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"},
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"}};
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision",
            "hdr10", "8.1");
}

TEST(TestStoredStreamDetails, CompleteHdrFieldsNeedsTheSameStream)
{
  const std::vector<StoredVideoStream> stored = {
      {"hevc", 3840, 2160, "dolbyvision", "hdr10", "8.1"}};
  ExpectHdr(CompleteHdrFields(stored, "hevc", 1920, 1080, {"dolbyvision", "", ""}), "dolbyvision",
            "", "");
  ExpectHdr(CompleteHdrFields(stored, "h264", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision",
            "", "");
  ExpectHdr(CompleteHdrFields(stored, "hevc", 3840, 2160, {"hdr10", "", ""}), "hdr10", "", "");
  ExpectHdr(CompleteHdrFields({}, "hevc", 3840, 2160, {"dolbyvision", "", ""}), "dolbyvision", "",
            "");
}

TEST(TestStoredStreamDetails, IsPlainColumnName)
{
  EXPECT_TRUE(IsPlainColumnName("strAudioProfile"));
  EXPECT_TRUE(IsPlainColumnName("iAudioObjects"));
  EXPECT_TRUE(IsPlainColumnName("_column_2"));
  EXPECT_FALSE(IsPlainColumnName(""));
  EXPECT_FALSE(IsPlainColumnName("2column"));
  EXPECT_FALSE(IsPlainColumnName("audio profile"));
  EXPECT_FALSE(IsPlainColumnName("profile'"));
  EXPECT_FALSE(IsPlainColumnName("a;b"));
  EXPECT_FALSE(IsPlainColumnName("\xC3\xA9"));
}
