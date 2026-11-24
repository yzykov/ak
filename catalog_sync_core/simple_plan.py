"""S3-backed simple sync planner: proven link -> keep; no link -> create.

No uncertain-match review gate. Explicitly mark legacy unlinked target cards for
possible deactivation, but never execute disappearance against incomplete crawls.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import re
from dataclasses import dataclass, field
from .matching import compare, same_base, strong_identity, candidate_score
from .models import Decision, Item


@dataclass
class SimplePlan:
    keep: list[Decision] = field(default_factory=list)
    update_price: list[Decision] = field(default_factory=list)
    create: list[Decision] = field(default_factory=list)
    activate: list[Decision] = field(default_factory=list)
    deactivate: list[Decision] = field(default_factory=list)
    protected: list[Decision] = field(default_factory=list)
    pending_category: list[Decision] = field(default_factory=list)
    blocked: list[Decision] = field(default_factory=list)
    link: list[Decision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    safe_to_write: bool = True

    def dump(self) -> dict:
        names = ('keep', 'create', 'activate', 'deactivate', 'protected',
                 'pending_category', 'blocked', 'link', 'update_price')
        return {'counts': {n: len(getattr(self, n)) for n in names},
                **{n: [d.to_dict() for d in getattr(self, n)] for n in names},
                'errors': self.errors, 'warnings': self.warnings,
                'safe_to_write': self.safe_to_write}


def protected_section(t: Item, taxonomy, codes: set[str]) -> bool:
    return any(taxonomy.is_descendant(t.section_id, section.id)
               for section in taxonomy.sections.values() if section.code.lower() in codes)


def build_simple_plan(source: list[Item], target: list[Item], taxonomy, links: dict, crawl: dict,
                      *, protected_codes=('kommercheskiy-transport', 'zapchasty')) -> SimplePlan:
    plan = SimplePlan()
    by_id = {t.bitrix_id: t for t in target}
    if len(by_id) != len(target):
        plan.errors.append('Duplicate Bitrix product IDs')
    src_keys = [(s.source, s.source_id) for s in source]
    if len(set(src_keys)) != len(src_keys):
        plan.errors.append('Duplicate supplier product IDs')
    by_xml: dict[str, Item] = {}
    for t in target:
        if t.xml_id:
            if t.xml_id in by_xml:
                plan.errors.append('Duplicate Bitrix XML_ID: ' + t.xml_id)
            by_xml[t.xml_id] = t
    if plan.errors:
        plan.safe_to_write = False
        return plan

    current = set(src_keys)
    present_targets: set[int] = set()
    links_by_target = defaultdict(list)
    for k, row in links.items():
        links_by_target[row['bitrix_id']].append(k)
    reserved_imports: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for s in source:
        key = (s.source, s.source_id)
        old = links.get(key)
        if old:
            t = by_id.get(old['bitrix_id'])
            if t is None:
                plan.blocked.append(Decision('blocked', source=s.source, source_id=s.source_id,
                                             title=s.title, source_url=s.url,
                                             element_id=old['bitrix_id'],
                                             reason='S3 содержит связь, но товара нет в Битрикс; не создавать дубль'))
                continue
            selected = t
            method = 'S3'
        else:
            xml_match = by_xml.get(f'{s.source}:{s.source_id}')
            if xml_match is not None:
                selected = xml_match
                method = 'XML_ID'
            else:
                # A card with a proven configuration is a candidate, not a merely
                # similar name. Allow >1 suppliers to link to same Bitrix card.
                possible = []
                for t in target:
                    if not same_base(s, t):
                        continue
                    ev = compare(s, t)
                    if strong_identity(s, t, ev):
                        possible.append((candidate_score(s, t, ev), t))
                possible.sort(key=lambda x: (x[0], x[1].bitrix_id), reverse=True)
                # If alternatives are close, do not guess. Create a fresh SKU.
                if len(possible) == 1 or (len(possible) > 1 and possible[0][0] - possible[1][0] >= 8):
                    selected = possible[0][1]
                    method = 'strong identity'
                else:
                    selected = None
                    method = ''
                    if possible:
                        plan.warnings.append(f'{s.source}: неоднозначные точные кандидаты для {s.title}; новая карточка')
        if selected is not None:
            present_targets.add(selected.bitrix_id)
            reserved_imports[selected.bitrix_id].append(key)
            if not old:
                plan.link.append(Decision('link', source=s.source, source_id=s.source_id,
                                          source_url=s.url, title=s.title, element_id=selected.bitrix_id,
                                          expected_name=selected.title,
                                          reason=f'Подтверждено через {method}'))
            d = Decision('keep' if selected.active else 'activate', source=s.source,
                         source_id=s.source_id, source_url=s.url, title=s.title,
                         element_id=selected.bitrix_id, expected_xml_id=selected.xml_id,
                         expected_name=selected.title, expected_active=selected.active,
                         reason=f'Постоянная связь: {method}')
            (plan.keep if selected.active else plan.activate).append(d)
            # The exact import owner alone may change price. Registry links to
            # manually created cards or other suppliers never confer ownership.
            if (s.source in ('gkgross', 'liftnet') and s.source_id
                    and selected.xml_id == f'{s.source}:{s.source_id}'
                    and type(s.price_rub) is int and 1000 <= s.price_rub <= 2_000_000_000):
                state = crawl.get(s.source, {})
                if not (state.get('complete') and state.get('count', 0) > 0 and state.get('finished', True)):
                    plan.warnings.append(f'{selected.bitrix_id}: цена пропущена — неполный обход {s.source}')
                elif not isinstance(selected.current_price, str) or not isinstance(selected.current_price_flag, str):
                    plan.errors.append(f'{selected.bitrix_id}: нет полей цены в list.php или неверная схема свойств; обновите API')
                    plan.safe_to_write = False
                elif (selected.current_price != '' and not re.fullmatch(r'\d+(?:\.\d+)?', selected.current_price)) or selected.current_price_flag not in ('', 'Y', 'N'):
                    plan.warnings.append(f'{selected.bitrix_id}: неизвестный формат текущей цены/признака; пропуск')
                elif (Decimal(selected.current_price or '0') != s.price_rub
                      or selected.current_price_flag != ('Y' if s.price_is_from else 'N')):
                    plan.update_price.append(Decision('update_price', source=s.source, source_id=s.source_id,
                        source_url=s.url, title=s.title, element_id=selected.bitrix_id,
                        section_id=selected.section_id, expected_xml_id=selected.xml_id,
                        expected_price=selected.current_price, expected_price_flag=selected.current_price_flag,
                        price_rub=s.price_rub, price_is_from=s.price_is_from,
                        reason='Изменение цены или признака от у исходного поставщика'))
            continue

        if not s.section_id or taxonomy.excluded(s.section_id):
            plan.pending_category.append(Decision('pending_category', source=s.source, source_id=s.source_id,
                                                  source_url=s.url, title=s.title,
                                                  reason=f'Уточнить категорию: {s.category or "(не указана)"}'))
        else:
            plan.create.append(Decision('create', source=s.source, source_id=s.source_id,
                                        source_url=s.url, title=s.title, section_id=s.section_id,
                                        reason='Не найдено доказанное соответствие; создать новую карточку'))

    # Deactivation is about the Bitrix CARD, not about a single supplier link.
    protected_codes = set(protected_codes)
    registered_providers = set(crawl)
    if not registered_providers:
        plan.errors.append('No source crawl status')
    for t in target:
        if not t.active or t.bitrix_id in present_targets or protected_section(t, taxonomy, protected_codes):
            continue
        old_links = links_by_target.get(t.bitrix_id, [])
        if old_links:
            missing_providers = sorted({provider for provider, _ in old_links if (provider, _) not in current})
            invalid = sorted({provider for provider, _ in old_links if provider not in crawl})
            complete = not invalid and all(crawl[x].get('complete') and crawl[x].get('count', 0) > 0
                                           for x in missing_providers)
            # If the target has any S3 link that is currently present in sources,
            # but did not resolve (e.g. broken Bitrix target), never deactivate.
            all_missing = all(key not in current for key in old_links)
            if all_missing and complete and missing_providers:
                plan.deactivate.append(Decision('deactivate', title=t.title, element_id=t.bitrix_id,
                                                expected_xml_id=t.xml_id, expected_active=True,
                                                reason='Все привязанные товары исчезли из подтверждённо полных каталогов'))
            else:
                plan.protected.append(Decision('protected', title=t.title, element_id=t.bitrix_id,
                                               reason='Есть связь S3, но отсутствие не подтверждено полным обходом'))
        else:
            plan.deactivate.append(Decision('deactivate_candidate', title=t.title, element_id=t.bitrix_id,
                                            expected_xml_id=t.xml_id, expected_active=True,
                                            reason='Старая карточка без подтверждённого матча; кандидат на отключение'))
    return plan
