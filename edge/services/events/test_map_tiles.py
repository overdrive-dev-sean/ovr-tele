import sqlite3
import unittest

from map_tiles import (
    build_guardrail_status,
    get_tile_counts,
    increment_tile_count,
    init_map_tables,
)


class MapTileTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_map_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_month_rollover_separates_counts(self):
        increment_tile_count(self.conn, "2026-01", "mapbox", 5)
        increment_tile_count(self.conn, "2026-02", "mapbox", 2)
        self.conn.commit()

        jan = get_tile_counts(self.conn, "2026-01")
        feb = get_tile_counts(self.conn, "2026-02")

        self.assertEqual(jan["mapbox"], 5)
        self.assertEqual(feb["mapbox"], 2)

    def test_guardrail_blocks_at_threshold(self):
        status = build_guardrail_status(
            "mapbox",
            {"mapbox": 95, "esri": 0},
            {"mapbox": 100, "esri": 100},
            0.95,
        )
        self.assertTrue(status["blocked"]["mapbox"])
        self.assertEqual(status["recommended_provider"], "esri")


if __name__ == "__main__":
    unittest.main()
