# -*- coding: utf-8 -*-
"""
도지 스윙 시스템 — 파라미터 검증 백테스트
목적: 승률 최적화가 아니라 "신호 개수가 쓸 만한가"를 확인하는 것.

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

# ══════════ 확정 파라미터 ══════════
P = dict(
    BODY_MAX_PCT=8.0,        # 몸통 / 전체범위
    MIN_RANGE_ATR=0.5,       # 최소 범위 (ATR14 배수)
    DRAGON_LOWER=60.0,       # 잠자리형 아래꼬리 최소
    DRAGON_UPPER=15.0,       # 잠자리형 위꼬리 최대
    FALL_DAYS=5,             # 하락 조건 기간
    FALL_PCT=5.0,            # 하락 조건 %
    SUPPORT_ATR=0.25,        # 지지선 터치 허용오차
    VOL_MULT=1.2,            # 거래량 배수
    MIN_VALUE_KRW=1_000_000_000,   # 거래대금 하한 10억
    BREAK_BUFFER=0.002,      # 돌파 여유 0.2%
    STOP_ATR_BUF=0.15,       # 손절 버퍼
    MAX_STOP_PCT=2.0,        # 손절폭 상한
    RISK_PCT=0.5,            # 1회 리스크 (계좌 %)
    TIME_STOP_DAYS=5,        # 시간 손절
    MIN_PRICE=1000,
)

# params.yml 이 있으면 덮어쓴다 (폰에서 이 파일만 고치면 됨)
_PF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.yml")
if yaml and os.path.exists(_PF):
    with open(_PF, encoding="utf-8") as _f:
        _loaded = yaml.safe_load(_f) or {}
    _unknown = [k for k in _loaded if k not in P]
    if _unknown:
        print(f"[경고] params.yml 에 모르는 항목: {_unknown}")
    P.update({k: v for k, v in _loaded.items() if k in P})
    print("[설정] params.yml 적용됨")


def atr14(df):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def analyze(df, code, name, p=P):
    """일봉 DataFrame -> 도지 신호 리스트"""
    if df is None or len(df) < 60:
        return []
    df = df.copy()
    df.columns = [str(c).capitalize() for c in df.columns]
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(df.columns):
        return []

    df["atr"] = atr14(df)
    df["ma20"] = df["Close"].rolling(20).mean()
    df["low20"] = df["Low"].rolling(20).min()
    df["vol20"] = df["Volume"].rolling(20).mean()
    df["value20"] = (df["Close"] * df["Volume"]).rolling(20).mean()

    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    rng = h - l
    rng = rng.replace(0, np.nan)
    body = (c - o).abs()

    body_r = body / rng * 100
    up_r = (h - pd.concat([o, c], axis=1).max(axis=1)) / rng * 100
    dn_r = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng * 100

    is_doji = (body_r <= p["BODY_MAX_PCT"]) & (rng >= df["atr"] * p["MIN_RANGE_ATR"])
    is_dragon = is_doji & (dn_r >= p["DRAGON_LOWER"]) & (up_r <= p["DRAGON_UPPER"])
    # 비석형 제외
    is_grave = is_doji & (up_r >= p["DRAGON_LOWER"]) & (dn_r <= p["DRAGON_UPPER"])
    ok_type = is_doji & ~is_grave

    # 위치: 하락 OR 지지선
    base = c.shift(p["FALL_DAYS"])
    fell = (base - c) / base * 100 >= p["FALL_PCT"]
    tol = df["atr"] * p["SUPPORT_ATR"]
    near_sup = ((l - df["low20"]).abs() <= tol) | ((l - df["ma20"]).abs() <= tol)
    pos_ok = fell | near_sup

    vol_ok = df["Volume"] >= df["vol20"] * p["VOL_MULT"]
    liq_ok = (df["value20"] >= p["MIN_VALUE_KRW"]) & (c >= p["MIN_PRICE"])

    sig = ok_type & pos_ok & vol_ok & liq_ok
    idx = np.where(sig.fillna(False).values)[0]

    out = []
    arr = df[["Open", "High", "Low", "Close", "Volume"]].values
    dates = df.index
    for i in idx:
        if i + 1 >= len(df):
            continue
        d_high, d_low = arr[i, 1], arr[i, 2]
        a = df["atr"].values[i]
        if not np.isfinite(a):
            continue

        entry = d_high * (1 + p["BREAK_BUFFER"])
        stop = d_low - max(a * p["STOP_ATR_BUF"], entry * 0.001)
        risk = entry - stop
        if risk <= 0:
            continue
        stop_pct = risk / entry * 100

        rec = dict(
            code=code, name=name, date=str(dates[i])[:10],
            close=round(arr[i, 3], 2), doji_high=round(d_high, 2), doji_low=round(d_low, 2),
            entry=round(entry, 2), stop=round(stop, 2), stop_pct=round(stop_pct, 2),
            dragon=bool(is_dragon.values[i]),
            over_limit=stop_pct > p["MAX_STOP_PCT"],
            broke=False, outcome="미돌파", hold_days=0, r=0.0,
        )

        # 다음날 돌파 여부 (일봉 근사: 다음날 고가가 진입가 이상)
        nh = arr[i + 1, 1]
        if nh >= entry:
            rec["broke"] = True
            target = entry + risk  # +1R
            res, held = "보유중", 0
            for j in range(i + 1, min(i + 1 + p["TIME_STOP_DAYS"] + 1, len(df))):
                held = j - i
                lo_j, hi_j = arr[j, 2], arr[j, 1]
                hit_stop = lo_j <= stop
                hit_tgt = hi_j >= target
                if hit_stop and hit_tgt:
                    res = "손절"          # 같은 날 둘 다 -> 보수적으로 손절
                    break
                if hit_stop:
                    res = "손절"
                    break
                if hit_tgt:
                    res = "+1R"
                    break
            if res == "보유중":
                j = min(i + p["TIME_STOP_DAYS"], len(df) - 1)
                res = "시간손절"
                rec["r"] = (arr[j, 3] - entry) / risk
            else:
                rec["r"] = 1.0 if res == "+1R" else -1.0
            rec["outcome"], rec["hold_days"] = res, held
        out.append(rec)
    return out


# ══════════ 데이터 ══════════
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
        bad = "스팩|우선주|리츠|ETN"
        df = df[~df[namecol].astype(str).str.contains(bad, na=False)]
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
    start = end - timedelta(days=int(a.months * 30.5) + 90)  # 지표 워밍업 여유
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    print("적용 파라미터:", {k: P[k] for k in
          ("BODY_MAX_PCT", "MIN_RANGE_ATR", "FALL_PCT", "VOL_MULT", "MAX_STOP_PCT")})
    print(f"[1/3] 종목 리스트 수집 (상위 {a.universe})...", flush=True)
    uni = load_universe(a.universe)
    print(f"      {len(uni)}종목", flush=True)

    print(f"[2/3] 일봉 수집 {s} ~ {e}...", flush=True)
    store, done = {}, 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch, c, s, e): (c, n) for c, n in uni}
        for f in as_completed(futs):
            c, n = futs[f]
            done += 1
            if done % 50 == 0:
                print(f"      {done}/{len(uni)}", flush=True)
            try:
                d = f.result()
                if d is not None and len(d) >= 60:
                    store[c] = (n, d)
            except Exception:
                pass
    print(f"      수집 완료 {len(store)}종목", flush=True)

    rows = []
    for c, (n, d) in store.items():
        try:
            rows.extend(analyze(d, c, n))
        except Exception:
            pass

    if not rows:
        print("신호 0건. 파라미터가 너무 조입니다.")
        return

    df = pd.DataFrame(rows).sort_values("date")
    os.makedirs("results", exist_ok=True)
    df.to_csv("results/signals.csv", index=False, encoding="utf-8-sig")

    print("\n[3/3] 결과\n" + "=" * 46)
    days = df["date"].nunique()
    tradable = df[~df["over_limit"]]
    broke = tradable[tradable["broke"]]

    print(f"전체 신호           {len(df):,}건 / {days}거래일")
    print(f"일평균 신호         {len(df)/max(days,1):.1f}건")
    print(f"손절폭 2% 이내      {len(tradable):,}건 ({len(tradable)/len(df)*100:.0f}%)")
    print(f"  일평균            {len(tradable)/max(days,1):.1f}건   <- 이 값이 3~5면 적정")
    if len(tradable):
        print(f"다음날 돌파         {len(broke):,}건 ({len(broke)/len(tradable)*100:.0f}%)")
    if len(broke):
        vc = broke["outcome"].value_counts()
        win = int(vc.get("+1R", 0))
        print(f"\n돌파 후 결과 ({len(broke)}건)")
        for k, v in vc.items():
            print(f"  {k:8s} {v:5d}건 ({v/len(broke)*100:.0f}%)")
        print(f"  승률     {win/len(broke)*100:.1f}%")
        print(f"  합계 R   {broke['r'].sum():+.1f}R")
        print(f"  건당 기대값 {broke['r'].mean():+.3f}R")
        print(f"  평균 손절폭 {broke['stop_pct'].mean():.2f}%")
        if len(broke) < 30:
            print("  ** 표본 30건 미만 - 판단 보류 **")

    print("\n" + "=" * 46)
    print("파라미터 민감도 — 일평균 진입가능 신호 수")
    print("            거래량 1.0x  1.2x  1.5x  2.0x")
    for bp in (5, 8, 10, 12):
        cells = []
        for vm in (1.0, 1.2, 1.5, 2.0):
            p2 = dict(P, BODY_MAX_PCT=bp, VOL_MULT=vm)
            cnt = 0
            for c, (n, d) in store.items():
                try:
                    cnt += sum(1 for r in analyze(d, c, n, p2)
                               if not r["over_limit"])
                except Exception:
                    pass
            cells.append(f"{cnt/max(days,1):5.1f}")
        mark = " <-현재" if bp == P["BODY_MAX_PCT"] else ""
        print(f"  몸통 {bp:2d}%  " + "  ".join(cells) + mark)
    print("  * 하루 3~5건 나오는 조합을 고르세요")

    print("\nresults/signals.csv 저장 완료")


if __name__ == "__main__":
    sys.exit(main())
