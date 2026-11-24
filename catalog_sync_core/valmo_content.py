"""Valmo product tabs: main specifications, description, detailed systems and equipment.

Only the product's own .cardd__tab is parsed, never cross-sells/brands/banners.
Conflicting source claims remain in the local snapshot/report, not in public payload.
"""
from __future__ import annotations

import re
from bs4 import Tag, BeautifulSoup, NavigableString
from .identity import clean

WHEELS = re.compile(r'(?<!\d)(?:4|6|8|10|12)\s*[xх×]\s*(?:2|4|6|8|10)(?!\d)', re.I)
NORM_POWER = re.compile(r'(?<!\d)(\d{2,4})\s*(?:л\s*\.?\s*с\.?|лс)\b', re.I)
HEADINGS = {'двигатель', 'кпп', 'тормозная система', 'выхлопная система',
            'топливные баки', 'шины', 'комплектация', 'подвеска',
            'электрооборудование', 'кабина', 'гидравлическая система'}
AD_STOP = re.compile(r'^(?:цена\b|купить\b|заказать\b|приобрести\b|как купить\b)', re.I)


def _wheel(value: str) -> str:
    m = WHEELS.search(str(value))
    return re.sub(r'\s*[xх×]\s*', 'x', m.group(0).lower()) if m else ''


def _field(specs: dict[str, str], *aliases: str) -> str:
    normalized = lambda x: re.sub(r'[^а-яa-z0-9]+', '', str(x).casefold().replace('ё', 'е'))
    wanted = {normalized(x) for x in aliases}
    return next((v for k, v in specs.items() if normalized(k) in wanted and v), '')


def _spec_tabs(soup: BeautifulSoup) -> tuple[Tag | None, Tag | None]:
    desc = char = None
    for li in soup.select('.cardd__tab .accordion-tabs > li'):
        heading = li.select_one('a')
        body = li.select_one('section')
        if not heading or not body:
            continue
        label = clean(heading.get_text(' ', strip=True)).casefold().replace('ё', 'е')
        if 'описан' in label:
            desc = body
        elif 'характеристик' in label:
            char = body
    return desc, char


def _lines(node: Tag) -> list[str]:
    """Split <br> and block breaks without concatenating adjacent equipment items."""
    # BS4's separator creates a newline for every fragment; normalize whitespace
    # within ordinary paragraphs but preserve explicit <br> boundaries.
    html = str(node)
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    view = BeautifulSoup(html, 'html.parser')
    return [clean(line) for line in view.get_text(' ', strip=True).replace('\xa0', ' ').split('\n') if clean(line)]


def _text_lines(node: Tag) -> list[str]:
    content = BeautifulSoup(str(node), 'html.parser')
    for br in content.select('br'):
        br.replace_with('\n')
    text = content.get_text(' ', strip=True)
    return [clean(line) for line in text.split('\n') if clean(line)]


def _description_lines(root: Tag | None) -> list[str]:
    if root is None:
        return []
    lines: list[str] = []
    stop = False
    # Process only direct content of the description tab, before the vendor's
    # sales/price section. A number of Valmo paragraphs are bare text nodes.
    for child in root.children:
        if isinstance(child, NavigableString):
            value = clean(str(child))
            if value and not stop:
                lines.append(value)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in ('h2', 'h3', 'h4'):
            label = clean(child.get_text(' ', strip=True))
            if AD_STOP.match(label):
                stop = True
            elif not stop and label and not label.startswith('Характеристики'):
                lines.append(label)
            continue
        if stop:
            continue
        if child.name == 'li':
            candidates = [child]
        elif child.name in ('ul', 'ol'):
            candidates = child.find_all('li')
        elif child.name in ('p', 'div'):
            candidates = [child]
        else:
            candidates = []
        for node in candidates:
            value = clean(node.get_text(' ', strip=True))
            if value and not re.search(r'^Информация о характеристиках товаров может', value, re.I):
                lines.append(value)
    return list(dict.fromkeys(lines))


def _split_semi(value: str) -> list[str]:
    return [clean(x) for x in re.split(r';\s*', value) if clean(x)]


