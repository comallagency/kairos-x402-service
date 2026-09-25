import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app import config
from app.config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    route TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,          -- unpaid | paid | error | capacity_reached | payment_failed
    latency_ms INTEGER,
    amount_usdc REAL,
    payer TEXT,
    user_agent TEXT,
    body_excerpt TEXT,
    error_reason TEXT,
    mpp_attempted INTEGER DEFAULT 0,
    network TEXT,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_route ON requests(route);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- queued | running | done | failed
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    input_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    steps_executed INTEGER DEFAULT 0,
    sources_read INTEGER DEFAULT 0,
    amount_usdc REAL,
    payer TEXT
);

-- La boite aux lettres de l'agent (app/handlers/contact.py). Ecrire est
-- gratuit et sans compte ; repondre demande le jeton du PC. `fingerprint` rend
-- l'identifiant deja attribue a un doublon exact plutot qu'une erreur : un
-- client qui reessaie apres un timeout n'est pas un spammeur.
CREATE TABLE IF NOT EXISTS contact_messages (
    id          TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    sender      TEXT NOT NULL,
    reply_to    TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT '',
    from_ip     TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    answered_at TEXT,
    answer      TEXT
);
CREATE INDEX IF NOT EXISTS idx_contact_received ON contact_messages(received_at);
CREATE INDEX IF NOT EXISTS idx_contact_unanswered ON contact_messages(answered_at, received_at);
CREATE INDEX IF NOT EXISTS idx_contact_fingerprint ON contact_messages(fingerprint, received_at);

-- CE QUE L'AGENT PUBLIE sous son propre nom (app/handlers/place.py).
-- Lire est gratuit et sans compte ; publier demande le jeton du PC.
-- `published_at` ne bouge jamais : c'est la seule colonne qui reponde a
-- « depuis quand est-ce public », et une colonne qu'on reecrit ne repond plus
-- a rien.
CREATE TABLE IF NOT EXISTS place (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_place_recent ON place(updated_at DESC);

CREATE TABLE IF NOT EXISTS quota_cache (
    key TEXT PRIMARY KEY,
    remaining INTEGER,
    limit_value INTEGER,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats(ts);

CREATE TABLE IF NOT EXISTS chain_payments (
    tx_hash TEXT PRIMARY KEY,
    from_address TEXT NOT NULL,
    amount_usdc REAL NOT NULL,
    block_number INTEGER,
    block_time TEXT,
    is_mechanical INTEGER DEFAULT 0,
    network TEXT
);
CREATE INDEX IF NOT EXISTS idx_chain_payments_block_time ON chain_payments(block_time);

CREATE TABLE IF NOT EXISTS chain_sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS seen_wallets (
    address TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mpp_nonces (
    nonce TEXT PRIMARY KEY,
    from_address TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

-- Usine (scripts/controleur.py, comptable.py, fossoyeur.py) - see usine/README.md
CREATE TABLE IF NOT EXISTS route_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    route TEXT NOT NULL,
    check_name TEXT NOT NULL,   -- discover | check | cdp_validate | probe_5methods | sample | header_size
    passed INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_route_checks_route_ts ON route_checks(route, ts);

-- Real-world visibility: is anything we publish actually indexed by the
-- catalogs agents actually query, not just "did we register" (see
-- BRIEF-CORRECTIONS.md 2026-09-06 - register succeeding is not the same as
-- being searchable, x402scan/mppscan registration and agentcash.dev's own
-- search index are entirely separate systems). One row per (catalog, query)
-- per run, gated to ~daily regardless of how often scripts/controleur.py
-- itself is invoked (loop cycles, manual dashboard clicks, the real nightly
-- cron all call the same function).
CREATE TABLE IF NOT EXISTS index_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    catalog TEXT NOT NULL,   -- agentcash | bazaar
    query TEXT NOT NULL,     -- the domain or keyword searched, or "_presence" for bazaar
    found INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_checks_ts ON index_checks(ts);

CREATE TABLE IF NOT EXISTS controleur_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    routes_tested INTEGER NOT NULL,
    routes_rejected INTEGER NOT NULL,
    duration_ms INTEGER,
    summary TEXT               -- one sentence, written by the script itself
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,       -- prospecteur | ouvrier | controleur | crieur | comptable | fossoyeur | operator
    action TEXT NOT NULL,      -- created | rejected | registered | killed | ...
    route TEXT,
    detail TEXT NOT NULL,
    trigger_kind TEXT DEFAULT 'cron'  -- cron | manual
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);

-- Bouton "Lancer" de /admin/usine (les 3 rôles mécaniques). Une ligne par
-- rôle (clé primaire = role), écrasée à chaque nouveau lancement : donne à
-- la fois "tourne-t-il en ce moment" (status) et "quand a-t-il été lancé
-- pour la dernière fois" (started_at, base du cooldown de 5 min). Pour
-- fossoyeur (hôte, hors conteneur), status='requested' est le marqueur que
-- le poller cron hôte ramasse - jamais d'exécution depuis le conteneur.
CREATE TABLE IF NOT EXISTS agent_manual_runs (
    role TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,      -- requested | running | done | error
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
);

-- Point d'arrêt manuel entre Prospecteur et Ouvrier : Prospecteur POSTe une
-- proposition ici (statut 'proposée'), l'opérateur l'approuve depuis
-- /admin/usine (bouton Approuver, statut -> 'approuvée'), Ouvrier ne
-- construit jamais que les entrées 'approuvée' puis les ferme ('traitée').
-- Vit dans la base (bind-mount data/), pas dans routes_registry.yaml : ce
-- dernier est baké dans l'image Docker au build, un conteneur en cours
-- d'exécution ne peut pas y persister d'écriture (voir docker-compose.yml,
-- seuls data/ et logs/ sont montés) - la même contrainte qui impose à
-- Fossoyeur de tourner hors conteneur s'applique ici.
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_at TEXT NOT NULL,
    intention TEXT NOT NULL,
    source TEXT,
    estimated_routes INTEGER,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'proposée',  -- proposée | approuvée | traitée | rejetée | bloquée_budget
    approved_at TEXT,
    closed_at TEXT
);

-- État du pipeline PC (usine/nightly_run.sh) - le tableau de bord tourne sur
-- le VPS et ne peut rien observer directement sur la machine de l'opérateur
-- (PC potentiellement éteint) ; ce n'est donc que ce que nightly_run.sh
-- rapporte lui-même à la fin de chaque run (une seule ligne, toujours
-- écrasée). Les runs par rôle (Prospecteur/Ouvrier/Crieur) restent
-- journalisés via la table `journal` existante (action='run',
-- trigger_kind='scheduled') - lue par db.last_agent_run(), déjà affichée
-- sur les cartes agent du dashboard.
CREATE TABLE IF NOT EXISTS pc_pipeline_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reported_at TEXT NOT NULL,
    result TEXT NOT NULL,      -- ok | partial | error
    detail TEXT,
    task_active INTEGER        -- 1 | 0 | NULL (état de la tâche planifiée Windows, inconnu si non rapporté)
);

-- Le VPS ne peut pas lancer un programme sur le PC de l'opérateur - c'est
-- l'inverse : usine/pc_listener.py sonde GET /admin/usine/pc-request toutes
-- les 30s et exécute lui-même ce qu'il y trouve. Une seule demande active
-- (requested|running) à la fois, tous rôles/pipeline confondus - Ouvrier et
-- Crieur tournent sur la même session PC et ne doivent jamais se chevaucher
-- (voir db.latest_pc_request() et le contrôle fait par l'endpoint avant
-- l'insertion).
CREATE TABLE IF NOT EXISTS pc_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,        -- pipeline | prospecteur | ouvrier | crieur | loop_start | loop_stop
    status TEXT NOT NULL,      -- requested | running | done | error | timeout
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    detail TEXT
);

-- Dernier contact de usine/pc_listener.py (une ligne, écrasée à chaque
-- sondage réussi) - la seule façon pour le tableau de bord de savoir si
-- l'écouteur tourne encore, puisque le PC peut être éteint sans prévenir.
CREATE TABLE IF NOT EXISTS pc_listener_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_at TEXT NOT NULL
);

