/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

// FileFactory1.h: interface for the CFileFactory class.
//
//////////////////////////////////////////////////////////////////////

#include "IFile.h"

#include <functional>
#include <string>

namespace XFILE
{
class CFileFactory
{
public:
  CFileFactory();
  virtual ~CFileFactory();
  static IFile* CreateLoader(const std::string& strFileName);
  // interrupt: optional read interrupt, also for a wake-on-access wait before the file opens
  static IFile* CreateLoader(const CURL& url, const std::function<bool()>& interrupt = {});
  // A VFS add-on serves the URL's protocol; CreateLoader picks it before the built-in backends.
  static bool IsVFSAddonProtocol(const CURL& url);
};
}
