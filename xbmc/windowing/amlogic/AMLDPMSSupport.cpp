/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * See LICENSES/README.md for more information.
 */
#include "AMLDPMSSupport.h"

#include "utils/log.h"

#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#include <utility>

CAMLDPMSSupport::CAMLDPMSSupport(std::function<bool()> ready,
                               std::function<void()> woke,
                               std::string path)
  : m_ready(std::move(ready)), m_woke(std::move(woke)), m_path(std::move(path))
{
  if (access(m_path.c_str(), F_OK) != 0)
    return; // Older kernels do not provide persistent output inhibition.
  m_controlPresent = true;
  bool inhibit;
  if (!Read(inhibit) || !Write(false))
  {
    CLog::Log(LOGERROR, "Amlogic DPMS: cannot reconcile the kernel PHY inhibit at startup");
    m_initialized = false;
    return;
  }
  m_supportedModes.push_back(OFF);
}

CAMLDPMSSupport::~CAMLDPMSSupport()
{
  if (m_controlPresent && !Write(false))
    CLog::Log(LOGERROR, "Amlogic DPMS: failed to release PHY inhibit during destruction");
}

bool CAMLDPMSSupport::Read(bool& inhibit) const
{
  const int fd = open(m_path.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd < 0)
    return false;
  char value[4]{};
  ssize_t size;
  do
    size = read(fd, value, sizeof(value));
  while (size < 0 && errno == EINTR);
  close(fd);
  if ((size != 1 && size != 2) || (value[0] != '0' && value[0] != '1') ||
      (size == 2 && value[1] != '\n'))
    return false;
  inhibit = value[0] == '1';
  return true;
}

bool CAMLDPMSSupport::Write(bool inhibit) const
{
  const int fd = open(m_path.c_str(), O_WRONLY | O_CLOEXEC);
  if (fd < 0)
    return false;
  ssize_t size;
  do
    size = write(fd, inhibit ? "1\n" : "0\n", 2);
  while (size < 0 && errno == EINTR);
  const int closed = close(fd);
  bool actual;
  return size == 2 && closed == 0 && Read(actual) && actual == inhibit;
}

bool CAMLDPMSSupport::EnablePowerSaving(PowerSavingMode mode)
{
  if (!IsModeSupported(mode))
    return false;
  if (Write(true))
    return true;
  // A failed readback may follow a successful write. Keep the GUI usable and
  // attempt an idempotent release before reporting entry failure.
  if (!Write(false))
    CLog::Log(LOGERROR, "Amlogic DPMS: entry failed and PHY inhibit state is unresolved");
  return false;
}

bool CAMLDPMSSupport::DisablePowerSaving()
{
  return !m_controlPresent || Write(false);
}
