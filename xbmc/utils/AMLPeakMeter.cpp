/*
 *  Copyright (C) 2026 Team CoreELEC
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AMLPeakMeter.h"

#include "ServiceBroker.h"
#include "cores/DataCacheCore.h"
#include "utils/ScreenshotAML.h"
#include "platform/linux/SysfsPath.h"
#include "utils/log.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <mutex>
#include <vector>

#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace
{
constexpr const char* PEAK_PARAM = "/sys/module/amdolby_vision/parameters/xbmc_dv_new_hdr10_meas_peak";
constexpr const char* CAPTURE_DEVICE = "/dev/amvideocap0";
constexpr unsigned long AMVIDEOCAP_IOW_SET_WANTFRAME_WIDTH = _IOW('V', 0x02, int);
constexpr unsigned long AMVIDEOCAP_IOW_SET_WANTFRAME_HEIGHT = _IOW('V', 0x03, int);

constexpr auto PERIOD = std::chrono::milliseconds(100); // 10 captures per second
constexpr int SAMPLES = 32000;                          // maxRGB samples per capture
constexpr float PERCENTILE = 0.9995f;                   // ignore a few isolated pixels
// Shot cut: mean absolute change of the 16x9 grid of mean maxRGB codes between
// two captures. In a calm shot it stays below ~4 codes even in dark scenes and
// cuts give 7-50; fast motion, strobes and effects reach 10-30 for seconds
// (Matrix Resurrections 3:25-3:33). So a cut must pass CUT_DIFF and be
// CUT_RATIO times the median of the last CUT_HISTORY changes, and a shot lasts
// at least MIN_SHOT_MS: a quick montage acts as one shot (the peak only rises).
constexpr float CUT_DIFF = 8.0f;
constexpr float CUT_RATIO = 2.5f;
constexpr size_t CUT_HISTORY = 8;
constexpr int64_t MIN_SHOT_MS = 1000;
// Safety for a long shot that got much darker (slow pan): after 15 s, when the
// last 2 s stayed under 70 % of the shot peak, release 3 % per capture.
constexpr int64_t RELEASE_AFTER_MS = 15000;
constexpr int64_t RECENT_MS = 2000;
constexpr float RELEASE_BELOW = 0.7f;
constexpr float RELEASE = 0.97f;
// Inside a shot a brighter part (a lamp, a pan) does not jump the tone curve:
// the peak climbs at most RISE_PCT per second and highlights clip a little
// meanwhile (preferred to a faster climb in a blind A/B on Matrix). At a cut
// the new peak applies at once.
constexpr float RISE_PCT = 20.0f;
// ... except in the first captures of a shot, which may have caught the
// transition frame or a stale picture after a seek: rise at once there.
constexpr int64_t SETTLE_MS = 400;
// A cut to a darker picture waits for the next capture to confirm it (still
// darker, same picture): a strobe or a single dark frame in a busy shot would
// otherwise drop the peak and climb back. A cut to a brighter picture applies
// at once (its highlights would clip).
constexpr float DARKER = 0.8f;
// The confirming capture must show the same picture as the cut one: real cuts
// confirm at 0.1-0.9, busy shots passed 7.1 and 2.6 and dipped 972 -> 138-349.
constexpr float CONFIRM_DIFF = 1.5f;
// No cut to a darker picture while the recent motion is above this: a fast
// montage keeps its highest peak like one shot. Dialogue cuts come at <= 3.5,
// the Matrix 3:25-3:33 montage at 5.5-10.
constexpr float DARKER_MAX_MOTION = 4.5f;
const float STEP = std::log(1.05f); // write on a 5 % change only

// PQ (ST 2084) code, full range 8 bit as the capture delivers it -> nits
std::array<float, 256> BuildNits()
{
  std::array<float, 256> t{};
  for (int c = 0; c < 256; ++c)
  {
    const double e = std::pow(c / 255.0, 1.0 / 78.84375);
    const double v = std::max(e - 0.8359375, 0.0) / (18.8515625 - 18.6875 * e);
    t[c] = static_cast<float>(10000.0 * std::pow(v, 1.0 / 0.1593017578125));
  }
  return t;
}
const std::array<float, 256> NITS = BuildNits();

int64_t NowMs()
{
  using namespace std::chrono;
  return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}
} // namespace

CAMLPeakMeter::CAMLPeakMeter() : CThread("AMLPeakMeter")
{
}

CAMLPeakMeter::~CAMLPeakMeter()
{
  Stop();
}

void CAMLPeakMeter::Start(int srcWidth, int srcHeight, float masteringPeak)
{
  Stop();
  // Requests smaller than the double-write copy are cropped, not scaled.
  m_width = (srcWidth / 4) & ~31;
  m_height = (srcHeight / 4) & ~1;
  if (m_width <= 0 || m_height <= 0 || !CSysfsPath(PEAK_PARAM).Exists())
    return;
  m_step = std::max(1, static_cast<int>(std::sqrt(m_width * m_height / static_cast<float>(SAMPLES))));
  m_cap = masteringPeak > 0.0f ? masteringPeak : 10000.0f;
  m_written = -1;
  m_out = 0.0f;
  m_haveGrid = false;
  m_haveShot = false;
  m_pendingCut = false;
  m_diffs.clear();
  m_window.clear();
  m_flush = false;
  CLog::Log(LOGINFO, "CAMLPeakMeter: measuring the HDR10 peak on {}x{} captures, step {}",
            m_width, m_height, m_step);
  Create();
}

void CAMLPeakMeter::Stop()
{
  if (IsRunning())
    StopThread(true);
  if (m_written > 0)
    Write(0);
}

bool CAMLPeakMeter::Measure(float& nits, std::array<float, 144>& grid)
{
  const int size = m_width * m_height * 3;
  std::vector<uint8_t> buf(size);
  ssize_t got = -1;
  {
    std::lock_guard<std::mutex> lock(CScreenshotAML::CaptureMutex());
    const int fd = open(CAPTURE_DEVICE, O_RDWR);
    if (fd < 0)
      return false;
    if (ioctl(fd, AMVIDEOCAP_IOW_SET_WANTFRAME_WIDTH, m_width) == 0 &&
        ioctl(fd, AMVIDEOCAP_IOW_SET_WANTFRAME_HEIGHT, m_height) == 0)
      got = pread(fd, buf.data(), size, 0); // EAGAIN at playback start/seek
    close(fd);
  }
  if (got != size)
    return false;

  std::array<int, 256> hist{};
  std::array<int, 144> cellCount{};
  grid.fill(0.0f);
  int count = 0;
  for (int y = 0; y < m_height; y += m_step)
  {
    const uint8_t* p = buf.data() + y * m_width * 3;
    const int row = std::min(8, y * 9 / m_height) * 16;
    for (int x = 0; x < m_width; x += m_step, p += 3 * m_step)
    {
      const uint8_t m = std::max({p[0], p[1], p[2]});
      hist[m]++;
      const int cell = row + std::min(15, x * 16 / m_width);
      grid[cell] += m;
      cellCount[cell]++;
      count++;
    }
  }
  for (int c = 0; c < 144; ++c)
    grid[c] = cellCount[c] ? grid[c] / cellCount[c] : 0.0f;
  // A compressed-only frame read as NV21 is noise, mostly saturated; the first
  // capture after open also is, with fewer saturated pixels but a 10000-nit
  // percentile that the shot peak would then keep. No real grade sits there.
  if (!count || hist[255] > count / 4 || hist[255] > static_cast<int>(count * (1.0f - PERCENTILE)))
    return false;

  int left = static_cast<int>(count * (1.0f - PERCENTILE));
  for (int c = 255; c >= 0; --c)
  {
    left -= hist[c];
    if (left < 0)
    {
      nits = NITS[c];
      return true;
    }
  }
  nits = 0.0f;
  return true;
}

void CAMLPeakMeter::Write(int nits)
{
  CSysfsPath peak{PEAK_PARAM};
  if (peak.Exists())
    peak.Set(nits);
  m_written = nits;
}

void CAMLPeakMeter::Process()
{
  while (!m_bStop)
  {
    const auto start = std::chrono::steady_clock::now();
    if (m_flush.exchange(false))
    {
      m_window.clear();
      m_haveGrid = false;
      m_haveShot = false;
      m_pendingCut = false;
      m_diffs.clear();
      m_out = 0.0f;
    }

    // HDR10+ converted to DV also starts as HDR10: the kernel only applies the
    // peak to HDR10 input, but keep the param at 0 for anything else.
    if (CServiceBroker::GetDataCacheCore().GetVideoHdrType() != StreamHdrType::HDR_TYPE_HDR10)
    {
      if (m_written != 0)
        Write(0);
      m_window.clear();
      m_haveGrid = false;
      m_haveShot = false;
      m_pendingCut = false;
      m_diffs.clear();
      m_out = 0.0f;
    }
    else
    {
      float p;
      std::array<float, 144> grid;
      if (Measure(p, grid))
      {
        const int64_t now = NowMs();
        p = std::min(p, m_cap);
        float diff = 0.0f;
        for (int c = 0; c < 144; ++c)
          diff += std::abs(grid[c] - m_grid[c]);
        diff /= 144.0f;
        float baseline = 0.0f;
        if (!m_diffs.empty())
        {
          std::vector<float> sorted(m_diffs.begin(), m_diffs.end());
          std::nth_element(sorted.begin(), sorted.begin() + sorted.size() / 2, sorted.end());
          baseline = sorted[sorted.size() / 2];
        }
        const bool cut = !m_haveGrid || (diff > CUT_DIFF && diff > CUT_RATIO * baseline &&
                                         now - m_shotStart >= MIN_SHOT_MS);
        if (m_haveGrid)
        {
          m_diffs.push_back(diff);
          if (m_diffs.size() > CUT_HISTORY)
            m_diffs.pop_front();
        }
        m_grid = grid;
        m_haveGrid = true;

        m_window.emplace_back(now, p);
        while (!m_window.empty() && m_window.front().first < now - RECENT_MS)
          m_window.pop_front();
        const bool confirmed = m_pendingCut && diff <= CONFIRM_DIFF && p < m_out * DARKER;
        m_pendingCut = false;
        if (cut && m_haveShot && p < m_out * DARKER)
        {
          m_pendingCut = baseline <= DARKER_MAX_MOTION;
          CLog::Log(LOGDEBUG, "CAMLPeakMeter: darker cut {} (grid diff {:.1f}, motion {:.1f})",
                    m_pendingCut ? "pending" : "ignored, busy shot", diff, baseline);
        }
        else if (cut || confirmed)
        {
          // new shot (or first sample of a playback/seek): start from this picture
          m_haveShot = true;
          m_window.clear();
          m_window.emplace_back(now, p);
          m_shotStart = now;
          m_out = p;
          CLog::Log(LOGDEBUG, "CAMLPeakMeter: shot cut{} (grid diff {:.1f}, motion {:.1f})",
                    confirmed ? " confirmed" : "", diff, baseline);
        }
        else if (p > m_out)
        {
          if (now - m_shotStart < SETTLE_MS)
            m_out = p;
          else
            m_out = std::min(p, m_out * std::pow(1.0f + RISE_PCT / 100.0f,
                                                 std::chrono::duration<float>(PERIOD).count()));
        }
        else if (now - m_shotStart > RELEASE_AFTER_MS)
        {
          float recent = 0.0f;
          for (const auto& s : m_window)
            recent = std::max(recent, s.second);
          if (recent < m_out * RELEASE_BELOW)
            m_out = std::max(recent, m_out * RELEASE);
        }
        const int q = std::max(
            100, static_cast<int>(std::lround(std::exp(std::round(std::log(std::max(m_out, 1.0f)) / STEP) * STEP))));
        // re-read: a manual A/B may have reset it
        CSysfsPath peak{PEAK_PARAM};
        if (q != m_written || peak.Get<int>().value_or(-1) != q)
        {
          if (q != m_written)
            CLog::Log(LOGDEBUG, "CAMLPeakMeter: scene peak {:.0f} nits -> {}", p, q);
          Write(q);
        }
      }
    }

    const auto spent = std::chrono::steady_clock::now() - start;
    if (spent < PERIOD)
      Sleep(std::chrono::duration_cast<std::chrono::milliseconds>(PERIOD - spent));
  }
}
