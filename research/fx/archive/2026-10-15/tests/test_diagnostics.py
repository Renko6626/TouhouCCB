"""Numerical regressions that change scientific conclusions."""

import unittest

import numpy as np

from research.fx.diagnostics import acf, describe, pareto_fit, rs_curve, tail_alternatives


class DiagnosticTests(unittest.TestCase):
    def test_lognormal_family_contains_pareto_limit(self):
        values = 1 + np.random.default_rng(21).pareto(12, 10000)
        fit = pareto_fit(values)
        result = tail_alternatives(values, fit)
        # The nested boundary has identical likelihood, one extra AIC parameter.
        self.assertLessEqual(result['aic']['truncated_lognormal'], result['aic']['pareto'] + 2.00001)

    def test_constant_series_is_not_fabricated_correlation_or_memory(self):
        self.assertEqual(describe([0,0,1,2])['zero_fraction'],.5)
        self.assertEqual(len(acf([1,2,3,4],10)),4)
        self.assertTrue(np.isnan(acf(np.ones(2000), 10)).all())
        curve = rs_curve(np.ones(2000), 20)
        self.assertIsNone(curve['scaling_slope'])


if __name__ == '__main__':
    unittest.main()
