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
# ══════ 봉우리 판정 (교본: "누가 봐도 3개") ══════
SWING_N = 10         # 좌우 이 일수보다 높아야 스윙 고점
PROMINENCE = 10.0    # 주변 저점 대비 이 % 이상 튀어나와야 봉우리
VALLEY_MIN = 5.0     # 봉우리 사이 골이 이 % 이상 깊어야 별개 봉우리
MIN_GAP = 20         # 봉우리 간 최소 간격(거래일)
LOOKBACK = 1250      # 저항선 탐색 기간 (약 5년)

# ══════ 패턴 1. 삼봉언덕 (교본: 세 고점이 대략 수평) ══════
FLAT_PCT = 5.0       # 세 고점의 최대 편차 % (수평 판정)

# ══════ 패턴 2. 가운데자리 (교본: 호가 1~2칸, 0.1~0.2%) ══════
CLUSTER_PCT = 0.2    # 교본: 호가 1~2칸 (0.1~0.2%)

# ══════ 공통 필터 ══════
UPPER_CHECK = 15.0   # 저항선 위 이 % 이내에 더 높은 고점 있으면 제외
MA240_FILTER = True  # 현재가가 240일선 위인 종목만
RISE_MIN = 2.0       # 고점상승: 직전 고점보다 이 % 이상 높아야

# ══════ 패턴 3. 양양음 / 패턴 4. 또로록 ══════
MA5_LEN = 5          # 5주기 이평 (교본: 일봉=5일)
TORO_UP_MIN = 3.0    # 또로록: 조정 전 상승폭이 이 % 이상이어야 "상승하던" 것으로 인정
MIN_TOUCH = 2        # 최소 터치 횟수
RES_NEAR = 5.0       # 현재가가 저항선 이 % 아래일 때만 후보
RES_STOP_BUF = 2.5   # 저항선 돌파형 손절: 저항선 아래 이 %
STOP_MIN = 2.0       # 손절폭 하한 % (교본: 2~3%)
STOP_MAX = 3.0       # 손절폭 상한 %
TARGET_PCT = 5.0     # 1차 익절 목표 % (교본: +5%)
RR_MIN = 1.5         # 손익비 하한 (교본: +5% / 손절폭 >= 1.5)


BODY_MAX = 5.0       # 도지 몸통 최대 비율 %


def yang_yang_eum(A, ma5v, i):
    """
    패턴 3. 양양음 (교본 4조건 전부 충족)
      1) 음봉 뒤 양봉 2개 연속
      2) 둘째 양봉이 5주기 이평 상향 돌파
      3) 뒤따르는 음봉의 저점이 5선 아래로 이탈하지 않음
      4) 그 음봉의 고점이 둘째 양봉 고점을 넘지 않음
    반환: 저항선(둘째 양봉 고가) 또는 None
    """
    if i < 4:
        return None
    e0, y1, y2, cur = i - 3, i - 2, i - 1, i          # 음 양 양 음
    O, H, L, C = 0, 1, 2, 3
    if not (A[e0, C] < A[e0, O]):                     # 1) 첫 봉 음봉
        return None
    if not (A[y1, C] > A[y1, O] and A[y2, C] > A[y2, O]):   # 양봉 2개
        return None
    m2 = ma5v[y2]
    if not np.isfinite(m2):
        return None
    if not (A[y2, C] > m2 and A[y1, C] <= ma5v[y1]):  # 2) 둘째가 5선 상향 돌파
        return None
    if not (A[cur, C] < A[cur, O]):                   # 마지막 봉 음봉
        return None
    mc = ma5v[cur]
    if not np.isfinite(mc) or A[cur, L] < mc:         # 3) 5선 이탈 금지
        return None
    if A[cur, H] > A[y2, H]:                          # 4) 둘째 양봉 고점 안 넘음
        return None
    return float(A[y2, H])


def ttororok(A, i):
    """
    패턴 4. 또로록 (흑삼병)
      상승하던 주가가 음봉 3개로 조정 -> 조정 시작 전 고점이 저항선
    반환: (저항선, 고점인덱스) 또는 None
    """
    if i < 8:
        return None
    O, H, L, C = 0, 1, 2, 3
    for k in (i, i - 1, i - 2):                       # 최근 3봉이 모두 음봉
        if A[k, C] >= A[k, O]:
            return None
    if not (A[i, C] < A[i - 1, C] < A[i - 2, C]):     # 계단식 하락
        return None
    start = i - 3                                     # 조정 직전 봉
    hi_i, hi_v = start, A[start, H]
    for k in range(max(0, start - 4), start + 1):     # 직전 고점 탐색
        if A[k, H] > hi_v:
            hi_i, hi_v = k, A[k, H]
    lo_before = A[max(0, hi_i - 5):hi_i + 1, L].min()
    if lo_before <= 0 or (hi_v / lo_before - 1) * 100 < TORO_UP_MIN:
        return None                                   # "상승하던" 조건 미충족
    return float(hi_v), hi_i


