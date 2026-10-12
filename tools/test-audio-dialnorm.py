#!/usr/bin/env python3
"""Exercise real passthrough parsing/emission and dialnorm labels on the host.

The parser is compiled whole; only logging is stubbed. Synthetic headers do not
establish decoder/receiver behavior. Set FFMPEG_INCLUDE to an FFmpeg include
folder if its headers are not installed. --baseline REV also compares every
emitted byte against that revision, with identical existing defeat switches.
"""
import argparse
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'xbmc/cores/AudioEngine/Utils'

HARNESS = r'''
#include <bits/stdc++.h>
#define private public
#include "AEStreamInfo.h"
#undef private
using Bytes = std::vector<uint8_t>;
unsigned checks = 0;
void check(bool value) { ++checks; if (!value) std::abort(); }
void bits(Bytes& f, unsigned start, unsigned width, unsigned value) {
  for (unsigned n = 0; n < width; ++n) {
    const unsigned p = start + n, mask = 1u << (7 - p % 8);
    f[p / 8] = (f[p / 8] & ~mask) | (((value >> (width - 1 - n)) & 1) ? mask : 0);
  }
}
Bytes ac3(unsigned dn, unsigned mode) {
  Bytes f(128, 0x55);
  f[0] = 0x0b; f[1] = 0x77; f[4] = 0; f[5] = 8 << 3;
  // Independent BSI bit positions for the eight acmod values.
  const unsigned positions[] = {52, 52, 54, 54, 54, 56, 54, 56};
  bits(f, 48, 3, mode); bits(f, positions[mode], 5, dn);
  return f;
}
Bytes eac3(unsigned dn, unsigned type = 0, unsigned sub = 0, unsigned mode = 2) {
  Bytes f(32, 0x55);
  f[0] = 0x0b; f[1] = 0x77;
  f[2] = (type << 6) | (sub << 3); f[3] = 15;
  f[4] = 0x30 | (mode << 1); f[5] = 16 << 3;
  bits(f, 45, 5, dn);
  return f;
}
void metadata(const CAEStreamInfo& i, bool available, int original, int applied) {
#ifdef DIALNORM_METADATA
  check(i.m_hasDialNorm == available);
  if (available) { check(i.m_dialNormOriginal == original); check(i.m_dialNormApplied == applied); }
#endif
}
void output(const Bytes& f) { std::cout.write(reinterpret_cast<const char*>(f.data()), f.size()); }
Bytes packet(CAEStreamParser& p, const Bytes& f, CAEStreamInfo::DataType type) {
  check(f.size() <= sizeof(p.m_buffer));
  std::copy(f.begin(), f.end(), p.m_buffer);
  p.m_bufferSize = p.m_fsize = f.size(); p.m_info.m_type = type;
  uint8_t* data = nullptr; unsigned size = 0;
  p.GetPacket(&data, &size);
  Bytes result(data, data + size); delete[] data;
  check(size == f.size()); output(result); return result;
}
Bytes truehd(CAEStreamParser& p, unsigned dn, bool atmos, unsigned extension) {
  unsigned major = extension ? 30 + extension * 2 : 28;
  Bytes f(4 + major, 0);
  f[1] = f.size() / 2;
  f[4] = 0xf8; f[5] = 0x72; f[6] = 0x6f; f[7] = 0xba;
  f[11] = 1; f[20] = 0x10; f[21] = atmos ? 0x80 : 0;
  if (extension) { f[29] = 1; f[30] = extension << 4; bits(f, 244, 5, dn); }
  const uint16_t crc = av_crc(p.m_crcTrueHD, 0, f.data() + 4, major - 4) ^
                       ((f[major + 1] << 8) | f[major]);
  f[major + 2] = crc; f[major + 3] = crc >> 8;
  return f;
}
int main() {
  CAEStreamParser p;
  for (bool defeat : {false, true}) {
    p.SetDefeatAC3DialNorm(defeat);
    for (unsigned mode = 0; mode < 8; ++mode) {
      for (unsigned dn : {0, 1, 27, 31}) {
        auto original = ac3(dn, mode);
        auto result = packet(p, original, CAEStreamInfo::STREAM_TYPE_AC3);
        metadata(p.m_info, mode != 0 && dn != 0, static_cast<int>(dn) - 31,
                 defeat ? 0 : static_cast<int>(dn) - 31);
        if (!defeat || dn == 31) check(result == original);
      }
    }
    for (unsigned type = 0; type < 4; ++type) {
      for (unsigned sub : {0, 1}) {
        for (unsigned dn : {0, 1, 27, 31}) {
          auto original = eac3(dn, type, sub);
          auto result = packet(p, original, CAEStreamInfo::STREAM_TYPE_EAC3);
          metadata(p.m_info, type != 1 && type != 3 && sub == 0 && dn != 0,
                   static_cast<int>(dn) - 31, defeat ? 0 : static_cast<int>(dn) - 31);
          if (!defeat || type == 1 || dn == 31) check(result == original);
        }
      }
    }
    // Dependent substream bytes must be retained; report primary presentation.
    auto combined = eac3(25); const auto dependent = eac3(19, 1);
    combined.insert(combined.end(), dependent.begin(), dependent.end());
    auto result = packet(p, combined, CAEStreamInfo::STREAM_TYPE_EAC3);
    metadata(p.m_info, true, -6, defeat ? 0 : -6);
    check(std::equal(dependent.begin(), dependent.end(), result.begin() + 32));
    packet(p, eac3(27, 0, 0, 0), CAEStreamInfo::STREAM_TYPE_EAC3);
    metadata(p.m_info, false, 0, 0);
  }
  for (bool defeat : {false, true}) {
    p.SetDefeatTrueHDDialNorm(defeat);
    for (unsigned dn : {0, 1, 27, 31}) {
      p.Reset(); auto f = truehd(p, dn, true, 2); auto original = f;
      check(p.SyncTrueHD(f.data(), f.size()) == 0 && p.m_hasSync);
      metadata(p.m_info, dn != 0, static_cast<int>(dn) - 31,
               defeat ? 0 : static_cast<int>(dn) - 31);
      if (!defeat || dn == 31) check(f == original);
      output(f);
      // A real minor frame (one substream, parity nibble 0xf) must retain
      // the preceding major sync's figures.
      Bytes minor(8, 0); minor[1] = 4; minor[2] = 0x0b;
      check(p.SyncTrueHD(minor.data(), minor.size()) == 0 && p.m_hasSync);
      metadata(p.m_info, dn != 0, static_cast<int>(dn) - 31,
               defeat ? 0 : static_cast<int>(dn) - 31);
      output(minor);
      // Format equality must ignore metadata, so changing attenuation cannot
      // reopen an audio sink.
      auto changed = p.m_info;
#ifdef DIALNORM_METADATA
      changed.m_dialNormApplied = -17; changed.m_hasDialNorm = !changed.m_hasDialNorm;
#endif
      check(changed == p.m_info);
      p.Reset(); metadata(p.m_info, false, 0, 0);
    }
    for (bool atmos : {false, true}) {
      auto f = truehd(p, 27, atmos, 0);
      check(p.SyncTrueHD(f.data(), f.size()) == 0 && p.m_hasSync);
      metadata(p.m_info, false, 0, 0); output(f);
    }
    auto f = truehd(p, 27, true, 2);
    check(p.SyncTrueHD(f.data(), f.size()) == 0 && p.m_hasSync);
    metadata(p.m_info, true, -4, defeat ? 0 : -4);
    // A new unsupported major sync retires the previous presentation's data.
    f = truehd(p, 27, false, 0);
    check(p.SyncTrueHD(f.data(), f.size()) == 0 && p.m_hasSync);
    metadata(p.m_info, false, 0, 0);
  }
  packet(p, ac3(27, 2), CAEStreamInfo::STREAM_TYPE_DTS_512);
  metadata(p.m_info, false, 0, 0);
  // Incomplete headers cannot report a known zero or read out of bounds.
  packet(p, Bytes(8, 0), CAEStreamInfo::STREAM_TYPE_AC3);
  metadata(p.m_info, false, 0, 0);
  // Exercise the actual ingestion/sync/packet path, including a fragmented
  // packet, a policy change, a seek reset, and complete synchronization loss.
  for (bool defeat : {false, true}) {
    CAEStreamParser ingest;
    ingest.SetSyncFamily(CAEStreamParser::SyncFamily::AC3);
    ingest.SetDefeatAC3DialNorm(defeat);
    auto f = eac3(27); auto next = eac3(25);
    f.insert(f.end(), next.begin(), next.end());
    uint8_t* data = nullptr; unsigned size = 0;
    check(ingest.AddData(f.data(), 12, &data, &size) == 12 && size == 0);
    metadata(ingest.m_info, false, 0, 0);
    check(ingest.AddData(f.data() + 12, f.size() - 12, &data, &size) == 52 && size == 32);
    metadata(ingest.m_info, true, -4, defeat ? 0 : -4);
    output(Bytes(data, data + size));
    ingest.Reset(); metadata(ingest.m_info, false, 0, 0);
    ingest.SetDefeatAC3DialNorm(!defeat);
    check(ingest.AddData(f.data(), f.size(), &data, &size) == 64 && size == 32);
    metadata(ingest.m_info, true, -4, defeat ? -4 : 0);
    output(Bytes(data, data + size));
    // Consume the retained next frame, then force sync loss with random data.
    Bytes noise(64, 0x55);
    ingest.AddData(noise.data(), noise.size(), &data, &size);
    check(size == 32); metadata(ingest.m_info, true, -6, defeat ? -6 : 0);
    output(Bytes(data, data + size));
    ingest.AddData(noise.data(), noise.size(), &data, &size);
    check(size == 0 && !ingest.IsValid());
    metadata(ingest.m_info, false, 0, 0);
    delete[] data;
  }
  std::cerr << checks << " parser/emission assertions passed\n";
}
'''

