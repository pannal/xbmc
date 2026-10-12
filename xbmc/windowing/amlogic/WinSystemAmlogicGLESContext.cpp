/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "VideoSyncAML.h"
#include "AMLNativeTransaction.h"
#include "DolbyVisionAML.h"
#include "WinSystemAmlogicGLESContext.h"
#include "ServiceBroker.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "guilib/WindowIDs.h"
#include "platform/linux/SysfsPath.h"
#include "settings/AdvancedSettings.h"
#include "settings/SettingsComponent.h"
#include "utils/AMLUtils.h"
#include "utils/PlaybackDiagnostics.h"
#include "utils/PlaybackEndDiagnostics.h"
#include "utils/log.h"
#include "threads/SingleLock.h"
#include "windowing/GraphicContext.h"
#include "windowing/WindowSystemFactory.h"
#include "settings/SettingsComponent.h"
#include "ServiceBroker.h"
#include "settings/Settings.h"
#include "windowing/WinSystem.h"

#include "application/ApplicationComponents.h"
#include "application/ApplicationPowerHandling.h"
#include "cores/DataCacheCore.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h"
#include "filesystem/File.h"
#include "guilib/GUIWindow.h"
#include "utils/DVBackendState.h"
#include "utils/JSONVariantWriter.h"

extern "C"
{
#include <libavutil/pixfmt.h>
}

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>

using namespace KODI;
using namespace KODI::WINDOWING::AML;

namespace
{
bool GuiABRead(const std::string& path, std::string& text)
{
  std::ifstream input(path);
  char buffer[512]{};
  if (!input.getline(buffer, sizeof(buffer)) || (!input.eof() && input.peek() != EOF))
    return false;
  text = buffer;
  while (!text.empty() && (text.back() == '\r' || text.back() == ' '))
    text.pop_back();
  return !text.empty();
}
bool GuiABUint(const std::string& text, uint64_t& value)
{
  const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
  return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}

// Kernel switches that let the OSD plane carry BT.2020 PQ (pannal kernel):
// amvecm skips its OSD SDR->HDR encode, amdolby_vision tells core2 the
// graphics are HDR RGB.
constexpr const char* OSD_PQ_PASSTHROUGH = "/sys/module/am_vecm/parameters/osd_pq_passthrough";
constexpr const char* DV_GRAPHIC_PQ = "/sys/module/amdolby_vision/parameters/dolby_vision_graphic_pq";
constexpr const char* DV_GRAPHIC_MAX = "/sys/module/amdolby_vision/parameters/dolby_vision_graphic_max";
// The VPP's OSD SDR->HDR OOTF: a flat gain table, 512 = 10000 nits.
constexpr const char* VPP_SDR_HDR_GAIN = "/sys/module/am_vecm/parameters/oo_y_lut_sdr_hdr";
constexpr const char* DV_OUTPUT_MODE = "/sys/module/amdolby_vision/parameters/dolby_vision_mode";
constexpr const char* DV_LL_POLICY = "/sys/module/amdolby_vision/parameters/dolby_vision_ll_policy";
constexpr const char* DV_GRAPHIC_MIN = "/sys/module/amdolby_vision/parameters/dolby_vision_graphic_min";
constexpr const char* DV_FLAGS = "/sys/module/amdolby_vision/parameters/dolby_vision_flags";
constexpr unsigned int DV_FLAG_FORCE_DOVI_LL = 0x4000;

// Route inputs are sysfs reads: refresh them at most this often.
constexpr auto ROUTE_INPUTS_REFRESH = std::chrono::milliseconds(250);

// The first engage of the VPP route waits this long: while DV re-engages at a
// playlist change it reads as off or in bypass for ~350 ms, which would look
// like HDR10 output (MenuCompositeRoute() also takes no route then).
constexpr auto OSD_ROUTE_FIRST_SETTLE = std::chrono::milliseconds(250);

unsigned int ReadUint(const char* path)
{
  CSysfsPath sysfs(path);
  return sysfs.Exists() ? sysfs.Get<unsigned int>().value_or(0) : 0;
}

// Rendered frames must report no PQ menu graphics for this long before the
// composite is released, so BD-J clear/redraw cycles do not flap the route.
// Frames that are not rendered (a static menu, nothing dirty) never release it.
constexpr auto MENU_RELEASE_DELAY = std::chrono::milliseconds(500);

// Once engaged, a different route must hold this long before it is taken:
// DV reads as off or in bypass for ~350 ms while it re-engages at a playlist
// change, and following that would flip the kernel switches mid-transition.
constexpr auto MENU_ROUTE_SETTLE = std::chrono::milliseconds(500);

bool KernelSwitchAvailable(const char* path)
{
  CSysfsPath sysfs(path);
  return sysfs.Exists();
}

void MarkGuiDirty()
{
  // At exit the GUI is gone before the window system.
  if (CGUIComponent* gui = CServiceBroker::GetGUI())
    gui->GetWindowManager().MarkDirty();
}

bool IsFullscreenVideoActive()
{
  CGUIComponent* gui = CServiceBroker::GetGUI();
  return gui && gui->GetWindowManager().IsWindowActive(WINDOW_FULLSCREEN_VIDEO);
}

void SetKernelSwitch(const char* path, int value)
{
  CSysfsPath sysfs(path);
  if (!sysfs.Exists())
    return;
  try
  {
    sysfs.Set(value);
  }
  catch (const std::exception&)
  {
  }
}
} // namespace

void CWinSystemAmlogicGLESContext::Register()
{
  KODI::WINDOWING::CWindowSystemFactory::RegisterWindowSystem(CreateWinSystem, "aml");
}

std::unique_ptr<CWinSystemBase> CWinSystemAmlogicGLESContext::CreateWinSystem()
{
  return std::make_unique<CWinSystemAmlogicGLESContext>();
}

bool CWinSystemAmlogicGLESContext::IsPrimaryContextCurrent() const
{
  return m_pGLContext.GetEGLDisplay() != EGL_NO_DISPLAY &&
         eglGetCurrentDisplay() == m_pGLContext.GetEGLDisplay() &&
         m_pGLContext.GetEGLContext() != EGL_NO_CONTEXT &&
         eglGetCurrentContext() == m_pGLContext.GetEGLContext() &&
         m_pGLContext.GetEGLSurface() != EGL_NO_SURFACE &&
         eglGetCurrentSurface(EGL_DRAW) == m_pGLContext.GetEGLSurface();
}

void CWinSystemAmlogicGLESContext::ReleaseCompositeResources()
{
  GuiABFinish("release_composite");
  InvalidateRenderTarget();
  if (IsPrimaryContextCurrent())
  {
    m_guiFbo.Cleanup();
    m_menuFbo.Cleanup();
  }
  else
  {
    m_guiFbo.Abandon();
    m_menuFbo.Abandon();
    if (m_compositeShader)
      m_compositeShader->Abandon();
  }
  m_compositeShader.reset();
  m_guiFboWidth = m_guiFboHeight = m_menuFboWidth = m_menuFboHeight = 0;
  m_guiFboBound = m_menuFboHasContent = false;
}

bool CWinSystemAmlogicGLESContext::DestroyRenderSystem()
{
  GuiABFinish("destroy_render_system");
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  if (!display)
    return false;
  m_displayGeometryReady = false;
  // Application cleanup unbinds the surface before DestroyWindowSystem. Retire
  // these main-owned resources now, without changing route/kernel-switch timing.
  ReleaseCompositeResources();
  return CRenderSystemGLES::DestroyRenderSystem();
}

bool CWinSystemAmlogicGLESContext::InitWindowSystem()
{
  if (m_shutdownRequested)
    return false;
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  if (!display)
    return false;
  CloseTextureResources();
  if (!CWinSystemAmlogic::InitWindowSystem(display.Request()))
  {
    return false;
  }

  // A previous Kodi that died with a disc menu up left its switch set.
  SetKernelSwitch(OSD_PQ_PASSTHROUGH, 0);
  SetKernelSwitch(DV_GRAPHIC_PQ, 0);

  if (!m_pGLContext.CreateDisplay(m_nativeDisplay))
  {
    return false;
  }

  if (!m_pGLContext.InitializeDisplay(EGL_OPENGL_ES_API))
  {
    return false;
  }

  if (!m_pGLContext.ChooseConfig(EGL_OPENGL_ES2_BIT))
  {
    return false;
  }

  CEGLAttributesVec contextAttribs;
  contextAttribs.Add({{EGL_CONTEXT_CLIENT_VERSION, 2}});

  if (!m_pGLContext.CreateContext(contextAttribs))
  {
    return false;
  }

  display.Finish(CAMLDisplayLifecycle::Phase::WAITING_FOR_RESET);
  return true;
}

bool CWinSystemAmlogicGLESContext::PrepareForShutdown()
{
  GuiABFinish("shutdown");
  m_shutdownRequested = true;
  SetNativeGuiWait(false);
  // Drain settings/deferred users before taking display admission: a completing
  // callback must not be fenced out by the shutdown that is waiting for it.
  if (!RetireNativeTransactions())
    return false;
  if (m_shutdownAdmission && *m_shutdownAdmission)
    return true;
  m_shutdownAdmission = std::make_unique<CAMLDisplayLifecycle::Mutation>(m_displayLifecycle);
  // Keep this exact display transaction across service shutdown and all three
  // destruction stages. An active native transaction finishes on its owner.
  return bool(*m_shutdownAdmission);
}

bool CWinSystemAmlogicGLESContext::DestroyWindowSystem()
{
  if (!PrepareForShutdown())
    return false;
  // Also cover direct/failed-startup teardown without Application::Cleanup.
  DestroyRenderSystem();
  ReleaseCompositeResources();
  CloseTextureResources();
  if (m_menuRoute != MenuRoute::NONE)
    DisengageMenuComposite();
  ApplyPendingKernelSwitch(); // no more swaps to wait for
  m_compositeShader.reset();   // while its context exists

  m_pGLContext.DestroyContext();
  m_pGLContext.Destroy();
  const bool result = CWinSystemAmlogic::DestroyWindowSystem();
  m_shutdownAdmission.reset(); // End mutation, keep admission fenced after destruction.
  return result;
}

