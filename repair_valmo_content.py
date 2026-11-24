#!/usr/bin/env python3
"""Fill existing Valmo-imported cards' missing description, equipment and specs.

Read-only by default; exact XML_ID required. Never writes images or prices.
Use fresh source_products.json produced by the patched crawler.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.models import Item
from catalog_sync_core.runtime import config_load, make_payload
LOG = logging.getLogger('valmo-content-repair')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--snapshot-dir', default='output')
    p.add_argument('--report', default='output/valmo_content_repair.jsonl')
    p.add_argument('--source-url-contains', default='', help='Filter supplier URL, e.g. x6000-4x21361')
    p.add_argument('--element-id', type=int)
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--confirm-content', action='store_true')
    return p.parse_args(argv)


def run(args):
    if args.apply and not args.confirm_content:
        raise ValueError('Для записи добавь --confirm-content')
    if args.limit < 0 or args.element_id is not None and args.element_id <= 0:
        raise ValueError('Некорректный лимит или ID')
    if args.source_url_contains and len(args.source_url_contains) < 8:
        raise ValueError('--source-url-contains должен содержать не менее 8 символов')
    source_path = Path(args.snapshot_dir) / 'source_products.json'
    source_rows = json.loads(source_path.read_text(encoding='utf-8'))
    source = [Item.from_dict(x) for x in source_rows if x.get('source') == 'valmo']
    if not source:
        raise RuntimeError('Нет Valmo в снимке; сначала запусти полный сбор каталога')
    # Require post-patch snapshot, not an old meta-only description.
    if not any(x.source_conflicts or x.equipment or ' — подробности' in ' '.join(x.specs) for x in source):
        raise RuntimeError('Снимок не содержит данных нового парсера. Сначала запусти python catalog_sync.py --out output')
    cfg = config_load(Path(args.config))
    client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(cfg['network']['timeout_sec']),
                          read_attempts=int(cfg['network'].get('bitrix_read_attempts', 3)))
    metadata = client.metadata()
    from catalog_sync_core.taxonomy import Taxonomy
    tc = cfg['taxonomy']
    taxonomy = Taxonomy(metadata['sections'], cfg['target']['excluded_section_codes'],
                        minimum_score=tc['minimum_match_score'], minimum_margin=tc['minimum_margin'],
                        aliases=tc.get('section_aliases', {}))
    current = client.products(cfg['target']['catalog_url'], taxonomy)
    target = {}
    for t in current:
        if t.xml_id.startswith('valmo:'):
            if t.xml_id in target:
                raise RuntimeError('Повтор XML_ID в Битрикс: ' + t.xml_id)
            target[t.xml_id] = t
    report = []
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    def append_report(row):
        # Append-only: avoids Windows antivirus/locking failures on os.replace().
        with report_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    for item in source:
        if args.source_url_contains and args.source_url_contains.casefold() not in item.url.casefold():
            continue
        xml = f'valmo:{item.source_id}'
        dest = target.get(xml)
        if not dest or not dest.bitrix_id or args.element_id and dest.bitrix_id != args.element_id:
            continue
        if args.limit and len(report) >= args.limit:
            break
        if dest.section_id <= 0:
            report.append({'element_id': dest.bitrix_id, 'xml_id': xml, 'status': 'skipped',
                           'error': 'Неизвестен текущий раздел товара'})
            append_report(report[-1])
            continue
        item.section_id = dest.section_id  # Payload only; PHP does not change sections.
        data = make_payload(item, metadata)
        content = {k: data[k] for k in ('description', 'characteristics', 'equipment')}
        content['card_properties'] = data.get('card_properties', {})
        content.update(element_id=dest.bitrix_id, expected_xml_id=xml, apply=args.apply,
                       expected_detail_text=dest.description,
                       replace_imported_placeholder=True)
        result = {'element_id': dest.bitrix_id, 'xml_id': xml,
                  'title': item.title, 'source_conflicts': item.source_conflicts}
        unavailable = False
        try:
            r = client.http.post(client.base + 'repair_valmo_content.php', json=content,
                                 timeout=client.timeout, allow_redirects=False)
            if r.status_code in (404, 405):
                unavailable = True
                raise RuntimeError('Новый PHP endpoint не установлен или путь неверен (HTTP %d)' % r.status_code)
            body = r.json()
            if r.status_code != 200 or body.get('success') is not True:
                raise RuntimeError(f'HTTP {r.status_code}: {body.get("error", "unknown")}')
            result.update(status='updated' if args.apply else 'preview', changes=body)
        except Exception as exc:
            # A POST might have committed before the socket went away. Never
            # retry this call blindly; next launch re-reads server state.
            result.update(status='error', error=str(exc))
        report.append(result)
        append_report(result)
        LOG.info('ID %d: %s; конфликтов %d', dest.bitrix_id, result['status'], len(item.source_conflicts))
        if unavailable:
            break
    print(f'Valmo: обработано {len(report)}; режим: {"запись" if args.apply else "просмотр"}; отчёт: {args.report}')
    return 1 if any(row['status'] == 'error' for row in report) else 0


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(args)
    except Exception as exc:
        LOG.exception('Ошибка: %s', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
