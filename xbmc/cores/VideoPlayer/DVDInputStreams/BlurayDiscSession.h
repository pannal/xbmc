/*
 *  This file is part of Kodi - https://kodi.tv
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include "BlurayIsoCache.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <string>

extern "C"
{
#include <libbluray/bluray.h>
#include <libbluray/overlay.h>
}

class CFileItem;
class CDVDInputStreamBluray;
class CDVDInputStreamFile;

// Stable callback context, owned until libbluray/Java and ISO prefetch finish.
// Only the overlay gate refers to the input; logical close removes that owner.
class CBlurayDiscSession
{
public:
  explicit CBlurayDiscSession(CDVDInputStreamBluray* owner, uint64_t diagnostic);
  ~CBlurayDiscSession();
  BLURAY* OpenNative();
  bool OpenStream(CFileItem& item, CBlurayIsoCache::Config config);
  int ReadBlocks(uint8_t* buffer, int lba, int blocks);
  void ResetAccessPattern();
  void Cancel();
  void DetachOwner();
  void Overlay(const BD_OVERLAY* overlay);
#ifdef HAVE_LIBBLURAY_BDJ
  void OverlayARGB(const bd_argb_overlay_s* overlay);
#endif
  void CloseNative();

  // One playback session at a time: libbluray's Java state is process-wide.
  // Retire queues an entire context; rejection closes synchronously outside
  // queue locks. Shutdown polls real completion before any service teardown.
  static void Retire(std::shared_ptr<CBlurayDiscSession> session);
  static bool Initialize();
  static bool Shutdown();

  std::string root;

private:
  int64_t ReadAt(int64_t offset, uint8_t* buffer, size_t bytes);
  CDVDInputStreamBluray* m_owner;
  std::mutex m_overlayMutex;
  std::mutex m_ioMutex;
  std::unique_ptr<CDVDInputStreamFile> m_stream;
  std::shared_ptr<CBlurayIsoCache> m_cache;
  std::atomic_bool m_cancelled{false};
  BLURAY* m_bd{nullptr};
  bool m_admitted{false};
  const uint64_t m_diagnostic;
};