void CWinSystemAmlogicGLESContext::EngageDolbyVisionAtStartup()
{
  if (!m_firstStartGUI)
    return;
  // A usable first GUI window consumes startup even with DV Off/On or this option off.
  m_firstStartGUI = false;

  if (aml_dv_mode() != DV_MODE_ON_DEMAND || aml_dv_playback_active())
    return;

  const auto settings = CServiceBroker::GetSettingsComponent()->GetSettings();
  if (!settings->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_ENGAGE_AT_STARTUP))
    return;

  // The DV-video selector offers SDR and Off (native DV); only SDR is a conversion.
  const int configuredMode = settings->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV);
  if (configuredMode != static_cast<int>(DOLBY_VISION_OUTPUT_MODE_SDR10))
  {
    CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext::{}: engage at startup skipped: set DV VS10 for DV video to SDR",
              __FUNCTION__);
    return;
  }

  CSysfsPath dolbyVisionMode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
  const auto existingMode = dolbyVisionMode.Get<unsigned int>();
  // Missing/unreadable DV hardware is not a mode mismatch.
  if (!existingMode || *existingMode == static_cast<unsigned int>(configuredMode))
    return;

  aml_dv_on(configuredMode);
  // Publish the new HDMI signalling as well as the DV core's internal mode.
  aml_dv_display_trigger();
  CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: set SDR startup output (existing mode {})",
            __FUNCTION__, *existingMode);
}

bool CWinSystemAmlogicGLESContext::CreateNewWindow(const std::string& name,
                                               bool fullScreen,
                                               RESOLUTION_INFO& res)
{
  GuiABFinish("create_window");
  if (m_shutdownRequested)
    return false;
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  PLAYBACK_DIAGNOSTICS::endDisplay.Generation(m_displayLifecycle.Serial());
  ObserveEndDisplay(display ? "display-admitted" : "display-deferred");
  if (!display)
    return false;
  // Native reads/restoration and the base mode change belong to this exact
  // display request. Never borrow an ambient native owner across retries.
  CAMLNativeTransaction native(display.Request());
  if (!native.TryBegin())
    return false;
  RefreshHDRCapabilities();
  RESOLUTION_INFO current_resolution;
  current_resolution.iWidth = current_resolution.iHeight = 0;
  RENDER_STEREO_MODE stereo_mode = CServiceBroker::GetWinSystem()->GetGfxContext().GetStereoMode();

  // check for frac_rate_policy change
  int fractional_rate = (res.fRefreshRate == floor(res.fRefreshRate)) ? 0 : 1;
  int cur_fractional_rate = fractional_rate;
  if (aml_has_frac_rate_policy())
  {
    CSysfsPath amhdmitx0_frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    cur_fractional_rate = amhdmitx0_frac_rate_policy.Get<int>().value_or(0);
  }

  // If changing in or out of Dolby Vision and it is on then make sure we do a mode swtich - TODO: combine with DV InfoFrame?
  StreamHdrType hdrType = CServiceBroker::GetWinSystem()->GetGfxContext().GetHDRType();
  const auto bypass_dv_mode_switch_gui = CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_HANDSHAKE_BYPASS);

  bool force_mode_switch_by_dv = !bypass_dv_mode_switch_gui &&
                                 ((hdrType != m_hdrType) &&
                                  ((hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION) ||
                                   (m_hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)) &&
                                  (aml_dv_mode() != DV_MODE_OFF));

  PLAYBACK_DIAGNOSTICS::endDisplay.Record(PLAYBACK_DIAGNOSTICS::NowUs(), "display-target", [&] {
    return fmt::format("width={} height={} screen_width={} screen_height={} hz={} hdr={} cached_hdr={} force_dv={}",
        res.iWidth, res.iHeight, res.iScreenWidth, res.iScreenHeight,
        static_cast<double>(res.fRefreshRate), static_cast<int>(hdrType),
        static_cast<int>(m_hdrType), force_mode_switch_by_dv);
  });
  // get current used resolution
  if (!aml_get_native_resolution(&current_resolution))
  {
    CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext::{}: failed to receive current resolution", __FUNCTION__);
    return false;
  }

  // The GUI size is not part of the display mode: the native GUI can be
  // suspended at the same mode (aml_native_gui_prepare), so compare against
  // the window actually created.
  if (m_bWindowCreated && m_nWidth > 0 && m_nHeight > 0 &&
      stereo_mode == RENDER_STEREO_MODE_OFF && m_stereo_mode == RENDER_STEREO_MODE_OFF)
  {
    current_resolution.iWidth = m_nWidth;
    current_resolution.iHeight = m_nHeight;
  }

  CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: "
    "m_bWindowCreated: {}, "
    "frac rate {:d}({:d}), "
    "hdrType: {}({}), force mode switch: {}, bypass DV switch: {}",
    __FUNCTION__,
    m_bWindowCreated,
    fractional_rate, cur_fractional_rate,
    CStreamDetails::DynamicRangeToString(hdrType), CStreamDetails::DynamicRangeToString(m_hdrType), force_mode_switch_by_dv, bypass_dv_mode_switch_gui);

  CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: "
    "cur: iWidth: {:04d}, iHeight: {:04d}, iScreenWidth: {:04d}, iScreenHeight: {:04d}, fRefreshRate: {:02.2f}, dwFlags: {:02x}",
    __FUNCTION__,
    current_resolution.iWidth, current_resolution.iHeight, current_resolution.iScreenWidth, current_resolution.iScreenHeight,
    current_resolution.fRefreshRate, current_resolution.dwFlags);
  CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: "
    "res: iWidth: {:04d}, iHeight: {:04d}, iScreenWidth: {:04d}, iScreenHeight: {:04d}, fRefreshRate: {:02.2f}, dwFlags: {:02x}",
    __FUNCTION__,
    res.iWidth, res.iHeight, res.iScreenWidth, res.iScreenHeight, res.fRefreshRate, res.dwFlags);

  // DV_MODE_ON: aml_dv_close() defers IPT restoration to the async
  // Player.OnStop announcement handler to avoid unnecessary DV cycles during
  // live-TV channel changes.  But that handler's off→on cycle (through Bypass
  // with display_auto_now) corrupts the HDMI TX color-space state, causing
  // persistent color distortion in the GUI.
  // Synchronously restore DV to IPT here, before any HDMI work or early
  // return.  The announcement handler's own checks will then skip the
  // redundant call.  All guard checks (incl. playback-active, which prevents
  // this from firing during playback-start mode switches where aml_dv_open()
  // has already set the correct output mode) run inside
  // aml_dv_restore_gui_ipt() UNDER the DV-core lock — atomic vs a concurrent
  // aml_dv_open() on the codec thread.
  aml_dv_wait_for_pipeline();
  if (aml_dv_restore_gui_ipt("SetFullScreen"))
  {
    // Re-write the display mode to trigger VPP reconfiguration for the
    // new DV output mode, matching what CWinSystemAmlogic::CreateNativeWindow
    // does via aml_dv_display_trigger() in the full mode-switch path.
    aml_dv_display_trigger();
  }

  // check if mode switch is needed
  if (current_resolution.iWidth == res.iWidth && current_resolution.iHeight == res.iHeight &&
      current_resolution.iScreenWidth == res.iScreenWidth && current_resolution.iScreenHeight == res.iScreenHeight &&
      m_bFullScreen == fullScreen && current_resolution.fRefreshRate == res.fRefreshRate &&
      (current_resolution.dwFlags & D3DPRESENTFLAG_MODEMASK) == (res.dwFlags & D3DPRESENTFLAG_MODEMASK) &&
      m_stereo_mode == stereo_mode && m_bWindowCreated && IsPrimaryContextCurrent() &&
      !force_mode_switch_by_dv &&
      (fractional_rate == cur_fractional_rate))
  {
    CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: No need to create a new window", __FUNCTION__);
    EngageDolbyVisionAtStartup();
    display.Finish(m_displayGeometryReady && !m_delayDispReset && CanRender()
                       ? CAMLDisplayLifecycle::Phase::READY
                       : CAMLDisplayLifecycle::Phase::WAITING_FOR_RESET);
    return true;
  }

  // Admission precedes both the native mode change and destruction of the old
  // surface. Only a later successful geometry reset may reopen this session.
  m_displayGeometryReady = false;
  if (!DestroyWindow())
    return false;

  // check if a forced mode switch is required
  if (((current_resolution.iWidth == res.iWidth && current_resolution.iHeight == res.iHeight &&
        current_resolution.iScreenWidth == res.iScreenWidth && current_resolution.iScreenHeight == res.iScreenHeight &&
        current_resolution.fRefreshRate == res.fRefreshRate) &&
       (force_mode_switch_by_dv ||
       (fractional_rate != cur_fractional_rate))) ||
       (m_stereo_mode != stereo_mode))
  {
    m_force_mode_switch = true;
    CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: force mode switch", __FUNCTION__);
  }

  // refresh backup data
  m_hdrType = hdrType;
  m_stereo_mode = stereo_mode;
  m_bFullScreen = fullScreen;

  if (!CWinSystemAmlogic::CreateNativeWindow(name, fullScreen, res, display.Request()))
  {
    return false;
  }

  // Wait for any in-progress DV pipeline restoration to complete before
  // creating the EGL surface. Prevents color corruption when the OnStop
  // handler is restoring IPT while we recreate the GL context.
  aml_dv_wait_for_pipeline();

  if (!m_pGLContext.CreateSurface(static_cast<EGLNativeWindowType>(m_nativeWindow)))
  {
    ObserveEndDisplay("surface-failed");
    return false;
  }

  if (!m_pGLContext.BindContext())
  {
    ObserveEndDisplay("bind-failed");
    return false;
  }

  EngageDolbyVisionAtStartup();

  InvalidateRenderTarget(); // Replacement surface is now bound; callbacks do not establish readiness.
  ResetVSync(); // A newly bound EGL surface needs its own interval initialization.
  m_vsyncFailureReported = false;

  if (!m_delayDispReset)
  {
    std::unique_lock<CCriticalSection> lock(m_resourceSection);
    // tell any shared resources
    for (std::vector<IDispResource *>::iterator i = m_resources.begin(); i != m_resources.end(); ++i)
      (*i)->OnResetDisplay();
  }

  display.Finish(CAMLDisplayLifecycle::Phase::WAITING_FOR_RESET);
  return true;
}

