"""AI-assisted CI / Pre-Merge Validation public API。"""

from app.validation.ai_tests import AITestGenerator, TestFileValidator
from app.validation.comparator import ValidationComparator
from app.validation.interfaces import InterfaceDiscovery
from app.validation.project import AdapterRegistry, ProjectDetector
from app.validation.repository import ValidationRepository
from app.validation.runner import DockerValidationRunner, RunnerLimits
from app.validation.schema import initialize_validation_schema
from app.validation.service import ValidationService

__all__ = ["AITestGenerator", "AdapterRegistry", "DockerValidationRunner", "InterfaceDiscovery", "ProjectDetector",
           "RunnerLimits", "TestFileValidator", "ValidationComparator", "ValidationRepository",
           "ValidationService", "initialize_validation_schema"]
