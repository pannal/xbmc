# Opt-in GUI frame timing

This diagnostic is compiled for `HAS_LIBAMCODEC` builds. It is disabled by
default. Before Kodi starts, set `P3I_GUI_TIMING_PATH` to a new **absolute** file
path. `CApplication::Run` initializes it once on the application thread. It
cannot be enabled later in the process. Other builds compile no-op hooks.

Creation uses `O_EXCL`, rejects existing files and symlinks, and sets mode 0600.
The filesystem must support reserving the complete 8,650,816-byte file with
`posix_fallocate`. Initialization failure disables capture; it may leave a newly
created partial file, which must not be interpreted as a valid capture. Existing
files are never replaced or removed. Choose another new path for another run.

The recorder pre-touches a fixed shared mapping at initialization. Active hooks
perform no explicit file I/O, heap allocations, GPU queries or GPU completion
waits. File-backed pages can still incur kernel writeback or later page faults.
Each coarse scope samples monotonic wall and current-thread CPU clocks at entry
and exit. This has unmeasured observer overhead. Disabled hooks in an AMCodec
build still test a cheap atomic/TLS gate. Workers never initialize the recorder
or contribute records, regions or counters. Hooks preserve `errno` and do not
read EGL/GL error state.

Each record spans one **whole main-loop iteration**, including end-loop idle
pacing when present. It is not a display-frame, swap, presentation or scanout
receipt. Regions can be skipped or invoked repeatedly. Their times are
**inclusive** and must not be summed as separate work. Modal/nested invocations
remain in the owning iteration; they can increase counts and inclusive totals.
A recursively nested region can consequently total more than its frame span.

| Region index | Scope |
| --- | --- |
| 0 Process | `Process()` call from the main loop |
| 1 FrameMove | `FrameMove(true, renderGUI)` call from the main loop |
| 2 Render | `Render()` call from the main loop |
| 3 WindowProcess | Entire `CGUIWindowManager::Process`, including graphics-lock acquisition, `DoProcess`, depth assignment and publishing bounds |
| 4 AssignDepth | Depth-assignment block, including active-dialog copying and sorting |
| 5 PublishBounds | Entire `PublishRenderBounds`, including admission check, graphics locking, collection and publication |
| 6 RenderPass | Entire `RenderPass`, including the selected single/dual path |
| 7 EGLSwap | The actual `eglSwapBuffers` call in `TrySwapBuffers`, excluding its admission and subsequent error handling |

EGLSwap wall/CPU durations include driver work and waits in that call; they are
not GPU execution time. Modal paths that call application methods directly
outside `Run` are not additional Process/FrameMove/Render scopes, although their
instrumented window-manager and swap calls contribute to the owning iteration.

Counters 0/1/2 are actual font-cache **Lookup/Hit/Miss invocations**, shared by
the static and dynamic caches. They have no per-call clock samples. They are
not text-draw counts: misses perform a further lookup when storing the result,
and bounds capture performs another lookup. Do not infer eligible cached draws
or allocations from these totals or from GL buffer uploads.

## Binary format, version 1

The target and supported host tests write little-endian fixed-width integers,
with the sizes below checked at compile time. All times are integer nanoseconds.
There are no pointers, strings, artwork URLs or credentials in the records.

The 64-byte header is `8s, 10*u32, 2*u64`:

| Offset | Field |
| --- | --- |
| 0 | Eight bytes `P3IGUI1\0` |
| 8, 12, 16, 20 | Version=1, header bytes=64, record bytes=264, capacity=32768 |
| 24, 28 | Region count=8, counter count=3 |
| 32, 36 | Initializing process ID and thread ID |
| 40, 44 | Completed-record count and header flags |
| 48, 56 | Reserved, zero |

Each 264-byte record is `5*u64, 2*u32, 27*u64`:

| Offset | Field |
| --- | --- |
| 0 | Sequential iteration ID, starting at 1 |
| 8, 16, 24, 32 | Begin wall, begin CPU, end wall, end CPU |
| 40, 44 | Record flags and nested-frame count |
| 48 | Eight region triples: calls, total inclusive wall ns, total inclusive CPU ns |
| 240 | Three counter totals |

Flags are `1=full`, `2=clock failure`, `4=unbalanced scopes`, `8=nested Frame`,
`16=recursive region`. The last two are record annotations; the first three are
header conditions. Clock or scope errors stop recording and leave the current
record unpublished. Reaching capacity publishes exactly 32768 records, marks
the header full, and stops observations. It never wraps or overwrites records.

The completed prefix is immutable and its length is published by an aligned
32-bit release store. Unfinished records and the zero-filled remainder are not
data. Prefer reading the file after capture has stopped; a copied live header is
not a transactional snapshot of its flags. Validate magic, version, format sizes/counts,
file length, count, conditions and record IDs before analysis. Header clock or
scope failures require reporting the incomplete capture. Full is an expected
bounded stop, about nine minutes at 60 iterations/s, rather than a lossless
record of the remaining run.

`python3 tools/test-gui-frame-timing.py --sanitize` compiles the actual header in
two translation units and checks the binary writer, opt-out/non-AMCodec builds,
exclusive creation, worker rejection, inclusive/nested accounting, capacity,
`errno` and injected failures. Negative controls remove ownership, inclusive
accumulation and exclusive creation. Host clocks are deterministic stubs; these
tests do not establish target performance, scheduling fairness or GUI smoothness.
