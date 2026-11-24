"""Bitrix-hosted JSON registry, accessed only through authenticated HTTPS API."""
from __future__ import annotations
import hashlib
import re
import logging
import time
from urllib.parse import urljoin, urlparse
import requests


def link_id(source, source_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source) or not source_id:
        raise ValueError('Invalid source/source_id')
    return source + '/' + hashlib.sha256(source_id.encode()).hexdigest()


def category_id(source, category):
    if not category.strip():
        raise ValueError('Empty category')
    return link_id(source, category.strip().casefold())


class BitrixRegistry:
    def __init__(self, api_base, token, *, session=None, timeout=30):
        if urlparse(api_base).scheme != 'https' or len(token) < 32:
            raise ValueError('Registry requires HTTPS and AKOMFORT_IMPORT_TOKEN >= 32 chars')
        self.url = urljoin(api_base.rstrip('/') + '/', 'matching.php')
        self.http = session or requests.Session()
        self.http.headers.update({'X-Import-Token': token, 'Accept': 'application/json'})
        self.timeout = timeout
        self.log = logging.getLogger("catalog-sync-registry")
        self.read_attempts = 4
        self.retry_delay = 1.5

    def _request(self, method, **kw):
        attempts = self.read_attempts if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                r = self.http.request(method, self.url, timeout=self.timeout,
                                      allow_redirects=False, **kw)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or data.get('ok') is not True:
                    raise ValueError('Invalid matching.php response')
                return data
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt + 1 >= attempts:
                    raise
                self.log.warning('GET matching.php: %s; повтор %d/%d',
                                 exc, attempt + 2, attempts)
                time.sleep(self.retry_delay * (attempt + 1))

    def read(self, name):
        return self._request('GET', params={'key': name}).get('value')

    def write(self, name, data):
        self._request('POST', json={'op': 'write', 'key': name, 'value': data})

    def initialize(self, target, sources):
        return self._request('POST', json={'op': 'initialize', 'target': target, 'sources': sources})['value']

    def require_initialized(self, target):
        row = self.read('manifest.json')
        if row is None:
            raise ValueError('Реестр не инициализирован; выполните --init-registry')
        if row.get('schema') != 1 or row.get('target') != target:
            raise ValueError('Registry manifest target/schema mismatch')
        return row

    def scan(self, folder):
        values = self._request('GET', params={'prefix': folder}).get('items')
        if not isinstance(values, dict):
            raise ValueError('Malformed registry listing')
        return values

    def load_links(self):
        result = {}
        for key, row in self.scan('links/').items():
            source, sid, bid = row.get('source'), row.get('source_id'), row.get('bitrix_id')
            if not source or not sid or type(bid) is not int or bid < 1 or key != 'links/' + link_id(source, sid) + '.json':
                raise ValueError('Corrupted registry link: ' + key)
            pair = (source, sid)
            if pair in result:
                raise ValueError('Duplicate registry link')
            result[pair] = row
        return result

    def save_link(self, source, source_id, bitrix_id, *, url='', title=''):
        if type(bitrix_id) is not int or bitrix_id < 1:
            raise ValueError('Invalid Bitrix ID')
        key = 'links/' + link_id(source, source_id) + '.json'
        payload = {'op': 'save_link', 'source': source,
                   'source_id': source_id, 'bitrix_id': bitrix_id,
                   'source_url': url, 'title': title}
        for attempt in range(4):
            try:
                return self._request('POST', json=payload)['value']
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # POST may have succeeded before the connection was dropped.
                # Never repeat it until a successful authoritative read proves absence.
                self.log.warning('save_link %s/%s: разрыв соединения: %s', source, source_id, exc)
                try:
                    existing = self.read(key)
                except Exception as read_exc:
                    raise RuntimeError('Неизвестен результат save_link; проверка реестра '
                                       'не удалась. Повтор POST запрещён') from read_exc
                if existing is not None:
                    if (existing.get('source') == source and
                        existing.get('source_id') == source_id and
                        existing.get('bitrix_id') == bitrix_id):
                        self.log.info('save_link подтверждён чтением реестра: %s', key)
                        return existing
                    raise RuntimeError('Конфликт связи в реестре: ' + key) from exc
                if attempt == 3:
                    raise RuntimeError('save_link не подтверждён после 4 попыток: ' + key) from exc
                time.sleep(self.retry_delay * (attempt + 1))

    def load_categories(self):
        result = {}
        for key, row in self.scan('categories/').items():
            source, cat, sid = row.get('source'), row.get('category'), row.get('section_id')
            if not source or not cat or type(sid) is not int or sid < 1 or key != 'categories/' + category_id(source, cat) + '.json':
                raise ValueError('Corrupted category rule: ' + key)
            result[(source, cat.strip().casefold())] = sid
        return result

    def set_category(self, source, category, section_id):
        if type(section_id) is not int or section_id < 1:
            raise ValueError('Invalid section')
        self._request('POST', json={'op': 'set_category', 'source': source,
                                   'category': category, 'category_key': category.strip().casefold(), 'section_id': section_id})

    def set_pending_category(self, source, category, example):
        self._request('POST', json={'op': 'set_pending_category', 'source': source,
                                   'category': category, 'category_key': category.strip().casefold(), 'example': example})
