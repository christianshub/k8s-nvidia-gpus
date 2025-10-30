#!/usr/bin/env python3
"""Generate multiple images from the SD15 API via HTTP."""
import argparse
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL = "http://192.168.0.176:30800/generate"

def generate(prompt: str, steps: int, url: str, out_dir: Path, prefix: str, count: int, delay: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for idx in range(1, count + 1):
        name = f"{prefix}_{idx:02d}.png"
        target = out_dir / name
        payload = {"prompt": prompt, "steps": steps}
        print(f"[*] Generating {name} -> {target}")
        resp = session.post(url, json=payload, timeout=600)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        gen_time = resp.headers.get("X-Gen-Time", "?")
        print(f"    ✔ done in {gen_time}s")
        if delay > 0 and idx != count:
            time.sleep(delay)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Batch-generate images via the SD15 API")
    parser.add_argument("prompt", help="prompt to send to the API")
    parser.add_argument("count", type=int, help="number of images to generate")
    parser.add_argument("prefix", help="output filename prefix, e.g. piggy")
    parser.add_argument("out_dir", nargs="?", default="outputs", help="directory to save images (default: outputs)")
    parser.add_argument("--steps", type=int, default=30, help="diffusion steps per image (default: 30)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API endpoint (default: {DEFAULT_URL})")
    parser.add_argument("--delay", type=float, default=0, help="seconds to sleep between requests")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    generate(args.prompt, args.steps, args.url, out_dir, args.prefix, args.count, args.delay)
    print(f"All done. Images saved under {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
