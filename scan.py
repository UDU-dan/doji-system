# -*- coding: utf-8 -*-
"""
도지 관심종목 스캐너 (국장 / 미장 공용)

전날 일봉에서 도지 후보를 뽑아 점수순으로 정렬하고,
상위 종목에는 실적발표 예정일과 최근 뉴스를 붙여준다.

수익을 보장하는 도구가 아니라, 볼 종목을 20개로 줄여주는 도구.
진입·청산 판단은 사용자가 한다.

실행:
  python scan.py --market kr --universe 800
  python scan.py --market us --universe 2500
"""
import argparse
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:
    yaml = None

S = dict(
    BODY_MAX_PCT=15.0,
    MIN_RANGE_ATR=0.4,
    VOL_MIN=1.0,
    DRAGON_LOWER=60.0,
    DRAGON_UPPER=15.0,
    MAX_STOP_PCT=10.0,
    MIN_VALUE_KR=1_000_000_000,
    MIN_VALUE_US=10_000_000,
    MIN_PRICE_KR=1000,
    MIN_PRICE_US=5,
    TOP_A=10,
    TOP_B=10,
    CHASE_PCT=0.5,
    NEWS_PER_STOCK=2,
    W_VOLUME=25,
    W_QUALITY=20,
    W_POSITION=15,
    W_52WH=20,
    W_STOP=12,
    W_LIQUIDITY=8,
    PEN_SURGE=15,
    SURGE_PCT=15.0,
    PEN_TURNOVER=10,
    TURNOVER_HOT=0.05,
    # ── 패턴 유형 ──
    NEAR52_PCT=1.0,      # 52주 고가 대비 이 % 이내면 신고가권
    PULLBACK_PCT=2.0,    # MA20 터치 허용 오차 %
    VOL_SURGE=3.0,       # 거래량 급증 기준 배수
    BONUS_COMBO=5,       # 유형 하나 겹칠 때마다 가점
)

_PF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_params.yml")
if yaml and os.path.exists(_PF):
    with open(_PF, encoding="utf-8") as f:
        _l = yaml.safe_load(f) or {}
    S.update({k: v for k, v in _l.items() if k in S})
    print("[설정] scan_params.yml 적용됨")


