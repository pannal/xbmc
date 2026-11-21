#!/usr/bin/env python3
"""Host test for the silent frames that bridge passthrough pauses.

On the tested receiver chain, the audio the receiver has already decoded is
discarded when an AC-3, E-AC-3, DTS or DTS-HD bitstream stops, so the end of
what played before a pause is never heard. While paused, the sink therefore
keeps sending silent frames built from the last real packet.

This test compiles the real generator, packer and sink code on the host and
checks that:

* silent frames keep the size and header of the source frame, have their
  AC-3 and E-AC-3 CRCs recomputed (DTS frames are sent without CRC) and
  dither switched off, and unsupported packets are rejected;
* a DTS-HD packet becomes a silent copy of its DTS core alone, packed with
  the burst type and period of the DTS-HD stream;
* the packer only builds silence from the last AC-3, E-AC-3, DTS or DTS-HD
  packet;
* the sink only sends silence while the pause bridge is armed and the gap is
  exactly one frame long, and sends pause data otherwise;
* pause data is zero-filled once the stream has stopped, even when a real
  pause burst was cached while it was still streaming.

If FFmpeg is available (on PATH or via --ffmpeg), FFmpeg also encodes
AC-3, E-AC-3 and DTS streams in many channel layouts, sample rates and
bitrates, and the silent frames generated from them must decode to exact
zeros with CRC checking on. With --dts-hd, silence built from the packets
of a DTS-HD stream and from their DTS core is checked the same way.

Run with --negative-control to check that broken copies of the code fail.
"""

import argparse
import array
import concurrent.futures
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
AE = ROOT / "xbmc" / "cores" / "AudioEngine"
UTILS = AE / "Utils"
COPIED = ["AEBitstreamPacker.cpp", "AEBitstreamPacker.h", "AEPackIEC61937.cpp",
          "AEPackIEC61937.h", "AEBitstreamSilence.cpp", "AEBitstreamSilence.h"]
SOURCES = {name: UTILS / name for name in COPIED}
SOURCES.update({
    "ActiveAESink.cpp": AE / "Engines" / "ActiveAE" / "ActiveAESink.cpp",
    "AEStreamInfo.h": UTILS / "AEStreamInfo.h",
    "AEStreamInfo.cpp": UTILS / "AEStreamInfo.cpp",
    "EndianSwap.cpp": ROOT / "xbmc" / "utils" / "EndianSwap.cpp",
    "EndianSwap.h": ROOT / "xbmc" / "utils" / "EndianSwap.h",
})
COMPILED = ["main.cpp", "AEBitstreamPacker.cpp", "AEPackIEC61937.cpp", "EndianSwap.cpp",
            "AEBitstreamSilence.cpp"]
SINK_FUNCTIONS = ["unsigned int CActiveAESink::OutputSamples(",
                  "void CActiveAESink::SwapInit(", "void CActiveAESink::SetPaused("]

LOG_H = """#pragma once
#include <sstream>
#include <string>
#include <vector>
constexpr int LOGDEBUG = 0, LOGINFO = 1, LOGWARNING = 2, LOGERROR = 3, LOGFATAL = 4;
struct LogRecord
{
  std::string message;
  std::vector<std::string> args;
};
struct CLog
{
  inline static std::vector<LogRecord> records;
  template<class T>
  static std::string Text(const T& value)
  {
    std::ostringstream out;
    out << value;
    return out.str();
  }
  template<class M, class... T>
  static void Log(int, const M& message, const T&... args)
  {
    records.push_back(LogRecord{std::string(message), {Text(args)...}});
  }
};
"""

CHANNEL_H = """#pragma once
constexpr int AE_CH_RAW = 1;
struct CAEChannelInfo
{
  unsigned int n = 0;
  void operator+=(int) { n++; }
  unsigned int Count() const { return n; }
};
"""

STREAM_INFO_H = """#pragma once
#include <stdint.h>
#include "AEChannelInfo.h"
#include "AEPackIEC61937.h"

"""


def block(source, marker):
    """Return the braced definition that starts at marker."""
    start = source.index(marker)
    depth = 0
    for end in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[end], 0)
        if depth == 0:
            return source[start:end + 1]
    raise ValueError(f"unterminated block {marker!r}")


def read_sources():
    return {name: path.read_text(encoding="utf-8") for name, path in SOURCES.items()}


def build(files, directory, sanitize):
    work = pathlib.Path(directory)
    (work / "utils").mkdir()

    def write(name, text):
        (work / name).write_text(text, encoding="utf-8")

    for name in COPIED + ["EndianSwap.cpp", "EndianSwap.h"]:
        write(name, files[name])
    write("utils/EndianSwap.h", files["EndianSwap.h"])
    write("utils/log.h", LOG_H)
    write("AEChannelInfo.h", CHANNEL_H)
    write("AEStreamInfo.h",
          STREAM_INFO_H + block(files["AEStreamInfo.h"], "class CAEStreamInfo\n{") + ";\n")
    sink = "\n\n".join(block(files["ActiveAESink.cpp"], marker) for marker in SINK_FUNCTIONS)
    duration = block(files["AEStreamInfo.cpp"], "double CAEStreamInfo::GetDuration() const")
    write("main.cpp", DRIVER.replace("@GET_DURATION@", duration).replace("@SINK@", sink))
    command = ["g++", "-std=c++17", "-O1", "-g", "-DTARGET_POSIX", f"-I{work}"]
    if sanitize:
        command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    command += [str(work / name) for name in COMPILED] + ["-o", str(work / "run")]
    subprocess.run(command, check=True)
    return work / "run"


def run(files, sanitize, after=None):
    with tempfile.TemporaryDirectory() as directory:
        binary = build(files, directory, sanitize)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=300)
        if after and result.returncode == 0:
            after(binary)
        return result


