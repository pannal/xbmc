"""FFmpeg media IO. No temporal filters, output frame rate or CFR conversion."""
import hashlib
import json
import math
import pathlib
import secrets
import shutil
import subprocess
from fractions import Fraction

import numpy as np

from .marker import GEOMETRY, decode, render


HDR_SIGNAL = {
    "pixel_format": "yuv420p10le", "primaries": "bt2020", "transfer": "smpte2084",
    "matrix": "bt2020nc", "range": "tv", "pattern_white_nits": 1000,
    "mapping": "Neutral grayscale 0..255 mapped to linear 0..1000 nits, then ST2084 PQ.",
    "mastering_display": "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
    "max_cll": 1000, "max_fall": 400,
}


def pq_normalized(nits):
    luminance = np.asarray(nits, dtype=np.float64) / 10000
    power = luminance ** (2610 / 16384)
    return ((3424 / 4096 + (2413 / 128) * power) /
            (1 + (2392 / 128) * power)) ** (2523 / 32)


PQ_LUMA = np.rint(64 + 876 * pq_normalized(np.arange(256) / 255 * 1000)).astype("<u2")


def hdr_frame_bytes(image):
    gray = np.asarray(image)[:, :, 0]
    chroma = np.full(gray.size // 2, 512, dtype="<u2")
    return PQ_LUMA[gray].tobytes() + chroma.tobytes()


def hdr_encoder_args():
    parameters = (
        "bframes=0:repeat-headers=1:aud=1:pools=2:frame-threads=1:"
        "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:"
        f"master-display={HDR_SIGNAL['mastering_display']}:max-cll=1000,400:"
        "vbv-maxrate=20000:vbv-bufsize=20000"
    )
    return [
        "-c:v", "libx265", "-preset", "fast", "-crf", "16", "-bf", "0",
        "-pix_fmt", "yuv420p10le", "-color_range", "tv", "-colorspace", "bt2020nc",
        "-color_trc", "smpte2084", "-color_primaries", "bt2020",
        "-x265-params", parameters,
    ]


def rational(value):
    result = Fraction(str(value))
    if result <= 0:
        raise ValueError("Rate/duration must be positive.")
    return result


def write_json(path, value):
    pathlib.Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path):
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_streams", "-show_frames", "-show_entries",
               "stream=width,height,time_base,r_frame_rate,avg_frame_rate,codec_name:"
               "frame=pts,best_effort_timestamp,pkt_dts,duration",
               "-of", "json", str(path)]
    return json.loads(subprocess.check_output(command))


def load_manifest(path):
    manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema") != "frame-pacing-source-v1" or manifest.get("marker") != GEOMETRY:
        raise ValueError("Unsupported source manifest/marker geometry.")
    rational(manifest["rate"])
    if not 0 < manifest["frame_count"] <= 0xffffffff or not 0 <= manifest["clip_tag"] <= 65535:
        raise ValueError("Invalid source count/tag.")
    return manifest


