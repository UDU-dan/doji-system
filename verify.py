# -*- coding: utf-8 -*-
"""
패턴 검증 스캐너 (일봉 기준 당일 단타)

최근 N일 안에 발생한 진입 후보를 모두 찾아서
  · 아직 저항선을 안 넘은 것 -> 감시 대상
  · 이미 넘은 것 -> 당일 성과 중심으로 집계

패턴
  도지        도지 캔들의 고가가 저항선
  삼봉        봉우리 3개 중 가운데가 가장 높을 때, 그 고점이 저항선
  다중저항    1년 내 비슷한 가격에서 2회 이상 막힌 가격대

손절  = 진입 신호 캔들의 직전 캔들 몸통 위쪽 (양봉=종가 / 음봉=시가)
익절  = 손절폭만큼 위 (1R)

실행: python verify.py --market kr --days 60 --universe 800
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import scan as SC

S = SC.S

# 패턴 파라미터
SWING_N = 5          # 좌우 이 일수보다 높으면 스윙 고점
PROMINENCE = 2.0     # 봉우리가 주변 저점보다 이 % 이상 튀어나와야 인정
LOOKBACK = 250       # 저항선 탐색 기간 (약 1년)
CLUSTER_PCT = 1.5    # 이 % 안이면 같은 저항선
MIN_GAP = 15         # 고점 간 최소 간격(거래일)
MIN_TOUCH = 2        # 최소 터치 횟수
RES_NEAR = 3.0       # 현재가가 저항선 이 % 아래일 때만 후보
RES_STOP_BUF = 1.5   # 저항선 돌파형 손절: 저항선 아래 이 %
STOP_MIN = 1.0       # 손절폭 하한 %
STOP_MAX = 3.0       # 손절폭 상한 %


def doji_kind(body_r, up_r, dn_r, rng_atr=1.0):
    if body_r > S["BODY_MAX_PCT"]:
        return None
    if dn_r >= S["DRAGON_LOWER"] and up_r <= S["DRAGON_UPPER"]:
        return "잠자리"
    if up_r >= S["DRAGON_LOWER"] and dn_r <= S["DRAGON_UPPER"]:
        return "grave"
    return "롱레그" if rng_atr >= 1.5 else "십자"


def swing_highs(highs, upto, n=SWING_N, lookback=LOOKBACK):
    """upto 인덱스까지의 스윙 고점 [(idx, price), ...]"""
    out = []
    start = max(n, upto - lookback)
    for k in range(start, upto - n + 1):
        w = highs[k - n:k + n + 1]
        if len(w) < 2 * n + 1:
            continue
        # 엄격 판정: 양옆보다 확실히 높고, 주변 대비 충분히 돌출돼야 함
        if not (highs[k] == w.max() and highs[k] > highs[k - n] and highs[k] > highs[k + n]):
            continue
        if w.min() <= 0 or (highs[k] / w.min() - 1) * 100 < PROMINENCE:
            continue
        if True:
            if out and k - out[-1][0] < MIN_GAP:
                if highs[k] > out[-1][1]:
                    out[-1] = (k, float(highs[k]))
                continue
            out.append((k, float(highs[k])))
    return out


def find_resistances(peaks, price):
    """(저항가, 터치수, 종류) 목록. 현재가 위쪽 저항만."""
    res = []
    if len(peaks) < 2:
        return res

    # 다중 저항: 비슷한 가격끼리 묶기
    used = [False] * len(peaks)
    for a in range(len(peaks)):
        if used[a]:
            continue
        grp = [peaks[a][1]]
        gidx = [peaks[a][0]]
        used[a] = True
        for b in range(a + 1, len(peaks)):
            if used[b]:
                continue
            if abs(peaks[b][1] - peaks[a][1]) / peaks[a][1] * 100 <= CLUSTER_PCT:
                grp.append(peaks[b][1])
                gidx.append(peaks[b][0])
                used[b] = True
        if len(grp) >= MIN_TOUCH:
            lvl = float(np.mean(grp))
            if price < lvl <= price * (1 + RES_NEAR / 100):
                res.append((lvl, len(grp), "다중저항", list(gidx)))

    # 삼봉: 연속 3개 중 가운데가 최고
    for k in range(len(peaks) - 2):
        p1, p2, p3 = peaks[k][1], peaks[k + 1][1], peaks[k + 2][1]
        if p2 > p1 and p2 > p3:
            if price < p2 <= price * (1 + RES_NEAR / 100):
                res.append((float(p2), 3, "삼봉",
                            [peaks[k][0], peaks[k + 1][0], peaks[k + 2][0]]))
    return res


def analyze(df, code, name, market, days, marcap=None):
    if df is None or len(df) < 140:
        return []
    d = df.copy()
    d.columns = [str(x).capitalize() for x in d.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(d.columns):
        return []
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < 140:
        return []

    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    ma20 = c.rolling(20).mean()
    vol20 = v.rolling(20).mean()
    val20 = (c * v).rolling(20).mean()
    rngs = (h - l).replace(0, np.nan)
    body_r = (c - o).abs() / rngs * 100
    up_r = (h - pd.concat([o, c], axis=1).max(axis=1)) / rngs * 100
    dn_r = (pd.concat([o, c], axis=1).min(axis=1) - l) / rngs * 100
    volr = v / vol20

    A = d[["Open", "High", "Low", "Close", "Volume"]].values
    HI = d["High"].values
    n = len(d)
    min_val = S["MIN_VALUE_KR"] if market == "kr" else S["MIN_VALUE_US"]
    min_px = S["MIN_PRICE_KR"] if market == "kr" else S["MIN_PRICE_US"]
    out = []

    for back in range(1, days + 1):
        i = n - back
        if i < 130:
            continue
        O, H, L, C = A[i, 0], A[i, 1], A[i, 2], A[i, 3]
        av, m20 = atr.iloc[i], ma20.iloc[i]
        if not np.isfinite(av) or not np.isfinite(m20) or not np.isfinite(val20.iloc[i]):
            continue
        if val20.iloc[i] < min_val or C < min_px:
            continue
        rg = H - L
        if rg <= 0:
            continue
        vr = float(volr.iloc[i]) if np.isfinite(volr.iloc[i]) else 0.0

        # 후보 목록: (패턴명, 저항가, 부가정보)
        cands = []

        # ① 도지
        if rg >= av * S["MIN_RANGE_ATR"] and vr >= S["VOL_MIN"]:
            k = doji_kind(float(body_r.iloc[i]), float(up_r.iloc[i]),
                          float(dn_r.iloc[i]), rg / av if av > 0 else 1.0)
            if k and k != "grave":
                cands.append((f"도지({k})", H, "도지",
                              f"{str(d.index[i])[5:10]} 고가 {int(round(H)):,}"))

        # ② 삼봉 / ③ 다중저항
        pk = swing_highs(HI, i)
        for lvl, touch, kind, idxs in find_resistances(pk, C):
            pts = " / ".join(f"{str(d.index[q])[5:10]} {int(round(HI[q])):,}"
                             for q in sorted(idxs))
            cands.append((f"{kind}({touch}회)" if kind == "다중저항" else kind,
                          lvl, kind, pts))

        if not cands:
            continue

        pO, pC = A[i - 1, 0], A[i - 1, 3]
        body_top = max(pO, pC)

        for pname, lvl, ptype, pts in cands:
            entry = lvl * 1.002
            if ptype == "도지":
                base = body_top                          # 직전 캔들 몸통 위쪽
            else:
                base = lvl * (1 - RES_STOP_BUF / 100)    # 저항선 아래 버퍼
                base = max(base, min(body_top, lvl))     # 몸통이 더 가까우면 그걸로
            stop1 = min(base, entry * (1 - STOP_MIN / 100))
            if stop1 >= entry:
                continue
            risk = entry - stop1
            sp = risk / entry * 100
            if sp <= 0 or sp > STOP_MAX:
                continue

            status, brk_day, res = "대기", None, ""
            d0_hi = d0_cl = d1_hi = mx = None
            stop2 = L - max(av * 0.15, entry * 0.001)

            for j in range(i + 1, n):
                if A[j, 3] <= stop1:                  # 종가가 손절선 아래 -> 무효
                    status, brk_day = "무효", j - i
                    break
                if A[j, 1] >= entry:                 # 장중 돌파
                    status, brk_day = "돌파", j - i
                    mx = float(A[j:n, 1].max())
                    d0_hi = (A[j, 1] / entry - 1) * 100
                    d0_cl = (A[j, 3] / entry - 1) * 100
                    d1_hi = ((max(A[j, 1], A[j + 1, 1]) / entry - 1) * 100
                             if j + 1 < n else d0_hi)
                    res = "보유중"
                    for k2 in range(j, n):
                        if A[k2, 1] >= entry + risk:
                            res = "+1R달성"
                            break
                        if A[k2, 3] <= stop1:
                            res = "손절"
                            break
                    break

            px = (lambda x: int(round(x))) if market == "kr" else (lambda x: round(x, 2))
            out.append(dict(
                code=code, name=name, date=str(d.index[i])[:10], dback=back,
                pattern=pname, ptype=ptype,
                close=px(C), entry=px(entry), stop1=px(stop1), stop2=px(stop2),
                points=pts,
                tgt=px(entry + risk), stop_pct=round(sp, 2),
                vol_ratio=round(vr, 2),
                value=(int(val20.iloc[i] / 1e8) if market == "kr"
                       else round(val20.iloc[i] / 1e6, 1)),
                status=status, days_to=brk_day, result=res,
                d0_hi=(round(d0_hi, 2) if d0_hi is not None else None),
                d0_cl=(round(d0_cl, 2) if d0_cl is not None else None),
                d1_hi=(round(d1_hi, 2) if d1_hi is not None else None),
                gain=(round((mx / entry - 1) * 100, 2) if mx else None),
                last=px(A[-1, 3]),
            ))
    return out


def merge_dup(x):
    """같은 종목·같은 날 여러 패턴 -> 진입가 낮은 것 기준으로 통합"""
    if len(x) == 0:
        return x

    def _m(g):
        g = g.sort_values("entry")
        b = g.iloc[0].copy()
        b["pattern"] = " + ".join(dict.fromkeys(g["pattern"]))
        return b

    return (x.groupby(["code", "date"], group_keys=True)
             .apply(_m, include_groups=False).reset_index())


def merge_and_score(wait, mk):
    """같은 종목의 여러 패턴을 하나로 합치고 점수를 매긴다."""
    out = []
    for code, g in wait.groupby("code"):
        g = g.sort_values("stop_pct")
        r = g.iloc[0].to_dict()
        pats = sorted(set(g["pattern"]))
        pts = []
        for p in g["points"]:
            for x in str(p).split(" / "):
                if x and x not in pts:
                    pts.append(x)
        togo = (r["entry"] / r["last"] - 1) * 100

        # 점수: 패턴 수 · 터치 수 · 손절폭 · 근접도 · 거래대금
        n_pat = len(pats)
        touch = sum(int(x.split("(")[1].split("회")[0])
                    for x in pats if "다중저항" in x and "회" in x) or 0
        sc_pat = min(n_pat, 3) / 3 * 100
        sc_touch = min(touch / 4, 1.0) * 100
        sc_stop = max(0.0, 1 - r["stop_pct"] / STOP_MAX) * 100
        sc_near = max(0.0, 1 - abs(togo) / 3.0) * 100
        base = 1e9 if mk == "kr" else 3e7
        sc_liq = float(np.clip(np.log10(max(r["value"] * (1e8 if mk == "kr" else 1e6), 1)
                                        / base) / 1.5, 0, 1) * 100)
        score = (sc_pat * 25 + sc_touch * 15 + sc_stop * 25 +
                 sc_near * 25 + sc_liq * 10) / 100

        r["pattern"] = " + ".join(pats)
        r["points"] = pts[:5]
        r["togo"] = round(togo, 2)
        r["score"] = round(score, 1)
        out.append(r)
    return sorted(out, key=lambda x: -x["score"])


def report(df, mk, days):
    L = []
    L.append(f"[최근 {days}일 패턴 검증 · {'국장' if mk == 'kr' else '미장'}]")
    L.append(f"기준일 {df['date'].max()} · 전체 후보 {len(df)}건")

    brk = df[df["status"] == "돌파"]
    wait = df[df["status"] == "대기"]
    void = df[df["status"] == "무효"]

    L.append("")
    L.append("━━ 전체 ━━")
    L.append(f"돌파 {len(brk)} · 대기 {len(wait)} · 무효 {len(void)}")

    L.append("")
    L.append("━━ 패턴별 성과 (당일 단타 기준) ━━")
    L.append(f"{'패턴':<10}{'후보':>5}{'돌파':>6}{'돌파율':>7}"
             f"{'당일+1%':>8}{'종가+':>7}{'당일최고':>9}")
    for pt, g in df.groupby("ptype"):
        b = g[g["status"] == "돌파"]
        if len(b) == 0:
            L.append(f"{pt:<10}{len(g):>5}{0:>6}      -")
            continue
        h0 = b["d0_hi"].dropna()
        c0 = b["d0_cl"].dropna()
        r1 = (h0 >= 1.0).mean() * 100 if len(h0) else 0
        cp = (c0 > 0).mean() * 100 if len(c0) else 0
        L.append(f"{pt:<10}{len(g):>5}{len(b):>6}{len(b)/len(g)*100:>6.0f}%"
                 f"{r1:>7.0f}%{cp:>6.0f}%{h0.mean():>+8.2f}%")

    if len(brk):
        h0 = brk["d0_hi"].dropna()
        c0 = brk["d0_cl"].dropna()
        h1 = brk["d1_hi"].dropna()
        L.append("")
        L.append("━━ 전체 돌파분 상세 ━━")
        L.append(f"  당일 최고 평균 {h0.mean():+.2f}% · 중앙 {h0.median():+.2f}%")
        L.append(f"  당일 종가 평균 {c0.mean():+.2f}% · 중앙 {c0.median():+.2f}%")
        L.append(f"  당일 종가가 진입가 위: {(c0>0).sum()}/{len(c0)}건 "
                 f"({(c0>0).mean()*100:.0f}%)")
        L.append("  당일 도달률: " + " · ".join(
            f"+{x}% {(h0>=x).sum()}건({(h0>=x).mean()*100:.0f}%)" for x in (0.5, 1, 2, 3)))
        L.append(f"  익일까지 최고 평균 {h1.mean():+.2f}%")
        L.append(f"  손절폭 중앙 {df['stop_pct'].median():.2f}% · "
                 f"돌파까지 평균 {brk['days_to'].mean():.1f}일")
        L.append("")
        L.append("  손절폭별 (당일 최고가가 손절폭 이상 = 1R 달성)")
        for cap in (1.0, 1.5, 2.0, 2.5, 3.0):
            sub = brk[brk["stop_pct"] <= cap]
            if len(sub) < 5:
                continue
            hh = sub["d0_hi"].dropna()
            ok = (hh >= sub.loc[hh.index, "stop_pct"]).mean() * 100
            L.append(f"    ~{cap:.1f}%  {len(sub):3d}건 · 당일 1R달성 {ok:3.0f}% · "
                     f"당일최고 평균 {hh.mean():+.2f}%")

        L.append("")
        L.append("━━ 월별 ━━")
        t = df.copy()
        t["ym"] = t["date"].str[:7]
        for ym, g in t.groupby("ym"):
            b = g[g["status"] == "돌파"]
            hh = b["d0_hi"].dropna()
            L.append(f"  {ym}  후보 {len(g):3d} · 돌파 {len(b):3d} "
                     f"({len(b)/len(g)*100:3.0f}%)"
                     + (f" · 당일+1% {(hh>=1).mean()*100:3.0f}%" if len(hh) else ""))

    L.append("")
    L.append("━━ 대기 중 · 점수 상위 10 ━━")
    if len(wait) == 0:
        L.append("없음")
    else:
        for k, r in enumerate(merge_and_score(wait, mk)[:10], 1):
            L.append(f"\n{k}위 {r['name'][:18]} ({r['code']})  {r['score']}점")
            L.append(f"  [{r['pattern']}] · 신호 {r['date']}")
            for p in r["points"]:
                L.append(f"    · {p}")
            L.append(f"  진입 {r['entry']:,} · 익절 {r['tgt']:,} · "
                     f"손절 {r['stop1']:,} (-{r['stop_pct']}%)")
            L.append(f"  현재 {r['last']:,} (진입까지 {r['togo']:+.2f}%) · "
                     f"거래대금 {r['value']}{'억' if mk=='kr' else 'M$'}")

    if len(brk):
        L.append("")
        L.append("━━ 이미 돌파 (당일 성과순) ━━")
        for _, r in merge_dup(brk).sort_values("d0_hi", ascending=False).head(10).iterrows():
            L.append(f"\n{r['name'][:16]} [{r['pattern']}] {r['date']}")
            L.append(f"  {r['days_to']}일 후 돌파 (진입 {r['entry']:,}) · "
                     f"당일 최고 {r['d0_hi']:+.2f}% / 종가 {r['d0_cl']:+.2f}%")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--universe", type=int, default=800)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    mk = a.market

    print(f"[1/3] 유니버스 ({mk.upper()} {a.universe})...", flush=True)
    uni = SC.universe_kr(a.universe) if mk == "kr" else SC.universe_us(a.universe)
    print(f"      {len(uni)}종목", flush=True)

    print(f"[2/3] 일봉 수집 · 최근 {a.days}일 판정...", flush=True)
    rows, done = [], 0
    if mk == "kr":
        end = datetime.today()
        s = (end - timedelta(days=700)).strftime("%Y-%m-%d")
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
            for t, dd in SC.fetch_us_batch(syms[k:k + 150], period="2y").items():
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
    txt = report(df, mk, a.days)
    print("\n" + txt)
    with open(f"results/verify_{mk}.txt", "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"\nresults/verify_{mk}.txt 저장")


if __name__ == "__main__":
    sys.exit(main())