bool CWinSystemAmlogicGLESContext::DestroyWindow()
{
  GuiABFinish("destroy_window");
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  if (!display)
    return false;
  m_displayGeometryReady = false;
  InvalidateRenderTarget();
  CancelGuiComposite();
  SetNativeGuiWait(false);
  m_pGLContext.DestroySurface();
  return CWinSystemAmlogic::DestroyWindow();
}

bool CWinSystemAmlogicGLESContext::ResizeWindow(int newWidth, int newHeight, int newLeft, int newTop)
{
  return ResetRenderSystem(newWidth, newHeight);
}

bool CWinSystemAmlogicGLESContext::SetFullScreen(bool fullScreen, RESOLUTION_INFO& res, bool blankOtherDisplays)
{
  GuiABFinish("fullscreen_change");
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  if (!display || !CreateNewWindow("", fullScreen, res) ||
      !ResetRenderSystem(res.iWidth, res.iHeight))
    return false;
  display.Finish(m_delayDispReset ? CAMLDisplayLifecycle::Phase::WAITING_FOR_RESET
                                 : CAMLDisplayLifecycle::Phase::READY);
  return true;
}

bool CWinSystemAmlogicGLESContext::ResetRenderSystem(int width, int height)
{
  GuiABFinish("reset_render_system");
  if (m_shutdownRequested)
    return false;
  CAMLDisplayLifecycle::Mutation display(m_displayLifecycle);
  if (!display)
    return false;
  m_displayGeometryReady = CRenderSystemGLES::ResetRenderSystem(width, height) && CanRender();
  ObserveEndDisplay("geometry-reset");
  if (!m_displayGeometryReady)
    return false;
  display.Finish(m_delayDispReset ? CAMLDisplayLifecycle::Phase::WAITING_FOR_RESET
                                 : CAMLDisplayLifecycle::Phase::READY);
  return true;
}

CWinSystemBase::NativeGuiWaitResult CWinSystemAmlogicGLESContext::SetNativeGuiWait(bool enabled)
{
  const bool previous = m_nativeGuiWait.Enabled();
  // Surface replacement/shutdown discards the lease. Main re-evaluates actual
  // session ownership before publication on the replacement surface.
  const bool deferred = enabled &&
                        (m_shutdownRequested || m_pGLContext.GetEGLSurface() == EGL_NO_SURFACE);
  const bool applied = m_nativeGuiWait.Apply(enabled && !deferred, "/dev/" + m_framebuffer_name);
  const int error = m_nativeGuiWait.Error();
  if (previous != m_nativeGuiWait.Enabled() || error != m_nativeGuiWaitReportedError)
    CLog::Log(error ? LOGERROR : LOGINFO,
              "p3i-native-gui-wait requested={} enabled={} error={}",
              enabled, m_nativeGuiWait.Enabled(), error);
  m_nativeGuiWaitReportedError = error;
  if (!applied)
    return NativeGuiWaitResult::FAILED;
  return deferred ? NativeGuiWaitResult::DEFERRED : NativeGuiWaitResult::READY;
}

void CWinSystemAmlogicGLESContext::SetVSyncImpl(bool enable)
{
  if (!m_pGLContext.SetVSync(enable))
  {
    ResetVSync(); // The void GLES backend hook must not cache a rejected setup.
    if (!m_vsyncFailureReported)
      CLog::Log(LOGERROR, "{},Could not set egl vsync; retrying on main", __FUNCTION__);
    m_vsyncFailureReported = true;
  }
  else
    m_vsyncFailureReported = false;
}

void CWinSystemAmlogicGLESContext::ObserveEndDisplay(const char* event, bool milestone)
{
  PLAYBACK_DIAGNOSTICS::endDisplay.Generation(m_displayLifecycle.Serial());
  PLAYBACK_DIAGNOSTICS::endDisplay.Record(PLAYBACK_DIAGNOSTICS::NowUs(), event, [&] {
    const auto deadline = std::chrono::duration_cast<std::chrono::microseconds>(
        m_dispResetTimer.GetStartTime() + m_dispResetTimer.GetInitialTimeoutValue() -
        PLAYBACK_DIAGNOSTICS::origin).count();
    const auto target = CaptureRenderTarget();
    return fmt::format("display={} target={} target_gen={} ready={} geometry={} can_render={} pending={} delay={} deadline_us={} hdr={} cached_hdr={}",
        m_displayLifecycle.Serial(), static_cast<const void*>(target.identity.get()), target.generation, m_displayLifecycle.Ready(),
        m_displayGeometryReady, CanRender(), m_displayLifecycle.Pending(), m_delayDispReset,
        m_delayDispReset ? deadline : 0, static_cast<int>(GetGfxContext().GetHDRType()),
        static_cast<int>(m_hdrType));
  }, milestone);
}

void CWinSystemAmlogicGLESContext::PresentRenderImpl(bool rendered)
{
  PLAYBACK_DIAGNOSTICS::endDisplay.Generation(m_displayLifecycle.Serial());
  const auto target = CaptureRenderTarget();
  // GUI-path HDMI link watchdog: catches a sink dropping sync in the menus,
  // where neither the DV-transition dump nor the playback vsync-stall snapshot
  // is active. Self-throttled to ~1Hz and only logs on a state change.
  const auto link = aml_hdmi_link_probe("present");
  if (link == AMLHDMILinkSample::CHANGED ||
      (link != AMLHDMILinkSample::SKIPPED && m_hdrRefreshPending.load()))
    RefreshHDRCapabilities();

  if (m_delayDispReset && m_dispResetTimer.IsTimePast())
  {
    m_delayDispReset = false;
    ObserveEndDisplay("delay-expired", true);
    std::unique_lock<CCriticalSection> lock(m_resourceSection);
    // tell any shared resources
    for (std::vector<IDispResource *>::iterator i = m_resources.begin(); i != m_resources.end(); ++i)
      (*i)->OnResetDisplay();
    aml_hdr10plus_vsif_hold(false);
  }
  // OnResetDisplay is record/post-only under m_resourceSection. Binding and
  // geometry, plus this exact transaction's delayed reset, establish readiness.
  if (!m_delayDispReset && m_displayGeometryReady && CanRender())
    m_displayLifecycle.Resume();
  CEGLContextUtils::SwapDiagnostics swap;
  auto observe = [&] {
    if (!PLAYBACK_DIAGNOSTICS::Enabled())
      return;
    auto& trace = PLAYBACK_DIAGNOSTICS::endDisplay;
    const auto now = PLAYBACK_DIAGNOSTICS::NowUs();
    trace.SwapResult(now, swap.attempted, m_presentResult == PresentResult::SWAP_ACCEPTED, swap.error);
    if (trace.PresentChanged(now, rendered, static_cast<int>(m_presentResult),
                             m_displayLifecycle.Ready(), m_delayDispReset))
    {
      ObserveEndDisplay("present-state");
      trace.Record(now, "present", [&] {
        return fmt::format("rendered={} result={} swap_attempted={} egl_error={}", rendered,
            static_cast<int>(m_presentResult), swap.attempted, swap.error);
      });
    }
  };
  m_presentResult = PresentResult::SKIPPED;
  if (!rendered)
  {
    GuiABPresented(false);
    observe();
    return;
  }
  if (!IsRenderTargetCurrent(target))
  {
    m_presentResult = PresentResult::TARGET_INVALID;
    GuiABFinish("render_target_invalid");
    observe();
    return;
  }

  // eglSwapBuffers() sometimes fails during modeswaps on AML; there is nothing
  // to do about it. The frame just swapped is the first one in the new
  // encoding: switch the OSD's interpretation with it, not a frame early. A
  // failed swap presented nothing new, so the switches stay pending.
  // This AML caller has no existing eglGetError consumer. Other platforms and
  // frames outside the trace use the unchanged default error ownership.
  auto* diagnostics = PLAYBACK_DIAGNOSTICS::Enabled() &&
                              PLAYBACK_DIAGNOSTICS::endDisplay.Active(PLAYBACK_DIAGNOSTICS::NowUs())
                          ? &swap : nullptr;
  GuiABDraw(); // Opt-in only; retain the sole normal swap even if the trial aborts.
  if (m_pGLContext.TrySwapBuffers(diagnostics))
  {
    m_presentResult = PresentResult::SWAP_ACCEPTED;
    ApplyPendingKernelSwitch();
  }
  else
    m_presentResult = PresentResult::SWAP_FAILED;
  GuiABPresented(m_presentResult == PresentResult::SWAP_ACCEPTED);
  observe();
}

void CWinSystemAmlogicGLESContext::QueueKernelSwitch(const char* path, int value)
{
  // A route change queues the old switch off and the new one on in the same
  // frame: both wait for the swap. A second change of the same switch before
  // then supersedes the first.
  for (PendingSwitch& p : m_pendingSwitches)
  {
    if (p.path == path || !p.path)
    {
      p.path = path;
      p.value = value;
      return;
    }
  }
}

void CWinSystemAmlogicGLESContext::ApplyPendingKernelSwitch()
{
  for (PendingSwitch& p : m_pendingSwitches)
  {
    if (p.path)
      SetKernelSwitch(p.path, p.value);
    p.path = nullptr;
  }
}

void CWinSystemAmlogicGLESContext::RequestMenuComposite(bool menuShown)
{
  m_menuReported = true;
  if (menuShown)
  {
    m_menuShown = true;
    m_menuGoneSince = {};
  }
  else if (m_menuShown && m_menuGoneSince == std::chrono::steady_clock::time_point{})
  {
    m_menuGoneSince = std::chrono::steady_clock::now();
  }
}