-- Boucle continue (usine/loop_run.sh, 2026-09-06) : une ligne, écrasée à
-- chaque run. `status='running'` entre le démarrage et l'arrêt (manuel via
-- /admin/usine ou automatique sur dérive) ; `cycle_count`/`last_cycle_at`
-- avancent à chaque tour complet Prospecteur->Ouvrier->Contrôleur->Crieur->
-- Comptable->Fossoyeur ; `consecutive_controleur_rejections` est le compteur
-- qui déclenche l'arrêt automatique après 3 refus d'affilée (voir
-- app/admin.py::LOOP_MAX_CONSECUTIVE_REJECTIONS).
CREATE TABLE IF NOT EXISTS usine_loop_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'stopped',  -- stopped | running
    started_at TEXT,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    last_cycle_at TEXT,
    consecutive_controleur_rejections INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT
);

-- Réparation autonome (2026-09-06) : combien de fois de suite le Réparateur
-- a été déclenché sur cette route sans qu'elle repasse au vert. Remis à
-- zéro dès qu'un passage du Contrôleur redevient vert pour cette route -
-- seuls des échecs CONSÉCUTIFS comptent, pas un total historique. À 2, la
-- route est retirée mécaniquement (loop_run.sh, pas une décision d'agent).
-- Mesh agent board (app/handlers/agent_mesh.py) : nœuds, bounties, ledger public.
CREATE TABLE IF NOT EXISTS mesh_nodes (
    id TEXT PRIMARY KEY,
    registered_at TEXT NOT NULL,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    skills TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    from_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mesh_nodes_registered ON mesh_nodes(registered_at);

CREATE TABLE IF NOT EXISTS mesh_bounties (
    id TEXT PRIMARY KEY,
    posted_at TEXT NOT NULL,
    poster_name TEXT NOT NULL,
    poster_endpoint TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    criteria TEXT NOT NULL,
    reward_usdc REAL,
    acceptance_digest TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    claimed_at TEXT,
    claim_note TEXT,
    claimer_endpoint TEXT,
    fingerprint TEXT NOT NULL,
    from_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mesh_bounties_status ON mesh_bounties(status, posted_at);

CREATE TABLE IF NOT EXISTS mesh_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    from_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mesh_events_ts ON mesh_events(ts);

CREATE TABLE IF NOT EXISTS route_repair_attempts (
    slug TEXT PRIMARY KEY,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_result TEXT
);
"""


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


_init_lock = threading.Lock()
_initialized = False


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLEs for columns added after the initial deploy -
    CREATE TABLE IF NOT EXISTS never touches an already-existing table."""
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN mpp_attempted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # LA DECLARATION, sur la meme table que le message. Un agent qui se declare
    # n'ouvre pas un second canal : il ecrit sur celui-ci en disant, en plus, ce
    # qu il est. Deux tables en feraient deux interlocuteurs.
    #
    # `declares_endpoint` est la seule des trois qui soit verifiable : un nom se
    # donne, une liste de competences se declare, une URL se rappelle.
    for colonne in (
        "declares_what TEXT",
        "declares_endpoint TEXT",
        "declares_skills TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE contact_messages ADD COLUMN {colonne}")
        except sqlite3.OperationalError:
            pass  # column already exists

    try:
        conn.execute("ALTER TABLE journal ADD COLUMN trigger_kind TEXT DEFAULT 'cron'")
    except sqlite3.OperationalError:
        pass  # column already exists

    added_network = False
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN network TEXT")
        added_network = True
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE chain_payments ADD COLUMN network TEXT")
        added_network = True
    except sqlite3.OperationalError:
        pass
    if added_network:
        # All rows predating this column were logged before the 2026-09-05
        # mainnet switch - every one of them is Sepolia test traffic.
        conn.execute("UPDATE requests SET network='eip155:84532' WHERE network IS NULL")
        conn.execute("UPDATE chain_payments SET network='eip155:84532' WHERE network IS NULL")

    try:
        conn.execute("ALTER TABLE proposals ADD COLUMN demand_evidence TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        conn.execute("ALTER TABLE proposals ADD COLUMN block_reason TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        conn.execute("ALTER TABLE requests ADD COLUMN client_ip TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = _conn()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _initialized = True


@contextmanager
def cursor():
    init_db()
    conn = _conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_request(
    route: str,
    method: str,
    status: str,
    latency_ms: int | None = None,
    amount_usdc: float | None = None,
    payer: str | None = None,
    user_agent: str | None = None,
    body_excerpt: str | None = None,
    error_reason: str | None = None,
    mpp_attempted: bool = False,
    network: str | None = None,
    client_ip: str | None = None,
) -> None:
    if body_excerpt is not None:
        body_excerpt = body_excerpt[:2048]
    if client_ip is None:
        from app.client_ip import current_client_ip

        client_ip = current_client_ip()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO requests
               (ts, route, method, status, latency_ms, amount_usdc, payer, user_agent, body_excerpt, error_reason, mpp_attempted, network, client_ip)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_iso(),
                route,
                method,
                status,
                latency_ms,
                amount_usdc,
                payer,
                user_agent,
                body_excerpt,
                error_reason,
                int(mpp_attempted),
                network or config.X402_NETWORK,
                client_ip,
            ),
        )


def count_paid_today(route: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE route=? AND status='paid' AND ts LIKE ?",
            (route, f"{today}%"),
        )
        return cur.fetchone()["n"]


def create_job(input_data: dict, amount_usdc: float | None = None, payer: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    with cursor() as cur:
        cur.execute(
            """INSERT INTO jobs (id, status, created_at, input_json, amount_usdc, payer)
               VALUES (?, 'queued', ?, ?, ?, ?)""",
            (job_id, now_iso(), json.dumps(input_data), amount_usdc, payer),
        )
    return job_id


def queue_position(job_id: str) -> int:
    """Number of jobs queued/running strictly before this one (0-indexed position)."""
    with cursor() as cur:
        cur.execute(
            """SELECT created_at FROM jobs WHERE id=?""",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        created_at = row["created_at"]
        cur.execute(
            """SELECT COUNT(*) AS n FROM jobs
               WHERE status IN ('queued','running') AND created_at < ?""",
            (created_at,),
        )
        return cur.fetchone()["n"]


def get_job(job_id: str) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def next_queued_job() -> dict | None:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


def mark_job_running(job_id: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (now_iso(), job_id),
        )


def finish_job(job_id: str, result: dict, steps_executed: int, sources_read: int) -> None:
    with cursor() as cur:
        cur.execute(
            """UPDATE jobs SET status='done', finished_at=?, result_json=?,
               steps_executed=?, sources_read=? WHERE id=?""",
            (now_iso(), json.dumps(result), steps_executed, sources_read, job_id),
        )


def fail_job(job_id: str, error: dict, steps_executed: int = 0, sources_read: int = 0) -> None:
    with cursor() as cur:
        cur.execute(
            """UPDATE jobs SET status='failed', finished_at=?, error_json=?,
               steps_executed=?, sources_read=? WHERE id=?""",
            (now_iso(), json.dumps(error), steps_executed, sources_read, job_id),
        )


def set_quota_cache(key: str, remaining: int, limit_value: int) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO quota_cache (key, remaining, limit_value, checked_at)
               VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET remaining=excluded.remaining,
                   limit_value=excluded.limit_value, checked_at=excluded.checked_at""",
            (key, remaining, limit_value, now_iso()),
        )


def get_quota_cache(key: str) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM quota_cache WHERE key=?", (key,))
        row = cur.fetchone()
        return dict(row) if row else None


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def calls_by_route_since(hours: int, network: str | None = None) -> dict:
    with cursor() as cur:
        if network is not None:
            cur.execute(
                """SELECT route, status, COUNT(*) AS n FROM requests
                   WHERE ts >= ? AND network=? GROUP BY route, status""",
                (_since(hours), network),
            )
        else:
            cur.execute(
                """SELECT route, status, COUNT(*) AS n FROM requests
                   WHERE ts >= ? GROUP BY route, status""",
                (_since(hours),),
            )
        rows = cur.fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["route"], {})[row["status"]] = row["n"]
    return result


def jobs_queue_snapshot() -> dict:
    with cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        by_status = {row["status"]: row["n"] for row in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='done' AND finished_at >= ?",
            (_since(24),),
        )
        done_24h = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='failed' AND finished_at >= ?",
            (_since(24),),
        )
        failed_24h = cur.fetchone()["n"]
    return {
        "queued": by_status.get("queued", 0),
        "running": by_status.get("running", 0),
        "done_24h": done_24h,
        "failed_24h": failed_24h,
    }


def recent_events(hours: int = 24) -> list[dict]:
    """Every request logged in the window, whatever its outcome - unpaid 402s,
    failed settlements, paid calls (first-free-call included, it was still a
    served call) and input-validation errors. Deliberately NOT filtered by
    network: a 402 that was never paid never settled on any chain, so tying it
    to `config.X402_NETWORK` at log time (see log_request's default) would
    hide it from a same-day network switch like the mainnet cutover - see
    app/admin.py::admin_data, which is the only place this feeds into."""
    with cursor() as cur:
        cur.execute(
            """SELECT ts, route, method, status, user_agent, body_excerpt, payer,
                      amount_usdc, error_reason, latency_ms
               FROM requests WHERE ts >= ? ORDER BY ts ASC""",
            (_since(hours),),
        )
        return [dict(row) for row in cur.fetchall()]


def recent_unconverted(limit: int = 20) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT ts, route, user_agent, body_excerpt, error_reason FROM requests
               WHERE status IN ('unpaid', 'capacity_reached')
               ORDER BY ts DESC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def count_mpp_attempts_since(hours: int) -> int:
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE mpp_attempted=1 AND ts >= ?",
            (_since(hours),),
        )
        return cur.fetchone()["n"]


def recent_payment_failures(limit: int = 10, hours: int = 24) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT ts, route, payer, error_reason FROM requests
               WHERE status='payment_failed' AND ts >= ?
               ORDER BY ts DESC LIMIT ?""",
            (_since(hours), limit),
        )
        return [dict(row) for row in cur.fetchall()]


def record_heartbeat(ok: bool) -> None:
    with cursor() as cur:
        cur.execute("INSERT INTO heartbeats (ts, ok) VALUES (?, ?)", (now_iso(), int(ok)))
        # keep the table bounded - only 7d of heartbeats are ever needed
        cur.execute("DELETE FROM heartbeats WHERE ts < ?", (_since(24 * 8),))


def availability_since(hours: int, expected_interval_seconds: int = 60) -> float | None:
    """Fraction of expected heartbeat ticks actually recorded in the window.

    A dead process writes no rows at all, so counting rows against the number
    expected (rather than averaging the `ok` column) is what actually catches
    downtime gaps, including full restarts.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS first_ts FROM heartbeats WHERE ts >= ?",
            (_since(hours),),
        )
        row = cur.fetchone()
        if not row or not row["n"]:
            return None
        window_start = max(row["first_ts"], _since(hours))
        elapsed_seconds = (
            datetime.now(timezone.utc) - datetime.fromisoformat(window_start)
        ).total_seconds()
        expected = max(1, elapsed_seconds / expected_interval_seconds)
        return min(1.0, row["n"] / expected)


def get_chain_sync_state(key: str) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT value FROM chain_sync_state WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def set_chain_sync_state(key: str, value: str) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO chain_sync_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )


def upsert_chain_payment(
    tx_hash: str,
    from_address: str,
    amount_usdc: float,
    block_number: int,
    block_time: str,
    is_mechanical: bool,
    network: str,
) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO chain_payments
               (tx_hash, from_address, amount_usdc, block_number, block_time, is_mechanical, network)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(tx_hash) DO NOTHING""",
            (tx_hash, from_address, amount_usdc, block_number, block_time, int(is_mechanical), network),
        )


def chain_summary(network: str) -> dict:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM chain_payments WHERE network=?", (network,))
        total = cur.fetchone()["n"]
        if not total:
            return {"total_payments": 0, "distinct_buyers": 0, "mechanical_share": None}
        cur.execute(
            "SELECT COUNT(DISTINCT from_address) AS n FROM chain_payments WHERE network=?",
            (network,),
        )
        distinct = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM chain_payments WHERE network=? AND is_mechanical=1",
            (network,),
        )
        mechanical = cur.fetchone()["n"]
    return {
        "total_payments": total,
        "distinct_buyers": distinct,
        "mechanical_share": mechanical / total,
    }


def chain_revenue_since(network: str, hours: int) -> float:
    """Excludes MECHANICAL_WALLETS (is_mechanical=1) - self-funded bootstrap
    settlements (Bazaar indexing, ping-pong top-ups) are not revenue."""
    with cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) AS total FROM chain_payments "
            "WHERE network=? AND block_time >= ? AND is_mechanical=0",
            (network, _since(hours)),
        )
        return cur.fetchone()["total"]


def chain_buyer_stats(network: str) -> dict:
    """Excludes MECHANICAL_WALLETS - see chain_revenue_since()."""
    with cursor() as cur:
        cur.execute(
            "SELECT from_address, COUNT(*) AS n FROM chain_payments "
            "WHERE network=? AND is_mechanical=0 GROUP BY from_address",
            (network,),
        )
        rows = cur.fetchall()
    distinct = len(rows)
    returning = sum(1 for r in rows if r["n"] >= 2)
    return_rate = (returning / distinct) if distinct else 0.0
    return {"distinct": distinct, "returning": returning, "return_rate": return_rate}


def chain_last_payment_at(network: str) -> str | None:
    """Excludes MECHANICAL_WALLETS - see chain_revenue_since()."""
    with cursor() as cur:
        cur.execute(
            "SELECT MAX(block_time) AS t FROM chain_payments WHERE network=? AND is_mechanical=0",
            (network,),
        )
        row = cur.fetchone()
        return row["t"] if row else None


_ROUTE_MATCH_WINDOW_SECONDS = 300  # settlement typically lands within seconds-to-low-minutes


def route_chain_stats(network: str, since_hours: int | None = None) -> dict[str, dict]:
    """Per-route breakdown of on-chain payments - used by scripts/comptable.py.

    chain_payments has no `route` column (every route shares the single
    X402_PAY_TO address, so the chain itself cannot say which route a
    transfer paid for - see BRIEF-CORRECTIONS.md). Attribution is done here
    by nearest-neighbor matching each on-chain transfer to OUR OWN `requests`
    row with the same payer + amount, closest in time within a narrow
    window - not a third-party counter (the rule "read the chain, never
    third-party counters" was about not trusting external catalogs'
    self-reported numbers, not about our own request log). This is
    deliberately a nearest-match, not a fan-out JOIN: two routes priced
    identically (search and translate are both $0.10) could otherwise
    double-count a single transfer against both.

    Best-effort: a transfer with no requests row inside the window (e.g. a
    request logged just as the process restarted) is counted in the network
    totals (chain_summary/chain_revenue_since) but not attributed to a route.
    """
    with cursor() as cur:
        chain_sql = "SELECT tx_hash, from_address, amount_usdc, block_time, is_mechanical FROM chain_payments WHERE network=?"
        chain_params: list = [network]
        if since_hours is not None:
            chain_sql += " AND block_time >= ?"
            chain_params.append(_since(since_hours))
        cur.execute(chain_sql, chain_params)
        payments = [dict(row) for row in cur.fetchall()]

        cur.execute(
            "SELECT route, payer, amount_usdc, ts FROM requests WHERE status='paid' AND payer IS NOT NULL"
        )
        candidates = [dict(row) for row in cur.fetchall()]

    by_payer: dict[str, list[dict]] = {}
    for c in candidates:
        by_payer.setdefault(c["payer"].lower(), []).append(c)

    stats: dict[str, dict] = {}
    buyer_route_counts: dict[tuple[str, str], int] = {}

    for payment in payments:
        pool = by_payer.get(payment["from_address"].lower(), [])
        block_dt = datetime.fromisoformat(payment["block_time"])
        best, best_delta = None, None
        for c in pool:
            if c["amount_usdc"] is None or abs(c["amount_usdc"] - payment["amount_usdc"]) > 0.0001:
                continue
            req_dt = datetime.fromisoformat(c["ts"])
            delta = abs((req_dt - block_dt).total_seconds())
            if delta > _ROUTE_MATCH_WINDOW_SECONDS:
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = c, delta
        if best is None:
            continue

        route = best["route"]
        entry = stats.setdefault(
            route, {"payments": 0, "amount_usdc": 0.0, "mechanical_count": 0, "buyers": set()}
        )
        entry["payments"] += 1
        entry["amount_usdc"] += payment["amount_usdc"]
        if payment["is_mechanical"]:
            entry["mechanical_count"] += 1
        entry["buyers"].add(payment["from_address"].lower())
        key = (route, payment["from_address"].lower())
        buyer_route_counts[key] = buyer_route_counts.get(key, 0) + 1

    result = {}
    for route, entry in stats.items():
        distinct = len(entry["buyers"])
        returning = sum(1 for (r, _), n in buyer_route_counts.items() if r == route and n >= 2)
        result[route] = {
            "payments": entry["payments"],
            "amount_usdc": round(entry["amount_usdc"], 6),
            "distinct_buyers": distinct,
            "return_rate": (returning / distinct) if distinct else 0.0,
            "mechanical_share": (entry["mechanical_count"] / entry["payments"]) if entry["payments"] else 0.0,
        }
    return result


def has_seen_wallet(address: str) -> bool:
    """Read-only check - used by handlers to decide what price to show in the
    receipt. Never records anything; only mark_wallet_seen() does that."""
    with cursor() as cur:
        cur.execute("SELECT 1 FROM seen_wallets WHERE address=?", (address.lower(),))
        return cur.fetchone() is not None


def mark_wallet_seen(address: str) -> bool:
    """Atomically record a wallet as seen. Returns True only if this call is
    the one that actually inserted the row (a genuine first sighting) - the
    authoritative check used by the x402 before-settle hook to decide whether
    to waive settlement. INSERT OR IGNORE + rowcount, not read-then-write, so
    two concurrent first calls from the same wallet can't both be granted."""
    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO seen_wallets (address, first_seen_at) VALUES (?, ?)",
            (address.lower(), now_iso()),
        )
        return cur.rowcount > 0


