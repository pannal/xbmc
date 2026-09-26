/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <cstdint>
#include <memory>

// Main-owned target provenance. Keeping the identity alive prevents reuse of a
// render-system address from making an old token valid again.
struct RenderTargetToken
{
  std::shared_ptr<const uint8_t> identity;
  uint64_t generation{0};
};

enum class PresentResult
{
  NOT_ATTEMPTED,
  UNREPORTED, // Other backends retain their existing presentation API.
  SKIPPED,
  TARGET_INVALID,
  SWAP_FAILED,
  SWAP_ACCEPTED // EGL accepted the swap; not physical presentation/GPU completion.
};

enum class RenderAttemptStatus
{
  STOPPED,
  BEGIN_REJECTED,
  CANCELLED,
  END_REJECTED,
  COMMANDS_COMPLETED
};

struct RenderAttemptResult
{
  RenderAttemptStatus status{RenderAttemptStatus::STOPPED};
  bool guiRendered{false};
  PresentResult present{PresentResult::NOT_ATTEMPTED};
};
