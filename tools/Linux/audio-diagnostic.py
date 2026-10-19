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
            if owned and not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {"available": False, "reason": "not_regular"}
            data = stream.read(limit + 1)
        if data is None:
            return {"available": False, "reason": "would_block"}
        if len(data) > limit:
            return {"available": False, "reason": "size_limit"}
        return {"available": True, "text": redact(data.decode("utf-8", "replace"))}
    except OSError as error:
        return {"available": False, "errno": error.errno}


def configuration(root=Path("/")):
    # Do not read PCM status: its ALSA getter resets avail_max/overrange.
    files = sorted(glob.glob(str(root / "proc/asound/card*/pcm*p/sub*/hw_params")))
    files += sorted(glob.glob(str(root / "proc/asound/card*/pcm*p/sub*/sw_params")))
    files += [str(root / path) for path in (
        "sys/class/amhdmitx/amhdmitx0/config", "sys/class/amhdmitx/amhdmitx0/avmute",
        "sys/class/display/mode",
    )]
    result = {"mono_us": time.monotonic_ns() // 1000, "wall_unix_ms": time.time_ns() // 1000000,
              "pcm_nodes_found": len(files) - 3, "nodes_truncated": len(files) > 35,
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

    def write(self, text):
        data = text.encode("utf-8")
        if self.truncated:
            return
        if self.bytes + len(data) > self.limit - 128:
            self.stream.write("\nTRUNCATED: output budget reached; remaining observations unavailable\n")
            self.truncated = True
            return
        self.stream.write(text)
        self.stream.flush()
        self.bytes += len(data)

    def close(self):
        self.stream.close()


def mark(line):
    match = re.fullmatch(r"\s*(\d{1,4}(?:[.:]\d{1,3})?)?(?:\s+(yes|no|unknown))?\s*", line)
    if not match:
        return None
    return {"mono_us": time.monotonic_ns() // 1000, "wall_unix_ms": time.time_ns() // 1000000,
            "track_position": match[1] or "unknown", "automatic_recovery": match[2] or "unknown"}


def collect(label, seconds):
    TEMP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = TEMP / f"audio-capture-{label}-{stamp}-{os.getpid()}"
    directory.mkdir(mode=0o700)
    config = BoundedFile(directory / "configuration.jsonl")
    kernel = BoundedFile(directory / "kernel-audio.log")
    metadata = {"label": label, "seconds": seconds, "kernel_release": os.uname().release,
                "release": read_node("/etc/release"), "kmsg": {"available": False},
                "clock": "CLOCK_MONOTONIC", "marks": []}
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
    kernel_reads = 0
    try:
        print(f"Collecting {label} for {seconds}s. Start the SAME track now.", flush=True)
        print("At a dropout press Enter, or enter e.g. '55 yes' (position, automatic recovery yes/no/unknown).", flush=True)
        while time.monotonic() < deadline:
            if time.monotonic() >= next_config:
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
                        event = kernel_event(line)
                        if event is not None:
                            kernel.write(json.dumps(event, separators=(",", ":")) + "\n")
                        if not line:
                            metadata["kmsg"]["available"] = False
                            metadata["kmsg"]["unexpected_eof"] = True
                            selector.unregister(kmsg)
                    except BlockingIOError:
                        pass
                    except OSError as error:
                        kernel.write(f"UNAVAILABLE mono_us={time.monotonic_ns() // 1000} kmsg errno={error.errno}\n")
                        if error.errno != errno.EPIPE:
                            metadata["kmsg"]["available"] = False
                            metadata["kmsg"]["runtime_error"] = error.errno
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
    metadata["configuration_truncated"] = config.truncated
    metadata["kernel_truncated"] = kernel.truncated
    metadata["kmsg"]["read_attempts"] = kernel_reads
    diagnostic = read_node(TEMP / "audio-diagnostic.log", MAX_DIAGNOSTIC, owned=True)
    if diagnostic.get("available"):
        text = diagnostic.pop("text")
        diagnostic["complete"] = "\nEND " in text
        # The trace may still be recording. A partial copy is explicit, not a healthy trace.
        (directory / "audio-diagnostic.log").write_text(text, encoding="utf-8")
        diagnostic["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    metadata["diagnostic"] = diagnostic
    (directory / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    archive = directory.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(directory.iterdir()):
            output.write(path, arcname=path.name)
    os.chmod(archive, 0o600)
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