def count_seen_wallets() -> int:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM seen_wallets")
        return cur.fetchone()["n"]


def has_mpp_nonce(nonce: str) -> bool:
    """Read-only fast-path check - used before attempting settlement to skip
    a known-already-settled credential without a chain round-trip. Never
    the sole guarantee against replay: the EIP-3009 nonce is enforced
    on-chain by the token contract regardless of this table's state."""
    with cursor() as cur:
        cur.execute("SELECT 1 FROM mpp_nonces WHERE nonce=?", (nonce.lower(),))
        return cur.fetchone() is not None


def mark_mpp_nonce_consumed(nonce: str, from_address: str) -> bool:
    """Atomic replay-protection check for MPP EIP-3009 authorizations -
    INSERT OR IGNORE + rowcount, same pattern as mark_wallet_seen(). Returns
    True only if this call is the one that actually consumed the nonce."""
    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO mpp_nonces (nonce, from_address, consumed_at) VALUES (?, ?, ?)",
            (nonce.lower(), from_address.lower(), now_iso()),
        )
        return cur.rowcount > 0


# --- usine (scripts/controleur.py, comptable.py, fossoyeur.py) ------------

def record_route_check(route: str, check_name: str, passed: bool, detail: str | None = None) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO route_checks (ts, route, check_name, passed, detail) VALUES (?,?,?,?,?)",
            (now_iso(), route, check_name, int(passed), detail),
        )


