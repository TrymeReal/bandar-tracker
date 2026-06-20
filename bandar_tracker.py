"""
╔══════════════════════════════════════════════╗
║  🐋 SOLANA BANDAR TRACKER v3 (async)        ║
║  Auto-scan token baru + Track whale wallet  ║
╚══════════════════════════════════════════════╝

MODE:
  python bandar_tracker.py          → auto-scan token baru, detect bandar
  python bandar_tracker.py <wallet> → track wallet spesifik

SETUP:
  pip install httpx websockets

  Buat file .env di folder yang sama:
    TG_TOKEN=...
    TG_CHAT_ID=...
    TG_THREAD=0          (opsional, untuk topik grup)
    HELIUS_API_KEY=...   (opsional, tapi direkomendasikan)
"""

import asyncio
import json
import os
import sys
import html
from datetime import datetime, timezone, timedelta

import httpx
import websockets

# ══════════════════════════════════════════════
# TELEGRAM NOTIF MODULE (inline dari telegram_notif.py)
# ══════════════════════════════════════════════

WIB = timezone(timedelta(hours=7))

_msg_queue: asyncio.Queue | None = None
_queue_task = None


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def score_label(score: int) -> str:
    if score >= 85:   return "🔴 <b>VERY HIGH</b>"
    elif score >= 70: return "🟠 <b>HIGH</b>"
    elif score >= 55: return "🟡 <b>MEDIUM</b>"
    else:             return "⚪ <b>LOW</b>"


def fmt_usd(val: float) -> str:
    if val >= 1_000_000: return f"${val/1_000_000:.2f}M"
    elif val >= 1_000:   return f"${val/1_000:.1f}K"
    return f"${val:.2f}"


def fmt_sol(val: float) -> str:
    return f"{val:.3f} SOL"


def short_addr(addr: str, n: int = 4) -> str:
    if len(addr) <= n * 2 + 3:
        return addr
    return f"{addr[:n]}...{addr[-n:]}"


def build_bandar_alert(
    token_name: str,
    mint: str,
    score: int,
    liquidity_usd: float,
    market_cap_usd: float,
    first_buy_sol: float,
    wallet: str,
    cluster_count: int,
    is_early_entry: bool,
    extra_notes: str = "",
) -> str:
    now_wib     = datetime.now(WIB).strftime("%H:%M:%S WIB")
    short_mint  = short_addr(mint, 6)
    short_wall  = short_addr(wallet, 6)

    if score >= 85:   header = "🚨 <b>BANDAR ALERT — VERY HIGH CONFIDENCE</b>"
    elif score >= 70: header = "⚠️ <b>BANDAR ALERT — HIGH CONFIDENCE</b>"
    else:             header = "👀 <b>BANDAR DETECTED — MEDIUM CONFIDENCE</b>"

    early_tag = "✅ Ya" if is_early_entry else "❌ Tidak"

    lines = [
        header, "",
        f"🪙 Token: <b>${esc(token_name)}</b>",
        f"📍 Mint: <code>{esc(mint)}</code>",
        "",
        f"🎯 Score: <b>{score}/100</b> — {score_label(score)}",
        "",
        f"💰 Likuiditas : <b>{fmt_usd(liquidity_usd)}</b>",
        f"📊 Market Cap : <b>{fmt_usd(market_cap_usd)}</b>",
        f"🛒 First Buy  : <b>{fmt_sol(first_buy_sol)}</b>",
        f"⚡ Early Entry: {early_tag}",
        f"👥 Cluster    : <b>{cluster_count} wallet terkait</b>",
    ]
    if extra_notes:
        lines += ["", f"📝 {esc(extra_notes)}"]

    lines += [
        "",
        f"👛 Wallet: <a href=\"https://solscan.io/account/{esc(wallet)}\">{esc(short_wall)}</a>",
        "",
        f"🔗 <a href=\"https://dexscreener.com/solana/{esc(mint)}\">Dexscreener</a>  |  "
        f"<a href=\"https://pump.fun/{esc(mint)}\">Pump.fun</a>  |  "
        f"<a href=\"https://birdeye.so/token/{esc(mint)}?chain=solana\">Birdeye</a>",
        "",
        f"⏱ Detected: {now_wib}",
    ]
    return "\n".join(lines)


