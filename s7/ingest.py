"""采集服务：扮演原厂云，接收秤上传并落库（含归属）。

独立进程，崩溃/重启不影响 Web；Web 崩溃也不影响收数。
"""

from __future__ import annotations

import logging
import socket
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor

from . import attribution, db, protocol

log = logging.getLogger("s7.ingest")

_LOCK = threading.Lock()
_MAX_BUF = 4096          # 正常帧最大 64B；远超此值说明流已错位或遭灌水，丢弃重同步
# 通知走独立线程池：pushplus 超时可达 16s，绝不能占用采集线程。
_NOTIFY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="s7-notify")


def _notify_async(member_id, state, weight_kg, measured_at_iso: str) -> None:
    """后台发送称重通知；失败只记日志，不影响采集。"""
    def _run() -> None:
        try:
            from . import notify
            who = None
            if member_id:
                who = next((m["name"] for m in db.active_members()
                            if m["id"] == member_id), None)
            events = ["measure.any"]
            if member_id:
                events.append(f"measure.member.{member_id}")
            if state == attribution.UNASSIGNED:
                events.append("measure.unassigned")
            notify.send("measure", {
                "title": (f"{who} 称重 {weight_kg:.1f} kg" if who
                          else f"有称重待认领 {weight_kg:.1f} kg"),
                "member": who,
                "weight_kg": weight_kg,
                "measured_at": measured_at_iso,
            }, aliases=events)
        except Exception:
            log.exception("异步通知失败")
    _NOTIFY_POOL.submit(_run)


def _event(mac: str, kind: str) -> None:
    try:
        db.execute("INSERT INTO device_events(mac,kind,at) VALUES(?,?,?)",
                   (mac, kind, db.now_iso()))
    except Exception:  # 事件记录失败不能影响主流程
        log.exception("event insert failed")


def _save(record: dict) -> None:
    """落库 + 归属（幂等）。"""
    weight_kg = record["weight_kg"]
    imp = protocol.body_impedance(record)
    imps = ",".join(str(v) for v in (record.get("impedances") or []))

    with _LOCK:
        members = db.active_members()
        refs = attribution.reference_weights()
        member_id, state, score = attribution.attribute(
            weight_kg, members, refs,
            device_mac=record["mac"], bindings=db.device_bindings())

        try:
            db.execute(
                """INSERT INTO measurements(
                       member_id, device_mac, measured_at, weight_centi_kg,
                       impedances, impedance_used_ohm, data_type, source,
                       match_state, match_score, raw_frame_hex, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (member_id, record["mac"], record["measured_at"].isoformat(),
                 record["weight_centi_kg"], imps, imp,
                 # 写帧里的真实 data_type，不再用「有阻抗=1」的猜测值覆盖
                 record.get("data_type", 1 if imp > 0 else 0),
                 record["source"], state, score,
                 record.get("raw"), db.now_iso()))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                log.info("跳过重复记录 %s %s", record["mac"], record["measured_at"])
                return
            raise

    log.info("入库 %.2fkg 阻抗%s 归属=%s(%s)",
             weight_kg, f"{imp:.1f}Ω" if imp else "无", member_id or "-", state)

    # 通知（异步，见 _notify_async）：任何称重 / 指定成员称重 / 无法确定归属，任一命中即发送
    try:
        _notify_async(member_id, state, weight_kg, record["measured_at"].isoformat())
    except Exception:
        # 入库已经提交，通知队列异常不能阻止向秤确认成功。
        log.exception("提交通知失败")


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(120)
        peer = "%s:%s" % self.client_address
        log.info("连接 %s", peer)
        buf = b""
        mac_seen = ""

        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    log.info("超时断开 %s", peer)
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_BUF:
                    log.warning("%s 缓冲区超限(%d)，丢弃重同步", peer, len(buf))
                    buf = buf[-2:]       # 只留末尾 2 字节，等待后续拼出同步头

                while True:
                    length = protocol.peek_frame_length(buf)
                    if length < 0:
                        # 帧头对不上：向后找下一个同步头，丢弃前面的垃圾字节，
                        # 否则 buf 只增不减、这条连接永久解不出帧。
                        if len(buf) < 4:
                            break        # 字节还不够，等下一次 recv
                        skip = protocol.resync_skip(buf)
                        if skip > 0:
                            buf = buf[skip:]
                            continue
                        break
                    if len(buf) < length:
                        break            # 帧还没收全，等下一次 recv
                    raw, buf = buf[:length], buf[length:]
                    self._dispatch(sock, raw, peer)
                    mac_seen = mac_seen or self._mac

        except ConnectionResetError:
            pass
        except Exception:
            log.exception("处理 %s 时出错", peer)
        finally:
            if mac_seen:
                _event(mac_seen, "disconnect")
            log.info("断开 %s", peer)

    _mac = ""

    def _dispatch(self, sock: socket.socket, raw: bytes, peer: str) -> None:
        plain = protocol.xor(raw)
        if not protocol.is_frame(plain) or not protocol.frame_is_valid(plain):
            log.warning("%s 无效帧 %s", peer, raw[:8].hex())
            return

        cmd = protocol.cmd_of(plain)
        self._mac = protocol.mac_of(plain)

        if cmd == protocol.CMD_LOGIN:
            sock.sendall(protocol.build_login_ack(raw))
            _event(self._mac, "login")
            log.info("登录 %s", self._mac)
            return

        if cmd == protocol.CMD_PING:
            sock.sendall(protocol.build_time_ack(raw, protocol.CMD_PING_ACK, 32))
            _event(self._mac, "heartbeat")
            return

        if cmd in (protocol.CMD_REPORT, protocol.CMD_RECORD):
            length = plain[3]
            record = protocol.parse_record(plain)
            if record is None:
                log.warning("%s 无法保存的称重帧 %s", peer, raw.hex())
                return
            record["raw"] = raw.hex()
            # 成功入库（或确认已存在）后才 ACK；失败时不确认，允许秤重试。
            # 通知走独立线程池，不等待外部推送；连接事件放在 ACK 之后。
            _save(record)
            ack_cmd = (protocol.CMD_REPORT_ACK if cmd == protocol.CMD_REPORT
                       else protocol.CMD_RECORD_ACK)
            sock.sendall(protocol.build_time_ack(raw, ack_cmd, length))
            _event(self._mac, "report")
            return

        # 其它命令：原样回 cmd|0x4000
        sock.sendall(protocol.build_time_ack(raw, cmd | 0x4000, plain[3]))


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    logging.basicConfig(level=logging.DEBUG if _debug() else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_db()
    from . import config
    addr = (config.INGEST_HOST, config.INGEST_PORT)
    with Server(addr, Handler) as srv:
        log.info("采集服务监听 %s:%s", addr[0], addr[1])
        srv.serve_forever()


def _debug() -> bool:
    from . import config
    return config.DEBUG


if __name__ == "__main__":
    main()
