"""Production defaults copied from the frozen research handoff; no research imports."""
SOURCE_PATH = "research/fx/reversion/repeated/FROZEN_CONFIG.json"
SOURCE_SHA256 = "b834898530f3f1fd69ab3b65bc713f0fac784f3d70cae5bbc10c0284bf54d3b0"
TICK_SECONDS = 5
FROZEN_PRESET = {"name": "pve-mixed-20-5s-v1", "items": [{"template": "random", "count": 10}, {"template": "inventory", "count": 5}, {"template": "short_episode", "count": 5}], "initial_cash": "5000", "initial_foreign": "1000"}
RANDOM_DEFAULTS = {"order_sigma_foreign": 120}
INVENTORY_DEFAULTS = {"inventory_window_seconds": 1800, "inventory_position_cap": 500, "inventory_sensitivity": 100, "inventory_adjustment": 0.5}
SHORT_EPISODE_DEFAULTS = {"trend_window_seconds": 150, "mean_window_seconds": 150, "trend_multiplier": 0.25, "entry_quantity": 200, "take_profit": 0.004, "stop_loss": 0.005, "trail_arm": 0.002, "trail_drawdown": 0.002}
# Assign by creation order modulo five and persist in params.
SHORT_EPISODE_PROFILES = ({'trend': 2.5189933783708245, 'reversion': 0.5644146005950922, 'sigma': 0.0004917684747965548}, {'trend': 61.1653174518216, 'reversion': 0.006878739586751304, 'sigma': 0.0009584245060764243}, {'trend': 4.797793852721078, 'reversion': 0.4998790835121927, 'sigma': 0.00022628828257235847}, {'trend': 46.83198243991382, 'reversion': 0.3065589009950954, 'sigma': 0.0004914244646153101}, {'trend': 0.4523081744537012, 'reversion': 0.1459158062552556, 'sigma': 0.0005440083729149102})
