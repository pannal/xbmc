/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "VideoSyncAML.h"
#include "WinSystemAmlogicGLESContext.h"
#include "platform/linux/SysfsPath.h"
#include "utils/AMLUtils.h"
#include "utils/log.h"
#include "threads/SingleLock.h"
#include "windowing/GraphicContext.h"
#include "windowing/WindowSystemFactory.h"
#include "settings/SettingsComponent.h"
#include "ServiceBroker.h"
#include "settings/Settings.h"
#include "windowing/WinSystem.h"

using namespace KODI;
using namespace KODI::WINDOWING::AML;

static std::shared_ptr<CSettings> settings()
{
  return CServiceBroker::GetSettingsComponent()->GetSettings();
}

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
  const auto bypass_dv_mode_switch_gui = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_DV_BYPASS);

  CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext::{}: "
    "m_bWindowCreated: {}, "
    "frac rate {:d}({:d}), "
    "m_bypassDVModeSwitchGUI: {}",
    __FUNCTION__,
    m_bWindowCreated,
    fractional_rate, cur_fractional_rate,
    bypass_dv_mode_switch_gui);

  bool force_mode_switch_by_dv = !bypass_dv_mode_switch_gui &&
                               ((hdrType != m_hdrType) &&
                                ((hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION) ||
                                 (m_hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)) &&
                                (aml_dv_mode() != DV_MODE_OFF));

  CLog::Log(LOGINFO, "CWinSystemAmlogicGLESContext::{}: "
    "m_bWindowCreated: {}, "
    "frac rate {:d}({:d}), "
    "force mode switch: {}, "
    "m_bypassDVModeSwitchGUI: {}",
    __FUNCTION__,
    m_bWindowCreated,
    fractional_rate, cur_fractional_rate,
    force_mode_switch_by_dv,
    bypass_dv_mode_switch_gui);

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
  // return.  The announcement handler's own mode check will then skip the
  // redundant call.  The playback-active flag prevents this from firing
  // during playback-start mode switches where aml_dv_open() has already set
  // the correct output mode.
  aml_dv_wait_for_pipeline();
  if (aml_dv_mode() == DV_MODE_ON && aml_is_dv_enable() && !aml_dv_playback_active())
  {
    unsigned int dvMode = aml_dv_dolby_vision_mode();
    if (dvMode != DOLBY_VISION_OUTPUT_MODE_IPT && dvMode != DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL)
    {
      aml_dv_start();
      // Re-write the display mode to trigger VPP reconfiguration for the
      // new DV output mode, matching what CWinSystemAmlogic::CreateNewWindow
      // does via aml_dv_display_trigger() in the full mode-switch path.
      aml_dv_display_trigger();
    }
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

  // Init the value set in the DV settings for ON DV_MODE_ON_DEMAND and the Kodi start
  if (aml_dv_mode() == DV_MODE_ON_DEMAND && aml_first_start_GUI && bypass_dv_mode_switch_gui)
  {
    // DOLBY_VISION_OUTPUT_MODE_IPT:        mode_string = "0-IPT";
    // DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL: mode_string = "1-IPT Tunnel";
    // DOLBY_VISION_OUTPUT_MODE_HDR10:      mode_string = "2-HDR10";
    // DOLBY_VISION_OUTPUT_MODE_SDR10:      mode_string = "3-SDR10";
    // DOLBY_VISION_OUTPUT_MODE_BYPASS:     mode_string = "5-Bypass";

    int configured_mode = CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV);

    CSysfsPath dolby_vision_mode{"/sys/module/amdolby_vision/parameters/dolby_vision_mode"};
    unsigned int existing_mode = dolby_vision_mode.Get<unsigned int>().value();
    
    if (existing_mode != (unsigned int)configured_mode)
    {
        aml_dv_on(configured_mode);    
        aml_first_start_GUI = false;
        
        CLog::Log(LOGDEBUG, "CWinSystemAmlogicGLESContext::{}: Set mode from settings: [{}] (existing_mode [{}])",
                  __FUNCTION__, configured_mode, existing_mode);
    }
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
