#!/usr/bin/env python3
"""Safely deduplicate generic characteristics and fill empty native card fields.

Works on imported Valmo/GKGross/Liftnet cards, matched by exact XML_ID.
Read-only by default. Does not touch any images or wheel schematics.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
from pathlib import Path
import uuid

from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.card_properties import card_warnings
from catalog_sync_core.runtime import config_load
from catalog_sync_core.sources import Crawler
from catalog_sync_core.taxonomy import Taxonomy
from fix_valmo_cards import specs_request, write_log
from repair_cards import snapshot_items, fresh_vendor_item

LOG = logging.getLogger('repair-imported-specs')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/catalog.json')
    parser.add_argument('--snapshot-dir', default='output')
    parser.add_argument('--report', default='output/imported_specs_preview.jsonl')
    parser.add_argument('--source', choices=['valmo', 'gkgross', 'liftnet'])
    parser.add_argument('--element-id', type=int)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-specs', action='store_true')
    parser.add_argument('--confirm-bulk', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if (args.element_id is not None and args.element_id < 1) or args.limit < 0:
        parser.error('Invalid element ID or limit')
    if args.apply and not args.confirm_specs:
        parser.error('--apply requires --confirm-specs')
    if args.apply and args.element_id is None and not args.confirm_bulk:
        parser.error('Bulk write requires --confirm-bulk')
    if not args.source and not args.element_id and not args.limit:
        parser.error('For broad scans specify --source, --element-id or --limit')
    try:
        cfg = config_load(Path(args.config))
        previous = snapshot_items(Path(args.snapshot_dir) / 'source_products.json')
        client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                              timeout=int(cfg['network']['timeout_sec']))
        meta = client.metadata()
        tc = cfg['taxonomy']
        taxonomy = Taxonomy(meta['sections'], cfg['target']['excluded_section_codes'],
                            minimum_score=tc['minimum_match_score'], minimum_margin=tc['minimum_margin'],
                            aliases=tc.get('section_aliases', {}))
        current = {x.xml_id: x for x in client.products(cfg['target']['catalog_url'], taxonomy) if x.xml_id}
        providers = {p['name']: p for p in cfg['sources']}
        entries = []
        seen = set()
        for item in previous:
            if not item.source_id or item.source not in ('valmo', 'gkgross', 'liftnet'):
                continue
            if args.source and item.source != args.source:
                continue
            xml = f'{item.source}:{item.source_id}'
            target = current.get(xml)
            if not target or not target.bitrix_id or xml in seen or (args.element_id and target.bitrix_id != args.element_id):
                continue
            seen.add(xml)
            entries.append((item, target, xml))
        entries.sort(key=lambda x: x[1].bitrix_id)
        if args.limit:
            entries = entries[:args.limit]
        if args.element_id and not entries:
            raise RuntimeError('Target element not found by exact imported XML_ID')
        crawlers = {}
        report = Path(args.report)
        run_id = str(uuid.uuid4())
        write_log(report, {'event': 'begin', 'run_id': run_id, 'count': len(entries),
                           'apply': args.apply, 'utc': dt.datetime.now(dt.timezone.utc).isoformat()})
        errors = 0
        for old, target, xml in entries:
            outcome = {'event': 'item', 'run_id': run_id, 'element_id': target.bitrix_id,
                       'xml_id': xml, 'source_url': old.url}
            try:
                if old.source not in crawlers:
                    crawlers[old.source] = Crawler(providers[old.source],
                        timeout=int(cfg['network']['timeout_sec']),
                        delay=float(cfg['network']['delay_sec']), max_pages=1, max_listing_pages=1)
                fresh = fresh_vendor_item(old, providers[old.source], crawlers[old.source])
                warnings = card_warnings(fresh)
                if warnings:
                    outcome.update({'status': 'needs_review', 'warnings': warnings})
                else:
                    outcome['specs'] = specs_request(client, element_id=target.bitrix_id,
                        xml_id=xml, fresh=fresh, apply=args.apply)
                    outcome['status'] = 'applied' if args.apply else 'preview'
            except Exception as e:
                errors += 1
                outcome.update({'status': 'error', 'error': str(e)})
                LOG.error('ID %d: %s', target.bitrix_id, e)
            write_log(report, outcome)
        write_log(report, {'event': 'end', 'run_id': run_id, 'items': len(entries), 'errors': errors})
        print(f'Карточек: {len(entries)}; ошибок: {errors}; отчет: {report}')
        return 1 if errors else 0
    except Exception as e:
        LOG.exception('Repair aborted: %s', e)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
