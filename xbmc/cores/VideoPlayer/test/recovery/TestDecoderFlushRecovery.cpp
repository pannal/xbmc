/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDMessage.h"
#include "cores/VideoPlayer/DecoderFlushRecovery.h"

#include <chrono>

#include <gtest/gtest.h>

using namespace std::chrono_literals;

namespace
{
using TimePoint = CVideoRecoveryGate::TimePoint;
constexpr TimePoint START = TimePoint{} + 1s;

CVideoRecoveryGate::Conditions EligibleConditions()
{
  CVideoRecoveryGate::Conditions conditions;
  conditions.generationMatches = true;
  conditions.canSeek = true;
  conditions.normalPlayback = true;
  conditions.streamPlaying = true;
  conditions.cacheReady = true;
  conditions.displayAvailable = true;
  conditions.sourceEligible = true;
  return conditions;
}

CVideoRecoveryPlaybackState BlockedStartup()
{
  CVideoRecoveryPlaybackState state;
  state.normalPlayback = true;
  state.streamPaused = true;
  state.buffering = true;
  state.streamStarting = true;
  state.videoQueueFull = true;
  return state;
}
} // namespace

TEST(TestDecoderFlushRecovery, RequestsReseekAfterSecondConsecutiveFlush)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  EXPECT_TRUE(recovery.OnNoOutputTimeout());
}

TEST(TestDecoderFlushRecovery, PicturePreventsIsolatedFlushesFromAccumulating)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  recovery.OnDecoderOutput();
  EXPECT_FALSE(recovery.OnNoOutputTimeout());
}

TEST(TestDecoderFlushRecovery, FailedLocalResetWaitsForOutputOrExternalReset)
{
  CDecoderFlushRecovery recovery;
  // No wall-clock window: this also covers 20/60-second watchdogs plus reset overhead.
  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  EXPECT_TRUE(recovery.OnNoOutputTimeout());
  // Escalation starts a fresh pair of local-reset observations.
  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  EXPECT_TRUE(recovery.OnNoOutputTimeout());
}

TEST(TestDecoderFlushRecovery, ExternalResetStartsANewObservation)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  recovery.Reset();
  EXPECT_FALSE(recovery.OnNoOutputTimeout());
}

TEST(TestDecoderFlushRecovery, StreamFlushStartsANewObservation)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  recovery.OnStreamFlush();
  EXPECT_FALSE(recovery.OnNoOutputTimeout());
  EXPECT_TRUE(recovery.OnNoOutputTimeout());
}

TEST(TestVideoRecoveryPlaybackState, RecognizesDecoderBackpressureBeforeFirstPostSeekFrame)
{
  EXPECT_TRUE(BlockedStartup().DecoderOutputBlocked());
}

TEST(TestVideoRecoveryPlaybackState, RejectsIntentionalPauseTrickplayAndSourceStarvation)
{
  auto state = BlockedStartup();
  state.normalPlayback = false;
  EXPECT_FALSE(state.DecoderOutputBlocked());

  state = BlockedStartup();
  state.streamPaused = false;
  EXPECT_FALSE(state.DecoderOutputBlocked());

  state = BlockedStartup();
  state.buffering = false;
  EXPECT_FALSE(state.DecoderOutputBlocked());

  state = BlockedStartup();
  state.streamStarting = false;
  EXPECT_FALSE(state.DecoderOutputBlocked());

  state = BlockedStartup();
  state.videoQueueFull = false;
  EXPECT_FALSE(state.DecoderOutputBlocked());
}

TEST(TestVideoRecoveryGate, AcceptsMatchingEligibleRequest)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, AcceptsAndExecutesBlockedStartupRecovery)
{
  CVideoRecoveryGate gate;
  auto conditions = EligibleConditions();
  conditions.streamPlaying = false;
  conditions.cacheReady = false;
  conditions.decoderOutputBlocked = BlockedStartup().DecoderOutputBlocked();

  EXPECT_TRUE(gate.TryBegin(START, conditions));
  EXPECT_TRUE(gate.CanExecute(conditions));

  // A source that has run dry while the seek was queued must not be recovered.
  auto state = BlockedStartup();
  state.videoQueueFull = false;
  conditions.decoderOutputBlocked = state.DecoderOutputBlocked();
  EXPECT_FALSE(gate.CanExecute(conditions));
}

