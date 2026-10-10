/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * See LICENSES/README.md for more information.
 */
#pragma once

#include <cmath>

// Source/output evidence, not the progressive flag cleared by a SW deinterlacer.
enum class MPEG2OutputMode
{
  UNKNOWN,
  PROGRESSIVE,
  INTERLACED_FRAME,
  INTERLACED_FIELD
};

class CMPEG2Cadence
{
public:
  void Reset(bool eligible, double fieldRate, bool doubled, double nowMs)
  {
    m_eligible = eligible && std::isfinite(fieldRate) && fieldRate > 55.0 && fieldRate < 61.0;
    m_fieldRate = fieldRate;
    m_doubled = doubled;
    m_mode = MPEG2OutputMode::UNKNOWN;
    StartProbe(nowMs);
  }

  bool Eligible() const { return m_eligible; }
  bool Film() const { return m_film; }
  bool FrameOutput() const { return m_frameOutput; }

  void Observe(double duration, double timeBase, double nowMs)
  {
    if (!m_eligible)
      return;
    Expire(nowMs);
    const double fields = duration * m_fieldRate / timeBase;
    const int count = std::isfinite(fields) && fields > 1.5 && fields < 2.5 ? 2 :
                      std::isfinite(fields) && fields > 2.5 && fields < 3.5 ? 3 : 0;
    if (!m_remaining && !m_film && m_mode == MPEG2OutputMode::PROGRESSIVE && count == 3)
    {
      // Progressive video and soft telecine share the same scan mode. New
      // repeat-field evidence can start a fresh bounded probe in that mode.
      const bool frameCandidate = m_frameCandidate;
      StartProbe(nowMs);
      m_frameCandidate = frameCandidate;
    }
    if (m_film)
    {
      // A mixed stream must earn a fresh film verdict after contradictory input.
      if (!count || count == m_lastFields)
      {
        if (++m_contradictions >= 2)
          StartProbe(nowMs);
      }
      else
        m_contradictions = 0;
    }
    if (m_remaining)
    {
      --m_remaining;
      if (count == 2)
        ++m_twoFields;
      if (count == 3)
        ++m_threeFields;
      if (count && m_lastFields && count != m_lastFields)
        ++m_alternations;
      else
        m_alternations = 0;
      if (m_alternations >= 3)
      {
        m_filmCandidate = true;
        m_remaining = 0;
      }
      if (!m_remaining)
        FinishProbe();
    }
    m_lastFields = count;
  }

  // No decoder/output gate: timeout, EOF and drain never prevent GetPicture.
  void Expire(double nowMs)
  {
    if (m_remaining && nowMs - m_startedMs >= 500.0)
      FinishProbe();
  }
  void Drain() { FinishProbe(); }

  double Update(MPEG2OutputMode mode, double nowMs)
  {
    if (!m_eligible)
      return m_fieldRate;
    Expire(nowMs);
    if ((m_film && mode != MPEG2OutputMode::PROGRESSIVE) ||
        (m_mode != MPEG2OutputMode::UNKNOWN && m_mode != MPEG2OutputMode::PROGRESSIVE &&
         mode == MPEG2OutputMode::PROGRESSIVE))
      StartProbe(nowMs);
    m_mode = mode;
    m_film = m_filmCandidate && mode == MPEG2OutputMode::PROGRESSIVE;
    m_frameOutput = m_frameCandidate &&
                    (mode == MPEG2OutputMode::INTERLACED_FRAME ||
                     mode == MPEG2OutputMode::PROGRESSIVE);
    return m_fieldRate * (m_film ? 0.4 : m_frameOutput ? 0.5 : 1.0);
  }

  struct Timing
  {
    double duration;
    double offset;
  };
  static Timing PictureTiming(double duration, double repeat, bool film, bool timestamp)
  {
    if (film)
      return {duration, timestamp && repeat > 0.0 ? repeat * duration * 0.4 : 0.0};
    const double extra = repeat * duration;
    return {duration + extra, extra};
  }

private:
  void StartProbe(double nowMs)
  {
    m_startedMs = nowMs;
    m_remaining = m_eligible ? 6 : 0;
    m_twoFields = m_threeFields = m_lastFields = m_alternations = m_contradictions = 0;
    m_film = m_filmCandidate = m_frameOutput = m_frameCandidate = false;
  }
  void FinishProbe()
  {
    m_remaining = 0;
    m_frameCandidate = m_doubled && m_twoFields >= 2 && !m_threeFields;
  }

  bool m_eligible{false};
  bool m_doubled{false};
  bool m_film{false};
  bool m_filmCandidate{false};
  bool m_frameOutput{false};
  bool m_frameCandidate{false};
  double m_fieldRate{0.0};
  double m_startedMs{0.0};
  int m_remaining{0};
  int m_twoFields{0};
  int m_threeFields{0};
  int m_lastFields{0};
  int m_alternations{0};
  int m_contradictions{0};
  MPEG2OutputMode m_mode{MPEG2OutputMode::UNKNOWN};
};
