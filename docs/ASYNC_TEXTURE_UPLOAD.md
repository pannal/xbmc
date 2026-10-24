# Amlogic asynchronous GUI texture upload

The existing advanced setting remains **off by default**:

```xml
<advancedsettings>
  <gui>
    <asynctextureupload>true</asynctextureupload>
  </gui>
</advancedsettings>
```

When enabled, the large-image loader uploads through a shared EGL context on
its worker thread and finishes GPU work before returning the image for
publication. This can move newly encountered poster/fanart uploads out of the
render loop. It does not remove bandwidth, GPU or driver contention, and cannot
explain warm scrolling intervals containing no uploads. Ordinary first-use
render-thread upload remains the fallback when the worker cannot bind.

The upload context uses surfaceless binding when supported, with a compatible
1x1 pbuffer fallback. The selected primary-window config is unchanged; pbuffer
creation requires its actual EGL_PBUFFER_BIT. Actual binding is checked on use.
An unsuccessful path is disabled for the context generation, avoiding an error
for every subsequent image. Surface replacement keeps the context/share group;
full context destruction requires the application retirement contract below.

The worker owns one mutex/TLS lease from successful bind through upload,
completion and release. Scope cleanup releases the same object on the same
thread if upload or synchronization throws. Failed unbind attempts same-thread
eglReleaseThread; a further failure poisons upload admission permanently for
that window-system object. Releasing the mutex does not make this poisoned
context available for another worker. EGL resource deletion can remain deferred;
this does not establish that another thread's current binding was released.
Reporting errors occurs after retiring the C++ lease, including when logging
allocation fails. Failed image jobs do not publish their partial texture, and
retain destruction until job deletion outside the large-image callback lock.

PrepareForShutdown closes upload admission before display retirement and waits
for the active lease. Normal application Stop rejects new jobs and drains all
job workers/completion callbacks before GUI/window-system deletion. Per-image
cancellation removes publication but lets an active upload finish and release
its context. Image jobs check cancellation before decoding and after decoding,
before starting GPU upload. A job already cancelled at those checkpoints skips
that work. Cancellation after the check, while waiting for the upload lease or
inside the driver, can still finish an upload that will not be published.
Prefetched images can also be uploaded before they are ever drawn; moving their
work to a worker does not by itself reduce the amount of GPU work.
The upload mutex alone does not own the window-system object or
track texture generations. Arbitrary full-context recreation with surviving
image jobs/textures is unsupported; draining only the current upload is
insufficient. Failed window-system initialization before GUI creation has no
large-image jobs. Preserve these boundaries when changing lifecycle code.

`tools/test-aml-texture-upload-context.py` retains the original baseline failure
witness and compatibility cases. `tools/test-aml-texture-upload-hardening.py
--sanitize --tsan` executes the actual upload-context, texture-upload and image
publication methods against recording EGL/GL/decode/job fixtures on real threads.
It covers exceptions, bind/release failures, cancellation, queued admission,
publication and callback-lock destruction, with destructive controls. The real
CGLTexture destructor and whole application job-drain orchestration are not
executed by this harness. TSan startup can fail on this host; that is reported
as unavailable for that attempt, separately from a completed clean run.

`tools/test-image-loader-cancellation.py --sanitize` exercises the actual image
loader and job-manager cancellation methods with threaded decode barriers and
recording texture/cache fixtures. It covers both loading paths, cancelled
requests and replacement-job isolation. It does not establish how many uploads
in a device capture were cancelled, or measure a pacing improvement.

Host tests and compiler acceptance do not establish Mali context sharing,
physical pixels, smoothness or safe deferred driver deletion. Before considering
an enabled default, validate a normally rebuilt artifact with matched async
off/on/off cold-artwork and warm comparisons, actual worker-thread uploads,
shared-texture visibility/completion, cancellation, skin/display transitions
and agreed orderly shutdown. Retain the original fade durations and separate
loop timing, swap cadence and physical presentation.
