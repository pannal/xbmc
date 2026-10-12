/*
 *  Copyright (C) 2013-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ScriptInvocationManager.h"

#include "interfaces/generic/ILanguageInvocationHandler.h"
#include "interfaces/generic/ILanguageInvoker.h"
#include "interfaces/generic/LanguageInvokerThread.h"
#include "utils/FileUtils.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/XTimeUtils.h"
#include "utils/log.h"

#include <cerrno>
#include <memory>
#include <mutex>
#include <utility>
#include <vector>

CScriptInvocationManager::~CScriptInvocationManager()
{
  Uninitialize();
}

CScriptInvocationManager& CScriptInvocationManager::GetInstance()
{
  static CScriptInvocationManager s_instance;
  return s_instance;
}

void CScriptInvocationManager::Process()
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  // go through all active threads and find and remove all which are done
  std::vector<LanguageInvokerThread> tempList;
  for (LanguageInvokerThreadMap::iterator it = m_scripts.begin(); it != m_scripts.end(); )
  {
    if (it->second.done)
    {
      tempList.push_back(it->second);
      m_scripts.erase(it++);
    }
    else
      ++it;
  }

  // remove the finished scripts from the script path map as well
  for (const auto& it : tempList)
    RemoveScriptPath(it.script, it.thread->GetId());

  // we can leave the lock now
  lock.unlock();

  // finally remove the finished threads but we do it outside of any locks in
  // case of any callbacks from the destruction of the CLanguageInvokerThread
  tempList.clear();

  // let the invocation handlers do their processing
  for (auto& it : m_invocationHandlers)
    it.second->Process();
}

void CScriptInvocationManager::Uninitialize()
{
  std::unique_lock<CCriticalSection> lock(m_critSection);

  // execute Process() once more to handle the remaining scripts
  Process();

  // it is safe to release early, thread must be in m_scripts too
  ReleaseLastInvokerThread();

  // make sure all scripts are done
  std::vector<LanguageInvokerThread> tempList;
  for (const auto& script : m_scripts)
    tempList.push_back(script.second);

  m_scripts.clear();
  m_scriptPaths.clear();

  // we can leave the lock now
  lock.unlock();

  // finally stop and remove the finished threads but we do it outside of any
  // locks in case of any callbacks from the stop or destruction logic of
  // CLanguageInvokerThread or the ILanguageInvoker implementation
  for (auto& it : tempList)
  {
    if (!it.done)
      it.thread->Stop(true);
  }

  lock.lock();

  tempList.clear();

  // uninitialize all invocation handlers and then remove them
  for (auto& it : m_invocationHandlers)
    it.second->Uninitialize();

  m_invocationHandlers.clear();
}

void CScriptInvocationManager::RegisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler, const std::string &extension)
{
  if (invocationHandler == NULL || extension.empty())
    return;

  std::string ext = extension;
  StringUtils::ToLower(ext);
  if (!StringUtils::StartsWithNoCase(ext, "."))
    ext = "." + ext;

  std::unique_lock<CCriticalSection> lock(m_critSection);
  if (m_invocationHandlers.find(ext) != m_invocationHandlers.end())
    return;

  m_invocationHandlers.insert(std::make_pair(extension, invocationHandler));

  bool known = false;
  for (const auto& it : m_invocationHandlers)
  {
    if (it.second == invocationHandler)
    {
      known = true;
      break;
    }
  }

  // automatically initialize the invocation handler if it's a new one
  if (!known)
    invocationHandler->Initialize();
}

void CScriptInvocationManager::RegisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler, const std::set<std::string> &extensions)
{
  if (invocationHandler == NULL || extensions.empty())
    return;

  for (const auto& extension : extensions)
    RegisterLanguageInvocationHandler(invocationHandler, extension);
}

void CScriptInvocationManager::UnregisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler)
{
  if (invocationHandler == NULL)
    return;

  std::unique_lock<CCriticalSection> lock(m_critSection);
  //  get all extensions of the given language invoker
  for (std::map<std::string, ILanguageInvocationHandler*>::iterator it = m_invocationHandlers.begin(); it != m_invocationHandlers.end(); )
  {
    if (it->second == invocationHandler)
      m_invocationHandlers.erase(it++);
    else
      ++it;
  }

  // automatically uninitialize the invocation handler
  invocationHandler->Uninitialize();
}

bool CScriptInvocationManager::HasLanguageInvoker(const std::string &script) const
{
  std::string extension = URIUtils::GetExtension(script);
  StringUtils::ToLower(extension);

  std::unique_lock<CCriticalSection> lock(m_critSection);
  std::map<std::string, ILanguageInvocationHandler*>::const_iterator it = m_invocationHandlers.find(extension);
  return it != m_invocationHandlers.end() && it->second != NULL;
}

int CScriptInvocationManager::GetReusablePluginHandle(const std::string& script)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);

  // a stopped thread serves nobody, even if it was claimed
  if (m_lastInvokerThread && m_lastInvokerThread->Retired())
    ReleaseLastInvokerThread();

  // A claimed thread is reserved for the request that got its handle; leave it
  // alone and let this request use a new handle and interpreter.
  if (!m_lastInvokerThread || m_lastInvokerClaimed)
    return -1;

  if (m_lastPluginHandle >= 0 && m_lastInvokerThread->Reuseable(script))
  {
    m_lastInvokerClaimed = true;
    return m_lastPluginHandle;
  }

  // Keep it for other add-ons and while it is busy. Drop it if a context menu
  // item kept it for this script without a plugin handle, so plugin calls
  // can't claim it.
  if (m_lastPluginHandle < 0 && m_lastInvokerThread->Keeps(script))
    ReleaseLastInvokerThread();
  return -1;
}

void CScriptInvocationManager::ReleaseLastInvokerThread()
{
  if (m_lastInvokerThread)
    m_lastInvokerThread->Release();
  m_lastInvokerThread = nullptr;
  m_lastPluginHandle = -1;
  m_lastInvokerClaimed = false;
}

bool CScriptInvocationManager::OwnsClaim(int pluginHandle) const
{
  return m_lastInvokerClaimed && pluginHandle >= 0 && pluginHandle == m_lastPluginHandle;
}

CLanguageInvokerThreadPtr CScriptInvocationManager::TakeReusableThread(const std::string& script,
                                                                       int pluginHandle)
{
  if (!m_lastInvokerThread)
    return nullptr;

  const bool ownsClaim = OwnsClaim(pluginHandle);
  if (m_lastInvokerClaimed && !ownsClaim)
    return nullptr;

  m_lastInvokerClaimed = false;

  // A plugin may keep the handle it was first called with, so a kept
  // interpreter only serves plugin requests that claimed its handle.
  if ((ownsClaim || pluginHandle < 0) && m_lastInvokerThread->Reuseable(script))
    return m_lastInvokerThread;

  if (m_lastInvokerThread->Retired())
    ReleaseLastInvokerThread();
  return nullptr;
}

LanguageInvokerPtr CScriptInvocationManager::GetLanguageInvoker(const std::string& script)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);

  std::string extension = URIUtils::GetExtension(script);
  StringUtils::ToLower(extension);

  std::map<std::string, ILanguageInvocationHandler*>::const_iterator it = m_invocationHandlers.find(extension);
  if (it != m_invocationHandlers.end() && it->second != NULL)
    return LanguageInvokerPtr(it->second->CreateInvoker());

  return LanguageInvokerPtr();
}

int CScriptInvocationManager::ExecuteAsync(
    const std::string& script,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    bool reuseable /* = false */,
    int pluginHandle /* = -1 */)
{
  if (script.empty() || !CFileUtils::Exists(script, false))
  {
    if (!script.empty())
      CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    // drop a claim taken by GetReusablePluginHandle() for this request
    std::unique_lock<CCriticalSection> lock(m_critSection);
    if (OwnsClaim(pluginHandle))
      m_lastInvokerClaimed = false;
    return -1;
  }

  std::unique_lock<CCriticalSection> lock(m_critSection);

  // Choosing the kept thread and handing it the script happen under one lock,
  // so no other invocation can release the thread or take its interpreter in
  // between. Execute() only takes the thread's own mutex, which is never held
  // while a script runs.
  CLanguageInvokerThreadPtr invokerThread = TakeReusableThread(script, pluginHandle);
  if (invokerThread)
  {
    CLog::Log(LOGDEBUG, "{} - Reusing LanguageInvokerThread {} for script {}", __FUNCTION__,
              invokerThread->GetId(), script);
    invokerThread->GetInvoker()->Reset();
    if (addon != NULL)
      invokerThread->SetAddon(addon);
    if (invokerThread->Execute(script, arguments))
      return invokerThread->GetId();

    // Stopped after it was chosen. Its interpreter is being torn down, run the
    // script in a new one.
    CLog::Log(LOGDEBUG, "{} - LanguageInvokerThread {} stopped before it could run script {}",
              __FUNCTION__, invokerThread->GetId(), script);
    if (m_lastInvokerThread == invokerThread)
      ReleaseLastInvokerThread();
  }
  lock.unlock();

  LanguageInvokerPtr invoker = GetLanguageInvoker(script);
  return ExecuteAsync(script, invoker, addon, arguments, reuseable, pluginHandle);
}

