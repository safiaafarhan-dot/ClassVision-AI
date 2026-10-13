"""
The assistant: questions in, analytics figures out.

Two endpoints and no state. Asking a question changes nothing, so both are
reads in every sense that matters - `ask` is a POST only because a question is
a body rather than a path, and it is explicitly marked as reading nothing it
could modify.

Scope is the caller's own, enforced by passing the Principal down to the
service, which passes it to DataService. The assistant has no privileged view:
a teacher asking about a class that is not theirs is answered exactly as they
would be about a class that does not exist.
"""

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from backend.services.assistant_service import AssistantService
from backend.services.authz import Principal, require_user
from backend.services.state import get_database

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _service():
    return AssistantService(get_database())


@router.get("/capabilities",
            summary="Every question the assistant can answer")
def capabilities(principal: Principal = Depends(require_user)):
    """
    The closed list, published.

    A router that answers a fixed set of questions owes the person asking the
    list, and the same list is what an unrecognised question is answered with
    rather than a guess.
    """
    return _service().capabilities()


@router.post("/ask", summary="Ask a question about recorded attendance",
             responses={200: {"description": "An answer, or a plain "
                                             "statement that the question was "
                                             "not understood"}})
def ask(payload: Dict[str, Any] = Body(...),
        principal: Principal = Depends(require_user)):
    """
    Answer by CALLING the analytics functions - never by computing a figure.

    A question that was not understood still returns 200 with
    `understood: false`. It is not a client error: the request was perfectly
    well formed, and the honest answer is that this assistant does not handle
    that phrasing. Returning 400 would invite a caller to retry the same
    question differently and get the same non-answer with an error attached.

    Every answer carries `sources`, naming the analytics function behind each
    figure and the dashboard page that shows the same number.
    """
    answer = _service().ask(payload.get("question"), principal=principal)
    return answer.as_dict()
