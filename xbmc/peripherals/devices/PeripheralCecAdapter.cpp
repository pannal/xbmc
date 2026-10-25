/*
 *  Copyright (C) 2005-2024 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PeripheralCecAdapter.h"

#include "ServiceBroker.h"
#include "application/ApplicationActionListeners.h"
#include "application/ApplicationComponents.h"
#include "application/ApplicationEnums.h"
#include "application/ApplicationPlayer.h"
#include "application/ApplicationPowerHandling.h"
#include "application/ApplicationVolumeHandling.h"
#include "dialogs/GUIDialogKaiToast.h"
#include "guilib/GUIComponent.h"
#include "guilib/GUIWindowManager.h"
#include "guilib/LocalizeStrings.h"
#include "input/actions/Action.h"
#include "input/actions/ActionIDs.h"
#include "input/remote/IRRemote.h"
#include "messaging/ApplicationMessenger.h"
#include "pictures/GUIWindowSlideShow.h"
#include "utils/JobManager.h"
#include "utils/Variant.h"
#include "utils/log.h"
#include "xbmc/interfaces/AnnouncementManager.h"
#include "xbmc/interfaces/legacy/configini.h"

#include <algorithm>
#include <mutex>

#include <libcec/cec.h>

using namespace PERIPHERALS;
using namespace CEC;
using namespace ANNOUNCEMENT;
using namespace std::chrono_literals;
using namespace XBMCAddon;

#define CEC_LIB_SUPPORTED_VERSION LIBCEC_VERSION_TO_UINT(4, 0, 0)

// CE default cec_func_config (0x7f) without the GUI-controlled wake bits
// (AUTO_POWER_ON, STREAMPATH_POWER_ON, ACTIVE_SOURCE) — those come from the
// wakeup_cec_* settings instead
#define CEC_FUNC_CONFIG_BASE 0x27

/* time in seconds to ignore standby commands from devices after the screensaver has been activated
 */
#define SCREENSAVER_TIMEOUT 20
#define STANDBY_WAKE_COOLDOWN 5
#define ACTIVATE_SOURCE_COOLDOWN 5
#define VOLUME_CHANGE_TIMEOUT 250
#define VOLUME_REFRESH_TIMEOUT 100

#define LOCALISED_ID_TV 36037
#define LOCALISED_ID_AVR 36038
#define LOCALISED_ID_TV_AVR 36039
#define LOCALISED_ID_STOP 36044
#define LOCALISED_ID_PAUSE 36045
#define LOCALISED_ID_POWEROFF 13005
#define LOCALISED_ID_SUSPEND 13011
#define LOCALISED_ID_HIBERNATE 13010
#define LOCALISED_ID_QUIT 13009
#define LOCALISED_ID_IGNORE 36028
#define LOCALISED_ID_RECORDING_DEVICE 36051
#define LOCALISED_ID_PLAYBACK_DEVICE 36052
#define LOCALISED_ID_TUNER_DEVICE 36053

#define LOCALISED_ID_NONE 231

/* time in seconds to suppress source activation after receiving OnStop */
#define CEC_SUPPRESS_ACTIVATE_SOURCE_AFTER_ON_STOP 2

namespace
{
void DrainCecCallbacks(ICECAdapter* adapter)
{
  if (!adapter)
    return;
  // libCEC holds its callback mutex until an in-flight callback returns.
  // Call outside m_critSection: callbacks can acquire that Kodi lock.
#if CEC_LIB_VERSION_MAJOR >= 5
  adapter->DisableCallbacks();
#else
  adapter->EnableCallbacks(nullptr, nullptr);
#endif
}
}

CPeripheralCecAdapter::CPeripheralCecAdapter(CPeripherals& manager,
                                             const PeripheralScanResult& scanResult,
                                             CPeripheralBus* bus)
  : CPeripheralHID(manager, scanResult, bus), CThread("CECAdapter"), m_cecAdapter(NULL)
{
  ResetMembers();
  m_features.push_back(FEATURE_CEC);
  m_strComPort = scanResult.m_strLocation;

  auto& components = CServiceBroker::GetAppComponents();
  auto appListener = components.GetComponent<CApplicationActionListeners>();
  if (appListener)
    appListener->RegisterActionListener(this);
}

CPeripheralCecAdapter::~CPeripheralCecAdapter()
{
  Deinitialize();
}

void CPeripheralCecAdapter::Deinitialize(void)
{
  // Close admission before waiting for an already executing reopen.
  m_retired.store(true, std::memory_order_release);
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_deinitialized)
    return;
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_bStop = true;
  }

  auto& components = CServiceBroker::GetAppComponents();
  auto appListener = components.GetComponent<CApplicationActionListeners>();
  if (appListener)
    appListener->UnregisterActionListener(this);

  StopThread(true);
  // An Open already in progress can register the announcer before it exits.
  if (auto announcements = CServiceBroker::GetAnnouncementManager())
    announcements->RemoveAnnouncer(this);
  delete m_queryThread;
  m_queryThread = nullptr;

  if (m_cecAdapter)
  {
    RetireCecCallbacks();
    CECDestroy(m_cecAdapter);
    m_cecAdapter = NULL;
  }
  m_deinitialized = true;
}

void CPeripheralCecAdapter::RetireCecCallbacks(void)
{
  m_sourceEventGeneration.fetch_add(1, std::memory_order_relaxed);
  DrainCecCallbacks(m_cecAdapter);
}

void CPeripheralCecAdapter::ResetMembers(void)
{
  if (m_cecAdapter)
  {
    RetireCecCallbacks();
    CECDestroy(m_cecAdapter);
  }
  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_cecAdapter = NULL;
  m_bStarted = false;
  m_bHasButton = false;
  m_bIsReady = false;
  ResetVolumeControl();
  m_buttonQueue.clear();
  m_strMenuLanguage = "???";
  m_iExitCode = EXITCODE_QUIT;

  m_bGoingToStandby = false;
  m_bBypassWakeCooldown = false;
  m_bExplicitStandbyPending = false;
  m_bIsRunning = false;
  m_bActiveSourcePending = false;
  m_bStandbyPending = false;
  m_bInactiveViewPending = false;
  m_bActiveSourceBeforeStandby = false;
  m_bOnPlayReceived = false;
  m_bPlaybackPaused = false;
  m_queryThread = NULL;
  m_bPowerOnScreensaver = false;
  m_bUseTVMenuLanguage = false;
  m_bSendInactiveSource = false;
  m_bPowerOffScreensaver = false;
  m_bPowerOffScreensaverPaused = false;
  m_bShutdownOnStandby = false;

  m_currentButton.iButton = 0;
  m_currentButton.iDuration = 0;
  m_standbySent.SetValid(false);
  m_ScreensaverStandbySent.SetValid(false);
  m_tvStandbyReceived.SetValid(false);
  m_configuration.Clear();
}

void CPeripheralCecAdapter::Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                                     const std::string& sender,
                                     const std::string& message,
                                     const CVariant& data)
{
  if (flag == ANNOUNCEMENT::System && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
      message == "OnQuit" && m_bIsReady)
  {
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_iExitCode = static_cast<int>(data["exitcode"].asInteger(EXITCODE_QUIT));
      CServiceBroker::GetAnnouncementManager()->RemoveAnnouncer(this);
    }

    if (m_iExitCode == EXITCODE_POWERDOWN)
    {
      CLog::Log(LOGDEBUG, "{} - Synchronously joining CEC thread to ensure StandbyDevices is sent before system powerdown", __FUNCTION__);
      StopThread(true);
    }
    else
    {
      StopThread(false);
    }
  }
  else if (flag == ANNOUNCEMENT::GUI && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           message == "OnScreensaverDeactivated" && m_bIsReady)
  {
    bool bIgnoreDeactivate(false);
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_ScreensaverStandbySent.SetValid(false);
      m_bStandbyPending = false;
      // Keep the explicit marker in sync when screensaver deactivation cancels standby.
      m_bExplicitStandbyPending = false;
    }
    if (data["shuttingdown"].isBoolean())
    {
      // don't respond to the deactivation if we are just going to suspend/shutdown anyway
      // the tv will not have time to switch on before being told to standby and
      // may not action the standby command.
      bIgnoreDeactivate = data["shuttingdown"].asBoolean();
      if (bIgnoreDeactivate)
        CLog::Log(LOGDEBUG, "{} - ignoring OnScreensaverDeactivated for power action",
                  __FUNCTION__);
    }
    if (m_bPowerOnScreensaver && !bIgnoreDeactivate)
    {
      // Clear the explicit-standby flag and wake the TV. ProcessActivateSource
      // has the STANDBY_WAKE_COOLDOWN guard (5s) that protects against the
      // screensaver video crash waking the TV during standby transition.
      {
        std::unique_lock<CCriticalSection> lock(m_critSection);
        m_bGoingToStandby = false;
      }
      ActivateSource();
    }
  }
  else if (flag == ANNOUNCEMENT::GUI && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           message == "OnScreensaverActivated" && m_bIsReady)
  {
    // Don't put devices to standby if application is currently playing
    const auto& components = CServiceBroker::GetAppComponents();
    const auto appPlayer = components.GetComponent<CApplicationPlayer>();
    if (m_bPowerOffScreensaver)
    {
      // Don't put devices to standby if application is currently playing. Check if video is paused.
      if (m_bPowerOffScreensaverPaused)
      {
        if (!appPlayer->IsPlaying() || appPlayer->IsPausedPlayback())
          StandbyDevices();
      }
      else
      {
        if (!appPlayer->IsPlaying())
          StandbyDevices();
      }
    }
  }
  else if (flag == ANNOUNCEMENT::System && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           message == "OnSleep")
  {
    // this will also power off devices when we're the active source
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_bGoingToStandby = true;
    }
    UnregisterDevice();
  }
  else if (flag == ANNOUNCEMENT::System && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           message == "OnWake")
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    if (m_bStarted)
    {
      CLog::Log(LOGDEBUG, "{} - reuse open CEC adapter connection after standby mode", __FUNCTION__);
      bool bActivate(false);
      {
        bActivate = m_bActiveSourceBeforeStandby;
        m_bActiveSourceBeforeStandby = false;
      }
      if (bActivate)
        ActivateSource();
    }
  }
  else if (flag == ANNOUNCEMENT::Player && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           message == "OnStop")
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_preventActivateSourceOnPlay = CDateTime::GetCurrentDateTime();
    m_bOnPlayReceived = false;
  }
  else if (flag == ANNOUNCEMENT::Player && sender == CAnnouncementManager::ANNOUNCEMENT_SENDER &&
           (message == "OnPlay" || message == "OnResume"))
  {
    const auto& components = CServiceBroker::GetAppComponents();
    const auto appPower = components.GetComponent<CApplicationPowerHandling>();
    if (appPower->IsInScreenSaver())
    {
      return;
    }

    // activate the source when playback started, and the option is enabled
    bool bActivateSource(false);
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      bActivateSource = (GetSettingBool("activate_on_activity") && !m_bOnPlayReceived &&
                         (!m_preventActivateSourceOnPlay.IsValid() ||
                          CDateTime::GetCurrentDateTime() - m_preventActivateSourceOnPlay >
                              CDateTimeSpan(0, 0, 0, CEC_SUPPRESS_ACTIVATE_SOURCE_AFTER_ON_STOP)));
      m_bOnPlayReceived = true;
    }
    if (bActivateSource && m_cecAdapter)
      ActivateSource();
  }
}

