/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include "system_gl.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

// A caller-owned DMA-BUF imported as AFBC RGBA8 storage, in either layout
// below, that the exact driver can render to and sample. Not copyable;
// Release() needs no context.
class CAMLAFBCImport
{
public:
  CAMLAFBCImport() = default;
  ~CAMLAFBCImport() { Release(); }
  CAMLAFBCImport(const CAMLAFBCImport&) = delete;
  CAMLAFBCImport& operator=(const CAMLAFBCImport&) = delete;

  // Plain layout: 16x16 superblocks, sparse body, YUV transform. The display
  // layout has 32x8 superblocks, each split: the OSD decoder holds one row of
  // superblocks, and a row of 16x16 ones wider than 1920 pixels does not fit.
  enum class Layout { PLAIN, WIDE_BLOCK };

  static bool Supported();
  bool Import(int fd, unsigned int width, unsigned int height, Layout layout = Layout::PLAIN);
  // Binds the storage to the texture bound to GL_TEXTURE_2D.
  bool BindTexture() const;
  void Release();

private:
  EGLDisplay m_display{EGL_NO_DISPLAY};
  EGLImageKHR m_image{EGL_NO_IMAGE_KHR};
  uint32_t m_mapping{UINT32_MAX};
  struct NativePixmap
  {
    uint32_t width{0}, height{0};
    struct Plane { uint32_t pitch{0}, unused{0}, offset{0}; } plane[3];
    uint32_t reserved{0}, format{0x360bb688}, flags{0};
    int32_t fd[3]{-1, -1, -1};
  } m_pixmap;
};

// Exact-driver external storage for a static texture. Conversion
// must run inside CTexture::LoadToGPUAsync's dedicated EGL upload-context lease.
// Retain this owner until the texture name has been deleted in the GUI context.
class CAMLAFBCTexture
{
public:
  static std::shared_ptr<CAMLAFBCTexture> Create(GLuint source,
                                               unsigned int width,
                                               unsigned int height,
                                               std::string& error);
  // Retire the upload-context cache before EGL handle destruction/reuse.
  static bool HasConversionResources(EGLContext context);
  static void ReleaseConversionResources(EGLContext context);
  ~CAMLAFBCTexture();
  GLuint TakeTexture();

private:
  CAMLAFBCTexture() = default;
  bool Allocate(unsigned int width, unsigned int height, std::string& error);
  CAMLAFBCTexture(const CAMLAFBCTexture&) = delete;
  CAMLAFBCTexture& operator=(const CAMLAFBCTexture&) = delete;

  GLuint m_texture{0};
  EGLDisplay m_display{EGL_NO_DISPLAY};
  EGLImageKHR m_image{EGL_NO_IMAGE_KHR};
  uint32_t m_mapping{UINT32_MAX};
  int m_ion{-1};
  int m_handle{-1};
  int m_fd{-1};
  size_t m_length{0};
  struct NativePixmap
  {
    uint32_t width{0}, height{0};
    struct Plane { uint32_t pitch{0}, unused{0}, offset{0}; } plane[3];
    uint32_t reserved{0}, format{0x360bb688}, flags{0};
    int32_t fd[3]{-1, -1, -1};
  } m_pixmap;
};
