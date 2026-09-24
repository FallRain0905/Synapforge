"""竞赛领域包（competition packs）。

阶段 6 的 CUMCM 领域工作流包落在这里：manifest、四问任务 DAG、模板与 schema。
平台通用内核不感知竞赛细节，只有本包知道 CUMCM 的字段与验收规则。
"""

from .loader import (
    CompetitionPack,
    CompetitionPackError,
    DagTask,
    PackManifest,
    TemplateSpec,
    UpgradePlan,
    UpgradeRule,
    available_pack_ids,
    list_packs,
    load_pack,
    parse_version,
    resolve_pack_id,
)
from .materializer import (
    MaterializedArtifact,
    MaterializedTask,
    PackMaterialization,
    PackMaterializationError,
    PackMaterializer,
)
from .validation import (
    PLATFORM_ARTIFACT_TYPES,
    PackValidator,
    ValidationFinding,
    ValidationReport,
)

__all__ = [
    "CompetitionPack",
    "CompetitionPackError",
    "DagTask",
    "MaterializedArtifact",
    "MaterializedTask",
    "PLATFORM_ARTIFACT_TYPES",
    "PackManifest",
    "PackMaterialization",
    "PackMaterializationError",
    "PackMaterializer",
    "PackValidator",
    "TemplateSpec",
    "UpgradePlan",
    "UpgradeRule",
    "ValidationFinding",
    "ValidationReport",
    "available_pack_ids",
    "list_packs",
    "load_pack",
    "parse_version",
    "resolve_pack_id",
]
