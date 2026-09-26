/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/EGLUtils.h"
#include "cores/VideoPlayer/VideoRenderers/FrameBufferObject.h"
#include "rendering/gles/GuiCompositeShaderGLES.h"
#include "rendering/gles/RenderSystemGLES.h"
#include "utils/GlobalsHandling.h"
#include "utils/StreamDetails.h"
#include "WinSystemAmlogic.h"

#include <chrono>
#include <memory>

namespace KODI
{
namespace WINDOWING
{
namespace AML
{

class CWinSystemAmlogicGLESContext : public CWinSystemAmlogic, public CRenderSystemGLES
{
public:
  CWinSystemAmlogicGLESContext() = default;
  virtual ~CWinSystemAmlogicGLESContext() = default;

  using CWinSystemAmlogic::Register;
  static void Register();
  static std::unique_ptr<CWinSystemBase> CreateWinSystem();

  // Implementation of CWinSystemBase via CWinSystemAmlogic
  CRenderSystemBase *GetRenderSystem() override { return this; }
  bool InitWindowSystem() override;
  bool DestroyWindowSystem() override;
  bool CreateNewWindow(const std::string& name,
                       bool fullScreen,
                       RESOLUTION_INFO& res) override;
  bool DestroyWindow() override;

  bool ResizeWindow(int newWidth, int newHeight, int newLeft, int newTop) override;
  bool SetFullScreen(bool fullScreen, RESOLUTION_INFO& res, bool blankOtherDisplays) override;

  virtual std::unique_ptr<CVideoSync> GetVideoSync(CVideoReferenceClock *clock) override;

  bool SupportsStereo(RENDER_STEREO_MODE mode) const override;

  bool BeginRender() override;

  // Disc menu graphics composite, see CWinSystemBase.
  void RequestMenuComposite(bool menuShown) override;
  bool IsMenuCompositeActive() const override { return m_menuRoute != MenuRoute::NONE; }
  void BeginGuiComposite() override;
  void EndGuiComposite() override;
  bool BeginMenuOverlayRender() override;
  void EndMenuOverlayRender() override;

  EGLDisplay GetEGLDisplay() const;
  EGLSurface GetEGLSurface() const;
  EGLContext GetEGLContext() const;
  EGLConfig  GetEGLConfig() const;
protected:
  void SetVSyncImpl(bool enable) override;
  void PresentRenderImpl(bool rendered) override;

private:
  // Where PQ menu graphics can reach the sink raw: the VPP OSD stage in
  // passthrough (HDR10 out, DV core idle), or DV core2 told the OSD is PQ.
  enum class MenuRoute
  {
    NONE,
    OSD_VPP,
    DV_CORE2
  };
  MenuRoute MenuCompositeRoute() const;
  bool EngageMenuComposite(MenuRoute route);
  void DisengageMenuComposite();
  CGuiCompositeShaderGLES::GuiTransfer MenuCompositeGuiTransfer(MenuRoute route) const;
  bool EnsureFbo(CFrameBufferObject& fbo, int& width, int& height);
  bool EnsureCompositeFbos();
  void CompositeGui();
  void QueueKernelSwitch(const char* path, int value);
  void ApplyPendingKernelSwitch();

  struct RouteInputs
  {
    bool dvEnable = false;
    unsigned int dvVideoProcessor = 0;
    unsigned int dvOutputMode = 0;
    bool dvSwitch = false;
    bool osdSwitch = false;
  };

  CEGLContextUtils m_pGLContext;
  StreamHdrType m_hdrType = StreamHdrType::HDR_TYPE_NONE;

  // Render thread only.
  bool m_menuShown = false;
  bool m_menuReported = false; // this frame's GUI pass reported menu visibility
  bool m_menuEngageFailed = false; // held off until the menu is released
  std::chrono::steady_clock::time_point m_menuGoneSince{};
  MenuRoute m_menuRoute = MenuRoute::NONE;
  RouteInputs m_routeInputs;
  std::chrono::steady_clock::time_point m_routeInputsRead{};
  CGuiCompositeShaderGLES::GuiTransfer m_guiTransfer;
  // Kernel switches to set after the next swap (a route change sets two).
  struct PendingSwitch
  {
    const char* path = nullptr;
    int value = 0;
  };
  PendingSwitch m_pendingSwitches[2];
  MenuRoute m_pendingRoute = MenuRoute::NONE;
  std::chrono::steady_clock::time_point m_pendingRouteSince{};
  std::unique_ptr<CGuiCompositeShaderGLES> m_compositeShader;
  CFrameBufferObject m_guiFbo;
  int m_guiFboWidth = 0;
  int m_guiFboHeight = 0;
  bool m_guiFboBound = false;
  CFrameBufferObject m_menuFbo;
  int m_menuFboWidth = 0;
  int m_menuFboHeight = 0;
  bool m_menuFboHasContent = false;
  bool m_menuScissor = false;
};

}
}
}
