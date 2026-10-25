/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "TimeUtils.h"

#include "XBDateTime.h"
#include "windowing/GraphicContext.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

#if   defined(TARGET_DARWIN)
#include <mach/mach_time.h>
#include <CoreVideo/CVHostTime.h>
#elif defined(TARGET_WINDOWS)
#include <windows.h>
#else
#include <time.h>
#endif

namespace
{
auto startTime = std::chrono::steady_clock::now();
}

int64_t CurrentHostCounter(void)
{
#if defined(TARGET_DARWIN)
  return( (int64_t)CVGetCurrentHostTime() );
#elif defined(TARGET_WINDOWS)
  LARGE_INTEGER PerformanceCount;
  QueryPerformanceCounter(&PerformanceCount);
  return( (int64_t)PerformanceCount.QuadPart );
#else
  struct timespec now;
#if defined(CLOCK_MONOTONIC_RAW) && !defined(TARGET_ANDROID)
  clock_gettime(CLOCK_MONOTONIC_RAW, &now);
#else
  clock_gettime(CLOCK_MONOTONIC, &now);
#endif // CLOCK_MONOTONIC_RAW && !TARGET_ANDROID
  return( ((int64_t)now.tv_sec * 1000000000L) + now.tv_nsec );
#endif
}

int64_t CurrentHostFrequency(void)
{
#if defined(TARGET_DARWIN)
  return( (int64_t)CVGetHostClockFrequency() );
#elif defined(TARGET_WINDOWS)
  LARGE_INTEGER Frequency;
  QueryPerformanceFrequency(&Frequency);
  return( (int64_t)Frequency.QuadPart );
#else
  return( (int64_t)1000000000L );
#endif
}

unsigned int CTimeUtils::frameTime = 0;

static inline uint64_t GetTimeInMs()
{
  timeval now_time;

  ::gettimeofday(&now_time, nullptr);

  return now_time.tv_sec * 1000ll + now_time.tv_usec / 1000ll;
}

static unsigned int GetUptimeInMs()
{
  static bool start_time_set = false;
  static uint64_t start_time = 0;

  uint64_t now_ms = GetTimeInMs();

  if (!start_time_set) {
    start_time = now_ms;
    start_time_set = true;
  }

  return (unsigned int) (now_ms - start_time);
}

void CTimeUtils::UpdateFrameTime(bool flip)
{
  // Sub-millisecond part of the frame time, the uptime at the previous update,
  // the frames presented since the loop last idled (up to 2) and the time
  // between swaps not yet counted as refreshes
  static double frameFraction = 0;
  static unsigned int lastTime = 0;
  static unsigned int presented = 0;
  static double uncounted = 0;

  const unsigned int currentTime = GetUptimeInMs();
  const double fps = CServiceBroker::GetWinSystem()->GetGfxContext().GetFPS();
  // Signed distances from the frame time and from the previous update to now;
  // uptime wraps
  const double ahead = static_cast<int32_t>(currentTime - frameTime) - frameFraction;
  const double interval = static_cast<int32_t>(currentTime - lastTime);
  lastTime = currentTime;

  // Idle: follow the clock
  double step = ahead;
  if (!flip)
    presented = 0;
  else if (fps > 0)
  {
    // A presented frame is shown one refresh after the previous one, or more
    // after a missed refresh. Count refreshes from the time between swaps,
    // carrying what is not yet a refresh to the next frame, so frames slower
    // than the display alternate one and two refreshes as they are shown;
    // except for the first two frames after idling: their swaps return while
    // the loop falls into step with the display, not a refresh apart. Advance
    // by whole refresh periods, so every displayed frame moves animations by
    // the same amount, pulling slowly towards the clock to follow a refresh
    // rate that differs from the reported one.
    const double period = 1000.0 / fps;
    const bool settling = presented < 2;
    double frames = 1;
    if (settling)
    {
      ++presented;
      uncounted = 0;
    }
    else
    {
      uncounted += interval;
      frames = std::max(1.0, std::floor(uncounted / period + 0.25));
      // A refresh rate that differs from the reported one leaves the same
      // remainder every frame: let it leak like the pull below, so it never
      // adds up to a refresh
      uncounted = std::clamp((uncounted - frames * period) * 63 / 64, -period, period);
    }
    if (ahead - frames * period > 4 * period)
      frames = std::round(ahead / period); // far behind, as after a stall: catch up
    else if (ahead < -2 * period)
      frames = 0; // far ahead, as when frames are not paced by the display: hold
    step = frames * period + (ahead - frames * period) / 64;
  }
  if (!(step > 0))
    return; // never backwards

  step += frameFraction;
  const double whole = std::floor(step);
  frameTime += static_cast<unsigned int>(whole);
  frameFraction = step - whole;
}

unsigned int CTimeUtils::GetFrameTime()
{
  return frameTime;
}

CDateTime CTimeUtils::GetLocalTime(time_t time)
{
  CDateTime result;

  tm *local;
#ifdef HAVE_LOCALTIME_R
  tm res = {};
  local = localtime_r(&time, &res); // Conversion to local time
#else
  local = localtime(&time); // Conversion to local time
#endif
  /*
   * Microsoft implementation of localtime returns NULL if on or before epoch.
   * http://msdn.microsoft.com/en-us/library/bf12f0hc(VS.80).aspx
   */
  if (local)
    result = *local;
  else
    result = time; // Use the original time as close enough.

  return result;
}

std::string CTimeUtils::WithoutSeconds(const std::string& hhmmss)
{
  return hhmmss.substr(0, 5);
}
