/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "AMLAFBCPresenter.h"

#include "cores/VideoPlayer/VideoRenderers/FrameBufferObject.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <system_error>
#include <utility>
#include <sys/ioctl.h>
#include <unistd.h>

namespace
{
// drivers/amlogic/media/osd/osd_afbc_scanout.h
struct ScanoutBuffer
{
  uint32_t index, width, height;
  int32_t fd;
  uint32_t size;
};
struct ScanoutFlip
{
  uint32_t index, interFormat, reverse, flags;
};
static_assert(sizeof(ScanoutBuffer) == 20);
static_assert(sizeof(ScanoutFlip) == 16);
constexpr unsigned long GET_SESSION = _IOR('F', 0x24, int32_t);
constexpr unsigned long SESSION_ALLOC = _IOWR('F', 0x25, ScanoutBuffer);
constexpr unsigned long SESSION_FLIP = _IOW('F', 0x26, ScanoutFlip);
constexpr unsigned long SESSION_DISABLE = _IOW('F', 0x27, uint32_t);
constexpr uint32_t FLIP_WAIT = 1;
constexpr uint32_t DISABLE_ON_PAN = 1;
// The exact driver's wide-block AFBC RGBA8: 32x8 superblocks, each split,
// sparse body, internal YUV transform (osd_sync.h YUV_TRANSFORM | BLOCK_SPLIT |
// SUPER_BLOCK_ASPECT). The OSD decoder holds one row of superblocks: a row of
// 16x16 ones wider than 1920 pixels does not fit and decodes as garbage.
constexpr uint32_t INTER_FORMAT = (1 << 8) | (1 << 9) | (1 << 16);
// A framebuffer object's first row is the bottom one; the decoder flips it.
constexpr uint32_t REVERSE_Y = 3;
constexpr auto LATCH_TIMEOUT = std::chrono::milliseconds(500);
constexpr EGLTimeKHR GPU_TIMEOUT_NS = 2000000000LL;
constexpr unsigned int MAX_RETRIES = 3;
constexpr auto RETRY_DELAY = std::chrono::seconds(1);

#ifndef GL_DEPTH24_STENCIL8_OES
#define GL_DEPTH24_STENCIL8_OES 0x88F0
#endif
} // namespace

bool CAMLAFBCPresenter::KernelQueuesFlips(const char* path)
{
  std::FILE* file = std::fopen(path, "re");
  if (!file)
    return false;
  char value = 0;
  const bool yes = std::fread(&value, 1, 1, file) == 1 && value == 'Y';
  std::fclose(file);
  return yes;
}