def record_index_check(catalog: str, query: str, found: bool, detail: str | None = None) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO index_checks (ts, catalog, query, found, detail) VALUES (?,?,?,?,?)",
            (now_iso(), catalog, query, int(found), detail),
        )


def last_index_check_at() -> str | None:
    """Gates scripts/controleur.py's daily visibility check to ~once/day
    regardless of how many times controleur.py itself runs (loop cycles,
    manual clicks, the real nightly cron all call the same function)."""
    with cursor() as cur:
        cur.execute("SELECT MAX(ts) AS ts FROM index_checks")
        row = cur.fetchone()
        return row["ts"] if row else None


def latest_index_checks(limit: int = 20) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT ts, catalog, query, found, detail FROM index_checks ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def latest_route_checks(route: str) -> list[dict]:
    """Most recent result per check_name for one route - not the full
    history, just "is this route currently green"."""
    with cursor() as cur:
        cur.execute(
            """SELECT check_name, passed, detail, ts FROM route_checks
               WHERE route=? AND id IN (
                   SELECT MAX(id) FROM route_checks WHERE route=? GROUP BY check_name
               )""",
            (route, route),
        )
        return [dict(row) for row in cur.fetchall()]


def route_is_green(route: str) -> bool:
    checks = latest_route_checks(route)
    return bool(checks) and all(c["passed"] for c in checks)


