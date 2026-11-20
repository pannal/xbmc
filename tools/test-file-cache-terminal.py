#!/usr/bin/env python3
"""Host integration checks for FileCache terminal reads and seeks on unknown or growing files.

Compiles the working tree's whole cache implementations, events and POSIX locks.
Adapters supply a controlled source, demand-driven data, local disk descriptors,
thread startup and the settings service. No Kodi binary or network is required.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

SUPPORT = r"""
#pragma once
#include "filesystem/IFile.h"
#include "threads/Event.h"

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <memory>
#include <thread>
#include <unistd.h>

using namespace std::chrono_literals;
inline int GetLastError() { return errno; }
inline void SetLastError(int error) { errno = error; }
class CURL
{
public:
  explicit CURL(std::string path): m_path(std::move(path)) {}
  const std::string& Get() const { return m_path; }
  const std::string& GetRedacted() const { return m_path; }
private:
  std::string m_path;
};

struct CLog { template<class... Args> static void Log(Args&&...) {} };
constexpr int LOGERROR=0, LOGWARNING=1, LOGDEBUG=2;
namespace fmt { template<class T> T* ptr(T* p) { return p; } }

struct Settings
{
  int memoryMiB = 1;
  int readFactor = 400;
  int GetInt(int setting) const { return setting == 0 ? memoryMiB : setting == 1 ? 64 : readFactor; }
};
struct CSettings
{
  enum { SETTING_FILECACHE_MEMORYSIZE, SETTING_FILECACHE_CHUNKSIZE, SETTING_FILECACHE_READFACTOR };
};
struct CServiceBroker
{
  static CServiceBroker* GetSettingsComponent() { static CServiceBroker service; return &service; }
  static Settings* GetSettings() { static Settings settings; return &settings; }
  static CServiceBroker& GetLogging() { return *GetSettingsComponent(); }
  bool IsLogLevelLogged(int) const { return false; }
};

inline CEvent seekWaiting{true};
class CThread
{
public:
  explicit CThread(const char*) {}
  virtual ~CThread() = default;
  virtual void Process() = 0;
  virtual void OnExit() = 0;
  void Create(bool)
  {
    m_bStop = false;
    m_stopEvent.Reset();
    m_running = true;
    m_worker = std::thread([this] { Process(); OnExit(); m_running = false; });
  }
  bool IsRunning() const { seekWaiting.Set(); return m_running; }
  virtual void StopThread(bool wait = true)
  {
    m_bStop = true;
    m_stopEvent.Set();
    if (wait && m_worker.joinable())
      m_worker.join();
  }
protected:
  enum { WAIT_SIGNALED, WAIT_INTERRUPTED };
  int AbortableWait(CEvent& event)
  {
    XbmcThreads::CEventGroup group{&event, &m_stopEvent};
    return group.wait() == &event ? WAIT_SIGNALED : WAIT_INTERRUPTED;
  }
  std::atomic<bool> m_bStop{false};
private:
  CEvent m_stopEvent{true};
  std::atomic<bool> m_running{false};
  std::thread m_worker;
};

struct CUtil
{
  static std::string GetNextFilename(const char*, int)
  {
    char path[] = "terminal-file-cache-XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    close(fd);
    return path;
  }
};
struct CSpecialProtocol { static std::string TranslatePath(std::string path) { return path; } };

