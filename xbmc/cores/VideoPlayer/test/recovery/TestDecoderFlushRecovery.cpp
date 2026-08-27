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
using TimePoint = CDecoderFlushRecovery::TimePoint;
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
} // namespace

TEST(TestDecoderFlushRecovery, RequestsReseekAfterSecondConsecutiveFlush)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout(START));
  EXPECT_TRUE(recovery.OnNoOutputTimeout(START + 5s));
}

TEST(TestDecoderFlushRecovery, PicturePreventsIsolatedFlushesFromAccumulating)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout(START));
  recovery.OnDecoderOutput();
  EXPECT_FALSE(recovery.OnNoOutputTimeout(START + 5s));
}

TEST(TestDecoderFlushRecovery, OldFlushStartsANewObservationWindow)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout(START));
  EXPECT_FALSE(recovery.OnNoOutputTimeout(START + 16s));
}

TEST(TestDecoderFlushRecovery, ObservationWindowIncludesExactBoundary)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout(START));
  EXPECT_TRUE(recovery.OnNoOutputTimeout(START + 15s));
}

TEST(TestDecoderFlushRecovery, StreamFlushStartsANewObservation)
{
  CDecoderFlushRecovery recovery;

  EXPECT_FALSE(recovery.OnNoOutputTimeout(START));
  recovery.OnStreamFlush();
  EXPECT_FALSE(recovery.OnNoOutputTimeout(START + 5s));
  EXPECT_TRUE(recovery.OnNoOutputTimeout(START + 10s));
}

TEST(TestVideoRecoveryGate, AcceptsMatchingEligibleRequest)
{
  CVideoRecoveryGate gate;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
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
  EXPECT_FALSE(detector.OnNoOutputTimeout(START));
  EXPECT_TRUE(detector.OnNoOutputTimeout(START + 5s));

  auto conditions = EligibleConditions();
  conditions.generationMatches = generation.Get() == 11;
  EXPECT_TRUE(gate.TryBegin(START + 5s, conditions));
}

TEST(TestVideoRecoveryOrdering, UserTimeSeekWinsRecoveryEnqueueRace)
{
  CVideoRecoveryGate gate;
  CVideoSeekQueueState queuedAfterUserSeekDequeued;
  queuedAfterUserSeekDequeued.recoverySeeks = 1;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  EXPECT_FALSE(queuedAfterUserSeekDequeued.HasQueuedUserSeek());
  gate.CancelPending();
  EXPECT_FALSE(gate.CanExecute(EligibleConditions()));
}

TEST(TestVideoRecoveryOrdering, UserChapterSeekWinsRecoveryEnqueueRace)
{
  CVideoRecoveryGate gate;
  CVideoSeekQueueState queuedAfterChapterSeekDequeued;
  queuedAfterChapterSeekDequeued.recoverySeeks = 1;

  EXPECT_TRUE(gate.TryBegin(START, EligibleConditions()));
  EXPECT_FALSE(queuedAfterChapterSeekDequeued.HasQueuedUserSeek());
  gate.CancelPending();
  EXPECT_FALSE(gate.CanExecute(EligibleConditions()));
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
  afterFirstChapter.recoverySeeks = 1;
  EXPECT_TRUE(afterFirstChapter.HasQueuedUserSeek());

  CVideoSeekQueueState afterSecondChapter;
  afterSecondChapter.userTimeSeeks = 1;
  afterSecondChapter.recoverySeeks = 1;
  EXPECT_TRUE(afterSecondChapter.HasQueuedUserSeek());

  CVideoSeekQueueState afterTimeSeek;
  afterTimeSeek.recoverySeeks = 1;
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
