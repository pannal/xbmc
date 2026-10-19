/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>

//! The room presets. Order matches the options in settings.xml.
enum OmniphonyRoomPreset
{
  OMNIPHONY_ROOM_OFF = 0,
  OMNIPHONY_ROOM_SMALL = 1,
  OMNIPHONY_ROOM_MEDIUM = 2,
  OMNIPHONY_ROOM_LARGE = 3,
};

//! A shoebox room in metres, as the engine's reflections take it.
struct OmniphonyRoom
{
  bool enabled = false;
  double width = 0.0;
  double depth = 0.0;
  double height = 0.0;
};

/*!
 * \brief The room a preset stands for with the speakers at `distanceM`.
 *
 * The presets were tuned with the speakers 1 m away. The room grows with the
 * distance, as a whole, never below the engine's floor for the scene and never
 * past its 20 m limit, so the engine has no axis left to resize - it floors and
 * clamps each axis on its own, and a room it resizes comes out with axes of the
 * same length, whose coincident reflections make dialogue sound like it comes
 * out of a tin can. A room that reaches the limit keeps its long axis there and
 * raises only its short one. An unknown preset is the medium room.
 */
OmniphonyRoom OmniphonyRoomFor(int preset, double distanceM);

/*!
 * \brief The engine's `reflections:` and `reverb:` lines, indented for
 * `render.binaural`, for a preset at `distanceM` with `reverbPercent` of
 * reverb. Reverb follows the room away: without a room there is none.
 */
std::string OmniphonyRoomYaml(int preset, double distanceM, int reverbPercent);
