# Async artwork upload admission and scheduling

Upload admission and scheduling are active with gui/asynctextureupload enabled,
independently of afbcfanart and afbcimages. They target delayed edge posters and
contention in the upload pipeline. On an AM6B+ with a 3840x2160 GUI paced at 60
Hz, a held List view scroll missed 0.13-0.16 refreshes per key with them,
against 0.18-0.21 with synchronous uploads.

## Admission before the first visible sliver

The texture Process path requests GPU preparation for pending foreground artwork
in the container's existing preload range. Projected bounds account for origins,
scaling, camera and animations. Images on screen receive foreground priority;
images within one image width/height of the display are speculative. The
horizontal margin is capped at a quarter of screen width and the vertical margin
at half of screen height. This does not walk the rest of the library or create
more CPU decode requests. It admits uploads for already requested images.

Hidden, bundled/normal, ready and background-sized textures do not enter this
foreground prefetch path. Unknown geometry retains its existing fallback.
Backgrounds still request uploads at their drawing attempt and respect motion
holds. The synchronous option skips the projection and prefetch work entirely.

## One admitted uploader

Waiting images stay in the large-texture manager, with at most one upload job
submitted to JobManager. They do not occupy several decoder worker slots while
waiting for the same EGL context. The existing high-priority job retains access
to the pool's reserved capacity. A long upload occupies one worker; other
normal-priority workers can continue decoding. This is a serialized queue, not a
new thread or a change to GPU context priority.

Current foreground demand is chosen before fanart, then nearby speculative
work. Older retained images have lower priority. Speculative waiting uploads
are capped at eight and expire after 250 ms without demand; the GUI thread
supplies clock snapshots under the manager lock, so callbacks do not read the
global animation clock concurrently. Shared requests coalesce by image path.

When the last consumer releases an image, its waiting upload is removed at
once while decoded cache data keeps its existing grace period. An upload
already admitted is allowed to finish for that cache, or observes the entry's
cancellation token if the entry is destroyed. Its callback always retires the
single-job reservation, including deletion/replacement of its image entry.
Individual release does not suppress this callback through CancelJob.

Textures stay private until the upload completes and releases its EGL context.
The existing worker glFinish remains: removing it without a replacement
completion contract could publish incomplete GPU data. Queue rejection and
unsupported contexts retain the normal CPU/main-renderer fallback. Partial
upload failures remain unpublished. Application-wide shutdown still drains
JobManager before destroying the window system.

## Device comparison

Start with the same navigation, fade-limit/blend options and debug state:

1. Async off, both AFBC options off.
2. Async on, both AFBC options off.
3. Async on with afbcfanart, then with afbcimages if desired.

Admission and scheduling have no advancedsettings elements of their own and
apply whenever asynctextureupload is true. Compare actual smoothness with debug
off; use a short debug-on capture separately. Faster preparation cannot
guarantee that a new row is ready when the GPU, decoder or storage is
overloaded, nor remove main-thread skin/control processing. AFBC conversion
still adds work.

## Host validation

Run `python3 tools/async-texture-upload/test-manager.py`. It compiles the actual
manager header/implementation plus production JobManager admission, completion
and cancellation methods against controlled decode and GPU completion fixtures
under ASan/UBSan. Cases cover worker-slot admission, priority, bounded/stale
prefetch, release/reacquire, deletion and replacement, cancellation while an
upload is executing, motion holds, rejection, missing context and upload errors.

Run `python3 tools/async-texture-upload/test-prefetch.py`. It compiles the actual
foreground prefetch method using real rectangle geometry and controlled screen
projection. Cases cover visible/near/far bounds, origins/scaling, zero geometry,
hidden/ready/normal/background bypass and no projection with async off.
These tests do not emulate Mali throughput or physical frame delivery.
