"""Local administration and auditable PDF execution: python -m invoiceiq --help."""
import argparse
import json
import os
from pathlib import Path
from .database import Repository
from .workflow import process_pdf


def main():
    parser = argparse.ArgumentParser(description="ClearPay local workflow")
    parser.add_argument("--db", default=os.getenv("INVOICEIQ_DB", "invoiceiq.sqlite"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Insert synthetic demo vendors and purchase orders")
    commands.add_parser("reset", help="Delete all processed runs and approvals, then re-seed (demo only)")
    process = commands.add_parser("process", help="Extract a PDF and run deterministic finance controls")
    process.add_argument("pdf", type=Path)
    process.add_argument("--mode", choices=["auto", "fixture", "vision"], default="auto")
    args = parser.parse_args()
    repo = Repository(args.db)
    if args.command == "seed":
        repo.seed()
        print("Synthetic procurement data seeded")
    elif args.command == "reset":
        repo.reset_demo()
        print("All runs and approvals deleted; synthetic procurement data re-seeded")
    else:
        result = process_pdf(args.pdf.read_bytes(), args.pdf.name, repo, mode=args.mode,
                             policy_dir=Path(__file__).resolve().parents[1] / "fixtures/policies")
        print(json.dumps({"run_id": result["run_id"], "backend": result["backend"],
                          "decision": result["decision"].model_dump(mode="json")}, indent=2))


if __name__ == "__main__":
    main()
