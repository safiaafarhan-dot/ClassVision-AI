"""Health endpoint (Step 7). Reports service state without revealing config."""

from fastapi import APIRouter

from backend.schemas.models import HealthResponse
from backend.services.state import get_data_service

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse,
            summary="Service and database health")
def health():
    """
    Is the dashboard usable?

    Reports only whether the database answers and the schema exists. It
    deliberately reveals no file path, connection string, or setting.
    """
    return get_data_service().health()
