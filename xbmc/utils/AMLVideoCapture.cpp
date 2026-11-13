/*
 *  Copyright (C) 2026 Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AMLVideoCapture.h"

#include "ServiceBroker.h"
#include "cores/DataCacheCore.h"
#include "platform/linux/SysfsPath.h"
#include "utils/AMLUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <optional>
#include <string>
#include <thread>

#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

namespace AML_VIDEO_CAPTURE
{
namespace
{

// vdin1's ioctl interface (drivers/amlogic/media/vin/tvin/vdin). Kodi runs as a
// 32-bit process on the arm64 kernel, so these mirror the kernel's 64-bit
// layout: its unsigned long and pointers are 8 bytes.
struct VdinV4l2Param // vdin_v4l2_param_s
{
  int32_t width;
  int32_t height;
  int32_t fps;
  int32_t dstFormat; // enum tvin_color_fmt_e
  int32_t dstWidth;
  int32_t dstHeight;
  uint32_t bitOrder;
  int32_t mode; // enum port_mode
};

struct VdinBuffer // vdin_para_to_enc_s
{
  uint32_t colorFormat;
  uint32_t width;
  uint32_t height;
  uint32_t canvas0;
  uint32_t canvas1;
  uint32_t bufferFormat; // enum vdin_buf_fmt_e
  uint64_t physY;
  uint64_t physU;
  uint64_t physV;
  uint32_t stride;
  uint32_t size;
  uint64_t timestamp;
  uint64_t vframe; // the kernel's struct vframe_s *, handed back by TVIN_IOC_P_BUF
};

struct VdinMemInfo // vdin_mem_info_s
{
  uint64_t start;
  uint32_t size;
  uint32_t reserved;
};

static_assert(sizeof(VdinV4l2Param) == 32, "vdin_v4l2_param_s");
static_assert(sizeof(VdinBuffer) == 72, "vdin_para_to_enc_s on arm64");
static_assert(sizeof(VdinMemInfo) == 16, "vdin_mem_info_s");

const unsigned long TVIN_IOC_G_BUF = _IOR('T', 0x0b, VdinBuffer);
const unsigned long TVIN_IOC_P_BUF = _IOW('T', 0x0c, VdinBuffer);
const unsigned long TVIN_IOC_S_VDIN_V4L2START = _IOW('T', 0x25, VdinV4l2Param);
const unsigned long TVIN_IOC_S_VDIN_V4L2STOP = _IO('T', 0x26);
const unsigned long TVIN_IOC_G_MEM_INFO = _IOR('T', 0x53, VdinMemInfo);

constexpr int32_t TVIN_NV12 = 7;
constexpr int32_t PORT_VIDEO_ONLY = 1; // TVIN_PORT_VIU1_WB0_VD1
// Write the frame in plain memory order (PARAM_STATE_WR_MEM_ORDER_STD). By
// default VDIN writes the order the VPP reads back: each 64-bit word byte
// reversed, which scrambles every 8 pixels of a row read as linear NV12.
constexpr uint32_t BIT_ORDER_STANDARD = 1;
constexpr uint32_t BUF_FMT_2_PLANE = 1;
// Only sets VDIN's timing limits; the write-back runs at the display's rate.
constexpr int32_t SESSION_FPS = 60;

// The first frame comes a vsync or two after the start; poll for it after this.
constexpr auto SETTLE = std::chrono::milliseconds(80);
// A session's first frame can be one flat colour, written before the loopback
// settled (seen once in about 45 captures, after HDR output switches): take a
// later one.
constexpr int MIN_FRAMES = 2;
constexpr auto TIMEOUT = std::chrono::milliseconds(600);
constexpr auto POLL = std::chrono::milliseconds(5);

constexpr const char* VDIN_DEVICE = "/dev/vdin1";
constexpr const char* VIDEO_STATE = "/sys/class/video/video_state";
constexpr const char* VIDEO_DISABLED = "/sys/class/video/disable_video";
constexpr const char* DISPLAY_INFO = "/sys/class/display/vinfo";
constexpr const char* DV_ENABLE = "/sys/module/amdolby_vision/parameters/dolby_vision_enable";
constexpr const char* DV_MODE = "/sys/module/amdolby_vision/parameters/dolby_vision_mode";
constexpr const char* DV_LL_POLICY = "/sys/module/amdolby_vision/parameters/dolby_vision_ll_policy";
constexpr const char* DV_SRC_FORMAT = "/sys/module/amdolby_vision/parameters/dolby_vision_src_format";
constexpr const char* HDMI_HDR_STATUS = "/sys/class/amhdmitx/amhdmitx0/hdmi_hdr_status";
// dolby_vision_src_format of a Dolby Vision source frame
constexpr unsigned int DV_SRC_FORMAT_DOVI = 3;
// Y while the title is Dolby Vision as authored; N for an RPU Kodi made
// (HDR10+ converted to Dolby Vision)
constexpr const char* DV_SOURCE_NATIVE = "/sys/module/amdolby_vision/parameters/xbmc_dv_source_native";

// The peak of a Dolby Vision title without HDR statics, and of a frame the DV
// core already mapped to an HDR display (as CoreELEC's Kodi 22 capture uses).
constexpr float TVLED_PEAK_NITS = 4000.0f;
constexpr float MAPPED_PEAK_NITS = 1000.0f;
// The display an HLG output is mapped from, as CoreELEC's Kodi 22 capture uses.
constexpr float HLG_PEAK_NITS = 1000.0f;
constexpr float MIN_PEAK_NITS = 100.0f;
constexpr float MAX_PEAK_NITS = 10000.0f;

std::atomic<bool> s_unavailableLogged{false};

std::optional<unsigned int> ReadUint(const char* path)
{
  CSysfsPath file{path};
  if (!file.Exists())
    return std::nullopt;
  return file.Get<unsigned int>();
}

// Logs once why the decoder's own capture is used instead.
void LogUnavailable(const std::string& reason)
{
  if (!s_unavailableLogged.exchange(true))
    CLog::Log(LOGINFO, "AMLVideoCapture: {}, using the decoder's capture", reason);
}

enum class Window
{
  SHOWN,
  EMPTY, // the video layer has no window on the display
  UNKNOWN, // video_state is missing or does not say
};

// The VPP scaler's output window: where the video is on the display, and the
// size VDIN writes it back at. video_state prints it as "VPP_hsc_startp 0x3c0."
Window GetVideoWindow(int& x, int& y, int& width, int& height)
{
  std::ifstream file(VIDEO_STATE);
  if (!file)
    return Window::UNKNOWN;
  std::string line;
  int hStart = -1, hEnd = -1, vStart = -1, vEnd = -1;
  while (std::getline(file, line))
  {
    char name[32];
    unsigned int value;
    if (std::sscanf(line.c_str(), "%31s 0x%x", name, &value) != 2)
      continue;
    if (!std::strcmp(name, "VPP_hsc_startp"))
      hStart = static_cast<int>(value);
    else if (!std::strcmp(name, "VPP_hsc_endp"))
      hEnd = static_cast<int>(value);
    else if (!std::strcmp(name, "VPP_vsc_startp"))
      vStart = static_cast<int>(value);
    else if (!std::strcmp(name, "VPP_vsc_endp"))
      vEnd = static_cast<int>(value);
  }
  if (hStart < 0 || hEnd < 0 || vStart < 0 || vEnd < 0)
    return Window::UNKNOWN;
  if (hEnd < hStart || vEnd < vStart)
    return Window::EMPTY;
  x = hStart;
  y = vStart;
  width = hEnd - hStart + 1;
  height = vEnd - vStart + 1;
  return Window::SHOWN;
}

bool GetDisplaySize(int& width, int& height)
{
  std::ifstream file(DISPLAY_INFO);
  std::string line;
  width = height = 0;
  while (std::getline(file, line))
  {
    int value;
    if (std::sscanf(line.c_str(), " width: %d", &value) == 1)
      width = value;
    else if (std::sscanf(line.c_str(), " height: %d", &value) == 1)
      height = value;
  }
  return width > 0 && height > 0;
}

// The peak a TV-led Dolby Vision frame is graded to: the title's HDR statics,
// which only PQ sources carry, else a default by source.
float GetTitlePeak()
{
  auto& data = CServiceBroker::GetDataCacheCore();
  const StreamHdrType source = data.GetVideoSourceHdrType();
  const bool dolbyVision = source == StreamHdrType::HDR_TYPE_DOLBYVISION;
  if (dolbyVision || source == StreamHdrType::HDR_TYPE_HDR10 ||
      source == StreamHdrType::HDR_TYPE_HDR10PLUS)
  {
    const HDRStaticMetadataInfo statics = data.GetVideoHDRStaticMetadataInfo();
    float peak = 0.0f;
    if (statics.has_cll_metadata && statics.max_cll > 0)
      peak = static_cast<float>(statics.max_cll);
    else if (statics.has_mdcv_metadata && statics.max_lum > 0)
      peak = static_cast<float>(statics.max_lum);
    if (peak > 0.0f)
      return std::clamp(peak, MIN_PEAK_NITS, MAX_PEAK_NITS);
  }
  return dolbyVision ? TVLED_PEAK_NITS : MAPPED_PEAK_NITS;
}

// Video the Dolby Vision core does not process: HLG when the output is HLG
// (VDIN passes it through), else what VDIN maps to SDR.
Signal GetPassedSignal(float& peak)
{
  CSysfsPath status{HDMI_HDR_STATUS};
  const std::optional<std::string> output =
      status.Exists() ? status.Get<std::string>() : std::nullopt;
  if (output && output->find("HLG") != std::string::npos)
  {
    peak = HLG_PEAK_NITS;
    return Signal::HLG;
  }
  return Signal::SDR;
}

bool IsNativeDolbyVision()
{
  CSysfsPath native{DV_SOURCE_NATIVE};
  const std::optional<std::string> value =
      native.Exists() ? native.Get<std::string>() : std::nullopt;
  return value && !value->empty() && (*value)[0] == 'Y';
}

Signal GetSignal(float& peak, Chroma& chroma)
{
  peak = 0.0f;
  chroma = Chroma::IPT;
  CSysfsPath enable{DV_ENABLE};
  if (!enable.Exists())
    return GetPassedSignal(peak);
  const std::optional<std::string> enabled = enable.Get<std::string>();
  if (!enabled || enabled->empty() || (*enabled)[0] != 'Y')
    return GetPassedSignal(peak);

  // The DV core sets this per frame source; bypass when it passes the video on.
  switch (ReadUint(DV_MODE).value_or(DOLBY_VISION_OUTPUT_MODE_BYPASS))
  {
    case DOLBY_VISION_OUTPUT_MODE_IPT:
    case DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL:
      // The core composes a Dolby Vision title by its own metadata in ICtCp.
      // An RPU Kodi made from HDR10+ comes out IPT, as does what VS10
      // converts to SDR10 or HDR10.
      if (ReadUint(DV_SRC_FORMAT).value_or(0) == DV_SRC_FORMAT_DOVI && IsNativeDolbyVision())
        chroma = Chroma::ICTCP;
      if (ReadUint(DV_LL_POLICY).value_or(DOLBY_VISION_LL_DISABLE) != DOLBY_VISION_LL_DISABLE)
      {
        peak = MAPPED_PEAK_NITS;
        return Signal::DV_MAPPED;
      }
      peak = GetTitlePeak();
      return Signal::DV_TV_LED;
    case DOLBY_VISION_OUTPUT_MODE_HDR10:
      peak = MAPPED_PEAK_NITS;
      return Signal::DV_MAPPED;
    case DOLBY_VISION_OUTPUT_MODE_SDR10:
    case DOLBY_VISION_OUTPUT_MODE_SDR8:
      return Signal::DV_SDR;
    default:
      return GetPassedSignal(peak);
  }
}

class CSession
{
public:
  explicit CSession(int fd) : m_fd(fd) {}
  ~CSession()
  {
    if (m_held)
      ioctl(m_fd, TVIN_IOC_P_BUF, &m_buffer);
    if (m_started)
      ioctl(m_fd, TVIN_IOC_S_VDIN_V4L2STOP);
    close(m_fd);
  }
  CSession(const CSession&) = delete;
  CSession& operator=(const CSession&) = delete;

  bool Start(int width, int height)
  {
    VdinV4l2Param param{};
    param.width = param.dstWidth = width;
    param.height = param.dstHeight = height;
    param.fps = SESSION_FPS;
    param.dstFormat = TVIN_NV12;
    param.mode = PORT_VIDEO_ONLY;
    param.bitOrder = BIT_ORDER_STANDARD;
    if (ioctl(m_fd, TVIN_IOC_S_VDIN_V4L2START, &param) < 0)
    {
      CLog::Log(LOGWARNING, "AMLVideoCapture: vdin1 did not start ({})", std::strerror(errno));
      return false;
    }
    m_started = true;
    return true;
  }

  // Holds the newest frame written since the start, once there are
  // MIN_FRAMES and none is waiting; at the timeout, whatever came, even
  // while frames keep arriving.
  bool Take()
  {
    std::this_thread::sleep_for(SETTLE);
    const auto end = std::chrono::steady_clock::now() + TIMEOUT;
    int frames = 0;
    for (;;)
    {
      VdinBuffer next{};
      const bool got = ioctl(m_fd, TVIN_IOC_G_BUF, &next) == 0;
      if (got)
      {
        if (m_held)
          ioctl(m_fd, TVIN_IOC_P_BUF, &m_buffer);
        m_buffer = next;
        m_held = true;
        frames++;
      }
      if ((!got && frames >= MIN_FRAMES) || std::chrono::steady_clock::now() >= end)
        return m_held;
      if (!got)
        std::this_thread::sleep_for(POLL);
    }
  }

  // Copies the held NV12 frame out of the (uncached) frame memory.
  bool Copy(Frame& frame)
  {
    VdinMemInfo mem{};
    const bool known = ioctl(m_fd, TVIN_IOC_G_MEM_INFO, &mem) == 0;
    if (!known || mem.size == 0)
    {
      CLog::Log(LOGWARNING, "AMLVideoCapture: no frame memory to map ({})",
                known ? "none allocated" : std::strerror(errno));
      return false;
    }
    const VdinBuffer& b = m_buffer;
    if (b.bufferFormat != BUF_FMT_2_PLANE || b.width == 0 || b.height == 0 || b.stride < b.width)
    {
      CLog::Log(LOGWARNING, "AMLVideoCapture: unexpected frame {}x{} stride {} format {}",
                b.width, b.height, b.stride, b.bufferFormat);
      return false;
    }
    const uint64_t lumaSize = static_cast<uint64_t>(b.stride) * b.height;
    const uint64_t chromaSize = static_cast<uint64_t>(b.stride) * ((b.height + 1) / 2);
    const uint64_t memEnd = mem.start + mem.size;
    if (b.physY < mem.start || b.physY + lumaSize > memEnd || b.physU < mem.start ||
        b.physU + chromaSize > memEnd)
    {
      CLog::Log(LOGWARNING, "AMLVideoCapture: frame outside the frame memory");
      return false;
    }

    const uint64_t first = std::min(b.physY, b.physU) - mem.start;
    const uint64_t last = std::max(b.physY + lumaSize, b.physU + chromaSize) - mem.start;
    const long page = sysconf(_SC_PAGESIZE);
    const uint64_t offset = first & ~static_cast<uint64_t>(page - 1);
    const size_t length = static_cast<size_t>(last - offset);
    void* map = mmap(nullptr, length, PROT_READ, MAP_SHARED, m_fd, static_cast<off_t>(offset));
    if (map == MAP_FAILED)
    {
      CLog::Log(LOGWARNING, "AMLVideoCapture: mmap failed ({})", std::strerror(errno));
      return false;
    }
    const uint8_t* mapped = static_cast<const uint8_t*>(map);
    frame.stride = static_cast<int>(b.stride);
    frame.frameWidth = static_cast<int>(b.width);
    frame.frameHeight = static_cast<int>(b.height);
    // one sequential read of each plane out of the uncached mapping
    frame.nv12.resize(static_cast<size_t>(lumaSize + chromaSize));
    std::memcpy(frame.nv12.data(), mapped + (b.physY - mem.start - offset),
                static_cast<size_t>(lumaSize));
    std::memcpy(frame.nv12.data() + lumaSize, mapped + (b.physU - mem.start - offset),
                static_cast<size_t>(chromaSize));
    munmap(map, length);
    return true;
  }

private:
  int m_fd;
  bool m_started{false};
  bool m_held{false};
  VdinBuffer m_buffer{};
};

} // namespace

Result Capture(Frame& frame)
{
  // No video only when the video layer says so. State it does not report
  // leaves the decoder's capture to try.
  const std::optional<unsigned int> disabled = ReadUint(VIDEO_DISABLED);
  if (!disabled)
  {
    LogUnavailable(std::string("no ") + VIDEO_DISABLED);
    return Result::UNAVAILABLE;
  }
  if (*disabled != 0)
    return Result::NO_VIDEO;
  int x, y, width, height;
  switch (GetVideoWindow(x, y, width, height))
  {
    case Window::EMPTY:
      return Result::NO_VIDEO;
    case Window::UNKNOWN:
      LogUnavailable(std::string("no video window in ") + VIDEO_STATE);
      return Result::UNAVAILABLE;
    case Window::SHOWN:
      break;
  }
  int displayWidth, displayHeight;
  if (!GetDisplaySize(displayWidth, displayHeight))
  {
    displayWidth = x + width;
    displayHeight = y + height;
  }

  const int fd = open(VDIN_DEVICE, O_RDWR | O_CLOEXEC);
  if (fd < 0 && errno == EBUSY)
  {
    // Another reader or session has vdin1; the kernel refuses a second one.
    CLog::Log(LOGWARNING, "AMLVideoCapture: {} is busy", VDIN_DEVICE);
    return Result::FAILED;
  }
  if (fd < 0)
  {
    LogUnavailable(std::string("no ") + VDIN_DEVICE + " (" + std::strerror(errno) + ")");
    return Result::UNAVAILABLE;
  }
  // A kernel without this ioctl cannot hand out the frames it writes back.
  VdinMemInfo probe{};
  if (ioctl(fd, TVIN_IOC_G_MEM_INFO, &probe) < 0)
  {
    LogUnavailable(std::string(VDIN_DEVICE) + " has no frame memory ioctl (" +
                   std::strerror(errno) + ")");
    close(fd);
    return Result::UNAVAILABLE;
  }
  CSession session(fd);
  const auto start = std::chrono::steady_clock::now();
  if (!session.Start(width, height))
    return Result::FAILED;
  if (!session.Take())
  {
    CLog::Log(LOGWARNING, "AMLVideoCapture: no {}x{} frame from vdin1", width, height);
    return Result::FAILED;
  }
  if (!session.Copy(frame))
    return Result::FAILED;

  frame.x = x;
  frame.y = y;
  frame.width = width;
  frame.height = height;
  frame.displayWidth = displayWidth;
  frame.displayHeight = displayHeight;
  frame.signal = GetSignal(frame.peakNits, frame.chroma);
  CLog::Log(LOGDEBUG,
            "AMLVideoCapture: {}x{} frame of the {}x{} window at {},{} of {}x{}, signal {} chroma {} "
            "peak {} in {} ms",
            frame.frameWidth, frame.frameHeight, width, height, x, y, displayWidth, displayHeight,
            static_cast<int>(frame.signal), static_cast<int>(frame.chroma), frame.peakNits,
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - start)
                .count());
  return Result::OK;
}

} // namespace AML_VIDEO_CAPTURE
