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
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// Renders the completed GUI straight into AFBC display buffers and has the OSD's
// Mali AFBC decoder scan those same buffers out, instead of a linear EGL window
// surface. Needs the kernel's AFBC scanout session (FBIOGET_OSD_AFBC_SCANOUT).
//
// The kernel allocates the contiguous buffers; each is the colour attachment of
// one framebuffer object. Three are used when the kernel shows a flip queued
// right after the previous one with the next vsync (afbc_scanout_queued_flips),
// otherwise two. A frame is drawn into a buffer that is neither on screen nor
// queued; with three, one frame can wait for its vsync while the next is drawn.
// If none is free, the render thread waits for a vsync to retire one before it
// binds it. The flip is queued behind an EGL fence, so the render thread never
// waits for the GPU.
//
// All methods run on the render thread with the primary context current,
// except Destroy(false) for a context that is already gone.
class CAMLAFBCPresenter
{
public:
  // Reads Y when the kernel shows queued flips with the next vsync.
  static constexpr const char* QUEUED_FLIPS_PARAMETER =
      "/sys/module/fb/parameters/afbc_scanout_queued_flips";
  static bool KernelQueuesFlips(const char* path = QUEUED_FLIPS_PARAMETER);

  // Invoked on the render or presenter thread, sometimes with m_mutex held.
  // The callback may only publish upload eligibility and must not block.
  explicit CAMLAFBCPresenter(std::function<void(bool)> setUploadEligibility = {});
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
  // flips it after the frames handed over before it, once the GPU is done.
  // Either way the EGL window surface is the default again until the next
  // BeginFrame. Returns the frame's number, or 0 if it was not handed over:
  // TakeOutcome() tells whether it reached the screen.
  uint64_t Present();
  // The next frame is an ordinary swap: show the linear framebuffer again in
  // the vsync of that swap's pan. Call before eglSwapBuffers. Frames still
  // waiting for the GPU or for their turn are dropped; one already handed to
  // the kernel is waited for, which takes a vsync (at most the kernel's one
  // second).
  void ReturnToLinear();
  void Destroy(bool contextCurrent);

  struct Frame
  {
    uint64_t number;
    bool shown; // it reached the screen; otherwise it never will
  };
  struct Outcome
  {
    // Each frame handed over is reported once. The frames shown are in the
    // order they were presented; Destroy() reports every frame not yet
    // taken as not shown, since its buffers are gone.
    std::vector<Frame> frames;
    // A frame was lost, or the session was closed while one was shown: the
    // plane shows an older frame or nothing, so the GUI must be drawn again.
    bool redraw{false};
  };
  // Replaces outcome with what became of the frames handed over since the
  // last call. Reusing one Outcome keeps its storage.
  void TakeOutcome(Outcome& outcome);

  // One of this presenter's buffers is shown or queued to be: the next
  // ordinary frame must return the plane to the linear framebuffer first.
  bool Shown() const { return m_shown; }
  // No further attempts until Kodi restarts.
  bool Failed() const { return m_failed; }
  // Display buffers of the current session, 0 without one.
  unsigned int Buffers() const { return m_ready ? m_count : 0; }

  // Tests only: where KernelQueuesFlips() looks.
  void SetQueuedFlipsParameter(std::string path) { m_queuedFlipsParameter = std::move(path); }

private:
  static constexpr unsigned int MAX_BUFFERS = 3;

  bool Create(const std::string& framebuffer, int width, int height, int depthBits, int stencilBits,
              bool allowThirdBuffer = true);
  // Records why Create() failed; returns false.
  bool Reject(std::string reason, int error, bool permanent);
  void Fail(const char* reason, int error);
  void Run();
  // A buffer neither on screen nor queued; -1 if none. Caller holds m_mutex.
  int FreeBuffer() const;
  // Reports the frames of jobs that will not be flipped. Caller holds m_mutex.
  void DropJobs();
  void EndFrame();
  void SetUploadEligibility(bool eligible) const;

  const std::function<void(bool)> m_setUploadEligibility;

  struct Buffer
  {
    int fd{-1};
    CAMLAFBCImport import;
    GLuint texture{0};
    GLuint framebuffer{0};
  };
  std::array<Buffer, MAX_BUFFERS> m_buffers;
  unsigned int m_count{0};
  GLuint m_depthStencil{0};
  int m_fb{-1};
  int m_session{-1};
  int m_width{0};
  int m_height{0};
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
  std::string m_queuedFlipsParameter{QUEUED_FLIPS_PARAMETER};
  bool m_ready{false};
  bool m_shown{false};
  bool m_failed{false};
  bool m_reported{false};
  EGLDisplay m_display{EGL_NO_DISPLAY};
  void (*m_discard)(GLenum, GLsizei, const GLenum*){nullptr};
  PFNEGLCREATESYNCKHRPROC m_createSync{nullptr};
  PFNEGLCLIENTWAITSYNCKHRPROC m_waitSync{nullptr};
  PFNEGLDESTROYSYNCKHRPROC m_destroySync{nullptr};

  // The buffer of the frame being drawn. Render thread only.
  int m_target{-1};
  int m_lastTarget{-1}; // the previous frame's, so the buffers take turns
  uint64_t m_lastFrame{0}; // the number of the last frame handed over

  // Presenter thread hand-off.
  std::thread m_thread;
  std::mutex m_mutex;
  std::condition_variable m_condition;
  struct Job
  {
    EGLSyncKHR sync;
    int buffer;
    uint64_t frame;
  };
  std::deque<Job> m_jobs; // handed over, not yet taken by the presenter thread
  // Buffers handed over and not yet shown or dropped, and the buffer the
  // plane shows or last showed: neither may be drawn into.
  std::array<bool, MAX_BUFFERS> m_queued{};
  int m_onScreen{-1};
  // What the presenter thread is doing with the job it took.
  enum class Phase
  {
    IDLE,
    GPU, // waiting for the GPU: ReturnToLinear() may still drop the frame
    FLIP, // the flip is with the kernel
  } m_phase{Phase::IDLE};
  bool m_cancel{false}; // drop the frame of the job in the GPU phase
  Outcome m_outcome;
  bool m_quit{false};
  // Set only by the presenter thread when a frame failed (its fence or its
  // flip); BeginFrame clears EAGAIN, which queued nothing.
  int m_error{0};
};
