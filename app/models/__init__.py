"""Models package — import all ORM models so SQLAlchemy metadata is populated."""
from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent, AnalysisCriterionResult
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.drafts import PromptDraft
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.models.typologies import Typology

__all__ = [
    "Prompt",
    "PromptVersion",
    "PromptBaseStructure",
    "PromptCriterion",
    "PromptCriterionTypology",
    "PromptDraft",
    "Analysis",
    "CallAnalysisCurrent",
    "AnalysisResult",
    "AnalysisCriterionResult",
    "MassEvaluationJob",
    "MassEvaluationRun",
    "MassEvaluationResult",
    "MassEvaluationCriterionResult",
    "Service",
    "Typology",
]


