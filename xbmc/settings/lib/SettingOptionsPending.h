/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <exception>

// An integer options filler has no fresh result yet. The caller must discard
// partial/previous options, preserve the value, and retry without holding a wait.
class CSettingOptionsPending : public std::exception
{
public:
  const char* what() const noexcept override { return "Setting options pending"; }
};