def doji_kind(body_r, up_r, dn_r, rng_atr=1.0):
    if body_r > min(BODY_MAX, S.get("BODY_MAX_PCT", BODY_MAX)):
        return None
    if dn_r >= S["DRAGON_LOWER"] and up_r <= S["DRAGON_UPPER"]:
        return "잠자리"
    if up_r >= S["DRAGON_LOWER"] and dn_r <= S["DRAGON_UPPER"]:
        return "grave"
    return "롱레그" if rng_atr >= 1.5 else "십자"


def swing_highs(highs, upto, lows=None, n=SWING_N, lookback=LOOKBACK):
    """
    upto 인덱스까지의 스윙 고점 [(idx, price), ...]

    교본 "누가 봐도 봉우리" 를 세 조건으로 구현한다.
      1) 좌우 n일보다 확실히 높다
      2) 주변 저점 대비 PROMINENCE % 이상 돌출
      3) 앞 봉우리와의 사이에 VALLEY_MIN % 이상의 골이 있다
    """
    if lows is None:
        lows = highs
    cand = []
    start = max(n, upto - lookback)
    for k in range(start, upto - n + 1):
        w = highs[k - n:k + n + 1]
        if len(w) < 2 * n + 1:
            continue
        if not (highs[k] == w.max() and highs[k] > highs[k - n]
                and highs[k] > highs[k + n]):
            continue
        # 돌출도는 봉우리 간 최소간격 범위의 저점 기준으로 측정
        lo_w = lows[max(0, k - MIN_GAP):k + MIN_GAP + 1]
        base = lo_w.min() if len(lo_w) else 0
        if base <= 0 or (highs[k] / base - 1) * 100 < PROMINENCE:
            continue
        cand.append((k, float(highs[k])))

    # 간격 · 골 깊이로 정리
    out = []
    for k, px in cand:
        if not out:
            out.append((k, px))
            continue
        pk, ppx = out[-1]
        gap_ok = (k - pk) >= MIN_GAP
        valley = lows[pk:k + 1].min() if k > pk else px
        deep_ok = valley > 0 and (min(ppx, px) / valley - 1) * 100 >= VALLEY_MIN
        if gap_ok and deep_ok:
            out.append((k, px))                # 별개 봉우리
        elif px > ppx:
            out[-1] = (k, px)                  # 같은 덩어리 -> 더 높은 쪽으로
    return out


def has_upper_wall(peaks, level, exclude=()):
    """
    저항선 위 UPPER_CHECK % 이내에 더 높은 고점이 있으면 True (제외 대상).
    exclude 에 넣은 인덱스(= 그 패턴을 구성하는 봉우리)는 벽으로 치지 않는다.
    """
    hi = level * (1 + UPPER_CHECK / 100)
    ex = set(exclude)
    return any(i not in ex and level * 1.002 < p < hi for i, p in peaks)


def find_resistances(peaks, price):
    """(저항가, 터치수, 종류, 지점인덱스) 목록. 현재가 바로 위 저항만."""
    res = []
    if len(peaks) < 2:
        return res

    def usable(lvl, own=(), tag=None):
        if not (price < lvl <= price * (1 + RES_NEAR / 100)):
            if tag:
                PAT_STATS[f"{tag}_near_fail"] += 1
            return False
        if has_upper_wall(peaks, lvl, own):
            if tag:
                PAT_STATS[f"{tag}_wall_fail"] += 1
            return False
        return True

    # ── 다중저항 : 완전히 같은 가격에서 2회 이상 ──
    used = [False] * len(peaks)
    for a in range(len(peaks)):
        if used[a]:
            continue
        grp, gidx = [peaks[a][1]], [peaks[a][0]]
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
            if max(abs(x - lvl) / lvl * 100 for x in grp) > CLUSTER_PCT:
                continue
            PAT_STATS["mid_pair"] += 1
            if usable(lvl, gidx, "mid"):
                PAT_STATS["mid_ok"] += 1
                res.append((lvl, len(grp), "다중저항", list(gidx)))

    # ── 삼봉언덕 : 연속 3개 고점이 대략 수평, 넥라인 = 최고점 ──
    for k in range(len(peaks) - 2):
        tri = [peaks[k], peaks[k + 1], peaks[k + 2]]
        ps = [p for _, p in tri]
        lo, hi = min(ps), max(ps)
        if lo <= 0:
            continue
        if (hi / lo - 1) * 100 > FLAT_PCT:      # 수평이 아니면 삼봉 아님
            PAT_STATS["tri_flat_fail"] += 1
            continue
        neck = hi                               # 넥라인 = 3봉 최고점
        idxs = [i for i, _ in tri]
        if usable(neck, idxs, "tri"):
            PAT_STATS["tri_ok"] += 1
            res.append((float(neck), 3, "삼봉", idxs))

    # ── 고점상승 : 직전 고점보다 다음 고점이 높은 계단식 구조 ──
    for k in range(len(peaks) - 1):
        p1, p2 = peaks[k][1], peaks[k + 1][1]
        if p2 < p1 * (1 + RISE_MIN / 100):
            continue                                   # 충분히 안 올라감
        if usable(p2, [peaks[k][0], peaks[k + 1][0]]):
            PAT_STATS["rise_ok"] += 1
            res.append((float(p2), 2, "고점상승",
                        [peaks[k][0], peaks[k + 1][0]]))
    return res


