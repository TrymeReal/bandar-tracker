"""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA BANDAR TRACKER v3          ║
║   Auto-scan token baru + Track whale wallet  ║
╚══════════════════════════════════════════════╝

MODE:
  python bandar_tracker.py           → auto-scan token baru, detect bandar
  python bandar_tracker.py <wallet>  → track wallet spesifik

SETUP:
  pip install requests websocket-client
  Isi .env (TG_TOKEN, TG_CHAT_ID, HELIUS_API_KEY, GMGN_API_KEY)
"""

import json, time, sys, os, threading, requests, websocket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════
# CONFIG — Baca dari .env (kalau ada)
# ══════════════════════════════════════════════
_env = {}
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _env[k.strip()] = v.strip()

TELEGRAM_TOKEN   = _env.get("TG_TOKEN") or os.environ.get("TG_TOKEN", "")
TELEGRAM_CHAT_ID = _env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
TELEGRAM_THREAD  = int(_env.get("TG_THREAD") or os.environ.get("TG_THREAD") or "0")

HELIUS_API_KEY   = _env.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API_KEY", "")
GMGN_API_KEY     = _env.get("GMGN_API_KEY") or os.environ.get("GMGN_API_KEY", "")
RPC_FALLBACK     = "https://api.mainnet-beta.solana.com"

# Mode auto-scan
SCAN_INTERVAL    = 15
MIN_LIQUIDITY    = 1000
ALERT_SCORE      = 50
MIN_BANDAR_SOL   = 0.5

# Mode track wallet
TRACK_INTERVAL   = 10
MIN_BUY_USD      = 5000

# Mode auto-discover smart money
DISCOVER_INTERVAL    = 60
DISCOVER_MIN_WINRATE = 0.60
DISCOVER_MIN_TOKENS  = 3
DISCOVER_MAX_WALLETS = 50
DISCOVER_MIN_HOLDER   = 0.3
PUMPED_MIN_LIQ       = 5_000

PRESET_WALLETS = {}

SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y68YB",
}
# ══════════════════════════════════════════════

# ── State global ──────────────────────────────
tracked_wallets = dict(PRESET_WALLETS)
seen_sigs       = set()
seen_mints      = set()
token_cache     = {}
_sol_price      = 150.0
DATA_FILE       = os.path.join(os.path.dirname(__file__), "wallets.json")
SEEN_SIGS_FILE  = os.path.join(os.path.dirname(__file__), "seen_sigs.json")
SEEN_MINTS_FILE = os.path.join(os.path.dirname(__file__), "seen_mints.json")
MODE_FILE       = os.path.join(os.path.dirname(__file__), "mode.json")
discovered_set  = set()

# ══════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════

def esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def log(msg, prefix=""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix}{msg}")

def rpc(method, params, url=None):
    endpoint = url or (
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY else RPC_FALLBACK
    )
    try:
        r = requests.post(endpoint, json={
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params
        }, timeout=10)
        return r.json().get("result")
    except:
        return None

def get_sol_price():
    global _sol_price
    try:
        r = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=4)
        _sol_price = float(r.json()["data"]["SOL"]["price"])
    except:
        pass
    return _sol_price

def get_current_holdings(addr: str) -> set[str]:
    """Return set of mint addresses currently held by wallet (skip SOL/stable)"""
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else RPC_FALLBACK,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    addr,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"encoding": "jsonParsed"}
                ]
            },
            timeout=10
        )
        accounts = r.json().get("result", {}).get("value", [])
        mints = set()
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint", "")
            amt = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
            if amt > 0 and mint not in SKIP_MINTS:
                mints.add(mint)
        return mints
    except:
        return set()

def calculate_holder_score(bought_mints: list[str], current_mints: set[str]) -> float:
    """Return 0.0-1.0 — berapa % token yg dibeli masih dipegang"""
    if not bought_mints:
        return 0.0
    held = sum(1 for m in bought_mints if m in current_mints)
    return held / len(bought_mints)

def save_wallets():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(tracked_wallets, f, indent=2)
    except:
        pass

def load_wallets():
    global tracked_wallets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                saved = json.load(f)
            tracked_wallets.update(saved)
            log(f"Loaded {len(saved)} saved wallets")
        except:
            pass

def save_seen():
    try:
        with open(SEEN_SIGS_FILE, "w") as f:
            json.dump({"sigs": list(seen_sigs)[-5000:]}, f)
    except:
        pass
    try:
        with open(SEEN_MINTS_FILE, "w") as f:
            json.dump({"mints": list(seen_mints)[-5000:]}, f)
    except:
        pass

def load_seen():
    global seen_sigs, seen_mints
    if os.path.exists(SEEN_SIGS_FILE):
        try:
            with open(SEEN_SIGS_FILE) as f:
                data = json.load(f)
                seen_sigs = set(data.get("sigs", []))
            log(f"Loaded {len(seen_sigs)} seen sigs")
        except:
            pass
    if os.path.exists(SEEN_MINTS_FILE):
        try:
            with open(SEEN_MINTS_FILE) as f:
                data = json.load(f)
                seen_mints = set(data.get("mints", []))
            log(f"Loaded {len(seen_mints)} seen mints")
        except:
            pass

def save_mode(mode: str):
    try:
        with open(MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except:
        pass

def load_mode() -> str | None:
    try:
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE) as f:
                return json.load(f).get("mode")
    except:
        pass
    return None

_last_update_id = 0
_tg_listener_started = False