DRIVER = r"""
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "AEBitstreamPacker.h"
#include "AEBitstreamSilence.h"
#include "AEStreamInfo.h"
#include "utils/EndianSwap.h"
#include "utils/log.h"

@GET_DURATION@

struct Format
{
  int m_dataFormat = 0;
  unsigned int m_frameSize = 4, m_frames = 1440, m_sampleRate = 192000;
  CAEStreamInfo m_streamInfo;
};
constexpr int AE_FMT_RAW = 0, AE_FMT_S16NE = 1, AE_FMT_S16BE = 2;
struct AEDelayStatus
{
  double delay = 0;
  void SetDelay(double d) { delay = d; }
};
struct CThread
{
  static void Sleep(std::chrono::milliseconds) {}
};
struct FakeSink
{
  unsigned int frameSize = 4;
  std::vector<uint8_t> out;
  unsigned int AddPackets(uint8_t** data, unsigned int frames, unsigned int offset)
  {
    const uint8_t* start = data[0] + offset * frameSize;
    out.insert(out.end(), start, start + frames * frameSize);
    return frames;
  }
  void GetDelay(AEDelayStatus& status) { status.SetDelay(0.125); }
  void AddPause(int) {}
};
struct Stats
{
  void UpdateSinkDelay(const AEDelayStatus&, int) {}
};
struct Packet
{
  uint8_t* data[1]{};
  unsigned int nb_samples = 0;
  int pause_burst_ms = 0;
};
struct CSampleBuffer
{
  Packet* pkt = nullptr;
  void* pool = nullptr;
};
struct CAEUtil
{
  static bool S16NeedsByteSwap(int, int output) { return output == AE_FMT_S16BE; }
};
struct CActiveAESink
{
  enum SwapState
  {
    SKIP_SWAP,
    NEED_BYTESWAP,
    CHECK_SWAP
  };
  Format m_requestedFormat, m_sinkFormat;
  bool m_needIecPack = true, m_extStreaming = true, m_extError = false;
  bool m_extPaused = false, m_pauseBridge = false;
  SwapState m_swapState = SKIP_SWAP;
  CAEBitstreamPacker* m_packer = nullptr;
  FakeSink* m_sink = nullptr;
  Stats* m_stats = nullptr;
  unsigned int OutputSamples(CSampleBuffer* samples);
  void SwapInit(CSampleBuffer* samples);
  void SetPaused(bool paused);
};

@SINK@

namespace
{
using Bytes = std::vector<uint8_t>;
constexpr auto kAc3 = CAEStreamInfo::STREAM_TYPE_AC3;
constexpr auto kEac3 = CAEStreamInfo::STREAM_TYPE_EAC3;
constexpr auto kDts = CAEStreamInfo::STREAM_TYPE_DTS_512;
constexpr auto kDtsHd = CAEStreamInfo::STREAM_TYPE_DTSHD_MA;
constexpr auto kTrueHd = CAEStreamInfo::STREAM_TYPE_TRUEHD;
// bit positions where the headers written by the *Frame functions end
constexpr unsigned int AC3_BSI_END = 67, EAC3_BSI_END = 54, DTS_HEADER_END = 104;
int failures = 0;

void Expect(bool ok, const std::string& what)
{
  if (!ok)
  {
    failures++;
    std::printf("FAIL: %s\n", what.c_str());
  }
}

struct BitWriter
{
  Bytes bytes;
  unsigned int pos = 0;
  void Put(unsigned int bits, uint32_t value)
  {
    while (bits-- > 0)
    {
      if (pos / 8 == bytes.size())
        bytes.push_back(0);
      if ((value >> bits) & 1)
        bytes[pos / 8] |= 0x80 >> (pos % 8);
      pos++;
    }
  }
  // pads the frame with deterministic noise that stands in for audio
  Bytes Finish(size_t size, uint32_t seed) const
  {
    Bytes data = bytes;
    while (data.size() < size)
    {
      seed = seed * 1664525u + 1013904223u;
      data.push_back(static_cast<uint8_t>(seed >> 24));
    }
    return data;
  }
};

Bytes Ac3Frame(uint32_t seed, unsigned int frmsizecod)
{
  static const unsigned int kbps[] = {32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192};
  BitWriter w;
  w.Put(16, 0x0B77);
  w.Put(16, 0); // crc1
  w.Put(2, 0); // 48 kHz
  w.Put(6, frmsizecod);
  w.Put(5, 8); // bsid
  w.Put(3, 0); // bsmod
  w.Put(3, 2); // acmod 2/0
  w.Put(2, 0); // dsurmod
  w.Put(1, 0); // lfeon
  w.Put(5, 27); // dialnorm
  w.Put(4, 0); // compre, langcode, audprodie, copyrightb
  w.Put(1, 1); // origbs
  w.Put(3, 0); // timecod1e, timecod2e, addbsie
  return w.Finish(kbps[frmsizecod / 2] * 4, seed);
}

Bytes Eac3Frame(uint32_t seed)
{
  const unsigned int size = 768;
  BitWriter w;
  w.Put(16, 0x0B77);
  w.Put(2, 0); // independent stream
  w.Put(3, 0); // substreamid
  w.Put(11, size / 2 - 1);
  w.Put(2, 0); // 48 kHz
  w.Put(2, 3); // six blocks
  w.Put(3, 2); // acmod 2/0
  w.Put(1, 0); // lfeon
  w.Put(5, 16); // bsid
  w.Put(5, 27); // dialnorm
  w.Put(4, 0); // compre, mixmdate, infomdate, addbsie
  return w.Finish(size, seed);
}

Bytes DtsFrame(uint32_t seed)
{
  const unsigned int size = 1024;
  BitWriter w;
  w.Put(32, 0x7FFE8001);
  w.Put(1, 1); // normal frame
  w.Put(5, 31); // no deficit samples
  w.Put(1, 0); // no CRC
  w.Put(7, 15); // 16 blocks of 32 samples
  w.Put(14, size - 1);
  w.Put(6, 2); // stereo
  w.Put(4, 13); // 48 kHz
  w.Put(5, 15); // 768 kbit/s
  w.Put(9, 0); // reserved, DRC, time stamp, aux, HDCD, extension
  w.Put(1, 1); // audio sync word insertion
  w.Put(2, 0); // no LFE
  w.Put(1, 0); // predictor history
  w.Put(1, 0); // filter
  w.Put(4, 7); // encoder version
  w.Put(2, 0); // copy history
  w.Put(3, 6); // 24 bit source
  w.Put(2, 0); // sum/difference flags
  w.Put(4, 0); // dialog normalisation
  return w.Finish(size, seed);
}

// a DTS core frame followed by an extension substream
Bytes DtsHdPacket(uint32_t seed)
{
  const unsigned int exssSize = 64;
  BitWriter w;
  w.Put(32, 0x64582025);
  w.Put(8, 0); // user defined bits
  w.Put(2, 0); // substream index
  w.Put(1, 0); // short size fields
  w.Put(8, 15); // header size
  w.Put(16, exssSize - 1);
  Bytes packet = DtsFrame(seed);
  const Bytes exss = w.Finish(exssSize, seed + 1);
  packet.insert(packet.end(), exss.begin(), exss.end());
  return packet;
}

CAEStreamInfo Info(CAEStreamInfo::DataType type, bool littleEndian = false)
{
  CAEStreamInfo info;
  info.m_type = type;
  info.m_sampleRate = 48000;
  info.m_repeat = 1;
  info.m_dataIsLE = littleEndian;
  return info;
}

// DTS-HD with 512 samples per frame, packed into bursts of 2048 frames
CAEStreamInfo DtsHdInfo(bool littleEndian = false)
{
  CAEStreamInfo info = Info(kDtsHd, littleEndian);
  info.m_dtsPeriod = 2048;
  return info;
}

Bytes Swap16(Bytes data)
{
  for (size_t i = 0; i + 1 < data.size(); i += 2)
    std::swap(data[i], data[i + 1]);
  return data;
}

unsigned int Bit(const Bytes& data, unsigned int pos)
{
  return (data[pos / 8] >> (7 - pos % 8)) & 1;
}

bool SameBits(const Bytes& a, const Bytes& b, unsigned int from, unsigned int to)
{
  for (unsigned int pos = from; pos < to; pos++)
    if (Bit(a, pos) != Bit(b, pos))
      return false;
  return true;
}

uint16_t Crc16(const uint8_t* data, size_t size)
{
  uint16_t crc = 0;
  for (size_t i = 0; i < size; i++)
  {
    crc ^= data[i] << 8;
    for (int bit = 0; bit < 8; bit++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x8005 : crc << 1;
  }
  return crc;
}

// returns the silent packet, or an empty one if the packet was rejected
Bytes Generate(const CAEStreamInfo& info, const Bytes& data)
{
  Bytes out(3, 0xAA);
  if (CAEBitstreamSilence::Generate(info, data.data(), data.size(), out))
    return out;
  Expect(out.empty(), "a rejected packet leaves no output");
  return {};
}

Bytes Burst(CAEBitstreamPacker& packer)
{
  return Bytes(packer.GetBuffer(), packer.GetBuffer() + packer.GetSize());
}

Bytes Packed(CAEStreamInfo info, Bytes data)
{
  CAEBitstreamPacker packer;
  packer.Pack(info, data.data(), data.size());
  return Burst(packer);
}

Bytes SilenceBurst(const CAEStreamInfo& info, const Bytes& data)
{
  const Bytes silence = Generate(info, data);
  Expect(!silence.empty(), "the silent frame is generated");
  return Packed(info, silence);
}

Bytes PauseBurst(CAEStreamInfo info, Bytes data, int ms)
{
  CAEBitstreamPacker packer;
  packer.Pack(info, data.data(), data.size());
  packer.PackPause(info, ms, packer.GetBuffer()[0] != 0);
  return Burst(packer);
}

Bytes Zeros(Bytes data)
{
  std::fill(data.begin(), data.end(), 0);
  return data;
}

void CheckDolby(const std::string& name, const Bytes& src, const Bytes& out,
                unsigned int bsiEnd, bool ac3)
{
  if (out.size() != src.size())
  {
    Expect(false, name + ": the silent frame has the size of the packet");
    return;
  }
  Expect(out != src, name + ": the silent frame replaces the audio");
  Expect(SameBits(src, out, 0, 16) && SameBits(src, out, ac3 ? 32 : 16, bsiEnd),
         name + ": the header is kept");
  Expect(Crc16(out.data() + 2, out.size() - 2) == 0, name + ": the frame CRC is valid");
  if (ac3)
  {
    const size_t words = out.size() / 2;
    Expect(Crc16(out.data() + 2, ((words >> 1) + (words >> 3)) * 2 - 2) == 0,
           name + ": crc1 is valid");
    // the first block starts with blksw and then dithflag for both channels
    Expect(!Bit(out, bsiEnd + 2) && !Bit(out, bsiEnd + 3), name + ": dither is off");
  }
  else
  {
    // audfrm has blkswe and then dithflage, without which dither is on
    Expect(!Bit(out, bsiEnd + 5) && Bit(out, bsiEnd + 6), name + ": dither flags are sent");
  }
}

void TestGenerator()
{
  const Bytes ac3 = Ac3Frame(1, 20), eac3 = Eac3Frame(2), dts = DtsFrame(3);
  CheckDolby("AC-3", ac3, Generate(Info(kAc3), ac3), AC3_BSI_END, true);
  CheckDolby("E-AC-3", eac3, Generate(Info(kEac3), eac3), EAC3_BSI_END, false);
  const Bytes silentDts = Generate(Info(kDts), dts);
  Expect(silentDts.size() == dts.size() && silentDts != dts &&
             SameBits(dts, silentDts, 0, DTS_HEADER_END),
         "DTS: the silent frame keeps the size and header");
  Expect(Generate(Info(kDts, true), Swap16(dts)) == Swap16(silentDts),
         "DTS: little-endian packets give the same frame");
  const Bytes dtsHd = DtsHdPacket(3);
  Expect(Generate(DtsHdInfo(), dtsHd) == silentDts,
         "DTS-HD: the silent packet is the silent core alone");
  Expect(Generate(Info(kAc3), ac3) == Generate(Info(kAc3), ac3),
         "AC-3: the silent frame is deterministic");

  Bytes truncated(ac3.begin(), ac3.end() - 2);
  Bytes noSync = ac3;
  noSync[1] = 0x78;
  Bytes oddLittleEndian = Swap16(dts);
  oddLittleEndian.push_back(0);
  CAEStreamInfo collected = Info(kEac3);
  collected.m_repeat = 6;
  Bytes otherData = dtsHd;
  otherData[dts.size()] = 0x7F; // no extension substream after the core
  const Bytes coreless(dtsHd.begin() + dts.size(), dtsHd.end());
  Bytes core14 = dtsHd;
  const uint8_t sync14[] = {0x1F, 0xFF, 0xE8, 0x00};
  std::copy(sync14, sync14 + 4, core14.begin());
  Bytes extended = dtsHd;
  extended[10] |= 0x10; // EXT_AUDIO
  struct Rejected
  {
    const char* name;
    CAEStreamInfo info;
    Bytes data;
  };
  const Rejected rejected[] = {
      {"TrueHD", Info(kTrueHd), ac3},
      {"DTS-HD without an extension substream", DtsHdInfo(), dts},
      {"DTS-HD with other data after the core", DtsHdInfo(), otherData},
      {"DTS-HD without a core", DtsHdInfo(), coreless},
      {"DTS-HD with a 14-bit core", DtsHdInfo(), core14},
      {"DTS-HD with core extensions", DtsHdInfo(), extended},
      {"little-endian DTS-HD", DtsHdInfo(true), Swap16(dtsHd)},
      {"a truncated AC-3 frame", Info(kAc3), truncated},
      {"an AC-3 frame without sync word", Info(kAc3), noSync},
      {"E-AC-3 packets collected into one burst", collected, eac3},
      {"AC-3 sent as DTS", Info(kDts), ac3},
      {"little-endian DTS of odd size", Info(kDts, true), oddLittleEndian},
  };
  for (const Rejected& r : rejected)
  {
    Bytes out(5, 0x55);
    const bool ok = CAEBitstreamSilence::Generate(r.info, r.data.data(), r.data.size(), out);
    Expect(!ok && out.empty(), std::string("rejects ") + r.name);
  }
}

struct Case
{
  const char* name;
  CAEStreamInfo info;
  Bytes data;
};

std::vector<Case> Cases()
{
  const Bytes dts = DtsFrame(3);
  return {{"AC-3", Info(kAc3), Ac3Frame(1, 20)},
          {"E-AC-3", Info(kEac3), Eac3Frame(2)},
          {"DTS", Info(kDts), dts},
          {"little-endian DTS", Info(kDts, true), Swap16(dts)},
          {"DTS-HD", DtsHdInfo(), DtsHdPacket(3)}};
}

void TestPacker()
{
  for (Case c : Cases())
  {
    const std::string name = c.name;
    CAEBitstreamPacker packer;
    Expect(!packer.PackSilence(c.info), name + ": no silence before the first packet");
    packer.Pack(c.info, c.data.data(), c.data.size());
    const Bytes silence = SilenceBurst(c.info, c.data);
    Expect(packer.PackSilence(c.info) && Burst(packer) == silence,
           name + ": silence is the packed silent frame");
    Expect(packer.PackSilence(c.info) && Burst(packer) == silence,
           name + ": repeated silence is unchanged");
  }
  Expect(SilenceBurst(Info(kDts), DtsFrame(3)) ==
             SilenceBurst(Info(kDts, true), Swap16(DtsFrame(3))),
         "DTS: the silent burst does not depend on byte order");
  const Bytes dtsHdBurst = Packed(DtsHdInfo(), DtsHdPacket(3));
  const Bytes dtsHdSilence = SilenceBurst(DtsHdInfo(), DtsHdPacket(3));
  Expect(dtsHdBurst.size() == 4 * 2048 && dtsHdSilence.size() == dtsHdBurst.size() &&
             std::equal(dtsHdBurst.begin(), dtsHdBurst.begin() + 6, dtsHdSilence.begin()),
         "DTS-HD: the silent burst keeps the burst type and period");

  CAEStreamInfo ac3Info = Info(kAc3);
  Bytes first = Ac3Frame(1, 20), second = Ac3Frame(4, 16);
  CAEBitstreamPacker packer;
  packer.Pack(ac3Info, first.data(), first.size());
  Expect(packer.PackSilence(ac3Info) && Burst(packer) == SilenceBurst(ac3Info, first),
         "silence follows the first packet");
  packer.Pack(ac3Info, second.data(), second.size());
  Expect(packer.PackSilence(ac3Info) && Burst(packer) == SilenceBurst(ac3Info, second),
         "silence follows the next packet");

  CAEStreamInfo trueHdInfo = Info(kTrueHd);
  Bytes mat(65536, 0);
  packer.Pack(trueHdInfo, mat.data(), 61440);
  Expect(!packer.PackSilence(ac3Info), "a TrueHD packet clears the silence source");

  Bytes unsupported = first;
  unsupported[5] = (unsupported[5] & 7) | (12 << 3); // bsid 12
  packer.Pack(ac3Info, unsupported.data(), unsupported.size());
  CLog::records.clear();
  const bool silent = packer.PackSilence(ac3Info) || packer.PackSilence(ac3Info);
  size_t logged = 0;
  for (const LogRecord& record : CLog::records)
    logged += record.message.find("CAEBitstreamPacker::PackSilence") != std::string::npos;
  Expect(!silent && logged == 1, "a packet without silent version is logged once and dropped");
}

struct Rig
{
  CAEBitstreamPacker packer;
  FakeSink sink;
  Stats stats;
  CActiveAESink s;
  explicit Rig(const CAEStreamInfo& info, int dataFormat = AE_FMT_S16NE)
  {
    s.m_requestedFormat.m_dataFormat = AE_FMT_RAW;
    s.m_sinkFormat.m_dataFormat = dataFormat;
    s.m_sinkFormat.m_streamInfo = info;
    s.m_sinkFormat.m_frameSize = sink.frameSize = info.m_type == kTrueHd ? 16 : 4;
    s.m_packer = &packer;
    s.m_sink = &sink;
    s.m_stats = &stats;
  }
  Bytes Output(Packet& pkt, bool pooled = true)
  {
    CSampleBuffer buffer;
    buffer.pkt = &pkt;
    buffer.pool = pooled ? this : nullptr;
    sink.out.clear();
    s.OutputSamples(&buffer);
    return sink.out;
  }
  Bytes Send(Bytes data, unsigned int size = 0)
  {
    Packet pkt;
    pkt.data[0] = data.data();
    pkt.nb_samples = size ? size : data.size();
    return Output(pkt);
  }
  // pooled = false models the sink's own silence sample
  Bytes Gap(int ms, bool pooled = true)
  {
    Packet pkt;
    pkt.pause_burst_ms = ms;
    return Output(pkt, pooled);
  }
};

void TestSink()
{
  for (Case c : Cases())
  {
    const std::string name = c.name;
    const int frame = static_cast<int>(c.info.GetDuration());
    const Bytes silence = SilenceBurst(c.info, c.data);
    const Bytes frameGap = PauseBurst(c.info, c.data, frame);
    Rig rig(c.info);
    Expect(rig.Send(c.data) == Packed(c.info, c.data), name + ": packets are packed");
    Expect(rig.Gap(frame) == frameGap, name + ": no silence before pausing");
    rig.s.SetPaused(true);
    for (int i = 0; i < 3; i++)
      Expect(rig.Gap(frame) == silence, name + ": paused gaps of one frame are silent frames");
    Expect(rig.Gap(5) == PauseBurst(c.info, c.data, 5), name + ": shorter gaps get pause bursts");
    Expect(rig.Gap(frame + 32) == PauseBurst(c.info, c.data, frame + 32),
           name + ": longer gaps get pause bursts");
    rig.Send(c.data);
    Expect(rig.Gap(frame) == silence, name + ": packets while paused keep the bridge");
    rig.s.SetPaused(false);
    Expect(rig.Gap(frame) == frameGap, name + ": no silence after resuming");
    rig.s.m_pauseBridge = true;
    Expect(rig.Gap(frame) == frameGap, name + ": resuming drops the silence source");
    rig.Send(c.data);
    rig.s.m_pauseBridge = true;
    Expect(rig.Gap(frame) == silence, name + ": an armed bridge sends silence");
    rig.Send(c.data);
    Expect(rig.Gap(frame) == frameGap, name + ": packets after resuming disarm the bridge");
  }

  CAEStreamInfo trueHd = Info(kTrueHd);
  Bytes mat(65536, 0);
  CAEBitstreamPacker reference;
  reference.Pack(trueHd, mat.data(), 61440);
  reference.PackPause(trueHd, 20, reference.GetBuffer()[0] != 0);
  Rig trueHdRig(trueHd);
  trueHdRig.Send(mat, 61440);
  trueHdRig.s.SetPaused(true);
  Expect(trueHdRig.Gap(20) == Burst(reference),
         "TrueHD: paused gaps get pause bursts while streaming");

  CAEStreamInfo ac3Info = Info(kAc3);
  const Bytes ac3 = Ac3Frame(1, 20);
  Rig swapped(ac3Info, AE_FMT_S16BE);
  swapped.s.m_swapState = CActiveAESink::CHECK_SWAP;
  Expect(swapped.Send(ac3) == Swap16(Packed(ac3Info, ac3)), "swapping sink: packets are swapped");
  Expect(swapped.s.m_swapState == CActiveAESink::NEED_BYTESWAP,
         "swapping sink: the swap is detected");
  swapped.s.SetPaused(true);
  const Bytes silence = Swap16(SilenceBurst(ac3Info, ac3));
  for (int i = 0; i < 3; i++)
    Expect(swapped.Gap(32) == silence, "swapping sink: silent frames are swapped once");
  const Bytes pause = Swap16(PauseBurst(ac3Info, ac3, 5));
  Expect(swapped.Gap(5) == pause && swapped.Gap(5) == pause,
         "swapping sink: repeated pause bursts are swapped once");
}

// Once paused, the sink is told the stream stopped (STREAMING false), so
// gaps that get no silent frame must be zero-filled, not IEC pause bursts.
void TestStoppedStream()
{
  for (Case c : Cases())
  {
    const std::string name = std::string(c.name) + ", stream stopped";
    const int frame = static_cast<int>(c.info.GetDuration());
    const Bytes silence = SilenceBurst(c.info, c.data);
    const Bytes burst = PauseBurst(c.info, c.data, 5);
    Rig rig(c.info);
    rig.Send(c.data);
    rig.s.SetPaused(true);
    Expect(rig.Gap(5) == burst, name + ": a pause burst is cached while streaming");
    rig.s.m_extStreaming = false;
    Expect(burst != Zeros(burst) && rig.Gap(5) == Zeros(burst),
           name + ": a cached pause burst is not reused");
    Expect(rig.Gap(5) == Zeros(burst), name + ": repeated pause data stays zero-filled");
    for (int i = 0; i < 3; i++)
      Expect(rig.Gap(frame) == silence, name + ": paused gaps of one frame are silent frames");
    Expect(rig.Gap(frame + 32) == Zeros(PauseBurst(c.info, c.data, frame + 32)),
           name + ": longer gaps are zero-filled");
    Expect(rig.Gap(frame, false) == silence,
           name + ": the sink's own silence sample gets a silent frame");

    CAEBitstreamPacker reopened;
    rig.s.m_packer = &reopened;
    rig.s.m_extStreaming = true;
    Expect(rig.Gap(frame) == Zeros(PauseBurst(c.info, c.data, frame)),
           name + ": a reopened sink without a packet sends zero-filled pause data");
    rig.Send(c.data);
    Expect(rig.Gap(frame) == silence, name + ": a packet after reopening restores silence");
  }

  CAEStreamInfo trueHd = Info(kTrueHd);
  Bytes mat(65536, 0);
  CAEBitstreamPacker reference;
  reference.Pack(trueHd, mat.data(), 61440);
  reference.PackPause(trueHd, 20, true);
  const Bytes burst = Burst(reference);
  Rig rig(trueHd);
  rig.Send(mat, 61440);
  rig.s.SetPaused(true);
  Expect(rig.Gap(20) == burst, "TrueHD: a pause burst is cached while streaming");
  rig.s.m_extStreaming = false;
  Expect(burst != Zeros(burst) && rig.Gap(20) == Zeros(burst) && rig.Gap(20) == Zeros(burst),
         "TrueHD: paused gaps are zero-filled once the stream stopped");
}

// --generate TYPE: reads [u32 size][frame] records from stdin and writes the
// silent frames as records to stdout, with size 0 for a rejected frame
int GenerateStream(const std::string& type)
{
  const CAEStreamInfo info = Info(type == "eac3"    ? kEac3
                                  : type == "dts"   ? kDts
                                  : type == "dtshd" ? kDtsHd
                                                    : kAc3);
  uint32_t size = 0;
  while (std::fread(&size, sizeof(size), 1, stdin) == 1)
  {
    Bytes frame(size), out;
    if (std::fread(frame.data(), 1, size, stdin) != size)
      return 1;
    if (!CAEBitstreamSilence::Generate(info, frame.data(), size, out))
      out.clear();
    const uint32_t outSize = static_cast<uint32_t>(out.size());
    std::fwrite(&outSize, sizeof(outSize), 1, stdout);
    std::fwrite(out.data(), 1, out.size(), stdout);
  }
  return 0;
}
} // namespace

int main(int argc, char** argv)
{
  if (argc == 3 && std::string(argv[1]) == "--generate")
    return GenerateStream(argv[2]);
  TestGenerator();
  TestPacker();
  TestSink();
  TestStoppedStream();
  if (failures)
  {
    std::printf("%d checks failed\n", failures);
    return 1;
  }
  std::printf("all pause silence checks passed\n");
  return 0;
}
"""