TEST(TestVideoRecoveryGate, BlockedStartupStillYieldsToPauseAndUserSeek)
{
  CVideoRecoveryGate gate;
  auto conditions = EligibleConditions();
  conditions.streamPlaying = false;
  conditions.cacheReady = false;
  conditions.decoderOutputBlocked = BlockedStartup().DecoderOutputBlocked();
  EXPECT_TRUE(gate.TryBegin(START, conditions));

  conditions.normalPlayback = false;
  EXPECT_FALSE(gate.CanExecute(conditions));
  conditions.normalPlayback = true;
  conditions.userSeekQueued = true;
  EXPECT_FALSE(gate.CanExecute(conditions));
  conditions.userSeekQueued = false;
  conditions.generationMatches = false;
  EXPECT_FALSE(gate.CanExecute(conditions));
  conditions.generationMatches = true;
  conditions.displayAvailable = false;
  EXPECT_FALSE(gate.CanExecute(conditions));
  conditions.displayAvailable = true;
  conditions.sourceEligible = false;
  EXPECT_FALSE(gate.CanExecute(conditions));
}

TEST(TestVideoRecoveryGate, RejectsStaleGeneration)
{
  CVideoRecoveryGate gate;
  auto conditions = EligibleConditions();
  conditions.generationMatches = false;

  EXPECT_FALSE(gate.TryBegin(START, conditions));
}

TEST(TestVideoRecoveryGate, GivesQueuedUserSeekPrecedence)
{
  CVideoRecoveryGate gate;
  auto conditions = EligibleConditions();
  conditions.userSeekQueued = true;

  EXPECT_FALSE(gate.TryBegin(START, conditions));
}

TEST(TestVideoRecoveryGate, RejectsIneligiblePlaybackStates)
{
  CVideoRecoveryGate gate;

  auto conditions = EligibleConditions();
  conditions.canSeek = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  conditions = EligibleConditions();
  conditions.normalPlayback = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  conditions = EligibleConditions();
  conditions.streamPlaying = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  conditions = EligibleConditions();
  conditions.cacheReady = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  conditions = EligibleConditions();
  conditions.displayAvailable = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  conditions = EligibleConditions();
  conditions.sourceEligible = false;
  EXPECT_FALSE(gate.TryBegin(START, conditions));
}

TEST(TestVideoRecoveryGate, RejectsRecoveryAtEndOfStream)
{
  CVideoRecoveryGate gate;

  auto conditions = EligibleConditions();
  conditions.endOfStream = true;
  EXPECT_FALSE(gate.TryBegin(START, conditions));

  // Blocked startup recovery is still refused once the tail drains.
  conditions = CVideoRecoveryGate::Conditions{};
  conditions.generationMatches = true;
  conditions.canSeek = true;
  conditions.normalPlayback = true;
  conditions.decoderOutputBlocked = BlockedStartup().DecoderOutputBlocked();
  conditions.displayAvailable = true;
  conditions.sourceEligible = true;
  EXPECT_TRUE(conditions.decoderOutputBlocked);
  conditions.endOfStream = true;
  EXPECT_FALSE(gate.TryBegin(START, conditions));
  conditions.endOfStream = false;
  EXPECT_TRUE(gate.TryBegin(START, conditions));
}

TEST(TestVideoRecoveryGate, AdmittedRecoveryDoesNotExecuteAtEndOfStream)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  auto conditions = EligibleConditions();
  conditions.endOfStream = true;
  EXPECT_FALSE(gate.CanExecute(conditions));
}

