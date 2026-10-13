"""Deterministic original Household Voice brand icon; no third-party artwork."""
import struct
import zlib


def icon_png():
    size = 256
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            # A simple house with a voice waveform, drawn from original geometry.
            corner_x = max(28 - x, 0, x - 227)
            corner_y = max(28 - y, 0, y - 227)
            inside = corner_x * corner_x + corner_y * corner_y <= 28 * 28
            color = (17, 34, 55, 255) if inside else (0, 0, 0, 0)
            roof = 48 <= y <= 116 and abs(x - 128) <= (y - 48) * 1.12
            house = 76 <= x <= 180 and 107 <= y <= 208
            if roof or house:
                color = (236, 247, 250, 255)
            for center, top, bottom in ((105, 137, 177), (128, 122, 192), (151, 137, 177)):
                if abs(x - center) <= 6 and top <= y <= bottom:
                    color = (0, 135, 153, 255)
            rows.extend(color)
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + chunk(b'IEND', b''))


if __name__ == '__main__':
    from pathlib import Path
    target = Path(__file__).resolve().parents[1] / 'custom_components/household_voice/brand/icon.png'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(icon_png())
