/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecOmniphony.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/OmniphonyConfig.h"

#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

namespace
{
using Status = OmniphonyListenerConfig::Status;

// render.yaml as the codec writes it for a room preset with reverb.
std::string Base()
{
  OmniphonyRenderChoices choices;
  choices.hrtf = "/storage/.kodi/userdata/omniphony/cache/kemar.sofa";
  choices.hrtfGrid = "/storage/.kodi/userdata/omniphony/cache/kemar-{khz}.grid";
  return OmniphonyRenderYaml(choices, "/usr/lib/kodi/omniphony/b.so", -3.0);
}

OmniphonyListenerConfig Merge(const std::string& listener)
{
  return OmniphonyMergeListenerConfig(Base(), listener, "/storage/.kodi/userdata/omniphony");
}

std::string Rejected(const std::string& listener)
{
  const OmniphonyListenerConfig merged = Merge(listener);
  EXPECT_EQ(merged.status, Status::Rejected) << listener << merged.yaml;
  EXPECT_TRUE(merged.yaml.empty());
  return merged.reason;
}
} // namespace

TEST(TestOmniphonyConfig, ATemplateOfNullsSetsNothing)
{
  EXPECT_EQ(Merge("").status, Status::None);
  EXPECT_EQ(Merge("# only comments\n\n").status, Status::None);
  const std::string nulls = "## explanation\n"
                            "render:\n"
                            "  master_gain: null\n"
                            "  binaural:\n"
                            "    hrir_source: null   # inherit\n"
                            "    reverb:\n"
                            "      enabled: ~\n"
                            "    ear_gains:\n"
                            "  current_layout: null\n"
                            "  placement:\n"
                            "    generic:\n"
                            "      mode: null\n";
  const OmniphonyListenerConfig merged = Merge(nulls);
  EXPECT_EQ(merged.status, Status::None) << merged.reason;
  EXPECT_FALSE(merged.layoutSet);
  EXPECT_FALSE(merged.decodeThreadSet);
}

