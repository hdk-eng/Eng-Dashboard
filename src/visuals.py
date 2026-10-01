from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import re
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd
from openpyxl.utils.datetime import from_excel



_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_RE = re.compile(r"([A-Z]+)(\d+)")


class _CellValue:
    def __init__(self, value: Any):
        self.value = value


class FastSheet:
    """Tiny worksheet adapter backed by XLSX XML values only; ignores drawings/styles/images."""
    def __init__(self, title: str, values: dict[tuple[int,int],Any], max_row: int, max_col: int):
        self.title=title; self._values=values; self.max_row=max_row; self.max_column=max_col
    def cell(self, row: int, column: int) -> _CellValue:
        return _CellValue(self._values.get((row,column)))


def _col_number(letters: str) -> int:
    n=0
    for ch in letters:
        n=n*26+(ord(ch)-64)
    return n


def _coord(rc: str) -> tuple[int,int]:
    m=_CELL_RE.match(rc or "")
    if not m: return 0,0
    return int(m.group(2)), _col_number(m.group(1))


def _xlsx_index(path: str | Path):
    zf=zipfile.ZipFile(path)
    shared=[]
    if "xl/sharedStrings.xml" in zf.namelist():
        root=ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{{{_NS_MAIN}}}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")))
    wb=ET.fromstring(zf.read("xl/workbook.xml"))
    rels=ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relmap={r.attrib["Id"]:r.attrib["Target"] for r in rels.findall(f"{{{_NS_PKG_REL}}}Relationship")}
    sheets={}
    for sh in wb.find(f"{{{_NS_MAIN}}}sheets"):
        name=sh.attrib.get("name","")
        rid=sh.attrib.get(f"{{{_NS_REL}}}id")
        target=relmap.get(rid,"")
        if target.startswith("/"): target=target.lstrip("/")
        elif not target.startswith("xl/"): target="xl/"+target
        sheets[name]=target
    return zf,shared,sheets


def _fast_sheet(zf: zipfile.ZipFile, shared: list[str], title: str, xml_path: str) -> FastSheet:
    values={}; maxr=0; maxc=0
    root=ET.fromstring(zf.read(xml_path))
    for c in root.iter(f"{{{_NS_MAIN}}}c"):
        r,cidx=_coord(c.attrib.get("r",""))
        if not r: continue
        maxr=max(maxr,r); maxc=max(maxc,cidx)
        typ=c.attrib.get("t")
        v_el=c.find(f"{{{_NS_MAIN}}}v")
        val=None
        if typ=="inlineStr":
            is_el=c.find(f"{{{_NS_MAIN}}}is")
            if is_el is not None: val="".join(t.text or "" for t in is_el.iter(f"{{{_NS_MAIN}}}t"))
        elif v_el is not None:
            raw=v_el.text or ""
            if typ=="s":
                try: val=shared[int(raw)]
                except Exception: val=raw
            elif typ in {"str","e"}: val=raw
            elif typ=="b": val=(raw=="1")
            else:
                try:
                    num=float(raw); val=int(num) if num.is_integer() else num
                except Exception: val=raw
        values[(r,cidx)]=val
    return FastSheet(title,values,maxr,maxc)

STATUS_NORMALIZE = {
    "closed": "Close", "close": "Close", "approved": "Approved", "a": "Approved",
    "open": "Open", "coordination": "Coordination", "review": "Review",
    "revision": "Revision", "revised": "Revision", "submit": "Submit",
    "process": "Process", "unstarted": "Unstarted", "done": "Done",
    "incomplete": "Incomplete",
}


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _date(v: Any) -> pd.Timestamp | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return pd.Timestamp(v)
    if isinstance(v, (int, float)):
        try:
            return pd.Timestamp(from_excel(v))
        except Exception:
            pass
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return pd.Timestamp(ts)
    except Exception:
        return None


def _date_iso(v: Any) -> str:
    """Return ISO date or blank; never expose pandas NaT to the UI/database."""
    ts = _date(v)
    if ts is None or pd.isna(ts):
        return ""
    try:
        return pd.Timestamp(ts).date().isoformat()
    except Exception:
        return ""


def _status(v: Any) -> str:
    t = _text(v)
    return STATUS_NORMALIZE.get(t.lower(), t or "Unknown")


