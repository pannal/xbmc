/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#pragma once

#include "guilib/AMLAFBCTexture.h"

#include "system_gl.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <array>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

// Renders the completed GUI straight into AFBC display buffers and has the OSD's
// Mali AFBC decoder scan those same buffers out, instead of a linear EGL window
// surface. Needs the kernel's AFBC scanout session (FBIOGET_OSD_AFBC_SCANOUT).
//
// The kernel allocates two contiguous buffers for it; each is the colour
// attachment of one framebuffer object. A frame is drawn into the buffer that
// is not on screen: before binding it, the render thread waits for the vsync
// that retired it. The flip is queued behind an EGL fence, so the render thread
// never waits for the GPU.
//
// All methods run on the render thread with the primary context current,
// except Destroy(false) for a context that is already gone.
class CAMLAFBCPresenter
{
public:
  CAMLAFBCPresenter() = default;
  ~CAMLAFBCPresenter() { Destroy(false); }
  CAMLAFBCPresenter(const CAMLAFBCPresenter&) = delete;
  CAMLAFBCPresenter& operator=(const CAMLAFBCPresenter&) = delete;

  // Selects this frame's display buffer as the default framebuffer. False
  // leaves the EGL window surface in place for an ordinary frame.
  bool BeginFrame(const std::string& framebuffer, int width, int height, int depthBits, int stencilBits);
  // The EGL window surface is the default again: an ordinary frame follows,
  // or the frame begun in a display buffer is not presented.
  void BeginLinearFrame();
  // Hands the frame drawn since BeginFrame to the presenter thread, which
  // flips it once the GPU is done. Either way the EGL window surface is the
  // default again until the next BeginFrame. True only says the frame was
  // handed over: TakeOutcome() tells whether it reached the screen.
  bool Present();
  // The next frame is an ordinary swap: show the linear framebuffer again in
  // the vsync of that swap's pan. Call before eglSwapBuffers. A frame still
  // waiting for the GPU is dropped; one already handed to the kernel is
  // waited for, which takes a vsync (at most the kernel's one second).
  void ReturnToLinear();
  void Destroy(bool contextCurrent);

  // What became of the frames handed over since the last call. Destroy()
  // keeps only the request to redraw.
  struct Outcome
  {
    bool shown{false}; // a frame reached the screen
    // A frame was lost, or the session was closed while one was shown: the
    // plane shows an older frame or nothing, so the GUI must be drawn again.
    bool redraw{false};
  };
  Outcome TakeOutcome();

  // One of this presenter's buffers is shown or queued to be: the next
  // ordinary frame must return the plane to the linear framebuffer first.
  bool Shown() const { return m_shown; }
  // No further attempts until Kodi restarts.
  bool Failed() const { return m_failed; }

private:
  bool Create(const std::string& framebuffer, int width, int height, int depthBits, int stencilBits);
  // Records why Create() failed; returns false.
  bool Reject(std::string reason, int error, bool permanent);
  // Waits for the last queued flip to be on screen or to fail; false if it
  // failed or, when bounded, is still outstanding.
  bool WaitLatched(std::unique_lock<std::mutex>& lock, bool bounded);
  void Fail(const char* reason, int error);
  void Run();

  struct Buffer
  {
    int fd{-1};
    CAMLAFBCImport import;
    GLuint texture{0};
    GLuint framebuffer{0};
  };
  std::array<Buffer, 2> m_buffers;
  GLuint m_depthStencil{0};
  int m_fb{-1};
  int m_session{-1};
  int m_width{0};
  int m_height{0};
  unsigned int m_target{0};
  unsigned int m_retries{0};
  // Failed Create() calls in a row, and when the next attempt may run (also
  // a flip refused while the display was not scanning out).
  unsigned int m_createFailures{0};
  std::chrono::steady_clock::time_point m_retryAt{};
  struct
  {
    std::string reason;
    int error{0};
    bool permanent{false}; // nothing a later attempt could change
  } m_rejection;
  bool m_ready{false};
  bool m_shown{false};
  bool m_failed{false};
  bool m_reported{false};
  EGLDisplay m_display{EGL_NO_DISPLAY};
  void (*m_discard)(GLenum, GLsizei, const GLenum*){nullptr};
  PFNEGLCREATESYNCKHRPROC m_createSync{nullptr};
  PFNEGLCLIENTWAITSYNCKHRPROC m_waitSync{nullptr};
  PFNEGLDESTROYSYNCKHRPROC m_destroySync{nullptr};

  // Presenter thread hand-off.
  std::thread m_thread;
  std::mutex m_mutex;
  std::condition_variable m_condition;
  EGLSyncKHR m_jobSync{EGL_NO_SYNC_KHR};
  int m_jobBuffer{-1};
  // What the presenter thread is doing with the job it took.
  enum class Phase
  {
    IDLE,
    GPU, // waiting for the GPU: ReturnToLinear() may still drop the frame
    FLIP, // the flip is with the kernel
  } m_phase{Phase::IDLE};
  bool m_cancel{false}; // drop the frame of the job in the GPU phase
  Outcome m_outcome;
  bool m_latched{true}; // the last queued flip is on screen, its predecessor retired
  bool m_quit{false};
  // Set only by the presenter thread once it is done with its flip: with
  // m_latched, the thread is idle. BeginFrame clears EAGAIN, which queued
  // nothing.
  int m_error{0};
};
