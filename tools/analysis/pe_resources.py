"""Разбор ресурсов PE-файла и бинарных форм Delphi (DFM).

Инструмент предназначен для проверяемого воспроизведения инвентаря
возможностей коммерческого комплекса: он извлекает перечень ресурсов
исполняемого модуля и состав компонентов его форм — то есть фактическую
поверхность интерфейса, а не документацию.

Зависимостей нет: только стандартная библиотека.

Примеры:

    python3 tools/analysis/pe_resources.py --exe InspectorRT.exe --list
    python3 tools/analysis/pe_resources.py --exe InspectorRT.exe --forms
    python3 tools/analysis/pe_resources.py --exe InspectorRT.exe --form TFRSIGNALSLIST
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass, field
from pathlib import Path

RT_NAMES = {
    1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG", 6: "STRING",
    7: "FONTDIR", 8: "FONT", 9: "ACCELERATOR", 10: "RCDATA", 11: "MESSAGETABLE",
    12: "GROUP_CURSOR", 14: "GROUP_ICON", 16: "VERSION", 24: "MANIFEST",
}


class PEError(RuntimeError):
    """Файл не является корректным PE-образом."""


@dataclass
class Resource:
    """Запись дерева ресурсов PE."""

    type_name: str
    name: str
    rva: int
    size: int


class PEFile:
    """Чтение заголовков PE и дерева ресурсов."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:2] != b"MZ":
            raise PEError(f"{self.path.name}: нет сигнатуры MZ")
        pe_off = struct.unpack_from("<I", self.data, 0x3C)[0]
        if self.data[pe_off:pe_off + 4] != b"PE\0\0":
            raise PEError(f"{self.path.name}: нет сигнатуры PE")
        coff = pe_off + 4
        self.num_sections = struct.unpack_from("<H", self.data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", self.data, coff + 16)[0]
        opt = coff + 20
        self.pe32plus = struct.unpack_from("<H", self.data, opt)[0] == 0x20B
        dd = opt + (112 if self.pe32plus else 96)
        self.res_rva, self.res_size = struct.unpack_from("<II", self.data, dd + 16)
        self.sections: list[tuple[int, int, int, int]] = []
        sec = opt + opt_size
        for i in range(self.num_sections):
            off = sec + i * 40
            vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", self.data, off + 8)
            self.sections.append((vaddr, vsize, raddr, rsize))

    def rva_to_offset(self, rva: int) -> int:
        """Смещение в файле для виртуального адреса."""
        for vaddr, vsize, raddr, rsize in self.sections:
            if vaddr <= rva < vaddr + max(vsize, rsize):
                return raddr + (rva - vaddr)
        raise PEError(f"RVA {rva:#x} вне секций")

    def resources(self) -> list[Resource]:
        """Плоский перечень ресурсов: тип, имя, адрес и размер."""
        base = self.rva_to_offset(self.res_rva)
        out: list[Resource] = []

        def name_of(raw: int) -> int | str:
            if raw & 0x80000000:
                off = base + (raw & 0x7FFFFFFF)
                n = struct.unpack_from("<H", self.data, off)[0]
                return self.data[off + 2:off + 2 + n * 2].decode("utf-16-le", "replace")
            return raw

        def walk(dir_off: int, level: int, ctx: tuple[str, str]) -> None:
            n_named, n_id = struct.unpack_from("<HH", self.data, dir_off + 12)
            for i in range(n_named + n_id):
                entry = dir_off + 16 + i * 8
                raw_name, offset = struct.unpack_from("<II", self.data, entry)
                nm = name_of(raw_name)
                if level == 0:
                    ctx = (RT_NAMES.get(nm, str(nm)), "")
                elif level == 1:
                    ctx = (ctx[0], str(nm))
                if offset & 0x80000000:
                    walk(base + (offset & 0x7FFFFFFF), level + 1, ctx)
                else:
                    rva, size = struct.unpack_from("<II", self.data, base + offset)
                    out.append(Resource(ctx[0], ctx[1], rva, size))

        walk(base, 0, ("", ""))
        return out

    def resource_data(self, resource: Resource) -> bytes:
        """Содержимое ресурса."""
        off = self.rva_to_offset(resource.rva)
        return self.data[off:off + resource.size]


@dataclass
class DfmComponent:
    """Компонент формы с опубликованными свойствами."""

    cls: str
    name: str
    props: list[tuple[str, str]] = field(default_factory=list)


def parse_dfm(blob: bytes) -> list[DfmComponent]:
    """Разбор бинарного DFM в плоский список компонентов.

    Поддерживаются значения, встречающиеся в формах Delphi 2009 и новее:
    строки с однобайтовой длиной, UTF-8 с четырёхбайтовой длиной, числа,
    логические значения, наборы, двоичные данные и коллекции.
    """
    if blob[:4] != b"TPF0":
        return []
    pos = 4

    def read_str() -> str:
        nonlocal pos
        n = blob[pos]
        pos += 1
        value = blob[pos:pos + n].decode("cp1251", "replace")
        pos += n
        return value

    def read_value() -> str:
        nonlocal pos
        t = blob[pos]
        pos += 1
        if t == 0x02:
            value = struct.unpack_from("<b", blob, pos)[0]; pos += 1; return str(value)
        if t == 0x03:
            value = struct.unpack_from("<h", blob, pos)[0]; pos += 2; return str(value)
        if t == 0x04:
            value = struct.unpack_from("<i", blob, pos)[0]; pos += 4; return str(value)
        if t == 0x05:
            pos += 10; return "<real>"
        if t in (0x06, 0x07):
            return read_str()
        if t == 0x08:
            return "False"
        if t == 0x09:
            return "True"
        if t == 0x0A:
            n = struct.unpack_from("<I", blob, pos)[0]; pos += 4 + n; return f"<bin {n}>"
        if t == 0x0B:
            items = []
            while blob[pos] != 0:
                items.append(read_str())
            pos += 1
            return "[" + ",".join(items) + "]"
        if t == 0x0C:
            n = struct.unpack_from("<I", blob, pos)[0]; pos += 4
            value = blob[pos:pos + n].decode("cp1251", "replace"); pos += n
            return value
        if t == 0x0D:
            return "<nil>"
        if t == 0x0E:
            return read_collection()
        if t == 0x0F:
            pos += 4; return "<single>"
        if t == 0x10:
            pos += 8; return "<currency>"
        if t == 0x11:
            n = struct.unpack_from("<I", blob, pos)[0]; pos += 4
            value = blob[pos:pos + n].decode("utf-8", "replace"); pos += n
            return value
        if t == 0x12:
            pos += 8; return "<double>"
        if t == 0x14:
            n = struct.unpack_from("<I", blob, pos)[0]; pos += 4
            value = blob[pos:pos + n].decode("utf-8", "replace"); pos += n
            return value
        return f"<type {t:#04x}>"

    def read_collection() -> str:
        nonlocal pos
        items = []
        while pos < len(blob) and blob[pos] != 0x00:
            pos += 1
            if pos >= len(blob) or blob[pos] == 0x00:
                break
            name = read_str()
            props = []
            while pos < len(blob):
                if blob[pos] == 0x00:
                    pos += 1
                    break
                pname = read_str()
                if not pname:
                    continue
                pval = read_value()
                if pval not in ("", "False", "0", "<nil>"):
                    props.append(f"{pname}={pval[:60]}")
            items.append(f"{name}({'; '.join(props[:4])})")
        if pos < len(blob):
            pos += 1
        return "<coll: " + "; ".join(items[:12]) + ">"

    result: list[DfmComponent] = []

    def read_object() -> None:
        nonlocal pos
        while pos < len(blob):
            if blob[pos] == 0x00:
                pos += 1
                return
            cls = read_str()
            name = read_str()
            props: list[tuple[str, str]] = []
            while pos < len(blob):
                if blob[pos] == 0x00:
                    pos += 1
                    break
                pname = read_str()
                if not pname:
                    continue
                pval = read_value()
                if pval not in ("", "False", "0"):
                    props.append((pname, pval[:160]))
            result.append(DfmComponent(cls, name, props))
            if pos < len(blob) and blob[pos] == 0x01:
                pos += 1
                while pos < len(blob) and blob[pos] != 0x00:
                    read_object()
                if pos < len(blob):
                    pos += 1

    try:
        read_object()
    except IndexError:
        # Часть форм содержит значения, не описанные в формате DFM
        # (например, крупные двоичные поля). Возвращаем разобранное.
        pass
    return result


def find_forms(pe: PEFile) -> list[tuple[str, list[DfmComponent]]]:
    """Формы из ресурсов RCDATA с разобранным DFM."""
    forms = []
    for res in pe.resources():
        if res.type_name != "RCDATA":
            continue
        blob = pe.resource_data(res)
        if blob[:4] != b"TPF0":
            continue
        forms.append((res.name, parse_dfm(blob)))
    return forms


def main() -> None:
    parser = argparse.ArgumentParser(description="Ресурсы PE и формы Delphi")
    parser.add_argument("--exe", required=True, help="путь к исполняемому модулю")
    parser.add_argument("--list", action="store_true", help="перечень ресурсов")
    parser.add_argument("--forms", action="store_true", help="перечень форм RCDATA")
    parser.add_argument("--form", help="компоненты одной формы по имени ресурса")
    args = parser.parse_args()

    pe = PEFile(args.exe)
    if args.list:
        for res in pe.resources():
            print(f"{res.type_name:<14} {res.name:<32} {res.size:>10}")
        return
    if args.forms:
        for name, components in find_forms(pe):
            print(f"{name:<36} компонентов: {len(components)}")
        return
    if args.form:
        for name, components in find_forms(pe):
            if name.upper() != args.form.upper():
                continue
            print(f"=== {name} ===")
            for comp in components:
                print(f"  {comp.cls} {comp.name}")
                for pname, pval in comp.props:
                    print(f"      {pname} = {pval}")
            return
        print(f"форма {args.form} не найдена")
        return
    parser.print_help()


if __name__ == "__main__":
    main()
