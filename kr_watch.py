# -*- coding: utf-8 -*-
"""
국장 통합 감시기 (자동 리스트 + 지정가)

KIS 웹소켓 연결 하나로 두 종류를 함께 감시한다.

  [자동]  daily.py 가 뽑은 관심종목 (results/watchlist_kr.csv)
  [지정]  텔레그램으로 직접 등록한 가격 (manual_watch.json)

알림은 모두 ALERT_TOKEN 봇(그룹)으로 전송.
텔레그램에서 자연어로 지정가를 등록·수정·해제할 수 있다.

환경변수: ALERT_TOKEN, ALERT_CHAT_ID, KIS_APP_KEY, KIS_APP_SECRET
실행: python kr_watch.py
"""
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import websocket

import alert_bot as AB          # 명령 해석·종목검색·저장 로직 재사용

KST = ZoneInfo("Asia/Seoul")
REST = "https://openapi.koreainvestment.com:9443"
WS_KR = "ws://ops.koreainvestment.com:21000"
TR_TRADE = "H0STCNT0"
OPEN_T = dtime(9, 0)
CLOSE_T = dtime(15, 20)
MAX_SUB = 38                    # KIS 세션 한도(41) 여유분
AUTO_CSV = "results/watchlist_kr.csv"
RESET_PCT = 0.3          # 저항선 아래로 이 % 이상 빠져야 재알림 허용
COOLDOWN_SEC = 900       # 같은 종목 재알림 최소 간격 (15분)
MAX_REPEAT = 3           # 같은 종목 돌파 알림 하루 최대 횟수

CLEANUP = {"ws": None, "approval": None, "codes": set()}


# ══════════ 공통 ══════════
def tg(msg):
    AB.tg(msg)


def now():
    return datetime.now(KST)


def sub_msg(approval, code, on=True):
    return json.dumps({
        "header": {"approval_key": approval, "custtype": "P",
                   "tr_type": "1" if on else "2", "content-type": "utf-8"},
        "body": {"input": {"tr_id": TR_TRADE, "tr_key": code}}})


def release_all(reason=""):
    ws, ap = CLEANUP.get("ws"), CLEANUP.get("approval")
    codes = list(CLEANUP.get("codes") or [])
    if not ws or not ap or not codes:
        return
    n = 0
    for c in codes:
        try:
            ws.send(sub_msg(ap, c, on=False))
            n += 1
            time.sleep(0.05)
        except Exception:
            break
    CLEANUP["codes"] = set()
    print(f"[정리] 구독 해제 {n}/{len(codes)}종목 {reason}", flush=True)
    try:
        ws.close()
    except Exception:
        pass
    CLEANUP["ws"] = None


def _on_signal(signum, frame):
    release_all(f"(신호 {signum})")
    sys.exit(0)


for _s in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_s, _on_signal)
    except Exception:
        pass


def get_approval(key, secret):
    r = requests.post(f"{REST}/oauth2/Approval",
                      headers={"content-type": "application/json"},
                      data=json.dumps({"grant_type": "client_credentials",
                                       "appkey": key, "secretkey": secret}),
                      timeout=15)
    r.raise_for_status()
    return r.json()["approval_key"]


def parse_trade(raw):
    p = raw.split("|")
    if len(p) < 4 or p[1] != TR_TRADE:
        return None
    f = p[3].split("^")
    try:
        return f[0], float(f[2])
    except (ValueError, IndexError):
        return None


# ══════════ 감시 대상 ══════════
def load_auto():
    """daily.py 결과에서 대기·보류 종목을 가져온다"""
    if not os.path.exists(AUTO_CSV):
        return {}
    try:
        df = pd.read_csv(AUTO_CSV, dtype={"code": str})
    except Exception as ex:
        print("자동 리스트 읽기 실패:", ex, flush=True)
        return {}
    if "status" in df.columns:
        df = df[df["status"].isin(["대기", "보류"])]
    if df.empty:
        return {}
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.sort_values("stop_pct").drop_duplicates("code")
    out = {}
    for _, r in df.iterrows():
        out[r["code"]] = dict(
            name=str(r["name"]), price=float(r["entry"]),
            stop=float(r["stop1"]), tgt=float(r["tgt"]),
            pattern=str(r.get("pattern", "")), kind="자동",
            state="대기", notified=[], last_px=None, last_at=None, events=[])
    return out


