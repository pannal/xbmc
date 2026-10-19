/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "AudioDiagnostics.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <charconv>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <sstream>
#include <thread>

#if defined(TARGET_LINUX)
#include <fcntl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#endif

namespace AudioDiagnostics
{
std::atomic<bool> enabled{false};
static_assert(std::atomic<bool>::is_always_lock_free);
namespace
{
constexpr size_t METRICS = static_cast<size_t>(Metric::Count);
constexpr size_t MAX_BYTES = 8 * 1024 * 1024;
constexpr unsigned int MAX_SECONDS = 900;
constexpr size_t MAX_EVENTS = 32;
static_assert(std::size(METRIC_NAMES) == METRICS);

struct Aggregate
{
  uint64_t count = 0;
  int64_t sum = 0;
  int64_t minimum = 0;
  int64_t maximum = 0;
  int64_t maxAt = 0;
  int64_t last = 0;
  int64_t lastAt = 0;
  bool sumSaturated = false;

  void Add(int64_t value, int64_t now)
  {
    if (!count || value < minimum)
      minimum = value;
    if (!count || value > maximum)
    {
      maximum = value;
      maxAt = now;
    }
    ++count;
    if ((value > 0 && sum > std::numeric_limits<int64_t>::max() - value) ||
        (value < 0 && sum < std::numeric_limits<int64_t>::min() - value))
    {
      sum = value > 0 ? std::numeric_limits<int64_t>::max() : std::numeric_limits<int64_t>::min();
      sumSaturated = true;
    }
    else
      sum += value;
    last = value;
    lastAt = now;
  }
};

struct EventRecord
{
  int64_t at = 0;
  std::array<char, 256> text{};
  bool truncated = false;
};

struct State
{
  std::mutex mutex;
  std::array<std::array<Aggregate, METRICS>, 2> values{};
  std::array<int64_t, METRICS> previous{};
  std::array<EventRecord, MAX_EVENTS> events{};
  size_t eventCount = 0;
  std::atomic<uint32_t> dropped{0};
  std::mutex wakeMutex;
  std::condition_variable wake;
  std::atomic<bool> stop{false};
  std::thread worker;

  ~State()
  {
    enabled = false;
    stop = true;
    wake.notify_all();
    if (worker.joinable())
      worker.join();
  }
};

State& GetState()
{
  static State state;
  return state;
}

// Only the worker writes. Reserve footer space, even when the byte budget ends.
bool Write(FILE* file, size_t& bytes, const std::string& text)
{
  if (text.size() > MAX_BYTES - 256 || bytes > MAX_BYTES - 256 - text.size())
    return false;
  if (std::fwrite(text.data(), 1, text.size(), file) != text.size())
    return false;
  bytes += text.size();
  return true;
}

void Worker(FILE* file, unsigned int seconds, int64_t start)
{
  auto& state = GetState();
  size_t bytes = 0;
  int64_t previous = start;
  const int64_t anchor = NowUs();
  const auto wall = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::system_clock::now().time_since_epoch()).count();
  std::ostringstream header;
  header << "AUDIO_DIAG schema=1 clock=CLOCK_MONOTONIC mono_us=" << anchor
         << " wall_unix_ms=" << wall << " duration_s=" << seconds
         << " capture_start_us=" << start << " max_bytes=" << MAX_BYTES << '\n'
         << "# metrics: window_start_us,end_us,name,count,sum,min,max,max_at_us,last,last_at_us,sum_saturated\n"
         << "# no observation is unavailable, not a healthy zero; aggregates may mix streams\n";
  bool writable = Write(file, bytes, header.str()) && std::fflush(file) == 0;
  const char* reason = "deadline";
  unsigned int sequence = 0;
  while (writable)
  {
    {
      std::unique_lock<std::mutex> lock(state.wakeMutex);
      state.wake.wait_for(lock, std::chrono::seconds(1), [&] { return state.stop.load(); });
    }
    int64_t now;
    std::array<std::array<Aggregate, METRICS>, 2> values;
    std::array<EventRecord, MAX_EVENTS> events;
    size_t eventCount;
    uint32_t dropped;
    {
      // No filesystem/logging/audio calls while holding this lock. Producers
      // use try_lock and count missed observations rather than waiting for us.
      std::lock_guard<std::mutex> lock(state.mutex);
      now = NowUs();
      values = state.values;
      state.values = {};
      events = state.events;
      eventCount = state.eventCount;
      state.eventCount = 0;
      dropped = state.dropped.exchange(0);
    }
    std::ostringstream text;
    text << "WINDOW seq=" << ++sequence << " start_us=" << previous << " end_us=" << now
         << " dropped=" << dropped << '\n';
    for (size_t bank = 0; bank < values.size(); ++bank)
    {
      for (size_t i = 0; i < METRICS; ++i)
      {
        const auto& value = values[bank][i];
        if (value.count)
          text << previous << ',' << now << ',' << (bank ? "prefill." : "") << METRIC_NAMES[i]
               << ',' << value.count << ',' << value.sum << ',' << value.minimum
               << ',' << value.maximum << ',' << value.maxAt << ',' << value.last
               << ',' << value.lastAt << ',' << value.sumSaturated << '\n';
      }
    }
    for (size_t i = 0; i < eventCount; ++i)
      text << "EVENT mono_us=" << events[i].at << " truncated=" << events[i].truncated
           << ' ' << events[i].text.data() << '\n';
    writable = Write(file, bytes, text.str()) && std::fflush(file) == 0;
    previous = now;
    if (state.stop)
    {
      reason = "stop";
      break;
    }
    if (!now)
    {
      reason = "clock_unavailable";
      break;
    }
    if (now - start >= static_cast<int64_t>(seconds) * 1000000 || sequence >= seconds)
      break;
  }
  if (!writable)
    reason = "output_limit_or_io_error";
  enabled = false;
  std::fprintf(file, "END mono_us=%lld reason=%s\n", static_cast<long long>(NowUs()), reason);
  std::fclose(file);
}
} // unnamed namespace

