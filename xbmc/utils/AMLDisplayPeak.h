/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <cmath>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>

namespace AML::DisplayPeak
{
// Independent of configured/generated VSVDB payloads. Zero is unavailable.
inline std::atomic<int> reportedNits{0};
inline void Publish(int nits)
{
  reportedNits.store(std::max(0, nits));
}
inline int Resolve(bool explicitOverride, int configuredNits)
{
  const int nits = explicitOverride ? configuredNits : reportedNits.load();
  return nits > 0 ? nits : 1000;
}

inline std::optional<int> Field(const std::string& text,
                                const std::string& label,
                                const std::string& suffix,
                                int maximum)
{
  std::istringstream lines(text);
  std::string line;
  std::optional<int> result;
  while (std::getline(lines, line))
  {
    const auto start = line.find_first_not_of(" \t\r");
    if (start == std::string::npos || line.compare(start, label.size(), label) != 0)
      continue;
    if (result.has_value())
      return std::nullopt;
    auto value = line.substr(start + label.size());
    const auto first = value.find_first_not_of(" \t");
    if (first == std::string::npos)
      return std::nullopt;
    value = value.substr(first);
    const auto last = value.find_last_not_of(" \t\r");
    value.resize(last + 1);
    if (value.size() < suffix.size() ||
        value.compare(value.size() - suffix.size(), suffix.size(), suffix) != 0)
      return std::nullopt;
    value.resize(value.size() - suffix.size());
    int number = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), number);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() || number < 0 ||
        number > maximum)
      return std::nullopt;
    result = number;
  }
  return result;
}

inline int DolbyVision(const std::string& text)
{
  if (text.compare(0, std::string_view("DolbyVision RX support list:\n").size(),
                   "DolbyVision RX support list:\n") != 0)
    return 0;
  // Both V1 lengths are emitted by the vendor driver.
  const auto versionPos = text.find("VSVDB Version: V");
  if (versionPos == std::string::npos ||
      text.find("VSVDB Version: V", versionPos + 1) != std::string::npos)
    return 0;
  const auto start = versionPos + std::string_view("VSVDB Version: V").size();
  const auto end = text.find('\n', start);
  const auto version = text.substr(start, end - start);
  if (version == "1" || version == "1(12-byte)" || version == "1(15-byte)")
  {
    const auto index = Field(text, "TmaxM:", "nti", 127);
    return index ? 100 + 50 * *index : 0;
  }
  if (version == "2")
  {
    // Preserve p3i's existing reported-nits convention, not payload quantization.
    constexpr std::array<int, 32> nits{96,   113,  132,  155,  181,  211,  245,  285,
                                       332,  385,  447,  518,  601,  696,  807,  934,
                                       1082, 1252, 1450, 1678, 1943, 2250, 2607, 3020,
                                       3501, 4060, 4710, 5467, 6351, 7382, 8588, 10000};
    const auto index = Field(text, "TmaxQ:", "pqi", 31);
    return index ? nits[*index] : 0;
  }
  if (version == "0")
  {
    const auto pq = Field(text, "TmaxQ:", "pqi", 4095);
    if (!pq || *pq == 0)
      return 0;
    constexpr double m1 = 2610.0 / 16384.0, m2 = 2523.0 / 32.0;
    constexpr double c1 = 3424.0 / 4096.0, c2 = 2413.0 / 128.0, c3 = 2392.0 / 128.0;
    const double p = std::pow(*pq / 4095.0, 1.0 / m2);
    return static_cast<int>(
        std::round(10000.0 * std::pow(std::max(p - c1, 0.0) / (c2 - c3 * p), 1.0 / m1)));
  }
  return 0;
}

inline int HDR(const std::string& text)
{
  // Only the static PQ block's maximum code; never CUVA or content mastering data.
  const auto start = text.find("HDR Static Metadata:");
  const auto end = text.find("HDR Dynamic Metadata:", start);
  if (start == std::string::npos)
    return 0;
  const auto block = text.substr(start, end - start);
  const auto pq = Field(block, "SMPTE ST 2084:", "", 1);
  const auto type = Field(block, "Supported SMD type1:", "", 1);
  const auto code = Field(block, "Max:", "", 255);
  if (!pq || *pq != 1 || !type || *type != 1 || !code || *code == 0)
    return 0;
  return static_cast<int>(std::round(50.0 * std::exp2(*code / 32.0)));
}

inline int Reported(const std::string& hdr, const std::string& dv)
{
  const int nits = DolbyVision(dv);
  return nits > 0 ? nits : HDR(hdr);
}
} // namespace AML::DisplayPeak
