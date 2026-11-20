# Numbered-frame HDMI pacing test

Generate original video with a machine-readable unique frame ID, play it in
Kodi, record native HDMI capture samples, then decode their IDs and timestamps.
This observes the complete playback/capture path rather than just decoder
statistics. No OCR, AI, downloaded media or private device configuration is needed.

Run from the repository's `tools` directory. Python 3.10+, NumPy, Pillow,
FFmpeg and ffprobe are required. FFmpeg needs libx264, libx265 and FFV1.
Install the Python dependencies with
`python -m pip install -r frame_pacing/requirements.txt`.
DV generation additionally requires `dovi_tool` on PATH or `--dovi-tool`;
FFmpeg must support the Dolby Vision encoder option and decoded RPU side data.
Missing dependencies fail explicitly, never producing mislabeled HDR10 as DV.

## Generate continuous references

```text
python -m frame_pacing generate frame_pacing/artifacts/sdr --rate 24 --duration 60
python -m frame_pacing generate frame_pacing/artifacts/hdr10 --rate 24000/1001 --duration 60 --width 3840 --height 2160 --format hdr10
python -m frame_pacing generate frame_pacing/artifacts/dv --rate 24000/1001 --duration 60 --width 3840 --height 2160 --format dv
python -m frame_pacing verify frame_pacing/artifacts/hdr10/source.json
```

The default is 60 seconds, 1920x1080 SDR at 24 fps. Use at least 30 seconds for
pacing investigations; prefer 60 seconds. Short clips remain useful unit fixtures.
There are no loops or frame-ID resets. Duration rounds up to a complete frame:
24000/1001 at 60 requested seconds produces 1440 IDs and exactly 60.06 seconds.
Rates such as 24, 25, 50, 30000/1001 and 60000/1001 retain exact rational values.
Generation is bounded to 1-120 fps, 300 seconds, even 640x360 to 3840x2160.

Each new output folder contains the video, manifest/hash, generation commands,
tool versions, decode logs and verification JSON. **Every encoded frame** must
decode to its intended ID/PTS. SDR is H.264 BT.709; HDR10 is 10-bit HEVC with
BT.2020/PQ, mastering metadata and MaxCLL/MaxFALL. DV is profile 8.1 with an
HDR10-compatible base and an RPU on every frame, verified together with actual
MP4 DOVI configuration. It has no EL/FEL or profile 5/7 coverage. Synthetic fixed
CMv2.9 metadata exercises pacing, not certified grading or dynamic tone mapping.
Monochrome HDR markers represent neutral 0..1000-nit values mapped through PQ.

## Capture and play

Capture is currently Windows DirectShow only. Generation/analysis are portable.
Obtain exclusive ownership and release the capture card from OBS or its preview
owner first. No command stops another process, reboots, changes EDID/settings or
controls Kodi. Playback is manual: preserve the current item/time/streams/settings,
start recording **before** opening the clip, allow startup and EOF headroom, then
restore the original state. Do not seek past the first marker or loop the source.

```text
python -m frame_pacing probe-capture --device "Cam Link 4K" --ack-exclusive
python -m frame_pacing capture frame_pacing/artifacts/capture --device "Cam Link 4K" --capture-size 3840x2160 --capture-rate 23.98 --seconds 75 --ack-exclusive
```

Use the **actual enumerated** device, resolution, rate and pixel format, not these
example values blindly. Only that explicit device opens; there is no webcam fallback.
Native NV12 is selected by default. The input pin's default rate is not overridden:
rounded labels can be rejected by DirectShow SetFormat. `--negotiate-rate` requests
an exact supported input rate when needed; it is not output FPS conversion.
The supplied capture rate is a nominal mode declaration/reservation, not calibration.
Capture adds no output `-r`, FPS filter, scaling or CFR duplication/dropping.

Bounded FFV1/NUT recording preserves original PTS with copyts, demux encoder
timebase and passthrough. The native stderr log and JSONL high-resolution
perf_counter_ns/QPC receipts retain driver/demux timestamps and host arrival
relationship. Receipt time is **not exposure or physical HDMI clock**. Warnings,
rate conversion, buffer loss or reader failure cannot be accepted as clean.
Files can be large; conservative free-space reservation is checked first.

