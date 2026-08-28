#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SW QLAB MONITOR  v1.0.0
QLab (4 / 5) を LAN 経由で監視するスタンドアロン・モニター。

- QLab OSC API (OSC 1.1 over TCP, SLIP frame, default port 53000)
- Python 標準ライブラリのみ / 単一ファイル
- ブラウザ UI (http://localhost:8780) — 同一 LAN の別端末からも閲覧可
- Bitfocus Companion のカスタム変数に REMAIN 等を流し込める
- Bonjour (_qlab._tcp.local.) でLAN上のQLabを自動検出 (追加ライブラリ不要)

Usage:
    python sw_qlab_monitor.py                       # 設定ウィンドウ
    python sw_qlab_monitor.py --host 192.168.0.30 --console
    python sw_qlab_monitor.py --demo                # 実機なしで動作確認
    python sw_qlab_monitor.py --discover            # Bonjourで検索して終了
"""

import argparse
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2.0"
DEFAULT_OSC_PORT = 53000
DEFAULT_WEB_PORT = 8780
COMPANION_PORT = 8000
POLL_HZ = 30.0
STRUCTURE_INTERVAL = 5.0
CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "SEVENTHWELL", "sw_qlab_monitor.json")

# SLIP (RFC 1055)
SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


# ---------------------------------------------------------------- OSC
def osc_pad(b):
    return b + b"\x00" * (4 - len(b) % 4)


def osc_encode(address, args=()):
    out = osc_pad(address.encode("utf-8") + b"\x00")
    tags, body = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            body += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            body += struct.pack(">f", a)
        else:
            tags += "s"
            body += osc_pad(str(a).encode("utf-8") + b"\x00")
    return out + osc_pad(tags.encode("ascii") + b"\x00") + body


def _osc_string(data, i):
    end = data.index(b"\x00", i)
    s = data[i:end].decode("utf-8", "replace")
    return s, i + (len(s) // 4 + 1) * 4


def osc_decode(data):
    """-> (address, [args])  失敗時 (None, [])"""
    try:
        address, i = _osc_string(data, 0)
        if i >= len(data):
            return address, []
        tags, i = _osc_string(data, i)
        args = []
        for t in tags[1:]:
            if t == "i":
                args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
            elif t == "f":
                args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
            elif t == "s" or t == "S":
                s, i = _osc_string(data, i)
                args.append(s)
            elif t == "b":
                n = struct.unpack(">i", data[i:i + 4])[0]; i += 4
                args.append(data[i:i + n]); i += (n + 3) // 4 * 4
            elif t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            elif t in "N I":
                args.append(None)
        return address, args
    except (ValueError, struct.error, IndexError):
        return None, []


def slip_encode(payload):
    out = bytearray([SLIP_END])
    for b in payload:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


def slip_decode_stream(buf):
    """-> (完成したパケットのリスト, 残りバッファ)"""
    packets, cur, esc, used = [], bytearray(), False, 0
    for idx, b in enumerate(buf):
        if b == SLIP_END:
            if cur:
                packets.append(bytes(cur))
                cur = bytearray()
            used = idx + 1
            esc = False
        elif esc:
            cur.append(SLIP_END if b == SLIP_ESC_END else
                       SLIP_ESC if b == SLIP_ESC_ESC else b)
            esc = False
        elif b == SLIP_ESC:
            esc = True
        else:
            cur.append(b)
    return packets, buf[used:]


# ---------------------------------------------------------------- client
class QLabError(Exception):
    pass


class QLabClient:
    """QLab OSC API (TCP / SLIP)。リプライは JSON で返るので dict にして返す。"""

    def __init__(self, host, port=DEFAULT_OSC_PORT, passcode="", timeout=4.0, source_ip=None):
        self.host = host
        self.port = int(port)
        self.passcode = passcode or ""
        self.timeout = timeout
        self.source_ip = source_ip
        self.sock = None
        self.buf = b""
        self.workspace = None       # uniqueID
        self.workspace_name = None
        self.lock = threading.Lock()
        self.pending = []           # 受信済みだが未処理のリプライ

    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""
        self.pending = []

    def connect(self):
        self.close()
        s = socket.create_connection(
            (self.host, self.port), timeout=self.timeout,
            source_address=(self.source_ip, 0) if self.source_ip else None)
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.buf = b""

        ver = self.call("/version")
        if ver is None:
            raise QLabError("QLab が応答しません（OSC ポート違い / 別アプリ）")
        wss = self.call("/workspaces") or []
        if not wss:
            raise QLabError("開いているワークスペースがありません")
        ws = wss[0]
        self.workspace = ws.get("uniqueID")
        self.workspace_name = ws.get("displayName") or "QLab"
        res = self.call("/workspace/%s/connect" % self.workspace,
                        [self.passcode] if self.passcode else [])
        if isinstance(res, str) and res.startswith("badpass"):
            raise QLabError("パスコードが違います")
        self.call("/alwaysReply", [1])
        self.version = ver

    # -- io -----------------------------------------------------------
    def _send(self, address, args=()):
        self.sock.sendall(slip_encode(osc_encode(address, args)))

    def _recv_replies(self):
        chunk = self.sock.recv(262144)
        if not chunk:
            raise QLabError("QLab が接続を閉じました")
        self.buf += chunk
        packets, self.buf = slip_decode_stream(self.buf)
        for p in packets:
            addr, args = osc_decode(p)
            if addr:
                self.pending.append((addr, args))

    @staticmethod
    def _match(addr, want):
        # /reply/workspace/<id>/cueLists  ←  /workspace/<id>/cueLists
        return addr == "/reply" + want or addr.endswith(want)

    def call(self, address, args=(), want_reply=True):
        return self.call_many([(address, args)], want_reply)[0]

    def call_many(self, calls, want_reply=True):
        """複数の OSC を一括送信し、リプライを対応付けて返す。"""
        if not self.sock:
            raise QLabError("not connected")
        with self.lock:
            out = b""
            for addr, args in calls:
                out += slip_encode(osc_encode(addr, args))
            try:
                self.sock.sendall(out)
                if not want_reply:
                    return [None] * len(calls)
                results = [None] * len(calls)
                filled = [False] * len(calls)
                deadline = time.time() + self.timeout
                while not all(filled):
                    if time.time() > deadline:
                        break
                    if not self.pending:
                        self._recv_replies()
                    rest = []
                    for addr, args in self.pending:
                        hit = False
                        for i, (want, _a) in enumerate(calls):
                            if filled[i] or not self._match(addr, want):
                                continue
                            results[i] = self._payload(args)
                            filled[i] = hit = True
                            break
                        if not hit:
                            rest.append((addr, args))
                    self.pending = rest[-200:]
                return results
            except (OSError, socket.timeout) as e:
                self.close()
                raise QLabError(str(e))

    @staticmethod
    def _payload(args):
        if not args:
            return None
        raw = args[0]
        if isinstance(raw, str):
            try:
                msg = json.loads(raw)
            except ValueError:
                return raw
            if isinstance(msg, dict):
                if msg.get("status") not in (None, "ok"):
                    return None
                return msg.get("data")
            return msg
        return raw


# ---------------------------------------------------------------- helpers
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def os_interfaces():
    out = []
    try:
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            text = subprocess.check_output(["ipconfig"], startupinfo=si,
                                           timeout=10).decode("cp932", "replace")
            name = "?"
            for line in text.splitlines():
                if line.strip() and not line.startswith((" ", "\t")):
                    name = line.strip().rstrip(":")
                elif "IPv4" in line:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        out.append((name, m.group(1)))
        else:
            raw = subprocess.check_output(["ip", "-4", "-o", "addr"], timeout=10)
            for line in raw.decode("utf-8", "replace").splitlines():
                p = line.split()
                if len(p) > 3:
                    out.append((p[1], p[3].split("/")[0]))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return [(n, ip) for n, ip in out if not ip.startswith("127.")]


def local_ips():
    ips = set(ip for _n, ip in os_interfaces())
    p = local_ip()
    if p:
        ips.add(p)
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except socket.gaierror:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


# ---------------------------------------------------------------- Bonjour (mDNS) 検索
# QLab は "_qlab._tcp.local." で自分自身を Bonjour に広告する (QLab Remote が
# ワークスペースを自動的に見つけて接続できるのはこの仕組みによる)。
# 追加ライブラリなしで最低限の mDNS クライアント(質問送信 + 応答パース)を実装する。
MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
QLAB_MDNS_SERVICE = "_qlab._tcp.local."


def _mdns_encode_name(name):
    out = b""
    for part in name.rstrip(".").split("."):
        b = part.encode("utf-8")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _mdns_build_query(service):
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    question = _mdns_encode_name(service) + struct.pack(">HH", 12, 1)  # PTR, IN
    return header + question


def _mdns_read_name(data, offset):
    """DNS 名前圧縮(0xC0 ポインタ)に対応した名前デコード。
    戻り値: (name, 直後に続くオフセット)"""
    labels = []
    jumped, after_offset = False, None
    guard = 0
    while guard < 128:
        guard += 1
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                after_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("utf-8", "replace"))
        offset += length
    name = (".".join(labels) + ".") if labels else ""
    return name, (after_offset if jumped else offset)


def _mdns_parse_records(data):
    try:
        _id, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    except struct.error:
        return []
    offset = 12
    for _ in range(qd):
        _name, offset = _mdns_read_name(data, offset)
        offset += 4  # QTYPE + QCLASS
    records = []
    for _ in range(an + ns + ar):
        if offset >= len(data):
            break
        name, offset = _mdns_read_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlength]
        records.append({"name": name, "type": rtype, "rdata": rdata, "rdata_offset": offset})
        offset += rdlength
    return records


def mdns_discover_qlab(timeout=3.0):
    """LAN上の QLab を Bonjour ("_qlab._tcp.local.") で検索する。
    -> [{"name": ワークスペース表示名, "host": IP, "port": ポート}, ...]"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    try:
        sock.bind(("", MDNS_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(MDNS_ADDR), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        sock.close()
        return []
    sock.settimeout(0.4)
    try:
        sock.sendto(_mdns_build_query(QLAB_MDNS_SERVICE), (MDNS_ADDR, MDNS_PORT))
    except OSError:
        sock.close()
        return []

    srv_by_name, a_by_host = {}, {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        for rec in _mdns_parse_records(data):
            if rec["type"] == 33 and rec["name"].lower().endswith(QLAB_MDNS_SERVICE):
                if len(rec["rdata"]) < 6:
                    continue
                _prio, _weight, port = struct.unpack(">HHH", rec["rdata"][:6])
                target, _ = _mdns_read_name(data, rec["rdata_offset"] + 6)
                srv_by_name[rec["name"]] = {"host": target, "port": port}
            elif rec["type"] == 1 and len(rec["rdata"]) == 4:  # A record
                a_by_host[rec["name"]] = ".".join(str(b) for b in rec["rdata"])
    sock.close()

    suffix = "." + QLAB_MDNS_SERVICE
    results = []
    for full_name, srv in srv_by_name.items():
        display = full_name[:-len(suffix)] if full_name.lower().endswith(suffix) else full_name
        ip = a_by_host.get(srv["host"], srv["host"])
        results.append({"name": display, "host": ip, "port": srv["port"]})
    return results


def tcp_open(ip, port, timeout=3.0, source_ip=None):
    try:
        with socket.create_connection((ip, port), timeout=timeout,
                                      source_address=(source_ip, 0) if source_ip else None):
            return True, None
    except OSError as e:
        return False, e


def _num(v, default=0.0):
    try:
        if isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_hms(sec):
    if sec is None:
        return "--:--:--"
    sec = max(0.0, sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def fmt_hmsf(sec, fps=30):
    if sec is None:
        return "--:--:--:--"
    sec = max(0.0, sec)
    f = min(int(fps) - 1, int((sec - int(sec)) * fps))
    return "%s:%02d" % (fmt_hms(sec), f)


def parse_targets(text, default_port=DEFAULT_OSC_PORT):
    out = []
    for chunk in re.split(r"[,\s]+", str(text or "")):
        chunk = chunk.strip()
        if not chunk:
            continue
        host, _sep, ps = chunk.partition(":")
        out.append((host.strip(), int(ps) if ps.strip().isdigit() else int(default_port)))
    return out


# ---------------------------------------------------------------- monitor
class Monitor(threading.Thread):
    """1台の QLab をポーリングして共有 state を更新する。"""

    daemon = True

    def __init__(self, host, port=DEFAULT_OSC_PORT, passcode="",
                 source_ip=None, dev_id="d1", name=None):
        super().__init__()
        self.dev_id = dev_id
        self.name = name or ("%s:%d" % (host, int(port)) if host else "QLab")
        self.client = QLabClient(host, port, passcode, source_ip=source_ip)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.force_scan = threading.Event()
        self.struct_rev = 0
        self.last_scan = 0.0
        self.lists = []          # [{id, number, name, cues:[{id,number,name,type,color,dur}]}]
        self.detail = {}         # cue id -> {"file": 素材名, "list": 所属リスト}
        self.state = {
            "connected": False,
            "host": host, "port": int(port),
            "workspace": None, "qlabVersion": None,
            "error": "未接続", "sourceIp": source_ip,
            "lists": [], "running": [], "playhead": None,
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def rescan(self):
        self.force_scan.set()

    def run(self):
        interval = 1.0 / POLL_HZ
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                if not self.client.connected:
                    self.client.connect()
                    with self.lock:
                        self.state["connected"] = True
                        self.state["error"] = None
                        self.state["workspace"] = self.client.workspace_name
                        self.state["qlabVersion"] = self.client.version
                        self.state["sourceIp"] = self.client.source_ip
                    self.name = "%s (%s)" % (self.client.host, self.client.workspace_name)
                    self.detail = {}
                    self.force_scan.set()

                if self.force_scan.is_set() or (time.time() - self.last_scan) > STRUCTURE_INTERVAL:
                    self.force_scan.clear()
                    self.scan_lists()
                    self.last_scan = time.time()

                self.poll_running()
            except QLabError as e:
                self._fail(str(e))
            except OSError as e:
                self._fail(str(e))
            except Exception as e:  # noqa: BLE001 - モニターは落とさない
                self._fail("%s: %s" % (type(e).__name__, e))
            time.sleep(max(0.002, interval - (time.time() - t0)))

    def _fail(self, msg):
        self.client.close()
        with self.lock:
            self.state["connected"] = False
            self.state["error"] = msg
            self.state["running"] = []
        # 既定の経路で駄目なら各NICを送信元にして再試行
        if "timed out" in msg or "unreachable" in msg:
            for src in local_ips():
                if src == self.client.source_ip:
                    continue
                self.client.source_ip = src
                try:
                    self.client.connect()
                    return
                except (QLabError, OSError):
                    self.client.close()
            self.client.source_ip = None
        time.sleep(0.8)

    # -- structure ----------------------------------------------------
    def scan_lists(self):
        c = self.client
        ws = c.workspace
        raw = c.call("/workspace/%s/cueLists/shallow" % ws) or []
        lists = []
        for cl in raw if isinstance(raw, list) else []:
            cues = []
            for cu in (cl.get("cues") or []):
                cues.append({"id": cu.get("uniqueID"), "number": cu.get("number") or "",
                             "name": cu.get("name") or "", "type": cu.get("type") or "",
                             "color": cu.get("colorName") or "none", "dur": 0.0})
            lists.append({"id": cl.get("uniqueID"), "number": cl.get("number") or "",
                          "name": cl.get("listName") or cl.get("name") or "Cue List",
                          "cues": cues})
        # 各キューの尺（バー表示用）
        for lst in lists:
            targets = lst["cues"][:120]
            if not targets:
                continue
            res = c.call_many([("/workspace/%s/cue_id/%s/currentDuration" % (ws, x["id"]), ())
                               for x in targets])
            for x, d in zip(targets, res):
                x["dur"] = round(_num(d), 3)
        with self.lock:
            self.lists = lists
            self.state["lists"] = lists
            self.struct_rev += 1

    def fetch_detail(self, ids):
        """再生中キューの素材ファイル名を1回だけ取って覚える（30Hz では取りに行かない）。"""
        need = [i for i in ids if i and i not in self.detail]
        if not need:
            return
        ws = self.client.workspace
        calls = []
        for cid in need[:8]:
            calls.append(("/workspace/%s/cue_id/%s/fileTarget" % (ws, cid), ()))
            calls.append(("/workspace/%s/cue_id/%s/listName" % (ws, cid), ()))
        res = self.client.call_many(calls)
        for i, cid in enumerate(need[:8]):
            path = res[2 * i]
            name = res[2 * i + 1]
            self.detail[cid] = {
                "file": (str(path).replace("\\", "/").rstrip("/").split("/")[-1]
                         if isinstance(path, str) and path.strip() else ""),
                "list": name if isinstance(name, str) else "",
            }

    # -- running ------------------------------------------------------
    def poll_running(self):
        c = self.client
        ws = c.workspace
        head = c.call_many([
            ("/workspace/%s/runningOrPausedCues/shallow" % ws, ()),
            ("/workspace/%s/cue/playhead/uniqueID" % ws, ()),
            ("/workspace/%s/cue/playhead/displayName" % ws, ()),
            ("/workspace/%s/cue/playhead/number" % ws, ()),
        ])
        raw, ph_id, ph_name, ph_num = head
        cues = [x for x in (raw or []) if isinstance(x, dict)]
        cues = cues[:12]
        if cues:
            calls = []
            for x in cues:
                cid = x.get("uniqueID")
                calls.append(("/workspace/%s/cue_id/%s/actionElapsed" % (ws, cid), ()))
                calls.append(("/workspace/%s/cue_id/%s/currentDuration" % (ws, cid), ()))
                calls.append(("/workspace/%s/cue_id/%s/isPaused" % (ws, cid), ()))
            vals = c.call_many(calls)
        else:
            vals = []

        running, p = [], 0
        for x in cues:
            el = _num(vals[p]); dur = _num(vals[p + 1]); paused = bool(vals[p + 2])
            p += 3
            running.append({
                "id": x.get("uniqueID"), "number": x.get("number") or "",
                "name": x.get("name") or x.get("listName") or "",
                "type": x.get("type") or "", "color": x.get("colorName") or "none",
                "elapsed": round(el, 3), "duration": round(dur, 3),
                "remain": round(max(0.0, dur - el), 3) if dur > 0 else None,
                "paused": paused,
            })
        try:
            self.fetch_detail([r["id"] for r in running])
        except QLabError:
            pass
        for r in running:
            d = self.detail.get(r["id"]) or {}
            r["file"] = d.get("file", "")
            r["list"] = d.get("list", "")
        running.sort(key=lambda r: (r["remain"] is None, -(r["remain"] or 0)))

        with self.lock:
            self.state["running"] = running
            self.state["playhead"] = ({"id": ph_id, "name": ph_name or "",
                                       "number": ph_num or ""} if ph_id else None)
            self.state["connected"] = True
            self.state["error"] = None
            self.state["stamp"] = time.monotonic()


# ---------------------------------------------------------------- devices
class DeviceManager:
    def __init__(self, targets=(), passcode="", source_ip=None):
        self.monitors = []
        self.passcode = passcode
        self.source_ip = source_ip
        self.set_targets(targets)

    def set_targets(self, targets, passcode=None, source_ip=None):
        if passcode is not None:
            self.passcode = passcode
        if source_ip is not None:
            self.source_ip = source_ip
        targets = [(h, int(p)) for h, p in targets if h]
        keep, seen = [], set()
        for host, port in targets:
            if (host, port) in seen:
                continue
            seen.add((host, port))
            found = next((m for m in self.monitors
                          if m.client.host == host and m.client.port == port), None)
            if found:
                found.client.passcode = self.passcode
                keep.append(found)
            else:
                mon = Monitor(host, port, self.passcode, self.source_ip,
                              dev_id="d%d" % len(seen))
                mon.start()
                keep.append(mon)
        for m in self.monitors:
            if m not in keep:
                m.stop_flag.set()
                m.client.close()
        for i, m in enumerate(keep):
            m.dev_id = "d%d" % (i + 1)
        self.monitors = keep

    def rescan(self):
        for m in self.monitors:
            m.rescan()

    @property
    def rev(self):
        return "-".join("%s:%s" % (m.dev_id, m.struct_rev) for m in self.monitors)

    def snapshot(self):
        devs, stamps = [], []
        for m in self.monitors:
            st = m.snapshot()
            st["id"] = m.dev_id
            st["name"] = m.name
            devs.append(st)
            if st.get("stamp"):
                stamps.append(st["stamp"])
        return {
            "version": VERSION,
            "devices": devs,
            "connected": any(d["connected"] for d in devs) if devs else False,
            "error": next((d["error"] for d in devs if d["error"]), None),
            "rev": self.rev,
            "stamp": max(stamps) if stamps else None,
        }

    def tick(self):
        devs, stamps = [], []
        for m in self.monitors:
            st = m.snapshot()
            devs.append({"id": m.dev_id, "connected": st["connected"],
                         "running": st["running"], "playhead": st["playhead"],
                         "error": st["error"]})
            if st.get("stamp"):
                stamps.append(st["stamp"])
        return {"c": 1, "rev": self.rev, "devices": devs,
                "connected": any(d["connected"] for d in devs) if devs else False,
                "stamp": max(stamps) if stamps else None}


# ---------------------------------------------------------------- Companion
class CompanionPush(threading.Thread):
    daemon = True

    def __init__(self, manager, host, port=COMPANION_PORT, rate=4.0):
        super().__init__()
        self.manager = manager
        self.host = host
        self.port = int(port)
        self.rate = rate
        self.stop_flag = threading.Event()
        self.last = {}
        self.status = "待機中"
        self.conn = None

    def values(self):
        st = self.manager.snapshot()
        devs = st.get("devices", [])
        out = {"qlab_connected": "OK" if st.get("connected") else "NG",
               "qlab_devices": "%d/%d" % (sum(1 for d in devs if d["connected"]), len(devs))}
        for i, dev in enumerate(devs):
            pre = "qlab_" if len(devs) <= 1 else "qlab%d_" % (i + 1)
            run = dev.get("running") or []
            main = run[0] if run else None
            ph = dev.get("playhead") or {}
            out[pre + "workspace"] = dev.get("workspace") or "—"
            out[pre + "state"] = ("PAUSED" if (main and main["paused"]) else
                                  "PLAYING" if main else
                                  "STANDBY" if dev["connected"] else "NO LINK")
            out[pre + "remain"] = fmt_hms(main["remain"]) if main else "--:--:--"
            out[pre + "elapsed"] = fmt_hms(main["elapsed"]) if main else "--:--:--"
            out[pre + "cue"] = ((main["number"] + " " if main["number"] else "") +
                                main["name"]) if main else "—"
            out[pre + "cue_number"] = main["number"] if main else "—"
            out[pre + "running_count"] = str(len(run))
            out[pre + "next"] = ((ph.get("number", "") + " " if ph.get("number") else "") +
                                 ph.get("name", "")) if ph else "—"
            out[pre + "end_at"] = (time.strftime("%H:%M:%S",
                                   time.localtime(time.time() + (main["remain"] or 0)))
                                   if main and main["remain"] is not None else "--:--:--")
        return out

    def push(self, name, value):
        path = "/api/custom-variable/%s/value?value=%s" % (
            urllib.parse.quote(name), urllib.parse.quote(str(value)))
        if self.conn is None:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=2.0)
        self.conn.request("POST", path)
        resp = self.conn.getresponse()
        resp.read()
        return resp.status

    def run(self):
        fails = 0
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                sent = 0
                for k, v in self.values().items():
                    if self.last.get(k) == v:
                        continue
                    code = self.push(k, v)
                    if code == 404:
                        self.status = "変数 %s が Companion にありません（作成してください）" % k
                    elif code >= 400:
                        self.status = "Companion 応答 %d" % code
                    self.last[k] = v
                    sent += 1
                if sent or self.status == "待機中":
                    self.status = "送信中 → %s:%d" % (self.host, self.port)
                fails = 0
            except (OSError, http.client.HTTPException) as e:
                fails += 1
                self.status = "Companion に接続できません (%s)" % e
                self.last.clear()
                if self.conn:
                    try:
                        self.conn.close()
                    except OSError:
                        pass
                self.conn = None
                time.sleep(min(5.0, 0.5 * fails))
            time.sleep(max(0.05, 1.0 / self.rate - (time.time() - t0)))


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

# ---------------------------------------------------------------- web UI
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SW QLAB MONITOR</title>
<style>
  :root{--bg:#050505;--fg:#f0f0f0;--dim:#6a6a6a;--line:#1e1e1e;--warn:#e0a020;--crit:#e03a2f}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--fg);overflow:hidden;
    font-family:Consolas,"SF Mono",Menlo,ui-monospace,"MS Gothic",monospace;-webkit-font-smoothing:antialiased}
  .app{display:flex;flex-direction:column;height:100%}
  header{display:flex;align-items:center;flex-wrap:wrap;gap:10px 18px;padding:10px 18px;
    border-bottom:1px solid var(--line);flex:0 0 auto}
  .brand{letter-spacing:.34em;font-size:12px;text-transform:uppercase}
  .brand b{font-weight:700}
  .ver{color:var(--dim);letter-spacing:.2em;font-size:10px}
  .spacer{flex:1}
  .conn{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.14em;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--crit);box-shadow:0 0 8px currentColor}
  .dot.ok{background:var(--fg)}
  input,button{font:inherit;font-size:11px;background:transparent;color:var(--fg);
    border:1px solid var(--line);padding:5px 9px;letter-spacing:.1em}
  input{width:190px}
  input.port{width:66px}
  button{cursor:pointer}
  button:hover{border-color:var(--fg)}
  button:focus-visible,input:focus-visible{outline:1px solid var(--fg);outline-offset:2px}

  main{flex:1;display:flex;min-height:0}
  .list{width:250px;flex:0 0 auto;border-right:1px solid var(--line);overflow-y:auto;padding:10px 0}
  .list .hd{font-size:10px;letter-spacing:.28em;color:var(--dim);padding:0 16px 10px}
  .devhd{font-size:10px;letter-spacing:.2em;color:var(--dim);padding:12px 16px 4px;
    border-top:1px solid var(--line);margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .devhd.ng{color:var(--crit)}
  .row{padding:9px 16px;border-left:2px solid transparent;cursor:pointer}
  .row:hover{background:#0d0d0d}
  .row.sel{border-left-color:var(--fg);background:#101010}
  .row .n{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row .s{font-size:10px;letter-spacing:.18em;color:var(--dim);margin-top:4px}
  .row.play .s{color:var(--fg)}
  .devempty{font-size:10px;color:var(--dim);padding:4px 16px 8px}

  .stage{flex:1;display:flex;flex-direction:column;min-width:0;padding:20px 26px 16px}
  .tlname{font-size:13px;letter-spacing:.3em;text-transform:uppercase;color:var(--dim);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hero{flex:1;display:flex;align-items:center;gap:34px;min-height:0}
  .big{flex:1 1 0;min-width:0;text-align:center;display:flex;flex-direction:column;justify-content:center}
  .big .lab{font-size:11px;letter-spacing:.42em;color:var(--dim);margin-bottom:10px}
  .big .val{font-size:64px;line-height:.92;font-weight:700;font-variant-numeric:tabular-nums;
    letter-spacing:-.015em;white-space:nowrap}
  .big.warn .val{color:var(--warn)}
  .big.crit .val{color:var(--crit)}
  .side{flex:0 0 auto;width:clamp(190px,23vw,320px);display:flex;flex-direction:column;gap:18px}
  .side .lab{font-size:10px;letter-spacing:.32em;color:var(--dim);margin-bottom:5px}
  .side .val{font-size:clamp(19px,2.7vw,36px);font-variant-numeric:tabular-nums;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .side .txt{font-size:15px;letter-spacing:.04em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .side .sub{font-size:11px;color:var(--dim);margin-top:2px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .state{font-size:15px!important;letter-spacing:.28em}
  @media (max-width:1100px){
    .hero{flex-direction:column;align-items:stretch;justify-content:center;gap:14px}
    .side{width:100%;flex-direction:row;flex-wrap:wrap;gap:12px 30px}
    .side>div{min-width:150px}
  }
  @media (max-width:760px){.list{width:150px}.stage{padding:14px}}

  .nowbar{flex:0 0 auto;display:flex;gap:10px;margin-top:10px;overflow-x:auto;padding-bottom:4px}
  .nowcard{position:relative;border:1px solid var(--line);border-left-width:4px;
    border-left-color:#3a3a3a;padding:9px 14px 11px;background:#0b0b0b;min-width:250px;
    max-width:420px;flex:1 1 250px;overflow:hidden}
  .nowcard .hd{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .nowcard .badge{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#0b0b0b;
    background:var(--dim);padding:1px 6px;white-space:nowrap}
  .nowcard .num{font-size:11px;color:var(--dim);letter-spacing:.12em}
  .nowcard .r{font-size:17px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .nowcard .f{font-size:11px;color:var(--dim);margin-top:3px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .nowcard .t{font-size:13px;font-variant-numeric:tabular-nums;margin-top:7px;
    display:flex;justify-content:space-between;gap:10px}
  .nowcard .t b{font-weight:700}
  .nowcard .t i{font-style:normal;color:var(--dim)}
  .nowcard .pbar{position:absolute;left:0;right:0;bottom:0;height:3px;background:#1a1a1a}
  .nowcard .pbar i{display:block;height:100%;background:var(--fg);width:0}
  .nowcard.pause{border-color:var(--warn)}
  .nowcard.pause .badge{background:var(--warn)}
  .nowcard.pause .pbar i{background:var(--warn)}
  .nowcard.soon .t b{color:var(--warn)}
  .nowcard.crit .t b{color:var(--crit)}
  .nowcard.crit .pbar i{background:var(--crit)}
  .nowempty{border:1px solid var(--line);padding:12px 16px;color:var(--dim);font-size:12px;
    letter-spacing:.16em;background:#0a0a0a}
  .track{flex:0 0 auto;margin-top:10px}
  .cuestrip .c.run{background:#3a3a3a}
  .cuestrip{display:flex;gap:2px;height:26px;border:1px solid var(--line);background:#0a0a0a;overflow:hidden}
  .cuestrip .c{position:relative;min-width:3px;background:#1c1c1c;display:flex;align-items:center;
    padding:0 5px;overflow:hidden}
  .cuestrip .c span{font-size:9px;color:var(--dim);white-space:nowrap}
  .cuestrip .c.run{background:#3a3a3a}
  .cuestrip .c.run span{color:var(--fg)}
  .cuestrip .c.head{box-shadow:inset 0 0 0 1px var(--fg)}
  .cuestrip .c .fill{position:absolute;left:0;top:0;bottom:0;background:#6a6a6a;opacity:.5}
  .ends{display:flex;justify-content:space-between;color:var(--dim);font-size:10px;
    letter-spacing:.18em;margin-top:6px}
  .msg{color:var(--dim);font-size:12px;letter-spacing:.16em;text-align:center;padding:36px}
  .msg b{color:var(--crit)}
  body.focus .list,body.focus header,body.focus .track,body.focus .nowbar{display:none}
  body.focus .stage{padding:0}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand"><b>SEVENTHWELL</b> &nbsp;QLAB MONITOR</div>
    <div class="ver">v__VER__</div>
    <div class="spacer"></div>
    <div class="conn"><span id="dot" class="dot"></span><span id="cstat">CONNECTING</span></div>
    <input id="host" placeholder="192.168.0.30" title="カンマ区切りで複数のQLabを指定できます">
    <input id="port" class="port" placeholder="53000">
    <button id="apply">接続</button>
    <button id="scan">再スキャン</button>
    <button id="full">全画面 (F)</button>
  </header>
  <main>
    <div class="list"><div class="hd">CUE LISTS</div><div id="rows"></div></div>
    <div class="stage">
      <div class="tlname" id="tlname">—</div>
      <div class="hero">
        <div class="big" id="remainBox">
          <div class="lab">REMAIN</div>
          <div class="val" id="remainVal">--:--:--</div>
        </div>
        <div class="side">
          <div><div class="lab">ELAPSED</div><div class="val" id="elapsed">--:--:--</div></div>
          <div><div class="lab">NOW</div><div class="txt" id="nowcue">—</div>
            <div class="sub" id="nowfile"></div></div>
          <div><div class="lab">NEXT (PLAYHEAD)</div><div class="txt" id="nextcue">—</div></div>
          <div><div class="lab">END AT</div><div class="val" id="endat">--:--:--</div></div>
          <div><div class="lab">STATE</div><div class="val state" id="mode">—</div></div>
        </div>
      </div>
      <div class="nowbar" id="nowbar"></div>
      <div class="track" id="track">
        <div class="cuestrip" id="strip"></div>
        <div class="ends"><span id="listinfo">—</span><span id="totinfo"></span></div>
      </div>
      <div class="msg" id="msg" style="display:none"></div>
    </div>
  </main>
</div>
<script>
let S=null, sel=null, lastFetch=0, updateMs=100, structKey="";
const QCOLOR={red:"#c0392b","hot pink":"#e0538a",crimson:"#b0203a",orange:"#d07820",
  peach:"#d09070",yellow:"#c8b030",olive:"#8a9040",green:"#3f9a55",forest:"#2f7040",
  cyan:"#38a0a8","sky blue":"#4a90c0",blue:"#3a6fb0",indigo:"#5050a0",midnight:"#303860",
  purple:"#7a4fa0",lavender:"#9080c0",magenta:"#a83fa0",plum:"#8a4a70",berry:"#9a3060",
  gray:"#707070"};
const disp={}; let lastFrame=performance.now();

function hms(sec){
  if(sec===null||sec===undefined||isNaN(sec)) return "--:--:--";
  sec=Math.max(0,sec);
  const p=n=>String(n).padStart(2,"0");
  return p(Math.floor(sec/3600))+":"+p(Math.floor(sec%3600/60))+":"+p(Math.floor(sec%60));
}
function devs(){ return (S&&S.devices)||[]; }
function curDev(){
  const d=devs();
  if(!d.length) return null;
  if(!sel) sel=(d.find(x=>(x.running||[]).length)||d[0]).id;
  return d.find(x=>x.id===sel)||d[0];
}
function mainCue(dev){ return dev&&(dev.running||[]).length?dev.running[0]:null; }

function tickClocks(){
  const now=performance.now();
  const dt=Math.min(.25,(now-lastFrame)/1000); lastFrame=now;
  for(const dev of devs()){
    for(const r of (dev.running||[])){
      const lag=(S.age||0)+(now-lastFetch)/1000;
      const running=!r.paused;
      const tgt={e:r.elapsed+(running?lag:0),
                 r:r.remain===null?null:Math.max(0,r.remain-(running?lag:0))};
      let d=disp[r.id];
      if(!d||d.paused!==r.paused){ disp[r.id]=d={e:tgt.e,r:tgt.r,paused:r.paused}; continue; }
      d.paused=r.paused;
      if(running){ d.e+=dt; if(d.r!==null) d.r=Math.max(0,d.r-dt); }
      let err=tgt.e-d.e;
      d.e = Math.abs(err)>0.5 ? tgt.e : d.e+err*Math.min(1,dt*5);
      if(tgt.r===null){ d.r=null; }
      else if(d.r===null){ d.r=tgt.r; }
      else { err=tgt.r-d.r; d.r=Math.abs(err)>0.5?tgt.r:Math.max(0,d.r+err*Math.min(1,dt*5)); }
    }
  }
}
function shown(r){ const d=r?disp[r.id]:null; return d?{e:d.e,r:d.r}:(r?{e:r.elapsed,r:r.remain}:null); }

function fitRemain(){
  const box=document.getElementById("remainBox"), el=document.getElementById("remainVal");
  const hero=document.querySelector(".hero");
  const size=Math.max(26,Math.min(200,box.clientWidth/5.2,hero.clientHeight*0.66));
  if(Math.abs(parseFloat(el.style.fontSize||0)-size)>0.5) el.style.fontSize=size+"px";
}

function render(){
  const dot=document.getElementById("dot"), cstat=document.getElementById("cstat");
  const msg=document.getElementById("msg"), d=devs();
  const ok=S&&S.connected;
  dot.className="dot"+(ok?" ok":"");
  cstat.textContent=!S?"NO LINK":d.length>1?("接続 "+d.filter(x=>x.connected).length+"/"+d.length)
    :(ok?(d[0].host+":"+d[0].port):"NO LINK");

  if(!ok){
    msg.style.display="block";
    msg.innerHTML=!S?"モニターに接続できません。":
      ("QLab に接続できません &nbsp;<b>"+((d[0]&&d[0].error)||"")+"</b><br><br>"+
       "QLab の Workspace Settings › Network › OSC Access で<br>"+
       "「Allow OSC Connections」を有効にし、No Passcode の権限を許可してください。");
    document.querySelector(".hero").style.display="none";
    document.getElementById("track").style.display="none";
    document.getElementById("nowbar").style.display="none";
  }else{
    msg.style.display="none";
    document.querySelector(".hero").style.display="";
    document.getElementById("track").style.display="";
    document.getElementById("nowbar").style.display="";
  }

  // 左リスト
  const rows=document.getElementById("rows");
  const key=d.map(x=>x.id+x.connected+(x.lists||[]).map(l=>l.id).join()).join("|")+"|"+sel;
  if(rows.dataset.k!==key){
    rows.dataset.k=key; rows.innerHTML="";
    const multi=d.length>1;
    d.forEach(dev=>{
      if(multi){
        const h=document.createElement("div");
        h.className="devhd"+(dev.connected?"":" ng");
        h.textContent=(dev.connected?"● ":"○ ")+dev.name;
        rows.appendChild(h);
      }
      const el=document.createElement("div");
      el.className="row"+(dev.id===sel?" sel":"");
      el.innerHTML='<div class="n"></div><div class="s"></div>';
      el.querySelector(".n").textContent=dev.workspace||dev.name;
      el.onclick=()=>{ sel=dev.id; rows.dataset.k=""; render(); };
      rows.appendChild(el);
      dev._el=el;
      if(!dev.connected){
        const e=document.createElement("div");
        e.className="devempty"; e.textContent=dev.error||"未接続";
        rows.appendChild(e);
      }
    });
  }else{
    const els=[...rows.querySelectorAll(".row")];
    d.forEach((dev,i)=>{ if(els[i]) dev._el=els[i]; });
  }
  d.forEach(dev=>{
    if(!dev._el) return;
    const m=mainCue(dev), c=shown(m);
    dev._el.classList.toggle("play",!!m&&!m.paused);
    dev._el.classList.toggle("sel",dev.id===sel);
    dev._el.querySelector(".s").textContent=
      (!dev.connected?"NO LINK":m?((m.paused?"PAUSED ":"PLAYING ")+hms(c.r)):"STANDBY");
  });

  const dev=curDev();
  if(!dev) return;
  const m=mainCue(dev), c=shown(m);
  document.getElementById("tlname").textContent=
    (d.length>1?dev.name+"  /  ":"")+(dev.workspace||"—");

  const rem=m?c.r:null;
  document.getElementById("remainVal").textContent=hms(rem);
  const box=document.getElementById("remainBox");
  box.className="big"+(rem===null?"":rem<=10?" crit":rem<=60?" warn":"");
  document.getElementById("elapsed").textContent=m?hms(c.e):"--:--:--";
  const nowEl=document.getElementById("nowcue");
  nowEl.textContent=m?((m.number?"Q"+m.number+"  ":"")+(m.name||m.type)):"—";
  nowEl.title=m&&m.file?m.file:"";
  const fileEl=document.getElementById("nowfile");
  fileEl.textContent=m?(m.file||m.type||""):"";
  const ph=dev.playhead;
  document.getElementById("nextcue").textContent=
    ph?((ph.number?"Q"+ph.number+"  ":"")+(ph.name||"")):"—";
  document.getElementById("mode").textContent=
    !dev.connected?"NO LINK":m?(m.paused?"PAUSED":"PLAYING"):"STANDBY";
  document.getElementById("endat").textContent=
    (m&&!m.paused&&rem!==null)?new Date(Date.now()+rem*1000).toTimeString().slice(0,8):"--:--:--";

  // NOW PLAYING（走っているキューを全部、素材名つきで大きく）
  const nb=document.getElementById("nowbar");
  const run=dev.running||[];
  const nk=dev.id+"|"+run.map(r=>r.id+r.paused+(r.file||"")).join("|");
  if(nb.dataset.k!==nk){
    nb.dataset.k=nk; nb.innerHTML="";
    if(!run.length){
      const e=document.createElement("div");
      e.className="nowempty"; e.textContent="再生中のキューはありません";
      nb.appendChild(e);
    }
    run.forEach(r=>{
      const card=document.createElement("div");
      card.className="nowcard"; card.dataset.id=r.id;
      if(QCOLOR[r.color]) card.style.borderLeftColor=QCOLOR[r.color];
      const hd=document.createElement("div"); hd.className="hd";
      const bg=document.createElement("span"); bg.className="badge";
      bg.textContent=(r.type||"cue");
      if(QCOLOR[r.color]) bg.style.background=QCOLOR[r.color];
      hd.appendChild(bg);
      if(r.number){ const n=document.createElement("span"); n.className="num";
                    n.textContent="Q "+r.number; hd.appendChild(n); }
      const nm=document.createElement("div"); nm.className="r"; nm.textContent=r.name||"（無題）";
      const f=document.createElement("div"); f.className="f";
      const ic=/audio|mic/i.test(r.type||"")?"♪":/video|camera/i.test(r.type||"")?"▶":"·";
      f.textContent=r.file?(ic+" "+r.file):(r.list?r.list:"");
      f.title=r.file||"";
      const t=document.createElement("div"); t.className="t";
      t.innerHTML='<b></b><i></i>';
      const pb=document.createElement("div"); pb.className="pbar"; pb.innerHTML="<i></i>";
      card.appendChild(hd); card.appendChild(nm);
      if(f.textContent) card.appendChild(f);
      card.appendChild(t); card.appendChild(pb);
      nb.appendChild(card);
    });
  }
  for(const card of nb.querySelectorAll(".nowcard")){
    const r=run.find(x=>x.id===card.dataset.id);
    if(!r) continue;
    const cc=shown(r);
    card.classList.toggle("pause",!!r.paused);
    card.classList.toggle("crit",cc.r!==null&&cc.r<=10);
    card.classList.toggle("soon",cc.r!==null&&cc.r>10&&cc.r<=60);
    card.querySelector(".t b").textContent=(r.paused?"❚❚ ":"")+hms(cc.r);
    card.querySelector(".t i").textContent=hms(cc.e)+" / "+hms(r.duration);
    card.querySelector(".pbar i").style.width=
      r.duration>0?Math.min(100,cc.e/r.duration*100)+"%":"0";
  }

  // キューリストのストリップ
  const lists=dev.lists||[];
  const lst=lists[0];
  const sKey=dev.id+"|"+(lst?lst.id+lst.cues.length:"none");
  const strip=document.getElementById("strip");
  if(structKey!==sKey){
    structKey=sKey; strip.innerHTML="";
    if(lst){
      const total=lst.cues.reduce((a,x)=>a+Math.max(x.dur,1),0)||1;
      lst.cues.forEach(cu=>{
        const el=document.createElement("div");
        el.className="c"; el.dataset.id=cu.id;
        el.style.flex=Math.max(cu.dur,1)/total;
        if(QCOLOR[cu.color]) el.style.boxShadow="inset 3px 0 0 "+QCOLOR[cu.color];
        el.title=(cu.number?cu.number+" ":"")+cu.name;
        el.innerHTML='<div class="fill" style="width:0"></div><span></span>';
        el.querySelector("span").textContent=(cu.number?cu.number+" ":"")+cu.name;
        strip.appendChild(el);
      });
      document.getElementById("listinfo").textContent=lst.name+"  ("+lst.cues.length+" cues)";
    }else{
      document.getElementById("listinfo").textContent="キューリストなし";
    }
  }
  for(const el of strip.querySelectorAll(".c")){
    const r=run.find(x=>x.id===el.dataset.id);
    el.classList.toggle("run",!!r);
    el.classList.toggle("head",!!(ph&&ph.id===el.dataset.id));
    const f=el.querySelector(".fill");
    f.style.width=(r&&r.duration>0)?Math.min(100,(shown(r).e/r.duration*100))+"%":"0";
  }
  document.getElementById("totinfo").textContent=
    run.length?(run.length+" running  ↻"+Math.round(1000/Math.max(1,updateMs))+"Hz"):"";
  fitRemain();
}

function onState(json){
  const now=performance.now();
  if(lastFetch) updateMs=updateMs*0.8+(now-lastFetch)*0.2;
  if(json.c){
    if(!S||S.rev!==json.rev) return;
    S.age=json.age; S.connected=json.connected;
    for(const nd of json.devices){
      const dev=S.devices.find(x=>x.id===nd.id);
      if(!dev) continue;
      dev.connected=nd.connected; dev.running=nd.running;
      dev.playhead=nd.playhead; dev.error=nd.error;
    }
    lastFetch=now; return;
  }
  S=json; lastFetch=now;
  const d0=(S.devices||[])[0];
  if(d0){ document.getElementById("host").placeholder=d0.host||"";
          document.getElementById("port").placeholder=d0.port||53000; }
}
async function poll(){
  try{ const r=await fetch("/api/state",{cache:"no-store"}); onState(await r.json()); }
  catch(e){ S=null; }
  render();
}
let es=null,pollTimer=null;
function startPolling(){ if(!pollTimer) pollTimer=setInterval(poll,150); }
function stopPolling(){ if(pollTimer){ clearInterval(pollTimer); pollTimer=null; } }
function startStream(){
  try{
    es=new EventSource("/api/stream");
    es.onmessage=e=>{ stopPolling(); onState(JSON.parse(e.data)); };
    es.onerror=()=>{ try{es.close();}catch(_){} es=null; startPolling(); setTimeout(startStream,3000); };
  }catch(e){ startPolling(); }
}
setInterval(()=>{ if(performance.now()-lastFetch>2000) startPolling(); },1000);

document.getElementById("apply").onclick=async()=>{
  const host=document.getElementById("host").value.trim()||document.getElementById("host").placeholder;
  const port=document.getElementById("port").value.trim()||document.getElementById("port").placeholder;
  await fetch("/api/connect",{method:"POST",body:JSON.stringify({host,port:parseInt(port)})});
  structKey=""; poll();
};
document.getElementById("scan").onclick=()=>{ structKey=""; fetch("/api/rescan",{method:"POST"}); };
document.getElementById("full").onclick=toggleFocus;
function toggleFocus(){
  document.body.classList.toggle("focus");
  if(document.body.classList.contains("focus")&&!document.fullscreenElement)
    document.documentElement.requestFullscreen().catch(()=>{});
  else if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
}
addEventListener("keydown",e=>{
  if(e.key==="f"||e.key==="F") toggleFocus();
  if(e.key==="Escape") document.body.classList.remove("focus");
});
(function loop(){ tickClocks(); render(); requestAnimationFrame(loop); })();
startStream(); poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    monitor = None
    server_version = "SWQLabMonitor/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                last_rev, last_full = None, 0.0
                while True:
                    now = time.monotonic()
                    rev = self.monitor.rev
                    if rev != last_rev or (now - last_full) > 2.0:
                        st = self.monitor.snapshot()
                        last_rev, last_full = rev, now
                    else:
                        st = self.monitor.tick()
                    stamp = st.pop("stamp", None)
                    if stamp is not None:
                        st["age"] = round(time.monotonic() - stamp, 4)
                    self.wfile.write(b"data: " + json.dumps(st).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1.0 / POLL_HZ)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(self.monitor.snapshot()),
                       "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            self._send(200, HTML.replace("__VER__", VERSION), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/connect":
            targets = parse_targets(body.get("host"), int(body.get("port") or DEFAULT_OSC_PORT))
            if targets:
                self.monitor.set_targets(targets)
                save_config({"host": body.get("host"), "port": int(body.get("port") or DEFAULT_OSC_PORT)})
            self._send(200, '{"ok":true}', "application/json")
        elif self.path == "/api/rescan":
            self.monitor.rescan()
            self._send(200, '{"ok":true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")


# ---------------------------------------------------------------- demo QLab
class DemoQLab(threading.Thread):
    """実機なしで UI を確認するための最小ダミー QLab (OSC/TCP/SLIP)。"""

    daemon = True
    WS = "DEMO-WORKSPACE-0001"

    def __init__(self, port=DEFAULT_OSC_PORT):
        super().__init__()
        self.port = port
        self.t0 = time.time()
        self.cues = [
            {"id": "c1", "number": "1", "name": "Preshow Loop", "type": "Video", "dur": 300.0,
             "color": "sky blue", "file": "/Users/qlab/Movies/preshow_loop_4K.mov"},
            {"id": "c2", "number": "2", "name": "Opening VT", "type": "Video", "dur": 95.0,
             "color": "red", "file": "/Users/qlab/Movies/opening_vt_final_v3.mov"},
            {"id": "c3", "number": "3", "name": "BGM - Main Theme", "type": "Audio", "dur": 240.0,
             "color": "green", "file": "/Users/qlab/Music/main_theme_master.wav"},
            {"id": "c4", "number": "4", "name": "Speaker Slides", "type": "Video", "dur": 600.0,
             "color": "yellow", "file": "/Users/qlab/Movies/keynote_capture.mov"},
            {"id": "c5", "number": "5", "name": "Ending Roll", "type": "Video", "dur": 180.0,
             "color": "purple", "file": "/Users/qlab/Movies/ending_roll.mov"},
        ]

    def cycle(self):
        """(順番に走るキュー, 経過, 次のキュー, 常時走る BGM の経過)"""
        seq = [c for c in self.cues if c["id"] != "c3"]
        total = sum(c["dur"] for c in seq)
        t = (time.time() - self.t0) % total
        acc = 0.0
        bgm = (time.time() - self.t0) % 240.0
        for i, c in enumerate(seq):
            if acc <= t < acc + c["dur"]:
                return c, t - acc, seq[(i + 1) % len(seq)], bgm
            acc += c["dur"]
        return seq[0], 0.0, seq[1], bgm

    def dispatch(self, addr):
        cur, el, nxt, bgm = self.cycle()
        bgm_cue = next(c for c in self.cues if c["id"] == "c3")
        p = addr.split("/")
        if addr == "/version":
            return "5.0.2"
        if addr == "/workspaces":
            return [{"uniqueID": self.WS, "displayName": "demo_show", "port": self.port,
                     "udpReplyPort": 53001, "version": "5.0.2"}]
        if addr.endswith("/connect"):
            return "ok"
        if addr == "/alwaysReply":
            return None
        if addr.endswith("/cueLists/shallow"):
            return [{"uniqueID": "list1", "number": "", "listName": "Main Cue List",
                     "type": "Cue List", "colorName": "none", "name": "Main Cue List",
                     "cues": [{"uniqueID": c["id"], "number": c["number"], "name": c["name"],
                               "type": c["type"], "colorName": c["color"], "listName": ""}
                              for c in self.cues]}]
        if addr.endswith("/runningOrPausedCues/shallow"):
            return [{"uniqueID": c["id"], "number": c["number"], "name": c["name"],
                     "type": c["type"], "colorName": c["color"], "listName": ""}
                    for c in (cur, bgm_cue)]
        if addr.endswith("/cue/playhead/uniqueID"):
            return nxt["id"]
        if addr.endswith("/cue/playhead/displayName"):
            return nxt["name"]
        if addr.endswith("/cue/playhead/number"):
            return nxt["number"]
        if "/cue_id/" in addr:
            cid = p[p.index("cue_id") + 1]
            cue = next((c for c in self.cues if c["id"] == cid), None)
            if not cue:
                return None
            if addr.endswith("/actionElapsed"):
                if cue["id"] == bgm_cue["id"]:
                    return round(bgm, 2)
                return round(el, 2) if cue["id"] == cur["id"] else 0.0
            if addr.endswith("/currentDuration"):
                return cue["dur"]
            if addr.endswith("/isPaused"):
                return False
            if addr.endswith("/fileTarget"):
                return cue["file"]
            if addr.endswith("/listName"):
                return "Main Cue List"
        return None

    def handle(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            packets, buf = slip_decode_stream(buf)
            out = b""
            for pkt in packets:
                addr, _args = osc_decode(pkt)
                if not addr:
                    continue
                data = self.dispatch(addr)
                payload = json.dumps({"workspace_id": self.WS, "address": addr,
                                      "status": "ok", "data": data})
                out += slip_encode(osc_encode("/reply" + addr, [payload]))
            if out:
                try:
                    conn.sendall(out)
                except OSError:
                    return

    def run(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()


# ---------------------------------------------------------------- GUI
BG, FG, DIM, LINE = "#050505", "#f0f0f0", "#7a7a7a", "#242424"


def message_box(title, text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
        return True
    except Exception:
        try:
            print("%s\n%s" % (title, text))
        except Exception:
            pass
        return False


def has_tkinter():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def run_gui(host, port, web_port):
    if not has_tkinter():
        return False
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    root = tk.Tk()
    root.title("SW QLAB MONITOR v" + VERSION)
    root.configure(bg=BG)
    root.geometry("860x480")
    root.minsize(720, 430)
    mono = ("Consolas", 10)
    state = {"httpd": None, "monitor": None, "web_port": web_port, "comp": None}

    def lab(parent, text, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=kw.pop("fg", FG),
                        font=kw.pop("font", mono), **kw)

    wrap = tk.Frame(root, bg=BG, padx=22, pady=18)
    wrap.pack(fill="both", expand=True)
    head = tk.Frame(wrap, bg=BG); head.pack(fill="x")
    lab(head, "SEVENTHWELL", font=("Consolas", 11, "bold")).pack(side="left")
    lab(head, "  QLAB MONITOR", font=("Consolas", 11)).pack(side="left")
    lab(head, "v" + VERSION, fg=DIM, font=("Consolas", 8)).pack(side="right")
    tk.Frame(wrap, bg=LINE, height=1).pack(fill="x", pady=(10, 16))

    row = tk.Frame(wrap, bg=BG); row.pack(fill="x")
    for i, t in enumerate(["QLab の IP（カンマ区切りで複数可）", "PORT",
                           "パスコード (任意)", "Companion (任意)"]):
        lab(row, t, fg=DIM, font=("Consolas", 9)).grid(row=0, column=i, sticky="w",
                                                       padx=(0 if i == 0 else 12, 0))
    host_var = tk.StringVar(value=host or cfg.get("host", ""))
    port_var = tk.StringVar(value=str(port))
    pass_var = tk.StringVar(value=cfg.get("passcode", ""))
    comp_var = tk.StringVar(value=cfg.get("companion", ""))
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("SW.TCombobox", fieldbackground="#101010", background="#101010",
                    foreground=FG, arrowcolor=FG, bordercolor=LINE, lightcolor=LINE,
                    darkcolor=LINE, selectbackground="#242424", selectforeground=FG)
    host_combo = ttk.Combobox(row, textvariable=host_var, values=[], width=26, font=mono,
                              style="SW.TCombobox")
    host_combo.grid(row=1, column=0, sticky="w", pady=(3, 0))
    bonjour_map = {}

    def on_bonjour_select(_event=None):
        item = bonjour_map.get(host_var.get())
        if item:
            host_var.set(item[0])
            port_var.set(str(item[1]))

    host_combo.bind("<<ComboboxSelected>>", on_bonjour_select)
    for col, var, w in ((1, port_var, 8), (2, pass_var, 12), (3, comp_var, 15)):
        tk.Entry(row, textvariable=var, width=w, font=mono, bg="#101010", fg=FG,
                 insertbackground=FG, relief="flat").grid(row=1, column=col, sticky="w",
                                                          padx=(12, 0), pady=(3, 0))

    btns = tk.Frame(wrap, bg=BG); btns.pack(fill="x", pady=(16, 0))

    def mkbtn(text, cmd):
        b = tk.Button(btns, text=text, command=cmd, font=mono, bg="#111111", fg=FG,
                      activebackground="#1e1e1e", activeforeground=FG, relief="flat",
                      padx=14, pady=6, cursor="hand2",
                      highlightthickness=1, highlightbackground=LINE)
        b.pack(side="left", padx=(0, 8))
        return b

    status = lab(wrap, "待機中", fg=DIM)
    detail = lab(wrap, "", fg=DIM, font=("Consolas", 9))

    def set_status(text, color=FG):
        status.configure(text=text, fg=color)

    def log(msg):
        def put():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, put)

    def open_ui():
        if state["httpd"]:
            webbrowser.open("http://localhost:%d/" % state["web_port"])

    def start(open_browser=True):
        h = host_var.get().strip()
        if not h:
            set_status("QLab の IP を入力してください", "#e0a020")
            return
        try:
            p = int(port_var.get().strip() or DEFAULT_OSC_PORT)
        except ValueError:
            p = DEFAULT_OSC_PORT
        pw, comp = pass_var.get().strip(), comp_var.get().strip()
        save_config({"host": h, "port": p, "passcode": pw, "companion": comp})
        targets = parse_targets(h, p)
        if state["monitor"] is None:
            mgr = DeviceManager(targets, passcode=pw)
            Handler.monitor = mgr
            state["monitor"] = mgr
            wp = state["web_port"]
            for cand in range(wp, wp + 20):
                try:
                    state["httpd"] = ThreadingHTTPServer(("0.0.0.0", cand), Handler)
                    state["web_port"] = cand
                    break
                except OSError:
                    continue
            threading.Thread(target=state["httpd"].serve_forever, daemon=True).start()
        else:
            state["monitor"].set_targets(targets, passcode=pw)
        if state["comp"] and state["comp"].host != comp.split(":")[0]:
            state["comp"].stop_flag.set(); state["comp"] = None
        if comp and not state["comp"]:
            chost, _, cport = comp.partition(":")
            cp = CompanionPush(state["monitor"], chost,
                               int(cport) if cport.isdigit() else COMPANION_PORT)
            cp.start(); state["comp"] = cp
        btn_start.configure(text="再接続")
        if open_browser:
            open_ui()

    def do_bonjour_discover():
        log("Bonjour ( _qlab._tcp.local. ) で検索中…")

        def worker():
            results = mdns_discover_qlab(timeout=3.0)

            def apply():
                if not results:
                    log("Bonjour: QLab が見つかりませんでした"
                        "(OSC/Bonjourが無効か、別ネットワークセグメントの可能性があります)")
                    return
                bonjour_map.clear()
                values = []
                for r in results:
                    disp = "%s  —  %s:%d" % (r["name"], r["host"], r["port"])
                    bonjour_map[disp] = (r["host"], r["port"])
                    values.append(disp)
                    log("見つかりました: %s (%s:%d)" % (r["name"], r["host"], r["port"]))
                host_combo["values"] = values
                if len(results) == 1 and not host_var.get().strip():
                    host_var.set(results[0]["host"])
                    port_var.set(str(results[0]["port"]))
            root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    btn_start = mkbtn("接続してモニター起動", start)
    mkbtn("ブラウザで開く", open_ui)
    mkbtn("再スキャン", lambda: state["monitor"] and state["monitor"].rescan())
    mkbtn("Bonjourで検索", do_bonjour_discover)

    status.pack(anchor="w", pady=(16, 2))
    detail.pack(anchor="w")
    logbox = tk.Text(wrap, height=8, bg="#0b0b0b", fg=DIM, font=("Consolas", 9),
                     relief="flat", highlightthickness=1, highlightbackground=LINE,
                     insertbackground=FG, wrap="none", state="disabled")
    logbox.pack(fill="both", expand=True, pady=(12, 8))
    tip = lab(wrap, "", fg=DIM, font=("Consolas", 8)); tip.pack(anchor="w")

    def tick():
        mgr = state["monitor"]
        if mgr:
            st = mgr.snapshot()
            devs = st.get("devices", [])
            okn = sum(1 for d in devs if d["connected"])
            if okn:
                set_status("接続 %d/%d   %s" % (okn, len(devs),
                           "   ".join(("●" if d["connected"] else "○") + " " + d["name"]
                                      for d in devs)),
                           FG if okn == len(devs) else "#e0a020")
                dev = next((d for d in devs if d.get("running")), devs[0])
                run = dev.get("running") or []
                detail.configure(text=("%s  %s  REMAIN %s" % (
                    dev.get("workspace") or "", run[0]["name"], fmt_hms(run[0]["remain"]))
                    if run else "%s  待機中" % (dev.get("workspace") or "")))
            else:
                set_status("● 未接続  %s" % (st.get("error") or ""), "#e03a2f")
                detail.configure(text="QLab の Workspace Settings › Network › OSC Access を確認")
            for d in devs:
                k = "err_" + d["id"]
                if state.get(k) != d["error"]:
                    state[k] = d["error"]
                    if d["error"]:
                        log("%s: %s" % (d["name"], d["error"]))
            cp = state.get("comp")
            if cp and state.get("comp_status") != cp.status:
                state["comp_status"] = cp.status
                log("Companion: " + cp.status)
            tip.configure(text="モニター画面: http://localhost:%d/   (他端末からは http://%s:%d/)%s"
                          % (state["web_port"], local_ip() or "<このPCのIP>", state["web_port"],
                             "   |  Companion: " + cp.status if cp else ""))
        root.after(500, tick)

    root.after(300, tick)
    if host_var.get().strip():
        root.after(400, lambda: start(open_browser=True))
    root.mainloop()
    os._exit(0)
    return True


# ---------------------------------------------------------------- main
def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="SW QLAB MONITOR v" + VERSION)
    ap.add_argument("--host", default=None,
                    help="QLab の IP。カンマ区切りで複数可 (例 192.168.0.30,192.168.0.31)")
    ap.add_argument("--port", type=int, default=cfg.get("port", DEFAULT_OSC_PORT),
                    help="QLab の OSC ポート (既定 53000)")
    ap.add_argument("--passcode", default=cfg.get("passcode", ""), help="OSC パスコード")
    ap.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    ap.add_argument("--rate", type=float, default=POLL_HZ, help="QLab を読む頻度 Hz (既定 30)")
    ap.add_argument("--source", metavar="IP", help="このPCのどのNIC（IP）から出るか指定")
    ap.add_argument("--companion", metavar="IP[:PORT]",
                    help="Companion のカスタム変数に REMAIN 等を送る (既定ポート 8000)")
    ap.add_argument("--companion-rate", type=float, default=4.0)
    ap.add_argument("--companion-test", metavar="IP[:PORT]",
                    help="Companion への書き込みを1回だけ試して結果を表示")
    ap.add_argument("--try", dest="try_target", metavar="IP[:PORT]",
                    help="指定の QLab に接続できるか診断して終了")
    ap.add_argument("--discover", action="store_true",
                    help="Bonjour (_qlab._tcp.local.) でLAN上のQLabを検索して終了")
    ap.add_argument("--demo", action="store_true", help="内蔵ダミー QLab で動作確認")
    ap.add_argument("--gui", action="store_true", help="設定ウィンドウ付きで起動")
    ap.add_argument("--console", action="store_true", help="ウィンドウ無しで起動")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    globals()["POLL_HZ"] = max(1.0, min(60.0, args.rate))

    if args.companion_test:
        chost, _, cport = str(args.companion_test).partition(":")
        cport = int(cport) if cport.isdigit() else COMPANION_PORT
        print("SW QLAB MONITOR v%s  Companion 書き込みテスト  %s:%d" % (VERSION, chost, cport))
        ok, e = tcp_open(chost, cport, timeout=3.0)
        print("TCP 接続: %s" % ("OK" if ok else "NG (%s)" % e))
        if not ok:
            print("→ Companion が起動しているか、IP とポート(既定8000)を確認してください。")
            return
        try:
            conn = http.client.HTTPConnection(chost, cport, timeout=3.0)
            conn.request("POST", "/api/custom-variable/qlab_remain/value?value=" +
                         urllib.parse.quote("00:00:TEST"))
            resp = conn.getresponse(); body = resp.read().decode("utf-8", "replace")[:200]
            print("HTTP %d %s   応答: %s" % (resp.status, resp.reason, body.strip() or "(空)"))
            conn.close()
            if resp.status < 300:
                print("→ 書き込めました。Companion の qlab_remain が 00:00:TEST なら成功です。")
            elif resp.status == 404:
                print("→ 変数 qlab_remain が Companion にありません。")
                print("  Variables › Custom Variables の一番下 Create custom variable で作成を。")
        except (OSError, http.client.HTTPException) as e:
            print("送信エラー: %s" % e)
        return

    if args.discover:
        print("SW QLAB MONITOR v%s  Bonjour検索  (_qlab._tcp.local., 3秒)" % VERSION)
        results = mdns_discover_qlab(timeout=3.0)
        if not results:
            print("見つかりませんでした。")
            print("→ QLab の Workspace Settings › Network › OSC Access で OSC が有効か、")
            print("  Bonjour/mDNSがネットワークでブロックされていないか確認してください。")
        else:
            for r in results:
                print("  %-24s %s:%d" % (r["name"], r["host"], r["port"]))
            print("\n→ この IP:ポートを --host / --port にそのまま使えます。")
        return

    if args.try_target:
        ip, _, ps = str(args.try_target).partition(":")
        pnum = int(ps) if ps.isdigit() else DEFAULT_OSC_PORT
        print("SW QLAB MONITOR v%s  接続診断  %s:%d" % (VERSION, ip, pnum))
        ifs = os_interfaces()
        if ifs:
            print("このPCのネットワークアダプタ:")
            for name, lip in ifs:
                same = " ← QLab と同じセグメント" if lip.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0] else ""
                print("  %-40s %s%s" % (name, lip, same))
        mine = local_ips()
        if (not ip.startswith("127.") and
                not [x for x in mine if x.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]]):
            print("\n!! このPCに %s.x のIPがありません。QLab と別セグメントです。"
                  % ip.rsplit(".", 1)[0])
            print("   同じスイッチに有線で挿し、そのアダプタに %s.x を設定してください。\n"
                  % ip.rsplit(".", 1)[0])
        ok, e = tcp_open(ip, pnum, timeout=5.0)
        print("TCP 接続: %s" % ("OK" if ok else "NG (%s)" % e))
        if not ok:
            print("→ QLab が起動しているか、Workspace Settings › Network › OSC Access で")
            print("  「Allow OSC Connections」が有効か、OSC Listening Port が %d か確認を。" % pnum)
            return
        c = QLabClient(ip, pnum, args.passcode, timeout=5.0)
        try:
            c.connect()
            print("QLab 応答: OK  version %s / workspace %s" % (c.version, c.workspace_name))
            run = c.call("/workspace/%s/runningOrPausedCues/shallow" % c.workspace) or []
            print("再生中のキュー: %d" % len(run))
            print("→ この値をそのまま入力欄に入れれば使えます: %s:%d" % (ip, pnum))
        except (QLabError, OSError) as ex:
            print("QLab 応答: なし (%s)" % ex)
            print("→ パスコードが設定されている場合は --passcode で指定してください。")
        finally:
            c.close()
        return

    host = args.host or cfg.get("host") or ""
    port = args.port
    if args.demo:
        DemoQLab(port=args.port).start()
        host = "127.0.0.1"
        time.sleep(0.3)

    want_gui = (args.gui or (len(sys.argv) == 1 and not args.console)) and not args.demo
    if want_gui:
        if run_gui(host, port, args.web_port) is not False:
            return
        message_box("SW QLAB MONITOR",
                    "この Python には tkinter が入っていないため、設定ウィンドウは開けません。\n"
                    "代わりにブラウザのモニター画面を開きます。")
        args.no_browser = False

    targets = parse_targets(host or "127.0.0.1", port)
    mgr = DeviceManager(targets, passcode=args.passcode, source_ip=args.source)
    Handler.monitor = mgr
    comp = args.companion or cfg.get("companion")
    if comp:
        chost, _, cport = str(comp).partition(":")
        CompanionPush(mgr, chost, int(cport) if cport.isdigit() else COMPANION_PORT,
                      rate=args.companion_rate).start()
        print(" Companion : %s:%s へ変数送信" % (chost, cport or COMPANION_PORT))

    httpd = ThreadingHTTPServer(("0.0.0.0", args.web_port), Handler)
    url = "http://localhost:%d/" % args.web_port
    print("=" * 58)
    print(" SW QLAB MONITOR  v%s" % VERSION)
    print(" QLab     : %s %s" % (", ".join("%s:%d" % t for t in targets),
                                 "(DEMO)" if args.demo else ""))
    print(" UI       : %s   (同一LANの端末からは http://<このPCのIP>:%d/)" % (url, args.web_port))
    print(" 終了     : Ctrl+C")
    print("=" * 58)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        for m in mgr.monitors:
            m.stop_flag.set()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        message_box("SW QLAB MONITOR - 起動できません", traceback.format_exc()[-1200:])
        raise
