/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <array>
#include <cstdint>
#include <optional>

namespace AML_FRAME_L5
{
using Bounds = std::optional<std::array<uint16_t, 4>>; // top, bottom, left, right

// amlvideo DQBUF extension v1. The eight timecode payload bytes contain
// big-endian offsets; type/flags continue to carry the decoded dimensions.
inline Bounds FromDequeue(uint32_t tag, const std::array<uint8_t, 8>& bytes,
                          unsigned int width, unsigned int height)
{
  if (tag != 0x50334c01 || !width || !height)
    return {};
  std::array<uint16_t, 4> offsets{};
  for (unsigned int i = 0; i < offsets.size(); ++i)
    offsets[i] = (static_cast<uint16_t>(bytes[2 * i]) << 8) | bytes[2 * i + 1];
  if (static_cast<unsigned int>(offsets[0]) + offsets[1] >= height ||
      static_cast<unsigned int>(offsets[2]) + offsets[3] >= width)
    return {};
  return offsets; // Valid zero is a full-frame measurement, not missing data.
}
} // namespace AML_FRAME_L5
