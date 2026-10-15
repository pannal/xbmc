/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "CallbackHandler.h"

#include "AddonUtils.h"
#include "commons/Exception.h"
#include "utils/log.h"

#include <cstdint>
#include <map>
#include <mutex>

namespace XBMCAddon
{
  class AsyncCallbackMessage : public AddonClass
  {
  public:
    AddonClass::Ref<Callback> cb;
    AddonClass::Ref<RetardedAsyncCallbackHandler> handler;
    AsyncCallbackMessage(Callback* _cb, RetardedAsyncCallbackHandler* _handler) :
      cb(_cb), handler(_handler) { XBMC_TRACE; }
  };

  //********************************************************************
  // Sequence keys preserve callback order across queue changes.
  typedef std::map<std::uint64_t, AddonClass::Ref<AsyncCallbackMessage>> CallbackQueue;
  //********************************************************************

  static CCriticalSection critSection;
  static CallbackQueue g_callQueue;
  static std::uint64_t g_nextCallbackId = 0;

  void RetardedAsyncCallbackHandler::invokeCallback(Callback* cb)
  {
    XBMC_TRACE;
    std::unique_lock<CCriticalSection> lock(critSection);
    g_callQueue.emplace(g_nextCallbackId++, new AsyncCallbackMessage(cb, this));
  }

  RetardedAsyncCallbackHandler::~RetardedAsyncCallbackHandler()
  {
    XBMC_TRACE;
    std::unique_lock<CCriticalSection> lock(critSection);

    // find any messages that might be there because of me ... and remove them
    CallbackQueue::iterator iter = g_callQueue.begin();
    while (iter != g_callQueue.end())
    {
      if (iter->second->handler.get() == this) // then this message is because of me
      {
        g_callQueue.erase(iter);
        iter = g_callQueue.begin();
      }
      else
        ++iter;
    }
  }

  void RetardedAsyncCallbackHandler::makePendingCalls()
  {
    XBMC_TRACE;
    std::unique_lock<CCriticalSection> lock(critSection);
    CallbackQueue::iterator iter = g_callQueue.begin();
    while (iter != g_callQueue.end())
    {
      AsyncCallbackMessage* message = iter->second.get();

      // only call when we are in the right thread state
      if (message->handler->isStateOk(message->cb->getObject()))
      {
        const auto sequence = iter->first;
        AddonClass::Ref<AsyncCallbackMessage> p(message);
        // remove it from the queue. No matter what we're done with
        //  this. Even if it doesn't execute for some reason.
        g_callQueue.erase(iter);

        // we need to release the critSection lock prior to grabbing the
        //  lock on the object. Not doing so results in deadlocks. We no
        //  longer are accessing the g_callQueue so it's fine to do this now
        {
          XBMCAddonUtils::InvertSingleLockGuard unlock(lock);

          // make sure the object is not deallocating

          // we need to grab the object lock to see if the object of the call
          //  is deallocating. holding this lock should prevent it from
          //  deallocating during the execution of this call.
#ifdef ENABLE_XBMC_TRACE_API
          CLog::Log(LOGDEBUG, "{}NEWADDON executing callback 0x{:x}", _tg.getSpaces(),
                    (long)(p->cb.get()));
#endif
          AddonClass* obj = (p->cb->getObject());
          AddonClass::Ref<AddonClass> ref(obj);
          std::unique_lock<CCriticalSection> lock2(*obj);
          if (!p->cb->getObject()->isDeallocating())
          {
            try
            {
              // need to make the call
              p->cb->executeCallback();
            }
            catch (XbmcCommons::Exception& e) { e.LogThrowMessage(); }
            catch (...)
            {
              CLog::Log(LOGERROR, "Unknown exception while executing callback {:#x}",
                        reinterpret_cast<int64_t>(p->cb.get()));
            }
          }
          // Release the selected message before the queue lock is reacquired.
          p.reset();
        }

        // Resume after the selected sequence when callbacks change the queue.
        iter = g_callQueue.upper_bound(sequence);
      }
      else // if we're not in the right thread for this callback...
        ++iter;
    }
  }

  void RetardedAsyncCallbackHandler::clearPendingCalls(void* userData)
  {
    XBMC_TRACE;
    std::unique_lock<CCriticalSection> lock(critSection);
    CallbackQueue::iterator iter = g_callQueue.begin();
    while (iter != g_callQueue.end())
    {
      AddonClass::Ref<AsyncCallbackMessage> p(iter->second);

      if(p->handler->shouldRemoveCallback(p->cb->getObject(),userData))
      {
#ifdef ENABLE_XBMC_TRACE_API
        CLog::Log(LOGDEBUG,
                  "{}NEWADDON removing callback 0x{:x} for PyThreadState 0x{:x} from queue",
                  _tg.getSpaces(), (long)(p->cb.get()), (long)userData);
#endif
        iter = g_callQueue.erase(iter);
      }
      else
        ++iter;
    }
  }
}

