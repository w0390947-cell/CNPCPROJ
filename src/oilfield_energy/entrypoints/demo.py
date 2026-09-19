"""Local demo bundle and long-running device/API service."""

import argparse
from pathlib import Path

import uvicorn

from oilfield_energy.bootstrap.demo import create_demo_app, generate_bundle, verify_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--bundle", type=Path, required=True)
    serve.add_argument("--output", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--tick-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.action == "generate":
        generate_bundle(args.output)
        print(args.output / "bundle.json")
    elif args.action == "verify":
        raise SystemExit(0 if verify_bundle(args.bundle, args.output) else 1)
    else:
        app = create_demo_app(args.bundle, args.output, args.tick_seconds)
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
