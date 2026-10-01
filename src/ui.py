from __future__ import annotations

import html
import io
from typing import Iterable

import pandas as pd

BENTO_CSS = """
<style>
:root{--card-border:rgba(128,128,128,.22);--soft:rgba(128,128,128,.07)}
.block-container{padding-top:1.15rem;padding-bottom:3rem;max-width:1550px}
[data-testid="stSidebar"]{border-right:1px solid var(--card-border)}
.hero-card{border:1px solid var(--card-border);border-radius:22px;padding:18px 24px;margin-bottom:13px;background:radial-gradient(circle at 84% 0%,rgba(66,111,255,.18),transparent 34%),radial-gradient(circle at 10% 100%,rgba(62,190,155,.11),transparent 32%),linear-gradient(145deg,rgba(255,255,255,.06),rgba(255,255,255,.015))}
.hero-title{font-size:1.82rem;font-weight:800;letter-spacing:-.03em}.hero-sub{opacity:.64;margin-top:3px;font-size:.9rem}
.bento-card{border:1px solid var(--card-border);border-radius:21px;padding:17px 19px;background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.012));box-shadow:0 10px 28px rgba(0,0,0,.035);min-height:112px}
.bento-card .label{font-size:.8rem;opacity:.66;margin-bottom:7px}.bento-card .value{font-size:1.72rem;font-weight:780;letter-spacing:-.02em}.bento-card .sub{font-size:.77rem;opacity:.58;margin-top:5px}
.section-title{font-size:1.13rem;font-weight:740;margin:10px 0 8px}.pill{display:inline-block;border:1px solid var(--card-border);border-radius:99px;padding:3px 8px;font-size:.74rem;opacity:.72;margin-right:4px}
[data-testid="stDataFrame"],[data-testid="stDataEditor"]{border-radius:15px;overflow:hidden}div[data-testid="stPlotlyChart"]{border:1px solid var(--card-border);border-radius:19px;padding:7px}.stButton>button,.stDownloadButton>button{border-radius:11px}
.photo-card{border:1px solid var(--card-border);border-radius:18px;padding:10px;margin-bottom:10px}
.project-brand-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;border:1px solid var(--card-border);border-radius:18px;overflow:hidden;margin:2px 0 12px;background:linear-gradient(145deg,rgba(255,255,255,.042),rgba(255,255,255,.010));box-shadow:0 8px 24px rgba(0,0,0,.028)}
.project-party{min-height:104px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:10px 14px;text-align:center;position:relative}
.project-party:not(:last-child){border-right:1px solid var(--card-border)}
.project-party-role{font-size:.58rem;font-weight:820;letter-spacing:.14em;opacity:.48;min-height:12px}
.project-party-logo{width:142px;height:50px;max-width:72%;border-radius:10px;background:#fff;display:flex;align-items:center;justify-content:center;padding:0;box-shadow:inset 0 0 0 1px rgba(0,0,0,.07),0 3px 10px rgba(0,0,0,.055);overflow:hidden}
.project-logo-img{display:block;width:100%;height:100%;object-fit:contain}
.project-logo-placeholder{font-size:.62rem;font-weight:800;letter-spacing:.15em;color:#9aa0aa}
.project-party-name{font-size:.76rem;font-weight:690;line-height:1.18;letter-spacing:-.005em;overflow-wrap:anywhere;min-height:1.8em;display:flex;align-items:flex-start;justify-content:center}
@media(max-width:900px){.project-brand-strip{grid-template-columns:1fr}.project-party:not(:last-child){border-right:0;border-bottom:1px solid var(--card-border)}.project-party{min-height:96px}.project-party-logo{width:138px;height:48px}}
</style>
"""


def hero(title: str, subtitle: str = "") -> str:
    sub = f'<div class="hero-sub">{html.escape(subtitle)}</div>' if str(subtitle or '').strip() else ''
    return f'<div class="hero-card"><div class="hero-title">{html.escape(title)}</div>{sub}</div>'


def metric_card(label: str, value: str, sub: str = "") -> str:
    return f'<div class="bento-card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div><div class="sub">{html.escape(sub)}</div></div>'


def fmt_int(value: int | float) -> str:
    try: return f"{int(value):,}".replace(",", ".")
    except Exception: return str(value)


def fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB": return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def numeric_columns(df: pd.DataFrame, exclude: Iterable[str] = ("_row_id",)) -> list[str]:
    result = []
    for col in df.columns:
        if col in exclude: continue
        s = df[col]
        if pd.api.types.is_numeric_dtype(s): result.append(col); continue
        if s.dropna().empty: continue
        parsed = pd.to_numeric(s, errors="coerce")
        if parsed.notna().mean() >= .9: result.append(col)
    return result


def global_search(df: pd.DataFrame, term: str) -> pd.DataFrame:
    if not term.strip() or df.empty: return df
    mask = pd.Series(False, index=df.index); needle = term.strip().lower()
    for col in df.columns:
        mask = mask | df[col].astype(str).str.lower().str.contains(needle, na=False, regex=False)
    return df.loc[mask]


def dataframe_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Data") -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=(sheet_name[:31] or "Data"))
        ws = writer.book[writer.sheets[(sheet_name[:31] or "Data")].title]
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        for cells in ws.columns:
            max_len = max((len(str(c.value)) if c.value is not None else 0 for c in cells), default=0)
            ws.column_dimensions[cells[0].column_letter].width = min(max(max_len+2,10),40)
    return bio.getvalue()