TEST(TestOmniphonyConfig, ValuesWinKeyByKeyAndListsWhole)
{
  const OmniphonyListenerConfig merged = Merge("render:\n"
                                               "  master_gain: -6.5\n"
                                               "  binaural:\n"
                                               "    reverb:\n"
                                               "      rt60_s: 0.5\n"
                                               "    reflections: { level: 0.25 }\n"
                                               "    ear_gains: [1.0, 0.9]\n"
                                               "  decode_thread: true\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  const std::string& y = merged.yaml;
  EXPECT_NE(y.find("  master_gain: -6.5\n"), std::string::npos) << y;
  EXPECT_EQ(y.find("master_gain: -3.00"), std::string::npos) << y;
  // Kodi's reverb merges key by key with the patch's.
  EXPECT_NE(y.find("    reverb:\n      enabled: true\n      level: 0.20\n      rt60_s: 0.5\n"
                   "      predelay_ms: 20\n"),
            std::string::npos)
      << y;
  EXPECT_NE(y.find("      level: 0.25\n"), std::string::npos) << y;
  // The default medium room, grown for the default 2 m speakers.
  EXPECT_NE(y.find("      room_width_m: 8.00\n"), std::string::npos) << y;
  EXPECT_NE(y.find("    ear_gains: [1.0, 0.9]\n"), std::string::npos) << y;
  // Kodi's own values and its LFE rows, as written.
  EXPECT_NE(y.find("  bridge_path: '/usr/lib/kodi/omniphony/b.so'\n"), std::string::npos) << y;
  EXPECT_NE(y.find("        speakers:\n          - { name: LFE, coord_mode: cartesian"),
            std::string::npos)
      << y;
  EXPECT_NE(y.find("    hrtf_grid_cache:\n"), std::string::npos) << y;
  EXPECT_TRUE(merged.decodeThreadSet);
  EXPECT_FALSE(merged.layoutSet);
  // The same files merge to the same text, so effective.yaml is not rewritten.
  EXPECT_EQ(Merge("render:\n  master_gain: -6.5\n  binaural:\n    reverb:\n      rt60_s: 0.5\n"
                  "    reflections: { level: 0.25 }\n    ear_gains: [1.0, 0.9]\n"
                  "  decode_thread: true\n")
                .yaml,
            y);
}

TEST(TestOmniphonyConfig, ABlockListReplacesWholeAndKeepsItsShape)
{
  const OmniphonyListenerConfig merged = Merge("render:\n"
                                               "  placement:\n"
                                               "    generic:\n"
                                               "      layout:\n"
                                               "        speakers:\n"
                                               "        - name: LFE\n"
                                               "          gain_db: -4\n"
                                               "  current_layout:\n"
                                               "    name: mine\n"
                                               "    speakers:\n"
                                               "      - { name: L, azimuth: -30 }\n"
                                               "      - { name: R, azimuth: 30 }\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  const std::string& y = merged.yaml;
  EXPECT_NE(y.find("        speakers:\n          - name: LFE\n            gain_db: -4\n"),
            std::string::npos)
      << y;
  EXPECT_EQ(y.find("name: LFE2"), std::string::npos) << y;
  EXPECT_NE(y.find("  current_layout:\n    name: mine\n    speakers:\n"
                   "      - { name: L, azimuth: -30 }\n      - { name: R, azimuth: 30 }\n"),
            std::string::npos)
      << y;
  EXPECT_TRUE(merged.layoutSet);
}

TEST(TestOmniphonyConfig, RelativePathsStartInItsFolder)
{
  OmniphonyListenerConfig merged = Merge("render:\n  binaural:\n    hrir_source: brir\n"
                                         "    brir_sofa_path: \"rooms/my room.room\"\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_EQ(merged.room, "/storage/.kodi/userdata/omniphony/rooms/my room.room");
  EXPECT_NE(merged.yaml.find(
                "    brir_sofa_path: '/storage/.kodi/userdata/omniphony/rooms/my room.room'\n"),
            std::string::npos)
      << merged.yaml;

  // Another HRTF set: Kodi's grid cache is of its own set and goes.
  merged = Merge("render:\n  binaural:\n    hrtf_sofa_path: '/storage/hrtf/it''s.sofa'\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_NE(merged.yaml.find("    hrtf_sofa_path: '/storage/hrtf/it''s.sofa'\n"), std::string::npos)
      << merged.yaml;
  EXPECT_EQ(merged.yaml.find("hrtf_grid_cache"), std::string::npos) << merged.yaml;
  EXPECT_TRUE(merged.room.empty());
}

TEST(TestOmniphonyConfig, WhatKodiOwnsIsRefused)
{
  for (const auto& [listener, what] : std::initializer_list<std::pair<const char*, const char*>>{
           {"render:\n  bridge_path: /x.so\n", "render.bridge_path"},
           {"render:\n  osc_port: 9000\n", "render.osc_port"},
           {"render:\n  adaptive_resampling_band: 3\n", "render.adaptive_resampling_band"},
           {"render:\n  speaker_layout: x.yaml\n", "current_layout instead"},
           {"render:\n  virtual_bed: x\n", "legacy"},
           {"render:\n  dialogue_gain_db: 3\n", "IAMF"},
           {"render:\n  channel_render_mode: host\n", "channel_render_mode"},
           {"render:\n  binaural:\n    output_mode: speakers\n", "headphones only"},
           {"render:\n  binaural:\n    hrir_source: sofa:/x.sofa\n", "hrtf_sofa_path"},
           {"render:\n  binaural:\n    head_tracking: { enabled: true }\n", "head tracker"},
           {"render:\n  binaural:\n    brir_head_tracking: true\n", "head tracker"},
           {"render:\n  binaural:\n    hrtf_grid_cache: { path: /x }\n", "Kodi's"},
           {"render:\n  binaural:\n    hrtf_sofa_path: smb://nas/x.sofa\n", "local path"},
           {"render:\n  binaural:\n    hrtf_sofa_path: [a]\n", "must be a path"},
           {"global:\n  loglevel: debug\n", "render:"},
           {"render: 3\n", "must be a mapping"},
           {"render:\n  binaural:\n    hrir_source: brir\n    brir_sofa_path: /r.room\n"
            "  current_layout: { name: x }\n",
            "measured loudspeakers"},
       })
  {
    const std::string why = Rejected(listener);
    EXPECT_NE(why.find(what), std::string::npos) << listener << why;
  }
  // brir_head_tracking off is what Kodi does anyway.
  EXPECT_EQ(Merge("render:\n  binaural:\n    brir_head_tracking: false\n").status, Status::Applied);
}

TEST(TestOmniphonyConfig, YamlItDoesNotReadIsRefusedWithItsLine)
{
  for (const auto& [listener, what] : std::initializer_list<std::pair<const char*, const char*>>{
           {"render:\n\tmaster_gain: 1\n", "line 2: a tab"},
           {"render:\n  master_gain: &a 1\n", "line 2: anchors"},
           {"render:\n  master_gain: !!float 1\n", "line 2: anchors"},
           {"render:\n  master_gain: |\n    1\n", "line 2: a multi-line"},
           {"render:\n  binaural: { reverb: { level: 0.1 }\n", "line 2: a { } mapping"},
           {"render:\n  binaural:\n    ear_gains: [1.0,\n      1.0]\n", "line 3: a [ ] list"},
           {"render:\n  master_gain: 'open\n", "line 2: a quoted"},
           {"render:\n  master_gain: 1\n    oops: 2\n", "line 3: a value continued"},
           {"render:\n  master_gain: 1\n master_gain: 2\n", "line 3: indented more"},
           {"  render:\n    master_gain: 1\nother: 2\n", "line 3: indented less"},
           {"render:\n  master_gain: 1\n  master_gain: 2\n", "line 3: master_gain appears twice"},
           {"render:\n  just text\n", "line 2: not `key: value`"},
           {"render:\n  a: b: c\n", "line 2: `key: value` inside"},
           {"render:\n  a: 1\n  - 1\n", "line 3: a list item"},
           {"---\nrender: {}\n---\nrender: {}\n", "line 3: one document"},
       })
  {
    const std::string why = Rejected(listener);
    EXPECT_NE(why.find(what), std::string::npos) << listener << why;
  }
  EXPECT_EQ(Merge("---\nrender:\n  master_gain: 1\n").status, Status::Applied);
  EXPECT_NE(Rejected(std::string(OMNI_LISTENER_CONFIG_MAX + 1, '#')).find("bytes"),
            std::string::npos);
}

TEST(TestOmniphonyConfig, CommentsAndQuotesAreReadAsYamlReadsThem)
{
  const OmniphonyListenerConfig merged =
      Merge("render:   # the section\n"
            "  binaural:\n"
            "    hrtf_sofa_path: \"/storage/a #1.sofa\"  # a comment\n"
            "    reverb: { enabled: true, level: 0.2 }  # also\n"
            "  object_generator_id: it's#not-a-comment\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_NE(merged.yaml.find("    hrtf_sofa_path: '/storage/a #1.sofa'\n"), std::string::npos)
      << merged.yaml;
  EXPECT_NE(merged.yaml.find("  object_generator_id: it's#not-a-comment\n"), std::string::npos)
      << merged.yaml;
  EXPECT_NE(merged.yaml.find("      level: 0.2\n"), std::string::npos) << merged.yaml;
}

TEST(TestOmniphonyConfig, EscapesAreDecodedOnceAndWrittenSingleQuoted)
{
  // What is checked is what is written: an escape cannot hide a value Kodi
  // refuses, and a path is the file it spells.
  OmniphonyListenerConfig merged = Merge("render:\n  binaural:\n"
                                         "    hrtf_sofa_path: \"Puls\\x65 caf\\u00e9.sofa\"\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_NE(merged.yaml.find("    hrtf_sofa_path: '/storage/.kodi/userdata/omniphony/Pulse "
                             "caf\xC3\xA9.sofa'\n"),
            std::string::npos)
      << merged.yaml;
  EXPECT_NE(Rejected("render:\n  channel_render_mode: \"\\x68ost\"\n").find("channel_render_mode"),
            std::string::npos);
  EXPECT_NE(Rejected("render:\n  binaural:\n    hrir_source: \"\\x62rir:/r.sofa\"\n")
                .find("brir_sofa_path"),
            std::string::npos);
  EXPECT_NE(Rejected("render:\n  binaural: { hrir_source: \"\\x62rir\", brir_sofa_path: /r.room }\n"
                     "  current_layout: { name: x }\n")
                .find("measured loudspeakers"),
            std::string::npos);
  merged = Merge("render:\n  drc_mode: \"standard\\/line\"\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_NE(merged.yaml.find("  drc_mode: 'standard/line'\n"), std::string::npos) << merged.yaml;
  EXPECT_NE(Rejected("render:\n  drc_mode: \"a\\qb\"\n").find("line 2: an escape"),
            std::string::npos);
  EXPECT_NE(Rejected("render:\n  drc_mode: \"\\x6\"\n").find("line 2: an escape"),
            std::string::npos);
}

TEST(TestOmniphonyConfig, ReadsTheYamlAReaderWouldRead)
{
  OmniphonyListenerConfig merged = Merge("\"render\" :\n"
                                         "  binaural: { mode: , head_radius_m: 0.1, }\n"
                                         "  drc_mode: x 'y # note: z\n"
                                         "...\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_NE(merged.yaml.find("    head_radius_m: 0.1\n"), std::string::npos) << merged.yaml;
  EXPECT_NE(merged.yaml.find("  drc_mode: x 'y\n"), std::string::npos) << merged.yaml;
  // mode: null inherits: Kodi's own mode is not written over.
  EXPECT_EQ(merged.yaml.find("mode: null"), std::string::npos) << merged.yaml;
  // `mode:direct` is one key to a YAML reader, not mode set to direct.
  EXPECT_NE(Rejected("render:\n  binaural: {mode:direct}\n").find("a space after the colon"),
            std::string::npos);
  EXPECT_NE(Rejected("render:\n  master_gain: 1\n...\nrender: {}\n").find("line 4: one document"),
            std::string::npos);
}

TEST(TestOmniphonyConfig, OnlyARoomConfigYamlNamesIsItsOwn)
{
  // The settings' room, in the cache: a merge that keeps it names no room of
  // its own, so a room the engine refuses is the settings' - Kodi prepares it
  // again. One config.yaml names is the listener's, and only reported.
  OmniphonyRenderChoices choices;
  choices.room = "/storage/.kodi/userdata/omniphony/cache/bbc.room";
  const std::string base = OmniphonyRenderYaml(choices, "/b.so", -3.0);
  const std::string dir = "/storage/.kodi/userdata/omniphony";

  OmniphonyListenerConfig merged =
      OmniphonyMergeListenerConfig(base, "render:\n  master_gain: -6\n", dir);
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_TRUE(merged.room.empty());
  EXPECT_FALSE(merged.ownResponse);
  EXPECT_NE(merged.yaml.find("brir_sofa_path: '" + choices.room + "'"), std::string::npos)
      << merged.yaml;

  merged = OmniphonyMergeListenerConfig(
      base, "render:\n  binaural:\n    brir_sofa_path: mine.room\n", dir);
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_EQ(merged.room, dir + "/mine.room");
  EXPECT_TRUE(merged.ownResponse);
}

TEST(TestOmniphonyConfig, SaysWhenConfigYamlChoosesTheResponse)
{
  // Whether the settings' room is what plays while config.yaml applies: not
  // when it chooses another source, or a room of its own; still when it only
  // tunes the render or names the room source without a file.
  const std::string dir = "/storage/.kodi/userdata/omniphony";
  for (const auto& [listener, own] : std::vector<std::pair<std::string, bool>>{
           {"render:\n  master_gain: -6\n", false},
           {"render:\n  binaural:\n    reverb: { level: 0.2 }\n", false},
           {"render:\n  binaural:\n    hrir_source: brir\n", false},
           {"render:\n  binaural:\n    hrir_source: saf\n", true},
           {"render:\n  binaural:\n    hrir_source: sofa\n    hrtf_sofa_path: /h.sofa\n", true},
           {"render:\n  binaural:\n    hrir_source: brir\n    brir_sofa_path: /r.room\n", true},
       })
  {
    const OmniphonyListenerConfig merged = OmniphonyMergeListenerConfig(Base(), listener, dir);
    ASSERT_EQ(merged.status, Status::Applied) << listener << merged.reason;
    EXPECT_EQ(merged.ownResponse, own) << listener;
  }
}

TEST(TestOmniphonyConfig, ARoomPathAloneSelectsNothing)
{
  OmniphonyListenerConfig merged =
      Merge("render:\n  binaural:\n    hrir_source: saf\n    brir_sofa_path: big.sofa\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_TRUE(merged.room.empty());
  merged = Merge("render:\n  binaural:\n    hrir_source: brir\n    brir_sofa_path: big.sofa\n");
  ASSERT_EQ(merged.status, Status::Applied) << merged.reason;
  EXPECT_EQ(merged.room, "/storage/.kodi/userdata/omniphony/big.sofa");
}

TEST(TestOmniphonyConfig, SaysWhenTheMergeRendersCascaded)
{
  // Kodi's render.yaml here is direct: cascade comes from config.yaml.
  EXPECT_FALSE(Merge("render:\n  master_gain: 1\n").cascade);
  EXPECT_TRUE(Merge("render:\n  binaural:\n    mode: cascaded\n").cascade);
  EXPECT_FALSE(Merge("render:\n  binaural:\n    mode: cascaded\n    hrir_source: brir\n"
                     "    brir_sofa_path: /r.room\n")
                   .cascade);
  // And over a cascaded render.yaml, mode: direct turns it off.
  OmniphonyRenderChoices choices;
  choices.cascade = true;
  const std::string base = OmniphonyRenderYaml(choices, "/b.so", -3.0);
  EXPECT_TRUE(OmniphonyMergeListenerConfig(base, "render:\n  master_gain: 1\n", "/d").cascade);
  EXPECT_FALSE(
      OmniphonyMergeListenerConfig(base, "render:\n  binaural:\n    mode: direct\n", "/d").cascade);
}
