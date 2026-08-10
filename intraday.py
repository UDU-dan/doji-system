# -*- coding: utf-8 -*-
"""
장중 돌파 감시기 (서머타임 자동 반영)

시작 시 관심종목을 뽑고, 개장 시각까지 대기한 뒤
장 마감까지 N분마다 현재가를 확인해 진입가 돌파를 알린다.

  · 서머타임은 zoneinfo 로 자동 처리 (하드코딩 없음)
  · 휴장일이면 시세가 안 들어오므로 자동 종료
  · 같은 종목은 한 번만 알림 / 손절 이탈시 감시 해제

실행:
  python intraday.py --market us
  python intraday.py --market kr

주의: yfinance 시세는 15~20분 지연될 수 있음.
"""
import argparse
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import scan as SC

KST = ZoneInfo("Asia/Seoul")

MARKET = {
    "us": dict(tz=ZoneInfo("America/New_York"), open=dtime(9, 30), close=dtime(16, 0),
               label="미장", cur="$"),
    "kr": dict(tz=ZoneInfo("Asia/Seoul"), open=dtime(9, 0), close=dtime(15, 20),
               label="국장", cur=""),
}


def tg(msg):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if not tok or not chat:
        print("[TG 미설정]", msg[:300])
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as ex:
        print("텔레그램 실패:", ex)


def now_kst():
    return datetime.now(KST)


def session_bounds(market):
    """오늘(현지 기준) 개장·마감 시각을 tz-aware 로 반환. 서머타임 자동 반영."""
    m = MARKET[market]
    local = datetime.now(m["tz"])
    if local.time() >= m["close"]:          # 이미 마감했으면 다음 거래일
        local += timedelta(days=1)
    while local.weekday() >= 5:             # 주말 건너뛰기
        local += timedelta(days=1)
    o = local.replace(hour=m["open"].hour, minute=m["open"].minute,
                      second=0, microsecond=0)
    c = local.replace(hour=m["close"].hour, minute=m["close"].minute,
                      second=0, microsecond=0)
    return o, c


def build_watchlist(market, universe, top_n):
    print(f"[준비] {market.upper()} 유니버스 {universe} 수집...", flush=True)
    uni = SC.universe_kr(universe) if market == "kr" else SC.universe_us(universe)
    res = []
    if market == "kr":
        from concurrent.futures import ThreadPoolExecutor, as_completed
        end = datetime.today()
        s = (end - timedelta(days=400)).strftime("%Y-%m-%d")
        e = end.strftime("%Y-%m-%d")
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(SC.fetch_kr, c, s, e): (c, n, m) for c, n, m in uni}
            for f in as_completed(futs):
                c, n, m = futs[f]
                try:
                    r = SC.evaluate(f.result(), c, n, market, m)
                    if r:
                        res.append(r)
                except Exception:
                    pass
    else:
        meta = {c: n for c, n, _ in uni}
        syms = list(meta.keys())
        CH = 150
        for k in range(0, len(syms), CH):
            for t, d in SC.fetch_us_batch(syms[k:k + CH]).items():
                try:
                    r = SC.evaluate(d, t, meta.get(t, t), market, None)
                    if r:
                        res.append(r)
                except Exception:
                    pass
            print(f"      {min(k + CH, len(syms))}/{len(syms)}", flush=True)

    if not res:
        return []
    df = pd.DataFrame(res)
    df["_g"] = df["grade"].map({"A": 0, "B": 1, "C": 2}).fillna(3)
    df = df.sort_values(["_g", "score"], ascending=[True, False])
    df = df.drop(columns=["_g"]).head(top_n).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df.to_dict("records")


def quotes_us(tickers):
    import yfinance as yf
    out = {}
    try:
        raw = yf.download(tickers, period="1d", interval="5m", group_by="ticker",
                          threads=True, progress=False, auto_adjust=False)
    except Exception as ex:
        print("시세 실패:", ex)
        return out
    for t in tickers:
        try:
            d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            d = d.dropna()
            if len(d) == 0:
                continue
            last = d.iloc[-1]
            out[t] = (float(last["Close"]), float(last["High"]),
                      float(last["Volume"]), float(d["Volume"].mean()))
        except Exception:
            pass
    return out


