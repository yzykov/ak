"""Persist supplier->Bitrix links and category decisions in an S3-compatible bucket.

No SQL, local persistent DB, or monolithic shared JSON. Each link has its own key.
The bucket must be private; enable bucket versioning and run a single writer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def link_id(source: str, source_id: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise ValueError('Invalid supplier name')
    if not source_id:
        raise ValueError('source_id is required')
    return f'{source}/{hashlib.sha256(source_id.encode("utf-8")).hexdigest()}'


def category_id(source: str, category: str) -> str:
    if not category.strip():
        raise ValueError('Category cannot be empty')
    return link_id(source, category.strip().casefold())


class S3Registry:
    def __init__(self, bucket: str, prefix: str = 'akomfort/catalog-sync/v1/', *, client=None):
        if not bucket:
            raise ValueError('S3_BUCKET не задан')
        self.bucket = bucket
        self.prefix = prefix.strip('/') + '/'
        if client is None:
            import boto3
            # Endpoint optional: native AWS S3 or any S3-compatible service.
            client = boto3.client('s3', endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,
                                  region_name=os.getenv('AWS_DEFAULT_REGION') or 'us-east-1')
        self.client = client

    def _key(self, name: str) -> str:
        return self.prefix + name

    def read(self, name: str) -> dict | None:
        from botocore.exceptions import ClientError
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(name))
        except ClientError as exc:
            code = str(exc.response.get('Error', {}).get('Code', ''))
            if code in ('NoSuchKey', '404', 'NotFound'):
                return None
            raise  # including AccessDenied: NEVER treat as empty registry
        row = json.loads(resp['Body'].read().decode('utf-8'))
        if not isinstance(row, dict):
            raise ValueError('Malformed S3 JSON object: ' + name)
        return row

    def write(self, name: str, data: dict) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(name),
                               Body=json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8'),
                               ContentType='application/json')

    def scan(self, folder: str) -> dict[str, dict]:
        """Paginate correctly and fail closed on any read/listing error."""
        out: dict[str, dict] = {}
        pagination = self.client.get_paginator('list_objects_v2')
        for page in pagination.paginate(Bucket=self.bucket, Prefix=self._key(folder)):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not key.endswith('.json'):
                    continue
                name = key.removeprefix(self.prefix)
                row = self.read(name)
                if row is None:
                    raise RuntimeError('S3 object disappeared during listing: ' + name)
                out[name] = row
        return out

    def initialize(self, target: str, sources: list[str]) -> dict:
        """One-time explicit bootstrap. Prevent accidental empty registry on typoed prefix."""
        if not target or not sources:
            raise ValueError('S3 registry needs target and suppliers')
        previous = self.read('manifest.json')
        if previous is not None:
            if previous.get('schema') != 1 or previous.get('target') != target:
                raise ValueError('S3 manifest refers to a different target/schema')
            return previous
        # Refuse to overwrite an existing partly populated prefix silently.
        if self.scan('links/') or self.scan('categories/'):
            raise ValueError('Cannot bootstrap: registry data already exists without a manifest')
        row = {'schema': 1, 'target': target, 'sources_at_creation': sorted(sources),
               'created_at': utc_now()}
        self.write('manifest.json', row)
        return row

    def require_initialized(self, target: str) -> dict:
        row = self.read('manifest.json')
        if row is None:
            raise ValueError('S3 реестр не инициализирован; выполните python catalog_sync.py --init-s3')
        if row.get('schema') != 1 or row.get('target') != target:
            raise ValueError('S3 manifest has unexpected schema or target')
        return row

    def load_links(self) -> dict[tuple[str, str], dict]:
        result = {}
        for key, row in self.scan('links/').items():
            source, source_id = row.get('source'), row.get('source_id')
            if not source or not source_id or not isinstance(row.get('bitrix_id'), int) or row['bitrix_id'] < 1:
                raise ValueError('Invalid S3 link: ' + key)
            if key != 'links/' + link_id(source, source_id) + '.json':
                raise ValueError('S3 link key/hash mismatch: ' + key)
            pair = (source, source_id)
            if pair in result:
                raise ValueError('Duplicate S3 link: ' + str(pair))
            result[pair] = row
        return result

    def save_link(self, source: str, source_id: str, bitrix_id: int, *, url: str = '', title: str = '') -> dict:
        if not isinstance(bitrix_id, int) or bitrix_id < 1:
            raise ValueError('Invalid Bitrix ID')
        key = 'links/' + link_id(source, source_id) + '.json'
        previous = self.read(key)
        if previous and (previous['source'], previous['source_id']) != (source, source_id):
            raise ValueError('Link hash collision or registry corruption')
        if previous and previous['bitrix_id'] != bitrix_id:
            raise ValueError(f'S3 link conflict: {source}:{source_id} -> {previous["bitrix_id"]} != {bitrix_id}')
        row = {'schema': 1, 'source': source, 'source_id': source_id,
               'bitrix_id': bitrix_id, 'source_url': url, 'title': title,
               'created_at': previous.get('created_at') if previous else utc_now(),
               'updated_at': utc_now()}
        self.write(key, row)
        return row

    def load_categories(self) -> dict[tuple[str, str], int]:
        result = {}
        for key, row in self.scan('categories/').items():
            source, category = row.get('source'), row.get('category')
            section_id = row.get('section_id')
            if not source or not category or not isinstance(section_id, int) or section_id < 1:
                raise ValueError('Invalid S3 category mapping: ' + key)
            if key != 'categories/' + category_id(source, category) + '.json':
                raise ValueError('Invalid S3 category key: ' + key)
            result[(source, category.strip().casefold())] = section_id
        return result

    def set_category(self, source: str, category: str, section_id: int) -> None:
        if not isinstance(section_id, int) or section_id < 1:
            raise ValueError('section_id must be positive')
        self.write('categories/' + category_id(source, category) + '.json',
                   {'schema': 1, 'source': source, 'category': category.strip(),
                    'section_id': section_id, 'updated_at': utc_now()})

    def set_pending_category(self, source: str, category: str, example: str) -> None:
        name = 'pending/' + category_id(source, category) + '.json'
        if self.read(name) is not None:
            return
        self.write(name, {'schema': 1, 'source': source, 'category': category.strip(),
                          'example': example, 'created_at': utc_now()})
