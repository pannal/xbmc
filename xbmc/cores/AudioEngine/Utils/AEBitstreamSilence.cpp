/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AEBitstreamSilence.h"

#include "AEStreamInfo.h"

#include <algorithm>

namespace
{
// AC-3 CRC polynomial x^16 + x^15 + x^2 + 1 and the inverse of x modulo it
constexpr uint32_t CRC16_POLY = 0x18005;
constexpr uint32_t CRC16_INVERSE_X = 0xC002;

constexpr unsigned int AC3_BLOCKS = 6;
// The last 18 bits of a frame are auxdatae, crcrsv and crc2
constexpr unsigned int AC3_TRAILER_BITS = 18;
constexpr unsigned int AC3_SKIPL_BITS = 9;
constexpr unsigned int AC3_MAX_SKIP_BYTES = 511;
constexpr unsigned int AC3_EXP_REUSE = 0;
constexpr unsigned int AC3_EXP_D45 = 3;
constexpr unsigned int AC3_FBW_CHANNELS[8] = {2, 1, 2, 3, 3, 4, 4, 5};
constexpr unsigned int AC3_BITRATES[19] = {32,  40,  48,  56,  64,  80,  96,  112, 128, 160,
                                           192, 224, 256, 320, 384, 448, 512, 576, 640};
// Exponent deltas that ramp from the largest absolute exponent of 15 to 24
constexpr unsigned int AC3_FBW_EXPONENT_GROUPS[6] = {124, 117, 62, 62, 62, 62};
constexpr unsigned int AC3_LFE_EXPONENT_GROUPS[2] = {124, 117};

constexpr unsigned int DTS_CHANNELS[10] = {1, 2, 2, 2, 2, 3, 3, 4, 4, 5};
constexpr unsigned int DTS_QUANT_INDEX_BITS[10] = {1, 2, 2, 2, 2, 3, 3, 3, 3, 3};

uint16_t Crc16(const uint8_t* data, unsigned int size)
{
  uint32_t crc = 0;
  for (unsigned int i = 0; i < size; ++i)
  {
    crc ^= static_cast<uint32_t>(data[i]) << 8;
    for (int bit = 0; bit < 8; ++bit)
    {
      crc <<= 1;
      if (crc & 0x10000)
        crc ^= CRC16_POLY;
    }
  }
  return static_cast<uint16_t>(crc);
}

uint32_t MulPoly(uint32_t a, uint32_t b)
{
  uint32_t c = 0;
  while (a)
  {
    if (a & 1)
      c ^= b;
    a >>= 1;
    b <<= 1;
    if (b & 0x10000)
      b ^= CRC16_POLY;
  }
  return c;
}

uint32_t PowPoly(uint32_t a, unsigned int n)
{
  uint32_t result = 1;
  while (n)
  {
    if (n & 1)
      result = MulPoly(result, a);
    a = MulPoly(a, a);
    n >>= 1;
  }
  return result;
}

// Copies header fields from the source frame and writes new fields into a
// zeroed frame of the same size
class CBitCopier
{
public:
  CBitCopier(const uint8_t* src, uint8_t* dst, unsigned int size)
    : m_src(src), m_dst(dst), m_bits(size * 8)
  {
  }

  uint32_t Read(unsigned int bits)
  {
    if (bits > 32 || bits > m_bits - m_readPos)
    {
      m_failed = true;
      m_readPos = m_bits;
      return 0;
    }

    uint32_t value = 0;
    for (; bits > 0; --bits, ++m_readPos)
      value = (value << 1) | ((m_src[m_readPos >> 3] >> (7 - (m_readPos & 7))) & 1);
    return value;
  }

  void Write(unsigned int bits, uint32_t value)
  {
    if (bits > 32 || bits > m_bits - m_writePos)
    {
      m_failed = true;
      m_writePos = m_bits;
      return;
    }

    for (; bits > 0; --bits, ++m_writePos)
    {
      if ((value >> (bits - 1)) & 1)
        m_dst[m_writePos >> 3] |= 0x80 >> (m_writePos & 7);
    }
  }

