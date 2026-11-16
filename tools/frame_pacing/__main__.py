"""Generate, capture and programmatically analyze numbered-frame pacing."""
import argparse
import pathlib
import subprocess
import sys

from .analysis import analyze, summary
from .media import generate, verify_source


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="Generate and verify every encoded source ID/PTS.")
    gen.add_argument("output", type=pathlib.Path)
    gen.add_argument("--rate", default="24", help="Exact rational, e.g. 24000/1001.")
    gen.add_argument("--duration", default="60")
    gen.add_argument("--width", type=int, default=1920)
    gen.add_argument("--height", type=int, default=1080)
    gen.add_argument("--format", dest="video_format", choices=("sdr", "hdr10", "dv"), default="sdr")
    gen.add_argument("--dovi-tool", type=pathlib.Path)
    verify = commands.add_parser("verify", help="Hash and decode every source frame/PTS and format.")
    verify.add_argument("manifest", type=pathlib.Path)
    analysis = commands.add_parser("analyze", help="Decode every native sample without temporal conversion.")
    analysis.add_argument("video", type=pathlib.Path)
    analysis.add_argument("--manifest", type=pathlib.Path, required=True)
    analysis.add_argument("--output", type=pathlib.Path, required=True)
    analysis.add_argument("--capture-rate", help="Declared input mode; never an inferred average.")
    analysis.add_argument("--display-rate", help="Known nominal HDMI rate; not a calibrated clock.")
    analysis.add_argument("--timestamp-tolerance-ms", default="1")
    modes = commands.add_parser("probe-capture", help="LIVE Windows: enumerate the explicit device.")
    record = commands.add_parser("capture", help="LIVE Windows: lossless native capture; no playback changes.")
    for command in (modes, record):
        command.add_argument("--device", required=True)
        command.add_argument("--ack-exclusive", action="store_true")
    record.add_argument("output", type=pathlib.Path)
    record.add_argument("--capture-rate", required=True, help="Native pin's declared nominal rate.")
    record.add_argument("--capture-size", required=True)
    record.add_argument("--seconds", type=float, default=75)
    record.add_argument("--pixel-format", default="nv12")
    record.add_argument("--negotiate-rate", action="store_true",
                        help="Force input SetFormat only if this exact rate is supported; no output resampling.")
    args = parser.parse_args(argv)
    try:
        if args.command in ("generate", "verify"):
            manifest = generate(args.output, args.rate, args.duration, args.width, args.height,
                                args.video_format, args.dovi_tool) if args.command == "generate" else args.manifest
            result = verify_source(manifest)
            fmt = result["format_verification"]
            print(f"verified {fmt['format']}: {result['verified_ids']} IDs/PTS @ {result['rate']}; RPU frames={fmt['rpu_frames']}")
            for warning in fmt["warnings"]:
                print("WARNING: " + warning, file=sys.stderr)
            print(manifest)
        elif args.command == "analyze":
            report = analyze(args.video, args.manifest, args.output, args.capture_rate,
                             args.timestamp_tolerance_ms, args.display_rate)
            print(summary(report))
            print(args.output / "analysis.json")
            return {"observed_clean": 0, "fail": 1, "inconclusive": 2}[report["full_coverage_status"]]
        else:
            from .capture import capture, probe_capture
            if args.command == "probe-capture":
                probe_capture(args.device, args.ack_exclusive)
            else:
                print(capture(args.output, args.capture_rate, args.capture_size, args.seconds,
                              args.ack_exclusive, args.device, args.pixel_format, args.negotiate_rate))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
