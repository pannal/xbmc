/*
 *  Copyright (c) 2006 elupus (Joakim Plate)
 *  Copyright (C) 2006-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "cores/IPlayer.h"
#include "threads/CriticalSection.h"
#include "threads/SystemClock.h"
#include "threads/Thread.h"
#include "utils/logtypes.h"

#include <atomic>
#include <functional>
#include <memory>
#include <string>
#include <vector>

class PLT_MediaController;

namespace UPNP
{

class CUPnPCommand;
class CUPnPPlayerController;

class CUPnPPlayer : public IPlayer, public CThread
{
public:
  CUPnPPlayer(IPlayerCallback& callback, const char* uuid);
  ~CUPnPPlayer() override;

  bool OpenFile(const CFileItem& file, const CPlayerOptions& options) override;
  bool QueueNextFile(const CFileItem &file) override;
  bool CloseFile(bool reopen = false) override;
  bool IsPlaying() const override;
  void Pause() override;
  bool HasVideo() const override { return m_hasVideo; }
  bool HasAudio() const override { return m_hasAudio; }
  void Seek(bool bPlus, bool bLargeStep, bool bChapterOverride) override;
  void SeekPercentage(float fPercent = 0) override;
  void SetVolume(float volume) override;

  int SeekChapter(int iChapter) override { return -1; }

  void SeekTime(int64_t iTime = 0) override;
  void SetSpeed(float speed = 0) override;

  bool IsCaching() const override { return false; }
  int GetCacheLevel() const override { return -1; }
  bool OnAction(const CAction &action) override;

  int PlayFile(const CFileItem& file,
               const CPlayerOptions& options,
               XbmcThreads::EndTime<>& timeout,
               uint64_t open);

private:
  bool IsPaused() const;
  // Ends an open in progress; the waits of the open end
  void CancelOpen();
  int WaitOnEvent(CUPnPCommand& command, XbmcThreads::EndTime<>& timeout, uint64_t open);
  // Sends a command that changes the renderer; the close stops the renderer
  // after the answer. Returns no command when the controller did not send the command.
  std::shared_ptr<CUPnPCommand> SendCommand(const std::function<int(void* userdata)>& send);
  int64_t GetTime();
  int64_t GetTotalTime();
  float GetPercentage();

  // implementation of CThread
  void Process() override;
  void OnExit() override;

  PLT_MediaController* m_control = nullptr;
  std::unique_ptr<CUPnPPlayerController> m_delegate;
  std::string m_current_uri;
  std::string m_current_meta;
  std::atomic<bool> m_started{false};
  std::atomic<bool> m_stopremote{false};
  // the worker also calls CloseFile after the loop ends
  std::atomic<bool> m_closed{false};
  // The current open; a close advances the sequence
  std::atomic<uint64_t> m_openSequence{0};
  CCriticalSection m_waitSection;
  std::vector<CUPnPCommand*> m_waiting; // the commands that an open waits for
  // Orders the start, the controls, the events and the end of the item with the
  // close: the renderer gets no control after the Stop, and the application gets
  // no event of the item after the end of the item
  CCriticalSection m_itemSection;
  // The sent commands that change the renderer, and the end of the wait for the answers
  std::vector<std::shared_ptr<CUPnPCommand>> m_commands;
  XbmcThreads::EndTime<> m_commandTimeout;
  // The Stop of the close and the end of the wait for the answer
  std::shared_ptr<CUPnPCommand> m_stop;
  XbmcThreads::EndTime<> m_stopTimeout;
  bool m_hasVideo{false};
  bool m_hasAudio{false};
  XbmcThreads::EndTime<> m_updateTimer;

  Logger m_logger;
};

} /* namespace UPNP */
