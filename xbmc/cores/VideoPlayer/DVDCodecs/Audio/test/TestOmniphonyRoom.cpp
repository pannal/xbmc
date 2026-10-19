/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Omniphony/OmniphonyRoom.h"
#include "cores/AudioEngine/Omniphony/OmniphonyTool.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecOmniphony.h"

#include <string>

#include <gtest/gtest.h>

// The lines below are the helper's own, as omniphony-helper.c and the engine's
// orender_sofa_describe write them; the describe lines are what the engine
// printed for the BBC's 7.1.4 room, its prepared room and a free-field set.

TEST(TestOmniphonyRoom, ReadsTheRoomStateAndTheDelayFromAStreamLine)
{
  const std::string line = "stream objects=0 spatial=1 channels=2 rate=48000 hrir=brir "
                           "brir=ready latency=127 source_label= bed=L,R,C,LFE";
  std::string value;
  ASSERT_TRUE(OmniphonyStatusField(line, "hrir", value));
  EXPECT_EQ(value, "brir");
  ASSERT_TRUE(OmniphonyStatusField(line, "brir", value));
  EXPECT_EQ(value, "ready");
  ASSERT_TRUE(OmniphonyStatusField(line, "latency", value));
  EXPECT_EQ(value, "127");
  // Present and empty is not absent: the field is live.
  ASSERT_TRUE(OmniphonyStatusField(line, "source_label", value));
  EXPECT_EQ(value, "");
}

TEST(TestOmniphonyRoom, NeverReadsOneFieldInsideAnother)
{
  // A field absent from the line is not found inside another: "brir=" in
  // "hrir=", nor "hrir=" in "brir=" the other way round.
  const std::string line = "stream objects=3 spatial=1 channels=2 rate=48000 hrir=saf "
                           "source_label= bed=LFE";
  std::string value;
  EXPECT_FALSE(OmniphonyStatusField(line, "brir", value));
  EXPECT_FALSE(OmniphonyStatusField(line, "latency", value));

  const std::string room = "stream objects=0 brir=loading hrir=saf bed=LFE";
  ASSERT_TRUE(OmniphonyStatusField(room, "hrir", value));
  EXPECT_EQ(value, "saf");
}

TEST(TestOmniphonyRoom, TurnsTheDelayIntoDvdTime)
{
  // 127 samples is the room's convolution delay: 2.65 ms at 48 kHz, half of
  // that at 96, where the same 127 samples are half as long.
  EXPECT_NEAR(OmniphonyLatencyUs(127, 48000), 2645.83, 0.01);
  EXPECT_NEAR(OmniphonyLatencyUs(127, 96000), 1322.92, 0.01);
  EXPECT_EQ(OmniphonyLatencyUs(0, 48000), 0.0);
  EXPECT_EQ(OmniphonyLatencyUs(127, 0), 0.0);
}

TEST(TestOmniphonyRoom, NamesWhatIsRendering)
{
  EXPECT_EQ(OmniphonyDescribeHrir("saf"), "Built-in HRIR");
  EXPECT_EQ(OmniphonyDescribeHrir("sofa"), "Custom HRIR");
  EXPECT_EQ(OmniphonyDescribeHrir("brir"), "Custom BRIR");
  // An engine that says something else is not guessed at.
  EXPECT_EQ(OmniphonyDescribeHrir("kemar"), "");
  EXPECT_EQ(OmniphonyDescribeHrir(""), "");

  EXPECT_EQ(OmniphonyDescribeRender("direct"), "Direct");
  EXPECT_EQ(OmniphonyDescribeRender("cascade:12"), "Cascade 12");
  EXPECT_EQ(OmniphonyDescribeRender("room:13"), "Room 13");
  // Speaker output is not this codec's, and a malformed count is no count.
  EXPECT_EQ(OmniphonyDescribeRender("speakers:12"), "");
  EXPECT_EQ(OmniphonyDescribeRender("room:"), "");
  EXPECT_EQ(OmniphonyDescribeRender("room:1x"), "");
  EXPECT_EQ(OmniphonyDescribeRender("direct:2"), "");
  EXPECT_EQ(OmniphonyDescribeRender(""), "");
}

