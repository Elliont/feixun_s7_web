"""斐讯 S7/S7P 帧编解码与记录解析。

协议全部经真机抓包逐字节验证，详见 04-S7-S7PE协议规格.md / 13-阻抗定义与算法契约.md

帧 = 整帧 XOR 0x55；明文结构：
    [0..3]        55 AA 00 LEN        LEN = 总长(32/48/64)
    [4..11]       产品码              S7 = 00 00 00 01 00 00 00 01
    [12..17]      源 MAC
    [18..23]      目标 MAC
    [24..25]      保留
    [26..27]      命令字（大端）
    [28..LEN-5]   载荷
    [LEN-4..-3]   保留（0）
    [LEN-2..-1]   CRC16/MODBUS（大端），覆盖明文 [2, LEN-4)
"""

from __future__ import annotations

from datetime import datetime

XOR_KEY = 0x55
MIN_FRAME = 32

CMD_LOGIN = 0x8000
CMD_LOGIN_ACK = 0xC000
CMD_REPORT = 0x8062
CMD_REPORT_ACK = 0xC062
CMD_RECORD = 0x8090
CMD_RECORD_ACK = 0xC090
CMD_PING = 0x8052
CMD_PING_ACK = 0xC052

# CS1258 无接触基线（0.1Ω）与人体合理区间（0.1Ω）
IMPEDANCE_INVALID_MIN, IMPEDANCE_INVALID_MAX = 1300, 1360
IMPEDANCE_MIN, IMPEDANCE_MAX = 1500, 30000
FOOT_CHANNEL_INDEX = 5  # 第 6 段 = 双脚


def xor(data: bytes) -> bytes:
    return bytes(b ^ XOR_KEY for b in data)


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def is_frame(plain: bytes) -> bool:
    return (
        len(plain) >= MIN_FRAME
        and plain[0] == 0x55
        and plain[1] == 0xAA
        and plain[2] == 0x00
        and MIN_FRAME <= plain[3] <= len(plain)
    )


def frame_is_valid(plain: bytes) -> bool:
    length = plain[3]
    if length < MIN_FRAME or len(plain) < length:
        return False
    want = (plain[length - 2] << 8) | plain[length - 1]
    return crc16_modbus(plain[2:length - 4]) == want


def cmd_of(plain: bytes) -> int:
    return (plain[26] << 8) | plain[27]


def mac_of(plain: bytes) -> str:
    return ":".join("%02X" % b for b in plain[12:18])


def peek_frame_length(buf: bytes) -> int:
    """从字节流前缀判断第一帧长度，-1 表示还不够。"""
    if len(buf) < 4:
        return -1
    p0, p1, p2, ln = buf[0] ^ XOR_KEY, buf[1] ^ XOR_KEY, buf[2] ^ XOR_KEY, buf[3] ^ XOR_KEY
    if p0 == 0x55 and p1 == 0xAA and p2 == 0x00 and ln >= MIN_FRAME:
        return ln
    return -1


def seal(plain: bytes) -> bytes:
    """明文 → 线上字节（补 CRC 后整帧 XOR）。"""
    plain = bytearray(plain)
    length = len(plain)
    crc = crc16_modbus(bytes(plain[2:length - 4]))
    plain[length - 4] = 0
    plain[length - 3] = 0
    plain[length - 2] = (crc >> 8) & 0xFF
    plain[length - 1] = crc & 0xFF
    return xor(bytes(plain))


def time_bytes(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    weekday = now.isoweekday() % 7  # 周日=0
    return bytes([
        (now.year - 2000) & 0xFF, now.month, now.day,
        now.hour, now.minute, now.second, weekday,
    ])


def build_login_ack(request_cipher: bytes, now: datetime | None = None) -> bytes:
    req = xor(request_cipher)
    p = bytearray(48)
    p[0], p[1], p[2], p[3] = 0x55, 0xAA, 0x00, 48
    p[4:12] = req[4:12]
    p[12:18] = b"\xff" * 6
    p[18:24] = req[12:18]
    p[26], p[27] = 0xC0, 0x00
    p[28:35] = time_bytes(now)
    p[35:39] = req[28:32]  # 回声
    p[39] = req[32]
    return seal(bytes(p))


def build_time_ack(request_cipher: bytes, cmd: int, length: int,
                   now: datetime | None = None) -> bytes:
    req = xor(request_cipher)
    p = bytearray(length)
    p[0], p[1], p[2], p[3] = 0x55, 0xAA, 0x00, length & 0xFF
    p[4:12] = req[4:12]
    p[12:18] = b"\xff" * 6
    p[18:24] = req[12:18]
    p[26] = (cmd >> 8) & 0xFF
    p[27] = cmd & 0xFF
    if length - 4 - 28 >= 7:
        p[28:35] = time_bytes(now)
    return seal(bytes(p))


def valid_impedance(v: int) -> bool:
    return IMPEDANCE_MIN <= v <= IMPEDANCE_MAX


def parse_record(plain: bytes) -> dict | None:
    """解析上报/历史帧，返回记录 dict；非数据帧返回 None。"""
    length = plain[3]
    cmd = cmd_of(plain)
    mac = mac_of(plain)

    if cmd == CMD_REPORT and length >= 60:
        weight = (plain[43] << 8) | plain[44]
        if weight <= 0:
            return None
        data_type = plain[45]
        imps = [((plain[46 + i * 2] << 8) | plain[47 + i * 2]) for i in range(6)]
        return {
            "cmd": "0x8062",
            "mac": mac,
            "weight_kg": round(weight / 100.0, 2),
            "weight_centi_kg": weight,
            "data_type": data_type,
            "impedances": imps,
            "measured_at": datetime.now().replace(microsecond=0),
            "source": "report",
        }

    if cmd == CMD_RECORD and length >= 60:
        year = 2000 + plain[30]
        month, day, hour, minute, second = plain[31], plain[32], plain[33], plain[34], plain[35]
        weight = (plain[37] << 8) | plain[38]
        data_type = plain[39]
        imps = [((plain[40 + i * 2] << 8) | plain[41 + i * 2]) for i in range(6)]
        try:
            measured = datetime(year, month, day, hour, minute, second)
            if abs((measured - datetime.now()).total_seconds()) > 86400:
                measured = datetime.now()
        except ValueError:
            measured = datetime.now()
        if weight <= 0:
            return None
        return {
            "cmd": "0x8090",
            "mac": mac,
            "weight_kg": round(weight / 100.0, 2),
            "weight_centi_kg": weight,
            "data_type": data_type,
            "impedances": imps,
            "measured_at": measured.replace(microsecond=0),
            "source": "history",
        }
    return None


def body_impedance(record: dict) -> float:
    """双足通道（第 6 段）优先，返回 Ω（float）；无效返回 0。"""
    imps = record.get("impedances") or []
    if len(imps) > FOOT_CHANNEL_INDEX and valid_impedance(imps[FOOT_CHANNEL_INDEX]):
        return imps[FOOT_CHANNEL_INDEX] / 10.0
    for v in imps:
        if valid_impedance(v):
            return v / 10.0
    return 0.0
