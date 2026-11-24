"""Атомарные контрольные точки обхода и чтения API.

Файлы checkpoint — техническое состояние запуска, а не подтверждённый
снимок каталога. Запись в Битрикс из возобновлённого состояния запрещена.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

CHECKPOINT_VERSION = 1


def fingerprint(obj: object) -> str:
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj: object) -> None:
    """Atomically write JSON, retrying transient Windows file-sharing locks.

    A unique temp filename avoids reusing a .tmp file while an antivirus,
    indexer or synchronizer may still have it open. The previous JSON remains
    intact if the rename ultimately fails.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.05 * (2 ** attempt), 0.6))
    finally:
        temp.unlink(missing_ok=True)


def read_checkpoint(path: Path, expected_fingerprint: str) -> dict | None:
    if not path.is_file():
        return None
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get('version') != CHECKPOINT_VERSION:
        raise ValueError(f'Неверная версия checkpoint: {path}. Запусти без --resume.')
    if data.get('fingerprint') != expected_fingerprint:
        raise ValueError(f'Изменена конфигурация checkpoint: {path}. Запусти без --resume.')
    return data


def save_checkpoint(path: Path, expected_fingerprint: str, state: dict) -> None:
    write_json(path, {'version': CHECKPOINT_VERSION,
                      'fingerprint': expected_fingerprint, **state})
