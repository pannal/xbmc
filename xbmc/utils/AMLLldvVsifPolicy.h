/*
 *  Copyright (C) 2026 Team CoreELEC
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <optional>
#include <string>

// Pure decision logic for the LLDV VSIF suppression setting, kept free of Kodi
// dependencies so it can be unit-tested on the build host.
namespace AML_LLDV_VSIF
{

// Kernel bool parameters read back as "Y"/"N"; accept "1"/"0" as well.
inline std::optional<bool> ParseKernelBool(std::string value)
{
  while (!value.empty() && (value.back() == '\n' || value.back() == ' '))
    value.pop_back();
  if (value == "Y" || value == "y" || value == "1")
    return true;
  if (value == "N" || value == "n" || value == "0")
    return false;
  return std::nullopt;
}

enum class Action
{
  NONE, // kernel lacks the parameter and the setting is off
  UNSUPPORTED, // setting is on but the kernel lacks the parameter
  READ_ERROR, // first run: the kernel value could not be read, leave it untouched
  ADOPT_KERNEL, // first run: take over the kernel's current value without changing it
  WRITE, // write the setting to the kernel and verify the readback
};

struct Plan
{
  Action action;
  bool value;
};

// adopted: the setting has already taken ownership from the kernel's boot default.
// userChange: the user changed the setting, which always takes ownership.
inline Plan Decide(bool supported, bool adopted, bool userChange, bool setting,
                   std::optional<bool> kernel)
{
  if (!supported)
    return {setting ? Action::UNSUPPORTED : Action::NONE, setting};
  if (!adopted && !userChange)
  {
    // A persisted non-default choice is the user's; it wins over the kernel's boot value.
    if (setting)
      return {Action::WRITE, true};
    if (!kernel)
      return {Action::READ_ERROR, setting};
    return {Action::ADOPT_KERNEL, *kernel};
  }
  return {Action::WRITE, setting};
}

inline bool Verified(bool wanted, std::optional<bool> readback)
{
  return readback.has_value() && *readback == wanted;
}

} // namespace AML_LLDV_VSIF
