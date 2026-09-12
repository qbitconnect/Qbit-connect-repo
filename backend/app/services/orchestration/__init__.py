"""QBIT CONNECT — Orchestration & Scraping Intelligence Package."""

from app.services.orchestration.completion import CompletionEngine, CompletionStatus, ProgressSnapshot
from app.services.orchestration.diagnostics import DiagnosticsEngine, FailureCategory, StructuredDiagnostics
from app.services.orchestration.interpreter import InterpretedTask, TaskInterpreter
from app.services.orchestration.orchestrator import ScrapingOrchestrator
from app.services.orchestration.planner import ExecutionPlan, ExecutionPlanner, PlanStep
from app.services.orchestration.tool_registry import ToolCapabilityDefinition, ToolRegistry
from app.services.orchestration.url_analyzer import UrlAnalysisResult, UrlAnalyzer
from app.services.orchestration.validation import ConfidenceScore, RecordQuality, RecordValidator, ValidationResult

__all__ = [
    "CompletionEngine",
    "CompletionStatus",
    "ConfidenceScore",
    "DiagnosticsEngine",
    "ExecutionPlan",
    "ExecutionPlanner",
    "FailureCategory",
    "InterpretedTask",
    "PlanStep",
    "ProgressSnapshot",
    "RecordQuality",
    "RecordValidator",
    "ScrapingOrchestrator",
    "StructuredDiagnostics",
    "TaskInterpreter",
    "ToolCapabilityDefinition",
    "ToolRegistry",
    "UrlAnalysisResult",
    "UrlAnalyzer",
    "ValidationResult",
]
