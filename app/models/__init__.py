"""Models package — import all ORM models so SQLAlchemy metadata is populated."""
from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent, AnalysisCriterionResult
from app.models.criteria import PromptCriterion, PromptCriterionTypology, CriteriaSyncLog
from app.models.drafts import PromptDraft
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure, StructurePermission, StructurePermissionAudit, BaseStructureTypology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.models.typologies import Typology
from app.models.users import User, UserAudit, PasswordResetToken
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingSchedulerSetting,
    TrainingCallSession,
    TrainingEvaluationPrompt,
    TrainingCallEvaluation,
)
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)

__all__ = [
    "Prompt",
    "PromptVersion",
    "PromptBaseStructure",
    "BaseStructureTypology",

    "StructurePermission",
    "StructurePermissionAudit",
    "PromptCriterion",
    "PromptCriterionTypology",
    "CriteriaSyncLog",
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
    "User",
    "UserAudit",
    "PasswordResetToken",
    "TrainingAgentSetting",
    "TrainingRun",
    "TrainingAgentReport",
    "TrainingSimulationPrompt",
    "TrainingCompletionStatus",
    "TrainingSchedulerSetting",
    "TrainingCallSession",
    "TrainingEvaluationPrompt",
    "TrainingCallEvaluation",
    "TrainerEvaluationConfig",
    "TrainerSimulation",
    "TrainerSimulationVersion",
    "TrainerSession",
    "TrainerEvaluation",
]