bool CAMLAFBCPresenter::Create(const std::string& framebuffer, int width, int height, int depthBits,
                             int stencilBits, bool allowThirdBuffer)
{
  if (!CAMLAFBCImport::Supported())
    return Reject("requires the verified ARM32 G52 r12p0 fbdev library", 0, true);
  // Errors other code left pending would read as this setup failing.
  for (unsigned int i = 0; i < 16 && glGetError() != GL_NO_ERROR; ++i)
  {
  }
  m_display = eglGetCurrentDisplay();
  m_createSync = reinterpret_cast<PFNEGLCREATESYNCKHRPROC>(eglGetProcAddress("eglCreateSyncKHR"));
  m_waitSync = reinterpret_cast<PFNEGLCLIENTWAITSYNCKHRPROC>(eglGetProcAddress("eglClientWaitSyncKHR"));
  m_destroySync = reinterpret_cast<PFNEGLDESTROYSYNCKHRPROC>(eglGetProcAddress("eglDestroySyncKHR"));
  const char* extensions = reinterpret_cast<const char*>(glGetString(GL_EXTENSIONS));
  if (extensions && std::strstr(extensions, "GL_EXT_discard_framebuffer"))
    m_discard = reinterpret_cast<void (*)(GLenum, GLsizei, const GLenum*)>(
        eglGetProcAddress("glDiscardFramebufferEXT"));
  if (m_display == EGL_NO_DISPLAY || !m_createSync || !m_waitSync || !m_destroySync)
    return Reject("EGL fence sync is unavailable", 0, true);
  const bool packed = extensions && std::strstr(extensions, "GL_OES_packed_depth_stencil");
  if (stencilBits > 0 && !packed)
    return Reject("the GUI's stencil needs GL_OES_packed_depth_stencil", 0, true);

  m_fb = open(framebuffer.c_str(), O_RDWR | O_CLOEXEC);
  int32_t session = -1;
  if (m_fb < 0 || ioctl(m_fb, GET_SESSION, &session) < 0 || session < 0)
  {
    // EBUSY: another session is still open, perhaps this process's last one
    // in a child between fork and exec.
    const int error = errno;
    return Reject("the kernel has no AFBC scanout session", error, error != EBUSY);
  }
  m_session = session;

  if (depthBits > 0 || stencilBits > 0)
  {
    glGenRenderbuffers(1, &m_depthStencil);
    glBindRenderbuffer(GL_RENDERBUFFER, m_depthStencil);
    glRenderbufferStorage(GL_RENDERBUFFER, packed ? GL_DEPTH24_STENCIL8_OES : GL_DEPTH_COMPONENT16, width,
                          height);
    glBindRenderbuffer(GL_RENDERBUFFER, 0);
  }
  // A third buffer only helps if a flip queued right after the previous one
  // is shown with the next vsync; otherwise every second one waits a vsync.
  const bool queued = KernelQueuesFlips(m_queuedFlipsParameter.c_str());
  const unsigned int wanted = queued && allowThirdBuffer ? MAX_BUFFERS : 2;
  m_count = 0;
  for (unsigned int i = 0; i < wanted; ++i)
  {
    Buffer& buffer = m_buffers[i];
    ScanoutBuffer request{i, static_cast<uint32_t>(width), static_cast<uint32_t>(height), -1, 0};
    std::string failure;
    int error = 0;
    if (ioctl(m_session, SESSION_ALLOC, &request) < 0 || request.fd < 0)
    {
      error = errno;
      failure = "display buffer allocation failed";
    }
    else
    {
      buffer.fd = request.fd;
      if (!buffer.import.Import(buffer.fd, width, height, CAMLAFBCImport::Layout::WIDE_BLOCK))
        failure = "display buffer import failed";
    }
    if (failure.empty())
    {
      glGenTextures(1, &buffer.texture);
      glBindTexture(GL_TEXTURE_2D, buffer.texture);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      const bool bound = buffer.import.BindTexture();
      glBindTexture(GL_TEXTURE_2D, 0);
      glGenFramebuffers(1, &buffer.framebuffer);
      glBindFramebuffer(GL_FRAMEBUFFER, buffer.framebuffer);
      glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, buffer.texture, 0);
      if (m_depthStencil)
      {
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, m_depthStencil);
        if (packed)
          glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_STENCIL_ATTACHMENT, GL_RENDERBUFFER, m_depthStencil);
      }
      const GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      const GLenum glError = glGetError();
      if (!bound || status != GL_FRAMEBUFFER_COMPLETE || glError != GL_NO_ERROR)
        failure = StringUtils::Format("display buffer is not renderable (bound {}, status 0x{:x}, GL error 0x{:x})",
                                      bound, status, glError);
    }
    if (failure.empty())
    {
      m_count = i + 1;
      continue;
    }
    if (i < 2)
      return Reject(failure, error, false);
    CLog::Log(LOGWARNING, "AFBC scanout: {} (errno {}); using two buffers", failure, error);
    // Closing the exported fd leaves the kernel session's allocation alive.
    // Recreate the session with two buffers if the third could not be used.
    if (buffer.fd >= 0)
    {
      Destroy(true);
      return Create(framebuffer, width, height, depthBits, stencilBits, false);
    }
    break;
  }

  m_width = width;
  m_height = height;
  m_target = m_lastTarget = -1;
  m_jobs.clear();
  m_queued = {};
  m_onScreen = -1;
  m_phase = Phase::IDLE;
  m_quit = m_cancel = false;
  m_error = 0;
  try
  {
    m_thread = std::thread(&CAMLAFBCPresenter::Run, this);
  }
  catch (const std::system_error& error)
  {
    return Reject(std::string("presenter thread: ") + error.what(), error.code().value(), false);
  }
  m_ready = true;
  if (!m_reported)
    CLog::Log(LOGINFO, "AFBC scanout: active; {}x{} GUI frames are rendered as AFBC display buffers ({}{})",
              width, height, m_count,
              queued ? " buffers" : " buffers, the kernel does not show a queued flip with the next vsync");
  m_reported = true;
  return true;
}

