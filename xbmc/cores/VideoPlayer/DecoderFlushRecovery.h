/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <chrono>
#include <cstdint>

class CVideoRecoveryGeneration
{
public:
  // Priority messages may overtake normal stream-change messages on the video queue.
  void AdvanceTo(uint64_t generation)
  {
    if (generation > m_generation)
      m_generation = generation;
  }

  uint64_t Get() const { return m_generation; }

private:
  uint64_t m_generation{0};
};

struct CVideoSeekQueueState
{
  // Recovery seeks are tracked but deliberately never supersede user input.
  bool HasQueuedUserSeek() const { return userTimeSeeks > 0 || userChapterSeeks > 0; }

  unsigned int userTimeSeeks{0};
  unsigned int userChapterSeeks{0};
  unsigned int recoverySeeks{0};
};

class CDecoderFlushRecovery
{
public:
  using TimePoint = std::chrono::steady_clock::time_point;

  bool OnNoOutputTimeout(TimePoint now)
  {
    if (m_lastFlush == TimePoint{} || now - m_lastFlush > OBSERVATION_WINDOW)
      m_consecutiveFlushes = 1;
    else
      ++m_consecutiveFlushes;

    m_lastFlush = now;
    if (m_consecutiveFlushes < 2)
      return false;

    ClearObservation();
    return true;
  }

  void OnDecoderOutput() { ClearObservation(); }
  void OnStreamFlush() { ClearObservation(); }
  void Reset() { ClearObservation(); }

private:
  void ClearObservation()
  {
    m_lastFlush = {};
    m_consecutiveFlushes = 0;
  }

  static constexpr std::chrono::seconds OBSERVATION_WINDOW{15};

  TimePoint m_lastFlush{};
  unsigned int m_consecutiveFlushes{0};
};

class CVideoRecoveryGate
{
public:
  using TimePoint = std::chrono::steady_clock::time_point;

  struct Conditions
  {
    bool generationMatches{false};
    bool userSeekQueued{false};
    bool canSeek{false};
    bool normalPlayback{false};
    bool streamPlaying{false};
    bool cacheReady{false};
    bool displayAvailable{false};
    bool sourceEligible{false};
  };

  bool TryBegin(TimePoint now, const Conditions& conditions)
  {
    if (!ConditionsEligible(conditions) || m_recoveryPending || now < m_cooldownUntil)
      return false;

    m_recoveryPending = true;
    m_cooldownUntil = now + RECOVERY_COOLDOWN;
    return true;
  }

  bool CanExecute(const Conditions& conditions) const
  {
    return m_recoveryPending && ConditionsEligible(conditions);
  }

  void CancelPending() { m_recoveryPending = false; }
  void OnFlush() { CancelPending(); }

  void Reset()
  {
    m_recoveryPending = false;
    m_cooldownUntil = {};
  }

private:
  static bool ConditionsEligible(const Conditions& conditions)
  {
    return conditions.generationMatches && !conditions.userSeekQueued && conditions.canSeek &&
           conditions.normalPlayback && conditions.streamPlaying && conditions.cacheReady &&
           conditions.displayAvailable && conditions.sourceEligible;
  }

  static constexpr std::chrono::seconds RECOVERY_COOLDOWN{60};

  TimePoint m_cooldownUntil{};
  bool m_recoveryPending{false};
};
