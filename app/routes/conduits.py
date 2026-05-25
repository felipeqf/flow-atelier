"""``/conduits`` REST routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response

from app.core.atelier import Atelier
from app.schemas.api import (
    ConduitDTO,
    CreateConduitInput,
    OpenPathInput,
    UpdateConduitInput,
)
from app.services.api.base import get_atelier

router = APIRouter(prefix="/conduits", tags=["conduits"])


def _error(message: str, code: int) -> JSONResponse:
    """Build a uniform ``{"error": message}`` JSON response.

    :param message: human-readable error message.
    :param code: HTTP status code to return.
    :returns: a :class:`JSONResponse` with the given status and body.
    """
    return JSONResponse(status_code=code, content={"error": message})


@router.get("")
async def list_conduits(atelier: Atelier = Depends(get_atelier)) -> list[dict]:
    """List every conduit visible to the facade (project + global).

    :param atelier: injected :class:`Atelier` facade.
    :returns: serialized :class:`ConduitDTO` dicts for each readable conduit.
    """
    out: list[dict] = []
    for name in atelier.list_conduits():
        try:
            conduit = atelier.store.read_conduit(name)
        except FileNotFoundError:
            continue
        out.append(ConduitDTO.model_validate(conduit.model_dump()).model_dump())
    return out


@router.get("/{name}")
async def get_conduit(name: str, atelier: Atelier = Depends(get_atelier)):
    """Return one conduit by name or 404 if missing.

    :param name: conduit identifier from the URL path.
    :param atelier: injected :class:`Atelier` facade.
    :returns: serialized :class:`ConduitDTO` dict or an error response.
    """
    try:
        conduit = atelier.store.read_conduit(name)
    except FileNotFoundError as e:
        return _error(str(e), 404)
    return ConduitDTO.model_validate(conduit.model_dump()).model_dump()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conduit(
    payload: CreateConduitInput, atelier: Atelier = Depends(get_atelier)
):
    """Create a new conduit, mapping known errors to HTTP status codes.

    :param payload: parsed :class:`CreateConduitInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: serialized :class:`ConduitDTO` dict or an error response.
    """
    try:
        conduit = atelier.create_conduit(payload)
    except FileExistsError as e:
        return _error(str(e), 409)
    except ValueError as e:
        return _error(str(e), 400)
    return ConduitDTO.model_validate(conduit.model_dump()).model_dump()


@router.put("/{name}")
@router.patch("/{name}")
async def update_conduit(
    name: str,
    payload: UpdateConduitInput,
    atelier: Atelier = Depends(get_atelier),
):
    """Patch an existing conduit, mapping known errors to HTTP status codes.

    :param name: conduit identifier from the URL path.
    :param payload: parsed :class:`UpdateConduitInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: serialized :class:`ConduitDTO` dict or an error response.
    """
    try:
        conduit = atelier.update_conduit(name, payload)
    except FileNotFoundError as e:
        return _error(str(e), 404)
    except ValueError as e:
        return _error(str(e), 400)
    return ConduitDTO.model_validate(conduit.model_dump()).model_dump()


@router.delete("/{name}")
async def delete_conduit(name: str, atelier: Atelier = Depends(get_atelier)):
    """Delete a conduit by name, returning 204 on success or 404 if missing.

    :param name: conduit identifier from the URL path.
    :param atelier: injected :class:`Atelier` facade.
    :returns: empty 204 response or an error response.
    """
    if not atelier.delete_conduit(name):
        return _error(f"conduit not found: {name}", 404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/open-path")
async def open_path(
    payload: OpenPathInput, atelier: Atelier = Depends(get_atelier)
) -> dict[str, Any]:
    """Open a path in the host OS file explorer.

    :param payload: parsed :class:`OpenPathInput` body.
    :param atelier: injected :class:`Atelier` facade.
    :returns: ``{"opened": true}`` on success, ``{"opened": false}`` on failure.
    """
    return {"opened": atelier.open_conduit_path(payload.run_path)}