int CAMLAFBCPresenter::FreeBuffer() const
{
  // Round from the last frame's buffer, so the buffers take turns.
  for (unsigned int n = 1; n <= m_count; ++n)
  {
    const int i = static_cast<int>((static_cast<unsigned int>(m_lastTarget + 1) + n - 1) % m_count);
    if (!m_queued[i] && i != m_onScreen)
      return i;
  }
  return -1;
}

bool CAMLAFBCPresenter::BeginFrame(const std::string& framebuffer,
                                   int width,
                                   int height,
                                   int depthBits,
                                   int stencilBits,
                                   bool allowThirdBuffer)
{
  if (m_target >= 0)
    BeginLinearFrame(); // the last frame was neither presented nor abandoned
  if (m_failed || width <= 0 || height <= 0)
    return false;
  if (m_ready)
  {
    int error;
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      error = m_error;
      if (error == EAGAIN)
        m_error = 0;
    }
    if (error == EAGAIN)
    {
      // The display is not scanning out (off, or changing mode): the flip was
      // refused and nothing queued, so its buffer is free again. Draw
      // ordinary frames and try again a second later.
      m_retryAt = std::chrono::steady_clock::now() + RETRY_DELAY;
      return false;
    }
    if (std::chrono::steady_clock::now() < m_retryAt)
      return false;
    // A mode set made the session stale, or a flip failed: start over.
    if (error || width != m_width || height != m_height)
    {
      // A missed vsync (a mode set in progress) is worth another try too.
      const bool retry = !error || error == ESTALE || error == ETIMEDOUT;
      Destroy(true);
      if (!retry || ++m_retries > MAX_RETRIES)
      {
        Fail("flip failed", error);
        return false;
      }
    }
  }
  if (!m_ready)
  {
    const auto now = std::chrono::steady_clock::now();
    if (now < m_retryAt)
      return false;
    if (!Create(framebuffer, width, height, depthBits, stencilBits, allowThirdBuffer))
    {
      Destroy(true);
      if (m_rejection.permanent || ++m_createFailures > MAX_RETRIES)
      {
        Fail(m_rejection.reason.c_str(), m_rejection.error);
        return false;
      }
      // A display reset or mode set can leave the buffers briefly unavailable.
      CLog::Log(LOGWARNING, "AFBC scanout: {} (errno {}); trying again", m_rejection.reason,
                m_rejection.error);
      m_retryAt = now + RETRY_DELAY;
      return false;
    }
    m_createFailures = 0;
  }
  int target;
  {
    // The GPU may write a buffer as soon as it is bound: wait until one has
    // left the screen and is not queued. If none does in time, this frame is
    // linear: the return to linear drains the flips.
    std::unique_lock<std::mutex> lock(m_mutex);
    if (!m_condition.wait_for(lock, LATCH_TIMEOUT, [this] { return FreeBuffer() >= 0 || m_error; }) ||
        m_error)
      return false;
    target = FreeBuffer();
  }
  m_target = m_lastTarget = target;
  glBindFramebuffer(GL_FRAMEBUFFER, m_buffers[target].framebuffer);
  CFrameBufferObject::SetDefaultFramebuffer(m_buffers[target].framebuffer);
  return true;
}

void CAMLAFBCPresenter::EndFrame()
{
  // Whatever is drawn before the next BeginFrame (a screenshot renders the
  // GUI between frames) goes to the EGL window surface, which is not shown,
  // never into a display buffer.
  glBindFramebuffer(GL_FRAMEBUFFER, 0);
  CFrameBufferObject::SetDefaultFramebuffer(0);
  m_target = -1;
}

