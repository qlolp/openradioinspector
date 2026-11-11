"""Recursive event conversion keeps raw types and extension objects intact."""
from collections import UserDict
from dataclasses import dataclass
from datetime import datetime,timezone
from enum import Enum
import json
import math
import unittest
import numpy as np

from radioinspector.contracts import to_primitive,api_payload
from radioinspector.jsonio import dumps


class EventCodecFidelityTests(unittest.TestCase):
    def test_scalars_enums_dataclasses_mappings_and_numpy_keep_wire_semantics(self):
        class State(str,Enum):
            READY='ready'
        @dataclass
        class Item:
            at:datetime
            state:State
            extension:object
        original=Item(datetime(2026,11,11,tzinfo=timezone.utc),State.READY,
                      UserDict({'levels':(-0.0,1.0000000000000002),'count':2**80,
                                'numpy':np.int64(137),'missing':None,'flag':True}))
        primitive=to_primitive(original)
        self.assertIs(type(primitive['state']),str)
        self.assertEqual(primitive['at'],'2026-11-11T00:00:00Z')
        wire=json.loads(dumps(api_payload(original)))['data']
        self.assertEqual(wire['extension']['count'],2**80)
        self.assertEqual(wire['extension']['numpy'],137)
        self.assertEqual(wire['extension']['levels'][1],1.0000000000000002)
        self.assertEqual(math.copysign(1,wire['extension']['levels'][0]),-1)
        self.assertIs(wire['extension']['flag'],True)
        self.assertIsNone(wire['extension']['missing'])
