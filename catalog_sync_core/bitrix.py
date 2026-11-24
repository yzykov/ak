"""HTTP-клиент Битрикс, строгое чтение и запись через защищённые endpoints."""
from __future__ import annotations

import io
import json
import logging
import os
import time
from urllib.parse import urljoin, urlparse

import requests

from .identity import clean, extract_brand, extract_model, extract_modification
from .models import Item

LOG = logging.getLogger(__name__)


class BitrixClient:
    def __init__(self, api_base: str, token: str, *, timeout: int = 30,
                 read_attempts: int = 3, retry_delay: float = 1.5):
        if urlparse(api_base).scheme != 'https':
            raise ValueError('API Bitrix должен работать по HTTPS')
        if len(token) < 32:
            raise ValueError('AKOMFORT_IMPORT_TOKEN: требуется минимум 32 символа')
        self.base = api_base.rstrip('/') + '/'
        self.timeout = timeout
        self.read_attempts = max(1, min(8, int(read_attempts)))
        self.retry_delay = max(0.0, float(retry_delay))
        self.http = requests.Session()
        self.http.headers.update({'X-Import-Token': token, 'Accept': 'application/json',
                                  'User-Agent': 'CatalogSync/2.0'})

    def _read(self, url: str) -> dict:
        """Повторяем только безопасные GET при временных сетевых ошибках.

        POST/create/status/images не повторяем автоматически: ответ мог
        потеряться уже после того, как сервер выполнил операцию.
        """
        for attempt in range(1, self.read_attempts + 1):
            try:
                r = self.http.get(url, timeout=self.timeout, allow_redirects=False)
                if r.status_code == 200:
                    data = r.json()
                    if not isinstance(data, dict):
                        raise ValueError('Неверный ответ API (ожидался JSON-объект)')
                    return data
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f'Bitrix GET {urlparse(url).path}: HTTP {r.status_code}')
                message = f'HTTP {r.status_code}'
            except requests.RequestException as exc:
                message = f'{type(exc).__name__}: {exc}'
            if attempt == self.read_attempts:
                raise RuntimeError(f'Bitrix GET {urlparse(url).path}: {message}; '
                                   f'не удалось за {attempt} попыток')
            delay = min(15.0, self.retry_delay * (2 ** (attempt - 1)))
            LOG.warning('Bitrix GET %s: %s; повтор %d/%d через %.1f с',
                        urlparse(url).path, message, attempt + 1, self.read_attempts, delay)
            time.sleep(delay)
        raise AssertionError('Недостижимо')

    def metadata(self) -> dict:
        data = self._read(self.base + 'meta.php')
        if not isinstance(data.get('sections'), list) or not isinstance(data.get('properties'), list):
            raise ValueError('meta.php: отсутствуют sections/properties')
        return data

    def products(self, root: str, taxonomy, *, resume_state: dict | None = None,
                 checkpoint_callback=None) -> list[Item]:
        """Читаем Битрикс постранично и сохраняем каждый завершённый ответ."""
        previous = resume_state or {}
        rows = [Item.from_dict(row) for row in previous.get('items', [])]
        seen_pages = set(previous.get('seen_pages', []))
        ids = {item.bitrix_id for item in rows}
        if len(ids) != len(rows):
            raise ValueError('Повторные ID в checkpoint Битрикс')
        url = previous.get('next_url', self.base + 'list.php')
        if not isinstance(url, (str, type(None))):
            raise ValueError('Неверный next_url в checkpoint Битрикс')

        def save_progress():
            if checkpoint_callback is not None:
                checkpoint_callback({'items': [x.to_dict() for x in rows],
                                     'seen_pages': sorted(seen_pages), 'next_url': url,
                                     'finished': url is None})

        save_progress()
        while url:
            p = urlparse(url)
            if (p.scheme != 'https' or p.hostname != urlparse(self.base).hostname
                    or url in seen_pages or len(seen_pages) >= 500):
                raise ValueError('Неверная пагинация list.php')
            data = self._read(url)
            entries = data.get('items')
            if not isinstance(entries, list):
                raise ValueError('list.php должен возвращать items')
            page_rows = []
            for raw in entries:
                if not isinstance(raw, dict):
                    raise ValueError('Неверный элемент list.php')
                sid = int(raw.get('IBLOCK_SECTION_ID') or 0)
                section_ids = raw.get('SECTION_IDS') or [sid]
                if not isinstance(section_ids, list) or any(not isinstance(x, int) for x in section_ids):
                    raise ValueError('list.php: invalid SECTION_IDS')
                if taxonomy.excluded(sid) or any(taxonomy.excluded(x) for x in section_ids):
                    continue
                pid = int(raw.get('ID') or 0)
                link = str(raw.get('DETAIL_PAGE_URL') or '')
                if not pid or not link or pid in ids:
                    raise ValueError('list.php: пустой/повторный ID или URL')
                ids.add(pid)
                specs = raw.get('PROPERTIES') or {}
                if not isinstance(specs, dict):
                    raise ValueError('list.php: неверные PROPERTIES')
                specs = {str(k): str(v) for k, v in specs.items() if isinstance(v, (str, int, float))}
                title = clean(str(raw.get('NAME') or ''))
                brand = extract_brand(title, specs)
                model = extract_model(title, specs)
                if sid not in taxonomy.sections:
                    raise ValueError(f'list.php: неизвестный раздел {sid} для товара {pid}')
                item = Item(source='akomfort', url=urljoin(root, link), title=title,
                            brand=brand, model=model, modification=extract_modification(specs, title),
                            category=taxonomy.sections[sid].name, section_id=sid, section_ids=section_ids, specs=specs,
                            description=str(raw.get('DETAIL_TEXT') or ''),
                            bitrix_id=pid, xml_id=str(raw.get('XML_ID') or ''),
                            active=str(raw.get('ACTIVE') or '') == 'Y',
                            preview_picture=int(raw.get('PREVIEW_PICTURE') or 0),
                            detail_picture=int(raw.get('DETAIL_PICTURE') or 0),
                            gallery_count=int(raw.get('GALLERY_COUNT') or 0),
                            current_price=raw.get('PRICE_NEW_RAW'),
                            current_price_flag=raw.get('PRICE_IS_FROM_RAW'))
                page_rows.append(item)
            nxt = data.get('next')
            if nxt is not None and not isinstance(nxt, str):
                raise ValueError('list.php: поле next некорректно')
            next_url = urljoin(url, nxt) if nxt else None
            # Данные страницы подтверждены, только теперь обновляем checkpoint.
            rows.extend(page_rows)
            seen_pages.add(url)
            url = next_url
            save_progress()
            LOG.info('АКомфорт: сохранено %d товаров, страниц %d', len(rows), len(seen_pages))
        if not rows:
            raise ValueError('list.php: пустой каталог вне исключённых разделов')
        return rows

    def post(self, endpoint: str, payload: dict, *, conflict_ok: bool = False) -> dict:
        if endpoint not in ('create.php', 'status.php', 'repair.php', 'repair_imported_catalog.php'):
            raise ValueError('Неизвестный endpoint записи')
        r = self.http.post(self.base + endpoint, json=payload, timeout=self.timeout, allow_redirects=False)
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(f'{endpoint}: HTTP {r.status_code} без JSON') from e
        if conflict_ok and r.status_code == 409 and data.get('error') == 'Product already exists':
            return {'existing': True, **data}
        if r.status_code not in (200, 201) or data.get('success') is not True:
            raise RuntimeError(f'{endpoint}: HTTP {r.status_code}: {data.get("error", "unknown")}')
        return data

    def upload_photos(self, item: Item, element_id: int, expected_xml_id: str, gallery_property: str,
                      photos: list[tuple[str, bytes]], expected_name: str = '',
                      replace_imported_images: bool = False) -> dict:
        if not photos:
            raise ValueError('Нет проверенных изображений для загрузки')
        data = {'element_id': str(element_id), 'expected_xml_id': expected_xml_id,
                'expected_name': expected_name, 'gallery_property': gallery_property,
                'replace_imported_images': '1' if replace_imported_images else '0'}
        multipart = [('photos[]', (name, io.BytesIO(blob), 'image/jpeg')) for name, blob in photos]
        r = self.http.post(self.base + 'images.php', data=data, files=multipart, timeout=max(self.timeout, 90), allow_redirects=False)
        try:
            body = r.json()
        except ValueError as e:
            raise RuntimeError(f'images.php: HTTP {r.status_code}, не JSON') from e
        if r.status_code != 200 or body.get('success') is not True:
            raise RuntimeError(f'images.php: HTTP {r.status_code}: {body.get("error", "unknown")}')
        return body