// The GUI plane reaches the sink converted when the last decoder open set the
// PQ GUI transfer or engaged the DV core (whose graphics path converts it too),
// or when DV mode On keeps the GUI in DV before any decoder opens. None of
// these is a sysfs read.
bool CWinSystemAmlogicGLESContext::IsGuiOutputHdr()
{
  return aml_transfer_pq_at_open() || aml_dv_core_at_open() || aml_dv_mode() == DV_MODE_ON;
}

// The one place that decides when disc menu graphics take the raw PQ route.
// Today: only while PQ-authored menu graphics are on screen, and only when the
// output path already carries HDR (the per-primitive GUI scale is active) or
// the DV core owns the OSD. Everything else keeps the existing paths.
CWinSystemAmlogicGLESContext::MenuRoute CWinSystemAmlogicGLESContext::MenuCompositeRoute() const
{
  if (!m_menuShown || m_menuEngageFailed)
    return MenuRoute::NONE;

  if (GetGfxContext().GetStereoMode() != RENDER_STEREO_MODE_OFF ||
      CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_guiFrontToBackRendering)
    return MenuRoute::NONE;

  const RouteInputs& in = m_routeInputs;
  // With the DV core in bypass the OSD goes through the VPP: the HDR10 test.
  if (in.dvEnable && in.dvOutputMode != DOLBY_VISION_OUTPUT_MODE_BYPASS)
  {
    // The video processor modes (DV processed for a non-DV display) replace
    // core2's graphics curve with an SDR one: PQ graphics are unvalidated there.
    // SDR output from the DV core keeps its SDR graphics too.
    if (in.dvVideoProcessor != 0 || in.dvOutputMode > DOLBY_VISION_OUTPUT_MODE_HDR10)
      return MenuRoute::NONE;
    return in.dvSwitch ? MenuRoute::DV_CORE2 : MenuRoute::NONE;
  }

  // The DV core owned the output at the last decoder open but reads as off
  // or in bypass now: treated as a DV restart at a playlist change
  // (~350 ms), not an HDR10 session. The VPP route would engage, then flip
  // to the DV one.
  if (aml_dv_core_at_open())
    return MenuRoute::NONE;

  // HDR10 output. CRendererAML::Reset (a renderer flush or teardown) clears
  // the context's PQ transfer flag without the output changing; the decision
  // made at the last decoder open is what the output still carries.
  if (aml_transfer_pq_at_open())
    return in.osdSwitch ? MenuRoute::OSD_VPP : MenuRoute::NONE;

  return MenuRoute::NONE;
}

CGuiCompositeShaderGLES::GuiTransfer CWinSystemAmlogicGLESContext::MenuCompositeGuiTransfer(
    MenuRoute route) const
{
  // Decode the GUI the way the hardware decodes the SDR GUI on the same route,
  // so Kodi's own controls over a menu look as they do without one. The
  // per-primitive path scales GUI code values by the guipeakluminance factor
  // whenever the output is PQ; the hardware then decodes the scaled value.
  CGuiCompositeShaderGLES::GuiTransfer t;
  t.inputScale = aml_transfer_pq_at_open() ? std::clamp(GetGuiSdrPeakLuminance(), 0.0f, 1.0f) : 1.0f;

  if (route == MenuRoute::DV_CORE2)
  {
    // core2 decodes SDR graphics with BT.1886 (gamma 2.4) between
    // dolby_vision_graphic_min (1/10000 nit) and dolby_vision_graphic_max
    // nits; a max of 0 means its target table (amdolby_vision
    // dv_target_graphics_max / _LL_max).
    unsigned int nits = ReadUint(DV_GRAPHIC_MAX);
    if (nits == 0)
    {
      const StreamHdrType hdr = GetGfxContext().GetHDRType();
      const bool hdrSource =
          hdr == StreamHdrType::HDR_TYPE_HDR10 || hdr == StreamHdrType::HDR_TYPE_HDR10PLUS;
      const bool ll = ReadUint(DV_LL_POLICY) >= DOLBY_VISION_LL_YUV422 ||
                      (ReadUint(DV_FLAGS) & DV_FLAG_FORCE_DOVI_LL);
      if (m_routeInputs.dvOutputMode == DOLBY_VISION_OUTPUT_MODE_HDR10)
        nits = 316;
      else if (ll && hdrSource)
        nits = 210;
      else
        nits = 300;
    }
    t.gamma = 2.4f;
    t.white = nits / 10000.0f;
    t.blackLift = ReadUint(DV_GRAPHIC_MIN) / 10000.0f / nits;
    return t;
  }

  // The VPP's OSD SDR->HDR stage decodes with a pure 2.2 power (eo_y_lut_sdr)
  // and multiplies SDR white by oo_y_lut_sdr_hdr (16/512 by default, 312.5
  // nits).
  int gain = 16;
  CSysfsPath gainLut(VPP_SDR_HDR_GAIN);
  if (gainLut.Exists())
  {
    // comma-separated array: the gain at SDR white is the last entry
    const std::string lut = gainLut.Get<std::string>().value_or("");
    const size_t comma = lut.find_last_of(',');
    const int last = std::atoi(lut.c_str() + (comma == std::string::npos ? 0 : comma + 1));
    if (last > 0)
      gain = last;
  }
  t.gamma = 2.2f;
  t.white = gain / 512.0f;
  return t;
}

bool CWinSystemAmlogicGLESContext::EngageMenuComposite(MenuRoute route)
{
  if (!m_compositeShader)
  {
    std::string defines;
    if (UseLimitedColor())
      defines += "#define KODI_LIMITED_RANGE 1\n";
    m_compositeShader = std::make_unique<CGuiCompositeShaderGLES>(defines);
    if (!m_compositeShader->CompileAndLink())
    {
      CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext: failed to compile the menu composite shader");
      m_compositeShader.reset();
      return false;
    }
  }

  m_guiTransfer = MenuCompositeGuiTransfer(route);
  m_compositeShader->SetGuiTransfer(m_guiTransfer);
  if (!m_compositeShader->CreateLUTs(AVCOL_TRC_SMPTE2084))
  {
    CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext: failed to create the menu composite LUTs");
    return false;
  }

  // Both layers must exist before the output is switched: without them the
  // GUI and the menus would reach a PQ plane unconverted.
  if (!EnsureCompositeFbos())
  {
    m_guiFbo.Cleanup();
    m_guiFboWidth = m_guiFboHeight = 0;
    m_menuFbo.Cleanup();
    m_menuFboWidth = m_menuFboHeight = 0;
    return false;
  }

  QueueKernelSwitch(route == MenuRoute::DV_CORE2 ? DV_GRAPHIC_PQ : OSD_PQ_PASSTHROUGH, 1);
  InvalidateRenderTarget();
  m_menuRoute = route;
  m_menuFboHasContent = false;

  // The GUI FBO starts empty: redraw everything into it.
  MarkGuiDirty();

  CLog::Log(LOGINFO,
            "CWinSystemAmlogicGLESContext: disc menu graphics composite on ({}, GUI white {:.0f} "
            "nits, gamma {:.1f}, black {:.4f} nits, scale {:.2f})",
            route == MenuRoute::DV_CORE2 ? "DV core2" : "OSD passthrough",
            m_guiTransfer.white * 10000.0f, m_guiTransfer.gamma,
            m_guiTransfer.blackLift * m_guiTransfer.white * 10000.0f, m_guiTransfer.inputScale);
  return true;
}

void CWinSystemAmlogicGLESContext::DisengageMenuComposite()
{
  QueueKernelSwitch(m_menuRoute == MenuRoute::DV_CORE2 ? DV_GRAPHIC_PQ : OSD_PQ_PASSTHROUGH, 0);
  InvalidateRenderTarget();
  m_menuRoute = MenuRoute::NONE;
  m_guiFbo.Cleanup();
  m_guiFboWidth = m_guiFboHeight = 0;
  m_menuFbo.Cleanup();
  m_menuFboWidth = m_menuFboHeight = 0;
  m_menuFboHasContent = false;
  // The shader stays compiled for the next menu (a Mali compile is a hitch).

  // The back buffer held only the composite; the GUI draws straight to it again.
  MarkGuiDirty();
  CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext: disc menu graphics composite off");
}

