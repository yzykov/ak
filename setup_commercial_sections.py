#!/usr/bin/env python3
"""Create two child categories of commercial transport, with dry-run default."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import requests
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.runtime import config_load


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='config/catalog.json')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--confirm-write', action='store_true')
    p.add_argument('--report', default='output/commercial_sections_setup.json')
    args = p.parse_args(argv)
    if args.apply and not args.confirm_write:
        p.error('Для записи требуются --apply --confirm-write')
    cfg = config_load(Path(args.config))
    client = BitrixClient(cfg['target']['api_base'], os.getenv('AKOMFORT_IMPORT_TOKEN', ''),
                          timeout=int(cfg['network']['timeout_sec']))
    target = client.base + 'setup_commercial_sections.php'
    # Never retry a write automatically: the server may have committed it.
    try:
        response = client.http.post(target, json={'apply': args.apply},
                                    timeout=client.timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise SystemExit('Сетевая ошибка; состояние записи неизвестно: ' + str(exc))
    content_type = response.headers.get('Content-Type', '')
    body = response.text
    report = {
        'url': target,
        'requested_apply': args.apply,
        'http_status': response.status_code,
        'content_type': content_type,
        'redirect_location': response.headers.get('Location', ''),
        'response_length': len(response.content),
    }
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Ответ JSON не является объектом')
        report.update(data)
    except ValueError:
        # Keep diagnostic excerpt only: HTML errors may include server details.
        report['response_excerpt'] = body[:600]
        data = None
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if response.status_code != 200 or not data or data.get('success') is not True:
        raise SystemExit('API не подтвердил выполнение. Проверь HTTP-код и тело ответа; '
                         'не повторяй --apply, пока не проверишь состояние разделов.')


if __name__ == '__main__':
    main()
