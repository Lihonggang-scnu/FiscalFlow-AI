"""Start a workbench and open it in the default browser."""

from __future__ import annotations

import getpass
import json
import os
import runpy
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))


def configure_model(workbench: str) -> None:
    from taxharness.provider import credential_status, provider_config

    config = provider_config()
    if not credential_status(config).get("format_valid"):
        key = getpass.getpass("输入模型 API Key（直接回车进入工作台）：").strip()
        if key:
            os.environ[config.api_key_env] = key
    if not credential_status(config).get("format_valid"):
        os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "false"
        os.environ["TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED"] = "false"
        os.environ["TAXHARNESS_UI_BUDGET_USD"] = "0"
        return

    print(f"当前模型：{config.model}")
    budget_text = input("请输入模型累计预算（人民币，默认 1 元，输入 0 关闭模型）：").strip() or "1"
    budget_cny = float(budget_text)
    if budget_cny < 0:
        raise ValueError("模型预算需要大于或等于 0。")
    if budget_cny == 0:
        os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "false"
        os.environ["TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED"] = "false"
        os.environ["TAXHARNESS_UI_BUDGET_USD"] = "0"
        return

    limit = round(budget_cny * 0.3, 6)
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"] = "true"
    os.environ.setdefault("TAXHARNESS_OFFPEAK_ONLY", "false")
    os.environ["TAXHARNESS_UI_BUDGET_USD"] = str(limit)
    if workbench == "enterprise":
        budget_path = APP / "artifacts" / "enterprise-budget.json"
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        budget_path.write_text(json.dumps({"status": "active", "currency": "CNY", "model": config.model,
                                           "new_cumulative_ledger_limit": limit}), encoding="utf-8")
        os.environ["TAXHARNESS_ENTERPRISE_BUDGET"] = str(budget_path)


def open_when_ready(port: int) -> None:
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        except OSError:
            time.sleep(0.25)
    print("工作台启动中，请查看运行窗口中的提示。")


def main() -> None:
    workbench = sys.argv[1]
    if workbench not in {"enterprise", "chat"}:
        raise ValueError("请选择 enterprise 或 chat 工作台。")
    os.chdir(APP)
    port_variable = "TAXHARNESS_ENTERPRISE_PORT" if workbench == "enterprise" else "TAXHARNESS_DIALOG_PORT"
    port = int(os.getenv(port_variable, "8011" if workbench == "enterprise" else "8010"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    configure_model(workbench)

    if workbench == "chat":
        from taxharness.storage import initialize_database

        database = APP / "artifacts" / "chainlit" / "live-business.sqlite3"
        if not database.exists():
            initialize_database(database, APP / "data" / "02_示例数据.xlsx", APP / "data" / "03_业务规则.docx.txt")
        os.environ["TAXHARNESS_DATABASE"] = str(database)
        sys.argv = ["chainlit", "run", str(APP / "scripts" / "chainlit_app.py"),
                    "--headless", "--host", "127.0.0.1", "--port", str(port)]
    print("工作台正在启动，完成后将自动打开浏览器。关闭此窗口即可停止。")
    threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()
    if workbench == "enterprise":
        runpy.run_path(str(APP / "scripts" / "enterprise_app.py"), run_name="__main__")
    else:
        runpy.run_module("chainlit", run_name="__main__")


if __name__ == "__main__":
    main()
