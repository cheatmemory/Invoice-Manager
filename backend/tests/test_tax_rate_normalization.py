"""税率归一化：带不带百分号是同一个税率。

回归的是「OCR 抽税率总是少一个 %，每次都得手动裁决这一项」那个缺陷。OCR 有
两条路径会漏掉百分号——正则里 ``%`` 是可选的、``_reconcile_totals`` 反推时
写的是 ``str(int(hit))``——漏掉之后与 LLM 给的「11%」字符串不等，被记成
conflict，于是一张识别得好好的票也要人工点一次。

判据是「归一化后不同才叫冲突」，所以这里同时钉住两件事：同一税率的不同写法
必须合并，真不同的税率不能被合并掉。
"""
import pytest

from app.services.invoice_service import _compare_and_resolve, build_item_rows
from app.services.ocr_service import normalize_tax_rate


@pytest.mark.parametrize("raw, expected", [
    ("11", "11%"),          # 正则路径：% 没读出来
    ("11%", "11%"),         # 已经带了，保持原样
    ("11％", "11%"),        # 全角百分号
    (" 6 % ", "6%"),        # OCR 把数字和百分号拆成两段
    ("17.0", "17%"),        # 反推路径写成 "17.0" 也要能收
    ("0.11", "11%"),        # 票面印 11%，写 0.11 是同一个数的另一种写法
    ("0", "0%"),
    ("免税", "免税"),        # 票面上这几格不印百分数，别去解析它
    ("不征税", "不征税"),
    ("***", "***"),
    (None, None),
    ("", None),
    ("   ", None),
])
def test_normalize_tax_rate(raw, expected):
    assert normalize_tax_rate(raw) == expected


def test_compare_treats_missing_percent_as_match():
    """OCR 的「11」与 LLM 的「11%」必须算一致，并且最终值取带 % 的那个。"""
    final, diffs = _compare_and_resolve(
        {"tax_rate": "11"}, {"tax_rate": "11%"}, has_llm=True)

    rate = next(d for d in diffs if d["field_name"] == "tax_rate")
    assert rate["source"] == "matched"
    assert rate["needs_review"] is False
    assert rate["final_value"] == "11%"
    assert final["tax_rate"] == "11%"


def test_compare_still_flags_a_real_difference():
    """归一化不能把真的分歧也抹平：6% 与 11% 仍然要报冲突。"""
    final, diffs = _compare_and_resolve(
        {"tax_rate": "6%"}, {"tax_rate": "11%"}, has_llm=True)

    rate = next(d for d in diffs if d["field_name"] == "tax_rate")
    assert rate["source"] == "conflict"
    assert rate["needs_review"] is True
    assert final["tax_rate"] is None


def test_item_tax_rate_is_normalized():
    """明细行的税率同样要带百分号 —— 它既来自 OCR，也来自用户在手改明细表。"""
    rows = build_item_rows([
        {"item_name": "停车服务费", "tax_rate": "6"},
        {"item_name": "打印装订费", "tax_rate": "免税"},
    ])
    assert rows[0]["tax_rate"] == "6%"
    assert rows[1]["tax_rate"] == "免税"
