/*
 *  Copyright (C) 2024 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DolbyVisionAML.h"

#include <atomic>
#include <chrono>
#include <iostream>
#include <iomanip>
#include <sstream>
#include <string>
#include <thread>

#include "ServiceBroker.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "settings/lib/SettingsManager.h"
#include "settings/lib/Setting.h"
#include "guilib/LocalizeStrings.h"
#include "utils/AMLUtils.h"
#include "utils/log.h"
#include "interfaces/AnnouncementManager.h"
#include "cores/DataCacheCore.h"
#include "settings/AdvancedSettings.h"

using namespace KODI;

static std::shared_ptr<CSettings> settings()
{
  return CServiceBroker::GetSettingsComponent()->GetSettings();
}

static void set_visible(const std::string& id, bool visible) {
  if (auto setting = settings()->GetSetting(id)) setting->SetVisible(visible);
}

// Dolby VSVDB Color space data
// cs == 10 (EPSON LS12000) does not index this array: it uses the XML override
// or hardcoded fallback values in CalculateVSVDBPayload_2()
static double colour_space_data[3][6] = {
    // Rx--[5/8]-------  Ry--[1/4]------  Gx--[1]-  Gy--[1/2]-----  Bx--[1/8]-------  By--[1/32]--------
    {(0.6800 - 0.625), (0.3200 - 0.25), (0.2650), (0.6900 - 0.5), (0.1500 - 0.125), (0.0600 - 0.03125)}, //  0: DCI-P3
    {(0.7080 - 0.625), (0.2920 - 0.25), (0.1700), (0.7970 - 0.5), (0.1310 - 0.125), (0.0460 - 0.03125)}, //  1: BT.2020
    {(0.6400 - 0.625), (0.3300 - 0.25), (0.3000), (0.6000 - 0.5), (0.1500 - 0.125), (0.0600 - 0.03125)}  //  2: BT.709
};

static inline int find_closest_lut_index(int value, const int *lut, int lut_size)
{
  int low = 0, high = (lut_size - 1);
  if (value <= lut[low]) return low;
  if (value >= lut[high]) return high;
  while ((high - low) > 1) {
    int mid = (low + high) / 2;
    if (lut[mid] == value) return mid;
    (lut[mid] < value) ? (low = mid) : (high = mid);
  }
  return ((value - lut[low]) < (lut[high] - value)) ? low : high;
}

static const int max_direct_to_pq_lut[128] = {
  2081, 2249, 2372, 2467, 2547, 2614, 2672, 2724, 2771, 2813, 2851, 2887, 2920, 2950, 2979, 3006,
  3032, 3056, 3079, 3101, 3121, 3141, 3160, 3178, 3196, 3213, 3229, 3245, 3260, 3275, 3289, 3302,
  3316, 3329, 3341, 3354, 3365, 3377, 3388, 3399, 3410, 3421, 3431, 3441, 3451, 3460, 3470, 3479,
  3488, 3497, 3505, 3514, 3522, 3530, 3538, 3546, 3554, 3561, 3569, 3576, 3583, 3590, 3597, 3604,
  3611, 3618, 3624, 3631, 3637, 3643, 3649, 3656, 3662, 3668, 3673, 3679, 3685, 3690, 3696, 3702,
  3707, 3712, 3718, 3723, 3728, 3733, 3738, 3743, 3748, 3753, 3758, 3762, 3767, 3772, 3776, 3781,
  3785, 3790, 3794, 3799, 3803, 3807, 3811, 3816, 3820, 3824, 3828, 3832, 3836, 3840, 3844, 3848,
  3852, 3855, 3859, 3863, 3867, 3870, 3874, 3878, 3881, 3885, 3888, 3892, 3895, 3899, 3902, 3906
};

static const int max_direct_to_nits_lut[128] = {
   100,  150,  200,  250,  300,  350,  400,  450,  500,  550,  600,  650,  700,  750,  800,  850,
   900,  950, 1000, 1050, 1100, 1150, 1200, 1250, 1300, 1350, 1400, 1450, 1500, 1550, 1600, 1650,
  1700, 1750, 1800, 1850, 1900, 1950, 2000, 2050, 2100, 2150, 2200, 2250, 2300, 2350, 2400, 2450,
  2500, 2550, 2600, 2650, 2700, 2750, 2800, 2850, 2900, 2950, 3000, 3050, 3100, 3150, 3200, 3250,
  3300, 3350, 3400, 3450, 3500, 3550, 3600, 3650, 3700, 3750, 3800, 3850, 3900, 3950, 4000, 4050,
  4100, 4150, 4200, 4250, 4300, 4350, 4400, 4450, 4500, 4550, 4600, 4650, 4700, 4750, 4800, 4850,
  4900, 4950, 5000, 5050, 5100, 5150, 5200, 5250, 5300, 5350, 5400, 5450, 5500, 5550, 5600, 5650,
  5700, 5750, 5800, 5850, 5900, 5950, 6000, 6050, 6100, 6150, 6200, 6250, 6300, 6350, 6400, 6450
};

// spreadsheet pq
static const int max_direct_to_pq_lut_2[32] = {
  2055, 2120, 2185, 2250, 2315, 2380, 2445, 2510, 2575, 2640, 2705, 2770, 2835, 2900, 2965, 3030,
  3095, 3160, 3225, 3290, 3355, 3420, 3485, 3550, 3615, 3680, 3745, 3810, 3875, 3940, 4005, 4070
};

// adjusted nits based on pq of spreadsheet
static const int max_direct_to_nits_lut_2_adj[32] = {
    94,  110,  129,  150,  175,  204,  237,  276,  320,  372,  431,  499,  578,  670,  775,  897,
  1037, 1200, 1387, 1605, 1856, 2147, 2485, 2876, 3330, 3857, 4470, 5183, 6014, 6982, 8113, 9434
};

// spreadsheet nits
static const int max_direct_to_nits_lut_2[32] = {
    96,  113,  132,  155,  181,  211,  245,  285,  332,  385,  447,  518,  601,  696,  807,  934,
  1082, 1252, 1450, 1678, 1943, 2250, 2607, 3020, 3501, 4060, 4710, 5467, 6351, 7382, 8588, 10000
};

// V1 VSVDB payload calculation (128-entry LUT based)
void CalculateVSVDBPayload()
{
  int max_lum_nits_value(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM));
  int max_lum_idx = find_closest_lut_index(max_lum_nits_value, max_direct_to_nits_lut, 128);
  int cs(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS));

  int max_lum_pq_value = 0;

  bool dv_dolby_vsvdb_inject(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT));
  bool override_edid(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID));

  if (override_edid)
  {
    if (cs == 3)
    {
      cs = 1;
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
  }
  else if (dv_dolby_vsvdb_inject)
  {
    if ((cs == 3) && aml_display_support_dv())
    {
      aml_get_dv_cap();
      switch (xbmc_dv_cap::dv_gx_i)
      {
        case 43: cs = 1; break;
        case 67: cs = 0; break;
        case 76: cs = 2; break;
        default: cs = 3; break;
      }
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
    else if ((cs == 3) && !aml_display_support_dv())
    {
      cs = 1;
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
  }
  else if (!dv_dolby_vsvdb_inject)
  {
    if (aml_display_support_dv())
    {
      aml_get_dv_cap();
      switch (xbmc_dv_cap::dv_gx_i)
      {
        case 43: cs = 1; break;
        case 67: cs = 0; break;
        case 76: cs = 2; break;
        default: cs = 3; break;
      }
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
      switch (xbmc_dv_cap::dv_ver_i)
      {
        case 1:
        {
          max_lum_idx = xbmc_dv_cap::dv_max_v1_i;
          max_lum_nits_value = max_direct_to_nits_lut[max_lum_idx];
          settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM, max_lum_nits_value);
          break;
        }
        case 2:
        {
          max_lum_nits_value = max_direct_to_nits_lut_2[xbmc_dv_cap::dv_max_v2_i];
          settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM, max_lum_nits_value);
          max_lum_pq_value = max_direct_to_pq_lut_2[xbmc_dv_cap::dv_max_v2_i];
          max_lum_idx = find_closest_lut_index(max_lum_pq_value, max_direct_to_pq_lut, 128);
          if ((max_lum_idx > 17) && (max_lum_idx < 127)) max_lum_idx = max_lum_idx + 1;
          break;
        }
        default:
          break;
      }
    }
    else
    {
      if (cs == 3)
      {
        cs = 1;
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
      }
    }
  }

  // Only validate/correct DISPLAY mode when NOT manually overriding EDID
  if (!override_edid && (cs == 3) && ((xbmc_dv_cap::dv_rx_i == 0) || (xbmc_dv_cap::dv_ry_i == 0) || (xbmc_dv_cap::dv_gx_i == 0) ||
      (xbmc_dv_cap::dv_gy_i == 0) || (xbmc_dv_cap::dv_bx_i == 0) || (xbmc_dv_cap::dv_by_i == 0)))
  {
    cs = 1;
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
  }

  unsigned char byte[7];

  byte[0] = (1 << 5) |
            (1 << 2) |
            (1 << 1) |
            (1 << 0);

  byte[1] = (max_lum_idx << 1) |
            (0 << 0);

  byte[2] = (0 << 1) |
            (1 << 0);

  const double one_256 = 0.00390625;

  if (cs == 3)
  {
      byte[3] = (xbmc_dv_cap::dv_bx_i << 5) |
                ((xbmc_dv_cap::dv_by_i << 2) & 0x1C) |
                (1 << 0);

      byte[4] = (xbmc_dv_cap::dv_gx_i << 1) |
                (xbmc_dv_cap::dv_ry_i & 0x01);

      byte[5] = (xbmc_dv_cap::dv_gy_i << 1) |
                ((xbmc_dv_cap::dv_ry_i >> 1) & 0x01);

      byte[6] = (xbmc_dv_cap::dv_rx_i << 3) |
                ((xbmc_dv_cap::dv_ry_i >> 2) & 0x07);
  }
  else
  {
      // Defensive fallback to prevent Out-Of-Bounds crash if V1 is accidentally routed with cs == 10
      int target_cs = cs;
      if (target_cs >= 3)
        target_cs = 1; // Fallback safely to BT.2020 standard values

      byte[3] = (static_cast<int>(colour_space_data[target_cs][4] / one_256) << 5) |
                ((static_cast<int>(colour_space_data[target_cs][5] / one_256) << 2) & 0x1C) |
                (1 << 0);

      byte[4] = (static_cast<int>(colour_space_data[target_cs][2] / one_256) << 1) |
                (static_cast<int>(colour_space_data[target_cs][1] / one_256) & 0x01);

      byte[5] = (static_cast<int>(colour_space_data[target_cs][3] / one_256) << 1) |
                ((static_cast<int>(colour_space_data[target_cs][1] / one_256) >> 1) & 0x01);

      byte[6] = (static_cast<int>(colour_space_data[target_cs][0] / one_256) << 3) |
                ((static_cast<int>(colour_space_data[target_cs][1] / one_256) >> 2) & 0x07);
  }

  std::stringstream ss;
  for (size_t i = 0; i < 7; i++) {
    ss << std::hex << std::uppercase << std::setw(2) << std::setfill('0') << static_cast<int>(byte[i]);
  }
  settings()->SetString(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_PAYLOAD, ss.str());
}

// V2 VSVDB payload calculation (32-entry LUT based)
void CalculateVSVDBPayload_2()
{
  int max_lum_nits_value(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM));
  int max_lum_idx = find_closest_lut_index(max_lum_nits_value, max_direct_to_nits_lut_2_adj, 32);
  int cs(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS));

  bool dv_dolby_vsvdb_inject(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT));
  bool override_edid(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID));

  if (override_edid)
  {
    if (cs == 3)
    {
      cs = 1;
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
  }
  else if (dv_dolby_vsvdb_inject)
  {
    if ((cs == 3) && aml_display_support_dv())
    {
      aml_get_dv_cap();
      switch (xbmc_dv_cap::dv_gx_i)
      {
        case 43: cs = 1; break;
        case 67: cs = 0; break;
        case 76: cs = 2; break;
        default: cs = 3; break;
      }
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
    else if ((cs == 3) && !aml_display_support_dv())
    {
      cs = 1;
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
    }
  }
  else if (!dv_dolby_vsvdb_inject)
  {
    if (aml_display_support_dv())
    {
      aml_get_dv_cap();
      switch (xbmc_dv_cap::dv_gx_i)
      {
        case 43: cs = 1; break;
        case 67: cs = 0; break;
        case 76: cs = 2; break;
        default: cs = 3; break;
      }
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
      switch (xbmc_dv_cap::dv_ver_i)
      {
        case 1:
        {
          max_lum_nits_value = max_direct_to_nits_lut[xbmc_dv_cap::dv_max_v1_i];
          settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM, max_lum_nits_value);
          max_lum_idx = find_closest_lut_index(max_lum_nits_value, max_direct_to_nits_lut_2_adj, 32);
          break;
        }
        case 2:
        {
          max_lum_idx = xbmc_dv_cap::dv_max_v2_i;
          max_lum_nits_value = max_direct_to_nits_lut_2[max_lum_idx];
          settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM, max_lum_nits_value);
          break;
        }
        default:
          break;
      }
    }
    else
    {
      if (cs == 3)
      {
        cs = 1;
        settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
      }
    }
  }

  // Only validate/correct DISPLAY mode when NOT manually overriding EDID
  if (!override_edid && (cs == 3) && ((xbmc_dv_cap::dv_rx_i == 0) || (xbmc_dv_cap::dv_ry_i == 0) || (xbmc_dv_cap::dv_gx_i == 0) ||
      (xbmc_dv_cap::dv_gy_i == 0) || (xbmc_dv_cap::dv_bx_i == 0) || (xbmc_dv_cap::dv_by_i == 0)))
  {
    cs = 1;
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, cs);
  }

  unsigned char byte[7];

  byte[0] = (2 << 5) |       // Version (2 [010]) in 7-5
            (2 << 2) |       // DM Version (2 [010]) in 4-2
            (0 << 1) |       // Backlight Control (Not Supported [0]) in 1
            (1 << 0);        // YUV 12 Bit (Supported [1]) in 0

  byte[1] = (0 << 3) |       // Minimum Luminance (PQ) in 7-3
            (0 << 2) |       // Global Dimming (Unsupported [0]) in 2
            (3 << 0);        // Backlight Min Lum (Disabled 3 [11]) in 1-0

  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  int dv_type_bits = (dv_type == DV_TYPE_DISPLAY_LED) ? 2 : 1;

  byte[2] = (max_lum_idx << 3) | // Maximum Luminance (PQ) in 7-3
            (0 << 2) |           // Reserved ([0]) in 2
            (dv_type_bits << 0); // DV type in 1-0

  const double one_256 = 0.00390625;
  int dv_12b_444_bits = (dv_type == DV_TYPE_DISPLAY_LED) ? 0 : 1;

  if (cs == 3)
  {
      byte[3] = (xbmc_dv_cap::dv_gx_i << 1) |
                (dv_12b_444_bits << 0);

      byte[4] = (xbmc_dv_cap::dv_gy_i << 1) |
                (0 << 0);

      byte[5] = (xbmc_dv_cap::dv_rx_i << 3) |
                (xbmc_dv_cap::dv_bx_i << 0);

      byte[6] = (xbmc_dv_cap::dv_ry_i << 3) |
                (xbmc_dv_cap::dv_by_i << 0);
  }
  else // Manual mode (0, 1, 2 or 10)
  {
    double r_x, r_y, g_x, g_y, b_x, b_y;
    bool rawBytesParsed = false;

    if (cs == 10)
    {
      // Retrieve the custom hex string from advancedsettings.xml
      std::string customVsvdbCoords = CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_dvCustomVsvdb;

      if (!customVsvdbCoords.empty())
      {
        unsigned int v[12];
        CLog::Log(LOGINFO, "CDolbyVisionAML - Found XML Payload: {}", customVsvdbCoords);

        // Parse 12 colon-separated hexadecimal bytes (verbatim Vertex2 format)
        if (sscanf(customVsvdbCoords.c_str(), "%x:%x:%x:%x:%x:%x:%x:%x:%x:%x:%x:%x",
                   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7], &v[8], &v[9], &v[10], &v[11]) == 12)
        {
          // Input validation: ensure parsed values fit within a single byte
          bool valid = true;
          for (int i = 0; i < 12; i++)
          {
            if (v[i] > 0xFF)
            {
              valid = false;
              break;
            }
          }

          if (valid)
          {
            // v[0..7] are parsed and discarded to accept full hex strings natively.
            // Map the chroma bytes (9-12 of the payload) to the Amlogic registers.
            byte[3] = static_cast<unsigned char>(v[8]);
            byte[4] = static_cast<unsigned char>(v[9]);
            byte[5] = static_cast<unsigned char>(v[10]);
            byte[6] = static_cast<unsigned char>(v[11]);
            rawBytesParsed = true;
            CLog::Log(LOGINFO, "CDolbyVisionAML - XML Payload parsed successfully!");
          }
          else
          {
            CLog::Log(LOGERROR, "CDolbyVisionAML - Out-of-bounds byte values detected in XML Payload!");
          }
        }
        else
        {
          CLog::Log(LOGERROR, "CDolbyVisionAML - Failed to parse XML Payload format! Expected 12 hex bytes separated by colons.");
        }
      }
      else
      {
        CLog::Log(LOGDEBUG, "CDolbyVisionAML - XML Payload is empty, using hardcoded Epson values");
      }

      if (!rawBytesParsed)
      {
        // Fallback calibrated coordinates for Epson LS12000
        r_x = 0.6670 - 0.625;
        r_y = 0.3310 - 0.25;
        g_x = 0.3140;
        g_y = 0.6720 - 0.5;
        b_x = 0.1480 - 0.125;
        b_y = 0.0420 - 0.03125;
      }
    }
    else
    {
      r_x = colour_space_data[cs][0];
      r_y = colour_space_data[cs][1];
      g_x = colour_space_data[cs][2];
      g_y = colour_space_data[cs][3];
      b_x = colour_space_data[cs][4];
      b_y = colour_space_data[cs][5];
    }

    if (!rawBytesParsed)
    {
      byte[3] = (static_cast<int>(g_x / one_256) << 1) | // Gx in 7-1
                (dv_12b_444_bits << 0);                  // 12b 444 in 0

      byte[4] = (static_cast<int>(g_y / one_256) << 1) | // Gy in 7-1
                (0 << 0);                                // 10b 444 in 0

      byte[5] = (static_cast<int>(r_x / one_256) << 3) | // Rx in 7-3
                (static_cast<int>(b_x / one_256) << 0);  // Bx in 2-0

      byte[6] = (static_cast<int>(r_y / one_256) << 3) | // Ry in 7-3
                (static_cast<int>(b_y / one_256) << 0);  // By in 2-0
    }
  }

  std::stringstream ss;
  for (size_t i = 0; i < 7; i++) {
    ss << std::hex << std::uppercase << std::setw(2) << std::setfill('0') << static_cast<int>(byte[i]);
  }
  settings()->SetString(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_PAYLOAD, ss.str());
}

static bool force_modes() {
  return settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_FORCE_MODES);
}

static bool support_dv() {
  return (force_modes() || aml_display_support_dv_std() || aml_display_support_dv_ll() || aml_display_support_hdr_pq());
}

void dv_type_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data) {
  bool force = force_modes();
  list.clear();
  if (force || aml_display_support_dv_std()) list.emplace_back(g_localizeStrings.Get(60023), DV_TYPE_DISPLAY_LED);
  if (force || aml_display_support_dv_ll()) list.emplace_back(g_localizeStrings.Get(60024), DV_TYPE_PLAYER_LED_LLDV);
  if (force || aml_display_support_hdr_pq()) list.emplace_back(g_localizeStrings.Get(60025), DV_TYPE_PLAYER_LED_HDR);
  if (force || aml_display_support_hdr_pq()) list.emplace_back(g_localizeStrings.Get(60579), DV_TYPE_PLAYER_LED_HDR2);
  list.emplace_back(g_localizeStrings.Get(60026), DV_TYPE_VS10_ONLY);
}

void dv_processor_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data) {
  bool force = force_modes();
  list.clear();
  list.emplace_back(g_localizeStrings.Get(60503), 0);
  if (force || aml_display_support_hdr_pq()) list.emplace_back(g_localizeStrings.Get(60580), 2);
  if (force || aml_display_support_hdr_pq()) list.emplace_back(g_localizeStrings.Get(60504), 1);
  if (force || aml_display_support_dv_ll()) list.emplace_back(g_localizeStrings.Get(60506), 4);
  if (force || aml_display_support_dv_ll()) list.emplace_back(g_localizeStrings.Get(60505), 3);
}

void vsvdb_min_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data) {
  list.clear();
  list.emplace_back("PQ 0 (0.00000000 cd/m^2)", 0);
  list.emplace_back("PQ 20 (0.00064354 cd/m^2)", 1);
  list.emplace_back("PQ 40 (0.00223738 cd/m^2)", 2);
  list.emplace_back("PQ 60 (0.00478965 cd/m^2)", 3);
  list.emplace_back("PQ 80 (0.00837904 cd/m^2)", 4);
  list.emplace_back("PQ 100 (0.01310152 cd/m^2)", 5);
  list.emplace_back("PQ 120 (0.01906315 cd/m^2)", 6);
  list.emplace_back("PQ 140 (0.02637791 cd/m^2)", 7);
  list.emplace_back("PQ 160 (0.03516709 cd/m^2)", 8);
  list.emplace_back("PQ 180 (0.04555910 cd/m^2)", 9);
  list.emplace_back("PQ 200 (0.05768953 cd/m^2)", 10);
  list.emplace_back("PQ 220 (0.07170139 cd/m^2)", 11);
  list.emplace_back("PQ 240 (0.08774531 cd/m^2)", 12);
  list.emplace_back("PQ 260 (0.10597988 cd/m^2)", 13);
  list.emplace_back("PQ 280 (0.12657199 cd/m^2)", 14);
  list.emplace_back("PQ 300 (0.14969718 cd/m^2)", 15);
  list.emplace_back("PQ 320 (0.17554001 cd/m^2)", 16);
  list.emplace_back("PQ 340 (0.20429448 cd/m^2)", 17);
  list.emplace_back("PQ 360 (0.23616447 cd/m^2)", 18);
  list.emplace_back("PQ 380 (0.27136414 cd/m^2)", 19);
  list.emplace_back("PQ 400 (0.31011844 cd/m^2)", 20);
  list.emplace_back("PQ 420 (0.35266356 cd/m^2)", 21);
  list.emplace_back("PQ 440 (0.39924746 cd/m^2)", 22);
  list.emplace_back("PQ 460 (0.45013035 cd/m^2)", 23);
  list.emplace_back("PQ 480 (0.50558532 cd/m^2)", 24);
  list.emplace_back("PQ 500 (0.56589883 cd/m^2)", 25);
  list.emplace_back("PQ 520 (0.63137136 cd/m^2)", 26);
  list.emplace_back("PQ 540 (0.70231800 cd/m^2)", 27);
  list.emplace_back("PQ 560 (0.77906912 cd/m^2)", 28);
  list.emplace_back("PQ 580 (0.86197104 cd/m^2)", 29);
  list.emplace_back("PQ 600 (0.95138673 cd/m^2)", 30);
  list.emplace_back("PQ 620 (1.04769654 cd/m^2)", 31);
}

void vsvdb_max_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data) {
  list.clear();
  list.emplace_back("PQ 2055 (96 cd/m^2)", 0);
  list.emplace_back("PQ 2120 (113 cd/m^2)", 1);
  list.emplace_back("PQ 2185 (132 cd/m^2)", 2);
  list.emplace_back("PQ 2250 (155 cd/m^2)", 3);
  list.emplace_back("PQ 2315 (181 cd/m^2)", 4);
  list.emplace_back("PQ 2380 (211 cd/m^2)", 5);
  list.emplace_back("PQ 2445 (245 cd/m^2)", 6);
  list.emplace_back("PQ 2510 (285 cd/m^2)", 7);
  list.emplace_back("PQ 2575 (332 cd/m^2)", 8);
  list.emplace_back("PQ 2640 (385 cd/m^2)", 9);
  list.emplace_back("PQ 2705 (447 cd/m^2)", 10);
  list.emplace_back("PQ 2770 (518 cd/m^2)", 11);
  list.emplace_back("PQ 2835 (601 cd/m^2)", 12);
  list.emplace_back("PQ 2900 (696 cd/m^2)", 13);
  list.emplace_back("PQ 2965 (807 cd/m^2)", 14);
  list.emplace_back("PQ 3030 (934 cd/m^2)", 15);
  list.emplace_back("PQ 3095 (1082 cd/m^2)", 16);
  list.emplace_back("PQ 3160 (1252 cd/m^2)", 17);
  list.emplace_back("PQ 3225 (1450 cd/m^2)", 18);
  list.emplace_back("PQ 3290 (1678 cd/m^2)", 19);
  list.emplace_back("PQ 3355 (1943 cd/m^2)", 20);
  list.emplace_back("PQ 3420 (2250 cd/m^2)", 21);
  list.emplace_back("PQ 3485 (2607 cd/m^2)", 22);
  list.emplace_back("PQ 3550 (3020 cd/m^2)", 23);
  list.emplace_back("PQ 3615 (3501 cd/m^2)", 24);
  list.emplace_back("PQ 3680 (4060 cd/m^2)", 25);
  list.emplace_back("PQ 3745 (4710 cd/m^2)", 26);
  list.emplace_back("PQ 3810 (5467 cd/m^2)", 27);
  list.emplace_back("PQ 3875 (6351 cd/m^2)", 38);
  list.emplace_back("PQ 3940 (7382 cd/m^2)", 29);
  list.emplace_back("PQ 4005 (8588 cd/m^2)", 30);
  list.emplace_back("PQ 4070 (10000 cd/m^2)", 31);
}

void add_vs10_bypass(std::vector<IntegerSettingOption>& list) {list.emplace_back(g_localizeStrings.Get(60063), DOLBY_VISION_OUTPUT_MODE_BYPASS);}
void add_vs10_dv_bypass(std::vector<IntegerSettingOption>& list) {list.emplace_back(g_localizeStrings.Get(60063), DOLBY_VISION_OUTPUT_MODE_IPT);}
void add_vs10_sdr(std::vector<IntegerSettingOption>& list) {list.emplace_back(g_localizeStrings.Get(60064), DOLBY_VISION_OUTPUT_MODE_SDR10);}
void add_vs10_hdr10(std::vector<IntegerSettingOption>& list) {list.emplace_back(g_localizeStrings.Get(60065), DOLBY_VISION_OUTPUT_MODE_HDR10);}
void add_vs10_dv(std::vector<IntegerSettingOption>& list) {list.emplace_back(g_localizeStrings.Get(60066), DOLBY_VISION_OUTPUT_MODE_IPT);}

void vs10_sdr_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data)
{
  bool force = force_modes();
  list.clear();
  add_vs10_bypass(list);
  add_vs10_sdr(list);
  if (force || aml_display_support_hdr_pq()) add_vs10_hdr10(list);
  if (support_dv()) add_vs10_dv(list);
}

void vs10_hdr10_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data)
{
  bool force = force_modes();
  list.clear();
  if (force || aml_display_support_hdr_pq()) add_vs10_bypass(list);
  add_vs10_sdr(list);
  if (force || aml_display_support_hdr_pq()) add_vs10_hdr10(list);
  if (support_dv()) add_vs10_dv(list);
}

void vs10_hdr_hlg_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data)
{
  bool force = force_modes();
  list.clear();
  if (force || aml_display_support_hdr_hlg()) add_vs10_bypass(list);
  add_vs10_sdr(list);
  if (force || aml_display_support_hdr_pq()) add_vs10_hdr10(list);
  if (support_dv()) add_vs10_dv(list);
}

void vs10_dv_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data)
{
  list.clear();
  add_vs10_sdr(list);
  if (support_dv()) add_vs10_dv_bypass(list);
}

void vsvdb_colour_space_filler(const SettingConstPtr& setting, std::vector<IntegerSettingOption>& list, int& current, void* data)
{
  CLog::Log(LOGDEBUG, "CDolbyVisionAML - vsvdb_colour_space_filler: list construction");
  list.clear();
  
  // Base options
  list.emplace_back(g_localizeStrings.Get(60081), 0); // DCI-P3
  list.emplace_back(g_localizeStrings.Get(60082), 1); // BT.2020
  list.emplace_back(g_localizeStrings.Get(60083), 2); // BT.709
  list.emplace_back("EPSON LS12000", 10);             // ID 10

  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  CLog::Log(LOGDEBUG, "CDolbyVisionAML - vsvdb_colour_space_filler: DV Type actuel = {}", (int)dv_type);

  // Add DISPLAY (3) only if Display-LED (0) mode
  if (dv_type == DV_TYPE_DISPLAY_LED)
  {
    CLog::Log(LOGDEBUG, "CDolbyVisionAML - vsvdb_colour_space_filler: Add DISPLAY option");
    list.emplace_back(g_localizeStrings.Get(60563), 3);
  }
}

CDolbyVisionAML::CDolbyVisionAML()
{
}

// VSVDB child visibility:
// - VS10-only: all hidden
// - Player-led modes: colour space and max luminance always visible
// - Override EDID on: all four visible
static void set_vsvdb_children_visible(bool show)
{
  bool override_edid = show && settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
  bool vs10_only = (dv_type == DV_TYPE_VS10_ONLY);

  bool vsvdb_basic = show && !vs10_only;
  bool vsvdb_extended = vsvdb_basic && override_edid;

  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS, vsvdb_basic);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM, vsvdb_basic);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MIN_LUM, vsvdb_extended);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_PAYLOAD, vsvdb_extended);
}

static void set_dv_settings_visible(bool show)
{
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE_ON_LUMINANCE, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_OSD_BRIGHTNESS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR_TM, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_REASSERT_AFTER_MODESWITCH, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT, show);
  set_vsvdb_children_visible(show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5_OSDST, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_RESTRICT_SUBS_ACTIVE_AREA, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_RESTRICT_SUBS_USER_POS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_THROTTLE, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR8, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR10, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_BOOST, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_SRC_MAX_NITS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_PER_FRAME_METADATA, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_TARGET_MIN_LUM, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10_OSD_BRIGHTNESS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10PLUS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDRHLG, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PREFER_CONVERT, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PEAK_BRIGHTNESS_SOURCE, show);
  set_visible(CSettings::SETTING_VIDEOPLAYER_CONVERTDOVI, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_SMART_THRESHOLD, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_OVERRIDE, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_AUDIO_SEAMLESSBRANCH, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_FORCE_MODES, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_HANDSHAKE_BYPASS, show);
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_KEEP_SDR_STATE, show);
}

enum TV_PRESET : int
{
  TV_PRESET_MANUAL = 0,
  TV_PRESET_AUTO,
  TV_PRESET_LG,
  TV_PRESET_SONY,
  TV_PRESET_SAMSUNG,
  TV_PRESET_PANASONIC,
  TV_PRESET_PHILIPS,
  TV_PRESET_TCL
};

static std::atomic<bool> s_applying_tv_preset{false};
// Scheduling state for deferred preset application. When the preset setting changes,
// the apply is deferred to a detached thread so it runs after the current event handling
// completes — critical for "Reset the above settings", which writes the preset then
// iterates to each child and writes its XML default. Running apply after the reset
// burst ensures preset-driven child values are the final state. The scheduled flag also
// suppresses the flip-to-Manual guard during the window so child resets mid-burst don't
// prematurely clear the pending preset.
static std::atomic<bool> s_tv_preset_apply_scheduled{false};
static std::atomic<int> s_tv_preset_pending{TV_PRESET_MANUAL};

static void apply_tv_preset(int preset)
{
  if (preset == TV_PRESET_MANUAL) return;
  s_applying_tv_preset = true;

  bool preset_has_dv = false;
  bool preset_has_hdr10plus = false;
  bool preset_has_hdr10 = false;

  switch (preset)
  {
    case TV_PRESET_AUTO:
      preset_has_dv = aml_display_support_dv();
      preset_has_hdr10plus = aml_display_support_hdr10plus();
      preset_has_hdr10 = preset_has_hdr10plus || aml_display_support_hdr_pq();
      break;
    case TV_PRESET_LG:
    case TV_PRESET_SONY:
      preset_has_dv = true;
      preset_has_hdr10plus = false;
      preset_has_hdr10 = true;
      break;
    case TV_PRESET_SAMSUNG:
      preset_has_dv = false;
      preset_has_hdr10plus = true;
      preset_has_hdr10 = true;
      break;
    case TV_PRESET_PANASONIC:
    case TV_PRESET_PHILIPS:
    case TV_PRESET_TCL:
      preset_has_dv = true;
      preset_has_hdr10plus = true;
      preset_has_hdr10 = true;
      break;
  }

  CLog::Log(LOGINFO,
    "CDolbyVisionAML::apply_tv_preset - preset={}, pnpid={}, classify: dv={}, hdr10plus={}, hdr10={}",
    preset, xbmc_dv_cap::edid_pnpid, preset_has_dv, preset_has_hdr10plus, preset_has_hdr10);

  if (!preset_has_dv && !preset_has_hdr10)
  {
    CLog::Log(LOGINFO, "CDolbyVisionAML::apply_tv_preset - display has no DV and no HDR10, no changes applied");
    s_applying_tv_preset = false;
    return;
  }

  enum DV_MODE cur_mode(static_cast<DV_MODE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE)));
  if (cur_mode == DV_MODE_OFF)
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, DV_MODE_ON_DEMAND);

  // When a file carries both DV and HDR10+ streams, pick the stream the TV
  // actually supports: DV on DV-capable TVs, HDR10+ on Samsung.
  settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY, preset_has_dv ? 0 : 1);

  // HDR10+→DV conversion is on only if the TV can't do HDR10+ natively. Prefer-
  // convert (choose converted HDR10+ over native DV when both present) is never a
  // good default — the native DV stream is always higher quality.
  settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PREFER_CONVERT, false);

  if (preset_has_dv)
  {
    // Pick the best DV output type the connected display actually supports.
    // Prefer Display-LED (DV-std) since the TV owns tonemapping and CMv4.0
    // metadata is useful there. Fall back to LLDV when the TV is DV-LL-only
    // (some Panasonic models), and further to HDR10 4:2:2 12-bit / VS10-only
    // for more exotic cases. 4:4:4 (DV_TYPE_PLAYER_LED_HDR) is deliberately
    // avoided — it washes out DV playback on non-DV-std displays.
    DV_TYPE picked = DV_TYPE_DISPLAY_LED;
    if (!aml_display_support_dv_std())
    {
      if (aml_display_support_dv_ll()) picked = DV_TYPE_PLAYER_LED_LLDV;
      else if (aml_display_support_hdr_pq()) picked = DV_TYPE_PLAYER_LED_HDR2;
      else picked = DV_TYPE_VS10_ONLY;
    }
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE, picked);
    settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT, !preset_has_hdr10plus);
    if (picked == DV_TYPE_DISPLAY_LED)
    {
      // 12-bit Deep Color pipeline targets the VS10 → HDMI TX path on Display-LED
      // and CMv4.0 append is only consumed by the TV in Display-LED too.
      settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_PREFER_12BIT, true);
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND, 3); // Smart
      // Down-convert to CMv2.9 is the opposite of append; keep it off on the
      // Display-LED auto-default (mutually exclusive with append above).
      settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP, false);
    }
  }
  else
  {
    // Samsung-class: no DV on the TV, but HDR10 support. Use Player-LED HDR10
    // 4:2:2 12-bit — the 4:4:4 alternative (DV_TYPE_PLAYER_LED_HDR) washes out
    // DV playback on these displays, and Samsung's HDMI path doesn't advertise
    // LLDV in EDID. HDR2 is already in the filler whenever hdr_pq is supported,
    // so no force.modes juggling needed.
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE, DV_TYPE_PLAYER_LED_HDR2);
    settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT, false);
  }

  s_applying_tv_preset = false;
}

static void schedule_tv_preset_apply(int preset)
{
  if (preset == TV_PRESET_MANUAL) return;

  s_tv_preset_pending.store(preset);

  // If an apply is already scheduled, the updated pending value will be picked up
  // when the existing thread wakes. Avoid spawning a second thread.
  if (s_tv_preset_apply_scheduled.exchange(true)) return;

  std::thread([]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    int preset_to_apply = s_tv_preset_pending.load();
    s_tv_preset_apply_scheduled.store(false);
    apply_tv_preset(preset_to_apply);
  }).detach();
}

// Defer the VSVDB payload re-derivation out of OnSettingChanged.
// CSetting*::SetValue holds the changed setting's exclusive lock across the
// callback dispatch, and set_vsvdb_payload_ver takes the DV-core lock — while
// DV-core lock holders (aml_dv_open/aml_dv_on) read these same settings.
// Taking the DV-core lock from callback context therefore closes the same
// ABBA cycle as the Back->replay GUI freeze (see the lock-ordering rule at
// s_dvCoreMutex in AMLUtils.cpp). The apply thread re-reads current values at
// run time, so bursts (e.g. a TV-preset apply writing several settings)
// coalesce into one write and scheduling order is irrelevant.
static std::atomic<bool> s_vsvdb_apply_scheduled{false};
// True while the deferred VSVDB recompute is writing its derived settings
// (DV_VSVDB_CS / DV_VSVDB_MAX_LUM, both registered callbacks). Like
// s_applying_tv_preset, this suppresses the flip-to-Manual guard: the recompute
// is programmatic, not a user edit. Critically it also closes the reset-time
// hole — a TV-preset apply writes DV mode/type, which schedules this recompute;
// that recompute runs *after* both s_applying_tv_preset and
// s_tv_preset_apply_scheduled have cleared, so without this flag its CS/MaxLum
// write would trip the guard and flip the just-applied preset back to Manual
// (the "reset needs two clicks" bug).
static std::atomic<bool> s_applying_vsvdb{false};
static void schedule_vsvdb_payload_apply()
{
  if (s_vsvdb_apply_scheduled.exchange(true)) return;

  std::thread([]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    // Clear before reading: a setting change landing after the reads below
    // schedules a fresh apply instead of being lost.
    s_vsvdb_apply_scheduled.store(false);
    DOVIStreamMetadata dovi_stream_metadata =
        CServiceBroker::GetDataCacheCore().GetVideoDoViStreamMetadata();
    int source_max_pq = static_cast<int>(dovi_stream_metadata.source_max_pq);
    enum DV_TYPE dv_type(static_cast<DV_TYPE>(
        settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));
    int max_lum_nits_value(
        settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM));
    s_applying_vsvdb = true;
    set_vsvdb_payload_ver(dv_type, max_lum_nits_value, source_max_pq);
    s_applying_vsvdb = false;
  }).detach();
}

bool CDolbyVisionAML::Setup()
{
  CLog::Log(LOGDEBUG, "CDolbyVisionAML::Setup - Begin");

  const auto settingsManager = settings()->GetSettingsManager();

  // Always register fillers and callbacks so Override EDID can enable DV live
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionType", dv_type_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionProcessor", dv_processor_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVSVDBMinLum", vsvdb_min_filler);
// settingsManager->RegisterSettingOptionsFiller("DolbyVisionVSVDBMaxLum", vsvdb_max_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10SDR8", vs10_sdr_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10SDR10", vs10_sdr_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10HDR10", vs10_hdr10_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10HDR10Plus", vs10_hdr10_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10HDRHLG", vs10_hdr_hlg_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVS10DV", vs10_dv_filler);
  settingsManager->RegisterSettingOptionsFiller("DolbyVisionVSVDBColourSpace", vsvdb_colour_space_filler);

  // Register for ui dv mode change - to change on the fly.
  std::set<std::string> settingSet;
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE_ON_LUMINANCE);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_OSD_BRIGHTNESS);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10_OSD_BRIGHTNESS);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_BOOST);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_SRC_MAX_NITS);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_PER_FRAME_METADATA);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_TARGET_MIN_LUM);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MIN_LUM);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PREFER_CONVERT);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5_OSDST);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_OVERRIDE);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_THROTTLE);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_FORCE_MODES);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND);
  settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP);
  settingSet.insert(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY);
  settingsManager->RegisterCallback(this, settingSet);

  // Override EDID is always visible (no DV mode dependency) so users can enable DV on non-DV displays
  set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID, true);

  bool override_edid = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
  bool dv_supported = aml_support_dolby_vision();

  if (!dv_supported && !override_edid)
  {
    set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, false);
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, DV_MODE_OFF);
    set_dv_settings_visible(false);
    CLog::Log(LOGDEBUG, "CDolbyVisionAML::Setup - Device does not support Dolby Vision");
    return true;
  }

  set_dv_settings_visible(true);

  // register for announcements to capture OnWake and re-apply DV if needed.
  auto announcer = CServiceBroker::GetAnnouncementManager();
  announcer->AddAnnouncer(this);

  // Turn on dv - if dv mode is on, limit the menu luminance as menu now can be in DV/HDR.
  aml_dv_start();

  CLog::Log(LOGDEBUG, "CDolbyVisionAML::Setup - Complete");

  return true;
}

void CDolbyVisionAML::OnSettingChanged(const std::shared_ptr<const CSetting>& setting)
{
  if (!setting) return;

  // Audio DDR-priority toggle applies live (no DV state involved) — write the
  // DMC urgent for the DEVICE port immediately and return before the DV logic.
  if (setting->GetId() == CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY)
  {
    aml_set_audio_ddr_urgent(settings()->GetBool(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY));
    return;
  }

  enum DV_TYPE dv_type(static_cast<DV_TYPE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)));

  static enum DV_TYPE previous_dv_type = DV_TYPE_DISPLAY_LED;
  bool reset_dv_vs10_dv = false;
  if ((previous_dv_type == DV_TYPE_VS10_ONLY) && (dv_type != DV_TYPE_VS10_ONLY)) reset_dv_vs10_dv = true;
  previous_dv_type = dv_type;

  enum DV_MODE dv_mode(static_cast<DV_MODE>(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE)));
  unsigned int dv_vp(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR));

  bool dv_type_vp_auto(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO));

  const std::string& settingId = setting->GetId();

  if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET)
  {
    schedule_tv_preset_apply(settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET));
    return;
  }

  // Any direct user edit of a DV setting (not driven by a preset apply) invalidates
  // the preset state — flip it to Manual so the label doesn't misrepresent what's active.
  // Suppressed while a preset apply is in flight (currently running, or scheduled and
  // waiting for the current event — e.g. a reset — to finish before it writes) and while
  // the deferred VSVDB recompute writes its derived settings — that recompute is triggered
  // by the preset apply's own mode/type writes and lands after the two preset flags clear,
  // so without s_applying_vsvdb it would flip the just-applied preset back to Manual.
  if (!s_applying_tv_preset && !s_tv_preset_apply_scheduled.load() && !s_applying_vsvdb.load() &&
      settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET) != TV_PRESET_MANUAL)
  {
    CLog::Log(LOGINFO,
      "CDolbyVisionAML::OnSettingChanged - flipping tv.preset -> Manual (trigger={})", settingId);
    settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TV_PRESET, TV_PRESET_MANUAL);
  }

  // CMv4.0 append and CMv4.0->CMv2.9 strip are mutually exclusive (appending then
  // stripping is nonsense). Enforced here in code rather than via a settings.xml
  // enable-dependency: the dependency greyed the control and fought the TV-preset
  // auto-apply (which force-writes append=2 on Display-LED). Whichever the user just
  // turned on wins; the other is forced off.
  if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP)
  {
    if (settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP) &&
        settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND) != 0)
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND, 0);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND)
  {
    if (settings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND) != 0 &&
        settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP))
      settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP, false);
  }

  if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE)
  {
    schedule_vsvdb_payload_apply();
    if ((dv_mode == DV_MODE_ON) && (dv_vp != 0)) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR, 0);
    if ((dv_mode == DV_MODE_ON) && dv_type_vp_auto) settings()->SetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO, false);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE)
  {
    // Smart default: fall back from Display-LED on non-DV-std displays (e.g. after settings reset).
    // Land on HDR10 4:2:2 12-bit — 4:4:4 (DV_TYPE_PLAYER_LED_HDR) is deliberately avoided
    // because it washes out DV playback on these sinks.
    if (dv_type == DV_TYPE_DISPLAY_LED && !aml_display_support_dv_std() && !force_modes())
    {
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE, DV_TYPE_PLAYER_LED_HDR2);
      return;
    }
    // Only re-show VSVDB children if the DV group is meant to be visible at all —
    // otherwise a reset cascade that flips override.edid off first and then touches
    // DV type would pop VSVDB rows back into view on !dv_supported devices.
    bool dv_group_visible = aml_support_dolby_vision() ||
                            settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
    set_vsvdb_children_visible(dv_group_visible);
    schedule_vsvdb_payload_apply();
    if (reset_dv_vs10_dv) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV, DOLBY_VISION_OUTPUT_MODE_IPT);
    if (dv_type == DV_TYPE_VS10_ONLY) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV, DOLBY_VISION_OUTPUT_MODE_SDR10);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR)
  {
    schedule_vsvdb_payload_apply();
    if ((dv_vp != 0) && (dv_mode == DV_MODE_ON)) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, DV_MODE_ON_DEMAND);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE_ON_LUMINANCE)
  {
    int max(std::dynamic_pointer_cast<const CSettingInt>(setting)->GetValue());
    aml_dv_set_osd_max(max);
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_OSD_BRIGHTNESS)
  {
    // This setting is documented as applying "during DV playback" -- gate the
    // live-apply on aml_dv_playback_active() rather than the current output
    // mode. IPT/IPT_TUNNEL is what a Display-LED setup outputs *during DV
    // playback* too, not just for the GUI, so checking the mode would also
    // block live adjustment during the very playback this setting exists
    // for. Playback-vs-GUI is the actual distinction to make here, since
    // this shares the same underlying dolby_vision_graphic_max kernel
    // parameter as coreelec.amlogic.dolbyvision.mode.on.luminance ("GUI max
    // luminance in menus"), which governs it outside of playback instead.
    if (aml_is_dv_enable() && aml_dv_playback_active() &&
        aml_dv_dolby_vision_mode() != DOLBY_VISION_OUTPUT_MODE_HDR10)
      aml_dv_set_osd_brightness(std::dynamic_pointer_cast<const CSettingInt>(setting)->GetValue());
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_HDR10_OSD_BRIGHTNESS)
  {
    if (aml_is_dv_enable() && aml_dv_dolby_vision_mode() == DOLBY_VISION_OUTPUT_MODE_HDR10)
      aml_dv_set_hdr10_osd_brightness(std::dynamic_pointer_cast<const CSettingInt>(setting)->GetValue());
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_BOOST ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_SRC_MAX_NITS)
  {
    unsigned int dv_out_mode(aml_dv_dolby_vision_mode());
    if (aml_is_dv_enable() && (dv_out_mode == DOLBY_VISION_OUTPUT_MODE_SDR10 || dv_out_mode == DOLBY_VISION_OUTPUT_MODE_SDR8))
      aml_dv_set_sdr_source_max_nits(aml_dv_sdr_boost_param());
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_PER_FRAME_METADATA)
  {
    unsigned int dv_out_mode(aml_dv_dolby_vision_mode());
    if (aml_is_dv_enable() && (dv_out_mode == DOLBY_VISION_OUTPUT_MODE_SDR10 || dv_out_mode == DOLBY_VISION_OUTPUT_MODE_SDR8))
      aml_dv_set_sdr_keep_ext(settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_SDR_PER_FRAME_METADATA));
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_TARGET_MIN_LUM)
  {
    // Affects the tone curve of every VS10 output mode - apply live
    // whenever the DV core is engaged.
    if (aml_is_dv_enable())
      aml_dv_set_target_min_lum(std::dynamic_pointer_cast<const CSettingInt>(setting)->GetValue());
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE_VP_AUTO)
  {
    schedule_vsvdb_payload_apply();
    if (dv_type_vp_auto && (dv_mode == DV_MODE_ON)) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, DV_MODE_ON_DEMAND);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT)
  {
    bool dv_group_visible = aml_support_dolby_vision() ||
                            settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
    set_vsvdb_children_visible(dv_group_visible);
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MIN_LUM)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_DUAL_PRIORITY)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_CONVERT)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_HDR10PLUS_PREFER_CONVERT)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV)
  {
    schedule_vsvdb_payload_apply();
    if (dv_type == DV_TYPE_VS10_ONLY) settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV, DOLBY_VISION_OUTPUT_MODE_SDR10);
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5 ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5 ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_STD_SOURCE_LEVEL_5_OSDST ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_SIGNAL_SUBS ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA ||
           settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_L5_AUTO_LETTERBOX)
  {
    // Re-push the L5 sysfs flags so per-folder override.ini writes from
    // service.p3i.sb take effect mid-playback. Without this, an L5 toggle
    // from Python would only land on the next aml_dv_on(). The override push
    // re-evaluates the auto-letterbox geometry so toggling that key applies
    // (or clears) immediately on cropped content.
    aml_dv_apply_l5_override_sysfs();
    aml_dv_apply_l5_sysfs();
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_LEVEL5_OVERRIDE)
  {
    // L5 active-area override engaged mid-playback (typically from
    // service.p3i.override writing at onAVStarted, ~1.1s after codec Open).
    // After kernel commit 61aaaed51c52 the override uses its own
    // xbmc_override_l5_* sysfs namespace, so detect_stop() (which only
    // zeroes xbmc_detected_l5_*) no longer clobbers our values — order
    // here is therefore informational, not load-bearing.
    aml_dv_apply_l5_override_sysfs();
    aml_dv_apply_l5_sysfs();
    if (aml_dv_l5_override_active())
      aml_dv_detect_active_area_stop();
    // Note: not auto-restarting detect when the override is cleared
    // mid-playback. Hidden setting, normally only the addon writes it, and
    // restart on clear is edge-case enough to defer.
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_FORCE_MODES)
  {
    schedule_vsvdb_payload_apply();
  }
  else if (settingId == CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID)
  {
    bool override_on = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID);
    bool dv_supported = aml_support_dolby_vision();
    bool show = dv_supported || override_on;
    set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, show);
    set_dv_settings_visible(show);
    if (!show)
      settings()->SetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE, DV_MODE_OFF);
    schedule_vsvdb_payload_apply();
  }
}

void CDolbyVisionAML::Announce(ANNOUNCEMENT::AnnouncementFlag flag,
              const std::string& sender,
              const std::string& message,
              const CVariant& data)
{
  // When Wake from Suspend re-trigger DV if in DV_MODE_ON
  if ((flag == ANNOUNCEMENT::System) && (message == "OnWake")) aml_dv_start();

  // When video playback fully stops (not channel-switch), restore DV to IPT
  // mode for the GUI.  aml_dv_close() defers this for DV_MODE_ON to avoid
  // unnecessary HDMI mode switches during live-TV channel changes.
  //
  // The playback-active guard is load-bearing: OnStop is dispatched
  // asynchronously on the announcement thread (CAnnouncementManager is a
  // CThread with a queue), so during a back-to-back DV->DV switch the OnStop
  // for the *previous* title can arrive AFTER the next title's aml_dv_open()
  // has already reconfigured the DV core.  Without it, aml_dv_start() runs
  // its off->Bypass->on(IPT) cycle mid-playback over a live stream,
  // corrupting the HDMI-TX/DV color-space (gray/green "trippy" output that
  // persists into the GUI since DV_MODE_ON keeps the core enabled).
  //
  // All guard checks (DV_MODE_ON, dv enabled, NOT playback-active, not
  // already IPT) now live inside aml_dv_restore_gui_ipt() UNDER the DV-core
  // lock, so the decision and the cycle are atomic vs a concurrent
  // aml_dv_open() — the unlocked check-then-cycle here used to leave a
  // narrower variant of the same race open.
  if ((flag == ANNOUNCEMENT::Player) && (message == "OnStop"))
    aml_dv_restore_gui_ipt("Player.OnStop");
}