def listen_tg_commands():
    global _last_update_id, _tg_listener_started
    if _tg_listener_started:
        return
    _tg_listener_started = True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    def poll():
        global _last_update_id
        while True:
            try:
                params = {"offset": _last_update_id + 1, "timeout": 10, "allowed_updates": ["message"]}
                r = requests.get(url, params=params, timeout=15)
                if not r.ok:
                    continue
                for upd in r.json().get("result", []):
                    _last_update_id = upd["update_id"]
                    msg = upd.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    thread_id = msg.get("message_thread_id")
                    text = (msg.get("text") or "").strip()

                    if chat_id != int(TELEGRAM_CHAT_ID):
                        continue
                    if TELEGRAM_THREAD and thread_id != TELEGRAM_THREAD:
                        continue
                    if text.startswith("/mode"):
                        parts = text.split()
                        if len(parts) != 2 or not parts[1].isdigit():
                            tg("❌ Format: <code>/mode &lt;angka&gt;</code> — contoh: <code>/mode 6</code>")
                            continue
                        mode_raw = parts[1]
                    elif text.isdigit():
                        mode_raw = text
                    else:
                        continue
                    if mode_raw not in ("1", "2", "3", "4", "5", "6", "7"):
                        tg(f"❌ Mode {mode_raw} tidak dikenal. Pilih 1-7")
                        continue
                    save_mode(mode_raw)
                    tg(f"✅ Mode diubah ke <b>{mode_raw}</b> — akan aktif di cycle berikutnya")
                    log(f"Mode changed to {mode_raw} via Telegram", "📱 ")
            except:
                pass
            time.sleep(3)

    t = threading.Thread(target=poll, daemon=True)
    t.start()

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════

def tg(msg: str):
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if TELEGRAM_THREAD:
            payload["message_thread_id"] = TELEGRAM_THREAD
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if not r.ok:
            log(f"Telegram API error: {r.status_code} {r.text[:200]}", "❌ ")
    except Exception as e:
        log(f"Telegram error: {e}", "❌ ")

# ══════════════════════════════════════════════
# HEURISTICS — Deteksi bandar
# ══════════════════════════════════════════════

def get_wallet_age(addr: str) -> tuple[float, str]:
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 1}]) or []
    if not sigs:
        return 999_999, "Unknown"
    tx = rpc("getTransaction", [sigs[-1]["signature"], {
        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
    }])
    if not tx:
        return 999_999, "Unknown"
    age = time.time() - (tx.get("blockTime") or 0)
    h = age / 3600
    if h < 1:   return age, "BARU < 1 jam 🍼"
    if h < 6:   return age, "< 6 jam"
    if h < 24:  return age, "< 24 jam"
    return age, f"{h/24:.0f} hari"

def get_funding_source(addr: str) -> dict:
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 20}]) or []
    for s in sigs:
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        pre  = tx.get("meta", {}).get("preBalances", []) or []
        post = tx.get("meta", {}).get("postBalances", []) or []
        for i, k in enumerate(keys):
            a = k if isinstance(k, str) else k.get("pubkey", "")
            if not a or a == addr:
                continue
            diff = (post[i] if i < len(post) else 0) - (pre[i] if i < len(pre) else 0)
            if diff < -1_000_000:
                return {"funder": a, "amount": abs(diff) / 1e9}
    return {"funder": None, "amount": 0}

def get_early_entry_rank(addr: str, token_mint: str) -> int | None:
    """
    FIX: Cek apakah wallet ini ada di early buyer token dengan cara yang bener.
    Ambil TX dari mint account, lalu cek owner dari postTokenBalances.
    Return rank (1-based) atau None kalau ga ketemu.
    """
    sigs = rpc("getSignaturesForAddress", [token_mint, {"limit": 30}]) or []
    for rank, s in enumerate(reversed(sigs), 1):   # reversed = dari paling awal
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
        for p in post_tok:
            if p.get("mint") != token_mint:
                continue
            owner = p.get("owner", "")
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            had   = any(x.get("mint") == token_mint and x.get("owner") == owner for x in pre_tok)
            if owner == addr and amt > 0 and not had:
                return rank
    return None