bool CWinSystemAmlogicGLESContext::BeginRender()
{
  if (!CanRender())
    return CRenderSystemGLES::BeginRender();

  // Outside fullscreen video (a skin's video preview, a window over playback)
  // the GUI pass never reports: a frame without a report there is no menu.
  // In fullscreen a missing report is a renderer reconfiguring, which keeps
  // the state it had.
  if (!m_menuReported && !IsFullscreenVideoActive())
    RequestMenuComposite(false);
  m_menuReported = false;

  if (m_menuGoneSince != std::chrono::steady_clock::time_point{} &&
      std::chrono::steady_clock::now() - m_menuGoneSince > MENU_RELEASE_DELAY)
  {
    m_menuShown = false;
    m_menuGoneSince = {};
    m_menuEngageFailed = false;
  }

  const auto now = std::chrono::steady_clock::now();
  // Only while a menu is shown or engaged: the plain GUI reads nothing.
  if ((m_menuShown || m_menuRoute != MenuRoute::NONE) &&
      now - m_routeInputsRead > ROUTE_INPUTS_REFRESH)
  {
    m_routeInputs.dvEnable = aml_is_dv_enable();
    m_routeInputs.dvVideoProcessor = aml_dv_video_processor_mode();
    m_routeInputs.dvOutputMode = ReadUint(DV_OUTPUT_MODE);
    m_routeInputs.dvSwitch = KernelSwitchAvailable(DV_GRAPHIC_PQ);
    m_routeInputs.osdSwitch = KernelSwitchAvailable(OSD_PQ_PASSTHROUGH);
    m_routeInputsRead = now;

    // Follow the GUI decode while engaged (graphic max/min, guipeakluminance).
    if (m_menuRoute != MenuRoute::NONE && m_compositeShader)
    {
      const CGuiCompositeShaderGLES::GuiTransfer t = MenuCompositeGuiTransfer(m_menuRoute);
      if (!(t == m_guiTransfer))
      {
        m_compositeShader->SetGuiTransfer(t);
        if (m_compositeShader->CreateLUTs(AVCOL_TRC_SMPTE2084))
        {
          InvalidateRenderTarget();
          m_guiTransfer = t;
        }
      }
    }
  }

  const MenuRoute want = MenuCompositeRoute();
  if (want == m_menuRoute)
  {
    m_pendingRouteSince = {};
  }
  else if (m_menuRoute == MenuRoute::NONE && want == MenuRoute::OSD_VPP)
  {
    // First engage of the VPP route: only once it has held (see
    // OSD_ROUTE_FIRST_SETTLE).
    if (want != m_pendingRoute || m_pendingRouteSince == std::chrono::steady_clock::time_point{})
    {
      m_pendingRoute = want;
      m_pendingRouteSince = now;
    }
  }
  else if (m_menuRoute != MenuRoute::NONE && m_menuShown)
  {
    // Engaged with a menu still up: only a route that holds counts.
    if (want != m_pendingRoute || m_pendingRouteSince == std::chrono::steady_clock::time_point{})
    {
      m_pendingRoute = want;
      m_pendingRouteSince = now;
    }
  }

  const bool firstOsd = m_menuRoute == MenuRoute::NONE && want == MenuRoute::OSD_VPP;
  const bool settled = m_pendingRouteSince != std::chrono::steady_clock::time_point{} &&
                       now - m_pendingRouteSince >
                           (firstOsd ? OSD_ROUTE_FIRST_SETTLE : MENU_ROUTE_SETTLE);
  const bool immediate =
      (m_menuRoute == MenuRoute::NONE && !firstOsd) || !m_menuShown || m_menuEngageFailed;
  if (want != m_menuRoute && (immediate || settled))
  {
    m_pendingRouteSince = {};
    if (m_menuRoute != MenuRoute::NONE)
      DisengageMenuComposite();
    if (want != MenuRoute::NONE && !EngageMenuComposite(want))
      m_menuEngageFailed = true;
  }
  else if (m_menuRoute != MenuRoute::NONE && !EnsureCompositeFbos())
  {
    // A layer could not be recreated (resolution change): fall back before
    // this frame's shaders are chosen, and stay off until the menu is released.
    DisengageMenuComposite();
    m_menuEngageFailed = true;
  }

  // Shader regime (per-primitive PQ scale, limited range) follows
  // IsMenuCompositeActive() in CRenderSystemGLES::BeginRender.
  const bool begun = CRenderSystemGLES::BeginRender();
  if (begun)
    GuiABBegin();
  else
    GuiABFinish("begin_render_failed");
  return begun;
}

bool CWinSystemAmlogicGLESContext::EnsureFbo(CFrameBufferObject& fbo, int& width, int& height)
{
  if (fbo.IsValid() && fbo.IsBound() && width == m_nWidth && height == m_nHeight)
    return true;

  InvalidateRenderTarget();
  fbo.Cleanup();
  if (!fbo.Initialize() || !fbo.CreateAndBindToTexture(GL_TEXTURE_2D, m_nWidth, m_nHeight, GL_RGBA))
  {
    CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext: failed to create a {}x{} composite FBO",
              m_nWidth, m_nHeight);
    fbo.Cleanup();
    width = height = 0;
    return false;
  }
  width = m_nWidth;
  height = m_nHeight;
  return true;
}

bool CWinSystemAmlogicGLESContext::EnsureCompositeFbos()
{
  const bool freshGui = !m_guiFbo.IsValid() || m_guiFboWidth != m_nWidth || m_guiFboHeight != m_nHeight;
  if (!EnsureFbo(m_guiFbo, m_guiFboWidth, m_guiFboHeight) ||
      !EnsureFbo(m_menuFbo, m_menuFboWidth, m_menuFboHeight))
    return false;
  if (freshGui)
  {
    // A new GUI layer starts empty: clear it and redraw everything into it.
    m_guiFbo.BeginRender();
    glDisable(GL_SCISSOR_TEST);
    glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_SCISSOR_TEST);
    m_guiFbo.EndRender();
    MarkGuiDirty();
  }
  return true;
}

bool CWinSystemAmlogicGLESContext::BeginGuiComposite()
{
  m_guiFboBound = false;
  m_menuFboHasContent = false;
  if (!CanRender())
    return false;
  if (m_menuRoute == MenuRoute::NONE)
    return true;
  m_guiTarget = CaptureRenderTarget();
  m_guiResources = GetTextureResources();
  m_guiScissor = glIsEnabled(GL_SCISSOR_TEST);
  if (!m_guiFbo.BeginRender())
  {
    m_menuEngageFailed = true;
    return false;
  }
  m_guiFboBound = true;
  return true;
}

bool CWinSystemAmlogicGLESContext::BeginMenuOverlayRender()
{
  if (m_menuRoute == MenuRoute::NONE || !m_guiFboBound || !IsRenderTargetCurrent(m_guiTarget))
    return false;

  if (!m_menuFbo.BeginRender())
  {
    // This frame's menus are lost; fall back to the existing paths from the
    // next one.
    m_menuEngageFailed = true;
    m_guiFbo.BeginRender();
    return false;
  }

  m_menuTarget = CaptureRenderTarget();

  // The menu layer is cleared and redrawn whole every time, so the GUI's
  // dirty-region scissor must not clip it.
  m_menuScissor = glIsEnabled(GL_SCISSOR_TEST);
  glDisable(GL_SCISSOR_TEST);
  glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
  glClear(GL_COLOR_BUFFER_BIT);
  return true;
}

void CWinSystemAmlogicGLESContext::EndMenuOverlayRender()
{
  if (!IsRenderTargetCurrent(m_menuTarget))
  {
    CancelGuiComposite();
    return;
  }
  m_menuFboHasContent = true;
  if (m_menuScissor)
    glEnable(GL_SCISSOR_TEST);
  // Back to the GUI pass, which this ran inside of.
  m_guiFbo.BeginRender();
}

void CWinSystemAmlogicGLESContext::CancelGuiComposite()
{
  GuiABFinish("render_cancelled");
  // A route/target change cancels the draw, but the same context still owns
  // its bindings. Never restore those bindings in a replacement namespace.
  if (m_guiFboBound && IsTextureContextCurrent(m_guiResources))
  {
    m_guiFbo.EndRender();
    if (m_guiScissor)
      glEnable(GL_SCISSOR_TEST);
    else
      glDisable(GL_SCISSOR_TEST);
  }
  m_guiFboBound = false;
  m_menuFboHasContent = false;
}

bool CWinSystemAmlogicGLESContext::EndGuiComposite()
{
  if (!m_guiFboBound)
    return CanRender() && m_menuRoute == MenuRoute::NONE;
  if (!IsRenderTargetCurrent(m_guiTarget))
  {
    CancelGuiComposite();
    return false;
  }
  m_guiFbo.EndRender();
  m_guiFboBound = false;
  return CompositeGui();
}

bool CWinSystemAmlogicGLESContext::CompositeGui()
{
  if (!m_compositeShader)
    return false;

  // The back buffer carries only the composite while it is active.
  glViewport(0, 0, m_guiFboWidth, m_guiFboHeight);
  glDisable(GL_SCISSOR_TEST);
  glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
  glClear(GL_COLOR_BUFFER_BIT);

  glActiveTexture(GL_TEXTURE0);
  glBindTexture(GL_TEXTURE_2D, m_guiFbo.Texture());

  // The shader writes premultiplied colour and "over" alpha, as the GUI does
  // into the back buffer without the composite; the VPP blends it the same way.
  glEnable(GL_BLEND);
  glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

  const float w = static_cast<float>(m_guiFboWidth);
  const float h = static_cast<float>(m_guiFboHeight);
  GLfloat proj[16] = {2.0f / w, 0, 0, 0, 0, -2.0f / h, 0, 0, 0, 0, -1, 0, -1.0f, 1.0f, 0, 1};

  m_compositeShader->SetProjection(proj);
  m_compositeShader->SetHdrTexture(m_menuFboHasContent ? m_menuFbo.Texture() : 0);
  if (!m_compositeShader->Enable())
  {
    m_compositeShader->Disable();
    glEnable(GL_SCISSOR_TEST);
    return false;
  }

  const GLint posLoc = m_compositeShader->GetPosLoc();
  const GLint texLoc = m_compositeShader->GetTexLoc();
  GLfloat vert[4][2] = {{0, 0}, {w, 0}, {w, h}, {0, h}};
  GLfloat tex[4][2] = {{0, 1}, {1, 1}, {1, 0}, {0, 0}};
  GLubyte idx[4] = {0, 1, 3, 2};

  glVertexAttribPointer(posLoc, 2, GL_FLOAT, GL_FALSE, 0, vert);
  glVertexAttribPointer(texLoc, 2, GL_FLOAT, GL_FALSE, 0, tex);
  glEnableVertexAttribArray(posLoc);
  glEnableVertexAttribArray(texLoc);
  glDrawElements(GL_TRIANGLE_STRIP, 4, GL_UNSIGNED_BYTE, idx);
  glDisableVertexAttribArray(posLoc);
  glDisableVertexAttribArray(texLoc);

  m_compositeShader->Disable();
  glEnable(GL_SCISSOR_TEST);
  return true;
}

EGLDisplay CWinSystemAmlogicGLESContext::GetEGLDisplay() const
{
  return m_pGLContext.GetEGLDisplay();
}

EGLSurface CWinSystemAmlogicGLESContext::GetEGLSurface() const
{
  return m_pGLContext.GetEGLSurface();
}

EGLContext CWinSystemAmlogicGLESContext::GetEGLContext() const
{
  return m_pGLContext.GetEGLContext();
}

EGLConfig  CWinSystemAmlogicGLESContext::GetEGLConfig() const
{
  return m_pGLContext.GetEGLConfig();
}

std::unique_ptr<CVideoSync> CWinSystemAmlogicGLESContext::GetVideoSync(CVideoReferenceClock *clock)
{
  std::unique_ptr<CVideoSync> pVSync(new CVideoSyncAML(clock));
  return pVSync;
}