def block(text, marker):
    start = text.index(marker)
    opening = text.index('{', start)
    end, depth = opening + 1, 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--baseline')
    opts = args.parse_args()
    include = Path(os.environ.get('FFMPEG_INCLUDE', '/usr/include/x86_64-linux-gnu'))
    library = os.environ.get('AVUTIL_LIBRARY') or ctypes.util.find_library('avutil')
    if not library:
        raise SystemExit('Host libavutil is required')
    with tempfile.TemporaryDirectory(prefix='audio-dialnorm-') as name:
        temp = Path(name)
        (temp / 'libavutil').mkdir()
        for header in ('crc.h', 'attributes.h'):
            shutil.copyfile(include / 'libavutil' / header, temp / 'libavutil' / header)
        (temp / 'utils').mkdir()
        (temp / 'utils/log.h').write_text('''#pragma once
constexpr int LOGINFO=1, LOGDEBUG=0, LOGERROR=2;
struct CLog { template<typename... T> static void Log(int, const char*, const T&...) {} };
''')
        (temp / 'harness.cpp').write_text(HARNESS)
        def run(revision=None):
            src = temp / ('baseline' if revision else 'current')
            src.mkdir()
            for filename in ('AEStreamInfo.h', 'AEStreamInfo.cpp'):
                data = subprocess.check_output(['git', 'show', f'{revision}:xbmc/cores/AudioEngine/Utils/{filename}'], cwd=ROOT) if revision else (UTILS / filename).read_bytes()
                (src / filename).write_bytes(data)
            command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I'+str(src), '-I'+str(temp), '-I'+str(UTILS),
                       str(src / 'AEStreamInfo.cpp'), str(temp / 'harness.cpp'),
                       '-Wl,-l:'+library, '-o', str(src / 'test')]
            if not revision:
                command.insert(1, '-DDIALNORM_METADATA')
            subprocess.run(command, check=True)
            env = os.environ.copy(); env['ASAN_OPTIONS'] = 'detect_leaks=0'
            return subprocess.check_output([str(src / 'test')], env=env)
        current = run()
        if opts.baseline:
            assert current == run(opts.baseline), 'Encoded output changed from baseline'
            print(f'All {len(current)} emitted bytes equal baseline {opts.baseline}')
        cache = (ROOT / 'xbmc/cores/DataCacheCore.cpp').read_text()
        methods = '\n'.join(block(cache, marker) for marker in (
            'void CDataCacheCore::SetAudioDialNorm(',
            'std::string CDataCacheCore::GetAudioDialNorm(',
            'void CDataCacheCore::ResetAudioCache('))
        audio_info = block((ROOT / 'xbmc/cores/DataCacheCore.h').read_text(), 'struct SPlayerAudioInfo')
        fmt_include = Path(os.environ.get('FMT_INCLUDE', str(include)))
        shutil.copytree(fmt_include / 'fmt', temp / 'fmt')
        fixture = r'''
#include <cassert>
#include <mutex>
#include <string>
#include "utils/StringUtils.h"
using CCriticalSection = std::mutex;
struct CDataCacheCore {
  CCriticalSection m_audioPlayerSection;
  @AUDIOINFO@ m_playerAudioInfo{};
  void SetAudioDialNorm(bool available=false, int original=0, int applied=0);
  std::string GetAudioDialNorm(bool colored=true);
  void ResetAudioCache();
};
@METHODS@
int main() {
  CDataCacheCore c;
  assert(c.GetAudioDialNorm().empty() && c.GetAudioDialNorm(false).empty());
  c.SetAudioDialNorm(true,-4,0);
  assert(c.GetAudioDialNorm() == "-4 dB [COLOR FF404040]|[/COLOR] applied 0 dB");
  assert(c.GetAudioDialNorm(false) == "-4 dB | applied 0 dB");
  c.SetAudioDialNorm(true,0,0); assert(!c.GetAudioDialNorm().empty());
  c.SetAudioDialNorm(true,-6,-6);
  assert(c.GetAudioDialNorm(false) == "-6 dB | applied -6 dB");
  c.SetAudioDialNorm(); assert(c.GetAudioDialNorm().empty());
  c.SetAudioDialNorm(true,-4,0); c.ResetAudioCache();
  assert(c.GetAudioDialNorm().empty() && c.GetAudioDialNorm(false).empty());
}
'''.replace('@METHODS@', methods).replace('@AUDIOINFO@', audio_info)
        (temp / 'cache.cpp').write_text(fixture)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-DFMT_HEADER_ONLY',
                        '-I'+str(temp), '-I'+str(ROOT / 'xbmc'),
                        str(temp / 'cache.cpp'), '-o', str(temp / 'cache')], check=True)
        subprocess.run([str(temp / 'cache')], check=True)
        print('Colored/plain/unknown/cache replacement assertions passed')

if __name__ == '__main__':
    main()
