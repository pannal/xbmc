/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * See LICENSES/README.md for more information.
 */
#pragma once

#include "cores/VideoPlayer/Interface/TimingConstants.h"
#include "utils/PlaybackDiagnostics.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

// One bounded video executor. No renderer index, GUI pointer or GPU resource is
// accepted here. Native operations are supplied by the original AML session.
// Main owns control effects and observes complete CPU overlay compositions.
class CAMLPresenter
{
public:
  enum class Method
  {
    SINGLE,
    BLEND,
    BOB
  };
  enum class Phase
  {
    STARTING,
    RUNNING,
    STOPPING,
    STOPPED,
    FAILED
  };
  struct Timing
  {
    double clock{0}; // DVD clock units (microseconds)
    double speed{1};
    double refresh{60};
    double latency{0};
    bool sync{false};
  };
  struct Frame
  {
    double pts{0};
    uint64_t epoch{0};
    Method method{Method::SINGLE};
    bool force{false};
    bool presented{false};
    std::atomic<bool> selected{false};
    std::shared_ptr<const void> observation; // CPU only; never holds the video lease
    virtual ~Frame() = default;
    // No permit is retained on return. SUBMITTED includes a failed QBUF attempt;
    // cancellation must never return that index again. DUPLICATE remains droppable.
    enum class Submission
    {
      BLOCKED,
      DUPLICATE,
      SUBMITTED,
      REUSED,
      INVALIDATED
    };
    virtual Submission Submit(int& previousPts) = 0;
    virtual bool Poll() = 0;
    virtual bool Retire() = 0;
  };
  struct Observation
  {
    std::shared_ptr<const void> payload;
    double pts{0};
    uint64_t epoch{0};
  };
  struct ControlRequest
  {
    uint64_t generation{0};
    std::shared_ptr<Frame> frame;
  };
  struct Hooks
  {
    // Start/finish run on worker. Authorization and acceptance by main are
    // separate caller operations; these callbacks cannot impersonate main.
    std::function<void()> enter;
    std::function<int()> sampleCpu;
    std::function<bool()> start;
    std::function<void()> finish;
    std::function<Timing()> timing;
    std::function<void(double)> adjustClock;
    std::function<void(double)> selected;
  };

  CAMLPresenter(size_t capacity, Hooks hooks)
    : m_capacity(std::max<size_t>(2, capacity)), m_hooks(std::move(hooks))
  {
  }
  ~CAMLPresenter() { Stop(); }
  CAMLPresenter(const CAMLPresenter&) = delete;
  CAMLPresenter& operator=(const CAMLPresenter&) = delete;

