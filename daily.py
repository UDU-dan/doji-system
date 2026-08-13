# -*- coding: utf-8 -*-
"""
당일 관심종목 리스트 (검증된 패턴 로직 재사용)

verify.py 와 완전히 같은 기준으로 오늘 기준 후보를 뽑고,
점수 상위 종목에 뉴스·시장국면을 붙여 텔레그램용 텍스트를 만든다.

  · 최근 N일(기본 5) 안에 발생해 아직 저항선을 안 넘은 것만
  · 도지 / 삼봉 / 다중저항 통합, 같은 종목은 병합
  · 제약·바이오 제외 (국장)

실행: python daily.py --market kr --days 5 --universe 800
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

import scan as SC
import verify as VF

BIO_WORDS = ("제약", "바이오", "생명과학", "파마", "메디", "테라퓨틱스",
             "헬스케어", "신약", "백신", "진단", "의약", "의료")
BIO_SECTOR = ("제약", "바이오", "의약", "생물", "의료", "헬스",
              "생명과학", "Pharma", "Biotech", "Health")


def bio_codes_kr():
    """상장 목록의 업종 정보로 제약·바이오 종목코드 집합을 만든다."""
    out = set()
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        df.columns = [str(x) for x in df.columns]
        cc = next((x for x in df.columns if x.lower() in ("code", "symbol")), None)
        nc = next((x for x in df.columns if x.lower() == "name"), None)
        skip = {"code", "symbol", "name", "market", "markettype",
                "marcap", "stocks", "amount", "close", "changes", "changescode"}
        sec = [x for x in df.columns
               if x.lower() not in skip and df[x].dtype == object]
        if cc is None:
            return out
        for _, r in df.iterrows():
            txt = " ".join(str(r[x]) for x in sec if pd.notna(r.get(x, None)))
            nm = str(r[nc]) if nc else ""
            if any(w in txt for w in BIO_SECTOR) or any(w in nm for w in BIO_WORDS):
                out.add(_norm(r[cc]))
        # 진단: 대형 바이오 몇 종목이 어떤 업종으로 분류되는지 확인
        probe = {"196170": "알테오젠", "068270": "셀트리온",
                 "207940": "삼성바이오로직스", "145020": "휴젤"}
        for _, r in df.iterrows():
            cd = _norm(r[cc])
            if cd in probe:
                txt = " | ".join(f"{x}={r[x]}" for x in sec
                                 if pd.notna(r.get(x, None)))[:120]
                print(f"      [진단] {probe[cd]}({cd}) {txt}", flush=True)
    except Exception as ex:
        print(f"      업종 조회 실패({ex}) - 이름 기준만 적용")
    return out


def _norm(x):
    """종목코드 표준화 (6자리 문자열)"""
    return (str(x).strip().upper()
            .replace(".KS", "").replace(".KQ", "").replace("A", "").zfill(6))


def is_bio(name, sector=""):
    t = f"{name} {sector}"
    return any(w in t for w in BIO_WORDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--universe", type=int, default=800)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-bio", action="store_true", default=True)
    a = ap.parse_args()
    mk = a.market
    unit = "억" if mk == "kr" else "M$"

    print(f"[1/4] 유니버스 ({mk.upper()} {a.universe})...", flush=True)
    uni = SC.universe_kr(a.universe) if mk == "kr" else SC.universe_us(a.universe)
    if mk == "kr" and a.no_bio:
        before = len(uni)
        bio = bio_codes_kr()
        uni_codes = {_norm(u[0]) for u in uni}
        hit = uni_codes & bio
        print(f"      업종기준 {len(bio)}종목 식별 · 유니버스와 교집합 {len(hit)}",
              flush=True)
        if bio and not hit:
            print(f"      [경고] 코드 형식 불일치 예시 "
                  f"bio={list(bio)[:3]} uni={list(uni_codes)[:3]}", flush=True)
        uni = [u for u in uni if _norm(u[0]) not in bio and not is_bio(u[1])]
        print(f"      제약·바이오 {before - len(uni)}종목 제외", flush=True)
    print(f"      {len(uni)}종목", flush=True)

    print(f"[2/4] 일봉 수집 · 최근 {a.days}일 판정...", flush=True)
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
                    rows.extend(VF.analyze(f.result(), c, n, mk, a.days, m))
                except Exception:
                    pass
    else:
        meta = {c: n for c, n, _ in uni}
        syms = list(meta.keys())
        for k in range(0, len(syms), 150):
            for t, dd in SC.fetch_us_batch(syms[k:k + 150], period="5y").items():
                try:
                    rows.extend(VF.analyze(dd, t, meta.get(t, t), mk, a.days))
                except Exception:
                    pass
            print(f"      {min(k + 150, len(syms))}/{len(syms)}", flush=True)

    os.makedirs("results", exist_ok=True)
    if not rows:
        msg = f"[{'국장' if mk == 'kr' else '미장'}] 오늘 후보 0건"
        print(msg)
        with open(f"results/watchlist_{mk}.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    df = pd.DataFrame(rows)
    df.to_csv(f"results/watchlist_{mk}.csv", index=False, encoding="utf-8-sig")
    wait = df[df["status"] == "대기"]
    hold = df[df["status"] == "보류"]
    if len(wait) == 0:
        msg = f"[{'국장' if mk == 'kr' else '미장'}] 대기 중 후보 0건 (전체 {len(df)}건)"
        print(msg)
        with open(f"results/watchlist_{mk}.txt", "w", encoding="utf-8") as f:
            f.write(msg)
        return

    all_ranked = VF.merge_and_score(wait, mk)
    bio_set = bio_codes_kr() if mk == "kr" else set()
    for r in all_ranked:
        r["is_bio"] = (_norm(r["code"]) in bio_set) or is_bio(r["name"])

    # 비바이오 기준으로 top개를 확보하되, 그 사이에 낀 바이오는 표시만 하고 유지
    ranked, non_bio = [], 0
    for r in all_ranked:
        ranked.append(r)
        if not r["is_bio"]:
            non_bio += 1
        if non_bio >= a.top:
            break
    nb = sum(1 for r in ranked if r["is_bio"])
    if nb:
        print(f"      바이오 {nb}종목 포함 - 뒤 순위에서 보충", flush=True)

    print("[3/4] 시장 국면...", flush=True)
    regime = SC.market_regime(mk)

    print(f"[4/4] 뉴스 ({len(ranked)}종목)...", flush=True)
    for r in ranked:
        r["news"] = SC.get_news(r["name"], mk, 2)
        time.sleep(0.3)

    base = df["date"].max()
    L = [f"[{base} 관심종목 · {'국장' if mk == 'kr' else '미장'}]",
         f"대기 {len(wait)}건 중 {len(ranked)}종목 "
         f"(비바이오 {sum(1 for r in ranked if not r.get('is_bio'))}) · "
         f"보류 {len(hold)}건",
         f"시장국면: {regime}",
         f"필터: 유동성 {VF.STATS['liquidity']}/{VF.STATS['total']} · "
         f"도지 {VF.STATS['doji']} · 저항선 {VF.STATS['peaks']} · "
         f"손절폭 {VF.STATS['stopwidth']}",
         "",
         "진입=저항 돌파시 / 익절=1R 절반정리 후 손절을 진입가로", ""]

    for k, r in enumerate(ranked, 1):
        tag = "  [바이오]" if r.get("is_bio") else ""
        L.append(f"\n{k}위 {r['name'][:18]} ({r['code']})  {r['score']}점{tag}")
        L.append(f"  [{r['pattern']}] · 신호 {r['date']}{VF.age_tag(r)}")
        for p in r["points"]:
            L.append(f"    · {p}")
        L.append(f"  진입 {r['entry']:,} · 익절 {r['tgt']:,} · "
                 f"손절 {r['stop1']:,} (-{r['stop_pct']}%)")
        L.append(f"  현재 {r['last']:,} (진입까지 {r['togo']:+.2f}%) · "
                 f"거래대금 {r['value']}{unit}")
        for nw in r.get("news", []):
            L.append(f"  · {nw}")

    if len(hold):
        L.append("")
        L.append("━━ 보류 (손절선 아래 · 복귀시 부활) ━━")
        for r in VF.merge_and_score(hold, mk)[:8]:
            L.append(f"{r['name'][:14]} [{r['pattern']}] 진입 {r['entry']:,} "
                     f"· 현재 {r['last']:,} ({r['togo']:+.1f}%)")

    txt = "\n".join(L)
    print("\n" + txt)
    with open(f"results/watchlist_{mk}.txt", "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"\nresults/watchlist_{mk}.txt 저장")


if __name__ == "__main__":
    sys.exit(main())
