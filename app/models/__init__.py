"""Models package — import all ORM models so SQLAlchemy metadata is populated."""
from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.drafts import PromptDraft
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
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
    "MassEvaluationJob",
    "MassEvaluationRun",
    "MassEvaluationResult",
    "Service",
    "Typology",
]