# name, file, code to break, replacement, copies of the code
MUTANTS = [
    ("silence-for-longer-gaps", "ActiveAESink.cpp",
     "samples->pkt->pause_burst_ms ==", "samples->pkt->pause_burst_ms >=", 1),
    ("bridge-never-disarms", "ActiveAESink.cpp",
     "if (!m_extPaused)\n          m_pauseBridge = false;", "", 1),
    ("packets-always-disarm", "ActiveAESink.cpp",
     "if (!m_extPaused)\n          m_pauseBridge = false;", "m_pauseBridge = false;", 1),
    ("resume-keeps-silence-source", "ActiveAESink.cpp",
     "  if (!paused && m_packer)\n    m_packer->ResetSilence();\n", "", 1),
    ("truehd-keeps-silence-source", "AEBitstreamPacker.cpp",
     "  else\n    ResetSilence();\n", "", 1),
    ("stale-silent-frame", "AEBitstreamPacker.cpp", "    m_silence.clear();\n", "", 1),
    ("pause-cache-ignores-bursts", "AEBitstreamPacker.cpp",
     " && m_pauseIecBursts == iecBursts", "", 1),
    ("failed-packet-retried", "AEBitstreamPacker.cpp",
     "      ResetSilence();\n      return false;", "      return false;", 1),
    ("dither-on", "AEBitstreamSilence.cpp", "bits.Zeros(fbw); // dithflag",
     "bits.Write(1, 1), bits.Zeros(fbw - 1); // dithflag", 2),
    ("collected-eac3-silenced", "AEBitstreamSilence.cpp", "info.m_repeat != 1",
     "info.m_repeat == 0", 1),
    ("wrong-crc1", "AEBitstreamSilence.cpp", "PowPoly(CRC16_INVERSE_X, 8 * size58 - 16)",
     "PowPoly(CRC16_INVERSE_X, 8 * size58 - 8)", 1),
    ("dtshd-extension-unchecked", "AEBitstreamSilence.cpp",
     "  if (exss[0] != 0x64 || exss[1] != 0x58 || exss[2] != 0x20 || exss[3] != 0x25)\n"
     "    return false;\n", "", 1),
    ("dtshd-whole-packet", "AEBitstreamSilence.cpp", "out.assign(coreSize, 0);",
     "out.assign(size, 0);", 1),
    ("dtshd-not-silenced", "AEBitstreamPacker.cpp",
     "STREAM_TYPE_DTSHD_CORE:\n    case CAEStreamInfo::STREAM_TYPE_DTSHD:\n"
     "    case CAEStreamInfo::STREAM_TYPE_DTSHD_MA:\n", "STREAM_TYPE_DTSHD_CORE:\n", 1),
]


