import hashlib
import unittest

from app.rag.models import BuiltRetrievalText, RagReference
from app.rag.text_builder import EmbeddingTextBuilder
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest
from app.sharing.models import SharedGuideSnapshot, SharedTripRequestSnapshot


def beijing_snapshot(*, free_text_input: str = "") -> SharedGuideSnapshot:
    plan = TripPlan.model_validate(
        {
            "city": "北京市",
            "start_date": "2026-08-01",
            "end_date": "2026-08-03",
            "days": [
                {
                    "date": "2026-08-01",
                    "day_index": 0,
                    "description": "游览故宫和景山公园，重点体验明清历史文化。",
                    "transportation": "地铁",
                    "accommodation": "经济型酒店",
                    "attractions": [
                        {"name": "故宫", "address": "x", "location": {"longitude": 1, "latitude": 2}, "visit_duration": 90, "description": "x", "poi_id": "poi-1", "image_url": "https://secret/image", "ticket_price": 80},
                        {"name": "景山公园", "address": "x", "location": {"longitude": 3, "latitude": 4}, "visit_duration": 60, "description": "x", "poi_id": "poi-2", "ticket_price": 2},
                        {"name": "故宫", "address": "x", "location": {"longitude": 5, "latitude": 6}, "visit_duration": 90, "description": "x", "poi_id": "poi-1b"},
                    ],
                    "meals": [],
                },
                {
                    "date": "2026-08-02",
                    "day_index": 1,
                    "description": "游览天坛和前门，安排北京特色餐饮。",
                    "transportation": "公交",
                    "accommodation": "经济型酒店",
                    "attractions": [{"name": "天坛", "address": "x", "location": {"longitude": 7, "latitude": 8}, "visit_duration": 90, "description": "x", "poi_id": "poi-3"}, {"name": "前门", "address": "x", "location": {"longitude": 9, "latitude": 10}, "visit_duration": 60, "description": "x", "poi_id": "poi-4"}],
                    "meals": [],
                },
                {
                    "date": "2026-08-03",
                    "day_index": 2,
                    "description": "",
                    "transportation": "驾车",
                    "accommodation": "经济型酒店",
                    "attractions": [{"name": "南锣鼓巷", "address": "x", "location": {"longitude": 11, "latitude": 12}, "visit_duration": 90, "description": "x", "poi_id": "poi-5"}, {"name": "什刹海", "address": "x", "location": {"longitude": 13, "latitude": 14}, "visit_duration": 90, "description": "x", "poi_id": "poi-6"}],
                    "meals": [],
                },
            ],
            "weather_info": [{"date": "2026-08-01", "day_weather": "雨"}],
            "overall_suggestions": "景点预约应提前完成，市区内优先乘坐地铁。",
            "budget": {"total": 9999},
        }
    )
    return SharedGuideSnapshot(
        request=SharedTripRequestSnapshot(city="北京市", travel_days=3, transportation="地铁", accommodation=" 经济型酒店 ", preferences=["美食", "历史文化", "美食"]),
        trip_plan=plan,
    )


