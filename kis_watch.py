# -*- coding: utf-8 -*-
"""
국장 실시간 돌파 감시기 (KIS 웹소켓)

아침 리스트(results/watchlist_kr.csv)를 읽어 종목별 진입가·손절가를 세팅하고,
KIS 실시간 체결가(H0STCNT0)를 받아 돌파/익절/이탈 시점에 텔레그램으로 알린다.

  · 개장까지 대기 -> 09:00~15:20 감시 -> 자동 종료
  · 연결이 끊기면 자동 재접속 후 구독 복구
  · 같은 이벤트는 한 번만 알림

환경변수: KIS_APP_KEY, KIS_APP_SECRET, TG_TOKEN, TG_CHAT
실행: python kis_watch.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import websocket

KST = ZoneInfo("Asia/Seoul")
REST = "https://openapi.koreainvestment.com:9443"
WS = "ws://ops.koreainvestment.com:21000"
TR_TRADE = "H0STCNT0"          # 국내주식 실시간 체결
OPEN_T = dtime(9, 0)
CLOSE_T = dtime(15, 20)
MAX_SUB = 40                    # 세션당 등록 상한 (여유분 확보)


def tg(msg):
    tok, chat = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if not tok or not chat:
        print("[TG 미설정]", msg[:200], flush=True)
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data),
            timeout=15).read()
    except Exception as ex:
        print("텔레그램 실패:", ex, flush=True)


def now():
    return datetime.now(KST)


def get_approval(key, secret):
    """웹소켓 접속키 발급"""
    r = requests.post(f"{REST}/oauth2/Approval",
                      headers={"content-type": "application/json"},
                      data=json.dumps({"grant_type": "client_credentials",
                                       "appkey": key, "secretkey": secret}),
                      timeout=15)
    r.raise_for_status()
    return r.json()["approval_key"]


def load_watchlist(path="results/watchlist_kr.csv"):
    """아침 리스트에서 대기 종목만 추출"""
    if not os.path.exists(path):
        return []
    df = pd.read_csv(path, dtype={"code": str})
    df = df[df["status"].isin(["대기", "보류"])]
    if df.empty:
        return []
    df["code"] = df["code"].astype(str).str.zfill(6)
    # 같은 종목은 손절폭 좁은 것 하나만
    df = df.sort_values("stop_pct").drop_duplicates("code")
    out = []
    for _, r in df.head(MAX_SUB).iterrows():
        out.append(dict(code=r["code"], name=str(r["name"]),
                        entry=float(r["entry"]), stop=float(r["stop1"]),
                        tgt=float(r["tgt"]), pattern=str(r.get("pattern", "")),
                        fired=False, hit=False, dropped=False))
    return out


def sub_msg(approval, code, subscribe=True):
    return json.dumps({
        "header": {"approval_key": approval, "custtype": "P",
                   "tr_type": "1" if subscribe else "2",
                   "content-type": "utf-8"},
        "body": {"input": {"tr_id": TR_TRADE, "tr_key": code}}})


def parse_trade(raw):
    """체결 데이터 파싱 -> (종목코드, 현재가, 체결시각)"""
    # 형식: 0|H0STCNT0|001|005930^123032^71900^...
    parts = raw.split("|")
    if len(parts) < 4 or parts[1] != TR_TRADE:
        return None
    f = parts[3].split("^")
    if len(f) < 3:
        return None
    try:
        return f[0], float(f[2]), f[1]
    except (ValueError, IndexError):
        return None


def main():
    key = os.environ.get("KIS_APP_KEY", "")
    secret = os.environ.get("KIS_APP_SECRET", "")
    if not key or not secret:
        tg("[국장 감시] KIS 키가 없습니다 - 종료")
        print("KIS_APP_KEY / KIS_APP_SECRET 미설정")
        return 1

    wl = load_watchlist()
    if not wl:
        tg("[국장 감시] 오늘 감시 대상 0건 - 종료")
        print("감시 대상 없음")
        return 0

    today = now().date()
    open_at = datetime.combine(today, OPEN_T, KST)
    close_at = datetime.combine(today, CLOSE_T, KST)
    if now() >= close_at:
        tg("[국장 감시] 이미 장 마감 - 종료")
        return 0

    head = [f"[국장 실시간 감시] {now():%m/%d %H:%M} KST",
            f"감시 {len(wl)}종목 · 개장 09:00 · 마감 15:20", ""]
    for k, w in enumerate(wl[:12], 1):
        head.append(f"{k}. {w['name'][:14]} {w['entry']:,.0f} 돌파시 알림 "
                    f"(손절 {w['stop']:,.0f})")
    if len(wl) > 12:
        head.append(f"... 외 {len(wl) - 12}종목")
    tg("\n".join(head))
    print("\n".join(head), flush=True)

    # 개장까지 대기
    while now() < open_at:
        left = (open_at - now()).total_seconds()
        print(f"개장까지 {int(left / 60)}분 대기", flush=True)
        time.sleep(min(left, 300))

    watch = {w["code"]: w for w in wl}
    approval = get_approval(key, secret)
    print(f"[{now():%H:%M}] approval 발급 완료", flush=True)

    fired = hit = dropped = 0
    reconnects = 0

    while now() < close_at:
        try:
            ws = websocket.create_connection(WS, timeout=30)
            for code in watch:
                ws.send(sub_msg(approval, code))
                time.sleep(0.06)
            print(f"[{now():%H:%M}] 구독 {len(watch)}종목 "
                  f"(재접속 {reconnects}회)", flush=True)

            ws.settimeout(60)
            while now() < close_at:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    continue
                if raw[0] not in ("0", "1"):          # PINGPONG 등 제어 메시지
                    try:
                        j = json.loads(raw)
                        if j.get("header", {}).get("tr_id") == "PINGPONG":
                            ws.send(raw)
                    except Exception:
                        pass
                    continue

                p = parse_trade(raw)
                if not p:
                    continue
                code, px, tstr = p
                w = watch.get(code)
                if not w or w["dropped"]:
                    continue
                hhmm = now().strftime("%H:%M")

                if (not w["fired"]) and px <= w["stop"]:
                    w["dropped"] = True
                    dropped += 1
                    tg(f"[이탈] {w['name']} ({code})\n"
                       f"현재 {px:,.0f} · 손절선 {w['stop']:,.0f} 이탈\n감시 해제")
                    continue

                if not w["fired"] and px >= w["entry"]:
                    w["fired"] = True
                    fired += 1
                    over = (px / w["entry"] - 1) * 100
                    chase = "진입 가능" if over <= 0.5 else "추격 금지 · 눌림 대기"
                    tg(f"[돌파] {w['name']} ({code})  [{w['pattern']}]\n"
                       f"현재 {px:,.0f} (기준 {w['entry']:,.0f} 대비 {over:+.2f}%)\n"
                       f"→ {chase}\n"
                       f"익절 {w['tgt']:,.0f} · 손절 {w['stop']:,.0f}\n{hhmm} KST")
                    continue

                if w["fired"] and not w["hit"] and px >= w["tgt"]:
                    w["hit"] = True
                    hit += 1
                    tg(f"[익절] {w['name']} ({code})\n"
                       f"현재 {px:,.0f} · 목표 {w['tgt']:,.0f} 도달\n"
                       f"절반 정리 후 손절을 진입가({w['entry']:,.0f})로\n{hhmm} KST")

            ws.close()
            break

        except Exception as ex:
            reconnects += 1
            print(f"[{now():%H:%M}] 연결 오류: {ex} - 10초 후 재접속", flush=True)
            if reconnects == 1:
                tg(f"[국장 감시] 연결 끊김 - 자동 재접속 시도")
            if reconnects > 30:
                tg("[국장 감시] 재접속 30회 초과 - 종료")
                break
            time.sleep(10)

    tg(f"[국장 감시 종료] {now():%H:%M} KST\n"
       f"돌파 {fired}건 · 익절도달 {hit}건 · 이탈 {dropped}건 · 재접속 {reconnects}회")
    print("종료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
