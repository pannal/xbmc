/*
 *  Copyright (C) 2005-2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "commons/Exception.h"
#include "interfaces/legacy/CallbackFunction.h"
#include "interfaces/legacy/CallbackHandler.h"

#include <atomic>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

using namespace XBMCAddon;

namespace
{
XBMCCOMMONS_STANDARD_EXCEPTION(TestCallbackException);

// Stands in for a language thread state: one distinct address per owner.
struct Owner
{
  int unused = 0;
};

class TestTarget : public AddonClass
{
public:
  void Retire() { deallocating(); }
};

class TestHandler : public RetardedAsyncCallbackHandler
{
public:
  explicit TestHandler(const Owner& owner) : RetardedAsyncCallbackHandler(&owner) {}

  // Mirrors PythonCallbackHandler: run only for the owner, remove by the owner.
  bool isStateOk(AddonClass*) override
  {
    ++stateChecks;
    return stateOk;
  }
  bool shouldRemoveCallback(AddonClass*, void* userData) override { return userData == getOwner(); }

  std::atomic<bool> stateOk{true};
  std::atomic<int> stateChecks{0};
};

class TestCallback : public Callback
{
public:
  TestCallback(AddonClass* target, std::function<void()> run, std::function<void()> release = {})
    : Callback(target), m_run(std::move(run)), m_release(std::move(release))
  {
  }
  ~TestCallback() override
  {
    if (m_release)
      m_release();
  }

  void executeCallback() override { m_run(); }

private:
  std::function<void()> m_run;
  std::function<void()> m_release;
};

class TestCallbackHandler : public testing::Test
{
protected:
  void TearDown() override
  {
    // The queue is process-wide; leave nothing behind for the next test.
    Clear(m_ownerA);
    Clear(m_ownerB);
  }

  void Queue(TestHandler& handler, std::function<void()> run, std::function<void()> release = {})
  {
    handler.invokeCallback(new TestCallback(m_target.get(), std::move(run), std::move(release)));
  }

  std::function<void()> Record(const std::string& name)
  {
    return [this, name] { m_calls.push_back(name); };
  }

  static void Drain(const Owner& owner) { RetardedAsyncCallbackHandler::makePendingCalls(&owner); }
  static void Clear(Owner& owner) { RetardedAsyncCallbackHandler::clearPendingCalls(&owner); }

  Owner m_ownerA;
  Owner m_ownerB;
  AddonClass::Ref<TestTarget> m_target{new TestTarget};
  AddonClass::Ref<TestHandler> m_handlerA{new TestHandler(m_ownerA)};
  AddonClass::Ref<TestHandler> m_handlerB{new TestHandler(m_ownerB)};
  std::vector<std::string> m_calls;
};
} // namespace

TEST_F(TestCallbackHandler, DrainRunsOnlyTheOwnersCallbacksInOrder)
{
  Queue(*m_handlerA, Record("a1"));
  Queue(*m_handlerB, Record("b1"));
  Queue(*m_handlerA, Record("a2"));
  Queue(*m_handlerB, Record("b2"));
  Queue(*m_handlerA, Record("a3"));

  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2", "a3"}), m_calls);
  // A drain never inspects callbacks that another owner must run.
  EXPECT_EQ(0, m_handlerB->stateChecks.load());

  Drain(m_ownerB);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2", "a3", "b1", "b2"}), m_calls);
}

TEST_F(TestCallbackHandler, CallbackQueuedDuringDrainRunsAfterQueuedCallbacks)
{
  Queue(*m_handlerA,
        [this]
        {
          m_calls.push_back("a1");
          Queue(*m_handlerA, Record("a3"));
          Queue(*m_handlerB, Record("b1"));
        });
  Queue(*m_handlerA, Record("a2"));

  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2", "a3"}), m_calls);

  Drain(m_ownerB);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2", "a3", "b1"}), m_calls);
}

TEST_F(TestCallbackHandler, CallbackThatClearsItsOwnerStopsTheDrain)
{
  Queue(*m_handlerA,
        [this]
        {
          m_calls.push_back("a1");
          Clear(m_ownerA);
        });
  Queue(*m_handlerA, Record("a2"));
  Queue(*m_handlerB, Record("b1"));

  Drain(m_ownerA);
  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1"}), m_calls);

  // Clearing one owner leaves the other owner's callbacks queued.
  Drain(m_ownerB);
  EXPECT_EQ((std::vector<std::string>{"a1", "b1"}), m_calls);
}

TEST_F(TestCallbackHandler, NestedDrainRunsEachCallbackOnce)
{
  Queue(*m_handlerA,
        [this]
        {
          m_calls.push_back("a1");
          Drain(m_ownerA);
        });
  Queue(*m_handlerA, Record("a2"));
  Queue(*m_handlerA, Record("a3"));

  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2", "a3"}), m_calls);
}

TEST_F(TestCallbackHandler, CallbackWaitsUntilTheOwnerStateIsOk)
{
  m_handlerA->stateOk = false;
  Queue(*m_handlerA, Record("a1"));

  Drain(m_ownerA);
  EXPECT_TRUE(m_calls.empty());

  m_handlerA->stateOk = true;
  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1"}), m_calls);
}

TEST_F(TestCallbackHandler, RetiredTargetSkipsAndDropsTheCallback)
{
  AddonClass::Ref<TestTarget> retired(new TestTarget);
  m_handlerA->invokeCallback(new TestCallback(retired.get(), Record("retired")));
  Queue(*m_handlerA, Record("a1"));
  retired->Retire();

  Drain(m_ownerA);
  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1"}), m_calls);
}

TEST_F(TestCallbackHandler, ThrowingCallbackDoesNotStopTheDrain)
{
  Queue(*m_handlerA, [] { throw TestCallbackException("expected by the test"); });
  Queue(*m_handlerA, [] { throw 42; });
  Queue(*m_handlerA, Record("a1"));

  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1"}), m_calls);
}

TEST_F(TestCallbackHandler, ReleasedCallbackCanQueueAnotherCallback)
{
  // Clear releases removed callbacks after the queue lock, so a release can reenter.
  Queue(*m_handlerA, Record("a1"), [this] { Queue(*m_handlerB, Record("b1")); });

  Clear(m_ownerA);
  Drain(m_ownerA);
  Drain(m_ownerB);
  EXPECT_EQ((std::vector<std::string>{"b1"}), m_calls);
}

TEST_F(TestCallbackHandler, ReleaseDuringDrainCanQueueForTheSameOwner)
{
  Queue(*m_handlerA, Record("a1"), [this] { Queue(*m_handlerA, Record("a2")); });

  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2"}), m_calls);
}

TEST_F(TestCallbackHandler, OwnerCanQueueAgainAfterTheQueueEmpties)
{
  Queue(*m_handlerA, Record("a1"));
  Drain(m_ownerA);

  // A new handler for the same owner key, as when a thread state address is reused.
  AddonClass::Ref<TestHandler> reused(new TestHandler(m_ownerA));
  reused->invokeCallback(new TestCallback(m_target.get(), Record("a2")));
  Drain(m_ownerA);
  EXPECT_EQ((std::vector<std::string>{"a1", "a2"}), m_calls);
}

TEST_F(TestCallbackHandler, ConcurrentQueueingKeepsEveryCallbackAndPerThreadOrder)
{
  constexpr int THREADS = 4;
  constexpr int PER_THREAD = 1000;
  std::mutex resultsMutex;
  std::vector<std::vector<int>> results(THREADS);
  std::atomic<int> producersDone{0};

  std::vector<std::thread> producers;
  for (int thread = 0; thread < THREADS; ++thread)
  {
    producers.emplace_back(
        [&, thread]
        {
          for (int i = 0; i < PER_THREAD; ++i)
            Queue(*m_handlerA,
                  [&, thread, i]
                  {
                    std::unique_lock<std::mutex> lock(resultsMutex);
                    results[thread].push_back(i);
                  });
          ++producersDone;
        });
  }

  while (producersDone.load() < THREADS)
    Drain(m_ownerA);
  for (auto& producer : producers)
    producer.join();
  Drain(m_ownerA);

  for (int thread = 0; thread < THREADS; ++thread)
  {
    ASSERT_EQ(static_cast<size_t>(PER_THREAD), results[thread].size());
    for (int i = 0; i < PER_THREAD; ++i)
      ASSERT_EQ(i, results[thread][i]);
  }
}