def routes_billing_without_delivery() -> list[dict]:
    """Routes whose most recent /sample check failed with a real server-side
    5xx - the buyer already paid (payment settles before the handler runs,
    see BRIEF-CORRECTIONS.md) and got nothing back. Deliberately narrower
    than route_is_green(): a 4xx (bad input), a "discover" warning, or an
    oversized header also turn a route non-green, but none of those charge a
    real buyer for a broken response the way an unhandled 5xx on /sample
    does - only this class is severe enough to alert on, per the standing
    rule that a route billing without delivering is worse than a slow one."""
    with cursor() as cur:
        cur.execute(
            """SELECT route, detail, ts FROM route_checks
               WHERE check_name='sample' AND passed=0 AND id IN (
                   SELECT MAX(id) FROM route_checks WHERE check_name='sample' GROUP BY route
               )"""
        )
        rows = [dict(row) for row in cur.fetchall()]
    return [r for r in rows if re.search(r"\bstatus 5\d\d\b", r["detail"] or "")]


def record_controleur_run(routes_tested: int, routes_rejected: int, duration_ms: int, summary: str) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO controleur_runs (ts, routes_tested, routes_rejected, duration_ms, summary)
               VALUES (?,?,?,?,?)""",
            (now_iso(), routes_tested, routes_rejected, duration_ms, summary),
        )


def latest_controleur_run() -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM controleur_runs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None


def add_journal_entry(
    agent: str, action: str, detail: str, route: str | None = None, trigger_kind: str = "cron"
) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO journal (ts, agent, action, route, detail, trigger_kind) VALUES (?,?,?,?,?,?)",
            (now_iso(), agent, action, route, detail, trigger_kind),
        )


def recent_journal(limit: int = 100) -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]


def count_settlement_attempts() -> int:
    """The one number the usine dashboard leads with: a settlement was
    attempted (paid or rejected after a real payment attempt) - not visits,
    not revenue. See app/admin.py::collect_usine_data()."""
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM requests WHERE status IN ('paid','payment_failed')")
        return cur.fetchone()["n"]


def last_agent_run(agent: str) -> dict | None:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM journal WHERE agent=? AND action='run' ORDER BY id DESC LIMIT 1",
            (agent,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


MANUAL_RUN_COOLDOWN_SECONDS = 5 * 60


def get_manual_run(role: str) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM agent_manual_runs WHERE role=?", (role,))
        row = cur.fetchone()
        return dict(row) if row else None


def manual_run_allowed(role: str) -> tuple[bool, str | None]:
    """Both gates are independent: a run already in flight blocks regardless
    of elapsed time, and a recent launch blocks the next click even after it
    finished (so a controleur pass that takes longer than the cooldown can't
    be double-triggered the moment it completes)."""
    existing = get_manual_run(role)
    if existing is None:
        return True, None
    if existing["status"] in ("requested", "running"):
        return False, "déjà en cours"
    started = datetime.fromisoformat(existing["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    remaining = MANUAL_RUN_COOLDOWN_SECONDS - elapsed
    if remaining > 0:
        return False, f"délai minimum entre deux lancements : encore {int(remaining)}s"
    return True, None


def start_manual_run(role: str, run_id: str, status: str = "running") -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO agent_manual_runs (role, run_id, status, started_at, finished_at, detail)
               VALUES (?,?,?,?,NULL,NULL)
               ON CONFLICT(role) DO UPDATE SET
                 run_id=excluded.run_id, status=excluded.status,
                 started_at=excluded.started_at, finished_at=NULL, detail=NULL""",
            (role, run_id, status, now_iso()),
        )


