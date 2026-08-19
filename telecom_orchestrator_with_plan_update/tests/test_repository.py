from pathlib import Path

from app.repositories.plan_repository import PlanRepository
from app.schemas.user_profile import UserProfile


ROOT = Path(__file__).resolve().parents[1]


def make_repo():
    return PlanRepository(
        ROOT / "data" / "plans.csv",
        ROOT / "data" / "benefits.csv",
    )


def test_budget_and_carrier_filter():
    repo = make_repo()
    profile = UserProfile(
        budget_krw=30000,
        carrier_type="MVNO",
        preferred_carrier="KT",
    )

    candidates = repo.find_candidates(profile, limit=100)

    assert candidates
    for plan in candidates:
        assert plan.carrier_type == "MVNO"
        assert plan.host_mno == "KT"
        fee = plan.discounted_fee if plan.discounted_fee is not None else plan.monthly_fee
        assert fee is not None
        assert fee <= 30000


def test_qos_parser_is_exposed_in_candidates():
    repo = make_repo()
    profile = UserProfile(min_qos_mbps=3)
    candidates = repo.find_candidates(profile, limit=50)
    assert candidates
