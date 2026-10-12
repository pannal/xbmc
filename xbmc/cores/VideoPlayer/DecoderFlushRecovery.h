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
};

class CDecoderFlushRecovery
{
public:
  bool OnNoOutputTimeout()
  {
    // A local reset restarts the configured watchdog (up to 60 seconds).
    // Only output or an external state change ends a run of failed local resets.
    ++m_consecutiveFlushes;
    if (m_consecutiveFlushes < 2)
      return false;

    ClearObservation();
    return true;
  }

  void OnDecoderOutput() { ClearObservation(); }
  void OnStreamFlush() { ClearObservation(); }
  void Reset() { ClearObservation(); }

private:
  void ClearObservation() { m_consecutiveFlushes = 0; }

  unsigned int m_consecutiveFlushes{0};
};

struct CVideoRecoveryPlaybackState
{
  bool DecoderOutputBlocked() const
  {
    return normalPlayback && streamPaused && buffering && streamStarting && videoQueueFull;
  }

  bool normalPlayback{false};
  bool streamPaused{false};
  bool buffering{false};
  bool streamStarting{false};
  bool videoQueueFull{false};
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
    bool decoderOutputBlocked{false};
    bool displayAvailable{false};
    bool sourceEligible{false};
    // The demuxer reached the end of input and the tail is draining: a reseek
    // would cut, interrupt or replay the ending.
    bool endOfStream{false};
    // Current playback position, for the per-section budget.
    std::chrono::milliseconds position{0};
  };

  bool TryBegin(TimePoint now, const Conditions& conditions)
  {
    // Playback is well away from the last recovery: a new wedge is not the same damaged section.
    const bool newSection =
        std::chrono::abs(conditions.position - m_lastRecoveryPosition) >= RECOVERY_SECTION;
    if (!ConditionsEligible(conditions) || m_recoveryPending || now < m_cooldownUntil ||
        (!newSection && m_attempts >= RECOVERY_ATTEMPTS_PER_SECTION))
      return false;

    if (newSection)
      m_attempts = 0;

    m_recoveryPending = true;
    m_cooldownUntil = now + RECOVERY_COOLDOWN;
    return true;
  }

  bool CanExecute(const Conditions& conditions) const
  {
    return m_recoveryPending && ConditionsEligible(conditions);
  }

  // The recovery seek starts: only executed recoveries use up the budget,
  // so one superseded by a user seek or canceled at end of stream does not.
  void OnExecute(std::chrono::milliseconds position)
  {
    ++m_attempts;
    m_lastRecoveryPosition = position;
  }
  void CancelPending() { m_recoveryPending = false; }
  void OnFlush() { CancelPending(); }

  void Reset()
  {
    m_recoveryPending = false;
    m_cooldownUntil = {};
    m_attempts = 0;
    m_lastRecoveryPosition = {};
  }

private:
  static bool ConditionsEligible(const Conditions& conditions)
  {
    return conditions.generationMatches && !conditions.userSeekQueued && conditions.canSeek &&
           conditions.normalPlayback &&
           ((conditions.streamPlaying && conditions.cacheReady) ||
            conditions.decoderOutputBlocked) &&
           conditions.displayAvailable && conditions.sourceEligible && !conditions.endOfStream;
  }

  static constexpr std::chrono::seconds RECOVERY_COOLDOWN{60};
  // Repeated timeouts cannot tell a decoder wedge from damaged input: a reseek
  // into the same broken section must not repeat. After this many, only the
  // local reset remains (the behaviour before recovery) until playback is
  // RECOVERY_SECTION away from the last recovery, or a new stream starts.
  // The position follows the player clock during playback and the pending
  // seek target until startup sync re-anchors it, excluding EDL cuts in both.
  static constexpr unsigned int RECOVERY_ATTEMPTS_PER_SECTION{2};
  static constexpr std::chrono::minutes RECOVERY_SECTION{10};

  TimePoint m_cooldownUntil{};
  unsigned int m_attempts{0};
  std::chrono::milliseconds m_lastRecoveryPosition{0};
  bool m_recoveryPending{false};
};
