"""The serial flash set: a zip with the bootloader, the partition table,
the OTA data, the image under its release name, a flat `flash_args.txt`
and a README with the esptool line. Built from an ESP-IDF build directory
(its `flash_args` names the offsets). Stdlib only.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile


class FlashSetError(ValueError):
    pass


def parse_flash_args(text):
    """(options, [(offset, path)]) from an IDF `flash_args` file."""
    tokens = text.split()
    options = []
    pairs = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            options.append(t)
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--") and not tokens[i + 1].startswith("0x"):
                options.append(tokens[i + 1])
                i += 1
        elif t.startswith("0x") and i + 1 < len(tokens):
            pairs.append((t, tokens[i + 1]))
            i += 1
        i += 1
    return options, pairs


def build(build_dir, image_bytes, image_name, chip="esp32s3"):
    """The zip bytes and the sha256 of the bootloader inside it."""
    args_path = os.path.join(build_dir, "flash_args")
    if not os.path.isfile(args_path):
        raise FlashSetError("no flash_args in " + build_dir)
    with open(args_path, "r", encoding="utf-8") as f:
        options, pairs = parse_flash_args(f.read())
    if not pairs:
        raise FlashSetError("flash_args names no files")

    members = []  # (zip name, bytes)
    flat_pairs = []
    bootloader_sha = None
    for offset, rel in pairs:
        name = os.path.basename(rel)
        src = os.path.join(build_dir, rel)
        if name == os.path.basename(image_name) or rel.endswith(".bin") and _is_app(rel, pairs):
            data = image_bytes
            name = image_name
        else:
            if not os.path.isfile(src):
                raise FlashSetError("%s is missing from the build" % rel)
            with open(src, "rb") as f:
                data = f.read()
        if name == "bootloader.bin":
            bootloader_sha = hashlib.sha256(data).hexdigest()
        members.append((name, data))
        flat_pairs.append((offset, name))

    flat_args = " ".join(options + [x for p in flat_pairs for x in p]) + "\n"
    readme = ("Serial flash set for %s\n\nWith the device on USB (hold the boot button if it does not enter the "
              "download mode by itself):\n\n  python -m esptool --chip %s --port COMx --baud 460800 write_flash %s\n\n"
              "The same offsets are in flash_args.txt. A unit updated over the air keeps its bootloader; this set is "
              "for a fresh flash or a unit that cannot update over the air.\n"
              % (image_name, chip, " ".join(options + [x for p in flat_pairs for x in p])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
        z.writestr("flash_args.txt", flat_args)
        z.writestr("README.txt", readme)
    return buf.getvalue(), bootloader_sha


def _is_app(rel, pairs):
    """The app image is the last .bin of flash_args that is not one of the
    three fixed parts."""
    fixed = ("bootloader", "partition-table", "partition_table", "ota_data_initial")
    base = os.path.basename(rel)
    if any(base.startswith(f) for f in fixed):
        return False
    bins = [p for _o, p in pairs if p.endswith(".bin") and not any(os.path.basename(p).startswith(f) for f in fixed)]
    return bool(bins) and rel == bins[-1]


def self_test():
    opts, pairs = parse_flash_args("--flash-mode dio --flash-freq 80m --flash-size 16MB 0x0 bootloader/bootloader.bin "
                                   "0x8000 partition_table/partition-table.bin 0xd000 ota_data_initial.bin 0x10000 app.bin")
    assert opts == ["--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "16MB"], opts
    assert pairs == [("0x0", "bootloader/bootloader.bin"), ("0x8000", "partition_table/partition-table.bin"),
                     ("0xd000", "ota_data_initial.bin"), ("0x10000", "app.bin")], pairs
    assert _is_app("app.bin", pairs) and not _is_app("bootloader/bootloader.bin", pairs)
    return 1


if __name__ == "__main__":
    self_test()
    print("flashset: OK")
