"""Общие правила распознавания товара без списков конкретных брендов/моделей."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

# Технические маркеры -- это не марки и не модели. Не содержит конкретные товары.
TECHNICAL = frozenset({'CNG', 'LNG', 'DIESEL', 'EURO', 'HP', 'ABS', 'ESP', 'AT', 'MT', 'RWD', 'AWD', 'AGR', '4X2', '6X4', '8X4'})
MODEL_PATTERN = re.compile(r'(?<![A-Z0-9])(?=[A-Z0-9-]{2,32}(?![A-Z0-9]))(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*[0-9])[A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Z0-9])', re.I)
WHEELS = re.compile(r'(?<!\d)(?:4|6|8|10|12)\s*[xх×*]\s*(?:2|4|6|8|10)(?!\d)', re.I)
POWER = re.compile(r'(?<!\d)(\d{2,4}(?:[.,]\d+)?)\s*(?:л\.?\s*с\.?|hp)\b', re.I)
VOLUME = re.compile(r'(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:м\s*[³3]|куб\.?\s*м?)\b', re.I)


def clean(s: str) -> str:
    return ' '.join(str(s or '').split())


# Смешанные русско-латинские коды моделей: «Т5G» -> «T5G», «С7Н» -> «C7H».
# Только токены с цифрами, которые целиком состоят из латиницы и похожих букв.
# Обычные русские слова и названия разделов не транслитерируются.
_MODEL_HOMOGLYPHS = str.maketrans({
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
    'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X',
})


def _canonical_model_token(match: re.Match) -> str:
    token = match.group(0)
    if not any(ch.isdigit() for ch in token):
        return token
    candidate = token.translate(_MODEL_HOMOGLYPHS)
    return candidate if re.fullmatch(r'[A-Z0-9-]+', candidate) else token


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', str(s or '')).upper().replace('Ё', 'Е').replace('Х', 'X')
    s = re.sub(r'(?<![\w])[A-ZА-Я0-9]+(?:-[A-ZА-Я0-9]+)*(?![\w])',
               _canonical_model_token, s)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\-\s]', ' ', s)).strip()


def base_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), (p.netloc or '').lower(), p.path.rstrip('/') + '/', '', '', ''))


def record_key(source: str, url: str, id_pattern: str = '') -> str:
    """Stable site-specific ID from configured pattern, else a URL-derived key.

    A factory model code is not necessarily a unique product/variant identifier.
    """
    if id_pattern:
        match = re.search(id_pattern, urlparse(url).path)
        if match:
            candidate = match.groupdict().get('id') or match.group(1)
            if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', candidate):
                return candidate
    return 'u' + hashlib.sha256(base_url(url).encode()).hexdigest()[:30]


def extract_brand(title: str, specs: dict[str, str], structured: str = '') -> str:
    if clean(structured):
        return norm(structured)
    for k, value in specs.items():
        label = norm(k)
        if any(s in label for s in ('ДВИГАТ', 'МОТОР', 'КПП', 'ГИДРО', 'НАСОС')):
            continue
        if any(s in label for s in ('БРЕНД', 'МАРКА ТЕХНИКИ', 'МАРКА АВТОМОБИЛЯ', 'ПРОИЗВОДИТЕЛЬ ТЕХНИКИ', 'ПРОИЗВОДИТЕЛЬ МАШИНЫ')):
            return norm(value)
    # Статистика о марках должна приходить со страницы; если нет структурированного
    # поля, первое латинское слово без цифр выступает консервативным кандидатом.
    for token in re.findall(r'(?<![A-Z0-9])([A-Z]{2,})(?![A-Z0-9])', norm(title)):
        if token not in TECHNICAL:
            return token
    # У части LiftNet техники марка только кириллицей: «Четра Е220».
    # Не угадываем по справочнику марок: берём слово непосредственно
    # перед явно определённой моделью, исключая общие названия техники.
    model = extract_model(title, specs)
    if model:
        tokens = re.findall(r'[A-ZА-Я0-9-]+', norm(title))
        generic_words = {
            'ЭКСКАВАТОР', 'ПОГРУЗЧИК', 'ТЯГАЧ', 'АВТОБЕТОНОСМЕСИТЕЛЬ',
            'САМОСВАЛ', 'АВТОКРАН', 'КАТОК', 'ГРЕЙДЕР', 'ГУСЕНИЧНЫЙ',
            'КОЛЕСНЫЙ', 'ФРОНТАЛЬНЫЙ', 'МИНИ', 'МИНИПОГРУЗЧИК',
            'ОДНОКОВШОВЫЙ', 'ДОРОЖНЫЙ', 'МАШИНА', 'ШАССИ',
        }
        for index, token in enumerate(tokens):
            if token == model and index > 0:
                candidate = tokens[index - 1]
                if re.fullmatch(r'[А-Я]{3,}', candidate) and candidate not in generic_words:
                    return candidate
    return ''


def extract_model(title: str, specs: dict[str, str], structured: str = '') -> str:
    if structured:
        return norm(structured)
    for k, v in specs.items():
        label = norm(k)
        if 'МОДЕЛЬ' in label and not any(x in label for x in ('ДВИГАТ', 'МОТОР', 'НАСОС', 'КПП')):
            found = MODEL_PATTERN.search(norm(v))
            if found:
                return found.group(0)
    candidates = MODEL_PATTERN.findall(norm(title))
    for candidate in candidates:
        if WHEELS.fullmatch(candidate) or candidate in TECHNICAL or re.fullmatch(r'R\d+', candidate):
            continue
        return candidate
    # Технические индексы с нелатинской частью («ПК-3») одинаково
    # нормализуются на обоих сайтах. Это не список конкретных моделей.
    rus_code = re.search(r'(?<![А-ЯA-Z0-9])([А-Я]{1,5}-?\d{1,5}[А-ЯA-Z0-9-]*)(?![А-ЯA-Z0-9])', norm(title))
    if rus_code:
        return rus_code.group(1)
    # Есть модельные индексы из одних букв (например HOWO TX).
    # Требуем место непосредственно перед колесной формулой, чтобы не
    # принять «АКПП», марку или рекламный прилагательный за модель.
    m = re.search(r'\b([A-Z]{2,10}(?:-[A-Z]{2,10})?)\s+(?=(?:4|6|8|10|12)X(?:2|4|6|8|10)\b)', norm(title))
    if m and m.group(1) not in TECHNICAL:
        # Слово перед формулой должно следовать за отдельной маркой,
        # иначе «SITRAK 6x4» ошибочно превращается в модель SITRAK.
        before = re.findall(r'(?<![A-ZА-Я0-9])([A-Z]{2,})(?![A-ZА-Я0-9])', norm(title)[:m.start()])
        if before:
            return m.group(1)
    return ''


# Универсальный длинный заводской код, не конкретная марка/модель.
FACTORY_CODE = re.compile(r'(?<![A-Z0-9])(?=[A-Z0-9-]{10,30}(?![A-Z0-9]))(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Z0-9])', re.I)
# Код шасси может быть слит с колёсной формулой «4x2ZZ4186...»,
# тогда прежнее регулярное выражение ошибочно возвращало «4X2ZZ...». 
FACTORY_CORE = re.compile(r'(?<![A-Z])([A-Z]{1,4}\d{4,}[A-Z0-9]{4,})(?![A-Z0-9])', re.I)


def _factory_from_text(value: str) -> str:
    normalized = norm(value)
    match = FACTORY_CORE.search(normalized)
    if match:
        return match.group(1)
    match = FACTORY_CODE.search(normalized)
    return match.group(0) if match else ''


def extract_modification(specs: dict[str, str], title: str = '') -> str:
    for k, v in specs.items():
        if 'МОДИФИКАЦ' in norm(k) or 'ЗАВОДСКОЙ КОД' in norm(k):
            found = _factory_from_text(v)
            if found:
                return found
    # В АКомфорт код шасси часто содержится только в названии товара.
    return _factory_from_text(title)


def configuration(item) -> dict[str, str]:
    title = norm(item.title)
    specs = {norm(k): norm(v) for k, v in item.specs.items()}
    def prop(*names):
        return next((value for label, value in specs.items() if any(name in label for name in names)), '')
    fuel = norm(' '.join((title, prop('ТОПЛИВ', 'ТИП ДВИГАТЕЛЯ'))))
    if re.search(r'\bCNG\b|\bКПГ\b|СЖАТ.*ГАЗ', fuel):
        fuel = 'CNG'
    elif re.search(r'\bLNG\b|\bСПГ\b|СЖИЖЕН.*ГАЗ', fuel):
        fuel = 'LNG'
    elif 'ДИЗЕЛ' in fuel:
        fuel = 'DIESEL'
    elif 'МЕТАН' in fuel or 'ГАЗОВЫЙ' in fuel:
        fuel = 'GAS_UNKNOWN'
    else:
        fuel = ''
    power = POWER.search(item.title + ' ' + prop('МОЩНОСТ'))
    power_hp = power.group(1).replace(',', '.') if power else ''
    if not power_hp:
        # Трёхзначная мощность непосредственно перед обозначением КПП в названии.
        # Числа размеров, базы и заводского кода сюда не попадают.
        context = re.search(r'(?<![A-Z0-9])(\d{3})\s+(?=АКПП|МКПП)\b', title)
        if context and 100 <= int(context.group(1)) <= 999:
            power_hp = context.group(1)
    if not power_hp:
        val = prop('МОЩНОСТ')
        # Numeric power without units is only unambiguous if label itself says horsepower.
        if 'Л С' in next((k for k in specs if 'МОЩНОСТ' in k), ''):
            m = re.search(r'\d+(?:[.,]\d+)?', val)
            if m:
                power_hp = m.group(0).replace(',', '.')
    gearbox = 'AT' if 'АКПП' in title else 'MT' if 'МКПП' in title else ''
    if not gearbox:
        raw = prop('КПП', 'КОРОБК', 'ТРАНСМИСС')
        gearbox = 'AT' if 'АВТОМАТ' in raw or 'АКПП' in raw else 'MT' if 'МЕХАНИЧ' in raw or 'МКПП' in raw else ''
    body = VOLUME.search(item.title + ' ' + prop('ОБЪЕМ КУЗОВА', 'ОБЪЕМ БУНКЕРА'))
    wheel = WHEELS.search(item.title + ' ' + prop('КОЛЕСН'))
    tire = re.search(r'\bR\s*(\d{2}(?:[.,]\d+)?)\b', title)
    # An uppercase trim directly following the model is a variant label even
    # when it is not a numeric model code (e.g. a manufacturer-specific trim).
    suffix = ''
    if item.model:
        m = re.search(r'(?i:' + re.escape(item.model) + r')\s+([A-Z]{3,12})(?![a-zA-Z0-9])', item.title)
        if m and m.group(1) not in TECHNICAL:
            suffix = m.group(1)
    if not suffix and item.model and re.search(re.escape(norm(item.model)) + r'\s+МАХ\b', title):
        suffix = 'MAX'
    # Варианты, которые часто различают две карточки одного C7H, нельзя
    # отбрасывать: ADR, КОМ, тип подвески и исполнение коробки.
    full = title + ' ' + prop('ПОДВЕСК', 'РЕССОР', 'ТИП КПП', 'КОМПЛЕКТАЦ', 'ADR')
    adr = 'YES' if re.search(r'\bADR\b|\bАДР\b', full) else ''
    pto = 'YES' if re.search(r'(?<![A-ZА-Я0-9])КОМ(?![A-ZА-Я0-9])|КОРОБК\w* ОТБОРА МОЩНОСТ', full) else ''
    suspension = ('HEAVY_SPRING' if re.search(r'УСИЛЕНН\w* РЕССОР', full) else
                  'SPRING' if 'РЕССОР' in full else
                  'PNEUMATIC' if re.search(r'ПНЕВМ\w*', full) else '')
    gearbox_variant = 'HW' if re.search(r'\bАКПП\s+HW\b', full) else ''
    return {'brand': norm(item.brand), 'model': norm(item.model), 'variant_suffix': suffix,
            'modification': norm(item.modification), 'fuel': fuel,
            'wheel': norm(wheel.group(0)).replace(' ', '') if wheel else '',
            'power_hp': power_hp, 'gearbox': gearbox,
            'body_m3': body.group(1).replace(',', '.') if body else '',
            'tire': ('R' + tire.group(1)) if tire else '',
            'retarder': 'YES' if re.search(r'\bРЕТАРДЕР\w*|\bРЕТ\b', title) else '',
            'visor': 'YES' if 'КОЗЫРЕК' in title else '',
            'adr': adr, 'pto': pto, 'suspension': suspension,
            'gearbox_variant': gearbox_variant}
