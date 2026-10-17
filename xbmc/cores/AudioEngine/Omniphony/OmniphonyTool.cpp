/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyTool.h"

#include "dialogs/GUIDialogBusy.h"
#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "guilib/LocalizeStrings.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <mutex>

#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
#include <signal.h>

#include <fcntl.h>
#include <poll.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace ActiveAE
{

namespace
{

unsigned int ToCount(const std::string& value)
{
  return static_cast<unsigned int>(std::strtoul(value.c_str(), nullptr, 10));
}

#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
// The same two precautions CDVDAudioCodecOmniphony::CHelper::Start takes, for
// the same reasons: a descriptor installed with dup2 onto itself keeps
// FD_CLOEXEC and is closed by the exec it was meant to survive, and a helper
// that keeps Kodi's descriptors keeps the Amlogic video devices referenced.
// The settings screen can be opened over a playing film.
bool MoveClearOfStdio(int& fd)
{
  while (fd >= 0 && fd <= STDERR_FILENO)
  {
    const int moved = fcntl(fd, F_DUPFD_CLOEXEC, STDERR_FILENO + 1);
    if (moved < 0)
      return false;
    close(fd);
    fd = moved;
  }
  return true;
}

int InheritedFdCeiling()
{
  constexpr rlim_t sweepLimit = 65536;
  struct rlimit lim;
  if (getrlimit(RLIMIT_NOFILE, &lim) == 0 && lim.rlim_cur != RLIM_INFINITY &&
      lim.rlim_cur > static_cast<rlim_t>(STDERR_FILENO + 1))
    return static_cast<int>(std::min<rlim_t>(lim.rlim_cur, sweepLimit));
  return 4096;
}

void CloseAll(std::initializer_list<int> fds)
{
  for (const int fd : fds)
    if (fd >= 0)
      close(fd);
}
#endif

} // unnamed namespace

bool OmniphonyParseSofaInfo(const std::string& line, OmniphonySofaInfo& info)
{
  info = OmniphonySofaInfo{};

  // A sentence, so it is last and is read to the end; nothing after it is a
  // field even if it looks like one.
  std::string fields = line;
  const size_t reasonAt = StringUtils::StartsWith(line, "reason=") ? 0 : line.find(" reason=");
  if (reasonAt != std::string::npos)
  {
    const size_t from = line.find('=', reasonAt) + 1;
    info.reason = line.substr(from);
    fields = line.substr(0, reasonAt);
  }

  bool named = false;
  for (const std::string& field : StringUtils::Split(fields, ' '))
  {
    const size_t eq = field.find('=');
    if (eq == std::string::npos)
      continue;
    const std::string key = field.substr(0, eq);
    const std::string value = field.substr(eq + 1);
    if (key == "hrtf")
    {
      info.hrtf = value == "yes";
      named = true;
    }
    else if (key == "room")
    {
      info.room = value == "yes";
      named = true;
    }
    else if (key == "prepared")
      info.prepared = value == "yes";
    else if (key == "conventions")
      info.conventions = value;
    else if (key == "measurements")
      info.measurements = ToCount(value);
    else if (key == "receivers")
      info.receivers = ToCount(value);
    else if (key == "emitters")
      info.emitters = ToCount(value);
    else if (key == "samples")
      info.samples = ToCount(value);
    else if (key == "rate")
      info.rate = ToCount(value);
    else if (key == "orientations")
      info.orientations = ToCount(value);
    else if (key == "speakers")
      info.speakers = ToCount(value);
    else if (key == "names")
      info.names = value;
    else if (key == "source")
      info.source = value;
  }
  return named;
}

void OmniphonyParseToolAnswer(const std::string& line,
                              std::string& word,
                              std::string& reason,
                              std::string& detail)
{
  word.clear();
  reason.clear();
  detail.clear();

  const size_t space = line.find(' ');
  word = line.substr(0, space);
  if (space == std::string::npos)
    return;
  detail = line.substr(space + 1);

  if (word == "failed" && StringUtils::StartsWith(detail, "reason="))
  {
    const size_t end = detail.find(' ');
    reason = detail.substr(7, end == std::string::npos ? std::string::npos : end - 7);
    detail = end == std::string::npos ? std::string() : detail.substr(end + 1);
  }
}

std::string OmniphonyDescribeSofaInfo(const OmniphonySofaInfo& info)
{
  std::string names = info.names;
  StringUtils::Replace(names, ",", ", ");

  // Which stage takes it decides the words, because that is what the listener
  // is choosing between: an HRTF set is counted in directions, a room in its
  // loudspeakers. A set both stages take is a small free-field one, and is an
  // HRTF set first.
  if (info.room && info.prepared)
    return StringUtils::Format(g_localizeStrings.Get(39352), info.speakers, names, info.samples,
                               info.rate);
  if (info.hrtf)
    return StringUtils::Format(g_localizeStrings.Get(39350), info.conventions, info.measurements,
                               info.samples, info.rate);
  if (info.room)
    return StringUtils::Format(g_localizeStrings.Get(39351), info.speakers, names,
                               info.orientations, info.samples, info.rate);
  return StringUtils::Format(g_localizeStrings.Get(39353), info.conventions, info.measurements,
                             info.emitters, info.reason);
}

COmniphonyTool::COmniphonyTool(std::vector<std::string> args, std::string input)
  : m_args(std::move(args)), m_input(std::move(input))
{
}

std::string COmniphonyTool::HelperPath()
{
  return CSpecialProtocol::TranslatePath("special://xbmcbin/omniphony/omniphony-helper");
}

std::string COmniphonyTool::EnginePath()
{
  return CSpecialProtocol::TranslatePath("special://xbmcbin/omniphony/liborender.so");
}

void COmniphonyTool::Run()
{
  m_status = Status::Unavailable;
  m_word.clear();
  m_reason.clear();
  m_detail.clear();

#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
  const std::string exe = HelperPath();
  if (access(exe.c_str(), X_OK) != 0)
  {
    CLog::Log(LOGDEBUG, "COmniphonyTool: no helper at {}", exe);
    return;
  }

  // Built before the fork: the child may only make async-signal-safe calls
  // until it execs, and allocating is not one. A command without an input
  // works on a local file the helper opens itself, and is handed no size.
  int64_t size = 0;
  XFILE::CFile file;
  if (!m_input.empty())
  {
    if (!file.Open(m_input))
    {
      m_status = Status::Unreadable;
      return;
    }
    size = file.GetLength();
    if (size <= 0)
    {
      m_status = Status::Unreadable;
      return;
    }
  }
  std::vector<std::string> args = Arguments(size);
  std::vector<char*> argv;
  argv.push_back(const_cast<char*>(exe.c_str()));
  for (std::string& arg : args)
    argv.push_back(arg.data());
  argv.push_back(nullptr);

  // stdin is a socket rather than a pipe so that writing to a helper that has
  // already answered and left - it refuses a file it has no memory for before
  // reading a byte of it - is EPIPE from send(), not a SIGPIPE: Kodi does not
  // ignore that signal itself, it is only ignored where systemd started it.
  int input[2] = {-1, -1};
  int output[2] = {-1, -1};
  int errors[2] = {-1, -1};
  if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, input) != 0 ||
      pipe2(output, O_CLOEXEC) != 0 || pipe2(errors, O_CLOEXEC) != 0 ||
      !MoveClearOfStdio(input[1]) || !MoveClearOfStdio(output[1]) || !MoveClearOfStdio(errors[1]))
  {
    CloseAll({input[0], input[1], output[0], output[1], errors[0], errors[1]});
    return;
  }

  const int fdCeiling = InheritedFdCeiling();

  pid_t pid;
  {
    std::unique_lock<CCriticalSection> lock(m_lock);
    if (m_cancel)
    {
      CloseAll({input[0], input[1], output[0], output[1], errors[0], errors[1]});
      m_status = Status::Cancelled;
      return;
    }
    pid = fork();
    if (pid == 0)
    {
      if (dup2(input[1], STDIN_FILENO) < 0 || dup2(output[1], STDOUT_FILENO) < 0 ||
          dup2(errors[1], STDERR_FILENO) < 0)
        _exit(127);
      for (int fd = STDERR_FILENO + 1; fd < fdCeiling; ++fd)
        close(fd);
      signal(SIGPIPE, SIG_DFL);
      execv(argv[0], argv.data());
      _exit(127);
    }
    m_pid = pid;
  }
  CloseAll({input[1], output[1], errors[1]});
  if (pid < 0)
  {
    CloseAll({input[0], output[0], errors[0]});
    std::unique_lock<CCriticalSection> lock(m_lock);
    m_pid = -1;
    return;
  }

  // The helper reads all of its input before it does anything else, so the
  // bytes go first and its answer is read after; it says nothing on stdout
  // until then, and a line or two on stderr fits in the pipe meanwhile.
  std::vector<char> buffer(256 * 1024);
  bool sending = !m_input.empty();
  while (sending)
  {
    {
      std::unique_lock<CCriticalSection> lock(m_lock);
      if (m_cancel)
        break;
    }
    const ssize_t got = file.Read(buffer.data(), buffer.size());
    if (got <= 0)
      break;
    size_t off = 0;
    while (off < static_cast<size_t>(got))
    {
      const ssize_t put =
          send(input[0], buffer.data() + off, static_cast<size_t>(got) - off, MSG_NOSIGNAL);
      if (put > 0)
        off += static_cast<size_t>(put);
      else if (put < 0 && errno == EINTR)
        continue;
      else
      {
        // The helper has stopped reading, which it does only to answer: what
        // it said is on stdout.
        sending = false;
        break;
      }
    }
  }
  file.Close();
  close(input[0]);

  std::string out;
  std::string err;
  struct pollfd fds[2] = {{output[0], POLLIN, 0}, {errors[0], POLLIN, 0}};
  while (fds[0].fd >= 0 || fds[1].fd >= 0)
  {
    const int rc = poll(fds, 2, -1);
    if (rc < 0 && errno == EINTR)
      continue;
    if (rc < 0)
      break;
    for (int i = 0; i < 2; ++i)
    {
      if (fds[i].fd < 0 || !(fds[i].revents & (POLLIN | POLLHUP | POLLERR)))
        continue;
      char chunk[4096];
      const ssize_t got = read(fds[i].fd, chunk, sizeof(chunk));
      if (got > 0)
        (i == 0 ? out : err).append(chunk, static_cast<size_t>(got));
      else if (got == 0 || errno != EINTR)
      {
        close(fds[i].fd);
        fds[i].fd = -1;
      }
    }
  }
  CloseAll({fds[0].fd, fds[1].fd});

  int status = 0;
  while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
  {
  }
  bool cancelled;
  {
    std::unique_lock<CCriticalSection> lock(m_lock);
    m_pid = -1;
    cancelled = m_cancel;
  }

  // What the engine logged while it worked: a room it had to cut, a layout it
  // warned about. Not an answer, but the place someone asking why will look.
  for (const std::string& line : StringUtils::Split(err, '\n'))
    if (!line.empty())
      CLog::Log(LOGDEBUG, "COmniphonyTool: helper stderr: {}", line);

  if (cancelled)
  {
    m_status = Status::Cancelled;
    return;
  }

  const std::string line = out.substr(0, out.find('\n'));
  if (line.empty())
  {
    CLog::Log(LOGERROR, "COmniphonyTool: {} {} said nothing (status {})", exe,
              m_args.empty() ? std::string() : m_args.front(), status);
    m_status = Status::Failed;
    return;
  }
  CLog::Log(LOGDEBUG, "COmniphonyTool: {} {}: {}", m_args.empty() ? std::string() : m_args.front(),
            m_input, line);
  OmniphonyParseToolAnswer(line, m_word, m_reason, m_detail);
  m_status = Status::Done;
