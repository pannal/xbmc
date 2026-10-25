#!/usr/bin/env python3
"""Read published GUI timing frames; supports the explicit v1 and v2 ABIs.

Durations are inclusive main-thread CPU/wall observations, not GPU or physical
presentation time. Counters sum within each frame; v2 scalar fields describe the
last completed message/decision. Message navigation describes TMSG_GUI_ACTION
routing candidates, not all physical inputs or proof of the actual recipient.
Unknown selection/window/control values use signed 64-bit -1. Known flags remain
visible; a flagged or bounded capture is not silently promoted to full coverage.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct

REGIONS = ('Process', 'FrameMove', 'Render', 'WindowProcess', 'AssignDepth',
           'PublishBounds', 'RenderPass', 'EGLSwap')
COUNTERS = ('FontLookup', 'FontHit', 'FontMiss', 'MessageNavStarted',
            'MessageNavCompleted', 'MessageNavHandled', 'MessageNavCandidateChanged',
            'MessageNavTargetChanged', 'MessageNavUnknownSelection', 'MessageNavLastAction',
            'MessageNavRequestedWindow', 'MessageNavBeforeWindow', 'MessageNavBeforeControl',
            'MessageNavBeforeSelection', 'MessageNavAfterWindow', 'MessageNavAfterControl',
            'MessageNavAfterSelection', 'WindowProcessCalls', 'DirtyEmitted', 'RenderCalls',
            'EffectiveBufferAge', 'DirtyAlgorithm', 'MarkedBeforeAge', 'MarkedAfterAge',
            'SolvedRegions', 'RenderForced', 'RenderDirty', 'RenderClean', 'Idle40ms',
            'RenderAttempt', 'PresentResult', 'GuiRendered')
SIGNED = frozenset(('MessageNavRequestedWindow', 'MessageNavBeforeWindow',
                    'MessageNavBeforeControl', 'MessageNavBeforeSelection',
                    'MessageNavAfterWindow', 'MessageNavAfterControl',
                    'MessageNavAfterSelection', 'EffectiveBufferAge', 'DirtyAlgorithm'))
SCALARS = SIGNED | frozenset(('MessageNavLastAction', 'RenderAttempt',
                              'PresentResult', 'GuiRendered'))
HEADER = struct.Struct('<8s10I2Q')
CAPACITY = 32768


def read(path, expected_pid=None, expected_tid=None):
    data = Path(path).read_bytes()
    if len(data) < HEADER.size:
        raise ValueError('Truncated header')
    h = HEADER.unpack_from(data)
    magic, version, header_bytes, stride, capacity, region_count, counter_count, pid, tid, completed, flags, spare0, spare1 = h
    shapes = {1: (b'P3IGUI1\0', 264, 3), 2: (b'P3IGUI2\0', 496, 32)}
    if version not in shapes or (magic, stride, counter_count) != shapes[version]:
        raise ValueError('Unsupported GUI timing ABI')
    if header_bytes != 64 or capacity != CAPACITY or region_count != len(REGIONS):
        raise ValueError('Invalid header shape')
    if spare0 or spare1 or flags & ~31 or not pid or pid != tid:
        raise ValueError('Invalid header identity/flags/reserved fields')
    if ((expected_pid is not None and pid != expected_pid) or
            (expected_tid is not None and tid != expected_tid)):
        raise ValueError('Capture PID/TID mismatch')
    if completed > capacity or len(data) != header_bytes + capacity * stride:
        raise ValueError('Invalid capture length/completed count')
    if completed == capacity and not flags & 1:
        raise ValueError('Full capture lacks bounded flag')
    names = COUNTERS[:counter_count]
    shape = struct.Struct('<5Q2I' + str(3 * region_count + counter_count) + 'Q')
    if shape.size != stride:
        raise ValueError('Record size mismatch')
    records = []
    prior_end = prior_cpu_end = 0
    for i in range(completed):
        row = shape.unpack_from(data, header_bytes + i * stride)
        frame, begin, cpu_begin, end, cpu_end, record_flags, nested = row[:7]
        if (frame != i + 1 or not begin or end < begin or cpu_end < cpu_begin or begin < prior_end or
                cpu_begin < prior_cpu_end or record_flags & ~31 or
                bool(nested) != bool(record_flags & 8)):
            raise ValueError('Invalid published frame sequence/clocks/flags')
        prior_end, prior_cpu_end = end, cpu_end
        regions = {}
        for j, name in enumerate(REGIONS):
            calls, wall, cpu = row[7 + j * 3:10 + j * 3]
            if not calls and (wall or cpu):
                raise ValueError('Duration without region invocation')
            regions[name] = {'calls': calls, 'wall_ns': wall, 'cpu_ns': cpu}
        counters = dict(zip(names, row[31:]))
        for name in SIGNED & counters.keys():
            if counters[name] >= 1 << 63:
                counters[name] -= 1 << 64
        records.append({'frame': frame, 'begin_ns': begin, 'end_ns': end,
                        'begin_cpu_ns': cpu_begin, 'end_cpu_ns': cpu_end,
                        'flags': record_flags, 'nested_frames': nested,
                        'regions': regions, 'counters': counters})
    # A sequential live copy can include several later frames written after its
    # header was copied. Only the header's published prefix is immutable. Expose
    # the tail's presence, but never decode or count those unpublished bytes.
    tail = header_bytes + completed * stride
    return {'version': version, 'pid': pid, 'tid': tid, 'completed': completed,
            'flags': flags, 'record_bytes': stride, 'records': records,
            'has_annotated_frames': any(r['flags'] for r in records),
            'unpublished_tail_nonzero': any(data[tail:])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--tid', type=int)
    parser.add_argument('--records', action='store_true', help='include published frames')
    args = parser.parse_args()
    capture = read(args.capture, args.pid, args.tid)
    totals = Counter()
    for row in capture['records']:
        totals.update({k: v for k, v in row['counters'].items() if k not in SCALARS})
    if not args.records:
        capture.pop('records')
    capture['counter_totals'] = dict(totals)
    print(json.dumps(capture, indent=2))


if __name__ == '__main__':
    main()