def score_wallet(addr: str, token_mint: str) -> dict:
    """
    Skor wallet 0–100 berdasarkan heuristics bandar:
    - Wallet age     (max 35)
    - Funding source (max 30)
    - Early entry    (max 25) — FIX: pakai get_early_entry_rank yg bener
    - Cluster        (max 10)
    """
    score   = 0
    reasons = []

    # ── 1. Wallet age (max 35) ──
    age_sec, age_lbl = get_wallet_age(addr)
    if age_sec < 3_600:    score += 35; reasons.append(f"Wallet baru banget {age_lbl}")
    elif age_sec < 21_600: score += 25; reasons.append(f"Wallet {age_lbl}")
    elif age_sec < 86_400: score += 15; reasons.append(f"Wallet {age_lbl}")
    else:                              reasons.append(f"Wallet lama ({age_lbl})")

    # ── 2. Funding source (max 30) ──
    funding = get_funding_source(addr)
    if funding["funder"]:
        score += 15
        reasons.append(f"Dana dari {funding['funder'][:8]}... ({funding['amount']:.2f} SOL)")
        funder_age, _ = get_wallet_age(funding["funder"])
        if funder_age < 86_400:
            score += 15
            reasons.append("Funder juga wallet baru ⚠️")
    else:
        reasons.append("Funding source tidak jelas")

    # ── 3. Early entry (max 25) — FIX ──
    rank = get_early_entry_rank(addr, token_mint)
    if rank is not None:
        if rank == 1:    score += 25; reasons.append("🎯 TX PERTAMA di token ini!")
        elif rank <= 3:  score += 20; reasons.append(f"Early buyer rank #{rank}")
        elif rank <= 10: score += 10; reasons.append(f"Early buyer rank #{rank}")
        else:                         reasons.append(f"Entry rank #{rank}")
    else:
        reasons.append("Bukan early buyer")

    # ── 4. Cluster check (max 10) ──
    wallet_sigs = rpc("getSignaturesForAddress", [addr, {"limit": 15}]) or []
    co_wallets = {}
    for s in wallet_sigs:
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx: continue
        keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        for k in keys:
            a = k if isinstance(k, str) else k.get("pubkey", "")
            if a and a != addr:
                co_wallets[a] = co_wallets.get(a, 0) + 1
    cluster = [k for k, v in co_wallets.items() if v >= 3]
    if len(cluster) >= 5:   score += 10; reasons.append(f"Cluster {len(cluster)} wallets terdeteksi 🕸️")
    elif len(cluster) >= 2: score += 5;  reasons.append(f"Small cluster ({len(cluster)} wallets)")

    score = max(0, min(100, score))
    conf  = "TINGGI 🔴" if score >= 70 else ("SEDANG 🟡" if score >= 40 else "RENDAH 🟢")

    return {
        "score": score, "conf": conf, "reasons": reasons,
        "age_lbl": age_lbl, "funding": funding,
    }

# ══════════════════════════════════════════════
# TOKEN INFO
# ══════════════════════════════════════════════

def get_token_info(mint: str) -> dict:
    if mint in token_cache:
        return token_cache[mint]
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=5
        )
        pair = r.json().get("pairs", [{}])[0]
        info = {
            "name":    pair.get("baseToken", {}).get("name", mint[:8] + "..."),
            "symbol":  pair.get("baseToken", {}).get("symbol", "???"),
            "price":   pair.get("priceUsd", "?"),
            "liq":     float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "change":  float(pair.get("priceChange", {}).get("h24", 0) or 0),
            "dex_url": pair.get("url", f"https://dexscreener.com/solana/{mint}"),
        }
    except:
        info = {"name": mint[:8]+"...", "symbol": "???", "price": "?", "liq": 0,
                "change": 0, "dex_url": f"https://dexscreener.com/solana/{mint}"}
    token_cache[mint] = info
    return info

