/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <istream>
#include <optional>

namespace AML_ACTIVE_AREA_VDIN
{
using Borders = std::array<uint16_t, 4>;

struct Sample
{
  uint64_t stamp;
  Borders borders;
};

enum class Source
{
  VDIN,
  HEVC
};

// The kernel reports inclusive active-picture bounds in the capture canvas.
inline std::optional<Sample> Read(std::istream& stream, int width, int height,
                                  Source source = Source::VDIN)
{
  int version, canvasWidth, canvasHeight, top, bottom, left, right, lumaMin, lumaMax;
  uint64_t stamp;
  if (!(stream >> version >> stamp >> canvasWidth >> canvasHeight >> top >> bottom >> left >>
        right >> lumaMin >> lumaMax) || version != 1 || width < 64 || width > UINT16_MAX ||
      height < 64 || height > UINT16_MAX || canvasWidth != width || canvasHeight != height)
  {
    stream.setstate(std::ios::failbit);
    return {};
  }
  if (top < 0 || top >= bottom || bottom >= height || left < 0 || left >= right ||
      right >= width || lumaMin < 0 || lumaMin > 255 || lumaMax > 255 || lumaMax < lumaMin + 16)
    return {};

  bottom = height - 1 - bottom;
  right = width - 1 - right;
  if (top > height / 3 || bottom > height / 3 || left > width / 3 || right > width / 3 ||
      (source == Source::VDIN && (std::abs(top - bottom) > 4 || std::abs(left - right) > 4)))
    return {};

  return Sample{stamp, {static_cast<uint16_t>(top), static_cast<uint16_t>(bottom),
                        static_cast<uint16_t>(left), static_cast<uint16_t>(right)}};
}

class Consensus
{
public:
  explicit Consensus(Source source = Source::VDIN) : m_source(source) {}

  std::optional<Borders> Observe(const std::optional<Sample>& sample)
  {
    if (!sample)
    {
      m_count = 0;
      return {};
    }
    if (m_stamp == sample->stamp)
      return {};
    m_stamp = sample->stamp;

    // The quarter-resolution HEVC surface moves in four coded pixels.
    const int tolerance = m_source == Source::HEVC ? 4 : 2;
    bool matches = m_count != 0;
    if (matches && m_source == Source::HEVC)
    {
      const auto& b = sample->borders;
      // A one-step bar and an uncropped axis are distinct geometries.
      matches = ((m_candidate[0] || m_candidate[1]) == (b[0] || b[1])) &&
                ((m_candidate[2] || m_candidate[3]) == (b[2] || b[3]));
    }
    for (std::size_t i = 0; matches && i < m_candidate.size(); ++i)
      matches = std::abs(static_cast<int>(m_candidate[i]) - sample->borders[i]) <= tolerance;
    if (!matches)
    {
      m_candidate = sample->borders;
      m_count = 1;
    }
    else if (m_count < 3)
      ++m_count;

    if (m_count == 3)
      return m_candidate;
    return {};
  }

private:
  const Source m_source;
  std::optional<uint64_t> m_stamp;
  Borders m_candidate{};
  int m_count{0};
};
} // namespace AML_ACTIVE_AREA_VDIN
