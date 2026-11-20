/*
 *  Copyright (C) 2012-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "File.h"
#include "IFile.h"

#include <functional>

namespace XFILE
{
  class CImageFile: public IFile
  {
  public:
    CImageFile();
    ~CImageFile() override;
    bool Open(const CURL& url) override;
    bool Exists(const CURL& url) override;
    int Stat(const CURL& url, struct __stat64* buffer) override;

    ssize_t Read(void* lpBuf, size_t uiBufSize) override;
    int64_t Seek(int64_t iFilePosition, int iWhence = SEEK_SET) override;
    void Close() override;
    int64_t GetPosition() override;
    int64_t GetLength() override;
    // IOCTRL_READ_INTERRUPT reaches the fetch of an image that is not cached yet.
    int IoControl(EIoControl request, void* param) override;

  protected:
    CFile m_file;
    std::function<bool()> m_readInterrupt;
  };
}
