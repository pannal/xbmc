"""Versioned full-frame-relative, complemented CRC-protected optical markers."""
import binascii
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFont

GEOMETRY = {
    "version": 1, "columns": 20, "rows": 8,
    "rectangles": [[.10, .08, .90, .26], [.10, .39, .90, .57],
                   [.10, .70, .90, .88]],
    "sample_inset": .25,
    "payload": "big-endian sync16(0xd391), clip_tag16, frame_id32, CRC16-CCITT(seed=0xffff)",
    "coding": "MSB first; each bit followed by its complement; all three copies must agree",
}


def bits_for(frame_id, tag):
    payload = struct.pack(">HHI", 0xD391, tag, frame_id)
    packet = payload + struct.pack(">H", binascii.crc_hqx(payload, 0xffff))
    bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
    return np.column_stack((bits, 1 - bits)).reshape(8, 20)


def cell_boxes(width, height, rectangle):
    x0, y0, x1, y1 = rectangle
    xs = [round(width * (x0 + (x1 - x0) * i / 20)) for i in range(21)]
    ys = [round(height * (y0 + (y1 - y0) * i / 8)) for i in range(9)]
    return [(xs[x], ys[y], xs[x + 1], ys[y + 1])
            for y in range(8) for x in range(20)]


def render(frame_id, tag, width, height, frame_count, signal_label="SDR"):
    image = Image.new("RGB", (width, height), (32, 32, 32))
    draw = ImageDraw.Draw(image)
    bits = bits_for(frame_id, tag).ravel()
    for rectangle in GEOMETRY["rectangles"]:
        for box, bit in zip(cell_boxes(width, height, rectangle), bits):
            x0, y0, x1, y1 = box
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill="white" if bit else "black")
    try:
        font = ImageFont.truetype("consola.ttf", max(16, height // 24))
    except OSError:
        font = ImageFont.load_default(size=max(16, height // 24))
    draw.text((width * .10, height * .28), f"FRAME {frame_id:08d} / {frame_count:08d}",
              fill="white", font=font)
    draw.text((width * .10, height * .59), f"{signal_label} PACING  tag {tag:04X}",
              fill="white", font=font)
    x = round((width - 1) * (frame_id - 1) / max(1, frame_count - 1))
    draw.rectangle((x, round(height * .93), min(width - 1, x + width // 100),
                    height - 1), fill=(200, 200, 200))
    return image


def decode(gray, tag):
    """Reject ambiguous cells, CRC errors, mixed copies and foreign clips."""
    height, width = gray.shape
    copies = []
    for rectangle in GEOMETRY["rectangles"]:
        levels, spreads = [], []
        for x0, y0, x1, y1 in cell_boxes(width, height, rectangle):
            dx, dy = (x1 - x0) // 4, (y1 - y0) // 4
            patch = gray[y0 + dy:y1 - dy, x0 + dx:x1 - dx]
            if patch.size < 4:
                copies.append({"error": "marker_too_small"})
                break
            lo, med, hi = np.percentile(patch, [10, 50, 90])
            levels.append(med)
            spreads.append(hi - lo)
        else:
            low, high = np.percentile(levels, [10, 90])
            contrast = high - low
            if contrast < 90:
                copies.append({"error": "low_contrast"})
                continue
            normalized = (np.array(levels) - low) / contrast
            if (np.any((normalized > .25) & (normalized < .75))
                    or max(spreads) > contrast * .45):
                copies.append({"error": "ambiguous_or_mixed_cells"})
                continue
            bits = (normalized > .5).astype(np.uint8).reshape(-1, 2)
            if np.any(bits[:, 0] == bits[:, 1]):
                copies.append({"error": "complement_failure"})
                continue
            packet = np.packbits(bits[:, 0]).tobytes()
            sync, found_tag, frame_id, crc = struct.unpack(">HHIH", packet)
            if sync != 0xD391 or binascii.crc_hqx(packet[:8], 0xffff) != crc:
                copies.append({"error": "sync_or_crc_failure"})
            elif found_tag != tag:
                copies.append({"error": "foreign_clip"})
            else:
                copies.append({"id": frame_id})
    ids = [copy.get("id") for copy in copies]
    if len(ids) == 3 and None not in ids and len(set(ids)) == 1:
        return {"id": ids[0], "decode_status": "valid", "copies": copies}
    return {"id": None, "decode_status": "invalid_or_mixed", "copies": copies}
