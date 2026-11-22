"""Check the ABI and ELF layout of every native library in a Field APK/AAB."""

import argparse
import struct
from pathlib import Path
from zipfile import ZipFile

PAGE_SIZE = 16384


def verify_library(data: bytes, name: str) -> None:
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
        raise ValueError(f"{name}: expected little-endian AArch64 ELF64")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    loads = 0
    for index in range(phnum):
        kind, _, offset, vaddr, _, _, _, align = struct.unpack_from(
            "<II6Q", data, phoff + index * phentsize
        )
        if kind == 1:  # PT_LOAD
            loads += 1
            if align < PAGE_SIZE or offset % PAGE_SIZE != vaddr % PAGE_SIZE:
                raise ValueError(f"{name}: segment {index} incompatible with 16 KiB pages")
    if loads == 0:
        raise ValueError(f"{name}: no loadable segments")


def verify_package(path: Path) -> None:
    with ZipFile(path) as package:
        prefix = "base/lib/" if path.suffix == ".aab" else "lib/"
        libraries = [name for name in package.namelist() if name.startswith(prefix) and name.endswith(".so")]
        if prefix + "arm64-v8a/libori_android_field.so" not in libraries:
            raise ValueError(f"{path.name}: missing Field JNI library")
        for name in libraries:
            if not name.startswith(prefix + "arm64-v8a/"):
                raise ValueError(f"{path.name}: unexpected ABI in {name}")
            verify_library(package.read(name), name)
            print(f"{path.name}  {name}  AArch64, 16 KiB PT_LOAD")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", type=Path, nargs="+")
    for package_path in parser.parse_args().packages:
        verify_package(package_path)
