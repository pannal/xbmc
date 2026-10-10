/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "platform/linux/input/LibInputHandler.h"
#include "rendering/gles/RenderSystemGLES.h"
#include "threads/CriticalSection.h"
#include "windowing/WinSystem.h"
#include "threads/SystemClock.h"
#include "system_egl.h"
#include <EGL/fbdev_window.h>
#include "DolbyVisionAML.h"

#include <atomic>
#include <chrono>
#include <optional>
#include <mutex>

class IDispResource;

class CWinSystemAmlogic : public CWinSystemBase
{
public:
  CWinSystemAmlogic();

  bool InitWindowSystem() override;
  bool DestroyWindowSystem() override;

  bool DestroyWindow() override;
  void UpdateResolutions() override;
  bool IsHDRDisplay() override;
  CHDRCapabilities GetDisplayHDRCapabilities() const override;
  float GetDisplayLatency() override;
  float GetGuiSdrPeakLuminance() const override;
  std::pair<float, float> GetGuiColourAdjustment() const override;

  bool Hide() override;
  bool Show(bool show = true) override;
  virtual void Register(IDispResource *resource);
  virtual void Unregister(IDispResource *resource);
protected:
  std::string m_framebuffer_name;
  EGLDisplay m_nativeDisplay;
  fbdev_window *m_nativeWindow;

  RENDER_STEREO_MODE m_stereo_mode;

  bool m_delayDispReset;
  XbmcThreads::EndTime<> m_dispResetTimer;

  CCriticalSection m_resourceSection;
  std::vector<IDispResource*> m_resources;
  std::unique_ptr<CLibInputHandler> m_libinput;
  CHDRCapabilities m_hdr_caps;
  mutable std::mutex m_hdrCapsMutex;
  std::mutex m_hdrRefreshMutex;
  std::atomic<bool> m_hdrRefreshPending{true};
  bool m_hdrCapsValid{false};
  std::optional<CHDRCapabilities> m_hdrLossCandidate;
  std::string m_hdrLossEDID;
  std::chrono::steady_clock::time_point m_hdrLossSince{};
  void RefreshHDRCapabilities();
  bool m_force_mode_switch;

protected:
  // Native mode work requires the exact active display parent. The concrete
  // window owner retains pending creation and owns its public entry point.
  bool CreateNativeWindow(const std::string& name, bool fullScreen,
                          RESOLUTION_INFO& res, CAMLSession::DisplayRequest display);
  bool InitWindowSystem(CAMLSession::DisplayRequest display);
  bool RetireNativeTransactions();

private:
  std::unique_ptr<CDolbyVisionAML> m_dolbyVisionAML;
};
