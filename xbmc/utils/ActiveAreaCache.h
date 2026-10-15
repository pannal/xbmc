/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <array>
#include <cstdint>
#include <string>

namespace ACTIVE_AREA_CACHE
{
struct Identity
{
  std::string path;
  int64_t size{0};
  int64_t modified{0};
  int64_t created{0};
  uint64_t device{0};
  uint64_t inode{0};
  std::string validator; // digest of a strong HTTP ETag; never its plaintext
  int streamIndex{-1};
  int codec{0};
  int width{0};
  int height{0};
  bool nativeDV{false};

  bool operator==(const Identity& other) const;
};

struct Record
{
  int state{4}; // DvDetectState: preserve why native geometry was withheld.
  bool hasStrict{false};
  bool hasSubtitle{false};
  bool highConfidence{false}; // Strict evidence may replace authored native L5.
  std::array<uint16_t, 4> strict{};
  std::array<uint16_t, 4> subtitle{};
};

// HTTP needs size and a modification time or strong ETag; other files need stat
// timestamps. Missing content/stream identity disables persistence. No media writes.
bool ReadIdentity(Identity& identity);
bool Load(const Identity& identity, Record& record);
bool Store(const Identity& identity, const Record& record);
} // namespace ACTIVE_AREA_CACHE
