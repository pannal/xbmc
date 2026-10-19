# Capture a native 44.1 kHz audio dropout

Use the diagnostic image supplied for your existing CoreELEC installation.
Send both `audio-diagnostic.py` and `audio-diagnostic-system.py` alongside that
image and place them together in `/storage/`. These instructions use CoreELEC's
standard root profile. The collector checks that its companion is present before
creating a capture. Use the matching diagnostic kernel and Kodi revision.

**Allow native HDMI PCM sample rates** is ON by default. Saved choices can
override the default, so explicitly check the option for each case. Native reproduction needs it ON with actual **44100 Hz** output; the
compatibility control needs it OFF with actual **48000 Hz** output.

Keep the same affected FLAC, PAPlayer, HDMI chain/input, volume, DSP/ReplayGain,
crossfade and display mode throughout. Start with the existing reliable
reproduction. A matching-samples FLAC/WAV comparison is a later discriminator.
FLAC and AAC both decode to PCM; that fact does not explain a difference.

## Enable one bounded diagnostic window

In SSH:

```sh
python3 /storage/audio-diagnostic.py enable
systemctl restart kodi
```

The restart interrupts audio and can put HDMI devices into standby. Allow at
least 30 seconds, then confirm the TV and audio device are awake before playing.
The helper does not change playback, settings, services or boot configuration.
The restart above is an explicit operator action.
During `collect`, it temporarily enables only the dedicated kernel audio
diagnostic window, using an owner token. It leaves another active owner's window
alone and stops only its own window. A failed stop is explicit; the kernel
deadline bounds remaining observations. Persistent reboot tracing is untouched.

Kodi reads the enable marker once at startup. No general debug/component
logging is required. Recording has a budget of 900 seconds or 8 MiB of
diagnostic output, subject to OS/filesystem progress. The output is
`/storage/.kodi/temp/audio-diagnostic.log`.
The marker must be removed afterward to prevent another recording at restart.
Restarting overwrites this trace; collect it before restarting.
The collector prints a Kodi diagnostic preflight result before playback. For a
valid comparison it must say **current**, with no warning that the budget will
expire during collection. Otherwise enable/restart Kodi, wait for startup and
begin a new collection. The collector warns and still saves other evidence;
it does not restart Kodi automatically.

## Native reproduction

In **Settings > System > Audio**, at Advanced level, turn **Allow native HDMI
PCM sample rates ON**. Keep the existing HDMI Multi Ch PCM endpoint. Allow the
brief audio reconfiguration, then prepare the same track without starting it.

Immediately before starting the track, run:

```sh
python3 /storage/audio-diagnostic.py collect native --seconds 180
```

Wait for **Collection ready. Start the SAME track now.** The collector takes
driver/system baselines before that cue. Keep the SSH session open. A fresh
Kodi preflight and a verified driver window are both needed for the complete
comparison; report warnings rather than treating missing telemetry as healthy.

The collector prints the active ALSA rates every five seconds. Verify actual
**44100 Hz** while the track is playing. If it shows 48000, another rate, or
unavailable/closed throughout playback, report that and do not describe this
as a verified native test. The trace also records the requested/negotiated
rate, format, channels, endpoint and native-option value.

Listen without seeking, pausing or changing settings, beyond the known
55-second point. At each audible dropout, press Enter in the SSH collector.
Alternatively, enter a position and whether audio recovered automatically:

```text
55 yes
1:12 no
```

Each mark records wall time and monotonic time. A late mark is useful: the
trace retains the earlier one-second aggregates. Also report approximate track
position, gap duration, whether audio returned automatically, and any visible
receiver mute/lock change. If you only pressed Enter, include position and
recovery in your reply. No screenshots or unrelated settings dumps are needed.

## Resampled compatibility control

After the native capture finishes, stop playback, turn the native option
**OFF** and allow reconfiguration. Retrieve the previous ZIP, then enable/restart
Kodi for a fresh diagnostic window. Allow at least 30 seconds for startup and
check that the TV/audio are awake. Prepare the same track from its beginning,
then run the collector before starting playback:

```sh
python3 /storage/audio-diagnostic.py collect compatibility --seconds 180
```

Verify actual **48000 Hz** during playback. Keep the player, DSP, display and
HDMI setup identical. Mark any dropout the same way. Restarting overwrites the
Kodi trace, so retrieve each ZIP first. A fresh window keeps diagnostic recording
active in both cases even when preparation takes longer than expected.