def generate(folder, rate="24", duration="60", width=1920, height=1080,
             video_format="sdr", dovi_tool=None):
    rate, duration = rational(rate), rational(duration)
    if not 1 <= rate <= 120 or not duration <= 300:
        raise ValueError("Generation supports 1-120 fps and at most 300 seconds.")
    if width % 2 or height % 2 or not (640 <= width <= 3840 and 360 <= height <= 2160):
        raise ValueError("Use even dimensions between 640x360 and 3840x2160.")
    if video_format not in ("sdr", "hdr10", "dv"):
        raise ValueError("Format must be sdr, hdr10 or dv.")
    if video_format == "dv":
        dovi_tool = dovi_tool or shutil.which("dovi_tool")
        if not dovi_tool:
            raise RuntimeError("Dolby Vision generation needs dovi_tool; supply --dovi-tool.")
        dovi_tool = pathlib.Path(dovi_tool).resolve()
        if not dovi_tool.is_file():
            raise RuntimeError("Dolby Vision generation needs dovi_tool; supply --dovi-tool. No mislabeled HDR10 substitute is generated.")
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    count, tag = math.ceil(rate * duration), secrets.randbits(16)
    clip = folder / "source.mp4"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "rawvideo", "-pixel_format", "rgb24" if video_format == "sdr" else "yuv420p10le",
        "-video_size", f"{width}x{height}",
        "-framerate", str(rate), "-i", "pipe:0", "-an", "-fps_mode", "passthrough", "-threads", "4",
    ]
    if video_format == "sdr":
        command += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-bf", "0",
            "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
            "-color_trc", "bt709", "-color_primaries", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        ]
    else:
        command += hdr_encoder_args()
    command += (["-f", "hevc", "-n", str(folder / "base.hevc")] if video_format == "dv"
                else ["-tag:v", "hvc1", "-movflags", "+faststart", "-n", str(clip)]
                if video_format == "hdr10" else ["-movflags", "+faststart", "-n", str(clip)])
    with (folder / "generate-ffmpeg.log").open("wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            for frame_id in range(1, count + 1):
                image = render(frame_id, tag, width, height, count, video_format.upper())
                process.stdin.write(image.tobytes() if video_format == "sdr" else hdr_frame_bytes(image))
            process.stdin.close()
            if process.wait(timeout=120):
                raise RuntimeError("Generation failed; see generate-ffmpeg.log.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    dv_metadata = None
    if video_format == "dv":
        config = {
            "cm_version": "V29", "profile": "8.1", "length": count,
            "level6": {"max_display_mastering_luminance": 1000,
                       "min_display_mastering_luminance": 1,
                       "max_content_light_level": 1000, "max_frame_average_light_level": 400},
            "default_metadata_blocks": [{"Level1": {"min_pq": 0, "max_pq": 3079, "avg_pq": 2000}}],
        }
        write_json(folder / "rpu-config.json", config)
        metadata_commands = [
            [str(dovi_tool), "generate", "-j", "rpu-config.json", "-o", "RPU.bin"],
            [str(dovi_tool), "inject-rpu", "-i", "base.hevc", "--rpu-in", "RPU.bin",
             "-o", "with-rpu.hevc"],
            ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
             "-f", "hevc", "-framerate", str(rate), "-i", "with-rpu.hevc", "-an",
             "-fps_mode", "passthrough", *hdr_encoder_args(), "-dolbyvision", "1",
             "-tag:v", "hvc1", "-strict", "unofficial", "-movflags", "+faststart",
             "-n", "source.mp4"],
        ]
        with (folder / "dolby-vision-generation.log").open("wb") as log:
            for step in metadata_commands:
                subprocess.run(step, cwd=folder, check=True, stdout=log, stderr=log, timeout=300)
        dv_metadata = {
            "profile": "8.1", "content_mapping": "CMv2.9 synthetic fixed L1, HDR10-compatible base; no EL/FEL",
            "color_accuracy": "Synthetic metadata for pacing-path exercise, not a Dolby-certified color grade.",
            "tool_version": subprocess.check_output([str(dovi_tool), "--version"], text=True).strip(),
            "tool_sha256": sha256(dovi_tool), "rpu_sha256": sha256(folder / "RPU.bin"),
            "commands": metadata_commands, "config": config,
        }
    manifest = {
        "schema": "frame-pacing-source-v1",
        "provenance": f"generated {video_format.upper()} reference, not HDMI capture",
        "video_format": video_format, "signal": HDR_SIGNAL if video_format != "sdr" else {
            "pixel_format": "yuv420p", "primaries": "bt709", "transfer": "bt709", "matrix": "bt709"},
        "dolby_vision": dv_metadata,
        "rate": str(rate), "requested_duration": str(duration),
        "duration": str(Fraction(count, 1) / rate), "frame_count": count,
        "first_id": 1, "clip_tag": tag, "width": width, "height": height,
        "marker": GEOMETRY, "file": clip.name, "sha256": sha256(clip),
        "generation_command": command,
        "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    }
    write_json(folder / "source.json", manifest)
    return folder / "source.json"


def observations(path, manifest, log_path, info=None):
    """Yield every decoded sample paired one-to-one with original ffprobe timestamps."""
    info = probe(path) if info is None else info
    if len(info.get("streams", [])) != 1:
        raise RuntimeError("Expected one selected video stream.")
    stream = info["streams"][0]
    width, height = stream["width"], stream["height"]
    frame_bytes = width * height
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
               "-copyts", "-i", str(path), "-map", "0:v:0", "-an",
               "-fps_mode", "passthrough", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
    with pathlib.Path(log_path).open("wb") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log)
        try:
            for index, frame in enumerate(info["frames"]):
                data = bytearray()
                while len(data) < frame_bytes:
                    chunk = process.stdout.read(frame_bytes - len(data))
                    if not chunk:
                        raise RuntimeError("Decoded video/ffprobe sample count mismatch.")
                    data.extend(chunk)
                gray = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
                result = decode(gray, manifest["clip_tag"])
                if result["id"] is not None and not 1 <= result["id"] <= manifest["frame_count"]:
                    result = {**result, "id": None, "decode_status": "id_out_of_manifest"}
                yield {
                    "sample": index, "pts": frame.get("pts"),
                    "best_effort_timestamp": frame.get("best_effort_timestamp"),
                    "pkt_dts": frame.get("pkt_dts"), "duration": frame.get("duration"),
                    "time_base": stream["time_base"],
                    "time": (str(Fraction(frame["pts"]) * Fraction(stream["time_base"]))
                             if "pts" in frame else None),
                    "image_sha256": hashlib.sha256(data).hexdigest(), **result,
                }
            if process.stdout.read(1):
                raise RuntimeError("Decoded video has more samples than ffprobe.")
            if process.wait(timeout=30):
                raise RuntimeError("Decode failed; see decode-ffmpeg.log.")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


def verify_source(manifest_path):
    manifest_path = pathlib.Path(manifest_path)
    manifest = load_manifest(manifest_path)
    clip = manifest_path.parent / manifest["file"]
    if sha256(clip) != manifest["sha256"]:
        raise RuntimeError("Source hash does not match manifest.")
    expected_period = 1 / Fraction(manifest["rate"])
    count = 0
    for row in observations(clip, manifest, manifest_path.parent / "verify-ffmpeg.log"):
        count += 1
        if row["id"] != count or row["time"] is None:
            raise RuntimeError(f"Source roundtrip failed at sample {row['sample']}: {row}")
        if Fraction(row["time"]) != (count - 1) * expected_period:
            raise RuntimeError("Source PTS do not match exact intended rational rate.")
    if count != manifest["frame_count"]:
        raise RuntimeError("Source roundtrip frame count mismatch.")
    format_info = inspect_format(clip, manifest)
    write_json(manifest_path.parent / "format-verification.json", format_info)
    result = {"status": "verified_generated_source", "verified_ids": count,
              "rate": manifest["rate"], "sha256": manifest["sha256"],
              "format_verification": format_info}
    write_json(manifest_path.parent / "verification.json", result)
    return result


def inspect_format(clip, manifest):
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_frames",
        "-show_entries", "stream=codec_name,profile,pix_fmt,color_range,color_space,color_transfer,"
        "color_primaries:stream_side_data:frame=pts:frame_side_data=side_data_type",
        "-of", "json", str(clip),
    ]
    info = json.loads(subprocess.check_output(command))
    stream = info["streams"][0]
    expected = manifest.get("video_format", "sdr")
    legacy_sdr = "video_format" not in manifest and expected == "sdr"
    if legacy_sdr and (stream.get("color_primaries") not in (None, "unknown", "bt709")
                       or stream.get("color_transfer") not in (None, "unknown", "bt709")):
        raise RuntimeError("Legacy SDR source contains incompatible color/HDR signaling.")
    if not legacy_sdr and stream.get("color_primaries") != ("bt709" if expected == "sdr" else "bt2020"):
        raise RuntimeError("Source color primaries do not match intended format.")
    if not legacy_sdr and stream.get("color_transfer") != ("bt709" if expected == "sdr" else "smpte2084"):
        raise RuntimeError("Source transfer function does not match intended format.")
    dv = [entry for entry in stream.get("side_data_list", [])
          if entry["side_data_type"] == "DOVI configuration record"]
    frame_types = [{side["side_data_type"] for side in frame.get("side_data_list", [])}
                   for frame in info["frames"]]
    rpu_count = sum("Dolby Vision RPU Data" in types for types in frame_types)
    if expected != "sdr":
        if stream.get("codec_name") != "hevc" or stream.get("pix_fmt") != "yuv420p10le":
            raise RuntimeError("HDR/DV source is not 10-bit HEVC.")
        types = set().union(*frame_types)
        if not {"Mastering display metadata", "Content light level metadata"} <= types:
            raise RuntimeError("HDR10 mastering/content-light metadata is missing.")
    if expected == "dv":
        if (len(dv) != 1 or dv[0]["dv_profile"] != 8
                or dv[0]["dv_bl_signal_compatibility_id"] != 1
                or dv[0]["rpu_present_flag"] != 1 or dv[0]["el_present_flag"] != 0
                or rpu_count != manifest["frame_count"]
                or any("Dolby Vision Metadata" not in types for types in frame_types)):
            raise RuntimeError("DV source lacks profile 8.1 container signaling or valid RPU metadata on every frame.")
    elif dv or rpu_count:
        raise RuntimeError("Unexpected Dolby Vision metadata in SDR/HDR10 source.")
    return {"format": expected, "stream": stream, "decoded_frames": len(frame_types),
            "rpu_frames": rpu_count,
            "warnings": (["Legacy SDR sample: explicit transfer/primaries tags were not verified."]
                         if legacy_sdr else [])}
