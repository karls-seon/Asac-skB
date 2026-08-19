"""
Graph Assembly (오케스트레이터 담당)

이 파일은 노드 로직을 직접 구현하지 않는다.
각 노드를 import해서 연결 순서와 조건부 라우팅만 정의한다.

노드 담당자는 이 파일을 건드릴 필요 없이 자기 폴더만 수정하면 된다.
그래프 구조(순서, 분기)를 바꿔야 하면 팀 논의 후 이 파일을 수정한다.
"""

from langgraph.graph import StateGraph, END

from ..schemas import GraphState
from ..profiling import user_profiling_node
from ..matching import matching_ranking_node
from ..report import report_node
from .retrieval_step import retrieval_step


def build() -> StateGraph:
    graph = StateGraph(GraphState)
    graph.add_node("profiling", user_profiling_node)
    graph.add_node("retrieval", retrieval_step)
    graph.add_node("matching", matching_ranking_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("profiling")
    graph.add_edge("profiling", "retrieval")
    graph.add_edge("retrieval", "matching")
    graph.add_edge("matching", "report")
    graph.add_edge("report", END)
    return graph


app = build().compile()