Each command prints a ZIP path and SHA256. Retrieve those two ZIPs using SFTP
and return them with the listening observations. The ZIPs contain only this
diagnostic trace, selected playback ALSA/HDMI/clock snapshots, new audio-related
kernel event categories/numeric fields, system/driver observations and capture/mark
metadata. They exclude the general Kodi log,
media URLs/paths, settings databases, credentials and full configuration dumps.
Endpoint output is restricted to known ALSA endpoint names and CARD/DEV IDs;
the collector additionally redacts URLs, IPv4 and MAC addresses in the approved
configuration snapshots. Raw kernel message text is never exported; event
categories and selected rate/errno numbers replace it. Inspect files before sharing.
The collector also reads the cached HDMI fractional-rate policy and retains
approved numeric fields from existing HDMI setup messages: selected N, lookup
clock/depth/rate enums and audio source. These messages must occur after collection
starts, so start the track only after the collector is ready. Missing setup
messages are unavailable; they are not a physical N/CTS measurement.

`system.jsonl` and `driver.jsonl` retain about one-second snapshots. Each has an
8 MiB cap; configuration and kernel-message files each have a512 KiB cap, and
the Kodi trace retains its8 MiB cap. Truncation, first lost time, event sequence
gaps, producer contention and kernel-log losses are recorded. These are bounded
files, not unlimited logging. The collection itself remains limited to60–600
seconds. The kernel window permits at most900seconds and has fixed aggregate
slots plus a32-event ring. Samples before/after stopping retain terminal events.

## Comparing display modes on an affected setup

Keep native rates **ON**, verify actual **44100 Hz** in both cases and use the
same track from its beginning, player, chain, DSP and colour/deep-colour settings.
Choose the first display mode in Kodi, then enable/restart Kodi for a fresh
window, allow at least 30 seconds and confirm the TV/audio are awake. Run:

```sh
python3 /storage/audio-diagnostic.py collect native --seconds 180
```

Listen beyond the previously affected positions and mark every gap. Retrieve
the printed ZIP. Repeat from enable/restart for the other display mode, using
the **same command and `native` label**. Identify which ZIP is 60 Hz and which is
120 Hz in the reply; display snapshots also record the modes. The labels
`native60` and `native120` are not accepted. Report whether each gap recovered
automatically; no drops is also a useful result.

For an N-table candidate comparison, retain the same affected 120 Hz mode and
workload and report the actual selected N. The current table correction uses
N4704 for integer120Hz; fractional120Hz selects unchangedN4459. Do not change
fractional policy or flash an extra image to force a branch for this capture.
The next two mode captures can use the same fully instrumented image. A normal
test-image update remains an agreed operator action. A successful run is
validation evidence, not proof of a downstream clock/receiver mechanism.

## Finish

```sh
python3 /storage/audio-diagnostic.py disable
```

This removes only the audio marker. The current recording ends at its bound;
a normal Kodi restart disables it immediately. Restore your preferred native-rate
setting after the comparison. No reboot-trace token, DDR setting or driver/HDMI
policy is changed. Keep collected files until
they have been received; remove only these owned capture directories/ZIPs and
the diagnostic log when finished. Return to the ordinary image using the same
normal CoreELEC update procedure after the test build is no longer needed.

## Reading the evidence

The source records decoder read duration/result/bytes and ring fill; preparation
and started-stream decoder/player observations have separate `prefill.` and
unprefixed aggregates. Player records distinguish no decoded data with AE space,
AE backpressure, feed-loop gaps and partial acceptance. Stream probes observe
the existing 200 ms buffer wait. Loop/call durations include intentional waits;
interpret them with state and buffer availability. Engine probes record state, dispatch, buffered
time, target, stream buffering, resampling ratio/quality and transferred frames.
Sink probes distinguish pooled payload from synthetic silence and record
delivery gaps, call duration and returns. Pooled payload can include GUI audio.

ALSA probes wrap every existing write, including retries, with attempted/accepted
frames, short/zero/error results and timing. Existing delay/state calls and
recover/prepare/resume/start/forward/drop/drain/close results are observed.
There are no new ALSA status queries, buffer/timeout changes or recovery fixes.
Expected blocking write time is not automatically a fault.
PCM writes additionally probe at most16 evenly spaced bytes per block and export
only probe/nonzero/zero-block counts. Passthrough is skipped. No audio bytes,
samples or media fingerprints are stored. A zero-probe block is sampled data;
these counts do not prove audibility, full-block silence or correct samples.

