import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.trip_schema import TripPlan


class SharedGuideDomainTests(unittest.TestCase):
    def test_public_request_snapshot_excludes_private_request_fields(self):
        from app.sharing.models import SharedTripRequestSnapshot

        self.assertEqual(
            set(SharedTripRequestSnapshot.model_fields),
            {"city", "travel_days", "transportation", "accommodation", "preferences"},
        )
        self.assertIn("start_date", TripPlan.model_fields)
        self.assertIn("end_date", TripPlan.model_fields)

    def test_record_rejects_invalid_values_and_inconsistent_public_ready_state(self):
        from app.sharing.models import (
            PublicationStatus,
            ShareIndexStatus,
            SharedGuideRecord,
        )

        base = {
            "share_id": "share-1",
            "author_user_id": "user-1",
            "source_session_id": "session-1",
            "source_version_id": "version-1",
            "source_version_number": 1,
            "title": "Hangzhou weekend",
            "city": "Hangzhou",
            "city_normalized": "hangzhou",
            "travel_days": 2,
            "transportation": "transit",
            "accommodation": "hotel",
            "preferences": ["food"],
            "snapshot": {"request": {"city": "Hangzhou", "travel_days": 2, "transportation": "transit", "accommodation": "hotel"}, "trip_plan": {"city": "Hangzhou", "start_date": "2026-08-01", "end_date": "2026-08-02", "days": [], "overall_suggestions": "Enjoy"}},
            "retrieval_text": "Hangzhou two day guide",
            "content_hash": "a" * 64,
            "quality_level": "acceptable",
            "quality_score": 85.0,
            "publication_status": PublicationStatus.PUBLISHING,
            "index_status": ShareIndexStatus.PENDING,
            "embedding_model": "qwen3.7-text-embedding",
            "embedding_dimension": 768,
            "retrieval_template_version": "retrieval_template_v1",
            "index_version": 1,
        }

        for field, value in (("embedding_dimension", 0), ("travel_days", 31), ("quality_score", 101), ("like_count", -1)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SharedGuideRecord(**(base | {field: value}))

        with self.assertRaises(ValidationError):
            SharedGuideRecord(
                **(base | {"publication_status": PublicationStatus.PUBLIC, "index_status": ShareIndexStatus.READY})
            )

        now = datetime.now(timezone.utc)
        record = SharedGuideRecord(
            **(base | {"publication_status": PublicationStatus.PUBLIC, "index_status": ShareIndexStatus.READY, "indexed_at": now, "published_at": now})
        )
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)

        with self.assertRaises(ValidationError):
            SharedGuideRecord(**(base | {"created_at": datetime(2026, 8, 26)}))


class SharedGuideMigrationTests(unittest.TestCase):
    def test_revision_has_expected_parent(self):
        path = Path("migrations/versions/f4c2a81d9e30_add_shared_guides_and_rag_jobs.py")
        spec = importlib.util.spec_from_file_location("shared_guide_migration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(module.revision, "f4c2a81d9e30")
        self.assertEqual(module.down_revision, "d9f4b2c7a861")


if __name__ == "__main__":
    unittest.main()