def fetch_new_tokens() -> list[dict]:
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=8
        )
        tokens = r.json() or []
        return [
            {
                "mint":   t["tokenAddress"],
                "name":   t.get("tokenName", t.get("symbol", "")),
                "symbol": t.get("symbol", "???"),
            }
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except:
        return []

# ══════════════════════════════════════════════
# AUTO-SCAN MODE
# ══════════════════════════════════════════════

def get_first_buyer(mint: str):
    """Return (wallet_addr, tx_sig, sol_spent) dari buyer pertama token
    FIX: skip creator/minter (sol_spent terlalu kecil), cari real DEX buyer
    """
    sigs = rpc("getSignaturesForAddress", [mint, {"limit": 15}]) or []
    if not sigs:
        return None
    for s in reversed(sigs):
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        pre_bal  = tx.get("meta", {}).get("preBalances",  [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
        for p in post_tok:
            if p.get("mint") != mint: continue
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            owner = p.get("owner", "")
            had   = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
            if not had and owner and amt > 0:
                if sol_spent < MIN_BANDAR_SOL:
                    continue
                return owner, s["signature"], sol_spent
    return None

def process_token(token: dict):
    mint = token["mint"]
    sym  = token.get("symbol", "???")

    if mint in SKIP_MINTS:
        return

    info = get_token_info(mint)
    if info["liq"] < MIN_LIQUIDITY:
        return
    log(f"${sym} ({mint[:8]}...) liq=${info['liq']:.0f} — scanning...", "🔍 ")

    res = get_first_buyer(mint)
    if not res:
        return
    buyer, sig, sol_spent = res

    if sol_spent < MIN_BANDAR_SOL:
        log(f"⏭️ {buyer[:8]}... cuma {sol_spent:.3f} SOL (skip)", "⏭️ ")
        return

    analysis = score_wallet(buyer, mint)
    score    = analysis["score"]
    conf     = analysis["conf"]
    reasons  = analysis["reasons"]

    log(f"  {buyer[:8]}... score={score}/100 {conf} spent={sol_spent:.2f}SOL | {reasons[0] if reasons else ''}")

    if score >= ALERT_SCORE:
        r_str = "\n".join(f"▸ {r}" for r in reasons[:4])
        if sol_spent >= 50:   tier = "🐳 MEGA WHALE"
        elif sol_spent >= 10: tier = "🐋 WHALE"
        elif sol_spent >= 1:  tier = "🐟 MINI WHALE"
        else:                 tier = "🎯 BANDAR"

        msg = (
            f"{tier}\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Token</b>: ${esc(sym)} ({esc(info['name'])})\n"
            f"<b>CA</b>: <code>{esc(mint)}</code>\n"
            f"<b>Liquidity</b>: ${info['liq']:,.0f}\n"
            f"<b>Price</b>: ${esc(info['price'])}\n"
            f"<b>Bandar spend</b>: {sol_spent:.2f} SOL (${sol_spent*_sol_price:,.0f})\n\n"
            f"<b>Wallet</b>: <code>{esc(buyer)}</code>\n"
            f"<b>Age</b>: {esc(analysis['age_lbl'])}\n"
            f"<b>Score</b>: {score}/100 | {esc(conf)}\n\n"
            f"{esc(r_str)}\n\n"
            f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
            f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
            f"<a href='https://solscan.io/account/{esc(buyer)}'>Wallet</a> | "
            f"<a href='https://gmgn.ai/sol/address/{esc(buyer)}'>GMGN</a>"
        )
        log(f"  → Alert sent! Score {score}/100 | {sol_spent:.2f}SOL", "✅ ")
        tg(msg)

def auto_scan_once():
    tokens = fetch_new_tokens()
    new_tokens = [t for t in tokens if t["mint"] not in seen_mints]
    if new_tokens:
        log(f"{len(new_tokens)} token baru ditemukan")
        for t in new_tokens:
            seen_mints.add(t["mint"])
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(process_token, t): t for t in new_tokens}
            for f in as_completed(futures):
                try: f.result()
                except Exception as e: log(f"Error: {e}", "❌ ")
    else:
        log("Tidak ada token baru", "💤 ")

def auto_scan_loop():
    log("Auto-scan mode aktif — mencari bandar dari token baru", "🔍 ")
    log(f"Min liquidity: ${MIN_LIQUIDITY} | Alert score: {ALERT_SCORE}/100")
    while True:
        auto_scan_once()
        time.sleep(SCAN_INTERVAL)

# ══════════════════════════════════════════════
# WS SCAN — Real-time via WebSocket (Pump.fun)
# ══════════════════════════════════════════════

PUMPFUN_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

def _handle_ws_token(sig, mint, owner, sol_spent=0):
    if mint in seen_mints or mint in SKIP_MINTS:
        return
    seen_mints.add(mint)

    if sol_spent < MIN_BANDAR_SOL:
        log(f"⏭️ Skip {mint[:8]}... bandar cuma {sol_spent:.3f} SOL", "⏭️ ")
        return

    log(f"⚡ Token baru via WS: {mint[:12]}... owner={owner[:8]}... spent={sol_spent:.2f}SOL", "⚡ ")
    time.sleep(2)
    info = get_token_info(mint)

    if info["liq"] < MIN_LIQUIDITY:
        log(f"⏭️ Skip {mint[:8]}... belum migrasi (liq=${info['liq']:.0f})", "⏭️ ")
        return

    analysis = score_wallet(owner, mint)
    score    = analysis["score"]
    conf     = analysis["conf"]
    reasons  = analysis["reasons"]
    log(f"  {owner[:8]}... score={score}/100 {conf}", "⚡ ")

    if score >= ALERT_SCORE:
        r_str   = "\n".join(f"▸ {esc(r)}" for r in reasons[:4])
        liq_str = f"Liquidity: ${info['liq']:,.0f}" if info['liq'] > 0 else "🚀 Bonding curve"
        if sol_spent >= 50:   tier = "🐳 MEGA WHALE"
        elif sol_spent >= 10: tier = "🐋 WHALE"
        elif sol_spent >= 1:  tier = "🐟 MINI WHALE"
        else:                 tier = "🎯 BANDAR"

        msg = (
            f"{tier} ⚡\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
            f"<b>CA</b>: <code>{esc(mint)}</code>\n"
            f"{liq_str}\n"
            f"<b>Price</b>: ${esc(info['price'])}\n"
            f"<b>Bandar spend</b>: {sol_spent:.2f} SOL (${sol_spent*_sol_price:,.0f})\n\n"
            f"<b>Wallet</b>: <code>{esc(owner)}</code>\n"
            f"<b>Age</b>: {esc(analysis['age_lbl'])}\n"
            f"<b>Score</b>: {score}/100 | {esc(conf)}\n\n"
            f"{r_str}\n\n"
            f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
            f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
            f"<a href='https://solscan.io/account/{esc(owner)}'>Wallet</a> | "
            f"<a href='https://gmgn.ai/sol/address/{esc(owner)}'>GMGN</a>"
        )
        tg(msg)

def ws_scan_loop():
    if not HELIUS_API_KEY:
        log("HELIUS_API_KEY wajib buat WS mode!", "❌ ")
        return

    log("⚡ WebSocket scan — real-time Pump.fun detector", "⚡ ")
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

    def on_msg(ws, raw):
        try:
            data = json.loads(raw)
            if "params" not in data:
                return
            val  = data["params"]["result"]["value"]
            sig  = val["signature"]
            logs = val.get("logs", [])
            if val.get("err") or sig in seen_sigs:
                return
            seen_sigs.add(sig)

            if not any("reate" in l for l in logs):
                return

            tx = rpc("getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
            }])
            if not tx:
                return

            pre_bal  = tx.get("meta", {}).get("preBalances",  [0]) or [0]
            post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
            sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

            pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
            post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []

            for p in post_tok:
                mint  = p.get("mint", "")
                owner = p.get("owner", "")
                amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                if not mint or len(mint) < 32 or amt <= 0 or mint in SKIP_MINTS:
                    continue
                had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
                if had:
                    continue
                _handle_ws_token(sig, mint, owner, sol_spent)
        except Exception as e:
            log(f"WS msg error: {e}", "❌ ")

    def on_open(ws):
        sub = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [PUMPFUN_ID]}, {"commitment": "processed"}]
        })
        ws.send(sub)
        log("🔌 WS connected! Listening for new Pump.fun tokens...")

    while True:
        try:
            wsa = websocket.WebSocketApp(ws_url,
                on_open=on_open, on_message=on_msg,
                on_error=lambda w, e: log(f"WS error: {e}", "❌ "),
                on_close=lambda w, a, b: log("🔄 WS closed, reconnecting..."))
            wsa.run_forever()
        except Exception as e:
            log(f"WS connection failed: {e}, retry 5s", "❌ ")
            time.sleep(5)

