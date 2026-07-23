"""MCP parity: every feature area added v1.6-v1.21 has tools, and they run."""

from __future__ import annotations

import asyncio
import importlib

import pytest

EXPECTED_NEW = [
    "node_history", "instance_history", "node_interfaces",
    "power_affected", "power_node", "power_batch",
    "log_units", "log_tail",
    "alert_list", "alert_active", "alert_test_webhook",
    "usage_get",
    "schedule_list", "schedule_now", "schedule_create", "schedule_update", "schedule_delete",
    "backup_export", "backup_import", "backup_run_now", "backup_list_s3",
    "backup_restore_s3", "backup_status",
    "storage_report", "storage_delete_orphan", "storage_clear_hf_cache",
    "image_tags", "image_update",
]


@pytest.fixture()
def mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_DATA_DIR", str(tmp_path))
    import app.config as config

    config.get_settings.cache_clear()
    import app.db as db

    importlib.reload(db)
    asyncio.run(db.init_db())
    from app.mcp_server import build_mcp_server

    yield build_mcp_server()
    config.get_settings.cache_clear()


def test_all_new_tools_registered(mcp_server):
    names = {t.name for t in asyncio.run(mcp_server.list_tools())}
    missing = [n for n in EXPECTED_NEW if n not in names]
    assert missing == []


def test_destructive_tools_are_marked(mcp_server):
    tools = {t.name: t for t in asyncio.run(mcp_server.list_tools())}
    for name in ("power_node", "power_batch", "backup_import",
                 "backup_restore_s3", "storage_delete_orphan"):
        assert "DESTRUCTIVE" in (tools[name].description or ""), name


def test_read_tools_execute(mcp_server):
    async def run():
        out = {}
        for name, args in [
            ("schedule_now", {}),
            ("alert_active", {}),
            ("node_history", {"minutes": 5}),
            ("backup_status", {}),
            ("backup_export", {}),
            ("schedule_list", {}),
            ("usage_get", {}),
        ]:
            out[name] = await mcp_server.call_tool(name, args)
        return out

    results = asyncio.run(run())

    def payload(res):
        # this SDK returns a list of TextContent blocks with JSON text
        import json

        return json.loads(res[0].text)

    assert payload(results["backup_export"])["kind"] == "spark-controlplane-backup"
    assert 0 <= payload(results["schedule_now"])["weekday"] <= 6
