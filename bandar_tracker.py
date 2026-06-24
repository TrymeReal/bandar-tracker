"""
╔══════════════════════════════════════════════╗
║        🐋 SOLANA WALLET TRACKER             ║
║   Track wallet → notif BUY & SELL ke Telegram ║
╚══════════════════════════════════════════════╝

Track wallet yang ada di wallets.json.
Setiap wallet BUY atau SELL token → kirim notif ke Telegram.

Fitur:
  • Deteksi BUY/SELL (bayar pakai SOL maupun USDC/USDT)
  • PnL realized per wallet per token saat SELL
  • Cluster alert 🚨 — 2+ wallet beli token sama dalam X menit
  • Info anti-rug di notif (MarketCap / Liquidity / Age)
  • Filter MIN_USD biar transaksi receh ga spam
  • Perintah Telegram: /add /remove /list /help

  python bandar_tracker.py            → track semua wallet di wallets.json
  python bandar_tracker.py <wallet>   → track wallet tambahan dari CLI

SETUP:
  pip install requests
  Isi .env (TG_TOKEN, TG_CHAT_ID, HELIUS_API_KEY)
  Opsional .env: MIN_USD, CLUSTER_WINDOW_MIN, CLUSTER_MIN_WALLET, TG_THREAD
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

def _cfg(key, default=""):
    return _env.get(key) or os.environ.get(key) or default

TELEGRAM_TOKEN   = _cfg("TG_TOKEN")
TELEGRAM_CHAT_ID = _cfg("TG_CHAT_ID")
TELEGRAM_THREAD  = int(_cfg("TG_THREAD", "0"))

HELIUS_API_KEY   = _cfg("HELIUS_API_KEY")

RPC_FALLBACK     = "https://api.mainnet-beta.solana.com"

TRACK_INTERVAL   = 10

# ── Filter & fitur (bisa di-override lewat .env) ──
MIN_USD            = float(_cfg("MIN_USD", "30"))          # skip notif transaksi < segini
CLUSTER_WINDOW_MIN = float(_cfg("CLUSTER_WINDOW_MIN", "30"))  # window cluster (menit)
CLUSTER_MIN_WALLET = int(_cfg("CLUSTER_MIN_WALLET", "2"))  # min wallet biar dianggap cluster

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC     = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT     = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS = {USDC, USDT}

# Token yang tidak dianggap sebagai "token yang dibeli/dijual"
# (SOL/stablecoin/LST — ini sisi pembayaran, bukan target)
SKIP_MINTS = {
    SOL_MINT, USDC, USDT,
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

positions       = {}   # {addr: {mint: {"usd_in": float, "tokens": float}}}
stats           = {}   # {addr: {"realized": float, "wins": int, "trades": int}}
backfilled      = set()# wallet yang cost-basis-nya sudah di-backfill dari history
recent_buys     = {}   # {mint: [[label, ts], ...]}   untuk deteksi cluster
cluster_alerted = {}   # {mint: ts}                    biar ga spam cluster
tg_offset       = 0    # offset getUpdates Telegram

DATA_FILE      = os.path.join(os.path.dirname(__file__), "wallets.json")
SEEN_SIGS_FILE = os.path.join(os.path.dirname(__file__), "seen_sigs.json")
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
CLUSTER_FILE   = os.path.join(os.path.dirname(__file__), "cluster.json")
TG_OFFSET_FILE = os.path.join(os.path.dirname(__file__), "tg_offset.json")

# ══════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════

def esc(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def log(msg, prefix=""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix}{msg}")

def fmt_usd(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1e9: return f"${n/1e9:.1f}B"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    if n >= 1e3: return f"${n/1e3:.1f}k"
    return f"${n:.0f}"

def fmt_age(created_ms):
    if not created_ms:
        return "?"
    try:
        secs = time.time() - float(created_ms) / 1000
        if secs < 0:        return "?"
        if secs < 3600:     return f"{int(secs // 60)}m"
        if secs < 86400:    return f"{int(secs // 3600)}j"
        return f"{int(secs // 86400)}h"
    except Exception:
        return "?"

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
    """Ambil harga SOL dgn beberapa fallback. Selalu return harga terakhir kalau semua gagal."""
    global _sol_price
    # 1) Jupiter Price API v3 (endpoint publik gratis, tanpa API key)
    try:
        r = requests.get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}", timeout=5)
        p = float(r.json()[SOL_MINT]["usdPrice"])
        if p > 0:
            _sol_price = p
            return _sol_price
    except Exception as e:
        log(f"Harga SOL (Jupiter) gagal: {e}", "⚠️ ")
    # 2) CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5
        )
        p = float(r.json()["solana"]["usd"])
        if p > 0:
            _sol_price = p
            return _sol_price
    except Exception as e:
        log(f"Harga SOL (CoinGecko) gagal: {e}", "⚠️ ")
    # 3) DexScreener
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}", timeout=5)
        p = float(r.json()["pairs"][0]["priceUsd"])
        if p > 0:
            _sol_price = p
            return _sol_price
    except Exception as e:
        log(f"Harga SOL (DexScreener) gagal: {e}", "⚠️ ")
    log(f"Semua sumber harga SOL gagal — pakai cache ${_sol_price:.2f}", "⚠️ ")
    return _sol_price

# ══════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════

def _save_json(path, data, what):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Gagal save {what}: {e}", "⚠️ ")

def _load_json(path, what, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"Gagal load {what}: {e}", "⚠️ ")
        return default

def load_wallets():
    global tracked_wallets
    saved = _load_json(DATA_FILE, "wallets", {})
    if saved:
        tracked_wallets.update(saved)
        log(f"Loaded {len(saved)} wallet dari wallets.json")

def save_wallets():
    _save_json(DATA_FILE, tracked_wallets, "wallets")

def save_seen():
    _save_json(SEEN_SIGS_FILE, {"sigs": list(seen_sigs)[-5000:]}, "seen_sigs")

def load_seen():
    global seen_sigs
    data = _load_json(SEEN_SIGS_FILE, "seen_sigs", {})
    seen_sigs = set(data.get("sigs", []))
    if seen_sigs:
        log(f"Loaded {len(seen_sigs)} seen sigs")

def save_state():
    _save_json(POSITIONS_FILE,
               {"positions": positions, "stats": stats, "backfilled": list(backfilled)},
               "positions")
    _save_json(CLUSTER_FILE,
               {"recent_buys": recent_buys, "cluster_alerted": cluster_alerted},
               "cluster")

def load_state():
    global positions, stats, backfilled, recent_buys, cluster_alerted
    d = _load_json(POSITIONS_FILE, "positions", {})
    positions  = d.get("positions", {})
    stats      = d.get("stats", {})
    backfilled = set(d.get("backfilled", []))
    c = _load_json(CLUSTER_FILE, "cluster", {})
    recent_buys     = c.get("recent_buys", {})
    cluster_alerted = c.get("cluster_alerted", {})
    if positions:
        log(f"Loaded posisi {sum(len(v) for v in positions.values())} token")

def save_tg_offset():
    _save_json(TG_OFFSET_FILE, {"offset": tg_offset}, "tg_offset")

def load_tg_offset():
    global tg_offset
    tg_offset = _load_json(TG_OFFSET_FILE, "tg_offset", {}).get("offset", 0)

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════

def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
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

def _label_of(w):
    l = tracked_wallets.get(w)
    if isinstance(l, str):
        return l
    if isinstance(l, dict):
        return l.get("label", w[:8])
    return w[:8]

def handle_command(text: str):
    parts = text.split()
    cmd  = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    if cmd in ("start", "help"):
        tg("🤖 <b>Perintah Wallet Tracker</b>\n"
           "/add &lt;wallet&gt; [nama] — tambah wallet\n"
           "/remove &lt;wallet|nama&gt; — hapus wallet\n"
           "/list — daftar wallet di-track\n"
           "/stats — leaderboard PnL per wallet")

    elif cmd == "add":
        if not args:
            tg("Format: <code>/add &lt;wallet&gt; [nama]</code>"); return
        w = args[0]
        if len(w) < 32:
            tg("❌ Alamat wallet tidak valid"); return
        label  = " ".join(args[1:]) or f"W {w[:4]}"
        is_new = w not in tracked_wallets
        tracked_wallets[w] = label
        save_wallets()
        # backfill cost-basis + seed seen_sigs (skip notif historis)
        try:
            backfill_wallet(w)
        except Exception as e:
            log(f"Backfill gagal {w[:8]}: {e}", "⚠️ ")
        tg(f"{'✅ Ditambahkan' if is_new else '✏️ Diperbarui'}: <b>{esc(label)}</b>\n"
           f"<code>{esc(w)}</code>")
        log(f"CMD add: {label} ({w[:8]})", "💬 ")

    elif cmd in ("remove", "rm", "del"):
        if not args:
            tg("Format: <code>/remove &lt;wallet|nama&gt;</code>"); return
        q = args[0]
        target = q if q in tracked_wallets else None
        if not target:
            for a in tracked_wallets:
                if _label_of(a).lower() == q.lower():
                    target = a; break
        if target:
            lbl = _label_of(target)
            tracked_wallets.pop(target)
            save_wallets()
            tg(f"🗑️ Dihapus: <b>{esc(lbl)}</b>")
            log(f"CMD remove: {lbl}", "💬 ")
        else:
            tg("❌ Wallet tidak ditemukan")

    elif cmd in ("list", "ls"):
        if not tracked_wallets:
            tg("📋 Belum ada wallet yang di-track."); return
        lines = ["📋 <b>Wallet di-track</b>:"]
        for a in tracked_wallets:
            lines.append(f"• <b>{esc(_label_of(a))}</b> — <code>{esc(a)}</code>")
        tg("\n".join(lines))

    elif cmd in ("stats", "pnl"):
        rows = [(a, s) for a, s in stats.items() if s.get("trades")]
        if not rows:
            tg("📊 Belum ada trade lengkap (beli+jual) yang kebaca bot."); return
        rows.sort(key=lambda x: x[1]["realized"], reverse=True)
        lines = ["📊 <b>Leaderboard PnL</b> <i>(trade yg kebaca bot)</i>"]
        total = 0.0
        for a, s in rows:
            total += s["realized"]
            wr   = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            sign = "🟢" if s["realized"] >= 0 else "🔴"
            lines.append(
                f"{sign} <b>{esc(_label_of(a))}</b>: ${s['realized']:+,.0f} "
                f"({s['trades']} trade, {wr:.0f}% win)"
            )
        lines.append(f"\n<b>Total</b>: ${total:+,.0f}")
        tg("\n".join(lines))

    else:
        tg(f"❓ Perintah tidak dikenal: <code>/{esc(cmd)}</code>\nKetik /help")

def poll_telegram_commands():
    """Cek pesan masuk → proses perintah /add /remove /list."""
    global tg_offset
    if not TELEGRAM_TOKEN:
        return
    try:
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if tg_offset:
            params["offset"] = tg_offset
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=15
        )
        data = r.json()
        if not data.get("ok"):
            return
        for upd in data.get("result", []):
            tg_offset = upd["update_id"] + 1
            msg  = upd.get("message") or {}
            chat = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            # hanya proses perintah dari chat yang dikonfigurasi
            if TELEGRAM_CHAT_ID and chat != str(TELEGRAM_CHAT_ID):
                continue
            handle_command(text)
        save_tg_offset()
    except Exception as e:
        log(f"getUpdates error: {e}", "⚠️ ")

def seed_tg_offset():
    """Buang backlog perintah lama saat pertama jalan (kalau belum ada offset tersimpan)."""
    global tg_offset
    if tg_offset or not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"timeout": 0}, timeout=10
        )
        res = r.json().get("result", [])
        if res:
            tg_offset = res[-1]["update_id"] + 1
            save_tg_offset()
    except Exception:
        pass

# ══════════════════════════════════════════════
# TOKEN INFO (+ info anti-rug)
# ══════════════════════════════════════════════

def get_token_info(mint: str) -> dict:
    now = time.time()
    if mint in token_cache:
        cached_info, cached_ts = token_cache[mint]
        if now - cached_ts < TOKEN_CACHE_TTL:
            return cached_info

    info = {
        "name":   mint[:8] + "...", "symbol": "???", "price": "?",
        "liq": None, "mcap": None, "age": "?",
        "dex_url": f"https://dexscreener.com/solana/{mint}",
    }
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5
        )
        pairs = r.json().get("pairs") or [{}]
        pair  = pairs[0]
        info.update({
            "name":    pair.get("baseToken", {}).get("name", info["name"]),
            "symbol":  pair.get("baseToken", {}).get("symbol", "???"),
            "price":   pair.get("priceUsd", "?"),
            "liq":     (pair.get("liquidity") or {}).get("usd"),
            "mcap":    pair.get("marketCap") or pair.get("fdv"),
            "age":     fmt_age(pair.get("pairCreatedAt")),
            "dex_url": pair.get("url", info["dex_url"]),
        })
    except Exception as e:
        log(f"Gagal ambil token info {mint[:8]}: {e}", "⚠️ ")

    token_cache[mint] = (info, now)
    return info

def meta_line(info: dict) -> str:
    """Baris 'MC: $.. | Liq: $.. | Age: ..' untuk notif."""
    bits = []
    if info.get("mcap"): bits.append(f"MC {fmt_usd(info['mcap'])}")
    if info.get("liq"):  bits.append(f"Liq {fmt_usd(info['liq'])}")
    if info.get("age") and info["age"] != "?": bits.append(f"Age {info['age']}")
    return " | ".join(bits)

# ══════════════════════════════════════════════
# PnL (cost-basis per wallet per token, dalam USD)
# ══════════════════════════════════════════════

def record_buy(addr, mint, usd, tokens):
    pos = positions.setdefault(addr, {}).setdefault(mint, {"usd_in": 0.0, "tokens": 0.0})
    pos["usd_in"] += max(0.0, usd)
    pos["tokens"] += max(0.0, tokens)

def record_sell(addr, mint, usd_recv, sold_tokens):
    """Update posisi, return dict PnL realized atau None kalau cost basis tak diketahui."""
    pos = positions.get(addr, {}).get(mint)
    if not pos or pos["tokens"] <= 0:
        return None
    frac = min(1.0, sold_tokens / pos["tokens"]) if pos["tokens"] else 1.0
    cost = pos["usd_in"] * frac
    pos["usd_in"]  -= cost
    pos["tokens"]  -= sold_tokens
    if pos["tokens"] < 1e-9:
        pos["tokens"] = 0.0
        pos["usd_in"] = 0.0
    if cost <= 0:
        return None
    pnl = usd_recv - cost
    st = stats.setdefault(addr, {"realized": 0.0, "wins": 0, "trades": 0})
    st["realized"] += pnl
    st["trades"]   += 1
    if pnl > 0:
        st["wins"] += 1
    return {"cost": cost, "pnl": pnl, "pct": pnl / cost * 100}

# ══════════════════════════════════════════════
# CLUSTER (bandar masuk bareng)
# ══════════════════════════════════════════════

def note_buy_for_cluster(mint, label):
    """Catat buy. Return list wallet kalau jadi cluster baru, else None."""
    now    = time.time()
    window = CLUSTER_WINDOW_MIN * 60
    lst = recent_buys.setdefault(mint, [])
    lst.append([label, now])
    recent_buys[mint] = [e for e in lst if now - e[1] <= window]  # prune

    distinct = sorted({e[0] for e in recent_buys[mint]})
    if len(distinct) >= CLUSTER_MIN_WALLET:
        last = cluster_alerted.get(mint, 0)
        if now - last > window:           # belum pernah alert / window sudah lewat
            cluster_alerted[mint] = now
            return distinct
    return None

# ══════════════════════════════════════════════
# TRACK MODE — deteksi BUY & SELL
# ══════════════════════════════════════════════

def parse_tx(tx, addr, sol_price):
    """Ekstrak perubahan token + nilai USD dari satu transaksi untuk `addr`.
    Return dict: changes {mint: delta}, usd_spent, usd_recv, sol_spent, sol_recv."""
    meta     = tx.get("meta") or {}
    pre_tok  = meta.get("preTokenBalances",  []) or []
    post_tok = meta.get("postTokenBalances", []) or []

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

    def _delta(balances, only=None, skip=None):
        m = {}
        for p in balances:
            owner   = p.get("owner", "")
            acc_idx = p.get("accountIndex", -1)
            mint    = p.get("mint", "")
            if only is not None and mint not in only:
                continue
            if skip is not None and mint in skip:
                continue
            if owner != addr and acc_idx not in wallet_indices:
                continue
            amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            m[mint] = m.get(mint, 0) + amt
        return m

    pre_map   = _delta(pre_tok,  skip=SKIP_MINTS)
    post_map  = _delta(post_tok, skip=SKIP_MINTS)
    pre_stab  = _delta(pre_tok,  only=STABLE_MINTS)
    post_stab = _delta(post_tok, only=STABLE_MINTS)

    all_stab     = set(pre_stab) | set(post_stab)
    stable_spent = sum(max(0, pre_stab.get(m, 0)  - post_stab.get(m, 0)) for m in all_stab)
    stable_recv  = sum(max(0, post_stab.get(m, 0) - pre_stab.get(m, 0))  for m in all_stab)

    changes = {}
    for m in set(pre_map) | set(post_map):
        d = post_map.get(m, 0) - pre_map.get(m, 0)
        if d != 0:
            changes[m] = d

    return {
        "changes":   changes,
        "sol_spent": sol_spent, "sol_recv": sol_recv,
        "usd_spent": sol_spent * sol_price + stable_spent,
        "usd_recv":  sol_recv  * sol_price + stable_recv,
    }

def backfill_wallet(addr, limit=100):
    """Baca history wallet → rekonstruksi cost-basis & PnL lama.
    Sekalian seed seen_sigs biar tx historis ga di-notif ulang."""
    if addr in backfilled:
        return
    sigs = rpc("getSignaturesForAddress", [addr, {"limit": limit}]) or []
    sol_price = _sol_price or 150.0
    for s in reversed(sigs):                       # urut lama → baru
        seen_sigs.add(s["signature"])
        if s.get("err"):
            continue
        tx = rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }])
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        p = parse_tx(tx, addr, sol_price)
        for mint, delta in p["changes"].items():
            if delta > 0:
                record_buy(addr, mint, p["usd_spent"], delta)
            else:
                record_sell(addr, mint, p["usd_recv"], -delta)
    backfilled.add(addr)
    n = len(positions.get(addr, {}))
    log(f"Backfill {_label_of(addr)}: {n} posisi dari {len(sigs)} tx", "📚 ")

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
        if not tx or (tx.get("meta") or {}).get("err"):
            continue

        sol_price = _sol_price or get_sol_price()
        p = parse_tx(tx, addr, sol_price)
        if not p["changes"]:
            continue
        sol_spent, sol_recv = p["sol_spent"], p["sol_recv"]
        usd_spent, usd_recv = p["usd_spent"], p["usd_recv"]

        def _spend_str(sol_amt, usd):
            if sol_amt * sol_price >= 0.01:
                return f"{sol_amt:.3f} SOL (~${usd:,.0f})"
            return f"${usd:,.0f} (stablecoin)"

        for mint, delta in p["changes"].items():
            # ── BUY (token bertambah) ──
            if delta > 0:
                got = delta
                record_buy(addr, mint, usd_spent, got)   # akuntansi selalu jalan
                if usd_spent < MIN_USD:                   # skip notif receh
                    continue

                info    = get_token_info(mint)
                cluster = note_buy_for_cluster(mint, label)
                metas   = meta_line(info)
                entry   = f"MC {fmt_usd(info['mcap'])} | harga ${esc(info['price'])}" \
                          if info.get("mcap") else f"harga ${esc(info['price'])}"
                log(f"{label} BUY ${info['symbol']} ~${usd_spent:,.0f}", "🟢 ")
                msg = (
                    f"🟢 <b>BUY</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n\n"
                    f"<b>Wallet</b>: {esc(label)}\n"
                    f"<code>{esc(addr)}</code>\n\n"
                    f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                    f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                    f"<b>Beli</b>: {_spend_str(sol_spent, usd_spent)}\n"
                    f"<b>Beli di</b>: {entry}\n"
                    f"<b>Got</b>: {got:,.4f} token\n"
                    + (f"{esc(metas)}\n" if metas else "")
                    + f"\n"
                    f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                    f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
                    f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>"
                )
                tg(msg)
                if cluster:
                    send_cluster_alert(mint, info, cluster)

            # ── SELL (token berkurang) ──
            else:
                sold_amt = -delta
                pnl      = record_sell(addr, mint, usd_recv, sold_amt)  # akuntansi selalu jalan
                if usd_recv < MIN_USD:
                    continue

                info = get_token_info(mint)
                if pnl:
                    sign    = "🟢" if pnl["pnl"] >= 0 else "🔴"
                    pnl_str = (
                        f"<b>Beli di</b>: ~${pnl['cost']:,.0f}  →  <b>Jual di</b>: ${usd_recv:,.0f}\n"
                        f"<b>PnL</b>: {sign} {pnl['pct']:+.0f}% (${pnl['pnl']:+,.0f})\n"
                    )
                else:
                    pnl_str = "<b>PnL</b>: ? (harga beli tak diketahui)\n"
                log(f"{label} SELL {sold_amt:.4f} {info['symbol']} ~${usd_recv:,.0f}", "🔴 ")
                msg = (
                    f"🔴 <b>SELL</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n\n"
                    f"<b>Wallet</b>: {esc(label)}\n"
                    f"<code>{esc(addr)}</code>\n\n"
                    f"<b>Token</b>: ${esc(info['symbol'])} ({esc(info['name'])})\n"
                    f"<b>CA</b>: <code>{esc(mint)}</code>\n"
                    f"<b>Sold</b>: {sold_amt:,.4f} token\n"
                    f"<b>Jual</b>: {_spend_str(sol_recv, usd_recv)}\n"
                    f"{pnl_str}"
                    f"\n"
                    f"<a href='https://solscan.io/tx/{esc(sig)}'>TX</a> | "
                    f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
                    f"<a href='https://gmgn.ai/sol/address/{esc(addr)}'>GMGN</a>"
                )
                tg(msg)

def send_cluster_alert(mint, info, wallets):
    log(f"CLUSTER {len(wallets)} wallet → ${info['symbol']}", "🚨 ")
    metas = meta_line(info)
    msg = (
        f"🚨🚨 <b>BANDAR MASUK BARENG</b> 🚨🚨\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{len(wallets)} wallet</b> beli ${esc(info['symbol'])} "
        f"({esc(info['name'])}) dalam {int(CLUSTER_WINDOW_MIN)} menit!\n\n"
        f"<b>Wallets</b>: {esc(', '.join(wallets))}\n"
        f"<b>CA</b>: <code>{esc(mint)}</code>\n"
        f"<b>Price</b>: ${esc(info['price'])}\n"
        + (f"{esc(metas)}\n" if metas else "")
        + f"\n"
        f"<a href='{esc(info['dex_url'])}'>DexScreener</a> | "
        f"<a href='https://gmgn.ai/sol/token/{esc(mint)}'>GMGN</a>"
    )
    tg(msg)

def track_once():
    if not tracked_wallets:
        log("Tidak ada wallet yang di-track.", "⚠️ ")
        return
    log(f"🎯 Cek {len(tracked_wallets)} wallet...", "🎯 ")
    for addr in list(tracked_wallets.keys()):
        try:
            poll_wallet(addr, _label_of(addr))
        except Exception as e:
            log(f"Error polling {addr[:8]}: {e}", "❌ ")
    log("🎯 Selesai cek wallet", "🎯 ")

def _seed_seen():
    """Backfill cost-basis tiap wallet dari history + seed seen_sigs (skip notif historis)."""
    for addr in tracked_wallets:
        try:
            backfill_wallet(addr)
        except Exception as e:
            log(f"Backfill gagal {addr[:8]}: {e}", "⚠️ ")

def track_loop():
    if not tracked_wallets:
        log("Tidak ada wallet di wallets.json. Isi dulu.", "⚠️ ")
        return
    log(f"Track mode: monitoring {len(tracked_wallets)} wallet", "🎯 ")
    log(f"Poll tiap {TRACK_INTERVAL}s | MIN_USD=${MIN_USD:.0f} | "
        f"cluster={CLUSTER_MIN_WALLET} wallet/{int(CLUSTER_WINDOW_MIN)}m")
    _seed_seen()
    seed_tg_offset()
    log(f"Seeded {len(seen_sigs)} signature lama (skip notif historis)")
    cycle = 0
    while True:
        cycle += 1
        if cycle % 30 == 1:           # refresh harga SOL ~tiap 5 menit
            get_sol_price()
        poll_telegram_commands()
        track_once()
        save_seen()
        save_state()
        time.sleep(TRACK_INTERVAL)

# ══════════════════════════════════════════════
# CI / SINGLE-RUN (GitHub Actions) + continuous loop
# ══════════════════════════════════════════════

def ci_run():
    log("CI mode — track once", "🤖 ")
    poll_telegram_commands()
    track_once()
    save_seen()
    save_state()

def run_loop():
    start_time   = time.time()
    max_duration = 19800  # 5.5 jam
    cycle_count  = 0

    _seed_seen()
    seed_tg_offset()

    tg(f"🤖 <b>Wallet Tracker aktif</b> — {len(tracked_wallets)} wallet, "
       f"max {max_duration//3600}j")
    log(f"🔄 Continuous loop — interval={TRACK_INTERVAL}s, max={max_duration//3600}j")
    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_duration:
            log(f"⏰ Timeout ({elapsed:.0f}s), {cycle_count} cycle")
            break
        cycle_count += 1
        if cycle_count % 30 == 1:
            get_sol_price()
        poll_telegram_commands()
        track_once()
        save_seen()
        save_state()
        if time.time() - start_time >= max_duration:
            break
        time.sleep(TRACK_INTERVAL)
    save_seen()
    save_state()
    sys.exit(0)

# ══════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    load_wallets()
    load_seen()
    load_state()
    load_tg_offset()
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
            save_state()
            sys.exit(0)

    tg(f"🎯 <b>Wallet Tracker aktif</b> — Tracking {len(tracked_wallets)} wallet")
    track_loop()