int CScriptInvocationManager::ExecuteAsync(
    const std::string& script,
    const LanguageInvokerPtr& languageInvoker,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    bool reuseable /* = false */,
    int pluginHandle /* = -1 */)
{
  {
    // This request doesn't use the claimed thread, whatever happens below;
    // don't leave it reserved.
    std::unique_lock<CCriticalSection> lock(m_critSection);
    if (OwnsClaim(pluginHandle))
      m_lastInvokerClaimed = false;
  }

  if (script.empty() || languageInvoker == NULL)
    return -1;

  if (!CFileUtils::Exists(script, false))
  {
    CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    return -1;
  }

  std::unique_lock<CCriticalSection> lock(m_critSection);

  // An invoker belongs to one thread; a second thread would run or end its
  // interpreter under the first.
  for (const auto& invocation : m_scripts)
  {
    if (!invocation.second.done && invocation.second.thread->GetInvoker() == languageInvoker)
    {
      CLog::Log(LOGERROR, "{} - invoker of script {} is already in use", __FUNCTION__, script);
      return -1;
    }
  }

  // Only one interpreter is kept for reuse. A new thread runs once instead of
  // replacing it while it is claimed or still kept for the same script (busy),
  // so a burst of calls doesn't churn interpreters.
  const bool keep = reuseable && !m_lastInvokerClaimed &&
                    !(m_lastInvokerThread && m_lastInvokerThread->Keeps(script));
  CLanguageInvokerThreadPtr invokerThread =
      std::make_shared<CLanguageInvokerThread>(languageInvoker, this, keep);

  if (addon != NULL)
    invokerThread->SetAddon(addon);

  invokerThread->SetId(m_nextId++);

  if (keep)
  {
    // Release the previous thread, or it idles with its interpreter until shutdown
    ReleaseLastInvokerThread();
    m_lastInvokerThread = invokerThread;
    m_lastPluginHandle = pluginHandle;
  }

  LanguageInvokerThread thread = {invokerThread, script, false};
  m_scripts.insert(std::make_pair(invokerThread->GetId(), thread));
  m_scriptPaths.insert(std::make_pair(script, invokerThread->GetId()));
  lock.unlock();
  if (!invokerThread->Execute(script, arguments))
  {
    RemoveFailedInvocation(invokerThread);
    return -1;
  }

  return invokerThread->GetId();
}