  // The thread waits until the caller has authorized its exact thread identity.
  std::thread::id Launch()
  {
    m_thread = std::thread([this] { Run(); });
    return m_thread.get_id();
  }
  void Authorized()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_authorized = true;
    m_changed.notify_all();
  }
  Phase State() const { return m_phase.load(); }

  uint64_t Reserve(std::chrono::milliseconds timeout)
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    m_changed.wait_for(lock, timeout, [&] { return m_stop || m_count < m_capacity; });
    if (m_stop || m_count == m_capacity)
      return 0;
    const auto serial = ++m_serial;
    m_reservations.push_back(serial);
    ++m_count;
    return serial;
  }
  bool Publish(uint64_t serial, std::shared_ptr<Frame> frame)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto it = std::find(m_reservations.begin(), m_reservations.end(), serial);
    if (m_stop || !frame || it == m_reservations.end())
      return false;
    m_reservations.erase(it);
    m_queue.push_back(std::move(frame));
    ++m_progress.published;
    m_changed.notify_all();
    return true;
  }
  bool WaitSelected(const std::shared_ptr<Frame>& frame, std::chrono::milliseconds timeout)
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    return m_changed.wait_for(lock, timeout, [&] { return m_stop || frame->selected.load(); }) &&
           frame->selected;
  }
  bool IsCurrentControl(const ControlRequest& request) const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return !m_stop && request.frame && request.frame == m_pending.frame &&
           request.generation == m_pending.generation && request.generation == m_control;
  }
  void ResetClock()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_resetClock = true;
    m_changed.notify_all();
  }
  void Cancel(uint64_t serial)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto it = std::find(m_reservations.begin(), m_reservations.end(), serial);
    if (it != m_reservations.end())
    {
      m_reservations.erase(it);
      --m_count;
      m_changed.notify_all();
    }
  }
  void Show(bool show)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_show = show;
    if (!show)
      CancelQueued();
    m_changed.notify_all();
  }
  void Discard()
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    CancelQueued();
    m_changed.notify_all();
  }
  void SetControl(uint64_t generation)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_control = generation;
    m_changed.notify_all();
  }
  ControlRequest PendingControl() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_pending;
  }
  bool CompleteControl(const ControlRequest& request)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_stop || !request.frame || request.frame != m_pending.frame ||
        request.generation != m_pending.generation || request.generation != m_control)
      return false;
    m_progress.controlUs += PLAYBACK_DIAGNOSTICS::NowUs() - m_controlSinceUs;
    m_controlSinceUs = 0;
    m_applied = request.generation;
    m_appliedEpoch = request.frame->epoch;
    m_pending = {};
    m_changed.notify_all();
    return true;
  }
  Observation Observe() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_observation;
  }
  size_t Outstanding() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_count;
  }
  struct Statistics
  {
    int queued;
    int discard;
    int late;
    double pts;
    double framePts;
  };
  Statistics Stats() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    // Selection alone is not presentation: EOF must still wait for a blocked
    // submission, changed control, or second BOB pass. A completed held still
    // does not keep playback busy. Include worker-local returns in discard.
    const bool unfinished = m_current && (!m_current->presented || m_pending.frame);
    const size_t retiring = m_count - m_reservations.size() - m_queue.size() -
                            (m_current ? 1 : 0) - (m_past ? 1 : 0);
    return {static_cast<int>(m_queue.size()) + (unfinished ? 1 : 0),
            static_cast<int>(retiring), m_late / 10,
            m_renderPts, m_current ? m_current->pts : m_renderPts};
  }
  struct DiagnosticSnapshot
  {
    PLAYBACK_DIAGNOSTICS::Sample sample;
    size_t capacity, outstanding, queued, reservations, retiring;
    uint64_t control, applied, epoch, pendingControlUs, operationUs;
    int operation; // 0 idle, 1 Submit/admission, 2 Poll/admission, 3 Retire/admission
    Phase phase;
    uint64_t wakeLateMaxUs;
    int sampledCpu;
    uint64_t cpuSampleUs;
    bool show;
    double speed, clock, nextPts;
    int skipped, late;
  };
  DiagnosticSnapshot Diagnostics() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    const auto operation = m_operationToken.load(std::memory_order_relaxed);
    const auto now = PLAYBACK_DIAGNOSTICS::NowUs();
    return {{now, m_progress}, m_capacity, m_count, m_queue.size(), m_reservations.size(),
            m_count - m_reservations.size() - m_queue.size() -
                (m_current ? 1 : 0) - (m_past ? 1 : 0),
            m_control, m_applied, m_current ? m_current->epoch : 0,
            m_pending.frame ? now - m_controlSinceUs : 0,
            operation ? now - (operation >> 2) : 0, static_cast<int>(operation & 3), m_phase.load(), m_wakeLateMaxUs, m_sampledCpu, m_cpuSampleUs, m_show, m_diagnosticTiming.speed,
            m_diagnosticTiming.clock, m_queue.empty() ? DVD_NOPTS_VALUE : m_queue.front()->pts,
            m_skipped, m_late};
  }
  bool WaitIdle(std::chrono::milliseconds timeout)
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    m_forceNext = true;
    m_changed.notify_all();
    const bool completed =
        m_changed.wait_for(lock, timeout,
                           [&]
                           {
                             return m_stop || (m_queue.empty() && !m_pending.frame &&
                                               (!m_current || m_current->presented));
                           });
    m_forceNext = false;
    return completed && !m_stop;
  }
  int Skipped() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_skipped;
  }
  double RenderPts() const
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    return m_renderPts;
  }

  // No timeout is interpreted as completion. Caller must hold no lock needed by
  // native operations, and must accept the reverse owner receipt after join.
  // Native ioctl/poll return time is a platform limitation, not a promised bound.
  void Stop()
  {
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      m_stop = true;
      if (m_phase != Phase::FAILED && m_phase != Phase::STOPPED)
        m_phase = Phase::STOPPING;
      m_changed.notify_all();
    }
    if (m_thread.joinable())
      m_thread.join();
  }
  // After Stop, main may migrate these leases to the synchronous renderer.
  // No frame destruction or native callback occurs under the executor mutex.
  std::vector<std::shared_ptr<Frame>> TakeFrames()
  {
    std::unique_lock<std::mutex> lock(m_mutex);
    if (m_thread.joinable())
      return {};
    std::vector<std::shared_ptr<Frame>> frames;
    if (m_current)
      frames.push_back(std::move(m_current));
    auto past = std::move(m_past);
    for (auto& frame : m_queue)
      frames.push_back(std::move(frame));

    m_queue.clear();
    auto retired = std::move(m_retired);
    auto pending = std::move(m_pending);
    m_reservations.clear();
    m_count = 0;
    lock.unlock();
    return frames;
  }
  int PreviousPts() const { return m_previousPts; } // only after Stop