bool CPeripheralCecAdapter::InitialiseFeature(const PeripheralFeature feature)
{
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_retired.load(std::memory_order_acquire))
    return false;
  if (feature == FEATURE_CEC && !m_bStarted && GetSettingBool("enabled"))
  {
    // hide settings that have an override set
    if (!GetSettingString("wake_devices_advanced").empty())
      SetSettingVisible("wake_devices", false);
    if (!GetSettingString("standby_devices_advanced").empty())
      SetSettingVisible("standby_devices", false);

    SetConfigurationFromSettings();
    m_callbacks.Clear();
    m_callbacks.logMessage = &CecLogMessage;
    m_callbacks.keyPress = &CecKeyPress;
    m_callbacks.commandReceived = &CecCommand;
    m_callbacks.configurationChanged = &CecConfiguration;
    m_callbacks.alert = &CecAlert;
    m_callbacks.sourceActivated = &CecSourceActivated;
    m_configuration.callbackParam = this;
    m_configuration.callbacks = &m_callbacks;

    // A stopped session may still own its native adapter and query object.
    StopThread(true);
    delete m_queryThread;
    m_queryThread = nullptr;
    if (m_cecAdapter)
    {
      RetireCecCallbacks();
      CECDestroy(m_cecAdapter);
      m_cecAdapter = nullptr;
    }
    if (m_retired.load(std::memory_order_acquire))
      return false;
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_buttonQueue.clear();
      m_currentButton = {};
      m_bHasButton = false;
    }
    ResetVolumeControl();
    {
      std::unique_lock<CCriticalSection> volumeLock(m_volumeSection);
      m_volumeEnabled = true;
      m_tvVolumeControl = GetSettingBool("volume_control_tv");
    }
    m_cecAdapter = CECInitialise(&m_configuration);

    if (!m_cecAdapter || m_configuration.serverVersion < CEC_LIB_SUPPORTED_VERSION)
    {
      /* unsupported libcec version */
      CLog::Log(
          LOGERROR,
          "Detected version of libCEC interface ({0:x}) is lower than the supported version {1:x}",
          m_cecAdapter ? m_configuration.serverVersion : -1, CEC_LIB_SUPPORTED_VERSION);

      // display warning: incompatible libCEC
      std::string strMessage = StringUtils::Format(
          g_localizeStrings.Get(36040), m_cecAdapter ? m_configuration.serverVersion : -1,
          CEC_LIB_SUPPORTED_VERSION);
      CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Error, g_localizeStrings.Get(36000),
                                            strMessage);
      m_bError = true;
      if (m_cecAdapter)
      {
        RetireCecCallbacks();
        CECDestroy(m_cecAdapter);
      }
      m_cecAdapter = NULL;

      m_features.clear();
      return false;
    }
    else
    {
      CLog::Log(LOGDEBUG, "{} - using libCEC v{}", __FUNCTION__,
                m_cecAdapter->VersionToString(m_configuration.serverVersion));
      SetVersionInfo(m_configuration);
    }

    m_bStarted = true;
    Create();
  }

  return CPeripheral::InitialiseFeature(feature);
}

void CPeripheralCecAdapter::SetVersionInfo(const libcec_configuration& configuration)
{
  m_strVersionInfo = StringUtils::Format("libCEC {} - firmware v{}",
                                         m_cecAdapter->VersionToString(configuration.serverVersion),
                                         configuration.iFirmwareVersion);

  // append firmware build date
  if (configuration.iFirmwareBuildDate != CEC_FW_BUILD_UNKNOWN)
  {
    CDateTime dt((time_t)configuration.iFirmwareBuildDate);
    m_strVersionInfo += StringUtils::Format(" ({})", dt.GetAsDBDate());
  }
}

bool CPeripheralCecAdapter::OpenConnection(void)
{
  bool bIsOpen(false);

  if (!GetSettingBool("enabled"))
  {
    CLog::Log(LOGDEBUG, "{} - CEC adapter is disabled in peripheral settings", __FUNCTION__);
    m_bStarted = false;
    return bIsOpen;
  }

  // open the CEC adapter
  CLog::Log(LOGDEBUG, "{} - opening a connection to the CEC adapter: {}", __FUNCTION__,
            m_strComPort);

  bool bConnectionFailedDisplayed(false);

  // A previous close drained callbacks; a genuine reopen reinstalls them.
  m_sourceEventGeneration.fetch_add(1, std::memory_order_relaxed);
#if CEC_LIB_VERSION_MAJOR >= 5
  m_cecAdapter->SetCallbacks(&m_callbacks, this);
#else
  m_cecAdapter->EnableCallbacks(this, &m_callbacks);
#endif
  while (!m_bStop && !bIsOpen)
  {
    if ((bIsOpen = m_cecAdapter->Open(m_strComPort.c_str(), 10000)) == false)
    {
      // display warning: couldn't initialise libCEC
      CLog::Log(LOGERROR, "{} - could not opening a connection to the CEC adapter", __FUNCTION__);
      if (!bConnectionFailedDisplayed)
        CGUIDialogKaiToast::QueueNotification(
            CGUIDialogKaiToast::Error, g_localizeStrings.Get(36000), g_localizeStrings.Get(36012));
      bConnectionFailedDisplayed = true;

      CThread::Sleep(10000ms);
    }
  }

  if (bIsOpen)
  {
    CLog::Log(LOGDEBUG, "{} - connection to the CEC adapter opened", __FUNCTION__);

    // read the configuration
    libcec_configuration config;
    if (m_cecAdapter->GetCurrentConfiguration(&config))
    {
      // update the local configuration
      std::unique_lock<CCriticalSection> lock(m_critSection);
      SetConfigurationFromLibCEC(config);
    }
  }

  return bIsOpen;
}

void CPeripheralCecAdapter::Process(void)
{
  if (!OpenConnection())
    return;

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_iExitCode = EXITCODE_QUIT;
    m_bGoingToStandby = false;
    m_bIsRunning = true;
    m_bActiveSourceBeforeStandby = false;
  }

  CServiceBroker::GetAnnouncementManager()->AddAnnouncer(this);

  m_queryThread = new CPeripheralCecAdapterUpdateThread(this, &m_configuration);
  m_queryThread->Create(false);

  while (!m_bStop)
  {
    if (!m_bStop)
      ProcessVolumeChange();

    if (!m_bStop)
      ProcessActivateSource();

    if (!m_bStop)
      ProcessStandbyDevices();

    if (!m_bStop)
      ProcessInactiveView();

    if (!m_bStop)
      CThread::Sleep(5ms);
  }

  m_queryThread->StopThread(true);
  ProcessVolumeChange(true);

  RetireCecCallbacks();
  UnregisterDevice();

  m_cecAdapter->Close();

  CLog::Log(LOGDEBUG, "{} - CEC adapter processor thread ended", __FUNCTION__);

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_bStarted = false;
    m_bIsRunning = false;
  }
}

bool CPeripheralCecAdapter::HasAudioControl(void)
{
  std::unique_lock<CCriticalSection> lock(m_volumeSection);
  return HasAudioControlLocked();
}

bool CPeripheralCecAdapter::HasAudioControlLocked(void) const
{
  return m_volumeEnabled && !m_volumeConnectionLost && !m_bStop &&
         !m_retired.load(std::memory_order_acquire) && m_volumeTarget != CECDEVICE_UNKNOWN &&
         m_volumeReleaseFailures < 3;
}

void CPeripheralCecAdapter::InvalidateVolumeControl(void)
{
  // Caller holds m_volumeSection. Keep the pressed recipient until the worker
  // releases it; clearing the route must never redirect that release.
  ++m_volumeGeneration;
  m_volumeTarget = CECDEVICE_UNKNOWN;
  m_volumeChangeQueue = {};
  m_bIsMuted = false;
  m_audioNormalizationPending = false;
}

void CPeripheralCecAdapter::ResetVolumeControl(void)
{
  // Called only after the old worker has joined and its native session retired.
  std::unique_lock<CCriticalSection> lock(m_volumeSection);
  if (m_pressedVolumeTarget != CECDEVICE_UNKNOWN)
    CLog::Log(LOGWARNING, "CEC volume: retiring session with unreleased target {}",
              static_cast<int>(m_pressedVolumeTarget));
  InvalidateVolumeControl();
  m_volumeEnabled = false;
  m_volumeConnectionLost = false;
  m_pressedVolumeTarget = CECDEVICE_UNKNOWN;
  m_volumeReleaseFailures = 0;
  m_lastChange = VOLUME_CHANGE_NONE;
  m_lastKeypress = {};
  m_lastVolumeInput = {};
}

void CPeripheralCecAdapter::SetTvVolumeControl(bool enabled)
{
  {
    std::unique_lock<CCriticalSection> lock(m_volumeSection);
    m_tvVolumeControl = enabled;
    InvalidateVolumeControl();
  }
  {
    // Remove received keys mapped before the policy change as well.
    std::unique_lock<CCriticalSection> lock(m_critSection);
    const auto isVolume = [](const CecButtonPress& key)
    {
      return key.iButton == XINPUT_IR_REMOTE_VOLUME_PLUS ||
             key.iButton == XINPUT_IR_REMOTE_VOLUME_MINUS || key.iButton == XINPUT_IR_REMOTE_MUTE;
    };
    m_buttonQueue.erase(std::remove_if(m_buttonQueue.begin(), m_buttonQueue.end(), isVolume),
                        m_buttonQueue.end());
    if (isVolume(m_currentButton))
    {
      m_bHasButton = false;
      m_currentButton = {};
    }
  }
  // This option is Kodi routing policy, not a libCEC configuration field.
  if (IsRunning() && m_queryThread)
    m_queryThread->RefreshAudioControl();
}

bool CPeripheralCecAdapter::ReleaseVolumeKey(std::unique_lock<CCriticalSection>& lock)
{
  if (m_pressedVolumeTarget == CECDEVICE_UNKNOWN)
    return true;
  if (m_volumeConnectionLost)
    return false;

  const auto target = m_pressedVolumeTarget;
  lock.unlock();
  const bool released = m_cecAdapter->SendKeyRelease(target, false);
  lock.lock();
  CLog::Log(released ? LOGDEBUG : LOGERROR, "CEC volume: release target {} {}",
            static_cast<int>(target), released ? "accepted by libCEC" : "failed");
  if (released)
  {
    m_pressedVolumeTarget = CECDEVICE_UNKNOWN;
    m_lastChange = VOLUME_CHANGE_NONE;
    m_lastKeypress = {};
    m_volumeReleaseFailures = 0;
  }
  else
  {
    // Preserve the recipient for a later release attempt, but admit no new keys.
    InvalidateVolumeControl();
    ++m_volumeReleaseFailures;
    m_lastKeypress = std::chrono::steady_clock::now();
  }
  return released;
}

void CPeripheralCecAdapter::OnVolumeControlConnected(cec_logical_address target,
                                                     uint64_t volumeGeneration,
                                                     uint64_t sourceGeneration)
{
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  const auto current = [this, target, volumeGeneration, sourceGeneration]
  {
    return !m_retired.load(std::memory_order_acquire) && !m_bStop && m_volumeEnabled &&
           !m_volumeConnectionLost && m_audioNormalizationPending &&
           target == (m_tvVolumeControl ? CECDEVICE_TV : CECDEVICE_AUDIOSYSTEM) &&
           m_volumeGeneration == volumeGeneration &&
           m_sourceEventGeneration.load(std::memory_order_relaxed) == sourceGeneration;
  };
  {
    std::unique_lock<CCriticalSection> lock(m_volumeSection);
    if (!current())
      return;
  }
  // Run on main, without the volume lock and without reentering peripherals.
  // A query worker must never wait for GUI/application effects during teardown.
  auto& components = CServiceBroker::GetAppComponents();
  const auto appVolume = components.GetComponent<CApplicationVolumeHandling>();
  appVolume->SetLocalMute(false);
  appVolume->SetVolume(CApplicationVolumeHandling::VOLUME_MAXIMUM, false);
  {
    std::unique_lock<CCriticalSection> lock(m_volumeSection);
    if (current())
    {
      m_volumeTarget = target;
      m_audioNormalizationPending = false;
      CLog::Log(LOGDEBUG, "CEC volume: target {} routing enabled after local normalization",
                static_cast<int>(target));
    }
  }
}

