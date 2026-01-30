import os
import sqlite3
import unittest
from unittest import mock

from map_tiles import (
    build_tile_policy,
    get_preferred_provider,
    get_tile_usage_totals,
    init_map_tables,
    record_tile_usage,
    set_preferred_provider,
)

os.environ.setdefault("FLEET_DB_PATH", "/tmp/fleet-test.db")

import app as api_app  # noqa: E402


class FleetMapTileTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_map_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_preferred_provider_persistence(self):
        set_preferred_provider(self.conn, "deploy-1", "mapbox")
        self.conn.commit()
        self.assertEqual(get_preferred_provider(self.conn, "deploy-1"), "mapbox")

    def test_tile_usage_totals(self):
        record_tile_usage(self.conn, "2026-01", "mapbox", "node-a", "deploy-1", 5)
        record_tile_usage(self.conn, "2026-01", "mapbox", "node-a", "deploy-1", 3)
        record_tile_usage(self.conn, "2026-01", "esri", "node-b", "deploy-1", 2)
        self.conn.commit()
        totals = get_tile_usage_totals(self.conn, "2026-01", ["deploy-1"])
        self.assertEqual(totals["mapbox"], 8)
        self.assertEqual(totals["esri"], 2)

    def test_tile_policy_switching(self):
        totals = {"mapbox": 95, "esri": 10}
        thresholds = {"mapbox": 100, "esri": 100}
        policy = build_tile_policy("mapbox", totals, thresholds, 0.95, 1.0, "Jan 2026")
        self.assertEqual(policy["recommended_provider"], "esri")
        self.assertTrue(policy["blocked"]["mapbox"])

    def test_map_tiles_total_fallback(self):
        with mock.patch.object(api_app, "_map_tiles_metric_names", return_value=["a", "b"]), \
             mock.patch.object(api_app, "_vm_query_vector_status") as mock_query:
            mock_query.side_effect = [
                (True, []),
                (True, [{"value": [0, "42"]}]),
            ]
            value, ok = api_app._map_tiles_total("mapbox", None)
            self.assertTrue(ok)
            self.assertEqual(value, 42)


if __name__ == "__main__":
    unittest.main()
