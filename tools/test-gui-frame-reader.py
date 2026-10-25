#!/usr/bin/env python3
"""Exercise v1/v2 timing-file acceptance, rejection and state decoding offline."""
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from gui_frame_timing import CAPACITY, COUNTERS, HEADER, read


def payload(version=2):
    count = 3 if version == 1 else 32
    stride = 48 + 8 * 24 + count * 8
    data = bytearray(64 + CAPACITY * stride)
    HEADER.pack_into(data, 0, b'P3IGUI1\0' if version == 1 else b'P3IGUI2\0',
                     version, 64, stride, CAPACITY, 8, count, 111, 111, 2, 0, 0, 0)
    shape = struct.Struct('<5Q2I' + str(24 + count) + 'Q')
    for i in range(2):
        regions = [1, 70, 7] + [0] * 21
        counters = [1, 1, 0] + [0] * (count - 3)
        if version == 2:
            counters[COUNTERS.index('MessageNavBeforeSelection')] = (1 << 64) - 1
            counters[COUNTERS.index('MessageNavStarted')] = 2
            counters[COUNTERS.index('MessageNavCompleted')] = 2
            counters[COUNTERS.index('MessageNavCandidateChanged')] = 2
        shape.pack_into(data, 64 + i * stride, i + 1, 100 + i * 200, 10 + i * 20,
                        200 + i * 200, 20 + i * 20, 0, 0, *regions, *counters)
    return data


def main():
    header = (Path(__file__).resolve().parents[1] / 'xbmc/utils/GUIFrameTiming.h').read_text()
    body = re.search(r'enum class Counter[^\{]*\{([^}]+)\}', header).group(1)
    names = tuple(x.strip() for x in body.split(',') if x.strip() and x.strip() != 'Count')
    assert names == COUNTERS
    checks = 0
    with tempfile.TemporaryDirectory(prefix='gui-frame-reader-') as temporary:
        file = Path(temporary) / 'capture.bin'
        def accept(data, version=2):
            nonlocal checks
            file.write_bytes(data);result = read(file, expected_pid=111, expected_tid=111)
            assert result['version'] == version and result['completed'] == 2
            checks += 1
            return result
        def reject(data, **kwargs):
            nonlocal checks
            file.write_bytes(data)
            try:
                read(file, **kwargs)
            except ValueError:
                checks += 1
            else:
                raise AssertionError('Malformed or mismatched capture accepted')
        legacy = accept(payload(1), 1)
        assert tuple(legacy['records'][0]['counters']) == COUNTERS[:3]
        current = payload();result = accept(current)
        assert result['records'][0]['counters']['MessageNavBeforeSelection'] == -1
        assert result['records'][0]['counters']['MessageNavCandidateChanged'] == 2
        for index, value in [(1, 3), (2, 63), (3, 264), (4, 9), (5, 7), (6, 3),
                             (7, 0), (8, 0), (8, 222), (9, CAPACITY + 1), (10, 32), (11, 1), (12, 1)]:
            bad = current.copy();h = list(HEADER.unpack_from(bad));h[index] = value
            HEADER.pack_into(bad, 0, *h);reject(bad)
        reject(b'bad');reject(current[:-1]);reject(current + b'!')
        reject(current, expected_pid=999);reject(current, expected_tid=999)
        for offset, fmt, value in [(64, '<Q', 7), (64 + 8, '<Q', 0),
                                   (64 + 40, '<I', 8),
                                   (64 + 496 + 16, '<Q', 19), (64 + 24, '<Q', 0),
                                   (64 + 32, '<Q', 0), (64 + 40, '<I', 32),
                                   (64 + 44, '<I', 1), (64 + 48, '<Q', 0),
                                   (64 + 496 + 8, '<Q', 199)]:
            bad = current.copy();struct.pack_into(fmt, bad, offset, value);reject(bad)
        # A non-atomic live copy can contain multiple newer unpublished slots.
        live = current.copy();live[64 + 2 * 496] = 99;accept(live)
        newer = live.copy();newer[64 + 3 * 496] = 1
        result = accept(newer)
        assert len(result['records']) == 2 and result['unpublished_tail_nonzero']
        # Known annotation/failure flags remain visible, never silently dropped.
        annotated = current.copy();struct.pack_into('<I', annotated, 44, 2)
        struct.pack_into('<II', annotated, 64 + 40, 8, 1)
        result = accept(annotated);assert result['flags'] == 2 and result['has_annotated_frames']
        file.write_bytes(current)
        output = subprocess.check_output([sys.executable, str(Path(__file__).with_name('gui_frame_timing.py')), str(file)], text=True)
        summary = json.loads(output)
        assert 'records' not in summary and summary['counter_totals']['MessageNavCandidateChanged'] == 4
        assert 'MessageNavBeforeSelection' not in summary['counter_totals']
        checks += 1
    print('PASS', checks, 'reader checks; v1/v2 and writer enum agree; no device access')


if __name__ == '__main__':
    main()
