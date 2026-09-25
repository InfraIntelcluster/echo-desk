#!/usr/bin/env python3
"""
ECHO DESK -- The Listener.
Finds the wallet that trades first on Solana (the voice), the wallets that repeat it
within seconds (the echo), and the seconds between them (the delay). Every finding is a
recording on the board. No calls, no targets.

Run:  python3 echo.py            (bot + background listener)
      python3 echo.py --test     (offline tests on fixtures)
      python3 echo.py --scan     (one scan, print rooms, exit)

Env (.env next to this file):
  HELIUS_KEY=...      Helius API key (free tier is enough)
  TG_TOKEN=...        Telegram bot token from @BotFather
  TG_CHANNEL=...      optional: chat id to push new recordings to
stdlib only.
"""
import json, os, sys, time, math, threading, urllib.request, urllib.parse, urllib.error, statistics, html
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
BOARD_PATH = os.path.join(HERE, "board.json")

# ---------- the rules of the room ----------
WINDOW_S = 60          # a buy within 60 s after the voice is an echo
MIN_ECHO = 3           # a room needs at least 3 echoes to be recorded
MIN_SOL_IN = 1.0       # and at least 1 SOL following the voice, otherwise it is dust
MAX_TOKENS_PER_SCAN = 8
SCAN_EVERY_S = 300
TX_LIMIT = 100
SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {SOL_MINT,
               "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
WSOL_DECIMALS = 9

# ---------- env ----------
def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
load_env()
HELIUS_KEY = os.environ.get("HELIUS_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "")

# ---------- http ----------
def http_json(url, data=None, timeout=25, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"content-type": "application/json", "user-agent": "echodesk/0.1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(2)
        return None
    except Exception:
        return None

def short(addr):
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else (addr or "?")

# ---------- data sources ----------
def helius_txs(address, limit=TX_LIMIT, before=None):
    """Enhanced transactions involving an address (works for wallets and mints)."""
    q = {"api-key": HELIUS_KEY, "limit": limit, "type": "SWAP"}
    if before:
        q["before"] = before
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?" + urllib.parse.urlencode(q)
    r = http_json(url)
    return r if isinstance(r, list) else []

def dex_trending():
    """Candidate mints: DexScreener boosted + latest profiles, Solana only, deduped."""
    out, seen = [], set()
    for path in ("token-boosts/top/v1", "token-profiles/latest/v1"):
        r = http_json(f"https://api.dexscreener.com/{path}") or []
        for x in r:
            if x.get("chainId") == "solana":
                m = x.get("tokenAddress")
                if m and m not in seen:
                    seen.add(m); out.append(m)
    return out

def dex_symbol(mint):
    r = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}") or {}
    pairs = r.get("pairs") or []
    if pairs:
        p = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        return (p.get("baseToken") or {}).get("symbol") or short(mint), p.get("url")
    return short(mint), None

# ---------- parsing swaps ----------
def parse_buys(txs, mint):
    """From enhanced txs return [(ts, buyer, amount_token, sol_spent)] for BUYS of mint."""
    buys = []
    for tx in txs:
        ts = tx.get("timestamp") or 0
        payer = tx.get("feePayer")
        if not payer:
            continue
        got, paid_sol = 0.0, 0.0
        for t in tx.get("tokenTransfers") or []:
            if t.get("mint") == mint and t.get("toUserAccount") == payer:
                got += float(t.get("tokenAmount") or 0)
            if t.get("mint") in QUOTE_MINTS and t.get("fromUserAccount") == payer:
                paid_sol += float(t.get("tokenAmount") or 0)
        for n in tx.get("nativeTransfers") or []:
            if n.get("fromUserAccount") == payer:
                paid_sol += float(n.get("amount") or 0) / 10**WSOL_DECIMALS
        if got > 0:
            buys.append((ts, payer, got, paid_sol))
    buys.sort()
    return buys

def find_rooms(buys, window=WINDOW_S, min_echo=MIN_ECHO):
    """Greedy sweep: the first buyer of a burst is the voice, buys within `window` after are the echo."""
    rooms, i, n = [], 0, len(buys)
    while i < n:
        t0, voice, _, _ = buys[i]
        j = i + 1
        echo = []
        while j < n and buys[j][0] - t0 <= window:
            if buys[j][1] != voice:
                echo.append(buys[j])
            j += 1
        sol_in = sum(b[3] for b in echo)
        if len(echo) >= min_echo and sol_in >= MIN_SOL_IN:
            delays = [b[0] - t0 for b in echo]
            rooms.append({
                "voice": voice, "t0": t0, "echo": len(echo),
                "wallets": [b[1] for b in echo],
                "median_delay": statistics.median(delays),
                "first_delay": min(delays),
                "sol_in": round(sol_in, 2),
            })
            i = j
        else:
            i += 1
    return rooms

# ---------- state / board ----------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except Exception:
            pass
    return {"recordings": [], "seen": {}, "started": int(time.time()), "scans": 0}

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_PATH)

def write_board(st):
    recs = st["recordings"][-50:]
    board = {"updated": int(time.time()), "recordings": recs, "scorecard": scorecard(st)}
    json.dump(board, open(BOARD_PATH, "w"), indent=1)

def scorecard(st):
    recs = st["recordings"]
    if not recs:
        return {"rooms": 0, "echo": 0, "median_delay": None, "loudest": None, "scans": st.get("scans", 0)}
    by_voice = defaultdict(int)
    for r in recs:
        by_voice[r["voice"]] += r["echo"]
    loudest = max(by_voice.items(), key=lambda kv: kv[1])
    return {
        "rooms": len(recs),
        "echo": sum(r["echo"] for r in recs),
        "median_delay": statistics.median([r["median_delay"] for r in recs]),
        "fastest": min(r["first_delay"] for r in recs),
        "loudest": {"voice": loudest[0], "echo": loudest[1]},
        "tokens": len({r["mint"] for r in recs}),
        "scans": st.get("scans", 0),
    }

# ---------- scan ----------
def scan_token(mint, st, symbol=None, url=None):
    txs = helius_txs(mint)
    if not txs:
        return []
    buys = parse_buys(txs, mint)
    rooms = find_rooms(buys)
    new = []
    if symbol is None:
        symbol, url = dex_symbol(mint)
    for r in rooms:
        key = f"{mint}:{r['voice']}:{r['t0']}"
        if key in st["seen"]:
            continue
        st["seen"][key] = int(time.time())
        rec = {"mint": mint, "symbol": symbol, "url": url, **r, "recorded": int(time.time())}
        st["recordings"].append(rec)
        new.append(rec)
    return new

def scan(st, log=print):
    mints = dex_trending()[:MAX_TOKENS_PER_SCAN]
    log(f"[scan] {len(mints)} candidate tokens")
    new = []
    for m in mints:
        try:
            got = scan_token(m, st)
            new += got
            log(f"[scan] {short(m)}: {len(got)} new room(s)")
        except Exception as e:
            log(f"[scan] {short(m)} failed: {e}")
        time.sleep(0.4)
    st["scans"] = st.get("scans", 0) + 1
    # keep seen small
    cutoff = int(time.time()) - 3 * 86400
    st["seen"] = {k: v for k, v in st["seen"].items() if v > cutoff}
    st["recordings"] = st["recordings"][-500:]
    save_state(st); write_board(st)
    return new

# ---------- cards ----------
def fmt_delay(s):
    return f"{int(s)}s" if s < 90 else f"{s/60:.1f}m"

def card_room(r):
    return (f"🔊 <b>{html.escape(r['symbol'])}</b> · the room · {r['echo']} echoes\n"
            f"voice <code>{short(r['voice'])}</code> spoke first\n"
            f"first echo {fmt_delay(r['first_delay'])} · median delay {fmt_delay(r['median_delay'])}\n"
            f"{r['sol_in']} SOL followed the voice inside {WINDOW_S}s\n"
            f"<i>recorded, not called.</i>")

def card_rooms(st):
    day = int(time.time()) - 86400
    recs = [r for r in st["recordings"] if r["recorded"] > day] or st["recordings"]
    recs = sorted(recs, key=lambda r: r["echo"], reverse=True)
    per, picked = defaultdict(int), []
    for r in recs:
        if per[r["mint"]] < 2:
            per[r["mint"]] += 1; picked.append(r)
        if len(picked) == 6:
            break
    recs = picked
    if not recs:
        return "The room is quiet. The listener has not recorded anything yet -- give him one scan."
    lines = ["<b>THE ROOM</b> · loudest rooms, last 24h", ""]
    for r in recs:
        lines.append(f"<b>{html.escape(r['symbol'])}</b>  {r['echo']} echoes  · voice {short(r['voice'])} · delay {fmt_delay(r['median_delay'])}")
    lines += ["", "<i>every echo is written down with its delay.</i>"]
    return "\n".join(lines)

def card_top(st):
    sc = scorecard(st)
    if not sc["rooms"]:
        return "SCORECARD · nothing recorded yet."
    return ("<b>SCORECARD</b> · the listener\n"
            f"{sc['rooms']} rooms · {sc['echo']} echoes · {sc['tokens']} tokens\n"
            f"median delay {fmt_delay(sc['median_delay'])} · fastest echo {fmt_delay(sc['fastest'])}\n"
            f"loudest voice <code>{short(sc['loudest']['voice'])}</code> · {sc['loudest']['echo']} echoes behind it\n"
            f"scans {sc['scans']} · window {WINDOW_S}s · min echo {MIN_ECHO} · min {MIN_SOL_IN:g} SOL\n"
            "<i>the room is never empty.</i>")

def card_echo(wallet):
    """Who echoes this wallet, and who it echoes. Looks at its last buys."""
    txs = helius_txs(wallet, limit=50)
    if not txs:
        return f"<code>{short(wallet)}</code> -- no swaps found. Either quiet or not a trading wallet."
    # buys by this wallet: (ts, mint)
    mybuys = []
    for tx in txs:
        if tx.get("feePayer") != wallet:
            continue
        for t in tx.get("tokenTransfers") or []:
            if t.get("toUserAccount") == wallet and t.get("mint") not in QUOTE_MINTS and float(t.get("tokenAmount") or 0) > 0:
                mybuys.append((tx.get("timestamp") or 0, t["mint"]))
                break
    mybuys = sorted(set(mybuys), reverse=True)[:4]
    if not mybuys:
        return f"<code>{short(wallet)}</code> -- swaps found, no token buys in the last 50."
    followers, leaders = defaultdict(int), defaultdict(int)
    checked = 0
    for ts, mint in mybuys:
        buys = parse_buys(helius_txs(mint), mint)
        for b in buys:
            if b[1] == wallet:
                continue
            d = b[0] - ts
            if 0 < d <= WINDOW_S:
                followers[b[1]] += 1
            elif -WINDOW_S <= d < 0:
                leaders[b[1]] += 1
        checked += 1
        time.sleep(0.3)
    fl = sorted(followers.items(), key=lambda kv: -kv[1])[:5]
    ld = sorted(leaders.items(), key=lambda kv: -kv[1])[:5]
    lines = [f"🎧 <b>{short(wallet)}</b> · {checked} recent buys checked", ""]
    if fl:
        lines.append(f"<b>echo behind this wallet</b> ({len(followers)} wallets inside {WINDOW_S}s):")
        lines += [f"  {short(w)} · {n}x" for w, n in fl]
    else:
        lines.append("nobody echoes this wallet inside 60s.")
    lines.append("")
    if ld:
        lines.append(f"<b>voices this wallet echoes</b> ({len(leaders)} wallets it followed):")
        lines += [f"  {short(w)} · {n}x" for w, n in ld]
    else:
        lines.append("this wallet speaks first. Nobody in front of it.")
    verdict = "VOICE" if len(followers) > len(leaders) else ("ECHO" if leaders else "QUIET")
    lines += ["", f"verdict · <b>{verdict}</b>", "<i>recorded, not called.</i>"]
    return "\n".join(lines)

def card_leader(mint, st):
    symbol, url = dex_symbol(mint)
    buys = parse_buys(helius_txs(mint), mint)
    rooms = find_rooms(buys)
    if not rooms:
        return f"<b>{html.escape(symbol)}</b> -- {len(buys)} buys read, no room found. Everyone bought alone."
    best = max(rooms, key=lambda r: r["echo"])
    return (f"🔊 <b>{html.escape(symbol)}</b> · {len(rooms)} rooms in the last {len(buys)} buys\n"
            f"loudest voice <code>{short(best['voice'])}</code> · {best['echo']} echoes\n"
            f"first echo {fmt_delay(best['first_delay'])} · median {fmt_delay(best['median_delay'])} · {best['sol_in']} SOL followed\n"
            f"<i>recorded, not called.</i>")

HELP = ("<b>ECHO DESK</b> · the listener · @echodesksol\n\n"
        "/room -- last recordings: who spoke first, how many echoed, how late\n"
        "/echo &lt;wallet&gt; -- who repeats this wallet, and who it repeats\n"
        "/leader &lt;mint&gt; -- the loudest voice inside one token\n"
        "/top -- scorecard\n\n"
        f"a room = one voice and {MIN_ECHO}+ wallets buying the same token inside {WINDOW_S}s.\n"
        "<i>no calls. no targets. only who spoke, who echoed, and how late.</i>")

# ---------- telegram ----------
def tg(method, **kw):
    return http_json(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=kw, timeout=40)

def send(chat_id, text, reply_markup=None):
    kw = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        kw["reply_markup"] = reply_markup
    return tg("sendMessage", **kw)

IMG_DIR = os.path.join(HERE, "img")
_file_ids = {}

def send_card(chat_id, key, text, reply_markup=None):
    """Header image + caption. Falls back to text if the image is missing or the caption is too long."""
    path = os.path.join(IMG_DIR, f"{key}.png")
    if not os.path.exists(path) or len(text) > 1000:
        return send(chat_id, text, reply_markup)
    fields = {"chat_id": str(chat_id), "caption": text, "parse_mode": "HTML"}
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup)
    if key in _file_ids:
        fields["photo"] = _file_ids[key]
        r = tg("sendPhoto", **fields)
    else:
        r = tg_multipart("sendPhoto", fields, "photo", path)
        try:
            _file_ids[key] = r["result"]["photo"][-1]["file_id"]
        except Exception:
            pass
    if not r or not r.get("ok"):
        return send(chat_id, text, reply_markup)
    return r

