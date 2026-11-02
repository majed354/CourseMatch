import argparse
from .extract import ROOT, build_catalog

def main():
    parser = argparse.ArgumentParser(description="Local college course equivalency pilot")
    parser.add_argument("action", choices=["build", "serve"])
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--model", default="qwen3.8:27b-q8_0")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.action == "build":
        build_catalog(ROOT / "equivalency-pilot/catalog.json", workers=max(1, min(4, args.workers)))
    else:
        from .server import serve
        serve(port=args.port, model=args.model)


if __name__ == "__main__":
    main()
