/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <optional>
#include <mutex>

// Observation only. No playback policy, persisted state, or native admission.
namespace PLAYBACK_DIAGNOSTICS
{
inline const auto origin = std::chrono::steady_clock::now();
inline std::atomic<uint64_t> nextId{0};
inline uint64_t NextId() { return ++nextId; }
inline uint64_t NowUs()
{
  return std::chrono::duration_cast<std::chrono::microseconds>(
             std::chrono::steady_clock::now() - origin).count();
}

struct Duration
{
  uint64_t calls{0}, totalUs{0}, maxUs{0};
  void Add(uint64_t elapsed)
  {
    ++calls;
    totalUs += elapsed;
    if (elapsed > maxUs)
      maxUs = elapsed;
  }
};
class CaptureTimings
{
public:
  struct Report
  {
    uint64_t fromUs, toUs, failures;
    Duration duration;
  };
  std::optional<Report> Complete(uint64_t start, uint64_t end, bool success)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (!m_since)
      m_since = start;
    m_duration.Add(end - start);
    if (!success)
      ++m_failures;
    if (m_reported && end - m_lastReport < 5000000)
      return std::nullopt;
    Report result{m_since, end, m_failures, m_duration};
    m_lastReport = end;
    m_reported = true;
    m_since = end;
    m_failures = 0;
    m_duration = {};
    return result;
  }
private:
  std::mutex m_mutex;
  uint64_t m_since{0}, m_lastReport{0}, m_failures{0};
  bool m_reported{false};
  Duration m_duration;
};
struct VideoStages
{
  uint64_t sinceUs{NowUs()}, loopUs{sinceUs}, maxLoopUs{0}, modes{0};
  uint64_t messagesTimedOut{0}, inputEmpty{0}, addRejected{0};
  uint64_t decoderNone{0}, decoderNoBuffer{0};
  uint64_t decoderBuffer{0}, pictures{0}, decoderErrors{0}, decoderEof{0}, capacityRejected{0};
  Duration input, add, decode, capacity, lifecycle, publish;
  void BeginIteration(uint64_t now, unsigned int mode)
  {
    if (now - loopUs > maxLoopUs)
      maxLoopUs = now - loopUs;
    loopUs = now;
    modes |= uint64_t{1} << mode;
  }
};
struct Progress
{
    uint64_t published{0};
    uint64_t selected{0};
    uint64_t qbufAttempts{0};
    uint64_t completed{0};
    uint64_t polls{0};
    uint64_t retired{0};
    uint64_t discarded{0};
    uint64_t blocked{0};
    uint64_t submitUs{0};
    uint64_t pollUs{0};
    uint64_t retireUs{0};
    uint64_t controlUs{0};
  Progress Since(const Progress& before) const
  {
    Progress result;
      result.published = published - before.published;
      result.selected = selected - before.selected;
      result.qbufAttempts = qbufAttempts - before.qbufAttempts;
      result.completed = completed - before.completed;
      result.polls = polls - before.polls;
      result.retired = retired - before.retired;
      result.discarded = discarded - before.discarded;
      result.blocked = blocked - before.blocked;
      result.submitUs = submitUs - before.submitUs;
      result.pollUs = pollUs - before.pollUs;
      result.retireUs = retireUs - before.retireUs;
      result.controlUs = controlUs - before.controlUs;
    return result;
  }
};

struct Sample
{
  uint64_t atUs{0};
  Progress progress;
};
struct Gap
{
  uint64_t fromUs{0}, toUs{0};
  Progress progress; // Only work between these two main-service observations.
};
struct Report
{
  Sample current;
  Progress interval;
  uint64_t fromUs{0}, gaps{0};
  Gap worst;
};
// Main-owned; snapshots are captured under the executor's existing mutex.
// Retain the worst measured gap with its own counters, never lifetime totals.
class MainService
{
public:
  std::optional<Report> Observe(const Sample& sample, bool flush = false)
  {
    if (!m_previous)
    {
      m_previous = m_start = sample;
      return std::nullopt;
    }
    if (sample.atUs - m_previous->atUs >= 250000)
    {
      ++m_gaps;
      if (sample.atUs - m_previous->atUs > m_worst.toUs - m_worst.fromUs)
        m_worst = {m_previous->atUs, sample.atUs,
                   sample.progress.Since(m_previous->progress)};
    }
    m_previous = sample;
    if (!flush && sample.atUs - m_start->atUs < 5000000)
      return std::nullopt;
    Report result{sample, sample.progress.Since(m_start->progress), m_start->atUs,
                  m_gaps, m_worst};
    m_start = sample;
    m_gaps = 0;
    m_worst = {};
    return result;
  }
private:
  std::optional<Sample> m_previous, m_start;
  uint64_t m_gaps{0};
  Gap m_worst;
};
} // namespace PLAYBACK_DIAGNOSTICS
