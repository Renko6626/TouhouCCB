"""Discrete activity feedback; parameters are fixed by the fifth discussion."""
import json
import math
from dataclasses import dataclass
from pathlib import Path


def load_config(path=None):
    return json.loads(Path(path or Path(__file__).parent/'configs/reference.json').read_text())


@dataclass
class ActivityState:
    b: float
    tau: float
    mean_arrivals: float
    psi: float
    z: float = 0.

    def __post_init__(self):
        if not (0 <= self.b < 1 and self.tau > 0 and self.mean_arrivals > 0 and self.psi > 0):
            raise ValueError('invalid feedback parameters')
        self.a = math.exp(-1/self.tau)
        self.s2 = 1-self.a*self.a

    @property
    def intensity(self):
        value = (1-self.b)*self.mean_arrivals+self.b*self.z*self.z
        if not math.isfinite(value):
            raise ValueError('nonfinite activity intensity')
        return value

    def advance(self, realized_return):
        self.z = self.a*self.z+math.sqrt(self.s2)*realized_return/self.psi
        if not math.isfinite(self.z):
            raise ValueError('nonfinite activity state')

    def conditional_z_moments(self):
        """Exact second/fourth next-state moments in the LINEAR executor."""
        lam, az = self.intensity, self.a*self.z
        second = az*az+self.s2*lam
        fourth = az**4+6*az*az*self.s2*lam+3*self.s2**2*(lam+lam*lam)
        return second, fourth


def linear_theory(b, tau, mean_arrivals):
    a = math.exp(-1/tau)
    s2 = 1-a*a
    c = a*a+b*s2
    q4 = a**4+6*a*a*s2*b+3*s2*s2*b*b
    base = (1-b)*mean_arrivals
    z4 = ((6*a*a*s2*base+3*s2*s2*(b+2*base*b))*mean_arrivals
          +3*s2*s2*(base+base*base))/(1-q4) if q4 < 1 else None
    return dict(a=a, s2=s2, c=c, q4=q4,
                intensity_decay_ticks=-1/math.log(c) if b > 0 else None,
                stationary_z2=mean_arrivals, stationary_z4=z4,
                stationary_mean_intensity=mean_arrivals,
                stationary_variance_intensity=b*b*(z4-mean_arrivals**2) if z4 is not None else None,
                intensity_acf_defined=b > 0)