void CAMLAFBCPresenter::BeginLinearFrame()
{
  if (m_target >= 0)
  {
    // A frame begun in a display buffer is not presented; the buffer, never
    // queued, is drawn into again.
    EndFrame();
    return;
  }
  if (CFrameBufferObject::GetDefaultFramebuffer() == 0)
    return;
  CFrameBufferObject::SetDefaultFramebuffer(0);
  glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

uint64_t CAMLAFBCPresenter::Present()
{
  if (!m_ready || m_target < 0)
    return 0;
  glBindFramebuffer(GL_FRAMEBUFFER, m_buffers[m_target].framebuffer);
  // Depth and stencil are never shown: keep the GPU from writing them out.
  if (m_discard && m_depthStencil)
  {
    const GLenum attachments[]{GL_DEPTH_ATTACHMENT, GL_STENCIL_ATTACHMENT};
    m_discard(GL_FRAMEBUFFER, 2, attachments);
  }
  bool accept;
  {
    std::lock_guard<std::mutex> lock(m_mutex);
    accept = !m_error;
  }
  uint64_t frame = 0;
  if (accept)
  {
    const EGLSyncKHR sync = m_createSync(m_display, EGL_SYNC_FENCE_KHR, nullptr);
    if (sync == EGL_NO_SYNC_KHR)
      glFinish();
    else
      glFlush();
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      // A flip that failed meanwhile dropped the jobs queued before this
      // one; this frame would be flipped after them.
      accept = !m_error;
      if (accept)
      {
        frame = ++m_lastFrame;
        m_jobs.push_back({sync, m_target, frame});
        m_queued[m_target] = true;
      }
    }
    if (accept)
    {
      m_condition.notify_all();
      m_shown = true;
      m_retries = 0;
    }
    else if (sync != EGL_NO_SYNC_KHR)
    {
      m_destroySync(m_display, sync);
    }
  }
  EndFrame();
  return frame;
}

void CAMLAFBCPresenter::DropJobs()
{
  for (const Job& dropped : m_jobs)
  {
    if (dropped.sync != EGL_NO_SYNC_KHR)
      m_destroySync(m_display, dropped.sync);
    m_queued[dropped.buffer] = false;
    m_outcome.frames.push_back({dropped.frame, false});
  }
  m_jobs.clear();
}

void CAMLAFBCPresenter::Run()
{
  for (;;)
  {
    Job job;
    {
      std::unique_lock<std::mutex> lock(m_mutex);
      m_condition.wait(lock, [this] { return !m_jobs.empty() || m_quit; });
      if (m_jobs.empty())
        return;
      job = m_jobs.front();
      m_jobs.pop_front();
      m_phase = Phase::GPU;
    }
    int error = 0;
    if (job.sync != EGL_NO_SYNC_KHR)
    {
      if (m_waitSync(m_display, job.sync, 0, GPU_TIMEOUT_NS) != EGL_CONDITION_SATISFIED_KHR)
        error = EIO;
      m_destroySync(m_display, job.sync);
    }
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      if (m_cancel || m_quit)
      {
        // The render thread has gone back to linear frames, or the presenter
        // is being destroyed: nothing to flip.
        m_cancel = false;
        m_phase = Phase::IDLE;
        m_queued[job.buffer] = false;
        m_outcome.frames.push_back({job.frame, false});
        m_condition.notify_all();
        continue;
      }
      m_phase = Phase::FLIP;
    }
    if (!error)
    {
      // Returns after the vsync that put this buffer on screen, when the one
      // it replaced is no longer read, or with ETIMEDOUT when no vsync came
      // (the flip stays queued), or EAGAIN when the display was not scanning
      // out (nothing queued).
      ScanoutFlip flip{static_cast<uint32_t>(job.buffer), INTER_FORMAT, REVERSE_Y, FLIP_WAIT};
      if (ioctl(m_session, SESSION_FLIP, &flip) < 0)
        error = errno ? errno : EIO;
    }
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      m_phase = Phase::IDLE;
      m_queued[job.buffer] = false;
      m_outcome.frames.push_back({job.frame, !error});
      if (error)
      {
        // The frames queued behind it are not flipped either.
        DropJobs();
        m_error = error;
        m_outcome.redraw = true;
      }
      else
      {
        m_onScreen = job.buffer;
      }
    }
    m_condition.notify_all();
  }
}