AC3_KBPS = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512,
            576, 640]
CHANNEL_ORDER = ["FL", "FR", "FC", "LFE", "BL", "BR", "BC"]
ACMODS = {1: ["FC"], 2: ["FL", "FR"], 3: ["FL", "FR", "FC"], 4: ["FL", "FR", "BC"],
          5: ["FL", "FR", "FC", "BC"], 6: ["FL", "FR", "BL", "BR"],
          7: ["FL", "FR", "FC", "BL", "BR"]}
DOLBY_CHANNELS = [2, 1, 2, 3, 3, 4, 4, 5]
DTS_CHANNELS = [1, 2, 2, 2, 2, 3, 3, 4, 4, 5]


def bits(frame, offset, count):
    value = int.from_bytes(frame[:(offset + count + 7) // 8], "big")
    return (value >> (-(offset + count) % 8)) & ((1 << count) - 1)


def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ (0x8005 if crc & 0x8000 else 0)
        crc &= 0xFFFF
    return crc


def header(kind, frame):
    """Returns the size, samples per channel, channels and mode of a frame."""
    if kind == "dtshd":
        core = header("dts", frame)
        exss = frame[core["size"]:core["size"] + 12]
        if exss[:4] != b"\x64\x58\x20\x25":
            raise ValueError("no DTS-HD extension substream")
        wide = bits(exss, 42, 1)
        return dict(core, core=core["size"],
                    size=core["size"] + bits(exss, 51 + 4 * wide, 20 if wide else 16) + 1)
    if kind == "dts":
        if frame[:4] != b"\x7f\xfe\x80\x01":
            raise ValueError("no DTS sync word")
        amode, lff = bits(frame, 60, 6), bits(frame, 85, 2)
        return {"size": bits(frame, 46, 14) + 1, "samples": (bits(frame, 39, 7) + 1) * 32,
                "channels": DTS_CHANNELS[amode] + (lff != 0) if amode < 10 else 0,
                "ext_audio": bits(frame, 83, 1)}
    if frame[:2] != b"\x0b\x77":
        raise ValueError("no AC-3 sync word")
    bsid = frame[5] >> 3
    if bsid > 10:
        acmod, lfe = bits(frame, 36, 3), bits(frame, 39, 1)
        blocks = 6 if bits(frame, 32, 2) == 3 else [1, 2, 3, 6][bits(frame, 34, 2)]
        size = (bits(frame, 21, 11) + 1) * 2
    else:
        acmod, blocks = bits(frame, 48, 3), 6
        skip = bool(acmod & 1 and acmod != 1) + bool(acmod & 4) + (acmod == 2)
        lfe = bits(frame, 51 + 2 * skip, 1)
        fscod, code = bits(frame, 32, 2), bits(frame, 34, 6)
        kbps = AC3_KBPS[code >> 1]
        size = 2 * [2 * kbps, kbps * 96000 // 44100 + (code & 1), 3 * kbps][fscod]
    return {"size": size, "samples": 256 * blocks, "bsid": bsid, "acmod": acmod, "lfe": lfe,
            "channels": DOLBY_CHANNELS[acmod] + lfe}


def split(kind, data):
    frames, pos = [], 0
    while pos < len(data):
        size = header(kind, data[pos:pos + 16400])["size"]
        if pos + size > len(data):
            raise ValueError("truncated frame")
        frames.append(data[pos:pos + size])
        pos += size
    return frames


def run_ffmpeg(ffmpeg, args, data=b""):
    return subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *args],
                          input=data, capture_output=True, timeout=300)


def encode(ffmpeg, case, kbps):
    exprs = "|".join(f"0.2*sin(2*PI*{200 + 150 * i}*t)+0.1*(2*random(0)-1)"
                     for i in range(len(case["layout"].split("+"))))
    source = f"aevalsrc=exprs={exprs}:c={case['layout']}:s={case['rate']}:d=0.5"
    codec = {"ac3": "ac3", "eac3": "eac3", "dts": "dca"}[case["kind"]]
    result = run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", source, "-c:a", codec, "-strict", "-2",
                                 "-b:a", f"{kbps}k", *case.get("extra", []), "-f",
                                 case["kind"], "pipe:1"])
    return result.stdout if result.returncode == 0 else b""


