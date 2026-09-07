import argparse
import json
import os
import uuid
from pathlib import Path

from aiohttp import web
from .core import DEFAULT_DATA, UPSTREAMS, create_app, export_records, now, private_dir, write_json


def main():
    parser = argparse.ArgumentParser(description="Record local LLM traffic. Bodies may contain private source code.")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ROUTER_COLLECTOR_DATA_DIR", DEFAULT_DATA)))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--provider", choices=UPSTREAMS, required=True)
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--run-id", default=os.environ.get("ROUTER_COLLECTOR_RUN_ID"))
    export = commands.add_parser("export")
    export.add_argument("destination", type=Path)
    commands.add_parser("status")
    outcome = commands.add_parser("outcome")
    outcome.add_argument("--run-id", required=True)
    outcome.add_argument("--task-id", required=True)
    outcome.add_argument("--result", choices=("accepted", "rejected", "abandoned", "unknown"), required=True)
    outcome.add_argument("--note", default="")
    args = parser.parse_args()
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("port must be between 1 and 65535")
        web.run_app(create_app(args.provider, args.data_dir, args.run_id), host="127.0.0.1",
                    port=args.port, access_log=None, handler_cancellation=True)
    elif args.command == "export":
        print(json.dumps({"files": export_records(args.data_dir, args.destination), "archive": str(args.destination)}))
    elif args.command == "status":
        print(json.dumps({"data_dir": str(args.data_dir),
                          "records": len(list((args.data_dir / "records").glob("*/metadata.json"))),
                          "active_or_interrupted": len(list((args.data_dir / "active").glob("*"))),
                          "outcomes": len(list((args.data_dir / "outcomes").glob("*.json")))}, indent=2))
    else:
        record_id = str(uuid.uuid4())
        write_json(private_dir(args.data_dir / "outcomes") / (record_id + ".json"),
                   {"schema_version": 1, "id": record_id, "recorded_at": now(), "source": "explicit_user_feedback",
                    "run_id": args.run_id, "task_id": args.task_id, "result": args.result, "note": args.note})
        print(record_id)