bool CWinSystemAmlogicGLESContext::SupportsStereo(RENDER_STEREO_MODE mode) const
{
  if (aml_display_support_3d() &&
      mode == RENDER_STEREO_MODE_HARDWAREBASED) {
    // yes, we support hardware based MVC decoding
    return true;
  }

  return CRenderSystemGLES::SupportsStereo(mode);
}

bool CWinSystemAmlogicGLESContext::GuiABIdentity(std::vector<std::string>& identity)
{
  if (m_shutdownRequested)
    return false;
  std::shared_ptr<CApplicationPowerHandling> power;
  try
  {
    power = CServiceBroker::GetAppComponents().GetComponent<CApplicationPowerHandling>();
  }
  catch (const std::logic_error&)
  {
    return false; // Application component teardown is not an eligible trial.
  }
  if (!power || !power->GetRenderGUI() || !IsDisplayReadyForVideo() ||
      !IsFullscreenVideoActive() || m_menuRoute != MenuRoute::NONE || m_menuShown ||
      m_guiFboBound || m_pendingSwitches[0].path || m_pendingSwitches[1].path ||
      GetGfxContext().GetStereoMode() != RENDER_STEREO_MODE_OFF)
    return false;
  const auto source = CAMLCodec::GetCaptureSource();
  if (!source.codec || !source.epoch)
    return false;
  const auto session = source.codec->GetDiagnostics();
  if (session.epoch != source.epoch || session.fenced || session.displayFenced ||
      session.nativeFenced || session.transferring || session.controlPending ||
      session.controlActive)
    return false;
  auto& cache = CServiceBroker::GetDataCacheCore();
  const double pts = cache.GetRenderPts();
  const auto metadata = cache.GetVideoDoViFrameMetadata(pts);
  if (!std::isfinite(pts) || pts < 0.0 || !metadata.has_level5_metadata || metadata.level5_detected)
    return false; // Missing exact metadata is not authored zero L5.
  const int sourceWidth = cache.GetVideoWidth(), sourceHeight = cache.GetVideoHeight();
  if (sourceWidth <= 0 || sourceHeight <= 0 ||
      metadata.level5_active_area_top_offset >= sourceHeight / 2 ||
      metadata.level5_active_area_bottom_offset >= sourceHeight / 2 ||
      metadata.level5_active_area_left_offset >= sourceWidth / 2 ||
      metadata.level5_active_area_right_offset >= sourceWidth / 2)
    return false; // Require source active picture to include its centre.
  identity = {std::to_string(session.id),
              std::to_string(session.epoch),
              std::to_string(sourceWidth),
              std::to_string(sourceHeight),
              std::to_string(metadata.level5_active_area_top_offset),
              std::to_string(metadata.level5_active_area_bottom_offset),
              std::to_string(metadata.level5_active_area_left_offset),
              std::to_string(metadata.level5_active_area_right_offset),
              std::to_string(UseLimitedColor()),
              std::to_string(GetGfxContext().IsTransferPQ())};
  const auto active = cache.GetVideoActivePicture();
  if (!active.valid)
    return false;
  RESOLUTION_INFO reference;
  reference.iWidth = 1920;
  reference.iHeight = 1080;
  float scaleX, scaleY;
  TransformMatrix matrix;
  GetGfxContext().GetGUIScaling(reference, scaleX, scaleY, &matrix);
  CRect picture = active.bounds;
  float z = 0.0f;
  matrix.TransformPosition(picture.x1, picture.y1, z);
  z = 0.0f;
  matrix.TransformPosition(picture.x2, picture.y2, z);
  if (!std::isfinite(scaleX) || scaleX <= 0.0f || !std::isfinite(scaleY) || scaleY <= 0.0f ||
      !std::isfinite(picture.x1) || !std::isfinite(picture.y1) || !std::isfinite(picture.x2) ||
      !std::isfinite(picture.y2) || picture.IsEmpty())
    return false;
  for (float value : {picture.x1, picture.y1, picture.x2, picture.y2})
    identity.push_back(std::to_string(value));
  m_guiAB.activePicture = picture; // Logical geometry only, never a hardware receipt.
  static constexpr const char* names[] = {"dolby_vision_mode",
                                          "dolby_vision_target_mode",
                                          "dolby_vision_status",
                                          "dolby_vision_mask",
                                          "dolby_vision_flags",
                                          "dolby_vision_ll_policy",
                                          "xbmc_dv_vp",
                                          "dolby_vision_xbmc_osd",
                                          "dolby_vision_policy",
                                          "dolby_vision_hdr10_policy",
                                          "dolby_vision_subtitles",
                                          "dolby_vision_enable",
                                          "xbmc_meta_level_5",
                                          "xbmc_meta_level_5_osdst",
                                          "xbmc_meta_level_5_subt",
                                          "dolby_vision_graphic_pq",
                                          "xbmc_force_l5_override",
                                          "xbmc_override_l5_top",
                                          "xbmc_override_l5_bottom",
                                          "xbmc_override_l5_left",
                                          "xbmc_override_l5_right",
                                          "xbmc_l5_override_additive"};
  uint64_t values[std::size(names)]{};
  CVariant raw(CVariant::VariantTypeObject);
  for (size_t i = 0; i < std::size(names); ++i)
  {
    std::string text;
    if (!GuiABRead(std::string("/sys/module/amdolby_vision/parameters/") + names[i], text))
      return false;
    raw[names[i]] = text;
    const bool boolean = (i >= 11 && i <= 16) || i == 21;
    if (boolean && (text == "Y" || text == "N"))
      values[i] = text == "Y";
    else if (!GuiABUint(text, values[i]))
      return false;
    if (boolean && values[i] > 1)
      return false;
    identity.push_back(std::to_string(i == 4 ? values[i] & ~uint64_t{0x80000000} : values[i]));
  }
  if (values[0] != 1 || values[1] != 1 || values[2] != 3 || (values[3] & 7) != 7 ||
      (values[4] & 0xc800) || values[4] > UINT32_MAX || values[5] || values[6] || values[7] ||
      values[10] || values[11] != 1)
    return false;
  std::string backend, provider, video;
  if (!GuiABRead("/sys/class/amdolby_vision/backend_state", backend) ||
      !GuiABRead("/sys/class/video/presentation_state", provider) ||
      !GuiABRead("/sys/class/amdolby_vision/dv_video_on", video) || video != "1")
    return false;
  const auto parsed = DVBackendState::Parse(backend);
  uint64_t enabled = 0, epoch = 0, receipt = 0;
  std::string enabledText, epochText, receiptText;
  std::istringstream stream(provider);
  if (!parsed || parsed->backend < 0 || parsed->newUsable != 1 ||
      !(stream >> enabledText >> epochText >> receiptText) || !GuiABUint(enabledText, enabled) ||
      !GuiABUint(epochText, epoch) || !GuiABUint(receiptText, receipt) || enabled != 1 || !epoch ||
      !(stream >> std::ws).eof())
    return false;
  identity.push_back(backend);
  identity.push_back(std::to_string(epoch)); // The applied receipt is progress only.
  for (const char* path :
       {"/sys/class/display/mode", "/sys/class/amhdmitx/amhdmitx0/attr",
        "/sys/class/graphics/fb0/free_scale", "/sys/class/graphics/fb0/window_axis"})
  {
    std::string text;
    if (!GuiABRead(path, text))
      return false;
    identity.push_back(text);
  }
  raw["provider_raw"] = provider;
  raw["render_pts"] = pts;
  raw["source_width"] = sourceWidth;
  raw["source_height"] = sourceHeight;
  raw["authored_l5_cpu_only"] =
      std::vector<std::string>{identity[4], identity[5], identity[6], identity[7]};
  const auto currentSource = CAMLCodec::GetCaptureSource();
  if (currentSource.codec != source.codec || currentSource.epoch != source.epoch)
    return false;
  const auto currentSession = source.codec->GetDiagnostics();
  if (currentSession.epoch != source.epoch || currentSession.fenced ||
      currentSession.displayFenced || currentSession.nativeFenced || currentSession.transferring ||
      currentSession.controlPending || currentSession.controlActive)
    return false;
  const int phase = m_guiAB.phase == GuiAB::Phase::B ? 1 : 0;
  m_guiAB.report[phase ? "B_guard_last" : "A_guard_last"] = raw;
  // These are sequential CPU/source observations, not an applied metadata receipt.
  return true;
}
bool CWinSystemAmlogicGLESContext::GuiABCurrent()
{
  std::vector<std::string> identity;
  auto* gui = CServiceBroker::GetGUI();
  auto* home = gui ? gui->GetWindowManager().GetWindow(WINDOW_HOME) : nullptr;
  const bool valid = home && home->GetProperty("enky.gui_ab").asString() == "running" &&
                     IsRenderTargetCurrent(m_guiAB.target) &&
                     m_displayLifecycle.Serial() == m_guiAB.display && GuiABIdentity(identity) &&
                     identity == m_guiAB.identity &&
                     home->GetProperty("enky.gui_ab").asString() == "running" &&
                     IsRenderTargetCurrent(m_guiAB.target) &&
                     m_displayLifecycle.Serial() == m_guiAB.display && IsDisplayReadyForVideo() &&
                     !m_shutdownRequested && IsFullscreenVideoActive();
  if (!valid)
    m_guiAB.report["rejected_identity"] = identity;
  return valid;
}