def classify_sheet(filename: str, sheet_name: str) -> str:
    f = filename.lower()
    f_norm = re.sub(r"[_\-]+", " ", f)
    s = sheet_name.strip().lower()
    if "kurva" in f_norm or "kurva" in s:
        return "s_curve"
    if ("2 ming" in f_norm or "2 minggu" in f_norm or "lookahead" in f_norm or "rencana kerja" in f_norm
            or "2 ming" in s or "2 minggu" in s or "minggu lalu" in s or "minggu ke depan" in s):
        return "lookahead"
    if s == "dashboard" and ("deliverable" in f or "engineering" in f or "bim" in f):
        return "engineering_dashboard"
    if "model progress" in s:
        return "model_progress"
    if "clash" in s or "issue" in s:
        return "clash_issue"
    if s == "rfi" or "request for information" in s:
        return "rfi"
    if s == "si" or "site instruction" in s:
        return "si"
    if "shopdrawing" in s.replace(" ", "") or "shop drawing" in s:
        return "shopdrawing"
    if s == "apm" or "approval material" in s:
        return "apm"
    if s == "wms" or "method statement" in s:
        return "wms"
    if s in {"sheet1", "master", "reference", "referensi"} and ("deliverable" in f or "engineering" in f):
        return "reference"
    return "generic"


def workbook_quality(path: str | Path, selected_sheets: list[str] | None = None) -> dict[str, Any]:
    """Fast XML quality scan. Does not load images/styles and therefore stays responsive on large BIM workbooks."""
    zf, shared, sheets = _xlsx_index(path)
    names=selected_sheets or list(sheets.keys())
    errors=[]; tokens=("#REF!","#DIV/0!","#VALUE!","#NAME?","#N/A")
    try:
        for name in names:
            xml_path=sheets.get(name)
            if not xml_path: continue
            root=ET.fromstring(zf.read(xml_path))
            for c in root.iter(f"{{{_NS_MAIN}}}c"):
                coord=c.attrib.get("r","")
                typ=c.attrib.get("t")
                v=c.find(f"{{{_NS_MAIN}}}v")
                f=c.find(f"{{{_NS_MAIN}}}f")
                candidates=[]
                if typ=="e" and v is not None: candidates.append(v.text or "")
                if f is not None: candidates.append(f.text or "")
                for value in candidates:
                    if any(tok in value for tok in tokens):
                        errors.append({"sheet":name,"cell":coord,"value":value[:120]})
                        break
                if len(errors)>=200: break
            if len(errors)>=200: break
    finally:
        zf.close()
    return {"error_count":len(errors),"errors":errors,"sheet_count":len(names)}


def _find_row(ws, label: str, col: int = 1) -> int | None:
    needle = label.strip().upper()
    for r in range(1, ws.max_row + 1):
        if _text(ws.cell(r, col).value).upper() == needle:
            return r
    return None


def parse_s_curve(ws) -> dict[str, Any]:
    week_row = None
    for r in range(1, min(ws.max_row, 15) + 1):
        vals = [_text(ws.cell(r, c).value) for c in range(1, min(ws.max_column, 70) + 1)]
        if sum(v.startswith("W") and v[1:].isdigit() for v in vals) >= 8:
            week_row = r
            break
    if not week_row:
        return {"kind": "s_curve", "title": ws.title, "error": "Baris minggu W1.. tidak ditemukan"}

    week_cols: list[int] = []
    weeks: list[str] = []
    for c in range(1, ws.max_column + 1):
        t = _text(ws.cell(week_row, c).value)
        if t.startswith("W") and t[1:].isdigit():
            week_cols.append(c); weeks.append(t)
    date_labels = []
    for c in week_cols:
        v = ws.cell(max(1, week_row-1), c).value
        date_labels.append(_text(v))

    plan_r = _find_row(ws, "KUMULATIF RENCANA BOBOT MINGGUAN (%)")
    actual_r = _find_row(ws, "KUMULATIF REALISASI BOBOT MINGGUAN (%)")
    weekly_plan_r = _find_row(ws, "RENCANA BOBOT MINGGUAN (%)")
    weekly_actual_r = _find_row(ws, "REALISASI BOBOT MINGGUAN (%)")
    dev_r = _find_row(ws, "DEVIASI BOBOT KUMULATIF MINGGUAN (%)")

    def row_values(r: int | None) -> list[float | None]:
        return [_num(ws.cell(r, c).value) if r else None for c in week_cols]

    plan = row_values(plan_r)
    actual = row_values(actual_r)
    weekly_plan = row_values(weekly_plan_r)
    weekly_actual = row_values(weekly_actual_r)
    dev = row_values(dev_r)

    latest_idx = None
    for i, v in enumerate(actual):
        if v is not None:
            latest_idx = i
    if latest_idx is None:
        latest_idx = 0
    latest_actual = actual[latest_idx] if actual else None
    latest_plan = plan[latest_idx] if plan and latest_idx < len(plan) else None
    latest_dev = dev[latest_idx] if dev and latest_idx < len(dev) and dev[latest_idx] is not None else (
        (latest_actual - latest_plan) if latest_actual is not None and latest_plan is not None else None
    )

    rows=[]
    for i, wk in enumerate(weeks):
        rows.append({
            "Week": wk, "Period": date_labels[i] if i < len(date_labels) else "",
            "Plan": plan[i] if i < len(plan) else None,
            "Actual": actual[i] if i < len(actual) else None,
            "Weekly Plan": weekly_plan[i] if i < len(weekly_plan) else None,
            "Weekly Actual": weekly_actual[i] if i < len(weekly_actual) else None,
            "Deviation": dev[i] if i < len(dev) else None,
        })
    curve = pd.DataFrame(rows)

    # WBS level 1 breakdown, before summary rows.
    wbs=[]
    for r in range(week_row + 2, ws.max_row + 1):
        name=_text(ws.cell(r,4).value)
        level=_num(ws.cell(r,3).value)
        weight=_num(ws.cell(r,6).value)
        if name.upper().startswith("RENCANA BOBOT"):
            break
        if name and level == 1 and weight is not None:
            wbs.append({"WBS": _text(ws.cell(r,2).value) or _text(ws.cell(r,1).value), "Pekerjaan": name, "Bobot": weight})
    project_name = _text(ws.cell(2,1).value)
    return {
        "kind":"s_curve", "title":"Kurva S", "project_name":project_name,
        "curve":curve, "wbs":pd.DataFrame(wbs), "current_week":weeks[latest_idx] if weeks else "—",
        "actual":latest_actual, "plan":latest_plan, "deviation":latest_dev,
        "week_count":len(weeks),
    }


