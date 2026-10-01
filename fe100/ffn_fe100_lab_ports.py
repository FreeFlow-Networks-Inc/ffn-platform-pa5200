"""Explicit isolated test-port selection; never an appliance configuration.

The supervisor must verify that candidate/running configuration leaves both
ports unused before enabling them. Wiring comes from the board module.
"""
import os
from ffn_faceplate import PORTS


def profile(pair=None):
    if pair is None:
        value=os.environ.get('FFN_FE100_LAB_PAIR','5,13')
        pair=[int(v) for v in value.split(',')]
    if (not isinstance(pair,list) or len(pair)!=2 or len(set(pair))!=2 or
            any(type(p) is not int or not 5<=p<=24 for p in pair)):
        raise ValueError('Two distinct optical lab front ports required')
    return dict(front=list(pair),physical={p:PORTS[p-1] for p in pair},
                lif={pair[0]:2,pair[1]:1} if pair==[5,13] else {pair[0]:28,pair[1]:29})
