from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from .ui import metric_card, fmt_int


def _fmt_pct(v: Any) -> str:
    try:
        if v is None or pd.isna(v): return "—"
        return f"{float(v):.2f}%"
    except Exception:
        return "—"


def _status_figure(counts: dict[str,int], title: str = "Status"):
    if not counts:
        return None
    df=pd.DataFrame({"Status":list(counts.keys()),"Count":list(counts.values())})
    fig=px.pie(df,names="Status",values="Count",hole=.66)
    fig.update_layout(title=title,margin=dict(l=5,r=5,t=45,b=5),height=315,legend=dict(orientation="h",y=-.08))
    return fig




STATUS_COLORS = {
    "Open": "#2563EB",
    "Coordination": "#F59E0B",
    "Close": "#16A34A",
    "Closed": "#16A34A",
    "Approved": "#16A34A",
    "Review": "#7C3AED",
    "Revision": "#EF4444",
    "Submit": "#0EA5E9",
    "Process": "#F59E0B",
    "Belum Status": "#94A3B8",
    "Unknown": "#94A3B8",
}

def _clean_display_df(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitize display-only values so NaT/NaN never leak to the UI."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].apply(lambda v: "" if pd.isna(v) else pd.Timestamp(v).strftime("%d %b %Y"))
        else:
            out[c] = out[c].apply(lambda v: "" if pd.isna(v) or str(v).strip().lower() in {"nat", "nan", "none"} else v)
    return out


@st.dialog("Detail Engineering & BIM", width="large")
def _engineering_detail_dialog(title: str, df: pd.DataFrame, status_col: str, status_value: str, kind: str) -> None:
    st.markdown(f"### {title}")
    st.caption(f"Status: **{status_value}**")
    if not isinstance(df, pd.DataFrame) or df.empty or status_col not in df.columns:
        st.info("Tidak ada data untuk status ini.")
        return
    work = df.copy()
    normalized = work[status_col].astype(str).replace({"Unknown": "Belum Status"})
    work = work[normalized.eq(status_value)].copy()
    if work.empty:
        st.info("Tidak ada data untuk status ini.")
        return

    # Shop Drawing keeps Function → Subfunction grouping in the detail window.
    if kind == "shopdrawing":
        functions = [x for x in work.get("Function", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if x.strip()]
        if functions:
            selected_function = st.selectbox("Fungsi", ["Semua"] + functions, key=f"dlg_fn_{status_value}_{len(work)}")
            if selected_function != "Semua":
                work = work[work["Function"].astype(str).eq(selected_function)]
        subs = [x for x in work.get("Subfunction", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if x.strip()]
        if subs:
            selected_sub = st.selectbox("Subfungsi", ["Semua"] + subs, key=f"dlg_sub_{status_value}_{len(work)}")
            if selected_sub != "Semua":
                work = work[work["Subfunction"].astype(str).eq(selected_sub)]
        preferred = ["Function","Subfunction","Number","Description","Latest Rev","Latest Date","Latest Status","Area","Document Position"]
    elif kind == "model_progress":
        preferred = ["ID","Discipline","Area/Level","File","LOD","Status","Author"]
    elif kind == "clash_issue":
        preferred = ["No","Deskripsi","Level","Lokasi","Disiplin 1","Disiplin 2","Tindak Lanjut","Tanggal Close","Status","BIM Update","SD Update"]
    else:
        preferred = ["No","Number","Description","Category","Priority","Date","Status","Age (Days)"]
    cols = [c for c in preferred if c in work.columns]
    if not cols:
        cols = list(work.columns)
    st.dataframe(_clean_display_df(work[cols]), use_container_width=True, hide_index=True, height=min(620, 90 + 35*len(work)))


def _engineering_status_card(title: str, payload: dict[str,Any], key: str) -> None:
    counts = dict(payload.get("status_counts", {}) or {})
    if "Unknown" in counts:
        counts["Belum Status"] = int(counts.get("Belum Status", 0)) + int(counts.pop("Unknown", 0))
    df = payload.get("records", pd.DataFrame())
    kind = str(payload.get("kind", ""))
    status_col = "Latest Status" if kind == "shopdrawing" else "Status"
    total = int(payload.get("total", len(df) if isinstance(df,pd.DataFrame) else 0) or 0)
    st.markdown(f"#### {title}")
    if not counts:
        st.info("Belum ada data status.")
        return
    labels=list(counts.keys()); values=[int(counts[x]) for x in labels]
    colors=[STATUS_COLORS.get(x, "#64748B") for x in labels]
    fig=go.Figure(go.Pie(labels=labels,values=values,hole=.64,marker=dict(colors=colors),textinfo="percent",hovertemplate="%{label}: %{value}<extra></extra>"))
    fig.update_layout(
        height=300, margin=dict(l=5,r=5,t=5,b=5),
        showlegend=True, legend=dict(orientation="h",y=-.04,x=.5,xanchor="center"),
        annotations=[dict(text=f"<b>{total}</b><br><span style='font-size:11px'>Total</span>",x=.5,y=.5,showarrow=False,font=dict(size=20))],
    )
    st.plotly_chart(fig,use_container_width=True,key=f"{key}_donut",config={"displaylogo":False})
    # Clickable status buttons below the donut. This is more reliable than relying
    # on Plotly slice events and works on desktop/mobile/public viewers.
    status_cols=st.columns(min(4,max(1,len(labels))))
    for i,status in enumerate(labels):
        with status_cols[i % len(status_cols)]:
            if st.button(f"{status} · {counts[status]}", key=f"{key}_status_{i}", use_container_width=True):
                _engineering_detail_dialog(title, df, status_col, status, kind)


def render_s_curve(p: dict[str,Any], key: str="scurve") -> None:
    a,b,c,d=st.columns(4)
    with a: st.markdown(metric_card("Realisasi",_fmt_pct(p.get("actual")),f"{p.get('current_week','—')}"),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Rencana",_fmt_pct(p.get("plan")),"Pada minggu yang sama"),unsafe_allow_html=True)
    dev=p.get("deviation")
    with c: st.markdown(metric_card("Deviasi",_fmt_pct(dev),"Aktual - Rencana"),unsafe_allow_html=True)
    with d: st.markdown(metric_card("Rentang jadwal",f"{p.get('week_count',0)} minggu",p.get("project_name") or "Kurva S"),unsafe_allow_html=True)
    curve=p.get("curve",pd.DataFrame()).copy()
    if not curve.empty:
        fig=go.Figure()
        fig.add_trace(go.Scatter(x=curve["Week"],y=curve["Plan"],name="Rencana Kumulatif",mode="lines+markers",connectgaps=True))
        fig.add_trace(go.Scatter(x=curve["Week"],y=curve["Actual"],name="Realisasi Kumulatif",mode="lines+markers",connectgaps=True))
        fig.update_layout(height=440,margin=dict(l=10,r=10,t=30,b=10),yaxis_title="Progress (%)",xaxis_title="Minggu",hovermode="x unified")
        fig.update_yaxes(range=[0,max(100, float(pd.to_numeric(curve[["Plan","Actual"]].stack(),errors="coerce").max() or 100)*1.05)])
        st.plotly_chart(fig,use_container_width=True,key=f"{key}_curve")
        with st.expander("Lihat progress mingguan & deviasi"):
            st.dataframe(curve,use_container_width=True,hide_index=True,height=330)
    wbs=p.get("wbs",pd.DataFrame())
    if isinstance(wbs,pd.DataFrame) and not wbs.empty:
        st.markdown("#### Bobot pekerjaan level utama")
        fig=px.bar(wbs.sort_values("Bobot"),x="Bobot",y="Pekerjaan",orientation="h",text_auto=".2f")
        fig.update_layout(height=max(320,38*len(wbs)),margin=dict(l=10,r=10,t=20,b=10),xaxis_title="Bobot (%)",yaxis_title="")
        st.plotly_chart(fig,use_container_width=True,key=f"{key}_wbs")


def _add_text_annotation(fig, *, x: float, y: float, text: str, col: int, xanchor: str = "left", size: int = 11, color: str = "#111827", weight: int | None = None) -> None:
    """Text in its own subplot domain; annotations do not get clipped like Scatter text."""
    kw = dict(
        x=x, y=y, text=text, showarrow=False,
        xref=f"x{'' if col == 1 else col} domain",
        yref="y", xanchor=xanchor, yanchor="middle",
        align="left", font=dict(size=size, color=color),
    )
    fig.add_annotation(**kw)


def _lookahead_light_theme(fig) -> None:
    """Keep Gantt readable on screen and make Plotly PNG download presentation-ready."""
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font=dict(color="#111827", family="Arial, sans-serif"),
    )


def _gantt_config(filename: str) -> dict[str, Any]:
    return {
        "displaylogo": False,
        "toImageButtonOptions": {
            "format": "png",
            "filename": filename,
            "scale": 2,
        },
    }


def _activity_label(no: Any, job: Any, max_chars: int = 62) -> str:
    import textwrap
    no_text = str(no if pd.notna(no) else "").replace(".0", "").strip()
    job_text = str(job or "").strip()
    label = f"{no_text}. {job_text}" if no_text else job_text
    chunks = textwrap.wrap(label, width=max_chars, break_long_words=False, break_on_hyphens=False)
    # Keep up to two lines; the activity panel is intentionally wide.
    if len(chunks) > 2:
        chunks = chunks[:2]
        chunks[-1] = chunks[-1].rstrip() + "…"
    return "<br>".join(chunks) if chunks else "—"


def _configure_daily_gantt_axis(fig, start=None, finish=None, *, row: int = 1, col: int = 3, xref: str = "x3") -> None:
    """Daily calendar axis with month band directly attached to the date row."""
    start = pd.to_datetime(start, errors="coerce")
    finish = pd.to_datetime(finish, errors="coerce")
    if pd.isna(start) or pd.isna(finish):
        return
    start = start.normalize()
    finish = finish.normalize()
    days = pd.date_range(start, finish + pd.Timedelta(days=1), freq="D")
    fig.update_xaxes(
        type="date",
        range=[start - pd.Timedelta(hours=8), finish + pd.Timedelta(days=1, hours=8)],
        tickmode="array",
        tickvals=list(days),
        ticktext=[d.strftime("%d") for d in days],
        tickangle=0,
        side="top",
        ticks="outside",
        ticklen=5,
        tickfont=dict(size=10, color="#111827"),
        showgrid=True,
        gridwidth=1,
        gridcolor="#d9dee7",
        zeroline=False,
        title_text="",
        fixedrange=False,
        row=row,
        col=col,
    )
    # Weekend shading.
    for d in days:
        if d.weekday() >= 5:
            fig.add_vrect(
                x0=d, x1=d + pd.Timedelta(days=1),
                fillcolor="#f3f4f6", line_width=0,
                layer="below", row=row, col=col,
            )
    # Month header: close to days and with explicit border.
    periods = pd.period_range(start=start, end=finish, freq="M")
    for period in periods:
        lo = max(start, period.start_time.normalize())
        hi = min(finish + pd.Timedelta(days=1), (period.end_time + pd.Timedelta(days=1)).normalize())
        if hi <= lo:
            continue
        mid = lo + (hi - lo) / 2
        fig.add_shape(
            type="rect", x0=lo, x1=hi, y0=1.048, y1=1.105,
            xref=xref, yref="paper",
            line=dict(color="#6b7280", width=1.1),
            fillcolor="#f8fafc", layer="above",
        )
        fig.add_annotation(
            x=mid, y=1.076, xref=xref, yref="paper",
            text=f"<b>{period.start_time.strftime('%b %Y').upper()}</b>",
            showarrow=False, font=dict(size=11, color="#111827"),
            xanchor="center", yanchor="middle",
        )


def _add_gantt_bar(fig, y: float, start, finish, name: str, color: str, hover: str, showlegend: bool, *, row: int = 1, col: int = 3) -> None:
    start = pd.to_datetime(start, errors="coerce")
    finish = pd.to_datetime(finish, errors="coerce")
    if pd.isna(start):
        return
    if pd.isna(finish):
        finish = start
    # Inclusive day bar: a same-day activity remains visible.
    x_end = finish + pd.Timedelta(days=1)
    fig.add_trace(
        go.Bar(
            x=[(x_end-start).total_seconds()*1000],
            y=[y],
            base=[start],
            orientation="h",
            marker=dict(color=color, line=dict(width=0)),
            width=.34,
            name=name,
            showlegend=showlegend,
            hovertemplate=hover+"<extra></extra>",
        ), row=row, col=col,
    )


def render_lookahead(p: dict[str,Any], key: str="lookahead") -> None:
    tasks = p.get("tasks", pd.DataFrame()).copy()
    mode = p.get("mode", "forward")
    if mode == "actual":
        a,b,c,d = st.columns(4)
        with a: st.markdown(metric_card("Aktivitas",fmt_int(p.get("task_count",0)),"2 minggu lalu"),unsafe_allow_html=True)
        with b: st.markdown(metric_card("Selesai",fmt_int(p.get("completed",0)),"RI Finish terisi"),unsafe_allow_html=True)
        comp = p.get("compliance_pct")
        with c: st.markdown(metric_card("On Time",_fmt_pct(comp),f"{fmt_int(p.get('on_time',0))} aktivitas"),unsafe_allow_html=True)
        with d: st.markdown(metric_card("Perlu perhatian",fmt_int(p.get("late_finish",0)+p.get("not_realized",0)),"Late finish + belum realisasi"),unsafe_allow_html=True)
    else:
        a,b,c = st.columns(3)
        with a: st.markdown(metric_card("Aktivitas",fmt_int(p.get("task_count",0)),"2 minggu ke depan"),unsafe_allow_html=True)
        with b: st.markdown(metric_card("Mulai",str(p.get("start"))[:10] if p.get("start") is not None else "—","Window rencana"),unsafe_allow_html=True)
        with c: st.markdown(metric_card("Selesai",str(p.get("finish"))[:10] if p.get("finish") is not None else "—","Window rencana"),unsafe_allow_html=True)

    if tasks.empty:
        st.info("Belum ada aktivitas yang dapat divisualisasikan.")
        return

    if mode == "actual":
        st.markdown("#### Gantt 2 Minggu Lalu · Rencana vs Realisasi")
        # Cleaner two-zone layout: Activity | Calendar. Row type labels are removed.
        fig = make_subplots(
            rows=1, cols=2, shared_yaxes=True,
            column_widths=[0.38, 0.62], horizontal_spacing=0.01,
        )
        n = len(tasks)
        dates = []
        first_plan = True
        first_actual = True
        plan_color = "#16A34A"   # bright green
        actual_color = "#F97316" # bright orange
        for i, (_, r) in enumerate(tasks.iterrows()):
            activity = _activity_label(r.get("No"), r.get("Pekerjaan"), 66)
            base = (n - 1 - i) * 1.85
            plan_y = base + 0.64
            actual_y = base + 0.18
            group_y = (plan_y + actual_y) / 2
            ps = pd.to_datetime(r.get("RA Start"), errors="coerce")
            pf = pd.to_datetime(r.get("RA Finish"), errors="coerce")
            rs = pd.to_datetime(r.get("RI Start"), errors="coerce")
            rf = pd.to_datetime(r.get("RI Finish"), errors="coerce")
            dates += [x for x in [ps,pf,rs,rf] if pd.notna(x)]

            _add_text_annotation(fig, x=.015, y=group_y, text=activity, col=1, xanchor="left", size=10)

            _add_gantt_bar(
                fig, plan_y, ps, pf, "Rencana", plan_color,
                f"<b>{activity.replace('<br>',' ')}</b><br>Rencana<br>{ps.strftime('%d %b %Y') if pd.notna(ps) else '—'} → {pf.strftime('%d %b %Y') if pd.notna(pf) else '—'}",
                first_plan, col=2,
            )
            first_plan = False
            if pd.notna(rs):
                _add_gantt_bar(
                    fig, actual_y, rs, rf, "Realisasi", actual_color,
                    f"<b>{activity.replace('<br>',' ')}</b><br>Realisasi<br>{rs.strftime('%d %b %Y') if pd.notna(rs) else '—'} → {rf.strftime('%d %b %Y') if pd.notna(rf) else 'Berjalan'}<br>Status: {r.get('Status') or '—'}",
                    first_actual, col=2,
                )
                first_actual = False
            if i < n-1:
                sep_y = base - 0.18
                for cc in (1,2):
                    fig.add_hline(y=sep_y, line_width=1, line_dash="solid", line_color="#d7dde7", row=1, col=cc)

        if dates:
            _configure_daily_gantt_axis(fig, min(dates), max(dates), row=1, col=2, xref="x2")
        ymax = n*1.85 + 0.95
        fig.update_xaxes(range=[0,1], visible=False, fixedrange=True, row=1, col=1)
        for cc in (1,2):
            fig.update_yaxes(showticklabels=False, showgrid=False, zeroline=False, range=[-.35,ymax], fixedrange=True, row=1, col=cc)

        fig.add_annotation(x=.015, y=1.025, xref="x domain", yref="paper", text="<b>KEGIATAN</b>", showarrow=False,
                           xanchor="left", font=dict(size=10, color="#475569"))
        _lookahead_light_theme(fig)
        fig.update_layout(
            height=max(620, min(2100, 50*n + 150)),
            margin=dict(l=14, r=14, t=78, b=18),
            barmode="overlay", bargap=0, showlegend=True, hovermode="closest",
            legend=dict(orientation="h", yanchor="bottom", y=1.10, xanchor="right", x=1.0,
                        bgcolor="rgba(255,255,255,0.85)", bordercolor="#d1d5db", borderwidth=1),
        )
        st.plotly_chart(
            fig, use_container_width=True, key=f"{key}_actual_timeline",
            config=_gantt_config("gantt_2_minggu_lalu_rencana_vs_realisasi"),
        )
        st.caption("Download PNG dari toolbar chart menggunakan background putih dan resolusi 2×.")
        with st.expander("Variance rencana vs realisasi", expanded=False):
            keep=[c for c in ["No","Pekerjaan","RA Start","RA Finish","RI Start","RI Finish","Start Variance (hari)","Finish Variance (hari)","Status"] if c in tasks.columns]
            st.dataframe(tasks[keep],use_container_width=True,hide_index=True,height=420)
    else:
        st.markdown("#### Gantt 2 Minggu ke Depan · Lookahead")
        fig = make_subplots(rows=1, cols=2, shared_yaxes=True, column_widths=[0.40,0.60], horizontal_spacing=0.008)
        n = len(tasks)
        dates = []
        for i, (_, r) in enumerate(tasks.iterrows()):
            activity = _activity_label(r.get("No"), r.get("Pekerjaan"), 68)
            y = (n - 1 - i) * 1.22
            ps = pd.to_datetime(r.get("RA Start"), errors="coerce")
            pf = pd.to_datetime(r.get("RA Finish"), errors="coerce")
            dates += [x for x in [ps,pf] if pd.notna(x)]
            _add_text_annotation(fig, x=.015, y=y, text=activity, col=1, xanchor="left", size=10)
            _add_gantt_bar(
                fig, y, ps, pf, "Rencana", "#16A34A",
                f"<b>{activity.replace('<br>',' ')}</b><br>{ps.strftime('%d %b %Y') if pd.notna(ps) else '—'} → {pf.strftime('%d %b %Y') if pd.notna(pf) else '—'}",
                False, col=2,
            )
            if i < n-1:
                sep_y = y-.61
                for cc in (1,2):
                    fig.add_hline(y=sep_y, line_width=1, line_dash="dot", line_color="#e5e7eb", row=1, col=cc)
        if dates:
            _configure_daily_gantt_axis(fig, min(dates), max(dates), row=1, col=2, xref="x2")
        ymax=max(1,n*1.22)
        fig.update_xaxes(range=[0,1], visible=False, fixedrange=True, row=1, col=1)
        for cc in (1,2):
            fig.update_yaxes(showticklabels=False, showgrid=False, zeroline=False, range=[-.65,ymax], fixedrange=True, row=1, col=cc)
        fig.add_annotation(x=.015, y=1.025, xref="x domain", yref="paper", text="<b>KEGIATAN</b>", showarrow=False,
                           xanchor="left", font=dict(size=10, color="#475569"))
        _lookahead_light_theme(fig)
        fig.update_layout(height=max(520,min(1900,48*n+120)),margin=dict(l=14,r=14,t=78,b=18),showlegend=False)
        st.plotly_chart(
            fig,use_container_width=True,key=f"{key}_forward_timeline",
            config=_gantt_config("gantt_2_minggu_ke_depan"),
        )
        st.caption("Download PNG dari toolbar chart menggunakan background putih dan resolusi 2×.")
        with st.expander("Daftar aktivitas", expanded=False):
            keep=[c for c in ["No","Pekerjaan","RA Start","RA Finish","Durasi Rencana"] if c in tasks.columns]
            st.dataframe(tasks[keep],use_container_width=True,hide_index=True,height=380)

def render_model_progress(p: dict[str,Any], key: str="model") -> None:
    counts=p.get("status_counts",{})
    total=int(p.get("total",0)); close=int(counts.get("Close",0)); open_=int(counts.get("Open",0)); coord=int(counts.get("Coordination",0))
    a,b,c,d=st.columns(4)
    with a: st.markdown(metric_card("Total Model",fmt_int(total),"MIDP"),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Open",fmt_int(open_),"Belum koordinasi selesai"),unsafe_allow_html=True)
    with c: st.markdown(metric_card("Coordination",fmt_int(coord),"Dalam koordinasi"),unsafe_allow_html=True)
    with d: st.markdown(metric_card("Close",fmt_int(close),f"{(close/total*100):.1f}%" if total else "0%"),unsafe_allow_html=True)
    c1,c2=st.columns([1,1.7])
    with c1:
        fig=_status_figure(counts,"Status Model")
        if fig: st.plotly_chart(fig,use_container_width=True,key=f"{key}_status")
    with c2:
        bd=p.get("by_discipline",pd.DataFrame())
        if isinstance(bd,pd.DataFrame) and not bd.empty:
            fig=px.bar(bd,x="Discipline",y="Count",color="Status",barmode="stack")
            fig.update_layout(height=315,margin=dict(l=5,r=5,t=45,b=5),title="Model per disiplin")
            st.plotly_chart(fig,use_container_width=True,key=f"{key}_disc")
    df=p.get("records",pd.DataFrame())
    if isinstance(df,pd.DataFrame) and not df.empty:
        st.dataframe(df,use_container_width=True,hide_index=True,height=420)


def render_clash(p: dict[str,Any], key: str="clash") -> None:
    df=p.get("records",pd.DataFrame()).copy(); counts=p.get("status_counts",{})
    total=int(p.get("total",0)); open_=int(counts.get("Open",0)); close=int(counts.get("Close",0))
    a,b,c=st.columns(3)
    with a: st.markdown(metric_card("Total Clash/Issue",fmt_int(total),"Coordination register"),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Open",fmt_int(open_),"Perlu tindak lanjut"),unsafe_allow_html=True)
    with c: st.markdown(metric_card("Close",fmt_int(close),f"{(close/total*100):.1f}% closed" if total else "0%"),unsafe_allow_html=True)
    c1,c2=st.columns([1,1.8])
    with c1:
        fig=_status_figure(counts,"Status Clash/Issue")
        if fig: st.plotly_chart(fig,use_container_width=True,key=f"{key}_status")
    with c2:
        if not df.empty and "Level" in df:
            lev=df["Level"].replace("","Tanpa Level").value_counts().reset_index(); lev.columns=["Level","Count"]
            fig=px.bar(lev.head(12),x="Count",y="Level",orientation="h")
            fig.update_layout(height=315,margin=dict(l=5,r=5,t=45,b=5),title="Issue per level",yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig,use_container_width=True,key=f"{key}_level")
    if not df.empty:
        st.markdown("#### Register issue & clash")
        st.dataframe(df,use_container_width=True,hide_index=True,height=470)


def render_register(p: dict[str,Any], key: str) -> None:
    df=p.get("records",pd.DataFrame()).copy(); counts=p.get("status_counts",{}); total=int(p.get("total",0))
    open_=int(counts.get("Open",0)); close=int(counts.get("Close",0)+counts.get("Approved",0))
    a,b,c=st.columns(3)
    with a: st.markdown(metric_card("Total",fmt_int(total),p.get("title","Register")),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Open",fmt_int(open_),"Outstanding"),unsafe_allow_html=True)
    with c: st.markdown(metric_card("Close/Approved",fmt_int(close),"Selesai"),unsafe_allow_html=True)
    c1,c2=st.columns([1,1.8])
    with c1:
        fig=_status_figure(counts,"Status")
        if fig: st.plotly_chart(fig,use_container_width=True,key=f"{key}_status")
    with c2:
        if not df.empty and "Category" in df:
            cat=df["Category"].replace("","Tanpa kategori").value_counts().reset_index(); cat.columns=["Category","Count"]
            fig=px.bar(cat,x="Category",y="Count",text_auto=True)
            fig.update_layout(height=315,margin=dict(l=5,r=5,t=45,b=5),title="Per kategori")
            st.plotly_chart(fig,use_container_width=True,key=f"{key}_cat")
    if not df.empty:
        st.dataframe(df,use_container_width=True,hide_index=True,height=440)


def render_shopdrawing(p: dict[str,Any], key: str="sd") -> None:
    df=p.get("records",pd.DataFrame()).copy(); counts=p.get("status_counts",{}); total=int(p.get("total",0))
    approved=int(counts.get("Approved",0)); revision=int(counts.get("Revision",0)); submit=int(counts.get("Submit",0))
    a,b,c,d=st.columns(4)
    with a: st.markdown(metric_card("Total Dokumen",fmt_int(total),"Shop Drawing"),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Approved",fmt_int(approved),"Latest status"),unsafe_allow_html=True)
    with c: st.markdown(metric_card("Revision",fmt_int(revision),"Perlu revisi"),unsafe_allow_html=True)
    with d: st.markdown(metric_card("Submit",fmt_int(submit),"Dalam submission"),unsafe_allow_html=True)
    c1,c2=st.columns([1,1.8])
    with c1:
        fig=_status_figure(counts,"Latest Status")
        if fig: st.plotly_chart(fig,use_container_width=True,key=f"{key}_status")
    with c2:
        bs=p.get("by_scope",pd.DataFrame())
        if isinstance(bs,pd.DataFrame) and not bs.empty:
            fig=px.bar(bs.head(15),x="Count",y="Scope",orientation="h")
            fig.update_layout(height=315,margin=dict(l=5,r=5,t=45,b=5),title="Dokumen per scope",yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig,use_container_width=True,key=f"{key}_scope")
    if not df.empty:
        st.markdown("#### Register & status revisi terakhir")
        st.dataframe(_clean_display_df(df),use_container_width=True,hide_index=True,height=500)


def render_engineering_dashboard(p: dict[str,Any], key: str="engd") -> None:
    modules=p.get("modules",{})
    items=[
        ("Model Progress", modules.get("model_progress",{}), "model"),
        ("Clash / Issue", modules.get("clash_issue",{}), "clash"),
        ("RFI", modules.get("rfi",{}), "rfi"),
        ("Site Instruction", modules.get("si",{}), "si"),
        ("Shop Drawing", modules.get("shopdrawing",{}), "sd"),
        ("Approval Material", modules.get("apm",{}), "apm"),
        ("Work Method Statement", modules.get("wms",{}), "wms"),
    ]
    visible=[x for x in items if x[1]]
    if not visible:
        st.info("Belum ada data Engineering & BIM yang terbaca.")
        return
    # Every Engineering/BIM function uses the same visual language: donut + clickable statuses.
    for start in range(0,len(visible),2):
        cols=st.columns(2,gap="large")
        for offset,item in enumerate(visible[start:start+2]):
            title,payload,suffix=item
            with cols[offset]:
                with st.container(border=True):
                    _engineering_status_card(title,payload,key=f"{key}_{suffix}")


def render_model_progress_compact(p: dict[str,Any], key: str) -> None:
    fig=_status_figure(p.get("status_counts",{}),"Model Progress")
    if fig: st.plotly_chart(fig,use_container_width=True,key=key)


def render_payload(p: dict[str,Any], key: str="sheet") -> None:
    if p.get("error"):
        st.error(f"Visualisasi belum dapat dibuat: {p['error']}")
        return
    kind=p.get("kind")
    st.markdown(f"### {p.get('title','Visual Sheet')}")
    if kind=="s_curve": render_s_curve(p,key)
    elif kind=="lookahead": render_lookahead(p,key)
    elif kind=="model_progress": render_model_progress(p,key)
    elif kind=="clash_issue": render_clash(p,key)
    elif kind in {"rfi","si","apm","wms"}: render_register(p,key)
    elif kind=="shopdrawing": render_shopdrawing(p,key)
    elif kind=="engineering_dashboard": render_engineering_dashboard(p,key)
    else:
        df=p.get("records",pd.DataFrame())
        if isinstance(df,pd.DataFrame) and not df.empty:
            st.dataframe(df,use_container_width=True,hide_index=True,height=520)
        else:
            st.info("Sheet terpilih belum memiliki visual khusus atau belum berisi data terbaca.")


def render_executive(summary: dict[str,Any], key: str="exec") -> None:
    # Main KPI row inspired by the user's reference dashboard, but responsive/bento.
    a,b,c,d,e=st.columns(5)
    with a: st.markdown(metric_card("Progress Aktual",_fmt_pct(summary.get("progress_actual")),summary.get("current_week","—")),unsafe_allow_html=True)
    with b: st.markdown(metric_card("Progress Rencana",_fmt_pct(summary.get("progress_plan")),"Kurva S"),unsafe_allow_html=True)
    with c: st.markdown(metric_card("Deviasi",_fmt_pct(summary.get("progress_deviation")),"Aktual - Rencana"),unsafe_allow_html=True)
    with d: st.markdown(metric_card("Clash + RFI Open",fmt_int(summary.get("clash_open",0)+summary.get("rfi_open",0)),"Action required"),unsafe_allow_html=True)
    comp=summary.get("lookahead_compliance")
    la_sub=(f"{float(comp):.1f}% on time 2 minggu lalu" if comp is not None else "Aktivitas 2 minggu ke depan")
    with e: st.markdown(metric_card("2 Week Plan",fmt_int(summary.get("lookahead_tasks",0)),la_sub),unsafe_allow_html=True)

    curve=summary.get("curve",pd.DataFrame())
    if isinstance(curve,pd.DataFrame) and not curve.empty:
        fig=go.Figure()
        fig.add_trace(go.Scatter(x=curve["Week"],y=curve["Plan"],name="Rencana",mode="lines"))
        fig.add_trace(go.Scatter(x=curve["Week"],y=curve["Actual"],name="Realisasi",mode="lines+markers",connectgaps=True))
        fig.update_layout(height=390,margin=dict(l=10,r=10,t=38,b=10),title="Monitoring Progress · Kurva S",yaxis_title="Progress (%)",hovermode="x unified")
        st.plotly_chart(fig,use_container_width=True,key=f"{key}_curve")
