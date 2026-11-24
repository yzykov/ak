#!/usr/bin/env python3
"""Remove verified importer-owned MORE_PHOTO entries while keeping main pictures.

Dry-run by default. Uses the existing Bitrix list API and exact XML_ID checks.
For write mode: --apply --confirm-gallery-cleanup.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import uuid

from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.runtime import config_load
from catalog_sync_core.taxonomy import Taxonomy

LOG = logging.getLogger('cleanup-gallery')
XML_ID = re.compile(r'^(valmo|gkgross|liftnet):[A-Za-z0-9_-]{1,100}$')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--element-id', type=int, default=0)
    p.add_argument('--source', choices=('valmo', 'gkgross', 'liftnet'))
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--confirm-gallery-cleanup', action='store_true')
    p.add_argument('--report', default='output/gallery_cleanup.jsonl', help='Append-only JSONL report; no os.replace')
    return p.parse_args(argv)


def audit(path: Path, row: dict) -> None:
    """Append one complete JSON line instead of rename-based Windows checkpointing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def do_request(client: BitrixClient, *, element_id: int, xml_id: str, apply: bool) -> dict:
    response = client.http.post(client.base + 'gallery_cleanup.php',
                                json={'element_id': element_id, 'expected_xml_id': xml_id, 'apply': apply},
                                timeout=client.timeout, allow_redirects=False)
    try:
        payload = response.json()
    except ValueError as e:
        raise RuntimeError(f'gallery_cleanup.php: HTTP {response.status_code}, response is not JSON') from e
    if response.status_code != 200 or not isinstance(payload, dict) or payload.get('success') is not True:
        raise RuntimeError(f'gallery_cleanup.php: HTTP {response.status_code}: {payload.get("error", "unknown") if isinstance(payload, dict) else "invalid response"}')
    if payload.get('element_id') != element_id or payload.get('dry_run') is not (not apply):
        raise RuntimeError('Unexpected response (ID or mode mismatch)')
    return payload


def run(args) -> int:
    if args.apply and not args.confirm_gallery_cleanup:
        raise ValueError('Для удаления укажи оба флага: --apply --confirm-gallery-cleanup')
    if args.limit < 0 or args.element_id < 0:
        raise ValueError('Invalid limit/element ID')
    config = config_load(Path(args.config))
    client = BitrixClient(config['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(config['network']['timeout_sec']),
                          read_attempts=int(config['network'].get('bitrix_read_attempts', 3)))
    meta = client.metadata()
    tc = config['taxonomy']
    taxonomy = Taxonomy(meta['sections'], config['target']['excluded_section_codes'],
                        minimum_score=tc['minimum_match_score'], minimum_margin=tc['minimum_margin'],
                        aliases=tc.get('section_aliases', {}))
    elements = client.products(config['target']['catalog_url'], taxonomy)
    selected = [e for e in elements if XML_ID.fullmatch(e.xml_id)
                and (not args.source or e.xml_id.startswith(args.source + ':'))
                and (not args.element_id or e.bitrix_id == args.element_id)]
    selected.sort(key=lambda e: e.bitrix_id)
    if args.limit:
        selected = selected[:args.limit]
    if args.element_id and not selected:
        raise RuntimeError(f'ID {args.element_id} отсутствует среди импортированных товаров; ничего не удалено')
    report = Path(args.report)
    run_id = str(uuid.uuid4())
    audit(report, {'event': 'begin', 'run_id': run_id, 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'apply': args.apply, 'count': len(selected)})
    errors = 0
    to_remove = removed = unknown = 0
    for item in selected:
        record = {'event': 'item', 'run_id': run_id, 'element_id': item.bitrix_id, 'xml_id': item.xml_id}
        try:
            result = do_request(client, element_id=item.bitrix_id, xml_id=item.xml_id, apply=args.apply)
            record.update(result)
            to_remove += int(result.get('gallery_marked_for_removal', 0))
            removed += int(result.get('gallery_removed', 0))
            unknown += int(result.get('gallery_preserved_unknown', 0))
            if args.apply and (result.get('warning') or result.get('gallery_marked_for_removal') != result.get('gallery_removed')):
                errors += 1
            LOG.info('ID %d: галерея %s, импортных %s, удалено %s, неизвестных сохранено %s',
                     item.bitrix_id, result.get('gallery_found'), result.get('gallery_marked_for_removal'),
                     result.get('gallery_removed'), result.get('gallery_preserved_unknown'))
        except Exception as e:
            errors += 1
            record['error'] = str(e)
            LOG.error('ID %d: %s', item.bitrix_id, e)
        audit(report, record)
    audit(report, {'event': 'end', 'run_id': run_id, 'apply': args.apply,
                   'items': len(selected), 'gallery_marked_for_removal': to_remove,
                   'gallery_removed': removed, 'gallery_preserved_unknown': unknown, 'errors': errors})
    print(f'Товаров: {len(selected)}; фото к удалению: {to_remove}; удалено: {removed}; '
          f'неизвестные сохранены: {unknown}; ошибки: {errors}; отчет: {report}')
    return 1 if errors else 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(parse_args(argv))
    except Exception as e:
        LOG.exception('Gallery cleanup failed: %s', e)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
