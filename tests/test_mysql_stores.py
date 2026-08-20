"""五类 MySQL Store 的真实数据库契约和多 Worker 并发测试。"""

from __future__ import annotations

import os
import threading
import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, update

from app.agent_runtime import AgentState, TripOrchestrator
from app.core.config import settings
from app.persistence import MySQLDatabaseConfig, check_mysql_health, create_mysql_engine
from app.persistence.mysql_agent_state_store import MySQLAgentStateStore
from app.persistence.mysql_restaurant_cache import MySQLRestaurantCache
from app.persistence.mysql_route_cache import MySQLRouteCache
from app.persistence.mysql_trip_task_store import MySQLTripTaskStore
from app.persistence.mysql_trip_version_store import MySQLTripVersionStore
from app.persistence.sqlalchemy_models import (
    AgentSessionRow,
    RestaurantCacheRow,
    RouteCacheRow,
    TripDraftRow,
    TripPlanningTaskRow,
    TripPlanVersionRow,
    TripTaskEventRow,
)
from app.providers.amap.models import (
    GeoPoint,
    PoiCandidate,
    RestaurantSearchSnapshot,
    RouteEstimate,
    RouteEstimateResult,
)
from app.routing import build_route_legs, plan_route_fingerprint
from app.schemas.trip_draft_schema import TripDraft
from app.schemas.trip_schema import TripPlan, TripRequest
from app.services import TripDraftService
from app.task_runtime.models import utc_now
from app.tools.registry import ToolRegistry