void CPeripheralCecAdapter::ProcessVolumeChange(bool stopping)
{
  std::unique_lock<CCriticalSection> lock(m_volumeSection);
  if (stopping)
  {
    m_volumeEnabled = false;
    InvalidateVolumeControl();
  }

  if (m_volumeConnectionLost)
  {
    InvalidateVolumeControl();
    if (m_pressedVolumeTarget != CECDEVICE_UNKNOWN)
    {
      CLog::Log(LOGWARNING, "CEC volume: connection lost; unable to release target {}",
                static_cast<int>(m_pressedVolumeTarget));
      m_pressedVolumeTarget = CECDEVICE_UNKNOWN;
      m_lastChange = VOLUME_CHANGE_NONE;
    }
    return;
  }

  const auto now = std::chrono::steady_clock::now();
  const auto sincePress =
      std::chrono::duration_cast<std::chrono::milliseconds>(now - m_lastKeypress);
  const auto sinceInput =
      std::chrono::duration_cast<std::chrono::milliseconds>(now - m_lastVolumeInput);
  const bool ready = HasAudioControlLocked();
  if (!ready)
    m_volumeChangeQueue = {};

  if (m_pressedVolumeTarget != CECDEVICE_UNKNOWN &&
      (stopping || m_pressedVolumeTarget != m_volumeTarget ||
       m_pressedVolumeGeneration != m_volumeGeneration || !ready ||
       sinceInput.count() > VOLUME_CHANGE_TIMEOUT))
  {
    // Bound failed releases to three attempts at the refresh interval. Shutdown
    // makes one final attempt; a failed bus cannot guarantee a physical key-up.
    if (!stopping && m_volumeReleaseFailures >= 3)
      return;
    if (!stopping && sincePress.count() < VOLUME_REFRESH_TIMEOUT && !ready)
      return;
    if (!ReleaseVolumeKey(lock))
      return;
  }

  if (!HasAudioControlLocked() || m_volumeChangeQueue.empty())
    return;

  const auto change = m_volumeChangeQueue.front();
  const auto generation = m_volumeGeneration;
  m_volumeChangeQueue.pop();
  while (!m_volumeChangeQueue.empty() && m_volumeChangeQueue.front() == change)
    m_volumeChangeQueue.pop();

  if (m_pressedVolumeTarget != CECDEVICE_UNKNOWN)
  {
    if (change == m_lastChange)
    {
      // Mute is a toggle; a held key must not repeatedly toggle it.
      if (change == VOLUME_CHANGE_MUTE || sincePress.count() < VOLUME_REFRESH_TIMEOUT)
        return;
    }
    else if (!ReleaseVolumeKey(lock))
      return;
  }

  const auto key = change == VOLUME_CHANGE_UP     ? CEC_USER_CONTROL_CODE_VOLUME_UP
                   : change == VOLUME_CHANGE_DOWN ? CEC_USER_CONTROL_CODE_VOLUME_DOWN
                                                  : CEC_USER_CONTROL_CODE_MUTE;
  // Record the recipient even on failure: a failed native write does not prove
  // that no part of a press reached the bus. Always attempt its matching release.
  if (!HasAudioControlLocked() || generation != m_volumeGeneration)
    return;
  m_pressedVolumeTarget = m_volumeTarget;
  m_pressedVolumeGeneration = m_volumeGeneration;
  m_lastChange = change;
  m_lastKeypress = now;
  const auto target = m_pressedVolumeTarget;
  // This is the admission point. An already admitted native call cannot be
  // cancelled; settings/loss fence subsequent work, and the worker releases this
  // saved recipient before any new-generation press. Never stall GUI producers
  // on libCEC's synchronous transport.
  lock.unlock();
  const bool sent = m_cecAdapter->SendKeypress(target, key, false);
  lock.lock();
  CLog::Log(sent ? LOGDEBUG : LOGERROR, "CEC volume: key {:02x} target {} {}",
            static_cast<int>(key), static_cast<int>(m_pressedVolumeTarget),
            sent ? "accepted by libCEC" : "failed; reverting to local volume");
  if (!sent)
  {
    InvalidateVolumeControl();
    if (!m_volumeConnectionLost)
      ReleaseVolumeKey(lock);
  }
  else if (change == VOLUME_CHANGE_MUTE && !m_tvVolumeControl && generation == m_volumeGeneration)
    m_bIsMuted = !m_bIsMuted; // Legacy AVR estimate; never presented as TV status.
}

bool CPeripheralCecAdapter::QueueVolumeChange(CecVolumeChange change)
{
  std::unique_lock<CCriticalSection> lock(m_volumeSection);
  if (!HasAudioControlLocked())
    return false;
  m_lastVolumeInput = std::chrono::steady_clock::now();
  // Bound producer work; held repeats only need the most recent identical entry.
  if (m_volumeChangeQueue.empty() || m_volumeChangeQueue.back() != change)
  {
    if (m_volumeChangeQueue.size() >= 32)
      return true;
    m_volumeChangeQueue.push(change);
  }
  return true;
}

bool CPeripheralCecAdapter::VolumeUp(void)
{
  return QueueVolumeChange(VOLUME_CHANGE_UP);
}

bool CPeripheralCecAdapter::VolumeDown(void)
{
  return QueueVolumeChange(VOLUME_CHANGE_DOWN);
}

bool CPeripheralCecAdapter::ToggleMute(void)
{
  return QueueVolumeChange(VOLUME_CHANGE_MUTE);
}

bool CPeripheralCecAdapter::IsMuted(void)
{
  std::unique_lock<CCriticalSection> lock(m_volumeSection);
  // CEC key submission is not an audio-status report from the TV.
  return !m_tvVolumeControl && HasAudioControlLocked() && m_bIsMuted;
}

bool CPeripheralCecAdapter::SuppressCecVolumeKey(cec_user_control_code key) const
{
  // libCEC's key callback has no initiator. In TV-speaker mode, do not echo
  // incoming CEC volume keys back to the TV, including timed repeats/releases.
  return m_tvVolumeControl &&
         (key == CEC_USER_CONTROL_CODE_VOLUME_UP || key == CEC_USER_CONTROL_CODE_VOLUME_DOWN ||
          key == CEC_USER_CONTROL_CODE_MUTE || key == CEC_USER_CONTROL_CODE_MUTE_FUNCTION ||
          key == CEC_USER_CONTROL_CODE_RESTORE_VOLUME_FUNCTION);
}

void CPeripheralCecAdapter::SetMenuLanguage(const char* strLanguage)
{
  if (StringUtils::EqualsNoCase(m_strMenuLanguage, strLanguage))
    return;

  std::string strGuiLanguage;

  if (!strcmp(strLanguage, "bul"))
    strGuiLanguage = "bg_bg";
  else if (!strcmp(strLanguage, "hrv"))
    strGuiLanguage = "hr_hr";
  else if (!strcmp(strLanguage, "cze"))
    strGuiLanguage = "cs_cz";
  else if (!strcmp(strLanguage, "dan"))
    strGuiLanguage = "da_dk";
  else if (!strcmp(strLanguage, "deu"))
    strGuiLanguage = "de_de";
  else if (!strcmp(strLanguage, "dut"))
    strGuiLanguage = "nl_nl";
  else if (!strcmp(strLanguage, "eng"))
    strGuiLanguage = "en_gb";
  else if (!strcmp(strLanguage, "fin"))
    strGuiLanguage = "fi_fi";
  else if (!strcmp(strLanguage, "fre"))
    strGuiLanguage = "fr_fr";
  else if (!strcmp(strLanguage, "ger"))
    strGuiLanguage = "de_de";
  else if (!strcmp(strLanguage, "gre"))
    strGuiLanguage = "el_gr";
  else if (!strcmp(strLanguage, "hun"))
    strGuiLanguage = "hu_hu";
  else if (!strcmp(strLanguage, "ita"))
    strGuiLanguage = "it_it";
  else if (!strcmp(strLanguage, "nor"))
    strGuiLanguage = "nb_no";
  else if (!strcmp(strLanguage, "pol"))
    strGuiLanguage = "pl_pl";
  else if (!strcmp(strLanguage, "por"))
    strGuiLanguage = "pt_pt";
  else if (!strcmp(strLanguage, "rum"))
    strGuiLanguage = "ro_ro";
  else if (!strcmp(strLanguage, "rus"))
    strGuiLanguage = "ru_ru";
  else if (!strcmp(strLanguage, "srp"))
    strGuiLanguage = "sr_rs@latin";
  else if (!strcmp(strLanguage, "slo"))
    strGuiLanguage = "sk_sk";
  else if (!strcmp(strLanguage, "slv"))
    strGuiLanguage = "sl_si";
  else if (!strcmp(strLanguage, "spa"))
    strGuiLanguage = "es_es";
  else if (!strcmp(strLanguage, "swe"))
    strGuiLanguage = "sv_se";
  else if (!strcmp(strLanguage, "tur"))
    strGuiLanguage = "tr_tr";

  if (!strGuiLanguage.empty())
  {
    strGuiLanguage = "resource.language." + strGuiLanguage;
    CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SETLANGUAGE, -1, -1, nullptr, strGuiLanguage);
    CLog::Log(LOGDEBUG, "{} - language set to '{}'", __FUNCTION__, strGuiLanguage);
  }
  else
    CLog::Log(LOGWARNING, "{} - TV menu language set to unknown value '{}'", __FUNCTION__,
              strLanguage);
}

void CPeripheralCecAdapter::OnTvStandby(void)
{
  // announce regardless of the configured action - addons have no other way to
  // observe the TV going away (e.g. to cancel a queued auto-play)
  CServiceBroker::GetAnnouncementManager()->Announce(ANNOUNCEMENT::Other, "OnTVStandby");

  int iActionOnTvStandby = GetSettingInt("standby_pc_on_tv_standby");
  switch (iActionOnTvStandby)
  {
    case LOCALISED_ID_POWEROFF:
      CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_SHUTDOWN);
      break;
    case LOCALISED_ID_SUSPEND:
      CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_SUSPEND);
      break;
    case LOCALISED_ID_HIBERNATE:
      CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_HIBERNATE);
      break;
    case LOCALISED_ID_QUIT:
      CServiceBroker::GetAppMessenger()->PostMsg(TMSG_QUIT);
      break;
    case LOCALISED_ID_PAUSE:
      CServiceBroker::GetAppMessenger()->PostMsg(TMSG_MEDIA_PAUSE_IF_PLAYING);
      break;
    case LOCALISED_ID_STOP:
    {
      const auto& components = CServiceBroker::GetAppComponents();
      const auto appPlayer = components.GetComponent<CApplicationPlayer>();
      if (appPlayer->IsPlaying())
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_MEDIA_STOP);
      break;
    }
    case LOCALISED_ID_IGNORE:
      break;
    default:
      CLog::Log(LOGERROR, "{} - Unexpected [standby_pc_on_tv_standby] setting value", __FUNCTION__);
      break;
  }
}

