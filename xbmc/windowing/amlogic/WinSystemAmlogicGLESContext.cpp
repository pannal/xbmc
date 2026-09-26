/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "VideoSyncAML.h"
#include "WinSystemAmlogicGLESContext.h"
#include "ServiceBroker.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "guilib/WindowIDs.h"
#include "platform/linux/SysfsPath.h"
#include "settings/AdvancedSettings.h"
#include "settings/SettingsComponent.h"
#include "utils/AMLUtils.h"
#include "utils/log.h"
#include "threads/SingleLock.h"
#include "windowing/GraphicContext.h"
#include "windowing/WindowSystemFactory.h"

extern "C"
{
#include <libavutil/pixfmt.h>
}

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <string>

using namespace KODI;
using namespace KODI::WINDOWING::AML;

namespace
{
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

// Route inputs are sysfs reads: refresh them at most this often.
constexpr auto ROUTE_INPUTS_REFRESH = std::chrono::milliseconds(250);

// The first engage of the VPP route waits this long: while DV re-engages at a
// playlist change its enable state reads off for ~100 ms, which would look
// like HDR10 output.
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
// the DV enable state reads off for ~100 ms while DV re-engages at a playlist
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

bool CWinSystemAmlogicGLESContext::InitWindowSystem()
{
  if (!CWinSystemAmlogic::InitWindowSystem())
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

  return true;
}

bool CWinSystemAmlogicGLESContext::DestroyWindowSystem()
{
  if (m_menuRoute != MenuRoute::NONE)
    DisengageMenuComposite();
  ApplyPendingKernelSwitch(); // no more swaps to wait for

  m_pGLContext.DestroyContext();
  m_pGLContext.Destroy();
  return CWinSystemAmlogic::DestroyWindowSystem();
}

bool CWinSystemAmlogicGLESContext::CreateNewWindow(const std::string& name,
                                               bool fullScreen,
                                               RESOLUTION_INFO& res)
{
  RESOLUTION_INFO current_resolution;
  current_resolution.iWidth = current_resolution.iHeight = 0;
  RENDER_STEREO_MODE stereo_mode = CServiceBroker::GetWinSystem()->GetGfxContext().GetStereoMode();

  // check for frac_rate_policy change
  int fractional_rate = (res.fRefreshRate == floor(res.fRefreshRate)) ? 0 : 1;
  int cur_fractional_rate = fractional_rate;
  if (aml_has_frac_rate_policy())
  {
    CSysfsPath amhdmitx0_frac_rate_policy{"/sys/class/amhdmitx/amhdmitx0/frac_rate_policy"};
    cur_fractional_rate = amhdmitx0_frac_rate_policy.Get<int>().value();
  }

  // If changing in or out of Dolby Vision and it is on then make sure we do a mode swtich - TODO: combine with DV InfoFrame?
  StreamHdrType hdrType = CServiceBroker::GetWinSystem()->GetGfxContext().GetHDRType();
  bool force_mode_switch_by_dv = 
      ((hdrType != m_hdrType) &&
       ((hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION) || (m_hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)) &&
       (aml_dv_mode() != DV_MODE_OFF));

  // get current used resolution
  if (!aml_get_native_resolution(&current_resolution))
  {
    CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext::{}: failed to receive current resolution", __FUNCTION__);
    return false;
  }

  CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: "
    "m_bWindowCreated: {}, "
    "frac rate {:d}({:d}), "
    "hdrType: {}({}), force mode switch: {}",
    __FUNCTION__,
    m_bWindowCreated,
    fractional_rate, cur_fractional_rate,
    CStreamDetails::DynamicRangeToString(hdrType), CStreamDetails::DynamicRangeToString(m_hdrType), force_mode_switch_by_dv);
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
    // new DV output mode, matching what CWinSystemAmlogic::CreateNewWindow
    // does via aml_dv_display_trigger() in the full mode-switch path.
    aml_dv_display_trigger();
  }

  // check if mode switch is needed
  if (current_resolution.iWidth == res.iWidth && current_resolution.iHeight == res.iHeight &&
      current_resolution.iScreenWidth == res.iScreenWidth && current_resolution.iScreenHeight == res.iScreenHeight &&
      m_bFullScreen == fullScreen && current_resolution.fRefreshRate == res.fRefreshRate &&
      (current_resolution.dwFlags & D3DPRESENTFLAG_MODEMASK) == (res.dwFlags & D3DPRESENTFLAG_MODEMASK) &&
      m_stereo_mode == stereo_mode && m_bWindowCreated &&
      !force_mode_switch_by_dv &&
      (fractional_rate == cur_fractional_rate))
  {
    CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: No need to create a new window", __FUNCTION__);
    return true;
  }

  // destroy old window, then create a new one
  DestroyWindow();

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

  if (!CWinSystemAmlogic::CreateNewWindow(name, fullScreen, res))
  {
    return false;
  }

  // Wait for any in-progress DV pipeline restoration to complete before
  // creating the EGL surface. Prevents color corruption when the OnStop
  // handler is restoring IPT while we recreate the GL context.
  aml_dv_wait_for_pipeline();

  if (!m_pGLContext.CreateSurface(static_cast<EGLNativeWindowType>(m_nativeWindow)))
  {
    return false;
  }

  if (!m_pGLContext.BindContext())
  {
    return false;
  }

  if (!m_delayDispReset)
  {
    std::unique_lock<CCriticalSection> lock(m_resourceSection);
    // tell any shared resources
    for (std::vector<IDispResource *>::iterator i = m_resources.begin(); i != m_resources.end(); ++i)
      (*i)->OnResetDisplay();
  }

  return true;
}

