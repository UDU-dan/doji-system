# -*- coding: utf-8 -*-
"""
전략 비교 백테스트 v3

같은 데이터 · 같은 청산규칙 · 같은 비용으로 여러 진입신호를 비교한다.
무작위 진입(대조군)을 포함해서, 신호가 진짜 우위인지 아니면
청산규칙 덕분인지 구분한다.

전략
  S1 도지-하락후      : 5일 하락 후 도지 (기존 가설)
  S2 도지-추세눌림    : 상승추세 중 MA20 눌림에서 도지
  S3 20일신고가돌파   : 20일 최고종가 + 거래량 증가
  S4 거래량급증양봉   : 거래량 3배 + 양봉 + MA20 위
  S5 MA20눌림반등     : 상승추세 중 MA20 터치 후 전일고가 돌파
  BASE 무작위         : 20일마다 무작위 진입 (대조군)

실행: python backtest.py --months 36 --universe 500
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:
    yaml = None

P = dict(
    BODY_MAX_PCT=8.0, MIN_RANGE_ATR=0.5,
    DRAGON_LOWER=60.0, DRAGON_UPPER=15.0,
    FALL_DAYS=5, FALL_PCT=5.0, SUPPORT_ATR=0.25,
    VOL_MULT=1.2, MIN_VALUE_KRW=1_000_000_000, MIN_PRICE=1000,
    BREAK_BUFFER=0.002, STOP_ATR_BUF=0.15,
    MAX_STOP_PCT=99.0, RISK_PCT=0.5,
    TIME_STOP_DAYS=5, MAX_HOLD_DAYS=30,
    SELL_TAX_PCT=0.20, SLIPPAGE_PCT=0.10, HALF_EXIT_R=1.0,
)

_PF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.yml")
if yaml and os.path.exists(_PF):
    with open(_PF, encoding="utf-8") as _f:
        _l = yaml.safe_load(_f) or {}
    P.update({k: v for k, v in _l.items() if k in P})
    print("[설정] params.yml 적용됨")

RNG = np.random.default_rng(20260810)


def features(df):
    """공통 지표 계산"""
    d = df.copy()
    d.columns = [str(c).capitalize() for c in d.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(d.columns):
        return None
    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["ma20"] = c.rolling(20).mean()
    d["ma60"] = c.rolling(60).mean()
    d["low20"] = l.rolling(20).min()
    d["hh20"] = c.rolling(20).max()
    d["vol20"] = v.rolling(20).mean()
    d["value20"] = (c * v).rolling(20).mean()
    rng_ = (h - l).replace(0, np.nan)
    d["body_r"] = (c - o).abs() / rng_ * 100
    d["up_r"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / rng_ * 100
    d["dn_r"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng_ * 100
    d["vol_ratio"] = v / d["vol20"]
    d["fall5"] = (c.shift(P["FALL_DAYS"]) - c) / c.shift(P["FALL_DAYS"]) * 100
    d["liq"] = (d["value20"] >= P["MIN_VALUE_KRW"]) & (c >= P["MIN_PRICE"])
    d["is_doji"] = (d["body_r"] <= P["BODY_MAX_PCT"]) & (rng_ >= d["atr"] * P["MIN_RANGE_ATR"])
    d["is_grave"] = (d["up_r"] >= P["DRAGON_LOWER"]) & (d["dn_r"] <= P["DRAGON_UPPER"])
    d["uptrend"] = (c > d["ma60"]) & (d["ma60"] > d["ma60"].shift(10))
    return d


def signals(d, name):
    """전략별 신호 boolean Series"""
    c, h, l = d["Close"], d["High"], d["Low"]
    doji = d["is_doji"] & ~d["is_grave"]
    vol = d["vol_ratio"] >= P["VOL_MULT"]

    if name == "S1 도지-하락후":
        s = doji & vol & (d["fall5"] >= P["FALL_PCT"])
    elif name == "S2 도지-추세눌림":
        s = doji & vol & d["uptrend"] & (l <= d["ma20"] * 1.02) & (c > d["ma20"] * 0.97)
    elif name == "S3 20일신고가돌파":
        s = (c >= d["hh20"]) & (d["vol_ratio"] >= 1.5) & d["uptrend"]
    elif name == "S4 거래량급증양봉":
        s = (d["vol_ratio"] >= 3.0) & (c > d["Open"]) & (c > d["ma20"])
    elif name == "S5 MA20눌림반등":
        s = d["uptrend"] & (l.shift(1) <= d["ma20"].shift(1)) & (c > h.shift(1)) & vol
    elif name == "BASE 무작위":
        s = pd.Series(RNG.random(len(d)) < 0.05, index=d.index)
    else:
        raise ValueError(name)
    return (s & d["liq"]).fillna(False)


def simulate(arr, i, entry, stop, p):
    risk = entry - stop
    tgt1 = entry + risk * p["HALF_EXIT_R"]
    cost_r = (p["SELL_TAX_PCT"] + p["SLIPPAGE_PCT"]) / 100 * entry / risk
    pos, realized, cost, half = 1.0, 0.0, 0.0, False
    cur = stop
    n = len(arr)
    for j in range(i + 1, min(i + 1 + p["MAX_HOLD_DAYS"], n)):
        held = j - i
        o_, h_, l_, c_ = arr[j, 0], arr[j, 1], arr[j, 2], arr[j, 3]
        if o_ <= cur:
            realized += (o_ - entry) / risk * pos; cost += cost_r * pos
            return realized - cost, held
        if l_ <= cur:
            realized += (cur - entry) / risk * pos; cost += cost_r * pos
            return realized - cost, held
        if (not half) and h_ >= tgt1:
            realized += (tgt1 - entry) / risk * 0.5; cost += cost_r * 0.5
            pos, half = 0.5, True
            cur = max(entry, arr[j - 1, 2])
        if (not half) and held >= p["TIME_STOP_DAYS"]:
            realized += (c_ - entry) / risk * pos; cost += cost_r * pos
            return realized - cost, held
        if half:
            cur = max(cur, arr[j - 1, 2])
    j = min(i + p["MAX_HOLD_DAYS"], n - 1)
    realized += (arr[j, 3] - entry) / risk * pos; cost += cost_r * pos
    return realized - cost, j - i


def run_strategy(store, name):
    recs = []
    for code, (nm, d) in store.items():
        if d is None:
            continue
        sig = signals(d, name)
        idx = np.where(sig.values)[0]
        if len(idx) == 0:
            continue
        arr = d[["Open", "High", "Low", "Close", "Volume"]].values
        atr = d["atr"].values
        dates = d.index
        last_exit = -1
        for i in idx:
            if i + 1 >= len(d) or i < 60 or not np.isfinite(atr[i]):
                continue
            if i <= last_exit:          # 중복 진입 방지
                continue
            entry = arr[i, 1] * (1 + P["BREAK_BUFFER"])
            stop = arr[i, 2] - max(atr[i] * P["STOP_ATR_BUF"], entry * 0.001)
            risk = entry - stop
            if risk <= 0:
                continue
            stop_pct = risk / entry * 100
            if stop_pct > P["MAX_STOP_PCT"]:
                continue
            broke = arr[i + 1, 1] >= entry
            r, held = (simulate(arr, i, entry, stop, P) if broke else (0.0, 0))
            if broke:
                last_exit = i + held
            recs.append(dict(strategy=name, code=code, name=nm,
                             date=str(dates[i])[:10], broke=broke,
                             stop_pct=round(stop_pct, 2),
                             r=round(float(r), 3), hold=held))
    return recs


def stats(br):
    if len(br) == 0:
        return None
    r = br["r"].values.astype(float)
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    st = bs = 0
    for x in r:
        st = st + 1 if x < 0 else 0
        bs = max(bs, st)
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    return dict(n=len(r), win=(r > 0).mean() * 100, exp=r.mean(), tot=r.sum(),
                pf=(gp / gl) if gl > 0 else float("inf"),
                mdd=float((peak - eq).max()), streak=bs,
                stop=br["stop_pct"].mean(), hold=br["hold"].mean())


def load_universe(n):
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df.columns = [str(c) for c in df.columns]
    cc = next((c for c in df.columns if c.lower() in ("code", "symbol")), None)
    nc = next((c for c in df.columns if c.lower() == "name"), None)
    mc = next((c for c in df.columns if "marcap" in c.lower()), None)
    tc = next((c for c in df.columns if c.lower() in ("market", "markettype")), None)
    if tc:
        df = df[df[tc].astype(str).str.upper().str.contains("KOSPI|KOSDAQ", na=False)]
    if nc:
        df = df[~df[nc].astype(str).str.contains("스팩|리츠|ETN", na=False)]
        df = df[~df[nc].astype(str).str.endswith(("우", "우B", "우C"))]
    if mc:
        df = df.sort_values(mc, ascending=False)
    df = df.head(n)
    return [(str(r[cc]).zfill(6), str(r[nc]) if nc else "") for _, r in df.iterrows()]


def fetch(code, s, e):
    import FinanceDataReader as fdr
    for _ in range(2):
        try:
            return fdr.DataReader(code, s, e)
        except Exception:
            time.sleep(0.6)
    return None


STRATS = ["S1 도지-하락후", "S2 도지-추세눌림", "S3 20일신고가돌파",
          "S4 거래량급증양봉", "S5 MA20눌림반등", "BASE 무작위"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=36)
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    end = datetime.today()
    start = end - timedelta(days=int(a.months * 30.5) + 120)
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    print(f"비용: 매도세 {P['SELL_TAX_PCT']}% + 슬리피지 {P['SLIPPAGE_PCT']}% (매도 1회당)")
    print(f"청산: +{P['HALF_EXIT_R']}R 절반익절 -> 본절이동 -> 직전저점 추적 / "
          f"시간손절 {P['TIME_STOP_DAYS']}일 / 최대 {P['MAX_HOLD_DAYS']}일")

    print(f"\n[1/4] 종목 {a.universe}...", flush=True)
    uni = load_universe(a.universe)
    print(f"[2/4] 일봉 {s} ~ {e}...", flush=True)
    store, done = {}, 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch, c, s, e): (c, n) for c, n in uni}
        for f in as_completed(futs):
            c, n = futs[f]
            done += 1
            if done % 100 == 0:
                print(f"      {done}/{len(uni)}", flush=True)
            try:
                d = f.result()
                if d is not None and len(d) >= 120:
                    ft = features(d)
                    if ft is not None:
                        store[c] = (n, ft)
            except Exception:
                pass
    print(f"      수집 {len(store)}종목", flush=True)

    print("[3/4] 전략별 시뮬레이션...", flush=True)
    allrec = []
    for name in STRATS:
        allrec.extend(run_strategy(store, name))
    df = pd.DataFrame(allrec)
    if df.empty:
        print("신호 없음")
        return
    os.makedirs("results", exist_ok=True)
    df.to_csv("results/trades.csv", index=False, encoding="utf-8-sig")

    days = df["date"].nunique()
    mid = df["date"].median()

    print("\n[4/4] 결과")
    print("=" * 78)
    print(f"기간 {s} ~ {e} / 거래일 {days} / 종목 {len(store)}")
    print("=" * 78)
    print(f"{'전략':<18}{'후보':>6}{'진입':>7}{'승률':>8}{'기대값':>10}{'합계R':>9}{'PF':>7}{'낙폭':>7}{'연속손':>6}")
    print("-" * 78)
    summary = {}
    for name in STRATS:
        sub = df[df["strategy"] == name]
        br = sub[sub["broke"]]
        st = stats(br)
        if st is None or st["n"] < 20:
            print(f"{name:<18}{len(sub):>6}{len(br):>7}   표본부족")
            continue
        summary[name] = st
        print(f"{name:<18}{len(sub):>6}{st['n']:>7}{st['win']:>7.1f}%"
              f"{st['exp']:>+9.3f}R{st['tot']:>+8.1f}R{st['pf']:>7.2f}"
              f"{st['mdd']:>6.1f}R{st['streak']:>6}")

    print("\n" + "=" * 78)
    print("전후반 일관성 (전반부 / 후반부 기대값)  — 부호가 갈리면 신뢰 불가")
    print("-" * 78)
    for name in STRATS:
        sub = df[(df["strategy"] == name) & df["broke"]]
        if len(sub) < 40:
            print(f"{name:<18} 표본부족")
            continue
        a1 = stats(sub[sub["date"] < mid])
        a2 = stats(sub[sub["date"] >= mid])
        if a1 is None or a2 is None:
            continue
        ok = "일치" if (a1["exp"] > 0) == (a2["exp"] > 0) else "불일치"
        print(f"{name:<18} 전반 {a1['exp']:+.3f}R({a1['n']:>4}건)   "
              f"후반 {a2['exp']:+.3f}R({a2['n']:>4}건)   {ok}")

    print("\n" + "=" * 78)
    print("판단 기준")
    print("  · BASE 무작위보다 기대값이 확실히 높아야 신호에 의미가 있음")
    print("  · 전후반 부호가 같아야 함")
    print("  · 진입 100건 이상 · 기대값 +0.15R 이상 · PF 1.3 이상이면 실전 관찰 검토")
    print("\nresults/trades.csv 저장")


if __name__ == "__main__":
    sys.exit(main())