def load_manual():
    """지정가 등록분 (국장만)"""
    out = {}
    for c, v in AB.load_store().items():
        if v.get("market", "kr") != "kr":
            continue
        w = dict(v)
        w["kind"] = "지정"
        w.setdefault("stop", None)
        w.setdefault("tgt", None)
        out[c] = w
    return out


def build_targets():
    """자동 + 지정 병합. 같은 종목이면 지정가 우선."""
    t = load_auto()
    for c, v in load_manual().items():
        t[c] = v                       # 지정가가 덮어씀
    return dict(list(t.items())[:MAX_SUB])


# ══════════ 판정 ══════════
def judge(code, v, px):
    """알림 문자열. 없으면 None (보류·이탈은 알림 없이 상태만 갱신)"""
    target = float(v["price"])
    name, kind = v["name"], v.get("kind", "지정")
    icon = "⭐" if kind == "지정" else "🔵"
    pat = f" · {v['pattern']}" if v.get("pattern") else ""
    notified = v.get("notified", [])
    diff = (px - target) / target * 100
    ts = now().strftime("%H:%M:%S")
    f = lambda x: f"{x:,.0f}"

    v["last_px"] = px
    v["last_at"] = now().strftime("%m/%d %H:%M:%S")
    stop = v.get("stop")
    tgt = v.get("tgt")
    msg = None

    # ── 손절선 이탈 : 상태만 갱신, 알림 없음 ──
    if stop and px <= float(stop):
        if v.get("state") != "이탈":
            v["state"] = "이탈"
        return None

    # ── 지정가 아래 ──
    if px < target:
        if v.get("state") not in ("보류", "이탈"):
            v["state"] = "보류"
        # 확실히(0.3% 이상) 빠졌을 때만 재알림 허용
        if diff <= -RESET_PCT:
            v["notified"] = []
        return None

    if v.get("state") in ("보류", "이탈") and not notified:
        notified = []                      # 되돌림 후 복귀

    # ── 재알림 제한 ──
    last_t = float(v.get("last_alert_ts") or 0)
    cnt = int(v.get("alert_cnt") or 0)
    can_alert = (time.time() - last_t >= COOLDOWN_SEC) and cnt < MAX_REPEAT

    if px == target:
        v["state"] = "도달"
        if "도달" not in notified and can_alert:
            notified.append("도달")
            msg = (f"{icon} ⚡ 도 달 ⚡ {icon}\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"{name} ({code}){pat}\n"
                   f"지정 {f(target)} = 현재 {f(px)}\n"
                   f"동일가격 · 돌파 대기\n"
                   f"{ts}")
    else:
        v["state"] = "돌파"
        if "돌파" not in notified and can_alert:
            notified.append("돌파")
            extra = (f"\n익절 {f(float(tgt))} · 손절 {f(float(stop))}"
                     if tgt and stop else "")
            rep = f"  ({cnt + 1}회차)" if cnt else ""
            msg = (f"{icon} 🚨🚨 돌 파 🚨🚨 {icon}{rep}\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"{name} ({code}){pat}\n"
                   f"{f(target)} → {f(px)}  ({diff:+.2f}%)"
                   f"{extra}\n"
                   f"{ts}")
        elif tgt and px >= float(tgt) and "익절" not in notified:
            notified.append("익절")
            msg = (f"{icon} 💰 익 절 💰 {icon}\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"{name} ({code}){pat}\n"
                   f"현재 {f(px)} · 목표 {f(float(tgt))} 도달\n"
                   f"절반 정리 → 손절을 {f(target)}로\n"
                   f"{ts}")
        elif (not tgt) and diff >= AB.BREAK_EXTRA and "돌파+" not in notified:
            notified.append("돌파+")
            msg = (f"{icon} 📈 추가상승\n"
                   f"{name} ({code}) {f(target)} → {f(px)} ({diff:+.2f}%)\n"
                   f"진입 구간 벗어남 · 눌림 대기\n{ts}")

    v["notified"] = notified
    if msg:
        v["last_alert_ts"] = time.time()
        if "돌 파" in msg:
            v["alert_cnt"] = cnt + 1
        ev = v.setdefault("events", [])
        head = ("도달" if "도 달" in msg else
                "돌파" if "돌 파" in msg else
                "익절" if "익 절" in msg else "추가상승")
        ev.append(f"{now():%m/%d %H:%M:%S} {head} {f(px)}")
        v["events"] = ev[-8:]
    return msg


