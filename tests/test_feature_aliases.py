import tempfile
import unittest
from pathlib import Path
import pandas as pd
from feature_builder import load_source

class FeatureAliasTests(unittest.TestCase):
    def test_binance_camelcase_microstructure_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"x.csv"
            n=500
            t=[1700000000000 + 60000*i for i in range(n)]
            # align first timestamp to minute
            t0=(1700000000000//60000)*60000
            t=[t0+60000*i for i in range(n)]
            df=pd.DataFrame({
                "openTime":t, "open":[100.0]*n, "high":[101.0]*n, "low":[99.0]*n,
                "close":[100.5]*n, "volume":[10.0]*n, "quoteVolume":[1000.0]*n,
                "trades":[5]*n, "takerBuyBaseVol":[6.0]*n, "takerBuyQuoteVol":[600.0]*n,
            })
            df.to_csv(p,index=False)
            got=load_source(p)
            self.assertIn("quote_volume", got.columns)
            self.assertIn("taker_buy_base", got.columns)
            self.assertIn("taker_sell_base", got.columns)
            self.assertIn("delta_base", got.columns)
            self.assertAlmostEqual(float(got["delta_base"].iloc[0]), 2.0)

if __name__ == "__main__":
    unittest.main()
