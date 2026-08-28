import asyncio
from pathlib import Path
import httpx

env = {}
for line in Path(r'C:\Users\jesus\Desktop\proyecto_blackcat\backend\.env').read_text(encoding='utf-8').splitlines():
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

KEY = env['GOOGLE_PLACES_API_KEY']

async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Legacy Find Place
        r = await client.get(
            'https://maps.googleapis.com/maps/api/place/findplacefromtext/json',
            params={
                'input': 'Black Cat Hostal Boutique Compañía de Jesús 1921 Santiago',
                'inputtype': 'textquery',
                'fields': 'place_id,name,photos',
                'key': KEY,
            },
        )
        print('legacy find', r.status_code, r.json().get('status'), 'candidates', len(r.json().get('candidates') or []))
        data = r.json()
        if data.get('candidates'):
            c = data['candidates'][0]
            print('name', c.get('name'))
            print('place_id_len', len(c.get('place_id') or ''))
            print('photos', len(c.get('photos') or []))
            pid = c['place_id']
            # details with reviews and photos
            r2 = await client.get(
                'https://maps.googleapis.com/maps/api/place/details/json',
                params={
                    'place_id': pid,
                    'fields': 'name,photos,url,user_ratings_total,rating',
                    'key': KEY,
                },
            )
            d2 = r2.json()
            print('details', d2.get('status'), 'photos', len((d2.get('result') or {}).get('photos') or []))
            photos = (d2.get('result') or {}).get('photos') or []
            for i, p in enumerate(photos[:15]):
                print(i, p.get('html_attributions'), p.get('width'), 'x', p.get('height'))
            Path('tmp_place_id.txt').write_text(pid, encoding='utf-8')
            print('wrote place_id file')

asyncio.run(main())