TEST(TestVideoRecoveryGate, RejectsDuplicateUntilRecoveryFlushRuns)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  EXPECT_FALSE(gate.TryBegin(START + 1s, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, CooldownSurvivesRecoveryFlush)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  gate.OnFlush();
  EXPECT_FALSE(gate.TryBegin(START + 59s, EligibleConditions()));
  EXPECT_TRUE(gate.TryBegin(START + 60s, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, LimitsRecoveriesPerStream)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  gate.OnExecute();
  gate.OnFlush();
  EXPECT_TRUE(gate.TryBegin(START + 60s, EligibleConditions()));
  gate.OnExecute();
  gate.OnFlush();
  // Damaged input re-reached after each reseek: no third automatic recovery.
  EXPECT_FALSE(gate.TryBegin(START + 120s, EligibleConditions()));
  EXPECT_FALSE(gate.TryBegin(START + 600s, EligibleConditions()));

  // A replacement stream gets a fresh budget.
  gate.Reset();
  EXPECT_TRUE(gate.TryBegin(START + 601s, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, CanceledRecoveriesKeepTheBudget)
{
  CVideoRecoveryGate gate;

  // Superseded by user seeks or canceled at end of stream: none of them ran.
  for (int i = 0; i < 4; ++i)
  {
    EXPECT_TRUE(gate.TryBegin(START + i * 60s, EligibleConditions()));
    gate.CancelPending();
  }
  EXPECT_TRUE(gate.TryBegin(START + 240s, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, NewStreamClearsCooldown)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  gate.Reset();

  EXPECT_TRUE(gate.TryBegin(START + 1s, EligibleConditions()));
}

TEST(TestVideoRecoveryGate, RevalidatesRecoveryAtExecution)
{
  CVideoRecoveryGate gate;

  EXPECT_FALSE(gate.CanExecute(EligibleConditions()));

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  EXPECT_TRUE(gate.CanExecute(EligibleConditions()));

  auto conditions = EligibleConditions();
  conditions.generationMatches = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.userSeekQueued = true;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.canSeek = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.normalPlayback = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.streamPlaying = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.cacheReady = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.displayAvailable = false;
  EXPECT_FALSE(gate.CanExecute(conditions));

  conditions = EligibleConditions();
  conditions.sourceEligible = false;
  EXPECT_FALSE(gate.CanExecute(conditions));
}

TEST(TestVideoRecoveryGate, CancelPendingPreservesCooldown)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  gate.CancelPending();

  EXPECT_FALSE(gate.CanExecute(EligibleConditions()));
  EXPECT_FALSE(gate.TryBegin(START + 59s, EligibleConditions()));
  EXPECT_TRUE(gate.TryBegin(START + 60s, EligibleConditions()));
}

TEST(TestVideoRecoveryGeneration, PriorityFlushCannotBeRegressedByOlderStreamChange)
{
  CVideoRecoveryGeneration generation;

  generation.AdvanceTo(10);
  generation.AdvanceTo(11);
  generation.AdvanceTo(10);

  EXPECT_EQ(11u, generation.Get());
}

TEST(TestVideoRecoveryGeneration, InterleavedUpdatesRetainNewestGeneration)
{
  CVideoRecoveryGeneration generation;

  generation.AdvanceTo(4);
  generation.AdvanceTo(7);
  generation.AdvanceTo(5);
  generation.AdvanceTo(9);
  generation.AdvanceTo(8);

  EXPECT_EQ(9u, generation.Get());
}

TEST(TestVideoRecoveryGeneration, NewestGenerationCanRequestRecovery)
{
  CVideoRecoveryGeneration generation;
  CDecoderFlushRecovery detector;
  CVideoRecoveryGate gate;

  generation.AdvanceTo(10);
  generation.AdvanceTo(11);
  generation.AdvanceTo(10);
  EXPECT_FALSE(detector.OnNoOutputTimeout());
  EXPECT_TRUE(detector.OnNoOutputTimeout());

  auto conditions = EligibleConditions();
  conditions.generationMatches = generation.Get() == 11;
  EXPECT_TRUE(gate.TryBegin(START + 5s, conditions));
}

TEST(TestVideoRecoveryOrdering, RecoveryYieldsToQueuedUserSeek)
{
  CVideoSeekQueueState timeSeekQueued;
  timeSeekQueued.userTimeSeeks = 1;
  EXPECT_TRUE(timeSeekQueued.HasQueuedUserSeek());

  CVideoSeekQueueState chapterSeekQueued;
  chapterSeekQueued.userChapterSeeks = 1;
  EXPECT_TRUE(chapterSeekQueued.HasQueuedUserSeek());
}

TEST(TestVideoRecoveryOrdering, MixedChapterTimeSequenceRetainsLastUserOperation)
{
  CVideoSeekQueueState afterFirstChapter;
  afterFirstChapter.userChapterSeeks = 1;
  afterFirstChapter.userTimeSeeks = 1;
  EXPECT_TRUE(afterFirstChapter.HasQueuedUserSeek());

  CVideoSeekQueueState afterSecondChapter;
  afterSecondChapter.userTimeSeeks = 1;
  EXPECT_TRUE(afterSecondChapter.HasQueuedUserSeek());

  CVideoSeekQueueState afterTimeSeek;
  EXPECT_FALSE(afterTimeSeek.HasQueuedUserSeek());
}

TEST(TestVideoRecoveryOrdering, RecoverySeekUsesDistinctMessageType)
{
  CDVDMsgPlayerSeek::CMode userMode;
  CDVDMsgPlayerSeek userSeek(userMode);
  EXPECT_TRUE(userSeek.IsType(CDVDMsg::PLAYER_SEEK));
  EXPECT_FALSE(userSeek.IsType(CDVDMsg::PLAYER_VIDEO_RECOVERY_SEEK));

  CDVDMsgPlayerSeek::CMode recoveryMode;
  recoveryMode.videoRecovery = true;
  CDVDMsgPlayerSeek recoverySeek(recoveryMode);
  EXPECT_FALSE(recoverySeek.IsType(CDVDMsg::PLAYER_SEEK));
  EXPECT_TRUE(recoverySeek.IsType(CDVDMsg::PLAYER_VIDEO_RECOVERY_SEEK));
}
