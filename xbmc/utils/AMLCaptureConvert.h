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

} // namespace AML_CAPTURE_CONVERT
