"""Загрузка фото поставщиков в памяти, проверка формата и конвертация в JPEG."""
from __future__ import annotations

import hashlib
import io
import logging
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Item

LOG = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 35_000_000


class PhotoError(ValueError):
    pass


def prepare_photo(raw: bytes, *, max_output: int = 850_000, max_edge: int = 1800) -> bytes:
    if not raw:
        raise PhotoError('Пустое изображение')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            if image.width < 160 or image.height < 140:
                raise PhotoError('Слишком маленькое изображение')
            if image.width * image.height > 35_000_000:
                raise PhotoError('Слишком большое изображение в пикселях')
            if image.mode in ('RGBA', 'LA', 'P'):
                rgba = image.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel('A'))
                image = bg
            else:
                image = image.convert('RGB')
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            quality = 88
            while True:
                buffer = io.BytesIO()
                image.save(buffer, format='JPEG', optimize=True, quality=quality)
                result = buffer.getvalue()
                if len(result) <= max_output:
                    return result
                if quality > 63:
                    quality -= 8
                elif image.width > 650 and image.height > 500:
                    image.thumbnail((int(image.width * .85), int(image.height * .85)), Image.Resampling.LANCZOS)
                    quality = 78
                else:
                    raise PhotoError('Фотография не помещается в лимит')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise PhotoError('Файл не является допустимой фотографией') from exc


def download_images(item: Item, source: dict, *, timeout: int,
                    max_images: int = 8, max_download_bytes: int = 7_340_032) -> tuple[list[tuple[str, bytes]], list[str]]:
    allowed_hosts = {x.lower() for x in source.get('allowed_image_hosts', [])}
    if not allowed_hosts:
        raise PhotoError('Для источника не настроены домены изображений')
    session = requests.Session()
    session.headers['User-Agent'] = 'AkomfortCatalogSync/2.0'
    results: list[tuple[str, bytes]] = []
    warnings = []
    seen_hashes = set()
    for url in item.images:
        if len(results) >= max_images:
            break
        parsed = urlparse(url)
        if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in allowed_hosts:
            warnings.append(f'Пропущено изображение с постороннего домена: {url[:200]}')
            continue
        try:
            with session.get(url, stream=True, timeout=timeout, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise PhotoError(f'HTTP {response.status_code}')
                length = int(response.headers.get('Content-Length', '0') or '0')
                if length > max_download_bytes:
                    raise PhotoError('Файл слишком велик')
                blob = bytearray()
                for chunk in response.iter_content(128 * 1024):
                    blob.extend(chunk)
                    if len(blob) > max_download_bytes:
                        raise PhotoError('Превышен лимит загружаемого файла')
                photo = prepare_photo(bytes(blob))
                digest = hashlib.sha256(photo).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                results.append((f'catalog-{item.source}-{digest[:16]}.jpg', photo))
        except (requests.RequestException, PhotoError, ValueError) as exc:
            warnings.append(f'{url[:150]}: {exc}')
    return results, warnings