void CWinSystemAmlogicGLESContext::GuiABFinish(const char* reason)
{
  try
  {
    if (m_guiAB.phase != GuiAB::Phase::A && m_guiAB.phase != GuiAB::Phase::B)
      return;
    m_guiAB.report["reason"] = reason;
    m_guiAB.report["ended_us"] = PLAYBACK_DIAGNOSTICS::NowUs();
    m_guiAB.report["A_accepted"] = m_guiAB.accepted[0];
    m_guiAB.report["B_accepted"] = m_guiAB.accepted[1];
    m_guiAB.report["gl_error"] = m_guiAB.glError;
    m_guiAB.report["completed"] = std::strcmp(reason, "completed") == 0;
    m_guiAB.restore = m_guiAB.touched ? 3 : 0;
    m_guiAB.report["software_restoration_pending"] = m_guiAB.restore != 0;
    m_guiAB.phase = m_guiAB.restore ? GuiAB::Phase::RESTORE : GuiAB::Phase::IDLE;
    m_guiAB.ticket = false;
    m_guiAB.publish = true;
    if (m_guiAB.pixelPhase < 0)
      std::vector<uint8_t>().swap(m_guiAB.pixels);
  }
  catch (const std::bad_alloc&)
  {
    GuiABMemoryFailure();
  }
}
bool CWinSystemAmlogicGLESContext::MessagePump()
{
  const bool handled = CWinSystemAmlogic::MessagePump();
  try
  {
    if (m_guiAB.memoryFailure)
    {
      CLog::Log(LOGERROR, "p3i-gui-ab id={} allocation_failed; result unavailable",
                m_guiAB.started);
      m_guiAB.memoryFailure = false;
    }
    auto* gui = CServiceBroker::GetGUI();
    auto* home = gui ? gui->GetWindowManager().GetWindow(WINDOW_HOME) : nullptr;
    if (!home)
      return handled;
    const std::string request = home->GetProperty("enky.gui_ab").asString();
    if (request == "start" && m_guiAB.phase == GuiAB::Phase::IDLE)
    {
      m_guiAB = {};
      m_guiAB.phase = GuiAB::Phase::A;
      m_guiAB.started = PLAYBACK_DIAGNOSTICS::NowUs();
      m_guiAB.report["started_us"] = m_guiAB.started;
      m_guiAB.report["boundary"] = "current_render_candidate_only";
      m_guiAB.report["status"] = "INCONCLUSIVE_A_APPEARANCE_UNKNOWN";
      home->SetProperty("enky.gui_ab", "running");
      home->SetProperty("enky.gui_ab.a", "");
      home->SetProperty("enky.gui_ab.b", "");
      m_guiAB.target = CaptureRenderTarget();
      m_guiAB.display = m_displayLifecycle.Serial();
      if (!GuiABIdentity(m_guiAB.identity))
        GuiABFinish("NO_TEST_identity_unavailable_or_ineligible");
      else
      {
        m_guiAB.report["identity"] = m_guiAB.identity;
        m_guiAB.publish = true;
      }
    }
    else if (m_guiAB.phase == GuiAB::Phase::A || m_guiAB.phase == GuiAB::Phase::B)
    {
      if (request != "running")
        GuiABFinish("cancelled");
      else if (PLAYBACK_DIAGNOSTICS::NowUs() - m_guiAB.started >= 10000000)
        GuiABFinish("expired");
      else if (m_guiAB.ticket)
        GuiABFinish("incomplete_render_attempt");
      else if (!GuiABCurrent())
        GuiABFinish("identity_changed_or_unavailable");
    }
    if (!m_guiAB.report.isNull())
    {
      for (const char* name : {"a", "b"})
      {
        const std::string observation =
            home->GetProperty(std::string("enky.gui_ab.") + name).asString();
        const std::string normalized =
            observation == "bar_cue" || observation == "clear_cue" || observation == "no_cue"
                ? observation
                : "unknown";
        if (m_guiAB.report[name].asString() != normalized)
        {
          m_guiAB.report[name] = normalized;
          m_guiAB.publish = true;
        }
      }
      const std::string a = m_guiAB.report["a"].asString();
      if (a != "unknown" && a != "bar_cue")
        GuiABFinish("A_validity_control_failed");
    }
    GuiABDrain(); // Called by DriveRenderLoop before Render, outside GL/native callbacks.
    if (m_guiAB.phase == GuiAB::Phase::IDLE || m_guiAB.phase == GuiAB::Phase::RESTORE)
      home->SetProperty("enky.gui_ab", "");
    return handled;
  }
  catch (const std::bad_alloc&)
  {
    GuiABMemoryFailure();
    return handled;
  }
}

void CWinSystemAmlogicGLESContext::GuiABBegin()
{
  try
  {
    if (m_guiAB.phase == GuiAB::Phase::IDLE)
      return;
    if (m_guiAB.phase == GuiAB::Phase::RESTORE)
    {
      // Keep pending restoration explicit if the ordinary swap path fails. Do
      // not turn a broken display into an unbounded diagnostic redraw loop.
      if (m_guiAB.restoreAttempts < 6)
      {
        ++m_guiAB.restoreAttempts;
        MarkGuiDirty();
        m_guiAB.ticket = true;
      }
      else if (!m_guiAB.report["software_restore_attempts_exhausted"].asBoolean())
      {
        m_guiAB.report["software_restore_attempts_exhausted"] = true;
        m_guiAB.publish = true;
      }
      return;
    }
    if (m_guiAB.ticket || !GuiABCurrent())
    {
      GuiABFinish("incomplete_attempt_or_identity_changed");
      return;
    }
    if (PLAYBACK_DIAGNOSTICS::NowUs() - m_guiAB.started >= 10000000)
    {
      GuiABFinish("expired_before_render");
      return;
    }
    MarkGuiDirty(); // Before GUIWindowManager::Render, never fake hasRendered.
    m_guiAB.ticket = true;
  }
  catch (const std::bad_alloc&)
  {
    GuiABMemoryFailure();
  }
}
bool CWinSystemAmlogicGLESContext::GuiABDraw()
{
  try
  {
    if (m_guiAB.phase != GuiAB::Phase::A && m_guiAB.phase != GuiAB::Phase::B)
      return true;
    if (!m_guiAB.ticket || !GuiABCurrent() || !IsPrimaryContextCurrent() ||
        eglGetCurrentSurface(EGL_READ) != m_pGLContext.GetEGLSurface())
    {
      GuiABFinish("read_target_changed");
      return false;
    }
    auto fail = [&](const char* reason)
    {
      GuiABFinish(reason);
      return false;
    };
    m_guiAB.glError = glGetError();
    if (m_guiAB.glError != GL_NO_ERROR)
      return fail("preexisting_GL_error_consumed_and_recorded");
    const auto* version = reinterpret_cast<const char*>(glGetString(GL_VERSION));
    const auto* extensions = reinterpret_cast<const char*>(glGetString(GL_EXTENSIONS));
    if (!version || std::strncmp(version, "OpenGL ES 2.", 12) != 0 || !extensions ||
        std::strstr(extensions, "pixel_buffer_object") || std::strstr(extensions, "_pack_subimage") ||
        std::strstr(extensions, "pack_reverse_row_order") ||
        std::strstr(extensions, "pack_invert"))
      return fail("NO_TEST_unsupported_context_or_pack_extensions");
    GLint framebuffer = -1, alpha = 0, red = 0, green = 0, blue = 0, pack = 0;
    GLint viewport[4]{};
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &framebuffer);
    glGetIntegerv(GL_ALPHA_BITS, &alpha);
    glGetIntegerv(GL_RED_BITS, &red);
    glGetIntegerv(GL_GREEN_BITS, &green);
    glGetIntegerv(GL_BLUE_BITS, &blue);
    glGetIntegerv(GL_PACK_ALIGNMENT, &pack);
    glGetIntegerv(GL_VIEWPORT, viewport);
    EGLint width = 0, height = 0;
    const bool dimensions = eglQuerySurface(GetEGLDisplay(), GetEGLSurface(), EGL_WIDTH, &width) &&
                            eglQuerySurface(GetEGLDisplay(), GetEGLSurface(), EGL_HEIGHT, &height);
    m_guiAB.glError = glGetError();
    if (!dimensions || m_guiAB.glError || framebuffer || alpha != 8 || red != 8 || green != 8 ||
        blue != 8 || (pack != 1 && pack != 2 && pack != 4 && pack != 8) || width < 128 ||
        height < 128 || width > 4096 || height > 4096 || viewport[0] || viewport[1] ||
        viewport[2] != width || viewport[3] != height)
      return fail("NO_TEST_default_target_format_dimensions_or_GL_error");
    // Both cue boxes must fit in the published active picture, transformed back
    // from its fixed GUI coordinates. Physical cue visibility remains mandatory.
    const auto& picture = m_guiAB.activePicture;
    if (picture.x1 > width / 2 - 36 || picture.x2 < width / 2 + 36 ||
        picture.y1 > height - (height / 2 + 8) || picture.y2 < height - (height / 2 - 8))
      return fail("NO_TEST_cue_outside_active_picture");
    const size_t stride = (static_cast<size_t>(width) * 4 + pack - 1) / pack * pack;
    if (stride > GuiAB::MAX_BYTES / static_cast<size_t>(height))
      return fail("NO_TEST_readback_cap");
    if (m_guiAB.width && (width != m_guiAB.width || height != m_guiAB.height ||
                          stride != static_cast<size_t>(m_guiAB.stride)))
      return fail("read_layout_changed");
    m_guiAB.width = width;
    m_guiAB.height = height;
    m_guiAB.stride = static_cast<int>(stride);
    const int phase = m_guiAB.phase == GuiAB::Phase::B ? 1 : 0;
    if (!m_guiAB.read[phase])
    {
      try
      {
        m_guiAB.pixels.resize(stride * height);
      }
      catch (const std::exception&)
      {
        return fail("readback_allocation_failed");
      }
    }
    GLfloat clear[4]{};
    GLboolean mask[4]{};
    GLint scissor[4]{};
    const GLboolean enabled = glIsEnabled(GL_SCISSOR_TEST);
    glGetFloatv(GL_COLOR_CLEAR_VALUE, clear);
    glGetBooleanv(GL_COLOR_WRITEMASK, mask);
    glGetIntegerv(GL_SCISSOR_BOX, scissor);
    m_guiAB.glError = glGetError();
    if (m_guiAB.glError)
      return fail("GL_state_query_failed");
    m_guiAB.touched = true;
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    if (phase)
    {
      glDisable(GL_SCISSOR_TEST);
      glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
      glClear(GL_COLOR_BUFFER_BIT);
    }
    glEnable(GL_SCISSOR_TEST);
    for (int block = 0; block <= phase; ++block)
    {
      const int x = width / 2 - 36 + block * 40;
      const int y = height / 2 - 8;
      glScissor(x, y, 32, 16);
      glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
      glClear(GL_COLOR_BUFFER_BIT);
      glScissor(x, y, 16, 16);
      glClearColor(1.0f, 1.0f, 1.0f, 1.0f);
      glClear(GL_COLOR_BUFFER_BIT);
    }
    glClearColor(clear[0], clear[1], clear[2], clear[3]);
    glColorMask(mask[0], mask[1], mask[2], mask[3]);
    glScissor(scissor[0], scissor[1], scissor[2], scissor[3]);
    if (!enabled)
      glDisable(GL_SCISSOR_TEST);
    m_guiAB.glError = glGetError();
    if (m_guiAB.glError)
      return fail("GL_draw_or_restoration_failed");
    if (!m_guiAB.read[phase])
    {
      m_guiAB.readEnter = PLAYBACK_DIAGNOSTICS::NowUs();
      CLog::Log(LOGINFO, "p3i-gui-ab id={} phase={} read_enter_us={} bytes={}", m_guiAB.started,
                phase ? "B" : "A", m_guiAB.readEnter, m_guiAB.pixels.size());
      glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, m_guiAB.pixels.data());
      m_guiAB.readReturn = PLAYBACK_DIAGNOSTICS::NowUs();
      m_guiAB.glError = glGetError();
      m_guiAB.pixelPhase = phase;
      if (m_guiAB.glError)
        return fail("GL_read_failed");
      m_guiAB.read[phase] = true;
    }
    if (!GuiABCurrent() || !IsPrimaryContextCurrent() ||
        eglGetCurrentSurface(EGL_READ) != GetEGLSurface())
      return fail("identity_changed_after_draw_or_read");
    if (PLAYBACK_DIAGNOSTICS::NowUs() - m_guiAB.started >= 10000000)
      return fail("expired_after_draw_or_read");
    return true;
  }
  catch (const std::bad_alloc&)
  {
    GuiABMemoryFailure();
    return false;
  }
}

