"""Slice 0 tests: scaffolding, config, health endpoint."""

from __future__ import annotations


class TestScaffold:
    def test_health_ok(self, app_client):
        resp = app_client.get("/cnbc/health")
        assert resp.status_int == 200
        assert resp.json == {"status": "ok"}

    def test_config_loads_defaults(self):
        from app.config import settings

        assert settings.api_port == 8019
        assert settings.archive_collection == "TV-CNBC"
        assert settings.watchlist_signal_type == "cnbc_mention"

    def test_show_allowlist_parsing(self):
        from app.config import Settings

        s = Settings(shows="mad-money, squawk-box ,")
        assert s.show_allowlist == ["mad-money", "squawk-box"]
