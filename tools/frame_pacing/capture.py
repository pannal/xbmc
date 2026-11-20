"""Explicit Windows DirectShow capture; never control playback or another owner."""
import json
import pathlib
import shutil
import subprocess
import threading
import time

from .media import rational, sha256, write_json


def capture_command(path, rate, size, seconds, progress, device,
                    pixel_format="nv12", negotiate_rate=False):
    if not isinstance(device, str) or not device.strip() or any(c in device for c in "\r\n\0"):
        raise ValueError("An explicit native capture device name is required.")
    if not 1 <= float(seconds) <= 300 or not 1 <= rational(rate) <= 120:
        raise ValueError("Capture supports 1-300 seconds and a declared rate of 1-120 fps.")
    width, height = (int(value) for value in size.lower().split("x"))
    if not (320 <= width <= 3840 and 240 <= height <= 2160):
        raise ValueError("Capture size outside supported bounds.")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "verbose", "-debug_ts", "-nostdin",
        "-copyts", "-f", "dshow", "-rtbufsize", "256M",
        "-use_video_device_timestamps", "true", "-video_size", size,
        "-pixel_format", pixel_format,
    ]
    if negotiate_rate:
        command += ["-framerate", str(rational(rate))]
    return command + [
        "-t", str(seconds), "-i", f"video={device}", "-map", "0:v:0", "-an",
        "-filter_threads", "1", "-threads", "4",
        "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
        "-c:v", "ffv1", "-level", "3", "-g", "1",
        "-avoid_negative_ts", "disabled", "-flush_packets", "1",
        "-progress", str(progress), "-f", "nut", "-n", str(path),
    ]


def probe_capture(device, acknowledged):
    if not acknowledged:
        raise ValueError("--ack-exclusive is required even for native mode enumeration.")
    if not device:
        raise ValueError("An explicit capture device is required.")
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-f", "dshow", "-list_options", "true",
        "-i", f"video={device}",
    ], capture_output=True, text=True, timeout=30)
    text = result.stdout + result.stderr
    # FFmpeg deliberately exits nonzero after a successful option listing.
    if "pixel_format=" not in text and "vcodec=" not in text:
        raise RuntimeError("Native mode enumeration failed; no fallback device:\n" + text)
    print(text)


def capture(folder, rate, size, seconds, acknowledged, device,
            pixel_format="nv12", negotiate_rate=False):
    if not acknowledged:
        raise ValueError("--ack-exclusive is required; release the device from its owner first.")
    clock = time.get_clock_info("perf_counter")
    if not clock.monotonic or clock.resolution > 1e-5:
        raise RuntimeError("High-resolution monotonic arrival clock is unavailable.")
    folder = pathlib.Path(folder)
    video, progress = folder / "capture.nut", folder / "capture-progress.txt"
    command = capture_command(video, rate, size, seconds, progress, device, pixel_format, negotiate_rate)
    folder.mkdir(parents=True, exist_ok=False)
    width, height = map(int, size.lower().split("x"))
    reserve = int(width * height * 3 * float(rational(rate)) * float(seconds)) + 1024 ** 3
    if shutil.disk_usage(folder).free < reserve:
        raise RuntimeError(f"Insufficient free space: reserve {reserve} bytes for bounded lossless capture.")
    metadata = {
        "schema": "frame-pacing-capture-v1", "provenance": "explicit DirectShow device",
        "video": video.name, "capture_rate": str(rational(rate)), "capture_size": size,
        "seconds_limit": str(seconds), "command": command, "status": "starting",
        "device": device, "native_pixel_format": pixel_format,
        "input_rate_selection": "explicit negotiation" if negotiate_rate else "enumerated native pin default",
        "rate_meaning": "User-declared nominal input mode, not a calibrated physical clock.",
        "timestamp_policy": "copyts, demux encoder timebase, passthrough; original timestamps in native stderr log",
        "arrival_clock": vars(clock),
        "arrival_meaning": "perf_counter_ns/QPC native stderr receipt; not HDMI wire/exposure time",
        "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    }
    write_json(folder / "capture.json", metadata)
    process = reader = None
    reader_errors = []
    log = (folder / "capture-ffmpeg.log").open("wb")
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        metadata["owned_capture_pid"] = process.pid
        def read():
            try:
                with (folder / "qpc-arrival.jsonl").open("w", encoding="utf-8") as trace:
                    for line in iter(process.stderr.readline, b""):
                        trace.write(json.dumps({"qpc_ns": time.perf_counter_ns(),
                                                "wall_ns": time.time_ns(),
                                                "line": line.decode(errors="replace").rstrip()}) + "\n")
                        log.write(line)
                log.flush()
            except Exception as error:
                reader_errors.append(str(error))
            finally:
                process.stderr.close()
        reader = threading.Thread(target=read)
        reader.start()
        metadata["returncode"] = process.wait(timeout=float(seconds) + 30)
        if metadata["returncode"]:
            raise RuntimeError("Capture failed; inspect capture-ffmpeg.log. No fallback device was used.")
        reader.join(timeout=10)
        if reader.is_alive() or reader_errors:
            raise RuntimeError(f"Owned timestamp reader cleanup/fidelity failed: {reader_errors}")
        if not video.is_file() or not progress.is_file():
            raise RuntimeError("Capture video/progress was not produced.")
        text = (folder / "capture-ffmpeg.log").read_text(errors="replace").lower()
        resampled = any(int(line.split("=", 1)[1]) != 0 for line in
                        progress.read_text().splitlines() if line.startswith(("dup_frames=", "drop_frames=")))
        if resampled or any(word in text for word in (
                "real-time buffer", "too full", "i/o error", "error during", "non monoton",
                "non-monoton", "corrupt", "dropping", "dup!")):
            metadata["status"] = "capture_warning_inconclusive"
            raise RuntimeError("Capture reports loss/timing/IO warnings; evidence retained, not accepted as clean.")
        metadata["sha256"] = sha256(video)
        metadata["status"] = "recorded_not_yet_analyzed"
    except BaseException as error:
        metadata["error"] = str(error)
        if metadata["status"] != "capture_warning_inconclusive":
            metadata["status"] = "failed"
        raise
    finally:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if reader is not None and reader.ident is not None:
                reader.join(timeout=10)
                if reader.is_alive():
                    metadata["status"] = "failed"
                    raise RuntimeError("Owned timestamp reader is still running.")
            elif process is not None:
                process.stderr.close()
        finally:
            log.close()
            write_json(folder / "capture.json", metadata)
    return video