bool CWinSystemAmlogicGLESContext::DestroyWindow()
{
  m_pGLContext.DestroySurface();
  return CWinSystemAmlogic::DestroyWindow();
}

bool CWinSystemAmlogicGLESContext::ResizeWindow(int newWidth, int newHeight, int newLeft, int newTop)
{
  CRenderSystemGLES::ResetRenderSystem(newWidth, newHeight);
  return true;
}

bool CWinSystemAmlogicGLESContext::SetFullScreen(bool fullScreen, RESOLUTION_INFO& res, bool blankOtherDisplays)
{
  CreateNewWindow("", fullScreen, res);
  CRenderSystemGLES::ResetRenderSystem(res.iWidth, res.iHeight);
  return true;
}

void CWinSystemAmlogicGLESContext::SetVSyncImpl(bool enable)
{
  if (!m_pGLContext.SetVSync(enable))
  {
    CLog::Log(LOGERROR, "{},Could not set egl vsync", __FUNCTION__);
  }
}

void CWinSystemAmlogicGLESContext::PresentRenderImpl(bool rendered)
{
  // GUI-path HDMI link watchdog: catches a sink dropping sync in the menus,
  // where neither the DV-transition dump nor the playback vsync-stall snapshot
  // is active. Self-throttled to ~1Hz and only logs on a state change.
  aml_hdmi_link_probe("present");

  if (m_delayDispReset && m_dispResetTimer.IsTimePast())
  {
    m_delayDispReset = false;
    std::unique_lock<CCriticalSection> lock(m_resourceSection);
    // tell any shared resources
    for (std::vector<IDispResource *>::iterator i = m_resources.begin(); i != m_resources.end(); ++i)
      (*i)->OnResetDisplay();
    aml_hdr10plus_vsif_hold(false);
  }
  if (!rendered)
    return;

  // Ignore errors - eglSwapBuffers() sometimes fails during modeswaps on AML,
  // there is probably nothing we can do about it
  m_pGLContext.TrySwapBuffers();

  // The frame just swapped is the first one in the new encoding: switch the
  // OSD's interpretation with it, not a frame early.
  ApplyPendingKernelSwitch();
}