  void Zeros(unsigned int bits)
  {
    if (bits > m_bits - m_writePos)
    {
      m_failed = true;
      m_writePos = m_bits;
      return;
    }
    m_writePos += bits;
  }

  uint32_t Copy(unsigned int bits)
  {
    const uint32_t value = Read(bits);
    Write(bits, value);
    return value;
  }

  void CopyLong(unsigned int bits)
  {
    for (; bits > 16; bits -= 16)
      Copy(16);
    Copy(bits);
  }

  bool CopyFlag() { return Copy(1) != 0; }

  void CopyOptional(unsigned int bits)
  {
    if (CopyFlag())
      CopyLong(bits);
  }

  unsigned int WritePos() const { return m_writePos; }
  bool Failed() const { return m_failed; }

private:
  const uint8_t* m_src;
  uint8_t* m_dst;
  unsigned int m_bits;
  unsigned int m_readPos = 0;
  unsigned int m_writePos = 0;
  bool m_failed = false;
};

void WriteAC3Exponents(CBitCopier& bits, unsigned int fbw, bool lfe)
{
  bits.Zeros(6 * fbw); // chbwcod
  for (unsigned int ch = 0; ch < fbw; ++ch)
  {
    bits.Write(4, 15);
    for (const unsigned int group : AC3_FBW_EXPONENT_GROUPS)
      bits.Write(7, group);
    bits.Zeros(2); // gainrng
  }
  if (lfe)
  {
    bits.Write(4, 15);
    for (const unsigned int group : AC3_LFE_EXPONENT_GROUPS)
      bits.Write(7, group);
  }
}

void WriteAC3BitAllocation(CBitCopier& bits)
{
  bits.Write(1, 1); // baie
  bits.Write(2, 2); // sdcycod
  bits.Write(2, 1); // fdcycod
  bits.Write(2, 1); // sgaincod
  bits.Write(2, 3); // dbpbcod
  bits.Write(3, 4); // floorcod
}

void WriteSkip(CBitCopier& bits, unsigned int bytes)
{
  bits.Write(1, bytes > 0 ? 1 : 0); // skiple
  if (bytes > 0)
  {
    bits.Write(AC3_SKIPL_BITS, bytes); // skipl
    bits.Zeros(8 * bytes); // skipfld
  }
}

// Some receivers mute AC-3 frames whose audio blocks leave most of the frame
// unused. Encoders fill frames with mantissas, so silent frames fill the space
// with skip fields instead, spread evenly over the blocks. A skip field costs
// skipl plus at least one byte, so the later blocks get them when there is not
// room for six.
void SpreadSkipBytes(unsigned int unusedBits, unsigned int (&skip)[AC3_BLOCKS])
{
  const unsigned int fields = std::min(AC3_BLOCKS, unusedBits / (AC3_SKIPL_BITS + 8));
  if (fields == 0)
    return;

  const unsigned int bytes =
      std::min((unusedBits - fields * AC3_SKIPL_BITS) / 8, fields * AC3_MAX_SKIP_BYTES);
  for (unsigned int i = 0; i < fields; ++i)
    skip[AC3_BLOCKS - 1 - i] = bytes / fields + (i < bytes % fields ? 1 : 0);
}

void WriteCrc2(uint8_t* frame, unsigned int start, unsigned int size)
{
  uint16_t crc = Crc16(frame + start, size - start - 2);
  if (crc == 0x0B77)
  {
    // keep crc2 from looking like a sync word
    frame[size - 3] ^= 1;
    crc = Crc16(frame + start, size - start - 2);
  }
  frame[size - 2] = static_cast<uint8_t>(crc >> 8);
  frame[size - 1] = static_cast<uint8_t>(crc & 0xFF);
}

unsigned int AC3FrameSize(unsigned int fscod, unsigned int frmsizecod)
{
  if (fscod == 3 || frmsizecod > 37)
    return 0;

  const unsigned int bitrate = AC3_BITRATES[frmsizecod >> 1];
  switch (fscod)
  {
    case 0:
      return bitrate * 4;
    case 1:
      return (bitrate * 320 / 147 + (frmsizecod & 1)) * 2;
    default:
      return bitrate * 6;
  }
}

// Silence is coded with minimum exponents and SNR offsets of zero, which leave
// every mantissa without bits, and with dithering turned off. block1End and end
// receive the bit positions after blocks 1 and 5.
bool WriteAC3Frame(const uint8_t* src,
                   uint8_t* dst,
                   unsigned int size,
                   const unsigned int (&skip)[AC3_BLOCKS],
                   unsigned int& block1End,
                   unsigned int& end)
{
  CBitCopier bits(src, dst, size);

  bits.Copy(16); // syncword
  bits.Read(16); // crc1
  bits.Zeros(16);
  bits.Copy(2); // fscod
  bits.Copy(6); // frmsizecod
  bits.Copy(5); // bsid
  bits.Copy(3); // bsmod
  const unsigned int acmod = bits.Copy(3);
  if ((acmod & 1) && acmod != 1)
    bits.Copy(2); // cmixlev
  if (acmod & 4)
    bits.Copy(2); // surmixlev
  if (acmod == 2)
    bits.Copy(2); // dsurmod
  const bool lfe = bits.CopyFlag();
  const unsigned int programs = acmod == 0 ? 2 : 1;
  for (unsigned int i = 0; i < programs; ++i)
  {
    bits.Copy(5); // dialnorm
    bits.CopyOptional(8); // compr
    bits.CopyOptional(8); // langcod
    bits.CopyOptional(7); // mixlevel, roomtyp
  }
  bits.Copy(2); // copyrightb, origbs
  bits.CopyOptional(14); // timecod1 or xbsi1
  bits.CopyOptional(14); // timecod2 or xbsi2
  if (bits.CopyFlag()) // addbsie
    bits.CopyLong((bits.Copy(6) + 1) * 8);

  const unsigned int fbw = AC3_FBW_CHANNELS[acmod];
  for (unsigned int blk = 0; blk < AC3_BLOCKS; ++blk)
  {
    const bool first = blk == 0;
    bits.Zeros(fbw); // blksw
    bits.Zeros(fbw); // dithflag
    bits.Zeros(programs); // dynrnge
    if (first)
      bits.Write(2, 2); // cplstre, no coupling
    else
      bits.Zeros(1); // cplstre
    if (acmod == 2)
    {
      if (first)
      {
        bits.Write(1, 1); // rematstr
        bits.Zeros(4); // rematflg
      }
      else
        bits.Zeros(1); // rematstr
    }
    for (unsigned int ch = 0; ch < fbw; ++ch)
      bits.Write(2, first ? AC3_EXP_D45 : AC3_EXP_REUSE); // chexpstr
    if (lfe)
      bits.Write(1, first ? 1 : 0); // lfeexpstr
    if (first)
    {
      WriteAC3Exponents(bits, fbw, lfe);
      WriteAC3BitAllocation(bits);
      bits.Write(1, 1); // snroffste
      bits.Zeros(6); // csnroffst
      for (unsigned int ch = 0; ch < fbw + (lfe ? 1 : 0); ++ch)
      {
        bits.Zeros(4); // fsnroffst
        bits.Write(3, 4); // fgaincod
      }
    }
    else
    {
      bits.Zeros(1); // baie
      bits.Zeros(1); // snroffste
    }
    bits.Zeros(1); // deltbaie
    WriteSkip(bits, skip[blk]);
    if (blk == 1)
      block1End = bits.WritePos();
  }

  end = bits.WritePos();
  return !bits.Failed();
}

bool GenerateAC3Frame(const uint8_t* src, uint8_t* dst, unsigned int size)
{
  const unsigned int size58 = ((size >> 2) + (size >> 4)) << 1;
  const unsigned int limit = size * 8 - AC3_TRAILER_BITS;
  unsigned int skip[AC3_BLOCKS] = {};
  unsigned int block1End = 0;
  unsigned int end = 0;

  // blocks 0 and 1 must fit in the part of the frame that crc1 protects
  if (!WriteAC3Frame(src, dst, size, skip, block1End, end) || block1End > size58 * 8 ||
      end > limit)
    return false;

  // Skip bytes that would take block 0 or 1 out of that part move to blocks 2
  // to 5, which already have skip fields whenever block 0 or 1 has one, so the
  // moved bytes still fit in the frame
  SpreadSkipBytes(limit - end, skip);
  const unsigned int early = (size58 * 8 - block1End) / 2;
  const unsigned int earlyBytes = early > AC3_SKIPL_BITS ? (early - AC3_SKIPL_BITS) / 8 : 0;
  unsigned int spare = 0;
  for (unsigned int blk = 0; blk < 2; ++blk)
  {
    if (skip[blk] > earlyBytes)
    {
      spare += skip[blk] - earlyBytes;
      skip[blk] = earlyBytes;
    }
  }
  for (unsigned int blk = 2; blk < AC3_BLOCKS; ++blk)
  {
    const unsigned int moved = std::min(spare, AC3_MAX_SKIP_BYTES - skip[blk]);
    skip[blk] += moved;
    spare -= moved;
  }

  std::fill(dst, dst + size, 0);
  if (!WriteAC3Frame(src, dst, size, skip, block1End, end) || block1End > size58 * 8 ||
      end > limit)
    return false;

  const uint32_t crc1 =
      MulPoly(Crc16(dst + 4, size58 - 4), PowPoly(CRC16_INVERSE_X, 8 * size58 - 16));
  dst[2] = static_cast<uint8_t>(crc1 >> 8);
  dst[3] = static_cast<uint8_t>(crc1 & 0xFF);
  WriteCrc2(dst, size58, size);

  return Crc16(dst + 2, size58 - 2) == 0 && Crc16(dst + size58, size - size58) == 0;
}

// end receives the bit position after the last block
bool WriteEAC3Frame(const uint8_t* src,
                    uint8_t* dst,
                    unsigned int size,
                    const unsigned int (&skip)[AC3_BLOCKS],
                    unsigned int& end)
{
  CBitCopier bits(src, dst, size);

  bits.Copy(16); // syncword
  const unsigned int strmtyp = bits.Copy(2);
  bits.Copy(3); // substreamid
  const unsigned int frmsiz = bits.Copy(11);
  const unsigned int fscod = bits.Copy(2);
  const unsigned int numblkscod = bits.Copy(2);
  const unsigned int acmod = bits.Copy(3);
  const bool lfe = bits.CopyFlag();
  const unsigned int bsid = bits.Copy(5);
  if (strmtyp > 1 || (frmsiz + 1) * 2 != size || fscod == 3 || numblkscod != 3 || bsid != 16)
    return false;

  const bool independent = strmtyp == 0;
  const unsigned int programs = acmod == 0 ? 2 : 1;
  for (unsigned int i = 0; i < programs; ++i)
  {
    bits.Copy(5); // dialnorm
    bits.CopyOptional(8); // compr
  }
  if (!independent)
    bits.CopyOptional(16); // chanmap
  if (bits.CopyFlag()) // mixmdate
  {
    if (acmod > 2)
    {
      bits.Copy(2); // dmixmod
      if (acmod & 1)
        bits.Copy(6); // ltrtcmixlev, lorocmixlev
      if (acmod & 4)
        bits.Copy(6); // ltrtsurmixlev, lorosurmixlev
    }
    if (lfe)
      bits.CopyOptional(5); // lfemixlevcod
    if (independent)
    {
      for (unsigned int i = 0; i < programs; ++i)
        bits.CopyOptional(6); // pgmscl
      bits.CopyOptional(6); // extpgmscl
      switch (bits.Copy(2)) // mixdef
      {
        case 1:
          bits.Copy(5);
          break;
        case 2:
          bits.Copy(12);
          break;
        case 3:
          bits.CopyLong((bits.Copy(5) + 2) * 8);
          break;
        default:
          break;
      }
      if (acmod < 2)
      {
        for (unsigned int i = 0; i < programs; ++i)
          bits.CopyOptional(14); // paninfo
      }
      if (bits.CopyFlag()) // frmmixcfginfoe
      {
        for (unsigned int blk = 0; blk < AC3_BLOCKS; ++blk)
          bits.CopyOptional(5); // blkmixcfginfo
      }
    }
  }
  if (bits.CopyFlag()) // infomdate
  {
    bits.Copy(5); // bsmod, copyrightb, origbs
    if (acmod == 2)
      bits.Copy(4); // dsurmod, dheadphonmod
    if (acmod >= 6)
      bits.Copy(2); // dsurexmod
    for (unsigned int i = 0; i < programs; ++i)
      bits.CopyOptional(8); // mixlevel, roomtyp, adconvtyp
    bits.Copy(1); // sourcefscod
  }
  if (bits.CopyFlag()) // addbsie
    bits.CopyLong((bits.Copy(6) + 1) * 8);

  const unsigned int fbw = AC3_FBW_CHANNELS[acmod];
  bits.Write(1, 1); // expstre
  bits.Zeros(1); // ahte
  bits.Zeros(2); // snroffststr
  bits.Zeros(1); // transproce
  bits.Zeros(1); // blkswe
  bits.Write(1, 1); // dithflage
  bits.Write(1, 1); // bamode
  bits.Zeros(2); // frmfgaincode, dbaflde
  bits.Write(1, 1); // skipflde
  bits.Zeros(1); // spxattene
  if (acmod > 1)
    bits.Zeros(AC3_BLOCKS); // cplinu, then cplstre for each later block
  for (unsigned int blk = 0; blk < AC3_BLOCKS; ++blk)
  {
    for (unsigned int ch = 0; ch < fbw; ++ch)
      bits.Write(2, blk == 0 ? AC3_EXP_D45 : AC3_EXP_REUSE); // chexpstr
  }
  if (lfe)
    bits.Write(AC3_BLOCKS, 1 << (AC3_BLOCKS - 1)); // lfeexpstr
  if (independent)
    bits.Zeros(5 * fbw); // convexpstr
  bits.Zeros(6); // frmcsnroffst
  bits.Zeros(4); // frmfsnroffst
  bits.Zeros(1); // blkstrtinfoe

  for (unsigned int blk = 0; blk < AC3_BLOCKS; ++blk)
  {
    const bool first = blk == 0;
    bits.Zeros(fbw); // dithflag
    bits.Zeros(programs); // dynrnge
    bits.Zeros(1); // spxinu, then spxstre
    if (acmod == 2)
      bits.Zeros(first ? 4 : 1); // rematflg, then rematstr
    if (first)
    {
      WriteAC3Exponents(bits, fbw, lfe);
      WriteAC3BitAllocation(bits);
    }
    else
      bits.Zeros(1); // baie
    if (independent)
      bits.Zeros(1); // convsnroffste
    WriteSkip(bits, skip[blk]);
  }

  end = bits.WritePos();
  return !bits.Failed();
}

bool GenerateEAC3Frame(const uint8_t* src, uint8_t* dst, unsigned int size)
{
  const unsigned int limit = size * 8 - AC3_TRAILER_BITS;
  unsigned int skip[AC3_BLOCKS] = {};
  unsigned int end = 0;
  if (!WriteEAC3Frame(src, dst, size, skip, end) || end > limit)
    return false;

  SpreadSkipBytes(limit - end, skip);
  std::fill(dst, dst + size, 0);
  if (!WriteEAC3Frame(src, dst, size, skip, end) || end > limit)
    return false;

  WriteCrc2(dst, 2, size);
  return Crc16(dst + 2, size - 2) == 0;
}

bool GenerateDolby(const CAEStreamInfo& info,
                   const uint8_t* data,
                   unsigned int size,
                   std::vector<uint8_t>& out)
{
  // E-AC-3 packets shorter than six blocks are collected into one burst, which
  // a silent packet must not join
  if (info.m_type == CAEStreamInfo::STREAM_TYPE_EAC3 && info.m_repeat != 1)
    return false;

  unsigned int offset = 0;
  while (offset < size)
  {
    const uint8_t* frame = data + offset;
    const unsigned int left = size - offset;
    if (left < 6 || frame[0] != 0x0B || frame[1] != 0x77)
      return false;

    const unsigned int bsid = frame[5] >> 3;
    unsigned int frameSize = 0;
    bool generated = false;
    if (bsid <= 8)
    {
      frameSize = AC3FrameSize(frame[4] >> 6, frame[4] & 0x3F);
      generated = frameSize > 0 && frameSize <= left &&
                  GenerateAC3Frame(frame, out.data() + offset, frameSize);
    }
    else if (bsid == 16)
    {
      frameSize = ((((frame[2] & 0x07) << 8) | frame[3]) + 1) * 2;
      generated = frameSize <= left && GenerateEAC3Frame(frame, out.data() + offset, frameSize);
    }

    if (!generated)
      return false;
    offset += frameSize;
  }
  return true;
}

// Silence is coded as two subbands per channel with no bits allocated, so no
// subband samples follow
bool GenerateDTSFrame(const uint8_t* src, uint8_t* dst, unsigned int size)
{
  CBitCopier bits(src, dst, size);

  if (bits.Copy(16) != 0x7FFE || bits.Copy(16) != 0x8001)
    return false;
  bits.Copy(1); // FTYPE
  if (bits.Copy(5) != 31) // SHORT
    return false;
  const bool crcPresent = bits.Read(1) != 0;
  bits.Zeros(1); // CPF, the silent frame has no CRCs
  const unsigned int blocks = bits.Copy(7) + 1; // NBLKS
  const unsigned int frameSize = bits.Copy(14) + 1; // FSIZE
  const unsigned int amode = bits.Copy(6);
  bits.Copy(4); // SFREQ
  bits.Copy(5); // RATE
  if (blocks % 8 || frameSize != size || amode >= 10 || bits.Read(1))
    return false;
  bits.Zeros(1); // FixedBit
  const bool drc = bits.CopyFlag(); // DYNF
  bits.Read(2);
  bits.Zeros(2); // TIMEF, AUXF
  bits.Copy(1); // HDCD
  bits.Copy(3); // EXT_AUDIO_ID
  if (bits.Copy(1)) // EXT_AUDIO
    return false;
  const bool syncEverySubsubframe = bits.CopyFlag(); // ASPF
  const unsigned int lfe = bits.Copy(2); // LFF
  if (lfe == 3)
    return false;
  bits.Copy(1); // HFLAG
  if (crcPresent)
    bits.Read(16); // HCRC
  bits.Copy(1); // FILTS
  bits.Copy(4); // VERNUM
  bits.Copy(2); // CHIST
  bits.Copy(3); // PCMR
  bits.Copy(2); // SUMF, SUMS
  bits.Copy(4); // DIALNORM

  const unsigned int channels = DTS_CHANNELS[amode];
  unsigned int subsubframes = blocks / 8;
  const unsigned int subframes = (subsubframes + 3) / 4;
  bits.Write(4, subframes - 1); // SUBFS
  bits.Write(3, channels - 1); // PCHS
  bits.Zeros(5 * channels); // SUBS, two subbands
  for (unsigned int ch = 0; ch < channels; ++ch)
    bits.Write(5, 1); // VQSUB, no VQ subbands
  bits.Zeros(3 * channels); // JOINX
  bits.Zeros(2 * channels); // THUFF
  bits.Zeros(3 * channels); // SHUFF
  for (unsigned int ch = 0; ch < channels; ++ch)
    bits.Write(3, 5); // BHUFF, 4-bit allocation indexes
  // SEL without Huffman codes, so no scale factor adjustments follow
  for (const unsigned int selBits : DTS_QUANT_INDEX_BITS)
  {
    for (unsigned int ch = 0; ch < channels; ++ch)
      bits.Write(selBits, (1u << selBits) - 1);
  }

  for (unsigned int sf = 0; sf < subframes; ++sf)
  {
    const unsigned int count = std::min(subsubframes, 4u);
    subsubframes -= count;
    bits.Write(2, count - 1); // SSC
    bits.Zeros(3); // PSC
    bits.Zeros(2 * channels); // PMODE
    bits.Zeros(8 * channels); // ABITS
    if (drc)
      bits.Zeros(8); // RANGE
    if (lfe)
      bits.Zeros(8 * (2 * lfe * count + 1)); // LFE samples and scale factor
    for (unsigned int ssf = 0; ssf < count; ++ssf)
    {
      if (syncEverySubsubframe || ssf == count - 1)
        bits.Write(16, 0xFFFF); // DSYNC
    }
  }

  return !bits.Failed();
}

bool GenerateDTS(const CAEStreamInfo& info,
                 const uint8_t* data,
                 unsigned int size,
                 std::vector<uint8_t>& out)
{
  if (size < 4)
    return false;
  if (!info.m_dataIsLE)
    return GenerateDTSFrame(data, out.data(), size);
  if (size & 1)
    return false;

  std::vector<uint8_t> frame(size);
  std::vector<uint8_t> silence(size, 0);
  for (unsigned int i = 0; i < size; i += 2)
  {
    frame[i] = data[i + 1];
    frame[i + 1] = data[i];
  }
  if (!GenerateDTSFrame(frame.data(), silence.data(), size))
    return false;
  for (unsigned int i = 0; i < size; i += 2)
  {
    out[i] = silence[i + 1];
    out[i + 1] = silence[i];
  }
  return true;
}

// A DTS-HD packet is a core frame followed by an extension substream. Its
// silent packet is a silent copy of the core alone, which the packer still
// sends as DTS-HD so the receiver sees no format change.
bool GenerateDTSHD(const CAEStreamInfo& info,
                   const uint8_t* data,
                   unsigned int size,
                   std::vector<uint8_t>& out)
{
  if (info.m_dataIsLE || size < 8)
    return false;
  if (data[0] != 0x7F || data[1] != 0xFE || data[2] != 0x80 || data[3] != 0x01)
    return false;
  const unsigned int coreSize =
      (((data[5] & 0x03) << 12) | (data[6] << 4) | (data[7] >> 4)) + 1; // FSIZE
  if (coreSize + 4 > size)
    return false;
  const uint8_t* exss = data + coreSize;
  if (exss[0] != 0x64 || exss[1] != 0x58 || exss[2] != 0x20 || exss[3] != 0x25)
    return false;

  out.assign(coreSize, 0);
  return GenerateDTSFrame(data, out.data(), coreSize);
}
} // namespace

bool CAEBitstreamSilence::Generate(const CAEStreamInfo& info,
                                   const uint8_t* data,
                                   unsigned int size,
                                   std::vector<uint8_t>& out)
{
  out.assign(size, 0);

  bool generated = false;
  if (data && size > 0)
  {
    switch (info.m_type)
    {
      case CAEStreamInfo::STREAM_TYPE_AC3:
      case CAEStreamInfo::STREAM_TYPE_EAC3:
        generated = GenerateDolby(info, data, size, out);
        break;

      case CAEStreamInfo::STREAM_TYPE_DTS_512:
      case CAEStreamInfo::STREAM_TYPE_DTS_1024:
      case CAEStreamInfo::STREAM_TYPE_DTS_2048:
      case CAEStreamInfo::STREAM_TYPE_DTSHD_CORE:
        generated = GenerateDTS(info, data, size, out);
        break;

      case CAEStreamInfo::STREAM_TYPE_DTSHD:
      case CAEStreamInfo::STREAM_TYPE_DTSHD_MA:
        generated = GenerateDTSHD(info, data, size, out);
        break;

      default:
        break;
    }
  }

  if (!generated)
    out.clear();
  return generated;
}
