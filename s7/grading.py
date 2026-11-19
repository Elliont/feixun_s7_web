"""指标分级与展示规格。

每个指标给出：标签、数值、单位、等级（ok/warn/bad/low/info）、参考区间说明。
阈值参考：中国成人 BMI 标准、ACE 体脂分级、常见体成分秤参考区间。
"""

from __future__ import annotations

from . import bodycomp, fatstandards

OK = "ok"
WARN = "warn"
BAD = "bad"
LOW = "low"
INFO = "info"


def _tier(value: float, lo: float | None, hi: float | None,
          low_label: str = "偏低", high_label: str = "偏高") -> str:
    if lo is not None and value < lo:
        return LOW if low_label == "偏低" else BAD
    if hi is not None and value > hi:
        return WARN if high_label == "偏高" else BAD
    return OK


def bmi_level(bmi: float) -> tuple[str, str]:
    """中国成人标准。"""
    if bmi < 18.5:
        return LOW, "偏低"
    if bmi < 24:
        return OK, "正常"
    if bmi < 28:
        return WARN, "偏胖"
    return BAD, "肥胖"


def fat_level(bfr: float, male: bool, age: int,
              standard: str | None = None) -> tuple[str, str]:
    """体脂评价词，随「体脂判定标准」设置变化（ACE 5 档 / Gallagher 4 档 / 小米 4 档）。

    ACE 多出一个「运动员」档（6–13% 男 / 14–20% 女），按偏低色展示——
    它低于健康下限，但对运动人群是合理区间，所以用 low 而不是 warn。
    """
    grade = bodycomp.fat_grade(bfr, male, age, standard)
    level = {"偏低": LOW, "运动员": LOW, "正常": OK, "偏高": WARN, "肥胖": BAD}[grade]
    return level, grade


def water_level(tfr: float, male: bool) -> tuple[str, str]:
    # 区间按芯海 50kHz 公式的实际输出分布校准（该公式普遍比 Tanita 低 3–6 个点）
    lo, hi = (50.0, 62.0) if male else (45.0, 58.0)
    if tfr < lo:
        return WARN, "偏低"
    if tfr > hi:
        return INFO, "偏高"
    return OK, "正常"


def protein_level(pp: float) -> tuple[str, str]:
    if pp < 16:
        return WARN, "偏低"
    if pp > 20:
        return INFO, "偏高"
    return OK, "正常"


def smi_level(smm_kg: float, height_cm: float, male: bool) -> tuple[str, str]:
    """骨骼肌指数 SMM/身高²（亚洲肌少症工作组思路）。"""
    smi = smm_kg / ((height_cm / 100.0) ** 2)
    lo, hi = (7.0, 9.5) if male else (5.7, 8.0)
    if smi < lo:
        return WARN, "偏低"
    if smi > hi:
        return OK, "充足"
    return OK, "正常"


def visceral_level(level: float) -> tuple[str, str]:
    if level <= 9:
        return OK, "正常"
    if level < 15:
        return WARN, "偏高"
    return BAD, "过高"


def metabolic_age_level(body_age: int, age: int) -> tuple[str, str]:
    """代谢年龄只给数值，不出评价词（用户决议：卡面不显示「偏年轻」这类备注）。

    等级仍固定 OK，好让卡片沿用统一的等级着色与数据属性。
    """
    return OK, ""


def score_level(score: int) -> tuple[str, str]:
    if score >= 80:
        return OK, "优秀"
    if score >= 60:
        return OK, "良好"
    return WARN, "偏低"



def _fmt(v, digits=1) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int,)) and digits == 0:
        return str(v)
    return f"{v:.{digits}f}"


