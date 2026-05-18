"""Models package — import all ORM models so SQLAlchemy metadata is populated."""
from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.models.criteria import PromptCriterion
from app.models.drafts import PromptDraft
from app.models.prompts import Prompt, PromptVersion

__all__ = [
    "Prompt",
    "PromptVersion",
    "PromptCriterion",
    "PromptDraft",
    "Analysis",
    "CallAnalysisCurrent",
    "AnalysisResult",
]
