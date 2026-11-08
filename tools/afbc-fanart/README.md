# Selective AFBC artwork textures on CoreELEC Mali-G52

This path, on by default, converts eligible static artwork once on Kodi's
existing shared-context upload worker. The retained image uses AFBC storage;
normal GUI drawing and crossfades sample it through the existing texture shader.
The JPEG disk cache, `<fanartres>` and `<imageres>` settings are unchanged. The
normal `<fanartres>1080</fanartres>` is supported; no 4K upscaling is added.

Install a kernel with the linux-amlogic commits "ion: initialize pool cache
policy and flush device-less ARM64 pages" and "ion: report the ARM64 device-less
page cache sync in sysfs". The first initializes the page-pool cache flag and
repairs ARM64's device-less cache sync: `get_dma_ops(NULL)` previously selected
a dummy no-op, allowing cached zeros from fresh pages to write back over
uncached/GPU data. The corrected path prints `ion: ARM64 device-less page cache
sync enabled` once when it runs. The second adds the read-only
`/sys/module/ion/parameters/arm64_deviceless_sync`, which reads `Y`.

The reported CoreELEC G52 hardware comparison confirms the repair: the old build
failed three of three GPU probe runs and one of three CPU-only memory runs. All
six updated-build GPU probe suites passed, including 1,872 pixel comparisons and
120 guard checks without pixel mismatches or guard damage. The standalone
`test-probe` branch of v-lix/xbmc retains detailed correctness diagnostics and
its `hardware-validation.json` records these results.

`afbcfanart` and `afbcimages` default to true, but Kodi converts only while
`/sys/module/ion/parameters/arm64_deviceless_sync` reads `Y`. On a kernel
without the fix, the Mali import and the conversion would still succeed while
dirty cache lines later overwrite the GPU-written image, so no error would reach
the ordinary-texture fallback. Kodi reads the file once, before its first
conversion; if it is missing or not `Y`, Kodi logs a warning and keeps ordinary
textures until it restarts. To turn the conversion off, merge these elements
into `advancedsettings.xml`, then restart Kodi:

```xml
<advancedsettings>
  <gui>
    <afbcfanart>false</afbcfanart>
    <afbcimages>false</afbcimages>
  </gui>
</advancedsettings>
```

`afbcimages` enables selective artwork compression, including eligible fanart,
posters, wall thumbnails, covers and large transparent images. `afbcfanart`
alone admits only opaque background fanart, roughly 16:9 and at least 640 pixels
wide. Both options require `asynctextureupload`; no conversion runs on the GUI
thread. Enabling `afbcimages` is sufficient for both categories.

Selective artwork must contain at least **131,072 decoded pixels** (512 KiB of
RGBA), with both dimensions at least 128 and neither above 4096. For example,
480x720 and 300x450 posters qualify; 256x256 thumbnails and tiny buttons do not.
This is a conservative starting policy, not a measured hardware break-even point.
JPEG/PNG file size is not the selection criterion: the GPU reads decoded pixels.
There is no content scan or per-image compression-ratio trial on the upload path.

The texture must be RGBA/BGRA, uploaded by the dedicated worker, without mipmaps,
retained CPU shadows or padded texture extents. Alpha is preserved by the
converter. Compressed bundles, animated texture-manager arrays, fonts, video,
render targets and synchronous GUI-thread uploads do not enter this conversion
path. Large-loader classification and motion-delay controls remain independent.
Already-published textures are not retroactively recompressed; restart Kodi for
a comparison. Small assets keep the ordinary path, even if a tiny solid colour
would be highly compressible: import, allocation, padding and conversion have
fixed costs. A shader-drawn solid rectangle needs no image conversion at all.

This policy avoids turning rapid scrolling into many tiny conversion jobs. It
still adds one draw per newly uploaded qualifying image, so performance must be
compared on the device. AFBC does not shorten CPU layout/input processing or
remove source-image decoding. An actual time break-even depends on conversion
cost, saved bandwidth, texture reuse and competing GPU/CPU work, not a universal
file-size threshold.