def cards(comp: dict, age: int, male: bool) -> list[dict]:
    """生成详情页的指标卡片列表。

    每张卡带三个文本位：
    - value      数值
    - verdict    紧跟在数值后的评价词（偏胖 / 正常 / 不足…），无评价则空
    - hint       问号弹层里的**计算方法**（不含当前判定，判定已上卡面）
    """
    out: list[dict] = []

    def add(key, label, value, unit, level, hint, verdict=""):
        out.append({"key": key, "label": label, "value": value, "unit": unit,
                    "level": level, "hint": hint, "verdict": verdict})

    if comp.get("bmi") is not None:
        lv, txt = bmi_level(comp["bmi"])
        add("bmi", "BMI", _fmt(comp["bmi"]), "", lv,
            "体重 ÷ 身高²。中国成人标准：<18.5 偏瘦，18.5–23.9 正常，"
            "24–27.9 偏胖，≥28 肥胖", txt)

    if not comp.get("has_body_fat"):
        return out

    lv, txt = fat_level(comp["body_fat_percent"], male, age)
    add("bfr", "体脂率", _fmt(comp["body_fat_percent"]), "%", lv,
        fatstandards.describe(male, age), txt)

    add("fat_mass", "脂肪量", _fmt(comp["fat_mass_kg"]), "kg", INFO,
        "体重 × 体脂率，即全身脂肪的绝对重量")

    add("lbm", "去脂体重", _fmt(comp["lbm_kg"]), "kg", OK,
        "体重 − 脂肪量，即肌肉、水分、蛋白质与骨骼等非脂肪部分的总和")

    lv, txt = smi_level(comp["skeletal_muscle_kg"], comp.get("height_cm", 0) or 0, male) \
        if comp.get("height_cm") else (OK, "正常")
    h2 = ((comp.get("height_cm") or 0) / 100.0) ** 2
    lo_i, hi_i = (7.0, 9.5) if male else (5.7, 8.0)
    smm_kg_range = (f"；按身高 {comp['height_cm']:.0f} cm 折算，约 "
                    f"{lo_i * h2:.1f}–{hi_i * h2:.1f} kg 为正常") if h2 else ""
    add("smm", "骨骼肌", _fmt(comp["skeletal_muscle_kg"]), "kg", lv,
        "全身骨骼肌的绝对重量（kg），由体重、身高、年龄与阻抗回归得出。"
        "等级不直接看重量，而看骨骼肌指数（骨骼肌 kg ÷ 身高 m²）："
        "男 7.0–9.5、女 5.7–8.0 为正常，低于下限算偏少，高于上限算充足" + smm_kg_range, txt)

    add("muscle", "肌肉量", _fmt(comp["muscle_kg"]), "kg", INFO,
        "由体重、身高、年龄与阻抗推算的肌肉总量，含肌肉中的水分与蛋白质")

    lv, txt = water_level(comp["water_percent"], male)
    add("water", "水分率", _fmt(comp["water_percent"]), "%", lv,
        f"由体重、身高、年龄与阻抗推算的全身水分占体重比例。"
        f"正常区间 {'50–62' if male else '45–58'}%", txt)

    lv, txt = protein_level(comp["protein_percent"])
    w_kg = comp.get("weight_kg") or 0.0
    p_range = (f"，按当前体重折算约 {0.16 * w_kg:.1f}–{0.20 * w_kg:.1f} kg 为正常"
               if w_kg else "")
    add("protein", "蛋白质", _fmt(comp["protein_kg"]), "kg", lv,
        "全身蛋白质的绝对重量（kg）＝ 肌肉量 − 全身水分。"
        "健康范围约占体重的 16–20%" + p_range, txt)

    lv, txt = visceral_level(comp["visceral_level"])
    add("vfr", "内脏脂肪", _fmt(comp["visceral_level"]), "级", lv,
        "由体重、身高、年龄与阻抗估算的内脏脂肪等级（0.5 级步长）。"
        "≤9 正常，10–14 偏高，≥15 过高", txt)

    lv, txt = metabolic_age_level(comp["metabolic_age"], age)
    add("bage", "代谢年龄", _fmt(comp["metabolic_age"], 0), "岁", lv,
        "由体重、身高、年龄与阻抗推算基础代谢，再折算成同龄人群中的等价年龄", txt)

    lv, txt = score_level(comp["body_score"])
    add("score", "身体评分", _fmt(comp["body_score"], 0), "分", lv,
        "体脂率、内脏脂肪、骨骼肌量与体重的加权评分，满分 100。"
        "≥80 优秀，60–79 良好，<60 偏低", txt)

    return out
