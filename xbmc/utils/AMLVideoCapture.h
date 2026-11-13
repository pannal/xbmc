/*
 *  Copyright (C) 2026 Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>
#include <vector>

// Reads the video layer from vdin1's VD1 write-back (/dev/vdin1, loopback port
// VIU1_WB0_VD1): after the VPP scaler, its HDR processing and the input stage
// of the Dolby Vision core (before its display mapping), without the GUI.
// Needs the tvin_vdin and tvin_viuin modules and a kernel with
// TVIN_IOC_G_MEM_INFO.
namespace AML_VIDEO_CAPTURE
{

// How the captured codes decode.
enum class Signal
{
  // BT.709 limited range. VDIN maps an HDR10 or HDR10+ output to SDR itself.
  SDR,
  // An HLG output, which VDIN passes through: BT.2020 limited range HLG,
  // tone-mapped here from a 1000 cd/m2 display.
  HLG,
  // The Dolby Vision core's input codes of the source, which the TV maps:
  // tone-mapped here.
  DV_TV_LED,
  // The Dolby Vision core's input codes, which it maps to an HDR display (low
  // latency, DV to HDR10): tone-mapped here from that display's peak.
  DV_MAPPED,
  // The Dolby Vision core's input codes, which it maps to an SDR display (DV
  // to SDR10 or SDR8).
  DV_SDR,
};

// The colour space of the Dolby Vision core's codes on VD1, which sits before
// its display mapping.
enum class Chroma
{
  // Classic IPT: what VS10 makes of an HDR10, HLG or SDR source, of a Dolby
  // Vision source it converts to SDR or HDR10, and of HDR10+ that Kodi
  // converts to Dolby Vision.
  IPT,
  // ICtCp: the composer's output of a Dolby Vision title, by its metadata.
  ICTCP,
};

struct Frame
{
  Signal signal{Signal::SDR};
  Chroma chroma{Chroma::IPT};
  // The peak DV_TV_LED, DV_MAPPED and HLG tone-map from, in cd/m2.
  float peakNits{0.0f};
  // The video window on the display, and the display size.
  int x{0};
  int y{0};
  int width{0};
  int height{0};
  int displayWidth{0};
  int displayHeight{0};
  // The captured frame, the window at its size unless VDIN scaled it: NV12,
  // luma rows, then interleaved Cb/Cr rows at half the height, both with
  // stride bytes per row.
  int frameWidth{0};
  int frameHeight{0};
  int stride{0};
  std::vector<uint8_t> nv12;
};

enum class Result
{
  OK,
  NO_VIDEO, // the video layer is off or has no window on the display
  UNAVAILABLE, // no vdin1 capture, or no video state to place it by: use amvideocap
  FAILED, // vdin1 is busy (another reader has it) or gave no frame
};

// Reads the frame on the display now; blocks for a few frame periods.
Result Capture(Frame& frame);

} // namespace AML_VIDEO_CAPTURE
