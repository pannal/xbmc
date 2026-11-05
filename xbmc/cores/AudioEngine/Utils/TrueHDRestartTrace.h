/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <array>
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
  static constexpr unsigned int PACKET_LIMIT = 256;

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

// Observe source AU boundaries, not sync-like bytes inside compressed payload.
// The packer supplies actual audio copies, excluding MAT markers and padding.
class CTrueHDMajorTrace
{
public:
  struct Span
  {
    unsigned int packet{0};
    unsigned int offset{0};
    unsigned int bytes{0};
  };

  void Reset(uint64_t epoch)
  {
    m_epoch = epoch;
    m_packet = 1;
    m_dequeued = 0;
    m_au = 0;
    m_active = false;
  }

  uint64_t Epoch() const { return m_epoch; }
  unsigned int Packet() const { return m_packet; }
  unsigned int Dequeue()
  {
    if (m_dequeued <= CTrueHDRestartTrace::PACKET_LIMIT)
      ++m_dequeued;
    return m_dequeued;
  }
  void Flush()
  {
    if (m_epoch && m_packet <= CTrueHDRestartTrace::PACKET_LIMIT)
      ++m_packet;
  }

  void Begin(const uint8_t* data, unsigned int bytes)
  {
    m_active = false;
    if (!m_epoch || m_packet > CTrueHDRestartTrace::PACKET_LIMIT)
      return;
    ++m_au;
    if (bytes < 8 || data[4] != 0xf8 || data[5] != 0x72 || data[6] != 0x6f ||
        data[7] != 0xba)
      return;

    m_active = true;
    m_auBytes = bytes;
    m_mapped = 0;
    m_spans = {};
    m_spanCount = 0;
    m_headerBytes = 0;
    m_crcValid = false;
    m_structureValid = false;
    if (bytes < 32)
      return;
    const unsigned int majorBytes = 28 + ((data[29] & 1) ? 2 + 2 * (data[30] >> 4) : 0);
    m_headerBytes = 4 + majorBytes;
    const unsigned int declared = 2 * (((data[0] & 15) << 8) | data[1]);
    m_structureValid = declared == bytes && bytes >= m_headerBytes && (data[8] >> 4) != 15;
    if (!m_structureValid)
      return;

    // Same major-header checksum as CAEStreamParser::SyncTrueHD. FFmpeg's
    // non-reflected 16-bit table returns the byte-swapped MSB-first remainder.
    uint16_t crc = 0;
    for (unsigned int i = 4; i < majorBytes; ++i)
    {
      crc ^= static_cast<uint16_t>(data[i]) << 8;
      for (unsigned int bit = 0; bit < 8; ++bit)
        crc = static_cast<uint16_t>((crc << 1) ^ ((crc & 0x8000) ? 0x002d : 0));
    }
    crc = static_cast<uint16_t>((crc >> 8) | (crc << 8));
    crc ^= (data[majorBytes + 1] << 8) | data[majorBytes];
    m_crcValid = crc == ((data[majorBytes + 3] << 8) | data[majorBytes + 2]);
  }

  bool Append(unsigned int offset, unsigned int bytes)
  {
    if (!m_active || !bytes)
      return false;
    if (m_spanCount && m_spanCount <= m_spans.size() &&
        m_spans[m_spanCount - 1].packet == m_packet &&
        m_spans[m_spanCount - 1].offset + m_spans[m_spanCount - 1].bytes == offset)
      m_spans[m_spanCount - 1].bytes += bytes;
    else
    {
      if (m_spanCount < m_spans.size())
        m_spans[m_spanCount] = {m_packet, offset, bytes};
      ++m_spanCount;
    }
    m_mapped += bytes;
    if (m_mapped < m_auBytes)
      return false;
    m_active = false;
    return true;
  }

  unsigned int AU() const { return m_au; }
  unsigned int AUBytes() const { return m_auBytes; }
  unsigned int HeaderBytes() const { return m_headerBytes; }
  bool StructureValid() const { return m_structureValid; }
  bool CRCValid() const { return m_crcValid; }
  bool Complete() const { return m_mapped == m_auBytes && m_spanCount <= m_spans.size(); }
  const std::array<Span, 2>& Spans() const { return m_spans; }

private:
  uint64_t m_epoch{0};
  unsigned int m_packet{1};
  unsigned int m_dequeued{0};
  unsigned int m_au{0};
  unsigned int m_auBytes{0};
  unsigned int m_headerBytes{0};
  unsigned int m_mapped{0};
  unsigned int m_spanCount{0};
  bool m_active{false};
  bool m_structureValid{false};
  bool m_crcValid{false};
  std::array<Span, 2> m_spans{};
};
} // namespace AE
