# -*- coding: utf-8 -*-
"""
도지 스윙 시스템 — 백테스트 v2

v1 대비 변경점
  · 실제 청산 규칙을 그대로 시뮬레이션 (+1R 절반 익절 -> 본절 이동 -> 직전저점 추적)
  · 거래세·슬리피지 반영한 순기대값 계산
  · 파라미터 조합별로 '신호 개수'가 아니라 '순기대값'을 비교
  · 최대 연속 손절 / 누적 R 낙폭 출력

실행: python backtest.py --months 12 --universe 500
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

# ══════════ 파라미터 ══════════
P = dict(
    BODY_MAX_PCT=8.0,
    MIN_RANGE_ATR=0.5,
    DRAGON_LOWER=60.0,
    DRAGON_UPPER=15.0,
    FALL_DAYS=5,
    FALL_PCT=5.0,
    SUPPORT_ATR=0.25,
    VOL_MULT=1.2,
    MIN_VALUE_KRW=1_000_000_000,
    MIN_PRICE=1000,
    BREAK_BUFFER=0.002,
    STOP_ATR_BUF=0.15,
    MAX_STOP_PCT=5.0,
    RISK_PCT=0.5,
    TIME_STOP_DAYS=5,
    MAX_HOLD_DAYS=30,      # 잔여 절반 최대 보유
    SELL_TAX_PCT=0.20,     # 매도 거래세+농특세
    SLIPPAGE_PCT=0.10,     # 편도 슬리피지 가정
    HALF_EXIT_R=1.0,       # 절반 익절 시점
)

_PF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.yml")
if yaml and os.path.exists(_PF):
    with open(_PF, encoding="utf-8") as _f:
        _loaded = yaml.safe_load(_f) or {}
    _unknown = [k for k in _loaded if k not in P]
    if _unknown:
        print(f"[경고] params.yml 에 모르는 항목: {_unknown}")
    P.update({k: v for k, v in _loaded.items() if k in P})
    print("[설정] params.yml 적용됨")

# 그리드 탐색용 최대 완화값 (조이는 조합은 결과 필터링으로 처리)
LOOSE = dict(P, BODY_MAX_PCT=15.0, VOL_MULT=1.0, MAX_STOP_PCT=99.0, FALL_PCT=0.0)


def atr14(df):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def simulate(arr, i, entry, stop, p):
    """
    진입 이후를 실제 규칙대로 시뮬레이션.
    반환: (순R, 결말, 보유일)

    보수적 가정
      · 같은 날 손절·목표 동시 도달 -> 손절 먼저
      · 갭하락 시 손절가가 아니라 시가로 체결
    """
    risk = entry - stop
    tgt1 = entry + risk * p["HALF_EXIT_R"]
    cost_r = (p["SELL_TAX_PCT"] + p["SLIPPAGE_PCT"]) / 100 * entry / risk

    pos = 1.0
    realized = 0.0
    cost = 0.0
    half_done = False
    cur_stop = stop
    n = len(arr)

    for j in range(i + 1, min(i + 1 + p["MAX_HOLD_DAYS"], n)):
        held = j - i
        o_, h_, l_, c_ = arr[j, 0], arr[j, 1], arr[j, 2], arr[j, 3]

        if o_ <= cur_stop:
            realized += (o_ - entry) / risk * pos
            cost += cost_r * pos
            return realized - cost, ("손절" if not half_done else "추적청산"), held

        if l_ <= cur_stop:
            realized += (cur_stop - entry) / risk * pos
            cost += cost_r * pos
            return realized - cost, ("손절" if not half_done else "추적청산"), held

        if (not half_done) and h_ >= tgt1:
            realized += (tgt1 - entry) / risk * 0.5
            cost += cost_r * 0.5
            pos = 0.5
            half_done = True
            cur_stop = max(entry, arr[j - 1, 2])

        if (not half_done) and held >= p["TIME_STOP_DAYS"]:
            realized += (c_ - entry) / risk * pos
            cost += cost_r * pos
            return realized - cost, "시간손절", held

        if half_done:
            cur_stop = max(cur_stop, arr[j - 1, 2])

    j = min(i + p["MAX_HOLD_DAYS"], n - 1)
    realized += (arr[j, 3] - entry) / risk * pos
    cost += cost_r * pos
    return realized - cost, "기간만료", j - i


def analyze(df, code, name, p=LOOSE):
    """느슨한 기준으로 후보를 뽑고, 판정 지표를 함께 반환."""
    if df is None or len(df) < 60:
        return []
    df = df.copy()
    df.columns = [str(c).capitalize() for c in df.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns):
        return []

    df["atr"] = atr14(df)
    df["ma20"] = df["Close"].rolling(20).mean()
    df["low20"] = df["Low"].rolling(20).min()
    df["vol20"] = df["Volume"].rolling(20).mean()
    df["value20"] = (df["Close"] * df["Volume"]).rolling(20).mean()

    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    rng = (h - l).replace(0, np.nan)
    body_r = (c - o).abs() / rng * 100
    up_r = (h - pd.concat([o, c], axis=1).max(axis=1)) / rng * 100
    dn_r = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng * 100

    is_doji = (body_r <= p["BODY_MAX_PCT"]) & (rng >= df["atr"] * p["MIN_RANGE_ATR"])
    is_grave = (up_r >= p["DRAGON_LOWER"]) & (dn_r <= p["DRAGON_UPPER"])
    ok_type = is_doji & ~is_grave

    base = c.shift(p["FALL_DAYS"])
    fall_pct = (base - c) / base * 100
    tol = df["atr"] * p["SUPPORT_ATR"]
    near_sup = ((l - df["low20"]).abs() <= tol) | ((l - df["ma20"]).abs() <= tol)
    vol_ratio = df["Volume"] / df["vol20"]
    liq_ok = (df["value20"] >= p["MIN_VALUE_KRW"]) & (c >= p["MIN_PRICE"])

    cand = ok_type & liq_ok & (near_sup | (fall_pct > 0))
    idx = np.where(cand.fillna(False).values)[0]

    arr = df[["Open", "High", "Low", "Close", "Volume"]].values
    dates = df.index
    fp = fall_pct.values
    out = []
    for i in idx:
        if i + 1 >= len(df):
            continue
        a = df["atr"].values[i]
        if not np.isfinite(a):
            continue
        d_high, d_low = arr[i, 1], arr[i, 2]
        entry = d_high * (1 + p["BREAK_BUFFER"])
        stop = d_low - max(a * p["STOP_ATR_BUF"], entry * 0.001)
        risk = entry - stop
        if risk <= 0:
            continue

        rec = dict(
            code=code, name=name, date=str(dates[i])[:10],
            close=round(float(arr[i, 3]), 2),
            entry=round(float(entry), 2), stop=round(float(stop), 2),
            stop_pct=round(float(risk / entry * 100), 3),
            body_r=round(float(body_r.values[i]), 2),
            vol_ratio=round(float(vol_ratio.values[i]), 2),
            fall_pct=round(float(fp[i]), 2) if np.isfinite(fp[i]) else 0.0,
            near_sup=bool(near_sup.values[i]),
            dragon=bool(dn_r.values[i] >= p["DRAGON_LOWER"] and up_r.values[i] <= p["DRAGON_UPPER"]),
            broke=False, outcome="미돌파", hold_days=0, r=0.0,
        )
        if arr[i + 1, 1] >= entry:
            rec["broke"] = True
            r, res, held = simulate(arr, i, entry, stop, p)
            rec.update(r=round(float(r), 3), outcome=res, hold_days=int(held))
        out.append(rec)
    return out


def stats(sub, days):
    br = sub[sub["broke"]]
    if len(br) == 0:
        return None
    r = br["r"].values.astype(float)
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    mdd = float((peak - eq).max()) if len(eq) else 0.0
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    return dict(
        cand=len(sub), per_day=len(sub) / max(days, 1),
        trades=len(br), break_rate=len(br) / len(sub) * 100,
        win=(r > 0).mean() * 100, exp=r.mean(), total=r.sum(),
        pf=(gp / gl) if gl > 0 else float("inf"),
        mdd=mdd, streak=best,
        avg_stop=br["stop_pct"].mean(), avg_hold=br["hold_days"].mean(),
    )


def show(title, s):
    if s is None:
        print(f"\n{title}\n  진입 0건")
        return
    print(f"\n{title}")
    print(f"  후보 {s['cand']:,}건 (일평균 {s['per_day']:.1f})  ->  "
          f"돌파 진입 {s['trades']:,}건 ({s['break_rate']:.0f}%)")
    print(f"  승률 {s['win']:.1f}%   건당 순기대값 {s['exp']:+.3f}R   합계 {s['total']:+.1f}R")
    print(f"  PF {s['pf']:.2f}   최대 누적낙폭 {s['mdd']:.1f}R   최대 연속손절 {s['streak']}회")
    print(f"  평균 손절폭 {s['avg_stop']:.2f}%   평균 보유 {s['avg_hold']:.1f}일")


def load_universe(n):
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df.columns = [str(c) for c in df.columns]
    codecol = next((c for c in df.columns if c.lower() in ("code", "symbol")), None)
    namecol = next((c for c in df.columns if c.lower() == "name"), None)
    mkcol = next((c for c in df.columns if "marcap" in c.lower()), None)
    mktcol = next((c for c in df.columns if c.lower() in ("market", "markettype")), None)
    if mktcol:
        df = df[df[mktcol].astype(str).str.upper().str.contains("KOSPI|KOSDAQ", na=False)]
    if namecol:
        df = df[~df[namecol].astype(str).str.contains("스팩|리츠|ETN", na=False)]
        df = df[~df[namecol].astype(str).str.endswith(("우", "우B", "우C"))]
    if mkcol:
        df = df.sort_values(mkcol, ascending=False)
    df = df.head(n)
    return [(str(r[codecol]).zfill(6), str(r[namecol]) if namecol else "") for _, r in df.iterrows()]


def fetch(code, start, end):
    import FinanceDataReader as fdr
    for _ in range(2):
        try:
            return fdr.DataReader(code, start, end)
        except Exception:
            time.sleep(0.6)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    end = datetime.today()
    start = end - timedelta(days=int(a.months * 30.5) + 90)
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    print("현재 설정:", {k: P[k] for k in
          ("BODY_MAX_PCT", "FALL_PCT", "VOL_MULT", "MAX_STOP_PCT")})
    print(f"비용 가정: 매도 거래세 {P['SELL_TAX_PCT']}% + 슬리피지 {P['SLIPPAGE_PCT']}%")

    print(f"\n[1/3] 종목 리스트 (상위 {a.universe})...", flush=True)
    uni = load_universe(a.universe)
    print(f"      {len(uni)}종목", flush=True)

    print(f"[2/3] 일봉 수집 {s} ~ {e}...", flush=True)
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
                if d is not None and len(d) >= 60:
                    store[c] = (n, d)
            except Exception:
                pass
    print(f"      수집 {len(store)}종목", flush=True)

    rows = []
    for c, (n, d) in store.items():
        try:
            rows.extend(analyze(d, c, n))
        except Exception:
            pass
    if not rows:
        print("후보 0건.")
        return

    df = pd.DataFrame(rows).sort_values("date")
    days = df["date"].nunique()
    os.makedirs("results", exist_ok=True)
    df.to_csv("results/signals_all.csv", index=False, encoding="utf-8-sig")

    def flt(body, vol, capp, fall):
        m = ((df["body_r"] <= body) & (df["vol_ratio"] >= vol) &
             (df["stop_pct"] <= capp) &
             ((df["fall_pct"] >= fall) | df["near_sup"]))
        return df[m]

    print("\n[3/3] 결과")
    print("=" * 62)
    cur = flt(P["BODY_MAX_PCT"], P["VOL_MULT"], P["MAX_STOP_PCT"], P["FALL_PCT"])
    cur.to_csv("results/signals.csv", index=False, encoding="utf-8-sig")
    show(f"현재 설정 (몸통{P['BODY_MAX_PCT']}% 거래량{P['VOL_MULT']}x "
         f"손절폭≤{P['MAX_STOP_PCT']}% 하락{P['FALL_PCT']}%)", stats(cur, days))

    print("\n" + "=" * 62)
    print("손절폭 분포 (전체 후보)")
    for q in (10, 25, 50, 75, 90):
        print(f"  하위 {q:2d}%    {np.percentile(df['stop_pct'], q):.2f}%")

    print("\n" + "=" * 62)
    print("손절폭 상한별 성과")
    print("  상한   진입    승률    순기대값     합계R     PF")
    for capp in (2, 3, 4, 5, 7, 99):
        st = stats(flt(P["BODY_MAX_PCT"], P["VOL_MULT"], capp, P["FALL_PCT"]), days)
        if st is None or st["trades"] < 5:
            print(f"  {capp:3d}%   표본부족")
            continue
        print(f"  {capp:3d}%  {st['trades']:5d}건  {st['win']:5.1f}%  "
              f"{st['exp']:+8.3f}R  {st['total']:+7.1f}R  {st['pf']:5.2f}")

    print("\n" + "=" * 62)
    print("조합별 순기대값(R) / 진입건수")
    print("             거래량 1.0x        1.2x        1.5x        2.0x")
    for body in (5, 8, 10, 12, 15):
        cells = []
        for vol in (1.0, 1.2, 1.5, 2.0):
            st = stats(flt(body, vol, P["MAX_STOP_PCT"], P["FALL_PCT"]), days)
            cells.append("   부족    " if (st is None or st["trades"] < 10)
                         else f"{st['exp']:+.3f}/{st['trades']:<5d}")
        mark = " <-현재" if body == P["BODY_MAX_PCT"] else ""
        print(f"  몸통 {body:2d}%   " + " ".join(cells) + mark)

    print("\n  * 진입 10건 미만은 '부족'. 30건 미만이면 숫자를 믿지 마세요.")
    print("  * 순기대값 +0.1R 이상인 조합만 후보로 봅니다.")
    print("\nresults/signals.csv · signals_all.csv 저장")


if __name__ == "__main__":
    sys.exit(main())
