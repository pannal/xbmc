/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "interfaces/generic/ILanguageInvocationHandler.h"
#include "interfaces/generic/ILanguageInvoker.h"
#include "interfaces/generic/ScriptInvocationManager.h"
#include "commons/Exception.h"

#include <atomic>
#include <chrono>
#include <filesystem>
#include <future>
#include <fstream>
#include <mutex>
#include <random>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

using namespace std::chrono_literals;

namespace
{
// Interpreters whose script has started and that weren't torn down yet
std::atomic<int> g_alive{0};
std::atomic<int> g_created{0}; // interpreters ever started
std::atomic<int> g_errors{0};
std::atomic<bool> g_block{false};
std::atomic<bool> g_throw{false};
std::atomic<int> g_entered{0}; // scripts that started running
std::mutex g_doneLock;
std::set<int> g_doneCalls;

// Stands in for CPythonInvoker. Its "interpreter" exists from the first
// execute() until onExecutionDone(), and it records any use outside that.
class CFakeInvoker : public ILanguageInvoker
{
public:
  explicit CFakeInvoker(ILanguageInvocationHandler* handler) : ILanguageInvoker(handler) {}

protected:
  bool execute(const std::string& script, const std::vector<std::string>& arguments) override
  {
    if (m_tornDown || ++m_executing != 1)
      ++g_errors;
    if (!m_started)
    {
      m_started = true;
      ++g_alive;
      ++g_created;
    }
    setState(InvokerStateRunning);
    ++g_entered;
    if (g_throw)
    {
      --m_executing;
      throw XbmcCommons::UncheckedException("script threw");
    }
    while (g_block)
      std::this_thread::sleep_for(1ms);
    std::this_thread::sleep_for(std::chrono::microseconds(std::rand() % 2000));
    --m_executing;
    setState(InvokerStateScriptDone);

    // like xbmcplugin.endOfDirectory(): the caller stops waiting here
    std::unique_lock<std::mutex> lock(g_doneLock);
    g_doneCalls.insert(std::stoi(arguments.at(1)));
    return true;
  }

  bool stop(bool abort) override { return true; }

  void onExecutionDone() override
  {
    if (m_executing != 0)
      ++g_errors;
    if (m_started && !m_tornDown)
      --g_alive;
    m_tornDown = true;
    ILanguageInvoker::onExecutionDone();
  }

private:
  std::atomic<int> m_executing{0};
  std::atomic<bool> m_tornDown{false};
  bool m_started{false};
};

class CFakeInvocationHandler : public ILanguageInvocationHandler
{
public:
  ILanguageInvoker* CreateInvoker() override { return new CFakeInvoker(this); }
};

// Like Kodi's, both outlive any script thread a test leaves behind
CFakeInvocationHandler g_handler;
std::atomic<int> g_nextHandle{1};

class TestScriptInvocationManager : public ::testing::Test
{
protected:
  void SetUp() override
  {
    m_dir = std::filesystem::temp_directory_path() / "kodi-test-script-invocation";
    std::filesystem::create_directories(m_dir);
    for (const char* name : {"a", "b", "c"})
      std::ofstream(Script(name)) << "";
    g_alive = 0;
    g_created = 0;
    g_entered = 0;
    g_errors = 0;
    g_block = false;
    g_throw = false;
    {
      std::unique_lock<std::mutex> lock(g_doneLock);
      g_doneCalls.clear();
    }
    Manager().RegisterLanguageInvocationHandler(&g_handler, ".fakeinvoker");
  }

  void TearDown() override
  {
    Settle();
    Manager().StopRunningScripts();
    Settle();
    Manager().UnregisterLanguageInvocationHandler(&g_handler);
    std::filesystem::remove_all(m_dir);
  }

  static CScriptInvocationManager& Manager() { return CScriptInvocationManager::GetInstance(); }

  std::string Script(const std::string& name) const
  {
    return (m_dir / (name + ".fakeinvoker")).string();
  }

