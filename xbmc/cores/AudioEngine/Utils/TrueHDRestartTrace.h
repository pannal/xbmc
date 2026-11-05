/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <atomic>
#include <cstdint>

namespace AE
{
// Software packet identity only; never changes the encoded payload or proves
// that a receiver accepted or decoded it.
struct TrueHDRestartTrace
{
  uint64_t epoch{0};
  unsigned int packet{0};
  unsigned int packetBytes{0};
  unsigned int offset{0};
  unsigned int bytes{0};
  bool majorAtStart{false};
  unsigned int insertedPauses{0};
  uint64_t insertedPauseMs{0};

  explicit operator bool() const { return epoch != 0; }

  TrueHDRestartTrace Slice(unsigned int sourceOffset, unsigned int count) const
  {
    if (!epoch || sourceOffset > packetBytes || count > packetBytes - sourceOffset)
      return {};
    auto result = *this;
    result.offset = sourceOffset;
    result.bytes = count;
    result.insertedPauses = 0;
    result.insertedPauseMs = 0;
    return result;
  }
};

class CTrueHDRestartTrace
{
public:
  static constexpr unsigned int PACKET_LIMIT = 32;

  void Reset(bool enabled)
  {
    // Allocated only on a debug-enabled codec reset. Old in-flight packets
    // retain their own ID rather than consulting a global current epoch.
    static std::atomic<uint64_t> nextEpoch{0};
    m_epoch = enabled ? ++nextEpoch : 0;
    m_packet = 0;
  }

  uint64_t Epoch() const { return m_epoch; }

  TrueHDRestartTrace Next(const uint8_t* data, unsigned int bytes)
  {
    if (!m_epoch || m_packet >= PACKET_LIMIT || !data || !bytes)
      return {};
    TrueHDRestartTrace result;
    result.epoch = m_epoch;
    result.packet = ++m_packet;
    result.packetBytes = bytes;
    result.bytes = bytes;
    // 8 reserved IEC bytes + 20 MAT start-code bytes + 4 AU prefix bytes.
    // This structural check is not a scan/validation of all majors in MAT.
    result.majorAtStart = bytes >= 36 && data[32] == 0xf8 && data[33] == 0x72 &&
                          data[34] == 0x6f && data[35] == 0xba;
    return result;
  }

private:
  uint64_t m_epoch{0};
  unsigned int m_packet{0};
};
} // namespace AE
