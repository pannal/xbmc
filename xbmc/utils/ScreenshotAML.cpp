/*
 *  Copyright (C) 2015-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/ScreenshotAML.h"
#include "utils/AMLCaptureConvert.h"
#include "utils/AMLVideoCapture.h"
#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>
#include <algorithm>
#include <limits>

#include <sys/ioctl.h>

// taken from linux/amlogic/amports/amvideocap.h - needs to be synced - no changes expected though
#define AMVIDEOCAP_IOC_MAGIC  'V'
#define AMVIDEOCAP_IOW_SET_WANTFRAME_WIDTH      _IOW(AMVIDEOCAP_IOC_MAGIC, 0x02, int)
#define AMVIDEOCAP_IOW_SET_WANTFRAME_HEIGHT     _IOW(AMVIDEOCAP_IOC_MAGIC, 0x03, int)
#define AMVIDEOCAP_IOW_SET_CANCEL_CAPTURE       _IOW(AMVIDEOCAP_IOC_MAGIC, 0x33, int)

// capture format already defaults to GE2D_FORMAT_S24_RGB - no need to pull in all the ge2d headers :)

#define CAPTURE_DEVICEPATH "/dev/amvideocap0"

bool CScreenshotAML::ComposeVideo(unsigned char* buffer, int width, int height)
{
  AML_VIDEO_CAPTURE::Frame frame;
  switch (AML_VIDEO_CAPTURE::Capture(frame))
  {
    case AML_VIDEO_CAPTURE::Result::OK:
      return AML_CAPTURE_CONVERT::ComposeUnder(frame, buffer, width, height, width * 4);
    case AML_VIDEO_CAPTURE::Result::UNAVAILABLE:
      // Without vdin1, read the decoder's frame as before.
      return CaptureVideoFrame(buffer, width, height);
    case AML_VIDEO_CAPTURE::Result::NO_VIDEO:
      // Nothing on the video layer: the GUI is the whole picture.
      return true;
    case AML_VIDEO_CAPTURE::Result::FAILED:
    default:
      // Not an amvideocap read instead, which may be undecodable.
      return false;
  }
}

//the buffer format is BGRA (4 byte)
bool CScreenshotAML::CaptureVideoFrame(unsigned char *buffer, int iWidth, int iHeight, bool bBlendToBuffer)
{
  if (!buffer || iWidth <= 0 || iHeight <= 0 ||
      iWidth > std::numeric_limits<int>::max() / 3 - 31)
    return false;
  const int stride = ((iWidth + 31) & ~31) * 3;
  if (iHeight > std::numeric_limits<int>::max() / stride)
    return false;
  const bool diagnostics = PLAYBACK_DIAGNOSTICS::Enabled();
  const auto captureStart = diagnostics ? PLAYBACK_DIAGNOSTICS::NowUs() : 0;
  bool captured = false;
  int captureFd = open(CAPTURE_DEVICEPATH, O_RDWR, 0);
  if (captureFd >= 0)
  {
    int buffSize = stride * iHeight;
    int readSize = 0;
    // videobuffer should be rgb according to docu - but it is bgr ...
    unsigned char *videoBuffer = new unsigned char[buffSize];

    if (videoBuffer != NULL)
    {
      // configure destination
      if (ioctl(captureFd, AMVIDEOCAP_IOW_SET_WANTFRAME_WIDTH, stride / 3) == 0 &&
          ioctl(captureFd, AMVIDEOCAP_IOW_SET_WANTFRAME_HEIGHT, iHeight) == 0)
        readSize = pread(captureFd, videoBuffer, buffSize, 0);
    }

    close(captureFd);

    if (readSize == buffSize)
    {
      captured = true;

      for (int y = 0; y < iHeight; ++y)
      {
        unsigned char *videoPtr = videoBuffer + y * stride;

        for (int x = 0; x < iWidth; ++x, buffer += 4, videoPtr += 3)
        {
          if (bBlendToBuffer)
          {
            // The overlay is premultiplied, as the GUI pass draws it: it goes
            // over the video as it is, which shows through by 1 - alpha.
            const float under = 1.0f - buffer[3] / 255.0f;
            for (int c = 0; c < 3; ++c) // B, G, R
              buffer[c] = static_cast<unsigned char>(
                  std::min(buffer[c] + under * videoPtr[c] + 0.5f, 255.0f));
            buffer[3] = 0xff;// we are solid now
          }
          else
          {
            memcpy(buffer, videoPtr, 3);
            buffer[3] = 0xff;
          }
        }
      }
    }
    delete [] videoBuffer;
  }
  static PLAYBACK_DIAGNOSTICS::CaptureTimings timings;
  if (diagnostics)
  {
    if (const auto report = timings.Complete(captureStart, PLAYBACK_DIAGNOSTICS::NowUs(), captured))
      CLog::Log(LOGDEBUG, "p3i-capture from_us={} to_us={} calls={} failures={} total_us={} max_us={} "
              "includes=io-and-copy",
              report->fromUs, report->toUs, report->duration.calls, report->failures,
              report->duration.totalUs, report->duration.maxUs);
  }
  return captured;
}
