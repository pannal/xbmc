# Amlogic idle-wake display diagnostics

This capture investigates a resumed box whose receiver still reports no signal.
It does not repair display state or establish that the HDMI picture returned.
The startup-shutdown investigation is separate.

## Setup and capture

Use a normally rebuilt image containing the wake hook and observer cleanup.
Record the installed Kodi/kernel artifact identities: the reporter's October 25
binaural image and later GUI candidates are different evidence. Panni builds
images; this change does not require a kernel change or a new advanced setting.

Enable Kodi's existing debug logging for an agreed test, preserving its original
state for cleanup. The existing `debug.showloginfo` setting controls this; merely
adding `P3I_GUI_TIMING_PATH` does not enable this trace. Debug logging remains
off by default. Older builds' debug logging alone does not arm an idle-wake trace.

After network, audio and player restoration, `CPowerManager::OnWake` starts a
fresh five-second trace on main, immediately before the `System.OnWake`
announcement. A long restoration does not consume this post-restoration budget.
If restoration never reaches that point, no wake trace is armed; retain the
complete Kodi and monotonic kernel/journal logs to distinguish that case.

Look for `p3i-end-display` records: `event=begin wake`, main-loop frame/present
states, bounded status snapshots, and a summary. Existing playback stop/end
triggers remain. A later trigger can supersede a wake trace; the preceding trace
emits a `superseded` summary rather than silently disappearing.

The main-loop pump runs even when rendering is disabled or rejected. At most
three status snapshots are requested: the first pump, first accepted GUI swap,
and timeout without a GUI swap. The existing limits remain five seconds,
32 detail records, eight milestone records and a 64-entry queue with explicit
loss accounting. Repeated states are suppressed. A zero-attempt timeout is
useful evidence, not success.

## Interpreting records

`t_us`, display-reset deadlines and first-draw/swap timestamps use the process
steady-clock origin. They are not calendar timestamps or receiver scanout times.
Use the separately recorded boot identity, monotonic/boottime clocks and raw
kernel/journal timing to interpret actual suspend and wall-clock adjustments.

| Field | Meaning |
| --- | --- |
| `render_enabled`, `status`, `gui_draw` | Main-loop render admission and command outcome. No draw is a distinct result. |
| `draw_display`, `draw_target_gen` | The generation retained for that attempted draw, before later window callbacks can replace it. |
| Snapshot `target_gen`, `current_target_valid` | The current target and admission at snapshot time, not proof that the last drawn target was valid. |
| `ready`, `geometry`, `can_render`, `pending`, `delay` | Existing main-owned display lifecycle, geometry and reset-delay state; also available when no present occurs. |
| `swap_attempted`, summary `swap_attempts` | Whether an actual EGL swap was attempted. Zero is not a failed swap. |
| `present`/`result`, `swap_failures`, `last_egl_error` | Skipped, invalid-target, failed and accepted outcomes remain separate. Error capture is limited to attempted failures in the active trace. |
| `first_gui_swap_us=0` | No accepted GUI swap was observed in that trace. |

`RenderAttemptStatus`: 0 stopped/not attempted, 1 begin rejected, 2 cancelled,
3 end rejected, 4 commands completed. `PresentResult`: 0 not attempted,
1 unreported, 2 skipped, 3 target invalid, 4 swap failed, 5 swap accepted.
Accepted means EGL accepted the call; it does not establish GPU completion,
valid pixels, receiver lock or physical presentation. Existing EGL error
ownership is unchanged outside the active diagnostic window.

## Status reads and observer cleanup

The three AML observers now omit `rxsense_state`, whose G12B getter writes PHY
registers, and `attr`, whose getter can materialize a default format cache used
by later modesets. Omitted fields say `unobserved`; they never report a healthy
numeric substitute. This applies even when a DV diagnostic log is suppressed,
because logging arguments are evaluated before filtering.

The wake snapshot uses cached `fb0/state` instead of the empty `fb0/blank`
getter and explicitly leaves blanking unobserved. Framebuffer state describes
software running/suspended state, not visible pixels. Geometry, video policy,
DV module parameters and HPD/VIC fields are mostly cached. `config` is an
existing driver-owned register/cached snapshot, not an arbitrary register dump
or receiver query. Snapshot values are sequential, limited to 80 characters per
status field, and do not form an atomic hardware receipt.

The audit covers all 49 original observer paths plus `fb0/state` against kernel
`2d0e0cc0db958d8d88f76407ac922f08d9a4004e`. Its 33 module parameters use standard
getters. This is not a blanket passivity claim for every other reader or kernel:
for example, the output-mode info label retains its separately scoped `attr`
reader. The full node/getter/source-line manifest accompanies the build handoff.

**Observer cleanup is an independent confound.** Removing receiver-sense polling
changes hardware access and removes receiver-sense-only change notifications
from the periodic probe. Surviving change and pending-refresh handling remain.
A non-reproduction after this change cannot identify the original culprit or
prove the wake trace fixed it. No kernel policy, automatic modeset, context
recreation, OSD repair, AVMUTE clearing, CEC or watchdog recovery is added.

## Coordinated device verification

Obtain fresh device availability, install/test scope, one real suspend/wake,
an explicit receiver-observation cue and the desired final chain state. Capture
the unchanged failure before any recovery. TV off/on and a Kodi restart are
not substitutes for suspending the Ugoos.

Preserve the installation, exact settings, cache, fades and persistent reboot
tracing. If the failure already occurs with a 1080 GUI, keep that GUI size and
compare actual HDMI 1080p60 versus 2160p60, with async/DV/skin and startup state
unchanged. Compare scaled versus native GUI separately if that remains an open
variable. Separately compare async OFF/ON with positive worker-activity evidence.
Record all build/observer differences. A restart or modeset can affect several
layers, so recovery alone does not identify a cause.

The prepared external `collect-passive.py` remains a fixed-node collector with
separately documented missing/empty/truncated and clock semantics. Its fake-root
test guards against an unsafe allowlist change; the production helper has no
additional dynamic excluded-node guard. Stream captures locally only after
approval. Host tests and CE compiler acceptance do not replace real suspend,
swap, receiver and physical-picture verification.