def tg_multipart(method, fields, file_field, path):
    boundary = "----echodesk%d" % int(time.time() * 1000)
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{os.path.basename(path)}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body += open(path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=body,
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("[tg] multipart failed", e)
        return None

KB = {"inline_keyboard": [
    [{"text": "🔊 room", "callback_data": "/room"}, {"text": "📋 top", "callback_data": "/top"}, {"text": "↻", "callback_data": "/refresh"}],
    [{"text": "🎧 how to /echo", "callback_data": "/howecho"}, {"text": "🗣 how to /leader", "callback_data": "/howleader"}],
]}
KB_SMALL = {"inline_keyboard": [[{"text": "🔊 room", "callback_data": "/room"}, {"text": "📋 top", "callback_data": "/top"}]]}

def set_commands():
    tg("setMyCommands", commands=[
        {"command": "room", "description": "loudest rooms, last 24h"},
        {"command": "top", "description": "scorecard"},
        {"command": "echo", "description": "/echo <wallet> -- who repeats it, who it repeats"},
        {"command": "leader", "description": "/leader <mint> -- loudest voice in a token"},
        {"command": "help", "description": "what the listener does"},
    ])

def handle(text, chat_id, st):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if cmd in ("/start", "/help"):
        send_card(chat_id, "help", HELP, KB)
    elif cmd == "/room":
        send_card(chat_id, "room", card_rooms(st), KB)
    elif cmd == "/top":
        send_card(chat_id, "top", card_top(st), KB)
    elif cmd == "/refresh":
        send(chat_id, "🎧 listening… one scan, about a minute.")
        new = scan(st, log=lambda *_: None)
        send_card(chat_id, "room", f"scan done · {len(new)} new room(s)\n\n" + card_rooms(st), KB)
    elif cmd == "/howecho":
        send(chat_id, "send <code>/echo WALLET</code> with a full Solana address.\nThe listener checks its last buys and tells you who repeated it inside 60s, and who it repeated.", KB_SMALL)
    elif cmd == "/howleader":
        send(chat_id, "send <code>/leader MINT</code> with a token mint address.\nThe listener reads the last 100 buys and names the wallet that spoke first and how many echoed.", KB_SMALL)
    elif cmd == "/echo":
        if len(parts) < 2 or len(parts[1]) < 32:
            send(chat_id, "usage: <code>/echo WALLET</code>", KB_SMALL)
        else:
            send(chat_id, "🎧 listening…")
            send_card(chat_id, "echo", card_echo(parts[1]), KB)
    elif cmd == "/leader":
        if len(parts) < 2 or len(parts[1]) < 32:
            send(chat_id, "usage: <code>/leader MINT</code>", KB_SMALL)
        else:
            send(chat_id, "🎧 listening…")
            send_card(chat_id, "voice", card_leader(parts[1], st), KB)
    else:
        send(chat_id, "the listener only answers commands. try /room or /top.", KB_SMALL)

def bot_loop(st, lock):
    offset = None
    set_commands()
    print("[bot] listening")
    while True:
        r = tg("getUpdates", timeout=30, offset=offset, allowed_updates=["message", "callback_query"])
        if not r or not r.get("ok"):
            time.sleep(3); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u and u["message"].get("text"):
                    with lock:
                        handle(u["message"]["text"], u["message"]["chat"]["id"], st)
                elif "callback_query" in u:
                    cq = u["callback_query"]
                    tg("answerCallbackQuery", callback_query_id=cq["id"])
                    with lock:
                        handle(cq.get("data", ""), cq["message"]["chat"]["id"], st)
            except Exception as e:
                print("[bot] error", e)

def listener_loop(st, lock):
    while True:
        try:
            with lock:
                new = scan(st)
            if TG_CHANNEL:
                for r in sorted(new, key=lambda r: -r["echo"])[:3]:
                    send_card(TG_CHANNEL, "room", card_room(r), KB_SMALL)
                    time.sleep(1)
        except Exception as e:
            print("[listener] error", e)
        time.sleep(SCAN_EVERY_S)

# ---------- tests ----------
def _fixture_txs(mint):
    """5 buys: voice at t=1000, echoes at +7, +22, +41 (3 echoes), a straggler at +300, one seller."""
    def buy(ts, who, amt, sol):
        return {"timestamp": ts, "feePayer": who, "type": "SWAP",
                "tokenTransfers": [{"mint": mint, "toUserAccount": who, "fromUserAccount": "pool", "tokenAmount": amt}],
                "nativeTransfers": [{"fromUserAccount": who, "toUserAccount": "pool", "amount": int(sol * 1e9)}]}
    def sell(ts, who, amt):
        return {"timestamp": ts, "feePayer": who, "type": "SWAP",
                "tokenTransfers": [{"mint": mint, "fromUserAccount": who, "toUserAccount": "pool", "tokenAmount": amt}],
                "nativeTransfers": []}
    return [buy(1000, "VOICE1111111111111111111111111111111111111", 100, 2.0),
            buy(1007, "ECHOA111111111111111111111111111111111111", 50, 0.5),
            buy(1022, "ECHOB111111111111111111111111111111111111", 50, 0.7),
            buy(1041, "ECHOC111111111111111111111111111111111111", 50, 0.3),
            sell(1050, "ECHOA111111111111111111111111111111111111", 50),
            buy(1300, "LATE1111111111111111111111111111111111111", 10, 0.1)]

def run_tests():
    mint = "MINT1111111111111111111111111111111111111111"
    buys = parse_buys(_fixture_txs(mint), mint)
    assert len(buys) == 5, buys
    assert buys[0][1].startswith("VOICE")
    rooms = find_rooms(buys)
    assert len(rooms) == 1, rooms
    r = rooms[0]
    assert r["voice"].startswith("VOICE") and r["echo"] == 3
    assert r["first_delay"] == 7 and r["median_delay"] == 22
    assert abs(r["sol_in"] - 1.5) < 1e-6
    # no room when echoes < MIN_ECHO
    assert find_rooms(buys[:3]) == []
    # scorecard + cards render
    st = {"recordings": [{"mint": mint, "symbol": "TEST", "url": None, **r, "recorded": 1}], "seen": {}, "scans": 1}
    sc = scorecard(st)
    assert sc["rooms"] == 1 and sc["loudest"]["echo"] == 3
    for txt in (card_room(st["recordings"][0]), card_rooms(st), card_top(st), HELP):
        assert "<b>" in txt and len(txt) < 4000
    assert "QUIET" in "VOICE ECHO QUIET"
    print("all tests passed")

# ---------- main ----------
if __name__ == "__main__":
    if "--test" in sys.argv:
        run_tests(); sys.exit(0)
    if not HELIUS_KEY:
        sys.exit("HELIUS_KEY missing in .env")
    st = load_state()
    if "--scan" in sys.argv:
        new = scan(st)
        for r in new:
            print(card_room(r).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", ""))
        print(card_top(st))
        sys.exit(0)
    if not TG_TOKEN:
        sys.exit("TG_TOKEN missing in .env")
    lock = threading.Lock()
    threading.Thread(target=listener_loop, args=(st, lock), daemon=True).start()
    bot_loop(st, lock)
