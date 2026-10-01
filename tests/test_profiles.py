from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.visuals import extract_workbook_visuals

sample=ROOT/'sample_data'/'06_Rencana_2_Mingguan_RA_RI.xlsx'
payloads=extract_workbook_visuals(sample)
assert set(payloads)=={'2 Minggu Lalu','2 Minggu ke depan'}
actual=payloads['2 Minggu Lalu']
forward=payloads['2 Minggu ke depan']
assert actual['kind']=='lookahead' and actual['mode']=='actual'
assert forward['kind']=='lookahead' and forward['mode']=='forward'
assert actual['task_count']==36 and forward['task_count']==36
assert {'RA Start','RA Finish','RI Start','RI Finish','Status'}.issubset(actual['tasks'].columns)
assert {'RA Start','RA Finish'}.issubset(forward['tasks'].columns)
print('PROFILE_TESTS_OK')
