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
import threading
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
LIVE = {}          # 현재 세션에서 감시 중인 종목 (메모리 최신값)


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
def fmt_price(x, market):
    """국장은 원 단위 정수, 미장은 달러 소수 2자리"""
    return f"{x:,.0f}원" if market == "kr" else f"${x:,.2f}"


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


_last_save = [0.0]


def periodic_save(store, interval=30):
    """알림이 없어도 주기적으로 현재값을 파일에 반영"""
    if time.time() - _last_save[0] >= interval:
        _last_save[0] = time.time()
        merge_save(store)


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
        return []                       # 목록에 없는 코드는 등록 거부

    qu = q.upper()
    # 미장: 티커 정확 일치
    for sym, nm in us_list():
        if sym.upper() == qu:
            out.append((sym, nm, "us"))
            break

    # 국장: 이름 정확 일치(대소문자 무시) -> 부분 일치
    exact = [(c, n, "kr") for c, n in kr_list() if n.upper() == qu]
    if exact:
        out += exact
    else:
        out += [(c, n, "kr") for c, n in kr_list() if qu in n.upper()][:4]

    # 미장: 회사명 부분 일치
    if len(out) < 5:
        for sym, nm in us_list():
            if any(x[0] == sym for x in out):
                continue
            if qu in nm.upper():
                out.append((sym, nm, "us"))
            if len(out) >= 5:
                break

    # 아무것도 없고 순수 영문 1~5자면 미장 티커로 간주
    if not out and re.fullmatch(r"[A-Za-z.\-]{1,5}", q):
        return [(qu, qu, "us")]
    return out[:5]


# ══════════ 명령 해석 ══════════
DEL_WORDS = ("해제", "삭제", "빼", "취소", "제거", "그만", "중단")
LIST_WORDS = ("목록", "리스트", "감시중")
STATUS_WORDS = ("상황", "상태", "현황", "어때", "어떻게", "어떻노", "됐어",
                "됐나", "진행", "체크")
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
    if any(w in t for w in STATUS_WORDS) and not re.search(r"\d{3}", t):
        return ("status", None, None)
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
        old = allst.get(c["code"], {})
        allst[c["code"]] = dict(
            name=c["name"], price=c["price"], state="대기", notified=[],
            market=mkt,
            added=old.get("added") or datetime.now(KST).strftime("%m/%d %H:%M"),
            last_px=None, last_at=None, events=[])
        save_store(allst)
        if mkt == market:
            store[c["code"]] = allst[c["code"]]
            LIVE[c["code"]] = allst[c["code"]]
            note = "감시 시작"
        else:
            note = ("국장 종목 - 다음 국장 세션(09:00)부터 감시"
                    if mkt == "kr" else "미장 종목 - 다음 미장 세션부터 감시")
        pending.clear()
        return (f"등록 완료\n{c['name']} ({c['code']}) "
                f"{fmt_price(c['price'], mkt)}\n{note}")

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
            lbl = "국장" if mkt == "kr" else "미장"
            return (f"{name} ({code}) · {lbl}\n"
                    f"{fmt_price(pending['price'], mkt)} 등록할까요?\n네 / 아니오")
        return "번호를 다시 선택해주세요."

    if act == "status":
        allst = load_store()
        if not allst:
            return "등록된 종목이 없습니다."
        L = [f"[현재 상황] {datetime.now(KST):%m/%d %H:%M:%S} KST"]
        for lbl, mm in (("국장", "kr"), ("미장", "us")):
            grp = {k: v for k, v in allst.items() if v.get("market", "kr") == mm}
            if not grp:
                continue
            L.append(f"\n[{lbl} {len(grp)}종목]")
            for c, v in grp.items():
                v = LIVE.get(c, v)              # 메모리 최신값 우선
                live = "●" if mm == market else "○"
                L.append(f"\n{live} {v['name']} ({c})  {v.get('state', '대기')}")
                L.append(f"   지정가 {fmt_price(v['price'], mm)}"
                         f" · 등록 {v.get('added', '-')}")
                lp = v.get("last_px")
                if lp:
                    d = (lp - v["price"]) / v["price"] * 100
                    L.append(f"   현재 {fmt_price(lp, mm)} ({d:+.2f}%)"
                             f" · {v.get('last_at', '')}")
                else:
                    L.append("   현재 시세 미수신")
                for e in v.get("events", [])[-3:]:
                    L.append(f"   · {e}")
        return "\n".join(L)

    if act == "list":
        allst = load_store()
        if not allst:
            return "등록된 종목이 없습니다."
        L = [f"등록 {len(allst)}종목 (현재 {'국장' if market == 'kr' else '미장'} 세션)"]
        for code, v in allst.items():
            v = LIVE.get(code, v)
            m = v.get("market", "kr")
            live = "●" if m == market else "○"
            L.append(f"  {live} {v['name']} ({code}) "
                     f"{fmt_price(v['price'], m)} · {v['state']}")
        L.append("\n● 지금 감시 중 · ○ 해당 시장 개장시 감시")
        return "\n".join(L)

    if act == "del":
        allst = load_store()
        for code, v in list(allst.items()):
            if code == sym or (sym and sym in v["name"]) or \
                    (sym and sym.upper() == code.upper()):
                allst.pop(code)
                store.pop(code, None)
                LIVE.pop(code, None)
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
        pre = (f"현재 {fmt_price(old['price'], mkt)} → " if old else "")
        lbl = "국장" if mkt == "kr" else "미장"
        return (f"{name} ({code}) · {lbl}\n"
                f"{pre}{fmt_price(price, mkt)} 등록할까요?\n네 / 아니오")

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
    target = float(v["price"])
    px = float(px)
    name = v["name"]
    notified = v.get("notified", [])
    mkt = v.get("market", "kr")
    fmt = lambda x: fmt_price(x, mkt)
    diff = (px - target) / target * 100
    ts = datetime.now(KST).strftime("%H:%M:%S")

    v["last_px"] = px
    v["last_at"] = datetime.now(KST).strftime("%m/%d %H:%M:%S")
    msg = None

    if px < target:
        # 지정가 아래 -> 보류 (상태가 바뀔 때만 알림)
        if v.get("state") != "보류":
            v["state"] = "보류"
            v["notified"] = []
            msg = (f"[보류] {name} ({code})\n"
                   f"지정가 {fmt(target)} · 현재 {fmt(px)} ({diff:+.2f}%)\n"
                   f"지정가 아래 - 복귀시 다시 알림\n{ts} KST")
    elif px == target:
        if v.get("state") == "보류":
            notified = []                  # 복귀 -> 다시 알림
        v["state"] = "도달"
        if "도달" not in notified:
            notified.append("도달")
            msg = (f"[도달] {name} ({code})\n"
                   f"지정가 {fmt(target)} · 현재 {fmt(px)}\n동일가격\n{ts} KST")
    else:
        if v.get("state") == "보류":
            notified = []                  # 복귀 -> 다시 알림
        v["state"] = "돌파"
        if "돌파" not in notified:
            notified.append("돌파")
            msg = (f"[돌파] {name} ({code})\n"
                   f"지정가 {fmt(target)} → 현재 {fmt(px)} ({diff:+.2f}%)\n"
                   f"{ts} KST")
        elif diff >= BREAK_EXTRA and "돌파+" not in notified:
            notified.append("돌파+")
            msg = (f"[추가상승] {name} ({code})\n"
                   f"지정가 {fmt(target)} → 현재 {fmt(px)} ({diff:+.2f}%)\n"
                   f"진입 구간 벗어남 - 눌림 대기 또는 가격 재설정\n{ts} KST")

    v["notified"] = notified
    if msg:
        kind = msg.split("]")[0].lstrip("[")
        ev = v.setdefault("events", [])
        ev.append(f"{datetime.now(KST):%m/%d %H:%M:%S} {kind} {fmt(px)}")
        v["events"] = ev[-8:]
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