void CPeripheralCecAdapter::CecCommand(void* cbParam, const cec_command* command)
{
  CPeripheralCecAdapter* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!adapter)
    return;

  if (adapter->m_bIsReady)
  {
    switch (command->opcode)
    {
      case CEC_OPCODE_STANDBY:
        if ((command->initiator == CECDEVICE_TV ||
             command->destination == CECDEVICE_TV ||
             command->destination == CECDEVICE_BROADCAST) &&
            (!adapter->m_standbySent.IsValid() ||
             CDateTime::GetCurrentDateTime() - adapter->m_standbySent >
                 CDateTimeSpan(0, 0, 0, SCREENSAVER_TIMEOUT)))
        {
          {
            std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
            adapter->m_tvStandbyReceived = CDateTime::GetCurrentDateTime();
          }

          if (adapter->m_bSendInactiveSource)
          {
            CLog::Log(LOGDEBUG, "{} - pending inactive source command because TV initiated standby", __FUNCTION__);
            std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
            adapter->m_bInactiveViewPending = true;
          }
          adapter->OnTvStandby();
        }
        break;
      case CEC_OPCODE_SET_MENU_LANGUAGE:
        if (adapter->m_bUseTVMenuLanguage == 1 && command->initiator == CECDEVICE_TV &&
            command->parameters.size == 3)
        {
          char strNewLanguage[4];
          for (int iPtr = 0; iPtr < 3; iPtr++)
            strNewLanguage[iPtr] = command->parameters[iPtr];
          strNewLanguage[3] = 0;
          adapter->SetMenuLanguage(strNewLanguage);
        }
        break;
      case CEC_OPCODE_DECK_CONTROL:
        if (command->initiator == CECDEVICE_TV && command->parameters.size == 1 &&
            command->parameters[0] == CEC_DECK_CONTROL_MODE_STOP)
        {
          cec_keypress key;
          key.duration = 500;
          key.keycode = CEC_USER_CONTROL_CODE_STOP;
          adapter->PushCecKeypress(key);
        }
        break;
      case CEC_OPCODE_PLAY:
        if (command->initiator == CECDEVICE_TV && command->parameters.size == 1)
        {
          if (command->parameters[0] == CEC_PLAY_MODE_PLAY_FORWARD)
          {
            cec_keypress key;
            key.duration = 500;
            key.keycode = CEC_USER_CONTROL_CODE_PLAY;
            adapter->PushCecKeypress(key);
          }
          else if (command->parameters[0] == CEC_PLAY_MODE_PLAY_STILL)
          {
            cec_keypress key;
            key.duration = 500;
            key.keycode = CEC_USER_CONTROL_CODE_PAUSE;
            adapter->PushCecKeypress(key);
          }
        }
        break;
      default:
        break;
    }
  }
}

void CPeripheralCecAdapter::CecConfiguration(void* cbParam, const libcec_configuration* config)
{
  CPeripheralCecAdapter* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!adapter)
    return;

  std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
  adapter->SetConfigurationFromLibCEC(*config);
}

void CPeripheralCecAdapter::CecAlert(void* cbParam,
                                     const libcec_alert alert,
                                     const libcec_parameter data)
{
  CPeripheralCecAdapter* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!adapter)
    return;

  bool bReopenConnection(false);
  int iAlertString(0);
  switch (alert)
  {
    case CEC_ALERT_SERVICE_DEVICE:
      iAlertString = 36027;
      break;
    case CEC_ALERT_CONNECTION_LOST:
      bReopenConnection = true;
      break;
#if defined(CEC_ALERT_PERMISSION_ERROR)
    case CEC_ALERT_PERMISSION_ERROR:
      bReopenConnection = true;
      iAlertString = 36031;
      break;
    case CEC_ALERT_PORT_BUSY:
      bReopenConnection = true;
      iAlertString = 36032;
      break;
#endif
    default:
      break;
  }

  // display the alert
  if (iAlertString)
  {
    std::string strLog(g_localizeStrings.Get(iAlertString));
    if (data.paramType == CEC_PARAMETER_TYPE_STRING && data.paramData)
      strLog += StringUtils::Format(" - {}", (const char*)data.paramData);
    CGUIDialogKaiToast::QueueNotification(CGUIDialogKaiToast::Info, g_localizeStrings.Get(36000),
                                          strLog);
  }

  if (bReopenConnection)
  {
    // Callback-safe admission fence. A callback never waits for native work or
    // lifecycle ownership; a native transmit may itself cause this alert.
    adapter->m_volumeConnectionLost = true;
    // Reopen the connection asynchronously. Otherwise a deadlock may occur.
    // Reconnect means destruction and recreation of our libcec instance, but libcec
    // calls this callback function synchronously and must not be destroyed meanwhile.
    adapter->ReopenConnection(true);
  }
}

void CPeripheralCecAdapter::CecKeyPress(void* cbParam, const cec_keypress* key)
{
  CPeripheralCecAdapter* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!!adapter)
    adapter->PushCecKeypress(*key);
}

void CPeripheralCecAdapter::GetNextKey(void)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_bHasButton = false;
  if (m_bIsReady)
  {
    std::vector<CecButtonPress>::iterator it = m_buttonQueue.begin();
    if (it != m_buttonQueue.end())
    {
      m_currentButton = (*it);
      m_buttonQueue.erase(it);
      m_bHasButton = true;
    }
  }
}

void CPeripheralCecAdapter::PushCecKeypress(const CecButtonPress& key)
{
  CLog::Log(LOGDEBUG, "{} - received key {:2x} duration {}", __FUNCTION__, key.iButton,
            key.iDuration);

  std::unique_lock<CCriticalSection> lock(m_critSection);
  // Recheck after mapping under the same lock used to clear received keys on a
  // policy change. A callback may have passed the raw-key guard before opt-in.
  if (m_tvVolumeControl &&
      (key.iButton == XINPUT_IR_REMOTE_VOLUME_PLUS ||
       key.iButton == XINPUT_IR_REMOTE_VOLUME_MINUS || key.iButton == XINPUT_IR_REMOTE_MUTE))
    return;
  // avoid the queue getting too long
  if (m_configuration.iButtonRepeatRateMs && m_buttonQueue.size() > 5)
    return;
  if (m_configuration.iButtonRepeatRateMs == 0 && key.iDuration > 0)
  {
    if (m_currentButton.iButton == key.iButton && m_currentButton.iDuration == 0)
    {
      // update the duration
      if (m_bHasButton)
        m_currentButton.iDuration = key.iDuration;
      // ignore this one, since it's already been handled by xbmc
      return;
    }
    // if we received a keypress with a duration set, try to find the same one without a duration
    // set, and replace it
    for (std::vector<CecButtonPress>::reverse_iterator it = m_buttonQueue.rbegin();
         it != m_buttonQueue.rend(); ++it)
    {
      if ((*it).iButton == key.iButton)
      {
        if ((*it).iDuration == 0)
        {
          // replace this entry
          (*it).iDuration = key.iDuration;
          return;
        }
        // add a new entry
        break;
      }
    }
  }

  m_lastCecKeypressTime = std::chrono::steady_clock::now();
  m_buttonQueue.push_back(key);
}

void CPeripheralCecAdapter::PushCecKeypress(const cec_keypress& key)
{
  if (SuppressCecVolumeKey(key.keycode))
    return;

  CecButtonPress xbmcKey;
  xbmcKey.iDuration = key.duration;

  switch (key.keycode)
  {
    case CEC_USER_CONTROL_CODE_SELECT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_SELECT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_UP;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_DOWN;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_LEFT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_LEFT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_LEFT_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_LEFT;
      PushCecKeypress(xbmcKey);
      xbmcKey.iButton = XINPUT_IR_REMOTE_UP;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_LEFT_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_LEFT;
      PushCecKeypress(xbmcKey);
      xbmcKey.iButton = XINPUT_IR_REMOTE_DOWN;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_RIGHT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_RIGHT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_RIGHT_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_RIGHT;
      PushCecKeypress(xbmcKey);
      xbmcKey.iButton = XINPUT_IR_REMOTE_UP;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_RIGHT_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_RIGHT;
      PushCecKeypress(xbmcKey);
      xbmcKey.iButton = XINPUT_IR_REMOTE_DOWN;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_SETUP_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_TITLE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_CONTENTS_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CONTENTS_MENU;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_ROOT_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_ROOT_MENU;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_TOP_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_TOP_MENU;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_DVD_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_DVD_MENU;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_FAVORITE_MENU:
      xbmcKey.iButton = XINPUT_IR_REMOTE_MENU;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_EXIT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_BACK;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_ENTER:
      xbmcKey.iButton = XINPUT_IR_REMOTE_ENTER;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_CHANNEL_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CHANNEL_MINUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_CHANNEL_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CHANNEL_PLUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_PREVIOUS_CHANNEL:
      xbmcKey.iButton = XINPUT_IR_REMOTE_TELETEXT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_SOUND_SELECT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_LANGUAGE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_POWER:
    case CEC_USER_CONTROL_CODE_POWER_TOGGLE_FUNCTION:
    case CEC_USER_CONTROL_CODE_POWER_OFF_FUNCTION:
      xbmcKey.iButton = XINPUT_IR_REMOTE_POWER;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_VOLUME_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_VOLUME_PLUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_VOLUME_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_VOLUME_MINUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_MUTE:
    case CEC_USER_CONTROL_CODE_MUTE_FUNCTION:
    case CEC_USER_CONTROL_CODE_RESTORE_VOLUME_FUNCTION:
      xbmcKey.iButton = XINPUT_IR_REMOTE_MUTE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_PLAY:
      xbmcKey.iButton = XINPUT_IR_REMOTE_PLAY;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_STOP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_STOP;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_PAUSE:
      xbmcKey.iButton = XINPUT_IR_REMOTE_PAUSE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_REWIND:
      xbmcKey.iButton = XINPUT_IR_REMOTE_REVERSE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_FAST_FORWARD:
      xbmcKey.iButton = XINPUT_IR_REMOTE_FORWARD;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER0:
      xbmcKey.iButton = XINPUT_IR_REMOTE_0;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER1:
      xbmcKey.iButton = XINPUT_IR_REMOTE_1;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER2:
      xbmcKey.iButton = XINPUT_IR_REMOTE_2;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER3:
      xbmcKey.iButton = XINPUT_IR_REMOTE_3;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER4:
      xbmcKey.iButton = XINPUT_IR_REMOTE_4;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER5:
      xbmcKey.iButton = XINPUT_IR_REMOTE_5;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER6:
      xbmcKey.iButton = XINPUT_IR_REMOTE_6;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER7:
      xbmcKey.iButton = XINPUT_IR_REMOTE_7;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER8:
      xbmcKey.iButton = XINPUT_IR_REMOTE_8;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NUMBER9:
      xbmcKey.iButton = XINPUT_IR_REMOTE_9;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_RECORD:
      xbmcKey.iButton = XINPUT_IR_REMOTE_RECORD;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_CLEAR:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CLEAR;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_DISPLAY_INFORMATION:
      xbmcKey.iButton = XINPUT_IR_REMOTE_INFO;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_PAGE_UP:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CHANNEL_PLUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_PAGE_DOWN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_CHANNEL_MINUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_FORWARD:
      xbmcKey.iButton = XINPUT_IR_REMOTE_SKIP_PLUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_BACKWARD:
      xbmcKey.iButton = XINPUT_IR_REMOTE_SKIP_MINUS;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_F1_BLUE:
      xbmcKey.iButton = XINPUT_IR_REMOTE_BLUE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_F2_RED:
      xbmcKey.iButton = XINPUT_IR_REMOTE_RED;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_F3_GREEN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_GREEN;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_F4_YELLOW:
      xbmcKey.iButton = XINPUT_IR_REMOTE_YELLOW;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_ELECTRONIC_PROGRAM_GUIDE:
      xbmcKey.iButton = XINPUT_IR_REMOTE_GUIDE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_AN_CHANNELS_LIST:
      xbmcKey.iButton = XINPUT_IR_REMOTE_LIVE_TV;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_NEXT_FAVORITE:
    case CEC_USER_CONTROL_CODE_DOT:
    case CEC_USER_CONTROL_CODE_AN_RETURN:
      xbmcKey.iButton = XINPUT_IR_REMOTE_TITLE; // context menu
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_DATA:
      xbmcKey.iButton = XINPUT_IR_REMOTE_TELETEXT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_SUB_PICTURE:
      xbmcKey.iButton = XINPUT_IR_REMOTE_SUBTITLE;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_EJECT:
      xbmcKey.iButton = XINPUT_IR_REMOTE_EJECT;
      PushCecKeypress(xbmcKey);
      break;
    case CEC_USER_CONTROL_CODE_POWER_ON_FUNCTION:
    case CEC_USER_CONTROL_CODE_INPUT_SELECT:
    case CEC_USER_CONTROL_CODE_INITIAL_CONFIGURATION:
    case CEC_USER_CONTROL_CODE_HELP:
    case CEC_USER_CONTROL_CODE_STOP_RECORD:
    case CEC_USER_CONTROL_CODE_PAUSE_RECORD:
    case CEC_USER_CONTROL_CODE_ANGLE:
    case CEC_USER_CONTROL_CODE_VIDEO_ON_DEMAND:
    case CEC_USER_CONTROL_CODE_TIMER_PROGRAMMING:
    case CEC_USER_CONTROL_CODE_PLAY_FUNCTION:
    case CEC_USER_CONTROL_CODE_PAUSE_PLAY_FUNCTION:
    case CEC_USER_CONTROL_CODE_RECORD_FUNCTION:
    case CEC_USER_CONTROL_CODE_PAUSE_RECORD_FUNCTION:
    case CEC_USER_CONTROL_CODE_STOP_FUNCTION:
    case CEC_USER_CONTROL_CODE_TUNE_FUNCTION:
    case CEC_USER_CONTROL_CODE_SELECT_MEDIA_FUNCTION:
    case CEC_USER_CONTROL_CODE_SELECT_AV_INPUT_FUNCTION:
    case CEC_USER_CONTROL_CODE_SELECT_AUDIO_INPUT_FUNCTION:
    case CEC_USER_CONTROL_CODE_F5:
    case CEC_USER_CONTROL_CODE_NUMBER_ENTRY_MODE:
    case CEC_USER_CONTROL_CODE_NUMBER11:
    case CEC_USER_CONTROL_CODE_NUMBER12:
    case CEC_USER_CONTROL_CODE_SELECT_BROADCAST_TYPE:
    case CEC_USER_CONTROL_CODE_SELECT_SOUND_PRESENTATION:
    case CEC_USER_CONTROL_CODE_UNKNOWN:
    default:
      break;
  }
}

