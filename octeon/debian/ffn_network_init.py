#!/usr/bin/env python3
"""Initialize missing runtime state with disabled ports; preserve deployed intent."""
import fcntl
import json
import os
import tempfile
from pathlib import Path
import ffn_network as network


def initialize(path=network.STATE, ports=network.MAX_PORTS):
    path = Path(path)
    if path.exists():
        return network.validate(json.loads(path.read_text()))
    if type(ports) is not int or not 1 <= ports <= network.MAX_PORTS:
        raise ValueError('Invalid platform port count')
    config = network.validate({'revision': 0, 'ports': {
        'p%d' % port: {'mode': 'disabled'} for port in range(1, ports + 1)}, 'routes': []})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish complete state without replacing any concurrently created intent.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)
    return config


if __name__ == '__main__':
    with open('/run/ffn-network.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        initialize()