class RetrievalTextBuilderTests(unittest.TestCase):
    def test_document_is_byte_for_byte_canonical(self):
        built = EmbeddingTextBuilder().build_document(beijing_snapshot())
        expected = """文档类型：公开旅行攻略
目的地：北京
旅行天数：3天
主要交通：公共交通
住宿偏好：经济型酒店
旅行偏好：历史文化、美食
主要景点：故宫、景山公园、天坛、前门、南锣鼓巷、什刹海

每日摘要：
第1天：游览故宫和景山公园,重点体验明清历史文化。
第2天：游览天坛和前门,安排北京特色餐饮。
第3天：游览南锣鼓巷、什刹海。

总体建议：
景点预约应提前完成,市区内优先乘坐地铁。"""
        self.assertEqual(built.text, expected)
        self.assertEqual(built.content_hash, hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(built.city_normalized, "北京")
        self.assertEqual(built.transportation_normalized, "公共交通")
        self.assertEqual(built.template_version, "retrieval_template_v1")

    def test_normalization_aliases_and_privacy_boundary(self):
        request = TripRequest(city="重 市 庆", start_date="2026-01-01", end_date="2026-01-02", travel_days=2, transportation="地铁", accommodation=" hotel\n", preferences=["  美食 ", "历史文化", "美食"], free_text_input="<b>博物馆</b>\u0000")
        query = EmbeddingTextBuilder().build_query(request, selected_attractions=[" 故宫 ", "故宫"])
        self.assertIn("目的地：重 市 庆", query)
        self.assertIn("主要交通：公共交通", query)
        self.assertIn("额外要求：博物馆", query)
        self.assertIn("用户明确选择景点：故宫", query)
        document = EmbeddingTextBuilder().build_document(beijing_snapshot()).text
        for secret in ("2026-08", "雨", "9999", "https://", "poi-", "author", "like"):
            self.assertNotIn(secret, document)

    def test_query_transport_aliases_and_omits_unsupplied_selected_attractions(self):
        builder = EmbeddingTextBuilder()
        for alias, normalized in (("公交", "公共交通"), ("驾车", "自驾")):
            with self.subTest(alias=alias):
                request = TripRequest(
                    city="北京",
                    start_date="2026-01-01",
                    end_date="2026-01-02",
                    travel_days=2,
                    transportation=alias,
                    accommodation="酒店",
                    preferences=[],
                )
                query = builder.build_query(request)
                self.assertIn(f"主要交通：{normalized}", query)
                self.assertNotIn("用户明确选择景点：", query)

    def test_empty_description_fallback_is_bounded_and_deterministic_with_many_attractions(self):
        snapshot = beijing_snapshot()
        snapshot.trip_plan.days[2].attractions = [
            Attraction.model_validate({
                "name": f"景点{i:02d}" + "很长的景点名称" * 12,
                "address": "x",
                "location": {"longitude": i, "latitude": i},
                "visit_duration": 90,
                "description": "x",
                "poi_id": f"many-{i}",
            })
            for i in range(61)
        ]
        builder = EmbeddingTextBuilder()
        first = builder.build_document(snapshot)
        second = builder.build_document(snapshot)
        summary_line = next(line for line in first.text.splitlines() if line.startswith("第3天："))
        self.assertLessEqual(len(summary_line), 500)
        self.assertTrue(summary_line.endswith("。"))
        self.assertEqual(first.text, second.text)
        self.assertLessEqual(len(first.text), 12000)

    def test_query_does_not_accept_candidate_pool(self):
        request = TripRequest(city="北京", start_date="2026-01-01", end_date="2026-01-02", travel_days=2, transportation="公交", accommodation="酒店", preferences=[])
        with self.assertRaises(TypeError):
            EmbeddingTextBuilder().build_query(request, amap_candidates=["不应接受"])

    def test_prompt_payload_contains_only_public_reference_fields(self):
        reference = RagReference(
            share_id="secret-internal-id",
            title="北京三日攻略",
            city="北京",
            travel_days=3,
            transportation="公共交通",
            preferences=["历史文化"],
            attraction_names=["故宫"],
            daily_summaries=["第1天：游览故宫。"],
            overall_suggestions="提前预约。",
            vector_score=0.99,
            final_score=0.98,
        )
        self.assertNotIn("share_id", reference.prompt_payload())
        self.assertNotIn("vector_score", reference.prompt_payload())
        self.assertNotIn("final_score", reference.prompt_payload())
        self.assertEqual(reference.prompt_payload()["attraction_names"], ["故宫"])

    def test_long_fields_are_deterministic_and_bounded(self):
        snapshot = beijing_snapshot()
        snapshot.request.city = "北" * 1000
        snapshot.trip_plan.overall_suggestions = "建议" * 2000
        first = EmbeddingTextBuilder().build_document(snapshot)
        second = EmbeddingTextBuilder().build_document(snapshot)
        self.assertLessEqual(len(first.text), 12000)
        self.assertEqual(first.text, second.text)
        self.assertIsInstance(first, BuiltRetrievalText)


if __name__ == "__main__":
    unittest.main()