void CWinSystemAmlogicGLESContext::GuiABPresented(bool accepted)
{
  try
  {
    if (m_guiAB.phase == GuiAB::Phase::IDLE)
      return;
    if (m_guiAB.phase == GuiAB::Phase::RESTORE)
    {
      if (m_guiAB.ticket && accepted)
      {
        m_guiAB.ticket = false;
        if (--m_guiAB.restore == 0)
        {
          m_guiAB.phase = GuiAB::Phase::IDLE;
          m_guiAB.report["software_redraws_accepted"] = 3;
          m_guiAB.report["software_restoration_pending"] = false;
          m_guiAB.publish = true;
        }
      }
      else
        m_guiAB.ticket = false;
      if (m_guiAB.restoreAttempts == 6 && m_guiAB.restore &&
          !m_guiAB.report["software_restore_attempts_exhausted"].asBoolean())
      {
        m_guiAB.report["software_restoration_pending"] = true;
        m_guiAB.report["software_restore_attempts_exhausted"] = true;
        m_guiAB.publish = true;
        // Pending full-invalidation intent remains; recover the normal display
        // before running another trial. No subsequent diagnostic dirty marks.
      }
      return;
    }
    if (!m_guiAB.ticket || !accepted)
    {
      GuiABFinish("skipped_or_failed_swap");
      return;
    }
    if (!GuiABCurrent())
    {
      GuiABFinish("identity_or_request_changed_during_swap");
      return;
    }
    if (PLAYBACK_DIAGNOSTICS::NowUs() - m_guiAB.started >= 10000000)
    {
      GuiABFinish("expired_after_swap");
      return;
    }
    m_guiAB.ticket = false;
    const int phase = m_guiAB.phase == GuiAB::Phase::B ? 1 : 0;
    const auto now = PLAYBACK_DIAGNOSTICS::NowUs();
    if (!m_guiAB.accepted[phase])
      m_guiAB.report[phase ? "B_first_accepted_us" : "A_first_accepted_us"] = now;
    m_guiAB.report[phase ? "B_last_accepted_us" : "A_last_accepted_us"] = now;
    if (++m_guiAB.accepted[phase] == GuiAB::BUDGET)
    {
      if (phase)
        GuiABFinish("completed");
      else
        m_guiAB.phase = GuiAB::Phase::B;
    }
  }
  catch (const std::bad_alloc&)
  {
    GuiABMemoryFailure();
  }
}

void CWinSystemAmlogicGLESContext::GuiABDrain()
{
  if (m_guiAB.pixelPhase >= 0)
  {
    const int phase = m_guiAB.pixelPhase;
    CVariant sample(CVariant::VariantTypeObject), rows(CVariant::VariantTypeArray);
    uint32_t hash = 2166136261U;
    bool cueValid = true, zeroOutsideCue = true;
    const int w = m_guiAB.width, h = m_guiAB.height;
    std::string top, cue;
    static constexpr char hex[] = "0123456789abcdef";
    const int topRows = std::min(32, 65536 / (w * 4));
    for (int y = 0; y < h; ++y)
    {
      uint64_t coverage = 0, alphaSum = 0, rgbSum = 0;
      for (int x = 0; x < w; ++x)
      {
        const auto* pixel = &m_guiAB.pixels[static_cast<size_t>(y) * m_guiAB.stride + x * 4];
        int block = -1;
        for (int b = 0; b <= phase; ++b)
          if (y >= h / 2 - 8 && y < h / 2 + 8 && x >= w / 2 - 36 + b * 40 && x < w / 2 - 4 + b * 40)
            block = b;
        if (block >= 0)
        {
          const int colour = x < w / 2 - 20 + block * 40 ? 255 : 0;
          cueValid &=
              pixel[0] == colour && pixel[1] == colour && pixel[2] == colour && pixel[3] == 255;
        }
        else
          zeroOutsideCue &= !pixel[0] && !pixel[1] && !pixel[2] && !pixel[3];
        if (block < 0)
        {
          coverage += pixel[3] != 0;
          alphaSum += pixel[3];
          rgbSum += static_cast<uint64_t>(pixel[0]) + pixel[1] + pixel[2];
        }
        for (int c = 0; c < 4; ++c)
        {
          hash = (hash ^ pixel[c]) * 16777619U;
          if (y >= h - topRows || block >= 0)
          {
            std::string& destination = block >= 0 ? cue : top;
            destination += hex[pixel[c] >> 4];
            destination += hex[pixel[c] & 15];
          }
        }
      }
      CVariant row(CVariant::VariantTypeArray);
      row.push_back(coverage);
      row.push_back(alphaSum);
      row.push_back(rgbSum);
      rows.push_back(std::move(row));
    }
    sample["width"] = w;
    sample["height"] = h;
    sample["stride"] = m_guiAB.stride;
    sample["row_origin"] = "GL_bottom_left; top band starts at height-top_rows";
    sample["rows_excluding_cue_coverage_alphaSum_rgbSum"] = std::move(rows);
    sample["top_rows"] = topRows;
    sample["top_rgba_hex"] = std::move(top);
    sample["cue_rgba_hex"] = std::move(cue);
    sample["fnv1a32_rgba"] = hash;
    sample["cue_pixels_valid"] = cueValid;
    sample["zero_rgba_outside_cue"] = zeroOutsideCue;
    sample["read_enter_us"] = m_guiAB.readEnter;
    sample["read_return_us"] = m_guiAB.readReturn;
    sample["read_valid"] = m_guiAB.read[phase];
    m_guiAB.report[phase ? "B" : "A"] = std::move(sample);
    m_guiAB.pixelPhase = -1;
    std::vector<uint8_t>().swap(m_guiAB.pixels);
    m_guiAB.publish = true;
    if (!cueValid || (phase && !zeroOutsideCue))
      GuiABFinish("readback_cue_or_transparent_substitute_invalid");
  }
  if (!m_guiAB.publish)
    return;
  const bool interpretable = m_guiAB.report["completed"].asBoolean() &&
                             m_guiAB.report["a"].asString() == "bar_cue" &&
                             (m_guiAB.report["b"].asString() == "bar_cue" ||
                              m_guiAB.report["b"].asString() == "clear_cue") &&
                             m_guiAB.report["A"]["read_valid"].asBoolean() &&
                             m_guiAB.report["B"]["read_valid"].asBoolean() &&
                             m_guiAB.report["A"]["cue_pixels_valid"].asBoolean() &&
                             m_guiAB.report["B"]["cue_pixels_valid"].asBoolean() &&
                             m_guiAB.report["B"]["zero_rgba_outside_cue"].asBoolean();
  m_guiAB.report["b_interpretable"] = interpretable;
  m_guiAB.report["status"] =
      interpretable ? "READY_FOR_COORDINATOR_INTERPRETATION" : "INCONCLUSIVE";
  m_guiAB.report["physical_page_or_completion_observed"] = false;
  std::string json;
  XFILE::CFile file;
  const bool saved = CJSONVariantWriter::Write(m_guiAB.report, json, false) &&
                     file.OpenForWrite("special://temp/enky-gui-ab.json", true) &&
                     file.Write(json.data(), json.size()) == static_cast<ssize_t>(json.size());
  CLog::Log(saved ? LOGINFO : LOGERROR,
            "p3i-gui-ab id={} A={} B={} reason={} GL_error={} result_saved={} status={}",
            m_guiAB.started, m_guiAB.accepted[0], m_guiAB.accepted[1],
            m_guiAB.report["reason"].asString(), m_guiAB.glError, saved,
            m_guiAB.report["status"].asString());
  m_guiAB.publish = false; // One write attempt per result/observation; no retry loop.
}

void CWinSystemAmlogicGLESContext::GuiABMemoryFailure() noexcept
{
  m_guiAB.memoryFailure = true;
  m_guiAB.restore = m_guiAB.touched ? 3 : 0;
  m_guiAB.phase = m_guiAB.restore ? GuiAB::Phase::RESTORE : GuiAB::Phase::IDLE;
  m_guiAB.ticket = m_guiAB.publish = false;
  m_guiAB.pixelPhase = -1;
  m_guiAB.report = CVariant{};
  std::vector<uint8_t>().swap(m_guiAB.pixels);
}
