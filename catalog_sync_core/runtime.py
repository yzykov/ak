"""Shared catalog collection and payload helpers (no legacy entrypoint)."""
from __future__ import annotations
import json, logging, os, re, shutil
from collections import Counter
from pathlib import Path
import requests
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.checkpoints import fingerprint, read_checkpoint, save_checkpoint, write_json
from catalog_sync_core.identity import configuration, norm, extract_brand, extract_model, extract_modification
from catalog_sync_core.models import Item
from catalog_sync_core.card_properties import card_properties
try:
    from catalog_sync_core.card_properties import covered_spec_entries
except ImportError:
    # Compatibility with v9.4.2 installations that did not yet have this helper.
    def covered_spec_entries(item, card):
        return []
from catalog_sync_core.valmo_content import public_specs, safe_card_properties
from catalog_sync_core.sources import Crawler
from catalog_sync_core.taxonomy import Taxonomy, find_category_from_title
LOG = logging.getLogger('catalog-sync')

def json_write(path: Path, obj) -> None:
    write_json(path, obj)

def json_read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def config_load(path: Path) -> dict:
    config = json_read(path)
    if not isinstance(config.get('sources'), list) or not config['sources']:
        raise ValueError('Конфигурация должна содержать список sources')
    names = [x['name'] for x in config['sources']]
    if len(names) != len(set(names)):
        raise ValueError('Имена поставщиков повторяются')
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]+', str(name)) for name in names):
        raise ValueError('Имена поставщиков могут содержать только латиницу, цифры, - и _')
    if any(x.get('mode') not in ('sitemap_catalog', 'listing', 'catalog_tree') for x in config['sources']):
        raise ValueError('Допустимые режимы: sitemap_catalog, listing или catalog_tree')
    return config

