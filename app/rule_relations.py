"""召回规则关系分析:在上线前找出可能命中同一合法 GS1 扫描串的规则对。

规则的命中集合是四组约束的合取:GTIN(固定值)、批号 AI 10(不约束或固定值)、
序列号 AI 21(不约束或固定值)、日期 AI 17(不约束或合法日期全集上的闭区间)。

AI 17 只可能是 2000-01-01~2099-12-31 之间的合法日期,因此:

- 规则所附日期区间与这段全集无交集时,该规则不可能命中任何合法扫描;
- 其余规则两两比较,仅返回命中集合有交集的规则对,关系为等价 / 左包含右 /
  右包含左 / 部分相交;每对附一条同时命中两者的合法扫描串作为凭证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .gs1 import FNC1
from .models import Rule

MIN_DATE = date(2000, 1, 1)  # AI 17 可表示的最早日期(000101)
MAX_DATE = date(2099, 12, 31)  # AI 17 可表示的最晚日期(991231)

# 关系取值
EQUIVALENT = "equivalent"  # 集合相同
LEFT_CONTAINS_RIGHT = "left_contains_right"  # 右规则的命中集合是左规则的真子集
RIGHT_CONTAINS_LEFT = "right_contains_left"  # 左规则的命中集合是右规则的真子集
PARTIAL_OVERLAP = "partial_overlap"  # 有交集但互不包含


@dataclass(frozen=True)
class _Shape:
    """一条规则在合法扫描全集上的命中集合形状(日期区间已裁剪到可表示范围)。"""

    gtin: str
    batch: str | None
    serial: str | None
    has_date: bool
    date_lo: date
    date_hi: date


def _shape(rule: Rule) -> _Shape | None:
    """把规则投影成集合形状;不可能命中任何合法扫描时返回 None。"""
    if rule.date_from is not None and rule.date_from > MAX_DATE:
        return None
    if rule.date_to is not None and rule.date_to < MIN_DATE:
        return None
    if rule.date_from is not None or rule.date_to is not None:
        lo = max(rule.date_from or MIN_DATE, MIN_DATE)
        hi = min(rule.date_to or MAX_DATE, MAX_DATE)
        return _Shape(rule.gtin, rule.batch, rule.serial, True, lo, hi)
    return _Shape(rule.gtin, rule.batch, rule.serial, False, MIN_DATE, MAX_DATE)


def _intersect(a: _Shape, b: _Shape) -> bool:
    """两个命中集合是否有交集。"""
    if a.gtin != b.gtin:
        return False
    if a.batch is not None and b.batch is not None and a.batch != b.batch:
        return False
    if a.serial is not None and b.serial is not None and a.serial != b.serial:
        return False
    if a.has_date and b.has_date and (a.date_lo > b.date_hi or b.date_lo > a.date_hi):
        return False
    return True


def _exact_subset(small: _Shape, large: _Shape) -> bool:
    """small 的命中集合是否为 large 的子集(允许相等)。"""
    if small.gtin != large.gtin:
        return False
    if large.batch is not None and small.batch != large.batch:
        return False
    if large.serial is not None and small.serial != large.serial:
        return False
    if large.has_date:
        if not small.has_date:
            return False
        if small.date_lo < large.date_lo or small.date_hi > large.date_hi:
            return False
    return True


def _witness_scan(a: _Shape, b: _Shape) -> str:
    """构造一条同时命中两条(有交集)规则的合法 GS1 扫描串。

    批号 / 序列号只在至少一条规则约束时出现,取约束值(交集内唯一值);
    日期在交集中取最早一天;字段顺序为 01、17、10、21,变长字段后仍有
    字段时以 FNC1 分隔。
    """
    parts = ["01" + a.gtin]
    if a.has_date or b.has_date:
        # 双方都约束日期取交集最早一天;仅一方约束时取该区间最早可行日
        lower_bounds = [s.date_lo for s in (a, b) if s.has_date]
        d = max(lower_bounds)
        parts.append("17" + f"{d.year - 2000:02d}{d.month:02d}{d.day:02d}")
    batch = a.batch if a.batch is not None else b.batch
    serial = a.serial if a.serial is not None else b.serial
    if batch is not None:
        parts.append("10" + batch + (FNC1 if serial is not None else ""))
    if serial is not None:
        parts.append("21" + serial)
    return "".join(parts)


def analyze_rule_relations(
    rules: list[Rule],
) -> tuple[list[str], list[tuple[int, int, str, str]]]:
    """返回(不可能命中任何合法扫描的规则 ID 列表, 有交集的规则对列表)。

    不可能的规则 ID 按输入顺序排列;规则对按 (左下标, 右下标) 排列,
    每项为 (left_index, right_index, 关系, 凭证扫描串)。
    """
    shapes = [_shape(rule) for rule in rules]
    impossible = [rule.id for rule, shape in zip(rules, shapes) if shape is None]

    pairs: list[tuple[int, int, str, str]] = []
    n = len(rules)
    for i in range(n):
        left = shapes[i]
        if left is None:
            continue
        for j in range(i + 1, n):
            right = shapes[j]
            if right is None or not _intersect(left, right):
                continue
            left_in_right = _exact_subset(left, right)
            right_in_left = _exact_subset(right, left)
            if left_in_right and right_in_left:
                relation = EQUIVALENT
            elif right_in_left:
                relation = LEFT_CONTAINS_RIGHT
            elif left_in_right:
                relation = RIGHT_CONTAINS_LEFT
            else:
                relation = PARTIAL_OVERLAP
            pairs.append((i, j, relation, _witness_scan(left, right)))
    return impossible, pairs