namespace XFILE
{
inline int64_t failDiskWriteAt = -1;
inline CEvent diskWriteFailed{true};
class CPosixFile : public IFile
{
public:
  bool Open(const CURL& url) override { return (m_fd = open(url.Get().c_str(), O_RDONLY)) >= 0; }
  bool OpenForWrite(const CURL& url, bool) override
  {
    return (m_fd = open(url.Get().c_str(), O_WRONLY | O_TRUNC)) >= 0;
  }
  bool Exists(const CURL&) override { return false; }
  int Stat(const CURL&, struct __stat64*) override { return -1; }
  int64_t GetPosition() override { return lseek(m_fd, 0, SEEK_CUR); }
  int64_t GetLength() override { return -1; }
  bool Delete(const CURL& url) override { return unlink(url.Get().c_str()) == 0; }
  void Close() override { if (m_fd >= 0) { close(m_fd); m_fd = -1; } }
  ssize_t Read(void* out, size_t count) override { return read(m_fd, out, count); }
  ssize_t Write(const void* in, size_t count) override
  {
    if (failDiskWriteAt >= 0 && GetPosition() >= failDiskWriteAt)
    {
      diskWriteFailed.Set();
      errno = ENOSPC;
      return -1;
    }
    return write(m_fd, in, count);
  }
  int64_t Seek(int64_t pos, int whence) override { return lseek(m_fd, pos, whence); }
private:
  int m_fd = -1;
};

struct Source
{
  std::atomic<int64_t> length{0};
  std::atomic<int64_t> bytes{8};
  int64_t pauseAt = -1;
  bool failEndSeek = false;
  std::atomic<unsigned> endSeeks{0};
  CEvent paused{true};
  CEvent releaseTail{true};
  int64_t lengthAfterSeek = 0;
  bool gateUntilSeek = false;
  ssize_t terminal = 0;
  bool wrongSeek = false;
  bool failSeek = false;
  std::atomic<int64_t> lastSeek{-1};
  std::atomic<unsigned int> reads{0};
  CEvent allowRead{true};
  CEvent terminalRead{true};

  static char ByteAt(int64_t position) { return static_cast<char>((position % 113) + 1); }
};
inline Source* source = nullptr;
class CFile
{
public:
  bool Open(const std::string&, unsigned) { return true; }
  void Close() {}
  static bool Exists(const std::string&) { return false; }
  static int Stat(const std::string&, struct __stat64*) { return -1; }
  static unsigned DetermineChunkSize(unsigned supplied, unsigned) { return supplied; }
  int GetChunkSize() { return 64; }
  int IoControl(EIoControl request, void* param)
  {
    if (request == IOCTRL_READ_INTERRUPT)
      m_interrupt = *static_cast<std::function<bool()>*>(param);
    return request == IOCTRL_SEEK_POSSIBLE;
  }
  int64_t GetLength() const { return source->length; }
  IFile* GetImplementation() const { return nullptr; }
  int64_t Seek(int64_t position, int whence)
  {
    if (whence == SEEK_END)
    {
      ++source->endSeeks;
      if (source->failEndSeek)
        return -1;
      position += source->bytes.load();
    }
    else
      assert(whence == SEEK_SET);
    source->lastSeek = position;
    if (source->failSeek)
      return -1;
    if (source->lengthAfterSeek > 0)
      source->length = source->lengthAfterSeek;
    m_position = position + (source->wrongSeek ? 1 : 0);
    return m_position;
  }
  ssize_t Read(void* buf, size_t count)
  {
    ++source->reads;
    while (!source->allowRead.Wait(5ms))
      if (m_interrupt && m_interrupt())
        return -1;
    if (m_position == source->pauseAt)
    {
      source->paused.Set();
      while (!source->releaseTail.Wait(5ms))
        if (m_interrupt && m_interrupt())
          return -1;
    }
    const int64_t end = source->gateUntilSeek && source->lastSeek.load() < 0 ? 8 : source->bytes.load();
    if (m_position >= end)
    {
      source->terminalRead.Set();
      return source->terminal;
    }
    const size_t amount = std::min<int64_t>(count, end - m_position);
    for (size_t i = 0; i < amount; ++i)
      static_cast<char*>(buf)[i] = Source::ByteAt(m_position++);
    return amount;
  }
private:
  int64_t m_position = 0;
  std::function<bool()> m_interrupt;
};
}
"""

TESTS = r"""
#include "support.h"
#include "filesystem/FileCache.h"

#include <iostream>
#include <future>
using namespace XFILE;

IFile::IFile() = default;
IFile::~IFile() = default;
bool IFile::ReadString(char*, int) { return false; }
int IFile::Stat(struct __stat64*) { return -1; }

struct Fixture
{
  Source state;
  CFileCache cache;
  explicit Fixture(unsigned flags) : cache(flags) { source = &state; }
  void Open()
  {
    assert(cache.Open(CURL{"test://source"}));
    state.allowRead.Set();
  }
  ~Fixture() { cache.Close(); }
};

