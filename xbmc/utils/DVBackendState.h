/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <charconv>
#include <cstdint>
#include <optional>
#include <string_view>

// Read-only kernel publication: provider generation, applied video backend and newer usability.
// A missing/old kernel cannot establish identity; never fall back to last mode.
struct DVBackendState
{
  uint64_t epoch;
  int backend;
  unsigned int newUsable;

  // Paired registration parameter is decimal 0/1, not bool Y/N. String
  // parsing also rejects failbit/missing-input cases in generic sysfs readers.
  static std::optional<unsigned int> ParseAvailability(std::string_view text)
  {
    if (text == "0")
      return 0;
    if (text == "1")
      return 1;
    return std::nullopt;
  }

  static std::optional<DVBackendState> Parse(std::string_view text)
  {
    if (text.empty())
      return std::nullopt;
    DVBackendState state{};
    const char* end = text.data() + text.size();
    auto generation = std::from_chars(text.data(), end, state.epoch);
    if (generation.ec != std::errc{} || !state.epoch || generation.ptr == end ||
        *generation.ptr != ' ')
      return std::nullopt;
    auto identity = std::from_chars(generation.ptr + 1, end, state.backend);
    if (identity.ec != std::errc{} || state.backend < -1 || state.backend > 1 ||
        identity.ptr == end || *identity.ptr != ' ')
      return std::nullopt;
    auto usable = std::from_chars(identity.ptr + 1, end, state.newUsable);
    if (usable.ec != std::errc{} || state.newUsable > 1)
      return std::nullopt;
    for (const char* tail = usable.ptr; tail != end; ++tail)
      if (*tail != '\n' && *tail != '\r')
        return std::nullopt;
    return state;
  }
};
