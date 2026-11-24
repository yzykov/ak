"""Conservative mapping of supplier specifications to the actual Bitrix iblock #5 card fields.

Never invent values: absent or ambiguous supplier data stays absent. The wheel-schema
image is selected server-side from existing Akomfort products, not from the supplier.
"""
from __future__ import annotations

import re
import unicodedata

from .models import Item

# All scalar codes exist in the supplied properties.json (2026-11-17).
SINGLE_CODES = frozenset({
    'BRAND', 'FORM_WHEEL', 'FORM_WHEEL_TITLE', 'WHEEL_DRIVE',
    'TRANSMISSION', 'TRANSMISSION_TITLE', 'ENGINE', 'POWER', 'VOLUME',
    'SUSPENSION_NAME', 'RASHOD', 'REGISTRATION', 'WEIGHT', 'FULL_WEIGHT',
    'BEARING', 'VOLUME_BODY', 'EXP_WHEIGHT', 'VOLUME_COVWA',
    'ENGINE_VOLUME', 'MODEL_ENGINE', 'CAPACITY', 'ECO_CLASS', 'DLINA_STREL',
})
MULTI_CODES = frozenset({'SUSPENSION'})


def _key(value: str) -> str:
    text = unicodedata.normalize('NFKC', str(value)).casefold().replace('ё', 'е')
    return re.sub(r'[^\wа-я]+', ' ', text).strip()


def _lookup(specs: dict[str, str], *keys: str) -> str:
    candidates = {_key(x) for x in keys}
    for name, value in specs.items():
        if _key(name) in candidates and str(value).strip():
            return str(value).strip()[:255]
    return ''


WHEEL_PATTERN = re.compile(r'(?<!\d)(4|6|8|10|12)\s*[xх×]\s*(2|4|6|8|10|12)(?!\d)', re.I)
TRUCK_TOKENS = ('самосвал', 'тягач', 'грузов', 'шасси', 'автобетоно', 'миксер', 'зерновоз', 'фургон', 'бортов')
GAS_FUEL_PATTERN = re.compile(r'(?<![\w])(?:CNG|LNG|КПГ|СПГ)(?![\w])|\b(?:метан\w*|газомоторн\w*|газов\w*)\b', re.I)


def card_warnings(item: Item) -> list[str]:
    """Flag unequivocal supplier/title contradictions for manual review.

    This is a narrow safety check, not a general fuel classifier. We do not
    automatically change a diesel description into a gas-engine description.
    """
    raw_engine = _lookup(item.specs or {}, 'Двигатель', 'Тип двигателя')
    if GAS_FUEL_PATTERN.search(item.title) and 'дизел' in _key(raw_engine):
        return ['Название указывает газовое топливо (CNG/LNG/метан), '
                'но характеристика «Двигатель» содержит «дизель»; проверьте источник']
    return []


def normalize_wheel(value: str) -> str:
    if not value:
        return ''
    match = WHEEL_PATTERN.search(str(value))
    if not match:
        return ''
    a, b = int(match.group(1)), int(match.group(2))
    return f'{a}x{b}' if b <= a else ''


def _transmission(raw: str) -> tuple[str, str]:
    if not raw:
        return '', ''
    s = _key(raw)
    if 'мкпп' in s or 'механическ' in s or 'manual' in s:
        short = 'МКПП'
    elif 'акпп' in s or 'автоматическ' in s or 'automatic' in s:
        short = 'АКПП'
    elif 'амт' in s or 'роботизир' in s or 'robotized' in s:
        short = 'АМТ'
    else:
        # Detailed gearbox name alone is not enough to infer its operating type.
        return raw[:255], ''
    return short, raw[:255] if _key(raw) != _key(short) else ''