void TestTerminal(unsigned flags, ssize_t terminal, int64_t length)
{
  Fixture f(flags);
  f.state.length = length;
  f.state.terminal = terminal;
  f.Open();
  if (length <= 0)
    assert(f.state.terminalRead.Wait(2s));
  char data[8]{};
  assert(f.cache.Read(data, 3) == 3);
  assert(f.cache.Read(data + 3, 5) == 5);
  for (size_t i = 0; i < sizeof(data); ++i)
    assert(data[i] == Source::ByteAt(i));
  assert(f.cache.Read(data, 0) == 0);
  const ssize_t expected = terminal < 0 ? -1 : 0;
  errno = 0;
  assert(f.cache.Read(data, 1) == expected);
  if (expected < 0)
    assert(errno == EIO);
  errno = 0;
  assert(f.cache.Read(data, 1) == expected);
  if (expected < 0)
    assert(errno == EIO);
}

void TestUnknownSeek(unsigned flags, int64_t length, bool fail, bool wrong)
{
  Fixture f(flags);
  constexpr int64_t target = 700003;
  f.state.length = length;
  f.state.bytes = target + 8;
  f.state.gateUntilSeek = true;
  f.state.failSeek = fail;
  f.state.wrongSeek = wrong;
  f.Open();
  assert(f.state.terminalRead.Wait(2s));
  const auto position = f.cache.Seek(target, SEEK_SET);
  assert(f.state.lastSeek == target);
  if (fail || wrong)
  {
    assert(position == -1);
    char pending[8]{};
    assert(f.cache.Read(pending, sizeof(pending)) == sizeof(pending));
    for (size_t i = 0; i < sizeof(pending); ++i)
      assert(pending[i] == Source::ByteAt(i));
    errno = 0;
    assert(f.cache.Read(pending, 1) == -1 && errno == EIO);
    f.state.failSeek = false;
    f.state.wrongSeek = false;
    assert(f.cache.Seek(target, SEEK_SET) == target);
    assert(f.cache.Read(pending, 1) == 1 && pending[0] == Source::ByteAt(target));
    return;
  }
  assert(position == target && f.cache.GetPosition() == target);
  char byte = 0;
  assert(f.cache.Read(&byte, 1) == 1 && byte == Source::ByteAt(target));
  assert(f.cache.Seek(0, SEEK_SET) == 0);
  assert(f.cache.Read(&byte, 1) == 1 && byte == Source::ByteAt(0));
}

void TestDiskWriteError(unsigned flags)
{
  Fixture f(flags);
  f.state.bytes = 72;
  failDiskWriteAt = 64;
  diskWriteFailed.Reset();
  f.Open();
  assert(diskWriteFailed.Wait(2s));
  char bytes[64]{};
  assert(f.cache.Read(bytes, sizeof(bytes)) == sizeof(bytes));
  for (size_t i = 0; i < sizeof(bytes); ++i)
    assert(bytes[i] == Source::ByteAt(i));
  errno = 0;
  assert(f.cache.Read(bytes, 1) == -1 && errno == EIO);
  f.cache.Close();
  failDiskWriteAt = -1;
}

void TestCancelledRead(unsigned flags)
{
  Fixture f(flags);
  // Keep the source blocked throughout. The observer runs after a cache wait.
  bool waited = false;
  std::function<void()> observe = [&] { waited = true; };
  std::function<bool()> interrupt = [&] { return waited; };
  assert(f.cache.Open(CURL{"test://source"}));
  assert(f.cache.IoControl(IOCTRL_CACHE_SET_WAIT_CALLBACK, &observe) == 0);
  assert(f.cache.IoControl(IOCTRL_READ_INTERRUPT, &interrupt) == 0);
  char byte{};
  assert(f.cache.Read(&byte, 1) == -1);
  assert(waited && f.cache.GetPosition() == 0);
  // Close joins the worker while the source is blocked in its Read function.
  f.cache.Close();
}

