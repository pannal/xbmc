/*
 *  Copyright (C) 2024 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "cores/VideoPlayer/DVDStreamInfo.h"

#include "HDR10Plus.h"

enum class PeakBrightnessSource {
  Histogram = 0,
  Histogram99,
  MaxScl,
  MaxSclLuminance,
  HistogramPlus
};

int max_pq_to_nits(int pq);

std::vector<uint8_t> create_rpu_nalu_for_hdr10plus(
  const Hdr10PlusMetadata& meta,
  const PeakBrightnessSource& peak_source,
  const HDRStaticMetadataInfo& hdrStaticMetadataInfo,
  bool capMaxPqAtSource);