#endif
}

std::vector<std::string> COmniphonyTool::Arguments(int64_t size) const
{
  std::vector<std::string> args = m_args;
  if (size > 0)
    args.push_back(std::to_string(size));
  return args;
}

void COmniphonyTool::Cancel()
{
  std::unique_lock<CCriticalSection> lock(m_lock);
  m_cancel = true;
#if defined(TARGET_LINUX) && !defined(TARGET_ANDROID)
  if (m_pid > 0)
    kill(m_pid, SIGKILL);
#endif
}

COmniphonyTool::Status COmniphonyTool::Execute(bool interactive)
{
  if (!interactive)
  {
    Run();
    return m_status;
  }

  // A cancelled wait has already called Cancel, and the busy dialog joins the
  // thread that ran this before it returns. The answer a helper managed to
  // give in the meantime is not wanted: the listener said stop.
  if (!CGUIDialogBusy::Wait(this, 100, true))
    m_status = Status::Cancelled;
  return m_status;
}

COmniphonyTool::Status COmniphonyTool::Describe(const std::string& path,
                                                bool interactive,
                                                OmniphonySofaInfo& info,
                                                std::string& failure)
{
  failure.clear();
  COmniphonyTool tool({"--describe", EnginePath()}, path);
  const Status status = tool.Execute(interactive);
  if (status != Status::Done)
    return status;

  if (tool.Word() == "described")
  {
    if (!OmniphonyParseSofaInfo(tool.Detail(), info))
      failure = "internal";
  }
  else
  {
    failure = tool.Reason().empty() ? "internal" : tool.Reason();
    info.reason = tool.Detail();
  }
  return status;
}

} // namespace ActiveAE
