"""Home Assistant transport used by the controller.

This module contains transport only; it deliberately owns no battery or tariff policy.
"""
import asyncio
import json
import logging
import os

import aiohttp

LOG = logging.getLogger('home_energy_controller')


class HA:
    def __init__(self):
        t = os.environ.get('SUPERVISOR_TOKEN')
        if not t:
            raise RuntimeError('SUPERVISOR_TOKEN missing')
        self.t = t
        self.s = None
        self.base = 'http://supervisor/core/api'
        self.ws = 'ws://supervisor/core/websocket'

    async def open(self):
        self.s = aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + self.t})

    async def close(self):
        await self.s.close()

    async def state(self, e):
        if not e:
            return None
        try:
            async with self.s.get(f'{self.base}/states/{e}', timeout=15) as r:
                return await r.json() if r.status == 200 else None
        except Exception:
            return None

    async def states(self):
        try:
            async with self.s.get(f'{self.base}/states', timeout=20) as r:
                return await r.json() if r.status == 200 else []
        except Exception:
            return []

    async def publish(self, e, state, attrs):
        async with self.s.post(
            f'{self.base}/states/{e}', json={'state': state, 'attributes': attrs}, timeout=15
        ) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f'publish HTTP {r.status}')

    async def write(self, e, v):
        d = e.split('.', 1)[0]
        data = {'entity_id': e}
        if d == 'switch':
            service = 'turn_on' if str(v).lower() in ('on', 'true', '1') else 'turn_off'
        elif d in ('number', 'input_number'):
            service = 'set_value'
            data['value'] = float(v)
        elif d in ('select', 'input_select'):
            service = 'select_option'
            data['option'] = str(v)
        elif d == 'time':
            service = 'set_value'
            data['time'] = str(v)
        elif d == 'input_datetime':
            service = 'set_datetime'
            data['time'] = str(v)
        else:
            raise ValueError('Unsupported writable entity domain ' + d)
        async with self.s.post(f'{self.base}/services/{d}/{service}', json=data, timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f'{d}.{service} HTTP {r.status}')

    async def events(self, ready=None):
        while True:
            try:
                async with self.s.ws_connect(self.ws, heartbeat=30) as ws:
                    m = await ws.receive_json()
                    if m.get('type') == 'auth_required':
                        await ws.send_json({'type': 'auth', 'access_token': self.t})
                        m = await ws.receive_json()
                    if m.get('type') != 'auth_ok':
                        raise RuntimeError('WS auth failed')
                    await ws.send_json({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'})
                    ack = await ws.receive_json()
                    if ack.get('type') != 'result' or not ack.get('success', False):
                        raise RuntimeError('WS event subscription failed')
                    if ready is not None:
                        ready.set()
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            j = json.loads(m.data)
                            if j.get('type') == 'event':
                                yield j['event']['data']
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning('WebSocket reconnect: %s', e)
                await asyncio.sleep(5)
