"""Historical regression checks for daily market analysis."""

import unittest
from market_catalog import detect_price_control_zones, evaluate_market_opportunity
from tests.integration.historical_support import frozen_store


class HistoricalDailyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = frozen_store()
        cls.addClassCleanup(cls.store._con.close)

    def test_bloom_energy_april_control_zone_detection(self):
        """Verifica que en Bloom Energy (BE) se detecte la zona de control de abril 2026 ($197-$203) como soporte."""
        store = self.store
        df = store.load_bars("BE")
        self.assertFalse(df.empty, "Se requieren datos históricos de BE en Parquet.")

        res = detect_price_control_zones(df, min_gap_pct=0.03)
        self.assertGreater(len(res["zones"]), 0)

        # Buscar el gap de abril 2026 en BE
        april_gaps = [z for z in res["zones"] if z["date"].startswith("2026-04-14")]
        self.assertEqual(len(april_gaps), 1, "Debe existir el gap clave del 2026-04-14 en BE")
        be_gap = april_gaps[0]

        # Verificar límites de la zona: $197.40 a $202.78 (aprox. $197 - $203)
        self.assertAlmostEqual(be_gap["lower_bound"], 197.40, places=1)
        self.assertAlmostEqual(be_gap["upper_bound"], 202.78, places=1)
        self.assertEqual(be_gap["type"], "ALCISTA")
        self.assertEqual(be_gap["status"], "CERRADO")
        self.assertEqual(be_gap["role"], "SOPORTE")

        # Verificar que la función get_price_control_zones de MarketDataStore identifica esta zona como soporte más cercano
        store_res = store.get_price_control_zones("BE", min_gap_pct=0.03)
        nearest_sup = store_res["nearest_support"]
        self.assertIsNotNone(nearest_sup)
        self.assertEqual(nearest_sup["date"], "2026-04-14")
        self.assertAlmostEqual(nearest_sup["lower_bound"], 197.40, places=1)


    def test_detect_price_control_zones_price_inside_zone_and_support_resolution(self):
        """Verifica que cuando el precio cotiza dentro de una zona de soporte, se identifique como active_zone y nearest_support."""
        store = self.store
        df = store.load_bars("BE")
        self.assertFalse(df.empty)

        # Simular evaluación de mercado cuando BE cotiza a $201.00 (dentro del gap de abril 197.40 - 202.78)
        res = detect_price_control_zones(df, min_gap_pct=0.03, current_price=201.00)
        self.assertIsNotNone(res["active_zone"])
        self.assertEqual(res["active_zone"]["date"], "2026-04-14")
        # active_zone captura la zona de abril 2026 en la que cotiza el precio
        self.assertIsNotNone(res["nearest_support"])
        self.assertLessEqual(res["nearest_support"]["upper_bound"], 201.00)


    def test_evaluate_market_opportunity_real_be_data_at_201(self):
        """Verifica la integración integral con datos reales de BE testeando zona $197-$203 disparando señal COMPRAR."""
        store = self.store
        df = store.load_bars("BE")
        levels = store.get_market_levels("BE")
        multi = store.get_multi_horizon_levels("BE")
        cz = detect_price_control_zones(df, min_gap_pct=0.03, current_price=203.50)

        res = evaluate_market_opportunity(
            symbol="BE",
            last_close=203.50,
            levels=levels,
            multi_levels=multi,
            current_position=0,
            control_zones=cz,
        )
        self.assertEqual(res["veredicto"], "COMPRAR (LONG)")
        self.assertIn("197.40", res["control_zone_soporte"])
        self.assertIn("MOMENTO ÓPTIMO PARA LARGOS", res["razonamiento"])
        self.assertGreater(res["long_rr_ratio"], 1.5)
