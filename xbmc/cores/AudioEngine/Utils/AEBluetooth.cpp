/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AEBluetooth.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <sstream>

namespace
{
// Best first: the order the setting offers them in.
constexpr std::array<const char*, 5> CODECS = {"ldac", "aptx_hd", "aptx", "aac", "sbc"};

constexpr const char* PROFILE_PREFIX = "a2dp_sink_";

std::string Trim(const std::string& s)
{
  const auto first = s.find_first_not_of(" \t\r\n");
  if (first == std::string::npos)
    return {};
  const auto last = s.find_last_not_of(" \t\r\n");
  return s.substr(first, last - first + 1);
}
} // namespace

std::vector<std::string> AEBluetoothAvailableCodecs(const std::string& pactlCards)
{
  std::vector<std::string> available;
  bool inBluetoothCard = false;

  std::istringstream lines(pactlCards);
  std::string line;
  while (std::getline(lines, line))
  {
    const std::string text = Trim(line);
    if (text.rfind("Name: ", 0) == 0)
    {
      inBluetoothCard = text.rfind("Name: bluez_card.", 0) == 0;
      continue;
    }
    if (!inBluetoothCard || text.rfind(PROFILE_PREFIX, 0) != 0 ||
        text.find("available: yes") == std::string::npos)
      continue;

    // "a2dp_sink_aptx_hd: High Fidelity ..." -> "aptx_hd"
    const auto colon = text.find(':');
    if (colon == std::string::npos)
      continue;
    const std::string codec =
        text.substr(std::string(PROFILE_PREFIX).size(), colon - std::string(PROFILE_PREFIX).size());
    if (std::find(CODECS.begin(), CODECS.end(), codec) != CODECS.end() &&
        std::find(available.begin(), available.end(), codec) == available.end())
      available.push_back(codec);
  }

  std::vector<std::string> ordered;
  for (const char* codec : CODECS)
  {
    if (std::find(available.begin(), available.end(), codec) != available.end())
      ordered.emplace_back(codec);
  }
  return ordered;
}

std::string AEBluetoothCodecProfile(const std::string& codec)
{
  if (std::find(CODECS.begin(), CODECS.end(), codec) == CODECS.end())
    return {};
  return PROFILE_PREFIX + codec;
}

bool AEBluetoothIsA2DPSink(const std::string& sinkName)
{
  const std::string name = Trim(sinkName);
  return name.rfind("bluez_sink.", 0) == 0 && name.find(".a2dp_sink") != std::string::npos;
}

std::string AEBluetoothPulseSink(const std::string& device)
{
  // Split as CAESinkFactory::ParseDevice does: the driver up to the first ':'
  // (any case), the name up to the last '|'.
  const auto colon = device.find(':');
  if (colon == std::string::npos)
    return {};
  std::string driver = device.substr(0, colon);
  std::transform(driver.begin(), driver.end(), driver.begin(),
                 [](unsigned char c) { return std::toupper(c); });
  if (driver != "PULSE")
    return {};

  const std::string name = device.substr(colon + 1);
  return name.substr(0, name.find_last_of('|'));
}

float AEBluetoothStreamAmplification(float requested, float boost)
{
  return requested * boost;
}
