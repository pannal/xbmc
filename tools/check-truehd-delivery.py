#!/usr/bin/env python3
"""Compare debug TrueHD content fingerprints through codec, IEC framing and ALSA.

CRC uses Kodi Crc32 (MSB-first 0x04c11db7, initial ffffffff, no final XOR).
Successful ALSA writes establish software acceptance, not HDMI/eARC reception.
Startup/seek codec runs can be flushed legitimately. By default compare the last
codec run from its first observed sink match, after MAT 500 (about ten seconds).
"""
import argparse
import functools
import json
import lzma
from pathlib import Path
import random
import re

MASK = 0xffffffff
POLY = 0x04c11db7


def shift_bit(crc):
    return ((crc << 1) & MASK) ^ (POLY if crc & 0x80000000 else 0)


def matrix_times(matrix, value):
    result = 0
    while value:
        bit = value & -value
        result ^= matrix[bit.bit_length() - 1]
        value ^= bit
    return result


@functools.lru_cache(maxsize=128)
def shift_matrix(length):
    matrix = tuple(1 << bit for bit in range(32))
    step = []
    for value in matrix:
        for _ in range(8):
            value = shift_bit(value)
        step.append(value)
    while length:
        if length & 1:
            matrix = tuple(matrix_times(step, value) for value in matrix)
        step = tuple(matrix_times(step, value) for value in step)
        length >>= 1
    return matrix


def join_crc(first, second, second_bytes):
    matrix = shift_matrix(second_bytes)
    return matrix_times(matrix, first ^ MASK) ^ second


def crc_bytes(data):
    crc = MASK
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = shift_bit(crc)
    return crc


def analyze(lines, min_mat=500, catalog=None):
    codecs, sinks, issues = [], [], []
    current = None
    for line_number, line in enumerate(lines, 1):
        match = re.search(r'TrueHD delivery (codec|sink|ALSA|other): (.*)', line)
        if not match:
            continue
        kind, body = match.groups()
        fields = dict(re.findall(r'(\w+)=([0-9a-f]+)', body))
        fields = {key: int(value, 16 if key.endswith('_crc') else 10)
                  for key, value in fields.items()}
        fields['line'] = line_number
        if kind == 'codec':
            if codecs and fields['mat'] == 1:
                codecs = []
            codecs.append(fields)
        elif kind == 'sink':
            current = {**fields, 'writes': []}
            sinks.append(current)
        elif kind == 'other':
            current = None
        elif current is not None:
            current['writes'].append(fields)

    # Check each sink transaction independently of codec alignment.
    for sink in sinks:
        position, crc = 0, MASK
        for write in sink['writes']:
            if (write['offset'] != position or write['frame_size'] != sink['frame_size'] or
                    write['accepted'] > write['requested'] or
                    write['requested'] > sink['frames'] - position):
                issues.append({'line': write['line'], 'problem': 'write range/count mismatch'})
            accepted_bytes = write['accepted'] * write['frame_size']
            crc = join_crc(crc, write['accepted_crc'], accepted_bytes)
            position += write['accepted']
        if position != sink['frames'] or position * sink['frame_size'] != sink['carrier_bytes']:
            issues.append({'line': sink['line'], 'problem': 'incomplete software acceptance',
                           'accepted_frames': position, 'expected_frames': sink['frames']})
        if crc != sink['carrier_crc']:
            issues.append({'line': sink['line'], 'problem': 'accepted byte CRC mismatch',
                           'expected_crc': f"{sink['carrier_crc']:08x}",
                           'accepted_crc': f'{crc:08x}'})

    eligible = [record for record in codecs if record['mat'] >= min_mat]
    for previous, record in zip(codecs, codecs[1:]):
        if max(previous['mat'], record['mat']) >= min_mat and record['mat'] != previous['mat'] + 1:
            issues.append({'line': record['line'], 'problem': 'codec MAT ordinal gap or reorder',
                           'previous_mat': previous['mat'], 'mat': record['mat']})
    reference = {}
    if catalog is not None:
        for record in catalog['records']:
            reference[record['mat']] = record
        for record in eligible:
            expected = reference.get(record['mat'])
            if expected is None or record['payload_crc'] != int(expected['payload_crc'], 16):
                issues.append({'line': record['line'], 'problem': 'codec payload differs from source catalog at MAT',
                               'mat': record['mat']})
    matched, index, started, first_mat = 0, 0, False, None
    byte_orders = {'normal', 'swapped'}
    for sink in sinks:
        if not eligible or sink['line'] < eligible[0]['line']:
            continue
        if not started:
            # A legitimately flushed prefix is not evidence of corruption. State
            # exactly where coverage begins; do not silently claim that prefix.
            candidates = [i for i, item in enumerate(eligible)
                          if item['payload_crc'] == sink['payload_crc']]
            if not candidates:
                continue
            index = candidates[0]
            first_mat = eligible[index]['mat']
            started = True
        if index >= len(eligible):
            issues.append({'line': sink['line'], 'problem': 'extra sink packet'})
            continue
        expected = eligible[index]
        if sink['payload_crc'] != expected['payload_crc'] or expected['bytes'] != sink['carrier_bytes']:
            issues.append({'line': sink['line'], 'problem': 'codec/sink sequence or payload mismatch',
                           'expected_mat': expected['mat']})
        else:
            matched += 1
        if catalog is not None:
            entry = reference.get(expected['mat'])
            observed = {order for order, key in (('normal', 'carrier_crc'), ('swapped', 'swapped_carrier_crc'))
                        if entry and sink['carrier_crc'] == int(entry[key], 16)}
            if not observed:
                issues.append({'line': sink['line'], 'problem': 'carrier CRC differs from source catalog'})
            elif not byte_orders.intersection(observed):
                issues.append({'line': sink['line'], 'problem': 'carrier byte order changed within compared run'})
            else:
                byte_orders.intersection_update(observed)
        index += 1
    if not started:
        issues.append({'problem': 'no codec/sink comparison coverage'})
    elif index != len(eligible):
        issues.append({'problem': 'codec output remains unmatched', 'count': len(eligible) - index})
    if not sinks:
        issues.append({'problem': 'no sink delivery records'})
    return {'last_codec_run_packets': len(codecs), 'sink_packets': len(sinks),
            'matched_codec_packets': matched, 'first_compared_mat': first_mat,
            'reference_catalog_supplied': catalog is not None,
            'carrier_byte_orders': sorted(byte_orders) if catalog is not None and started else [],
            'issues': issues,
            'limit': 'CRCs and successful writes establish software continuity, not receiver reception.'}