# ══════════════════════════════════════════════
# TRACK MODE
# ══════════════════════════════════════════════

def poll_wallet(addr: str, label: str):
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
    for s in sigs:
        sig = s["signature"]
        if sig in seen_sigs: continue
        seen_sigs.add(sig)

        tx = rpc("getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx: continue
        meta     = tx.get("meta") or {}
        pre_tok  = meta.get("preTokenBalances",  []) or []
        post_tok = meta.get("postTokenBalances", []) or []
        pre_sol  = (meta.get("preBalances",  [0]) or [0])[0]
        post_sol = (meta.get("postBalances", [0]) or [0])[0]
        sol_spent = max(0, (pre_sol - post_sol) / 1e9)

        def _build_map(balances):
            m = {}
            for p in balances:
                if p.get("owner") != addr: continue
                mint = p.get("mint", "")
                if mint in SKIP_MINTS: continue
                amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                m[mint] = m.get(mint, 0) + amt
            return m

        pre_map  = _build_map(pre_tok)
        post_map = _build_map(post_tok)

        usd_val = sol_spent * get_sol_price()

        # DETECT BUY
        for mint, post_amt in post_map.items():
            pre_amt = pre_map.get(mint, 0)
            if post_amt <= pre_amt:
                continue
            info = get_token_info(mint)
            log(f"{label} BUY ${info['symbol']} {sol_spent:.2f}SOL (~${usd_val:,.0f})", "🐋 ")
            msg = (
                f"🟢 <b>BUY</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Wallet</b>: {esc(label)}\n"
                f"<code>{esc(addr)}</code>\n\n"
                f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                f"<b>Amount</b>: {sol_spent:.2f} SOL (~${usd_val:,.0f})\n"
                f"<b>Price</b>: ${esc(info['price'])}\n\n"
                f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
                f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>"
            )
            tg(msg)
            break

        # DETECT SELL
        for mint, pre_amt in pre_map.items():
            post_amt = post_map.get(mint, 0)
            if post_amt >= pre_amt:
                continue
            sold_amt = pre_amt - post_amt
            info = get_token_info(mint)
            log(f"{label} SELL {sold_amt:.4f} {info['symbol']}", "💰 ")
            msg = (
                f"🔴 <b>SELL</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Wallet</b>: {esc(label)}\n"
                f"<code>{esc(addr)}</code>\n\n"
                f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                f"<b>Sold</b>: {sold_amt:.4f} tokens\n"
                f"<b>Price</b>: ${esc(info['price'])}\n\n"
                f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                f"<a href='{esc(info['dex_url'])}'>DexScreener</a>"
            )
            tg(msg)
            break

def track_once():
    if not tracked_wallets:
        log("Tidak ada wallet yang di-track.", "⚠️ ")
        return
    log(f"🎯 Track: memeriksa {len(tracked_wallets)} wallet...", "🎯 ")
    for addr, label in list(tracked_wallets.items()):
        try:
            poll_wallet(addr, label if isinstance(label, str) else label.get("label", addr[:8]))
        except Exception as e:
            log(f"Error polling {addr[:8]}: {e}", "❌ ")
    log(f"🎯 Track selesai — {len(tracked_wallets)} wallets diperiksa", "🎯 ")

def track_loop():
    if not tracked_wallets:
        log("Tidak ada wallet yang di-track. Tambah via menu atau CLI arg.", "⚠️ ")
        return
    log(f"Track mode: monitoring {len(tracked_wallets)} wallets", "🎯 ")
    log(f"Min alert: ${MIN_BUY_USD:,} | Poll: {TRACK_INTERVAL}s")
    for addr in tracked_wallets:
        sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
        for s in sigs: seen_sigs.add(s["signature"])
    log(f"Seeded {len(seen_sigs)} existing signatures")
    while True:
        track_once()
        time.sleep(TRACK_INTERVAL)

# ══════════════════════════════════════════════
# AUTO-DISCOVER SMART MONEY
# ══════════════════════════════════════════════

def fetch_pumped_tokens(limit: int = 30) -> list[dict]:
    results = []
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=8)
        for t in (r.json() or []):
            if t.get("chainId") == "solana":
                mint = t.get("tokenAddress", "")
                if mint and mint not in SKIP_MINTS:
                    results.append({"mint": mint, "source": "boosted"})
    except:
        pass

    if len(results) < 10:
        try:
            r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=8)
            for t in (r.json() or []):
                if t.get("chainId") == "solana":
                    mint = t.get("tokenAddress", "")
                    if mint and mint not in SKIP_MINTS:
                        results.append({"mint": mint, "source": "latest"})
        except:
            pass

    seen = set()
    out  = []
    for t in results:
        if t["mint"] not in seen:
            seen.add(t["mint"])
            out.append(t)
    return out[:limit]