void TestGrowingSeek(unsigned flags, bool publishGrowth)
{
  Fixture f(flags);
  constexpr int64_t initial = 4096;
  constexpr int64_t target = 700003;
  f.state.length = initial;
  f.state.bytes = target + 8;
  if (publishGrowth)
    f.state.lengthAfterSeek = target + 8;
  f.Open();
  // The file's positive length report lags its readable data until seek (or later).
  char byte{};
  assert(f.cache.Read(&byte, 1) == 1 && byte == Source::ByteAt(0));
  assert(f.cache.GetLength() == initial);
  assert(f.cache.Seek(target, SEEK_SET) == target);
  assert(f.state.lastSeek == target && f.cache.GetPosition() == target);
  assert(f.cache.Read(&byte, 1) == 1 && byte == Source::ByteAt(target));
}

void TestGrowthAfterEOF(unsigned flags)
{
  Fixture f(flags);
  f.state.length = 8;
  f.state.bytes = 8;
  f.Open();
  char bytes[8]{};
  assert(f.cache.Read(bytes, sizeof(bytes)) == sizeof(bytes));
  assert(f.cache.Read(bytes, 1) == 0);
  f.state.bytes = 12;
  f.state.length = 12;
  assert(f.cache.Seek(8, SEEK_SET) == 8);
  assert(f.cache.Read(bytes, 1) == 1 && bytes[0] == Source::ByteAt(8));
}

void TestEndSeekAfterGrowth(unsigned flags)
{
  Fixture f(flags);
  f.state.length = 8;
  f.Open();
  char bytes[8]{};
  assert(f.cache.Read(bytes, sizeof(bytes)) == sizeof(bytes));
  assert(f.cache.Read(bytes, 1) == 0);
  f.state.bytes = 12; // The reported size stays at eight.
  assert(f.cache.Seek(-1, SEEK_END) == 11);
  assert(f.cache.GetPosition() == 11 && f.state.endSeeks > 0);
  assert(f.cache.Read(bytes, 1) == 1 && bytes[0] == Source::ByteAt(11));
}

void TestEndSeekWithActiveCache(unsigned flags)
{
  Fixture f(flags);
  f.state.length = 192;
  f.state.bytes = 192;
  f.state.pauseAt = 64;
  f.Open();
  assert(f.state.paused.Wait(2s)); // The first 64 bytes are already cached.
  seekWaiting.Reset();
  auto seek = std::async(std::launch::async, [&] { return f.cache.Seek(-164, SEEK_END); });
  // The reader has handed the seek to the worker, blocked on chunk two.
  assert(seekWaiting.Wait(2s));
  f.state.releaseTail.Set();
  assert(seek.wait_for(2s) == std::future_status::ready && seek.get() == 28);
  // Cross the end of the original cache and verify reads resume at the right source byte.
  char bytes[48]{};
  int64_t pos = 28;
  while (pos < 192)
  {
    const ssize_t count = f.cache.Read(bytes, sizeof(bytes));
    assert(count > 0 && pos + count <= 192);
    for (ssize_t i = 0; i < count; ++i)
      assert(bytes[i] == Source::ByteAt(pos++));
  }
  assert(f.cache.Read(bytes, 1) == 0);
}

void TestEndProbeFailure(unsigned flags, int64_t reportedLength)
{
  Fixture f(flags);
  f.state.length = reportedLength;
  f.state.bytes = 12;
  f.state.failEndSeek = true;
  f.Open();
  assert(f.state.terminalRead.Wait(2s));
  assert(f.cache.Seek(-1, SEEK_END) == -1);
  assert(f.state.endSeeks > 0);
}

void TestProbeAtReportedEnd(unsigned flags)
{
  Fixture f(flags);
  f.state.length = 4;
  f.state.bytes = 8;
  f.Open();
  char bytes[8]{};
  size_t total = 0;
  while (total < sizeof(bytes))
  {
    const ssize_t n = f.cache.Read(bytes + total, sizeof(bytes) - total);
    assert(n > 0);
    total += n;
  }
  for (size_t i = 0; i < total; ++i)
    assert(bytes[i] == Source::ByteAt(i));
  assert(f.cache.Read(bytes, 1) == 0);
}