STATS = dict(total=0, liquidity=0, doji=0, peaks=0, stopwidth=0, wait=0)
PAT_STATS = dict(
    peak_cnt=[],        # 종목별 봉우리 개수 분포
    tri_flat_fail=0,    # 삼봉: 수평 조건 탈락
    tri_wall_fail=0,    # 삼봉: 위쪽 벽
    tri_near_fail=0,    # 삼봉: 현재가 거리
    tri_ok=0,
    mid_pair=0,         # 가운데자리: 동일가 쌍 발견
    mid_wall_fail=0,
    mid_near_fail=0,
    mid_ok=0,
    rise_ok=0,
)


def analyze(df, code, name, market, days, marcap=None):
    if df is None or len(df) < 150:
        return []
    d = df.copy()
    d.columns = [str(x).capitalize() for x in d.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(d.columns):
        return []
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < 150:
        return []

    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    ma20 = c.rolling(20).mean()
    ma240 = c.rolling(240).mean()
    ma5 = c.rolling(MA5_LEN).mean()
    vol20 = v.rolling(20).mean()
    val20 = (c * v).rolling(20).mean()
    rngs = (h - l).replace(0, np.nan)
    body_r = (c - o).abs() / rngs * 100
    up_r = (h - pd.concat([o, c], axis=1).max(axis=1)) / rngs * 100
    dn_r = (pd.concat([o, c], axis=1).min(axis=1) - l) / rngs * 100
    volr = v / vol20

    A = d[["Open", "High", "Low", "Close", "Volume"]].values
    HI = d["High"].values
    LO = d["Low"].values
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
        STATS["total"] += 1
        if MA240_FILTER:
            m240 = ma240.iloc[i]
            if not np.isfinite(m240) or C <= m240:
                continue                       # 240일선 아래면 제외
        if val20.iloc[i] < min_val or C < min_px:
            continue
        STATS["liquidity"] += 1
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
                STATS["doji"] += 1
                cands.append((f"도지({k})", H, "도지",
                              f"{str(d.index[i])[5:10]} 고가 {int(round(H)):,}", 0))

        # ③ 양양음
        m5 = ma5.values
        yy = yang_yang_eum(A, m5, i)
        if yy:
            STATS["yye"] = STATS.get("yye", 0) + 1
            cands.append(("양양음", yy, "양양음",
                          f"{str(d.index[i-1])[5:10]} 양봉고가 {int(round(yy)):,}", 0))

        # ④ 또로록
        tt = ttororok(A, i)
        if tt:
            lvl, hidx = tt
            STATS["toro"] = STATS.get("toro", 0) + 1
            cands.append(("또로록", lvl, "또로록",
                          f"{str(d.index[hidx])[:10]} 조정전고점 {int(round(lvl)):,}",
                          i - hidx))

        # ② 삼봉 / ③ 다중저항
        pk = swing_highs(HI, i, LO)
        PAT_STATS["peak_cnt"].append(len(pk))
        for lvl, touch, kind, idxs in find_resistances(pk, C):
            pts = " / ".join(f"{str(d.index[q])[:10]} {int(round(HI[q])):,}"
                             for q in sorted(idxs))
            age_d = i - min(idxs)          # 가장 오래된 지점까지의 거래일
            STATS["peaks"] += 1
            cands.append((f"{kind}({touch}회)" if kind == "다중저항" else kind,
                          lvl, kind, pts, age_d))

        if not cands:
            continue

        pO, pC = A[i - 1, 0], A[i - 1, 3]
        body_top = max(pO, pC)

        for pname, lvl, ptype, pts, age_d in cands:
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
            if TARGET_PCT / sp < RR_MIN:        # 손익비 미달이면 진입 보류
                continue
            STATS["stopwidth"] += 1

            status, brk_day, res = "대기", None, ""
            d0_hi = d0_cl = d1_hi = mx = None
            stop2 = L - max(av * 0.15, entry * 0.001)

            below = False
            for j in range(i + 1, n):
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
                # 종가가 손절선 아래면 보류. 위로 복귀하면 다시 대기.
                below = A[j, 3] <= stop1

            if status == "대기" and below:
                status = "보류"

            px = (lambda x: int(round(x))) if market == "kr" else (lambda x: round(x, 2))
            out.append(dict(
                code=code, name=name, date=str(d.index[i])[:10], dback=back,
                pattern=pname, ptype=ptype,
                close=px(C), entry=px(entry), stop1=px(stop1), stop2=px(stop2),
                points=pts, age_days=int(age_d),
                tgt=px(entry * (1 + TARGET_PCT / 100)), stop_pct=round(sp, 2),
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


def age_tag(r):
    """저항선이 얼마나 오래됐는지 표시"""
    d = int(r.get("age_days", 0) or 0)
    if d < 250:
        return ""
    y = d / 250
    mark = "  ** " if y >= 3 else "  * "
    return f"{mark}저항선 {y:.1f}년 전 형성 - 재확인 권장"


def merge_and_score(wait, mk):
    """같은 종목·같은 저항선끼리만 합치고 점수를 매긴다."""
    out = []
    w = wait.copy()
    w["_lvl"] = w["entry"].round(0)          # 저항선(진입가) 단위로 구분
    for (code, lvl), g in w.groupby(["code", "_lvl"]):
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
        r["age_days"] = int(g["age_days"].max())
        out.append(r)
    # 같은 종목이 여러 저항선을 가지면 점수 높은 것만 남긴다
    best = {}
    for r in sorted(out, key=lambda x: -x["score"]):
        if r["code"] not in best:
            best[r["code"]] = r
    return sorted(best.values(), key=lambda x: -x["score"])


def report(df, mk, days):
    L = []
    L.append(f"[최근 {days}일 패턴 검증 · {'국장' if mk == 'kr' else '미장'}]")
    L.append(f"기준일 {df['date'].max()} · 전체 후보 {len(df)}건")

    brk = df[df["status"] == "돌파"]
    wait = df[df["status"] == "대기"]
    hold = df[df["status"] == "보류"]

    L.append("")
    L.append("━━ 전체 ━━")
    L.append(f"돌파 {len(brk)} · 대기 {len(wait)} · 보류 {len(hold)}")
    import numpy as _np
    pc = PAT_STATS["peak_cnt"]
    if pc:
        L.append("")
        L.append("━━ 패턴 진단 ━━")
        L.append(f"  봉우리 개수: 평균 {_np.mean(pc):.1f} · "
                 f"중앙 {int(_np.median(pc))} · 최대 {max(pc)} · "
                 f"3개이상 {sum(1 for x in pc if x >= 3)}/{len(pc)}")
        L.append(f"  삼봉    수평탈락 {PAT_STATS['tri_flat_fail']} · "
                 f"위쪽벽 {PAT_STATS['tri_wall_fail']} · "
                 f"거리 {PAT_STATS['tri_near_fail']} · 성공 {PAT_STATS['tri_ok']}")
        L.append(f"  가운데자리 동일가쌍 {PAT_STATS['mid_pair']} · "
                 f"위쪽벽 {PAT_STATS['mid_wall_fail']} · "
                 f"거리 {PAT_STATS['mid_near_fail']} · 성공 {PAT_STATS['mid_ok']}")
        L.append(f"  고점상승 성공 {PAT_STATS['rise_ok']}")
    L.append(f"필터 통과: 유동성 {STATS['liquidity']}/{STATS['total']} · "
             f"도지 {STATS['doji']} · 저항선 {STATS['peaks']} · "
             f"손절폭 {STATS['stopwidth']}")

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
            L.append(f"  [{r['pattern']}] · 신호 {r['date']}{age_tag(r)}")
            for p in r["points"]:
                L.append(f"    · {p}")
            L.append(f"  진입 {r['entry']:,} · 익절 {r['tgt']:,} · "
                     f"손절 {r['stop1']:,} (-{r['stop_pct']}%)")
            L.append(f"  현재 {r['last']:,} (진입까지 {r['togo']:+.2f}% 필요) · "
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
        s = (end - timedelta(days=2000)).strftime("%Y-%m-%d")
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
            for t, dd in SC.fetch_us_batch(syms[k:k + 150], period="5y").items():
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
