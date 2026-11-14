/*
 *  Copyright (C) 2015-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

class CScreenshotAML
{
  public:
    // Captures the video on the display and puts it under the passed
    // overlay: the GUI pass as read back, premultiplied BGRA (4 byte). Reads
    // the video layer through vdin1 where the system has it, else through
    // amvideocap. True with no video on the display; false if the video
    // could not be captured or converted.
    static bool ComposeVideo(unsigned char* buffer, int width, int height);

    // Captures the current visible video framebuffer through amvideocap and
    // blends it into the passed overlay. The buffer format is BGRA (4 byte).
    // amvideocap reads the decoder's frame, which it cannot decode while the
    // decoder writes it compressed (10-bit HEVC).
    static bool CaptureVideoFrame(unsigned char *buffer, int iWidth, int iHeight, bool bBlendToBuffer = true);
};