def collect(args, config: dict, out: Path, client: BitrixClient | None) -> tuple[list[Item], list[Item], Taxonomy, dict, dict, bool]:
    if args.reuse_snapshot:
        snap = Path(args.reuse_snapshot)
        manifest_path = snap / 'snapshot_status.json'
        if manifest_path.exists() and not json_read(manifest_path).get('complete'):
            raise ValueError('Снимок каталога ещё не завершён. Сначала выполни '
                             'python catalog_sync.py --resume --out ' + str(snap))
        required = ('meta.json', 'source_products.json', 'target_products.json', 'crawl_status.json')
        missing = [name for name in required if not (snap / name).is_file()]
        if missing:
            raise ValueError(f'Неполный snapshot {snap}: отсутствуют {", ".join(missing)}. '
                             'Для продолжения сетевого сбора используй --resume, а не --reuse-snapshot.')
        meta = json_read(snap / 'meta.json')
        source = [Item.from_dict(x) for x in json_read(snap / 'source_products.json')]
        target = [Item.from_dict(x) for x in json_read(snap / 'target_products.json')]
        crawl = json_read(snap / 'crawl_status.json')
    else:
        assert client is not None
        json_write(out / 'snapshot_status.json', {'complete': False, 'message': 'Чтение каталогов не завершено'})
        checkpoint_dir = out / 'checkpoints'
        if not args.resume and checkpoint_dir.exists():
            # Новый запуск не должен случайно объединять два разных обхода.
            shutil.rmtree(checkpoint_dir)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        meta_fp = fingerprint({'target': config['target'], 'api_base': client.base})
        meta_cp_path = checkpoint_dir / 'meta.json'
        meta_cp = read_checkpoint(meta_cp_path, meta_fp) if args.resume else None
        if meta_cp:
            meta = meta_cp['meta']
            LOG.info('Метаданные Битрикс восстановлены из %s', meta_cp_path)
        else:
            meta = client.metadata()
            save_checkpoint(meta_cp_path, meta_fp, {'meta': meta})
        json_write(out / 'meta.json', meta)
        source, crawl = [], {}
        net = config['network']
        every = int(net.get('checkpoint_every', 10))
        if every < 1:
            raise ValueError('network.checkpoint_every должен быть >= 1')

        for provider in config['sources']:
            name = provider['name']
            cp_path = checkpoint_dir / 'sources' / (name + '.json')
            source_fp = fingerprint({'provider': provider, 'network': {
                'max_pages': net['max_pages'], 'max_listing_pages': net['max_listing_pages']},
                'crawler_version': 5})
            previous = read_checkpoint(cp_path, source_fp) if args.resume else None
            if previous and previous.get('finished'):
                items = [Item.from_dict(x) for x in previous.get('items', [])]
                crawl[name] = previous['crawl']
                source.extend(items)
                LOG.info('%s: восстановлено %d карточек из checkpoint, повторный обход не нужен', name, len(items))
                json_write(out / 'source_products.json', [x.to_dict() for x in source])
                json_write(out / 'crawl_status.json', crawl)
                continue

            LOG.info('Сканирование %s%s...', name, ' (продолжение)' if previous else '')
            crawler = Crawler(provider, timeout=int(net['timeout_sec']), delay=float(net['delay_sec']),
                              max_pages=int(net['max_pages']), max_listing_pages=int(net['max_listing_pages']))
            last_state = {}

            def progress(state, *, source_fp=source_fp, cp_path=cp_path, name=name):
                nonlocal last_state
                last_state = state
                partial_crawl = {'count': len(state['items']), 'complete': False,
                                 'finished': False, 'processed': len(state['processed_urls']),
                                 'discovered': len(state['urls']),
                                 'warnings': state['warnings'], 'errors': state['errors']}
                save_checkpoint(cp_path, source_fp, {**state, 'finished': False, 'crawl': partial_crawl})
                # Даже если следующий источник или API АКомфорт упадёт, уже
                # найденные товары доступны как обычный JSON.
                json_write(out / 'source_products.json',
                           [x.to_dict() for x in source] + state['items'])
                json_write(out / 'crawl_status.json', {**crawl, name: partial_crawl})

            items = crawler.crawl(resume_state=previous, checkpoint_callback=progress,
                                  checkpoint_every=every)
            source.extend(items)
            status = {'count': len(items), 'complete': crawler.complete,
                      'finished': True, 'processed': len(last_state.get('processed_urls', [])),
                      'discovered': crawler.discovered_count,
                      'warnings': crawler.warnings, 'errors': crawler.errors}
            crawl[name] = status
            # Если были временные сетевые ошибки, следующий --resume повторит
            # именно неуспешные URL. При нуле товаров, но без сетевых ошибок,
            # адаптер считается завершённым (хотя полнота всё равно false).
            pending = len(last_state.get('processed_urls', [])) < crawler.discovered_count
            save_checkpoint(cp_path, source_fp, {**last_state, 'finished': not pending, 'crawl': status})
            json_write(out / 'source_products.json', [x.to_dict() for x in source])
            json_write(out / 'crawl_status.json', crawl)
            LOG.info('%s: %d карточек, полнота: %s; сохранено в %s',
                     name, len(items), crawler.complete, cp_path)

        tax_config = config['taxonomy']
        taxonomy = Taxonomy(meta['sections'], config['target']['excluded_section_codes'],
                            minimum_score=tax_config['minimum_match_score'],
                            minimum_margin=tax_config['minimum_margin'],
                            aliases=tax_config.get('section_aliases', {}))
        target_fp = fingerprint({'target': config['target'], 'meta_sections': meta['sections'],
                                 'api_base': client.base, 'target_reader_version': 1})
        target_cp_path = checkpoint_dir / 'target.json'
        previous_target = read_checkpoint(target_cp_path, target_fp) if args.resume else None
        if previous_target and previous_target.get('finished'):
            target = [Item.from_dict(x) for x in previous_target['items']]
            LOG.info('АКомфорт: восстановлено %d товаров из checkpoint', len(target))
        else:
            def target_progress(state):
                save_checkpoint(target_cp_path, target_fp, state)
                json_write(out / 'target_products.partial.json', state['items'])
            target = client.products(config['target']['catalog_url'], taxonomy,
                                     resume_state=previous_target, checkpoint_callback=target_progress)
        json_write(out / 'target_products.json', [x.to_dict() for x in target])
        (out / 'target_products.partial.json').unlink(missing_ok=True)

    # Каждая новая версия алгоритма должна пересчитать идентичность и на
    # готовых snapshot/checkpoint. Иначе старые ошибочно сохранённые «5G»
    # вместо «Т5G» и «4X2ZZ...» вместо «ZZ...» оставались бы навсегда.
    # Поля, отсутствующие в HTML/названии, сохраняем из исходной выгрузки.
    for item in source + target:
        item.brand = item.brand or extract_brand(item.title, item.specs)
        discovered_model = extract_model(item.title, item.specs)
        if discovered_model:
            item.model = discovered_model
        discovered_mod = extract_modification(item.specs, item.title)
        if not discovered_mod and item.modification:
            # Нормализуем и старое значение из checkpoint, если в карточке
            # нет заводского кода; раньше оно могло содержать префикс «4X2».
            discovered_mod = extract_modification({'Модификация': item.modification})
        if discovered_mod:
            item.modification = discovered_mod

    # В offline-режиме taxonomy создаём из сохранённой metadata.
    if args.reuse_snapshot:
        tax_config = config['taxonomy']
        taxonomy = Taxonomy(meta['sections'], config['target']['excluded_section_codes'],
                            minimum_score=tax_config['minimum_match_score'],
                            minimum_margin=tax_config['minimum_margin'],
                            aliases=tax_config.get('section_aliases', {}))
    sections = [x for x in taxonomy.sections.values() if not taxonomy.excluded(x.id)]
    for item in source:
        hint = find_category_from_title(item.title, sections)
        if not item.category:
            item.category = hint
        result = taxonomy.choose(item)
        if hint and hint != item.category:
            probe = Item(source=item.source, url=item.url, title=item.title, category=hint)
            child = taxonomy.choose(probe)
            if child.section_id and (not result.section_id or
                                     (child.section_id != result.section_id and
                                      taxonomy.is_descendant(child.section_id, result.section_id))):
                item.category = hint
                result = child
        if (result.section_id and result.reason != 'Подтверждённый псевдоним категории'
                and any(s.parent_id == result.section_id for s in sections)):
            compatible = {t.section_id for t in target
                          if t.brand and item.brand and norm(t.brand) == norm(item.brand)
                          and t.model and item.model and norm(t.model) == norm(item.model)
                          and taxonomy.is_descendant(t.section_id, result.section_id)}
            if len(compatible) == 1:
                chosen_id = next(iter(compatible))
                result = type(result)(chosen_id, result.score, 'Раздел существующей модели в Битрикс')
            else:
                result = type(result)(0, result.score, 'Несколько возможных подразделов')
        item.section_id = result.section_id
        item.section_match_confidence = result.score
    counts = Counter(p.source for p in source)
    completeness = all(p.get('complete') and p.get('count', 0) > 0 for p in crawl.values())
    if len(crawl) != len(config['sources']):
        completeness = False
    json_write(out / 'meta.json', meta)
    json_write(out / 'source_products.json', [x.to_dict() for x in source])
    json_write(out / 'target_products.json', [x.to_dict() for x in target])
    json_write(out / 'crawl_status.json', crawl)
    valmo_reviews = [
        {'url': item.url, 'source_id': item.source_id, 'title': item.title,
         'conflicts': item.source_conflicts}
        for item in source if item.source == 'valmo' and item.source_conflicts
    ]
    json_write(out / 'valmo_content_conflicts.json', valmo_reviews)
    if valmo_reviews:
        LOG.warning('Valmo: %d карточек с противоречиями текста и характеристик. '
                    'Список: %s', len(valmo_reviews), out / 'valmo_content_conflicts.json')
    if not args.reuse_snapshot:
        json_write(out / 'snapshot_status.json', {'complete': True, 'message': 'Снимки поставщиков и АКомфорт сохранены',
                                                   'sources_complete': completeness})
    return source, target, taxonomy, dict(counts), crawl, completeness

