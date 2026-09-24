#!/usr/bin/env python3
"""Host regression checks of the production DTS dialnorm rewrite.

Synthetic headers cover core version semantics, preservation of unrelated bits,
existing format/CRC guards, and continued DTS-HD extension processing. This does
not decode audio or establish CE compiler acceptance or receiver behavior.
"""

import binascii
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def exss_header(dialnorm):
    # ETSI TS 102 114: one asset, no static fields or DRC, 16-byte EXSS header.
    fields = [(0x64582025, 32), (0, 8), (0, 2), (0, 1), (15, 8),
              (31, 16), (0, 1), (15, 16), (3, 9), (0, 3),
              (0, 1), (1, 1), (dialnorm, 5)]
    bits = ''.join(f'{value:0{width}b}' for value, width in fields)
    header = int(bits.ljust(112, '0'), 2).to_bytes(14, 'big')
    crc = binascii.crc_hqx(header[5:], 0xffff)
    return header + crc.to_bytes(2, 'big')


def main():
    source = (ROOT / 'xbmc/cores/AudioEngine/Utils/AEStreamInfo.cpp').read_text()
    start = source.index('static inline uint32_t DTS_ReadBits(')
    rewrite = source.index('void CAEStreamParser::DefeatDTSDialNorm(', start)
    end = source.index('\n}\n', rewrite) + 2
    definitions = '\n'.join(line for line in source.splitlines()
                            if line.startswith('#define DTS_SYNC_'))
    preamble = r'''
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <vector>
class CAEStreamParser {
public:
  static void DefeatDTSDialNorm(uint8_t*, unsigned int);
};
'''
    fixtures = ''
    for name, value in [('exssOriginal', 11), ('exssDefeated', 0)]:
        values = ', '.join(f'0x{byte:02x}' for byte in exss_header(value))
        fixtures += f'const std::array<uint8_t, 16> {name} = {{{values}}};\n'
    cases = r'''
using Frame = std::vector<uint8_t>;
unsigned checks = 0, failures = 0;

// Direct byte masks form fixtures independently of the production bit helpers.
Frame core(unsigned version, unsigned dialnorm, bool cpf, uint8_t fill) {
  Frame frame(128, fill);
  frame[0] = 0x7f; frame[1] = 0xfe; frame[2] = 0x80; frame[3] = 0x01;
  frame[4] = (frame[4] & 0xfd) | (cpf ? 0x02 : 0);
  // FSIZE = 127 (128-byte core), bits 46..59.
  frame[5] &= 0xfc;
  frame[6] = 0x07;
  frame[7] = (frame[7] & 0x0f) | 0xf0;
  const unsigned shift = cpf ? 2 : 0;
  // VERNUM bits 89..92 and DIALNORM bits 100..103, after optional HCRC.
  frame[11 + shift] = (frame[11 + shift] & 0x87) | (version << 3);
  frame[12 + shift] = (frame[12 + shift] & 0xf0) | dialnorm;
  return frame;
}

void check(Frame input, const Frame& expected, const char* label) {
  ++checks;
  CAEStreamParser::DefeatDTSDialNorm(input.data(), input.size());
  if (input != expected) {
    if (++failures <= 8) std::fprintf(stderr, "FAIL: %s (case %u)\n", label, checks);
  }
  const Frame once = input;
  CAEStreamParser::DefeatDTSDialNorm(input.data(), input.size());
  if (input != once) {
    ++failures;
    std::fprintf(stderr, "FAIL: not idempotent: %s\n", label);
  }
}

int main() {
  for (unsigned version = 0; version < 16; ++version)
    for (unsigned dialnorm = 0; dialnorm < 16; ++dialnorm)
      for (bool cpf : {false, true})
        for (uint8_t fill : {0x00, 0xff}) {
          const Frame input = core(version, dialnorm, cpf, fill);
          const bool defeat = !cpf && (version == 6 || version == 7);
          const Frame expected = defeat ? core(7, 0, false, fill) : input;
          check(input, expected, "core version/dialnorm/CRC matrix");
        }

  // Extension defeat must still run when the core needs no edit or is guarded.
  for (unsigned version = 0; version < 16; ++version)
    for (bool cpf : {false, true})
      for (bool alreadyZero : {false, true})
        for (bool validCRC : {false, true}) {
          Frame input = core(version, 0, cpf, 0xa5);
          Frame expected = !cpf && (version == 6 || version == 7)
                               ? core(7, 0, false, 0xa5) : input;
          auto extension = alreadyZero ? exssDefeated : exssOriginal;
          if (!validCRC) extension.back() ^= 1;
          const auto expectedExtension = validCRC ? exssDefeated : extension;
          input.insert(input.end(), extension.begin(), extension.end());
          expected.insert(expected.end(), expectedExtension.begin(), expectedExtension.end());
          // EXSS frame size is 32 bytes; include a payload sentinel.
          input.resize(160, 0x5a);
          expected.resize(160, 0x5a);
          check(input, expected, "extension continuation and CRC preservation");
        }

  for (uint32_t sync : {0x1fffe800u, 0xff1f00e8u, 0xfe7f0180u, 0x12345678u}) {
    Frame input = core(6, 5, false, 0xa5);
    for (unsigned i = 0; i < 4; ++i) input[i] = sync >> (24 - 8 * i);
    check(input, input, "unsupported core packing");
  }
  for (unsigned size = 0; size < 16; ++size) {
    Frame input = core(6, 5, false, 0xa5);
    input.resize(size);
    check(input, input, "short input");
  }
  std::printf("%u DTS dialnorm cases, %u failures (each checked for idempotence)\n",
              checks, failures);
  return failures ? 1 : 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='kodi-dts-dialnorm-') as tmp:
        src = Path(tmp) / 'test.cpp'
        binary = Path(tmp) / 'test'
        src.write_text(preamble + definitions + '\n' + source[start:end] + fixtures + cases)
        subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        '-o', str(binary), str(src)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
