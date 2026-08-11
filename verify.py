# -*- coding: utf-8 -*-
"""
최근 N일 도지 검증 스캐너

지난 N일 안에 발생한 도지/패턴을 모두 찾아서
  · 아직 도지 고가를 안 넘은 것 -> 감시 대상
  · 이미 넘은 것 -> 그 후 어떻게 됐는지 성과 집계
를 함께 보여준다.

"도지 고가를 넘으면 오른다"가 실제로 맞는지 데이터로 확인하는 용도.

실행:
  python verify.py --market kr --days 5 --universe 800
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import scan as SC

S = SC.S


def doji_kind(body_r, up_r, dn_r, rng_atr=1.0):
    """도지면 유형 문자열, 아니면 None. 비석형은 제외 대상이라 'grave' 반환."""
    if body_r > S["BODY_MAX_PCT"]:
        return None
    if dn_r >= S["DRAGON_LOWER"] and up_r <= S["DRAGON_UPPER"]:
        return "잠자리"
    if up_r >= S["DRAGON_LOWER"] and dn_r <= S["DRAGON_UPPER"]:
        return "grave"
    # 위·아래 균형형: 범위가 평소(ATR)보다 크게 벌어졌으면 롱레그
    if rng_atr >= 1.5:
        return "롱레그"
    return "십자"


def analyze(df, code, name, market, days, marcap=None):
    """최근 days 봉 각각을 후보로 판정하고, 이후 결과까지 추적."""
    if df is None or len(df) < 80:
        return []
    d = df.copy()
    d.columns = [str(x).capitalize() for x in d.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(d.columns):
        return []
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < 80:
        return []

    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    hi52 = h.rolling(min(250, len(d))).max()
    vol20 = v.rolling(20).mean()
    val20 = (c * v).rolling(20).mean()
    rngs = (h - l).replace(0, np.nan)
    body_r = (c - o).abs() / rngs * 100
    up_r = (h - pd.concat([o, c], axis=1).max(axis=1)) / rngs * 100
    dn_r = (pd.concat([o, c], axis=1).min(axis=1) - l) / rngs * 100
    volr = v / vol20

    A = d[["Open", "High", "Low", "Close", "Volume"]].values
    n = len(d)
    min_val = S["MIN_VALUE_KR"] if market == "kr" else S["MIN_VALUE_US"]
    min_px = S["MIN_PRICE_KR"] if market == "kr" else S["MIN_PRICE_US"]
    out = []

    for back in range(1, days + 1):
        i = n - back                      # -1 = 최근 봉
        if i < 70:
            continue
        O, H, L, C = A[i, 0], A[i, 1], A[i, 2], A[i, 3]
        av, m20, m60 = atr.iloc[i], ma20.iloc[i], ma60.iloc[i]
        if not all(np.isfinite(x) for x in (av, m20, m60)) or not np.isfinite(val20.iloc[i]):
            continue
        if val20.iloc[i] < min_val or C < min_px:
            continue
        rg = H - L
        if rg <= 0 or rg < av * S["MIN_RANGE_ATR"]:
            continue
        vr = float(volr.iloc[i]) if np.isfinite(volr.iloc[i]) else 0
        if vr < S["VOL_MIN"]:
            continue

        kind = doji_kind(float(body_r.iloc[i]), float(up_r.iloc[i]),
                         float(dn_r.iloc[i]), rg / av if av > 0 else 1.0)
        if kind == "grave":
            continue

        pats = []
        if kind:
            pats.append(f"도지({kind})")
        uptrend = C > m60 and m60 > (ma60.iloc[i - 10] if i >= 10 else m60)
        prev_low = A[i - 1, 2] if i >= 1 else L
        if uptrend and min(L, prev_low) <= m20 * 1.02 and C > m20 * 0.98:
            pats.append("MA20눌림")
        if C >= float(hi52.iloc[i]) * 0.99 and vr >= 1.5:
            pats.append("52주신고가")
        if vr >= S["VOL_SURGE"] and C > O and C > m20:
            pats.append("거래량급증")
        if not pats:
            continue

        entry = H * 1.002
        # 손절 = 도지 직전 캔들 몸통의 위쪽 끝 (양봉이면 종가, 음봉이면 시가)
        pO, pC = A[i - 1, 0], A[i - 1, 3]
        stop1 = max(pO, pC)
        if stop1 >= entry:                     # 직전 몸통이 도지 위 -> 무효
            continue
        stop2 = L - max(av * 0.15, entry * 0.001)
        risk = entry - stop1
        if risk <= 0:
            continue
        sp = risk / entry * 100
        if sp > S["MAX_STOP1_PCT"]:
            continue

        # ── 이후 결과 추적 ──
        status, brk_day, mx, res = "대기", None, None, ""
        for j in range(i + 1, n):
            if A[j, 2] <= stop2:                  # 도지 저가 이탈 -> 무효
                status = "무효"
                brk_day = j - i
                break
            if A[j, 1] >= entry:                  # 돌파
                status = "돌파"
                brk_day = j - i
                mx = float(A[j:n, 1].max())
                # 종가 기준 판정: 장중 스침은 무시
                res = "보유중"
                for k in range(j, n):
                    if A[k, 1] >= entry + risk:    # 목표 도달
                        res = "+1R달성"
                        break
                    if A[k, 3] <= stop1:           # 종가가 손절선 아래
                        res = "손절"
                        break
                break

        px = (lambda x: int(round(x))) if market == "kr" else (lambda x: round(x, 2))
        out.append(dict(
            code=code, name=name, date=str(d.index[i])[:10], dback=back,
            pats="+".join(pats), kind=kind or "-",
            close=px(C), entry=px(entry), stop1=px(stop1), stop2=px(stop2),
            tgt=px(entry + risk), stop_pct=round(sp, 2),
            vol_ratio=round(vr, 2), value=(int(val20.iloc[i] / 1e8) if market == "kr"
                                           else round(val20.iloc[i] / 1e6, 1)),
            status=status, days_to=brk_day,
            max_after=(px(mx) if mx else None),
            gain=(round((mx / entry - 1) * 100, 2) if mx else None),
            result=res, last=px(A[-1, 3]),
        ))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--universe", type=int, default=800)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    mk = a.market
    unit = "원" if mk == "kr" else "$"

    print(f"[1/3] 유니버스 ({mk.upper()} {a.universe})...", flush=True)
    uni = SC.universe_kr(a.universe) if mk == "kr" else SC.universe_us(a.universe)
    print(f"      {len(uni)}종목", flush=True)

    print(f"[2/3] 일봉 수집 · 최근 {a.days}일 판정...", flush=True)
    rows, done = [], 0
    if mk == "kr":
        end = datetime.today()
        s = (end - timedelta(days=400)).strftime("%Y-%m-%d")
        e = end.strftime("%Y-%m-%d")
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(SC.fetch_kr, c, s, e): (c, n, m) for c, n, m in uni}
            for f in as_completed(futs):
                c, n, m = futs[f]
                done += 1
                if done % 200 == 0:
                    print(f"      {done}/{len(uni)}", flush=True)
                try:
                    rows.extend(analyze(f.result(), c, n, mk, a.days, m))
                except Exception:
                    pass
    else:
        meta = {c: n for c, n, _ in uni}
        syms = list(meta.keys())
        for k in range(0, len(syms), 150):
            for t, dd in SC.fetch_us_batch(syms[k:k + 150]).items():
                try:
                    rows.extend(analyze(dd, t, meta.get(t, t), mk, a.days))
                except Exception:
                    pass
            print(f"      {min(k + 150, len(syms))}/{len(syms)}", flush=True)

    if not rows:
        print("후보 0건")
        return

    df = pd.DataFrame(rows)
    os.makedirs("results", exist_ok=True)
    df.to_csv(f"results/verify_{mk}.csv", index=False, encoding="utf-8-sig")

    wait = df[df["status"] == "대기"].sort_values(["dback", "stop_pct"])
    brk = df[df["status"] == "돌파"]
    void = df[df["status"] == "무효"]
    dj = df[df["kind"] != "-"]
    dj_brk = dj[dj["status"] == "돌파"]

    L = []
    L.append(f"[최근 {a.days}일 도지 검증 · {'국장' if mk == 'kr' else '미장'}]")
    L.append(f"기준일 {df['date'].max()} · 전체 후보 {len(df)}건")
    L.append("")
    L.append("━━ 결과 집계 ━━")
    L.append(f"돌파 {len(brk)}건 · 대기 {len(wait)}건 · 무효 {len(void)}건")
    if len(brk):
        r1 = (brk["result"] == "+1R달성").sum()
        sl = (brk["result"] == "손절").sum()
        L.append(f"돌파 후 +1R 달성 {r1}건 ({r1/len(brk)*100:.0f}%) · "
                 f"손절 {sl}건 ({sl/len(brk)*100:.0f}%)")
        L.append(f"돌파까지 평균 {brk['days_to'].mean():.1f}일 · "
                 f"돌파 후 최대 상승 평균 {brk['gain'].mean():+.2f}%")
    if len(dj):
        L.append("")
        L.append(f"도지만 따로: {len(dj)}건 중 돌파 {len(dj_brk)}건 "
                 f"({len(dj_brk)/len(dj)*100:.0f}%)")
        if len(dj_brk):
            L.append(f"  도지 돌파 후 최대상승 평균 {dj_brk['gain'].mean():+.2f}% · "
                     f"+1R 달성 {(dj_brk['result']=='+1R달성').sum()}건")
        kc = dj["kind"].value_counts().to_dict()
        L.append("  유형: " + " · ".join(f"{k} {v}" for k, v in kc.items()))

    if len(brk):
        import numpy as _np
        L.append("")
        L.append("━━ 손절폭 분포 (직전캔들 몸통 기준) ━━")
        sp = df["stop_pct"].values
        L.append(f"  중앙 {_np.median(sp):.2f}% · "
                 f"하위25% {_np.percentile(sp, 25):.2f}% · "
                 f"상위75% {_np.percentile(sp, 75):.2f}%")
        L.append("")
        L.append("━━ 목표 도달률 (돌파분 기준) ━━")
        L.append("  목표    도달")
        for cap in (1.0, 1.5, 2.0, 3.0, 5.0):
            g = brk["gain"].dropna()
            hit = (g >= cap).sum()
            L.append(f"  +{cap:3.1f}%   {hit:3d}/{len(g)}건 ({hit/max(len(g),1)*100:3.0f}%)")

    L.append("")
    L.append("━━ 아직 대기 중 (감시 대상) ━━")
    if len(wait) == 0:
        L.append("없음")
    for _, r in wait.head(12).iterrows():
        L.append(f"\n{r['name'][:18]} ({r['code']})  [{r['pats']}]")
        L.append(f"  도지 발생 {r['date']} ({r['dback']}거래일 전)")
        L.append(f"  거래량 {r['vol_ratio']}배 · "
                 f"거래대금 {r['value']}{'억' if mk=='kr' else 'M$'}")
        L.append(f"  진입 {r['entry']:,} · 익절 {r['tgt']:,} · "
                 f"손절 {r['stop1']:,} (-{r['stop_pct']}%)")
        L.append(f"  현재 {r['last']:,} (진입까지 "
                 f"{(r['entry']/r['last']-1)*100:+.2f}%)")

    if len(brk):
        L.append("")
        L.append("━━ 이미 돌파 (참고) ━━")
        for _, r in brk.sort_values("gain", ascending=False).head(12).iterrows():
            L.append(f"\n{r['name'][:16]} [{r['pats']}]")
            L.append(f"  도지 {r['date']} → {r['days_to']}거래일 후 돌파 "
                     f"(진입 {r['entry']:,})")
            L.append(f"  이후 최고 {r['max_after']:,} ({r['gain']:+.2f}%) · {r['result']}")

    if len(void):
        L.append("")
        L.append("━━ 무효 (도지 저가 이탈) ━━")
        for _, r in void.head(8).iterrows():
            L.append(f"{r['name'][:16]} · 도지 {r['date']} → "
                     f"{r['days_to']}거래일 후 저가 이탈")

    txt = "\n".join(L)
    print("\n" + txt)
    with open(f"results/verify_{mk}.txt", "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"\nresults/verify_{mk}.txt · .csv 저장")


if __name__ == "__main__":
    sys.exit(main())
