"""Upgrade-in-place: a live pre-multi-worker DB (nodes.role UNIQUE) must migrate
losslessly on startup — same rows, same ids, FKs intact — and then accept a
second worker row.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest


OLD_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER NOT NULL,
    role VARCHAR(16),
    name VARCHAR(64) NOT NULL,
    lan_ip VARCHAR(64) NOT NULL,
    qsfp_ip VARCHAR(64) NOT NULL,
    qsfp_iface VARCHAR(32) NOT NULL,
    ssh_user VARCHAR(64) NOT NULL,
    ssh_port INTEGER NOT NULL,
    auth_method VARCHAR(16) NOT NULL,
    ssh_password_enc TEXT,
    ssh_private_key_enc TEXT,
    ssh_key_passphrase_enc TEXT,
    sudo_mode VARCHAR(16) NOT NULL,
    sudo_password_enc TEXT,
    hardened BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (role)
);
CREATE TABLE models (
    id INTEGER NOT NULL,
    repo_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (repo_id)
);
CREATE TABLE model_node_states (
    id INTEGER NOT NULL,
    model_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    present BOOLEAN NOT NULL,
    status VARCHAR(16) NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_model_node UNIQUE (model_id, node_id),
    FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES nodes (id) ON DELETE CASCADE
);
"""

NODE_ROW = (
    "{id}, '{role}', '{name}', '192.168.1.{o}', '10.10.10.{o}', 'enp1s0f1np1', "
    "'user', 22, 'password', NULL, NULL, NULL, 'nopasswd', NULL, 0, "
    "'2026-01-01 00:00:00', '2026-01-01 00:00:00'"
)


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    """Fresh app.db module bound to a temp SPARK_DATA_DIR."""
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config
    import app.db as db

    config.get_settings.cache_clear()
    importlib.reload(db)
    return db, tmp_path


async def test_old_db_migrates_and_accepts_multiple_workers(app_db):
    db, tmp_path = app_db
    path = tmp_path / "spark.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute(f"INSERT INTO nodes VALUES ({NODE_ROW.format(id=1, role='head', name='spark-01', o=160)})")
    con.execute(f"INSERT INTO nodes VALUES ({NODE_ROW.format(id=2, role='worker', name='spark-02', o=161)})")
    con.execute(
        "INSERT INTO models VALUES (1, 'org/m', 'm', 'present', '2026-01-01', '2026-01-01')"
    )
    con.execute("INSERT INTO model_node_states VALUES (1, 1, 1, 1, 'present', '2026-01-01')")
    con.execute("INSERT INTO model_node_states VALUES (2, 1, 2, 1, 'present', '2026-01-01')")
    con.commit()
    con.close()

    await db.init_db()

    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    # UNIQUE(role) is gone; a second worker inserts cleanly.
    rows = con.execute("SELECT id, role, name FROM nodes ORDER BY id").fetchall()
    assert rows == [(1, "head", "spark-01"), (2, "worker", "spark-02")]
    # explicit columns: the rebuilt table also gains newer columns (mac_address)
    con.execute(
        "INSERT INTO nodes (id, role, name, lan_ip, qsfp_ip, qsfp_iface, ssh_user, ssh_port, "
        "auth_method, sudo_mode, hardened, created_at, updated_at) VALUES "
        "(3, 'worker', 'spark-03', '192.168.1.162', '10.10.10.3', 'enp1s0f1np1', 'user', 22, "
        "'password', 'nopasswd', 0, '2026-01-01', '2026-01-01')"
    )
    # FKs survived the rebuild (same node ids).
    assert con.execute("SELECT COUNT(*) FROM model_node_states").fetchone()[0] == 2
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # Cascade still wired to the rebuilt parent table.
    con.execute("DELETE FROM nodes WHERE id=2")
    assert con.execute("SELECT COUNT(*) FROM model_node_states").fetchone()[0] == 1
    con.close()

    # Second startup: migration is a no-op, nothing breaks.
    await db.init_db()


