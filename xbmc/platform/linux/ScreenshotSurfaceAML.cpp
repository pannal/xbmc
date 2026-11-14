/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  Copyright (C) 2020-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "ServiceBroker.h"
#include "rendering/RenderSystem.h"
#include "threads/SingleLock.h"
#include "ScreenshotSurfaceAML.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h"
#include "utils/Screenshot.h"
#include "utils/ScreenshotAML.h"
#include "utils/log.h"
#include "system_gl.h"

void CScreenshotSurfaceAML::Register()
{
  CScreenShot::Register(CScreenshotSurfaceAML::CreateSurface);
}

std::unique_ptr<IScreenshotSurface> CScreenshotSurfaceAML::CreateSurface()
{
  return std::unique_ptr<CScreenshotSurfaceAML>(new CScreenshotSurfaceAML());
}

bool CScreenshotSurfaceAML::Capture()
{
  // Resolve the original session before GUI rendering can deliver callbacks.
  const auto video = CAMLCodec::GetCaptureSource();
  std::unique_lock<CCriticalSection> lock(CServiceBroker::GetWinSystem()->GetGfxContext());
  auto* winsystem = CServiceBroker::GetWinSystem();
  auto* renderSystem = CServiceBroker::GetRenderSystem();
  if (!renderSystem || !renderSystem->CanRender())
    return false;
  // Draw this pass as plain SDR, as the video under it is converted to: no
  // limited range, HDR scale or GUI tuning, and disc menus through their SDR
  // path rather than the PQ menu composite. The next frame draws as before.
  struct PlainPass
  {
    CWinSystemBase* winsystem;
    CRenderSystemBase* renderSystem;
    bool active{true};
    void End()
    {
      if (!active)
        return;
      active = false;
      winsystem->SetPlainGuiCapture(false);
      renderSystem->SetPlainGuiEncoding(false);
    }
    ~PlainPass() { End(); }
  } plain{winsystem, renderSystem};
  winsystem->SetPlainGuiCapture(true);
  renderSystem->SetPlainGuiEncoding(true);
  const auto target = renderSystem->CaptureRenderTarget();
  if (!winsystem->BeginGuiComposite())
    return false;
  auto cancel = [](CWinSystemBase* window) { window->CancelGuiComposite(); };
  std::unique_ptr<CWinSystemBase, decltype(cancel)> composite(winsystem, cancel);
  // Draw the whole GUI into the surface read back below. That surface may hold
  // any older picture: with AFBC scanout the frames on screen live in the
  // presenter's buffers and the EGL surface is never drawn, and in linear mode
  // the back buffer is an older frame. Clear it to transparent first, so that
  // where the GUI draws nothing the video blended in below shows.
  winsystem->GetGfxContext().ResetScissors();
  renderSystem->ClearBuffers(0);
  CServiceBroker::GetGUI()->GetWindowManager().RenderForCapture();
  if (!renderSystem->IsRenderTargetCurrent(target) || !winsystem->EndGuiComposite())
    return false;
  composite.release();

#ifndef HAS_GLES
  glReadBuffer(GL_BACK);
#endif

  //get current viewport
  GLint viewport[4];
  glGetIntegerv(GL_VIEWPORT, viewport);

  m_width  = viewport[2] - viewport[0];
  m_height = viewport[3] - viewport[1];
  m_stride = m_width * 4;
  unsigned char* surface = new unsigned char[m_stride * m_height];

  //read pixels from the backbuffer
#if HAS_GLES >= 2
  glReadPixels(viewport[0], viewport[1], viewport[2], viewport[3], GL_RGBA, GL_UNSIGNED_BYTE, (GLvoid*)surface);
#else
  glReadPixels(viewport[0], viewport[1], viewport[2], viewport[3], GL_BGRA, GL_UNSIGNED_BYTE, (GLvoid*)surface);
#endif

  //make a new buffer and copy the read image to it with the Y axis inverted
  m_buffer = new unsigned char[m_stride * m_height];
  for (int y = 0; y < m_height; y++)
  {
#ifdef HAS_GLES
    // we need to save in BGRA order so XOR Swap RGBA -> BGRA
    unsigned char* swap_pixels = surface + (m_height - y - 1) * m_stride;
    for (int x = 0; x < m_width; x++, swap_pixels+=4)
    {
      std::swap(swap_pixels[0], swap_pixels[2]);
    }
#endif
    memcpy(m_buffer + y * m_stride, surface + (m_height - y - 1) *m_stride, m_stride);
  }

  delete [] surface;
  plain.End();

  // The GUI is read. The video capture waits for frames to arrive, so let the
  // other threads have the graphics context meanwhile.
  lock.unlock();
  // Put the video under the GUI (m_buffer), if it is still the session the GUI
  // was drawn over. A screenshot without the video it was taken of fails,
  // rather than saving the GUI over black.
  auto permit = video.Acquire();
  const bool composed = permit ? CScreenshotAML::ComposeVideo(m_buffer, m_width, m_height)
                               : !video.codec;
  if (!composed)
  {
    CLog::Log(LOGWARNING, "CScreenshotSurfaceAML: {}",
              permit ? "the video could not be captured"
                     : "playback changed while the screenshot was taken");
    delete[] m_buffer;
    m_buffer = nullptr;
    return false;
  }
  return true;
}
