# Capture a native 44.1 kHz audio dropout

Use the diagnostic image supplied for your existing CoreELEC installation.
Send `audio-diagnostic.py` alongside that image and place the script in
`/storage/`. These instructions use CoreELEC's standard root profile.

The image keeps **Allow native HDMI PCM sample rates OFF by default**.
A default-settings playback is the resampled compatibility case. It does
**not** test the reported native 44.1 kHz failure.

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
kernel event categories/numeric fields and capture/mark metadata. They exclude the general Kodi log,
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
workload before/after the normal test-image update. Confirm baseline selection
N4707 and candidate selection N4704 in the returned setup events. Fractional
120 Hz selects N4459 in both versions and does not exercise this correction.
The test-image update remains an agreed operator action. A successful candidate
run is validation evidence, not proof of a downstream clock/receiver mechanism.

## Finish

```sh
python3 /storage/audio-diagnostic.py disable
```

This removes only the audio marker. The current recording ends at its bound;
a normal Kodi restart disables it immediately. Keep the native option OFF
after the control unless explicitly continuing native testing. No reboot-trace
token, DDR setting or driver/HDMI policy is changed. Keep collected files until
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
Kernel access/clock nodes may
be unavailable, and a new-event kernel capture contains no earlier history.
Kernel records retain their printk timestamp and the collector's monotonic
receive time separately. An unclassified event means its raw text was discarded,
not that no fault occurred.
ALSA proc `status` is intentionally excluded because reading it resets some
diagnostic counters. HDMI `config` and clock snapshots show host configuration,
not continuous wire timing or receiver lock; AVMUTE readback has a narrower
hardware meaning. Only existing kernel/HDMI messages are collected; no new
driver/FIFO instrumentation is enabled.

A clean host trace during an audible gap can narrow the search toward portions
not observed here. It does not prove a receiver fault or continuous hardware
delivery. Listener marks remain essential. Diagnostic timing includes measurement
cost; host cost tests do not establish the cost on the ARM device. Time budgets
cannot guarantee completion while a kernel/filesystem read or write is stalled.
Such gaps are unavailable evidence.
