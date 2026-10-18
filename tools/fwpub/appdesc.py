"""An ESP32 application image: the app descriptor (esp_app_desc_t at image
offset 32) and the integrity the bootloader checks (the XOR checksum that
closes the segments and the SHA-256 appended after it). Ported from the
firmware bench's tools/testbench/lib/fw_image.py, which `esptool
image-info` agrees with. Stdlib only.

    parse(data)  -> dict: version, project_name, idf_ver, build_date,
                    build_time, elf_sha256, chip_id, size, sha256,
                    has_descriptor, checksum_ok, digest_ok, trailer_ok
    verify(data) -> bool: the descriptor is present and the integrity holds
"""

from __future__ import annotations

import hashlib
import struct

IMAGE_MAGIC = 0xE9
DESC_MAGIC = 0xABCD5432
HEADER_LEN = 24
SEG_HDR_LEN = 8
DESC_OFF = HEADER_LEN + SEG_HDR_LEN      # 32: the descriptor opens segment 0
VERSION_OFF = DESC_OFF + 16              # 48, 32 bytes
PROJECT_OFF = DESC_OFF + 48              # 80, 32 bytes
TIME_OFF = DESC_OFF + 80                 # 16 bytes
DATE_OFF = DESC_OFF + 96                 # 16 bytes
IDF_OFF = DESC_OFF + 112                 # 32 bytes
ELF_SHA_OFF = DESC_OFF + 144             # 32 bytes
CHECKSUM_SEED = 0xEF


class ImageError(ValueError):
    pass


def _layout(data):
    """(segments as [(hdr_off, data_off, data_len)], checksum_off, hash_appended)."""
    if len(data) < HEADER_LEN or data[0] != IMAGE_MAGIC:
        raise ImageError("not an application image (magic)")
    seg_count = data[1]
    hash_appended = data[23] == 1
    pos = HEADER_LEN
    segs = []
    for _ in range(seg_count):
        if pos + SEG_HDR_LEN > len(data):
            raise ImageError("segment header past the end")
        _addr, length = struct.unpack_from("<II", data, pos)
        segs.append((pos, pos + SEG_HDR_LEN, length))
        pos += SEG_HDR_LEN + length
        if pos > len(data):
            raise ImageError("segment data past the end")
    # the checksum byte closes a 16-byte aligned block
    checksum_off = (pos + 16) // 16 * 16 - 1 if (pos % 16) != 15 else pos
    if checksum_off >= len(data):
        raise ImageError("checksum position past the end")
    return segs, checksum_off, hash_appended


def _checksum(data, segs):
    c = CHECKSUM_SEED
    for _hdr, off, length in segs:
        for b in data[off:off + length]:
            c ^= b
    return c


def _field(data, off, length):
    raw = data[off:off + length]
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def parse(data):
    """The descriptor and the integrity verdicts of one image. Raises
    ImageError when the bytes are not an application image at all."""
    segs, csum_off, hash_appended = _layout(data)
    chip_id = struct.unpack_from("<H", data, 12)[0]
    has_desc = len(data) >= DESC_OFF + 256 and \
        struct.unpack_from("<I", data, DESC_OFF)[0] == DESC_MAGIC
    out = {
        "size": len(data),
        "segments": len(segs),
        "chip_id": chip_id,
        "has_descriptor": has_desc,
        "version": _field(data, VERSION_OFF, 32) if has_desc else "",
        "project_name": _field(data, PROJECT_OFF, 32) if has_desc else "",
        "build_time": _field(data, TIME_OFF, 16) if has_desc else "",
        "build_date": _field(data, DATE_OFF, 16) if has_desc else "",
        "idf_ver": _field(data, IDF_OFF, 32) if has_desc else "",
        "elf_sha256": data[ELF_SHA_OFF:ELF_SHA_OFF + 32].hex() if has_desc else "",
        "hash_appended": hash_appended,
        "sha256": sha256_hex(data),
    }
    out["checksum_ok"] = data[csum_off] == _checksum(data, segs)
    if hash_appended:
        body = data[:csum_off + 1]
        out["digest_ok"] = data[csum_off + 1:csum_off + 33] == hashlib.sha256(body).digest()
        out["trailer_ok"] = len(data) == csum_off + 33
    else:
        out["digest_ok"] = None
        out["trailer_ok"] = len(data) == csum_off + 1
    return out


def verify(data):
    """True when the image carries a descriptor and its checksum, appended
    digest (when present) and length all hold."""
    try:
        i = parse(data)
    except ImageError:
        return False
    return bool(i["has_descriptor"] and i["checksum_ok"] and
                (i["digest_ok"] is None or i["digest_ok"]) and i["trailer_ok"])


def self_test():
    # a two-segment image with a descriptor, checksum and appended digest,
    # built here so the walk is exercised without a real binary
    seg0 = bytearray(256)
    struct.pack_into("<I", seg0, 0, DESC_MAGIC)
    seg0[16:16 + 7] = b"1.2.3\0\0"
    seg0[48:48 + 6] = b"demo\0\0"
    seg0[112:112 + 7] = b"v6.0.2\0"
    seg1 = bytes(range(40))
    body = bytearray()
    body += bytes([IMAGE_MAGIC, 2, 0, 0]) + b"\0" * 19 + b"\1"      # hash appended
    body += struct.pack("<II", 0x3C000000, len(seg0)) + seg0
    body += struct.pack("<II", 0x3FC00000, len(seg1)) + seg1
    pos = len(body)
    csum_off = (pos + 16) // 16 * 16 - 1 if (pos % 16) != 15 else pos
    body += b"\0" * (csum_off - pos)
    c = CHECKSUM_SEED
    for b in seg0:
        c ^= b
    for b in seg1:
        c ^= b
    body += bytes([c])
    body += hashlib.sha256(body).digest()
    info = parse(bytes(body))
    assert info["version"] == "1.2.3" and info["project_name"] == "demo", info
    assert info["idf_ver"] == "v6.0.2" and info["segments"] == 2, info
    assert info["checksum_ok"] and info["digest_ok"] and info["trailer_ok"], info
    assert verify(bytes(body))
    bad = bytearray(body)
    bad[40] ^= 1
    assert not verify(bytes(bad))
    assert not verify(b"\x00" * 64)
    return 1


if __name__ == "__main__":
    self_test()
    print("appdesc: OK")