  // What CRunningScriptsHandler::RunScript() and CScriptRunner do for a plugin
  // call. Returns false if the script never ran.
  bool RunPlugin(const std::string& script, bool reuse, int* scriptId = nullptr)
  {
    const int call = m_nextCall++;
    // like CRunningScriptsHandler, also for add-ons that don't reuse
    int handle = Manager().GetReusablePluginHandle(script);
    if (handle < 0)
      handle = g_nextHandle++;

    const int id = Manager().ExecuteAsync(script, ADDON::AddonPtr(),
                                          {script, std::to_string(call)}, reuse, handle);
    if (id < 0)
      return false;
    if (scriptId)
      *scriptId = id;

    const auto end = std::chrono::steady_clock::now() + 10s;
    while (std::chrono::steady_clock::now() < end)
    {
      {
        std::unique_lock<std::mutex> lock(g_doneLock);
        if (g_doneCalls.erase(call))
          return true;
      }
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }

  static void Settle()
  {
    for (int i = 0; i < 50; ++i)
    {
      Manager().Process();
      std::this_thread::sleep_for(10ms);
    }
  }

  std::filesystem::path m_dir;
  std::atomic<int> m_nextCall{1};
};
} // namespace

TEST_F(TestScriptInvocationManager, SequentialCallsReuseOneInterpreter)
{
  for (int i = 0; i < 5; ++i)
  {
    EXPECT_TRUE(RunPlugin(Script("a"), true));
    Settle(); // the kept thread is idle again
  }

  EXPECT_EQ(1, g_created);
  EXPECT_EQ(1, g_alive);
  EXPECT_EQ(0, g_errors);
}

TEST_F(TestScriptInvocationManager, InvokerInUseIsNotRunTwice)
{
  LanguageInvokerPtr invoker(g_handler.CreateInvoker());
  ASSERT_GE(Manager().ExecuteAsync(Script("a"), invoker, ADDON::AddonPtr(), {Script("a"), "0"},
                                   true),
            0);
  Settle();

  EXPECT_LT(Manager().ExecuteAsync(Script("a"), invoker, ADDON::AddonPtr(), {Script("a"), "0"},
                                   true),
            0);
  EXPECT_EQ(0, g_errors);
}

TEST_F(TestScriptInvocationManager, RejectedCallDropsItsClaim)
{
  ASSERT_TRUE(RunPlugin(Script("a"), true));
  Settle();
  const int handle = Manager().GetReusablePluginHandle(Script("a"));
  ASSERT_GE(handle, 0);

  // b's script keeps its invoker busy
  g_block = true;
  LanguageInvokerPtr invoker(g_handler.CreateInvoker());
  ASSERT_GE(Manager().ExecuteAsync(Script("b"), invoker, ADDON::AddonPtr(), {Script("b"), "0"}),
            0);
  while (g_entered == 0)
    std::this_thread::sleep_for(1ms);

  EXPECT_LT(Manager().ExecuteAsync(Script("a"), invoker, ADDON::AddonPtr(), {Script("a"), "0"},
                                   true, handle),
            0);
  g_block = false;
  Settle();

  // the claim was dropped, so the kept thread can be claimed again
  const int again = Manager().GetReusablePluginHandle(Script("a"));
  EXPECT_EQ(handle, again);
  EXPECT_GE(Manager().ExecuteAsync(Script("a"), ADDON::AddonPtr(), {Script("a"), "0"}, true, again),
            0);
}

TEST_F(TestScriptInvocationManager, InvalidScriptDropsItsClaim)
{
  ASSERT_TRUE(RunPlugin(Script("a"), true));
  Settle();

  for (const std::string& script : {std::string(), Script("missing")})
  {
    const int handle = Manager().GetReusablePluginHandle(Script("a"));
    ASSERT_GE(handle, 0);
    EXPECT_LT(Manager().ExecuteAsync(script, ADDON::AddonPtr(), {script, "0"}, true, handle), 0);
  }
  EXPECT_GE(Manager().GetReusablePluginHandle(Script("a")), 0);
}

TEST_F(TestScriptInvocationManager, ThreadEndedByExceptionIsReplaced)
{
  g_throw = true;
  ASSERT_GE(Manager().ExecuteAsync(Script("a"), ADDON::AddonPtr(), {Script("a"), "0"}, true,
                                   g_nextHandle++),
            0);
  Settle();
  g_throw = false;

  // the first call replaces the ended thread, the second reuses it
  EXPECT_TRUE(RunPlugin(Script("a"), true));
  Settle();
  EXPECT_TRUE(RunPlugin(Script("a"), true));
  Settle();
  EXPECT_EQ(2, g_created);
  EXPECT_EQ(1, g_alive);
  EXPECT_EQ(0, g_errors);
}

TEST_F(TestScriptInvocationManager, StopAndWaitReturns)
{
  int id = -1;
  ASSERT_TRUE(RunPlugin(Script("a"), true, &id));
  Settle();

  // the kept thread's OnExit() needs the manager lock Stop() used to hold
  auto stopped = std::async(std::launch::async, [id] { return Manager().Stop(id, true); });
  EXPECT_EQ(std::future_status::ready, stopped.wait_for(5s));
  Settle();
  EXPECT_EQ(0, g_alive);
}

TEST_F(TestScriptInvocationManager, KeptHandleGoesToOneCaller)
{
  ASSERT_TRUE(RunPlugin(Script("a"), true));
  Settle();

  const int first = Manager().GetReusablePluginHandle(Script("a"));
  const int second = Manager().GetReusablePluginHandle(Script("a"));
  EXPECT_GE(first, 0);
  EXPECT_LT(second, 0);

  // let the claim be used
  EXPECT_GE(Manager().ExecuteAsync(Script("a"), ADDON::AddonPtr(), {Script("a"), "0"}, true, first),
            0);
}

TEST_F(TestScriptInvocationManager, PluginCallsReplaceThreadKeptWithoutHandle)
{
  // like CContextMenuItem: own invoker, reusable, no plugin handle
  LanguageInvokerPtr invoker(g_handler.CreateInvoker());
  ASSERT_GE(Manager().ExecuteAsync(Script("a"), invoker, ADDON::AddonPtr(), {Script("a"), "0"},
                                   true),
            0);
  Settle();

  EXPECT_TRUE(RunPlugin(Script("a"), true));
  EXPECT_TRUE(RunPlugin(Script("a"), true));
  Settle();
  EXPECT_EQ(1, g_alive);

  // the plugin's kept thread can be claimed again
  const int handle = Manager().GetReusablePluginHandle(Script("a"));
  EXPECT_GE(handle, 0);
  EXPECT_GE(Manager().ExecuteAsync(Script("a"), ADDON::AddonPtr(), {Script("a"), "0"}, true, handle),
            0);
}

TEST_F(TestScriptInvocationManager, CallsWithoutReuseDontRetireTheKeptInterpreter)
{
  ASSERT_TRUE(RunPlugin(Script("a"), true));
  Settle();
  for (int i = 0; i < 3; ++i)
    EXPECT_TRUE(RunPlugin(Script("c"), false));
  Settle();
  EXPECT_TRUE(RunPlugin(Script("a"), true));
  Settle();

  // a once, c three times
  EXPECT_EQ(4, g_created);
  EXPECT_EQ(1, g_alive);
}

TEST_F(TestScriptInvocationManager, ReleaseDoesNotWaitForRunningScript)
{
  g_block = true;
  std::thread caller([this] { RunPlugin(Script("a"), true); });
  while (g_entered == 0)
    std::this_thread::sleep_for(1ms);

  // another reusing add-on replaces the kept thread while its script runs
  const auto start = std::chrono::steady_clock::now();
  EXPECT_GE(Manager().ExecuteAsync(Script("b"), ADDON::AddonPtr(), {Script("b"), "0"}, true), 0);
  EXPECT_LT(std::chrono::steady_clock::now() - start, 1s); // a's script is still blocked

  g_block = false;
  caller.join();
  Settle();
  EXPECT_EQ(1, g_alive); // b is kept, a was released
}

TEST_F(TestScriptInvocationManager, ConcurrentCallsNeitherShareNorLeakInterpreters)
{
  std::atomic<int> lost{0};
  std::atomic<bool> stop{false};
  std::thread processor([&stop] {
    while (!stop)
    {
      Manager().Process();
      std::this_thread::sleep_for(2ms);
    }
  });

  std::vector<std::thread> workers;
  for (int w = 0; w < 8; ++w)
    workers.emplace_back(
        [this, w, &lost]
        {
          std::mt19937 random(w);
          for (int i = 0; i < 200; ++i)
          {
            const unsigned int pick = random() % 4;
            const bool reuse = pick != 3;
            if (!RunPlugin(Script(pick < 2 ? "a" : (pick == 2 ? "b" : "c")), reuse))
              ++lost;
          }
        });
  for (auto& worker : workers)
    worker.join();
  stop = true;
  processor.join();
  Settle();

  EXPECT_EQ(0, lost);
  EXPECT_EQ(0, g_errors);
  EXPECT_LE(g_alive, 1);
}
