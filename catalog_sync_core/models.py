from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Item:
    source: str
    url: str
    title: str
    brand: str = ''
    model: str = ''
    modification: str = ''
    category: str = ''
    category_path: list[str] = field(default_factory=list)
    section_id: int = 0
    section_ids: list[int] = field(default_factory=list)
    section_match_confidence: float = 0
    specs: dict[str, str] = field(default_factory=dict)
    equipment: list[str] = field(default_factory=list)
    description: str = ''
    source_conflicts: list[dict] = field(default_factory=list)
    price_rub: int = 0
    price_is_from: bool = False
    images: list[str] = field(default_factory=list)
    bitrix_id: int = 0
    xml_id: str = ''
    active: bool = False
    preview_picture: int = 0
    detail_picture: int = 0
    gallery_count: int = 0
    source_id: str = ''
    # Raw target properties; None means unavailable, empty string means unset.
    current_price: str | None = None
    current_price_flag: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict) -> Item:
        return cls(**row)


@dataclass(frozen=True)
class Section:
    id: int
    name: str
    code: str
    parent_id: int = 0
    active: bool = True


@dataclass
class Decision:
    action: str
    source: str = ''
    source_id: str = ''
    source_url: str = ''
    title: str = ''
    element_id: int = 0
    section_id: int = 0
    reason: str = ''
    score: float = 0
    expected_xml_id: str = ''
    expected_name: str = ''
    expected_active: bool | None = None
    expected_price: str | None = None
    expected_price_flag: str | None = None
    price_rub: int = 0
    price_is_from: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