void CAMLAFBCPresenter::ReturnToLinear()
{
  if (!m_shown)
    return;
  m_shown = false;
  if (m_session < 0)
    return;
  {
    // No AFBC flip may land after the linear frame: it would show AFBC again
    // with nothing left to end it. Frames not yet handed to the kernel are
    // dropped instead of waited for; their buffers, never shown, are drawn
    // into next. A flip with the kernel is ordered before the return to
    // linear once its call has returned. The buffer last shown stays out of
    // use until another flip replaces it.
    std::unique_lock<std::mutex> lock(m_mutex);
    DropJobs();
    if (m_phase == Phase::GPU)
      m_cancel = true;
    else
      m_condition.wait(lock, [this] { return m_phase == Phase::IDLE; });
  }
  uint32_t flags = DISABLE_ON_PAN;
  ioctl(m_session, SESSION_DISABLE, &flags);
}

void CAMLAFBCPresenter::TakeOutcome(Outcome& outcome)
{
  outcome.frames.clear();
  std::lock_guard<std::mutex> lock(m_mutex);
  // The emptied vector's storage collects the next frames.
  std::swap(outcome.frames, m_outcome.frames);
  outcome.redraw = m_outcome.redraw;
  m_outcome.redraw = false;
}

void CAMLAFBCPresenter::Destroy(bool contextCurrent)
{
  // Not a frame to present any more; the buffers go with the session.
  m_target = -1;
  if (m_thread.joinable())
  {
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      m_quit = true;
    }
    m_condition.notify_all();
    m_thread.join();
  }
  DropJobs(); // a safeguard: the thread reports every job it takes before it quits
  m_queued = {};
  m_onScreen = -1;
  // Closing the session returns the plane to the linear framebuffer before
  // the kernel releases the buffers; the imports below only drop references.
  if (m_session >= 0)
    close(m_session);
  m_session = -1;
  if (contextCurrent)
  {
    GLint bound = 0;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &bound);
    for (Buffer& buffer : m_buffers)
      if (buffer.framebuffer && static_cast<GLuint>(bound) == buffer.framebuffer)
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
  }
  for (Buffer& buffer : m_buffers)
  {
    if (contextCurrent)
    {
      if (buffer.framebuffer)
        glDeleteFramebuffers(1, &buffer.framebuffer);
      if (buffer.texture)
        glDeleteTextures(1, &buffer.texture);
    }
    buffer.framebuffer = buffer.texture = 0;
    buffer.import.Release();
    if (buffer.fd >= 0)
      close(buffer.fd);
    buffer.fd = -1;
  }
  if (contextCurrent && m_depthStencil)
    glDeleteRenderbuffers(1, &m_depthStencil);
  m_depthStencil = 0;
  if (m_fb >= 0)
    close(m_fb);
  m_fb = -1;
  CFrameBufferObject::SetDefaultFramebuffer(0);
  // A frame shown since the last TakeOutcome() is gone with its buffer,
  // perhaps a GUI size ago. Closing the session left the plane transparent,
  // if it showed a buffer.
  for (Frame& frame : m_outcome.frames)
    frame.shown = false;
  if (m_shown)
    m_outcome.redraw = true;
  m_ready = m_shown = false;
  m_count = 0;
  m_width = m_height = 0;
  m_quit = m_cancel = false;
  m_phase = Phase::IDLE;
  m_error = 0;
}

bool CAMLAFBCPresenter::Reject(std::string reason, int error, bool permanent)
{
  m_rejection.reason = std::move(reason);
  m_rejection.error = error;
  m_rejection.permanent = permanent;
  return false;
}

void CAMLAFBCPresenter::Fail(const char* reason, int error)
{
  if (!m_failed)
    CLog::Log(LOGWARNING, "AFBC scanout: disabled, {} (errno {}); the GUI keeps its linear framebuffer",
              reason, error);
  m_failed = true;
}
