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

// The kernel reports inclusive active-picture bounds in the capture canvas.
inline std::optional<Sample> Read(std::istream& stream, int width, int height)
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
      std::abs(top - bottom) > 4 || std::abs(left - right) > 4)
    return {};

  return Sample{stamp, {static_cast<uint16_t>(top), static_cast<uint16_t>(bottom),
                        static_cast<uint16_t>(left), static_cast<uint16_t>(right)}};
}

class Consensus
{
public:
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

    bool matches = m_count != 0;
    for (std::size_t i = 0; matches && i < m_candidate.size(); ++i)
      matches = std::abs(static_cast<int>(m_candidate[i]) - sample->borders[i]) <= 2;
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
  std::optional<uint64_t> m_stamp;
  Borders m_candidate{};
  int m_count{0};
};
} // namespace AML_ACTIVE_AREA_VDIN
