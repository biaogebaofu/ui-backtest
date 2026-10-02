import unittest
from batch4_compare import rank90,config_key
class RankingTests(unittest.TestCase):
 def test_strict_threshold_and_stable_tie(self):
  rows=[dict(mode='THEORETICAL',trades=1,final=100,dd=.2,id='high'),dict(mode='THEORETICAL',trades=1,final=90,dd=0,id='boundary'),dict(mode='THEORETICAL',trades=1,final=95,dd=.1,id='lowerdd')]
  chosen,top,mx=rank90(rows)
  self.assertEqual([r['id'] for r in chosen],['lowerdd','high']);self.assertEqual(mx,100)
 def test_never_mix_execution_models(self):
  with self.assertRaises(ValueError):rank90([dict(mode='THEORETICAL'),dict(mode='CLOSE_CONFIRMED')])
 def test_no_trades_excluded_and_top5000_first(self):
  rows=[dict(mode='THEORETICAL',trades=0,final=10000,dd=0,id='empty'),dict(mode='THEORETICAL',trades=1,final=100,dd=.2,id='valid')]
  chosen,top,mx=rank90(rows);self.assertEqual(len(chosen),1);self.assertEqual(mx,100)
if __name__=='__main__':unittest.main()