def extract_valmo(soup: BeautifulSoup) -> tuple[dict[str, str], list[str], list[str], list[dict[str, str]]]:
    """Returns (structured/main + detailed specs, sanitized description, equipment, conflicts).

    The structured main span-pairs are authoritative *candidates*, but a
    disagreement with the title or narrative causes those fields to be held
    out of public native properties until reviewed.
    """
    desc_tab, specs_tab = _spec_tabs(soup)
    main: dict[str, str] = {}
    detail: dict[str, str] = {}
    equip: list[str] = []
    root = specs_tab.select_one('.cardd__list') if specs_tab else None
    if root is None:
        root = soup.select_one('.cardd__right .cardd__characteristics .cardd__list')
    section = ''
    if root is not None:
        for p in root.children:
            if isinstance(p, NavigableString):
                if section.casefold() == 'комплектация':
                    value = clean(str(p))
                    if 4 <= len(value) <= 700:
                        equip.append(value)
                continue
            if not isinstance(p, Tag) or p.name == 'br':
                continue
            if p.name == 'p':
                pair = p.find_all('span', recursive=False)
                if len(pair) == 2:
                    key, val = [clean(x.get_text(' ', strip=True)) for x in pair]
                    key = key.rstrip(':').strip()
                    if key and val and len(key) < 90:
                        main.setdefault(key, val[:1000])
                    continue
                label = clean(p.get_text(' ', strip=True))
                if p.find('b') and label.casefold().replace('ё', 'е') in HEADINGS:
                    section = label
                    continue
            if not section:
                continue
            parts = _text_lines(p)
            for part in parts:
                if section.casefold() == 'комплектация':
                    if len(part) >= 4 and len(part) <= 700:
                        equip.append(part)
                else:
                    key = section + ' — подробности'
                    existing = detail.get(key, '')
                    detail[key] = (existing + '; ' + part if existing else part)[:3500]

    # Supplier's compact card is demonstrably inconsistent with its own prose.
    # Never import compact card values as specifications.
    text_lines = _description_lines(desc_tab)
    title_node = soup.select_one('.cardd__title h1') or soup.select_one('h1')
    title = clean(title_node.get_text(' ', strip=True)) if title_node else ''
    conflicts: list[dict[str, str]] = []
    narrative_wheels = set(filter(None, [_wheel(title)] + [_wheel(line) for line in text_lines
                             if re.search(r'кол[её]сная формула|тягач|самосвал|шасси', line, re.I)]))
    compact_wheel = _wheel(_field(main, 'Колёсная формула', 'Колесная формула'))
    if compact_wheel and narrative_wheels and compact_wheel not in narrative_wheels:
        conflicts.append({'field': 'FORM_WHEEL', 'structured': compact_wheel,
                          'text': ', '.join(sorted(narrative_wheels)), 'reason': 'Недостоверная краткая карточка'})
    # Narrative may itself disagree with the expanded technical blocks.
    narrative_power = set()
    for line in text_lines:
        if re.search(r'двигател|мощност', line, re.I):
            narrative_power.update(NORM_POWER.findall(line))
    detail_power = set()
    for value in detail.values():
        detail_power.update(NORM_POWER.findall(value))
    if narrative_power and detail_power and not narrative_power.intersection(detail_power):
        conflicts.append({'field': 'POWER', 'structured': ', '.join(sorted(detail_power)),
                          'text': ', '.join(sorted(narrative_power)), 'reason': 'Противоречие описания и подробностей'})
    # Use prose and expanded specification sections only. Discard compact card
    # pairs (model, fuel, wheel, gearbox etc.) entirely.
    safe_lines = list(text_lines)
    extra_lines = []
    for key, value in detail.items():
        if key == 'Двигатель — подробности' and any(c['field'] == 'POWER' for c in conflicts):
            continue
        extra_lines.append(key.replace(' — подробности', '') + ': ' + value)
    description = safe_lines + (['Дополнительные технические сведения:'] + extra_lines if extra_lines else [])
    specs = dict(detail)
    # Only a single unambiguous wheel formula from narrative is safe to map.
    if len(narrative_wheels) == 1:
        specs['Колёсная формула'] = next(iter(narrative_wheels))
    return specs, description, list(dict.fromkeys(equip))[:60], conflicts


def public_specs(specs: dict[str, str], conflicts: list[dict]) -> dict[str, str]:
    """Remove conflicting detailed claims; primary table values stay available.

    Main values are withheld from native card tiles by safe_card_properties.
    """
    blocked = {c['field'] for c in conflicts}
    result = dict(specs)
    if blocked & {'ENGINE', 'MODEL_ENGINE', 'POWER'}:
        result.pop('Двигатель — подробности', None)
    return result


def safe_card_properties(item, props: dict) -> dict:
    if item.source != 'valmo':
        return props
    blocked = {x.get('field') for x in getattr(item, 'source_conflicts', [])}
    final = dict(props)
    if 'FORM_WHEEL' in blocked:
        final.pop('FORM_WHEEL', None)
        final.pop('FORM_WHEEL_TITLE', None)
    if blocked & {'ENGINE', 'MODEL_ENGINE', 'POWER'}:
        for name in ('ENGINE', 'MODEL_ENGINE', 'POWER', 'ENGINE_VOLUME'):
            final.pop(name, None)
    return final
