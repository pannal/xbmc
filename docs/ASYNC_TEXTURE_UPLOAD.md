# Amlogic asynchronous GUI texture upload

Asynchronous GUI texture upload is **on by default**. Device tests once showed
better frame pacing without it, when several uploads could be admitted at once;
with uploads limited to one admitted job, it removes the 12-17 ms render-thread
upload of each new 1920x1080 fanart, and with AFBC artwork (also on by default
where the kernel reports its ION cache fix, see `tools/afbc-fanart/README.md`)
it removed missed refreshes from held List, WideList and Fanart view scrolls on
an AM6B+ with a 3840x2160 GUI. To use synchronous uploads, add this override
and restart Kodi:

```xml
<advancedsettings>
  <gui>
    <asynctextureupload>false</asynctextureupload>
  </gui>
</advancedsettings>
```

Remove the override or set it to `true` and restart Kodi to return to
asynchronous uploads.

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

Interface > Skin > "Delay background images during motion"
(`lookandfeel.delaybackgroundimages`) is off by default; background images are
then neither deferred nor held.

When it is on, background-sized images, whose declared screen coverage is at
least half the screen, defer their initial decode job while the GUI moves.
Containers and group lists report each frame in which their scroller runs, and
controls each frame in which a non-repeating animation is in progress (a delay
alone, looped and pulsed animations do not count). After all windows and dialogs
have been processed, deferred requests start on a quiet frame or after their own
three-second timeout. Without motion they start at the end of their request
frame. A new foreground consumer of the same path promotes the deferred decode
immediately, preserving the shared request and its original cache policy.
Polling or GPU draw demand alone does not override background deferral. Smaller
images start decoding immediately. This scheduling applies with async uploads
both enabled and disabled.

A background request for an image that is still loaded is held the same way:
it is neither uploaded nor handed out until motion stops, so its crossfade does
not start during that frame's motion. The image may be loaded because it was
released within the two-second cache grace, or because another consumer keeps
it: an image control in a hidden view keeps the texture it had when its group
was hidden, such as the fanart of the item focused when the window opened. A
decode that a background consumer requested, alone or together with a
foreground one, is also held when it completes, until the end of its frame and
within that consumer's timeout, because motion may have started again while it
decoded. A hold only delays background consumers: foreground consumers of the
same image are still served, and their upload demand proceeds. The copy a
crossfade makes of its outgoing image is not a new request, and is neither
deferred nor held. A crossfade draws the whole image twice, see-through, so a
background image's crossfade does not run while the GUI moves in this or the
previous frame: the incoming image is shown at once if it is ready; otherwise,
also when there is no incoming image, the last outgoing image that shows
anything is kept at full strength, and fades as usual once the GUI is still.

Its child setting "Also delay images of the selected item"
(`lookandfeel.delayselectionimages`, off by default, available only while the
parent is on) treats a window image that follows the selection as a background
image too, from a tenth of the screen, such as the poster in a list view's info
panel. A window image is one that is not part of a list row: list rows show
their own items and are never delayed this way. It follows the selection when
its texture uses list item info, directly, as an argument of another info or
through a skin variable's labels or conditions, so playback artwork such as
the now-playing cover is not delayed. Smaller images such as logos and flags
still change at once, and images with a constant texture are unaffected.
A selection image is not delayed while a direct consumer holds the same file:
an image that is neither a background request nor a selection image, such as a
list row. The big poster of a poster view, such as the Poster view of P3i
Estuary, shows the file of a poster in the scrolling list. Delaying it would
show the previous poster twice, so it changes and crossfades as usual during
motion. A panel showing other artwork of the selected item, or a poster whose
row is not loaded yet, is still delayed. Any direct consumer of the file, even
one in a hidden view or a row of another item with the same icon, exempts the
image too. The manager
counts the references direct consumers acquired; each texture remembers the
kind of its reference for its release.

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
executes the frontend allocation, clipping, polling, idle redraw and fade paths,
including background fades ended or held by motion and empty incoming images,
which window images count as selection images, and selection images shown by
a list row, which are neither delayed nor have their crossfade ended.
`tools/test-gui-selection-labels.py` checks which labels follow the selection,
through container infos and nested skin variables.
`tools/test-gui-background-defer.py` checks deferred decoding, foreground promotion,
per-request expiry, held reuse and completion, the setting on and off, its child
setting, motion reports, direct consumer counting, composition
with draw-demand uploads, the actual control and animation Animate methods'
motion reports and the group list's report call. The list and panel scroll tests
verify that containers report their scroller's active frames.

Host tests and compiler acceptance do not establish Mali context sharing,
physical pixels, smoothness or safe deferred driver deletion. Async is on by
default; broader physical acceptance is unfinished.
Continue validating normally rebuilt artifacts with matched async off/on/off
cold-artwork and warm comparisons, actual worker-thread uploads,
shared-texture visibility/completion, cancellation, skin/display transitions
and agreed orderly shutdown. Retain the original fade durations and separate
loop timing, swap cadence and physical presentation.
