from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from fpdf import FPDF


STATUS_OPTIONS = ["Aktif", "On Hold", "Selesai", "Terminasi"]


def effective_finish(project: dict[str, Any]) -> str:
    """Return revised finish when present, otherwise contractual finish."""
    return str(project.get("revised_finish") or project.get("contract_finish") or "").strip()


def portfolio_frame(projects: pd.DataFrame) -> pd.DataFrame:
    """Build a management-friendly multi-project summary without mutating source data."""
    if projects.empty:
        return pd.DataFrame(columns=["Kode", "Proyek", "Status", "Owner", "Lokasi", "Mulai", "Effective Finish", "Fungsi", "Foto"])
    out = pd.DataFrame()
    out["Kode"] = projects.get("code", pd.Series(dtype=str)).fillna("")
    out["Proyek"] = projects.get("name", pd.Series(dtype=str)).fillna("")
    if "project_status" in projects.columns:
        out["Status"] = projects["project_status"].fillna("Aktif").replace("", "Aktif")
    else:
        out["Status"] = "Aktif"
    out["Owner"] = projects.get("client", pd.Series(dtype=str)).fillna("")
    out["Lokasi"] = projects.get("location", pd.Series(dtype=str)).fillna("")
    out["Mulai"] = projects.get("contract_start", pd.Series(dtype=str)).fillna("")
    revised = projects.get("revised_finish", pd.Series([""] * len(projects), index=projects.index)).fillna("").astype(str).str.strip()
    contract = projects.get("contract_finish", pd.Series([""] * len(projects), index=projects.index)).fillna("").astype(str).str.strip()
    out["Effective Finish"] = revised.where(revised.ne(""), contract)
    out["Fungsi"] = pd.to_numeric(projects.get("function_count", 0), errors="coerce").fillna(0).astype(int)
    out["Foto"] = pd.to_numeric(projects.get("photo_count", 0), errors="coerce").fillna(0).astype(int)
    return out.reset_index(drop=True)


def _latin(text: Any) -> str:
    """Core PDF fonts are Latin-1; replace unsupported characters instead of failing."""
    return str(text if text is not None else "").encode("latin-1", "replace").decode("latin-1")


def build_project_report_pdf(
    project: dict[str, Any],
    reporting: dict[str, Any] | None = None,
    *,
    dataset_count: int = 0,
    photo_count: int = 0,
) -> bytes:
    """Create a compact management report PDF for download/email attachment."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_title(_latin(f"HDK Project Report - {project.get('code','')}"))
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _latin("HDK PROJECT DATA HUB"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 7, _latin(f"{project.get('code','-')} - {project.get('name','-')}"))
    pdf.ln(2)

    rows = [
        ("Status", project.get("project_status") or "Aktif"),
        ("Owner", project.get("client") or "-"),
        ("Lokasi", project.get("location") or "-"),
        ("Mulai", project.get("contract_start") or "-"),
        ("Finish Kontrak", project.get("contract_finish") or "-"),
        ("Effective Finish", effective_finish(project) or "-"),
        ("No. Kontrak", project.get("contract_no") or "-"),
        ("Dataset aktif", dataset_count),
        ("Foto / visual", photo_count),
    ]
    if reporting:
        rows = [
            ("Periode Laporan", reporting.get("period_label") or "-"),
            ("Revisi", reporting.get("revision_no", 0)),
            ("Status Laporan", str(reporting.get("status") or "draft").upper()),
            ("Data per tanggal", reporting.get("data_as_of") or "-"),
        ] + rows

    pdf.set_font("Helvetica", size=10)
    for label, value in rows:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(45, 7, _latin(label))
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 7, _latin(value))

    description = str(project.get("description") or "").strip()
    if description:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, _latin("Catatan Proyek"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 6, _latin(description))

    if reporting and str(reporting.get("note") or "").strip():
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, _latin("Catatan Periode"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 6, _latin(reporting.get("note")))

    pdf.ln(5)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 5, _latin(f"Dibuat dari HDK Project Data Hub pada {date.today().isoformat()}."))
    return bytes(pdf.output())
