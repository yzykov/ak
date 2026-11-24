#!/usr/bin/env python3
"""Telegram category mapper. No DB; state and decisions live in Битрикс.

Requires: TG_BOT_TOKEN, TG_CHAT_ID, TG_ADMIN_USER_ID, AKOMFORT_IMPORT_TOKEN,
Битрикс_BUCKET (+ Битрикс_ENDPOINT_URL/AWS_* as needed).

Commands: /pending, /sections [filter], /map <hash> <section_id>, /help
The bot maps ONLY to an existing active Bitrix section. To create a section,
use Bitrix admin and then assign its ID through the bot.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

import requests
from catalog_sync import _client
from catalog_sync_core.runtime import config_load
from catalog_sync_core.bitrix_registry import BitrixRegistry
from catalog_sync_core.taxonomy import Taxonomy
from pathlib import Path

LOG = logging.getLogger('category-bot')


def pending_rows(registry, scope=None):
    mapped = registry.load_categories()
    rows = []
    for key, row in registry.scan('pending/').items():
        pair = (row['source'], row['category'].strip().casefold())
        if pair in mapped or (scope is not None and pair not in scope):
            continue
        hash_id = row['source'] + ':' + key.rsplit('/', 1)[-1].removesuffix('.json')[:12]
        rows.append((hash_id, row))
    return sorted(rows, key=lambda item: (item[1]['source'], item[1]['category']))


def process_command(text, registry, tax, scope=None):
    command = text.strip().split()
    if not command or command[0] in ('/help', '/start'):
        return ('Команды:\n/pending — категории без раздела\n'
                '/sections экскаваторы — поиск раздела Битрикс\n'
                '/map <код> <ID раздела> — сохранить правило в Битрикс'
                + ('\n/done — завершить согласование' if scope is not None else ''))
    if command[0] == '/pending':
        rows = pending_rows(registry, scope)
        if not rows:
            return 'Неопределённых категорий нет.'
        return ('Нужно определить категории (' + str(len(rows)) + '):\n' +
                '\n'.join(f'{i}. [{hash_id}] {row["source"]}: {row["category"]} — {row.get("example", "")[:48]}'
                          for i, (hash_id, row) in enumerate(rows[:25], 1)) +
                ('\n... покажу первые 25' if len(rows) > 25 else '') +
                '\nПример: /map gkgross:abc123456789 42')
    if command[0] == '/sections':
        needle = ' '.join(command[1:]).casefold()
        rows = [s for s in tax.sections.values()
                if s.active and not tax.excluded(s.id) and
                (not needle or needle in s.name.casefold() or needle in s.code.casefold())]
        return ('Разделы (' + str(len(rows)) + '):\n' +
                '\n'.join(f'{s.id}: {s.name} [{s.code}]' for s in rows[:35]) +
                ('\n... уточните поиск' if len(rows) > 35 else ''))
    if command[0] == '/map':
        if len(command) != 3 or not command[2].isdigit():
            return 'Формат: /map <код из /pending> <ID раздела>'
        code = command[1].lower()
        matches = [(h, row) for h, row in pending_rows(registry, scope)
                   if h.startswith(code) or (':' not in code and h.split(':', 1)[-1].startswith(code))]
        if len(matches) != 1:
            return 'Код не найден или неоднозначен. Выполни /pending.'
        section_id = int(command[2])
        section = tax.sections.get(section_id)
        if section is None or not section.active or tax.excluded(section_id):
            return 'Раздел не существует, отключён или запрещён для импорта.'
        row = matches[0][1]
        registry.set_category(row['source'], row['category'], section_id)
        return (f'Сохранено в Битрикс: {row["source"]} / {row["category"]} '
                f'→ {section_id} ({section.name}). ' +
                ('Текущая синхронизация перечитает правило.' if scope is not None
                 else 'Следующий запуск применит правило.'))
    return 'Неизвестная команда. /help'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/catalog.json')
    parser.add_argument('--wait-seconds', type=int, default=0)
    parser.add_argument('--pending-file', type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 3600 or bool(args.wait_seconds) != bool(args.pending_file):
        parser.error('--wait-seconds (1..3600) and --pending-file must be used together')
    scope = (set(map(tuple, json.loads(args.pending_file.read_text(encoding='utf-8'))))
             if args.pending_file else None)
    deadline = time.monotonic() + args.wait_seconds if args.wait_seconds else None
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('TG_BOT_TOKEN', '')
    chat = os.getenv('TG_CHAT_ID', '')
    user = os.getenv('TG_ADMIN_USER_ID', '')
    if not token or not chat or not user or not user.isdecimal():
        raise ValueError('Нужны TG_BOT_TOKEN, TG_CHAT_ID и TG_ADMIN_USER_ID')
    config = config_load(Path(args.config))
    registry = BitrixRegistry(config['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''))
    registry.require_initialized(config['target']['catalog_url'])
    client = _client(config)
    api = f'https://api.telegram.org/bot{token}/'
    s = requests.Session()
    # After a week without Telegram updates IDs may be randomized. Never use
    # the previous weekly session's high-water mark for a new bounded session.
    offset_record = (registry.read('telegram/offset.json') or {'offset': 0}) if scope is None else {'offset': 0}
    offset = int(offset_record.get('offset', 0))
    LOG.info('Telegram bot running; offset=%d', offset)

    def send(text):
        answer = s.post(api + 'sendMessage', data={'chat_id': chat, 'text': text[:3950]}, timeout=20)
        answer.raise_for_status()
        if not answer.json().get('ok'):
            raise RuntimeError('Telegram sendMessage returned error')

    # Do not execute old queued /map commands from previous weekly sessions.
    started = int(time.time())
    if scope is not None:
        if not pending_rows(registry, scope):
            return 0
        send(f'АКомфорт: согласование категорий, до {args.wait_seconds // 60} мин.\n'
             + process_command('/pending', registry, None, scope)
             + '\n/sections <название> — поиск раздела; /done — закончить раньше.')
    while deadline is None or time.monotonic() < deadline:
        if scope is not None and not pending_rows(registry, scope):
            send('Все категории этого запуска согласованы. Продолжаю синхронизацию.')
            return 0
        remaining = 20 if deadline is None else min(20, max(0, int(deadline - time.monotonic())))
        if deadline is not None and remaining == 0:
            break
        response = s.get(api + 'getUpdates', params={'offset': offset, 'timeout': remaining}, timeout=remaining + 5)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            raise RuntimeError('Telegram getUpdates returned error')
        for update in data.get('result', []):
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            update_id = int(update['update_id'])
            message = update.get('message', {})
            done = False
            if (str(message.get('chat', {}).get('id')) == chat and
                str(message.get('from', {}).get('id')) == user and
                isinstance(message.get('text'), str) and
                (scope is None or message.get('date', 0) >= started)):
                try:
                    if scope is not None and message['text'].strip() == '/done':
                        done = True
                        reply = 'Согласование завершено. Остальные категории останутся в реестре.'
                    else:
                        meta = client.metadata()
                        tax = Taxonomy(meta['sections'], config['target']['excluded_section_codes'])
                        reply = process_command(message['text'], registry, tax, scope)
                except Exception as exc:
                    # Request exception strings can include the Telegram token.
                    LOG.error('Command failed: %s', type(exc).__name__)
                    reply = 'Ошибка команды: ' + type(exc).__name__
                send(reply)
            # Commit offset only after handling and replying.
            offset = max(offset, update_id + 1)
            if scope is None:
                registry.write('telegram/offset.json', {'offset': offset})
            if done:
                return 0
            if scope is not None and not pending_rows(registry, scope):
                send('Все категории этого запуска согласованы. Продолжаю синхронизацию.')
                return 0
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOG.error('Bot stopped: %s', type(exc).__name__)
        raise SystemExit(1) from None