def parse_lookahead(ws) -> dict[str, Any]:
    """Parse rolling 2-week plan workbooks.

    Supported HDK layouts:
    - ``2 Minggu Lalu``: NO | PEKERJAAN | RA START/FINISH | RI START/FINISH
    - ``2 Minggu ke depan``: NO | PEKERJAAN | RA START/FINISH

    RA = rencana, RI = realisasi.  The parser intentionally treats each upload
    as a reporting snapshot; activity names are *not* used as database keys.
    """
    header = None
    for r in range(1, min(ws.max_row, 25) + 1):
        vals = [_text(ws.cell(r, c).value).upper() for c in range(1, min(ws.max_column, 12) + 1)]
        if "PEKERJAAN" in vals:
            header = r
            break
    if not header:
        return {"kind": "lookahead", "title": ws.title, "error": "Header PEKERJAAN tidak ditemukan"}

    # Header consists of two levels.  Merged-group captions such as RA / RI
    # normally appear only in the first column of their two-column group.
    job_col = 2
    no_col = 1
    ra_start = ra_finish = ri_start = ri_finish = None
    current_group = ""
    for c in range(1, ws.max_column + 1):
        top = _text(ws.cell(header, c).value).upper()
        sub = _text(ws.cell(header + 1, c).value).upper()
        if top:
            current_group = top
        group = top or current_group
        if top == "NO":
            no_col = c
        if top == "PEKERJAAN":
            job_col = c
        if group in {"RA", "RENCANA"}:
            if sub == "START":
                ra_start = c
            elif sub == "FINISH":
                ra_finish = c
        if group in {"RI", "REALISASI"}:
            if sub == "START":
                ri_start = c
            elif sub == "FINISH":
                ri_finish = c

    # Backward-compatible fallback for older simple layout START / FINISH.
    if ra_start is None or ra_finish is None:
        for c in range(1, ws.max_column + 1):
            top = _text(ws.cell(header, c).value).upper()
            sub = _text(ws.cell(header + 1, c).value).upper()
            if (top == "START" or sub == "START") and ra_start is None:
                ra_start = c
            if (top == "FINISH" or sub == "FINISH") and ra_finish is None:
                ra_finish = c
    ra_start = ra_start or 3
    ra_finish = ra_finish or 4

    is_actual_sheet = bool(ri_start or ri_finish or "LALU" in ws.title.upper())
    mode = "actual" if is_actual_sheet else "forward"

    rows = []
    for r in range(header + 2, ws.max_row + 1):
        job = _text(ws.cell(r, job_col).value)
        if not job:
            continue
        plan_start = _date(ws.cell(r, ra_start).value)
        plan_finish = _date(ws.cell(r, ra_finish).value)
        actual_start = _date(ws.cell(r, ri_start).value) if ri_start else None
        actual_finish = _date(ws.cell(r, ri_finish).value) if ri_finish else None
        if plan_start is None and plan_finish is None and actual_start is None and actual_finish is None:
            continue
        plan_finish = plan_finish or plan_start
        actual_finish = actual_finish or (actual_start if actual_start is not None and ri_finish is None else None)

        plan_duration = None
        actual_duration = None
        start_var = None
        finish_var = None
        if plan_start is not None and plan_finish is not None:
            plan_duration = int((plan_finish - plan_start).days + 1)
        if actual_start is not None and actual_finish is not None:
            actual_duration = int((actual_finish - actual_start).days + 1)
        if plan_start is not None and actual_start is not None:
            start_var = int((actual_start - plan_start).days)
        if plan_finish is not None and actual_finish is not None:
            finish_var = int((actual_finish - plan_finish).days)

        if mode == "actual":
            if actual_start is None:
                status = "Belum Realisasi"
            elif actual_finish is None:
                status = "Berjalan"
            elif finish_var is not None and finish_var > 0:
                status = "Late Finish"
            elif start_var is not None and start_var > 0:
                status = "Late Start / Recovered"
            else:
                status = "On Time"
        else:
            status = "Planned"

        rows.append({
            "No": ws.cell(r, no_col).value,
            "Pekerjaan": job,
            "RA Start": plan_start,
            "RA Finish": plan_finish,
            "RI Start": actual_start,
            "RI Finish": actual_finish,
            "Durasi Rencana": plan_duration,
            "Durasi Realisasi": actual_duration,
            "Start Variance (hari)": start_var,
            "Finish Variance (hari)": finish_var,
            "Status": status,
            "Excel Row": r,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return {
            "kind": "lookahead", "title": ws.title, "mode": mode, "tasks": df,
            "task_count": 0, "start": None, "finish": None,
        }

    # Window is based on plan dates so that delayed actuals remain visible as variance.
    start = pd.to_datetime(df["RA Start"], errors="coerce").min()
    finish = pd.to_datetime(df["RA Finish"], errors="coerce").max()
    status_counts = df["Status"].value_counts().to_dict()
    completed = int(df["RI Finish"].notna().sum()) if mode == "actual" else 0
    on_time = int(df["Status"].eq("On Time").sum()) if mode == "actual" else 0
    compliance = (on_time / completed * 100.0) if completed else None

    return {
        "kind": "lookahead",
        "title": "Realisasi 2 Minggu Lalu" if mode == "actual" else "Rencana 2 Minggu ke Depan",
        "sheet_title": ws.title,
        "mode": mode,
        "tasks": df,
        "task_count": len(df),
        "start": start,
        "finish": finish,
        "status_counts": status_counts,
        "completed": completed,
        "on_time": on_time,
        "compliance_pct": compliance,
        "late_finish": int(df["Status"].eq("Late Finish").sum()) if mode == "actual" else 0,
        "not_realized": int(df["Status"].eq("Belum Realisasi").sum()) if mode == "actual" else 0,
    }

def parse_model_progress(ws) -> dict[str, Any]:
    records=[]; discipline="Other"
    for r in range(1,ws.max_row+1):
        a=_text(ws.cell(r,1).value); b=_text(ws.cell(r,2).value)
        if a and not a.replace('.','').isdigit() and b and r>9:
            # Group lines such as A / STRUKTUR.
            if len(a)<=4 and not any(ch.isdigit() for ch in a):
                discipline=b
                continue
        status=_status(ws.cell(r,13).value)
        if a and status not in {"Unknown",""} and r>10 and not a.lower().startswith("no"):
            if status in {"Open","Coordination","Close","Closed","Review","Approved","Revision","Submit","Process"}:
                records.append({
                    "ID":a,"Area/Level":b,"Author":_text(ws.cell(r,3).value),"File":_text(ws.cell(r,4).value),
                    "LOD":_text(ws.cell(r,5).value),"Status":"Close" if status=="Closed" else status,"Discipline":discipline,
                })
    df=pd.DataFrame(records)
    counts=df["Status"].value_counts().to_dict() if not df.empty else {}
    by_disc=(df.groupby(["Discipline","Status"]).size().reset_index(name="Count") if not df.empty else pd.DataFrame())
    return {"kind":"model_progress","title":"Model Progress","records":df,"status_counts":counts,"by_discipline":by_disc,"total":len(df)}


def parse_clash(ws) -> dict[str, Any]:
    records=[]
    for r in range(12,ws.max_row+1):
        no=ws.cell(r,2).value; desc=_text(ws.cell(r,3).value)
        if no is None and not desc: continue
        if not desc: continue
        records.append({
            "No":no,"Deskripsi":desc,"Tag":_text(ws.cell(r,4).value),"Level":_text(ws.cell(r,6).value),
            "Lokasi":_text(ws.cell(r,7).value),"Disiplin 1":_text(ws.cell(r,8).value),"Objek 1":_text(ws.cell(r,9).value),
            "Disiplin 2":_text(ws.cell(r,10).value),"Objek 2":_text(ws.cell(r,11).value),
            "Tanggal Temuan":_date(ws.cell(r,13).value),"Tindak Lanjut":_text(ws.cell(r,14).value),
            "Keputusan":_text(ws.cell(r,15).value),"Tanggal Close":_date(ws.cell(r,16).value),
            "Status":_status(ws.cell(r,17).value),"BIM Update":_status(ws.cell(r,19).value),"SD Update":_status(ws.cell(r,20).value),
        })
    df=pd.DataFrame(records)
    counts=df["Status"].value_counts().to_dict() if not df.empty else {}
    return {"kind":"clash_issue","title":"Clash & Issue","records":df,"status_counts":counts,"total":len(df)}


def _parse_register(ws, kind: str, title: str, status_col: int, desc_col: int, category_col: int | None = None,
                    date_col: int | None = None, priority_col: int | None = None, number_col: int = 3) -> dict[str, Any]:
    records=[]
    for r in range(7,ws.max_row+1):
        no=ws.cell(r,1).value; number=_text(ws.cell(r,number_col).value); desc=_text(ws.cell(r,desc_col).value)
        if no is None and not number and not desc: continue
        if not desc and not number: continue
        rec={"No":no,"Number":number,"Description":desc,"Status":_status(ws.cell(r,status_col).value)}
        if category_col: rec["Category"]=_text(ws.cell(r,category_col).value)
        if date_col: rec["Date"]=_date(ws.cell(r,date_col).value)
        if priority_col: rec["Priority"]=_text(ws.cell(r,priority_col).value)
        records.append(rec)
    df=pd.DataFrame(records)
    counts=df["Status"].value_counts().to_dict() if not df.empty else {}
    return {"kind":kind,"title":title,"records":df,"status_counts":counts,"total":len(df)}


def parse_rfi(ws) -> dict[str,Any]:
    payload=_parse_register(ws,"rfi","RFI Monitoring",7,5,4,9,6,3)
    # Add response/age when available.
    if not payload["records"].empty:
        ages=[]
        for r in range(7,ws.max_row+1):
            desc=_text(ws.cell(r,5).value); num=_text(ws.cell(r,3).value)
            if not desc and not num: continue
            ages.append(_num(ws.cell(r,12).value))
        payload["records"]["Age (Days)"]=ages[:len(payload["records"])]
    return payload


def parse_si(ws) -> dict[str,Any]:
    return _parse_register(ws,"si","Site Instruction",10,5,6,4,None,3)


def parse_apm(ws) -> dict[str,Any]:
    return _parse_register(ws,"apm","Approval Material",10,5,6,4,None,3)


def parse_wms(ws) -> dict[str,Any]:
    return _parse_register(ws,"wms","Work Method Statement",9,4,5,None,None,3)


def parse_shopdrawing(ws) -> dict[str,Any]:
    """Parse Shop Drawing while preserving Function → Subfunction hierarchy.

    The source workbook uses non-document rows as group headers. MEP in
    particular has nested discipline headers (Electrical, HVAC, Fire Fighting,
    Plumbing, Mechanical, Composite, Electronic). Those headers are carried
    forward to each actual document row. Empty dates are stored as blank ISO
    strings so ``NaT`` never reaches the UI or SQLite register.
    """
    records=[]
    current_function=""
    current_function_code=""
    current_subfunction=""
    major_codes={"PRL","STR","ARS","INF","MEP","ID","CDW","LSH"}
    mep_subfunctions={
        "ELEKTRIKAL","ELECTRICAL","HVAC","FIRE FIGHTING","PLUMBING",
        "MEKANIKAL","MECHANICAL","KOMPOSIT","COMPOSITE","ELEKTRONIK","ELECTRONIC"
    }

    for r in range(9,ws.max_row+1):
        no=ws.cell(r,1).value
        code=_text(ws.cell(r,2).value).upper()
        desc=_text(ws.cell(r,3).value)
        status_raw=_text(ws.cell(r,14).value)

        # Group / discipline header: no numeric document number, a description,
        # and no status value on the row.
        if not isinstance(no,(int,float)) and desc and not status_raw:
            title=desc.strip()
            title_upper=title.upper()
            if code in major_codes:
                current_function=title
                current_function_code=code
                current_subfunction=""
            elif current_function_code=="MEP" and title_upper in mep_subfunctions:
                current_subfunction=title
            else:
                # A new top-level group without a short code, e.g. INTERIOR,
                # FASILITAS SOSIAL or GENERAL.
                current_function=title
                current_function_code=code
                current_subfunction=""
            continue

        if not isinstance(no,(int,float)) or not desc:
            continue

        source_scope=_text(ws.cell(r,10).value)
        function=current_function or source_scope or "Tanpa Fungsi"
        records.append({
            "No":int(no),
            "Number":_text(ws.cell(r,2).value),
            "Description":desc,
            "Function":function,
            "Function Code":current_function_code or source_scope,
            "Subfunction":current_subfunction,
            "Scope":source_scope,
            "Area":_text(ws.cell(r,11).value),
            "Latest Rev":_text(ws.cell(r,12).value),
            "Latest Date":_date_iso(ws.cell(r,13).value),
            "Latest Status":_status(ws.cell(r,14).value),
            "Document Position":_text(ws.cell(r,15).value),
            "Plan Submission":_date_iso(ws.cell(r,18).value),
            "Plan Approval":_date_iso(ws.cell(r,20).value),
        })
    df=pd.DataFrame(records)
    if not df.empty:
        # Unknown is useful for data quality but should not dominate status KPI.
        counts=df["Latest Status"].replace("Unknown", "Belum Status").value_counts().to_dict()
        by_function=df["Function"].replace("", "Tanpa Fungsi").value_counts(sort=False).reset_index()
        by_function.columns=["Function","Count"]
        by_scope=df["Scope"].replace("", "Unassigned").value_counts().reset_index()
        by_scope.columns=["Scope","Count"]
    else:
        counts={}
        by_function=pd.DataFrame(columns=["Function","Count"])
        by_scope=pd.DataFrame(columns=["Scope","Count"])
    return {
        "kind":"shopdrawing","title":"Shop Drawing","records":df,
        "status_counts":counts,"by_function":by_function,"by_scope":by_scope,"total":len(df),
    }


def parse_reference(ws) -> dict[str,Any]:
    rows=[]
    for r in range(3,ws.max_row+1):
        vals=[ws.cell(r,c).value for c in range(1,ws.max_column+1)]
        if any(v is not None and _text(v) for v in vals): rows.append(vals)
    headers=[_text(ws.cell(3,c).value) or f"Column {c}" for c in range(1,ws.max_column+1)]
    df=pd.DataFrame(rows[1:],columns=headers) if len(rows)>1 else pd.DataFrame()
    return {"kind":"reference","title":"Reference / Master Data","records":df,"total":len(df)}


def parse_generic(ws) -> dict[str,Any]:
    # Find a likely header row by nonempty/string density.
    best=1; score=-1
    for r in range(1,min(ws.max_row,25)+1):
        vals=[ws.cell(r,c).value for c in range(1,ws.max_column+1)]
        non=[v for v in vals if v is not None and _text(v)]
        sc=len(non)+sum(isinstance(v,str) for v in non)*.5
        if sc>score: best=r; score=sc
    headers=[]; used={}
    for c in range(1,ws.max_column+1):
        h=_text(ws.cell(best,c).value) or f"Column_{c}"
        used[h]=used.get(h,0)+1
        if used[h]>1: h=f"{h}_{used[h]}"
        headers.append(h)
    rows=[]
    for r in range(best+1,ws.max_row+1):
        vals=[ws.cell(r,c).value for c in range(1,ws.max_column+1)]
        if any(v is not None and _text(v) for v in vals): rows.append(vals)
    df=pd.DataFrame(rows,columns=headers) if rows else pd.DataFrame(columns=headers)
    return {"kind":"generic","title":ws.title,"records":df,"total":len(df)}


def extract_workbook_visuals(path: str | Path, selected_sheets: list[str] | tuple[str,...] | None = None) -> dict[str,dict[str,Any]]:
    """Build visual payloads using direct XLSX XML values. Large workbooks with embedded BIM screenshots remain fast."""
    p=Path(path)
    zf,shared,sheets=_xlsx_index(p)
    names=list(selected_sheets) if selected_sheets else list(sheets.keys())
    out={}
    try:
        for name in names:
            xml_path=sheets.get(name)
            if not xml_path: continue
            ws=_fast_sheet(zf,shared,name,xml_path)
            kind=classify_sheet(p.name,name)
            try:
                if kind=="s_curve": payload=parse_s_curve(ws)
                elif kind=="lookahead": payload=parse_lookahead(ws)
                elif kind=="model_progress": payload=parse_model_progress(ws)
                elif kind=="clash_issue": payload=parse_clash(ws)
                elif kind=="rfi": payload=parse_rfi(ws)
                elif kind=="si": payload=parse_si(ws)
                elif kind=="shopdrawing": payload=parse_shopdrawing(ws)
                elif kind=="apm": payload=parse_apm(ws)
                elif kind=="wms": payload=parse_wms(ws)
                elif kind=="reference": payload=parse_reference(ws)
                elif kind=="engineering_dashboard": payload={"kind":"engineering_dashboard","title":"Engineering & BIM Dashboard"}
                else: payload=parse_generic(ws)
            except Exception as exc:
                payload={"kind":kind,"title":name,"error":str(exc)}
            out[name]=payload
    finally:
        zf.close()
    if any(v.get("kind")=="engineering_dashboard" for v in out.values()):
        eng={"kind":"engineering_dashboard","title":"Engineering & BIM Dashboard","modules":{}}
        for name,payload in out.items():
            if payload.get("kind") not in {"engineering_dashboard","reference","generic"}:
                eng["modules"][payload.get("kind")]=payload
        for name,payload in list(out.items()):
            if payload.get("kind")=="engineering_dashboard": out[name]=eng
    return out


def executive_summary(payloads: list[dict[str,Any]]) -> dict[str,Any]:
    summary={
        "progress_actual":None,"progress_plan":None,"progress_deviation":None,"current_week":"—",
        "lookahead_tasks":0,"lookahead_actual_total":0,"lookahead_on_time":0,"lookahead_compliance":None,
        "clash_open":0,"rfi_open":0,"model_open":0,"shopdrawing_total":0,
        "issues":pd.DataFrame(),"curve":pd.DataFrame(),"lookahead":pd.DataFrame(),
    }
    issue_frames=[]
    for p in payloads:
        kind=p.get("kind")
        if kind=="s_curve":
            summary.update(progress_actual=p.get("actual"),progress_plan=p.get("plan"),progress_deviation=p.get("deviation"),current_week=p.get("current_week","—"),curve=p.get("curve",pd.DataFrame()))
        elif kind=="lookahead":
            if p.get("mode")=="actual":
                summary["lookahead_actual_total"]+=int(p.get("task_count",0))
                summary["lookahead_on_time"]+=int(p.get("on_time",0))
                comp=p.get("compliance_pct")
                if comp is not None:
                    summary["lookahead_compliance"]=comp
            else:
                summary["lookahead_tasks"]+=int(p.get("task_count",0))
                summary["lookahead"]=p.get("tasks",pd.DataFrame())
        elif kind=="clash_issue":
            summary["clash_open"]+=int(p.get("status_counts",{}).get("Open",0))
            df=p.get("records",pd.DataFrame())
            if not df.empty:
                issue_frames.append(df[df.get("Status",pd.Series(index=df.index,dtype=str)).astype(str).str.lower().eq("open")].head(8))
        elif kind=="rfi":
            summary["rfi_open"]+=int(p.get("status_counts",{}).get("Open",0))
        elif kind=="model_progress":
            summary["model_open"]+=int(p.get("status_counts",{}).get("Open",0))
        elif kind=="shopdrawing":
            summary["shopdrawing_total"]+=int(p.get("total",0))
        elif kind=="engineering_dashboard":
            # modules may contain the same sibling payloads; avoid double counting here.
            pass
    if issue_frames:
        summary["issues"]=pd.concat(issue_frames,ignore_index=True)
    return summary

ENGINEERING_REGISTER_KINDS = {"model_progress", "clash_issue", "rfi", "si", "shopdrawing", "apm", "wms"}

PROFILE_FUNCTION_DEFAULTS = {
    "s_curve": "Project Control · Kurva S",
    "lookahead": "Project Control · Rencana 2 Mingguan",
    "model_progress": "Engineering & BIM · Model Progress",
    "clash_issue": "Engineering & BIM · Clash & Issue",
    "rfi": "Engineering & BIM · RFI",
    "si": "Engineering & BIM · Site Instruction",
    "shopdrawing": "Engineering & BIM · Shop Drawing",
    "apm": "Engineering & BIM · Material Approval",
    "wms": "Engineering & BIM · Work Method Statement",
    "reference": "Reference / Master",
    "engineering_dashboard": "Ignored / Dashboard Excel",
    "generic": "Other",
}


def default_function_for_profile(kind: str) -> str:
    return PROFILE_FUNCTION_DEFAULTS.get(kind, "Other")


def _stable_auto_key(prefix: str, values: list[Any]) -> str:
    import hashlib
    raw = "|".join(_text(v).strip().lower() for v in values)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10].upper()
    return f"{prefix}-AUTO-{digest}"


def build_register_sync_frame(payload: dict[str, Any]) -> tuple[pd.DataFrame, str, list[str]]:
    """Normalize a recognized Engineering sheet into an incremental register frame.

    Returns: dataframe, internal key column, data-quality warnings.  Existing
    records can be updated by key while new keys are appended. Missing records
    from the new upload are intentionally *not* deleted.
    """
    kind = payload.get("kind")
    if kind not in ENGINEERING_REGISTER_KINDS:
        raise ValueError(f"Profil {kind!r} bukan register Engineering")
    df = payload.get("records", pd.DataFrame()).copy()
    key_col = "_Register_Key"
    warnings: list[str] = []
    if df.empty:
        df[key_col] = pd.Series(dtype="string")
        return df, key_col, warnings

    keys: list[str] = []
    placeholder_count = 0
    for idx, row in df.iterrows():
        if kind == "model_progress":
            key = _text(row.get("ID"))
            if not key:
                key = _stable_auto_key("MODEL", [row.get("File"), row.get("Area/Level"), row.get("Discipline")])
        elif kind == "clash_issue":
            no = _text(row.get("No"))
            key = f"CI-{no}" if no else _stable_auto_key("CI", [row.get("Deskripsi"), row.get("Level"), row.get("Lokasi")])
        elif kind == "rfi":
            num = _text(row.get("Number"))
            key = num or _stable_auto_key("RFI", [row.get("Description"), row.get("Category"), row.get("Date")])
        elif kind == "si":
            num = _text(row.get("Number"))
            key = num or _stable_auto_key("SI", [row.get("Description"), row.get("Category"), row.get("Date")])
        elif kind == "apm":
            num = _text(row.get("Number"))
            key = num or _stable_auto_key("APM", [row.get("Description"), row.get("Category"), row.get("Date")])
        elif kind == "wms":
            num = _text(row.get("Number"))
            key = num or _stable_auto_key("WMS", [row.get("Description"), row.get("Category")])
        else:  # shopdrawing
            num = _text(row.get("Number"))
            invalid = (not num) or ("sematkan" in num.lower()) or ("nomor dokumen" in num.lower())
            if invalid:
                placeholder_count += 1
                key = _stable_auto_key("SD", [row.get("No"), row.get("Description"), row.get("Scope"), row.get("Area")])
            else:
                key = num
        keys.append(key)

    df.insert(0, key_col, keys)
    # Duplicate stable keys are unsafe for sync; keep first and warn, mirroring normal upsert semantics.
    dup = df[key_col].astype(str).duplicated(keep=False)
    if dup.any():
        warnings.append(f"{int(dup.sum())} record memiliki key register duplikat; cek nomor/ID sumber.")
    if kind == "shopdrawing" and placeholder_count:
        warnings.append(
            f"{placeholder_count} Shop Drawing belum mempunyai nomor dokumen valid; Data Hub memakai ID otomatis berbasis isi record sampai nomor resmi tersedia."
        )
    return df, key_col, warnings


def compare_register_frames(old_df: pd.DataFrame, new_df: pd.DataFrame, key_col: str = "_Register_Key") -> dict[str, Any]:
    """Compare two register states and return inserted/changed/unchanged plus field changes."""
    if key_col not in new_df.columns:
        raise ValueError(f"Kolom key {key_col} tidak ada pada data baru")
    old = old_df.copy() if isinstance(old_df, pd.DataFrame) else pd.DataFrame()
    new = new_df.copy()
    if key_col not in old.columns:
        old = pd.DataFrame(columns=new.columns)
    for frame in (old, new):
        frame[key_col] = frame[key_col].astype("string").fillna("").str.strip()
    old = old[old[key_col].ne("")].drop_duplicates(key_col, keep="last").set_index(key_col, drop=False)
    new = new[new[key_col].ne("")].drop_duplicates(key_col, keep="last").set_index(key_col, drop=False)
    inserted_keys = [k for k in new.index if k not in old.index]
    common = [k for k in new.index if k in old.index]
    fields = [c for c in new.columns if c != key_col]
    changes: list[dict[str, Any]] = []
    changed_keys: set[str] = set()

    def norm(v: Any) -> str:
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except Exception:
            pass
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        return str(v).strip()

    for k in common:
        for col in fields:
            old_v = old.loc[k, col] if col in old.columns else None
            new_v = new.loc[k, col]
            if norm(old_v) != norm(new_v):
                changed_keys.add(str(k))
                changes.append({"Register ID": str(k), "Kolom": col, "Sebelum": norm(old_v), "Sesudah": norm(new_v)})
    unchanged = max(0, len(common) - len(changed_keys))
    return {
        "inserted": len(inserted_keys),
        "changed": len(changed_keys),
        "unchanged": unchanged,
        "inserted_keys": inserted_keys,
        "changed_keys": sorted(changed_keys),
        "changes": pd.DataFrame(changes),
    }
