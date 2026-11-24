"""Семантический тип машины для сопоставления между разными деревьями каталогов.

Раздел Битрикс нужен для создания товара, но не является частью идентичности
существующей машины. Например, «Тягачи» и «Седельные тягачи 6x4» — tractor.
Список ниже содержит только типы техники, не марки/модели/ID разделов.
"""
from __future__ import annotations

import re

from .identity import norm

# Более специфичные типы должны идти перед общими («погрузчик» и «фургон»).
FAMILY_PATTERNS = (
    (r'КРАНО?МАНИПУЛЯТОР|КРАН.МАНИПУЛЯТОР|\bКМУ\b|МАНИПУЛЯТОРНАЯ УСТАНОВКА', 'kmu'),
    (r'ИЗОТЕРМИЧЕСК\w*\s+ФУРГОН', 'isothermal_van'),
    (r'ФУРГОН\w*\s+РЕФРИЖЕРАТОР|РЕФРИЖЕРАТОР', 'refrigerated_van'),
    (r'ЭКСКАВАТОР\w*.ПОГРУЗЧИК|ПОГРУЗЧИК\w*.ЭКСКАВАТОР', 'backhoe'),
    (r'АВТОБЕТОНОСМЕСИТЕЛ|БЕТОНОСМЕСИТЕЛ|\bМИКСЕР\b', 'mixer'),
    (r'САМОСВАЛ', 'dump_truck'),
    (r'ТЯГАЧ', 'tractor'),
    (r'МИНИ[\s-]?ПОГРУЗЧИК', 'skid_steer'),
    (r'ФРОНТАЛЬН\w*\s+ПОГРУЗЧИК', 'wheel_loader'),
    (r'АВТОГРЕЙДЕР|ГРЕЙДЕР', 'grader'),
    (r'ЭКСКАВАТОР', 'excavator'),
    (r'АВТОКРАН|КРАН\s+АВТОМОБИЛЬНЫЙ', 'truck_crane'),
    (r'КАТОК', 'roller'),
    (r'АВТОБУС', 'bus'),
    (r'ПОЛУПРИЦЕП', 'semi_trailer'),
    (r'ПРИЦЕП', 'trailer'),
    (r'ФУРГОН', 'van'),
    (r'ПОГРУЗЧИК', 'wheel_loader'),
    (r'ШАССИ', 'chassis'),
    (r'ЗАПЧАСТ|ЗАПАСН\w*\s+ЧАСТ', 'parts'),
)


def family_of(item) -> str:
    # Тип в названии товара предпочтительнее общей категории «Спецтехника».
    # Но тип в имени может быть рекламным: смотрим только начало названия.
    title = norm(item.title)[:135]
    for pattern, family in FAMILY_PATTERNS:
        if re.search(pattern, title):
            return family
    for category in reversed([item.category, *(item.category_path or [])]):
        text = norm(category)
        for pattern, family in FAMILY_PATTERNS:
            if re.search(pattern, text):
                return family
    return ''