def generate(binary, kind, frames):
    data = b"".join(struct.pack("<I", len(frame)) + frame for frame in frames)
    out = subprocess.run([str(binary), "--generate", kind], input=data, capture_output=True,
                         check=True, timeout=300).stdout
    silent, pos = [], 0
    while pos < len(out):
        (size,) = struct.unpack_from("<I", out, pos)
        silent.append(out[pos + 4:pos + 4 + size])
        pos += 4 + size
    return silent


def decode(ffmpeg, kind, data):
    result = run_ffmpeg(ffmpeg, ["-xerror", "-err_detect", "crccheck+bitstream+explode", "-f",
                                 "dts" if kind == "dtshd" else kind, "-i", "pipe:0", "-f",
                                 "f32le", "-c:a", "pcm_f32le", "pipe:1"], data)
    error = result.stderr.decode(errors="replace").strip()
    if result.returncode or error:
        return None, error or f"exit code {result.returncode}"
    return array.array("f", result.stdout), ""


def check_case(binary, ffmpeg, case):
    """Generates silence for one stream, checks it and decodes it with FFmpeg."""
    kind, name, data = case["kind"], case["name"], b""
    for kbps in case.get("kbps", []):
        data = encode(ffmpeg, case, kbps)
        if data:
            name += f" {kbps} kbps"
            break
    if "file" in case:
        core = [] if kind == "dtshd" else ["-bsf:a", "dca_core"]
        data = run_ffmpeg(ffmpeg, ["-i", case["file"], "-map", "0:a:0", "-t", "2", "-c:a",
                                   "copy", *core, "-f", "dts", "pipe:1"]).stdout
    if not data:
        return [f"{name}: FFmpeg could not produce the stream"], 0, ""
    frames = split(kind, data)
    headers = [header(kind, frame) for frame in frames]
    failures = [f"{name}: {key} is not {value} in every frame"
                for key, value in case.get("expect", {}).items()
                if any(h[key] != value for h in headers)]
    if len({h["size"] for h in headers}) != case.get("sizes", len({h["size"] for h in headers})):
        failures.append(f"{name}: expected {case['sizes']} different frame sizes")
    silent = generate(binary, kind, frames)
    if [bool(s) for s in silent] != [not h.get("ext_audio") for h in headers]:
        failures.append(f"{name}: wrong frames rejected")
    if any(s and len(s) != h.get("core", h["size"]) for s, h in zip(silent, headers)):
        failures.append(f"{name}: a silent packet has the wrong size")
    kept = [(s, h) for s, h in zip(silent, headers) if s]
    if kind == "ac3":
        for s, h in kept:
            words = h["size"] // 2
            if crc16(s[2:2 * ((words >> 1) + (words >> 3))]):
                failures.append(f"{name}: wrong CRC1 in a {h['size']} byte frame")
    if not kept:
        return failures, 0, f"{name}: all {len(frames)} frames rejected"
    samples, error = decode(ffmpeg, kind, b"".join(s for s, _ in kept))
    expected = sum(h["samples"] * h["channels"] for _, h in kept)
    if samples is None:
        failures.append(f"{name}: FFmpeg rejected the silent frames: {error}")
    elif len(samples) != expected or any(samples):
        failures.append(f"{name}: decoded {len(samples)} of {expected} samples, "
                        f"{sum(1 for sample in samples if sample)} not zero")
    if case.get("controls"):
        samples, _ = decode(ffmpeg, kind, data)
        if not samples or not any(samples):
            failures.append(f"{name}: the encoded stream did not decode to sound")
        if kind != "dts":
            damaged = bytearray(kept[0][0])
            damaged[len(damaged) // 2] ^= 0x10
            if decode(ffmpeg, kind, bytes(damaged))[0] is not None:
                failures.append(f"{name}: FFmpeg accepted a damaged silent frame")
    return failures, len(kept), ""


def decode_cases(dts_hd):
    cases, low = [], AC3_KBPS[:11]
    for acmod, names in ACMODS.items():
        for lfe in (0, 1):
            if acmod == 6 and lfe:
                continue  # FFmpeg's encoder has no 2/2 + LFE layout
            layout = "+".join(sorted(names + ["LFE"] * lfe, key=CHANNEL_ORDER.index))
            for rate in (48000, 44100, 32000):
                for kbps in (low, AC3_KBPS[-1:]):
                    cases.append({"name": f"AC-3 {layout} {rate} Hz", "kind": "ac3",
                                  "layout": layout, "rate": rate, "kbps": kbps,
                                  "expect": {"acmod": acmod, "lfe": lfe, "bsid": 8},
                                  "sizes": 2 if rate == 44100 else 1})
    extra = ["-dsurex_mode", "on", "-dheadphone_mode", "on", "-ad_conv_type", "hdcd"]
    for layout, more in (("FL+FR", []), ("FL+FR+FC+LFE+BL+BR", ["-dmix_mode", "loro"])):
        cases.append({"name": f"AC-3 {layout} alternate syntax", "kind": "ac3",
                      "layout": layout, "rate": 48000, "kbps": [448], "extra": extra + more,
                      "expect": {"bsid": 6}})
    for layout, acmod in (("FL+FR", 2), ("FL+FR+FC+LFE+BL+BR", 7)):
        for kbps in ([32, 48, 64, 96, 128, 192], [1024]):
            cases.append({"name": f"E-AC-3 {layout}", "kind": "eac3", "layout": layout,
                          "rate": 48000, "kbps": kbps,
                          "expect": {"bsid": 16, "acmod": acmod, "samples": 1536}})
    surround = "FL+FR+FC+LFE+SL+SR"
    for layout, rate, kbps in (("FL+FR", 48000, [320, 384, 448, 512]), (surround, 48000, [768]),
                               (surround, 48000, [1509]), (surround, 44100, [1411])):
        cases.append({"name": f"DTS {layout} {rate} Hz", "kind": "dts", "layout": layout,
                      "rate": rate, "kbps": kbps, "expect": {"ext_audio": 0}})
    if dts_hd:
        cases.append({"name": "DTS-HD core", "kind": "dts", "file": dts_hd})
        cases.append({"name": "DTS-HD packets", "kind": "dtshd", "file": dts_hd})
    for kind in ("ac3", "eac3", "dts"):
        next(case for case in cases if case["kind"] == kind)["controls"] = True
    return cases


def decode_check(binary, args):
    """Returns the failures and a summary of the FFmpeg decoder check."""
    if not args.ffmpeg:
        return [], ["decoder check skipped: FFmpeg not found, pass --ffmpeg"]
    cases = decode_cases(args.dts_hd)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda case: check_case(binary, args.ffmpeg, case), cases))
    failures = [failure for result in results for failure in result[0]]
    notes = [result[2] for result in results if result[2]]
    frames = sum(result[1] for result in results)
    return failures, notes + [f"decoder check: {frames} silent frames from {len(cases)} "
                              "streams decoded to exact zeros"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--negative-control", action="store_true",
                        help="also check that deliberately broken copies of the code fail")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"),
                        help="FFmpeg that decodes the silent frames (default: from PATH)")
    parser.add_argument("--dts-hd", metavar="FILE",
                        help="also check the DTS-HD packets of the first audio stream in "
                             "FILE and their DTS core, given as a path FFmpeg can open")
    args = parser.parse_args()
    files = read_sources()
    decoded = []
    result = run(files, sanitize=True,
                 after=lambda binary: decoded.extend(decode_check(binary, args)))
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="", file=sys.stderr)
        return 1
    failures, notes = decoded
    print("\n".join(failures + notes))
    if failures:
        return 1
    if not args.negative_control:
        return 0
    for name, filename, code, replacement, copies in MUTANTS:
        found = files[filename].count(code)
        if found != copies:
            print(f"{name}: expected {copies} copies of the code to break, found {found}")
            return 1
        mutated = dict(files)
        mutated[filename] = files[filename].replace(code, replacement)
        if run(mutated, sanitize=False).returncode == 0:
            print(f"{name}: not rejected")
            return 1
        print(f"{name}: rejected")
    print(f"all {len(MUTANTS)} negative controls rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
