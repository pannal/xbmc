# Active-picture snapshot for Python addons

p3i exposes one optional, read-only info label:

```python
raw = xbmc.getInfoLabel('Player.Process(video.active.picture)')
```

The label returns a JSON object. One call copies the complete snapshot under
the existing player-video cache lock; no separate bounds queries are needed.
Version 1 has these fields:

```json
{"version":1,"valid":true,"coordinate_system":"gui-1080","width":1920,"height":1080,"left":0,"top":140,"right":1920,"bottom":940}
```

`left`, `top`, `right` and `bottom` are floating-point rectangle edges, not
source offsets. The origin is top left; X increases right and Y increases down.
Right and bottom describe the outer edges, so a control with the same edge is
inside. Units are GUI coordinate units on a fixed 1920×1080 reference canvas,
independent of the display mode and the video's coded dimensions. Bounds are
clipped to the renderer's visible view before coordinate conversion.

The conversion inverts Kodi's normal GUI scaling for this reference canvas,
including video calibration/overscan, GUI insets, skin zoom and output pixel
ratio. It does not use the currently rendering window or group's transform.
For a normally scaled XML window of coordinate size `W×H`, convert its final
window-space button bounds with `x * 1920 / W` and `y * 1080 / H`. A 1080i window
already uses these units. Include window/group origins and any animation or
other control transforms when obtaining those bounds: Python control positions
alone are local coordinates, not necessarily the final visible rectangle.
Custom unscaled windows require their own screen-to-GUI-reference conversion.
Calibration can produce bounds outside 0–1920 or 0–1080; do not clamp them again.

The rectangle describes geometry **before temporary OSD/subtitle suppression**.
Reading it has no signalling or playback side effects. Changing
`script.plex.osd_active` does not change its geometry inputs. It is conceptual
picture/L5 geometry, not a readback of the current HDMI metadata or evidence
that a particular display honours that metadata. L5 forwarding and OSD policy
switches still determine whether the signal has an effect.

Geometry uses the renderer's current source crop, destination, zoom, pixel
ratio and vertical shift. For native Dolby Vision it prefers an applicable
manual L5 override (L5 substitution enabled), then nonzero authored L5 at the
presented frame's exact PTS, then a current detected area. Automatic letterbox
padding is already represented by the renderer's destination; additive source
L5 refines the coded picture without counting the padding twice. When automatic
letterboxing replaces implausible source L5, that source refinement is excluded.
An absolute manual override references the padded 16:9 canvas: on 3840×1600,
280/280 maps to the same picture as automatic padding on 3840×2160. A manual
zero override covers that whole canvas, including the player-added bars.
Other output types use current general detection when available. Missing,
stale or malformed source/detected refinements fall back to the known visible
video rectangle; this fallback can still be valid.

An unavailable renderer/presented picture, empty view, fully cropped/offscreen
active picture, invalid manual geometry,
invalid GUI scaling, rotation, source/output stereo or nonlinear stretch yields:

```json
{"version":1,"valid":false,"coordinate_system":"gui-1080","width":1920,"height":1080,"left":null,"top":null,"right":null,"bottom":null}
```

Startup, substantive renderer reconfiguration, flush and playback teardown clear
validity. Item retirement clears it when requested, before asynchronous native
cleanup completes. Publication holds the same renderer state lock as retirement,
so an in-flight update cannot republish an old item's rectangle after retirement.
The normal player cache reset also clears it. A paused displayed frame remains
valid and updates after view/override changes. Publication runs during FrameMove,
even when hardware video skips GUI rendering. Consumers may see the preceding
GUI frame's coherent snapshot; this is not a synchronous decoder query.

Other Kodi builds can return an empty label. Treat empty/malformed JSON, an
unknown version/coordinate system or `valid:false` as unavailable. Do not reuse
bounds from the previous item or substitute source L5 offsets. The caller owns
fallback policy; conservatively signalling while a marker is visible is safe
when protecting it from possible cropping, but the API does not assert a signal.

PM4K's intended valid-snapshot rule is:

```text
signal = full OSD open OR any visible marker button not fully contained in bounds
```

Containment needs all four edges, including a button that only partly crosses an
edge. This API does not change marker visibility, focus, countdowns or skipping.
PM4K's conditional signalling is a separate addon change.

Run `python3 tools/test-active-picture.py` for production geometry, GUI
conversion, snapshot/label and lifecycle checks with host service substitutes.
The host check requires g++ and fmt headers; set `FMT_INCLUDE` to an existing
include directory containing `fmt/` if the host does not install them.
These checks and CE syntax compilation do not establish native playback, rendered
GUI, HDMI behaviour, linking or inclusion in an image.