def self_test():
    assert crc_bytes(b'123456789') == 0x0376e6e7  # CRC-32/MPEG-2 check vector
    rng = random.Random(73)
    for n in [0, 1, 7, 16, 257, 61440]:
        data = rng.randbytes(n)
        for split in sorted({0, n // 3, n // 2, n}):
            assert join_crc(crc_bytes(data[:split]), crc_bytes(data[split:]), n - split) == crc_bytes(data)
    carrier = bytes(range(64))
    payload = crc_bytes(carrier[8:])
    base = [f'TrueHD delivery codec: mat=500 bytes=64 payload_crc={payload:08x}',
            f'TrueHD delivery sink: payload_crc={payload:08x} carrier_bytes=64 carrier_crc={crc_bytes(carrier):08x} frames=4 frame_size=16']
    writes = [f'TrueHD delivery ALSA: offset=0 requested=4 accepted=0 frame_size=16 accepted_crc={MASK:08x}',
              f'TrueHD delivery ALSA: offset=0 requested=4 accepted=1 frame_size=16 accepted_crc={crc_bytes(carrier[:16]):08x}',
              f'TrueHD delivery ALSA: offset=1 requested=3 accepted=3 frame_size=16 accepted_crc={crc_bytes(carrier[16:]):08x}']
    assert not analyze(base + writes)['issues']
    assert analyze(base + writes[:-1])['issues']
    assert analyze(base + [line.replace('offset=1', 'offset=2') for line in writes])['issues']
    assert analyze(base + [line.replace(f'{crc_bytes(carrier[16:]):08x}', '00000000') for line in writes])['issues']
    assert analyze(base + writes + [base[1]] + writes)['issues']
    assert not analyze(base + writes + ['TrueHD delivery other: input_bytes=0 carrier_frames=4'] + writes)['issues']
    assert analyze([])['issues']
    # Reject a missing/reordered codec output after coverage begins.
    two = base[:1] + [base[0].replace('mat=500', 'mat=501').replace(f'{payload:08x}', '12345678')] + base[1:] + writes + base[1:] + writes
    assert analyze(two)['issues']
    complete = base + writes
    assert analyze(complete + [line.replace('mat=500', 'mat=502') for line in complete])['issues']
    assert analyze(complete + complete)['issues']  # Duplicate codec record cannot mimic Reset().
    assert analyze(complete + [base[0].replace('mat=500', 'mat=499')])['issues']
    catalog = {'records': [{'mat': 500, 'payload_crc': f'{payload:08x}',
                           'carrier_crc': f'{crc_bytes(carrier):08x}',
                           'swapped_carrier_crc': f'{crc_bytes(carrier):08x}'}]}
    assert not analyze(base + writes, catalog=catalog)['issues']
    corrupted = bytes([carrier[0] ^ 1]) + carrier[1:]
    bad = base[:1] + [base[1].replace(f'{crc_bytes(carrier):08x}', f'{crc_bytes(corrupted):08x}'),
                     f'TrueHD delivery ALSA: offset=0 requested=4 accepted=4 frame_size=16 accepted_crc={crc_bytes(corrupted):08x}']
    assert not analyze(bad)['issues']  # Internally consistent bad framing needs a source reference.
    assert analyze(bad, catalog=catalog)['issues']
    wrong_source = {'records': [{**catalog['records'][0], 'payload_crc': '12345678'}]}
    assert analyze(base + writes, catalog=wrong_source)['issues']
    print('CRC check vector, joins, partial/zero writes, missing/corrupt/range/duplicate/order/no-coverage controls pass')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', nargs='?', type=Path)
    parser.add_argument('--min-mat', type=int, default=500)
    parser.add_argument('--catalog', type=Path,
                        help='ordered source-specific CRC catalog; requires playback from source start')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    if args.log:
        opener = lzma.open if args.log.suffix == '.xz' else open
        with opener(args.log, 'rt', errors='replace') as stream:
            catalog = json.loads(args.catalog.read_text()) if args.catalog else None
            result = analyze(stream, args.min_mat, catalog)
        print(json.dumps(result, indent=2))
        raise SystemExit(bool(result['issues']))
    if not args.self_test:
        parser.error('provide a log or --self-test')


if __name__ == '__main__':
    main()
