# TrueHD tracing and regression checks

The main development source has a focused restart trace. The broader MAT branch,
codec timing and delivery CRC traces from `dfe303ff` and `6218b2ec` remain removed.
Their historical instrumented source is preserved at
`9a90ccab8904ef1cf98b18bc7d1348cf469f33f2` for the existing regression checks.
Neither trace is a fix for reported dropouts or Evolution dialnorm behavior.

## Focused restart trace

Enable normal Kodi debug logging **before** the seek and keep it enabled for the
capture. No component-specific logging or new setting is required. A Full-sync,
TrueHD IEC codec reset arms the first 256 produced MAT packets. Opening a codec,
unpausing it, non-Full sync and other codecs do not arm a window. The trace does
not change audio data, reset policy, mute/drop decisions or sink configuration.

Every record begins `TrueHD restart:`. Match `epoch` and `packet`, not just wall
time or the latest reset. Old in-flight buffers retain their original epoch.

- `stage=reset` allocates an epoch for a codec reset. `major_trace=source_au_v1`
  identifies the complete source-AU mapping extension.
- `stage=player_reset` relates it to `GENERAL_FLUSH` or `GENERAL_RESET` and the
  player's `sync_epoch`. A flush can have causes other than a user seek; correlate
  the surrounding normal seek logs rather than treating every reset as a seek.
- `stage=produce` identifies the original MAT packet. `major_head=true` means the
  raw MAT has a major-sync word at offset 32 (reserved IEC bytes, MAT start code,
  then AU prefix). It is a structural check, not full TrueHD/CRC validation or a
  count of all later majors in the payload.
- `stage=enqueue` records the copied source byte range. `offset` and `bytes` refer
  to the original `packet_bytes`, including partial AddData retries/fragments.
- `stage=ae` gives the actual consumed packet's `decision=payload`, `mute` or
  `drop`. `mute` replaces its original bytes with pause output; `drop` consumes
  them without replacement. The source identity survives either decision.
- `stage=ae_insert_pause` is emitted only for the first inserted pause while that
  source fragment stays queued. The eventual `stage=ae` record reports the total
  inserted count and milliseconds. Inserted silence does not acquire the source
  identity and is not mistaken for consuming that packet.
- `stage=sink_api` reports packed carrier frames and the sink API's accepted
  frames, including partial acceptance on failure. `kind=replacement_pause` and
  `kind=discard` always have `payload_api_frames=0`. `complete` describes the
  attempted output, so a completed pause/discard is not submission of the original
  MAT. API acceptance is not physical HDMI delivery or receiver decoding.

The extension observes complete audio units (AUs) at their known input boundaries
in the MAT packer. It does not search compressed payload for sync-like patterns.
For each major-sync AU, it checks the declared AU length, variable major-header
length, rate and the same major-header CRC as `CAEStreamParser::SyncTrueHD`.
These checks do not validate substream/restart bodies, Evolution protection or
lossless decode. The existing `major_head` remains a structural shortcut only.

- `stage=major` identifies the source `epoch` and `au`. `au_bytes` is the complete
  AU size; `header_bytes` includes the four-byte AU prefix and variable major
  header. Require `structure_valid=true`, `crc_valid=true` and
  `mapping_complete=true` before treating its mapping as validated.
- `p1/o1/n1` and `p2/o2/n2` are exact MAT packet/byte-offset/byte-count spans of
  the **whole AU**, excluding MAT markers and padding. A zero second count means
  one span. They cover splits across the middle marker or a MAT boundary, even
  when the sync word or CRC itself is split. `au` counts all packed source AUs,
  including minors; gaps in major AU numbers are expected.
- `stage=mat_dequeue` confirms the packer's output packet number. Match it with
  `stage=produce` using epoch/packet and size before joining AU spans to downstream
  source ranges. Major events may precede dequeue/produce: they describe copies
  into a MAT that may still be incomplete or subsequently discarded.
- `stage=major_gap reason=padding_reset coverage=stopped` means the packer's
  existing recovery discarded its current unflushed MAT. Invalidate earlier AU
  mappings touching that packet; major coverage stops for the rest of that epoch.
  Existing recovery, payload and codec reset policy are unchanged.

