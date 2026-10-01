from __future__ import annotations

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_RE = re.compile(r"([A-Z]+)(\d+)")


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _coord(rc: str) -> tuple[int, int]:
    m = _CELL_RE.match(rc or "")
    return (int(m.group(2)), _col_number(m.group(1))) if m else (0, 0)


def _xlsx_index_from_bytes(data: bytes):
    zf = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{{{_NS_MAIN}}}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")))
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.attrib["Id"]: r.attrib["Target"] for r in rels.findall(f"{{{_NS_PKG_REL}}}Relationship")}
    sheets: dict[str, str] = {}
    sheet_root = wb.find(f"{{{_NS_MAIN}}}sheets")
    if sheet_root is not None:
        for sh in sheet_root:
            name = sh.attrib.get("name", "")
            rid = sh.attrib.get(f"{{{_NS_REL}}}id")
            target = relmap.get(rid, "")
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            sheets[name] = target
    return zf, shared, sheets


def _cell_value(c, shared: list[str], data_only: bool) -> Any:
    typ = c.attrib.get("t")
    f_el = c.find(f"{{{_NS_MAIN}}}f")
    if not data_only and f_el is not None:
        return "=" + (f_el.text or "")
    v_el = c.find(f"{{{_NS_MAIN}}}v")
    if typ == "inlineStr":
        is_el = c.find(f"{{{_NS_MAIN}}}is")
        return "".join(t.text or "" for t in is_el.iter(f"{{{_NS_MAIN}}}t")) if is_el is not None else ""
    if v_el is None:
        return None
    raw = v_el.text or ""
    if typ == "s":
        try:
            return shared[int(raw)]
        except Exception:
            return raw
    if typ in {"str", "e"}:
        return raw
    if typ == "b":
        return raw == "1"
    try:
        num = float(raw)
        return int(num) if num.is_integer() else num
    except Exception:
        return raw


def excel_sheet_names(data: bytes, filename: str) -> list[str]:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return ["CSV"]
    if ext in {".xlsx", ".xlsm"}:
        zf, _, sheets = _xlsx_index_from_bytes(data)
        try:
            return list(sheets.keys())
        finally:
            zf.close()
    if ext == ".xls":
        book = pd.ExcelFile(io.BytesIO(data), engine="xlrd")
        return book.sheet_names
    raise ValueError(f"Format {ext} belum didukung")


def _read_csv(data: bytes, max_rows: int | None = None) -> pd.DataFrame:
    decoded = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            decoded = data.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if decoded is None:
        raise ValueError("Encoding CSV tidak dikenali")
    sample = decoded[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        sep = dialect.delimiter
    except Exception:
        sep = ","
    return pd.read_csv(io.StringIO(decoded), header=None, sep=sep, nrows=max_rows)


def read_raw_sheet(data: bytes, filename: str, sheet_name: str, data_only: bool = True, max_rows: int | None = None) -> pd.DataFrame:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return _read_csv(data, max_rows=max_rows)
    if ext in {".xlsx", ".xlsm"}:
        zf, shared, sheets = _xlsx_index_from_bytes(data)
        try:
            xml_path = sheets.get(sheet_name)
            if not xml_path:
                raise KeyError(f"Sheet '{sheet_name}' tidak ditemukan")
            root = ET.fromstring(zf.read(xml_path))
            sparse: dict[tuple[int, int], Any] = {}
            max_r = 0
            max_c = 0
            for c in root.iter(f"{{{_NS_MAIN}}}c"):
                r, col = _coord(c.attrib.get("r", ""))
                if not r:
                    continue
                if max_rows and r > max_rows:
                    continue
                max_r = max(max_r, r)
                max_c = max(max_c, col)
                sparse[(r, col)] = _cell_value(c, shared, data_only)
            if max_rows:
                max_r = min(max_r, max_rows)
            rows = [[sparse.get((r, c)) for c in range(1, max_c + 1)] for r in range(1, max_r + 1)]
            return pd.DataFrame(rows)
        finally:
            zf.close()
    if ext == ".xls":
        return pd.read_excel(io.BytesIO(data), sheet_name=sheet_name, header=None, engine="xlrd", nrows=max_rows)
    raise ValueError(f"Format {ext} belum didukung")


def guess_header_row(raw: pd.DataFrame, scan_rows: int = 20) -> int:
    if raw.empty:
        return 1
    best_idx = 0
    best_score = float("-inf")
    for idx in range(min(scan_rows, len(raw))):
        vals = [v for v in raw.iloc[idx].tolist() if not pd.isna(v) and str(v).strip()]
        if not vals:
            continue
        non_empty = len(vals)
        strings = sum(isinstance(v, str) for v in vals)
        unique = len({str(v).strip().lower() for v in vals})
        numeric = sum(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
        score = non_empty * 2 + strings * 1.2 + unique * .4 - numeric * .15
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx + 1


def _header(value: Any, idx: int) -> str:
    try:
        if value is None or pd.isna(value):
            return f"Column_{idx}"
    except Exception:
        if value is None:
            return f"Column_{idx}"
    text = str(value).strip()
    return text or f"Column_{idx}"


def make_unique_headers(headers: list[Any]) -> list[str]:
    used: dict[str, int] = {}
    result: list[str] = []
    for idx, value in enumerate(headers, start=1):
        base = _header(value, idx)
        if base == "_row_id":
            base = "_row_id_data"
        if base not in used:
            used[base] = 1
            result.append(base)
        else:
            used[base] += 1
            result.append(f"{base}_{used[base]}")
    return result


def build_dataframe(raw: pd.DataFrame, header_row: int, fill_down: bool = False, drop_empty_columns: bool = True, include_excel_row: bool = False) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    header_idx = max(0, min(int(header_row) - 1, len(raw) - 1))
    headers = make_unique_headers(raw.iloc[header_idx].tolist())
    df = raw.iloc[header_idx + 1:].copy()
    # Keep original Excel row number before dropping blank rows.
    excel_rows = pd.Series(df.index + 1, index=df.index)
    df.columns = headers
    non_empty_mask = ~df.isna().all(axis=1)
    df = df.loc[non_empty_mask].copy()
    excel_rows = excel_rows.loc[non_empty_mask]
    if drop_empty_columns:
        df = df[[c for c in df.columns if not df[c].isna().all()]]
    if fill_down:
        df = df.ffill()
    if include_excel_row:
        df.insert(0, "__Excel_Row", excel_rows.astype(int).values)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)
    return df.reset_index(drop=True)


def column_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        s = df[col]
        samples = [str(v)[:60] for v in s.dropna().head(3).tolist()]
        rows.append({
            "Kolom": col,
            "Terisi": int(s.notna().sum()),
            "Kosong %": round(float(s.isna().mean() * 100), 1),
            "Tipe": str(s.dtype),
            "Contoh": " | ".join(samples),
        })
    return pd.DataFrame(rows)
