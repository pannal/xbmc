# Detected video borders

These `Player.Process` info labels expose the file scanner's accepted subtitle
envelope for SDR, HDR and Dolby Vision video, independently of authored Level 5
metadata, manual Level 5 overrides and display scaling:

| Label | Value |
| --- | --- |
| `Player.Process(video.borders.detected)` | `1` when a result is available for the current video; otherwise `0`. |
| `Player.Process(video.borders.left.offset)` | Left border in coded-video pixels. |
| `Player.Process(video.borders.right.offset)` | Right border in coded-video pixels. |
| `Player.Process(video.borders.top.offset)` | Top border in coded-video pixels. |
| `Player.Process(video.borders.bottom.offset)` | Bottom border in coded-video pixels. |
| `Player.Process(video.borders)` | One versioned JSON snapshot containing validity, coded dimensions and all four offsets. |

An unavailable offset is an empty string. A valid zero means no detected border
on that side. For skins, test `String.IsEqual(Player.Process(video.borders.detected),1)`
before using the offsets. Addons needing all four values together should read the
JSON label once, rather than combine separate reads across a video change.

The offsets describe the resolved file-wide subtitle envelope, which can include
variable framing. They do not assert a border on every individual frame or
high-confidence eligibility for replacing native Dolby Vision metadata. Detection
settings govern availability; an unchanged-file cache hit can supply the result.
An accepted result can survive a same-source seek, and changing the video selection
or dimensions invalidates it.

Example valid snapshot for a 1920×1080 video with 140-pixel top and bottom borders:

```json
{"version":1,"valid":true,"coordinate_system":"coded-video-pixels","width":1920,"height":1080,"left":0,"top":140,"right":0,"bottom":140}
```

Unavailable snapshots have `valid:false` and null dimensions and offsets. These
offsets use different coordinates from `Player.Process(video.active.picture)`,
which describes the displayed picture in GUI coordinates. The
`video.dovi.l5.*` labels describe native L5 with manual, auto-letterbox,
high-confidence detected and authored precedence.
