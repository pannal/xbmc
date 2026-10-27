/*
 *  Copyright (C) 2005-2024 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#if !defined(HAVE_LIBCEC)
#include "Peripheral.h"

namespace PERIPHERALS
{
/*!
 * \ingroup peripherals
 *
 * An empty implementation, so CPeripherals can be compiled without a bunch of
 * #ifdef's when libCEC is not available.
 */
class CPeripheralCecAdapter : public CPeripheral
{
public:
  bool HasAudioControl(void) { return false; }
  bool VolumeUp(void) { return false; }
  bool VolumeDown(void) { return false; }
  bool IsMuted(void) { return false; }
  bool ToggleMute(void) { return false; }
  bool ToggleDeviceState(CecStateChange mode = STATE_SWITCH_TOGGLE, bool forceType = false)
  {
    return false;
  }

  int GetButton(void) { return 0; }
  unsigned int GetHoldTime(void) { return 0; }
  void ResetButton(void) {}
};
} // namespace PERIPHERALS

#else

#include "PeripheralHID.h"
#include "XBDateTime.h"
#include "input/actions/interfaces/IActionListener.h"
#include "interfaces/AnnouncementManager.h"
#include "threads/CriticalSection.h"
#include "threads/Thread.h"

#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <queue>
#include <vector>

// undefine macro isset, it collides with function in cectypes.h
#ifdef isset
#undef isset
#endif
#include <libcec/cectypes.h>

class CVariant;

namespace CEC
{
class ICECAdapter;
};

namespace PERIPHERALS
{
class CPeripheralCecAdapterUpdateThread;
class CPeripheralCecAdapterReopenJob;

typedef struct
{
  int iButton;
  unsigned int iDuration;
} CecButtonPress;

typedef enum
{
  VOLUME_CHANGE_NONE,
  VOLUME_CHANGE_UP,
  VOLUME_CHANGE_DOWN,
  VOLUME_CHANGE_MUTE
} CecVolumeChange;

// CEC Wake Up flags from u-boot(bl301)
typedef enum
{
  CEC_FUNC_MASK = 0,
  AUTO_POWER_ON_MASK = 3,
  STREAMPATH_POWER_ON_MASK = 4,
  ACTIVE_SOURCE_MASK = 6
} CecWakeUpMask;

class CPeripheralCecAdapter : public CPeripheralHID,
                              public ANNOUNCEMENT::IAnnouncer,
                              public KODI::ACTION::IActionListener,
                              public std::enable_shared_from_this<CPeripheralCecAdapter>,
                              private CThread
{
  friend class CPeripheralCecAdapterUpdateThread;
  friend class CPeripheralCecAdapterReopenJob;

public:
  CPeripheralCecAdapter(CPeripherals& manager,
                        const PeripheralScanResult& scanResult,
                        CPeripheralBus* bus);
  ~CPeripheralCecAdapter() override;

  void Deinitialize(void) override;

  void Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                const std::string& sender,
                const std::string& message,
                const CVariant& data) override;

  bool OnAction(const CAction& action) override { return false; }
  void OnActionPre(const CAction& action) override;

  // audio control
  bool HasAudioControl(void);
  CDateTime m_ScreensaverStandbySent;
  bool VolumeUp(void);
  bool VolumeDown(void);
  bool ToggleMute(void);
  bool IsMuted(void);

  // CPeripheral callbacks
  void OnSettingChanged(const std::string& strChangedSetting) override;
  void OnDeviceRemoved(void) override;

  // input
  int GetButton(void);
  unsigned int GetHoldTime(void);
  void ResetButton(void);

  // public CEC methods
  void ActivateSource(void);
  void UnregisterDevice(void);
  void StandbyDevices(bool bBypassTimer = false);
  bool ToggleDeviceState(CecStateChange mode = STATE_SWITCH_TOGGLE, bool forceType = false);

private:
  bool InitialiseFeature(const PeripheralFeature feature) override;
  void ResetMembers(void);
  void RetireCecCallbacks(void);
  void OnCecSourceActivated(CEC::cec_logical_address address,
                            uint8_t activated,
                            uint64_t generation);
  void Process(void) override;
  bool IsRunning(void) const;

  bool OpenConnection(void);
  bool ReopenConnection(bool bAsync = false, uint64_t generation = 0);

  void SetConfigurationFromSettings(void);
  void SetConfigurationFromLibCEC(const CEC::libcec_configuration& config);
  void SetVersionInfo(const CEC::libcec_configuration& configuration);

  bool LoadCecFuncConfig(void);
  void SetWakeupBitsFromSettings(void);

  static void ReadLogicalAddresses(const std::string& strString,
                                   CEC::cec_logical_addresses& addresses);
  static void ReadLogicalAddresses(int iLocalisedId, CEC::cec_logical_addresses& addresses);
  bool WriteLogicalAddresses(const CEC::cec_logical_addresses& addresses,
                             const std::string& strSettingName,
                             const std::string& strAdvancedSettingName);

  void ProcessActivateSource(void);
  void ProcessStandbyDevices(void);
  void ProcessVolumeChange(bool stopping = false);
  bool QueueVolumeChange(CecVolumeChange change);
  bool HasAudioControlLocked(void) const;
  void SetTvVolumeControl(bool enabled);
  void OnVolumeControlConnected(CEC::cec_logical_address target,
                                uint64_t volumeGeneration,
                                uint64_t sourceGeneration);
  void InvalidateVolumeControl(void);
  void ResetVolumeControl(void);
  bool ReleaseVolumeKey(std::unique_lock<CCriticalSection>& lock);
  bool SuppressCecVolumeKey(CEC::cec_user_control_code key) const;

