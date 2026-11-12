/*
 *  Copyright (C) 2026 Team CoreELEC
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "threads/Thread.h"

#include <array>
#include <atomic>
#include <deque>
#include <utility>

// Measures the scene peak of HDR10 video on the decoded picture and hands it
// to the kernel (xbmc_dv_new_hdr10_meas_peak): HDR10 carries no per-scene peak,
// so the newer Dolby library would otherwise tone-map for the mastering display.
// Like a Dolby Vision grade, the peak belongs to a shot: it only rises inside a
// shot and starts again from the new picture at a cut, so the tone curve does
// not drift in the middle of a shot.
// The capture (/dev/amvideocap0) only sees the source while the HEVC decoder
// also writes its uncompressed 1/4 copy (double_write_mode 3), which the codec
// sets up before it opens the decoder.
class CAMLPeakMeter : private CThread
{
public:
  CAMLPeakMeter();
  ~CAMLPeakMeter() override;

  // srcWidth/srcHeight: coded size; the capture is the double-write copy (1/4 per side).
  // masteringPeak: the stream's mastering peak in nits (0 = unknown), caps each sample.
  void Start(int srcWidth, int srcHeight, float masteringPeak);
  void Stop();
  // Seek/reset: forget the previous scene.
  void Flush() { m_flush = true; }

protected:
  void Process() override;

private:
  // grid: 16x9 mean maxRGB code per cell, for shot cut detection
  bool Measure(float& nits, std::array<float, 144>& grid);
  void Write(int nits);

  int m_width = 0;
  int m_height = 0;
  int m_step = 1;
  float m_cap = 10000.0f;
  int m_written = -1;
  float m_out = 0.0f;
  int64_t m_shotStart = 0;
  bool m_haveShot = false;
  bool m_pendingCut = false;
  std::deque<float> m_diffs; // recent grid changes, the motion baseline
  bool m_haveGrid = false;
  std::array<float, 144> m_grid{};
  std::deque<std::pair<int64_t, float>> m_window;
  std::atomic<bool> m_flush{false};
};
