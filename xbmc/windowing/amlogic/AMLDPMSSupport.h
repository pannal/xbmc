/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * See LICENSES/README.md for more information.
 */
#pragma once

#include "powermanagement/DPMSSupport.h"

#include <functional>
#include <string>

class CAMLDPMSSupport : public CDPMSSupport
{
public:
  CAMLDPMSSupport(std::function<bool()> ready,
                 std::function<void()> woke,
                 std::string path = "/sys/class/amhdmitx/amhdmitx0/phy_inhibit");
  ~CAMLDPMSSupport() override;
  bool IsInitialized() const { return m_initialized; }
  bool EnablePowerSaving(PowerSavingMode mode) override;
  bool DisablePowerSaving() override;
  bool CanEnablePowerSaving() const override { return m_ready(); }
  bool PowerSavingInterruptsAudio() const override { return true; }
  bool HasPersistentPowerSaving() const override { return m_controlPresent; }
  void OnPowerSavingDisabled() override { m_woke(); }

private:
  bool Read(bool& inhibit) const;
  bool Write(bool inhibit) const;
  const std::function<bool()> m_ready;
  const std::function<void()> m_woke;
  const std::string m_path;
  bool m_controlPresent{false};
  bool m_initialized{true};
};