int main(int argc, char** argv)
{
  const std::string scenario = argc > 1 ? argv[1] : "all";
  auto run = [&](const char* name) { return scenario == "all" || scenario == name; };
  if (run("seek"))
  {
    auto* settings = CServiceBroker::GetSettings();
    settings->memoryMiB = 0;
    settings->readFactor = 0;
    TestUnknownSeek(READ_AUDIO_VIDEO, 0, false, false);
    settings->readFactor = 400;
  }
  for (int memory : {0, 1})
  {
    CServiceBroker::GetSettings()->memoryMiB = memory;
    for (unsigned flags : {READ_AUDIO_VIDEO, READ_AUDIO_VIDEO | READ_MULTI_STREAM})
    {
      if (run("read-error"))
      {
        TestTerminal(flags, -1, 0);
        TestTerminal(flags, -1, 20);
      }
      if (run("eof"))
      {
        TestTerminal(flags, 0, 0);
        TestTerminal(flags, 0, 8);
      }
      for (int64_t length : {0, -1})
      {
        if (run("seek"))
          TestUnknownSeek(flags, length, false, false);
        if (run("seek-failure"))
        {
          TestUnknownSeek(flags, length, true, false);
          TestUnknownSeek(flags, length, false, true);
        }
      }
      if (run("growing"))
      {
        TestGrowingSeek(flags, false);
        TestGrowingSeek(flags, true);
        TestGrowthAfterEOF(flags);
      }
      if (run("seek-end"))
      {
        TestEndSeekAfterGrowth(flags);
        TestEndSeekWithActiveCache(flags);
        TestEndProbeFailure(flags, -1);
        TestEndProbeFailure(flags, 8);
        TestProbeAtReportedEnd(flags);
      }
      if (run("cancel"))
        TestCancelledRead(flags);
      if (memory == 0 && run("disk-error"))
        TestDiskWriteError(flags);
      std::cout << "PASS " << (memory ? "circular" : "disk")
                << ((flags & READ_MULTI_STREAM) ? " double" : " single") << '\n';
    }
  }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--case', default='all', choices=['all', 'read-error', 'eof', 'seek',
                        'seek-failure', 'seek-end', 'growing', 'cancel', 'disk-error'])
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    root = args.root.resolve() / 'xbmc'
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        for name in ('FileCache', 'CacheStrategy', 'CircularCache'):
            for suffix in ('cpp', 'h'):
                path = Path('filesystem') / f'{name}.{suffix}'
                (build / path).parent.mkdir(exist_ok=True)
                shutil.copy(root / path, build / path)
        (build / 'support.h').write_text(SUPPORT)
        (build / 'test.cpp').write_text(TESTS)
        (build / 'PlatformDefs.h').write_text('#pragma once\n#define __stat64 stat\n')
        for header in (
                'filesystem/File.h', 'filesystem/SpecialProtocol.h', 'threads/Thread.h',
                'URL.h', 'Util.h', 'ServiceBroker.h', 'settings/Settings.h',
                'settings/SettingsComponent.h', 'utils/log.h', 'platform/posix/ConvUtils.h',
                'platform/posix/filesystem/PosixFile.h'):
            path = build / header
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('#include "support.h"\n')
        # Local includes in the cache sources also include the real IFile declaration.
        for header in ('IFile.h', 'IFileTypes.h'):
            shutil.copy(root / 'filesystem' / header, build / 'filesystem' / header)
        command = ['g++', '-std=c++17', '-O1', '-g', '-pthread', '-DTARGET_POSIX',
                   '-I', str(build), '-I', str(root), '-include', 'support.h',
                   str(root / 'threads/Event.cpp'),
                   str(root / 'platform/posix/threads/RecursiveMutex.cpp')]
        command += [str(build / 'filesystem' / f'{name}.cpp')
                    for name in ('FileCache', 'CacheStrategy', 'CircularCache')]
        command += [str(build / 'test.cpp'), '-o', str(build / 'run')]
        subprocess.run(command, check=True)
        for _ in range(args.repeat):
            subprocess.run([build / 'run', args.case], cwd=build, check=True, timeout=90)


if __name__ == '__main__':
    main()