def finish_manual_run(role: str, run_id: str, status: str, detail: str) -> None:
    """No-op if `run_id` no longer matches the stored row - a stale
    background task finishing after a newer manual run started must never
    clobber that newer run's state."""
    with cursor() as cur:
        cur.execute(
            """UPDATE agent_manual_runs SET status=?, finished_at=?, detail=?
               WHERE role=? AND run_id=?""",
            (status, now_iso(), detail[:500], role, run_id),
        )


def add_proposal(
    intention: str, source: str | None, estimated_routes: int | None, rationale: str | None,
    demand_evidence: str,
) -> int:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO proposals (proposed_at, intention, source, estimated_routes, rationale, status, demand_evidence)
               VALUES (?,?,?,?,?,'proposée',?)""",
            (now_iso(), intention, source, estimated_routes, rationale, demand_evidence),
        )
        return cur.lastrowid


def list_proposals(status: str | None = None, limit: int = 100) -> list[dict]:
    with cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            cur.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]


def get_proposal(proposal_id: int) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def approve_proposal(proposal_id: int) -> bool:
    """Only proposée -> approuvée - guards against re-approving a proposal
    already treated or rejected. Returns False if not applicable."""
    with cursor() as cur:
        cur.execute(
            "UPDATE proposals SET status='approuvée', approved_at=? WHERE id=? AND status='proposée'",
            (now_iso(), proposal_id),
        )
        return cur.rowcount > 0


def block_proposal_budget(proposal_id: int, reason: str) -> bool:
    """proposée or approuvée -> bloquée_budget - either at registration time
    (the upstream itself requires payment, see admin_usine_create_proposal)
    or later by the operator on an already-approved proposal Ouvrier can
    never build alone (2026-09-06: #6 and #7 sat 'approuvée' indefinitely,
    indistinguishable from 'ready to build'). Returns False if not
    applicable (already traitée/rejetée)."""
    with cursor() as cur:
        cur.execute(
            "UPDATE proposals SET status='bloquée_budget', block_reason=? "
            "WHERE id=? AND status IN ('proposée','approuvée')",
            (reason, proposal_id),
        )
        return cur.rowcount > 0


def close_proposal(proposal_id: int) -> bool:
    """Only approuvée -> traitée - called by Ouvrier once the route is built,
    tested and deployed. Returns False if not applicable."""
    with cursor() as cur:
        cur.execute(
            "UPDATE proposals SET status='traitée', closed_at=? WHERE id=? AND status='approuvée'",
            (now_iso(), proposal_id),
        )
        return cur.rowcount > 0


def report_pc_pipeline(result: str, detail: str, task_active: bool | None) -> None:
    """Single row, always overwritten - see usine/nightly_run.sh, called once
    at the end of each run. task_active is None when the scheduled task's
    state couldn't be queried (e.g. Windows Task Scheduler interop failed),
    never guessed."""
    active_value = None if task_active is None else int(task_active)
    with cursor() as cur:
        cur.execute(
            """INSERT INTO pc_pipeline_status (id, reported_at, result, detail, task_active)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 reported_at=excluded.reported_at, result=excluded.result,
                 detail=excluded.detail, task_active=excluded.task_active""",
            (now_iso(), result, detail[:500], active_value),
        )


def get_pc_pipeline_status() -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM pc_pipeline_status WHERE id=1")
        row = cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["task_active"] = None if data["task_active"] is None else bool(data["task_active"])
        return data


def create_pc_request(role: str) -> int:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO pc_requests (role, status, requested_at) VALUES (?, 'requested', ?)",
            (role, now_iso()),
        )
        return cur.lastrowid


PC_REQUEST_TIMEOUT_SECONDS = 20 * 60


def _expire_if_stale(request: dict) -> dict:
    """A request stuck at requested/running must not block the dashboard's
    buttons forever if the PC never answers (listener down, PC off, a
    session that hangs past any reasonable duration). Called from
    latest_pc_request() - the one function both the display and the
    run/{role} lock-check read - so the expiry is applied consistently
    everywhere, not just for display."""
    if request["status"] not in ("requested", "running"):
        return request
    reference_ts = request["started_at"] or request["requested_at"]
    reference = datetime.fromisoformat(reference_ts)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - reference).total_seconds()
    if elapsed <= PC_REQUEST_TIMEOUT_SECONDS:
        return request
    detail = "aucune nouvelle du PC depuis 20 minutes - marquée expirée automatiquement"
    update_pc_request_status(request["id"], "timeout", detail)
    add_journal_entry(
        "operator", "pc_timeout", f"demande #{request['id']} ({request['role']}) expirée", trigger_kind="manual"
    )
    request["status"] = "timeout"
    request["detail"] = detail
    return request


def latest_pc_request() -> dict | None:
    """The single global lock and the display state at once - only one PC
    request (pipeline or any one role) is ever allowed in flight, so "the
    most recent row" is unambiguous both as "is something running" and as
    "what was the last result"."""
    with cursor() as cur:
        cur.execute("SELECT * FROM pc_requests ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        request = dict(row) if row else None
    if request is None:
        return None
    return _expire_if_stale(request)


def oldest_pending_pc_request() -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM pc_requests WHERE status='requested' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None


def get_pc_request(request_id: int) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM pc_requests WHERE id=?", (request_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_pc_request_status(request_id: int, status: str, detail: str = "") -> bool:
    with cursor() as cur:
        if status == "running":
            cur.execute(
                "UPDATE pc_requests SET status=?, started_at=COALESCE(started_at, ?) WHERE id=?",
                (status, now_iso(), request_id),
            )
        else:
            cur.execute(
                "UPDATE pc_requests SET status=?, finished_at=?, detail=? WHERE id=?",
                (status, now_iso(), detail[:500], request_id),
            )
        return cur.rowcount > 0


def record_pc_heartbeat() -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO pc_listener_heartbeat (id, last_seen_at) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (now_iso(),),
        )


def get_pc_heartbeat() -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM pc_listener_heartbeat WHERE id=1")
        row = cur.fetchone()
        return dict(row) if row else None


def get_loop_state() -> dict:
    with cursor() as cur:
        cur.execute("SELECT * FROM usine_loop_state WHERE id=1")
        row = cur.fetchone()
        if row is not None:
            return dict(row)
    return {
        "id": 1, "status": "stopped", "started_at": None, "cycle_count": 0,
        "last_cycle_at": None, "consecutive_controleur_rejections": 0, "stop_reason": None,
    }


def start_loop() -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO usine_loop_state
                 (id, status, started_at, cycle_count, last_cycle_at, consecutive_controleur_rejections, stop_reason)
               VALUES (1, 'running', ?, 0, NULL, 0, NULL)
               ON CONFLICT(id) DO UPDATE SET
                 status='running', started_at=excluded.started_at, cycle_count=0,
                 last_cycle_at=NULL, consecutive_controleur_rejections=0, stop_reason=NULL""",
            (now_iso(),),
        )


