"""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA WALLET TRACKER             ║
║   Track wallet → notif BUY & SELL ke Telegram ║
╚══════════════════════════════════════════════╝

Hanya 1 mode: track wallet yang ada di wallets.json.
Setiap wallet BUY atau SELL token → kirim notif ke Telegram.

  python bandar_tracker.py            → track semua wallet di wallets.json
  python bandar_tracker.py <wallet>   → track wallet tambahan dari CLI

SETUP:
  pip install requests
  Isi .env (TG_TOKEN, TG_CHAT_ID, HELIUS_API_KEY)
"""

import json, time, sys, os, requests
from datetime import datetime

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

TRACK_INTERVAL   = 10

SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y68YB",
}

TOKEN_CACHE_TTL = 300

# ══════════════════════════════════════════════
# State global
# ══════════════════════════════════════════════
tracked_wallets = {}
seen_sigs       = set()
token_cache     = {}
_sol_price      = 150.0

DATA_FILE      = os.path.join(os.path.dirname(__file__), "wallets.json")
SEEN_SIGS_FILE = os.path.join(os.path.dirname(__file__), "seen_sigs.json")

# ══════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════

def esc(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
    except Exception as e:
        log(f"RPC error [{method}]: {e}", "⚠️ ")
        return None

def get_sol_price():
    global _sol_price
    try:
        r = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=4)
        _sol_price = float(r.json()["data"]["SOL"]["price"])
    except Exception as e:
        log(f"Gagal ambil harga SOL: {e}", "⚠️ ")
    return _sol_price

def load_wallets():
    global tracked_wallets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                saved = json.load(f)
            tracked_wallets.update(saved)
            log(f"Loaded {len(saved)} wallet dari wallets.json")
        except Exception as e:
            log(f"Gagal load wallets: {e}", "⚠️ ")

def save_wallets():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(tracked_wallets, f, indent=2)
    except Exception as e:
        log(f"Gagal save wallets: {e}", "⚠️ ")

def save_seen():
    try:
        with open(SEEN_SIGS_FILE, "w") as f:
            json.dump({"sigs": list(seen_sigs)[-5000:]}, f)
    except Exception as e:
        log(f"Gagal save seen_sigs: {e}", "⚠️ ")

def load_seen():
    global seen_sigs
    if os.path.exists(SEEN_SIGS_FILE):
        try:
            with open(SEEN_SIGS_FILE) as f:
                seen_sigs = set(json.load(f).get("sigs", []))
            log(f"Loaded {len(seen_sigs)} seen sigs")
        except Exception as e:
            log(f"Gagal load seen_sigs: {e}", "⚠️ ")

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
# TOKEN INFO
# ══════════════════════════════════════════════

def get_token_info(mint: str) -> dict:
    now = time.time()
    if mint in token_cache:
        cached_info, cached_ts = token_cache[mint]
        if now - cached_ts < TOKEN_CACHE_TTL:
            return cached_info

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
            "dex_url": pair.get("url", f"https://dexscreener.com/solana/{mint}"),
        }
    except Exception as e:
        log(f"Gagal ambil token info {mint[:8]}: {e}", "⚠️ ")
        info = {"name": mint[:8] + "...", "symbol": "???", "price": "?",
                "dex_url": f"https://dexscreener.com/solana/{mint}"}

    token_cache[mint] = (info, now)
    return info

# ══════════════════════════════════════════════
# TRACK MODE — deteksi BUY & SELL
# ══════════════════════════════════════════════

