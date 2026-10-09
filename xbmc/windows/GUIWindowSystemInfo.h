/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "guilib/GUIWindow.h"

#include <string>
#include <vector>
#ifdef HAS_LIBAMCODEC
#include <chrono>
#endif

class CGUIWindowSystemInfo : public CGUIWindow
{
public:
  CGUIWindowSystemInfo(void);
  ~CGUIWindowSystemInfo(void) override;
  bool OnMessage(CGUIMessage& message) override;
  void FrameMove() override;
private:
  int  m_section;
  void ResetLabels();
  void SetControlLabel(int id, const char *format, int label, int info);
  void LoadPrivacyPolicy();
  std::vector<std::string> m_diskUsage;
  bool m_privacyPolicyLoaded{false};
#ifdef HAS_LIBAMCODEC
  void UpdateDVModuleStatus();
  std::chrono::steady_clock::time_point m_dvModuleStatusUpdated{};
  bool m_doviLoaded{false};
  bool m_dovi5Loaded{false};
  std::string m_doviModuleFolder;
  std::string m_dovi5ModuleFolder;
#endif
};

