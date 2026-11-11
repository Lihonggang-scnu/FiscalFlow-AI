"""HTTP endpoints for approved delivery versions."""

from __future__ import annotations

from fastapi import Body, Depends
from fastapi.responses import Response

from .delivery import DeliveryService
from .storage import EnterpriseDatabase


def install_delivery_routes(app, get_actor, get_mutating_actor, get_database, fail) -> None:
    @app.get("/api/work-items/{task_id}/delivery-preview")
    def preview(task_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return DeliveryService(database).preview(user=state[0], task_id=task_id)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/deliveries")
    def list_deliveries(task_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return {"items": DeliveryService(database).list(user=state[0], task_id=task_id)}
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/deliveries")
    def create(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return DeliveryService(database).create(user=actor, task_id=task_id, request_key=payload.get("request_key"),
                                                    invoice_ids=payload.get("invoice_ids"))
        except Exception as error:
            fail(error)

    @app.get("/api/deliveries/{delivery_id}/download")
    def download(delivery_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            name, content = DeliveryService(database).download(user=state[0], delivery_id=delivery_id)
            return Response(content=content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
        except Exception as error:
            fail(error)