For HDR/DV presentation, verify actual decoder, HDMI resolution, EOTF,
colorimetry and HDR/VSI packets separately. A 4K HDR/DV source converted to
1080p SDR is not native 4K HDR/DV presentation. Muted capture colors may still
decode monochrome markers, but neither source tags nor capture colors prove
native wire signaling. This tool does not bypass protection or inject EDIDs.

## Analyze without temporal conversion

```text
python -m frame_pacing analyze frame_pacing/artifacts/capture/capture.nut --manifest frame_pacing/artifacts/hdr10/source.json --output frame_pacing/artifacts/analysis --capture-rate 23.98 --display-rate 24000/1001
```

Output includes JSON report/events/runs, JSONL and CSV observations, exact native
PTS/timebase, sample IDs and decode confidence/errors. Source, display and capture
rates are separate. Never substitute ffprobe's average for a negotiated capture
mode; it can hide drops. Display rate is a nominal assumption, not a wire-clock
measurement. Manifest and capture hashes prevent accidental provenance mismatch.

Repeated IDs are expected when sampling faster than playback: 24-to-60 has normal
2:3 cadence. Equal-rate `1,1,3` is anomalous. Extended holds, backwards IDs and
unexpected jumps remain visible; raw samples are not deduplicated. Unknown/mixed
frames, PTS gaps, insufficient sampling and timestamp/drift uncertainty remain
inconclusive; unknown intervals never imply exact attributed source drops.
First/last-ID startup/EOF holds are retained but not steady-state failures;
an interior last-ID hold is not censored merely because of its ID.

`status=observed_clean` applies only to the observed numbered sequence.
`source_coverage` separately lists unobserved ID ranges; these are not attributed
playback drops. `full_coverage_status` is inconclusive when source IDs are absent,
even with a clean interior. CLI exits: 0 observed-clean/full coverage, 1 anomaly,
2 inconclusive, 3 operational error. No capture proves perfect decoding, display
panel timing, or whether an anomaly originated in Kodi versus capture hardware.
Subcapture hold/exposure timing is not claimed.

## Marker geometry and tests

Three full-frame-relative regions are `[.10,.08,.90,.26]`,
`[.10,.39,.90,.57]`, `[.10,.70,.90,.88]`, each 20x8 cells.
Big-endian sync16 `0xd391`, random clip tag16, frame ID32 and CRC16-CCITT
(seed `0xffff`) form 80 bits. Each MSB-first bit is followed by its complement.
All three copies must pass contrast/ambiguity/complement/sync/CRC/tag checks
and agree. Mixed/torn or corrupt cells yield null IDs, never guessed counters.
Human-readable labels are supplemental, not the oracle.

```text
python -m unittest frame_pacing.test_frame_pacing frame_pacing.test_capture -v
```

Tests are offline: real encoded source/HDR/DV roundtrips, lossy resizing,
intentional repeats/skips/reordering/unknowns, rational cadence, PTS gaps,
partial coverage, marker corruption and mocked capture ownership/failure/QPC.
The optional DV test explicitly skips when its tool is unavailable.
No tests open DirectShow, SSH, RPC, preview, audio or hardware.

## Hardware observations and limits

Continuous 60.06-second references were exercised through native HDMI capture.
Native 3840x2160 HDR10 was independently confirmed with incoming checksum-valid
PQ/BT.2020 signaling and no metadata overrides. Its 1800 native samples decoded
1437/1440 IDs, with no interior unknown/ordering anomalies; startup IDs 1..3
were unobserved, so **full pacing acceptance was inconclusive**. A native 4K
SDR-output trial decoded 1435/1440 IDs with the same limitation.
DV source pacing was also observed through converted SDR output, not native DV
presentation. Physical HDMI clock calibration and HDR panel color/brightness
remain unmeasured. Raw captures/device configuration/private media are not included.