def stop_loop(reason: str) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO usine_loop_state (id, status, stop_reason) VALUES (1, 'stopped', ?)
               ON CONFLICT(id) DO UPDATE SET status='stopped', stop_reason=excluded.stop_reason""",
            (reason[:500],),
        )


def record_cycle(route_rejected: bool) -> dict:
    """Called once per completed cycle by usine/loop_run.sh. Tracks
    consecutive Contrôleur rejections across cycles (reset on any cycle
    that didn't reject) - the counter app/admin.py checks for the 3-in-a-row
    auto-stop condition. Returns the updated state so the caller can decide
    to stop without a second round-trip."""
    with cursor() as cur:
        cur.execute(
            """UPDATE usine_loop_state SET
                 cycle_count = cycle_count + 1,
                 last_cycle_at = ?,
                 consecutive_controleur_rejections = CASE WHEN ? THEN consecutive_controleur_rejections + 1 ELSE 0 END
               WHERE id=1""",
            (now_iso(), int(route_rejected)),
        )
    return get_loop_state()


def get_repair_attempts(slug: str) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM route_repair_attempts WHERE slug=?", (slug,))
        row = cur.fetchone()
        return dict(row) if row else None


def record_repair_attempt(slug: str, result: str) -> int:
    """Returns the new attempt count so the caller can compare against the
    2-strikes threshold without a second read."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO route_repair_attempts (slug, attempt_count, last_attempt_at, last_result)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 attempt_count = attempt_count + 1, last_attempt_at = excluded.last_attempt_at,
                 last_result = excluded.last_result""",
            (slug, now_iso(), result[:500]),
        )
        cur.execute("SELECT attempt_count FROM route_repair_attempts WHERE slug=?", (slug,))
        return cur.fetchone()["attempt_count"]


def reset_repair_attempts(slug: str) -> None:
    """A route passing Contrôleur again clears its repair history - only
    CONSECUTIVE failures count toward the 2-strikes kill."""
    with cursor() as cur:
        cur.execute("DELETE FROM route_repair_attempts WHERE slug=?", (slug,))


# Same classification as app/templates/live.html::isScanner() (front-end,
# kept in sync by hand - no shared module between the JS dashboard and this
# backend query). Used only for history_7d() below.
_SCANNER_UA_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"x402-list", r"forgemesh", r"x402scan|poncho", r"402index",
        r"autonomous-directory", r"directory.?probe", r"ampersend",
        r"nohumans", r"uptime|pingdom|healthcheck|monitor",
        r"\bprobe\b", r"\bscanner\b", r"\bcrawler\b", r"\bbot\b",
        r"AgentIndex-probe", r"circle.*scan", r"coinbase.*crawl",
        r"^node$", r"^conform/", r"^ops-check/", r"^post-cut/",
        r"^live-dashboard-smoke/", r"^discover-post-probe/",
        r"^hermes-contact-discovery/", r"^x402watch/",
    )
]


def _mechanical_wallets() -> set[str]:
    """Same parsing as chain_payments.py::_mechanical_wallets() - duplicated
    on purpose rather than imported, to keep db.py free of that module's
    own import surface."""
    raw = getattr(config, "MECHANICAL_WALLETS", "") or ""
    return {w.strip().lower() for w in raw.split(",") if w.strip()}


def _is_scanner_ua(user_agent: str | None) -> bool:
    ua = user_agent or ""
    return any(pattern.search(ua) for pattern in _SCANNER_UA_PATTERNS)


def history_7d() -> list[dict]:
    """Per UTC day, last 7 days: total requests, distinct client IPs that are
    neither a known scanner UA nor our own VPS (config.VPS_PUBLIC_IP - our own
    curl/verification traffic against the public domain), and real payments
    (status='paid', payer not in MECHANICAL_WALLETS - same rule as
    chain_revenue_since(), self-funded bootstrap settlements are not revenue)."""
    mechanical = _mechanical_wallets()
    with cursor() as cur:
        cur.execute(
            "SELECT ts, client_ip, user_agent, status, payer FROM requests WHERE ts >= ?",
            (_since(24 * 7),),
        )
        rows = cur.fetchall()

    by_day: dict[str, dict] = {}
    for row in rows:
        day = row["ts"][:10]
        bucket = by_day.setdefault(day, {"requests_total": 0, "identities": set(), "payments_real": 0})
        bucket["requests_total"] += 1
        ip = row["client_ip"]
        if ip and ip != config.VPS_PUBLIC_IP and not _is_scanner_ua(row["user_agent"]):
            bucket["identities"].add(ip)
        if row["status"] == "paid" and (row["payer"] or "").lower() not in mechanical:
            bucket["payments_real"] += 1

    return [
        {
            "day": day,
            "requests_total": bucket["requests_total"],
            "distinct_identities": len(bucket["identities"]),
            "payments_real": bucket["payments_real"],
        }
        for day, bucket in sorted(by_day.items())
    ]