def poll_wallet(addr: str, label: str):
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": 10}]) or []
    for s in reversed(sigs):
        sig = s["signature"]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        tx = rpc("getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx:
            continue

        meta     = tx.get("meta") or {}
        pre_tok  = meta.get("preTokenBalances",  []) or []
        post_tok = meta.get("postTokenBalances", []) or []

        # Cari index wallet di accountKeys
        keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        wallet_indices = set()
        pre_sol = post_sol = 0
        pre_bals  = meta.get("preBalances",  []) or []
        post_bals = meta.get("postBalances", []) or []

        for i, k in enumerate(keys):
            a = k if isinstance(k, str) else k.get("pubkey", "")
            if a == addr:
                wallet_indices.add(i)
                pre_sol  = pre_bals[i]  if i < len(pre_bals)  else 0
                post_sol = post_bals[i] if i < len(post_bals) else 0
                break

        sol_spent = max(0, (pre_sol - post_sol) / 1e9)
        sol_recv  = max(0, (post_sol - pre_sol) / 1e9)

        # FIX: cek owner ATAU accountIndex match wallet
        def _build_map(balances):
            m = {}
            for p in balances:
                owner = p.get("owner", "")
                acc_idx = p.get("accountIndex", -1)
                mint = p.get("mint", "")
                if mint in SKIP_MINTS:
                    continue
                # match jika owner = wallet ATAU accountIndex ada di wallet indices
                if owner != addr and acc_idx not in wallet_indices:
                    continue
                amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                m[mint] = m.get(mint, 0) + amt
            return m

        pre_map  = _build_map(pre_tok)
        post_map = _build_map(post_tok)

        # skip kalau ga ada perubahan token sama sekali
        if not pre_map and not post_map:
            continue

        sol_price = _sol_price or get_sol_price()

        # ── DETECT BUY (token bertambah) ──
        for mint, post_amt in post_map.items():
            pre_amt = pre_map.get(mint, 0)
            if post_amt <= pre_amt:
                continue
            usd_val = sol_spent * sol_price
            info = get_token_info(mint)
            log(f"{label} BUY ${info['symbol']} {sol_spent:.3f}SOL (~${usd_val:,.0f})", "🟢 ")
            msg = (
                f"🟢 <b>BUY</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Wallet</b>: {esc(label)}\n"
                f"<code>{esc(addr)}</code>\n\n"
                f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                f"<b>Amount</b>: {sol_spent:.3f} SOL (~${usd_val:,.0f})\n"
                f"<b>Got</b>: {post_amt - pre_amt:,.4f} token\n"
                f"<b>Price</b>: ${esc(info['price'])}\n\n"
                f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
                f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>"
            )
            tg(msg)

        # ── DETECT SELL (token berkurang) ──
        for mint, pre_amt in pre_map.items():
            post_amt = post_map.get(mint, 0)
            if post_amt >= pre_amt:
                continue
            sold_amt = pre_amt - post_amt
            usd_val  = sol_recv * sol_price
            info = get_token_info(mint)
            log(f"{label} SELL {sold_amt:.4f} {info['symbol']} (~${usd_val:,.0f})", "🔴 ")
            msg = (
                f"🔴 <b>SELL</b>\n"
                f"━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Wallet</b>: {esc(label)}\n"
                f"<code>{esc(addr)}</code>\n\n"
                f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                f"<b>Sold</b>: {sold_amt:,.4f} token\n"
                f"<b>Got</b>: {sol_recv:.3f} SOL (~${usd_val:,.0f})\n"
                f"<b>Price</b>: ${esc(info['price'])}\n\n"
                f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
                f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>"
            )
            tg(msg)

def track_once():
    if not tracked_wallets:
        log("Tidak ada wallet yang di-track.", "⚠️ ")
        return
    log(f"🎯 Cek {len(tracked_wallets)} wallet...", "🎯 ")
    for addr, label in list(tracked_wallets.items()):
        try:
            poll_wallet(addr, label if isinstance(label, str) else label.get("label", addr[:8]))
        except Exception as e:
            log(f"Error polling {addr[:8]}: {e}", "❌ ")
    log("🎯 Selesai cek wallet", "🎯 ")

def track_loop():
    if not tracked_wallets:
        log("Tidak ada wallet di wallets.json. Isi dulu.", "⚠️ ")
        return
    log(f"Track mode: monitoring {len(tracked_wallets)} wallet", "🎯 ")
    log(f"Poll tiap {TRACK_INTERVAL}s")
    # seed hanya 1 tx terakhir biar ga miss tx baru
    for addr in tracked_wallets:
        sigs = rpc("getSignaturesForAddress", [addr, {"limit": 1}]) or []
        for s in sigs:
            seen_sigs.add(s["signature"])
    log(f"Seeded {len(seen_sigs)} signature lama (skip notif historis)")
    while True:
        track_once()
        save_seen()
        time.sleep(TRACK_INTERVAL)

# ══════════════════════════════════════════════
# CI / SINGLE-RUN (GitHub Actions) + continuous loop
# ══════════════════════════════════════════════

def ci_run():
    log("CI mode — track once", "🤖 ")
    track_once()
    save_seen()

def run_loop():
    start_time   = time.time()
    max_duration = 19800  # 5.5 jam
    cycle_count  = 0

    for addr in tracked_wallets:
        sigs = rpc("getSignaturesForAddress", [addr, {"limit": 1}]) or []
        for s in sigs:
            seen_sigs.add(s["signature"])

    tg(f"🤖 <b>Wallet Tracker aktif</b> — {len(tracked_wallets)} wallet, max {max_duration//3600}j")
    log(f"🔄 Continuous loop — interval={TRACK_INTERVAL}s, max={max_duration//3600}j")
    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_duration:
            log(f"⏰ Timeout ({elapsed:.0f}s), {cycle_count} cycle")
            break
        cycle_count += 1
        track_once()
        save_seen()
        if time.time() - start_time >= max_duration:
            break
        time.sleep(TRACK_INTERVAL)
    save_seen()
    sys.exit(0)

# ══════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    load_wallets()
    load_seen()
    get_sol_price()

    cli_wallets = [a for a in sys.argv[1:] if len(a) >= 32 and not a.startswith("--")]
    for addr in cli_wallets:
        tracked_wallets[addr] = tracked_wallets.get(addr, f"CLI {addr[:8]}")
    if cli_wallets:
        save_wallets()

    ci_mode = "--ci" in sys.argv
    ci_env  = os.environ.get("CI", "").lower()

    if ci_mode:
        if ci_env == "false":
            run_loop()
        else:
            tg(f"🤖 <b>Wallet Tracker CI</b> — {len(tracked_wallets)} wallet")
            try:
                ci_run()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                log(f"CI error: {e}", "❌ ")
            save_seen()
            sys.exit(0)

    tg(f"🎯 <b>Wallet Tracker aktif</b> — Tracking {len(tracked_wallets)} wallet")
    track_loop()
