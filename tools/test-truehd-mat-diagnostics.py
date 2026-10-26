#!/usr/bin/env python3
"""Exercise the production MAT packer and codec GetData timing on synthetic AUs.

Compare --baseline-ref to prove diagnostics preserve complete bytes and timing.
Synthetic major-sync headers exercise packing, not lossless audio decoding or
the real parser, demux timestamp association, ALSA delivery or receiver lock.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKER = "xbmc/cores/AudioEngine/Utils/PackerMAT"
CODEC = "xbmc/cores/VideoPlayer/DVDCodecs/Audio/DVDAudioCodecPassthrough.cpp"


def block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def source(path, ref):
    if ref:
        return subprocess.check_output(
            ["git", "show", f"{ref}:{path}"], cwd=ROOT, text=True)
    return (ROOT / path).read_text()


HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <cmath>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include "utils/log.h"
#include "PackerMAT.h"
#include "cores/VideoPlayer/DVDCodecs/Audio/FloatingAverage.h"
#include "cores/VideoPlayer/Interface/TimingConstants.h"
constexpr double LOCAL_NOPTS = -1.0;
bool IsValidPts(double pts) { return pts >= 0 && pts <= 86400000000.0; }
struct CAEStreamInfo {
  enum DataType {STREAM_TYPE_AC3, STREAM_TYPE_EAC3, STREAM_TYPE_TRUEHD,
    STREAM_TYPE_DTSHD_MA, STREAM_TYPE_DTS_512, STREAM_TYPE_DTSHD_CORE,
    STREAM_TYPE_DTSHD, STREAM_TYPE_DTS_1024, STREAM_TYPE_DTS_2048};
  DataType m_type = STREAM_TYPE_TRUEHD;
  int m_sampleRate = 48000, m_dtsSamplesPerFrame = 0;
  double GetDuration() const;
};
@DURATION@
struct Format { CAEStreamInfo m_streamInfo; int m_sampleRate = 48000; };
struct DVDAudioFrame {
  uint8_t* data[1]{};
  int nb_frames = 0, framesOut = 0, planes = 0, bits_per_sample = 0;
  bool passthrough = false, hasDiscontinuity = false;
  double discontinuityCorrection = 0, duration = 0, pts = 0;
  Format format;
};
struct CDVDAudioCodecPassthrough {
  std::unique_ptr<CPackerMAT> m_packerMAT = std::make_unique<CPackerMAT>();
  std::vector<uint8_t> pending;
  Format m_format;
  bool m_lavStyleSyncEnabled = true, m_needsResync = false;
  double m_currentPts = 0, m_internalClock = 10000000, m_lastOutputPts = LOCAL_NOPTS;
  double m_jitterThreshold = 100000;
  AudioSync::CFloatingAverage<double, 256> m_jitterTracker;
  struct { void SetAudioPts(double) {} } m_dataCacheCore;
  int GetData(uint8_t** dst) { *dst = pending.data(); return pending.size(); }
  void GetData(DVDAudioFrame& frame);
};
@GET_DATA@

std::vector<uint8_t> au(uint16_t time, uint16_t out, int rate, int size) {
  std::vector<uint8_t> a(size, 0x11);
  a[0] = (size / 2) >> 8; a[1] = size / 2;
  a[2] = time >> 8; a[3] = time;
  std::fill(a.begin() + 4, a.begin() + 40, 0);
  a[4] = 0xf8; a[5] = 0x72; a[6] = 0x6f; a[7] = 0xba;
  a[8] = rate << 4; a[20] = 0x10;
  a[34] = 0xc0; a[36] = out >> 8; a[37] = out;
  return a;
}
template<class T> void write(std::ofstream& file, const T& value) {
  file.write(reinterpret_cast<const char*>(&value), sizeof(value));
}
size_t countLogs(const std::string& prefix) {
  return std::count_if(CLog::records.begin(), CLog::records.end(), [&](const auto& s) {
    return s.find(prefix) == 0;
  });
}
int main(int argc, char** argv) {
  assert(argc == 2);
  std::ofstream result(argv[1], std::ios::binary);
  int multiBufferCases = 0;
  for (int rate : {0, 1, 2, 8, 9, 10})
  for (int sign : {-1, 0, 1})
  for (bool wrap : {false, true})
  for (bool lav : {false, true}) {
    CDVDAudioCodecPassthrough c;
    auto& p = *c.m_packerMAT;
    p.SetLavStyleEnabled(lav);
    c.m_lavStyleSyncEnabled = lav;
    c.m_format.m_sampleRate = (rate & 8 ? 44100 : 48000) << (rate & 7);
    c.m_format.m_streamInfo.m_sampleRate = c.m_format.m_sampleRate;
    const int scale = 1 << (rate & 7), samples = 40 * scale;
    uint16_t time = wrap ? 65000 : 0;
    uint16_t out = time + 1840 * scale;
    [[maybe_unused]] uint64_t lastSequence = 0;
    size_t branchEmitCount = 0, branchDequeueCount = 0, timingCount = 0;
    bool branchActive = false;
    for (int n = 0; n < 360; ++n) {
      const bool branch = sign != 0 && n == 119;
      if (branch) {
        out += sign * samples;
        time = out - samples - 281 * scale;
        CLog::records.clear();
        branchActive = true;
      }
      auto input = au(time, out, rate, 400 + (n % 5) * 40);
      p.PackTrueHD(input.data(), input.size());
      int outputs = 0;
      for (;;) {
        auto bytes = p.GetOutputFrame();
        if (bytes.empty()) break;
        ++outputs;
        assert(bytes.size() == 61440);
        assert(bytes[8] == 0x07 && bytes[9] == 0x9e);
        assert(bytes[30716] == 0xc3 && bytes[30717] == 0xc1);
        assert(bytes[61416] == 0xc3 && bytes[61417] == 0xc2);
        if constexpr (@DIAGNOSTICS@) {
          @SEQUENCE_CHECK@
        }
        result.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
        write(result, p.GetSamplesOffset());
        write(result, p.HadDiscontinuity());
        c.pending = std::move(bytes);
        c.m_currentPts = 10000000.0 + n * 1000000.0 / 1200.0;
        DVDAudioFrame frame;
        c.GetData(frame);
        write(result, frame.pts); write(result, frame.duration);
        write(result, frame.hasDiscontinuity); write(result, frame.discontinuityCorrection);
        assert(frame.duration == DVD_MSEC_TO_TIME(c.m_format.m_streamInfo.GetDuration()));
      }
      if (branch && lav && outputs > 1) ++multiBufferCases;
      if (branchActive) {
        branchEmitCount = countLogs("CPackerMAT emit:");
        branchDequeueCount = countLogs("CPackerMAT dequeue:");
        timingCount = countLogs("TrueHD MAT timing:");
      }
      time += samples + (n % 2 ? -8 : 8) * scale;
      out += samples;
    }
    if (@DIAGNOSTICS@ && sign != 0 && lav) {
      assert(branchEmitCount == 8 && branchDequeueCount == 8 && timingCount == 8);
    } else {
      assert(branchEmitCount == 0 && branchDequeueCount == 0 && timingCount == 0);
    }
    p.Reset();
    @RESET_CHECK@
    auto input = au(0, 1840 * scale, rate, 400);
    p.PackTrueHD(input.data(), input.size());
    assert(p.GetOutputFrame().empty());
  }
  assert(multiBufferCases == 24);
  std::cout << "72 cases; 24 multi-buffer branches; complete bytes/offsets/flags/codec timing recorded\n";
}
'''


def run(directory, ref=None, mutation=None, sanitize=False):
    directory.mkdir()
    for path in ("utils", "libavutil"):
        (directory / path).mkdir()
    (directory / "utils/log.h").write_text(r'''
#pragma once
#include <cstring>
#include <sstream>
#include <string>
#include <vector>
constexpr int LOGDEBUG=0, LOGERROR=1;
struct CLog {
  inline static std::vector<std::string> records;
  template<class... T> static void Log(int, const char* message, T... args) {
    std::ostringstream s; s << message; ((s << ' ' << args), ...);
    records.push_back(s.str());
  }
};
''')
    (directory / "libavutil/common.h").write_text(
        "#define FFALIGN(x,a) (((x)+(a)-1)&~((a)-1))\n")
    (directory / "libavutil/intreadwrite.h").write_text(
        "#define AV_RB16(p) (((unsigned)(p)[0]<<8)|(p)[1])\n"
        "#define AV_RB32(p) (((unsigned)AV_RB16(p)<<16)|AV_RB16((p)+2))\n")
    header = source(PACKER + ".h", ref)
    packer = source(PACKER + ".cpp", ref)
    diagnostics = "HasOutputDiagnostics" in header
    if mutation == "unbounded":
        assert "m_matFrames + 8" in packer
        packer = packer.replace("m_matFrames + 8", "m_matFrames + 8000")
    if mutation == "wrong-sequence":
        assert "m_matFrames - m_outputQueue.size()" in packer
        packer = packer.replace("m_matFrames - m_outputQueue.size()", "m_matFrames")
    if mutation == "discard-carry":
        carry = "(m_state.nOutputTimeOffset - currentFrameOutputOffset)"
        assert carry in packer
        packer = packer.replace(carry, "0")
    (directory / "PackerMAT.h").write_text(header)
    (directory / "PackerMAT.cpp").write_text(packer)
    harness = HARNESS.replace("@GET_DATA@", block(source(CODEC, ref),
        "void CDVDAudioCodecPassthrough::GetData(DVDAudioFrame &frame)"))
    harness = harness.replace("@DURATION@", block(source(
        "xbmc/cores/AudioEngine/Utils/AEStreamInfo.cpp", ref),
        "double CAEStreamInfo::GetDuration() const"))
    harness = harness.replace("@DIAGNOSTICS@", "true" if diagnostics else "false")
    harness = harness.replace("@SEQUENCE_CHECK@", r'''
          assert(p.GetOutputFrameNumber() == ++lastSequence);
''' if diagnostics else "")
    harness = harness.replace("@RESET_CHECK@", r'''
    assert(p.GetOutputFrameNumber() == 0 && !p.HasOutputDiagnostics());
''' if diagnostics else "")
    (directory / "main.cpp").write_text(harness)
    command = ["g++", "-std=c++17", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
               "-I" + str(directory), "-I" + str(ROOT / "xbmc"),
               str(directory / "main.cpp"), str(directory / "PackerMAT.cpp"),
               "-o", str(directory / "run")]
    if sanitize:
        command[1:1] = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    compiled = subprocess.run(command, capture_output=True, text=True)
    if compiled.returncode:
        raise RuntimeError(compiled.stderr)
    output = directory / "output.bin"
    executed = subprocess.run([str(directory / "run"), str(output)],
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"}, capture_output=True, text=True)
    if mutation:
        assert executed.returncode != 0 and "Assertion" in executed.stderr, executed.stderr
        print(f"Rejected {mutation}")
        return None
    if executed.returncode:
        raise RuntimeError(executed.stderr)
    print(executed.stdout.strip())
    return output.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", help="compare full output against a Git revision")
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="truehd-mat-") as temporary:
        directory = Path(temporary)
        actual = run(directory / "actual", sanitize=args.sanitize)
        if args.baseline_ref:
            previous = run(directory / "baseline", args.baseline_ref, sanitize=args.sanitize)
            assert actual == previous, "MAT bytes, metadata or codec timing changed"
            print(f"Baseline equivalence: {len(actual)} bytes identical")
        for mutation in ("unbounded", "wrong-sequence", "discard-carry"):
            run(directory / mutation, mutation=mutation)


if __name__ == "__main__":
    main()
