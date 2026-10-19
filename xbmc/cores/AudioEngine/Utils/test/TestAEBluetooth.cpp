/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/AudioEngine/Utils/AEBluetooth.h"

#include <string>
#include <vector>

#include <gtest/gtest.h>

namespace
{
// `pactl list cards` as PulseAudio 17 with pulseaudio-modules-bt prints it for
// an AAC headset, trimmed: an ALSA card first, then the Bluetooth card whose
// LDAC and aptX profiles exist but are not available on this headset.
const std::string PENROSE = R"(Card #0
	Name: alsa_card.platform-auge_sound
	Driver: module-alsa-card.c
	Profiles:
		output:analog-stereo: Analog Stereo Output (sinks: 1, sources: 0, priority: 6500, available: yes)
		off: Off (sinks: 0, sources: 0, priority: 0, available: yes)
	Active Profile: output:analog-stereo

Card #1
	Name: bluez_card.40_2E_71_2D_A6_15
	Driver: module-bluez5-device.c
	Profiles:
		a2dp_sink_sbc: High Fidelity Playback (A2DP Sink: SBC) (sinks: 1, sources: 0, priority: 40, available: yes)
		a2dp_sink_aac: High Fidelity Playback (A2DP Sink: AAC) (sinks: 1, sources: 0, priority: 40, available: yes)
		a2dp_sink_aptx: High Fidelity Playback (A2DP Sink: aptX) (sinks: 1, sources: 0, priority: 40, available: no)
		a2dp_sink_aptx_hd: High Fidelity Playback (A2DP Sink: aptX HD) (sinks: 1, sources: 0, priority: 40, available: no)
		a2dp_sink_ldac: High Fidelity Playback (A2DP Sink: LDAC) (sinks: 1, sources: 0, priority: 40, available: no)
		headset_head_unit: Headset Head Unit (HSP/HFP) (sinks: 1, sources: 1, priority: 30, available: no)
		off: Off (sinks: 0, sources: 0, priority: 0, available: yes)
	Active Profile: a2dp_sink_aac
)";
} // namespace

TEST(TestAEBluetooth, OnlyTheCodecsTheHeadsetCanUseAreOffered)
{
  EXPECT_EQ((std::vector<std::string>{"aac", "sbc"}), AEBluetoothAvailableCodecs(PENROSE));
}

TEST(TestAEBluetooth, CodecsAreOfferedBestFirst)
{
  const std::string all = R"(	Name: bluez_card.88_C9_E8_45_FE_95
	Profiles:
		a2dp_sink_sbc: SBC (available: yes)
		a2dp_sink_aac: AAC (available: yes)
		a2dp_sink_aptx: aptX (available: yes)
		a2dp_sink_aptx_hd: aptX HD (available: yes)
		a2dp_sink_ldac: LDAC (available: yes)
)";
  EXPECT_EQ((std::vector<std::string>{"ldac", "aptx_hd", "aptx", "aac", "sbc"}),
            AEBluetoothAvailableCodecs(all));
}

TEST(TestAEBluetooth, ProfilesOfOtherCardsAreIgnored)
{
  const std::string notBluetooth = R"(	Name: alsa_card.usb
	Profiles:
		a2dp_sink_ldac: impostor (available: yes)
)";
  EXPECT_TRUE(AEBluetoothAvailableCodecs(notBluetooth).empty());
  EXPECT_TRUE(AEBluetoothAvailableCodecs("").empty());
}

TEST(TestAEBluetooth, EachCodecSwitchesToItsOwnProfile)
{
  EXPECT_EQ("a2dp_sink_ldac", AEBluetoothCodecProfile("ldac"));
  EXPECT_EQ("a2dp_sink_aptx_hd", AEBluetoothCodecProfile("aptx_hd"));
  EXPECT_EQ("a2dp_sink_aptx", AEBluetoothCodecProfile("aptx"));
  EXPECT_EQ("a2dp_sink_aac", AEBluetoothCodecProfile("aac"));
  EXPECT_EQ("a2dp_sink_sbc", AEBluetoothCodecProfile("sbc"));
}

TEST(TestAEBluetooth, AutomaticSwitchesNothing)
{
  // The setting's default: the headset keeps whatever PulseAudio negotiated.
  EXPECT_EQ("", AEBluetoothCodecProfile(AE_BLUETOOTH_CODEC_AUTOMATIC));
  EXPECT_EQ("", AEBluetoothCodecProfile("opus"));
}

TEST(TestAEBluetooth, OnlyBluetoothA2DPSinksCount)
{
  EXPECT_TRUE(AEBluetoothIsA2DPSink("bluez_sink.40_2E_71_2D_A6_15.a2dp_sink\n"));
  EXPECT_FALSE(AEBluetoothIsA2DPSink("bluez_sink.40_2E_71_2D_A6_15.headset_head_unit"));
  EXPECT_FALSE(AEBluetoothIsA2DPSink("alsa_output.platform-auge_sound.analog-stereo"));
  EXPECT_FALSE(AEBluetoothIsA2DPSink(""));
}

TEST(TestAEBluetooth, OnlyPulseAudioDevicesCanReachTheHeadset)
{
  // What CoreELEC's Bluetooth add-on selects when a headset connects.
  EXPECT_EQ("Default", AEBluetoothPulseSink("PULSE:Default|Bluetooth Audio (PULSEAUDIO)"));
  EXPECT_EQ("bluez_sink.40_2E_71_2D_A6_15.a2dp_sink",
            AEBluetoothPulseSink("PULSE:bluez_sink.40_2E_71_2D_A6_15.a2dp_sink|Penrose"));
  EXPECT_EQ("Default", AEBluetoothPulseSink("pulse:Default"));
  // HDMI and other ALSA outputs never play to the headset, even while it is
  // PulseAudio's default sink.
  EXPECT_EQ("", AEBluetoothPulseSink("ALSA:hdmi:CARD=AMLAUGESOUND,DEV=0|HDMI"));
  EXPECT_EQ("", AEBluetoothPulseSink("ALSA:surround71:CARD=AMLAUGESOUND,DEV=0|HDMI Multi Ch PCM"));
  EXPECT_EQ("", AEBluetoothPulseSink(""));
}

TEST(TestAEBluetooth, TheBoostScalesTheStreamsOwnAmplification)
{
  // The player's volume amplification survives the boost being switched on
  // and off: it is scaled, not replaced.
  EXPECT_FLOAT_EQ(2.5f, AEBluetoothStreamAmplification(2.0f, 1.25f));
  EXPECT_FLOAT_EQ(2.0f, AEBluetoothStreamAmplification(2.0f, 1.0f));
  EXPECT_FLOAT_EQ(1.25f, AEBluetoothStreamAmplification(1.0f, 1.25f));
}
