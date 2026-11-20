"""Sampling-aware evidence, not an attribution of failures to a playback device."""
import csv
import json
import math
import pathlib
from collections import Counter
from fractions import Fraction

from .media import load_manifest, observations, probe, rational, sha256, write_json


LIMITS = [
    "IDs describe captured samples, not every HDMI presentation or panel refresh.",
    "Anomalies cannot distinguish Kodi, HDMI, capture hardware/driver, or capture-host faults.",
    "Exact repeated images may be legitimate holds or capture duplicates; they are not attributable.",
    "Source and capture clocks may drift; nominal rates do not prove clock lock.",
    "Hold estimates are quantized by capture sampling; bounds require valid adjacent samples.",
    "No statement about source IDs before/after observed coverage; no exact skips across unknown intervals.",
]


def assess(rows, source_rate, capture_rate, timestamp_tolerance=Fraction(1, 1000),
           display_rate=None, frame_count=None):
    """Analyze the raw observations without deduplicating the pacing evidence."""
    source_rate, capture_rate = rational(source_rate), rational(capture_rate)
    tolerance = Fraction(timestamp_tolerance)
    if tolerance < 0:
        raise ValueError("Timestamp tolerance cannot be negative.")
    source_period, capture_period = 1 / source_rate, 1 / capture_rate
    display_rate = rational(display_rate) if display_rate is not None else None
    maximum_nominal_hold = (Fraction(math.ceil(display_rate / source_rate), 1) / display_rate
                            if display_rate else source_period + capture_period)
    valid_indices = [i for i, row in enumerate(rows) if row["id"] is not None]
    first, last = (valid_indices[0], valid_indices[-1]) if valid_indices else (None, None)
    source_boundary_samples = set()
    if first is not None and rows[first]["id"] == 1:
        for i in range(first, len(rows)):
            if rows[i]["id"] != 1:
                break
            source_boundary_samples.add(i)
    if last is not None and frame_count is not None and rows[last]["id"] == frame_count:
        for i in range(last, -1, -1):
            if rows[i]["id"] != frame_count:
                break
            source_boundary_samples.add(i)
    events, runs = [], []
    gaps = set()
    seen_time_bases = set()

    def event(kind, severity, index, **detail):
        events.append({"kind": kind, "severity": severity, "sample": index,
                       "pts": rows[index].get("pts"), "time": rows[index].get("time"),
                       **detail})

    for i, row in enumerate(rows):
        row["segment"] = None
        if row["time_base"] not in seen_time_bases:
            seen_time_bases.add(row["time_base"])
            if Fraction(row["time_base"]) >= capture_period / 2:
                event("coarse_timestamp_timebase", "uncertainty", i,
                      time_base=row["time_base"],
                      explanation="Timestamp quantization is too coarse for fine cadence conclusions.")
        inside = first is not None and first <= i <= last
        if row["id"] is None:
            event("unknown_marker", "uncertainty" if inside else "outside_coverage", i,
                  decode_status=row.get("decode_status"))
        if row.get("time") is None:
            gaps.add(i)
            event("missing_pts", "uncertainty", i)
        if i and row.get("time") is not None and rows[i - 1].get("time") is not None:
            dt = Fraction(row["time"]) - Fraction(rows[i - 1]["time"])
            tick = Fraction(row["time_base"])
            if dt <= 0 or dt > capture_period * Fraction(3, 2) + tick * 2 + tolerance * 2:
                gaps.add(i)
                event("pts_nonmonotonic" if dt <= 0 else "pts_gap", "uncertainty", i,
                      previous_sample=i - 1, interval=str(dt))

    active = None
    segment = -1
    phase_low = phase_high = None
    previous = None
    for i, row in enumerate(rows):
        if row["id"] is None or row.get("time") is None:
            previous, active = None, None
            phase_low = phase_high = None
            continue
        t, frame_id = Fraction(row["time"]), row["id"]
        continuous = previous is not None and i not in gaps
        if not continuous:
            segment += 1
            active = None
            phase_low = phase_high = None
        row["segment"] = segment
        if continuous:
            before = rows[previous]
            delta = frame_id - before["id"]
            detail = {"previous_sample": previous, "previous_id": before["id"], "id": frame_id}
            if delta < 0:
                event("backwards_id", "anomaly", i, **detail)
            elif delta > 1:
                if display_rate is not None and display_rate < source_rate:
                    event("presentation_rate_limit", "sampling_limit", i,
                          count=delta - 1, **detail)
                elif capture_rate >= source_rate:
                    event("unobserved_ids_between_adjacent_samples", "anomaly", i,
                          first_unobserved=before["id"] + 1, last_unobserved=frame_id - 1,
                          count=delta - 1, **detail)
                else:
                    dt = t - Fraction(before["time"])
                    severity = ("anomaly" if delta > math.ceil(dt * source_rate) + 1
                                else "sampling_limit")
                    event("forward_jump_under_sampling", severity, i, count=delta - 1, **detail)
        if active is not None and active["id"] == frame_id:
            active["last_sample"] = i
            active["samples"] += 1
        else:
            active = {"id": frame_id, "segment": segment, "first_sample": i,
                      "last_sample": i, "samples": 1}
            runs.append(active)
        if i in source_boundary_samples:
            phase_low = phase_high = None
            previous = i
            continue
        # A constant source clock must fit every sample's source-frame interval.
        tick = Fraction(row["time_base"])
        error = tolerance + tick / 2
        # Source transition may wait up to one HDMI refresh before scanout.
        # Unknown HDMI cadence uses one capture period as an explicitly assumed envelope.
        presentation_quantum = 1 / display_rate if display_rate else capture_period
        low = t - frame_id * source_period - error - presentation_quantum
        high = t - (frame_id - 1) * source_period + error
        phase_low = low if phase_low is None else max(low, phase_low)
        phase_high = high if phase_high is None else min(high, phase_high)
        if phase_low >= phase_high:
            event("nominal_cadence_phase_break", "uncertainty", i,
                  explanation="No nominal source phase fits the assumed presentation envelope; pacing, HDMI quantization or sample-clock mismatch.",
                  excess=str(phase_low - phase_high), tolerance=str(tolerance))
            phase_low, phase_high = low, high
        previous = i

    for run in runs:
        a, b = run["first_sample"], run["last_sample"]
        span = Fraction(rows[b]["time"]) - Fraction(rows[a]["time"])
        left = a > 0 and rows[a - 1]["segment"] == run["segment"]
        right = b + 1 < len(rows) and rows[b + 1]["segment"] == run["segment"]
        run.update({
            "first_pts": rows[a]["pts"], "last_pts": rows[b]["pts"],
            "time_base": rows[a]["time_base"], "observed_span": str(span),
            "nominal_sampled_hold": str(run["samples"] * capture_period),
            "hold_lower_bound": str(max(Fraction(0), span - tolerance * 2 -
                                       Fraction(rows[a]["time_base"]))),
            "hold_upper_bound": (str(Fraction(rows[b + 1]["time"]) -
                                     Fraction(rows[a - 1]["time"]) + tolerance * 2 +
                                     Fraction(rows[a]["time_base"]))
                                 if left and right else None),
            "boundary_censored": not (left and right),
            "source_boundary_censored": a in source_boundary_samples,
        })
        if run["source_boundary_censored"]:
            if run["samples"] > 1:
                event("source_boundary_hold_censored", "outside_coverage", a, id=run["id"],
                      last_sample=b, samples=run["samples"], observed_span=str(span),
                      explanation="Source's first/last ID may be held during startup or after EOF; retained, not scored as steady-state pacing.")
        elif span > maximum_nominal_hold + tolerance * 2 + Fraction(rows[a]["time_base"]) * 2:
            event("extended_identical_id_hold", "anomaly", a, id=run["id"],
                  last_sample=b, samples=run["samples"], observed_span=str(span),
                  nominal_source_period=str(source_period),
                  maximum_nominal_hold_envelope=str(maximum_nominal_hold))
        elif (left and right and run["samples"] >
              math.ceil(capture_rate * (maximum_nominal_hold if display_rate else source_period))):
            event("excess_repeat_candidate", "uncertainty", a, id=run["id"],
                  samples=run["samples"],
                  explanation="Beyond nominal repeat count; HDMI quantization, boundary phase/jitter or clock drift may explain this.")

    counts = Counter(e["severity"] for e in events)
    sufficient = capture_rate >= source_rate and (display_rate is None or display_rate >= source_rate)
    distinct = len({row["id"] for row in rows if row["id"] is not None})
    status = ("fail" if counts["anomaly"] else
              "inconclusive" if counts["uncertainty"] or not sufficient or distinct < 3
              else "observed_clean")
    limits = list(LIMITS)
    if not sufficient:
        limits.append("Capture or known HDMI rate is below source rate: absent IDs and short holds cannot be resolved.")
    if capture_rate <= source_rate:
        limits.append("Capture at/below source rate cannot reliably separate brief errors from sample-phase/clock slip.")
    if display_rate is None:
        limits.append("HDMI presentation rate is unknown. Hold checks use source period + capture period as a conservative assumed envelope, not measured display times.")
    else:
        limits.append("HDMI rate is supplied, not measured; nominal source cadence is quantized to HDMI refreshes and then sampled by capture.")
    intervals = [Fraction(rows[i]["time"]) - Fraction(rows[i - 1]["time"])
                 for i in range(1, len(rows))
                 if rows[i]["time"] is not None and rows[i - 1]["time"] is not None]
    timestamp_rate = ((len(rows) - 1) / sum(intervals, Fraction(0))
                      if len(intervals) == len(rows) - 1 and intervals and min(intervals) > 0 else None)
    unknown_intervals = []
    unknown = None
    for i, row in enumerate(rows):
        if row["id"] is not None:
            unknown = None
            continue
        if unknown is None:
            unknown = {
                "first_sample": i, "last_sample": i, "samples": 0,
                "first_pts": row.get("pts"), "last_pts": row.get("pts"),
                "time_base": row["time_base"],
                "coverage": ("no_marked_coverage" if first is None else
                             "head" if i < first else "tail" if i > last else "interior"),
                "decode_status_counts": {}, "marker_copy_error_counts": {},
            }
            unknown_intervals.append(unknown)
        unknown["last_sample"], unknown["last_pts"] = i, row.get("pts")
        unknown["samples"] += 1
        reasons = unknown["decode_status_counts"]
        reason = row.get("decode_status", "unspecified")
        reasons[reason] = reasons.get(reason, 0) + 1
        for copy in row.get("copies", []):
            if "error" in copy:
                errors = unknown["marker_copy_error_counts"]
                errors[copy["error"]] = errors.get(copy["error"], 0) + 1
    return {
        "schema": "frame-pacing-analysis-v1", "status": status,
        "meaning": {"fail": "Observed sequence/hold anomaly, not a proven playback-device fault.",
                    "inconclusive": "Sampling, coverage, timestamps or marker uncertainty prevents a clean observation.",
                    "observed_clean": "No anomaly observed within covered samples; not proof of perfect playback."}[status],
        "source_rate": str(source_rate), "capture_rate_assumption": str(capture_rate),
        "display_rate_assumption": str(display_rate) if display_rate else None,
        "source_period": str(source_period), "capture_period": str(capture_period),
        "maximum_nominal_hold_envelope": str(maximum_nominal_hold),
        "observed_pts_average_rate": str(timestamp_rate) if timestamp_rate else None,
        "observed_pts_rate_residual_ppm": (str((timestamp_rate / capture_rate - 1) * 1000000)
                                           if timestamp_rate else None),
        "timestamp_tolerance_seconds": str(tolerance),
        "sample_count": len(rows), "valid_samples": len(valid_indices),
        "distinct_ids": distinct, "coverage_first_sample": first, "coverage_last_sample": last,
        "first_observed_id": rows[first]["id"] if first is not None else None,
        "last_observed_id": rows[last]["id"] if last is not None else None,
        "repeat_samples": sum(run["samples"] - 1 for run in runs),
        "run_samples_histogram": dict(sorted(Counter(run["samples"] for run in runs).items())),
        "sample_interval_histogram_seconds": dict(Counter(str(value) for value in intervals)),
        "sampled_hold_histogram_seconds": dict(Counter(run["nominal_sampled_hold"] for run in runs)),
        "event_counts": dict(Counter(e["kind"] for e in events)),
        "unknown_marker_intervals": unknown_intervals,
        "severity_counts": dict(counts), "runs": runs, "events": events, "limits": limits,
    }


