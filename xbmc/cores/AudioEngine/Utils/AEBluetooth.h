/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>
#include <vector>

/*!
 * \brief The Bluetooth codec setting's value for "keep what PulseAudio
 * negotiated": its default, and the one choice that switches no profile.
 */
inline constexpr const char* AE_BLUETOOTH_CODEC_AUTOMATIC = "Default";

/*!
 * \brief The A2DP codecs a connected headset can use, best first.
 *
 * \param pactlCards What `pactl list cards` prints. Only the profiles of
 *        Bluetooth cards that PulseAudio reports as available count.
 * \return Codec ids as the Bluetooth codec setting stores them ("ldac",
 *         "aptx_hd", "aptx", "aac", "sbc").
 */
std::vector<std::string> AEBluetoothAvailableCodecs(const std::string& pactlCards);

/*!
 * \brief The card profile that selects `codec`, or "" for automatic and for
 * anything that is not a codec id - in which case nothing is switched.
 */
std::string AEBluetoothCodecProfile(const std::string& codec);

/*!
 * \brief Whether `sinkName` (as `pactl get-default-sink` prints it) is a
 * Bluetooth A2DP playback sink.
 */
bool AEBluetoothIsA2DPSink(const std::string& sinkName);

/*!
 * \brief The PulseAudio sink Kodi's audio `device` plays to: "Default" for
 * PulseAudio's default sink, or a sink name - and "" when the device is not a
 * PulseAudio one (HDMI and the other ALSA outputs), which never reaches a
 * Bluetooth headset.
 *
 * \param device The audio device setting, "DRIVER:NAME|description".
 */
std::string AEBluetoothPulseSink(const std::string& device);

/*!
 * \brief A stream's amplification with the Bluetooth volume boost applied.
 *
 * \param requested What the player asked the stream for (volume amplification).
 * \param boost The Bluetooth boost factor, 1 when it is off.
 */
float AEBluetoothStreamAmplification(float requested, float boost);
