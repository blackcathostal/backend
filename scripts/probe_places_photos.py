import asyncio
import json
from pathlib import Path

import httpx

env = {}
for line in Path(r'C:\Users\jesus\Desktop\proyecto_blackcat\backend\.env').read_text(encoding='utf-8').splitlines():
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

API_KEY = env['GOOGLE_PLACES_API_KEY']
PLACE_ID = env.get('GOOGLE_PLACE_ID', '').removeprefix('places/')

async def main():
    print('place_id set?', bool(PLACE_ID), 'len', len(PLACE_ID))
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(
            f'https://places.googleapis.com/v1/places/{PLACE_ID}',
            headers={
                'X-Goog-Api-Key': API_KEY,
                'X-Goog-FieldMask': 'id,displayName,googleMapsUri,photos',
            },
        )
        print('status', r.status_code)
        data = r.json()
        if r.status_code != 200:
            print(data.get('error', data))
            return
        photos = data.get('photos') or []
        print('name', data.get('displayName'))
        print('photos', len(photos))
        travelerish = 0
        business = (data.get('displayName') or {}).get('text') or 'Black Cat'
        for i, p in enumerate(photos):
            attrs = p.get('authorAttributions') or []
            names = [a.get('displayName') or '' for a in attrs]
            is_owner = any(business.lower() in n.lower() or 'black cat' in n.lower() for n in names) or not names
            if not is_owner:
                travelerish += 1
            if i < 25:
                print(i, 'owner?' if is_owner else 'user?', names, f"{p.get('widthPx')}x{p.get('heightPx')}")
        print('travelerish', travelerish, 'of', len(photos))

asyncio.run(main())
