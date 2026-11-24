#!/usr/bin/env python3
"""Audit and carefully repair Valmo imported cards, including swiper photos.

Dry run by default. Writes an append-only JSONL log (Windows-friendly).
Photo and specification writes require independent explicit confirmation flags.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
import uuid

from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.card_properties import card_properties, card_warnings, covered_spec_entries
from catalog_sync_core.identity import configuration, record_key
from catalog_sync_core.models import Item
from catalog_sync_core.photos import download_images
from catalog_sync_core.runtime import config_load
from catalog_sync_core.taxonomy import Taxonomy
from catalog_sync_core.sources import Crawler, parse_page
from repair_cards import snapshot_items, fresh_vendor_item

LOG = logging.getLogger('fix-valmo')


def args_parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/catalog.json')
    parser.add_argument('--snapshot-dir', default='output')
    parser.add_argument('--source-url', help='Exact Valmo product URL for a single --element-id; skips source_products.json')
    parser.add_argument('--capture-html', help='Save actual supplier response HTML for gallery diagnostics (preview mode, one element)')
    parser.add_argument('--report', default='output/valmo_fix_preview.jsonl')
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument('--element-id', type=int)
    select.add_argument('--all-valmo', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--apply-specs', action='store_true')
    parser.add_argument('--confirm-specs', action='store_true')
    parser.add_argument('--apply-photos', action='store_true')
    parser.add_argument('--confirm-photos', action='store_true')
    parser.add_argument('--confirm-bulk', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    return parser.parse_args(argv)


def write_log(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as file:
        file.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n')
        file.flush()
        os.fsync(file.fileno())


def specs_request(client: BitrixClient, *, element_id: int, xml_id: str, fresh, apply: bool):
    props = card_properties(fresh)
    power = configuration(fresh).get('power_hp')
    if power and not props.get('POWER'):
        props['POWER'] = str(power)[:255]
    removals = covered_spec_entries(fresh, props)
    if props.get('BRAND') and fresh.brand:
        removals.append({'name': 'Бренд', 'value': fresh.brand, 'property_code': 'BRAND'})
    payload = {'element_id': element_id, 'expected_xml_id': xml_id, 'apply': apply,
               'card_properties': props, 'remove_characteristics': removals}
    response = client.http.post(client.base + 'reconcile_specs.php', json=payload,
                                timeout=client.timeout, allow_redirects=False)
    try:
        body = response.json()
    except ValueError as e:
        raise RuntimeError(f'reconcile_specs.php: HTTP {response.status_code}, invalid JSON') from e
    if response.status_code != 200 or not isinstance(body, dict) or body.get('success') is not True:
        raise RuntimeError(f'reconcile_specs.php: HTTP {response.status_code}: '
                           f'{body.get("error", "unknown") if isinstance(body, dict) else "bad body"}')
    if body.get('element_id') != element_id or body.get('dry_run') is not (not apply):
        raise RuntimeError('reconcile_specs.php: unexpected response (ID or mode mismatch)')
    return body


def run(args) -> int:
    if args.element_id is not None and args.element_id <= 0 or args.limit < 0:
        raise ValueError('Invalid ID or limit')
    if args.apply_specs and not args.confirm_specs:
        raise ValueError('--apply-specs requires --confirm-specs')
    if args.apply_photos and not args.confirm_photos:
        raise ValueError('--apply-photos requires --confirm-photos')
    if args.all_valmo and (args.apply_specs or args.apply_photos) and not args.confirm_bulk:
        raise ValueError('Bulk modification requires --confirm-bulk after auditing one card')
    if args.source_url and args.element_id is None:
        raise ValueError('--source-url may only be used with --element-id')
    if args.capture_html and (args.element_id is None or args.apply_specs or args.apply_photos):
        raise ValueError('--capture-html requires one --element-id in preview mode')
    cfg = config_load(Path(args.config))
    provider = next(x for x in cfg['sources'] if x['name'] == 'valmo')
    if args.source_url:
        parsed = urlparse(args.source_url)
        allowed_hosts = set(provider['allowed_image_hosts'])
        prefix = provider['product_prefix'].rstrip('/') + '/'
        min_segments = int(provider.get('min_product_path_segments', 0))
        if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.port
                or not parsed.path.startswith(prefix)
                or len([piece for piece in parsed.path.split('/') if piece]) < min_segments):
            raise ValueError('Некорректный --source-url: требуется HTTPS-ссылка на товар Valmo')
        expected_source_id = record_key('valmo', args.source_url, provider.get('source_id_pattern', ''))
    else:
        source = snapshot_items(Path(args.snapshot_dir) / 'source_products.json')
    client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(cfg['network']['timeout_sec']))
    meta = client.metadata()
    tc = cfg['taxonomy']
    taxonomy = Taxonomy(meta['sections'], cfg['target']['excluded_section_codes'],
                        minimum_score=tc['minimum_match_score'], minimum_margin=tc['minimum_margin'],
                        aliases=tc.get('section_aliases', {}))
    targets = {x.xml_id: x for x in client.products(cfg['target']['catalog_url'], taxonomy) if x.xml_id}
    vals = []
    if args.source_url:
        xml = f'valmo:{expected_source_id}'
        target = targets.get(xml)
        if not target or target.bitrix_id != args.element_id:
            raise RuntimeError(f'XML_ID discrepancy: for ID {args.element_id} expected {xml}; '
                               'nothing was changed')
        old = Item(source='valmo', url=args.source_url, title=target.title,
                   source_id=expected_source_id)
        vals = [(old, target, xml)]
    else:
        visited = set()
        for item in source:
            if item.source != 'valmo' or not item.source_id:
                continue
            xml = f'valmo:{item.source_id}'
            target = targets.get(xml)
            if not target or target.bitrix_id <= 0 or xml in visited:
                continue
            visited.add(xml)
            if args.element_id and target.bitrix_id != args.element_id:
                continue
            vals.append((item, target, xml))
    vals.sort(key=lambda x: x[1].bitrix_id)
    if args.limit:
        vals = vals[:args.limit]
    if args.element_id and not vals:
        raise RuntimeError(f'ID {args.element_id} not found by exact XML_ID in snapshot')
    crawler = Crawler(provider, timeout=int(cfg['network']['timeout_sec']),
                      delay=float(cfg['network']['delay_sec']), max_pages=1,
                      max_listing_pages=1)
    run_id = str(uuid.uuid4())
    report = Path(args.report)
    write_log(report, {'event': 'begin', 'run_id': run_id, 'count': len(vals),
                       'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                       'apply_specs': args.apply_specs, 'apply_photos': args.apply_photos})
    error_count = 0
    for old, target, xml in vals:
        item_result = {'event': 'item', 'run_id': run_id, 'element_id': target.bitrix_id,
                       'xml_id': xml, 'source_url': old.url}
        try:
            if args.capture_html:
                # Use exactly the HTML seen by the production parser. This
                # is NOT a browser screenshot or the JS-rendered DOM.
                if not crawler.allowed(old.url):
                    raise ValueError('URL поставщика не разрешён')
                response = crawler.get(old.url)
                if response is None:
                    raise RuntimeError('Страница Valmo не загрузилась')
                html_path = Path(args.capture_html)
                html_path.parent.mkdir(parents=True, exist_ok=True)
                html_path.write_text(response.text, encoding='utf-8')
                fresh = parse_page(response.text, old.url, old.source,
                                   path_prefix=provider['product_prefix'],
                                   id_pattern=provider.get('source_id_pattern', ''),
                                   min_product_path_segments=int(provider.get('min_product_path_segments', 0)))
                if fresh is None or fresh.source_id != old.source_id:
                    raise ValueError('URL/идентификатор карточки изменился; запись запрещена')
                item_result['captured_html'] = str(html_path)
                item_result['html_bytes'] = len(response.content)
            else:
                fresh = fresh_vendor_item(old, provider, crawler)
            item_result.update({'title': fresh.title, 'photo_urls': fresh.images,
                                'photo_count': len(fresh.images),
                                'photos_from': 'valmo_product_swiper_only'})
            warnings = card_warnings(fresh)
            if warnings:
                item_result['warnings'] = warnings
                item_result['status'] = 'needs_review'
                # Skip BOTH actions if the page itself is contradictory.
            else:
                item_result['specs'] = specs_request(
                    client, element_id=target.bitrix_id, xml_id=xml,
                    fresh=fresh, apply=args.apply_specs)
                if args.apply_photos:
                    if not fresh.images:
                        raise RuntimeError('Valmo product swiper not found: refusing to replace existing photos')
                    photos, photo_warnings = download_images(
                        fresh, provider, timeout=int(cfg['network']['timeout_sec']),
                        max_images=int(cfg['images']['max_images_per_product']),
                        max_download_bytes=int(cfg['images']['max_bytes_each']))
                    item_result['photo_warnings'] = photo_warnings
                    if not photos:
                        raise RuntimeError('No valid supplier photos downloaded; existing photos untouched')
                    if len(photos) != min(len(fresh.images), int(cfg['images']['max_images_per_product'])):
                        raise RuntimeError('One or more supplier images were invalid/unavailable; refusing partial gallery replacement')
                    outcome = client.upload_photos(fresh, target.bitrix_id, xml, 'MORE_PHOTO',
                                                   photos, replace_imported_images=True)
                    if not outcome.get('main_image_saved') or outcome.get('gallery_saved') != len(photos):
                        raise RuntimeError(f'Images API reported inconsistent results: {outcome}')
                    item_result['photos'] = outcome
                item_result['status'] = 'applied' if (args.apply_specs or args.apply_photos) else 'preview'
        except Exception as exc:
            error_count += 1
            item_result['status'] = 'error'
            item_result['error'] = str(exc)
            LOG.error('ID %d: %s', target.bitrix_id, exc)
        write_log(report, item_result)
        LOG.info('ID %d: %s, swiper=%s', target.bitrix_id, item_result['status'], item_result.get('photo_count'))
    write_log(report, {'event': 'end', 'run_id': run_id, 'items': len(vals), 'errors': error_count})
    print(f'Products: {len(vals)}; errors: {error_count}; report: {report}')
    return 1 if error_count else 0


def main(argv=None):
    args = args_parse(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(args)
    except Exception as exc:
        LOG.exception('Fix stopped: %s', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
