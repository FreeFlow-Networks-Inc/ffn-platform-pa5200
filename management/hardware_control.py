#!/usr/bin/env python3
"""Journal worker for the complete boot-fenced hardware orchestration request."""
import asyncio
import json
import sys
import uuid
from hardware_harness import Harness, installed_adapters, DP_STAGES, DP_RUNTIME


async def execute(action, payload, harness=None):
    harness = harness or Harness(*installed_adapters())
    if action == 'status' and not payload:
        return await harness.status()
    if (action not in ('validate', 'apply') or
            set(payload) != {'revision', 'operation', 'expected_boot_id'} or
            type(payload['revision']) is not int or payload['revision'] != 0 or
            payload['operation'] not in (*DP_STAGES, *DP_RUNTIME) or
            str(uuid.UUID(payload['expected_boot_id'])) != payload['expected_boot_id']):
        raise ValueError('invalid hardware operation')
    if action == 'validate':
        status = await harness.status()
        if not status['dp']['available'] or status['dp']['data'].get('boot_id') != payload['expected_boot_id']:
            raise ValueError('DP unavailable or boot identity changed')
        return {'validated': True}
    method = harness.prepare if payload['operation'] in DP_STAGES else harness.runtime
    return await method(payload['operation'], payload['expected_boot_id'])


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(execute(sys.argv[1], json.load(sys.stdin)))))
    except Exception:
        print(json.dumps({'error': 'Hardware operation rejected or not verified; refresh status'}))
        sys.exit(2)
