#!/usr/bin/env python3
"""Repair already imported cards by exact XML_ID; never create or relink products.

Default is read-only. Requires the existing output/source_products.json snapshot.
Always reloads the current Bitrix catalog and requires an exact XML_ID match.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.card_properties import card_properties, card_warnings
from catalog_sync_core.identity import configuration
from catalog_sync_core.checkpoints import write_json
from catalog_sync_core.models import Item
from catalog_sync_core.photos import download_images
from catalog_sync_core.runtime import config_load, gallery_property
from catalog_sync_core.sources import Crawler, parse_page
from catalog_sync_core.taxonomy import Taxonomy

LOG = logging.getLogger('repair-cards')



class ReportWriter:
    """Keep the Bitrix repair running if Windows blocks an atomic report rename.

    Normal mode uses atomic JSON snapshots. If Windows still denies a rename
    after retries, switch to a new uniquely named report and save it directly.
    This fallback sacrifices atomicity for the report ONLY, never for Bitrix
    writes or catalog checkpoints. A failed report write is reported explicitly.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.direct_mode = False
        self.last_error = None

    def _save_direct(self, report: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('w', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())

    def save(self, report: list[dict]) -> None:
        if self.direct_mode:
            try:
                self._save_direct(report)
                self.last_error = None
            except OSError as exc:
                self.last_error = exc
                LOG.error('Не удалось обновить запасной отчёт %s: %s; обработка продолжается',
                          self.path, exc)
            return
        try:
            write_json(self.path, report)
            self.last_error = None
        except PermissionError as exc:
            requested = self.path
            # Avoid the locked destination AND the repeated .tmp rename.
            suffix = requested.suffix or '.json'
            self.path = requested.with_name(
                f'{requested.stem}_recovered_{os.getpid()}_{time.time_ns()}{suffix}'
            )
            self.direct_mode = True
            LOG.warning('Windows заблокировала атомарное сохранение %s (%s). '
                        'Продолжаем запись в %s без переименования .tmp',
                        requested, exc, self.path)
            try:
                self._save_direct(report)
                self.last_error = None
            except OSError as fallback_exc:
                self.last_error = fallback_exc
                LOG.error('Не удалось записать запасной отчёт %s: %s; '
                          'обработка продолжается', self.path, fallback_exc)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--snapshot-dir', default='output', help='Existing directory with source_products.json')
    p.add_argument('--report', default='repair_report.json')
    p.add_argument('--apply', action='store_true', help='Actually fill missing card properties')
    p.add_argument('--source', choices=('valmo', 'gkgross', 'liftnet'))
    p.add_argument('--element-id', type=int, help='Fix only this Bitrix element, for a safe first run')
    p.add_argument('--limit', type=int, default=0, help='Maximum number of imported products (0=all)')
    p.add_argument('--repair-images', action='store_true', help='Also refresh importer-owned photos, not manual photos')
    p.add_argument('--confirm-image-replacement', action='store_true', help='Required with --apply --repair-images')
    p.add_argument('--verbose', action='store_true')
    return p.parse_args(argv)


def snapshot_items(path: Path) -> list[Item]:
    if not path.is_file():
        raise FileNotFoundError(f'Нет {path}. Укажите каталог существующего запуска через --snapshot-dir')
    rows = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise ValueError('source_products.json должен быть списком')
    return [Item.from_dict(row) for row in rows]


def fresh_vendor_item(old: Item, provider: dict, crawler: Crawler) -> Item:
    if not crawler.allowed(old.url):
        raise ValueError(f'Посторонний URL поставщика: {old.url}')
    response = crawler.get(old.url)
    if response is None:
        raise RuntimeError('Не удалось прочитать актуальную карточку поставщика')
    item = parse_page(response.text, old.url, old.source,
                      path_prefix=provider['product_prefix'],
                      id_pattern=provider.get('source_id_pattern', ''),
                      min_product_path_segments=int(provider.get('min_product_path_segments', 0)))
    if not item or item.source_id != old.source_id:
        raise ValueError('Карточка поставщика изменила идентификатор; фотографии не трогаем')
    return item


def run(args) -> int:
    if args.limit < 0 or (args.element_id is not None and args.element_id <= 0):
        raise ValueError('Недопустимые --limit или --element-id')
    if args.apply and args.repair_images and not args.confirm_image_replacement:
        raise ValueError('--apply --repair-images требует --confirm-image-replacement')
    cfg = config_load(Path(args.config))
    source = snapshot_items(Path(args.snapshot_dir) / 'source_products.json')
    client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(cfg['network']['timeout_sec']),
                          read_attempts=int(cfg['network'].get('bitrix_read_attempts', 3)))
    metadata = client.metadata()
    tc = cfg['taxonomy']
    taxonomy = Taxonomy(metadata['sections'], cfg['target']['excluded_section_codes'],
                        minimum_score=tc['minimum_match_score'], minimum_margin=tc['minimum_margin'],
                        aliases=tc.get('section_aliases', {}))
    current = client.products(cfg['target']['catalog_url'], taxonomy)
    by_xml = {}
    for item in current:
        if item.xml_id:
            if item.xml_id in by_xml:
                raise RuntimeError(f'Повторяющийся XML_ID в Битрикс: {item.xml_id}')
            by_xml[item.xml_id] = item
    gallery = gallery_property(metadata, cfg['images'].get('gallery_property', 'auto'))
    providers = {p['name']: p for p in cfg['sources']}
    crawlers = {}
    report = []
    report_writer = ReportWriter(Path(args.report))
    seen = set()
    for item in source:
        if item.source not in ('valmo', 'gkgross', 'liftnet'):
            continue
        if args.source and args.source != item.source:
            continue
        if not item.source_id:
            continue
        xml = f'{item.source}:{item.source_id}'
        if xml in seen:
            continue
        seen.add(xml)
        target = by_xml.get(xml)
        if not target or target.bitrix_id <= 0:
            continue  # This command does not create products or resolve fuzzy links.
        if args.element_id and target.bitrix_id != args.element_id:
            continue
        if args.limit and len(report) >= args.limit:
            break
        props = card_properties(item)
        power = configuration(item).get('power_hp')
        if power and not props.get('POWER'):
            props['POWER'] = str(power)[:255]
        # Skip fields the existing card already has. Server rechecks this atomically.
        missing = {code: value for code, value in props.items()
                   if not target.specs.get(code, '').strip()}
        warnings = card_warnings(item)
        status = {'element_id': target.bitrix_id, 'xml_id': xml, 'title': target.title,
                  'missing_properties': missing, 'wheel_schema_requested': bool(props.get('FORM_WHEEL')),
                  'repair_images': args.repair_images,
                  'status': 'needs_review' if warnings else 'dry_run'}
        if warnings:
            status['warnings'] = warnings
        report.append(status)
        report_writer.save(report)
        # Never write an ambiguous product, even when --apply was requested.
        if warnings or not args.apply:
            continue
        try:
            response = client.post('repair.php', {'element_id': target.bitrix_id,
                                                   'expected_xml_id': xml,
                                                   'card_properties': props, 'apply': True})
            status.update({'status': 'repaired', 'server': response})
        except Exception as exc:
            status.update({'status': 'error', 'error': f'Card properties: {exc}'})
            report_writer.save(report)
            continue
        if args.repair_images:
            try:
                provider = providers[item.source]
                crawler = crawlers.get(item.source)
                if crawler is None:
                    crawler = Crawler(provider, timeout=int(cfg['network']['timeout_sec']),
                                      delay=float(cfg['network']['delay_sec']),
                                      max_pages=1, max_listing_pages=1)
                    crawlers[item.source] = crawler
                current_vendor = fresh_vendor_item(item, provider, crawler)
                photos, warnings = download_images(current_vendor, provider,
                    timeout=int(cfg['network']['timeout_sec']),
                    max_images=int(cfg['images']['max_images_per_product']),
                    max_download_bytes=int(cfg['images']['max_bytes_each']))
                status['image_warnings'] = warnings
                if not photos:
                    status['images'] = 'skipped: no verified product photos'
                else:
                    result = client.upload_photos(current_vendor, target.bitrix_id, xml, gallery,
                                                  photos, replace_imported_images=True)
                    status['images'] = result
            except Exception as exc:
                status.update({'status': 'image_error', 'image_error': str(exc)})
        report_writer.save(report)
        LOG.info('ID %d: %s', target.bitrix_id, status['status'])
    report_writer.save(report)
    print(f'Карточек: {len(report)}; режим: {"ЗАПИСЬ" if args.apply else "ПРОСМОТР"}; отчёт: {report_writer.path}')
    if report_writer.last_error:
        print(f'ВНИМАНИЕ: последний отчёт не удалось сохранить: {report_writer.last_error}')
    for row in report[:15]:
        print(f"  {row['element_id']}: {row['status']} ({', '.join(row['missing_properties']) or 'без пустых полей'})")
    review = [row for row in report if row['status'] == 'needs_review']
    if review:
        print(f'ВНИМАНИЕ: {len(review)} карточек пропущено из-за противоречий в данных:')
        for row in review[:30]:
            print(f"  ID {row['element_id']}: {'; '.join(row['warnings'])}")
    return 1 if report_writer.last_error or any(x['status'] in ('error', 'image_error', 'needs_review') for x in report) else 0


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(args)
    except Exception as exc:
        LOG.exception('Исправление остановлено: %s', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