async def test_fresh_db_has_no_role_unique(app_db):
    db, tmp_path = app_db
    await db.init_db()
    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    con.execute("INSERT INTO nodes (role, name, lan_ip, qsfp_ip, qsfp_iface, ssh_user, ssh_port, auth_method, sudo_mode, hardened, created_at, updated_at) VALUES ('worker','a','1.1.1.1','2.2.2.2','x','u',22,'password','nopasswd',0,'2026-01-01','2026-01-01')")
    con.execute("INSERT INTO nodes (role, name, lan_ip, qsfp_ip, qsfp_iface, ssh_user, ssh_port, auth_method, sudo_mode, hardened, created_at, updated_at) VALUES ('worker','b','1.1.1.2','2.2.2.3','x','u',22,'password','nopasswd',0,'2026-01-01','2026-01-01')")
    con.close()


# --- v1.24.0: instance lifecycle columns ----------------------------------
# The reconciler needs durable evidence (started_at / last_healthy_at /
# last_load_seconds). A live DB predates all three, and a running deployment
# must gain them without losing a row — including instances that are `running`
# at the moment of the upgrade.
INSTANCES_OLD = """
CREATE TABLE instances (
    id INTEGER NOT NULL,
    name VARCHAR(64) NOT NULL,
    model_id INTEGER NOT NULL,
    topology VARCHAR(16) NOT NULL,
    node_id INTEGER,
    port INTEGER NOT NULL,
    tensor_parallel_size INTEGER NOT NULL,
    gpu_memory_utilization FLOAT NOT NULL,
    trust_remote_code BOOLEAN NOT NULL,
    enable_tool_choice BOOLEAN NOT NULL,
    master_port INTEGER NOT NULL,
    tls_enabled BOOLEAN NOT NULL,
    tls_port INTEGER NOT NULL,
    autostart BOOLEAN NOT NULL,
    systemd_unit VARCHAR(128),
    status VARCHAR(16) NOT NULL,
    last_error TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name)
);
"""


async def test_instances_gain_lifecycle_columns(app_db):
    db, tmp_path = app_db
    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    con.executescript(OLD_SCHEMA)
    con.executescript(INSTANCES_OLD)
    con.execute(
        "INSERT INTO models VALUES (1, 'poolside/Laguna', 'laguna', 'present', "
        "'2026-01-01', '2026-01-01')"
    )
    con.execute(
        "INSERT INTO instances (id, name, model_id, topology, port, "
        "tensor_parallel_size, gpu_memory_utilization, trust_remote_code, "
        "enable_tool_choice, master_port, tls_enabled, tls_port, autostart, "
        "systemd_unit, status, created_at, updated_at) VALUES "
        "(7, 'lag', 1, 'distributed', 8000, 2, 0.72, 0, 1, 29500, 0, 443, 1, "
        "'vllm-lag.service', 'running', '2026-01-01', '2026-01-01')"
    )
    con.commit()
    con.close()

    await db.init_db()

    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    cols = {r[1] for r in con.execute("PRAGMA table_info('instances')")}
    assert {"started_at", "last_healthy_at", "last_load_seconds"} <= cols

    row = con.execute(
        "SELECT id, name, status, port, started_at, last_healthy_at, "
        "last_load_seconds FROM instances"
    ).fetchall()
    assert len(row) == 1
    id_, name, status, port, started, healthy, load = row[0]
    # The live row survives untouched; the new columns are simply empty. A
    # `running` row must NOT be rewritten by the upgrade — the observer decides.
    assert (id_, name, status, port) == (7, "lag", "running", 8000)
    assert (started, healthy, load) == (None, None, None)
    con.close()

    # Idempotent: a second start (e.g. a pod restart) must not fail or duplicate.
    await db.init_db()
    con = sqlite3.connect(tmp_path / "spark.sqlite3")
    assert con.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 1
    con.close()