def card_properties(item: Item) -> dict[str, str | list[str]]:
    specs = item.specs or {}
    p: dict[str, str | list[str]] = {}
    if item.brand:
        p['BRAND'] = item.brand[:255]

    wheel_raw = _lookup(specs, 'Колесная формула', 'Колёсная формула', 'Колесная схема', 'Wheel formula', 'Wheel arrangement')
    wheel = normalize_wheel(wheel_raw)
    if not wheel and any(token in _key(item.title + ' ' + item.category) for token in TRUCK_TOKENS):
        wheel = normalize_wheel(item.title)
    if wheel:
        p['FORM_WHEEL'] = wheel
        p['FORM_WHEEL_TITLE'] = 'Колёсная формула'
    drive = _lookup(specs, 'Привод', 'Тип привода')
    if drive:
        p['WHEEL_DRIVE'] = drive

    raw_transmission = _lookup(specs, 'Коробка передач', 'КПП', 'Модель КПП',
                               'Трансмиссия', 'Transmission', 'Gearbox')
    type_transmission = _lookup(specs, 'Тип коробки передач', 'Тип КПП', 'Тип трансмиссии')
    raw_transmission = raw_transmission or type_transmission
    short, details = _transmission(raw_transmission)
    if type_transmission:
        classified, _ = _transmission(type_transmission)
        if classified in ('МКПП', 'АКПП', 'АМТ'):
            short = classified
    if short:
        p['TRANSMISSION'] = short
    if raw_transmission:
        p['TRANSMISSION_TITLE'] = 'Коробка передач'

    mappings = {
        'ENGINE': ('Двигатель', 'Тип двигателя'),
        'MODEL_ENGINE': ('Модель двигателя', 'Двигатель модель', 'Engine model'),
        'POWER': ('Мощность двигателя л с', 'Мощность, л.с.', 'Мощность', 'Мощность двигателя', 'Engine power'),
        'VOLUME': ('Объем двигателя', 'Объём двигателя', 'Рабочий объем двигателя'),
        'RASHOD': ('Расход на 100 км', 'Расход топлива на 100 км', 'Расход топлива'),
        'REGISTRATION': ('Регистрация', 'Документ регистрации', 'ПТС или ПСМ'),
        'WEIGHT': ('Снаряженная масса', 'Снаряжённая масса'),
        'FULL_WEIGHT': ('Полная масса', 'Разрешенная максимальная масса', 'Полная масса автомобиля'),
        'BEARING': ('Грузоподъемность', 'Грузоподъёмность'),
        'VOLUME_BODY': ('Объем кузова', 'Объём кузова'),
        'EXP_WHEIGHT': ('Эксплуатационная масса', 'Эксплуатационный вес'),
        'VOLUME_COVWA': ('Объем ковша', 'Объём ковша'),
        'ENGINE_VOLUME': ('Рабочий объём двигателя, л', 'Рабочий объем двигателя, л'),
        'DLINA_STREL': ('Длина стрелы', 'Длина стреллы', 'Вылет стрелы'),
        'CAPACITY': ('Вместимость',),
        'ECO_CLASS': ('Экологический класс', 'Экокласс', 'Эко класс'),
    }
    for code, aliases in mappings.items():
        value = _lookup(specs, *aliases)
        if value:
            p[code] = value
    # Never publish a known contradictory engine type. Other fields may still
    # be useful, but repair_cards.py holds the whole card for manual review.
    if card_warnings(item):
        p.pop('ENGINE', None)
    if 'MODEL_ENGINE' in p and 'ENGINE' not in p and not card_warnings(item):
        p['ENGINE'] = p['MODEL_ENGINE']
    suspension = _lookup(specs, 'Подвеска', 'Тип подвески')
    if suspension:
        # The native Bitrix template uses SUSPENSION_NAME for the visible tile,
        # and multi-valued SUSPENSION for the details in the hover area.
        # Only classify unambiguous types; never invent missing hardware specs.
        suspension_normalized = _key(suspension)
        if 'гидропневмат' in suspension_normalized:
            p['SUSPENSION_NAME'] = 'Гидропневмо'
        elif 'пневмат' in suspension_normalized:
            p['SUSPENSION_NAME'] = 'Пневмо'
        elif 'рессор' in suspension_normalized:
            p['SUSPENSION_NAME'] = 'Рессорная'
        elif 'пружин' in suspension_normalized:
            p['SUSPENSION_NAME'] = 'Пружинная'
        p['SUSPENSION'] = [suspension]
    return p


# Canonical supplier field aliases that the native tile/card properties already
# display. Remove these from HARACTERISTICS only when a matching card field is
# actually present; do not hide information merely because we recognise a name.
CARD_SPEC_ALIASES: dict[str, tuple[str, ...]] = {
    'FORM_WHEEL': ('Колесная формула', 'Колёсная формула', 'Колесная схема',
                   'Wheel formula', 'Wheel arrangement'),
    'TRANSMISSION': ('КПП', 'Коробка передач', 'Модель КПП', 'Тип КПП',
                     'Тип коробки передач', 'Трансмиссия', 'Transmission', 'Gearbox'),
    'ENGINE': ('Двигатель', 'Тип двигателя', 'Модель двигателя', 'Engine model'),
    'POWER': ('Мощность', 'Мощность двигателя', 'Мощность двигателя л с',
              'Мощность, л.с.', 'Engine power'),
    'VOLUME': ('Объем двигателя', 'Объём двигателя', 'Рабочий объем двигателя'),
    'SUSPENSION': ('Подвеска', 'Тип подвески'),
    'RASHOD': ('Расход на 100 км', 'Расход топлива', 'Расход топлива на 100 км'),
    'REGISTRATION': ('Регистрация', 'Документ регистрации', 'ПТС или ПСМ'),
    'WEIGHT': ('Снаряженная масса', 'Снаряжённая масса'),
    'FULL_WEIGHT': ('Полная масса', 'Полная масса автомобиля',
                    'Разрешенная максимальная масса'),
    'BEARING': ('Грузоподъемность', 'Грузоподъёмность'),
    'VOLUME_BODY': ('Объем кузова', 'Объём кузова'),
    'EXP_WHEIGHT': ('Эксплуатационная масса', 'Эксплуатационный вес'),
    'VOLUME_COVWA': ('Объем ковша', 'Объём ковша'),
    'CAPACITY': ('Вместимость',),
    'ECO_CLASS': ('Экологический класс', 'Эко класс', 'Экокласс'),
    'DLINA_STREL': ('Длина стрелы', 'Длина стреллы', 'Вылет стрелы'),
}


def covered_spec_entries(item: Item, props: dict | None = None) -> list[dict[str, str]]:
    """Source rows replicated in a native card; do not remove other rows."""
    props = card_properties(item) if props is None else props
    aliases = {}
    for code, names in CARD_SPEC_ALIASES.items():
        if props.get(code):
            for name in names:
                aliases[_key(name)] = code
    if props.get('BRAND'):
        for name in ('Бренд', 'Марка', 'Производитель техники'):
            aliases[_key(name)] = 'BRAND'
    return [{'name': name, 'value': str(value), 'property_code': aliases[_key(name)]}
            for name, value in item.specs.items()
            if name and value and _key(name) in aliases]