def summary_text(store_all, market, title):
    """등록 종목 요약 (국장/미장 구분)"""
    L = [title]
    kr = {k: v for k, v in store_all.items() if v.get("market", "kr") == "kr"}
    us = {k: v for k, v in store_all.items() if v.get("market") == "us"}
    for lbl, grp, mm in (("국장", kr, "kr"), ("미장", us, "us")):
        if not grp:
            continue
        L.append(f"\n[{lbl} {len(grp)}종목]")
        for c, v in grp.items():
            line = (f"  {v['name']} ({c}) {fmt_price(v['price'], mm)} "
                    f"· {v.get('state', '대기')}")
            if v.get("added"):
                line += f" · 등록 {v['added']}"
            L.append(line)
            lp = v.get("last_px")
            if lp:
                d = (lp - v["price"]) / v["price"] * 100
                L.append(f"     현재 {fmt_price(lp, mm)} ({d:+.2f}%)"
                         f" {v.get('last_at', '')}")
    if not kr and not us:
        L.append("\n등록된 종목 없음")
    return "\n".join(L)


def bot_loop(store, market, stop_flag):
    """텔레그램 명령 처리 (별도 스레드). 웹소켓과 무관하게 즉시 응답."""
    pending = {}
    offset = 0
    _, offset = tg_updates(0)          # 이전 메시지 건너뛰기
    while not stop_flag["stop"]:
        try:
            msgs, offset = tg_updates(offset)
            for m in msgs:
                resp = handle(m, store, market, pending)
                if resp:
                    tg(resp)
        except Exception as ex:
            print("봇 루프 오류:", ex, flush=True)
        time.sleep(1)