int CPeripheralCecAdapter::GetButton(void)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  if (!m_bHasButton)
    GetNextKey();

  return m_bHasButton ? m_currentButton.iButton : 0;
}

unsigned int CPeripheralCecAdapter::GetHoldTime(void)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  if (!m_bHasButton)
    GetNextKey();

  return m_bHasButton ? m_currentButton.iDuration : 0;
}

void CPeripheralCecAdapter::ResetButton(void)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_bHasButton = false;

  // wait for the key release if the duration isn't 0
  if (m_currentButton.iDuration > 0)
  {
    m_currentButton.iButton = 0;
    m_currentButton.iDuration = 0;
  }
}

void CPeripheralCecAdapter::OnSettingChanged(const std::string& strChangedSetting)
{
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_retired.load(std::memory_order_acquire))
    return;
  if (StringUtils::EqualsNoCase(strChangedSetting, "enabled"))
  {
    bool bEnabled(GetSettingBool("enabled"));
    if (!bEnabled)
    {
      std::unique_lock<CCriticalSection> lock(m_volumeSection);
      m_volumeEnabled = false;
      InvalidateVolumeControl();
    }
    if (!bEnabled && IsRunning())
    {
      CLog::Log(LOGDEBUG, "{} - closing the CEC connection", __FUNCTION__);

      if (m_bInitialised)
      {
        CEC::libcec_configuration config = m_configuration;
        config.bPowerOffOnStandby = 0;
        config.powerOffDevices.Clear();
        m_cecAdapter->SetConfiguration(&config);
      }

      SetConfigurationFromSettings();
      StopThread(true);
    }
    else if (bEnabled && !IsRunning())
    {
      CLog::Log(LOGDEBUG, "{} - starting the CEC connection", __FUNCTION__);
      SetConfigurationFromSettings();
      InitialiseFeature(FEATURE_CEC);
    }
  }
  else if (StringUtils::EqualsNoCase(strChangedSetting, "volume_control_tv"))
  {
    SetTvVolumeControl(GetSettingBool("volume_control_tv"));
  }
  else if (StringUtils::StartsWithNoCase(strChangedSetting, "wakeup_"))
  {
    // seed the mask from config.ini before flipping bits in it: with CEC
    // disabled SetConfigurationFromSettings() never ran, so it is unset here
    if (!m_bFuncConfigLoaded)
      LoadCecFuncConfig();

    SetWakeupBitsFromSettings();

    XBMCAddon::xbmcvfs::configini* config_ini = new XBMCAddon::xbmcvfs::configini();
    config_ini->set("cec_func_config", StringUtils::Format("{:x}", m_iCec_func_config));
    delete(config_ini);
  }
  else if (StringUtils::EqualsNoCase(strChangedSetting, "device_name"))
  {
    XBMCAddon::xbmcvfs::configini* config_ini = new XBMCAddon::xbmcvfs::configini();
    std::string device_name = GetSettingString("device_name");
    if (device_name.length() > 14)
    {
      device_name = StringUtils::Left(device_name, 14);
      SetSetting("device_name", device_name);
    }
    config_ini->set("cec_osd_name", device_name.c_str());
    delete(config_ini);
  }
  else if (IsRunning())
  {
    if (m_queryThread->IsRunning())
    {
      CLog::Log(LOGDEBUG, "{} - sending the updated configuration to libCEC", __FUNCTION__);
      SetConfigurationFromSettings();
      m_queryThread->UpdateConfiguration(&m_configuration);
    }
  }
  else
  {
    CLog::Log(LOGDEBUG, "{} - restarting the CEC connection", __FUNCTION__);
    SetConfigurationFromSettings();
    InitialiseFeature(FEATURE_CEC);
  }
}

void CPeripheralCecAdapter::CecSourceActivated(void* cbParam,
                                               const CEC::cec_logical_address address,
                                               const uint8_t activated)
{
  auto* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!adapter)
    return;

  // Only native work remains inside libCEC's callback lifetime. GUI effects
  // cannot wait for main while main is draining this callback during shutdown.
  const auto generation = adapter->m_sourceEventGeneration.load(std::memory_order_relaxed);
  const bool isActive = adapter->m_cecAdapter->IsLibCECActiveSource();
  auto weak = adapter->weak_from_this();
  CServiceBroker::GetAppMessenger()->PostCallback([weak, generation, address, activated, isActive] {
    auto owner = weak.lock();
    if (!owner)
      return;
    if (owner->m_bStop || owner->m_sourceEventGeneration.load(std::memory_order_relaxed) != generation)
      return;
    owner->OnCecSourceActivated(address, activated, isActive, generation);
  });
}

void CPeripheralCecAdapter::OnCecSourceActivated(CEC::cec_logical_address address,
                                              uint8_t activated,
                                              bool isActive,
                                              uint64_t generation)
{
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_retired.load(std::memory_order_acquire))
    return;
  auto* adapter = this;
  bool pausedByCec;
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    if (m_bStop || m_sourceEventGeneration.load(std::memory_order_relaxed) != generation)
      return;
    pausedByCec = m_bPlaybackPaused;
  }
  FILE* fp = fopen("/run/kodi_cec_startup_done", "w");
  if (fp)
  {
    fputs(isActive ? "1\n" : "0\n", fp);
    fclose(fp);
  }
  auto& appComponentsForScreenSaver = CServiceBroker::GetAppComponents();
  const auto appPowerForScreenSaver = appComponentsForScreenSaver.GetComponent<CApplicationPowerHandling>();
  bool bWasInScreensaver = appPowerForScreenSaver->IsInScreenSaver();

  if (activated == 0)
  {
    std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
    adapter->m_lastSourceDeactivatedTime = std::chrono::steady_clock::now();
    if (adapter->GetSettingInt("pause_or_stop_playback_on_deactivate") != LOCALISED_ID_NONE)
    {
      adapter->m_bOnPlayReceived = false;
    }
  }

  CServiceBroker::GetAnnouncementManager()->Announce(
      ANNOUNCEMENT::Other, activated == 1 ? "OnCECSourceActivated" : "OnCECSourceDeactivated");

  // wake up the screensaver, so the user doesn't switch to a black screen
  if (activated == 1)
  {
    // TV has woken up — clear the standby flag so ActivateSource isn't blocked.
    // This covers cases where the TV wakes on its own (SimpLink, HDMI-CEC
    // re-initialization) rather than from a user button press through OnActionPre.
    {
      std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
      adapter->m_bGoingToStandby = false;
    }

    auto& components = CServiceBroker::GetAppComponents();
    const auto appPower = components.GetComponent<CApplicationPowerHandling>();
    appPower->WakeUpScreenSaverAndDPMS();
  }

  if (adapter->GetSettingInt("pause_or_stop_playback_on_deactivate") != LOCALISED_ID_NONE)
  {
    bool bShowingSlideshow =
        (CServiceBroker::GetGUI()->GetWindowManager().GetActiveWindow() == WINDOW_SLIDESHOW);
    CGUIWindowSlideShow* pSlideShow =
        bShowingSlideshow
            ? CServiceBroker::GetGUI()->GetWindowManager().GetWindow<CGUIWindowSlideShow>(
                  WINDOW_SLIDESHOW)
            : NULL;

    const auto& components = CServiceBroker::GetAppComponents();
    const auto appPlayer = components.GetComponent<CApplicationPlayer>();

    bool bPlayingAndDeactivated = activated == 0 && !bWasInScreensaver &&
                                  ((pSlideShow && pSlideShow->IsPlaying()) ||
                                   !appPlayer->IsPausedPlayback());

    bool bPausedAndActivated =
        activated == 1 && pausedByCec &&
        ((pSlideShow && pSlideShow->IsPaused()) || (appPlayer && appPlayer->IsPausedPlayback()));
    {
      std::unique_lock<CCriticalSection> lock(adapter->m_critSection);
      if (m_bStop || m_sourceEventGeneration.load(std::memory_order_relaxed) != generation)
        return;
      if (bPlayingAndDeactivated)
        adapter->m_bPlaybackPaused = true;
      else if (bPausedAndActivated)
        adapter->m_bPlaybackPaused = false;
    }

    if ((bPlayingAndDeactivated || bPausedAndActivated) &&
        adapter->GetSettingInt("pause_or_stop_playback_on_deactivate") == LOCALISED_ID_PAUSE)
    {
      if (pSlideShow)
        // pause/resume slideshow
        pSlideShow->OnAction(CAction(ACTION_PAUSE));
      else
        // pause/resume player
        CServiceBroker::GetAppMessenger()->SendMsg(TMSG_MEDIA_PAUSE);
    }
    else if (bPlayingAndDeactivated &&
             adapter->GetSettingInt("pause_or_stop_playback_on_deactivate") == LOCALISED_ID_STOP)
    {
      if (pSlideShow)
        pSlideShow->OnAction(CAction(ACTION_STOP));
      else
        CServiceBroker::GetAppMessenger()->SendMsg(TMSG_MEDIA_STOP);
    }
  }
}

void CPeripheralCecAdapter::CecLogMessage(void* cbParam, const cec_log_message* message)
{
  CPeripheralCecAdapter* adapter = static_cast<CPeripheralCecAdapter*>(cbParam);
  if (!adapter)
    return;

  int iLevel = -1;
  switch (message->level)
  {
    case CEC_LOG_ERROR:
      iLevel = LOGERROR;
      break;
    case CEC_LOG_WARNING:
      iLevel = LOGWARNING;
      break;
    case CEC_LOG_NOTICE:
      iLevel = LOGDEBUG;
      break;
    case CEC_LOG_TRAFFIC:
    case CEC_LOG_DEBUG:
      iLevel = LOGDEBUG;
      break;
    default:
      break;
  }

  if (iLevel >= CEC_LOG_NOTICE ||
      (iLevel >= 0 && CServiceBroker::GetLogging().IsLogLevelLogged(LOGDEBUG)))
    CLog::Log(iLevel, LOGCEC, "{} - {}", __FUNCTION__, message->message);
}