private:
  void CancelQueued() // mutex held
  {
    m_count -= m_reservations.size();
    m_reservations.clear();
    while (!m_queue.empty())
    {
      ++m_progress.discarded;
      m_queue.front()->selected = true;
      m_retired.push_back(std::move(m_queue.front()));
      m_queue.pop_front();
    }
  }
  void Select(const Timing& timing) // worker, mutex held
  {
    if (m_queue.empty() || (!m_show && !m_forceNext && !m_queue.front()->force))
      return;
    std::stable_sort(m_queue.begin(), m_queue.end(),
                     [](const auto& a, const auto& b) { return a->pts < b->pts; });
    const double duration = static_cast<double>(DVD_TIME_BASE) / timing.refresh;
    double render = timing.clock + timing.latency;
    const double next = timing.speed < 0 ? render : m_queue.front()->pts;
    if (timing.sync)
    {
      m_error += std::fmod(render - next, duration);
      if (++m_samples > 30)
      {
        m_syncOffset = m_error / m_samples;
        m_error = 0;
        m_samples = 0;
        m_hooks.adjustClock(-m_syncOffset);
      }
      render += duration / 2 - m_syncOffset;
    }
    else
      m_hooks.adjustClock(0);

    const bool combined = bool(m_past);
    if (m_past)
      m_retired.push_back(std::move(m_past));
    if (render >= next || m_forceNext || m_queue.front()->force)
    {
      if (m_current)
        m_retired.push_back(std::move(m_current));
      double diff = render - next;
      while (diff > 62000 && m_queue.size() > 2)
      {
        m_retired.push_back(std::move(m_queue.front()));
        m_queue.pop_front();
        ++m_skipped;
        ++m_progress.discarded;
        diff = render - m_queue.front()->pts;
      }
      m_late = static_cast<int>(std::max(0.0, diff / duration));
      m_current = std::move(m_queue.front());
      m_queue.pop_front();
      m_renderPts = m_current->pts - timing.latency;
    }
    else if (!combined && render > next - duration)
    {
      m_late = 0;
      m_past = std::move(m_current);
      m_current = std::move(m_queue.front());
      m_queue.pop_front();
      m_renderPts = m_current->pts - duration / 2 - timing.latency;
    }
  }
  void Run()
  {
    bool started = false;
    try
    {
      {
        std::unique_lock<std::mutex> lock(m_mutex);
        m_changed.wait(lock, [&] { return m_authorized || m_stop; });
        if (m_stop)
        {
          m_phase = Phase::STOPPED;
          return;
        }
      }
      if (m_hooks.enter)
        m_hooks.enter();
      while (!m_stop)
      {
        if (m_hooks.start())
        {
          started = true;
          break;
        }
        std::unique_lock<std::mutex> lock(m_mutex);
        m_changed.wait_for(lock, std::chrono::milliseconds(2), [&] { return m_stop.load(); });
      }
      if (started)
        m_phase = Phase::RUNNING;
      unsigned int passes = 0;
      bool submitted = false;
      bool advance = true;
      while (started && !m_stop)
      {
        if (m_hooks.sampleCpu && PLAYBACK_DIAGNOSTICS::NowUs() - m_cpuSampleUs >= 5000000)
        {
          const int cpu = m_hooks.sampleCpu();
          std::lock_guard<std::mutex> lock(m_mutex);
          m_sampledCpu = cpu;
          m_cpuSampleUs = PLAYBACK_DIAGNOSTICS::NowUs();
        }
        const Timing timing = m_hooks.timing();
        if (!(timing.refresh > 0) || !std::isfinite(timing.refresh))
          throw std::runtime_error("invalid presenter refresh rate");
        const auto nextTick = std::chrono::steady_clock::now() +
                              std::chrono::microseconds(static_cast<int64_t>(
                                  static_cast<double>(DVD_TIME_BASE) / timing.refresh));
        bool selectionChanged = false;
        std::shared_ptr<Frame> frame;
        std::vector<std::shared_ptr<Frame>> retired;
        {
          std::lock_guard<std::mutex> lock(m_mutex);
          m_diagnosticTiming = timing;
          if (m_resetClock)
          {
            m_error = 0;
            m_samples = 0;
            m_syncOffset = 0;
            m_resetClock = false;
            m_hooks.adjustClock(0);
          }
          if (advance && !passes && !m_pending.frame)
          {
            auto previous = m_current;
            Select(timing);
            if (previous != m_current)
            {
              selectionChanged = true;
              ++m_progress.selected;
              submitted = false;
              advance = false;
              m_current->selected = true;
              m_changed.notify_all();
            }
          }
          frame = m_current;
          retired.swap(m_retired);
        }
        if (selectionChanged && m_hooks.selected)
          m_hooks.selected(frame->pts);
        for (auto& old : retired)
        {
          BeginOperation(3);
          const bool returned = old->Retire();
          const auto retireUs = EndOperation();
          if (returned)
          {
            old.reset();
            std::lock_guard<std::mutex> lock(m_mutex);
            --m_count;
            ++m_progress.retired;
            m_progress.retireUs += retireUs;
            m_changed.notify_all();
          }
          else
          {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_progress.retireUs += retireUs;
            m_retired.push_back(std::move(old));
          }
        }
        if (frame)
        {
          BeginOperation(1);
          const auto result = frame->Submit(m_previousPts);
          const auto submitUs = EndOperation();
          {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_progress.submitUs += submitUs;
            if (result == Frame::Submission::SUBMITTED)
              ++m_progress.qbufAttempts;
            if (result == Frame::Submission::BLOCKED)
              ++m_progress.blocked;
          }
          if (result == Frame::Submission::SUBMITTED || result == Frame::Submission::REUSED)
            submitted = true;
          if (result == Frame::Submission::INVALIDATED)
          {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_pending = {};
            m_controlSinceUs = 0;
            ++m_progress.discarded;
            m_retired.push_back(std::move(m_current));
            advance = true;
            submitted = false;
            passes = 0;
          }
          bool ready =
              result != Frame::Submission::BLOCKED && result != Frame::Submission::INVALIDATED;
          {
            std::lock_guard<std::mutex> lock(m_mutex);
            if (submitted && (m_applied != m_control || m_appliedEpoch != frame->epoch))
            {
              if (!m_pending.frame)
                m_controlSinceUs = PLAYBACK_DIAGNOSTICS::NowUs();
              m_pending = {m_control, frame};
              ready = false;
            }
          }
          if (ready)
          {
            if (!passes)
              passes = frame->method == Method::SINGLE ? 1 : 2;
            const unsigned int attempts = frame->method == Method::BLEND ? passes : 1;
            for (unsigned int pass = 0; pass < attempts && !m_stop; ++pass)
            {
              BeginOperation(2);
              const bool polled = frame->Poll();
              const auto pollUs = EndOperation();
              std::lock_guard<std::mutex> lock(m_mutex);
              m_progress.pollUs += pollUs;
              if (!polled)
                break;
              --passes;
              ++m_progress.polls;
              m_observation = {frame->observation, frame->pts, frame->epoch};
              advance = passes == 0;
              if (advance && !frame->presented)
              {
                ++m_progress.completed; // software pass completion, not scanout
                frame->presented = true;
              }
              m_changed.notify_all();
            }
          }
        }
        std::unique_lock<std::mutex> lock(m_mutex);
        m_changed.wait_until(lock, nextTick, [&] { return m_stop.load(); });
        const auto late = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - nextTick).count();
        if (late > 0)
          m_wakeLateMaxUs = std::max(m_wakeLateMaxUs, static_cast<uint64_t>(late));
      }
      if (started)
        m_hooks.finish();
      m_phase = Phase::STOPPED;
    }
    catch (...)
    {
      m_stop = true;
      m_phase = Phase::FAILED;
      // A failed operation cannot acknowledge an owner transfer.
    }
  }

  void BeginOperation(int operation)
  {
    // No additional hot-path mutex: one token publishes operation and start.
    m_operationToken.store((PLAYBACK_DIAGNOSTICS::NowUs() << 2) | operation,
                           std::memory_order_relaxed);
  }
  uint64_t EndOperation()
  {
    const auto token = m_operationToken.exchange(0, std::memory_order_relaxed);
    return PLAYBACK_DIAGNOSTICS::NowUs() - (token >> 2);
  }
  Timing m_diagnosticTiming;
  PLAYBACK_DIAGNOSTICS::Progress m_progress;
  uint64_t m_controlSinceUs{0};
  std::atomic<uint64_t> m_operationToken{0};
  int m_sampledCpu{-1};
  uint64_t m_wakeLateMaxUs{0}, m_cpuSampleUs{0};
  const size_t m_capacity;
  Hooks m_hooks;
  mutable std::mutex m_mutex;
  std::condition_variable m_changed;
  std::thread m_thread;
  std::atomic<Phase> m_phase{Phase::STARTING};
  std::atomic<bool> m_stop{false};
  bool m_authorized{false};
  bool m_show{false};
  bool m_forceNext{false};
  bool m_resetClock{false};
  uint64_t m_serial{0};
  size_t m_count{0};
  std::vector<uint64_t> m_reservations;
  std::deque<std::shared_ptr<Frame>> m_queue;
  std::shared_ptr<Frame> m_current, m_past;
  std::vector<std::shared_ptr<Frame>> m_retired;
  Observation m_observation;
  ControlRequest m_pending;
  uint64_t m_control{1}, m_applied{0}, m_appliedEpoch{0};
  double m_error{0}, m_syncOffset{0}, m_renderPts{DVD_NOPTS_VALUE};
  int m_samples{0}, m_skipped{0}, m_previousPts{-1}, m_late{-1};
};