The private import path requires an exact original or byte-verified stripped
ARM32 G52 r12p0 fbdev library:

```text
949b043bd373a7779d4338a87ecdd929e0430dfddcb092de7a4e6cda3a905397
789adf85dc9c630bb049249bf7e28ed40380e919ae1f379a352167d3107ec445
```

The loaded library is identified through its actual entry-point address and
hashed once. No library is replaced or patched. Other drivers/architectures
retain ordinary textures. Only the plain AFBC format tested on the device is
used. Noise-like artwork may compress poorly; performance depends on the image
and the remaining GUI workload.

## Conversion and lifetime

Each newly uploaded eligible image uses one full-size conversion draw. The
converter reuses its shader program and framebuffer within the upload context;
context teardown releases that cache. The converted texture is detached from
the reusable framebuffer after every conversion. This prevents an attachment
from retaining storage beyond the GUI's external-backing lifetime.

The production path performs no full-image readback, CPU pixel comparison,
CPU mapping/initialization, DMA-BUF CPU-sync calls or allocation-guard scans.
The fixed kernel supplies zeroed ION pages and the GPU clears and overwrites
the render target. A conservative 64 KiB allocation tail is retained as padding,
without initializing or reading it. It does not reduce allocated RAM capacity.

The existing upload-completion synchronization remains before the texture is
published to the GUI context. Allocation, import, framebuffer and GL errors
retain the ordinary source texture and stop further AFBC attempts for the
process. Successful conversion frees the temporary ordinary GPU texture and
preserves the existing CPU staging-buffer policy.

EGLImage, private registry metadata and ION storage accompany the texture
through the GUI-thread deletion queue. Cancellation and eviction cannot release
that backing before its GL name is deleted. Texture updates allocate fresh
storage. The first successful image in each category produces an INFO message:

```text
AFBC images: background active; retained 1920x1080 texture
AFBC images: artwork active; retained 480x720 texture
```

Per-image success logging is omitted. Failures retain a useful warning.
There are no OSD/display-plane submissions or window-buffer format changes.
The final GUI framebuffer and alpha-blending workload remain unchanged.

## Validation

`TestConversion.cpp` exercises the production single-draw converter with Mesa
GLES textures at odd dimensions, 1080p and 4K, including RGBA/BGRA input and
alpha. Pixel readback exists only in this test, not the runtime converter.
It also checks sampler restoration, detached framebuffer attachments, resource
reuse and context-cache teardown.

```sh
c++ -std=c++17 -O1 -g -Wall -Wextra -Werror -DHAS_GLES=2 -DTARGET_LINUX \
  -Ixbmc -I/path/to/Khronos/headers -fsanitize=address,undefined \
  -fno-omit-frame-pointer tools/afbc-fanart/TestConversion.cpp \
  -o /tmp/test-afbc-conversion -lEGL -lGLESv2 -lcrypto -ldl
EGL_PLATFORM=surfaceless LIBGL_ALWAYS_SOFTWARE=1 ASAN_OPTIONS=detect_leaks=0 \
  /tmp/test-afbc-conversion
```

`python3 tools/afbc-fanart/test-lifetime.py` exercises actual retirement and GUI
deletion methods, including worker release, storage retention and duplicate
retirement. The separate upload-context suites cover cancellation and teardown.
`TestPolicy.cpp` covers the actual selective admission policy, size boundaries
and the kernel check with a missing, empty, `N` and `Y` file. Host controls do
not emulate Mali AFBC. On an AM6B+ with a 3840x2160 GUI paced at 60 Hz, a held
List view scroll missed 0.13-0.16 refreshes per key with asynchronous uploads
alone and 0.00 with AFBC artwork as well. To compare `afbcimages` on and off,
repeat the same navigation and keep asynchronous upload, motion-delay settings
and GUI resolution fixed.

The current feature only changes retained source textures.