To check header delivery, take the first `header_bytes` source bytes across the
one or two AU spans; to check whole-AU delivery, use both complete spans. Join
each required range to the matching `stage=ae` decision and `stage=sink_api`
result. A muted/dropped first span plus a payload second span is **not** a complete
header or AU delivery. Check actual range coverage and API frame acceptance;
`complete=true` on a replacement pause still submits no original payload. Do not
count missing dequeue/produce/sink records as successful delivery or a proven
drop. Partial sink writes require carrier/source-range accounting, not just a
positive accepted frame count.

`within_window=false` explicitly marks an AU crossing packet 256 into packet 257.
The second packet has no downstream tag, so delivery remains partial/unknown.
Headers and AUs beyond the window are unobserved. At 48 kHz, 256 MAT packets
represent about 5.12 seconds of source audio; queueing, pauses and correction mean
this is not a guaranteed wall-clock interval. A following reset starts a new
epoch. Missing/bad-CRC/gap records must not be converted into a claim that the TV
received a complete usable restart.

For example, `produce packet=1 major_head=true`, followed by the same identity's
`decision=mute` and `kind=replacement_pause payload_api_frames=0`, establishes
that software replaced that initial major-bearing packet. A matching `drop` also
establishes loss of that packet. Neither proves that later payload lacks another
major or that the loss causes recurring crackling/DV notifications. Missing
records alone do not prove a drop: data can be retired before enqueue, a later
flush can purge a queue, or logging may have been disabled.

Logs are bounded by the 256 source packets per reset; fragments have explicit
ranges and inserted pauses are aggregated. The observer validates major-header
CRCs only within that window; there is no steady-state payload scan or fingerprint
registry. Additional records and small side-data copies can
still perturb target timing; source/host checks do not establish neutrality on a
real device. Inspect the actual built artifact before attributing these records
to a running image. Normal release wrappers may reset the separate Kodi build
input; ensure the diagnostic commit is included in that input.

## Source checks

Run these from the source checkout being verified:

```sh
python3 tools/test-truehd-mat-diagnostics.py
python3 tools/test-truehd-delivery.py
python3 tools/check-truehd-delivery.py --self-test
python3 tools/test-activeae-truehd-sync.py --negative-control
python3 tools/test-truehd-restart.py
python3 tools/test-truehd-major-coverage.py
python3 tools/test-truehd-evolution-dialnorm.py --baseline-ref 6218b2ecb9144eada9384f052223df1f4e5c02b8
```

The MAT and delivery tests default both `--baseline-ref` and `--diagnostic-ref`
to the full pre-cleanup hash above. They compare the working production source
against that baseline, then require the instrumented source's diagnostic
assertions and negative controls to pass. For explicit local branch selection:

```sh
python3 tools/test-truehd-mat-diagnostics.py --diagnostic-ref codex/truehd-delivery-diagnostics
python3 tools/test-truehd-delivery.py --diagnostic-ref codex/truehd-delivery-diagnostics
```

A selected diagnostic revision without the required tracing fails; controls are
never skipped merely because the working source is cleaned. The MAT padding
and ALSA offset negative controls always exercise the working production source.
All original functional assertions remain. A later functional source change may
require a deliberate baseline update after reviewing the difference.

Both tools accept `--output` to retain generated fixtures and `--sanitize` for
ASan/UBSan. A host that prevents LeakSanitizer from inspecting threads needs
`ASAN_OPTIONS=detect_leaks=0`; record this limit rather than claiming a leak
check. The MAT fixture already disables leak checking for this reason. The parser
test accepts `FFMPEG_INCLUDE` for the libavutil public headers and repeated
`--media` arguments for complete raw TrueHD streams.

`test-truehd-delivery.py --media /absolute/path/sample.thd --catalog catalog.json`
still generates source-specific MAT/carrier catalogs from cleaned production.
`check-truehd-delivery.py` remains a captured-log analysis tool: it needs logs
from an instrumented build, even though the generator can run against cleaned
source. These host adapters and accepted-write checks do not establish physical
HDMI/eARC audio or timing neutrality.

The historical cleanup is already reflected in this main-source layout. Do not
restore the broad traces merely to get restart records. To compare with those
historical diagnostics, select their pinned revision in the tools above. Panni
handles image builds and reporter coordination; these commands run host source
checks only and do not build, install or test an image on a device.
