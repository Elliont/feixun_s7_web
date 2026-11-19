"""体成分计算 —— 芯海 50kHz 官方公式（反汇编 + 800 组实机采样验证，误差 ≤1e-5）。

来源：14-芯海算法公式与对比.md
输入：身高 cm、性别、出生年（推算年龄）、体重 kg、阻抗 Ω（整数，双脚通道）
注意：阻抗必须是 **整数 Ω**（帧里 0.1Ω，需 /10 取整）
"""

from __future__ import annotations

from datetime import date

from . import fatstandards

def _z(R: int, w: float) -> int:
    """阻抗归一化指数。"""
    return int((10 * R - 1000 - 4 * w) / 6)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round_half(v: float) -> float:
    """四舍五入到 0.5（芯海 VFR 的取整方式）。"""
    i = int(v)
    return i + 0.5 if (v - i) >= 0.5 else float(i)


def compute(weight_kg: float, height_cm: float, age: int, male: bool,
            impedance_ohm: float) -> dict:
    """返回体成分字典；阻抗无效时返回仅体重类指标。"""
    if weight_kg <= 0 or height_cm <= 0 or age <= 0:
        return {}

    w, h, a = float(weight_kg), float(height_cm), float(age)
    bmi = w / ((h / 100.0) ** 2)
    ideal = 0.7 * (h - 80) if male else 0.6 * (h - 70)
    out = {
        "bmi": round(bmi, 1),
        "ideal_weight_kg": round(ideal, 1),
        "obesity_degree": round((w - ideal) / ideal * 100.0, 1),
        "has_body_fat": False,
    }

    R = int(round(impedance_ohm))
    if R <= 0:
        return out

    Z = _z(R, w)

    # ---- 脂肪 ----
    if male:
        fm = 0.6216 * w - 0.3315 * h + 0.0183 * a + 0.0085 * Z + 22.554
    else:
        fm = 0.7509 * w - 0.3332 * h + 0.0196 * a + 0.0072 * Z + 22.7193
    bfr = _clamp(fm / w * 100.0, 5.0, 45.0)
    fat_mass = w * bfr / 100.0
    lbm = w - fat_mass

    # ---- 基础代谢 ----
    if male:
        bmr = 13.1523 * w + 7.5037 * h - 4.3376 * a - 0.3486 * Z - 311.775
    else:
        bmr = 9.9474 * w + 7.5432 * h - 3.4382 * a - 0.3090 * Z - 288.282

    # ---- 骨骼肌 ----
    smm_raw = 0.1745 * w + 0.2573 * h - 0.0161 * a - 0.0170 * Z
    smm = _clamp(smm_raw - (17.7896 if male else 20.2165), 20.0, 60.0)

    # ---- 水分 ----
    if male:
        tf = 0.3758 * w + 0.0939 * h - 0.0032 * a - 0.006925 * Z + 0.096999
    else:
        tf = 0.2973 * w + 0.0877 * h + 0.0128 * a - 0.006030 * Z + 0.517502
    tfr = tf / w * 100.0

    # ---- 肌肉（派健康显示的"肌肉"）----
    if male:
        slm = 0.3894 * w + 0.2867 * h - 0.0408 * a - 0.01235 * Z - 15.7665
    else:
        slm = 0.1934 * w + 0.3186 * h - 0.0206 * a - 0.01320 * Z - 16.4556
    if bfr >= 45.0:
        slm = 0.95 * w - 4
    elif bfr <= 5.0:
        slm = 0.95 * w - 1
    slm = _clamp(slm, 20.0, 70.0)

    protein = slm - tf
    bone = _clamp(lbm - slm, 1.0, 4.0)
    # Z 在阻抗相对体重偏低时会落到 0 或负数（如 R=150Ω/w≥125kg），
    # 直接除会抛 ZeroDivisionError 拖垮所有渲染页；此处按 0 兜底。
    inf = (tf - 0.06 * w - 0.07 * h * h / Z - 3.69) if Z > 0 else 0.0

    # ---- 内脏等级（0.5 步长）----
    if male:
        vfr_raw = 0.42 * w - 0.2675 * h + 0.1462 * a + 0.0123 * Z + 13.9871
    else:
        vfr_raw = 0.2628 * w - 0.1651 * h + 0.0649 * a + 0.0024 * Z + 12.3445
    vfr = _clamp(_round_half(vfr_raw), 1.0, 59.0)

    # ---- 代谢年龄 ----
    if male:
        ba = 0.9161 * w - 0.7471 * h + 0.4184 * a + 0.0517 * Z + 54.2267
    else:
        ba = 1.5784 * w - 1.1165 * h + 0.4615 * a + 0.0415 * Z + 83.2548
    body_age = int(_clamp(round(ba), 18, 80))

    out.update({
        "has_body_fat": True,
        "impedance_used_ohm": R,
        "z_index": Z,
        "body_fat_percent": round(bfr, 1),
        "fat_mass_kg": round(fat_mass, 2),
        "lbm_kg": round(lbm, 2),
        "muscle_kg": round(slm, 2),
        "muscle_percent": round(slm / w * 100.0, 1),
        "skeletal_muscle_kg": round(smm, 2),
        "bone_kg": round(bone, 2),
        "water_percent": round(tfr, 1),
        "water_kg": round(tf, 2),
        "protein_kg": round(protein, 2),
        "protein_percent": round(protein / w * 100.0, 1),
        "bmr_kcal": int(round(bmr)),
        "visceral_fat": round(inf, 1),
        "visceral_level": round(vfr, 1),
        "metabolic_age": body_age,
        "body_score": _body_score(bfr, vfr, smm, w, male, bmi),
        "body_type": _body_type(bfr, smm, w, male),
    })
    return out


