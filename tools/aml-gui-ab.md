# AML GUI-input A/B diagnostic

For a coordinator-approved device run on an **already visible grey strip** during
native AML video, with controls and subtitles closed. This is an opt-in experiment,
not a grey-strip fix. Do not use startup no-signal or the uncaptured full-grey-screen
symptom as equivalent reproductions.

Arm once over the existing Kodi action transport:

```sh
kodi-send --action='SetProperty(enky.gui_ab,start,home)'
```

A shows one small black/white block near the picture centre; B shows two. Each
phase requests 72 accepted presentations through the ordinary GUI Render/Flip
path. A keeps the ordinary GUI candidate; B substitutes transparent RGBA outside
its cue. Both phases use the same full redraw budget and one pre-swap readback.
Nominal phase duration is three seconds at 24 Hz; use the recorded timestamps,
not that nominal duration. The whole trial expires cooperatively after ten seconds.

Observe the strip and cue during each phase. Record observations afterward without
opening an OSD or changing playback. For example, if A's strip and cue survived and
B's strip disappeared with its cue visible:

```sh
kodi-send --action='SetProperty(enky.gui_ab.a,bar_cue,home)'
kodi-send --action='SetProperty(enky.gui_ab.b,clear_cue,home)'
```

For either property, `bar_cue` means strip and cue visible, `clear_cue` means no strip
and cue visible, and `no_cue` means cue absent. Leave it unset if unknown. A absent
strip, absent cue or unknown appearance makes **all B outcomes inconclusive**.
An explicit negative A observation also cancels an active trial. The program does
not infer appearance from successful EGL swaps or its own cue pixels.

Cancel at any point:

```sh
kodi-send --action='ClearProperty(enky.gui_ab,home)'
```

Stop/replacement, display/context/route changes, missing eligibility, failed or
incomplete rendering, read errors and expiry cancel without automatic retry.
GUI rendering must already be enabled; the diagnostic does not enable it.

After the result has been written, collect the bounded JSON and complete Kodi log
from the usual CoreELEC paths:

```sh
cat /storage/.kodi/temp/enky-gui-ab.json
tar -czf /storage/enky-gui-ab-result.tar.gz -C /storage/.kodi/temp enky-gui-ab.json kodi.log
```

Match JSON `started_us` to the newest `p3i-gui-ab id=...` log entry. Require
`result_saved=true`; a missing file, failed save or allocation failure is unavailable
rather than evidence from an older trial. Confirm the appearance properties were
incorporated in the collected result. A completed run has `A_accepted=72` and
`B_accepted=72`. `b_interpretable` also requires valid A/B candidate readbacks and
cues, B zero RGBA outside its cue, positive A appearance and a known B appearance.
This status only admits coordinator interpretation; it does not establish cause.

After completion/cancellation, if diagnostic pixels were touched, the diagnostic
schedules three accepted full normal GUI redraws to reconstruct the GUI and remove
cues. These are restoration, not a
reverse experiment or proof of hardware-page restoration. At most six restoration
attempts are forced. If swaps fail or rendering remains unavailable, the result
retains `software_restoration_pending=true`; exhaustion stops further diagnostic
dirty marks and blocks another trial. Recover the normal display through ordinary
Stop/display recovery. If restoration remains pending or attempts were exhausted,
restart Kodi before another trial. Never interpret pending restoration as success.

Readback covers the owned current GL candidate, **not** exported-buffer selection,
producer completion, fetched graphics pixels, DV composition or physical output.
The per-row sums exclude cues; row zero is the GL bottom row, and the retained top
band starts at `height-top_rows`. Nonzero RGB at zero alpha is not coverage.
Guards are sequential CPU/sysfs observations, not an atomic hardware snapshot.
No register programming, direct extra EGL swap, logical OSD/subtitle change, L5
change or DV policy change is performed. Requested work and memory are capped,
but a synchronous faulted GL read/swap cannot be forcibly timed out or cancelled
by the main-thread action transport.
