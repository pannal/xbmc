/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <string>
#include <sys/ioctl.h>
#include <unistd.h>

namespace KODI::WINDOWING::AML
{
// Matches osd_gui_wait.h. A copied-out sentinel distinguishes old drivers which
// return success for unknown ioctls. Neither command changes the 64-bit clock wait.
constexpr auto GUI_WAIT_CREATE = _IOR('F', 0x22, int32_t);
constexpr auto GUI_WAIT_SET = _IOW('F', 0x23, uint32_t);

struct NativeGuiWaitIO
{
  static int Open(const std::string& path) { return open(path.c_str(), O_RDWR | O_CLOEXEC); }
  static int Create(int fd, int32_t* lease) { return ioctl(fd, GUI_WAIT_CREATE, lease); }
  static int Set(int fd, uint32_t* enabled) { return ioctl(fd, GUI_WAIT_SET, enabled); }
  static void Close(int fd) { close(fd); }
};

// Main-owned lease; kernel matches all Mali callback threads in this process.
// Explicit disable precedes close so a fork/dup cannot keep the old ON policy.
template<class IO = NativeGuiWaitIO>
class NativeGuiWait
{
public:
  NativeGuiWait() = default;
  NativeGuiWait(const NativeGuiWait&) = delete;
  NativeGuiWait& operator=(const NativeGuiWait&) = delete;
  ~NativeGuiWait() { Reset(); }

  bool Apply(bool enabled, const std::string& path)
  {
    if (!enabled)
      return Reset();
    if (m_enabled)
      return true;
    if (m_failed)
      return false; // No per-frame open/ioctl retry stream on an old kernel.
    const int fb = IO::Open(path);
    if (fb < 0)
      return Fail(errno);
    int32_t lease = -1;
    const int result = IO::Create(fb, &lease);
    const int error = errno;
    IO::Close(fb);
    if (result < 0 || lease < 0)
      return Fail(result < 0 ? error : ENOTTY);
    m_fd = lease;
    uint32_t value = 1;
    if (IO::Set(m_fd, &value) < 0)
    {
      const int setError = errno;
      IO::Close(m_fd);
      m_fd = -1;
      return Fail(setError);
    }
    m_enabled = true;
    m_error = 0;
    return true;
  }
  bool Reset()
  {
    int error = 0;
    if (m_fd >= 0)
    {
      uint32_t value = 0;
      if (IO::Set(m_fd, &value) < 0)
        error = errno;
      IO::Close(m_fd);
      m_fd = -1;
    }
    m_enabled = m_failed = false;
    m_error = error;
    return !error;
  }
  bool Enabled() const { return m_enabled; }
  int Error() const { return m_error; }
private:
  bool Fail(int error)
  {
    m_failed = true;
    m_error = error;
    return false;
  }
  int m_fd{-1};
  int m_error{0};
  bool m_enabled{false}, m_failed{false};
};
}