def evaluate(df, code, name, market, marcap=None):
    if df is None or len(df) < 70:
        return None
    d = df.copy()
    d.columns = [str(c).capitalize() for c in d.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(d.columns):
        return None
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < 70:
        return None

    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    low20 = l.rolling(20).min()
    hi52 = h.rolling(min(250, len(d))).max()
    vol20 = v.rolling(20).mean()
    value20 = (c * v).rolling(20).mean()

    i = -1
    O, H, L, C, V = (float(x.iloc[i]) for x in (o, h, l, c, v))
    A, M20, M60 = (float(x.iloc[i]) for x in (atr, ma20, ma60))
    L20, H52 = float(low20.iloc[i]), float(hi52.iloc[i])
    V20, VAL20 = float(vol20.iloc[i]), float(value20.iloc[i])
    if not all(np.isfinite(x) for x in (A, M20, M60, V20, VAL20, H52)) or V20 <= 0:
        return None

    min_val = S["MIN_VALUE_KR"] if market == "kr" else S["MIN_VALUE_US"]
    min_px = S["MIN_PRICE_KR"] if market == "kr" else S["MIN_PRICE_US"]
    if VAL20 < min_val or C < min_px:
        return None

    rng = H - L
    if rng <= 0:
        return None
    body_r = abs(C - O) / rng * 100
    up_r = (H - max(O, C)) / rng * 100
    dn_r = (min(O, C) - L) / rng * 100
    vol_ratio = V / V20

    if vol_ratio < S["VOL_MIN"]:
        return None

    # ── 패턴 판정 (하나 이상 만족해야 후보) ──
    is_grave = up_r >= S["DRAGON_LOWER"] and dn_r <= S["DRAGON_UPPER"]
    is_dragon = dn_r >= S["DRAGON_LOWER"] and up_r <= S["DRAGON_UPPER"]
    is_doji = (body_r <= S["BODY_MAX_PCT"] and rng >= A * S["MIN_RANGE_ATR"]
               and not is_grave)

    M20_prev = float(ma20.iloc[-2]) if len(d) > 1 else M20
    M60_prev = float(ma60.iloc[-11]) if len(d) > 11 else M60
    uptrend = C > M60 and M60 > M60_prev
    prev_low = float(l.iloc[-2]) if len(d) > 1 else L

    pats = []
    if is_doji:
        pats.append("도지")
    if C >= H52 * (1 - S["NEAR52_PCT"] / 100) and vol_ratio >= 1.5:
        pats.append("52주신고가")
    if uptrend and min(L, prev_low) <= M20 * (1 + S["PULLBACK_PCT"] / 100) \
            and C > M20 * 0.98:
        pats.append("MA20눌림")
    if vol_ratio >= S["VOL_SURGE"] and C > O and C > M20:
        pats.append("거래량급증")
    if not pats:
        return None

    entry = H * (1 + 0.002)
    stop = L - max(A * 0.15, entry * 0.001)
    risk = entry - stop
    if risk <= 0:
        return None
    stop_pct = risk / entry * 100
    if stop_pct > S["MAX_STOP_PCT"]:
        return None

    sc_vol = min(vol_ratio / 3.0, 1.0) * 100
    sc_qual = max(0.0, 1 - body_r / S["BODY_MAX_PCT"]) * 100
    if is_dragon:
        sc_qual = min(100.0, sc_qual + 15)

    ma20_gap = (C - M20) / M20 * 100
    near_low = abs(L - L20) / max(L20, 1e-9) * 100
    pos = 0.0
    if -6 <= ma20_gap <= 3:
        pos += 60 * (1 - abs(ma20_gap) / 6)
    if near_low <= 3:
        pos += 25 * (1 - near_low / 3)
    if C > M60:
        pos += 15
    sc_pos = float(np.clip(pos, 0, 100))

    nearness = C / H52 if H52 > 0 else 0
    sc_52 = float(np.clip((nearness - 0.70) / 0.30, 0, 1) * 100)
    sc_stop = max(0.0, 1 - stop_pct / S["MAX_STOP_PCT"]) * 100
    sc_liq = float(np.clip(np.log10(max(VAL20, 1) / min_val) / 1.5, 0, 1) * 100)

    score = (S["W_VOLUME"] * sc_vol + S["W_QUALITY"] * sc_qual +
             S["W_POSITION"] * sc_pos + S["W_52WH"] * sc_52 +
             S["W_STOP"] * sc_stop + S["W_LIQUIDITY"] * sc_liq) / 100

    ret5 = (C / float(c.iloc[-6]) - 1) * 100 if len(c) > 6 else 0.0
    pen_s = 0.0
    if ret5 > S["SURGE_PCT"]:
        pen_s = S["PEN_SURGE"] * min((ret5 - S["SURGE_PCT"]) / S["SURGE_PCT"], 1.0)
    pen_t = 0.0
    turnover = (VAL20 / marcap) if (marcap and marcap > 0) else None
    if turnover and turnover > S["TURNOVER_HOT"]:
        pen_t = S["PEN_TURNOVER"] * min(
            (turnover - S["TURNOVER_HOT"]) / S["TURNOVER_HOT"], 1.0)
    combo = S["BONUS_COMBO"] * (len(pats) - 1)
    score = max(0.0, min(100.0, score + combo) - pen_s - pen_t)

    px = (lambda x: int(round(x))) if market == "kr" else (lambda x: round(x, 2))
    return dict(
        code=code, name=name, market=market, date=str(d.index[i])[:10],
        score=round(score, 1), close=px(C),
        entry=px(entry), chase=px(entry * (1 + S["CHASE_PCT"] / 100)),
        ma20=px(M20), stop=px(stop), stop_pct=round(stop_pct, 2),
        vol_ratio=round(vol_ratio, 2), body_r=round(body_r, 1),
        type="잠자리" if is_dragon else ("표준" if is_doji else "-"),
        pats="+".join(pats), npat=len(pats),
        ma20_gap=round(ma20_gap, 1), near52=round(nearness * 100, 1),
        ret5=round(ret5, 1), pen=round(pen_s + pen_t, 1),
        s_vol=int(sc_vol), s_qual=int(sc_qual), s_pos=int(sc_pos),
        s_52=int(sc_52), s_stop=int(sc_stop), s_liq=int(sc_liq),
    )


def universe_kr(n):
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df.columns = [str(x) for x in df.columns]
    cc = next((x for x in df.columns if x.lower() in ("code", "symbol")), None)
    nc = next((x for x in df.columns if x.lower() == "name"), None)
    mc = next((x for x in df.columns if "marcap" in x.lower()), None)
    tc = next((x for x in df.columns if x.lower() in ("market", "markettype")), None)
    if tc:
        df = df[df[tc].astype(str).str.upper().str.contains("KOSPI|KOSDAQ", na=False)]
    if nc:
        df = df[~df[nc].astype(str).str.contains("스팩|리츠|ETN", na=False)]
        df = df[~df[nc].astype(str).str.endswith(("우", "우B", "우C"))]
    if mc:
        df = df.sort_values(mc, ascending=False)
    df = df.head(n)
    return [(str(r[cc]).zfill(6), str(r[nc]) if nc else "",
             float(r[mc]) if mc and pd.notna(r[mc]) else None)
            for _, r in df.iterrows()]


def universe_us(n):
    import FinanceDataReader as fdr
    frames = []
    for mk in ("NASDAQ", "NYSE", "AMEX"):
        try:
            f = fdr.StockListing(mk)
            f.columns = [str(x) for x in f.columns]
            frames.append(f)
        except Exception as ex:
            print(f"      {mk} 조회 실패: {ex}")
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    sc = next((x for x in df.columns if x.lower() in ("symbol", "code")), None)
    nc = next((x for x in df.columns if x.lower() == "name"), None)
    df = df.dropna(subset=[sc]).drop_duplicates(subset=[sc])
    df = df[~df[sc].astype(str).str.contains(r"[\^\.\$]", na=False)]
    df = df[df[sc].astype(str).str.len() <= 5]
    df = df.head(n)
    return [(str(r[sc]), str(r[nc]) if nc else str(r[sc]), None)
            for _, r in df.iterrows()]


def fetch_kr(code, s, e):
    import FinanceDataReader as fdr
    for _ in range(2):
        try:
            return fdr.DataReader(code, s, e)
        except Exception:
            time.sleep(0.5)
    return None


def fetch_us_batch(tickers, period="1y"):
    import yfinance as yf
    out = {}
    try:
        raw = yf.download(tickers, period=period, group_by="ticker",
                          threads=True, progress=False, auto_adjust=False)
    except Exception as ex:
        print(f"      배치 실패: {ex}")
        return out
    for t in tickers:
        try:
            d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            if d is not None and len(d.dropna()) >= 70:
                out[t] = d
        except Exception:
            pass
    return out


def market_regime(market):
    try:
        if market == "kr":
            import FinanceDataReader as fdr
            idx = fdr.DataReader(
                "KS11", (datetime.today() - timedelta(days=500)).strftime("%Y-%m-%d"))
            nm = "KOSPI"
        else:
            import yfinance as yf
            idx = yf.download("^GSPC", period="2y", progress=False, auto_adjust=False)
            nm = "S&P500"
        c = idx["Close"].squeeze().dropna()
        ma200 = c.rolling(200).mean()
        cur, m = float(c.iloc[-1]), float(ma200.iloc[-1])
        gap = (cur / m - 1) * 100
        state = "양호 (200일선 위)" if cur > m else "주의 (200일선 아래)"
        return f"{nm} {cur:,.0f} · {state} · 이격 {gap:+.1f}%"
    except Exception:
        return "시장국면 조회 실패"


def get_news(query, market, k):
    try:
        hl, gl, ceid = ("ko", "KR", "KR:ko") if market == "kr" else ("en-US", "US", "US:en")
        q = f"{query} 주가" if market == "kr" else f"{query} stock"
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + f"&hl={hl}&gl={gl}&ceid={ceid}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            root = ET.fromstring(r.read())
        out = []
        for item in root.iter("item"):
            t = item.findtext("title") or ""
            if t:
                out.append(t.split(" - ")[0][:60])
            if len(out) >= k:
                break
        return out
    except Exception:
        return []


def get_earnings_us(ticker):
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        d = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            d = (v[0] if isinstance(v, (list, tuple)) and v else v)
        elif cal is not None and hasattr(cal, "loc"):
            d = cal.loc["Earnings Date"][0]
        if d is None:
            return None
        d = pd.to_datetime(d).date()
        dd = (d - datetime.today().date()).days
        return (d, dd) if -3 <= dd <= 30 else None
    except Exception:
        return None


def fmt(rows, title, brief=False):
    out = [title]
    for r in rows:
        if brief:
            out.append(f"{r['rank']}위 {r['name'][:12]} {r['score']}점 [{r['pats']}] · "
                       f"진입 {r['entry']:,} · 손절 {r['stop']:,}(-{r['stop_pct']}%)")
            continue
        ma = (f"MA20 {r['ma20']:,} 돌파시 더 강함" if r["ma20"] > r["entry"]
              else f"MA20 {r['ma20']:,} 이미 위")
        blk = [
            f"\n{r['rank']}위  {r['name'][:20]} ({r['code']})  {r['score']}점  [{r['pats']}]",
            f"  종가 {r['close']:,} · 거래량 {r['vol_ratio']}배"
            + (f" · {r['type']}형" if r['type'] != "-" else ""),
            f"  진입  {r['entry']:,} ~ {r['chase']:,}",
            f"  손절  {r['stop']:,} (-{r['stop_pct']}%)",
            f"  52주고가 대비 {r['near52']}% · {ma}",
        ]
        if r.get("pen", 0) > 0:
            blk.append(f"  △ 최근5일 {r['ret5']:+.1f}% · 과열감점 -{r['pen']}")
        if r.get("earn"):
            blk.append(f"  ⚠ 실적발표 {r['earn']}")
        for n in r.get("news", []):
            blk.append(f"  · {n}")
        out.append("\n".join(blk))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--universe", type=int, default=800)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    mk = a.market

    end = datetime.today()
    start = end - timedelta(days=400)
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    print(f"[1/5] 유니버스 ({mk.upper()} 상위 {a.universe})...", flush=True)
    uni = universe_kr(a.universe) if mk == "kr" else universe_us(a.universe)
    print(f"      {len(uni)}종목", flush=True)
    if not uni:
        print("유니버스 비어 있음")
        return

    print("[2/5] 일봉 수집...", flush=True)
    res = []
    if mk == "kr":
        done = 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(fetch_kr, c, s, e): (c, n, m) for c, n, m in uni}
            for f in as_completed(futs):
                c, n, m = futs[f]
                done += 1
                if done % 200 == 0:
                    print(f"      {done}/{len(uni)}", flush=True)
                try:
                    r = evaluate(f.result(), c, n, mk, m)
                    if r:
                        res.append(r)
                except Exception:
                    pass
    else:
        CH = 150
        meta = {c: n for c, n, _ in uni}
        syms = list(meta.keys())
        for k in range(0, len(syms), CH):
            chunk = syms[k:k + CH]
            for t, d in fetch_us_batch(chunk).items():
                try:
                    r = evaluate(d, t, meta.get(t, t), mk, None)
                    if r:
                        res.append(r)
                except Exception:
                    pass
            print(f"      {min(k + CH, len(syms))}/{len(syms)}", flush=True)

    if not res:
        print("후보 0건. scan_params.yml 의 BODY_MAX_PCT 를 올려보세요.")
        return

    df = pd.DataFrame(res).sort_values("score", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    os.makedirs("results", exist_ok=True)
    df.to_csv(f"results/watchlist_{mk}.csv", index=False, encoding="utf-8-sig")

    print("[3/5] 시장 국면...", flush=True)
    regime = market_regime(mk)

    top = df.head(S["TOP_A"] + S["TOP_B"]).to_dict("records")
    print(f"[4/5] 뉴스·실적 ({len(top)}종목)...", flush=True)
    for r in top:
        r["news"] = get_news(r["name"], mk, S["NEWS_PER_STOCK"])
        if mk == "us":
            ed = get_earnings_us(r["code"])
            if ed:
                d_, dd = ed
                r["earn"] = f"{d_} (D{dd:+d})"
        time.sleep(0.3)

    A = top[:S["TOP_A"]]
    B = top[S["TOP_A"]:]
    base = df["date"].iloc[0]
    cnt = {}
    for pp in df["pats"]:
        for x in str(pp).split("+"):
            cnt[x] = cnt.get(x, 0) + 1
    mix = " · ".join(f"{k} {v}" for k, v in
                     sorted(cnt.items(), key=lambda z: -z[1]))
    head = (f"[{base} 관심종목 · {'국장' if mk == 'kr' else '미장'}]\n"
            f"전체 {len(df)}건 · 최고 {df['score'].max():.1f}점 / "
            f"중앙 {df['score'].median():.1f}점\n"
            f"유형: {mix}\n"
            f"시장국면: {regime}")

    body = head + "\n" + fmt(A, "\n━━ A그룹 (주력) ━━")
    if B:
        body += "\n\n" + fmt(B, "━━ B그룹 (예비) ━━", brief=True)

    print("\n[5/5] 결과")
    print("=" * 66)
    print(body)
    print("\n" + "=" * 66)
    print("점수 구성 (상위 5)")
    print(f"{'종목':<16}{'총점':>6}{'거래량':>7}{'품질':>6}{'위치':>6}{'52주':>6}{'손절':>6}")
    for r in A[:5]:
        print(f"{str(r['name'])[:15]:<16}{r['score']:>6.1f}{r['s_vol']:>7}"
              f"{r['s_qual']:>6}{r['s_pos']:>6}{r['s_52']:>6}{r['s_stop']:>6}")

    with open(f"results/watchlist_{mk}.txt", "w", encoding="utf-8") as f:
        f.write(body)
    print(f"\nresults/watchlist_{mk}.txt · .csv 저장")


if __name__ == "__main__":
    sys.exit(main())