def build_track_update(
    token_name: str,
    mint: str,
    wallet: str,
    action: str,
    sol_amount: float,
    price_change_pct: float | None = None,
    current_liq_usd: float | None = None,
) -> str:
    now_wib     = datetime.now(WIB).strftime("%H:%M:%S WIB")
    short_wall  = short_addr(wallet, 6)

    action_emoji = {"BUY": "🟢", "SELL": "🔴", "ADD_LIQ": "💧", "REM_LIQ": "🚰"}.get(
        action.upper(), "⚡"
    )
    lines = [
        f"{action_emoji} <b>WALLET MOVE — {esc(action.upper())}</b>", "",
        f"🪙 Token: <b>${esc(token_name)}</b>",
        f"👛 Wallet: <a href=\"https://solscan.io/account/{esc(wallet)}\">{esc(short_wall)}</a>",
        "",
        f"💸 Jumlah: <b>{fmt_sol(sol_amount)}</b>",
    ]
    if price_change_pct is not None:
        arrow = "📈" if price_change_pct >= 0 else "📉"
        lines.append(f"{arrow} Harga: <b>{price_change_pct:+.1f}%</b>")
    if current_liq_usd is not None:
        lines.append(f"💰 Liq sekarang: <b>{fmt_usd(current_liq_usd)}</b>")
    lines += [
        "",
        f"🔗 <a href=\"https://dexscreener.com/solana/{esc(mint)}\">Chart</a>",
        f"⏱ {now_wib}",
    ]
    return "\n".join(lines)


def build_smart_money_alert(
    wallet: str,
    label: str,
    win_rate: float,
    wins: int,
    total: int,
    found_from_symbol: str,
    found_from_liq: float,
    sol_spent: float,
    top_wins: list[dict],
) -> str:
    now_wib    = datetime.now(WIB).strftime("%H:%M:%S WIB")
    short_wall = short_addr(wallet, 6)
    win_str = "\n".join(
        f"  ✅ ${esc(t['symbol'])} — liq {fmt_usd(t['liq'])} (spent {fmt_sol(t['sol'])})"
        for t in top_wins[:3]
    )
    lines = [
        "🧠 <b>SMART MONEY DISCOVERED</b>",
        "━━━━━━━━━━━━━━━━━", "",
        f"👛 Wallet: <a href=\"https://solscan.io/account/{esc(wallet)}\">{esc(short_wall)}</a>",
        f"🏷 Label: <b>{esc(label)}</b>", "",
        f"🏆 Win Rate: <b>{win_rate:.0%}</b> ({wins}/{total} tokens pumped)",
        f"🔍 Ditemukan dari: <b>${esc(found_from_symbol)}</b> (liq {fmt_usd(found_from_liq)})",
        f"💸 SOL dipakai: <b>{fmt_sol(sol_spent)}</b>", "",
        f"<b>Token yang pumped:</b>\n{win_str}", "",
        f"<a href=\"https://solscan.io/account/{esc(wallet)}\">Solscan</a> | "
        f"<a href=\"https://gmgn.ai/sol/address/{esc(wallet)}\">GMGN</a>", "",
        f"✅ <i>Wallet otomatis di-track!</i>",
        f"⏱ {now_wib}",
    ]
    return "\n".join(lines)


