"""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA BANDAR TRACKER v2          ║
║   Auto-scan token baru + Track whale wallet  ║
╚══════════════════════════════════════════════╝

MODE:
  python bandar_tracker.py           → auto-scan token baru, detect bandar
  python bandar_tracker.py <wallet>  → track wallet spesifik

SETUP:
  pip install requests websocket-client
  Isi config di bawah (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

OPTIONAL (lebih cepat):
  Daftar Helius gratis di helius.dev → isi HELIUS_API_KEY
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
RPC_FALLBACK     = "https://api.mainnet-beta.solana.com"

# Mode auto-scan
SCAN_INTERVAL    = 15                   # detik antar scan
MIN_LIQUIDITY    = 1000                 # min liquidity USD buat diproses
ALERT_SCORE      = 50                   # min score buat kirim alert (0-100)
MIN_BANDAR_SOL   = 0.5                  # min SOL yg dipake bandar (filter sniper kecil)

# Mode track wallet
TRACK_INTERVAL   = 10                   # detik antar poll
MIN_BUY_USD      = 5000                 # min buy dalam USD buat notif

# Mode auto-discover smart money
DISCOVER_INTERVAL   = 60               # detik antar discover cycle
DISCOVER_MIN_WINRATE = 0.60            # min win rate (60%) buat auto-add
DISCOVER_MIN_TOKENS  = 3               # min token yang pernah early-entry
DISCOVER_SCORE_MIN   = 60             # min score per token buat dihitung "win"
DISCOVER_MAX_WALLETS = 50              # max wallet yang di-track sekaligus
PUMPED_MIN_LIQ       = 5_000          # min liquidity token yang dianggap "pumped"
PUMPED_MIN_CHANGE    = 50.0           # min % price change buat dianggap pumped

# Wallet preset yang mau di-track (bisa diisi langsung atau via CLI)
PRESET_WALLETS   = {
    # "AaBbCc...": "nama label",
}

# Mint yang harus di-skip (bukan token biasa)
SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y68YB",  # stSOL
}
# ══════════════════════════════════════════════

# ── State global ──────────────────────────────
tracked_wallets  = dict(PRESET_WALLETS)
seen_sigs        = set()
seen_mints       = set()
token_cache      = {}
_sol_price       = 150.0
DATA_FILE        = os.path.join(os.path.dirname(__file__), "wallets.json")
SEEN_SIGS_FILE   = os.path.join(os.path.dirname(__file__), "seen_sigs.json")
SEEN_MINTS_FILE  = os.path.join(os.path.dirname(__file__), "seen_mints.json")
MODE_FILE        = os.path.join(os.path.dirname(__file__), "mode.json")

# State untuk auto-discover
wallet_history   = {}   # { wallet_addr: [{"mint":..., "score":..., "pumped":bool}, ...] }
discovered_set   = set()  # wallet yang sudah di-discover (hindari duplikat notif)

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
                    if not text.startswith("/mode"):
                        continue

                    parts = text.split()
                    if len(parts) != 2 or not parts[1].isdigit():
                        tg("❌ Format: <code>/mode &lt;angka&gt;</code> — contoh: <code>/mode 6</code>")
                        continue

                    new_mode = parts[1]
                    if new_mode not in ("1", "2", "3", "4", "5", "6", "7"):
                        tg(f"❌ Mode {new_mode} tidak dikenal. Pilih 1-7")
                        continue

                    save_mode(new_mode)
                    tg(f"✅ Mode diubah ke <b>{new_mode}</b> — akan aktif di cycle berikutnya")
                    log(f"Mode changed to {new_mode} via Telegram", "📱 ")
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
    """Return (age_seconds, label)"""
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
    """Cek dari mana wallet ini dapat SOL pertama kali"""
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 20}]) or []
    for s in sigs:
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        keys  = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        pre   = tx.get("meta", {}).get("preBalances", []) or []
        post  = tx.get("meta", {}).get("postBalances", []) or []
        for i, k in enumerate(keys):
            a = k if isinstance(k, str) else k.get("pubkey", "")
            if not a or a == addr:
                continue
            diff = (post[i] if i < len(post) else 0) - (pre[i] if i < len(pre) else 0)
            if diff < -1_000_000:  # kirim SOL ke wallet ini
                return {"funder": a, "amount": abs(diff) / 1e9}
    return {"funder": None, "amount": 0}

def score_wallet(addr: str, token_mint: str) -> dict:
    """
    Skor wallet 0–100 berdasarkan heuristics bandar:
    - Wallet age
    - Funding source
    - Early entry
    - Cluster behavior
    """
    score   = 0
    reasons = []

    # ── 1. Wallet age (max 35) ──
    age_sec, age_lbl = get_wallet_age(addr)
    if age_sec < 3_600:    score += 35; reasons.append(f"Wallet baru banget {age_lbl}")
    elif age_sec < 21_600: score += 25; reasons.append(f"Wallet {age_lbl}")
    elif age_sec < 86_400: score += 15; reasons.append(f"Wallet {age_lbl}")
    else: reasons.append(f"Wallet lama ({age_lbl})")

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

    # ── 3. Early entry — first buyer bonus (max 25) ──
    token_sigs = rpc("getSignaturesForAddress", [token_mint, {"limit": 30}]) or []
    wallet_sigs = rpc("getSignaturesForAddress", [addr, {"limit": 50}]) or []
    token_sig_set = {s["signature"] for s in token_sigs}
    wallet_sig_set = {s["signature"] for s in wallet_sigs}
    overlap = token_sig_set & wallet_sig_set

    if overlap:
        # Cari rank entry
        for rank, s in enumerate(token_sigs, 1):
            if s["signature"] in overlap:
                if rank == 1:   score += 25; reasons.append("🎯 TX PERTAMA di token ini!")
                elif rank <= 3: score += 20; reasons.append(f"Early buyer rank #{rank}")
                elif rank <= 10: score += 10; reasons.append(f"Early buyer rank #{rank}")
                break
    else:
        reasons.append("Bukan early buyer")

    # ── 4. Cluster check (max 10) ──
    # Cek wallet lain yang sering interaksi dengan wallet ini di token yang sama
    co_wallets = {}
    for s in (wallet_sigs or [])[:15]:
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
            "name":   pair.get("baseToken", {}).get("name", mint[:8] + "..."),
            "symbol": pair.get("baseToken", {}).get("symbol", "???"),
            "price":  pair.get("priceUsd", "?"),
            "liq":    float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "dex_url": pair.get("url", f"https://dexscreener.com/solana/{mint}"),
        }
    except:
        info = {"name": mint[:8]+"...", "symbol": "???", "price": "?", "liq": 0,
                "dex_url": f"https://dexscreener.com/solana/{mint}"}
    token_cache[mint] = info
    return info

def fetch_new_tokens() -> list[dict]:
    """Ambil token baru dari DexScreener"""
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
# AUTO-SCAN MODE — Deteksi bandar dari token baru
# ══════════════════════════════════════════════

def get_first_buyer(mint: str):
    """Return (wallet_addr, tx_sig, sol_spent) dari buyer pertama token"""
    sigs = rpc("getSignaturesForAddress", [mint, {"limit": 10}]) or []
    if not sigs:
        return None
    for s in reversed(sigs):
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        pre_bal = tx.get("meta", {}).get("preBalances", [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        meta = tx.get("meta") or {}
        pre  = meta.get("preTokenBalances")  or []
        post = meta.get("postTokenBalances") or []
        for p in post:
            if p.get("mint") != mint: continue
            amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            if amt <= 0: continue
            owner = p.get("owner", "")
            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre)
            if not had and owner:
                return owner, s["signature"], sol_spent
    keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys", [])
    for k in keys:
        a = k if isinstance(k, str) else k.get("pubkey", "")
        if a and a != mint:
            return a, sigs[-1]["signature"], sol_spent
    return None

def process_token(token: dict):
    mint   = token["mint"]
    sym    = token.get("symbol", "???")

    # Skip mint bukan token biasa
    if mint in SKIP_MINTS:
        return

    # Cek liquidity dulu
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
        bandar_sol = sol_spent
        if bandar_sol >= 50:     tier = "🐳 MEGA WHALE"
        elif bandar_sol >= 10:   tier = "🐋 WHALE"
        elif bandar_sol >= 1:    tier = "🐟 MINI WHALE"
        else:                    tier = "🎯 BANDAR"

        msg = (
            f"{tier}\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Token</b>: ${esc(sym)} ({esc(info['name'])})\n"
            f"<b>CA</b>: <code>{esc(mint)}</code>\n"
            f"<b>Liquidity</b>: ${info['liq']:,.0f}\n"
            f"<b>Price</b>: ${esc(info['price'])}\n"
            f"<b>Bandar spend</b>: {bandar_sol:.2f} SOL (${bandar_sol*_sol_price:,.0f})\n\n"
            f"<b>Wallet</b>: <code>{esc(buyer)}</code>\n"
            f"<b>Age</b>: {esc(analysis['age_lbl'])}\n"
            f"<b>Score</b>: {score}/100 | {esc(conf)}\n\n"
            f"{esc(r_str)}\n\n"
            f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
            f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
            f"<a href='https://solscan.io/account/{esc(buyer)}'>Wallet</a>"
        )
        log(f"  → Alert sent! Score {score}/100 | {bandar_sol:.2f}SOL", "✅ ")
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
    if mint in seen_mints:
        return
    if mint in SKIP_MINTS:
        return
    seen_mints.add(mint)

    if sol_spent < MIN_BANDAR_SOL:
        log(f"⏭️ Skip {mint[:8]}... bandar cuma {sol_spent:.3f} SOL", "⏭️ ")
        return

    log(f"⚡ Token baru via WS: {mint[:12]}... owner={owner[:8]}... spent={sol_spent:.2f}SOL", "⚡ ")
    time.sleep(2)
    info = get_token_info(mint)

    # Skip kalau belum migrasi ke DEX (liquidity < MIN_LIQUIDITY)
    if info["liq"] < MIN_LIQUIDITY:
        log(f"⏭️ Skip {mint[:8]}... belum migrasi (liq=${info['liq']:.0f})", "⏭️ ")
        return

    analysis = score_wallet(owner, mint)
    score = analysis["score"]
    conf = analysis["conf"]
    reasons = analysis["reasons"]
    log(f"  {owner[:8]}... score={score}/100 {conf}", "⚡ ")

    if score >= ALERT_SCORE:
        r_str = "\n".join(f"▸ {esc(r)}" for r in reasons[:4])
        liq_str = f"Liquidity: ${info['liq']:,.0f}" if info['liq'] > 0 else "🚀 Bonding curve"

        bsol = sol_spent
        if bsol >= 50:     tier = "🐳 MEGA WHALE"
        elif bsol >= 10:   tier = "🐋 WHALE"
        elif bsol >= 1:    tier = "🐟 MINI WHALE"
        else:              tier = "🎯 BANDAR"

        msg = (
            f"{tier} ⚡\n"
            f"━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
            f"<b>CA</b>: <code>{esc(mint)}</code>\n"
            f"{liq_str}\n"
            f"<b>Price</b>: ${esc(info['price'])}\n"
            f"<b>Bandar spend</b>: {bsol:.2f} SOL (${bsol*_sol_price:,.0f})\n\n"
            f"<b>Wallet</b>: <code>{esc(owner)}</code>\n"
            f"<b>Age</b>: {esc(analysis['age_lbl'])}\n"
            f"<b>Score</b>: {score}/100 | {esc(conf)}\n\n"
            f"{r_str}\n\n"
            f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
            f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
            f"<a href='https://solscan.io/account/{esc(owner)}'>Wallet</a>"
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
            val = data["params"]["result"]["value"]
            sig = val["signature"]
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

            pre_bal = tx.get("meta", {}).get("preBalances", [0]) or [0]
            post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
            sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

            pre_tok = tx.get("meta", {}).get("preTokenBalances", []) or []
            post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []

            for p in post_tok:
                mint = p.get("mint", "")
                owner = p.get("owner", "")
                amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                if not mint or len(mint) < 32 or amt <= 0:
                    continue
                if mint in SKIP_MINTS:
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
# TRACK MODE — Monitor wallet spesifik
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
        meta = tx.get("meta") or {}
        pre  = meta.get("preTokenBalances")  or []
        post = meta.get("postTokenBalances") or []

        # Cari token yang dibeli
        for p in post:
            amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            if amt <= 0: continue
            mint  = p.get("mint", "")
            owner = p.get("owner", "")
            if owner != addr: continue
            had = any(x.get("mint") == mint and x.get("owner") == addr for x in pre)
            if had: continue  # bukan beli baru

            # Hitung SOL yang dipakai
            pre_sol  = (meta.get("preBalances")  or [0])[0]
            post_sol = (meta.get("postBalances") or [0])[0]
            sol_spent = (pre_sol - post_sol) / 1e9
            usd_val   = sol_spent * get_sol_price()

            info = get_token_info(mint)
            log(f"{label} BUY ${info['symbol']} ({mint}) {sol_spent:.2f}SOL (~${usd_val:,.0f})", "🐋 ")

            if usd_val >= MIN_BUY_USD:
                msg = (
                    f"🐋 <b>WHALE BUY</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n\n"
                    f"<b>Wallet</b>: {esc(label)}\n"
                    f"<code>{esc(addr)}</code>\n\n"
                    f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                    f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                    f"<b>Amount</b>: {sol_spent:.2f} SOL (~${usd_val:,.0f})\n"
                    f"<b>Price</b>: ${esc(info['price'])}\n\n"
                    f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                    f"<a href='{esc(info['dex_url'])}'>DexScreener</a>"
                )
                tg(msg)
            break  # satu token per tx cukup

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
    """
    Ambil token Solana yang sudah pump dari DexScreener:
    - price change 24h tinggi
    - liquidity cukup
    """
    results = []
    try:
        # Coba endpoint boosted (token yang lagi trending)
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            timeout=8
        )
        boosts = r.json() or []
        for t in boosts:
            if t.get("chainId") != "solana":
                continue
            mint = t.get("tokenAddress", "")
            if mint and mint not in SKIP_MINTS:
                results.append({"mint": mint, "source": "boosted"})
    except:
        pass

    # Fallback: ambil dari token-profiles/latest
    if len(results) < 10:
        try:
            r = requests.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=8
            )
            tokens = r.json() or []
            for t in tokens:
                if t.get("chainId") != "solana":
                    continue
                mint = t.get("tokenAddress", "")
                if mint and mint not in SKIP_MINTS:
                    results.append({"mint": mint, "source": "latest"})
        except:
            pass

    # Deduplicate
    seen = set()
    out = []
    for t in results:
        if t["mint"] not in seen:
            seen.add(t["mint"])
            out.append(t)
    return out[:limit]


def get_early_buyers(mint: str, top_n: int = 5) -> list[tuple[str, float]]:
    """
    Return list of (wallet_addr, sol_spent) dari top-N early buyer token ini.
    Ambil dari signature paling awal di akun mint.
    """
    sigs = rpc("getSignaturesForAddress", [mint, {"limit": 20}]) or []
    if not sigs:
        return []

    buyers = []
    for s in reversed(sigs):   # reversed = dari yang paling lama
        if len(buyers) >= top_n:
            break
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue

        pre_bal  = tx.get("meta", {}).get("preBalances", [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []

        for p in post_tok:
            if p.get("mint") != mint:
                continue
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            owner = p.get("owner", "")
            if amt <= 0 or not owner:
                continue
            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
            if had:
                continue
            if sol_spent >= MIN_BANDAR_SOL:
                buyers.append((owner, sol_spent))
            break

    return buyers


def analyze_wallet_history(addr: str) -> dict:
    """
    Cek performa historis wallet ini di token lain:
    - Berapa kali dia early entry di token yang akhirnya pump?
    - Hitung win_rate = pumped / total_checked
    """
    # Ambil 50 tx terakhir wallet ini
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 50}]) or []
    if not sigs:
        return {"win_rate": 0, "wins": 0, "total": 0, "tokens": []}

    mints_bought = {}   # mint -> sol_spent

    for s in sigs:
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue
        pre_tok  = tx.get("meta", {}).get("preTokenBalances",  []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
        pre_bal  = tx.get("meta", {}).get("preBalances",  [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)

        for p in post_tok:
            mint  = p.get("mint", "")
            owner = p.get("owner", "")
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            if owner != addr or not mint or amt <= 0 or mint in SKIP_MINTS:
                continue
            had = any(x.get("mint") == mint and x.get("owner") == addr for x in pre_tok)
            if not had:
                mints_bought[mint] = sol_spent

    if not mints_bought:
        return {"win_rate": 0, "wins": 0, "total": 0, "tokens": []}

    # Check mana yang pumped
    wins = 0
    token_results = []
    for mint, sol in list(mints_bought.items())[:15]:    # max 15 buat hemat RPC
        info = get_token_info(mint)
        liq  = info.get("liq", 0)
        # Anggap "pumped" kalau liquidity cukup tinggi
        pumped = liq >= PUMPED_MIN_LIQ
        if pumped:
            wins += 1
        token_results.append({
            "mint":   mint,
            "symbol": info.get("symbol", "???"),
            "liq":    liq,
            "pumped": pumped,
            "sol":    sol,
        })

    total = len(token_results)
    win_rate = wins / total if total > 0 else 0

    return {
        "win_rate": win_rate,
        "wins":     wins,
        "total":    total,
        "tokens":   token_results,
    }


def discover_smart_wallets_from_token(token: dict) -> list[str]:
    """
    Dari satu token yang pumped, extract early buyer-nya,
    analisis historis mereka, dan return wallet yang layak di-track.
    """
    mint = token["mint"]
    info = get_token_info(mint)
    if info["liq"] < PUMPED_MIN_LIQ:
        return []

    log(f"🔎 Discover dari ${info['symbol']} ({mint[:8]}...) liq=${info['liq']:.0f}", "🧠 ")
    tg(f"🔎 Discover dari <b>${esc(info['symbol'])}</b> — liq ${info['liq']:,.0f}")
    buyers = get_early_buyers(mint, top_n=5)
    if not buyers:
        tg(f"⏭️ <b>${esc(info['symbol'])}</b> — tidak ada early buyer")
        return []

    tg(f"👥 <b>${esc(info['symbol'])}</b> — {len(buyers)} early buyer ditemukan, menganalisis...")
    good_wallets = []
    for i, (addr, sol_spent) in enumerate(buyers, 1):
        if addr in tracked_wallets or addr in discovered_set:
            continue
        if len(tracked_wallets) >= DISCOVER_MAX_WALLETS:
            log(f"Max wallet ({DISCOVER_MAX_WALLETS}) tercapai, skip discover", "⚠️ ")
            break

        log(f"   Analisis {addr[:10]}... (spent {sol_spent:.2f} SOL)", "🧠 ")
        tg(f"⏳ Analisis wallet {i}/{len(buyers)}: <code>{esc(addr[:10])}...</code> (spent {sol_spent:.2f} SOL)")
        hist = analyze_wallet_history(addr)
        wr   = hist["win_rate"]
        wins = hist["wins"]
        total = hist["total"]

        log(f"   → win_rate={wr:.0%} ({wins}/{total} tokens pumped)", "🧠 ")

        if total >= DISCOVER_MIN_TOKENS and wr >= DISCOVER_MIN_WINRATE:
            label = f"SmartMoney_{addr[:6]}"
            tracked_wallets[addr] = label
            discovered_set.add(addr)
            save_wallets()
            good_wallets.append(addr)

            # Kirim notif Telegram
            top_wins = [t for t in hist["tokens"] if t["pumped"]][:3]
            win_str  = "\n".join(
                f"  ✅ ${esc(t['symbol'])} — <code>{esc(t['mint'])}</code> — liq ${t['liq']:,.0f} (spent {t['sol']:.2f} SOL)"
                for t in top_wins
            )
            msg = (
                f"🧠 <b>SMART MONEY DISCOVERED</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Wallet</b>: <code>{esc(addr)}</code>\n"
                f"<b>Label</b>: {esc(label)}\n\n"
                f"<b>Win Rate</b>: {wr:.0%} ({wins}/{total} tokens pumped)\n"
                f"<b>Ditemukan dari</b>: ${esc(info['symbol'])} "
                f"(liq ${info['liq']:,.0f})\n"
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
        tg(f"📡 Auto-Discover: {len(pumped)} pumped token ditemukan")
        new_wallets_total = 0
        for i, token in enumerate(pumped, 1):
            if token["mint"] in seen_mints:
                continue
            seen_mints.add(token["mint"])
            log(f"🔎 Token {i}/{len(pumped)}: {token['mint'][:8]}...", "🧠 ")
            found = discover_smart_wallets_from_token(token)
            new_wallets_total += len(found)
        if new_wallets_total:
            log(f"🧠 Siklus selesai — {new_wallets_total} wallet baru ditambahkan", "🧠 ")
            tg(f"✅ <b>Discover selesai</b> — {new_wallets_total} wallet baru ditambahkan")
        else:
            log(f"🧠 Siklus selesai — tidak ada wallet baru", "🧠 ")
            tg(f"✅ <b>Discover selesai</b> — tidak ada wallet baru")
    except Exception as e:
        log(f"Error di discover: {e}", "❌ ")

def auto_discover_loop():
    """
    Loop utama auto-discover:
    1. Ambil token yang pumped dari DexScreener
    2. Extract early buyer-nya
    3. Analisis historis wallet
    4. Auto-add kalau win rate cukup tinggi
    """
    log("🧠 Auto-Discover Smart Money aktif", "🧠 ")
    log(f"   Min win rate: {DISCOVER_MIN_WINRATE:.0%} | Min tokens: {DISCOVER_MIN_TOKENS}")
    log(f"   Max wallets tracked: {DISCOVER_MAX_WALLETS}")
    tg(
        f"🧠 <b>Auto-Discover aktif</b>\n"
        f"Min win rate: {DISCOVER_MIN_WINRATE:.0%} | "
        f"Min tokens: {DISCOVER_MIN_TOKENS} | "
        f"Max wallet: {DISCOVER_MAX_WALLETS}"
    )
    while True:
        auto_discover_once()
        time.sleep(DISCOVER_INTERVAL)


# ══════════════════════════════════════════════
# INTERACTIVE MENU
# ══════════════════════════════════════════════

def print_banner():
    print("""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA BANDAR TRACKER v3          ║
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
# CI / SINGLE-RUN (untuk GitHub Actions & cron)
# ══════════════════════════════════════════════