void CPeripheralCecAdapter::SetConfigurationFromLibCEC(const CEC::libcec_configuration& config)
{
  bool bChanged(false);

  // set the primary device type
  m_configuration.deviceTypes.Clear();
  m_configuration.deviceTypes.Add(config.deviceTypes[0]);

  // set the connected device
  m_configuration.baseDevice = config.baseDevice;

  // set the devices to wake when starting
  m_configuration.wakeDevices = config.wakeDevices;
  bChanged |= WriteLogicalAddresses(config.wakeDevices, "wake_devices", "wake_devices_advanced");

  // set the devices to power off when stopping
  m_configuration.powerOffDevices = config.powerOffDevices;
  bChanged |=
      WriteLogicalAddresses(config.powerOffDevices, "standby_devices", "standby_devices_advanced");

  // set the boolean settings
  m_configuration.bActivateSource = config.bActivateSource;

  m_configuration.iDoubleTapTimeoutMs = config.iDoubleTapTimeoutMs;
  bChanged |= SetSetting("double_tap_timeout_ms", (int)m_configuration.iDoubleTapTimeoutMs);

  m_configuration.iButtonRepeatRateMs = config.iButtonRepeatRateMs;
  bChanged |= SetSetting("button_repeat_rate_ms", (int)m_configuration.iButtonRepeatRateMs);

  m_configuration.iButtonReleaseDelayMs = config.iButtonReleaseDelayMs;
  bChanged |= SetSetting("button_release_delay_ms", (int)m_configuration.iButtonReleaseDelayMs);

  m_configuration.bPowerOffOnStandby = config.bPowerOffOnStandby;

  m_configuration.iFirmwareVersion = config.iFirmwareVersion;

  memcpy(m_configuration.strDeviceLanguage, config.strDeviceLanguage, 3);
  m_configuration.iFirmwareBuildDate = config.iFirmwareBuildDate;

  SetVersionInfo(m_configuration);

  if (bChanged)
    CLog::Log(LOGDEBUG, "SetConfigurationFromLibCEC - settings updated by libCEC");
}

void CPeripheralCecAdapter::SetWakeupBitsFromSettings(void)
{
  m_iCec_func_config &= ~(1 << AUTO_POWER_ON_MASK);
  m_iCec_func_config |= ((int)GetSettingBool("wakeup_cec_auto_power") << AUTO_POWER_ON_MASK);
  m_iCec_func_config &= ~(1 << STREAMPATH_POWER_ON_MASK);
  m_iCec_func_config |= ((int)GetSettingBool("wakeup_cec_streaming") << STREAMPATH_POWER_ON_MASK);
  m_iCec_func_config &= ~(1 << ACTIVE_SOURCE_MASK);
  m_iCec_func_config |= ((int)GetSettingBool("wakeup_cec_active_route") << ACTIVE_SOURCE_MASK);
}

bool CPeripheralCecAdapter::LoadCecFuncConfig(void)
{
  XBMCAddon::xbmcvfs::configini* config_ini = new XBMCAddon::xbmcvfs::configini();
  std::string cec_func_config = config_ini->get("cec_func_config");
  delete (config_ini);

  bool bHasEntry = !cec_func_config.empty();
  if (bHasEntry)
  {
    m_iCec_func_config = std::stoul(cec_func_config, nullptr, 16);
  }
  else
  {
    // config.ini ships with cec_func_config commented out. Don't assume the
    // old 0x7f default here: that would arm wake-by-active-source (bit 6),
    // waking the box whenever another device on the CEC bus announces itself.
    // The wakeup_cec_* settings are the source of truth until an entry exists.
    m_iCec_func_config = CEC_FUNC_CONFIG_BASE;
    SetWakeupBitsFromSettings();
  }

  m_bFuncConfigLoaded = true;
  return bHasEntry;
}

void CPeripheralCecAdapter::SetConfigurationFromSettings(void)
{
  // client version matches the version of libCEC that we originally used the API from
  m_configuration.clientVersion = LIBCEC_VERSION_TO_UINT(4, 0, 0);

  // device name 'XBMC'
  snprintf(m_configuration.strDeviceName, 13, "%s", GetSettingString("device_name").c_str());

  // set the primary device type
  m_configuration.deviceTypes.Clear();
  switch (GetSettingInt("device_type"))
  {
    case LOCALISED_ID_PLAYBACK_DEVICE:
      m_configuration.deviceTypes.Add(CEC_DEVICE_TYPE_PLAYBACK_DEVICE);
      break;
    case LOCALISED_ID_TUNER_DEVICE:
      m_configuration.deviceTypes.Add(CEC_DEVICE_TYPE_TUNER);
      break;
    case LOCALISED_ID_RECORDING_DEVICE:
    default:
      m_configuration.deviceTypes.Add(CEC_DEVICE_TYPE_RECORDING_DEVICE);
      break;
  }

  // always try to autodetect the address.
  // when the firmware supports this, it will override the physical address, connected device and
  // hdmi port settings
  m_configuration.bAutodetectAddress = CEC_DEFAULT_SETTING_AUTODETECT_ADDRESS;

  // set the physical address
  // when set, it will override the connected device
  std::string strPhysicalAddress = GetSettingString("physical_address");
  int iPhysicalAddress;
  if (sscanf(strPhysicalAddress.c_str(), "%x", &iPhysicalAddress) &&
      iPhysicalAddress >= CEC_PHYSICAL_ADDRESS_TV && iPhysicalAddress <= CEC_MAX_PHYSICAL_ADDRESS)
    m_configuration.iPhysicalAddress = iPhysicalAddress;
  else
    m_configuration.iPhysicalAddress = CEC_PHYSICAL_ADDRESS_TV;

  // reset the HDMI port number
  m_configuration.iHDMIPort = CEC_HDMI_PORTNUMBER_NONE;

  // set the tv vendor override
  int iVendor = GetSettingInt("tv_vendor");
  if (iVendor >= CEC_MIN_VENDORID && iVendor <= CEC_MAX_VENDORID)
    m_configuration.tvVendor = iVendor;

  // read the devices to wake when starting
  std::string strWakeDevices = GetSettingString("wake_devices_advanced");
  StringUtils::Trim(strWakeDevices);
  m_configuration.wakeDevices.Clear();
  if (!strWakeDevices.empty())
    ReadLogicalAddresses(strWakeDevices, m_configuration.wakeDevices);
  else
    ReadLogicalAddresses(GetSettingInt("wake_devices"), m_configuration.wakeDevices);

  // read the devices to power off when stopping
  std::string strStandbyDevices = GetSettingString("standby_devices_advanced");
  StringUtils::Trim(strStandbyDevices);
  m_configuration.powerOffDevices.Clear();
  if (!strStandbyDevices.empty())
    ReadLogicalAddresses(strStandbyDevices, m_configuration.powerOffDevices);
  else
    ReadLogicalAddresses(GetSettingInt("standby_devices"), m_configuration.powerOffDevices);

  // read the boolean settings
  m_bUseTVMenuLanguage = GetSettingBool("use_tv_menu_language");
  m_configuration.bActivateSource = 0; // Handled manually by Kodi
  m_bPowerOffScreensaver = GetSettingBool("cec_standby_screensaver");
  m_bPowerOffScreensaverPaused = GetSettingBool("cec_standby_screensaver_paused") ? 1 : 0;
  m_bPowerOnScreensaver = GetSettingBool("cec_wake_screensaver");
  m_bSendInactiveSource = GetSettingBool("send_inactive_source");
  m_configuration.bAutoWakeAVR = GetSettingBool("power_avr_on_as") ? 1 : 0;

  // read the mutually exclusive boolean settings
  int iStandbyAction(GetSettingInt("standby_pc_on_tv_standby"));
  m_configuration.bPowerOffOnStandby =
      (iStandbyAction == LOCALISED_ID_SUSPEND || iStandbyAction == LOCALISED_ID_HIBERNATE) ? 1 : 0;
  m_bShutdownOnStandby = iStandbyAction == LOCALISED_ID_POWEROFF;

  // double tap prevention timeout in ms
  m_configuration.iDoubleTapTimeoutMs = GetSettingInt("double_tap_timeout_ms");
  m_configuration.iButtonRepeatRateMs = GetSettingInt("button_repeat_rate_ms");
  m_configuration.iButtonReleaseDelayMs = GetSettingInt("button_release_delay_ms");

  if (GetSettingBool("pause_playback_on_deactivate"))
  {
    SetSetting("pause_or_stop_playback_on_deactivate", LOCALISED_ID_PAUSE);
    SetSetting("pause_playback_on_deactivate", false);
  }

  bool bHasFuncConfig = LoadCecFuncConfig();

  XBMCAddon::xbmcvfs::configini* config_ini = new XBMCAddon::xbmcvfs::configini();

  if (GetSettingBool("enabled") != ((m_iCec_func_config >> CEC_FUNC_MASK) & 0x1))
  {
    m_iCec_func_config ^= (1 << CEC_FUNC_MASK);
    config_ini->set("cec_func_config", StringUtils::Format("{:x}", m_iCec_func_config));
  }

  config_ini->set("cec_osd_name", GetSettingString("device_name").c_str());

  delete(config_ini);

  // only sync the GUI toggles from an entry that actually exists in
  // config.ini; otherwise keep the setting defaults / what the user persisted
  if (bHasFuncConfig)
  {
    SetSetting("wakeup_cec_auto_power", (bool)((m_iCec_func_config >> AUTO_POWER_ON_MASK) & 0x1));
    SetSetting("wakeup_cec_streaming",
               (bool)((m_iCec_func_config >> STREAMPATH_POWER_ON_MASK) & 0x1));
    SetSetting("wakeup_cec_active_route", (bool)((m_iCec_func_config >> ACTIVE_SOURCE_MASK) & 0x1));
  }
}

void CPeripheralCecAdapter::ReadLogicalAddresses(const std::string& strString,
                                                 cec_logical_addresses& addresses)
{
  for (size_t iPtr = 0; iPtr < strString.size(); iPtr++)
  {
    std::string strDevice = strString.substr(iPtr, 1);
    StringUtils::Trim(strDevice);
    if (!strDevice.empty())
    {
      int iDevice(0);
      if (sscanf(strDevice.c_str(), "%x", &iDevice) == 1 && iDevice >= 0 && iDevice <= 0xF)
        addresses.Set((cec_logical_address)iDevice);
    }
  }
}

void CPeripheralCecAdapter::ReadLogicalAddresses(int iLocalisedId, cec_logical_addresses& addresses)
{
  addresses.Clear();
  switch (iLocalisedId)
  {
    case LOCALISED_ID_TV:
      addresses.Set(CECDEVICE_TV);
      break;
    case LOCALISED_ID_AVR:
      addresses.Set(CECDEVICE_AUDIOSYSTEM);
      break;
    case LOCALISED_ID_TV_AVR:
      addresses.Set(CECDEVICE_TV);
      addresses.Set(CECDEVICE_AUDIOSYSTEM);
      break;
    case LOCALISED_ID_NONE:
    default:
      break;
  }
}

bool CPeripheralCecAdapter::WriteLogicalAddresses(const cec_logical_addresses& addresses,
                                                  const std::string& strSettingName,
                                                  const std::string& strAdvancedSettingName)
{
  bool bChanged(false);

  // only update the advanced setting if it was set by the user
  if (!GetSettingString(strAdvancedSettingName).empty())
  {
    std::string strPowerOffDevices;
    for (unsigned int iPtr = CECDEVICE_TV; iPtr <= CECDEVICE_BROADCAST; iPtr++)
      if (addresses[iPtr])
        strPowerOffDevices += StringUtils::Format(" {:X}", iPtr);
    StringUtils::Trim(strPowerOffDevices);
    bChanged = SetSetting(strAdvancedSettingName, strPowerOffDevices);
  }

  int iSettingPowerOffDevices = LOCALISED_ID_NONE;
  if (addresses[CECDEVICE_TV] && addresses[CECDEVICE_AUDIOSYSTEM])
    iSettingPowerOffDevices = LOCALISED_ID_TV_AVR;
  else if (addresses[CECDEVICE_TV])
    iSettingPowerOffDevices = LOCALISED_ID_TV;
  else if (addresses[CECDEVICE_AUDIOSYSTEM])
    iSettingPowerOffDevices = LOCALISED_ID_AVR;
  return SetSetting(strSettingName, iSettingPowerOffDevices) || bChanged;
}

