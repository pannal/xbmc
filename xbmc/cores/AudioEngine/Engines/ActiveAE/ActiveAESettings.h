/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "settings/lib/ISettingCallback.h"
#include "threads/CriticalSection.h"

#include <atomic>
#include <memory>
#include <string>
#include <vector>

class CSetting;
class CAEStreamInfo;
struct IntegerSettingOption;
struct StringSettingOption;

namespace ActiveAE
{
class CActiveAE;

class CActiveAESettings : public ISettingCallback
{
public:
  CActiveAESettings(CActiveAE &ae);
  ~CActiveAESettings() override;

  void OnSettingChanged(const std::shared_ptr<const CSetting>& setting) override;
  void OnSettingAction(const std::shared_ptr<const CSetting>& setting) override;

  static void SettingOptionsAudioDevicesFiller(const std::shared_ptr<const CSetting>& setting,
                                               std::vector<StringSettingOption>& list,
                                               std::string& current,
                                               void* data);
  static void SettingOptionsAudioDevicesPassthroughFiller(
      const std::shared_ptr<const CSetting>& setting,
      std::vector<StringSettingOption>& list,
      std::string& current,
      void* data);
  static void SettingOptionsAudioQualityLevelsFiller(const std::shared_ptr<const CSetting>& setting,
                                                     std::vector<IntegerSettingOption>& list,
                                                     int& current,
                                                     void* data);
  static void SettingOptionsAudioStreamsilenceFiller(const std::shared_ptr<const CSetting>& setting,
                                                     std::vector<IntegerSettingOption>& list,
                                                     int& current,
                                                     void* data);
  static void SettingOptionsBluetoothCodecsFiller(const std::shared_ptr<const CSetting>& setting,
                                                   std::vector<StringSettingOption>& list,
                                                   std::string& current,
                                                   void* data);
  static bool IsSettingVisible(const std::string& condition,
                               const std::string& value,
                               const std::shared_ptr<const CSetting>& setting,
                               void* data);

protected:
  static void SettingOptionsAudioDevicesFillerGeneral(
      const std::shared_ptr<const CSetting>& setting,
      std::vector<StringSettingOption>& list,
      std::string& current,
      bool passthrough);

  CActiveAE &m_audioEngine;
  CCriticalSection m_cs;
  /*!
   * \brief React to the binaural response being switched.
   *
   * Choosing a custom HRTF set or a measured room asks for the file there and
   * then, because either with no file is not a state worth keeping, and a
   * cancel or a refused file returns to the response that was in use. Choosing
   * the built-in set empties both file controls; what the cache keeps stays.
   * All of those are settings writes that re-enter this class, so it is
   * called outside the lock.
   */
  static void OnOmniphonyHrtfModeChanged(int mode);

  /*!
   * \brief Ask for an HRTF set or, with \p room, a room-response file, check
   * it and keep it in the cache (COmniphonyHrtf::Choose, COmniphonyRoom::Choose)
   * and say what came of it. The same file chosen again is checked against
   * what is kept for it, which is why the file buttons are actions rather
   * than Kodi's own file control, which says nothing when the value stays.
   *
   * \return true when the file is kept and set
   */
  static bool ChooseFile(bool room);

  /*!
   * \brief Keep passthrough and binaural rendering from both being on.
   *
   * They are answers to the same question - where the sound is going - and only
   * one of them can be true: passthrough sends the bitstream to an amplifier to
   * decode, and binaural rendering makes a two-channel image for headphones. A
   * listener who has one is not using the other.
   *
   * Turning either on therefore turns the other off, rather than leaving it on
   * and inert. Greying the loser out would hide the reason and leave a toggle
   * nobody could explain; letting the value move says what happened, on screen,
   * at the moment it happens - the open settings dialog redraws the control
   * because CGUIDialogSettingsBase listens for exactly this.
   *
   * \param changedId the setting that was just written, which must be one of
   *                  the two
   */
  static void EnforceExclusiveOutput(const std::string& changedId);

  static CActiveAESettings* m_instance;
};
};