void CScriptInvocationManager::RemoveFailedInvocation(const CLanguageInvokerThreadPtr& thread)
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  const int id = thread->GetId();
  const auto invocation = m_scripts.find(id);
  if (invocation != m_scripts.end() && invocation->second.thread == thread)
  {
    const auto script = invocation->second.script;
    m_scripts.erase(invocation);
    RemoveScriptPath(script, id);
  }
  if (m_lastInvokerThread == thread)
    ReleaseLastInvokerThread();
  // The caller retains thread until after this lock is released. Destruction
  // must not run interpreter callbacks while holding the invocation lock.
}

void CScriptInvocationManager::RemoveScriptPath(const std::string& script, int id)
{
  const auto path = m_scriptPaths.find(script);
  if (path == m_scriptPaths.end() || path->second != id)
    return;

  m_scriptPaths.erase(path);
  // Another invocation of the same path may have started before this one
  // retired. Keep path-based Stop/IsRunning bound to a surviving invocation.
  for (const auto& invocation : m_scripts)
  {
    if (!invocation.second.done && invocation.second.script == script)
    {
      m_scriptPaths.emplace(script, invocation.first);
      break;
    }
  }
}

int CScriptInvocationManager::ExecuteSync(
    const std::string& script,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    uint32_t timeoutMs /* = 0 */,
    bool waitShutdown /* = false */)
{
  if (script.empty())
    return -1;

  if (!CFileUtils::Exists(script, false))
  {
    CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    return -1;
  }

  LanguageInvokerPtr invoker = GetLanguageInvoker(script);
  return ExecuteSync(script, invoker, addon, arguments, timeoutMs, waitShutdown);
}

