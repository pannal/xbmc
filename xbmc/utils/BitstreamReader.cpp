/*
 *  Copyright (C) 2017-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "BitstreamReader.h"

extern "C"
{
#include <libavutil/intreadwrite.h>
}

CBitstreamReader::CBitstreamReader(const uint8_t* buf, std::size_t len)
  : m_buffer(buf), m_lengthBits(static_cast<uint64_t>(len) * 8)
{
}

uint32_t CBitstreamReader::ReadBits(unsigned int nbits)
{
  const uint32_t value = GetBits(nbits);
  if (!m_error)
    m_posBits += nbits;
  return value;
}

void CBitstreamReader::SkipBits(uint64_t nbits)
{
  if (nbits > AvailableBits())
    m_error = true;
  else if (!m_error)
    m_posBits += nbits;
}

uint32_t CBitstreamReader::GetBits(unsigned int nbits)
{
  if (nbits > 32 || nbits > AvailableBits())
    m_error = true;
  if (m_error || nbits == 0)
    return 0;

  const unsigned int offset = m_posBits % 8;
  const unsigned int bytes = (offset + nbits + 7) / 8;
  const uint8_t* data = m_buffer + m_posBits / 8;
  uint64_t value = 0;
  for (unsigned int i = 0; i < bytes; ++i)
    value = (value << 8) | data[i];

  return (value >> (bytes * 8 - offset - nbits)) & ((uint64_t{1} << nbits) - 1);
}

const uint8_t* find_start_code(const uint8_t *p, const uint8_t *end, uint32_t *state)
{
  if (p >= end)
    return end;

  for (int i = 0; i < 3; i++)
  {
    uint32_t tmp = *state << 8;
    *state = tmp + *(p++);
    if (tmp == 0x100 || p == end)
      return p;
  }

  while (p < end)
  {
    if (p[-1] > 1) p += 3;
    else if (p[-2]) p += 2;
    else if (p[-3] | (p[-1] - 1)) p++;
    else {
      p++;
      break;
    }
  }

  p = (p < end)? p - 4 : end - 4;
  *state = AV_RB32(p);

  return p + 4;
}