void CWinSystemAmlogicGLESContext::QueueKernelSwitch(const char* path, int value)
{
  // Two changes before a swap (engage and release in one frame): the earlier
  // one is superseded only for the same switch.
  if (m_pendingSwitchPath && m_pendingSwitchPath != path)
    ApplyPendingKernelSwitch();
  m_pendingSwitchPath = path;
  m_pendingSwitchValue = value;
}

void CWinSystemAmlogicGLESContext::ApplyPendingKernelSwitch()
{
  if (!m_pendingSwitchPath)
    return;
  SetKernelSwitch(m_pendingSwitchPath, m_pendingSwitchValue);
  m_pendingSwitchPath = nullptr;
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
  if (in.dvEnable)
  {
    // The video processor modes (DV processed for a non-DV display) replace
    // core2's graphics curve with an SDR one: PQ graphics are unvalidated there.
    // SDR output from the DV core keeps its SDR graphics too.
    if (in.dvVideoProcessor != 0 || in.dvOutputMode > DOLBY_VISION_OUTPUT_MODE_HDR10)
      return MenuRoute::NONE;
    return in.dvSwitch ? MenuRoute::DV_CORE2 : MenuRoute::NONE;
  }

  // HDR10 output. The renderer clears the PQ transfer flag on a flush (a seek
  // or a menu jump) and sets it again only when a decoder opens: while the
  // stream it was set for plays on, the output is still PQ.
  const bool pqOutput =
      GetGfxContext().IsTransferPQ() ||
      (m_pqOutputHdrType != StreamHdrType::HDR_TYPE_NONE &&
       GetGfxContext().GetHDRType() == m_pqOutputHdrType);
  if (pqOutput)
    return in.osdSwitch ? MenuRoute::OSD_VPP : MenuRoute::NONE;

  return MenuRoute::NONE;
}

float CWinSystemAmlogicGLESContext::MenuCompositeGuiWhite(MenuRoute route) const
{
  // Match the GUI white the hardware gives the SDR GUI on the same route, so
  // Kodi's own controls over a menu look as they do without one.
  if (route == MenuRoute::DV_CORE2)
  {
    // core2 maps SDR graphics white to dolby_vision_graphic_max nits; 0 means
    // its target table (amdolby_vision dv_target_graphics_max / _LL_max).
    unsigned int nits = ReadUint(DV_GRAPHIC_MAX);
    if (nits == 0)
    {
      const bool dvSource = GetGfxContext().GetHDRType() ==
                            StreamHdrType::HDR_TYPE_DOLBYVISION;
      if (m_routeInputs.dvOutputMode == DOLBY_VISION_OUTPUT_MODE_HDR10)
        nits = 316;
      else if (ReadUint(DV_LL_POLICY) >= DOLBY_VISION_LL_YUV422 && !dvSource)
        nits = 210;
      else
        nits = 300;
    }
    // The per-primitive path scales GUI code values by the guipeakluminance
    // factor whenever it is active; core2 then decodes them.
    const float scale =
        GetGfxContext().IsTransferPQ() ? std::clamp(GetGuiSdrPeakLuminance(), 0.0f, 1.0f) : 1.0f;
    return nits / 10000.0f * std::pow(scale, 2.2f);
  }

  // The VPP's OSD SDR->HDR stage multiplies SDR white by oo_y_lut_sdr_hdr
  // (16/512 by default, 312.5 nits); the per-primitive path scales GUI code
  // values by the guipeakluminance factor before that.
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
  const float scale = std::clamp(GetGuiSdrPeakLuminance(), 0.0f, 1.0f);
  return gain / 512.0f * std::pow(scale, 2.2f);
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

  const float white = MenuCompositeGuiWhite(route);
  m_compositeShader->SetSdrPeak(white);
  if (!m_compositeShader->CreateLUTs(AVCOL_TRC_SMPTE2084))
  {
    CLog::Log(LOGERROR, "CWinSystemAmlogicGLESContext: failed to create the menu composite LUTs");
    m_compositeShader.reset();
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
    m_compositeShader.reset();
    return false;
  }

  QueueKernelSwitch(route == MenuRoute::DV_CORE2 ? DV_GRAPHIC_PQ : OSD_PQ_PASSTHROUGH, 1);
  m_menuRoute = route;
  m_menuFboHasContent = false;

  // The GUI FBO starts empty: redraw everything into it.
  MarkGuiDirty();

  CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext: disc menu graphics composite on ({}, GUI white {:.0f} nits)",
            route == MenuRoute::DV_CORE2 ? "DV core2" : "OSD passthrough", white * 10000.0f);
  return true;
}