async def _queue_worker(bot_token: str, chat_id: str, thread_id: int = 0):
    """Kirim pesan 1 per 1 dengan flood-control + retry."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            text = await _msg_queue.get()
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if thread_id:
                payload["message_thread_id"] = thread_id

            for attempt in range(3):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                        log(f"TG rate-limit, wait {retry_after}s", "⚠️ ")
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    break
                except Exception as e:
                    if attempt == 2:
                        log(f"TG gagal send 3x: {e}", "❌ ")
                    else:
                        await asyncio.sleep(2 ** attempt)

            _msg_queue.task_done()
            await asyncio.sleep(1.2)


def init_notif(bot_token: str, chat_id: str, thread_id: int = 0):
    """Inisialisasi queue + jalankan worker. Panggil SEKALI di startup."""
    global _msg_queue, _queue_task
    _msg_queue   = asyncio.Queue()
    loop         = asyncio.get_event_loop()
    _queue_task  = loop.create_task(_queue_worker(bot_token, chat_id, thread_id))
    log("Notification queue initialized.", "[TG] ")


async def tg(text: str):
    """Kirim pesan ke Telegram (async, non-blocking via queue)."""
    if _msg_queue is None:
        log("init_notif() belum dipanggil!", "❌ ")
        return
    await _msg_queue.put(text)


# ══════════════════════════════════════════════
# CONFIG — Baca dari .env
# ══════════════════════════════════════════════

def _load_env() -> dict:
    env = {}
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

_env = _load_env()

def _cfg(key: str, default: str = "") -> str:
    return _env.get(key) or os.environ.get(key, default)

TELEGRAM_TOKEN   = _cfg("TG_TOKEN")
TELEGRAM_CHAT_ID = _cfg("TG_CHAT_ID")
TELEGRAM_THREAD  = int(_cfg("TG_THREAD", "0"))
HELIUS_API_KEY   = _cfg("HELIUS_API_KEY")
RPC_FALLBACK     = "https://api.mainnet-beta.solana.com"

# Mode auto-scan
SCAN_INTERVAL   = 15     # detik
MIN_LIQUIDITY   = 1000   # min liq USD
ALERT_SCORE     = 50     # min score buat kirim alert
MIN_BANDAR_SOL  = 0.5    # min SOL yang dipakai bandar

# Mode track wallet
TRACK_INTERVAL  = 10     # detik
MIN_BUY_USD     = 5000   # min buy USD buat notif

# Auto-discover
DISCOVER_INTERVAL    = 60
DISCOVER_MIN_WINRATE = 0.60
DISCOVER_MIN_TOKENS  = 3
DISCOVER_MAX_WALLETS = 50
PUMPED_MIN_LIQ       = 5_000

# Preset wallets {addr: label}
PRESET_WALLETS: dict[str, str] = {}

SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y68YB",
}

# ══════════════════════════════════════════════
# STATE GLOBAL
# ══════════════════════════════════════════════

tracked_wallets: dict[str, str] = dict(PRESET_WALLETS)
seen_sigs:  set[str] = set()
seen_mints: set[str] = set()
token_cache: dict    = {}
_sol_price           = 150.0

wallet_history: dict = {}
discovered_set: set  = set()

DATA_FILE       = os.path.join(os.path.dirname(__file__), "wallets.json")
SEEN_SIGS_FILE  = os.path.join(os.path.dirname(__file__), "seen_sigs.json")
SEEN_MINTS_FILE = os.path.join(os.path.dirname(__file__), "seen_mints.json")

# ══════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════

def log(msg: str, prefix: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix}{msg}")


def save_wallets():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(tracked_wallets, f, indent=2)
    except Exception:
        pass


def load_wallets():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                saved = json.load(f)
            tracked_wallets.update(saved)
            log(f"Loaded {len(saved)} saved wallets")
        except Exception:
            pass


def save_seen():
    try:
        with open(SEEN_SIGS_FILE, "w") as f:
            json.dump({"sigs": list(seen_sigs)[-5000:]}, f)
    except Exception:
        pass
    try:
        with open(SEEN_MINTS_FILE, "w") as f:
            json.dump({"mints": list(seen_mints)[-5000:]}, f)
    except Exception:
        pass


def load_seen():
    global seen_sigs, seen_mints
    if os.path.exists(SEEN_SIGS_FILE):
        try:
            with open(SEEN_SIGS_FILE) as f:
                seen_sigs = set(json.load(f).get("sigs", []))
            log(f"Loaded {len(seen_sigs)} seen sigs")
        except Exception:
            pass
    if os.path.exists(SEEN_MINTS_FILE):
        try:
            with open(SEEN_MINTS_FILE) as f:
                seen_mints = set(json.load(f).get("mints", []))
            log(f"Loaded {len(seen_mints)} seen mints")
        except Exception:
            pass

# ══════════════════════════════════════════════
# ASYNC HTTP / RPC
# ══════════════════════════════════════════════

def _rpc_endpoint() -> str:
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    return RPC_FALLBACK


async def rpc(method: str, params: list, client: httpx.AsyncClient) -> dict | list | None:
    try:
        r = await client.post(
            _rpc_endpoint(),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=10,
        )
        return r.json().get("result")
    except Exception:
        return None


async def get_sol_price(client: httpx.AsyncClient) -> float:
    global _sol_price
    try:
        r = await client.get("https://price.jup.ag/v4/price?ids=SOL", timeout=4)
        _sol_price = float(r.json()["data"]["SOL"]["price"])
    except Exception:
        pass
    return _sol_price


async def get_token_info(mint: str, client: httpx.AsyncClient) -> dict:
    if mint in token_cache:
        return token_cache[mint]
    try:
        r = await client.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5
        )
        pair = r.json().get("pairs", [{}])[0]
        info = {
            "name":    pair.get("baseToken", {}).get("name", mint[:8] + "..."),
            "symbol":  pair.get("baseToken", {}).get("symbol", "???"),
            "price":   pair.get("priceUsd", "?"),
            "liq":     float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "mc":      float(pair.get("marketCap", 0) or 0),
            "dex_url": pair.get("url", f"https://dexscreener.com/solana/{mint}"),
        }
    except Exception:
        info = {
            "name": mint[:8] + "...", "symbol": "???", "price": "?",
            "liq": 0, "mc": 0, "dex_url": f"https://dexscreener.com/solana/{mint}",
        }
    token_cache[mint] = info
    return info


async def fetch_new_tokens(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(
            "https://api.dexscreener.com/token-profiles/latest/v1", timeout=8
        )
        tokens = r.json() or []
        return [
            {"mint": t["tokenAddress"], "symbol": t.get("symbol", "???")}
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except Exception:
        return []


async def fetch_pumped_tokens(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
    results = []
    try:
        r = await client.get(
            "https://api.dexscreener.com/token-boosts/top/v1", timeout=8
        )
        for t in (r.json() or []):
            if t.get("chainId") == "solana":
                mint = t.get("tokenAddress", "")
                if mint and mint not in SKIP_MINTS:
                    results.append({"mint": mint})
    except Exception:
        pass
    if len(results) < 10:
        for t in await fetch_new_tokens(client):
            results.append(t)
    seen, out = set(), []
    for t in results:
        if t["mint"] not in seen:
            seen.add(t["mint"])
            out.append(t)
    return out[:limit]

# ══════════════════════════════════════════════
# HEURISTICS — Deteksi bandar (async)
# ══════════════════════════════════════════════

async def get_wallet_age(addr: str, client: httpx.AsyncClient) -> tuple[float, str]:
    sigs = await rpc("getSignaturesForAddress", [addr, {"limit": 1}], client) or []
    if not sigs:
        return 999_999, "Unknown"
    tx = await rpc("getTransaction", [sigs[-1]["signature"], {
        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
    }], client)
    if not tx:
        return 999_999, "Unknown"
    age = (datetime.now().timestamp()) - (tx.get("blockTime") or 0)
    h = age / 3600
    if h < 1:   return age, "BARU < 1 jam 🍼"
    if h < 6:   return age, f"< 6 jam"
    if h < 24:  return age, f"< 24 jam"
    return age, f"{h/24:.0f} hari"


async def get_funding_source(addr: str, client: httpx.AsyncClient) -> dict:
    sigs = await rpc("getSignaturesForAddress", [addr, {"limit": 20}], client) or []
    for s in sigs:
        tx = await rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue
        keys    = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        pre     = tx.get("meta", {}).get("preBalances", []) or []
        post    = tx.get("meta", {}).get("postBalances", []) or []
        for i, k in enumerate(keys):
            a    = k if isinstance(k, str) else k.get("pubkey", "")
            diff = (post[i] if i < len(post) else 0) - (pre[i] if i < len(pre) else 0)
            if a and a != addr and diff < -1_000_000:
                return {"funder": a, "amount": abs(diff) / 1e9}
    return {"funder": None, "amount": 0}


async def score_wallet(addr: str, token_mint: str, client: httpx.AsyncClient) -> dict:
    """Skor wallet 0–100. Jalankan semua sub-cek secara concurrent."""
    score   = 0
    reasons = []

    # Jalankan age + funding concurrent
    age_task     = asyncio.create_task(get_wallet_age(addr, client))
    funding_task = asyncio.create_task(get_funding_source(addr, client))
    tok_sigs_task = asyncio.create_task(
        rpc("getSignaturesForAddress", [token_mint, {"limit": 30}], client)
    )
    wall_sigs_task = asyncio.create_task(
        rpc("getSignaturesForAddress", [addr, {"limit": 50}], client)
    )

    age_sec, age_lbl = await age_task
    funding          = await funding_task
    token_sigs       = await tok_sigs_task or []
    wallet_sigs      = await wall_sigs_task or []

    # 1. Wallet age (max 35)
    if age_sec < 3_600:    score += 35; reasons.append(f"Wallet baru banget {age_lbl}")
    elif age_sec < 21_600: score += 25; reasons.append(f"Wallet {age_lbl}")
    elif age_sec < 86_400: score += 15; reasons.append(f"Wallet {age_lbl}")
    else:                  reasons.append(f"Wallet lama ({age_lbl})")

    # 2. Funding source (max 30)
    if funding["funder"]:
        score += 15
        reasons.append(f"Dana dari {short_addr(funding['funder'], 6)} ({funding['amount']:.2f} SOL)")
        funder_age, _ = await get_wallet_age(funding["funder"], client)
        if funder_age < 86_400:
            score += 15
            reasons.append("Funder juga wallet baru ⚠️")
    else:
        reasons.append("Funding source tidak jelas")

    # 3. Early entry (max 25)
    tok_sig_set  = {s["signature"] for s in token_sigs}
    wall_sig_set = {s["signature"] for s in wallet_sigs}
    overlap      = tok_sig_set & wall_sig_set
    if overlap:
        for rank, s in enumerate(token_sigs, 1):
            if s["signature"] in overlap:
                if rank == 1:   score += 25; reasons.append("🎯 TX PERTAMA di token ini!")
                elif rank <= 3: score += 20; reasons.append(f"Early buyer rank #{rank}")
                elif rank <= 10:score += 10; reasons.append(f"Early buyer rank #{rank}")
                break
    else:
        reasons.append("Bukan early buyer")

    # 4. Cluster (max 10)
    co_wallets: dict[str, int] = {}
    for s in wallet_sigs[:15]:
        tx = await rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue
        keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        for k in keys:
            a = k if isinstance(k, str) else k.get("pubkey", "")
            if a and a != addr:
                co_wallets[a] = co_wallets.get(a, 0) + 1
    cluster = [k for k, v in co_wallets.items() if v >= 3]
    if len(cluster) >= 5:   score += 10; reasons.append(f"Cluster {len(cluster)} wallets 🕸️")
    elif len(cluster) >= 2: score += 5;  reasons.append(f"Small cluster ({len(cluster)} wallets)")

    score = max(0, min(100, score))
    conf  = "TINGGI 🔴" if score >= 70 else ("SEDANG 🟡" if score >= 40 else "RENDAH 🟢")
    is_early = any("TX PERTAMA" in r or "Early buyer" in r for r in reasons)

    return {
        "score": score, "conf": conf, "reasons": reasons,
        "age_lbl": age_lbl, "funding": funding,
        "cluster_count": len(cluster), "is_early": is_early,
    }

# ══════════════════════════════════════════════
# AUTO-SCAN MODE
# ══════════════════════════════════════════════

async def get_first_buyer(mint: str, client: httpx.AsyncClient):
    sigs = await rpc("getSignaturesForAddress", [mint, {"limit": 10}], client) or []
    for s in reversed(sigs):
        tx = await rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue
        pre  = tx.get("meta", {}).get("preBalances", [0]) or [0]
        post = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent  = max(0, (pre[0] - post[0]) / 1e9)
        pre_tok    = tx.get("meta", {}).get("preTokenBalances", []) or []
        post_tok   = tx.get("meta", {}).get("postTokenBalances", []) or []

        for p in post_tok:
            if p.get("mint") != mint:
                continue
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            owner = p.get("owner", "")
            if amt <= 0 or not owner:
                continue
            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
            if not had:
                return owner, s["signature"], sol_spent
    return None


async def process_token(token: dict, client: httpx.AsyncClient):
    mint = token["mint"]
    if mint in SKIP_MINTS or mint in seen_mints:
        return

    info = await get_token_info(mint, client)
    if info["liq"] < MIN_LIQUIDITY:
        return

    log(f"${info['symbol']} ({mint[:8]}...) liq={fmt_usd(info['liq'])} — scanning...", "🔍 ")

    res = await get_first_buyer(mint, client)
    if not res:
        return
    buyer, sig, sol_spent = res

    if sol_spent < MIN_BANDAR_SOL:
        log(f"⏭ {buyer[:8]}... cuma {sol_spent:.3f} SOL (skip)")
        return

    analysis = await score_wallet(buyer, mint, client)
    score    = analysis["score"]

    log(f"  {buyer[:8]}... score={score}/100 spent={sol_spent:.2f}SOL | {analysis['reasons'][0] if analysis['reasons'] else ''}")

    if score >= ALERT_SCORE:
        extra = " | ".join(analysis["reasons"][1:3])
        msg = build_bandar_alert(
            token_name    = info["symbol"],
            mint          = mint,
            score         = score,
            liquidity_usd = info["liq"],
            market_cap_usd= info["mc"],
            first_buy_sol = sol_spent,
            wallet        = buyer,
            cluster_count = analysis["cluster_count"],
            is_early_entry= analysis["is_early"],
            extra_notes   = extra,
        )
        await tg(msg)
        log(f"  → Alert sent! score={score}/100 | {sol_spent:.2f}SOL", "✅ ")


async def auto_scan_loop(client: httpx.AsyncClient):
    log("Auto-scan mode aktif", "🔍 ")
    log(f"Min liq: {fmt_usd(MIN_LIQUIDITY)} | Alert score: {ALERT_SCORE}/100")
    while True:
        tokens    = await fetch_new_tokens(client)
        new_ones  = [t for t in tokens if t["mint"] not in seen_mints]
        if new_ones:
            log(f"{len(new_ones)} token baru ditemukan")
            tasks = [process_token(t, client) for t in new_ones]
            for t in new_ones:
                seen_mints.add(t["mint"])
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            log("Tidak ada token baru", "💤 ")
        await asyncio.sleep(SCAN_INTERVAL)

# ══════════════════════════════════════════════
# WEBSOCKET SCAN (real-time pump.fun)
# ══════════════════════════════════════════════

PUMPFUN_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def _handle_ws_token(sig: str, mint: str, owner: str, sol_spent: float, client: httpx.AsyncClient):
    if mint in seen_mints or mint in SKIP_MINTS:
        return
    seen_mints.add(mint)
    if sol_spent < MIN_BANDAR_SOL:
        return

    log(f"⚡ Token baru via WS: {mint[:12]}... owner={owner[:8]}... spent={sol_spent:.2f}SOL", "⚡ ")
    await asyncio.sleep(2)

    info = await get_token_info(mint, client)
    if info["liq"] < MIN_LIQUIDITY:
        return

    analysis = await score_wallet(owner, mint, client)
    score    = analysis["score"]
    if score >= ALERT_SCORE:
        extra = " | ".join(analysis["reasons"][1:3])
        msg = build_bandar_alert(
            token_name    = info["symbol"],
            mint          = mint,
            score         = score,
            liquidity_usd = info["liq"],
            market_cap_usd= info["mc"],
            first_buy_sol = sol_spent,
            wallet        = owner,
            cluster_count = analysis["cluster_count"],
            is_early_entry= analysis["is_early"],
            extra_notes   = extra,
        )
        await tg(msg)


async def ws_scan_loop(client: httpx.AsyncClient):
    if not HELIUS_API_KEY:
        log("HELIUS_API_KEY wajib buat WS mode!", "❌ ")
        return
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    log("⚡ WebSocket scan — real-time Pump.fun detector", "⚡ ")

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                sub = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [PUMPFUN_ID]}, {"commitment": "processed"}],
                })
                await ws.send(sub)
                log("🔌 WS connected!")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        if "params" not in data:
                            continue
                        val  = data["params"]["result"]["value"]
                        sig  = val["signature"]
                        logs = val.get("logs", [])
                        if val.get("err") or sig in seen_sigs:
                            continue
                        seen_sigs.add(sig)
                        if not any("reate" in l for l in logs):
                            continue

                        tx = await rpc("getTransaction", [sig, {
                            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
                        }], client)
                        if not tx:
                            continue

                        pre_bal  = tx.get("meta", {}).get("preBalances", [0]) or [0]
                        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
                        sol_spent = max(0, (pre_bal[0] - post_bal[0]) / 1e9)
                        pre_tok   = tx.get("meta", {}).get("preTokenBalances", []) or []
                        post_tok  = tx.get("meta", {}).get("postTokenBalances", []) or []

                        for p in post_tok:
                            mint  = p.get("mint", "")
                            owner = p.get("owner", "")
                            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                            if not mint or len(mint) < 32 or amt <= 0 or mint in SKIP_MINTS:
                                continue
                            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
                            if not had:
                                asyncio.create_task(
                                    _handle_ws_token(sig, mint, owner, sol_spent, client)
                                )
                    except Exception as e:
                        log(f"WS msg error: {e}", "❌ ")
        except Exception as e:
            log(f"WS error: {e}, retry 5s...", "❌ ")
            await asyncio.sleep(5)

# ══════════════════════════════════════════════
# TRACK MODE
# ══════════════════════════════════════════════

async def poll_wallet(addr: str, label: str, client: httpx.AsyncClient):
    sigs = await rpc("getSignaturesForAddress", [addr, {"limit": 10}], client) or []
    for s in sigs:
        sig = s["signature"]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        tx = await rpc("getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue

        meta    = tx.get("meta") or {}
        pre_tok = meta.get("preTokenBalances") or []
        post_tok= meta.get("postTokenBalances") or []

        for p in post_tok:
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            mint  = p.get("mint", "")
            owner = p.get("owner", "")
            if owner != addr or amt <= 0:
                continue
            had = any(x.get("mint") == mint and x.get("owner") == addr for x in pre_tok)
            if had:
                continue

            pre_sol   = (meta.get("preBalances") or [0])[0]
            post_sol  = (meta.get("postBalances") or [0])[0]
            sol_spent = (pre_sol - post_sol) / 1e9
            usd_val   = sol_spent * _sol_price

            info = await get_token_info(mint, client)
            log(f"{label} BUY ${info['symbol']} {sol_spent:.2f}SOL (~{fmt_usd(usd_val)})", "🐋 ")

            if usd_val >= MIN_BUY_USD:
                msg = build_track_update(
                    token_name      = info["symbol"],
                    mint            = mint,
                    wallet          = addr,
                    action          = "BUY",
                    sol_amount      = sol_spent,
                    current_liq_usd = info["liq"],
                )
                await tg(msg)
            break


async def track_loop(client: httpx.AsyncClient):
    if not tracked_wallets:
        log("Tidak ada wallet yang di-track.", "⚠️ ")
        return
    log(f"Track mode: monitoring {len(tracked_wallets)} wallets", "🎯 ")

    # Seed existing sigs dulu biar gak notif TX lama
    seed_tasks = [
        rpc("getSignaturesForAddress", [addr, {"limit": 10}], client)
        for addr in tracked_wallets
    ]
    all_sigs = await asyncio.gather(*seed_tasks)
    for sigs in all_sigs:
        for s in (sigs or []):
            seen_sigs.add(s["signature"])
    log(f"Seeded {len(seen_sigs)} existing signatures")

    while True:
        tasks = [
            poll_wallet(addr, label, client)
            for addr, label in list(tracked_wallets.items())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(TRACK_INTERVAL)

# ══════════════════════════════════════════════
# AUTO-DISCOVER SMART MONEY
# ══════════════════════════════════════════════

async def get_early_buyers(mint: str, top_n: int, client: httpx.AsyncClient) -> list[tuple[str, float]]:
    sigs   = await rpc("getSignaturesForAddress", [mint, {"limit": 20}], client) or []
    buyers = []
    for s in reversed(sigs):
        if len(buyers) >= top_n:
            break
        tx = await rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue
        pre_bal  = tx.get("meta", {}).get("preBalances", [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent= max(0, (pre_bal[0] - post_bal[0]) / 1e9)
        pre_tok  = tx.get("meta", {}).get("preTokenBalances", []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
        for p in post_tok:
            if p.get("mint") != mint:
                continue
            amt   = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            owner = p.get("owner", "")
            if amt <= 0 or not owner:
                continue
            had = any(x.get("mint") == mint and x.get("owner") == owner for x in pre_tok)
            if not had and sol_spent >= MIN_BANDAR_SOL:
                buyers.append((owner, sol_spent))
                break
    return buyers


async def analyze_wallet_history(addr: str, client: httpx.AsyncClient) -> dict:
    sigs = await rpc("getSignaturesForAddress", [addr, {"limit": 50}], client) or []
    mints_bought: dict[str, float] = {}

    for s in sigs:
        tx = await rpc("getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
        }], client)
        if not tx:
            continue
        pre_bal  = tx.get("meta", {}).get("preBalances", [0]) or [0]
        post_bal = tx.get("meta", {}).get("postBalances", [0]) or [0]
        sol_spent= max(0, (pre_bal[0] - post_bal[0]) / 1e9)
        pre_tok  = tx.get("meta", {}).get("preTokenBalances", []) or []
        post_tok = tx.get("meta", {}).get("postTokenBalances", []) or []
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

    # Check mana yang pumped — concurrent
    info_tasks = {
        mint: asyncio.create_task(get_token_info(mint, client))
        for mint in list(mints_bought.keys())[:15]
    }
    await asyncio.gather(*info_tasks.values())

    wins, token_results = 0, []
    for mint, sol in list(mints_bought.items())[:15]:
        info   = info_tasks[mint].result()
        pumped = info.get("liq", 0) >= PUMPED_MIN_LIQ
        if pumped:
            wins += 1
        token_results.append({
            "mint": mint, "symbol": info.get("symbol", "???"),
            "liq": info.get("liq", 0), "pumped": pumped, "sol": sol,
        })

    total    = len(token_results)
    win_rate = wins / total if total > 0 else 0
    return {"win_rate": win_rate, "wins": wins, "total": total, "tokens": token_results}


async def discover_from_token(token: dict, client: httpx.AsyncClient):
    mint = token["mint"]
    info = await get_token_info(mint, client)
    if info["liq"] < PUMPED_MIN_LIQ:
        return
    log(f"🔎 Discover dari ${info['symbol']} ({mint[:8]}...) liq={fmt_usd(info['liq'])}", "🧠 ")

    buyers = await get_early_buyers(mint, top_n=5, client=client)
    for addr, sol_spent in buyers:
        if addr in tracked_wallets or addr in discovered_set:
            continue
        if len(tracked_wallets) >= DISCOVER_MAX_WALLETS:
            log("Max wallet tercapai, skip", "⚠️ ")
            break

        hist = await analyze_wallet_history(addr, client)
        wr, wins, total = hist["win_rate"], hist["wins"], hist["total"]
        log(f"  {addr[:10]}... win={wr:.0%} ({wins}/{total})", "🧠 ")

        if total >= DISCOVER_MIN_TOKENS and wr >= DISCOVER_MIN_WINRATE:
            label = f"SmartMoney_{addr[:6]}"
            tracked_wallets[addr] = label
            discovered_set.add(addr)
            save_wallets()

            top_wins = [t for t in hist["tokens"] if t["pumped"]]
            msg = build_smart_money_alert(
                wallet            = addr,
                label             = label,
                win_rate          = wr,
                wins              = wins,
                total             = total,
                found_from_symbol = info["symbol"],
                found_from_liq    = info["liq"],
                sol_spent         = sol_spent,
                top_wins          = top_wins,
            )
            await tg(msg)
            log(f"  ✅ Auto-added: {label}", "🧠 ")


async def auto_discover_loop(client: httpx.AsyncClient):
    log("🧠 Auto-Discover Smart Money aktif", "🧠 ")
    log(f"  Min win rate: {DISCOVER_MIN_WINRATE:.0%} | Min tokens: {DISCOVER_MIN_TOKENS}")
    while True:
        pumped = await fetch_pumped_tokens(client)
        new    = [t for t in pumped if t["mint"] not in seen_mints]
        for t in new:
            seen_mints.add(t["mint"])
        if new:
            await asyncio.gather(
                *[discover_from_token(t, client) for t in new],
                return_exceptions=True,
            )
        await asyncio.sleep(DISCOVER_INTERVAL)

# ══════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════

BANNER = """
╔══════════════════════════════════════════════╗
║  🐋 SOLANA BANDAR TRACKER v3 (async)        ║
╚══════════════════════════════════════════════╝