TEST(TestOmniphonyRoom, ReadsWhatBecameOfConfigYaml)
{
  using Status = OmniphonyOverrideAck::Status;
  const std::string open = "open codec=truehd rate=48000 engine=0.13 decode_thread=on";

  EXPECT_EQ(OmniphonyReadOverrideAck(open).status, Status::Absent);
  EXPECT_EQ(OmniphonyReadOverrideAck(open + " override=applied").status, Status::Applied);
  EXPECT_EQ(OmniphonyReadOverrideAck(open + " override=rejected").status, Status::Rejected);
  // A word this build does not know is not taken for any of them.
  EXPECT_EQ(OmniphonyReadOverrideAck(open + " override=later").status, Status::Absent);
}

TEST(TestOmniphonyRoom, QuotesPathsSoNothingInThemIsRead)
{
  EXPECT_EQ(OmniphonyYamlQuote("/storage/.kodi/userdata/omniphony/cache/bbcrdlr_systemG.room"),
            "'/storage/.kodi/userdata/omniphony/cache/bbcrdlr_systemG.room'");
  // Only the quote is special in single quotes, and it is doubled; a
  // backslash, a colon or a hash stay what they are.
  EXPECT_EQ(OmniphonyYamlQuote("/storage/.kodi/userdata/omniphony/cache/it's #1: a\\b.room"),
            "'/storage/.kodi/userdata/omniphony/cache/it''s #1: a\\b.room'");
}

TEST(TestOmniphonyRoom, ARoomRendersInPlaceOfTheHeadModel)
{
  OmniphonyRenderChoices choices;
  choices.hrtf = "/storage/.kodi/userdata/omniphony/cache/kemar.sofa";
  choices.room = "/storage/.kodi/userdata/omniphony/cache/bbcrdlr_systemG.room";
  const std::string yaml = OmniphonyRenderYaml(choices, "/usr/lib/kodi/omniphony/b.so", -3.0);

  EXPECT_NE(yaml.find("    hrir_source: brir\n"), std::string::npos) << yaml;
  EXPECT_NE(
      yaml.find(
          "    brir_sofa_path: '/storage/.kodi/userdata/omniphony/cache/bbcrdlr_systemG.room'\n"),
      std::string::npos)
      << yaml;
  EXPECT_EQ(yaml.find("hrtf_sofa_path"), std::string::npos) << yaml;
  // A room names no layout of its own: the engine builds on its loudspeakers.
  EXPECT_EQ(yaml.find("mode: cascaded"), std::string::npos) << yaml;
  EXPECT_NE(yaml.find("  bridge_path: '/usr/lib/kodi/omniphony/b.so'\n"), std::string::npos)
      << yaml;
}

TEST(TestOmniphonyRoom, WithoutARoomTheHeadModelIsAsBefore)
{
  OmniphonyRenderChoices choices;
  std::string yaml = OmniphonyRenderYaml(choices, "/b.so", -12.5);
  EXPECT_NE(yaml.find("    hrir_source: saf\n"), std::string::npos) << yaml;
  EXPECT_NE(yaml.find("  master_gain: -12.50\n"), std::string::npos) << yaml;
  // Objects are read on the listener's sphere, which the engine applies on
  // the direct path alone.
  EXPECT_NE(yaml.find("    sphere_coordinates: true\n"), std::string::npos) << yaml;
  EXPECT_EQ(yaml.find("brir"), std::string::npos) << yaml;

  choices.hrtf = "/storage/.kodi/userdata/omniphony/cache/kemar.sofa";
  choices.cascade = true;
  yaml = OmniphonyRenderYaml(choices, "/b.so", -3.0);
  EXPECT_NE(yaml.find("    hrir_source: sofa\n"), std::string::npos) << yaml;
  EXPECT_NE(yaml.find("    hrtf_sofa_path: '/storage/.kodi/userdata/omniphony/cache/kemar.sofa'\n"),
            std::string::npos)
      << yaml;
  EXPECT_NE(yaml.find("    mode: cascaded\n"), std::string::npos) << yaml;
  EXPECT_EQ(yaml.find("hrtf_grid_cache"), std::string::npos) << yaml;

  // The grids' cache, one file per rate, with the equalisation the codec
  // writes; no rate is named, so every rate is served.
  choices.hrtfGrid = "/storage/.kodi/userdata/omniphony/cache/kemar-{khz}.grid";
  yaml = OmniphonyRenderYaml(choices, "/b.so", -3.0);
  EXPECT_NE(yaml.find("    hrtf_grid_cache:\n"
                      "      path: '/storage/.kodi/userdata/omniphony/cache/kemar-{khz}.grid'\n"
                      "      diffuse_field_eq: true\n"),
            std::string::npos)
      << yaml;
  EXPECT_EQ(yaml.find("sample_rate:"), std::string::npos) << yaml;
  EXPECT_NE(yaml.find("    diffuse_field_eq: true\n"), std::string::npos) << yaml;
}

