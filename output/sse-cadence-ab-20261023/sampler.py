"""Use existing SQL/CPU/RSS/queue/lag sampling with unmodified app middleware."""
import os

if os.environ.get('FX_CAPACITY_VARIANT') != 'current':
    raise ValueError('this A/B sampler only permits the real current application')
from fx_capacity_server import app  # noqa: E402,F401
