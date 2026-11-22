/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/HevcSei.h"

#include <vector>

#include <gtest/gtest.h>

TEST(TestHevcSei, TrailingBytesAfterStopBit)
{
  // prefix SEI NAL: one 3-byte T.35 message, rbsp stop bit, 4 trailing bytes
  const std::vector<uint8_t> nal{0x4e, 0x01, 4, 3, 0x26, 0x00, 0x04, 0x80, 0x13, 0x77, 0x6b, 0x8c};

  const auto messages = CHevcSei::ParseSeiRbsp(nal.data(), nal.size());
  ASSERT_EQ(1u, messages.size());
  EXPECT_EQ(4, messages[0].m_payloadType);
  EXPECT_EQ(3u, messages[0].m_payloadSize);
}

namespace
{
struct PeakDimensions
{
  unsigned int rows;
  unsigned int cols;
};

std::vector<uint8_t> Hdr10PlusPayload(PeakDimensions target, PeakDimensions mastering,
                                      unsigned int windows = 1)
{
  std::vector<uint8_t> data{0xb5, 0, 0x3c, 0, 1, 4, 1};
  unsigned int bit = 0;
  auto write = [&](unsigned int n, unsigned int value)
  {
    while (n > 0)
    {
      if (bit == 0)
        data.push_back(0);
      data.back() |= ((value >> --n) & 1) << (7 - bit);
      bit = (bit + 1) % 8;
    }
  };
  auto matrix = [&](PeakDimensions dims)
  {
    const auto [rows, cols] = dims;
    write(1, rows || cols);
    if (!rows && !cols)
      return;
    write(5, rows);
    write(5, cols);
    for (unsigned int i = 0; i < rows * cols; ++i)
      write(4, i & 15);
  };

  write(2, windows);
  for (unsigned int i = 1; i < windows; ++i)
  {
    for (unsigned int j = 0; j < 6; ++j)
      write(16, 0);
    write(8, 0); // rotation angle
    for (unsigned int j = 0; j < 3; ++j)
      write(16, 0);
    write(1, 0);
  }
  write(27, 1000);
  matrix(target);
  for (unsigned int i = 0; i < windows; ++i)
  {
    for (unsigned int j = 0; j < 4; ++j)
      write(17, 100 + i);
    write(4, 0);
    write(10, 0);
  }
  matrix(mastering);
  for (unsigned int i = 0; i < windows; ++i)
  {
    write(1, 1); // tone mapping
    write(12, 48 + i);
    write(12, 90 + i);
    write(4, 2);
    write(10, 7);
    write(10, 19);
    write(1, 1);
    write(6, 37 + i);
  }
  return data;
}

auto ExtractPayload(const std::vector<uint8_t>& payload)
{
  std::vector<uint8_t> nal{0x4e, 1, 4};
  size_t size = payload.size();
  while (size >= 255)
  {
    nal.push_back(255);
    size -= 255;
  }
  nal.push_back(static_cast<uint8_t>(size));
  nal.insert(nal.end(), payload.begin(), payload.end());
  nal.push_back(0x80);
  return CHevcSei::ExtractHdr10Plus(CHevcSei::ParseSeiRbsp(nal.data(), nal.size()), nal);
}
} // namespace

TEST(TestHevcSei, Hdr10PlusPeakMatrices)
{
  for (const unsigned int side : {0u, 2u, 16u, 25u})
  {
    const auto metadata = ExtractPayload(Hdr10PlusPayload({side, side}, {side, side}));
    ASSERT_TRUE(metadata);
    EXPECT_EQ(1000u, metadata->targeted_system_display_maximum_luminance);
    EXPECT_EQ(37u, metadata->color_saturation_weight);
    const auto& target =
        metadata->actual_targeted_system_display.targeted_system_display_actual_peak_luminance;
    const auto& mastering = metadata->actual_mastering_display.mastering_display_actual_peak_luminance;
    ASSERT_EQ(side, target.size());
    ASSERT_EQ(side * side, mastering.size());
    for (unsigned int i = 0; i < mastering.size(); ++i)
    {
      ASSERT_EQ(side, target[i / side].size());
      EXPECT_EQ(i & 15, target[i / side][i % side]);
      EXPECT_EQ(i & 15, mastering[i]);
    }
  }
}

TEST(TestHevcSei, Hdr10PlusChecksBothPeakMatrixDimensions)
{
  for (PeakDimensions dims : {PeakDimensions{2, 25}, PeakDimensions{25, 2}})
  {
    EXPECT_TRUE(ExtractPayload(Hdr10PlusPayload(dims, {})));
    EXPECT_TRUE(ExtractPayload(Hdr10PlusPayload({}, dims)));
  }
  for (PeakDimensions dims : {PeakDimensions{1, 2}, PeakDimensions{2, 1},
                              PeakDimensions{26, 2}, PeakDimensions{2, 26}})
  {
    EXPECT_FALSE(ExtractPayload(Hdr10PlusPayload(dims, {})));
    EXPECT_FALSE(ExtractPayload(Hdr10PlusPayload({}, dims)));
  }
}

TEST(TestHevcSei, Hdr10PlusRejectsTruncationAndAcceptsFollowingSei)
{
  const auto complete = Hdr10PlusPayload({2, 25}, {25, 2}, 3);
  auto shortened = complete;
  do
  {
    shortened.pop_back();
    ASSERT_FALSE(ExtractPayload(shortened));
  } while (!shortened.empty());

  const auto metadata = ExtractPayload(complete);
  ASSERT_TRUE(metadata);
  ASSERT_EQ(3u, metadata->luminance.size());
  EXPECT_EQ(102u, metadata->luminance[2].average_maxrgb);
  EXPECT_EQ(48u, metadata->bezier_curve.knee_point_x);
  EXPECT_EQ((std::vector<uint16_t>{7, 19}), metadata->bezier_curve.bezier_curve_anchors);
  EXPECT_EQ(37u, metadata->color_saturation_weight);
}