# ══════════ 요약 ══════════
def summary(targets, title):
    auto = {k: v for k, v in targets.items() if v.get("kind") == "자동"}
    man = {k: v for k, v in targets.items() if v.get("kind") == "지정"}
    L = [title]
    for lbl, grp, ic in (("자동", auto, "🔵"), ("지정", man, "⭐")):
        if not grp:
            continue
        L.append(f"\n{ic} [{lbl} {len(grp)}종목]")
        for c, v in grp.items():
            line = f"  {ic} {v['name'][:14]} ({c}) {v['price']:,.0f}원 · {v.get('state','대기')}"
            if v.get("pattern"):
                line += f" · {v['pattern'][:18]}"
            L.append(line)
            if v.get("last_px"):
                d = (v["last_px"] - v["price"]) / v["price"] * 100
                L.append(f"     현재 {v['last_px']:,.0f}원 ({d:+.2f}%) {v.get('last_at','')}")
    if not auto and not man:
        L.append("\n감시 대상 없음")
    return "\n".join(L)


# ══════════ 텔레그램 스레드 ══════════
def bot_loop(targets, stop_flag):
    pending = {}
    offset = 0
    _, offset = AB.tg_updates(0)
    while not stop_flag["stop"]:
        try:
            msgs, offset = AB.tg_updates(offset)
            for m in msgs:
                low = m.strip()
                # 통합 현황은 여기서 직접 처리
                if any(w in low for w in AB.STATUS_WORDS) and not any(
                        ch.isdigit() for ch in low):
                    tg(summary(targets, f"[현재 상황] {now():%m/%d %H:%M:%S} KST"))
                    continue
                resp = AB.handle(m, {}, "kr", pending)
                if resp:
                    tg(resp)
                    # 등록/해제가 반영되도록 지정가분 다시 읽기
                    if resp.startswith(("등록 완료", "해제 완료")):
                        for c, v in load_manual().items():
                            if c not in targets or targets[c].get("kind") == "지정":
                                targets[c] = v
                        for c in [k for k, v in targets.items()
                                  if v.get("kind") == "지정" and
                                  c not in load_manual()]:
                            targets.pop(c, None)
        except Exception as ex:
            print("봇 루프 오류:", ex, flush=True)
        time.sleep(1)