  void PushCecKeypress(const CEC::cec_keypress& key);
  void PushCecKeypress(const CecButtonPress& key);
  void GetNextKey(void);
  void ProcessInactiveView(void);

  void SetMenuLanguage(const char* strLanguage);
  void OnTvStandby(void);

  // callbacks from libCEC
  static void CecLogMessage(void* cbParam, const CEC::cec_log_message* message);
  static void CecCommand(void* cbParam, const CEC::cec_command* command);
  static void CecConfiguration(void* cbParam, const CEC::libcec_configuration* config);
  static void CecAlert(void* cbParam,
                       const CEC::libcec_alert alert,
                       const CEC::libcec_parameter data);
  static void CecSourceActivated(void* param,
                                 const CEC::cec_logical_address address,
                                 const uint8_t activated);
  static void CecKeyPress(void* cbParam, const CEC::cec_keypress* key);

  CEC::ICECAdapter* m_cecAdapter;
  bool m_bStarted;
  bool m_bHasButton;
  bool m_bIsReady;
  std::string m_strMenuLanguage;
  CDateTime m_standbySent;
  std::vector<CecButtonPress> m_buttonQueue;
  CecButtonPress m_currentButton;
  std::queue<CecVolumeChange> m_volumeChangeQueue;
  // Only the adapter worker sends keys. This lock protects brief admission/state
  // updates and is always released across native sends; callbacks never take it.
  mutable CCriticalSection m_volumeSection;
  std::atomic<bool> m_volumeEnabled{false};
  std::atomic<bool> m_tvVolumeControl{false};
  std::atomic<bool> m_volumeConnectionLost{false};
  CEC::cec_logical_address m_volumeTarget{CEC::CECDEVICE_UNKNOWN};
  CEC::cec_logical_address m_pressedVolumeTarget{CEC::CECDEVICE_UNKNOWN};
  uint64_t m_volumeGeneration{0};
  uint64_t m_pressedVolumeGeneration{0};
  unsigned int m_volumeReleaseFailures{0};
  bool m_audioNormalizationPending{false};
  std::chrono::time_point<std::chrono::steady_clock> m_lastKeypress;
  std::chrono::time_point<std::chrono::steady_clock> m_lastVolumeInput;
  CecVolumeChange m_lastChange;
  int m_iExitCode;
  bool m_bIsMuted;
  bool m_bGoingToStandby;
  bool m_bBypassWakeCooldown;
  bool m_bIsRunning;
  std::atomic<bool> m_bDeviceRemoved{false};
  CPeripheralCecAdapterUpdateThread* m_queryThread;
  CEC::ICECCallbacks m_callbacks;
  // Only lifecycle owners take this lock; native callbacks must remain able to drain.
  CCriticalSection m_lifecycleSection;
  std::atomic<bool> m_retired{false};
  bool m_deinitialized{false};
  mutable CCriticalSection m_critSection;
  CEC::libcec_configuration m_configuration;
  bool m_bActiveSourcePending;
  bool m_bStandbyPending;
  bool m_bExplicitStandbyPending;
  bool m_bInactiveViewPending;
  CDateTime m_preventActivateSourceOnPlay;
  CDateTime m_tvStandbyReceived;
  bool m_bActiveSourceBeforeStandby;
  bool m_bOnPlayReceived;
  bool m_bPlaybackPaused;
  std::atomic<uint64_t> m_sourceEventGeneration{0};
  std::string m_strComPort;
  std::chrono::time_point<std::chrono::steady_clock> m_lastSourceDeactivatedTime;
  std::chrono::time_point<std::chrono::steady_clock> m_lastCecKeypressTime;
  std::chrono::time_point<std::chrono::steady_clock> m_lastActivateSourceTime;
  std::chrono::time_point<std::chrono::steady_clock> m_lastCheckTime;
  bool m_bPowerOnScreensaver;
  bool m_bPowerOffScreensaverPaused;
  bool m_bUseTVMenuLanguage;
  bool m_bSendInactiveSource;
  bool m_bPowerOffScreensaver;
  bool m_bShutdownOnStandby;
  int m_iCec_func_config = 0;
  bool m_bFuncConfigLoaded = false;
};

class CPeripheralCecAdapterUpdateThread : public CThread
{
public:
  CPeripheralCecAdapterUpdateThread(CPeripheralCecAdapter* adapter,
                                    CEC::libcec_configuration* configuration);
  ~CPeripheralCecAdapterUpdateThread(void) override;

  void RefreshAudioControl(void);
  bool UpdateConfiguration(CEC::libcec_configuration* configuration);

protected:
  void UpdateMenuLanguage(void);
  std::string UpdateAudioSystemStatus(void);
  bool WaitReady(void);
  bool SetInitialConfiguration(void);
  void Process(void) override;

  CPeripheralCecAdapter* m_adapter;
  CEvent m_event;
  CCriticalSection m_critSection;
  CEC::libcec_configuration m_configuration;
  CEC::libcec_configuration m_nextConfiguration;
  bool m_bNextConfigurationScheduled = false;
  bool m_bIsUpdating = true;
  std::atomic<bool> m_audioControlRefreshPending{false};
};
} // namespace PERIPHERALS

#endif
