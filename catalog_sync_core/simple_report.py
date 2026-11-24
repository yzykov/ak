"""Simple Excel + machine-readable report for S3 sync."""
from __future__ import annotations

import csv
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from .checkpoints import write_json

FIELDS = ['action', 'source', 'source_id', 'source_url', 'title', 'element_id', 'section_id',
          'reason', 'expected_name', 'expected_xml_id', 'expected_active',
          'expected_price', 'expected_price_flag', 'price_rub', 'price_is_from']
GROUPS = ('keep', 'link', 'create', 'activate', 'update_price', 'deactivate', 'pending_category', 'blocked', 'protected')


def report_simple(plan, out: Path) -> None:
    write_json(out / 'sync_plan.json', plan.dump())
    all_rows = []
    for group in GROUPS:
        for d in getattr(plan, group):
            all_rows.append({'action': group, **d.to_dict()})
    with (out / 'sync_plan.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_rows)
    wb = Workbook()
    summary = wb.active
    summary.title = 'Сводка'
    summary.append(['Операция', 'Количество'])
    for group in GROUPS:
        summary.append([group, len(getattr(plan, group))])
    summary.append(['Запись разрешена', str(plan.safe_to_write)])
    for group in GROUPS:
        ws = wb.create_sheet(group)
        ws.append(FIELDS)
        for d in getattr(plan, group):
            value = d.to_dict()
            ws.append([str(value.get(field, '')) for field in FIELDS])
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for c in ws[1]:
            c.fill = PatternFill('solid', fgColor='17365D')
            c.font = Font(bold=True, color='FFFFFF')
        for col, width in {'A': 23, 'B': 20, 'C': 35, 'D': 55, 'E': 65, 'F': 15,
                           'G': 15, 'H': 85, 'I': 65, 'J': 40, 'K': 20}.items():
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical='top', wrap_text=True)
    for name, lines in [('Ошибки', plan.errors), ('Предупреждения', plan.warnings)]:
        ws = wb.create_sheet(name)
        for line in lines or ['Нет записей']:
            ws.append([line])
        ws.column_dimensions['A'].width = 110
    wb.save(out / 'catalog_sync.xlsx')
