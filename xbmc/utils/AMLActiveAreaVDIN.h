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
inline constexpr int HEVC_DEFAULT_TAP_STEP = 4;

struct Sample
{
  uint64_t stamp;
  Borders borders;
  std::optional<Borders> snapped{};
  int tapStep{HEVC_DEFAULT_TAP_STEP};
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

  Sample sample{stamp, {static_cast<uint16_t>(top), static_cast<uint16_t>(bottom),
                        static_cast<uint16_t>(left), static_cast<uint16_t>(right)}};
  // HEVC can append refined offsets while keeping the original bounds for
  // consensus and for clients that only read the original fields.
  if (source == Source::HEVC)
  {
    const std::array<int, 4> limits{height / 3, height / 3, width / 3, width / 3};
    std::array<int, 4> offsets{};
    for (int& value : offsets)
    {
      if (!(stream >> value))
        return sample;
    }
    int tapStep;
    if (stream >> tapStep)
    {
      if (tapStep != 2 && tapStep != HEVC_DEFAULT_TAP_STEP)
        return sample;
      sample.tapStep = tapStep;
    }
    Borders snapped{};
    for (std::size_t i = 0; i < snapped.size(); ++i)
    {
      if (offsets[i] < 0 || offsets[i] > limits[i] ||
          std::abs(offsets[i] - sample.borders[i]) >= sample.tapStep)
        return sample;
      snapped[i] = static_cast<uint16_t>(offsets[i]);
    }
    sample.snapped = snapped;
  }
  return sample;
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

    const int tolerance = m_source == Source::HEVC ? sample->tapStep : 2;
    bool matches = m_count != 0;
    if (matches && m_source == Source::HEVC)
    {
      const auto& b = sample->borders;
      // A one-step bar and an uncropped axis are distinct geometries.
      matches = m_tapStep == sample->tapStep &&
                ((m_candidate[0] || m_candidate[1]) == (b[0] || b[1])) &&
                ((m_candidate[2] || m_candidate[3]) == (b[2] || b[3]));
    }
    for (std::size_t i = 0; matches && i < m_candidate.size(); ++i)
      matches = std::abs(static_cast<int>(m_candidate[i]) - sample->borders[i]) <= tolerance;
    if (!matches)
    {
      m_candidate = sample->borders;
      m_output = sample->snapped.value_or(sample->borders);
      m_tapStep = sample->tapStep;
      m_count = 1;
    }
    else if (m_count < 3)
      ++m_count;

    if (m_count == 3)
      return m_output;
    return {};
  }

private:
  const Source m_source;
  std::optional<uint64_t> m_stamp;
  Borders m_candidate{};
  Borders m_output{};
  int m_tapStep{HEVC_DEFAULT_TAP_STEP};
  int m_count{0};
};
} // namespace AML_ACTIVE_AREA_VDIN