# ---------------------------------------------------------------- 判定

def age_of(birth_year: int, today: date | None = None) -> int:
    today = today or date.today()
    return max(1, min(120, today.year - int(birth_year)))


def age_of_date(birth_date: str | None, birth_year: int | None = None,
                today: date | None = None) -> int:
    """按出生日期算周岁；无日期时回退到年份估算。"""
    today = today or date.today()
    if birth_date:
        try:
            y, m, d = (int(x) for x in birth_date.split("-")[:3])
            age = today.year - y - ((today.month, today.day) < (m, d))
            return max(1, min(120, age))
        except (ValueError, TypeError):
            pass
    if birth_year:
        return age_of(birth_year, today)
    return 30


def fat_grade(bfr: float, male: bool, age: int, standard: str | None = None) -> str:
    """体脂 4 档：偏低 / 正常 / 偏高 / 肥胖，阈值取决于当前判定标准。

    标准定义在 s7/fatstandards.py：ACE（不分年龄）/ Gallagher（按性别 + 年龄段）。
    """
    return fatstandards.grade(bfr, male, age, standard)


def best_fat_range(male: bool, age: int,
                   standard: str | None = None) -> tuple[float, float]:
    """最佳体脂区间（当前标准下的「正常」档）。"""
    return fatstandards.best_range(male, age, standard)


def _body_score(bfr: float, vfr: float, smm: float, w: float,
                male: bool, bmi: float = 22.0) -> int:
    """身体评分 40~98（相对值；芯海原版为多项式回归）。

    权重设计：体脂偏离理想值最重，其次 BMI 与内脏脂肪；
    肌肉量只做轻微修正，避免"胖但肌肉多"被评成优秀。
    """
    ideal = 18.0 if male else 26.0
    score = 100.0
    score -= max(0.0, bfr - ideal) * 2.2      # 体脂超标
    score -= max(0.0, ideal - bfr) * 1.5      # 体脂过低
    score -= max(0.0, vfr - 9.0) * 0.8        # 内脏脂肪
    score -= abs(bmi - 22.0) * 0.8            # BMI 偏离
    score -= abs(smm / w - 0.40) * 20         # 肌肉占比轻微修正
    return int(_clamp(round(score), 40, 98))


_BODY_TYPES = ["偏瘦", "偏瘦肌肉型", "肌肉型", "肥胖脂肪型", "脂肪肌肉型",
               "肌肉脂肪型", "缺乏运动型", "标准", "标准肌肉型"]


def _body_type(bfr: float, smm: float, w: float, male: bool) -> str:
    fat_hi = bfr >= (25 if male else 32)
    fat_lo = bfr <= (10 if male else 20)
    mus_hi = smm / w >= 0.42
    mus_lo = smm / w <= 0.34
    if fat_hi and mus_hi:
        return "脂肪肌肉型"
    if fat_hi and mus_lo:
        return "缺乏运动型"
    if fat_hi:
        return "肥胖脂肪型"
    if fat_lo and mus_hi:
        return "偏瘦肌肉型"
    if mus_hi:
        return "标准肌肉型"
    if fat_lo:
        return "偏瘦"
    return "标准"


def build(weight_kg: float, height_cm: float, birth_year: int | None, sex: str,
          impedance_ohm: float, birth_date: str | None = None) -> dict:
    """对外统一入口：补上年龄、分级、最佳区间等展示字段。"""
    male = (sex == "male")
    age = age_of_date(birth_date, birth_year)
    comp = compute(weight_kg, height_cm, age, male, impedance_ohm)
    if not comp:
        return {}
    out = dict(comp)
    out["weight_kg"] = round(float(weight_kg), 2)
    out["age"] = age
    if comp.get("has_body_fat"):
        out["fat_grade"] = fat_grade(comp["body_fat_percent"], male, age)
        lo, hi = best_fat_range(male, age)
        out["best_fat_min"] = lo
        out["best_fat_max"] = hi
    return out