int CScriptInvocationManager::ExecuteSync(
    const std::string& script,
    const LanguageInvokerPtr& languageInvoker,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    uint32_t timeoutMs /* = 0 */,
    bool waitShutdown /* = false */)
{
  int scriptId = ExecuteAsync(script, languageInvoker, addon, arguments);
  if (scriptId < 0)
    return -1;

  bool timeout = timeoutMs > 0;
  while ((!timeout || timeoutMs > 0) && IsRunning(scriptId))
  {
    unsigned int sleepMs = 100U;
    if (timeout && timeoutMs < sleepMs)
      sleepMs = timeoutMs;

    KODI::TIME::Sleep(std::chrono::milliseconds(sleepMs));

    if (timeout)
      timeoutMs -= sleepMs;
  }

  if (IsRunning(scriptId))
  {
    Stop(scriptId, waitShutdown);
    return ETIMEDOUT;
  }

  return 0;
}

bool CScriptInvocationManager::Stop(int scriptId, bool wait /* = false */)
{
  if (scriptId < 0)
    return false;

  std::unique_lock<CCriticalSection> lock(m_critSection);
  CLanguageInvokerThreadPtr invokerThread = getInvokerThread(scriptId).thread;
  if (invokerThread == NULL)
    return false;

  // Stopping with wait joins the thread, whose OnExit() takes m_critSection
  lock.unlock();
  return invokerThread->Stop(wait);
}

void CScriptInvocationManager::StopRunningScripts(bool wait /* = false */)
{
  std::vector<CLanguageInvokerThreadPtr> running;
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    for (const auto& it : m_scripts)
    {
      if (!it.second.done)
        running.push_back(it.second.thread);
    }
  }

  for (const auto& thread : running)
    thread->Stop(wait);
}

bool CScriptInvocationManager::Stop(const std::string &scriptPath, bool wait /* = false */)
{
  if (scriptPath.empty())
    return false;

  int scriptId;
  {
    std::unique_lock<CCriticalSection> lock(m_critSection);
    std::map<std::string, int>::const_iterator script = m_scriptPaths.find(scriptPath);
    if (script == m_scriptPaths.end())
      return false;
    scriptId = script->second;
  }

  return Stop(scriptId, wait);
}

bool CScriptInvocationManager::IsRunning(int scriptId) const
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  LanguageInvokerThread invokerThread = getInvokerThread(scriptId);
  if (invokerThread.thread == NULL)
    return false;

  return !invokerThread.done;
}

bool CScriptInvocationManager::IsRunning(const std::string& scriptPath) const
{
  std::unique_lock<CCriticalSection> lock(m_critSection);
  auto it = m_scriptPaths.find(scriptPath);
  if (it == m_scriptPaths.end())
    return false;

  return IsRunning(it->second);
}

void CScriptInvocationManager::OnExecutionDone(int scriptId)
{
  if (scriptId < 0)
    return;

  std::unique_lock<CCriticalSection> lock(m_critSection);
  LanguageInvokerThreadMap::iterator script = m_scripts.find(scriptId);
  if (script != m_scripts.end())
    script->second.done = true;
}

CScriptInvocationManager::LanguageInvokerThread CScriptInvocationManager::getInvokerThread(int scriptId) const
{
  if (scriptId < 0)
    return LanguageInvokerThread();

  LanguageInvokerThreadMap::const_iterator script = m_scripts.find(scriptId);
  if (script == m_scripts.end())
    return LanguageInvokerThread();

  return script->second;
}