# ══════════ 메인 ══════════
def main():
    key = os.environ.get("KIS_APP_KEY", "")
    secret = os.environ.get("KIS_APP_SECRET", "")
    if not key or not secret:
        tg("[국장 통합감시] KIS 키 없음 - 종료")
        return 1

    targets = build_targets()
    AB.LIVE.clear()
    AB.LIVE.update({k: v for k, v in targets.items() if v.get("kind") == "지정"})

    today = now().date()
    open_at = datetime.combine(today, OPEN_T, KST)
    close_at = datetime.combine(today, CLOSE_T, KST)
    if now() >= close_at:
        tg("[국장 통합감시] 이미 장 마감 - 종료")
        return 0

    AB.kr_list()
    AB.us_list()

    tg(summary(targets, f"[국장 통합감시] {now():%m/%d %H:%M} KST\n"
                        f"개장 09:00 · 마감 15:20")
       + "\n\n지정가 등록: 종목명 또는 코드 + 가격"
         "\n예) 삼성전자 71900 / GS건설 해제 / 현황")

    stop_flag = {"stop": False}
    threading.Thread(target=bot_loop, args=(targets, stop_flag),
                     daemon=True).start()

    while now() < open_at:
        time.sleep(2)

    approval = get_approval(key, secret)
    ws = None
    sub_sent, sub_ok, warned = {}, set(), set()
    fail = {}
    recv = [0]
    last_stat = last_retry = 0.0
    over = [False]

    def subscribe(code):
        if len(sub_sent) >= MAX_SUB:
            return
        ws.send(sub_msg(approval, code))
        sub_sent[code] = sub_sent.get(code, 0) + 1
        time.sleep(0.12)

    while now() < close_at:
        try:
            if ws is None:
                ws = websocket.create_connection(WS_KR, timeout=30)
                ws.settimeout(1)
                CLEANUP.update(ws=ws, approval=approval, codes=set())
                sub_sent.clear()
                sub_ok.clear()
                for c in list(targets):        # 잔여 등록 정리
                    try:
                        ws.send(sub_msg(approval, c, on=False))
                        time.sleep(0.05)
                    except Exception:
                        pass
                time.sleep(0.5)
                for c in list(targets):
                    subscribe(c)
                print(f"[{now():%H:%M}] 연결 · 구독요청 {len(sub_sent)}종목",
                      flush=True)

            for c in list(targets):
                if c not in sub_sent:
                    subscribe(c)
            for c in list(sub_sent):
                if c not in targets:
                    try:
                        ws.send(sub_msg(approval, c, on=False))
                    except Exception:
                        pass
                    sub_sent.pop(c, None)
                    sub_ok.discard(c)

            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None

            if raw:
                if raw[0] in ("0", "1"):
                    p = parse_trade(raw)
                    if p and p[0] in targets:
                        recv[0] += 1
                        sub_ok.add(p[0])
                        m = judge(p[0], targets[p[0]], p[1])
                        if m:
                            tg(m)
                            if targets[p[0]].get("kind") == "지정":
                                AB.merge_save(
                                    {p[0]: {k: v for k, v in targets[p[0]].items()
                                            if k not in ("kind", "stop", "tgt", "pattern")}})
                else:
                    try:
                        j = json.loads(raw)
                        h = j.get("header", {})
                        if h.get("tr_id") == "PINGPONG":
                            ws.send(raw)
                        else:
                            b = j.get("body", {}) or {}
                            k_ = h.get("tr_key")
                            m1 = str(b.get("msg1", "")).upper()
                            rt = str(b.get("rt_cd", ""))
                            print(f"[KIS] key={k_} rt={rt} msg={m1[:50]}", flush=True)
                            ok_w = ("SUBSCRIBE SUCCESS", "ALREADY IN SUBSCRIBE",
                                    "OPSP0000", "OPSP0002")
                            if k_ and (rt == "0" or any(w in m1 for w in ok_w)):
                                sub_ok.add(k_)
                                CLEANUP["codes"].add(k_)
                            elif k_:
                                fail[k_] = m1[:50]
                                if "MAX SUBSCRIBE" in m1:
                                    over[0] = True
                    except Exception:
                        pass

            if time.time() - last_retry > 60:
                last_retry = time.time()
                for c in [x for x in targets if x not in sub_ok]:
                    if sub_sent.get(c, 0) < 2:
                        subscribe(c)
                    elif c not in warned:
                        warned.add(c)
                        tg(f"[구독 실패] {targets[c]['name']} ({c})\n"
                           f"사유: {fail.get(c, '응답 없음')}")

            if time.time() - last_stat > 120:
                last_stat = time.time()
                miss = [c for c in targets if c not in sub_ok]
                print(f"[{now():%H:%M}] 체결 {recv[0]}건 · "
                      f"구독 {len(sub_ok)}/{len(targets)}"
                      + (f" · 미확인 {miss}" if miss else ""), flush=True)

        except Exception as ex:
            print("연결 오류:", ex, flush=True)
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(5)

    stop_flag["stop"] = True
    release_all("(정상 종료)")
    for c, v in targets.items():
        if v.get("kind") == "지정":
            AB.merge_save({c: {k: x for k, x in v.items()
                               if k not in ("kind", "stop", "tgt", "pattern")}})
    tg(summary(targets, f"[국장 통합감시 종료] {now():%H:%M} KST"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
