from pathlib import Path
import os
import sys
import tempfile
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.TemporaryDirectory()
os.environ['LOCAL_DB_PATH'] = str(Path(tmp.name) / 'test.db')

from src import db  # noqa: E402
from src.importers import build_dataframe, guess_header_row  # noqa: E402


def run():
    db.init_db()
    pid = db.create_project('TST', 'Test Project', bimx_url='https://example.com/bimx')

    raw = pd.DataFrame([
        ['REPORT', None, None],
        ['Activity_ID', 'Week_ID', 'Progress'],
        ['A1', 'W1', 10],
        ['A2', 'W1', 20],
    ])
    assert guess_header_row(raw) == 2
    first = build_dataframe(raw, 2)
    did = db.create_dataset(first, pid, 'Progress & Schedule', 'Progress')
    db.save_function_source(pid, 'Progress & Schedule', did, 'Sheet1', 2, ['Activity_ID', 'Week_ID'], 'first.xlsx')

    # AUTO: update rows + insert row + evolve new column.
    second = pd.DataFrame({
        'Activity_ID': ['A1', 'A2', 'A3'],
        'Week_ID': ['W1', 'W1', 'W1'],
        'Progress': [15, 20, 5],
        'New_Column': ['x', 'y', 'z'],
    })
    stats, rejected = db.upsert_dataframe(did, second, ['Activity_ID', 'Week_ID'])
    assert stats['updated'] == 2
    assert stats['inserted'] == 1
    assert rejected.empty
    out = db.get_dataframe(did)
    assert len(out) == 3
    assert float(out.loc[out['Activity_ID'] == 'A1', 'Progress'].iloc[0]) == 15
    assert 'New_Column' in out.columns

    # COLUMN mode: new column allowed, unknown row rejected/not inserted.
    col_update = pd.DataFrame({
        'Activity_ID': ['A1', 'A4'],
        'Week_ID': ['W1', 'W1'],
        'Forecast': [30, 40],
    })
    stats_col, rejected_col = db.upsert_dataframe(
        did, col_update, ['Activity_ID', 'Week_ID'], allow_insert=False, allow_new_columns=True
    )
    assert stats_col['updated'] == 1
    assert stats_col['inserted'] == 0
    assert stats_col['rejected'] == 1
    out2 = db.get_dataframe(did)
    assert 'Forecast' in out2.columns
    assert len(out2) == 3
    assert float(out2.loc[out2['Activity_ID'] == 'A1', 'Forecast'].iloc[0]) == 30

    # ROW mode: unknown row inserted, but schema evolution blocked.
    row_update = pd.DataFrame({
        'Activity_ID': ['A2', 'A5'],
        'Week_ID': ['W1', 'W1'],
        'Progress': [25, 1],
        'Should_Not_Be_Added': ['no', 'no'],
    })
    stats_row, rejected_row = db.upsert_dataframe(
        did, row_update, ['Activity_ID', 'Week_ID'], allow_insert=True, allow_new_columns=False
    )
    assert stats_row['updated'] == 1
    assert stats_row['inserted'] == 1
    assert stats_row['ignored_columns'] == 1
    assert rejected_row.empty
    out3 = db.get_dataframe(did)
    assert 'Should_Not_Be_Added' not in out3.columns
    assert len(out3) == 4

    # Duplicate/blank keys still rejected.
    bad = pd.DataFrame({
        'Activity_ID': ['A3', 'A3', None],
        'Week_ID': ['W1', 'W1', 'W1'],
        'Progress': [6, 7, 8],
    })
    stats2, rejected2 = db.upsert_dataframe(did, bad, ['Activity_ID', 'Week_ID'])
    assert stats2['updated'] == 1
    assert stats2['rejected'] == 2
    assert len(rejected2) == 2

    # Optional Excel-row identity is generated from original worksheet row numbers.
    raw2 = pd.DataFrame([
        ['Title', None],
        ['Name', 'Value'],
        ['A', 1],
        [None, None],
        ['B', 2],
    ])
    by_row = build_dataframe(raw2, 2, include_excel_row=True)
    assert by_row['__Excel_Row'].tolist() == [3, 5]

    # v2.3: register one file with multiple selectable sheets.
    sfid = db.upsert_source_file(pid, 'Control_Project.xlsx', 'Progress & Schedule', 'Workbook kontrol proyek', 12345)
    db.sync_source_sheet_selection(pid, sfid, ['Progress', 'Kurva S', 'Manpower'], ['Progress', 'Kurva S'], 'Progress & Schedule')
    selected = db.list_source_sheets(sfid, selected_only=True)
    assert set(selected['sheet_name']) == {'Progress', 'Kurva S'}

    # Attach the Progress dataset to one selected sheet and save a version snapshot.
    ssid = db.save_source_sheet_config(
        pid, sfid, 'Progress', dataset_id=did, function_name='Progress & Schedule',
        display_name='Progress Mingguan', description='Data progress per aktivitas',
        selected=True, header_row=2, key_columns=['Activity_ID', 'Week_ID'], update_mode='auto'
    )
    snap_id = db.create_snapshot(did, sfid, ssid, 'Versi Uji 01', 'Control_Project.xlsx')
    snaps = db.list_snapshots(source_sheet_id=ssid)
    assert snap_id in snaps['id'].tolist()
    snap_df = db.get_snapshot_dataframe(snap_id)
    assert len(snap_df) == len(db.get_dataframe(did))

    dash = db.list_dashboard_sheets(pid)
    assert 'Progress' in dash['sheet_name'].tolist()
    assert 'Kurva S' in dash['sheet_name'].tolist()

    # v2.7.2: overwrite active file without creating a new version, then purge source completely.
    purge_df = pd.DataFrame({'Doc_ID':['D1','D2'], 'Status':['Open','Closed']})
    purge_did = db.create_dataset(purge_df, pid, 'Other', 'Purge test dataset')
    purge_sfid = db.upsert_source_file(pid, 'Delete_Me.xlsx', 'Other', 'File khusus test hapus', 123)
    db.sync_source_sheet_selection(pid, purge_sfid, ['Data'], ['Data'], 'Other')
    purge_ssid = db.save_source_sheet_config(
        pid, purge_sfid, 'Data', dataset_id=purge_did, function_name='Other',
        display_name='Data', description='', selected=True, header_row=1, key_columns=['Doc_ID'], update_mode='auto'
    )
    db.create_snapshot(purge_did, purge_sfid, purge_ssid, 'Snapshot Purge', 'Delete_Me.xlsx')
    physical1 = Path(tmp.name) / 'active_v1.xlsx'
    physical1.write_bytes(b'first')
    vid1 = db.add_source_file_version(pid, purge_sfid, 'Aktif 01', 'Delete_Me.xlsx', str(physical1), physical1.stat().st_size)
    assert len(db.list_source_file_versions(purge_sfid)) == 1
    physical2 = Path(tmp.name) / 'active_v2.xlsx'
    physical2.write_bytes(b'second')
    vid2 = db.overwrite_current_source_version(purge_sfid, 'Delete_Me.xlsx', str(physical2), physical2.stat().st_size, 'Timpa 02')
    assert vid2 == vid1
    versions_after = db.list_source_file_versions(purge_sfid)
    assert len(versions_after) == 1
    assert versions_after.iloc[0]['label'] == 'Timpa 02'
    assert db.get_source_file(purge_sfid)['current_file_path'] == str(physical2)

    purge = db.delete_source_file_completely(purge_sfid)
    assert purge['removed_datasets'] == 1
    assert purge['removed_snapshots'] == 1
    assert db.get_source_file_by_name(pid, 'Delete_Me.xlsx') is None

    p = db.get_project(pid)
    assert p['bimx_url'] == 'https://example.com/bimx'
    print('ALL_CORE_TESTS_OK')


if __name__ == '__main__':
    run()
