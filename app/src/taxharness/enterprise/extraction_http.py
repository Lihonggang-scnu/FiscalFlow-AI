"""HTTP and worker wiring for durable reply extraction."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import Body, Depends

from .extraction_queue import ExtractionQueue
from .storage import EnterpriseDatabase


@asynccontextmanager
async def extraction_lifespan(app):
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            try:
                with EnterpriseDatabase(app.state.enterprise_database) as database:
                    from .mandates import WorkMandates
                    WorkMandates(database).advance_active()
                    result = ExtractionQueue(database).run_once(
                        model=app.state.enterprise_model, vision_model=app.state.enterprise_vision_model,
                        live_authorized=os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true",
                        budget_path=os.getenv("TAXHARNESS_ENTERPRISE_BUDGET"), ledger_path=app.state.agent_store)
                app.state.extraction_worker_error = None
                if result is not None and result.get("state") != "queued":
                    continue
            except Exception as error:
                app.state.extraction_worker_error = type(error).__name__
            stop.wait(1.0)

    worker = None
    if os.getenv("TAXHARNESS_EXTRACTION_WORKER_ENABLED", "true").lower() == "true":
        worker = threading.Thread(target=work, name="fiscalflow-extraction", daemon=True)
        app.state.extraction_worker = worker
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker:
            worker.join(timeout=2)


def install_extraction_routes(app, get_actor, get_mutating_actor, get_database, fail) -> None:
    @app.post("/api/responses/{response_id}/extractions")
    def enqueue(response_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ExtractionQueue(database).enqueue(user=actor, response_id=response_id)
        except Exception as error:
            fail(error)

    @app.get("/api/requests/{request_id}/extractions")
    def list_runs(request_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return {"items": ExtractionQueue(database).list_request(user=state[0], request_id=request_id)}
        except Exception as error:
            fail(error)

    @app.get("/api/extractions/{run_id}")
    def get_run(run_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ExtractionQueue(database).get(user=state[0], run_id=run_id)
        except Exception as error:
            fail(error)

    @app.post("/api/extractions/{run_id}/cancel")
    def cancel(run_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ExtractionQueue(database).cancel(user=actor, run_id=run_id)
        except Exception as error:
            fail(error)

    @app.post("/api/extractions/{run_id}/retry")
    def retry(run_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ExtractionQueue(database).retry(user=actor, run_id=run_id)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/mandates")
    def list_mandates(task_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .mandates import WorkMandates
            return {"items": WorkMandates(database).list(user=state[0], task_id=task_id)}
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/mandates")
    def create_mandate(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .mandates import WorkMandates
            return WorkMandates(database).create(user=actor, task_id=task_id, valid_until=payload.get("valid_until"),
                max_steps_per_run=payload.get("max_steps_per_run", 10), budget_ref=app.state.agent_store)
        except Exception as error:
            fail(error)

    @app.post("/api/mandates/{mandate_id}/action")
    def change_mandate(mandate_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .mandates import WorkMandates
            return WorkMandates(database).change(user=actor, mandate_id=mandate_id, action=payload.get("action"),
                                                 expected_revision=payload.get("expected_revision"))
        except Exception as error:
            fail(error)