CPeripheralCecAdapterUpdateThread::CPeripheralCecAdapterUpdateThread(
    CPeripheralCecAdapter* adapter, libcec_configuration* configuration)
  : CThread("CECAdapterUpdate"), m_adapter(adapter), m_configuration(*configuration)
{
  m_nextConfiguration.Clear();
  m_event.Reset();
}

CPeripheralCecAdapterUpdateThread::~CPeripheralCecAdapterUpdateThread(void)
{
  StopThread(false);
  m_event.Set();
  StopThread(true);
}

void CPeripheralCecAdapterUpdateThread::RefreshAudioControl(void)
{
  m_audioControlRefreshPending = true;
}

bool CPeripheralCecAdapterUpdateThread::UpdateConfiguration(libcec_configuration* configuration)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  if (!configuration)
    return false;

  if (m_bIsUpdating)
  {
    m_bNextConfigurationScheduled = true;
    m_nextConfiguration = *configuration;
  }
  else
  {
    m_configuration = *configuration;
    m_event.Set();
  }
  return true;
}

bool CPeripheralCecAdapterUpdateThread::WaitReady(void)
{
  bool bActivateSource = m_adapter->GetSettingBool("activate_source");

  // don't wait if we're not powering up anything
  if (m_configuration.wakeDevices.IsEmpty() && !bActivateSource)
    return true;

  // wait for the TV if we're configured to become the active source.
  // wait for the first device in the wake list otherwise.
  cec_logical_address waitFor =
      (bActivateSource) ? CECDEVICE_TV : m_configuration.wakeDevices.primary;

  cec_power_status powerStatus(CEC_POWER_STATUS_UNKNOWN);
  bool bContinue(true);
  while (bContinue && !m_adapter->m_bStop && !m_bStop && powerStatus != CEC_POWER_STATUS_ON)
  {
    powerStatus = m_adapter->m_cecAdapter->GetDevicePowerStatus(waitFor);
    if (powerStatus != CEC_POWER_STATUS_ON)
      bContinue = !m_event.Wait(1000ms);
  }

  return powerStatus == CEC_POWER_STATUS_ON;
}

void CPeripheralCecAdapterUpdateThread::UpdateMenuLanguage(void)
{
  // request the menu language of the TV
  if (m_adapter->m_bUseTVMenuLanguage == 1)
  {
    CLog::Log(LOGDEBUG, "{} - requesting the menu language of the TV", __FUNCTION__);
    std::string language(m_adapter->m_cecAdapter->GetDeviceMenuLanguage(CECDEVICE_TV));
    m_adapter->SetMenuLanguage(language.c_str());
  }
  else
  {
    CLog::Log(LOGDEBUG, "{} - using TV menu language is disabled", __FUNCTION__);
  }
}

std::string CPeripheralCecAdapterUpdateThread::UpdateAudioSystemStatus(void)
{
  bool tv;
  uint64_t generation;
  {
    std::unique_lock<CCriticalSection> lock(m_adapter->m_volumeSection);
    tv = m_adapter->m_tvVolumeControl;
    generation = m_adapter->m_volumeGeneration;
  }
  // Native queries run without Kodi locks needed by callbacks. TV mode does not
  // require or probe an audio system. Presence is not proof of volume support.
  const auto target = tv ? CECDEVICE_TV : CECDEVICE_AUDIOSYSTEM;
  const bool present = m_adapter->m_cecAdapter->IsActiveDeviceType(
      tv ? CEC_DEVICE_TYPE_TV : CEC_DEVICE_TYPE_AUDIO_SYSTEM);
  const std::string name = present ? m_adapter->m_cecAdapter->GetDeviceOSDName(target) : "";

  std::unique_lock<CCriticalSection> lock(m_adapter->m_volumeSection);
  if (generation != m_adapter->m_volumeGeneration || !m_adapter->m_volumeEnabled ||
      m_adapter->m_volumeConnectionLost || m_adapter->m_bStop ||
      m_adapter->m_retired.load(std::memory_order_acquire))
    return {};

  const auto selectedTarget = present ? target : CECDEVICE_UNKNOWN;
  if (m_adapter->m_volumeTarget != selectedTarget || m_adapter->m_audioNormalizationPending)
    m_adapter->InvalidateVolumeControl();

  if (present)
  {
    // Normalize Kodi's local output for both TV and AVR CEC control on main.
    // Weak ownership and both generations reject retired sessions/settings.
    m_adapter->m_volumeTarget = CECDEVICE_UNKNOWN;
    m_adapter->m_audioNormalizationPending = true;
    auto weak = m_adapter->weak_from_this();
    const auto volumeGeneration = m_adapter->m_volumeGeneration;
    const auto sourceGeneration =
        m_adapter->m_sourceEventGeneration.load(std::memory_order_relaxed);
    lock.unlock();
    CServiceBroker::GetAppMessenger()->PostCallback(
        [weak, target, volumeGeneration, sourceGeneration]
        {
          if (auto owner = weak.lock())
            owner->OnVolumeControlConnected(target, volumeGeneration, sourceGeneration);
        });
    CLog::Log(LOGDEBUG, "CEC volume: {} target {} ({}); local normalization pending",
              tv ? "TV speakers" : "audio system", static_cast<int>(target), name);
    return tv ? "" : name;
  }
  m_adapter->m_volumeTarget = selectedTarget;
  CLog::Log(LOGDEBUG, "CEC volume: {} target {} ({}); {}", tv ? "TV speakers" : "audio system",
            static_cast<int>(target), name,
            present ? "external control available" : "unavailable; using local volume");
  return present && !tv ? name : "";
}

bool CPeripheralCecAdapterUpdateThread::SetInitialConfiguration(void)
{
  bool bStealFocus = true;
  bool bIsColdBoot = false;
  FILE* fp = fopen("/run/kodi_cec_startup_done", "r");
  if (fp)
  {
    char buf[2] = {0};
    if (fread(buf, 1, 1, fp) == 1)
    {
      if (buf[0] == '0')
        bStealFocus = false;
    }
    fclose(fp);
  }
  else
  {
    bIsColdBoot = true;
  }

  bool bIsActiveSource = false;

  // On cold boot, respect the GUI setting.
  // On soft restart, respect our previous state (bStealFocus) regardless of the setting.
  // This prevents libcec's internal blast and gives us control over active source behavior
  if ((bIsColdBoot && m_adapter->GetSettingBool("activate_source")) || (!bIsColdBoot && bStealFocus))
  {
    m_adapter->m_cecAdapter->SetActiveSource();
    bIsActiveSource = true;
  }

  // devices to wake are set
  cec_logical_addresses tvOnly;
  tvOnly.Clear();
  tvOnly.Set(CECDEVICE_TV);
  if (!m_configuration.wakeDevices.IsEmpty() &&
      (m_configuration.wakeDevices != tvOnly || !bIsActiveSource))
    m_adapter->m_cecAdapter->PowerOnDevices(CECDEVICE_BROADCAST);

  // wait until devices are powered up
  if (!WaitReady())
    return false;

  UpdateMenuLanguage();

  // request the OSD name of the TV
  std::string strNotification;
  std::string tvName(m_adapter->m_cecAdapter->GetDeviceOSDName(CECDEVICE_TV));
  strNotification = StringUtils::Format("{}: {}", g_localizeStrings.Get(36016), tvName);

  std::string strAmpName = UpdateAudioSystemStatus();
  if (!strAmpName.empty())
    strNotification += StringUtils::Format("- {}", strAmpName);

  m_adapter->m_bIsReady = true;

  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_bIsUpdating = false;
  return true;
}

bool CPeripheralCecAdapter::IsRunning(void) const
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  return m_bIsRunning;
}

void CPeripheralCecAdapterUpdateThread::Process(void)
{
  // set the initial configuration
  if (!SetInitialConfiguration())
    return;

  // and wait for updates
  bool bUpdate(false);
  while (!m_bStop)
  {
    if (m_audioControlRefreshPending.exchange(false))
      UpdateAudioSystemStatus();

    // update received
    if (bUpdate || m_event.Wait(500ms))
    {
      if (m_bStop)
        return;
      // set the new configuration
      libcec_configuration configuration;
      {
        std::unique_lock<CCriticalSection> lock(m_critSection);
        configuration = m_configuration;
        m_bIsUpdating = false;
      }

      CLog::Log(LOGDEBUG, "{} - updating the configuration", __FUNCTION__);
      bool bConfigSet(m_adapter->m_cecAdapter->SetConfiguration(&configuration));
      // display message: config updated / failed to update
      if (!bConfigSet)
        CLog::Log(LOGERROR, "{} - libCEC couldn't set the new configuration", __FUNCTION__);
      else
      {
        UpdateMenuLanguage();
        UpdateAudioSystemStatus();
      }

      {
        std::unique_lock<CCriticalSection> lock(m_critSection);
        if ((bUpdate = m_bNextConfigurationScheduled) == true)
        {
          // another update is scheduled
          m_bNextConfigurationScheduled = false;
          m_configuration = m_nextConfiguration;
        }
        else
        {
          // nothing left to do, wait for updates
          m_bIsUpdating = false;
          m_event.Reset();
        }
      }
    }
  }
}

void CPeripheralCecAdapter::OnDeviceRemoved(void)
{
  m_retired.store(true, std::memory_order_release);
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_bDeviceRemoved = true;
  }
  Deinitialize();
}

class PERIPHERALS::CPeripheralCecAdapterReopenJob : public CJob
{
public:
  CPeripheralCecAdapterReopenJob(std::weak_ptr<CPeripheralCecAdapter> adapter, uint64_t generation)
    : m_adapter(std::move(adapter)), m_generation(generation)
  {
  }
  ~CPeripheralCecAdapterReopenJob() override = default;

  bool DoWork(void) override
  {
    auto adapter = m_adapter.lock();
    return adapter && adapter->ReopenConnection(false, m_generation);
  }

private:
  std::weak_ptr<CPeripheralCecAdapter> m_adapter;
  uint64_t m_generation;
};

bool CPeripheralCecAdapter::ReopenConnection(bool bAsync /* = false */, uint64_t generation)
{
  if (bAsync)
  {
    if (m_retired.load(std::memory_order_acquire))
      return false;
    CServiceBroker::GetJobManager()->AddJob(
        new CPeripheralCecAdapterReopenJob(
            weak_from_this(), m_sourceEventGeneration.load(std::memory_order_relaxed)),
        nullptr, CJob::PRIORITY_NORMAL);
    return true;
  }

  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_retired.load(std::memory_order_acquire) ||
      m_sourceEventGeneration.load(std::memory_order_relaxed) != generation)
    return false;

  // stop running thread
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_iExitCode = EXITCODE_RESTARTAPP;
    StopThread(false);
  }
  StopThread();
  if (auto announcements = CServiceBroker::GetAnnouncementManager())
    announcements->RemoveAnnouncer(this);
  delete m_queryThread;
  m_queryThread = nullptr;

  // reset all members to their defaults
  ResetMembers();

  // reopen the connection
  if (m_retired.load(std::memory_order_acquire))
    return false;
  return InitialiseFeature(FEATURE_CEC);
}

void CPeripheralCecAdapter::ActivateSource(void)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_bActiveSourcePending = true;
}

