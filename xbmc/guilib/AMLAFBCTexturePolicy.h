/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <cstdint>
#include <fstream>

namespace AML_AFBC
{
enum class ImageClass
{
  None,
  Background,
  Artwork
};

// Decoded RGBA size, not JPEG/PNG file size. This is a conservative admission
// policy, not a measured break-even point. Keep small assets on the cheap path.
constexpr uint64_t MIN_ARTWORK_PIXELS = 512 * 1024 / 4;
inline ImageClass Classify(
    unsigned int width, unsigned int height, bool background, bool alpha, bool fanart, bool images)
{
  if (!width || !height || width > 4096 || height > 4096)
    return ImageClass::None;
  if (images && width >= 128 && height >= 128 && uint64_t(width) * height >= MIN_ARTWORK_PIXELS)
    return background ? ImageClass::Background : ImageClass::Artwork;
  // afbcfanart alone admits opaque background fanart, roughly 16:9 and at
  // least 640 pixels wide.
  const uint64_t aspect = uint64_t(width) * 100 / height;
  if (fanart && background && !alpha && width >= 640 && aspect >= 170 && aspect <= 185)
    return ImageClass::Background;
  return ImageClass::None;
}

// The kernel's arm64 ION fix cleans and invalidates device-less pages. Without
// it, the Mali import and conversion still succeed, but dirty cache lines can
// later overwrite the GPU-written image. The fix reports itself here; older
// kernels have no such file.
constexpr const char* KERNEL_SYNC_PARAMETER = "/sys/module/ion/parameters/arm64_deviceless_sync";
inline bool KernelSyncsDevicelessPages(const char* path = KERNEL_SYNC_PARAMETER)
{
  std::ifstream file(path);
  char value = 0;
  return file.get(value) && value == 'Y';
}
} // namespace AML_AFBC