void CWinSystemAmlogicGLESContext::DisengageMenuComposite()
{
  QueueKernelSwitch(m_menuRoute == MenuRoute::DV_CORE2 ? DV_GRAPHIC_PQ : OSD_PQ_PASSTHROUGH, 0);
  m_menuRoute = MenuRoute::NONE;
  m_guiFbo.Cleanup();
  m_guiFboWidth = m_guiFboHeight = 0;
  m_menuFbo.Cleanup();
  m_menuFboWidth = m_menuFboHeight = 0;
  m_menuFboHasContent = false;
  m_compositeShader.reset();

  // The back buffer held only the composite; the GUI draws straight to it again.
  MarkGuiDirty();
  CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext: disc menu graphics composite off");
}

bool CWinSystemAmlogicGLESContext::BeginRender()
{
  if (!m_bRenderCreated)
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
  if (now - m_routeInputsRead > ROUTE_INPUTS_REFRESH)
  {
    m_routeInputs.dvEnable = aml_is_dv_enable();
    m_routeInputs.dvVideoProcessor = aml_dv_video_processor_mode();
    m_routeInputs.dvOutputMode = ReadUint(DV_OUTPUT_MODE);
    m_routeInputs.dvSwitch = KernelSwitchAvailable(DV_GRAPHIC_PQ);
    m_routeInputs.osdSwitch = KernelSwitchAvailable(OSD_PQ_PASSTHROUGH);
    m_routeInputsRead = now;
  }
  if (!m_menuShown)
    m_pqOutputHdrType = StreamHdrType::HDR_TYPE_NONE;
  else if (GetGfxContext().IsTransferPQ())
    m_pqOutputHdrType = GetGfxContext().GetHDRType();

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
  const bool immediate = (m_menuRoute == MenuRoute::NONE && !firstOsd) || !m_menuShown;
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
  return CRenderSystemGLES::BeginRender();
}

bool CWinSystemAmlogicGLESContext::EnsureFbo(CFrameBufferObject& fbo, int& width, int& height)
{
  if (fbo.IsValid() && fbo.IsBound() && width == m_nWidth && height == m_nHeight)
    return true;

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

void CWinSystemAmlogicGLESContext::BeginGuiComposite()
{
  m_guiFboBound = false;
  // The menu layer is sampled only if this frame's GUI pass drew into it.
  m_menuFboHasContent = false;
  if (m_menuRoute == MenuRoute::NONE)
    return;

  // BeginRender made sure both layers exist.
  if (!m_guiFbo.BeginRender())
    return;
  m_guiFboBound = true;
}

bool CWinSystemAmlogicGLESContext::BeginMenuOverlayRender()
{
  if (m_menuRoute == MenuRoute::NONE || !m_guiFboBound)
    return false;

  if (!m_menuFbo.BeginRender())
  {
    m_guiFbo.BeginRender();
    return false;
  }

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
  m_menuFboHasContent = true;
  if (m_menuScissor)
    glEnable(GL_SCISSOR_TEST);
  // Back to the GUI pass, which this ran inside of.
  m_guiFbo.BeginRender();
}

void CWinSystemAmlogicGLESContext::EndGuiComposite()
{
  if (!m_guiFboBound)
    return;
  m_guiFbo.EndRender();
  m_guiFboBound = false;
  CompositeGui();
}

void CWinSystemAmlogicGLESContext::CompositeGui()
{
  if (!m_compositeShader)
    return;

  // The back buffer carries only the composite while it is active.
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
  m_compositeShader->Enable();

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
