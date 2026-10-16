"""Small numerical helpers used by research reports."""
import numpy as np


def correlation(x, y):
    return float(np.corrcoef(x,y)[0,1]) if np.std(x)>0 and np.std(y)>0 else None


def block_mean(x, width=100):
    return x[:len(x)//width*width].reshape(-1,width).mean(axis=1)
