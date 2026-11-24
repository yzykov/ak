"""Published per-product RUB prices from GKGross and LiftNet.

Never scan recommendations, a category's minimum price, promotional discounts,
or monthly leasing payments. An unknown price remains unknown.
"""
from __future__ import annotations

import re
from bs4 import BeautifulSoup

PRICE = re.compile(r'(?<!\w)(?P<from>от\s+)?(?P<num>\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{4,10})(?:[.,]00)?\s*(?:₽|руб\.?|рублей)(?!\w)', re.I)
CONTEXT = re.compile(r'лизинг|плат[её]ж|скидк|аванс|экономия|сервис|доставк|стоимость ремонта|аренд|первоначальн|выкупн', re.I)
RANGE = (1000, 2_000_000_000)


def _amount(value: str) -> int:
    number = re.sub(r'[^0-9]', '', value)
    if not number or len(number) > 10:
        return 0
    amount = int(number)
    return amount if RANGE[0] <= amount <= RANGE[1] else 0


def parse_price(soup: BeautifulSoup, source: str) -> tuple[int, bool]:
    if source not in ('gkgross', 'liftnet'):
        return 0, False
    h1 = soup.select_one('h1')
    if h1 is None:
        return 0, False

    # Only scan a short section following the product H1. Price banners,
    # related products and category-wide price ranges are outside this scope.
    parts: list[str] = []
    char_count = 0
    from bs4 import NavigableString, Tag
    for node in h1.next_elements:
        if isinstance(node, Tag):
            if node.name in ('h2', 'h3') and parts:
                break
            if node.name in ('footer',):
                break
        if isinstance(node, NavigableString):
            value = ' '.join(str(node).split())
            if value:
                parts.append(value)
                char_count += len(value) + 1
        if char_count >= 1800:
            break
    text = ' '.join(parts)
    for match in PRICE.finditer(text):
        before = text[max(0, match.start() - 55):match.start()]
        # Drop leasing and promotional text but keep a simple "Цена от" label.
        if CONTEXT.search(before):
            continue
        amount = _amount(match.group('num'))
        if amount:
            return amount, bool(match.group('from'))

    # Product's own JSON-LD offer may have a numeric price (not a sale/discount).
    # This is a fallback, not a scan of all JSON-LD Offers on the page.
    from .sources import jsonld
    for product in jsonld(soup, 'Product'):
        title = str(product.get('name', '')).casefold()
        if title and title not in h1.get_text(' ', strip=True).casefold() and h1.get_text(' ', strip=True).casefold() not in title:
            continue
        offers = product.get('offers', [])
        if isinstance(offers, dict):
            offers = [offers]
        for offer in offers if isinstance(offers, list) else []:
            if not isinstance(offer, dict) or str(offer.get('priceCurrency', '')).upper() not in ('RUB', 'RUR'):
                continue
            raw = str(offer.get('price', '')).replace(' ', '').replace(',', '.')
            if re.fullmatch(r'\d+(?:\.00?)?', raw):
                amount = int(float(raw))
                if RANGE[0] <= amount <= RANGE[1]:
                    return amount, False
    return 0, False
