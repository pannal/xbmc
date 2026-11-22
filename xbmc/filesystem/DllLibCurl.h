/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "threads/CriticalSection.h"

#include <map>
#include <set>
#include <stdio.h>
#include <string>
#include <sys/time.h>
#include <sys/types.h>
#include <type_traits>
#include <vector>

#define CURL CURL_HANDLE
#include <curl/curl.h>
#undef CURL

namespace XCURL
{

class DllLibCurl
{
public:
  virtual ~DllLibCurl() = default;

  CURLcode global_init(long flags);
  void global_cleanup();
  CURL_HANDLE* easy_init();
  template<typename... Args>
  CURLcode easy_setopt(CURL_HANDLE* handle, CURLoption option, Args... args)
  {
    return curl_easy_setopt(handle, option, std::forward<Args>(args)...);
  }
  CURLcode easy_perform(CURL_HANDLE* handle);
  CURLcode easy_pause(CURL_HANDLE* handle, int bitmask);
  void easy_reset(CURL_HANDLE* handle);
  template<typename... Args>
  CURLcode easy_getinfo(CURL_HANDLE* curl, CURLINFO info, Args... args)
  {
    return curl_easy_getinfo(curl, info, std::forward<Args>(args)...);
  }
  void easy_cleanup(CURL_HANDLE* handle);
  virtual CURL_HANDLE* easy_duphandle(CURL_HANDLE* handle);
  CURLM* multi_init(void);
  CURLMcode multi_add_handle(CURLM* multi_handle, CURL_HANDLE* easy_handle);
  CURLMcode multi_perform(CURLM* multi_handle, int* running_handles);
  CURLMcode multi_remove_handle(CURLM* multi_handle, CURL_HANDLE* easy_handle);
  CURLMcode multi_fdset(CURLM* multi_handle,
                        fd_set* read_fd_set,
                        fd_set* write_fd_set,
                        fd_set* exc_fd_set,
                        int* max_fd);
  CURLMcode multi_timeout(CURLM* multi_handle, long* timeout);
  /*!
   * \brief Wait for activity before the next multi_perform() call.
   */
  CURLMcode multi_poll(CURLM* multi_handle, int timeout_ms);
  CURLMsg* multi_info_read(CURLM* multi_handle, int* msgs_in_queue);
  CURLMcode multi_cleanup(CURLM* handle);
  curl_slist* slist_append(curl_slist* list, const char* to_append);
  void slist_free_all(curl_slist* list);
  const char* easy_strerror(CURLcode code);
};

class DllLibCurlGlobal : public DllLibCurl
{
public:
  DllLibCurlGlobal();
  ~DllLibCurlGlobal();
  /* extend interface with buffered functions */
  void easy_acquire(const char* protocol,
                    const char* hostname,
                    CURL_HANDLE** easy_handle,
                    CURLM** multi_handle);
  void easy_release(CURL_HANDLE** easy_handle, CURLM** multi_handle);
  /*!
   * \brief Replace a leased session's multi handle after its easy handle is detached.
   * If allocation fails, retry before the next transfer attaches.
   */
  bool multi_renew(CURLM** multi_handle);
  /*!
   * \brief Refresh the session's address pins and attach the easy handle.
   * The function can replace the multi handle. On success the caller owns the
   * resolve list and must keep it alive until the easy handle is detached.
   */
  CURLcode multi_attach(CURLM** multi_handle,
                        CURL_HANDLE* easy_handle,
                        const std::string& hostname,
                        int port,
                        curl_slist*& resolve_list);
  void easy_duplicate(CURL_HANDLE* easy,
                      const CURLM* multi,
                      CURL_HANDLE** easy_out,
                      CURLM** multi_out);
  CURL_HANDLE* easy_duphandle(CURL_HANDLE* easy_handle) override;
  void CheckIdle();

  /* overloaded load and unload with reference counter */

  struct ResolveHost
  {
    std::string address;
    std::set<int> ports;

    bool operator==(const ResolveHost& other) const
    {
      return address == other.address && ports == other.ports;
    }
  };
  using ResolvePins = std::map<std::string, ResolveHost>;

  /* structure holding a session info */
  typedef struct SSession
  {
    std::chrono::time_point<std::chrono::steady_clock>
        m_idletimestamp; // timestamp of when this object when idle
    std::string m_protocol;
    std::string m_hostname;
    bool m_busy;
    CURL_HANDLE* m_easy;
    CURLM* m_multi;
    // Kodi DNS cache addresses and ports to refresh before the next transfer.
    ResolvePins m_pinned;
    bool m_multiUsed = false;
    bool m_needsRenewal = false;
  } SSession;

  typedef std::vector<SSession> VEC_CURLSESSIONS;

  VEC_CURLSESSIONS m_sessions;
  CCriticalSection m_critSection;

private:
  // The caller holds m_critSection.
  SSession* FindBusySession(CURLM* multi_handle);
};
} // namespace XCURL

extern XCURL::DllLibCurlGlobal g_curlInterface;
