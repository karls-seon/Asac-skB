from __future__ import annotations

import re

from app.schemas.user_profile import UserProfile


class RuleBasedUserAnalyzer:
    """
    팀의 실제 LLM User Analysis Agent가 들어오기 전까지 사용하는 baseline.
    자연어 전체를 완벽히 이해하려는 목적이 아니라 Graph 통합 테스트용이다.
    """

    def analyze(self, user_query: str) -> tuple[UserProfile, list[str], str | None]:
        q = user_query.replace(",", "")
        profile = UserProfile()

        # 예산: "3만원 이하", "30000원"
        m = re.search(r"(\d+(?:\.\d+)?)\s*만원", q)
        if m:
            profile.budget_krw = int(float(m.group(1)) * 10000)
        else:
            m = re.search(r"(\d{4,6})\s*원", q)
            if m:
                profile.budget_krw = int(m.group(1))

        # 데이터: "20GB", "30기가"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gb|기가)", q, flags=re.I)
        if m:
            profile.monthly_data_gb = float(m.group(1))

        # QoS
        m = re.search(r"(\d+(?:\.\d+)?)\s*mbps", q, flags=re.I)
        if m:
            profile.min_qos_mbps = float(m.group(1))

        # carrier
        if "알뜰폰" in q or "mvno" in q.lower():
            profile.carrier_type = "MVNO"

        if "kt망" in q.lower() or "kt 망" in q.lower():
            profile.preferred_carrier = "KT"
        elif "skt망" in q.lower() or "skt 망" in q.lower():
            profile.preferred_carrier = "SKT"
        elif "lgu+" in q.lower() or "lg u+" in q.lower() or "lg유플러스" in q.lower():
            profile.preferred_carrier = "LGU+"

        if "5g" in q.lower():
            profile.network_gen = "5G"
        elif "lte" in q.lower():
            profile.network_gen = "LTE"

        # unlimited
        if "통화 무제한" in q:
            profile.voice_unlimited = True
        if "문자 무제한" in q:
            profile.sms_unlimited = True

        known_benefits = [
            "넷플릭스", "유튜브 프리미엄", "디즈니+", "티빙",
            "웨이브", "밀리의서재", "지니뮤직", "테더링",
        ]
        profile.preferred_benefits = [
            benefit for benefit in known_benefits if benefit.lower() in q.lower()
        ]

        # 정책: 추천 자체는 예산/데이터가 없어도 가능.
        # 정보가 전혀 없는 매우 포괄적인 요청만 보완 질문.
        meaningful = any([
            profile.budget_krw is not None,
            profile.monthly_data_gb is not None,
            profile.preferred_carrier is not None,
            profile.carrier_type is not None,
            profile.network_gen is not None,
            bool(profile.preferred_benefits),
            profile.voice_unlimited is not None,
            profile.sms_unlimited is not None,
        ])

        if not meaningful:
            missing = ["recommendation_preferences"]
            question = (
                "원하는 월 예산, 데이터 사용량, 선호 통신망(SKT/KT/LGU+), "
                "알뜰폰 여부 중 하나 이상을 알려주세요."
            )
        else:
            missing = []
            question = None

        return profile, missing, question
