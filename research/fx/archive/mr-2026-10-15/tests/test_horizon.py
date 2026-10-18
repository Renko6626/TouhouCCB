import unittest
import numpy as np
from research.fx.switching import Agent
from research.fx.switching_run import read_config


class HorizonTests(unittest.TestCase):
    def test_fixed_reference_normalization_changes_actual_signals(self):
        cfg={**read_config(),'trend_window':120,'mean_window':120,'normalize_signals':True}
        agent=Agent(0,'M',False,41,cfg,1)
        agent.observe(1.,[0.]*120+[1.])
        np.testing.assert_allclose(agent.raw_signals,[1.,-119/121])
        np.testing.assert_allclose(agent.signals,[.5,-58/121])

    def test_reference_window_is_exact_identity(self):
        plain=Agent(0,'M',False,41,read_config(),5)
        normalized=Agent(0,'M',False,41,{**read_config(),'normalize_signals':True},5)
        history=[1.5]*30+[1.6]
        plain.observe(1.6,history)
        normalized.observe(1.6,history)
        np.testing.assert_array_equal(plain.signals,normalized.signals)
