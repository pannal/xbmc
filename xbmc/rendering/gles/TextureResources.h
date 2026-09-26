/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include <cstdint>
#include <mutex>
#include <set>
#include <thread>
#include <vector>

// One primary-context incarnation. CPU owners may retire names from any thread;
// only the creating thread may drain/close with GL deletion. Surface replacement
// does not replace this namespace. No shared-context consumer is admitted here.
class CGLESTextureResources
{
public:
  bool IsOwner() const { return m_owner == std::this_thread::get_id(); }
  bool IsOpen() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_open;
  }
  void Register(uint32_t texture)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_open && IsOwner() && texture)
      m_live.insert(texture);
  }
  void Retire(uint32_t texture)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_open && m_live.erase(texture))
      m_retired.push_back(texture);
  }
  std::vector<uint32_t> TakeRetired()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    std::vector<uint32_t> retired;
    if (m_open && IsOwner())
      retired.swap(m_retired);
    return retired;
  }
  std::vector<uint32_t> Close()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    std::vector<uint32_t> retired;
    if (!IsOwner())
      return retired;
    retired.swap(m_retired);
    retired.insert(retired.end(), m_live.begin(), m_live.end());
    m_live.clear();
    m_open = false;
    return retired;
  }

private:
  const std::thread::id m_owner{std::this_thread::get_id()};
  mutable std::mutex m_mutex;
  bool m_open{true};
  std::set<uint32_t> m_live;
  std::vector<uint32_t> m_retired;
};