void CPeripheralCecAdapter::ProcessActivateSource(void)
{
  bool bActivate(false);

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    bActivate = m_bActiveSourcePending;
    m_bActiveSourcePending = false;

    bool bBypass = m_bBypassWakeCooldown;
    m_bBypassWakeCooldown = false;

    // If a standby is currently queued to be sent, or if a standby was just triggered in the last STANDBY_WAKE_COOLDOWN seconds,
    // silently swallow any queued ActivateSource commands (such as those generated blindly by OnActionPre)
    // because they will conflict with the standby request and wake the TV back up.
    if (!bBypass &&
        (m_bStandbyPending || 
         (m_standbySent.IsValid() && (CDateTime::GetCurrentDateTime() - m_standbySent < CDateTimeSpan(0, 0, 0, STANDBY_WAKE_COOLDOWN))) ||
         (m_tvStandbyReceived.IsValid() && (CDateTime::GetCurrentDateTime() - m_tvStandbyReceived < CDateTimeSpan(0, 0, 0, STANDBY_WAKE_COOLDOWN)))))
    {
      if (bActivate)
      {
        CLog::Log(LOGDEBUG, "{} - swallowing pending ActivateSource due to pending or recent standby", __FUNCTION__);
      }
      bActivate = false;
    }
  }

  if (bActivate && m_cecAdapter && !m_cecAdapter->IsLibCECActiveSource())
    m_cecAdapter->SetActiveSource();
}

void CPeripheralCecAdapter::ProcessInactiveView(void)
{
  bool bInactiveView(false);

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    bInactiveView = m_bInactiveViewPending;
    m_bInactiveViewPending = false;
  }

  if (bInactiveView && m_cecAdapter)
  {
    CLog::Log(LOGDEBUG, "{} - sending inactive source command", __FUNCTION__);
    m_cecAdapter->SetInactiveView();
  }
}

void CPeripheralCecAdapter::UnregisterDevice(void)
{
  if ( m_iExitCode == EXITCODE_RESTARTAPP
    || m_iExitCode == EXITCODE_REBOOT
    || m_bDeviceRemoved)
      return;

  bool bActiveSource = m_cecAdapter->IsLibCECActiveSource();

  if (m_bGoingToStandby)
    m_bActiveSourceBeforeStandby = bActiveSource;

  if (bActiveSource)
  {
    if ( GetSettingBool("standby_tv_on_pc_standby") && !m_configuration.powerOffDevices.IsEmpty())
    {
      CLog::Log(LOGDEBUG, "{} - sending standby commands", __FUNCTION__);
      m_standbySent = CDateTime::GetCurrentDateTime();
      m_cecAdapter->StandbyDevices();
    }

    if (m_bSendInactiveSource)
    {
      CLog::Log(LOGDEBUG, "{} - sending inactive source commands", __FUNCTION__);
      m_cecAdapter->SetInactiveView();
    }
  }
  else
  {
    CLog::Log(LOGDEBUG, "{} - Kodi is not the active source, not sending any unregister commands", __FUNCTION__);
  }
}

void CPeripheralCecAdapter::StandbyDevices(bool bBypassTimer /* = false */)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  m_bStandbyPending = true;
  if (bBypassTimer)
    m_ScreensaverStandbySent.SetValid(false);
  // Only set the screensaver delay timer if there's no explicit standby pending.
  // An explicit standby (CECStandby) bypasses the timer, and the screensaver's
  // own StandbyDevices() call must not re-enable it.
  else if (!m_bExplicitStandbyPending)
    m_ScreensaverStandbySent = CDateTime::GetCurrentDateTime();
}

void CPeripheralCecAdapter::ProcessStandbyDevices(void)
{
  bool bExplicitStandby(false);
  bool bSendInactiveSource(false);
  bool bHasPowerOffDevices(false);

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    if (!m_bStandbyPending)
      return;

    int iScreensaverDelay = GetSettingInt("screensaver_delay_standby");

    if ((iScreensaverDelay > 0) && m_ScreensaverStandbySent.IsValid())
      if (CDateTime::GetCurrentDateTime() - m_ScreensaverStandbySent < CDateTimeSpan(0, 0, iScreensaverDelay, 0))
        return;

    m_bStandbyPending = false;
    m_ScreensaverStandbySent.SetValid(false);
    bExplicitStandby = m_bExplicitStandbyPending;
    m_bExplicitStandbyPending = false;
    bSendInactiveSource = (m_bSendInactiveSource == 1);
    bHasPowerOffDevices = !m_configuration.powerOffDevices.IsEmpty();
  }
  // lock released — safe to call into libCEC (which fires callbacks on another thread)

  // For screensaver-triggered standbys, skip if we're not the active source.
  // For explicit standbys (user pressed a button), always proceed.
  if (!bExplicitStandby && !m_cecAdapter->IsLibCECActiveSource())
    return;

  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    m_bGoingToStandby = true;
  }

  CLog::Log(LOGDEBUG, "{} - bHasPowerOffDevices={}, bSendInactiveSource={}, bExplicitStandby={}",
            __FUNCTION__, bHasPowerOffDevices, bSendInactiveSource, bExplicitStandby);

  if (bHasPowerOffDevices)
  {
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_standbySent = CDateTime::GetCurrentDateTime();
    }
    CLog::Log(LOGDEBUG, "{} - calling StandbyDevices(CECDEVICE_BROADCAST)", __FUNCTION__);
    m_cecAdapter->StandbyDevices(CECDEVICE_BROADCAST);
    CLog::Log(LOGDEBUG, "{} - StandbyDevices completed", __FUNCTION__);
  }
  // Also mark ourselves inactive when standby commands were sent: the standby
  // broadcast never clears libcec's active-source flag, and a stale "we are
  // active source" swallows the next deliberate wake in ProcessActivateSource()
  // and flips CECToggleState the wrong way. Same sequential pattern as
  // UnregisterDevice().
  if (bSendInactiveSource)
  {
    CLog::Log(LOGDEBUG, "{} - sending inactive source commands", __FUNCTION__);
    m_cecAdapter->SetInactiveView();
  }

  // The configured action for explicit standby fires here on the CEC thread, after 
  // any queue delays and after the standby broadcast has been sent to the bus. 
  // This ensures the TV processes the standby command before Kodi shuts down.
  if (bExplicitStandby)
  {
    int iActionOnExplicitStandby = GetSettingInt("action_on_explicit_standby");
    switch (iActionOnExplicitStandby)
    {
      case LOCALISED_ID_POWEROFF:
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_SHUTDOWN);
        break;
      case LOCALISED_ID_SUSPEND:
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_SUSPEND);
        break;
      case LOCALISED_ID_HIBERNATE:
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_SYSTEM_POWERDOWN, TMSG_HIBERNATE);
        break;
      case LOCALISED_ID_QUIT:
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_QUIT);
        break;
      case LOCALISED_ID_PAUSE:
        CServiceBroker::GetAppMessenger()->PostMsg(TMSG_MEDIA_PAUSE_IF_PLAYING);
        break;
      case LOCALISED_ID_STOP:
      {
        const auto& components = CServiceBroker::GetAppComponents();
        const auto appPlayer = components.GetComponent<CApplicationPlayer>();
        if (appPlayer->IsPlaying())
          CServiceBroker::GetAppMessenger()->PostMsg(TMSG_MEDIA_STOP);
        break;
      }
      case LOCALISED_ID_IGNORE:
        break;
      default:
        CLog::Log(LOGERROR, "{} - Unexpected [action_on_explicit_standby] setting value",
                  __FUNCTION__);
        break;
    }
  }
}

bool CPeripheralCecAdapter::ToggleDeviceState(CecStateChange mode /*= STATE_SWITCH_TOGGLE */,
                                              bool forceType /*= false */)
{
  std::unique_lock<CCriticalSection> lifecycleLock(m_lifecycleSection);
  if (m_retired.load(std::memory_order_acquire) || !IsRunning())
    return false;

  // For explicit standby, skip the IsLibCECActiveSource() check entirely.
  // On fresh boot, libCEC may not have established active source yet, but the
  // user's intent is clear: they asked for standby, so just do it.
  if (mode == STATE_STANDBY)
  {
    CLog::Log(LOGDEBUG, "{} - putting CEC device on standby (explicit)...", __FUNCTION__);
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      m_bExplicitStandbyPending = true;
    }
    StandbyDevices(true);
    return false;
  }

  if (m_cecAdapter->IsLibCECActiveSource() && mode == STATE_SWITCH_TOGGLE)
  {
    CLog::Log(LOGDEBUG, "{} - putting CEC device on standby (toggle)...", __FUNCTION__);
    StandbyDevices(true);
    return false;
  }
  else if (mode == STATE_SWITCH_TOGGLE || mode == STATE_ACTIVATE_SOURCE)
  {
    CLog::Log(LOGDEBUG, "{} - waking up CEC device...", __FUNCTION__);
    ActivateSource();
    return true;
  }

  return false;
}

void CPeripheralCecAdapter::OnActionPre(const CAction& action)
{
  auto actionId = action.GetID();
  // Ignore pure analog noise and continuous scroll/drag events so we only trigger on distinct presses
  if (actionId == ACTION_MOUSE_MOVE || actionId == ACTION_NONE || actionId == ACTION_ANALOG_MOVE ||
      actionId == ACTION_MOUSE_DRAG || actionId == ACTION_MOUSE_WHEEL_UP || actionId == ACTION_MOUSE_WHEEL_DOWN)
    return;

  if (GetSettingBool("activate_on_activity") && m_cecAdapter)
  {
    auto now = std::chrono::steady_clock::now();

    // Throttle this entire check to at most once per second to prevent input processing lag
    // caused by taking locks in IsLibCECActiveSource during rapid input streams.
    if (std::chrono::duration_cast<std::chrono::milliseconds>(now - m_lastCheckTime).count() < 1000)
      return;

    m_lastCheckTime = now;

    // Fast-path guard: if a standby command is currently queued but hasn't been
    // processed yet by the CEC thread, don't queue an activate that would
    // conflict. ProcessActivateSource has its own cooldown check for the wider
    // window after standby completes.
    bool bExplicitWake = false;
    {
      std::unique_lock<CCriticalSection> lock(m_critSection);
      if (m_bStandbyPending || m_bExplicitStandbyPending)
      {
        CLog::Log(LOGDEBUG, "{} - ignoring OnActionPre because standby is pending", __FUNCTION__);
        return;
      }

      // If we're in explicit standby, the user is intentionally trying to wake.
      // Bypass all cooldowns — this is not noise, it's a deliberate wake request.
      if (m_bGoingToStandby)
      {
        bExplicitWake = true;
        m_bGoingToStandby = false;
        m_bBypassWakeCooldown = true;
      }
    }

    if (bExplicitWake)
    {
      CLog::Log(LOGDEBUG, "{} - waking from explicit standby", __FUNCTION__);
      m_lastActivateSourceTime = now;
      ActivateSource();
      return;
    }

    const auto& components = CServiceBroker::GetAppComponents();
    const auto appPower = components.GetComponent<CApplicationPowerHandling>();
    if (appPower->IsInScreenSaver())
    {
      CLog::Log(LOGDEBUG, "{} - ignoring OnActionPre because screensaver is active", __FUNCTION__);
      return;
    }

    if (std::chrono::duration_cast<std::chrono::milliseconds>(now - m_lastCecKeypressTime).count() > 1000 &&
        std::chrono::duration_cast<std::chrono::seconds>(now - m_lastSourceDeactivatedTime).count() > ACTIVATE_SOURCE_COOLDOWN)
    {
         if (std::chrono::duration_cast<std::chrono::seconds>(now - m_lastActivateSourceTime).count() >= ACTIVATE_SOURCE_COOLDOWN)
         {
           m_lastActivateSourceTime = now;
           
           // ActivateSource() safely sets a boolean under a mutex. The background CEC thread 
           // will process it and call libcec's SetActiveSource().
           // This completely decouples the input thread from libcec!
           ActivateSource();
         }
       }
    }
}