System observations include selected audio/main thread execution and runnable
queue-wait counters, context switches, priorities, CPU affinity, per-CPU jiffies
and selected audio/display IRQ/softIRQ counts. PID/TID start times and boot UUID
separate lifetimes; missing roles, counter resets and sampling gaps are explicit.
No arbitrary thread names, command lines or full process/status dumps are exported.
On this vendor4.9 kernel, CONFIG_SCHEDSTATS permits sched_info run-queue counters
independently of the extended runtime schedstats toggle. The collector records
the matching configuration flags and toggle; unknown enablement is unavailable.
One-second deltas do not measure maximum individual scheduler delay.

Kernel observations distinguish shared primary IRQ handler invocations, threaded
handler entry/exit and dispatch delay. A shared primary invocation is not proof
of a hardware-owned period interrupt. Cached route generations, prepare/trigger/
free/close events and normalized DMA positions scope progress across tracks.
No raw DMA addresses are exported. Relative-pointer repeats and invocation gaps
can occur intentionally while stopping/pausing; correlate them with lifecycle
events. Raw FRDDR status is retained without a universal FIFO-health decode.

HDMI events cover ACR selection, audio setup, FIFO/audio reset requests, mute/GCP,
mode/PHY changes, HPD/internal IRQ observations and existing HDCP status changes.
The G12 passive snapshot includes N, four immediate raw CTS readbacks with a
stability flag, FIFO latch/mask, packet/mute/clock state and PLL0/PLL1. It uses no
busy-wait, hardware write, interrupt acknowledgement or recovery operation.
Unsupported/offline/busy/expired paths are unavailable. Register samples are
not atomic with every programming callback; compare event timestamps and ready
state. A stable readback is not a physical clock or HDMI packet measurement.
FIFO bits may be sticky from setup; repeated high samples are not new faults.
The vendor's measured-CTS debug reader uses the same registers, but G12 hardware
coherence and receiver-visible CTS remain unverified. Physical wire continuity,
receiver lock/buffer state and exact transient FIFO event counts remain outside
this software trace.

`WINDOW` records describe actual monotonic intervals and dropped diagnostic
submissions. CSV rows contain interval start/end, metric, observation count,
sum, minimum, maximum, time of maximum, last value/time and sum-saturation flag.
Microsecond suffixes describe time; ppm suffixes mean a ratio multiplied by
one million. Frame/sample/byte metrics retain their named units. RAW/passthrough
uses different packet units; use this capture to investigate PCM, and check
the RAW flags before interpreting frame totals. Metrics can aggregate multiple
started streams. Do not infer exact per-stream conservation from their sums.

No observation, dropped submissions, absent windows, invalid reads or truncation
are **unavailable evidence**, not healthy zeros. `capture.json` now separates
`recording_complete` (an `END` footer exists) from `coverage` for this collection:
`current`, `partial`, `none` or `unavailable`. It saves collection start/end and
trace window times, reasons, dropped submissions and final flush lag. `current`
allows up to two seconds of worker flush lag, reported as `end_lag_us`; it is a
temporal assessment, not a healthy-audio verdict. A running trace can have current
coverage without `END`; an expired, completed trace can have no current coverage.
Older collectors' `complete` field described recording completion only. Unknown
wall/monotonic clock relationships and discontinuous windows are unavailable.
The new logger also supplies boot UUID, PID and process start ticks. Capture
start/end identity, inode and header checks reject replacement/restarted traces;
separate event-loss and event-ring-overflow counts distinguish missing events.
Kernel access/clock nodes may
be unavailable, and a new-event kernel capture contains no earlier history.
Kernel records retain their printk timestamp and the collector's monotonic
receive time separately. An unclassified event means its raw text was discarded,
not that no fault occurred.
ALSA proc `status` is intentionally excluded because reading it resets some
diagnostic counters. HDMI `config` and clock snapshots show host configuration,
not continuous wire timing or receiver lock; AVMUTE readback has a narrower
hardware meaning. Existing kernel messages and the dedicated bounded diagnostic
interface are both collected; no new IRQ masks, acknowledgements or recovery
actions are enabled.

A clean host trace during an audible gap can narrow the search toward portions
not observed here. It does not prove a receiver fault or continuous hardware
delivery. Listener marks remain essential. Diagnostic timing includes measurement
cost; host cost tests do not establish the cost on the ARM device. Time budgets
cannot guarantee completion while a kernel/filesystem read or write is stalled.
Such gaps are unavailable evidence.