TEST(TestOmniphonyRoom, TheSameChoicesWriteTheSameConfig)
{
  // What a rate re-open relies on: the config is written again from the
  // choices read at open, so the second helper renders what the first did.
  OmniphonyRenderChoices choices;
  choices.roomPreset = 3;
  choices.distanceM = 4.5;
  choices.reverbPercent = 35;
  choices.lfeDb = 6.0;
  EXPECT_EQ(OmniphonyRenderYaml(choices, "/b.so", -3.0),
            OmniphonyRenderYaml(choices, "/b.so", -3.0));
  const std::string yaml = OmniphonyRenderYaml(choices, "/b.so", -3.0);
  EXPECT_NE(yaml.find("unit_scale_m: 4.50"), std::string::npos) << yaml;
  // The large room, grown for 4.5 m speakers up to the engine's 20 m limit.
  EXPECT_NE(yaml.find("room_width_m: 15.00"), std::string::npos) << yaml;
  EXPECT_NE(yaml.find("    reverb:\n      enabled: true\n      level: 0.35\n"), std::string::npos)
      << yaml;
  EXPECT_NE(yaml.find("gain_db: 6.0 }"), std::string::npos) << yaml;
}

TEST(TestOmniphonyRoom, ReadsWhatARoomSetHolds)
{
  ActiveAE::OmniphonySofaInfo info;
  ASSERT_TRUE(ActiveAE::OmniphonyParseSofaInfo(
      "hrtf=no room=yes prepared=no conventions=MultiSpeakerBRIR measurements=180 receivers=2 "
      "emitters=13 samples=16384 rate=48000 source=274203845-0123456789abcdef orientations=180 "
      "speakers=13 "
      "names=C,FWL,FWR,FL,FR,SL,SR,BL,BR,TFL,TFR,TSL,TSR reason=13 loudspeakers in every "
      "measurement: the HRTF stage takes one direction per measurement",
      info));
  EXPECT_FALSE(info.hrtf);
  EXPECT_TRUE(info.room);
  EXPECT_FALSE(info.prepared);
  EXPECT_EQ(info.conventions, "MultiSpeakerBRIR");
  EXPECT_EQ(info.measurements, 180u);
  EXPECT_EQ(info.receivers, 2u);
  EXPECT_EQ(info.emitters, 13u);
  EXPECT_EQ(info.samples, 16384u);
  EXPECT_EQ(info.rate, 48000u);
  EXPECT_EQ(info.source, "274203845-0123456789abcdef");
  EXPECT_EQ(info.orientations, 180u);
  EXPECT_EQ(info.speakers, 13u);
  EXPECT_EQ(info.names, "C,FWL,FWR,FL,FR,SL,SR,BL,BR,TFL,TFR,TSL,TSR");
  // A sentence, read to the end, with what looks like fields in it left alone.
  EXPECT_EQ(info.reason,
            "13 loudspeakers in every measurement: the HRTF stage takes one direction per "
            "measurement");
}

TEST(TestOmniphonyRoom, ReadsWhatAnHrtfSetAndAPreparedRoomHold)
{
  ActiveAE::OmniphonySofaInfo info;
  ASSERT_TRUE(ActiveAE::OmniphonyParseSofaInfo(
      "hrtf=yes room=no prepared=no conventions=SimpleFreeFieldHRIR measurements=1250 "
      "receivers=2 emitters=1 samples=256 rate=48000 reason=1250 measured directions, more "
      "than the 64 loudspeakers a listening room is prepared with: a free-field HRTF set is "
      "chosen as an HRTF",
      info));
  EXPECT_TRUE(info.hrtf);
  EXPECT_FALSE(info.room);
  EXPECT_EQ(info.measurements, 1250u);
  EXPECT_EQ(info.speakers, 0u);

  ASSERT_TRUE(ActiveAE::OmniphonyParseSofaInfo(
      "hrtf=no room=yes prepared=yes conventions=MultiSpeakerBRIR measurements=1 receivers=2 "
      "emitters=13 samples=16384 rate=48000 source=274203845-0123456789abcdef orientations=1 "
      "speakers=13 names=C,FWL "
      "reason=a prepared room renders only as a room",
      info));
  EXPECT_TRUE(info.prepared);
  // A prepared room carries the identity of the file it was prepared from.
  EXPECT_EQ(info.source, "274203845-0123456789abcdef");
  EXPECT_EQ(info.orientations, 1u);

  // A line naming neither stage is no description.
  EXPECT_FALSE(ActiveAE::OmniphonyParseSofaInfo("conventions=GeneralFIR", info));
}