RUN_MYSQL_TESTS = os.getenv("RUN_MYSQL_INTEGRATION_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def make_request(city: str = "杭州") -> TripRequest:
    return TripRequest(
        city=city,
        start_date="2026-08-20",
        end_date="2026-08-20",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光"],
    )


def make_plan() -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "杭州",
            "start_date": "2026-08-20",
            "end_date": "2026-08-20",
            "days": [
                {
                    "date": "2026-08-20",
                    "day_index": 0,
                    "description": "杭州一日游",
                    "transportation": "公共交通",
                    "accommodation": "经济型酒店",
                    "attractions": [
                        {
                            "name": "西湖",
                            "address": "西湖风景区",
                            "location": {"longitude": 120.15, "latitude": 30.25},
                            "visit_duration": 90,
                            "description": "湖景",
                        }
                    ],
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "合理安排时间",
            "budget": None,
        }
    )


def make_completed_state() -> AgentState:
    request = make_request()
    plan = make_plan()
    legs = build_route_legs(request, plan)
    routes = [
        {
            "day_index": leg.day_index,
            "leg_index": leg.leg_index,
            "leg_type": leg.leg_type,
            "date": leg.date,
            "origin_name": leg.origin.name,
            "destination_name": leg.destination.name,
            "mode": leg.mode,
            "available": True,
            "distance_meters": 800,
            "duration_seconds": 480,
        }
        for leg in legs
    ]
    return AgentState(
        request=request,
        status="completed",
        finished=True,
        trip_plan=plan,
        route_estimates=RouteEstimateResult(
            plan_fingerprint=plan_route_fingerprint(request, plan),
            requested_legs=len(legs),
            evaluated_legs=len(legs),
            routes=routes,
        ).model_dump(mode="json"),
    )


@unittest.skipUnless(RUN_MYSQL_TESTS, "设置 RUN_MYSQL_INTEGRATION_TESTS=1 后运行真实 MySQL Store 测试")
class MySQLStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = MySQLDatabaseConfig.from_settings(
            settings,
            database=settings.MYSQL_TEST_DATABASE,
        )
        cls.engine = create_mysql_engine(cls.config)
        health = check_mysql_health(cls.engine, cls.config)
        if not health.healthy:
            cls.engine.dispose()
            raise unittest.SkipTest(f"MySQL 测试库不可用: {health.error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        # 按外键依赖逆序清空测试库，绝不触碰开发库和 SQLite 数据。
        with self.engine.begin() as connection:
            for table in (
                TripTaskEventRow.__table__,
                TripPlanningTaskRow.__table__,
                TripDraftRow.__table__,
                TripPlanVersionRow.__table__,
                RestaurantCacheRow.__table__,
                RouteCacheRow.__table__,
                AgentSessionRow.__table__,
            ):
                connection.execute(delete(table))

    def test_agent_state_route_restaurant_and_version_contracts(self) -> None:
        state_store = MySQLAgentStateStore(self.engine)
        route_cache = MySQLRouteCache(self.engine)
        restaurant_cache = MySQLRestaurantCache(self.engine)
        version_store = MySQLTripVersionStore(self.engine)

        state = make_completed_state()
        state_store.save_state(state)
        loaded = state_store.get_state(state.session_id)
        self.assertEqual(loaded, state)
        self.assertEqual(state_store.list_sessions()[0].session_id, state.session_id)
        self.assertEqual(state_store.get_execution_baseline().analyzed_session_count, 1)
        self.assertEqual(state_store.get_quality_baseline().analyzed_session_count, 1)

        estimate = RouteEstimate(
            day_index=0,
            leg_index=0,
            date="2026-08-20",
            origin_name="酒店",
            destination_name="西湖",
            mode="transit",
            distance_meters=1200,
            duration_seconds=900,
        )
        route_cache.set("route-key", estimate, ttl_seconds=3600)
        self.assertEqual(route_cache.get("route-key").distance_meters, 1200)

        snapshot = RestaurantSearchSnapshot(
            query_city="杭州",
            keywords="餐厅",
            center=GeoPoint(longitude=120.15, latitude=30.25),
            radius_meters=2000,
            total_received=1,
            candidates=[
                PoiCandidate(
                    poi_id="food-1",
                    name="湖畔餐厅",
                    address="湖滨路1号",
                    location=GeoPoint(longitude=120.151, latitude=30.251),
                )
            ],
        )
        restaurant_cache.set("food-key", snapshot, ttl_seconds=3600)
        self.assertEqual(restaurant_cache.get("food-key").candidates[0].name, "湖畔餐厅")

        service = TripDraftService(
            state_store=state_store,
            version_store=version_store,
            tool_registry=ToolRegistry(),
            orchestrator=TripOrchestrator(tool_registry=ToolRegistry()),
        )
        original = service.ensure_original_version(loaded)
        self.assertEqual(version_store.get_confirmed_version(state.session_id).version_id, original.version_id)
        self.assertEqual(version_store.next_version_number(state.session_id), 2)

        draft = TripDraft(
            draft_id=str(uuid4()),
            session_id=state.session_id,
            base_version=1,
            trip_plan=original.trip_plan,
        )
        version_store.save_draft(draft)
        self.assertEqual(version_store.get_draft(state.session_id, draft.draft_id), draft)

        candidate = original.model_copy(deep=True)
        candidate.version_id = str(uuid4())
        candidate.version_number = 2
        candidate.status = "candidate"
        candidate.source = "draft"
        candidate.source_draft_id = draft.draft_id
        candidate.confirmed_at = None
        version_store.save_version(candidate)
        confirmed = version_store.confirm_version(candidate)
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(version_store.get_version_by_id(original.version_id).status, "superseded")
        self.assertEqual([item.version_number for item in version_store.list_versions(state.session_id)], [2, 1])

    def test_cache_expiry_is_deleted_on_read(self) -> None:
        cache = MySQLRouteCache(self.engine)
        estimate = RouteEstimate(
            day_index=0,
            leg_index=0,
            origin_name="A",
            destination_name="B",
            mode="walking",
            distance_meters=100,
            duration_seconds=60,
        )
        cache.set("expired", estimate, ttl_seconds=3600)
        with self.engine.begin() as connection:
            connection.execute(
                update(RouteCacheRow.__table__)
                .where(RouteCacheRow.cache_key == "expired")
                .values(expires_at=utc_now().replace(tzinfo=None) - timedelta(seconds=1))
            )
        self.assertIsNone(cache.get("expired"))

    def test_task_contract_and_event_atomicity(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        created, reused = store.create_task(make_request(), idempotency_key="mysql-task")
        self.assertFalse(reused)
        same, reused = store.create_task(make_request(), idempotency_key="mysql-task")
        self.assertTrue(reused)
        self.assertEqual(same.task_id, created.task_id)

        claimed = store.claim_next("worker-1", lease_seconds=30)
        self.assertEqual(claimed.task_id, created.task_id)
        progressed = store.record_progress(
            created.task_id,
            worker_id="worker-1",
            event_type="action_started",
            stage="search_attractions",
            stage_name="景点搜索",
            progress_percent=10,
            current_step=1,
            max_steps=40,
            message="开始搜索",
        )
        self.assertEqual(progressed.current_step, 1)
        succeeded = store.mark_succeeded(
            created.task_id,
            "worker-1",
            session_id=created.session_id,
        )
        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(
            [event.event_type for event in store.list_events(created.task_id)],
            ["task_queued", "task_started", "action_started", "task_succeeded"],
        )

    def test_claim_next_is_exclusive_across_workers(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        created, _ = store.create_task(make_request(), idempotency_key="claim-race")
        barrier = threading.Barrier(3)
        results: list[object] = []

        def claim(worker_id: str) -> None:
            barrier.wait()
            results.append(store.claim_next(worker_id, lease_seconds=30))

        threads = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].task_id, created.task_id)

    def test_expired_lease_is_recovered_and_old_worker_is_rejected(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        created, _ = store.create_task(make_request(), idempotency_key="recover")
        store.claim_next("worker-old", lease_seconds=30)
        with self.engine.begin() as connection:
            connection.execute(
                update(TripPlanningTaskRow.__table__)
                .where(TripPlanningTaskRow.task_id == created.task_id)
                .values(lease_expires_at=utc_now().replace(tzinfo=None) - timedelta(seconds=1))
            )
        recovered = store.claim_next("worker-new", lease_seconds=30)
        self.assertEqual(recovered.worker_id, "worker-new")
        self.assertEqual(recovered.recovery_count, 1)
        with self.assertRaisesRegex(RuntimeError, "失去任务"):
            store.assert_worker_owns_task(created.task_id, "worker-old")

    def test_cancelled_queued_task_cannot_be_claimed(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        created, _ = store.create_task(make_request(), idempotency_key="cancel")
        cancelled = store.request_cancel(created.task_id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(store.claim_next("worker", lease_seconds=30))

    def test_concurrent_different_keys_for_same_request_are_deduplicated(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        barrier = threading.Barrier(3)
        results: list[tuple[object, bool]] = []
        errors: list[BaseException] = []

        def create(key: str) -> None:
            try:
                barrier.wait()
                results.append(store.create_task(make_request(), idempotency_key=key))
            except BaseException as exc:  # pragma: no cover - 仅用于跨线程回传断言
                errors.append(exc)

        threads = [
            threading.Thread(target=create, args=("double-click-a",)),
            threading.Thread(target=create, args=("double-click-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({item[0].task_id for item in results}), 1)
        self.assertEqual(sorted(item[1] for item in results), [False, True])

    def test_concurrent_idempotent_create_produces_one_task(self) -> None:
        store = MySQLTripTaskStore(self.engine)
        barrier = threading.Barrier(3)
        results: list[tuple[object, bool]] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                barrier.wait()
                results.append(store.create_task(make_request(), idempotency_key="same-click"))
            except BaseException as exc:  # pragma: no cover - 仅用于跨线程回传断言
                errors.append(exc)

        threads = [threading.Thread(target=create), threading.Thread(target=create)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({item[0].task_id for item in results}), 1)
        self.assertEqual(sorted(item[1] for item in results), [False, True])