def simple_text_properties(item: Item, metadata: dict) -> dict[str, str]:
    # Optional existing Bitrix single-text properties. Never guess list/HL IDs.
    names = {
        'brand': {'БРЕНД', 'МАРКА', 'МАРКА ТЕХНИКИ', 'ПРОИЗВОДИТЕЛЬ ТЕХНИКИ', 'BRAND'},
        'model': {'МОДЕЛЬ', 'МОДЕЛЬ ТЕХНИКИ', 'MODEL'},
        'modification': {'МОДИФИКАЦИЯ', 'МОДИФИКАЦИЯ ТЕХНИКИ', 'MODIFICATION'},
    }
    values = {'brand': item.brand, 'model': item.model, 'modification': item.modification}
    found = {}
    for field, aliases in names.items():
        value = values[field]
        if not value:
            continue
        matching = [p for p in metadata.get('properties', [])
                    if p.get('type') == 'S' and p.get('multiple') is False
                    and p.get('code') and (norm(p.get('name', '')) in aliases
                                           or norm(p.get('code', '')) in aliases)]
        if len(matching) == 1:
            found[str(matching[0]['code'])] = value
    return found

def make_payload(item: Item, metadata: dict | None = None) -> dict:
    if not item.section_id:
        raise ValueError(f'Не сопоставлен раздел: {item.title}')
    # HARACTERISTICS contains the *remaining* supplier specs, not duplicates of
    # native card tiles. The server later stores label in VALUE and text in DESCRIPTION.
    card = safe_card_properties(item, card_properties(item))
    covered = {name['name'] for name in covered_spec_entries(item, card)}
    public = (public_specs(item.specs, item.source_conflicts) if item.source == 'valmo'
              else item.specs)
    blocked_codes = {c.get('field') for c in item.source_conflicts}
    blocked_main = set()
    if item.source == 'valmo':
        if 'FORM_WHEEL' in blocked_codes:
            blocked_main.update(('Колёсная формула', 'Колесная формула'))
        if blocked_codes & {'ENGINE', 'MODEL_ENGINE', 'POWER'}:
            blocked_main.update(('Тип двигателя', 'Двигатель', 'Модель двигателя',
                                 'Мощность, л.с.', 'Мощность двигателя'))
    characteristics = [
        {'name': str(name)[:150], 'value': str(value)[:1500]}
        for name, value in list(public.items())[:110]
        if name and value and name not in covered and name not in blocked_main
    ]
    result = {'source': item.source, 'source_id': item.source_id, 'name': item.title[:255],
              'brand': item.brand, 'model': item.model, 'modification': item.modification,
              'description': item.description[:30000],
              'sections': [item.section_id], 'primary_section': item.section_id,
              'characteristics': characteristics,
              'equipment': [x[:700] for x in item.equipment[:60]]}
    if metadata:
        extra = simple_text_properties(item, metadata)
        if extra:
            result['extra_properties'] = extra
    config_values = configuration(item)
    if config_values['power_hp'] and 'POWER' not in blocked_codes:
        result['power'] = config_values['power_hp']
    if config_values['power_hp'] and not card.get('POWER') and 'POWER' not in blocked_codes:
        card['POWER'] = config_values['power_hp']
    if item.source in ('gkgross', 'liftnet') and item.price_rub > 0:
        result['price_rub'] = item.price_rub
        result['price_is_from'] = bool(item.price_is_from)
    if card:
        result['card_properties'] = card
    return result

def notify(message: str) -> None:
    token, chat = os.getenv('TG_BOT_TOKEN', ''), os.getenv('TG_CHAT_ID', '')
    if not token or not chat:
        LOG.info('Telegram не настроен')
        return
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          data={'chat_id': chat, 'text': message[:3900],
                                'disable_web_page_preview': 'true'}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        LOG.error('Не удалось отправить Telegram: %s', type(exc).__name__)

def gallery_property(metadata: dict, configured: str) -> str:
    if configured and configured.lower() != 'auto':
        return configured
    candidates = [p for p in metadata.get('properties', [])
                  if p.get('type') == 'F' and p.get('multiple') is True and p.get('code')]
    if len(candidates) == 1:
        return candidates[0]['code']
    likely = [p for p in candidates if any(x in (p.get('name', '') + ' ' + p.get('code', '')).upper()
                for x in ('ФОТО', 'ГАЛЕРЕЯ', 'ИЗОБРАЖ', 'PHOTO', 'GALLERY', 'PICTURE'))]
    if len(likely) == 1:
        return likely[0]['code']
    return 'NONE'
