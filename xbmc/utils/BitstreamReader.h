/*
 *  Copyright (C) 2017-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstddef>
#include <stdint.h>

class CBitstreamReader
{
public:
  CBitstreamReader(const uint8_t* buf, std::size_t len);
  uint32_t ReadBits(unsigned int nbits);
  void SkipBits(uint64_t nbits);
  uint32_t GetBits(unsigned int nbits);
  uint64_t Position() const { return m_posBits; }
  uint64_t AvailableBits() const { return m_lengthBits - m_posBits; }
  bool HasError() const { return m_error; }

private:
  const uint8_t* m_buffer;
  uint64_t m_lengthBits;
  uint64_t m_posBits{0};
  bool m_error{false};
};

const uint8_t* find_start_code(const uint8_t* p, const uint8_t* end, uint32_t* state);
