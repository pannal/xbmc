/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyRoomPreset.h"

#include <algorithm>
#include <cstdio>

namespace
{
//! The distance the presets were tuned at: unit scale 1.
constexpr double TUNED_DISTANCE_M = 1.0;

//! The margin the engine keeps between the scene and its walls
//! (ROOM_FLOOR_MARGIN_M in omniphony-renderer's binaural/reflections.rs): it
//! floors every room axis at 2 x (distance + this).
constexpr double ENGINE_ROOM_MARGIN_M = 0.35;

//! The longest axis the engine takes (MAX_ROOM_M in the same file); it clamps
//! every axis to this.
constexpr double ENGINE_MAX_ROOM_M = 20.0;

//! The wall's high-frequency cutoff: a furnished room. Stated rather than left
//! to the engine's 6 kHz default, so the reflected copies stay darker than the
//! direct sound they would otherwise colour.
constexpr int WALL_CUTOFF_HZ = 4000;

//! Per-reflection wall gain, applied on top of the engine's distance law.
constexpr const char* REFLECTION_LEVEL = "0.5";

/*!
 * \brief The shoebox each preset stands for at the tuned distance.
 *
 * Three rooms rather than a pair of dimension sliders, because what a listener
 * can judge is whether the space sounds right, not whether it is 4.2 m across.
 * Carried over unchanged from the in-process renderer, where they were tuned.
 */
OmniphonyRoom TunedRoom(int preset)
{
  static const OmniphonyRoom rooms[] = {
      {false, 0.0, 0.0, 0.0}, // off
      {true, 3.0, 3.5, 2.4}, // small
      {true, 4.0, 5.0, 2.7}, // medium
      {true, 6.0, 8.0, 3.2}, // large
  };
  if (preset < OMNIPHONY_ROOM_OFF || preset > OMNIPHONY_ROOM_LARGE)
    preset = OMNIPHONY_ROOM_MEDIUM;
  return rooms[preset];
}

std::string Format(const char* fmt, double value)
{
  char buf[64];
  std::snprintf(buf, sizeof(buf), fmt, value);
  return buf;
}
} // namespace

OmniphonyRoom OmniphonyRoomFor(int preset, double distanceM)
{
  OmniphonyRoom room = TunedRoom(preset);
  if (!room.enabled)
    return room;

  const double distance = std::max(distanceM, TUNED_DISTANCE_M);
  const double floor = 2.0 * (distance + ENGINE_ROOM_MARGIN_M);
  const double smallest = std::min({room.width, room.depth, room.height});
  const double largest = std::max({room.width, room.depth, room.height});

  // Grown with the distance, and at least enough to hold the scene - but no
  // further than the engine takes, or it would clamp the long axes to the same
  // length. Past that point the short axis comes up to the floor on its own,
  // which keeps all three apart because the floor stays below the other two.
  const double scale = std::min(std::max(distance / TUNED_DISTANCE_M, floor / smallest),
                                ENGINE_MAX_ROOM_M / largest);
  room.width = std::max(room.width * scale, floor);
  room.depth = std::max(room.depth * scale, floor);
  room.height = std::max(room.height * scale, floor);
  return room;
}

std::string OmniphonyRoomYaml(int preset, double distanceM, int reverbPercent)
{
  const OmniphonyRoom r = OmniphonyRoomFor(preset, distanceM);
  if (!r.enabled)
    return "    reflections:\n"
           "      enabled: false\n"
           "    reverb:\n"
           "      enabled: false\n";

  std::string yaml = "    reflections:\n";
  yaml += "      enabled: true\n";
  yaml += Format("      room_width_m: %.2f\n", r.width);
  yaml += Format("      room_depth_m: %.2f\n", r.depth);
  yaml += Format("      room_height_m: %.2f\n", r.height);
  yaml += std::string("      level: ") + REFLECTION_LEVEL + "\n";
  yaml += "      wall_cutoff_hz: " + std::to_string(WALL_CUTOFF_HZ) + "\n";

  // Reverb is the tail, reflections are the room; the tail without the room
  // sounds like an effect rather than a place, so it follows the room away.
  yaml += "    reverb:\n";
  if (reverbPercent <= 0)
  {
    yaml += "      enabled: false\n";
  }
  else
  {
    yaml += "      enabled: true\n";
    yaml += Format("      level: %.2f\n", reverbPercent / 100.0);
    yaml += "      rt60_s: 0.35\n";
    yaml += "      predelay_ms: 20\n";
  }
  return yaml;
}
