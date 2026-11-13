/*
 *  Copyright (C) 2026 Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

// The tone mapping follows CoreELEC's AMLCaptureConvert in Kodi 22: Kodi's
// default video tone map, scaled at the brightest channel, then gamma 2.2.

#include "AMLCaptureConvert.h"

#include "AMLVideoCapture.h"
#include "utils/log.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <functional>
#include <thread>
#include <vector>

extern "C"
{
#include <libavutil/pixfmt.h>
#include <libswscale/swscale.h>
}

namespace AML_CAPTURE_CONVERT
{
namespace
{

using AML_VIDEO_CAPTURE::Chroma;
using AML_VIDEO_CAPTURE::Signal;
using Matrix = std::array<std::array<double, 3>, 3>;

// clang-format off
// ITU-R BT.2100 table 7: linear BT.2020 RGB to LMS
constexpr Matrix RGB_TO_LMS{{
  {1688.0 / 4096, 2146.0 / 4096, 262.0 / 4096},
  {683.0 / 4096, 2951.0 / 4096, 462.0 / 4096},
  {99.0 / 4096, 309.0 / 4096, 3688.0 / 4096},
}};
// IPT from PQ-coded LMS, the space of the Dolby Vision core
constexpr Matrix LMS_TO_IPT{{
  {0.4, 0.4, 0.2},
  {4.4550, -4.8510, 0.3960},
  {0.8056, 0.3572, -1.1628},
}};
// ITU-R BT.2100 table 7: ICtCp from PQ-coded LMS
constexpr Matrix LMS_TO_ICTCP{{
  {0.5, 0.5, 0.0},
  {6610.0 / 4096, -13613.0 / 4096, 7003.0 / 4096},
  {17933.0 / 4096, -17390.0 / 4096, -543.0 / 4096},
}};
// ITU-R BT.2087, linear light
constexpr Matrix BT709_TO_BT2020{{
  {0.6274039, 0.3292830, 0.0433131},
  {0.0690973, 0.9195404, 0.0113623},
  {0.0163914, 0.0880133, 0.8955953},
}};
// clang-format on

// Under Dolby Vision, G12B's VD1 holds the DV core's input as full-range
// codes, intensity at 256 a unit. What VS10 makes of a source, and of HDR10+
// that Kodi converts to Dolby Vision, is IPT, P and T at 128 a unit around
// 128: fitted from VD1 grabs of titles that VS10 sent as Dolby Vision against
// grabs of the same frames sent as SDR or HDR10. A Dolby Vision title's own
// composer output is ICtCp at Kodi 22's constants, Ct at 64 and Cp at 128 a
// unit: fitted against the low-latency output of the frame. Both decodes
// match those references within a few codes; the other one turns hues purple
// or green.
constexpr float VD1_I_SCALE = 256.0f;
constexpr float VD1_PT_SCALE = 128.0f;
constexpr float VD1_CT_SCALE = 64.0f;
constexpr float VD1_CP_SCALE = 128.0f;
constexpr float CHROMA_ZERO = 128.0f;

// swscale widens 8-bit codes to YUV444P16 by a shift of 8
constexpr float CODE_SCALE_8_TO_16 = 256.0f;

constexpr float LIMITED_BLACK = 16.0f;
constexpr float LIMITED_LUMA_SPAN = 219.0f;
constexpr float LIMITED_CHROMA_SPAN = 224.0f;
constexpr float BT709_KR = 0.2126f;
constexpr float BT709_KB = 0.0722f;
constexpr float BT2020_KR = 0.2627f;
constexpr float BT2020_KB = 0.0593f;

// ITU-R BT.2100 HLG: the inverse OETF to scene light, and the OOTF of a
// 1000 cd/m2 display (system gamma 1.2), in units of the PQ peak.
constexpr double HLG_A = 0.17883277;
constexpr double HLG_B = 1 - 4 * HLG_A;
constexpr double HLG_C = 0.55991073;
constexpr double HLG_DISPLAY_NITS = 1000.0;
constexpr double HLG_SYSTEM_GAMMA = 1.2;
constexpr int HLG_STEPS = 4096;

constexpr double PQ_M1 = 2610.0 / 16384;
constexpr double PQ_M2 = 2523.0 / 4096 * 128;
constexpr double PQ_C1 = 3424.0 / 4096;
constexpr double PQ_C2 = 2413.0 / 4096 * 32;
constexpr double PQ_C3 = 2392.0 / 4096 * 32;
constexpr double PQ_PEAK_NITS = 10000.0;

// The Dolby Vision SDR output codes BT.709 by the BT.1886 EOTF of the DV
// core's SDR target display.
constexpr double BT1886_GAMMA = 2.4;
constexpr double DV_SDR_WHITE_NITS = 100.0;
constexpr double DV_SDR_BLACK_NITS = 0.005;

// Kodi's default video tone map: Hable at parameter 1.0, as the GLES video
// shader applies it. The shader scales linear light by (10000 / peak) *
// (2 / param), then by the white point peak / 100, so the scale is fixed and
// the peak only sets where white sits.
constexpr std::array<double, 6> HABLE{0.15, 0.5, 0.1, 0.2, 0.02, 0.3};
constexpr double TONEMAP_SCALE = PQ_PEAK_NITS / 100.0 * 2.0 / 1.0;
constexpr double SDR_OUTPUT_GAMMA = 2.2;

// Interpolated tables, within a tenth of an 8-bit code of the functions.
constexpr int PQ_EOTF_STEPS = 16384;
// A log table indexes linear light by a float's exponent and top mantissa
// bits: uniform in linear within each of its octaves.
constexpr int LOG_TABLE_STEP_BITS = 8;
constexpr int LOG_TABLE_OCTAVES = 48;
constexpr int FLOAT_MANTISSA_BITS = 23;
constexpr int LOG_TABLE_SHIFT = FLOAT_MANTISSA_BITS - LOG_TABLE_STEP_BITS;
const float LOG_TABLE_MIN = std::ldexp(1.0f, -LOG_TABLE_OCTAVES);

Matrix Invert(const Matrix& m)
{
  const double det = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) -
                     m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
                     m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
  Matrix out{};
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 3; c++)
    {
      const int r0 = (c + 1) % 3;
      const int r1 = (c + 2) % 3;
      const int c0 = (r + 1) % 3;
      const int c1 = (r + 2) % 3;
      out[r][c] = (m[r0][c0] * m[r1][c1] - m[r0][c1] * m[r1][c0]) / det;
    }
  return out;
}

Matrix Multiply(const Matrix& a, const Matrix& b)
{
  Matrix out{};
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 3; c++)
      out[r][c] = a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c];
  return out;
}

double PQToLinear(double e)
{
  const double p = std::pow(std::clamp(e, 0.0, 1.0), 1 / PQ_M2);
  return std::pow(std::max(p - PQ_C1, 0.0) / (PQ_C2 - PQ_C3 * p), 1 / PQ_M1);
}

double HlgToScene(double e)
{
  return e <= 0.5 ? e * e / 3 : (std::exp((e - HLG_C) / HLG_A) + HLG_B) / 12;
}

double HlgOotfGain(double sceneLuminance)
{
  return HLG_DISPLAY_NITS / PQ_PEAK_NITS * std::pow(sceneLuminance, HLG_SYSTEM_GAMMA - 1);
}

double Hable(double x)
{
  const auto& [a, b, c, d, e, f] = HABLE;
  return ((x * (a * x + c * b) + d * e) / (x * (a * x + b) + d * f)) - e / f;
}

double LinearToSdr(double l)
{
  return std::pow(l, 1 / SDR_OUTPUT_GAMMA);
}

double LinearToBt1886(double l)
{
  const double white = std::pow(DV_SDR_WHITE_NITS, 1 / BT1886_GAMMA);
  const double black = std::pow(DV_SDR_BLACK_NITS, 1 / BT1886_GAMMA);
  const double v = std::pow(l * PQ_PEAK_NITS, 1 / BT1886_GAMMA);
  return std::clamp((v - black) / (white - black), 0.0, 1.0);
}

// One entry past the end, so a lookup at 1.0 interpolates without a check.
template<typename F>
std::vector<float> MakeTable(int steps, F f)
{
  std::vector<float> table;
  table.reserve(steps + 2);
  for (int i = 0; i <= steps + 1; i++)
    table.push_back(static_cast<float>(f(std::min(static_cast<double>(i) / steps, 1.0))));
  return table;
}

template<typename F>
std::vector<float> MakeLogTable(F f)
{
  const int octaveSteps = 1 << LOG_TABLE_STEP_BITS;
  const int steps = LOG_TABLE_OCTAVES * octaveSteps;
  std::vector<float> table;
  table.reserve(steps + 2);
  for (int i = 0; i <= steps; i++)
  {
    const double linear = std::ldexp(1.0 + static_cast<double>(i % octaveSteps) / octaveSteps,
                                     i / octaveSteps - LOG_TABLE_OCTAVES);
    table.push_back(static_cast<float>(f(linear)));
  }
  table.push_back(table.back());
  return table;
}

uint32_t FloatBits(float v)
{
  uint32_t bits;
  std::memcpy(&bits, &v, sizeof(bits));
  return bits;
}

float LookupLog(const float* table, float linear)
{
  static const uint32_t minBits = FloatBits(LOG_TABLE_MIN);
  const uint32_t bits = FloatBits(std::clamp(linear, LOG_TABLE_MIN, 1.0f)) - minBits;
  const uint32_t i = bits >> LOG_TABLE_SHIFT;
  const float fraction =
      static_cast<float>(bits & ((1u << LOG_TABLE_SHIFT) - 1)) * (1.0f / (1u << LOG_TABLE_SHIFT));
  return table[i] + (table[i + 1] - table[i]) * fraction;
}

float Lookup(const std::vector<float>& table, float v)
{
  const float pos = std::clamp(v, 0.0f, 1.0f) * static_cast<float>(table.size() - 2);
  const int i = static_cast<int>(pos);
  return table[i] + (table[i + 1] - table[i]) * (pos - static_cast<float>(i));
}

struct Tables
{
  std::vector<float> pqToLinear = MakeTable(PQ_EOTF_STEPS, PQToLinear);
  std::vector<float> linearToSdr = MakeLogTable(LinearToSdr);
  std::vector<float> linearToBt1886 = MakeLogTable(LinearToBt1886);
  std::vector<float> hlgToScene = MakeTable(HLG_STEPS, HlgToScene);
  std::vector<float> hlgOotfGain = MakeLogTable(HlgOotfGain);
  // IPT or ICtCp codes to PQ-coded LMS, and linear LMS to linear BT.709
  Matrix iptToLms = Invert(LMS_TO_IPT);
  Matrix ictcpToLms = Invert(LMS_TO_ICTCP);
  Matrix lmsToBt709 = Multiply(Invert(BT709_TO_BT2020), Invert(RGB_TO_LMS));
  Matrix bt2020ToBt709 = Invert(BT709_TO_BT2020);
};

const Tables& GetTables()
{
  static const Tables tables;
  return tables;
}

// The gain to SDR light at a pixel's brightest channel, gamma-coded: scaling
// all channels by one gain keeps the hue.
std::vector<float> MakeToneMap(float peakNits)
{
  const double white = Hable(TONEMAP_SCALE * static_cast<double>(peakNits) / PQ_PEAK_NITS);
  return MakeLogTable(
      [white](double linear)
      {
        const double sdr = std::clamp(Hable(linear * TONEMAP_SCALE) / white, 0.0, 1.0);
        return LinearToSdr(sdr / linear);
      });
}

struct Rect
{
  int x0, y0, x1, y1;
};

// The video window in image pixels.
Rect Destination(const AML_VIDEO_CAPTURE::Frame& frame, int width, int height)
{
  const double sx = static_cast<double>(width) / frame.displayWidth;
  const double sy = static_cast<double>(height) / frame.displayHeight;
  Rect r;
  r.x0 = std::clamp(static_cast<int>(std::lround(frame.x * sx)), 0, width);
  r.y0 = std::clamp(static_cast<int>(std::lround(frame.y * sy)), 0, height);
  r.x1 = std::clamp(static_cast<int>(std::lround((frame.x + frame.width) * sx)), 0, width);
  r.y1 = std::clamp(static_cast<int>(std::lround((frame.y + frame.height) * sy)), 0, height);
  return r;
}

// Scales the NV12 frame to the window's size in the image, as YUV 4:4:4.
bool Scale(const AML_VIDEO_CAPTURE::Frame& frame, int width, int height, std::vector<uint16_t>& yuv)
{
  SwsContext* context =
      sws_getContext(frame.frameWidth, frame.frameHeight, AV_PIX_FMT_NV12, width, height,
                     AV_PIX_FMT_YUV444P16LE, SWS_BILINEAR, nullptr, nullptr, nullptr);
  if (!context)
    return false;
  const size_t plane = static_cast<size_t>(width) * height;
  yuv.resize(plane * 3);
  const uint8_t* src[] = {frame.nv12.data(),
                          frame.nv12.data() + static_cast<size_t>(frame.stride) * frame.frameHeight,
                          nullptr, nullptr};
  const int srcStride[] = {frame.stride, frame.stride, 0, 0};
  uint8_t* dst[] = {reinterpret_cast<uint8_t*>(yuv.data()),
                    reinterpret_cast<uint8_t*>(yuv.data() + plane),
                    reinterpret_cast<uint8_t*>(yuv.data() + 2 * plane), nullptr};
  const int dstStride[] = {width * 2, width * 2, width * 2, 0};
  sws_scale(context, src, srcStride, 0, frame.frameHeight, dst, dstStride);
  sws_freeContext(context);
  return true;
}

// One pixel's codes to gamma-coded SDR BT.709 in [0, 1].
class CDecoder
{
public:
  CDecoder(Signal signal, Chroma chroma, float peakNits)
    : m_signal(signal), m_tables(GetTables())
  {
    if (signal == Signal::DV_TV_LED || signal == Signal::DV_MAPPED || signal == Signal::HLG)
      m_toneMap = MakeToneMap(peakNits);
    const bool ictcp = chroma == Chroma::ICTCP;
    m_chromaScale[0] = ictcp ? VD1_CT_SCALE : VD1_PT_SCALE;
    m_chromaScale[1] = ictcp ? VD1_CP_SCALE : VD1_PT_SCALE;
    const Matrix& toLms = ictcp ? m_tables.ictcpToLms : m_tables.iptToLms;
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 3; c++)
      {
        m_codesToLms[r][c] = static_cast<float>(toLms[r][c]);
        m_lmsToRgb[r][c] = static_cast<float>(m_tables.lmsToBt709[r][c]);
        m_bt2020ToRgb[r][c] = static_cast<float>(m_tables.bt2020ToBt709[r][c]);
      }
  }

  void Decode(float y, float cb, float cr, float rgb[3]) const
  {
    if (m_signal == Signal::SDR)
    {
      const float l = (y - LIMITED_BLACK) / LIMITED_LUMA_SPAN;
      const float b = (cb - CHROMA_ZERO) / LIMITED_CHROMA_SPAN;
      const float r = (cr - CHROMA_ZERO) / LIMITED_CHROMA_SPAN;
      rgb[0] = l + 2 * (1 - BT709_KR) * r;
      rgb[2] = l + 2 * (1 - BT709_KB) * b;
      rgb[1] = (l - BT709_KR * rgb[0] - BT709_KB * rgb[2]) / (1 - BT709_KR - BT709_KB);
      for (int c = 0; c < 3; c++)
        rgb[c] = std::clamp(rgb[c], 0.0f, 1.0f);
      return;
    }

    float linear[3];
    if (m_signal == Signal::HLG)
    {
      const float l = (y - LIMITED_BLACK) / LIMITED_LUMA_SPAN;
      const float b = (cb - CHROMA_ZERO) / LIMITED_CHROMA_SPAN;
      const float r = (cr - CHROMA_ZERO) / LIMITED_CHROMA_SPAN;
      float scene[3];
      scene[0] = l + 2 * (1 - BT2020_KR) * r;
      scene[2] = l + 2 * (1 - BT2020_KB) * b;
      scene[1] = (l - BT2020_KR * scene[0] - BT2020_KB * scene[2]) / (1 - BT2020_KR - BT2020_KB);
      for (int c = 0; c < 3; c++)
        scene[c] = Lookup(m_tables.hlgToScene, scene[c]);
      const float luminance = BT2020_KR * scene[0] +
                              (1 - BT2020_KR - BT2020_KB) * scene[1] + BT2020_KB * scene[2];
      const float ootf = LookupLog(m_tables.hlgOotfGain.data(), luminance);
      for (int r = 0; r < 3; r++)
        linear[r] = std::max((m_bt2020ToRgb[r][0] * scene[0] + m_bt2020ToRgb[r][1] * scene[1] +
                              m_bt2020ToRgb[r][2] * scene[2]) *
                                 ootf,
                             0.0f);
    }
    else
    {
      const float codes[3] = {y / VD1_I_SCALE, (cb - CHROMA_ZERO) / m_chromaScale[0],
                              (cr - CHROMA_ZERO) / m_chromaScale[1]};
      float lms[3];
      for (int r = 0; r < 3; r++)
        lms[r] = Lookup(m_tables.pqToLinear,
                        m_codesToLms[r][0] * codes[0] + m_codesToLms[r][1] * codes[1] +
                            m_codesToLms[r][2] * codes[2]);
      for (int r = 0; r < 3; r++)
        linear[r] = std::max(
            m_lmsToRgb[r][0] * lms[0] + m_lmsToRgb[r][1] * lms[1] + m_lmsToRgb[r][2] * lms[2],
            0.0f);

      if (m_signal == Signal::DV_SDR)
      {
        for (int c = 0; c < 3; c++)
          rgb[c] = LookupLog(m_tables.linearToBt1886.data(), linear[c]);
        return;
      }
    }

    // Tone-map by the brightest channel; past the PQ range it scales to white.
    const float brightest = std::max({linear[0], linear[1], linear[2]});
    const float gain = LookupLog(m_toneMap.data(), brightest);
    const float scale = brightest > 1.0f ? 1.0f / brightest : 1.0f;
    for (int c = 0; c < 3; c++)
      rgb[c] =
          std::min(LookupLog(m_tables.linearToSdr.data(), linear[c] * scale) * gain, 1.0f);
  }

private:
  Signal m_signal;
  const Tables& m_tables;
  std::vector<float> m_toneMap;
  // The codes per unit of the two chroma channels, and their matrix to LMS.
  float m_chromaScale[2];
  float m_codesToLms[3][3];
  float m_lmsToRgb[3][3];
  float m_bt2020ToRgb[3][3];
};

// Codes of one pixel of the window, in 8-bit units.
using Source = void (*)(const void* data, int x, int y, float ycc[3]);

// The frame as captured: each chroma sample covers 2x2 luma samples.
struct Nv12
{
  const uint8_t* luma;
  const uint8_t* chroma;
  int stride;
};

void FromNv12(const void* data, int x, int y, float ycc[3])
{
  const Nv12& p = *static_cast<const Nv12*>(data);
  const uint8_t* c = p.chroma + static_cast<ptrdiff_t>(y / 2) * p.stride + (x & ~1);
  ycc[0] = p.luma[static_cast<ptrdiff_t>(y) * p.stride + x];
  ycc[1] = c[0];
  ycc[2] = c[1];
}

// The frame scaled by swscale, as YUV 4:4:4 at 16 bits.
struct Yuv444
{
  const uint16_t* data;
  int width;
  size_t plane;
};

void FromYuv444(const void* data, int x, int y, float ycc[3])
{
  const Yuv444& p = *static_cast<const Yuv444*>(data);
  const size_t i = static_cast<size_t>(y) * p.width + x;
  ycc[0] = p.data[i] / CODE_SCALE_8_TO_16;
  ycc[1] = p.data[i + p.plane] / CODE_SCALE_8_TO_16;
  ycc[2] = p.data[i + 2 * p.plane] / CODE_SCALE_8_TO_16;
}

// Puts rows [y0, y1) of the window under the GUI: the GUI is premultiplied,
// so it goes over the video as it is, which shows through by 1 - alpha.
void ComposeRows(const CDecoder& decoder,
                 Source source,
                 const void* data,
                 uint8_t* bgra,
                 int stride,
                 const Rect& r,
                 int y0,
                 int y1)
{
  const int w = r.x1 - r.x0;
  for (int y = y0; y < y1; y++)
  {
    uint8_t* out = bgra + static_cast<ptrdiff_t>(r.y0 + y) * stride + r.x0 * 4;
    for (int x = 0; x < w; x++, out += 4)
    {
      float ycc[3];
      float rgb[3];
      source(data, x, y, ycc);
      decoder.Decode(ycc[0], ycc[1], ycc[2], rgb);
      const float under = (255 - out[3]) * (1.0f / 255);
      out[0] = static_cast<uint8_t>(std::min(out[0] + rgb[2] * under * 255 + 0.5f, 255.0f));
      out[1] = static_cast<uint8_t>(std::min(out[1] + rgb[1] * under * 255 + 0.5f, 255.0f));
      out[2] = static_cast<uint8_t>(std::min(out[2] + rgb[0] * under * 255 + 0.5f, 255.0f));
      out[3] = 0xFF;
    }
  }
}

// A 4K frame takes about a second on one core; spread the rows.
constexpr unsigned int MAX_THREADS = 4;

void Compose(const CDecoder& decoder,
             Source source,
             const void* data,
             uint8_t* bgra,
             int stride,
             const Rect& r)
{
  const int h = r.y1 - r.y0;
  const unsigned int threads = std::clamp(std::thread::hardware_concurrency(), 1u, MAX_THREADS);
  const int rows = (h + static_cast<int>(threads) - 1) / static_cast<int>(threads);
  std::vector<std::thread> workers;
  for (unsigned int t = 1; t < threads; t++)
  {
    const int y0 = static_cast<int>(t) * rows;
    if (y0 >= h)
      break;
    workers.emplace_back(ComposeRows, std::cref(decoder), source, data, bgra, stride, std::cref(r),
                         y0, std::min(h, y0 + rows));
  }
  ComposeRows(decoder, source, data, bgra, stride, r, 0, std::min(h, rows));
  for (auto& worker : workers)
    worker.join();
}

} // namespace

bool ComposeUnder(
    const AML_VIDEO_CAPTURE::Frame& frame, uint8_t* bgra, int width, int height, int stride)
{
  if (!bgra || width <= 0 || height <= 0 || frame.width <= 0 || frame.height <= 0 ||
      frame.frameWidth <= 0 || frame.frameHeight <= 0 || frame.displayWidth <= 0 ||
      frame.displayHeight <= 0 || frame.nv12.empty())
    return false;
  const Rect r = Destination(frame, width, height);
  const int w = r.x1 - r.x0;
  const int h = r.y1 - r.y0;
  if (w <= 0 || h <= 0)
    return false;

  const CDecoder decoder(frame.signal, frame.chroma, frame.peakNits);
  if (w == frame.frameWidth && h == frame.frameHeight)
  {
    // the frame at its own size: decode it as it is
    const Nv12 nv12{frame.nv12.data(),
                    frame.nv12.data() + static_cast<size_t>(frame.stride) * frame.frameHeight,
                    frame.stride};
    Compose(decoder, FromNv12, &nv12, bgra, stride, r);
    return true;
  }

  std::vector<uint16_t> yuv;
  if (!Scale(frame, w, h, yuv))
  {
    CLog::Log(LOGERROR, "AMLCaptureConvert: cannot scale {}x{} to {}x{}", frame.frameWidth,
              frame.frameHeight, w, h);
    return false;
  }
  const Yuv444 scaled{yuv.data(), w, static_cast<size_t>(w) * h};
  Compose(decoder, FromYuv444, &scaled, bgra, stride, r);
  return true;
}

} // namespace AML_CAPTURE_CONVERT
