"""体脂率判定标准。

卡片上的体脂评价词、问号弹层里的阈值、最佳体脂区间，统一从这里取；
标准本身可在「系统设置 → 计算公式 → 体脂判定标准」切换，切换后全站生效。

三套标准（各自保留原本的档位结构，不强行统一）：

- ``ace``       5 档，**不分年龄**。美国运动委员会（ACE）个人训练手册的体脂参考表：
                必需脂肪 2–5 / 运动员 6–13 / 健康 14–17 / 可接受 18–24 / 肥胖 ≥25（男），
                女 10–13 / 14–20 / 21–24 / 25–31 / ≥32。
                页面上的评价词：偏低（必需脂肪）/ 运动员 / 正常（健康）/ 偏高（可接受）/ 肥胖。
- ``gallagher`` 4 档，**按性别 + 年龄段**（20–39 / 40–59 / 60–79）。
                Gallagher et al. 2000, Am J Clin Nutr 71:1494（NHANES III 四室模型）。
- ``xiaomi``    4 档，**按性别 + 年龄段**（0–11 / 12–13 / 14–15 / 16–17 / 18–39 / 40–59 / 60+）。
                取自 bodymiscale（dckiller51）`metrics/scale.py` 的 `_FAT_SCALES`：
                [very_low, low, normal, high] 四阈值，low 以下算偏低、normal 以上算偏高、
                high 以上算肥胖。这是小米体脂秤生态里通用的一套档位，不是小米官方文档。

改变标准只会改变「评价词与阈值」，不会改变体脂率数值本身——数值永远来自
bodycomp.py 的芯海 50kHz 公式。
"""

from __future__ import annotations

DEFAULT = "ace"

STANDARDS: dict[str, dict] = {
    "ace": {
        "name": "ACE",
        "note": "5 档，不分年龄",
        "source": "ACE Personal Trainer Manual 体脂率参考表",
    },
    "gallagher": {
        "name": "Gallagher",
        "note": "4 档，按年龄段和性别",
        "source": "Gallagher et al. 2000, Am J Clin Nutr 71(6):1494",
    },
    "xiaomi": {
        "name": "小米",
        "note": "4 档，按年龄段和性别",
        "source": "bodymiscale metrics/scale.py _FAT_SCALES（小米体脂秤生态通用档位）",
    },
}

# ---- ACE：5 档，不分年龄 -------------------------------------------------
# 边界取自 ACE 原表 5 段：必需 2–5 / 运动员 6–13 / 健康 14–17 / 可接受 18–24 / 肥胖 ≥25
_ACE_EDGES = {True: (6.0, 14.0, 18.0, 25.0), False: (14.0, 21.0, 25.0, 32.0)}
_ACE_LABELS = ("偏低", "运动员", "正常", "偏高", "肥胖")

# ---- Gallagher：4 档，3 个年龄段 -----------------------------------------
_GA_EDGES = {
    True: ((8.0, 20.0, 25.0), (11.0, 22.0, 28.0), (13.0, 25.0, 30.0)),
    False: ((21.0, 33.0, 39.0), (23.0, 34.0, 40.0), (24.0, 36.0, 42.0)),
}
_GA_BANDS = ("20–39 岁", "40–59 岁", "60–79 岁")

# ---- 小米（bodymiscale）：4 档，7 个年龄段 --------------------------------
# (age_min, age_max, female[very_low, low, normal, high], male[...])
_XM_ROWS = (
    (0, 12, [12.0, 21.0, 30.0, 34.0], [7.0, 16.0, 25.0, 30.0]),
    (12, 14, [15.0, 24.0, 33.0, 37.0], [7.0, 16.0, 25.0, 30.0]),
    (14, 16, [18.0, 27.0, 36.0, 40.0], [7.0, 16.0, 25.0, 30.0]),
    (16, 18, [20.0, 28.0, 37.0, 41.0], [7.0, 16.0, 25.0, 30.0]),
    (18, 40, [21.0, 28.0, 35.0, 40.0], [11.0, 17.0, 22.0, 27.0]),
    (40, 60, [22.0, 29.0, 36.0, 41.0], [12.0, 18.0, 23.0, 28.0]),
    (60, 200, [23.0, 30.0, 37.0, 42.0], [14.0, 20.0, 25.0, 30.0]),
)

_4_LABELS = ("偏低", "正常", "偏高", "肥胖")

_cache: str | None = None


def current() -> str:
    """当前生效的标准 key（读 settings.grading，非法值回退默认）。"""
    global _cache
    if _cache is None:
        from . import db
        raw = (db.get_setting("grading", DEFAULT) or "").strip().lower()
        _cache = raw if raw in STANDARDS else DEFAULT
    return _cache


def invalidate() -> None:
    """设置保存后调用，丢掉进程内缓存。"""
    global _cache
    _cache = None


