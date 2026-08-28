import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services import ai_api_sources
from app.services.deepseek_billing import calculate_platform_cost


class AiApiSourcesTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_cost_uses_balance_difference(self):
        self.assertEqual(
            calculate_platform_cost({"usd": 10.0}, {"usd": 9.97}),
            0.03,
        )
        self.assertIsNone(calculate_platform_cost(None, {"usd": 9.97}))

    async def test_ticketmaster_discards_expired_events(self):
        payload = {
            "_embedded": {
                "events": [
                    {
                        "name": "Evento vencido",
                        "dates": {"start": {"dateTime": "2020-01-01T10:00:00Z"}},
                        "_embedded": {"venues": [{"name": "Teatro"}]},
                    },
                    {
                        "name": "Concierto futuro",
                        "dates": {"start": {"dateTime": "2099-01-01T10:00:00Z"}},
                        "_embedded": {
                            "venues": [
                                {
                                    "name": "Teatro Municipal",
                                    "address": {"line1": "Agustinas 794"},
                                    "city": {"name": "Santiago"},
                                }
                            ]
                        },
                        "url": "https://example.com/evento",
                    },
                ]
            }
        }
        with (
            patch.object(settings, "ticketmaster_api_key", "test-key"),
            patch.object(
                ai_api_sources,
                "_get_json",
                new=AsyncMock(return_value=payload),
            ),
        ):
            result = await ai_api_sources.fetch_ticketmaster_events()

        self.assertIsNotNone(result)
        self.assertIn("Concierto futuro", result["text"])
        self.assertNotIn("Evento vencido", result["text"])
        self.assertEqual(result["records"], 1)

    async def test_optional_apis_are_skipped_without_credentials(self):
        with (
            patch.object(settings, "ticketmaster_api_key", ""),
            patch.object(settings, "bandsintown_app_id", ""),
            patch.object(settings, "songkick_api_key", ""),
        ):
            self.assertIsNone(await ai_api_sources.fetch_ticketmaster_events())
            self.assertIsNone(await ai_api_sources.fetch_bandsintown_events())
            self.assertIsNone(await ai_api_sources.fetch_songkick_events())

    async def test_collection_keeps_working_when_one_endpoint_fails(self):
        valid = {"source": "test", "text": "dato verificado", "records": 1}
        with (
            patch.object(ai_api_sources, "fetch_sernatur_attractions", new=AsyncMock(side_effect=RuntimeError)),
            patch.object(ai_api_sources, "fetch_sernatur_network", new=AsyncMock(return_value=valid)),
            patch.object(ai_api_sources, "fetch_open_data_catalog", new=AsyncMock(return_value=None)),
            patch.object(ai_api_sources, "fetch_ticketmaster_events", new=AsyncMock(return_value=None)),
            patch.object(ai_api_sources, "fetch_bandsintown_events", new=AsyncMock(return_value=None)),
            patch.object(ai_api_sources, "fetch_songkick_events", new=AsyncMock(return_value=None)),
            patch.object(ai_api_sources, "fetch_openstreetmap_places", new=AsyncMock(return_value=valid)),
            patch.object(ai_api_sources, "fetch_open_meteo", new=AsyncMock(return_value=valid)),
        ):
            results = await ai_api_sources.collect_external_api_materials()

        self.assertEqual(len(results), 3)
        self.assertEqual({item["source"] for item in results}, {"test"})


if __name__ == "__main__":
    unittest.main()
