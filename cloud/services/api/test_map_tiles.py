import sqlite3
import unittest

from map_tiles import get_preferred_provider, init_map_tables, set_preferred_provider


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


if __name__ == "__main__":
    unittest.main()
