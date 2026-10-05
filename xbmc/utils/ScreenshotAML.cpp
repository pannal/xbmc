/*
 *  Copyright (C) 2015-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/ScreenshotAML.h"
#include "utils/PlaybackDiagnostics.h"
#include "utils/log.h"
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>
#include <limits>

#include <sys/ioctl.h>

// taken from linux/amlogic/amports/amvideocap.h - needs to be synced - no changes expected though
#define AMVIDEOCAP_IOC_MAGIC  'V'
#define AMVIDEOCAP_IOW_SET_WANTFRAME_WIDTH      _IOW(AMVIDEOCAP_IOC_MAGIC, 0x02, int)
#define AMVIDEOCAP_IOW_SET_WANTFRAME_HEIGHT     _IOW(AMVIDEOCAP_IOC_MAGIC, 0x03, int)
#define AMVIDEOCAP_IOW_SET_CANCEL_CAPTURE       _IOW(AMVIDEOCAP_IOC_MAGIC, 0x33, int)

// capture format already defaults to GE2D_FORMAT_S24_RGB - no need to pull in all the ge2d headers :)

#define CAPTURE_DEVICEPATH "/dev/amvideocap0"

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
          float alpha = bBlendToBuffer ? buffer[3] / (float)255 : 0.0f;

          if (bBlendToBuffer)
          {
            //B
            buffer[0] = alpha * (float)buffer[0] + (1 - alpha) * (float)videoPtr[0];
            //G
            buffer[1] = alpha * (float)buffer[1] + (1 - alpha) * (float)videoPtr[1];
            //R
            buffer[2] = alpha * (float)buffer[2] + (1 - alpha) * (float)videoPtr[2];
            //A
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