int64_t NowUs() noexcept
{
#if defined(TARGET_LINUX)
  timespec time{};
  if (clock_gettime(CLOCK_MONOTONIC, &time) != 0)
    return 0;
  return static_cast<int64_t>(time.tv_sec) * 1000000 + time.tv_nsec / 1000;
#else
  return std::chrono::duration_cast<std::chrono::microseconds>(
             std::chrono::steady_clock::now().time_since_epoch()).count();
#endif
}

void Start(const std::string& directory)
{
#if defined(TARGET_LINUX)
  Stop();
  const std::string marker = directory + "/audio-diagnostic.enable";
  int fd = open(marker.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
  if (fd < 0)
    return;
  struct stat info{};
  char value[17]{};
  const bool valid = fstat(fd, &info) == 0 && S_ISREG(info.st_mode) && info.st_size > 0 &&
                     info.st_size < static_cast<off_t>(sizeof(value));
  const auto count = valid ? read(fd, value, sizeof(value) - 1) : -1;
  close(fd);
  if (count <= 0)
    return;
  unsigned int seconds = 0;
  const auto parsed = std::from_chars(value, value + count, seconds);
  const char* tail = parsed.ptr;
  while (tail < value + count && (*tail == '\n' || *tail == '\r' || *tail == ' ' || *tail == '\t'))
    ++tail;
  if (parsed.ec != std::errc{} || tail != value + count || seconds == 0 || seconds > MAX_SECONDS)
    return;
  const std::string path = directory + "/audio-diagnostic.log";
  fd = open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
  if (fd < 0)
    return;
  if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || fchmod(fd, 0600) != 0)
  {
    close(fd);
    return;
  }
  FILE* file = fdopen(fd, "w");
  if (!file)
  {
    close(fd);
    return;
  }
  auto& state = GetState();
  int64_t start;
  {
    std::lock_guard<std::mutex> lock(state.mutex);
    state.values = {};
    state.previous = {};
    state.eventCount = 0;
    state.dropped = 0;
    start = NowUs();
  }
  state.stop = false;
  enabled = true;
  try
  {
    state.worker = std::thread([file, seconds, start] {
      try
      {
        Worker(file, seconds, start);
      }
      catch (...)
      {
        enabled = false;
        std::fclose(file);
      }
    });
  }
  catch (...)
  {
    enabled = false;
    std::fclose(file);
  }
#endif
}

