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

## Native reproduction

In **Settings > System > Audio**, at Advanced level, turn **Allow native HDMI
PCM sample rates ON**. Keep the existing HDMI Multi Ch PCM endpoint. Allow the
brief audio reconfiguration, then start a fresh playback of the same track.

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
**OFF**, allow reconfiguration and use the same track from its start:

```sh
python3 /storage/audio-diagnostic.py collect compatibility --seconds 180
```

Verify actual **48000 Hz** during playback. Keep the player, DSP, display and
HDMI setup identical. Mark any dropout the same way. No Kodi restart is needed
between these two cases. Together they fit within the 900-second window if
started promptly. If recording expired or another restart is necessary, collect
the current trace first, then enable/restart a new window.

Each command prints a ZIP path and SHA256. Retrieve those two ZIPs using SFTP
and return them with the listening observations. The ZIPs contain only this
diagnostic trace, selected playback ALSA/HDMI/clock snapshots, new audio-related
kernel event categories/numeric fields and capture/mark metadata. They exclude the general Kodi log,
media URLs/paths, settings databases, credentials and full configuration dumps.
Endpoint output is restricted to known ALSA endpoint names and CARD/DEV IDs;
the collector additionally redacts URLs, IPv4 and MAC addresses in the approved
configuration snapshots. Raw kernel message text is never exported; event
categories and selected rate/errno numbers replace it. Inspect files before sharing.

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

No observation, dropped submissions, absent windows, invalid reads, truncation
or missing `END` are **unavailable evidence**, not healthy zeros. A capture made
while recording continues normally lacks final `END`; its last window may be
partial. The collector states this explicitly. Kernel access/clock nodes may
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