def choices() -> list[dict]:
    return [{"value": k, "label": v["name"], "note": v["note"],
             "source": v["source"]} for k, v in STANDARDS.items()]


def _xm_row(age: int):
    for row in _XM_ROWS:
        if row[0] <= age < row[1]:
            return row
    return _XM_ROWS[-1]


def _ga_index(age: int) -> int:
    return 0 if age < 40 else (1 if age < 60 else 2)


def bands(standard: str | None, male: bool, age: int) -> tuple[tuple[float, ...], tuple[str, ...]]:
    """返回 (边界, 档位名)。

    边界比档位名少一个：低于边界[0] 取第 0 档，低于边界[i] 取第 i 档，否则最后一档。
    """
    key = standard or current()
    if key not in STANDARDS:
        key = DEFAULT
    if key == "ace":
        return _ACE_EDGES[bool(male)], _ACE_LABELS
    if key == "xiaomi":
        row = _xm_row(age)
        vals = row[3] if male else row[2]
        return (vals[1], vals[2], vals[3]), _4_LABELS
    return _GA_EDGES[bool(male)][_ga_index(age)], _4_LABELS


def grade(bfr: float, male: bool, age: int, standard: str | None = None) -> str:
    """体脂率 → 评价词（档位数随标准而定：ACE 5 档，其余 4 档）。"""
    edges, labels = bands(standard, male, age)
    for edge, label in zip(edges, labels):
        if bfr < edge:
            return label
    return labels[-1]


def best_range(male: bool, age: int,
               standard: str | None = None) -> tuple[float, float]:
    """最佳体脂区间 = 当前标准里「正常」那一档（整数百分比闭区间）。"""
    edges, labels = bands(standard, male, age)
    idx = labels.index("正常")
    lo = edges[idx - 1] if idx > 0 else 0.0
    hi = edges[idx] - 1 if idx < len(edges) else 999
    return lo, hi


def age_band_label(standard: str | None, age: int) -> str:
    """当前标准下这个人落在哪个年龄段（ACE 不分年龄）。"""
    key = standard or current()
    if key == "ace":
        return "不分年龄"
    if key == "xiaomi":
        row = _xm_row(age)
        return f"{row[0]}–{min(row[1], 200) - 1} 岁" if row[1] < 200 else "60 岁以上"
    return _GA_BANDS[_ga_index(age)]


def _fmt(v: float) -> str:
    return f"{v:g}"


def ranges_text(standard: str, male: bool, age: int) -> str:
    """一句话阈值描述，例如「6% 以下偏低，6–13% 运动员，14–17% 正常…」。"""
    edges, labels = bands(standard, male, age)
    parts = [f"{_fmt(edges[0])}% 以下{labels[0]}"]
    for i in range(1, len(edges)):
        parts.append(f"{_fmt(edges[i - 1])}–{_fmt(edges[i] - 1)}% {labels[i]}")
    parts.append(f"{_fmt(edges[-1])}% 及以上{labels[-1]}")
    return "，".join(parts)




def describe(male: bool, age: int, standard: str | None = None) -> str:
    """指标卡问号弹层：一行说清当前标准与此人的阈值。

    示例：当前标准：小米（40–59 岁），18% 以下偏低，18–22% 正常，
    23–27% 偏高，28% 及以上肥胖。
    """
    key = standard or current()
    if key not in STANDARDS:
        key = DEFAULT
    return (f"当前标准：{STANDARDS[key]['name']}（{age_band_label(key, age)}），"
            f"{ranges_text(key, male, age)}。")


def settings_tip() -> str:
    """设置页问号弹层：三套标准的阈值摘要。"""
    def row(male: bool) -> str:
        return ranges_text("ace", male, 30)

    gal = "；".join(
        f"{'男' if m else '女'} "
        + "，".join(f"{_GA_BANDS[i]} {_fmt(_GA_EDGES[m][i][0])}–"
                    f"{_fmt(_GA_EDGES[m][i][1] - 1)}%"
                    for i in range(3))
        for m in (True, False))
    xm = "；".join(
        f"{'男' if m else '女'} 18–39 岁 {_fmt(_XM_ROWS[4][3 if m else 2][1])}–"
        f"{_fmt(_XM_ROWS[4][3 if m else 2][2] - 1)}%"
        for m in (True, False))
    return (f"ACE（5 档，不分年龄）：男 {row(True)}；女 {row(False)}。"
            f"Gallagher（4 档，分年龄段）：{gal} 为正常，超出即偏高，再上一档为肥胖。"
            f"小米（4 档，分年龄段，取自 bodymiscale 的 _FAT_SCALES）：{xm} 为正常"
            f"（其余年龄段略有不同，点各指标卡上的问号可看该成员的阈值）。"
            f"小米官方没有公开体脂分档阈值，这套表是小米体脂秤生态通用档位。"
            f"切换后全站的体脂评价词与最佳区间立即跟着变。")