def analyze(video, manifest_path, output, capture_rate=None, tolerance_ms="1",
            display_rate=None):
    video, output = pathlib.Path(video), pathlib.Path(output)
    manifest = load_manifest(manifest_path)
    output.mkdir(parents=True, exist_ok=False)
    info = probe(video)
    stream = info["streams"][0]
    # A container's average rate can hide capture drops. Require a negotiated rate,
    # or the capture sidecar's explicit negotiation, never infer from frame count.
    sidecar_path = video.parent / "capture.json"
    sidecar = json.loads(sidecar_path.read_text()) if sidecar_path.exists() else None
    if capture_rate is None:
        if not sidecar or sidecar.get("video") != video.name:
            raise ValueError("Supply --capture-rate (the negotiated native sampling rate), not an inferred average.")
        capture_rate = sidecar["capture_rate"]
    if sidecar and sidecar.get("video") != video.name:
        raise ValueError("capture.json refers to a different video; use an isolated evidence folder.")
    digest = sha256(video)
    if sidecar and sidecar.get("sha256") and sidecar["sha256"] != digest:
        raise ValueError("Capture hash does not match capture.json.")
    rows = list(observations(video, manifest, output / "decode-ffmpeg.log", info))
    report = assess(rows, manifest["rate"], capture_rate, rational(tolerance_ms) / 1000
                    if Fraction(tolerance_ms) else Fraction(0), display_rate,
                    manifest["frame_count"])
    report.update({
        "video": str(video.resolve()), "video_sha256": digest,
        "implementation_sha256": {name: sha256(pathlib.Path(__file__).with_name(name))
                                  for name in ("analysis.py", "marker.py", "media.py")},
        "source_manifest": manifest, "input_stream": stream,
        "capture_provenance": sidecar,
        "provenance": ("generated_source_roundtrip_not_HDMI" if digest == manifest["sha256"]
                       else "analyzed_input_not_authenticated_as_HDMI"),
    })
    diagnostics = (output / "decode-ffmpeg.log").read_text(errors="replace").strip()
    report["decoder_diagnostics_present"] = bool(diagnostics)
    if diagnostics:
        report["status"] = "inconclusive" if report["status"] != "fail" else "fail"
        report["meaning"] = "Decoder diagnostics require review; inspect decode-ffmpeg.log before interpreting pacing evidence."
        report["limits"].append("FFmpeg emitted decode/conversion diagnostics; they have not been treated as a clean decode.")
    if sidecar and sidecar.get("status") != "recorded_not_yet_analyzed":
        report["status"] = "inconclusive" if report["status"] != "fail" else "fail"
        report["meaning"] = "Capture provenance reports failure/warnings; inspect capture.json and capture-ffmpeg.log. Sequence anomalies, if any, are not attributed to playback."
        report["limits"].append("Capture process did not complete without known warnings.")
    ids = sorted({row["id"] for row in rows if row["id"] is not None})
    unobserved = []
    previous = 0
    for value in ids:
        if value > previous + 1:
            unobserved.append([previous + 1, value - 1])
        previous = value
    if previous < manifest["frame_count"]:
        unobserved.append([previous + 1, manifest["frame_count"]])
    report["source_coverage"] = {
        "expected_ids": manifest["frame_count"], "observed_ids": len(ids),
        "unobserved_id_ranges": unobserved, "all_source_ids_observed": not unobserved,
        "meaning": "Unobserved IDs are not attributed drops; unknown/startup/tail intervals retain uncertainty.",
    }
    report["full_coverage_status"] = ("inconclusive" if unobserved and report["status"] == "observed_clean"
                                    else report["status"])
    with (output / "observations.jsonl").open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row) + "\n")
    fields = ["sample", "pts", "time_base", "time", "best_effort_timestamp",
              "pkt_dts", "duration", "id", "decode_status", "segment", "image_sha256"]
    with (output / "observations.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    write_json(output / "analysis.json", report)
    return report


def summary(report):
    return (f"{report['status']}: {report['valid_samples']}/{report['sample_count']} valid samples, "
            f"{report['distinct_ids']} IDs; repeats={report['repeat_samples']}; "
            f"events={json.dumps(report['event_counts'], sort_keys=True)}\n{report['meaning']}\n"
            f"Full-source coverage: {report['full_coverage_status']}; "
            f"unobserved ID ranges={report['source_coverage']['unobserved_id_ranges']}")