TEST(TestOmniphonyRoom, ReadsWhatARoomPreparedWhenChosenHolds)
{
  // What --prepare-brir says once it prepared the room: the file's
  // description, the room's own figures before its reason.
  std::string word;
  std::string reason;
  std::string detail;
  ActiveAE::OmniphonyParseToolAnswer(
      "prepared hrtf=no room=yes prepared=no conventions=MultiSpeakerBRIR measurements=180 "
      "receivers=2 emitters=13 samples=16384 rate=48000 source=274203845-0123456789abcdef "
      "orientations=180 speakers=13 names=C,FWL seconds=0.512 bytes=1712345 reason=13 "
      "loudspeakers in every measurement",
      word, reason, detail);
  EXPECT_EQ(word, "prepared");
  ActiveAE::OmniphonySofaInfo info;
  ASSERT_TRUE(ActiveAE::OmniphonyParseSofaInfo(detail, info));
  EXPECT_TRUE(info.room);
  EXPECT_FALSE(info.prepared);
  EXPECT_EQ(info.orientations, 180u);
  EXPECT_EQ(info.source, "274203845-0123456789abcdef");
  EXPECT_EQ(info.reason, "13 loudspeakers in every measurement");

  // An HRTF set chosen as a room is refused with its description.
  ActiveAE::OmniphonyParseToolAnswer(
      "failed reason=notroom hrtf=yes room=no prepared=no conventions=SimpleFreeFieldHRIR "
      "measurements=1250 receivers=2 emitters=1 samples=256 rate=48000 reason=1250 measured "
      "directions",
      word, reason, detail);
  EXPECT_EQ(word, "failed");
  EXPECT_EQ(reason, "notroom");
  ASSERT_TRUE(ActiveAE::OmniphonyParseSofaInfo(detail, info));
  EXPECT_TRUE(info.hrtf);
  EXPECT_FALSE(info.room);
}

TEST(TestOmniphonyRoom, HandsTheHelperTheSizeLast)
{
  // The room kept for the file comes before the size of the bytes on stdin,
  // which is last for every command with an input: a size before it would
  // be read as the room, and a re-chosen room refused.
  const std::vector<std::string> args =
      ActiveAE::COmniphonyRoom::Preparer("/cache/x.room.next", "/files/x.sofa", "/cache/x.room")
          ->Arguments(400);
  ASSERT_EQ(args.size(), 5u);
  EXPECT_EQ(args[0], "--prepare-brir");
  EXPECT_EQ(args[2], "/cache/x.room.next");
  EXPECT_EQ(args[3], "/cache/x.room");
  EXPECT_EQ(args[4], "400");
  // No room kept: the output, then the size.
  const std::vector<std::string> first =
      ActiveAE::COmniphonyRoom::Preparer("/cache/x.room.next", "/files/x.sofa")->Arguments(400);
  ASSERT_EQ(first.size(), 4u);
  EXPECT_EQ(first[3], "400");
}

TEST(TestOmniphonyRoom, SplitsTheHelpersAnswer)
{
  std::string word, reason, detail;
  ActiveAE::OmniphonyParseToolAnswer("prepared emitters=13 orientations=1 seconds=0.337 rate=48000",
                                     word, reason, detail);
  EXPECT_EQ(word, "prepared");
  EXPECT_EQ(reason, "");
  EXPECT_EQ(detail, "emitters=13 orientations=1 seconds=0.337 rate=48000");

  ActiveAE::OmniphonyParseToolAnswer("failed reason=memory needs 407 MB, 300 MB available", word,
                                     reason, detail);
  EXPECT_EQ(word, "failed");
  EXPECT_EQ(reason, "memory");
  EXPECT_EQ(detail, "needs 407 MB, 300 MB available");

  ActiveAE::OmniphonyParseToolAnswer("failed reason=unsupported", word, reason, detail);
  EXPECT_EQ(reason, "unsupported");
  EXPECT_EQ(detail, "");
}
