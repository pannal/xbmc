/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/HDR10PlusConvert.h"

#include <cstdint>
#include <vector>

#include <gtest/gtest.h>

#ifdef HAVE_LIBDOVI

extern "C"
{
#include <libdovi/rpu_parser.h>
}

namespace
{

struct L1Values
{
  uint16_t sourceMinPq = 0;
  uint16_t minPq = 0;
  uint16_t maxPq = 0;
  uint16_t avgPq = 0;
};

// CM v4.0 L1 limits as libdovi/dovi_tool clamp them.
constexpr uint16_t CMV40_L1_MIN_PQ_MAX = 12;
constexpr uint16_t CMV40_L1_AVG_PQ_MIN = 1229;

HDRStaticMetadataInfo MasteringDisplay(uint32_t minLum)
{
  HDRStaticMetadataInfo info;
  info.has_mdcv_metadata = true;
  info.max_lum = 1000;
  info.min_lum = minLum; // 0.0001 cd/m2 units
  info.has_cll_metadata = true;
  info.max_cll = 970;
  info.max_fall = 261;
  return info;
}

// One HDR10+ window with a 1000 nit peak and the given scene average,
// both in 0.1 cd/m2 units as carried in the SEI.
Hdr10PlusMetadata Scene(uint32_t averageMaxRgb)
{
  Hdr10PlusMetadata meta{};
  meta.num_windows = 1;
  Luminance luminance{};
  luminance.maxscl[0] = luminance.maxscl[1] = luminance.maxscl[2] = 10000;
  luminance.average_maxrgb = averageMaxRgb;
  luminance.num_distribution_maxrgb_percentiles = 1;
  luminance.distribution_maxrgb.push_back({99, 10000});
  meta.luminance.push_back(luminance);
  return meta;
}

// Parse the generated RPU NAL the way a DV decoder would and read back L1.
L1Values ConvertAndReadL1(const Hdr10PlusMetadata& meta, uint32_t minLum)
{
  const std::vector<uint8_t> nal = create_rpu_nalu_for_hdr10plus(
      meta, PeakBrightnessSource::HistogramPlus, MasteringDisplay(minLum),
      true /* capMaxPqAtSource: TV-led, max_pq capped at the mastering peak */);

  L1Values values;
  EXPECT_FALSE(nal.empty());
  if (nal.empty())
    return values;

  DoviRpuOpaque* rpu = dovi_parse_unspec62_nalu(nal.data(), nal.size());
  EXPECT_EQ(dovi_rpu_get_error(rpu), nullptr);
  const DoviVdrDmData* dm = dovi_rpu_get_vdr_dm_data(rpu);
  EXPECT_NE(dm, nullptr);
  if (dm)
  {
    values.sourceMinPq = dm->source_min_pq;
    EXPECT_NE(dm->dm_data.level1, nullptr);
    if (dm->dm_data.level1)
    {
      values.minPq = dm->dm_data.level1->min_pq;
      values.maxPq = dm->dm_data.level1->max_pq;
      values.avgPq = dm->dm_data.level1->avg_pq;
    }
    dovi_rpu_free_vdr_dm_data(dm);
  }
  dovi_rpu_free(rpu);
  return values;
}

} // namespace

TEST(TestHDR10PlusConvert, DarkSceneAverageMeetsCmv40Minimum)
{
  // 1 nit scene average is far below the CM v4.0 floor (about 10 nits).
  const L1Values l1 = ConvertAndReadL1(Scene(10), 1);

  EXPECT_EQ(l1.avgPq, CMV40_L1_AVG_PQ_MIN);
  EXPECT_LT(l1.avgPq, l1.maxPq);
}

TEST(TestHDR10PlusConvert, BrightSceneAverageIsKept)
{
  // 100 nit average (PQ 2081) is valid and must pass through unchanged.
  const L1Values l1 = ConvertAndReadL1(Scene(1000), 1);

  EXPECT_EQ(l1.avgPq, 2081);
  EXPECT_EQ(l1.maxPq, 3079);
}

TEST(TestHDR10PlusConvert, L1MinStaysWithinCmv40Limit)
{
  // A 0.005 nit mastering black maps to PQ 62: valid as the source minimum,
  // but above what L1 min may carry.
  const L1Values l1 = ConvertAndReadL1(Scene(500), 50);

  EXPECT_LE(l1.minPq, CMV40_L1_MIN_PQ_MAX);
  EXPECT_EQ(l1.sourceMinPq, 62);
}

#endif // HAVE_LIBDOVI
