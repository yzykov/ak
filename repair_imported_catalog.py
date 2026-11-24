#!/usr/bin/env python3
"""Repair old imported prices and move misfiled truck chassis. Dry-run by default."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.models import Item
from catalog_sync_core.runtime import config_load
from catalog_sync_core.taxonomy import Taxonomy, is_truck_chassis, commercial_import_destination

LOG = logging.getLogger('repair-imported-catalog')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--snapshot-dir', default='output')
    p.add_argument('--report', default='output/imported_catalog_repair.jsonl')
    p.add_argument('--mode', choices=('prices', 'chassis', 'categories', 'all'), default='all')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--confirm-write', action='store_true')
    return p.parse_args(argv)


def plan_operations(source: list[Item], targets: list[Item], taxonomy: Taxonomy, mode: str) -> list[dict]:
    by_xml = {}
    for t in targets:
        if t.xml_id in by_xml:
            raise ValueError('Duplicate target XML_ID: ' + t.xml_id)
        by_xml[t.xml_id] = t
    operations = []
    for s in source:
        xml = s.source + ':' + s.source_id
        target = by_xml.get(xml)
        if not target or target.section_id <= 0:
            continue  # Not imported / not proven by exact XML_ID.
        if (mode in ('prices', 'all') and s.source in ('gkgross', 'liftnet')
                and s.price_rub > 0):
            existing = str(target.specs.get('PRICE_NEW', '')).strip()
            # Server verifies live values; it never overwrites a different existing price.
            operations.append({'operation': 'sync_price', 'element_id': target.bitrix_id,
                               'expected_xml_id': xml, 'expected_section_id': target.section_id,
                               'price_rub': s.price_rub, 'price_is_from': bool(s.price_is_from),
                               'source_url': s.url})
        if (mode in ('chassis', 'all') and s.source in ('gkgross', 'liftnet', 'valmo')
                and is_truck_chassis(target.title, target.brand or s.brand)):
            lcv_ids = [x.id for x in taxonomy.sections.values() if x.code == 'kommercheskiy-transport']
            if len(lcv_ids) != 1:
                raise ValueError('LCV section missing or ambiguous')
            if taxonomy.is_descendant(target.section_id, lcv_ids[0]):
                operations.append({'operation': 'move_chassis', 'element_id': target.bitrix_id,
                                   'expected_xml_id': xml, 'expected_section_id': target.section_id,
                                   'source_url': s.url})
        # Existing buses and grain carriers previously imported to commercial
        # root must be moved to their own child sections. Preserve DONGFENG LCV.
        if mode in ('categories', 'all'):
            target_code = commercial_import_destination(s)
            if target_code:
                roots = [x.id for x in taxonomy.sections.values()
                         if x.code == 'kommercheskiy-transport' and x.active]
                destinations = [x for x in taxonomy.sections.values()
                                if x.code == target_code and x.active]
                if len(roots) != 1 or len(destinations) != 1 or destinations[0].parent_id != roots[0]:
                    raise ValueError('Отсутствует или неоднозначен подраздел ' + target_code
                                     + '; сначала setup_commercial_sections.py')
                if (target.section_id != destinations[0].id and
                        taxonomy.is_descendant(target.section_id, roots[0])):
                    operations.append({'operation': 'move_category', 'element_id': target.bitrix_id,
                                       'expected_xml_id': xml,
                                       'expected_section_id': target.section_id,
                                       'target_code': target_code, 'source_url': s.url})
    return operations


def run(args):
    if args.apply and not args.confirm_write:
        raise ValueError('Для изменений требуется --apply --confirm-write')
    if args.limit < 0:
        raise ValueError('--limit must not be negative')
    cfg = config_load(Path(args.config))
    datafile = Path(args.snapshot_dir) / 'source_products.json'
    source = [Item.from_dict(x) for x in json.loads(datafile.read_text(encoding='utf-8'))]
    client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(cfg['network']['timeout_sec']),
                          read_attempts=int(cfg['network'].get('bitrix_read_attempts', 3)))
    meta = client.metadata()
    rules = cfg['taxonomy']
    tax = Taxonomy(meta['sections'], cfg['target']['excluded_section_codes'],
                   minimum_score=rules['minimum_match_score'],
                   minimum_margin=rules['minimum_margin'],
                   aliases=rules.get('section_aliases', {}))
    # Always read live Bitrix state; a prior --apply may have partly succeeded.
    current = client.products(cfg['target']['catalog_url'], tax)
    ops = plan_operations(source, current, tax, args.mode)
    if args.limit:
        ops = ops[:args.limit]
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    for i, operation in enumerate(ops, 1):
        payload = {k: v for k, v in operation.items() if k != 'source_url'}
        payload['apply'] = args.apply
        entry = {'n': i, **operation}
        try:
            response = client.http.post(client.base + 'repair_imported_catalog.php',
                                        json=payload, timeout=client.timeout,
                                        allow_redirects=False)
            try:
                body = response.json()
            except ValueError as exc:
                raise RuntimeError(f'HTTP {response.status_code}: non-JSON response') from exc
            if response.status_code != 200 or body.get('success') is not True:
                raise RuntimeError(f'HTTP {response.status_code}: {body.get("error", "unknown")}')
            entry.update(status='ok', server=body)
        except Exception as exc:
            # This POST might have executed. Never blindly resend; rerunning
            # the command will fetch the current state and skip finished rows.
            entry.update(status='error', error=str(exc))
            failures += 1
        with out.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        LOG.info('%d/%d: %s #%s %s', i, len(ops), operation['operation'],
                 operation['element_id'], entry['status'])
    print(f'Режим: {"ЗАПИСЬ" if args.apply else "ПРОСМОТР"}; запланировано {len(ops)}, ошибок {failures}; отчёт {out}')
    return 1 if failures else 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(parse_args(argv))
    except Exception as exc:
        LOG.exception('Ошибка: %s', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
