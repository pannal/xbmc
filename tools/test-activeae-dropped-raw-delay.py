#!/usr/bin/env python3
"""Actual OutputSamples must query physical delay for a dropped RAW burst.

Reuses the delivery fixture/packer and controlled ALSA boundary without a Git
baseline or diagnostic commit. No encoded-media input or hardware is required.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "delivery", ROOT / "tools/test-truehd-delivery.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    sink = (ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE/ActiveAESink.cpp").read_text()
    output = delivery.block(sink, "unsigned int CActiveAESink::OutputSamples(")
    original = delivery.HARNESS
    with tempfile.TemporaryDirectory(prefix="ae-dropped-raw-") as temp:
        for label in ["production"] + (["lost-physical-sink-delay"] if args.negative_controls else []):
            if label != "production":
                assert "m_sink->GetDelay(status);" in output
                mutant = output.replace("m_sink->GetDelay(status);", "status.SetDelay(0.0);")
                delivery.HARNESS = original.replace("@OUTPUT@", mutant)
            directory = Path(temp) / label
            result, _ = delivery.build(ROOT, directory, sanitize=True)
            # build also executes the existing carrier/offset/ALSA-error matrix.
            if label == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            dropped = subprocess.run([str(directory / "run"), str(directory / "dropped.bin"),
                                      "--dropped-raw"], capture_output=True, text=True)
            if label == "production":
                assert dropped.returncode == 0, dropped.stderr
                print("PASS whole OutputSamples dropped DTS RAW: measured sink delay retained with IEC and non-IEC output")
            else:
                assert dropped.returncode and "Discarded raw burst erased the sink delay" in dropped.stderr, dropped.stderr
                print("REJECTED:", label)
    delivery.HARNESS = original


if __name__ == "__main__":
    main()
