/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/HDRCapabilities.h"

#include <array>
#include <optional>
#include <sstream>
#include <string>

namespace AML::HDR
{
inline unsigned int Mask(const CHDRCapabilities& caps)
{
  return (caps.SupportsHDR10() ? 1u : 0u) | (caps.SupportsHDR10Plus() ? 2u : 0u) |
         (caps.SupportsHLG() ? 4u : 0u) | (caps.SupportsDolbyVision() ? 8u : 0u);
}

inline std::string Trim(const std::string& value)
{
  const auto first = value.find_first_not_of(" \t\r\n");
  if (first == std::string::npos)
    return {};
  return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}

inline std::optional<CHDRCapabilities> Parse(const std::string& hdr, const std::string& dv)
{
  CHDRCapabilities caps;
  const std::array<std::string, 3> labels{
      "Traditional HDR:", "HDR10Plus Supported:", "Hybrid Log-Gamma:"};
  const std::string legacyHLGLabel{"Hybrif Log-Gamma:"};
  std::array<std::optional<bool>, 3> flags;
  if (Trim(hdr) != "mask rx hdr capability")
  {
    std::istringstream lines(hdr);
    std::string line;
    while (std::getline(lines, line))
    {
      line = Trim(line);
      for (size_t i = 0; i < labels.size(); ++i)
      {
        const bool legacyHLG = i == 2 && line.compare(0, legacyHLGLabel.size(), legacyHLGLabel) == 0;
        const auto& label = legacyHLG ? legacyHLGLabel : labels[i];
        if (line.compare(0, label.size(), label) != 0)
          continue;
        const auto value = Trim(line.substr(label.size()));
        if (flags[i].has_value() || (value != "0" && value != "1"))
          return std::nullopt;
        // The alternate DRM driver typo previously left HLG false. Accept its
        // complete field without changing that interpretation or losing HDR/DV.
        flags[i] = !legacyHLG && value == "1";
      }
    }
    for (const auto& flag : flags)
    {
      if (!flag.has_value())
        return std::nullopt;
    }
    // Preserve the driver's existing Traditional HDR mapping for HDR10.
    if (*flags[0])
      caps.SetHDR10();
    if (*flags[1])
      caps.SetHDR10Plus();
    if (*flags[2])
      caps.SetHLG();
  }

  const auto dvText = Trim(dv);
  if (dvText == "The Rx don't support DolbyVision")
    return caps;
  if (Trim(dvText.substr(0, dvText.find('\n'))) != "DolbyVision RX support list:")
    return std::nullopt;
  caps.SetDolbyVision();
  return caps;
}

inline bool ReadyEDID(const std::string& raw)
{
  // Match the kernel's readiness policy without adding checksum rejection.
  if (raw.size() < 256 || raw.size() > 8 * 256 || raw.size() % 256 != 0 ||
      raw.compare(0, 16, "00ffffffffffff00") != 0)
    return false;
  return raw.find_first_not_of("0123456789abcdefABCDEF") == std::string::npos;
}

template<typename Reader>
std::optional<CHDRCapabilities> ReadStable(Reader&& read, std::string* edid = nullptr)
{
  constexpr std::array<const char*, 5> paths{
      "hpd_state", "edid_parsing", "rawedid", "hdr_cap", "dv_cap"};
  std::array<std::string, 5> before;
  for (size_t i = 0; i < paths.size(); ++i)
  {
    const auto value = read(paths[i]);
    if (!value.has_value())
      return std::nullopt;
    before[i] = Trim(*value);
  }
  if (before[0] != "1" || before[1] != "ok" || !ReadyEDID(before[2]))
    return std::nullopt;
  const auto caps = Parse(before[3], before[4]);
  if (!caps.has_value())
    return std::nullopt;
  // These sysfs files are not an atomic kernel snapshot. Reject observable
  // changes across the read rather than publishing mixed sink generations.
  for (size_t i = 0; i < paths.size(); ++i)
  {
    const auto value = read(paths[i]);
    if (!value.has_value() || Trim(*value) != before[i])
      return std::nullopt;
  }
  if (edid != nullptr)
    *edid = before[2];
  return caps;
}
} // namespace AML::HDR
