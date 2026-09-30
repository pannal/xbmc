/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "cores/VideoPlayer/DVDClock.h"
#include "threads/PerformanceCores.h"
#include "utils/log.h"
#include "cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h"
#include "cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.h"
#include "cores/VideoPlayer/VideoRenderers/AMLPresenter.h"
#include "cores/VideoPlayer/VideoRenderers/BaseRenderer.h"
#include "cores/VideoPlayer/VideoRenderers/OverlayRenderer.h"

// Original-session adapter. Destruction joins before releasing the clock owner;
// codecs keep ProcessInfo alive even if a deferred pool return outlives playback.
class CAMLPresenterSession
{
public:
  struct OverlayObservation
  {
    OVERLAY::CRenderer::OverlayBatch overlays;
    EFIELDSYNC field{FS_NONE};
    CAMLPresenter::Method method{CAMLPresenter::Method::SINGLE};
  };
  struct Frame final : CAMLPresenter::Frame
  {
    VideoPicture picture;
    CAMLVideoBuffer* buffer;
    Frame(const VideoPicture& source, std::shared_ptr<const OverlayObservation> overlays)
      : buffer(dynamic_cast<CAMLVideoBuffer*>(source.videoBuffer))
    {
      picture.CopyRef(source);
      pts = source.pts;
      epoch = buffer->OperationEpoch();
      method = overlays->method;
      observation = std::move(overlays);
    }
    Submission Submit(int& previousPts) override
    {
      auto codec = buffer->Codec();
      if (codec->IsOperationInvalidated(epoch))
        return Submission::INVALIDATED;
      auto permit = buffer->AcquirePresentation();
      if (!permit)
        return Submission::BLOCKED;
      if (!buffer->Submit(permit, previousPts))
        return buffer->WasSubmitted() ? Submission::REUSED : Submission::DUPLICATE;
      codec->RefreshDecoderRate(permit);
      return Submission::SUBMITTED;
    }
    bool Poll() override
    {
      auto permit = buffer->AcquirePresentation();
      if (!permit)
        return false;
      buffer->Poll(permit);
      return true;
    }
    bool Retire() override { return buffer->Drop(); }
  };

  CAMLPresenterSession(std::shared_ptr<CAMLCodec> codec,
                       CDVDClock& clock,
                       size_t capacity,
                       std::shared_ptr<const void> processInfo,
                       std::function<void(bool)> reportClock = {},
                       std::function<void(double)> reportSelection = {})
    : m_codec(std::move(codec)), m_main(std::this_thread::get_id()), m_clock(clock)
  {
    m_codec->RetainProcessInfo(std::move(processInfo));
    CAMLPresenter::Hooks hooks;
    hooks.enter = [this] {
      PERFORMANCE_CORES::ApplyCurrentThread("aml-presenter", m_codec->GetDiagnostics().id);
    };
    hooks.sampleCpu = [] { return PERFORMANCE_CORES::CurrentCpu(); };
    hooks.start = [this] {
      const bool accepted = m_codec->AcceptPresentationOwner(m_toWorker, true);
      if (accepted)
      {
        const auto state = m_codec->GetDiagnostics();
        CLog::Log(LOGINFO, "p3i-transition t_us={} session={} epoch={} owner_gen={} owner=presenter accepted",
                  PLAYBACK_DIAGNOSTICS::NowUs(), state.id, state.epoch, state.owner);
      }
      return accepted;
    };
    hooks.finish = [this] { m_toMain = m_codec->RequestPresentationOwner(m_main); };
    hooks.timing = [this, reportClock]
    {
      CAMLPresenter::Timing timing;
      double fps;
      {
        std::lock_guard<std::mutex> lock(m_mutex);
        timing.refresh = m_refresh;
        timing.latency = m_latency;
        fps = m_fps;
      }
      timing.clock = m_clock.GetClock();
      timing.speed = m_clock.GetClockSpeed();
      double speed, refresh;
      int missed;
      const bool running = m_clock.GetClockInfo(missed, speed, refresh);
      if (running && fps > 0)
      {
        double ratio = timing.refresh / (fps * speed);
        if (ratio < 1)
          ratio = 1 / ratio;
        timing.sync = std::abs(std::round(ratio) - ratio) < 0.0005;
      }
      if (reportClock)
        reportClock(timing.sync);
      return timing;
    };
    hooks.selected = std::move(reportSelection);
    hooks.adjustClock = [this](double value) { m_clock.SetVsyncAdjust(value); };
    queue = std::make_unique<CAMLPresenter>(capacity, std::move(hooks));
    m_toWorker = m_codec->RequestPresentationOwner(queue->Launch());
    if (!m_toWorker)
      throw std::runtime_error("AML presentation owner transfer refused");
    queue->Authorized();
  }
  ~CAMLPresenterSession()
  {
    if (!Stop())
      std::terminate(); // Never silently free the original owner after failed transfer.
  }
  bool Stop()
  {
    if (m_stopped)
      return true;
    queue->Stop();
    if (m_toMain)
    {
      if (!m_codec->AcceptPresentationOwner(m_toMain, true))
        return false;
    }
    else if (!m_codec->CancelPresentationOwner(m_toWorker))
      return false;
    m_stopped = true;
    return true;
  }
  void SetTiming(double refresh, double fps, double latency)
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_refresh != refresh || m_fps != fps)
      queue->ResetClock();
    m_refresh = refresh;
    m_fps = fps;
    m_latency = latency;
  }
  bool ApplyControl(const CRect& source, const CRect& destination)
  {
    const auto request = queue->PendingControl();
    if (!request.frame)
      return true;
    if (!queue->IsCurrentControl(request))
      return false;
    auto frame = std::static_pointer_cast<Frame>(request.frame);
    auto permit = m_codec->AcquireMainControl(frame->epoch);
    if (!permit)
      return false;
    frame->buffer->ApplyGeometry(permit, source, destination);
    return queue->CompleteControl(request);
  }
  CAMLSession::DiagnosticSnapshot AdmissionDiagnostics() const { return m_codec->GetDiagnostics(); }
  PLAYBACK_DIAGNOSTICS::MainService mainService; // main-owned diagnostic interval
  std::unique_ptr<CAMLPresenter> queue;

private:
  std::shared_ptr<CAMLCodec> m_codec;
  std::thread::id m_main;
  CDVDClock& m_clock;
  CAMLSession::OwnerTransfer m_toWorker, m_toMain;
  bool m_stopped{false};
  std::mutex m_mutex;
  double m_refresh{60}, m_fps{0}, m_latency{0};
};
