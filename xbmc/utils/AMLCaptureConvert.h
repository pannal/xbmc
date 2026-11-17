/*
 *  Copyright (C) 2026 Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>

namespace AML_VIDEO_CAPTURE
{
struct Frame;
}

namespace AML_CAPTURE_CONVERT
{

// Converts the captured video to SDR BT.709 and puts it under the GUI in
// place. bgra holds the GUI pass as read back, premultiplied BGRA, which the
// VPP blends over the video the same way. The video lands in its window,
// scaled from the display's size to the image's; elsewhere the GUI is over
// black. Returns false if nothing was composed.
bool ComposeUnder(
    const AML_VIDEO_CAPTURE::Frame& frame, uint8_t* bgra, int width, int height, int stride);

// Scales the GUI pass as read back, premultiplied BGRA (width * 4 bytes a
// row), to outWidth x outHeight into out (outWidth * 4 a row), bilinear at
// pixel centres. On screen the GUI is scaled to the display's size and the
// video is not, so a GUI rendered smaller than the display is scaled up for
// the video to go under it at its own resolution. Returns false if the sizes
// are not valid.
bool ScaleGui(
    const uint8_t* bgra, int width, int height, uint8_t* out, int outWidth, int outHeight);

} // namespace AML_CAPTURE_CONVERT
