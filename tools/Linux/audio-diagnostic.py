#!/usr/bin/env python3
"""Bounded p3i audio capture. No playback, settings, services or boot changes.

Enable the startup marker, restart Kodi normally, then collect each listening
case separately. Never includes the general Kodi log, settings or media paths.
"""

import argparse
import datetime
import errno
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import stat
import sys
import time
import zipfile

TEMP = Path("/storage/.kodi/temp")
LIMIT = 512 * 1024
MAX_DIAGNOSTIC = 8 * 1024 * 1024
AUDIO = re.compile(r"audio|alsa|snd_|hdmi|tdm|frddr|i2s|fifo|xrun", re.I)
SYSTEM_ROOT = Path("/")


def support_module():
    path = Path(__file__).with_name("audio-diagnostic-system.py")
    if not path.is_file():
        raise RuntimeError("Place audio-diagnostic-system.py beside audio-diagnostic.py before collecting")
    spec = importlib.util.spec_from_file_location("audio_diagnostic_system", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def redact(text):
    text = re.sub(r"\b(?:https?|smb|nfs|plex)://[^\s]+", "[URL]", text, flags=re.I)
    text = re.sub(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", "[IP]", text)
    return re.sub(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", "[MAC]", text, flags=re.I)


def kernel_event(line):
    """Export event categories and approved numbers, never arbitrary driver text."""
    header = re.match(r"^\d{1,3},(\d{1,20}),(\d{1,20}),[^;]*;(.*)", line)
    message = header[3] if header else line.splitlines()[0] if line else ""
    if not AUDIO.search(message):
        return None
    lower = message.lower()
    kind = "unclassified_audio_event"
    if "fifo" in lower:
        match = re.search(r"\b(underrun|underflow|overflow|empty|full)\b", lower)
        kind = "fifo_" + match[1] if match else "fifo_event"
    elif re.search(r"\b(underrun|underflow|xrun)\b", lower):
        kind = "underrun_or_xrun"
    elif "hdmi" in lower:
        if "hpd" in lower or "hotplug" in lower:
            kind = "hdmi_hotplug"
        elif "mute" in lower:
            kind = "hdmi_mute"
        elif "audio" in lower:
            kind = "hdmi_audio_configuration"
        else:
            kind = "hdmi_event"
    elif re.search(r"\b(failed|failure|error)\b", lower):
        kind = "audio_error"
    elif "tdm" in lower or "frddr" in lower:
        kind = "tdm_or_frddr_event"
    event = {"kind": kind, "received_mono_us": time.monotonic_ns() // 1000,
             "source_header_available": header is not None}
    if header:
        event.update(kernel_sequence=int(header[1]), kernel_timestamp_us=int(header[2]))
    # Existing vendor setup messages only: no raw text or extra driver tracing.
    n_value = re.fullmatch(r"hdmitx:\s*hw:\s*aud_n_para = (\d{1,7})\s*", lower)
    selection = re.fullmatch(r"hdmitx: fs = (\d{1,2}), cd = (\d{1,2}), tmds_clk = (\d{1,7})\s*", lower)
    source = re.fullmatch(r"hdmitx:\s*hw:\s*hdmitx tx_aud_src = ([01]), audio_param->channel_num = (\d{1,2}), hdev->aud_output_ch = (\d{1,3})\s*", lower)
    if n_value and 0 < int(n_value[1]) < 1048576:
        event.update(kind="hdmi_acr_n", n=int(n_value[1]))
    elif selection:
        event.update(kind="hdmi_acr_selection", fs_enum=int(selection[1]),
                     depth_enum=int(selection[2]), lookup_clock_khz=int(selection[3]))
    elif source:
        event.update(kind="hdmi_audio_source", tx_aud_src=int(source[1]),
                     channel_num_enum=int(source[2]), forced_output_channels=int(source[3]))
    rate = re.search(r"\b(?:sample[_ -]?rate|rate|fs)\s*[:=]\s*(\d{4,6})\b", lower)
    if rate and int(rate[1]) in (8000, 11025, 16000, 22050, 24000, 32000, 44100,
                               48000, 88200, 96000, 176400, 192000, 352800, 384000):
        event["rate_hz"] = int(rate[1])
    error = re.search(r"\b(?:errno|err|error|ret|result)\s*[:=]\s*(-\d{1,4})\b", lower)
    if error and -4095 <= int(error[1]) < 0:
        event["errno"] = int(error[1])
    return event


class MarkInput:
    """Fixed-size partial-line state; no readline() or unbounded input buffering."""
    def __init__(self):
        self.buffer = bytearray()
        self.discard = False
        self.bytes = 0

    def feed(self, data):
        self.bytes += len(data)
        result = []
        for value in data:
            if value == 10:
                if not self.discard:
                    item = mark(self.buffer.decode("ascii", "replace"))
                    if item is not None:
                        result.append(item)
                self.buffer.clear()
                self.discard = False
            elif len(self.buffer) < 80 and not self.discard:
                self.buffer.append(value)
            else:
                self.buffer.clear()
                self.discard = True
        return result


def read_node(path, limit=4096, owned=False):
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        if owned:
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", buffering=0) as stream:
            info = os.fstat(stream.fileno())
            if owned and not stat.S_ISREG(info.st_mode):
                return {"available": False, "reason": "not_regular"}
            data = stream.read(limit + 1)
        if data is None:
            return {"available": False, "reason": "would_block"}
        if len(data) > limit:
            return {"available": False, "reason": "size_limit"}
        result = {"available": True, "text": redact(data.decode("utf-8", "replace"))}
        if owned:
            result["file_identity"] = {"device": info.st_dev, "inode": info.st_ino}
        return result
    except OSError as error:
        return {"available": False, "errno": error.errno}


def configuration(root=Path("/")):
    # Do not read PCM status: its ALSA getter resets avail_max/overrange.
    files = sorted(glob.glob(str(root / "proc/asound/card*/pcm*p/sub*/hw_params")))
    files += sorted(glob.glob(str(root / "proc/asound/card*/pcm*p/sub*/sw_params")))
    files += [str(root / path) for path in (
        "sys/class/amhdmitx/amhdmitx0/config", "sys/class/amhdmitx/amhdmitx0/avmute",
        "sys/class/display/mode", "sys/class/amhdmitx/amhdmitx0/frac_rate_policy",
    )]
    result = {"mono_us": time.monotonic_ns() // 1000, "wall_unix_ms": time.time_ns() // 1000000,
              "pcm_nodes_found": len(files) - 4, "nodes_truncated": len(files) > 35,
              "nodes": {str(Path(path).relative_to(root)): read_node(path) for path in files[:35]}}
    clocks = read_node(root / "sys/kernel/debug/clk/clk_summary", 256 * 1024)
    if clocks.get("available"):
        clocks["text"] = "\n".join(line for line in clocks["text"].splitlines()
                                      if re.search(r"\b(mpll2|mclk_c|tdm\w*|audio\w*)\b", line, re.I))
        if not clocks["text"]:
            clocks = {"available": False, "reason": "no_matching_audio_clocks"}
    result["audio_clocks"] = clocks
    return result


class BoundedFile:
    def __init__(self, path, limit=LIMIT):
        self.stream = path.open("x", encoding="utf-8")
        os.chmod(path, 0o600)
        self.limit = limit
        self.bytes = 0
        self.truncated = False
        self.first_lost_mono_us = None

    def write(self, text):
        data = text.encode("utf-8")
        if self.truncated:
            return False
        if self.bytes + len(data) > self.limit - 128:
            self.stream.write("\nTRUNCATED: output budget reached; remaining observations unavailable\n")
            self.truncated = True
            self.first_lost_mono_us = time.monotonic_ns() // 1000
            return False
        self.stream.write(text)
        self.stream.flush()
        self.bytes += len(data)
        return True

    def close(self):
        self.stream.close()


def mark(line):
    match = re.fullmatch(r"\s*(\d{1,4}(?:[.:]\d{1,3})?)?(?:\s+(yes|no|unknown))?\s*", line)
    if not match:
        return None
    return {"mono_us": time.monotonic_ns() // 1000, "wall_unix_ms": time.time_ns() // 1000000,
            "track_position": match[1] or "unknown", "automatic_recovery": match[2] or "unknown"}


def diagnostic_coverage(text, start_us, end_us, wall_start_ms):
    """Temporal coverage only; current windows do not establish healthy audio.

    Allow two seconds of worker flush lag at the end, and expose the actual
    lag. Wall/monotonic anchors conservatively reject old-boot/changed-clock
    ambiguity; an unknown clock relationship must never look current.
    """
    result = {"coverage": "unavailable", "reason": "invalid_header",
              "recording_complete": False, "worker_lag_allowance_us": 2000000}
    header = re.match(r"AUDIO_DIAG schema=1 clock=CLOCK_MONOTONIC mono_us=(\d{1,20}) "
                      r"wall_unix_ms=(\d{1,20}) duration_s=(\d{1,4}) "
                      r"capture_start_us=(\d{1,20}) max_bytes=(\d{1,20})\n", text)
    if not header or end_us < start_us:
        return result
    anchor, wall, seconds, trace_start, _ = map(int, header.groups())
    result.update(trace_start_us=trace_start, configured_deadline_us=trace_start + seconds * 1000000)
    delta = wall_start_ms - (wall + (start_us - anchor) // 1000)
    result["wall_anchor_difference_ms"] = delta
    if abs(delta) > 5000:
        result["reason"] = "clock_relationship_unverified"
        return result
    windows = []
    malformed = False
    for line in text.splitlines():
        if result["recording_complete"] and line:
            malformed = True
        if line.startswith("WINDOW "):
            match = re.fullmatch(r"WINDOW seq=(\d{1,20}) start_us=(\d{1,20}) "
                                 r"end_us=(\d{1,20}) dropped=(\d{1,20})"
                                 r"(?: event_lost=(\d{1,20}) event_overflow=(\d{1,20}))?", line)
            if match:
                seq, first, last, dropped = map(int, match.groups()[:4])
                if trace_start <= first < last <= end_us + 2000000:
                    windows.append((seq, first, last, dropped))
                    if last >= start_us and first <= end_us and match[5] is not None:
                        result["event_lost"] = result.get("event_lost", 0) + int(match[5])
                        result["event_overflow"] = result.get("event_overflow", 0) + int(match[6])
                else:
                    malformed = True
            else:
                malformed = True
        elif line.startswith("END "):
            match = re.fullmatch(r"END mono_us=(\d{1,20}) reason=([a-z_]{1,32})", line)
            if match:
                result.update(recording_complete=True, recording_end_us=int(match[1]),
                              recording_end_reason=match[2])
            else:
                malformed = True
    result["window_count"] = len(windows)
    if malformed or any(b[0] != a[0] + 1 or b[1] != a[2] for a, b in zip(windows, windows[1:])):
        result["reason"] = "invalid_or_discontinuous_windows"
        return result
    if not windows:
        result["reason"] = "no_windows"
        return result
    first, last = windows[0][1], windows[-1][2]
    if result.get("recording_end_us", last) < last:
        result["reason"] = "footer_precedes_windows"
        return result
    result.update(first_window_start_us=first, last_window_end_us=last,
                  end_lag_us=max(0, end_us - last),
                  overlapping_dropped_submissions=sum(w[3] for w in windows
                                                     if w[2] >= start_us and w[1] <= end_us))
    preflight_recent = start_us == end_us and last >= start_us - result["worker_lag_allowance_us"]
    if (last < start_us and not preflight_recent) or first > end_us:
        result.update(coverage="none", reason="no_current_windows")
    elif first <= start_us and last >= end_us - result["worker_lag_allowance_us"]:
        result.update(coverage="current", reason="windows_cover_collection_with_worker_lag")
    else:
        result.update(coverage="partial", reason="windows_do_not_cover_collection")
    if result.get("recording_end_us", end_us) < start_us:
        result.update(coverage="none", reason="recording_ended_before_collection")
    elif result.get("recording_end_us", end_us) < end_us:
        result.update(coverage="partial", reason="recording_ended_during_collection")
    return result


def trace_prefix(text):
    lines = text.splitlines()
    selected = [line for line in lines[:8] if line.startswith(("AUDIO_DIAG ", "IDENTITY "))]
    return hashlib.sha256("\n".join(selected).encode("ascii", "replace")).hexdigest()


def trace_lifetime(text, before, after):
    match = re.search(r"^IDENTITY boot_id=([0-9a-f-]{36}) pid=(\d{1,10}) start_ticks=(\d{1,20})$", text, re.M)
    if not match:
        return {"verified": False, "reason": "logger_identity_unavailable"}
    boot, pid, ticks = match[1], int(match[2]), int(match[3])
    for item in (before, after):
        if not item.get("boot_id", {}).get("available") or not item.get("kodi", {}).get("available"):
            return {"verified": False, "reason": "current_identity_unavailable"}
        if (item["boot_id"]["value"], item["kodi"]["pid"], item["kodi"]["start_ticks"]) != (boot, pid, ticks):
            return {"verified": False, "reason": "boot_or_kodi_lifetime_changed"}
    return {"verified": True, "boot_id": boot, "pid": pid, "start_ticks": ticks}


def collect(label, seconds):
    support = support_module()
    system_sampler = support.SystemSampler(SYSTEM_ROOT)
    driver_capture = support.KernelCapture(SYSTEM_ROOT)
    start_us = time.monotonic_ns() // 1000
    wall_start_ms = time.time_ns() // 1000000
    TEMP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = TEMP / f"audio-capture-{label}-{stamp}-{os.getpid()}"
    directory.mkdir(mode=0o700)
    config = BoundedFile(directory / "configuration.jsonl")
    kernel = BoundedFile(directory / "kernel-audio.log")
    system_log = BoundedFile(directory / "system.jsonl", 8 * 1024 * 1024)
    driver_log = BoundedFile(directory / "driver.jsonl", 8 * 1024 * 1024)
    metadata = {"label": label, "seconds": seconds, "kernel_release": os.uname().release,
                "release": read_node("/etc/release"), "kmsg": {"available": False},
                "clock": "CLOCK_MONOTONIC", "marks": [],
                "collection_start_us": start_us, "collection_wall_start_ms": wall_start_ms}
    initial = read_node(TEMP / "audio-diagnostic.log", MAX_DIAGNOSTIC, owned=True)
    preflight = diagnostic_coverage(initial["text"], start_us, start_us, wall_start_ms) if initial.get("available") else {
        "coverage": "unavailable", "reason": "trace_unavailable"}
    metadata["diagnostic_preflight"] = preflight
    metadata["identity_start"] = support.identity(SYSTEM_ROOT)
    if initial.get("available"):
        preflight["lifetime"] = trace_lifetime(initial["text"], metadata["identity_start"], metadata["identity_start"])
        if preflight["coverage"] == "current" and not preflight["lifetime"]["verified"]:
            preflight.update(coverage="unavailable", reason=preflight["lifetime"]["reason"])
    selector = selectors.DefaultSelector()
    kmsg = None
    try:
        kmsg = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        os.lseek(kmsg, 0, os.SEEK_END)  # Per-reader cursor only; never clear kernel logs.
        selector.register(kmsg, selectors.EVENT_READ, "kernel")
        metadata["kmsg"] = {"available": True, "scope": "new events during collection"}
    except OSError as error:
        metadata["kmsg"] = {"available": False, "errno": error.errno}
        if kmsg is not None:
            os.close(kmsg)
        kmsg = None
    stdin_fd, stdin_blocking = None, None
    marks = MarkInput()
    try:
        stdin_fd = sys.stdin.fileno()
        stdin_blocking = os.get_blocking(stdin_fd)
        os.set_blocking(stdin_fd, False)
        selector.register(stdin_fd, selectors.EVENT_READ, "mark")
        metadata["mark_input"] = {"available": True}
    except (OSError, ValueError):
        metadata["mark_input"] = {"available": False}
    deadline = time.monotonic() + seconds
    next_config = 0.0
    next_system = 0.0
    kernel_reads = 0
    kernel_last_sequence = None
    kernel_counts = {"records": 0, "retained": 0, "filtered": 0, "sequence_gaps": 0,
                     "epipe": 0, "invalid_headers": 0, "sequence_resets": 0}
    try:
        print("Kodi diagnostic preflight: " + preflight["coverage"] + " (" + preflight["reason"] + ")", flush=True)
        if preflight["coverage"] != "current":
            print("WARNING: no verified fresh Kodi coverage. Enable/restart Kodi for a valid comparison; this command does not restart it.", flush=True)
        elif preflight.get("configured_deadline_us", 0) < start_us + seconds * 1000000:
            print("WARNING: Kodi diagnostic budget expires during this collection. Enable/restart Kodi before the comparison.", flush=True)
        print(f"Preparing {label} collection for {seconds}s.", flush=True)
        print("At a dropout press Enter, or enter e.g. '55 yes' (position, automatic recovery yes/no/unknown).", flush=True)
        metadata["driver_start"] = driver_capture.start(max(1, int(seconds)))
        print("Driver diagnostic window: " + str(metadata["driver_start"].get("reason", "unavailable")), flush=True)
        if not metadata["driver_start"].get("available"):
            print("WARNING: driver diagnostic window is unverified; driver observations may be incomplete.", flush=True)
        system_log.write(json.dumps(system_sampler.sample(), separators=(",", ":")) + "\n")
        driver_log.write(json.dumps({"mono_us": time.monotonic_ns() // 1000, "phase": "baseline",
                                    "observations": driver_capture.sample()}, separators=(",", ":")) + "\n")
        next_system = time.monotonic() + 1
        print("Collection ready. Start the SAME track now.", flush=True)
        while time.monotonic() < deadline:
            if time.monotonic() >= next_system:
                if not system_log.truncated:
                    system_log.write(json.dumps(system_sampler.sample(), separators=(",", ":")) + "\n")
                if not driver_log.truncated:
                    driver_log.write(json.dumps({"mono_us": time.monotonic_ns() // 1000,
                                                 "observations": driver_capture.sample()}, separators=(",", ":")) + "\n")
                next_system = time.monotonic() + 1
            if time.monotonic() >= next_config and not config.truncated:
                sample = configuration()
                config.write(json.dumps(sample, separators=(",", ":")) + "\n")
                rates = sorted({int(value) for node in sample["nodes"].values()
                                for value in re.findall(r"^rate:\s*(\d+)", node.get("text", ""), re.M)})
                print("Observed ALSA rates: " + (str(rates) if rates else "unavailable/closed"), flush=True)
                next_config = time.monotonic() + 5
            for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                if key.data == "mark":
                    try:
                        data = os.read(stdin_fd, 512)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        metadata["mark_input"].update(available=False, errno=error.errno)
                        selector.unregister(stdin_fd)
                        continue
                    if not data:
                        metadata["mark_input"]["eof"] = True
                        selector.unregister(stdin_fd)
                        continue
                    for item in marks.feed(data):
                        if len(metadata["marks"]) < 32:
                            metadata["marks"].append(item)
                            print("Dropout mark: " + json.dumps(item), flush=True)
                    if marks.bytes >= 4096:
                        selector.unregister(stdin_fd)
                        metadata["mark_input"]["read_limit_reached"] = True
                else:
                    # One bounded read per wakeup. No producer tracing or new driver flags.
                    try:
                        kernel_reads += 1
                        line = os.read(kmsg, 8192).decode("utf-8", "replace")
                        kernel_counts["records"] += bool(line)
                        sequence = re.match(r"^\d{1,3},(\d{1,20}),", line)
                        if sequence:
                            value = int(sequence[1])
                            if kernel_last_sequence is not None:
                                if value > kernel_last_sequence:
                                    kernel_counts["sequence_gaps"] += value - kernel_last_sequence - 1
                                else:
                                    kernel_counts["sequence_resets"] += 1
                            kernel_last_sequence = value
                        elif line:
                            kernel_counts["invalid_headers"] += 1
                        event = kernel_event(line)
                        if event is not None:
                            saved = kernel.write(json.dumps(event, separators=(",", ":")) + "\n")
                            kernel_counts["retained"] += bool(saved)
                            kernel_counts["budget_discarded"] = kernel_counts.get("budget_discarded", 0) + (not saved)
                        else:
                            kernel_counts["filtered"] += bool(line)
                        if not line:
                            metadata["kmsg"]["available"] = False
                            metadata["kmsg"]["unexpected_eof"] = True
                            selector.unregister(kmsg)
                    except BlockingIOError:
                        pass
                    except OSError as error:
                        kernel_counts["epipe"] += error.errno == errno.EPIPE
                        kernel.write(f"UNAVAILABLE mono_us={time.monotonic_ns() // 1000} kmsg errno={error.errno}\n")
                        if error.errno != errno.EPIPE:
                            metadata["kmsg"]["available"] = False
                            metadata["kmsg"]["runtime_error"] = error.errno
                            selector.unregister(kmsg)
                    if kernel.truncated:
                        metadata["kmsg"].update(available=False, output_budget_exhausted=True)
                        if kmsg in selector.get_map():
                            selector.unregister(kmsg)
                    if kernel_reads >= 4096:
                        kernel.write("UNAVAILABLE kernel read budget exhausted\n")
                        metadata["kmsg"]["read_limit_reached"] = True
                        metadata["kmsg"]["available"] = False
                        if kmsg in selector.get_map():
                            selector.unregister(kmsg)
    except KeyboardInterrupt:
        metadata["interrupted"] = True
    finally:
        try:
            if not driver_log.truncated:
                driver_log.write(json.dumps({"mono_us": time.monotonic_ns() // 1000, "phase": "before_stop",
                                             "observations": driver_capture.sample()}, separators=(",", ":")) + "\n")
        except Exception:
            metadata["driver_final_snapshot_error"] = True
        try:
            metadata["driver_stop"] = driver_capture.stop()
        except BaseException:
            # A failed diagnostic stop must not skip restoring the shared stdin
            # flags or closing files. The kernel window still has its deadline.
            metadata["driver_stop"] = {"available": False, "reason": "cleanup_failed_deadline_fallback"}
        try:
            if not driver_log.truncated:
                driver_log.write(json.dumps({"mono_us": time.monotonic_ns() // 1000, "phase": "after_stop",
                                             "observations": driver_capture.sample()}, separators=(",", ":")) + "\n")
        except Exception:
            metadata["driver_final_snapshot_error"] = True
        selector.close()
        if stdin_fd is not None and stdin_blocking is not None:
            try:
                os.set_blocking(stdin_fd, stdin_blocking)
            except OSError as error:
                metadata["mark_input"]["restore_errno"] = error.errno
        if kmsg is not None:
            os.close(kmsg)
        config.close()
        kernel.close()
        system_log.close()
        driver_log.close()
    metadata["configuration_truncated"] = config.truncated
    metadata["kernel_truncated"] = kernel.truncated
    metadata["kmsg"]["read_attempts"] = kernel_reads
    metadata["kmsg"]["counts"] = kernel_counts
    metadata["output_budgets"] = {name: {"truncated": output.truncated,
                                             "first_lost_mono_us": output.first_lost_mono_us}
                                  for name, output in (("configuration", config), ("kernel", kernel),
                                                       ("system", system_log), ("driver", driver_log))}
    end_us = time.monotonic_ns() // 1000
    metadata["collection_end_us"] = end_us
    metadata["collection_wall_end_ms"] = time.time_ns() // 1000000
    metadata["identity_end"] = support.identity(SYSTEM_ROOT)
    diagnostic = read_node(TEMP / "audio-diagnostic.log", MAX_DIAGNOSTIC, owned=True)
    if diagnostic.get("available"):
        text = diagnostic.pop("text")
        diagnostic.update(diagnostic_coverage(text, start_us, end_us, wall_start_ms))
        diagnostic["lifetime"] = trace_lifetime(text, metadata["identity_start"], metadata["identity_end"])
        changed = (initial.get("available") and
                   (initial.get("file_identity") != diagnostic.get("file_identity") or
                    trace_prefix(initial["text"]) != trace_prefix(text)))
        diagnostic["trace_replaced_or_restarted"] = bool(changed)
        if changed:
            diagnostic.update(coverage="partial", reason="trace_replaced_or_restarted")
        elif diagnostic["coverage"] == "current" and not diagnostic["lifetime"]["verified"]:
            diagnostic.update(coverage="unavailable", reason=diagnostic["lifetime"]["reason"])
        # Completion and current collection coverage are independent.
        (directory / "audio-diagnostic.log").write_text(text, encoding="utf-8")
        diagnostic["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        diagnostic.update(coverage="unavailable", reason="trace_unavailable")
    metadata["diagnostic"] = diagnostic
    (directory / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    archive = directory.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(directory.iterdir()):
            output.write(path, arcname=path.name)
    os.chmod(archive, 0o600)
    print("Kodi diagnostic coverage: " + diagnostic["coverage"] + " (" + diagnostic["reason"] + ")")
    if metadata["driver_stop"].get("reason") == "cleanup_failed_deadline_fallback":
        print("WARNING: driver diagnostic stop was not verified; recording is bounded by its kernel deadline.")
    if diagnostic["coverage"] != "current":
        print("WARNING: this archive lacks verified Kodi coverage for the full collection; do not treat missing metrics as healthy.")
    print(f"Capture: {archive}\nSHA256: {hashlib.sha256(archive.read_bytes()).hexdigest()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable", "collect"))
    parser.add_argument("label", nargs="?", choices=("native", "compatibility"), default="native")
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if not 60 <= args.seconds <= 600:
        parser.error("collection must be60–600 seconds")
    marker = TEMP / "audio-diagnostic.enable"
    if args.action == "enable":
        TEMP.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "w") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                parser.error("diagnostic marker is not a regular file")
            stream.write("900\n")
        print("Enabled for the next Kodi start, at most900s/8MiB. Restart Kodi normally, then wait at least30s and check TV/audio are awake.")
    elif args.action == "disable":
        marker.unlink(missing_ok=True)
        print("Marker removed. Current recording ends automatically; restart Kodi normally to disable immediately.")
    else:
        collect(args.label, args.seconds)


if __name__ == "__main__":
    main()