void Stop()
{
  enabled = false;
  auto& state = GetState();
  state.stop = true;
  state.wake.notify_all();
  if (state.worker.joinable())
    state.worker.join();
}

void Record(std::initializer_list<Observation> values, bool preparation) noexcept
{
  if (!Enabled())
    return;
  auto& state = GetState();
  std::unique_lock<std::mutex> lock(state.mutex, std::try_to_lock);
  if (!lock.owns_lock())
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  if (!Enabled())
    return;
  const int64_t now = NowUs();
  if (!now)
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  for (const auto& value : values)
  {
    const size_t index = static_cast<size_t>(value.metric);
    if (index < METRICS)
      state.values[preparation ? 1 : 0][index].Add(value.value, now);
  }
}

void Scaled(Metric metric, double value, bool preparation) noexcept
{
  if (!Enabled())
    return;
  const double scaled = value * 1000000.0;
  // Reject unavailable/nonfinite or unrepresentable values rather than turning
  // them into healthy zeros or invoking undefined float-to-integer conversion.
  if (!std::isfinite(scaled) || scaled <= static_cast<double>(std::numeric_limits<int64_t>::min()) ||
      scaled >= static_cast<double>(std::numeric_limits<int64_t>::max()))
  {
    GetState().dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  Record({{metric, static_cast<int64_t>(scaled)}}, preparation);
}

void Gap(Metric metric) noexcept
{
  if (!Enabled())
    return;
  auto& state = GetState();
  std::unique_lock<std::mutex> lock(state.mutex, std::try_to_lock);
  if (!lock.owns_lock())
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  const size_t index = static_cast<size_t>(metric);
  if (index >= METRICS)
    return;
  const int64_t now = NowUs();
  if (!Enabled() || !now)
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  if (state.previous[index])
    state.values[0][index].Add(now - state.previous[index], now);
  state.previous[index] = now;
}

void Event(const std::string& text) noexcept
{
  if (!Enabled())
    return;
  auto& state = GetState();
  std::unique_lock<std::mutex> lock(state.mutex, std::try_to_lock);
  if (!lock.owns_lock() || state.eventCount == MAX_EVENTS)
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  const int64_t now = NowUs();
  if (!Enabled() || !now)
  {
    state.dropped.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  auto& event = state.events[state.eventCount++];
  event.at = now;
  event.truncated = text.size() >= event.text.size();
  const size_t count = std::min(text.size(), event.text.size() - 1);
  for (size_t i = 0; i < count; ++i)
  {
    const unsigned char ch = static_cast<unsigned char>(text[i]);
    event.text[i] = ch >= 32 && ch < 127 ? static_cast<char>(ch) : '_';
  }
  event.text[count] = 0;
}

std::string SafeEndpoint(const std::string& endpoint)
{
  // ALSA endpoints can be custom plugin definitions. Keep only known endpoint
  // names and CARD/DEV identifiers; never export arbitrary plugin parameters.
  const auto colon = endpoint.find(':');
  const std::string name = endpoint.substr(0, colon);
  if (name != "surround71" && name != "hdmi" && name != "iec958" && name != "hw" &&
      name != "plughw" && name != "default" && name != "sysdefault")
    return "other_redacted";
  if (colon == std::string::npos)
    return name;
  std::string result = name + ':';
  std::istringstream parameters(endpoint.substr(colon + 1));
  std::string parameter;
  while (std::getline(parameters, parameter, ','))
  {
    std::string value;
    if (parameter.compare(0, 5, "CARD=") == 0)
      value = parameter.substr(5);
    else if (parameter.compare(0, 4, "DEV=") == 0)
      value = parameter.substr(4);
    else
      value = parameter;
    const bool numeric = !value.empty() && value.find_first_not_of("0123456789") == std::string::npos;
    const bool card = parameter.compare(0, 5, "CARD=") == 0 && !value.empty() && value.size() <= 64 &&
                      value.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_") == std::string::npos;
    const bool device = parameter.compare(0, 4, "DEV=") == 0 && numeric;
    if ((parameter == value && numeric && value.size() <= 3) || card || (device && value.size() <= 3))
      result += parameter;
    else
      result += "redacted";
    result += ',';
    if (result.size() > 160)
      break;
  }
  result.pop_back();
  return result;
}
} // namespace AudioDiagnostics
