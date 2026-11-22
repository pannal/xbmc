/*
 *  Copyright (C) 2024 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "HDR10Plus.h"

#include <vector>

namespace
{
bool ReadPeakDimensions(CBitstreamReader& br, uint8_t& rows, uint8_t& cols)
{
  rows = br.ReadBits(5);
  cols = br.ReadBits(5);
  return !br.HasError() && rows >= 2 && rows <= 25 && cols >= 2 && cols <= 25 &&
         static_cast<unsigned int>(rows * cols) <= br.AvailableBits() / 4;
}
} // namespace

std::optional<Hdr10PlusMetadata> hdr10plus_sei_to_metadata(CBitstreamReader& br)
{
  Hdr10PlusMetadata metadata = {};

  metadata.itu_t_t35_country_code = br.ReadBits(8);
  metadata.itu_t_t35_terminal_provider_code = br.ReadBits(16);
  metadata.itu_t_t35_terminal_provider_oriented_code = br.ReadBits(16);
  metadata.application_identifier = br.ReadBits(8);
  metadata.application_version = br.ReadBits(8);
  metadata.num_windows = br.ReadBits(2);

  if (metadata.num_windows == 0)
    return std::nullopt;

  if (metadata.num_windows > 1) {

    metadata.processing_windows = std::vector<ProcessingWindow>(metadata.num_windows);

    for (uint8_t i = 1; i < metadata.num_windows; i++) {
        ProcessingWindow& window = metadata.processing_windows[i];
        window.window_upper_left_corner_x = br.ReadBits(16);
        window.window_upper_left_corner_y = br.ReadBits(16);
        window.window_lower_right_corner_x = br.ReadBits(16);
        window.window_lower_right_corner_y = br.ReadBits(16);
        window.center_of_ellipse_x = br.ReadBits(16);
        window.center_of_ellipse_y = br.ReadBits(16);
        window.rotation_angle = br.ReadBits(8);
        window.semimajor_axis_internal_ellipse = br.ReadBits(16);
        window.semimajor_axis_external_ellipse = br.ReadBits(16);
        window.semiminor_axis_external_ellipse = br.ReadBits(16);
        window.overlap_process_option = br.ReadBits(1);
    }
  }

  metadata.targeted_system_display_maximum_luminance = br.ReadBits(27);
  metadata.targeted_system_display_actual_peak_luminance_flag = br.ReadBits(1);

  if (metadata.targeted_system_display_actual_peak_luminance_flag) {
    auto& display = metadata.actual_targeted_system_display;
    auto& rows = display.num_rows_targeted_system_display_actual_peak_luminance;
    auto& cols = display.num_cols_targeted_system_display_actual_peak_luminance;
    if (!ReadPeakDimensions(br, rows, cols))
      return std::nullopt;
    auto& matrix = display.targeted_system_display_actual_peak_luminance;
    matrix.resize(rows, std::vector<uint8_t>(cols));
    for (auto& row : matrix)
      for (auto& luminance : row)
        luminance = br.ReadBits(4);
  }

  // Parse luminance info
  if (metadata.num_windows > 0) {

    metadata.luminance = std::vector<Luminance>(metadata.num_windows);

    for (uint8_t i = 0; i < metadata.num_windows; i++) {

      Luminance& luminance = metadata.luminance[i];
      // Parse maxscl and average_maxrgb
      for (int i = 0; i < 3; i++) {
        luminance.maxscl[i] = br.ReadBits(17);
      }
      luminance.average_maxrgb = br.ReadBits(17);

      // Parse distribution maxrgb
      luminance.num_distribution_maxrgb_percentiles = br.ReadBits(4);
      luminance.distribution_maxrgb.resize(luminance.num_distribution_maxrgb_percentiles);
      for (uint8_t i = 0; i < luminance.num_distribution_maxrgb_percentiles; i++) {
        luminance.distribution_maxrgb[i].percentage = br.ReadBits(7);
        luminance.distribution_maxrgb[i].percentile = br.ReadBits(17);
      }
      luminance.fraction_bright_pixels = br.ReadBits(10);
    }
  }

  // Parse mastering display info
  metadata.mastering_display_actual_peak_luminance_flag = br.ReadBits(1);
  if (metadata.mastering_display_actual_peak_luminance_flag) {
    auto& display = metadata.actual_mastering_display;
    auto& rows = display.num_rows_mastering_display_actual_peak_luminance;
    auto& cols = display.num_cols_mastering_display_actual_peak_luminance;
    if (!ReadPeakDimensions(br, rows, cols))
      return std::nullopt;
    auto& matrix = display.mastering_display_actual_peak_luminance;
    matrix.resize(rows * cols);
    for (std::size_t i = 0; i < matrix.size(); ++i)
      matrix[i] = br.ReadBits(4);
  }

  // Conversion uses the first window, but every window must be complete.
  for (unsigned int i = 0; i < metadata.num_windows; ++i)
  {
    const bool toneMapping = br.ReadBits(1);
    if (i == 0)
      metadata.tone_mapping_flag = toneMapping;
    if (toneMapping)
    {
      const auto kneeX = br.ReadBits(12);
      const auto kneeY = br.ReadBits(12);
      const auto count = br.ReadBits(4);
      if (i == 0)
      {
        auto& curve = metadata.bezier_curve;
        curve.knee_point_x = kneeX;
        curve.knee_point_y = kneeY;
        curve.num_bezier_curve_anchors = count;
        curve.bezier_curve_anchors.resize(count);
        for (auto& anchor : curve.bezier_curve_anchors)
          anchor = br.ReadBits(10);
      }
      else
        br.SkipBits(count * 10);
    }

    const bool saturation = br.ReadBits(1);
    const auto weight = saturation ? br.ReadBits(6) : 0;
    if (i == 0)
    {
      metadata.color_saturation_mapping_flag = saturation;
      metadata.color_saturation_weight = weight;
    }
  }

  if (br.HasError())
    return std::nullopt;
  return metadata;
}
