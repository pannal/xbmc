# Amlogic asynchronous GUI texture upload

Asynchronous GUI texture upload is **off by default**. Matched device tests
showed better frame pacing with it disabled. To opt in for further testing,
add this override and restart Kodi:

```xml
<advancedsettings>
  <gui>
    <asynctextureupload>true</asynctextureupload>
  </gui>
</advancedsettings>
```

Existing explicit `true` overrides remain enabled. Remove the override or set
it to `false` and restart Kodi to use synchronous uploads.

When enabled, large-image decoding and GPU upload run as separate jobs. Decoded
images stay private until a clipped draw attempt requests their upload through
the shared EGL context. One high-priority upload job serves all consumers of a
request; publication follows GPU completion and context release. Transparent
incoming fade targets also request upload. Auto-sized images conservatively
request it during allocation because their unknown bounds can prevent drawing.
This can move newly encountered poster/fanart uploads out of the render loop.
It does not remove bandwidth, GPU or driver contention. Upload-free warm
intervals do not establish an upload-offloading benefit. Ordinary first-use
render-thread upload remains the fallback when the worker cannot bind or an
upload job cannot be scheduled. Partially failed uploads are not published.

Background-sized images, whose declared screen coverage is at least half the
screen, also defer their initial decode job while a list or panel scrolls.
Containers report scrolling each frame; after all windows and dialogs have been
processed, deferred requests start on a quiet frame or after their own
three-second timeout. Without scrolling they start at the end of their request
frame. A new foreground consumer of the same path promotes the deferred decode
immediately, preserving the shared request and its original cache policy.
Polling or GPU draw demand alone does not override background deferral. Smaller
images start decoding immediately. This scheduling applies with async uploads
both enabled and disabled.

The previous crossfade image stays until the new one is ready. Cache grace and
fade durations are unchanged. These are conservative admission rules, not exact
visibility or decoded-size tests: transformed/auto-sized images may bypass the
background heuristic, and an admitted image may later be clipped or hidden.
Deferral can increase first-image latency and retain decoded CPU staging longer.
Timeout expiry can still start work while scrolling; no frame-timing improvement
or total-memory reduction is established by the source tests.

The upload context uses surfaceless binding when supported, with a compatible
1x1 pbuffer fallback. The selected primary-window config is unchanged; pbuffer
creation requires its actual EGL_PBUFFER_BIT. Actual binding is checked on use.
An unsuccessful path is disabled for the context generation, avoiding an error
for every subsequent image. Surface replacement keeps the context/share group;
full context destruction requires the application retirement contract below.

The renderer's automatic HIGH context-priority hint is not added to the upload
context. The upload context retains the caller's original attributes, including
any explicit priority; otherwise EGL defaults to MEDIUM. The assigned upload
priority is queried and logged at debug level. Priority is a driver hint, so
different requests do not guarantee different scheduling or better pacing.

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
including failed direct decoding before trying the cache fallback. A job already
cancelled at those checkpoints skips subsequent work.

Each image request, decoder and upload job share an atomic cancellation token.
Releasing the last queued consumer destroys that request and marks its token;
decoded entries retain the existing two-second cache grace unless released
immediately. Other consumers keep the request live. Deferred decodes have no job
to cancel; rejected decode admission becomes a load failure, not a pending job.
Jobs retain the token while they run. Worker upload checks it before binding and
again after acquiring the lease, without taking a job-manager lock under the EGL
lease. A cancelled waiter skips upload and synchronization but still releases the lease
and reports release failure. A replacement request for the same path has a new
token. Generic job-manager cancellation alone does not mark this request token;
it retains the loader checkpoints and publication suppression. These checks do
not interrupt decoding or cache waits, or GL work admitted by the final token
check. An admitted upload can therefore still finish without being published.
Known-size prefetched images normally wait for a draw request before GPU upload;
auto-size and conservative draw-attempt admission can still upload images that
never contribute pixels. Once admitted, work is not revoked on every frame.
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

`tools/test-image-upload-request-cancellation.py --sanitize` adds actual request
creation, reference release and token ownership methods to threaded lease-wait
tests. It covers shared consumers, replacement requests, copied callbacks,
failed decoding, active-upload cancellation and release errors. Recording
fixtures reject manager locking under the lease. Historical request methods and
semantic mutations must fail their specific assertions. Binding, decode and GL
remain fixtures. `tools/test-aml-texture-upload-priority.py --sanitize` records
the actual context-creation attributes and advisory priority query, including
driver-forced priorities, failed queries and unavailable upload contexts. It
does not exercise real driver scheduling.

`tools/test-image-upload-demand.py --sanitize` exercises the actual large-image
manager and jobs, including private publication, shared consumers, cancellation,
cache grace, stale callbacks and admission failures. `tools/test-gui-upload-demand.py`
executes the frontend allocation, clipping, polling, idle redraw and fade paths.
`tools/test-gui-background-defer.py` checks deferred decoding, foreground promotion,
per-request expiry and composition with draw-demand uploads. The list and panel
scroll tests verify that containers report their scroller's active frames.

Host tests and compiler acceptance do not establish Mali context sharing,
physical pixels, smoothness or safe deferred driver deletion. Async remains
opt-in; broader physical acceptance is unfinished.
Continue validating normally rebuilt artifacts with matched async off/on/off
cold-artwork and warm comparisons, actual worker-thread uploads,
shared-texture visibility/completion, cancellation, skin/display transitions
and agreed orderly shutdown. Retain the original fade durations and separate
loop timing, swap cadence and physical presentation.