def get_early_buyers(mint: str, top_n: int = 5) -> list[tuple[str, float]]:
    sigs = rpc("getSignaturesForAddress", [mint, {"limit": 20}]) or []
    if not sigs:
        return []

    buyers = []
    for s in reversed(sigs):
        if len(buyers) >= top_n:
            break
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue

        pre_bal  = tx.get("meta", {}).get("preBalances",  [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []

        for p in post_tok:
            if p.get("mint") != mint: continue
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            owner = p.get("owner", "")
            if amt <= 0 or not owner: continue
            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
            if had: continue
            if sol_spent >= MIN_BANDAR_SOL:
                buyers.append((owner, sol_spent))
            break

    return buyers

def analyze_wallet_history(addr: str) -> dict:
    """
    FIX: Kalau GMGN_API_KEY ada → pakai GMGN (1 call, akurat pakai realized PnL).
    Fallback ke RPC kalau tidak ada key.
    """
    if GMGN_API_KEY:
        return _analyze_wallet_gmgn(addr)
    return _analyze_wallet_rpc(addr)

def _analyze_wallet_gmgn(addr: str) -> dict:
    """Ambil win rate & PnL dari GMGN API — 1 HTTP call per wallet."""
    try:
        headers = {
            "Authorization": f"Bearer {GMGN_API_KEY}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        r = requests.get(
            f"https://gmgn.ai/api/v1/smartmoney/sol/walletNew/{addr}",
            headers=headers,
            params={"period": "7d"},
            timeout=8,
        )
        if not r.ok:
            log(f"GMGN API error {r.status_code} untuk {addr[:8]}, fallback ke RPC", "⚠️ ")
            return _analyze_wallet_rpc(addr)

        data            = r.json().get("data", {}) or {}
        win_rate        = float(data.get("win_rate") or 0)
        realized_profit = float(data.get("realized_profit") or 0)
        total_trades    = int(data.get("buy_30d") or 0)
        wins            = round(win_rate * total_trades)

        token_results = []
        for t in (data.get("token_list") or []):
            token_results.append({
                "mint":   t.get("address", ""),
                "symbol": t.get("symbol", "???"),
                "liq":    float(t.get("liquidity") or 0),
                "pumped": float(t.get("realized_profit") or 0) > 0,
                "sol":    float(t.get("cost") or 0) / (_sol_price or 150),
            })

        return {
            "win_rate":        win_rate,
            "wins":            wins,
            "total":           total_trades,
            "realized_profit": realized_profit,
            "tokens":          token_results,
        }

    except Exception as e:
        log(f"GMGN error untuk {addr[:8]}: {e}, fallback ke RPC", "⚠️ ")
        return _analyze_wallet_rpc(addr)

def _analyze_wallet_rpc(addr: str) -> dict:
    """Fallback: hitung dari raw RPC (boros tapi tetap jalan tanpa GMGN key)."""
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 50}]) or []
    if not sigs:
        return {"win_rate": 0, "wins": 0, "total": 0, "tokens": []}

    mints_bought = {}
    for s in sigs:
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx: continue
        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
        pre_bal  = tx.get("meta", {}).get("preBalances",  [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        for p in post_tok:
            mint  = p.get("mint", "")
            owner = p.get("owner", "")
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            if owner != addr or not mint or amt <= 0 or mint in SKIP_MINTS: continue
            had = any(x.get("mint") == mint and x.get("owner") == addr for x in pre_tok)
            if not had:
                mints_bought[mint] = sol_spent

    if not mints_bought:
        return {"win_rate": 0, "wins": 0, "total": 0, "tokens": []}

    wins = 0
    token_results = []
    for mint, sol in list(mints_bought.items())[:15]:
        info   = get_token_info(mint)
        liq    = info.get("liq", 0)
        # FIX: cek liq DAN price change supaya lebih akurat
        change = info.get("change", 0)
        pumped = liq >= PUMPED_MIN_LIQ and change > 0
        if pumped: wins += 1
        token_results.append({
            "mint": mint, "symbol": info.get("symbol", "???"),
            "liq": liq, "pumped": pumped, "sol": sol,
        })

    total    = len(token_results)
    win_rate = wins / total if total > 0 else 0
    return {"win_rate": win_rate, "wins": wins, "total": total, "tokens": token_results}

def discover_smart_wallets_from_token(token: dict) -> list[str]:
    mint = token["mint"]
    info = get_token_info(mint)
    if info["liq"] < PUMPED_MIN_LIQ:
        return []

    log(f"🔎 Discover dari ${info['symbol']} ({mint[:8]}...) liq=${info['liq']:.0f}", "🧠 ")
    buyers = get_early_buyers(mint, top_n=5)
    if not buyers:
        return []

    good_wallets = []
    for i, (addr, sol_spent) in enumerate(buyers, 1):
        if addr in tracked_wallets or addr in discovered_set:
            continue
        if len(tracked_wallets) >= DISCOVER_MAX_WALLETS:
            log(f"Max wallet ({DISCOVER_MAX_WALLETS}) tercapai, skip", "⚠️ ")
            break

        log(f"   Analisis {addr[:10]}... (spent {sol_spent:.2f} SOL)", "🧠 ")
        hist  = analyze_wallet_history(addr)
        wr    = hist["win_rate"]
        wins  = hist["wins"]
        total = hist["total"]
        realized = hist.get("realized_profit", 0)

        log(f"   → win_rate={wr:.0%} ({wins}/{total})", "🧠 ")

        if not (total >= DISCOVER_MIN_TOKENS and wr >= DISCOVER_MIN_WINRATE):
            log(f"   ⏭️ Skip (win rate terlalu rendah)", "⏭️ ")
            continue

        # Cek holder score — skip flipper (buy then sell cepet)
        bought = [t.get("mint") for t in hist.get("tokens", []) if t.get("mint")]
        current_mints = get_current_holdings(addr)
        holder_score = calculate_holder_score(bought, current_mints) if bought else 0
        log(f"   → holder_score={holder_score:.0%} (holding {len(current_mints)} token)", "🧠 ")

        if current_mints and holder_score < DISCOVER_MIN_HOLDER:
            log(f"   ⏭️ Skip flipper (holder_score={holder_score:.0%})", "⏭️ ")
            continue
        if not current_mints:
            log(f"   ⚠️ Gagal cek holdings, tetap diproses", "⚠️ ")

        label = f"SmartMoney_{addr[:6]}"
        tracked_wallets[addr] = label
        discovered_set.add(addr)
        save_wallets()
        good_wallets.append(addr)

        top_wins = [t for t in hist["tokens"] if t["pumped"]][:3]
        win_str  = "\n".join(
            f"  ✅ ${esc(t['symbol'])} — liq ${t['liq']:,.0f} (spent {t['sol']:.2f} SOL)"
            for t in top_wins
        )
        realized_str = f"\n<b>Realized PnL</b>: ${realized:,.0f}" if realized else ""
        msg = (
            f"🧠 <b>HOLDER DISCOVERED</b>\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Wallet</b>: <code>{esc(addr)}</code>\n"
            f"<b>Label</b>: {esc(label)}\n\n"
            f"<b>Win Rate</b>: {wr:.0%} ({wins}/{total} tokens pumped)"
            f"{realized_str}\n"
            f"<b>Holder Score</b>: {holder_score:.0%}\n"
            f"<b>Ditemukan dari</b>: ${esc(info['symbol'])} (liq ${info['liq']:,.0f})\n"
            f"<b>SOL dipakai</b>: {sol_spent:.2f} SOL\n\n"
            f"<b>Token yang pumped:</b>\n{win_str}\n\n"
            f"<a href='https://solscan.io/account/{esc(addr)}'>Solscan</a> | "
            f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>\n\n"
            f"✅ <i>Wallet otomatis di-track!</i>"
        )
        tg(msg)
        log(f"   ✅ Auto-added: {label} (win_rate={wr:.0%})", "🧠 ")

    return good_wallets

def auto_discover_once():
    try:
        pumped = fetch_pumped_tokens(limit=20)
        log(f"📡 {len(pumped)} pumped token ditemukan", "🧠 ")
        new_wallets_total = 0
        for i, token in enumerate(pumped, 1):
            if token["mint"] in seen_mints:
                continue
            seen_mints.add(token["mint"])
            log(f"🔎 Token {i}/{len(pumped)}: {token['mint'][:8]}...", "🧠 ")
            found = discover_smart_wallets_from_token(token)
            new_wallets_total += len(found)
        if new_wallets_total:
            log(f"🧠 Siklus selesai — {new_wallets_total} wallet baru", "🧠 ")
    except Exception as e:
        log(f"Error di discover: {e}", "❌ ")

def auto_discover_loop():
    log("🧠 Auto-Discover Smart Money aktif", "🧠 ")
    log(f"   Min win rate: {DISCOVER_MIN_WINRATE:.0%} | Min tokens: {DISCOVER_MIN_TOKENS}")
    log(f"   Max wallets tracked: {DISCOVER_MAX_WALLETS}")
    while True:
        auto_discover_once()
        time.sleep(DISCOVER_INTERVAL)

# ══════════════════════════════════════════════
# INTERACTIVE MENU
# ══════════════════════════════════════════════

def print_banner():
    mode_str = "✅ GMGN API" if GMGN_API_KEY else "⚠️  RPC fallback"
    print(f"""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA BANDAR TRACKER v3          ║
║   Wallet analysis: {mode_str:<24}║
╚══════════════════════════════════════════════╝""")

def interactive_menu():
    print_banner()
    print("""
Mode:
  [1] Auto-scan token baru (deteksi bandar otomatis)
  [2] Track wallet tertentu
  [3] Auto-scan + Track bersamaan
  [4] ⚡ Fast-scan (WebSocket real-time) ⚡
  [5] ⚡ Fast-scan + Track bersamaan
  [6] 🧠 Auto-Discover Smart Money (auto-add wallet bagus)
  [7] 🧠 Auto-Discover + Track bersamaan (full mode)
  [a] Add wallet ke tracker
  [l] List tracked wallets
  [q] Quit
""")
    while True:
        c = input(">> ").strip().lower()

        if c == "1":
            print("\n🔍 Memulai auto-scan...\n")
            tg("🔍 <b>Bandar Tracker aktif</b> — Auto-scan mode")
            auto_scan_loop()

        elif c == "2":
            if not tracked_wallets:
                addr  = input("Wallet address: ").strip()
                label = input("Label (opsional): ").strip() or f"Wallet {addr[:8]}"
                if len(addr) >= 32:
                    tracked_wallets[addr] = label
                    save_wallets()
            print("\n🎯 Memulai track mode...\n")
            tg(f"🎯 <b>Bandar Tracker aktif</b> — Tracking {len(tracked_wallets)} wallets")
            track_loop()

        elif c == "3":
            print("\n🚀 Memulai full mode (scan + track)...\n")
            tg(f"🚀 <b>Bandar Tracker aktif</b> — Full mode ({len(tracked_wallets)} wallets + auto-scan)")
            t1 = threading.Thread(target=auto_scan_loop, daemon=True)
            t2 = threading.Thread(target=track_loop, daemon=True)
            t1.start(); t2.start()
            try:
                while True: time.sleep(60)
            except KeyboardInterrupt:
                print("\nStopped.")

        elif c == "4":
            print("\n⚡ Memulai WebSocket fast-scan...\n")
            tg("⚡ <b>Bandar Tracker aktif</b> — Real-time WS scan mode")
            ws_scan_loop()

        elif c == "5":
            print("\n🚀 Memulai WS fast-scan + track...\n")
            tg(f"🚀 <b>Bandar Tracker aktif</b> — WS fast-scan + track ({len(tracked_wallets)} wallets)")
            t1 = threading.Thread(target=ws_scan_loop, daemon=True)
            t2 = threading.Thread(target=track_loop, daemon=True)
            t1.start(); t2.start()
            try:
                while True: time.sleep(60)
            except KeyboardInterrupt:
                print("\nStopped.")

        elif c == "6":
            print("\n🧠 Memulai Auto-Discover Smart Money...\n")
            auto_discover_loop()

        elif c == "7":
            print("\n🚀 Memulai Auto-Discover + Track bersamaan...\n")
            tg(f"🚀 <b>Bandar Tracker v3 aktif</b> — Auto-Discover + Track ({len(tracked_wallets)} wallets)")
            t1 = threading.Thread(target=auto_discover_loop, daemon=True)
            t2 = threading.Thread(target=track_loop, daemon=True)
            t1.start(); t2.start()
            try:
                while True: time.sleep(60)
            except KeyboardInterrupt:
                print("\nStopped.")

        elif c == "a":
            addr  = input("Wallet address: ").strip()
            label = input("Label: ").strip() or f"Wallet {addr[:8]}"
            if len(addr) >= 32:
                tracked_wallets[addr] = label
                save_wallets()
                print(f"✅ Added: {addr[:12]}... ({label})")
            else:
                print("❌ Address tidak valid")

        elif c == "l":
            if not tracked_wallets:
                print("  (kosong)")
            for addr, lbl in tracked_wallets.items():
                label = lbl if isinstance(lbl, str) else lbl.get("label", "?")
                print(f"  {label:<20} {addr[:12]}...")

        elif c == "q":
            print("Bye!")
            break

        else:
            print("Pilihan tidak dikenal")

# ══════════════════════════════════════════════
# CI / SINGLE-RUN (untuk GitHub Actions)
# ══════════════════════════════════════════════

def ci_run(mode: str):
    log(f"CI mode — mode={mode}", "🤖 ")
    if mode == "2":
        track_once()
    elif mode == "6":
        auto_discover_once()
    elif mode == "7":
        auto_discover_once()
        track_once()
    else:
        auto_scan_once()
    save_seen()

def run_loop(mode: str):
    start_time   = time.time()
    max_duration = 19800  # 5.5 jam
    cycle_count  = 0
    current_mode = mode

    listen_tg_commands()
    save_mode(mode)

    tg(f"🤖 <b>Tracker aktif</b> — Mode {mode}, max {max_duration//3600}j")
    log(f"🔄 Continuous loop started — interval={SCAN_INTERVAL}s, max={max_duration//3600}j")
    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_duration:
            log(f"⏰ Timeout tercapai ({elapsed:.0f}s), {cycle_count} cycle")
            break

        new_mode = load_mode()
        if new_mode and new_mode != current_mode:
            current_mode = new_mode
            log(f"Mode changed to {current_mode} via Telegram", "📱 ")
            tg(f"📱 <b>Mode diubah ke {current_mode}</b>")

        cycle_count += 1
        cycle_start  = time.time()
        log(f"🔄 Cycle #{cycle_count} ({elapsed/3600:.2f}h elapsed)")

        if current_mode == "2":
            track_once()
        elif current_mode == "6":
            auto_discover_once()
        elif current_mode == "7":
            auto_discover_once()
            track_once()
        else:
            auto_scan_once()

        save_seen()
        cycle_duration = time.time() - cycle_start
        log(f"Cycle #{cycle_count} selesai dalam {cycle_duration/60:.1f} menit")

        if time.time() - start_time >= max_duration:
            break
        time.sleep(SCAN_INTERVAL)

    save_seen()
    sys.exit(0)

# ══════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    load_wallets()
    load_seen()
    get_sol_price()

    ci_mode = "--ci" in sys.argv
    ci_env  = os.environ.get("CI", "").lower()
    mode    = None
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            mode = sys.argv[idx + 1]

    if ci_mode and mode:
        if ci_env == "false":
            run_loop(mode)
        else:
            tg(f"🤖 <b>Bandar Tracker CI</b> — Mode {mode}")
            try:
                ci_run(mode)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                log(f"CI error: {e}", "❌ ")
            save_seen()
            sys.exit(0)

    cli_wallets = [a for a in sys.argv[1:] if len(a) >= 32 and not a.startswith("--")]
    if cli_wallets:
        for addr in cli_wallets:
            tracked_wallets[addr] = tracked_wallets.get(addr, f"CLI {addr[:8]}")
        save_wallets()
        print_banner()
        print(f"\n🎯 Tracking {len(cli_wallets)} wallet dari CLI args\n")
        tg(f"🎯 <b>Bandar Tracker aktif</b> — Tracking {len(cli_wallets)} wallets")
        track_loop()
    else:
        interactive_menu()