Mode:
  [1] Auto-scan token baru
  [2] Track wallet tertentu
  [3] Auto-scan + Track bersamaan
  [4] ⚡ WebSocket real-time (butuh Helius)
  [5] ⚡ WebSocket + Track
  [6] 🧠 Auto-Discover Smart Money
  [7] 🧠 Auto-Discover + Track (full mode)
  [a] Add wallet ke tracker
  [l] List tracked wallets
  [q] Quit

CLI (untuk GitHub Actions / cron):
  python bandar_tracker.py --ci --mode 7
"""

# ══════════════════════════════════════════════
# CI MODE — satu siklus lalu exit (untuk cron)
# ══════════════════════════════════════════════

async def ci_run(mode: str, client: httpx.AsyncClient):
    """
    Jalankan SATU siklus (bukan loop) lalu return.
    Cocok untuk GitHub Actions yang jalan tiap 15 menit.
    """
    log(f"CI mode aktif — mode={mode}")

    if mode in ("1", "3"):
        tokens   = await fetch_new_tokens(client)
        new_ones = [t for t in tokens if t["mint"] not in seen_mints]
        log(f"{len(new_ones)} token baru")
        for t in new_ones:
            seen_mints.add(t["mint"])
        await asyncio.gather(
            *[process_token(t, client) for t in new_ones],
            return_exceptions=True,
        )

    if mode in ("2", "3", "5", "7"):
        if tracked_wallets:
            tasks = [
                poll_wallet(addr, label, client)
                for addr, label in list(tracked_wallets.items())
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            log("Tidak ada wallet di-track.", "⚠️ ")

    if mode in ("6", "7"):
        pumped = await fetch_pumped_tokens(client)
        new    = [t for t in pumped if t["mint"] not in seen_mints]
        for t in new:
            seen_mints.add(t["mint"])
        await asyncio.gather(
            *[discover_from_token(t, client) for t in new],
            return_exceptions=True,
        )

    save_seen()
    save_wallets()

    # Tunggu semua pesan Telegram terkirim sebelum exit
    if _msg_queue is not None:
        log("Menunggu Telegram queue kosong...")
        await _msg_queue.join()

    log("CI siklus selesai ✅")


# ══════════════════════════════════════════════
# INTERACTIVE MENU
# ══════════════════════════════════════════════

async def add_wallet_interactive():
    addr  = input("Wallet address: ").strip()
    label = input("Label (opsional): ").strip() or f"Wallet_{addr[:8]}"
    if len(addr) >= 32:
        tracked_wallets[addr] = label
        save_wallets()
        log(f"Wallet ditambahkan: {label}")
    else:
        log("Address tidak valid.", "❌ ")


async def run_mode(c: str, client: httpx.AsyncClient):
    """Jalankan mode tertentu dalam loop (untuk interactive/lokal)."""
    if c == "1":
        await tg("🔍 <b>Bandar Tracker aktif</b> — Auto-scan mode")
        await auto_scan_loop(client)
    elif c == "2":
        if not tracked_wallets:
            await add_wallet_interactive()
        await tg(f"🎯 <b>Bandar Tracker aktif</b> — Tracking {len(tracked_wallets)} wallets")
        await track_loop(client)
    elif c == "3":
        await tg(f"🚀 <b>Full mode</b> — {len(tracked_wallets)} wallets + auto-scan")
        await asyncio.gather(auto_scan_loop(client), track_loop(client))
    elif c == "4":
        await tg("⚡ <b>WebSocket scan aktif</b>")
        await ws_scan_loop(client)
    elif c == "5":
        await tg("⚡ <b>WebSocket + Track aktif</b>")
        await asyncio.gather(ws_scan_loop(client), track_loop(client))
    elif c == "6":
        await tg("🧠 <b>Auto-Discover aktif</b>")
        await auto_discover_loop(client)
    elif c == "7":
        await tg("🧠 <b>Full mode + Discover</b>")
        await asyncio.gather(auto_discover_loop(client), track_loop(client))
    else:
        print("Pilihan tidak dikenal.")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Solana Bandar Tracker")
    parser.add_argument("wallet",   nargs="?",       help="Wallet address (opsional, langsung track)")
    parser.add_argument("--ci",     action="store_true", help="CI mode: satu siklus lalu exit")
    parser.add_argument("--mode",   default="7",     help="Mode 1-7 (default: 7)")
    args = parser.parse_args()

    # Validasi config
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌  TG_TOKEN dan TG_CHAT_ID wajib diisi di .env atau environment variable!")
        sys.exit(1)

    load_wallets()
    load_seen()

    # Kalau ada wallet di CLI arg, tambahkan dulu
    if args.wallet and len(args.wallet) >= 32:
        addr  = args.wallet.strip()
        label = f"CLI_{addr[:8]}"
        tracked_wallets[addr] = label
        save_wallets()
        log(f"Wallet dari CLI ditambahkan: {label}")

    init_notif(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD)

    async with httpx.AsyncClient() as client:
        await get_sol_price(client)

        # ── CI MODE (GitHub Actions / cron) ──
        if args.ci:
            await ci_run(args.mode, client)
            return

        # ── INTERACTIVE MODE (lokal) ──
        print(BANNER)
        while True:
            c = input(">> ").strip().lower()
            if c == "q":
                save_seen()
                print("Bye!")
                break
            elif c == "l":
                if tracked_wallets:
                    for a, lbl in tracked_wallets.items():
                        print(f"  {lbl:30s}  {a}")
                else:
                    print("  (kosong)")
            elif c == "a":
                await add_wallet_interactive()
            else:
                await run_mode(c, client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        save_seen()
        print("\nStopped.")
