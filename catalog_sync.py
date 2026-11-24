#!/usr/bin/env python3
"""Simplified catalog sync with Bitrix JSON as the only persistent matching store.

Default: read-only. Use --apply to save links, create products and update imported prices.
Use --allow-deactivate to additionally process deletion candidates after a clean crawl.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import requests

from catalog_sync_core.runtime import (collect, config_load, gallery_property, json_read,
                                 json_write, make_payload, notify)
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.photos import download_images
from catalog_sync_core.bitrix_registry import BitrixRegistry
from catalog_sync_core.simple_plan import build_simple_plan
from catalog_sync_core.simple_report import report_simple

LOG = logging.getLogger('catalog-sync-s3')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--out', default='output')
    p.add_argument('--init-registry', action='store_true', help='One-time Bitrix JSON registry initialization')
    p.add_argument('--apply', action='store_true', help='Create/link/activate using API + Bitrix JSON')
    p.add_argument('--activate-new', action='store_true', help='Activate newly created products with main image')
    p.add_argument('--allow-deactivate', action='store_true', help='Also deactivate confirmed missing cards')
    p.add_argument('--confirm-mass-deactivate', action='store_true', help='Allow deactivation above safety caps')
    p.add_argument('--reuse-snapshot', help='Use existing catalog snapshots for offline inspection; no writes')
    p.add_argument('--resume', action='store_true', help='Resume checkpoint-based supplier/Bitrix reads')
    p.add_argument('--category-wait-seconds', type=int, default=0,
                   help='Wait for Telegram category decisions (0 = disabled; requires --apply)')
    p.add_argument('--verbose', action='store_true')
    return p.parse_args(argv)


def _client(config):
    return BitrixClient(config['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                        timeout=int(config['network']['timeout_sec']),
                        read_attempts=int(config['network'].get('bitrix_read_attempts', 3)),
                        retry_delay=float(config['network'].get('bitrix_retry_delay_sec', 1.5)))


def _source_status(crawl, name):
    x = crawl.get(name, {})
    return bool(x.get('complete') and x.get('count', 0) > 0 and x.get('finished', True))


def apply_category_links(source, registry, categories, taxonomy):
    for item in source:
        if not item.category:
            continue
        chosen = categories.get((item.source, item.category.strip().casefold()))
        if chosen is None:
            continue
        if chosen not in taxonomy.sections or taxonomy.excluded(chosen) or not taxonomy.sections[chosen].active:
            raise ValueError(f'В Bitrix JSON неверный раздел для {item.source}/{item.category}: {chosen}')
        item.section_id = chosen
        item.section_match_confidence = 100



def pending_examples(plan, source):
    lookup = {(x.source, x.source_id): x for x in source}
    examples = {}
    for d in plan.pending_category:
        obj = lookup[(d.source, d.source_id)]
        if obj.category:
            examples.setdefault((obj.source, obj.category), obj.title)
    return examples


def wait_for_categories(plan, source, registry, args, out):
    """Register current unresolved categories before starting a bounded bot process."""
    if not args.apply or not args.category_wait_seconds or not plan.safe_to_write:
        return False
    examples = pending_examples(plan, source)
    if not examples:
        return False
    # This is the only write before the final plan: pending registry entries,
    # not product mutations. The bot must be able to read them immediately.
    for (name, category), example in examples.items():
        registry.set_pending_category(name, category, example)
    scope = out / 'category_approval_scope.json'
    json_write(scope, [[name, category.strip().casefold()] for name, category in examples])
    if not all(os.getenv(key, '').strip() for key in
               ('TG_BOT_TOKEN', 'TG_CHAT_ID', 'TG_ADMIN_USER_ID')):
        LOG.warning('Бот не запущен: не настроены переменные Telegram; категории сохранены')
        return False
    command = [sys.executable, str(Path(__file__).resolve().with_name('telegram_category_bot.py')),
               '--config', str(Path(args.config).resolve()),
               '--pending-file', str(scope.resolve()),
               '--wait-seconds', str(args.category_wait_seconds)]
    LOG.info('Ожидание согласования %d категорий (до %d секунд)', len(examples), args.category_wait_seconds)
    try:
        completed = subprocess.run(command, timeout=args.category_wait_seconds, check=False)
        if completed.returncode:
            LOG.warning('Бот завершился с кодом %d; перечитаем сохранённые решения', completed.returncode)
    except subprocess.TimeoutExpired:
        LOG.info('Истекло время согласования; бот остановлен')
    except OSError as exc:
        LOG.warning('Не удалось запустить бота: %s', type(exc).__name__)
    return True


def execute(plan, source, client, registry, cfg, args, out, crawl):
    """Save registry link right after a successful match/creation; never retry POST blindly."""
    by_key = {(s.source, s.source_id): s for s in source}
    providers = {s['name']: s for s in cfg['sources']}
    metadata = json_read(out / 'meta.json')
    images_cfg = cfg['images']
    gallery_code = gallery_property(metadata, images_cfg.get('gallery_property', 'auto'))
    results = []
    LOG.info('Свежий план: уже сохранённые связи будут исключены при следующем запуске; '
             'журнал операций: %s', out / 'sync_progress.jsonl')

    def record(op, d, *, success, **details):
        results.append({'operation': op, 'source': d.source, 'source_id': d.source_id,
                        'element_id': d.element_id, 'success': success, **details})
        json_write(out / 'sync_results.json', results)
        with (out / 'sync_progress.jsonl').open('a', encoding='utf-8') as journal:
            journal.write(json.dumps(results[-1], ensure_ascii=False) + '\\n')

    # Trusted links to already-existing Bitrix cards are saved first.
    for d in plan.link:
        s = by_key[(d.source, d.source_id)]
        registry.save_link(d.source, d.source_id, d.element_id, url=s.url, title=s.title)
        record('link', d, success=True)

    for d in plan.pending_category:
        s = by_key[(d.source, d.source_id)]
        if s.category:
            registry.set_pending_category(s.source, s.category, s.title)
        record('pending_category', d, success=False, error='Раздел не определён; ожидается решение')

    any_create_error = False
    for d in plan.create:
        s = by_key[(d.source, d.source_id)]
        xml_id = f'{s.source}:{s.source_id}'
        try:
            created = client.post('create.php', make_payload(s, metadata), conflict_ok=True)
            new_id = created.get('element_id')
            # 409 from create.php is an exact XML_ID collision and returns
            # element_id but (in existing PHP) no xml_id field.
            existing = bool(created.get('existing'))
            valid_xml = existing or created.get('xml_id') == xml_id
            if isinstance(new_id, bool) or not isinstance(new_id, int) or new_id < 1 or not valid_xml:
                raise RuntimeError('create.php: unexpected ID or XML_ID')
            registry.save_link(s.source, s.source_id, new_id, url=s.url, title=s.title)
            record('create' if not created.get('existing') else 'create_recovered', d,
                   success=True, element_id_created=new_id)
        except Exception as exc:
            any_create_error = True
            LOG.error('Не создан %s: %s', s.title, exc)
            record('create', d, success=False, error=str(exc))
            # An uncertain POST response might mean the product exists now.
            # Never use a fuzzy fallback; next run recovers through XML_ID.
            continue

        if created.get('existing'):
            continue  # image/activation after next fresh catalog fetch
        photos_ok = False
        if images_cfg.get('enabled', True):
            try:
                photos, warnings = download_images(s, providers[s.source],
                                                  timeout=int(cfg['network']['timeout_sec']),
                                                  max_images=int(images_cfg['max_images_per_product']),
                                                  max_download_bytes=int(images_cfg['max_bytes_each']))
                for w in warnings:
                    LOG.warning('%s: %s', s.title, w)
                if photos:
                    outcome = client.upload_photos(s, new_id, xml_id, gallery_code, photos, expected_name=s.title[:255])
                    photos_ok = bool(outcome.get('main_image_saved'))
                record('images', d, success=photos_ok, element_id_created=new_id)
            except Exception as exc:
                LOG.warning('Фото для %s: %s', s.title, exc)
                record('images', d, success=False, error=str(exc), element_id_created=new_id)
        if args.activate_new and (photos_ok or not images_cfg.get('require_main_image_for_activation', True)):
            try:
                client.post('status.php', {'element_id': new_id, 'active': True, 'dry_run': False,
                                           'expected_xml_id': xml_id, 'expected_active': False})
                record('activate_new', d, success=True, element_id_created=new_id)
            except Exception as exc:
                LOG.warning('Активация %s: %s', new_id, exc)
                record('activate_new', d, success=False, error=str(exc), element_id_created=new_id)

    any_price_error = False
    for d in plan.update_price:
        try:
            outcome = client.post('repair_imported_catalog.php', {
                'operation': 'update_price', 'element_id': d.element_id,
                'expected_xml_id': d.expected_xml_id, 'expected_section_id': d.section_id,
                'expected_price': d.expected_price, 'expected_price_flag': d.expected_price_flag,
                'price_rub': d.price_rub, 'price_is_from': d.price_is_from, 'apply': True,
            })
            if outcome.get('status') not in ('price_synced', 'already_current') or outcome.get('dry_run') is not False:
                raise RuntimeError('Unexpected price update response')
            record('update_price', d, success=True, old_price=d.expected_price,
                   old_price_flag=d.expected_price_flag, price_rub=d.price_rub,
                   price_is_from=d.price_is_from, status=outcome['status'])
        except Exception as exc:
            any_price_error = True
            LOG.error('Цена %s: %s', d.element_id, exc)
            record('update_price', d, success=False, error=str(exc), old_price=d.expected_price,
                   old_price_flag=d.expected_price_flag, price_rub=d.price_rub,
                   price_is_from=d.price_is_from)

    for d in plan.activate:
        try:
            client.post('status.php', {'element_id': d.element_id, 'active': True, 'dry_run': False,
                                       'expected_xml_id': d.expected_xml_id, 'expected_active': False})
            record('activate', d, success=True)
        except Exception as exc:
            LOG.error('Не удалось активировать %s: %s', d.element_id, exc)
            record('activate', d, success=False, error=str(exc))

    if not args.allow_deactivate:
        return results
    # Never deactivate old inventory while its replacements were just created
    # inactive or could not be published. Avoid a suddenly empty shop.
    if plan.create and not args.activate_new:
        LOG.warning('Деактивация отложена: новые карточки созданы неактивными. '
                    'Для замены старых запусти также --activate-new и проверь фото.')
        return results
    if plan.create and any(r['operation'] == 'create_recovered' for r in results):
        LOG.warning('Деактивация отложена: Bitrix восстановил ранее созданную карточку; нужен повторный полный цикл')
        return results
    if plan.create and any(r['operation'] == 'activate_new' and not r['success'] for r in results):
        LOG.warning('Деактивация отложена: часть новых карточек не активирована')
        return results
    activated_ids = {r.get('element_id_created') for r in results if r['operation'] == 'activate_new' and r['success']}
    if plan.create and any(r['operation'] == 'create' and r['success'] and
                           r.get('element_id_created') not in activated_ids for r in results):
        LOG.warning('Деактивация отложена: есть созданные, но не опубликованные товары')
        return results
    if any_create_error or any_price_error or plan.blocked or plan.pending_category:
        LOG.warning('Деактивация отменена: ошибки создания/цен, сломанные связи или нерешённые категории')
        return results
    complete_all = all(_source_status(crawl, s['name']) for s in cfg['sources'])
    candidates = []
    for d in plan.deactivate:
        if d.action == 'deactivate_candidate' and not complete_all:
            LOG.warning('Неактивируемый кандидат %s: общий обход неполный', d.element_id)
            continue
        candidates.append(d)
    limit_abs = int(cfg['safety'].get('max_deactivate', 5))
    limit_pct = float(cfg['safety'].get('max_deactivate_percent', 5))
    # Use total active Bitrix count rather than matched count as denominator.
    target_items = json_read(out / 'target_products.json')
    count_active = sum(bool(x.get('active')) for x in target_items)
    exceeds = len(candidates) > limit_abs or (count_active and len(candidates) / count_active * 100 > limit_pct)
    if exceeds and not args.confirm_mass_deactivate:
        LOG.warning('Деактивация %d карточек превышает лимиты. Нужен --confirm-mass-deactivate', len(candidates))
        return results
    # A newly-linked or created Bitrix product must never be deactivated now.
    live_ids = {d.element_id for d in plan.keep + plan.activate + plan.link}
    for d in candidates:
        if d.element_id in live_ids:
            continue
        try:
            client.post('status.php', {'element_id': d.element_id, 'active': False, 'dry_run': False,
                                       'expected_xml_id': d.expected_xml_id, 'expected_active': True})
            record('deactivate', d, success=True)
        except Exception as exc:
            record('deactivate', d, success=False, error=str(exc))
    return results


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        if args.apply and (args.reuse_snapshot or args.resume):
            raise ValueError('Запись по старому snapshot/checkpoint запрещена, требуется свежий обход')
        if not 0 <= args.category_wait_seconds <= 3600:
            raise ValueError('--category-wait-seconds должен быть от 0 до 3600')
        if args.category_wait_seconds and not args.apply:
            raise ValueError('--category-wait-seconds требует --apply')
        if args.activate_new and not args.apply:
            raise ValueError('--activate-new требует --apply')
        if args.allow_deactivate and not args.apply:
            raise ValueError('--allow-deactivate требует --apply')
        cfg = config_load(Path(args.config))
        if any(s['name'] == 'dongfeng_lcv' for s in cfg['sources']):
            raise ValueError('Dongfeng исключён из v9: удалите источник из конфига')
        reg_cfg = cfg.get('registry', {})
        registry = BitrixRegistry(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''))
        if args.init_registry:
            registry.initialize(cfg['target']['catalog_url'], [s['name'] for s in cfg['sources']])
            LOG.info('Bitrix registry initialized/verified')
            return 0
        registry.require_initialized(cfg['target']['catalog_url'])
        links = registry.load_links()   # Bitrix JSON failure is a fatal error; never assume empty.
        categories = registry.load_categories()
        LOG.info('Bitrix: загружено %d связей и %d правил категорий', len(links), len(categories))
        client = None if args.reuse_snapshot else _client(cfg)
        source, target, taxonomy, counts, crawl, complete = collect(args, cfg, out, client)
        # Product-level category decision if the supplier provides no breadcrumb.
        # Stable source ID means the same decision is reused next time.
        for item in source:
            if not item.category:
                item.category = 'Без категории / ' + item.source_id
        apply_category_links(source, registry, categories, taxonomy)
        plan = build_simple_plan(source, target, taxonomy, links, crawl,
                                 protected_codes=cfg['safety'].get('protected_section_codes',
                                                                   ['kommercheskiy-transport', 'zapchasty']))
        if args.category_wait_seconds and args.apply and plan.pending_category and plan.safe_to_write:
            # Save preliminary diagnostics even if the runner stops during approval.
            report_simple(plan, out)
            if wait_for_categories(plan, source, registry, args, out):
                # Read authoritative decisions again; never assume a timeout means no changes.
                categories = registry.load_categories()
                links = registry.load_links()
                meta = client.metadata()
                from catalog_sync_core.taxonomy import Taxonomy
                tc = cfg['taxonomy']
                taxonomy = Taxonomy(meta['sections'], cfg['target']['excluded_section_codes'],
                                    minimum_score=tc['minimum_match_score'],
                                    minimum_margin=tc['minimum_margin'],
                                    aliases=tc.get('section_aliases', {}))
                json_write(out / 'meta.json', meta)
                # Do not create in a section removed/disabled during the approval window.
                for item in source:
                    section = taxonomy.sections.get(item.section_id)
                    if section is None or not section.active or taxonomy.excluded(item.section_id):
                        item.section_id = 0
                        item.section_match_confidence = 0
                apply_category_links(source, registry, categories, taxonomy)
                plan = build_simple_plan(source, target, taxonomy, links, crawl,
                                         protected_codes=cfg['safety'].get('protected_section_codes',
                                                                           ['kommercheskiy-transport', 'zapchasty']))
                # The same supplier and target snapshots are used. Do not deactivate
                # after an interactive delay: a separate fresh run can handle removals.
                if args.allow_deactivate:
                    args.allow_deactivate = False
                    plan.warnings.append('Деактивация отложена после интерактивного согласования; нужен свежий запуск')
        if plan.pending_category and args.apply:
            examples = pending_examples(plan, source)
            if examples:
                message = 'Нужно определить категории (' + str(len(examples)) + '):\n'
                message += '\n'.join(
                    f'• {name}: {cat} — {title[:55]}'
                    for (name, cat), title in list(examples.items())[:15]
                )
                suffix = ('\nОстались несогласованные категории; доступны при следующем запуске бота.'
                          if args.category_wait_seconds else '\nКоманды бота: /pending, /map <код> <ID>')
                notify(message + suffix)
        if plan.blocked:
            plan.warnings.append(f'Сломанные существующие связи реестра: {len(plan.blocked)}')
        if any(not _source_status(crawl, x['name']) for x in cfg['sources']):
            plan.warnings.append('Не все источники собраны полностью: обычные карточки можно создать, '
                                 'но массовую деактивацию выполнять нельзя')
        report_simple(plan, out)
        if not plan.safe_to_write and args.apply:
            raise RuntimeError('; '.join(plan.errors))
        results = execute(plan, source, client, registry, cfg, args, out, crawl) if args.apply else []
        json_write(out / 'sync_results.json', results)
        LOG.info('План: %s', plan.dump()['counts'])
        notify('АКомфорт: ' + json.dumps(plan.dump()['counts'], ensure_ascii=False))
        return 3 if plan.errors or any(not r['success'] and r['operation'] not in ('pending_category', 'images')
                                       for r in results) else 0
    except Exception as exc:
        LOG.exception('Синхронизация остановлена: %s', exc)
        json_write(out / 'fatal_error.json', {'type': type(exc).__name__, 'error': str(exc)})
        return 2


if __name__ == '__main__':
    sys.exit(main())
