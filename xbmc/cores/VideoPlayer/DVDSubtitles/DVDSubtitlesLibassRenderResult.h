/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstdint>
#include <memory>
#include <vector>

typedef struct ass_image ASS_Image;

namespace OVERLAY
{
class COverlay;
}

// CPU output owned independently of libass. Construct while the handler is locked;
// only synchronous renderer conversion can access the private image chain.
class CLibassRenderResult
{
public:
  // Supply unchangedBitmaps only for the handler's immediately preceding output
  // when libass reports identical content (changes == 0 or position-only == 1).
  explicit CLibassRenderResult(const ASS_Image* images,
                              const CLibassRenderResult* unchangedBitmaps = nullptr);
  ~CLibassRenderResult();

  CLibassRenderResult(const CLibassRenderResult&) = delete;
  CLibassRenderResult& operator=(const CLibassRenderResult&) = delete;

private:
  friend class OVERLAY::COverlay;
  std::unique_ptr<ASS_Image[]> m_images;
  std::shared_ptr<std::vector<std::vector<uint8_t>>> m_bitmaps;
};
