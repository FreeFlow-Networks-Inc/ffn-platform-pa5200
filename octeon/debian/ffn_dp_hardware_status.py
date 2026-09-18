#!/usr/bin/env python3
"""Read live DP agent and packet-engine status without changing the agent."""
import json
import uuid
from ffn_dp_agent import snapshot
from ffn_dp_packet_init import status

result=snapshot(str(uuid.uuid4()))
try: result['packet_initialization']=status()
except (OSError,ValueError,RuntimeError):
    result['packet_initialization']={'available':False}
print(json.dumps(result))
