/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/Geometry.h"

#include <cstdint>
#include <map>
#include <utility>

struct CGUIRenderBounds
{
  CRect bounds;
  uint64_t instance{0};
  bool valid{false};
  bool visible{false};
};

using GUIRenderBoundsMap = std::map<std::pair<int, int>, CGUIRenderBounds>;
