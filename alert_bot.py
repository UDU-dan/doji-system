# -*- coding: utf-8 -*-
"""
지정가 감시 봇 (국장 + 미장)

사용자가 직접 지정한 가격을 감시하고 상태 변화를 알린다.
텔레그램에서 자연어로 등록·수정·해제할 수 있다.

  "005930 71900 추가"     -> 확인 요청 -> "네" -> 등록
  "삼성전자 71900"         -> 종목명으로도 가능
  "NVDA 182.5"            -> 미장
  "005930 해제"            -> 감시 해제
  "목록"                   -> 현재 감시 중인 것

알림
  도달   현재가가 지정가와 완전히 동일
  돌파   지정가 위로. 한 번 + 0.5% 초과시 한 번 더
  보류   지정가 아래로 이탈 (몇 % 빠졌는지)
  복귀   다시 지정가 도달/돌파

환경변수: ALERT_TOKEN, ALERT_CHAT_ID, KIS_APP_KEY, KIS_APP_SECRET
실행: python alert_bot.py --market kr
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
REST = "https://openapi.koreainvestment.com:9443"
WS_KR = "ws://ops.koreainvestment.com:21000"
TR_TRADE = "H0STCNT0"
STORE = "manual_watch.json"
BREAK_EXTRA = 0.5        # 돌파 후 이 % 더 오르면 한 번 더 알림
POLL_US = 60             # 미장 폴링 주기(초)

TOKEN = os.environ.get("ALERT_TOKEN", "")
CHAT = os.environ.get("ALERT_CHAT_ID", "")


# ══════════ 텔레그램 ══════════
def tg(msg):
    if not TOKEN or not CHAT:
        print("[TG 미설정]", msg[:200], flush=True)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": msg}, timeout=15)
    except Exception as ex:
        print("텔레그램 실패:", ex, flush=True)


def tg_updates(offset):
    if not TOKEN:
        return [], offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15)
        js = r.json()
        if not js.get("ok"):
            return [], offset
        msgs = []
        for u in js.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) == str(CHAT) and m.get("text"):
                msgs.append(m["text"].strip())
        return msgs, offset
    except Exception:
        return [], offset


# ══════════ 저장 ══════════
def load_store():
    if os.path.exists(STORE):
        try:
            with open(STORE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_store(d):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def merge_save(store):
    """현재 세션 목록을 전체 파일에 반영 (다른 시장 항목 보존)"""
    allst = load_store()
    for k, v in store.items():
        allst[k] = v
    save_store(allst)


# ══════════ 종목 조회 ══════════
_KR_CACHE = None
_US_CACHE = None


def kr_list():
    global _KR_CACHE
    if _KR_CACHE is None:
        try:
            import FinanceDataReader as fdr
            df = fdr.StockListing("KRX")
            df.columns = [str(c) for c in df.columns]
            cc = next((c for c in df.columns if c.lower() in ("code", "symbol")), None)
            nc = next((c for c in df.columns if c.lower() == "name"), None)
            _KR_CACHE = [(str(r[cc]).zfill(6), str(r[nc])) for _, r in df.iterrows()
                         if cc and nc]
            print(f"[종목목록] 국장 {len(_KR_CACHE)}종목 로드", flush=True)
        except Exception as ex:
            print(f"[종목목록] 국장 로드 실패: {ex}", flush=True)
            _KR_CACHE = []
    return _KR_CACHE


def us_list():
    global _US_CACHE
    if _US_CACHE is None:
        _US_CACHE = []
        try:
            import FinanceDataReader as fdr
            for mkt in ("NASDAQ", "NYSE", "AMEX"):
                try:
                    df = fdr.StockListing(mkt)
                    df.columns = [str(c) for c in df.columns]
                    sc = next((c for c in df.columns
                               if c.lower() in ("symbol", "code")), None)
                    nc = next((c for c in df.columns if c.lower() == "name"), None)
                    if sc:
                        _US_CACHE += [(str(r[sc]), str(r[nc]) if nc else str(r[sc]))
                                      for _, r in df.iterrows()]
                except Exception:
                    pass
            print(f"[종목목록] 미장 {len(_US_CACHE)}종목 로드", flush=True)
        except Exception as ex:
            print(f"[종목목록] 미장 로드 실패: {ex}", flush=True)
    return _US_CACHE


def find_stock(q, market=None):
    """국장·미장 양쪽에서 검색 -> [(코드, 이름, 시장), ...]"""
    q = q.strip()
    out = []

    # 국장: 6자리 코드
    if re.fullmatch(r"\d{6}", q):
        for c, n in kr_list():
            if c == q:
                return [(c, n, "kr")]
        return [(q, q, "kr")]

    qu = q.upper()
    # 미장: 티커 정확 일치
    for sym, nm in us_list():
        if sym.upper() == qu:
            out.append((sym, nm, "us"))
            break

    # 국장: 이름 정확 일치 -> 부분 일치
    exact = [(c, n, "kr") for c, n in kr_list() if n == q]
    if exact:
        out += exact
    else:
        out += [(c, n, "kr") for c, n in kr_list() if q in n][:4]

    # 미장: 회사명 부분 일치
    if len(out) < 5:
        for sym, nm in us_list():
            if any(x[0] == sym for x in out):
                continue
            if qu in nm.upper():
                out.append((sym, nm, "us"))
            if len(out) >= 5:
                break

    # 아무것도 없고 영문이면 미장 티커로 간주
    if not out and re.fullmatch(r"[A-Za-z.\-]{1,6}", q):
        return [(qu, qu, "us")]
    return out[:5]


# ══════════ 명령 해석 ══════════
DEL_WORDS = ("해제", "삭제", "빼", "취소", "제거", "그만", "중단")
LIST_WORDS = ("목록", "리스트", "확인", "보여", "뭐 ", "뭐보", "감시중")
# 종목명에서 걷어낼 말들 (긴 것부터)
# 종목명 뒤에 붙을 수 있는 조사·어미 (첫 토큰 끝에서만 제거)
TAIL = ("으로", "원에", "에서", "까지", "부터", "이랑", "하고", "원")


def clean_symbol(t):
    """
    문장에서 종목명/티커만 뽑는다.
    종목명은 항상 문장 앞쪽에 온다는 전제로 첫 유효 토큰만 사용한다.
    """
    t = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", t)
    for w in t.split():
        if re.fullmatch(r"[\d,.]+", w):          # 순수 숫자는 건너뜀
            continue
        if w in DEL_WORDS or w in LIST_WORDS:
            continue
        if w in ("지금", "이거", "저거", "좀", "추가", "등록", "감시",
                 "잡아", "바꿔", "수정", "변경", "해줘", "하자"):
            continue
        # 숫자가 섞인 토큰에서 숫자 제거 (예: "삼성전자71900")
        w = re.sub(r"\d[\d,]*\.?\d*", "", w)
        if not w:
            continue
        # 끝에 붙은 조사·어미 하나만 제거
        for suf in TAIL:
            if w.endswith(suf) and len(w) > len(suf) + 1:
                w = w[:-len(suf)]
                break
        # 종목명 뒤 1글자 조사는 검색이 부분일치이므로 그대로 둔다
        if w:
            return w
    return ""


def parse_cmd(text):
    """(동작, 종목질의, 가격) 반환"""
    t = text.strip()
    low = t.lower()
    if low in ("네", "예", "ㅇ", "y", "yes", "응", "맞아", "ok", "오케이"):
        return ("yes", None, None)
    if low in ("아니", "아니오", "n", "no", "아뇨"):
        return ("no", None, None)
    if re.fullmatch(r"[1-5]", t):
        return ("pick", t, None)
    if any(w in t for w in LIST_WORDS) and not re.search(r"\d{3}", t):
        return ("list", None, None)

    nums = re.findall(r"\d[\d,]*\.?\d*", t)
    is_del = any(w in t for w in DEL_WORDS)
    sym = clean_symbol(t)

    code = None
    price_cands = list(nums)
    if not sym:
        # 종목명이 없으면 6자리 숫자를 종목코드로 본다
        code = next((n for n in nums if re.fullmatch(r"\d{6}", n)), None)
        if code:
            price_cands = [n for n in nums if n != code]

    if is_del:
        return ("del", sym or code, None)
    if price_cands:
        return ("add", sym or code, float(price_cands[-1].replace(",", "")))
    if sym or code:
        return ("query", sym or code, None)
    return (None, None, None)


def handle(text, store, market, pending):
    """명령 처리 -> 응답 문자열"""
    act, sym, price = parse_cmd(text)

    if act == "yes" and pending.get("cand"):
        c = pending["cand"]
        mkt = c.get("market", market)
        allst = load_store()
        allst[c["code"]] = dict(name=c["name"], price=c["price"],
                                state="대기", notified=[], market=mkt)
        save_store(allst)
        if mkt == market:
            store[c["code"]] = allst[c["code"]]
            note = "감시 시작"
        else:
            note = ("국장 종목 - 다음 국장 세션(09:00)부터 감시"
                    if mkt == "kr" else "미장 종목 - 다음 미장 세션부터 감시")
        pending.clear()
        px = f"{c['price']:,.0f}" if mkt == "kr" else f"{c['price']:,.2f}"
        return f"등록 완료\n{c['name']} ({c['code']}) {px}\n{note}"

    if act == "no":
        pending.clear()
        return "취소했습니다."

    if act == "pick" and pending.get("options"):
        i = int(sym) - 1
        opts = pending["options"]
        if 0 <= i < len(opts):
            code, name, mkt = opts[i]
            pending["cand"] = dict(code=code, name=name,
                                   price=pending["price"], market=mkt)
            pending.pop("options", None)
            px = (f"{pending['price']:,.0f}" if mkt == "kr"
                  else f"{pending['price']:,.2f}")
            lbl = "국장" if mkt == "kr" else "미장"
            return f"{name} ({code}) · {lbl}\n{px} 등록할까요?\n네 / 아니오"
        return "번호를 다시 선택해주세요."

    if act == "list":
        allst = load_store()
        if not allst:
            return "등록된 종목이 없습니다."
        L = [f"등록 {len(allst)}종목 (현재 {'국장' if market == 'kr' else '미장'} 세션)"]
        for code, v in allst.items():
            m = v.get("market", "kr")
            px = f"{v['price']:,.0f}" if m == "kr" else f"{v['price']:,.2f}"
            live = "●" if m == market else "○"
            L.append(f"  {live} {v['name']} ({code}) {px} · {v['state']}")
        L.append("\n● 지금 감시 중 · ○ 해당 시장 개장시 감시")
        return "\n".join(L)

    if act == "del":
        allst = load_store()
        for code, v in list(allst.items()):
            if code == sym or (sym and sym in v["name"]) or \
                    (sym and sym.upper() == code.upper()):
                allst.pop(code)
                store.pop(code, None)
                save_store(allst)
                return f"해제 완료\n{v['name']} ({code}) 감시 중단"
        return f"'{sym}' 을(를) 목록에서 찾지 못했습니다."

    if act == "add":
        found = find_stock(sym)
        if not found:
            return f"'{sym}' 종목을 찾지 못했습니다."
        if len(found) > 1:
            pending["options"] = found
            pending["price"] = price
            L = ["어느 종목인가요? 번호로 답해주세요."]
            for i, (c, n, m) in enumerate(found, 1):
                L.append(f"  {i}. {n} ({c}) · {'국장' if m == 'kr' else '미장'}")
            return "\n".join(L)
        code, name, mkt = found[0]
        pending["cand"] = dict(code=code, name=name, price=price, market=mkt)
        allst = load_store()
        old = allst.get(code)
        px = f"{price:,.0f}" if mkt == "kr" else f"{price:,.2f}"
        pre = (f"현재 {old['price']:,.2f} → " if old else "")
        lbl = "국장" if mkt == "kr" else "미장"
        return f"{name} ({code}) · {lbl}\n{pre}{px} 등록할까요?\n네 / 아니오"

    if act == "query":
        found = find_stock(sym)
        if not found:
            return f"'{sym}' 종목을 찾지 못했습니다."
        n0, m0 = found[0][1], found[0][2]
        ex = "71900" if m0 == "kr" else "182.5"
        return f"가격도 같이 알려주세요.\n예) {n0} {ex}"

    return None


# ══════════ 가격 판정 ══════════
def check(code, v, px, store):
    """가격 변화에 따른 알림 문자열. 없으면 None"""
    target = v["price"]
    name, notified = v["name"], v.get("notified", [])
    diff = (px - target) / target * 100
    ts = datetime.now(KST).strftime("%H:%M:%S")
    fmt = (lambda x: f"{x:,.0f}") if v.get("market", "kr") == "kr" else (lambda x: f"{x:,.2f}")
    msg = None

    if px == target and "도달" not in notified:
        notified.append("도달")
        msg = (f"[도달] {name} ({code})\n"
               f"지정가 {fmt(target)} · 현재 {fmt(px)}\n동일가격\n{ts} KST")

    elif px > target and "돌파" not in notified:
        notified.append("돌파")
        v["state"] = "돌파"
        msg = (f"[돌파] {name} ({code})\n"
               f"지정가 {fmt(target)} → 현재 {fmt(px)} ({diff:+.2f}%)\n{ts} KST")

    elif (px > target * (1 + BREAK_EXTRA / 100)
          and "돌파+" not in notified and "돌파" in notified):
        notified.append("돌파+")
        msg = (f"[추가상승] {name} ({code})\n"
               f"지정가 {fmt(target)} → 현재 {fmt(px)} ({diff:+.2f}%)\n"
               f"진입 구간 벗어남 - 눌림 대기 또는 가격 재설정\n{ts} KST")

    elif px < target and v.get("state") != "보류":
        v["state"] = "보류"
        for k in ("도달", "돌파", "돌파+"):
            if k in notified:
                notified.remove(k)
        msg = (f"[보류] {name} ({code})\n"
               f"지정가 {fmt(target)} · 현재 {fmt(px)} ({diff:+.2f}%)\n"
               f"지정가 아래 - 복귀시 다시 알림\n{ts} KST")

    elif px >= target and v.get("state") == "보류":
        v["state"] = "대기"

    v["notified"] = notified
    if msg:
        merge_save(store)
    return msg


# ══════════ 시세 ══════════
def get_approval(key, secret):
    r = requests.post(f"{REST}/oauth2/Approval",
                      headers={"content-type": "application/json"},
                      data=json.dumps({"grant_type": "client_credentials",
                                       "appkey": key, "secretkey": secret}),
                      timeout=15)
    r.raise_for_status()
    return r.json()["approval_key"]


def sub_msg(approval, code, on=True):
    return json.dumps({
        "header": {"approval_key": approval, "custtype": "P",
                   "tr_type": "1" if on else "2", "content-type": "utf-8"},
        "body": {"input": {"tr_id": TR_TRADE, "tr_key": code}}})


def parse_trade(raw):
    p = raw.split("|")
    if len(p) < 4 or p[1] != TR_TRADE:
        return None
    f = p[3].split("^")
    try:
        return f[0], float(f[2])
    except (ValueError, IndexError):
        return None


def us_prices(codes):
    import yfinance as yf
    out = {}
    if not codes:
        return out
    try:
        d = yf.download(list(codes), period="1d", interval="1m",
                        group_by="ticker", threads=True, progress=False,
                        auto_adjust=False)
        import pandas as pd
        for t in codes:
            try:
                x = d[t] if isinstance(d.columns, pd.MultiIndex) else d
                x = x.dropna()
                if len(x):
                    out[t] = float(x["Close"].iloc[-1])
            except Exception:
                pass
    except Exception as ex:
        print("미장 시세 실패:", ex, flush=True)
    return out


def session_bounds(market):
    """다음 거래일의 개장·마감 시각. 마감이 지났거나 주말이면 다음 평일로."""
    from datetime import timedelta
    if market == "kr":
        tz, o, c = KST, dtime(9, 0), dtime(15, 20)
    else:
        tz, o, c = NY, dtime(9, 30), dtime(16, 0)
    d = datetime.now(tz)
    day = d.date()
    if d.time() >= c:                    # 이미 마감했으면 다음 날
        day += timedelta(days=1)
    while day.weekday() >= 5:            # 토·일 건너뛰기
        day += timedelta(days=1)
    return (datetime.combine(day, o, tz), datetime.combine(day, c, tz))


# ══════════ 메인 ══════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--max-minutes", type=int, default=400)
    a = ap.parse_args()
    mk = a.market

    store = load_store()
    store = {k: v for k, v in store.items() if v.get("market", "kr") == mk}
    pending = {}
    offset = 0
    _, offset = tg_updates(0)          # 이전 메시지 건너뛰기

    open_at, close_at = session_bounds(mk)
    label = "국장" if mk == "kr" else "미장"
    hard = time.time() + a.max_minutes * 60

    L = [f"[{label} 지정가 감시] {datetime.now(KST):%m/%d %H:%M} KST",
         f"다음 개장 {open_at.astimezone(KST):%m/%d %H:%M} · "
         f"마감 {close_at.astimezone(KST):%H:%M} KST"]
    allst = load_store()
    if allst:
        L.append(f"\n등록 {len(allst)}종목 (● 지금 감시 · ○ 대기)")
        for c, v in allst.items():
            m = v.get("market", "kr")
            px = f"{v['price']:,.0f}" if m == "kr" else f"{v['price']:,.2f}"
            L.append(f"  {'●' if m == mk else '○'} {v['name']} ({c}) {px}")
    else:
        L.append("\n등록된 종목 없음")
    L.append("\n등록: 종목명 또는 티커 + 가격")
    L.append("예) 삼성전자 71900 / NVDA 182.5 / 목록 / 해제")
    tg("\n".join(L))
    print("\n".join(L), flush=True)

    # 개장 대기 (그동안에도 명령은 받는다)
    last_log = 0
    while datetime.now(KST) < open_at and time.time() < hard:
        msgs, offset = tg_updates(offset)
        for m in msgs:
            resp = handle(m, store, mk, pending)
            if resp:
                tg(resp)
        if time.time() - last_log > 600:
            last_log = time.time()
            left = (open_at - datetime.now(KST)).total_seconds() / 60
            print(f"[{datetime.now(KST):%H:%M}] 개장까지 {int(left)}분 · "
                  f"감시 {len(store)}종목 · 명령 대기 중", flush=True)
        time.sleep(3)

    if mk == "kr":
        key = os.environ.get("KIS_APP_KEY", "")
        secret = os.environ.get("KIS_APP_SECRET", "")
        if not key or not secret:
            tg(f"[{label}] KIS 키 없음 - 종료")
            return 1
        import websocket
        approval = get_approval(key, secret)
        subscribed = set()
        ws = None

        while datetime.now(KST) < close_at and time.time() < hard:
            try:
                if ws is None:
                    ws = websocket.create_connection(WS_KR, timeout=30)
                    ws.settimeout(2)
                    subscribed.clear()
                for c in list(store):
                    if c not in subscribed:
                        ws.send(sub_msg(approval, c))
                        subscribed.add(c)
                        time.sleep(0.06)
                for c in list(subscribed):
                    if c not in store:
                        ws.send(sub_msg(approval, c, on=False))
                        subscribed.discard(c)

                try:
                    raw = ws.recv()
                except Exception:
                    raw = None
                if raw:
                    if raw[0] in ("0", "1"):
                        p = parse_trade(raw)
                        if p and p[0] in store:
                            m = check(p[0], store[p[0]], p[1], store)
                            if m:
                                tg(m)
                    else:
                        try:
                            j = json.loads(raw)
                            if j.get("header", {}).get("tr_id") == "PINGPONG":
                                ws.send(raw)
                        except Exception:
                            pass

                msgs, offset = tg_updates(offset)
                for m in msgs:
                    resp = handle(m, store, mk, pending)
                    if resp:
                        tg(resp)
            except Exception as ex:
                print("연결 오류:", ex, flush=True)
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None
                time.sleep(5)
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    else:
        last = 0
        while datetime.now(NY) < close_at and time.time() < hard:
            if time.time() - last >= POLL_US:
                last = time.time()
                for code, px in us_prices(list(store)).items():
                    if code in store:
                        m = check(code, store[code], px, store)
                        if m:
                            tg(m)
            msgs, offset = tg_updates(offset)
            for m in msgs:
                resp = handle(m, store, mk, pending)
                if resp:
                    tg(resp)
            time.sleep(3)

    merge_save(store)
    tg(f"[{label} 지정가 감시 종료] {datetime.now(KST):%H:%M} KST\n"
       f"감시 {len(store)}종목 유지")
    return 0


if __name__ == "__main__":
    sys.exit(main())