# ══════════ 메인 ══════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["kr", "us"], default="kr")
    ap.add_argument("--max-minutes", type=int, default=400)
    a = ap.parse_args()
    mk = a.market

    allst = load_store()
    store = {k: v for k, v in allst.items() if v.get("market", "kr") == mk}
    LIVE.clear()
    LIVE.update(store)

    open_at, close_at = session_bounds(mk)
    label = "국장" if mk == "kr" else "미장"
    hard = time.time() + a.max_minutes * 60

    # 종목 목록 미리 로드 (첫 명령 응답을 빠르게)
    kr_list()
    us_list()

    head = (f"[{label} 지정가 감시] {datetime.now(KST):%m/%d %H:%M} KST\n"
            f"다음 개장 {open_at.astimezone(KST):%m/%d %H:%M} · "
            f"마감 {close_at.astimezone(KST):%H:%M} KST")
    tg(summary_text(allst, mk, head) +
       "\n\n등록: 종목명 또는 티커 + 가격"
       "\n예) 삼성전자 71900 / NVDA 182.5 / 목록 / GS건설 해제")

    # 텔레그램 처리 스레드 시작 (개장 전에도 등록 가능)
    stop_flag = {"stop": False}
    th = threading.Thread(target=bot_loop, args=(store, mk, stop_flag), daemon=True)
    th.start()

    # 개장 대기
    last_log = 0
    while datetime.now(KST) < open_at and time.time() < hard:
        if time.time() - last_log > 600:
            last_log = time.time()
            left = (open_at - datetime.now(KST)).total_seconds() / 60
            print(f"[{datetime.now(KST):%H:%M}] 개장까지 {int(left)}분 · "
                  f"등록 {len(store)}종목", flush=True)
        time.sleep(2)

    if time.time() < hard:
        tg(summary_text(load_store(), mk,
                        f"[{label} 개장] {datetime.now(KST):%H:%M} KST · 감시 시작"))

    if mk == "kr":
        key = os.environ.get("KIS_APP_KEY", "")
        secret = os.environ.get("KIS_APP_SECRET", "")
        if not key or not secret:
            tg(f"[{label}] KIS 키 없음 - 종료")
            stop_flag["stop"] = True
            return 1
        import websocket
        approval = get_approval(key, secret)
        ws = None
        sub_sent = {}          # 종목 -> 요청 보낸 횟수
        sub_ok = set()         # 구독 확인된 종목
        recv = [0]
        last_stat = last_retry = 0.0
        warned = set()

        def subscribe(code):
            ws.send(sub_msg(approval, code))
            sub_sent[code] = sub_sent.get(code, 0) + 1
            time.sleep(0.08)

        while datetime.now(KST) < close_at and time.time() < hard:
            try:
                # ── 연결 ──
                if ws is None:
                    ws = websocket.create_connection(WS_KR, timeout=30)
                    ws.settimeout(1)
                    sub_sent.clear()
                    sub_ok.clear()
                    for c in list(store):
                        subscribe(c)
                    print(f"[{datetime.now(KST):%H:%M}] 연결 · "
                          f"구독요청 {len(sub_sent)}종목", flush=True)

                # ── 신규 등록분 구독 / 해제분 정리 ──
                for c in list(store):
                    if c not in sub_sent:
                        subscribe(c)
                for c in list(sub_sent):
                    if c not in store:
                        try:
                            ws.send(sub_msg(approval, c, on=False))
                        except Exception:
                            pass
                        sub_sent.pop(c, None)
                        sub_ok.discard(c)

                # ── 수신 (타임아웃은 정상. 연결 유지) ──
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                except Exception:
                    raise                      # 진짜 연결 오류만 재접속

                if raw:
                    if raw[0] in ("0", "1"):
                        p = parse_trade(raw)
                        if p and p[0] in store:
                            recv[0] += 1
                            sub_ok.add(p[0])   # 데이터 도착 = 구독 성공
                            m = check(p[0], store[p[0]], p[1], store)
                            if m:
                                tg(m)
                    else:
                        try:
                            j = json.loads(raw)
                            h = j.get("header", {})
                            if h.get("tr_id") == "PINGPONG":
                                ws.send(raw)
                            else:
                                body = j.get("body", {})
                                key_ = h.get("tr_key") or \
                                    body.get("output", {}).get("key")
                                if body.get("msg1", "").upper().startswith("SUBSCRIBE"):
                                    if key_:
                                        sub_ok.add(key_)
                        except Exception:
                            pass

                # ── 미확인 종목 재구독 (20초마다) ──
                if time.time() - last_retry > 20:
                    last_retry = time.time()
                    pend = [c for c in store if c not in sub_ok]
                    for c in pend:
                        if sub_sent.get(c, 0) < 5:
                            subscribe(c)
                        elif c not in warned:
                            warned.add(c)
                            tg(f"[구독 실패] {store[c]['name']} ({c})\n"
                               f"시세를 받지 못하고 있습니다. 종목코드를 확인하거나 "
                               f"해제 후 다시 등록해주세요.")

                periodic_save(store)
                if time.time() - last_stat > 120:
                    last_stat = time.time()
                    print(f"[{datetime.now(KST):%H:%M}] 체결수신 {recv[0]}건 · "
                          f"구독확인 {len(sub_ok)}/{len(store)}종목", flush=True)

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
                periodic_save(store)
            time.sleep(2)

    stop_flag["stop"] = True
    merge_save(store)
    tg(summary_text(load_store(), mk,
                    f"[{label} 감시 종료] {datetime.now(KST):%H:%M} KST"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
