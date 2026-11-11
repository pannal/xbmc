/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <atomic>

// Controls whose content an add-on draws with its own graphics calls
// (visualisations, rendering controls). Such a call can bind the window's
// framebuffer itself, so a GUI drawn into another default framebuffer (the
// Amlogic AFBC scanout) keeps such frames on the window surface.
class CGUIExternalRendering
{
public:
  // A control will draw through an add-on in the frame at @frameTime.
  static void Note(unsigned int frameTime)
  {
    s_frameTime = frameTime;
    s_noted = true;
  }
  // Such a control drew within the last few frames up to @frameTime.
  static bool Recent(unsigned int frameTime)
  {
    return s_noted && frameTime - s_frameTime < RECENT_MS;
  }

private:
  static constexpr unsigned int RECENT_MS = 250;
  static inline std::atomic<unsigned int> s_frameTime{0};
  static inline std::atomic<bool> s_noted{false};
};