def quotes_kr(codes):
    out = {}
    s = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    e = datetime.today().strftime("%Y-%m-%d")
    for c in codes:
        try:
            d = SC.fetch_kr(c, s, e)
            if d is None or len(d) == 0:
                continue
            d.columns = [str(x).capitalize() for x in d.columns]
            last = d.iloc[-1]
            out[c] = (float(last["Close"]), float(last["High"]),
                      float(last["Volume"]), float(d["Volume"].mean()))
        except Exception:
            pass
        time.sleep(0.15)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="us")
    ap.add_argument("--universe", type=int, default=1500)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--max-minutes", type=int, default=330,
                    help="러너 제한 대비 최대 실행 시간")
    a = ap.parse_args()
    mkt, m = a.market, MARKET[a.market]
    cur, label = m["cur"], m["label"]

    hard_stop = datetime.now(KST) + timedelta(minutes=a.max_minutes)
    open_at, close_at = session_bounds(mkt)
    print(f"[시각] 지금 {now_kst():%m/%d %H:%M} KST", flush=True)
    print(f"[세션] 개장 {open_at:%m/%d %H:%M %Z} = {open_at.astimezone(KST):%H:%M} KST", flush=True)
    print(f"[세션] 마감 {close_at:%H:%M %Z} = {close_at.astimezone(KST):%H:%M} KST", flush=True)

    wl = build_watchlist(mkt, a.universe, a.top)
    if not wl:
        tg(f"[{label}] 감시 대상 0건 - 종료")
        return

    head = [f"[{label} 장중 감시] {now_kst():%m/%d %H:%M} KST 준비 완료",
            f"개장 {open_at.astimezone(KST):%H:%M} · 마감 {close_at.astimezone(KST):%H:%M} KST",
            f"감시 {len(wl)}종목 · {a.interval}분 간격", ""]
    for r in wl[:12]:
        head.append(f"{r['rank']}. {r['name'][:16]} <{r['grade']}> "
                    f"{cur}{r['entry']:,} 돌파시 알림 (손절 {cur}{r['stop1']:,})")
    if len(wl) > 12:
        head.append(f"... 외 {len(wl) - 12}종목")
    tg("\n".join(head))
    print("\n".join(head), flush=True)

    # 개장까지 대기
    while datetime.now(KST) < open_at and datetime.now(KST) < hard_stop:
        wait = min((open_at - datetime.now(KST)).total_seconds(), 300)
        if wait <= 0:
            break
        print(f"개장까지 {int((open_at - datetime.now(KST)).total_seconds() / 60)}분 대기",
              flush=True)
        time.sleep(wait)

    watch = {r["code"]: r for r in wl}
    fired, dropped = set(), set()
    rounds, empty = 0, 0

    while datetime.now(KST) < min(close_at, hard_stop):
        rounds += 1
        alive = [c for c in watch if c not in fired and c not in dropped]
        if not alive:
            tg(f"[{label}] 감시 대상 모두 소진 - 종료")
            break

        q = quotes_us(alive) if mkt == "us" else quotes_kr(alive)
        hhmm = now_kst().strftime("%H:%M")
        print(f"[{hhmm}] {rounds}회차 · 감시 {len(alive)} · 시세 {len(q)}", flush=True)

        if not q:
            empty += 1
            if empty >= 3 and rounds <= 4:
                tg(f"[{label}] 시세 없음 - 휴장일로 판단, 종료")
                break
        else:
            empty = 0

        for code, (px, hi, vol, vavg) in q.items():
            r = watch[code]
            entry, stop, tgt = r["entry"], r["stop1"], r["tgt1"]

            if px <= stop:
                dropped.add(code)
                tg(f"[무효] {r['name']} ({code})\n"
                   f"현재 {cur}{px:,.2f} · 손절선 {cur}{stop:,} 이탈\n감시 해제")
                continue

            if px >= entry:
                fired.add(code)
                over = (px / entry - 1) * 100
                vx = (vol / vavg) if vavg > 0 else 0
                chase = ("진입 가능" if over <= SC.S["CHASE_PCT"]
                         else "추격 금지 · 눌림 대기")
                tg(f"[돌파] {r['name']} ({code})  {r['score']}점 <{r['grade']}>\n"
                   f"현재 {cur}{px:,.2f} (기준 {cur}{entry:,} 대비 {over:+.2f}%)\n"
                   f"→ {chase}\n"
                   f"익절 {cur}{tgt:,} · 손절 {cur}{stop:,}\n"
                   f"5분 거래량 {vx:.1f}배 · {hhmm} KST")

        left = (min(close_at, hard_stop) - datetime.now(KST)).total_seconds()
        if left <= 0:
            break
        time.sleep(min(a.interval * 60, left))

    tg(f"[{label} 감시 종료] {now_kst():%H:%M} KST\n"
       f"{rounds}회 확인 · 돌파 {len(fired)}건 · 무효 {len(dropped)}건")
    print("종료", flush=True)


if __name__ == "__main__":
    sys.exit(main())