def ci_run(mode: str):
    """Single pass — jalan satu siklus, simpan state, exit"""
    log(f"CI mode — mode={mode}", "🤖 ")
    if mode == "2":
        for addr in tracked_wallets:
            sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
            for s in sigs: seen_sigs.add(s["signature"])
        log(f"Seeded {len(seen_sigs)} existing signatures")
        track_once()
    elif mode == "6":
        auto_discover_once()
    elif mode == "7":
        for addr in tracked_wallets:
            sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
            for s in sigs: seen_sigs.add(s["signature"])
        log(f"Seeded {len(seen_sigs)} existing signatures")
        auto_discover_once()
        track_once()
    else:
        auto_scan_once()
    save_seen()


def run_loop(mode: str):
    """Continuous loop — jalan terus dengan interval, auto-stop setelah 5.5 jam"""
    start_time = time.time()
    max_duration = 19800
    cycle_count = 0
    current_mode = mode

    listen_tg_commands()
    save_mode(mode)

    log(f"🔄 Continuous loop started — interval={SCAN_INTERVAL}s, max_duration={max_duration}s ({max_duration//3600}j)")
    tg(f"🔄 <b>Bandar Tracker Continuous</b> — Mode {mode}, interval {SCAN_INTERVAL}s, max {max_duration//3600}j")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_duration:
            log(f"⏰ Timeout {max_duration}s tercapai ({elapsed:.0f}s), {cycle_count} cycle")
            tg(f"⏰ <b>Bandar Tracker selesai</b> — {cycle_count} cycle dalam {elapsed/3600:.1f} jam")
            break

        new_mode = load_mode()
        if new_mode and new_mode != current_mode:
            current_mode = new_mode
            log(f"Mode changed to {current_mode} via Telegram", "📱 ")
            tg(f"📱 <b>Mode diubah ke {current_mode}</b> — mulai cycle berikutnya")

        cycle_count += 1
        log(f"🔄 Cycle #{cycle_count} ({elapsed/3600:.2f}h elapsed)")
        cycle_start = time.time()
        tg(f"🔄 <b>Cycle #{cycle_count}</b> — {elapsed/3600:.1f}h elapsed, mode {current_mode}...")

        if current_mode == "2":
            for addr in tracked_wallets:
                sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
                for s in sigs: seen_sigs.add(s["signature"])
            track_once()
        elif current_mode == "6":
            auto_discover_once()
        elif current_mode == "7":
            for addr in tracked_wallets:
                sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
                for s in sigs: seen_sigs.add(s["signature"])
            auto_discover_once()
            track_once()
        else:
            auto_scan_once()

        save_seen()

        cycle_duration = time.time() - cycle_start
        tg(f"✅ <b>Cycle #{cycle_count}</b> selesai dalam {cycle_duration/60:.1f} menit")

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
    ci_env = os.environ.get("CI", "").lower()
    mode = None
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

    # CLI: python bandar_tracker.py <wallet1> <wallet2> ...
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
