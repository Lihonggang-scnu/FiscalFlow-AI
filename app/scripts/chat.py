"""Interactive console entry for the natural-language tax assistant."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from taxharness.agent import AgentRuntime
from taxharness.errors import TaxHarnessError
from taxharness.storage import database_initialized


def _path_env(name: str, default: Path | None = None) -> Path | None:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


def main() -> int:
    parser = argparse.ArgumentParser(description="连续输入中文，与财税助手办理业务。")
    parser.add_argument("--user-id", default="U001", help="模拟用户，默认 U001")
    parser.add_argument("--company-id", default="C001", help="新会话的起始企业，默认 C001")
    parser.add_argument("--session-id", help="可选：继续一个已有对话会话")
    parser.add_argument("--database", type=Path, default=PROJECT / "artifacts/runtime/taxharness.sqlite3")
    parser.add_argument("--agent-store", type=Path, default=_path_env("TAXHARNESS_AGENT_STORE", PROJECT / "artifacts/agent/agent.sqlite3"), help="Agent 账本路径")
    parser.add_argument("--checkpoint-store", type=Path, default=_path_env("TAXHARNESS_CHECKPOINT_STORE", PROJECT / "artifacts/agent/agent_checkpoints.sqlite3"), help="Agent checkpoint 路径")
    parser.add_argument("--preference-store", type=Path, default=_path_env("TAXHARNESS_PREFERENCE_STORE"), help="长期偏好存储路径；省略时按数据库路径推导")
    parser.add_argument("--budget-usd", type=float, default=2.0, help="项目累计调用费用上限，默认 2 美元")
    args = parser.parse_args()

    if not database_initialized(args.database):
        print("无法开始办理：业务数据库尚未初始化。请先运行：", file=sys.stderr)
        print(f"  {sys.executable} -m taxharness.cli init-db --database {args.database} --source-xlsx data/02_示例数据.xlsx --rules-doc data/03_业务规则.docx.txt", file=sys.stderr)
        return 1

    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    runtime = AgentRuntime(
        database_path=args.database,
        agent_store_path=args.agent_store,
        checkpoint_path=args.checkpoint_store,
        preference_store_path=args.preference_store,
    )
    session_id = args.session_id or "chat-" + uuid4().hex
    current_company = args.company_id
    try:
        existing = runtime.agent_store.session(session_id, args.user_id)
        if existing:
            current_company = existing["current_company_id"]
        print(f"财税助手 · DeepSeek-V4.1-Flash-1017 · 用户 {args.user_id} · 企业 {current_company}")
        print(f"调用费用按项目累计，上限 US${args.budget_usd:g}。输入 /退出 结束聊天。")
        while True:
            try:
                message = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n聊天已结束，已保存的任务可以继续办理。")
                break
            if message in {"/退出", "/exit", "/quit"}:
                print("聊天已结束，已保存的任务可以继续办理。")
                break
            if not message:
                continue
            try:
                result = runtime.run_turn(
                    session_id=session_id,
                    user_id=args.user_id,
                    company_id=None if runtime.agent_store.session(session_id, args.user_id) else current_company,
                    message=message,
                    live_authorized=True,
                    budget_usd=args.budget_usd,
                )
            except KeyboardInterrupt:
                runtime.cancel(session_id=session_id, user_id=args.user_id)
                print("\n已停止本轮办理。已保存的结果可以在下次继续时查询。")
                break
            reply = result.get("reply") or result.get("summary") or "本轮未完成，请查看办理记录。"
            print("\n助手：" + reply)
            if result.get("error_type") == "BudgetExceeded":
                break
        return 0
    except (TaxHarnessError, OSError, ValueError) as error:
        print("无法开始办理：" + str(error), file=sys.stderr)
        return 1
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
