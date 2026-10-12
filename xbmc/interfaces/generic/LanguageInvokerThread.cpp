/*
 *  Copyright (C) 2013-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "LanguageInvokerThread.h"

#include "ScriptInvocationManager.h"
#include "utils/log.h"

#include <exception>
#include <system_error>
#include <utility>

CLanguageInvokerThread::CLanguageInvokerThread(LanguageInvokerPtr invoker,
                                               CScriptInvocationManager* invocationManager,
                                               bool reuseable)
  : ILanguageInvoker(NULL),
    CThread("LanguageInvoker"),
    m_invoker(std::move(invoker)),
    m_invocationManager(invocationManager),
    m_reusable(reuseable)
{ }

CLanguageInvokerThread::~CLanguageInvokerThread()
{
  Stop(true);
}

InvokerState CLanguageInvokerThread::GetState() const
{
  if (m_invoker == NULL)
    return InvokerStateFailed;

  return m_invoker->GetState();
}

bool CLanguageInvokerThread::Reuseable(const std::string& script) const
{
  std::unique_lock<std::mutex> lck(m_mutex);
  return !m_released && m_reusable && m_idle && GetState() == InvokerStateScriptDone &&
         m_script == script;
}

bool CLanguageInvokerThread::Keeps(const std::string& script) const
{
  std::unique_lock<std::mutex> lck(m_mutex);
  return !m_released && !m_cancelled && m_reusable && m_script == script;
}

bool CLanguageInvokerThread::Retired() const
{
  std::unique_lock<std::mutex> lck(m_mutex);
  return m_released || m_cancelled || !m_reusable;
}

void CLanguageInvokerThread::Release()
{
  {
    // Set the flag under the mutex Process() waits with, otherwise the wakeup
    // can be lost and the released thread idles until shutdown. CThread's
    // m_bStop can't be used: Create() clears it, losing a Release() that came
    // before the thread started.
    std::unique_lock<std::mutex> lck(m_mutex);
    m_released = true;
  }
  m_condition.notify_one();
}

bool CLanguageInvokerThread::execute(const std::string &script, const std::vector<std::string> &arguments)
{
  if (m_invoker == NULL || script.empty())
    return false;

  if (CThread::IsRunning())
  {
    std::unique_lock<std::mutex> lck(m_mutex);
    // A released or failed thread is leaving Process() and will not run
    // another script; the caller has to start a new thread instead.
    if (m_released || !m_reusable || !m_idle)
      return false;

    m_script = script;
    m_args = arguments;
    m_restart = true;
    m_idle = false;
    m_condition.notify_one();
  }
  else
  {
    // Launch under the mutex: stop() takes it to cancel, so it either cancels
    // before the launch or finds the thread running and stops it.
    std::unique_lock<std::mutex> lck(m_mutex);
    if (m_cancelled)
      return false;
    m_script = script;
    m_args = arguments;

    try
    {
      Create();
    }
    catch (const std::system_error& error)
    {
      CLog::Log(LOGERROR,
                "Cannot launch script '{}' (invoker {}): thread creation failed, code={} "
                "category={} message='{}' ({})",
                script, GetId(), error.code().value(), error.code().category().name(),
                error.code().message(), error.what());
      lck.unlock();
      OnLaunchFailed();
      return false;
    }
    catch (const std::exception& error)
    {
      CLog::Log(LOGERROR, "Cannot launch script '{}' (invoker {}): {}", script, GetId(),
                error.what());
      lck.unlock();
      OnLaunchFailed();
      return false;
    }
    lck.unlock();

    /* low prio */
    SetPriority(ThreadPriority::BELOW_NORMAL);
  }
  //Todo wait until running

  return true;
}

void CLanguageInvokerThread::OnLaunchFailed()
{
  {
    std::unique_lock<std::mutex> lck(m_mutex);
    m_reusable = false;
    m_released = true;
  }
  m_bStop = true;
  m_invoker->setState(InvokerStateFailed);
  // No interpreter was started. OnException/OnExit are worker-only teardown,
  // and assigning the inner invoker an ID would imply Python initialization.
  m_invocationManager->OnExecutionDone(GetId());
}

bool CLanguageInvokerThread::stop(bool wait)
{
  if (m_invoker == NULL)
    return false;

  {
    // Cancel and release together, so no restart is accepted in between. This
    // also cancels a launch that hasn't happened yet, e.g. during shutdown.
    std::unique_lock<std::mutex> lck(m_mutex);
    m_cancelled = true;
    m_released = true;
  }
  m_condition.notify_one();

  if (!CThread::IsRunning())
    return false;

  bool result = true;
  if (m_invoker->GetState() < InvokerStateExecutionDone)
  {
    // stop the language-specific invoker
    result = m_invoker->Stop(wait);
  }
  // stop the thread
  CThread::StopThread(wait);

  return result;
}

void CLanguageInvokerThread::OnStartup()
{
  if (m_invoker == NULL)
    return;

  m_invoker->SetId(GetId());
  if (m_addon != NULL)
    m_invoker->SetAddon(m_addon);
}

void CLanguageInvokerThread::Process()
{
  if (m_invoker == NULL)
    return;

  std::unique_lock<std::mutex> lckdl(m_mutex);
  do
  {
    if (m_cancelled)
      break;
    m_restart = false;
    const std::string script = m_script;
    const std::vector<std::string> arguments = m_args;

    // Don't hold the mutex while the script runs: Release() and execute() take
    // it and must not block for the duration of a script.
    lckdl.unlock();
    try
    {
      m_invoker->Execute(script, arguments);
    }
    catch (...)
    {
      // retired, not kept as busy forever, whoever catches this
      lckdl.lock();
      m_reusable = false;
      throw;
    }
    lckdl.lock();

    if (m_invoker->GetState() != InvokerStateScriptDone)
      m_reusable = false;

    // Only now may the thread be reused: the state checked above must not be
    // reset by a new caller while the script is still finishing.
    m_idle = true;
    m_condition.wait(lckdl, [this] { return m_released || m_restart || !m_reusable; });

    // A restart accepted by execute() before Release() still runs, its caller
    // is waiting for the result.
  } while (m_reusable && (m_restart || !m_released));
}

void CLanguageInvokerThread::OnExit()
{
  if (m_invoker == NULL)
    return;

  m_invoker->onExecutionDone();
  m_invocationManager->OnExecutionDone(GetId());
}

void CLanguageInvokerThread::OnException()
{
  if (m_invoker == NULL)
    return;

  m_invoker->onExecutionFailed();
  m_invocationManager->OnExecutionDone(GetId());
}
