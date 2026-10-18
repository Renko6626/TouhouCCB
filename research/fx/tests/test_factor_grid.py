import unittest

from research.fx.factor_activity.grid import load_spec, parameter_grid, select_candidates
from research.fx.factor_activity.run import load_config


class FactorGridTests(unittest.TestCase):
    def test_full_grid_is_unique_and_includes_original_parameters(self):
        base=load_config();cells=parameter_grid(base,load_spec())
        self.assertEqual(len(cells),162)
        self.assertEqual(len({c['id'] for c in cells}),162)
        originals=[c for c in cells if c['config']==base]
        self.assertEqual(len(originals),1)
        self.assertEqual(originals[0]['parameters']['trend_multiplier'],1)
        self.assertEqual({tuple(c['config']['factor_mix'][0]) for c in cells},
                         {(a,b) for a in [3,30,300] for b in [.01,.1,1]})

    def test_selection_keeps_three_rankings_baseline_and_excludes_failed_cells(self):
        rows=[dict(id='baseline',status='complete',volatility_min=-1,
                   feedback_gain_min=-1,max_signed=1)]
        for name,v,d,r in [('v1',10,0,100),('v2',9,0,100),
                           ('d1',1,10,100),('d2',1,9,100),
                           ('ratio1',1,0,.001),('ratio2',1,0,.002)]:
            rows.append(dict(id=name,status='complete',volatility_min=v,
                             feedback_gain_min=d,max_signed=r))
        rows.append(dict(id='failed',status='incomplete',volatility_min=100,
                         feedback_gain_min=100,max_signed=0))
        selected=select_candidates(rows,'baseline',2)
        self.assertEqual([r['id'] for r in selected],
                         ['v1','v2','d1','d2','ratio1','ratio2','baseline'])
        self.assertEqual(len({r['id'] for r in selected}),7)
