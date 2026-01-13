#!/usr/bin/env python3
import argparse
import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def build_prompt(
    prompt_text,
    negative_text,
    seed,
    width,
    height,
    frames,
    steps,
    cfg,
    sampler,
    scheduler,
    denoise,
    clip_name,
    unet_name,
    vae_name,
    fps_webm,
    fps_webp,
    filename_prefix,
    include_webm,
    include_webp,
    include_images,
):
    prompt = {
        "37": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": unet_name,
                "weight_dtype": "default",
            },
        },
        "38": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip_name,
                "type": "wan",
                "device": "default",
            },
        },
        "39": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": vae_name,
            },
        },
        "40": {
            "class_type": "EmptyHunyuanLatentVideo",
            "inputs": {
                "width": width,
                "height": height,
                "length": frames,
                "batch_size": 1,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["38", 0],
                "text": prompt_text,
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["38", 0],
                "text": negative_text,
            },
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["37", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["40", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": denoise,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["39", 0],
            },
        },
    }

    if include_webp:
        prompt["28"] = {
            "class_type": "SaveAnimatedWEBP",
            "inputs": {
                "images": ["8", 0],
                "filename_prefix": filename_prefix,
                "fps": fps_webp,
                "lossless": False,
                "quality": 90,
                "method": "default",
            },
        }

    if include_webm:
        prompt["47"] = {
            "class_type": "SaveWEBM",
            "inputs": {
                "images": ["8", 0],
                "filename_prefix": filename_prefix,
                "codec": "vp9",
                "fps": fps_webm,
                "crf": 32,
            },
        }

    if include_images:
        prompt["10"] = {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["8", 0],
                "filename_prefix": filename_prefix,
            },
        }

    return prompt


def request_json(url, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_server_reachable(base_url):
    try:
        request_json(urllib.parse.urljoin(base_url, "/queue"), timeout=3)
        return True
    except Exception:
        return False


def wait_for_server(base_url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_server_reachable(base_url):
            return True
        time.sleep(1)
    return False


def start_port_forward(namespace, deployment, local_port):
    cmd = [
        "kubectl",
        "port-forward",
        "-n",
        namespace,
        f"deploy/{deployment}",
        f"{local_port}:8181",
        "--address",
        "127.0.0.1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_port(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.port:
        return parsed.port
    return 8181


def extract_option_list(node_info, node_name, input_name):
    node = node_info.get(node_name, {})
    inputs = node.get("input", {}).get("required", {})
    spec = inputs.get(input_name)
    if not spec:
        return []
    if isinstance(spec, list):
        if spec and isinstance(spec[0], list):
            return spec[0]
        return spec
    return []


def preflight_check(base_url, unet_name, clip_name, vae_name):
    info = request_json(urllib.parse.urljoin(base_url, "/object_info"), timeout=30)
    missing = []
    unets = extract_option_list(info, "UNETLoader", "unet_name")
    clips = extract_option_list(info, "CLIPLoader", "clip_name")
    vaes = extract_option_list(info, "VAELoader", "vae_name")

    if unet_name not in unets:
        missing.append(f"UNET: {unet_name}")
    if clip_name not in clips:
        missing.append(f"CLIP: {clip_name}")
    if vae_name not in vaes:
        missing.append(f"VAE: {vae_name}")

    if missing:
        raise RuntimeError(
            "Missing model files in ComfyUI. " + ", ".join(missing)
        )


def submit_prompt(base_url, prompt, client_id):
    resp = request_json(
        urllib.parse.urljoin(base_url, "/prompt"),
        payload={"prompt": prompt, "client_id": client_id},
        timeout=30,
    )
    if "error" in resp:
        raise RuntimeError(resp["error"])
    if "prompt_id" not in resp:
        raise RuntimeError(f"Unexpected response: {resp}")
    return resp["prompt_id"]


def wait_for_prompt(base_url, prompt_id, timeout, poll_interval):
    deadline = time.time() + timeout
    history_url = urllib.parse.urljoin(base_url, f"/history/{prompt_id}")
    while time.time() < deadline:
        resp = request_json(history_url, timeout=30)
        if prompt_id in resp:
            entry = resp[prompt_id]
            status = entry.get("status") or {}
            if status.get("completed"):
                if status.get("status_str") != "success":
                    msg = ", ".join(status.get("messages") or [])
                    raise RuntimeError(f"Generation failed: {msg or status}")
                return entry
        time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for ComfyUI to finish.")


def extract_files(history_entry):
    files = []
    outputs = history_entry.get("outputs") or {}
    for output in outputs.values():
        for key in ("images", "videos", "gifs"):
            items = output.get(key)
            if not items:
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    files.append(item)
    return files


def download_file(base_url, file_info, dest_dir):
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")
    file_type = file_info.get("type", "output")
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": file_type,
    }
    view_url = urllib.parse.urljoin(base_url, "/view") + "?" + urllib.parse.urlencode(params)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    urllib.request.urlretrieve(view_url, dest_path)
    return dest_path


def write_index(dest_dir, prompt_text, files):
    lines = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>ComfyUI Outputs</title></head><body>",
        f"<h1>Prompt</h1><p>{prompt_text}</p>",
    ]
    for path in files:
        rel = path.name
        if path.suffix.lower() in {".webm", ".mp4"}:
            lines.append(f"<div><video controls src=\"{rel}\" style=\"max-width: 100%;\"></video></div>")
        else:
            lines.append(f"<div><img src=\"{rel}\" style=\"max-width: 100%;\"></div>")
    lines.append("</body></html>")
    (dest_dir / "index.html").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate Wan2.1 text-to-video outputs via ComfyUI API.")
    parser.add_argument("--prompt", required=True, help="Text prompt.")
    parser.add_argument("--negative", default="blurry, low quality, artifacts", help="Negative prompt.")
    parser.add_argument("--count", type=int, default=5, help="Number of outputs to generate.")
    parser.add_argument("--width", type=int, default=512, help="Output width (multiple of 16).")
    parser.add_argument("--height", type=int, default=320, help="Output height (multiple of 16).")
    parser.add_argument("--frames", type=int, default=16, help="Number of frames.")
    parser.add_argument("--steps", type=int, default=25, help="Sampling steps.")
    parser.add_argument("--cfg", type=float, default=6.0, help="CFG scale.")
    parser.add_argument("--sampler", default="uni_pc", help="Sampler name.")
    parser.add_argument("--scheduler", default="simple", help="Scheduler name.")
    parser.add_argument("--denoise", type=float, default=1.0, help="Denoise strength.")
    parser.add_argument("--mode", choices=["video", "image"], default="video", help="Output type.")
    parser.add_argument("--format", choices=["webm", "webp", "both"], default="webm", help="Video format.")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:8181", help="ComfyUI base URL.")
    parser.add_argument("--output-dir", default="generated", help="Directory to save results.")
    parser.add_argument("--seed", type=int, default=None, help="Base seed (optional).")
    parser.add_argument("--port-forward", action="store_true", help="Start kubectl port-forward automatically.")
    parser.add_argument("--namespace", default="llm", help="Kubernetes namespace for port-forward.")
    parser.add_argument("--deployment", default="wan-video-gen", help="Kubernetes deployment for port-forward.")
    parser.add_argument("--skip-check", action="store_true", help="Skip model presence checks.")
    args = parser.parse_args()

    include_webm = args.mode == "video" and args.format in ("webm", "both")
    include_webp = args.mode == "video" and args.format in ("webp", "both")
    include_images = args.mode == "image"
    if args.mode == "video" and not (include_webm or include_webp):
        raise SystemExit("Select at least one video format.")
    if args.mode == "image" and args.frames != 1:
        args.frames = 1

    base_dir = Path(args.output_dir).expanduser().resolve()
    run_dir = base_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    random_gen = random.SystemRandom()
    seeds = []
    for i in range(args.count):
        if args.seed is None:
            seeds.append(random_gen.randrange(0, 2**63))
        else:
            seeds.append(args.seed + i)

    client_id = f"cli-{random_gen.randrange(0, 1_000_000)}"
    all_files = []

    unet_name = "wan2.1_t2v_1.3B_bf16.safetensors"
    clip_name = "umt5_xxl_fp16.safetensors"
    vae_name = "wan_2.1_vae.safetensors"

    port_forward_proc = None
    try:
        if not is_server_reachable(args.comfy_url):
            if not args.port_forward:
                raise RuntimeError("ComfyUI is not reachable. Use --port-forward or set --comfy-url.")
            local_port = parse_port(args.comfy_url)
            port_forward_proc = start_port_forward(args.namespace, args.deployment, local_port)
            if not wait_for_server(args.comfy_url, timeout=30):
                raise RuntimeError("Port-forward started, but ComfyUI is not reachable.")

        if not args.skip_check:
            preflight_check(args.comfy_url, unet_name, clip_name, vae_name)

        for idx, seed in enumerate(seeds, start=1):
            prefix_base = "wan_t2v" if args.mode == "video" else "wan_t2i"
            filename_prefix = f"{prefix_base}_{idx:02d}"
            prompt = build_prompt(
                prompt_text=args.prompt,
                negative_text=args.negative,
                seed=seed,
                width=args.width,
                height=args.height,
                frames=args.frames,
                steps=args.steps,
                cfg=args.cfg,
                sampler=args.sampler,
                scheduler=args.scheduler,
                denoise=args.denoise,
                clip_name=clip_name,
                unet_name=unet_name,
                vae_name=vae_name,
                fps_webm=24,
                fps_webp=16,
                filename_prefix=filename_prefix,
                include_webm=include_webm,
                include_webp=include_webp,
                include_images=include_images,
            )

            print(f"[{idx}/{args.count}] Queuing prompt (seed={seed})...")
            prompt_id = submit_prompt(args.comfy_url, prompt, client_id)
            history_entry = wait_for_prompt(args.comfy_url, prompt_id, timeout=3600, poll_interval=5)
            files = extract_files(history_entry)

            if not files:
                raise RuntimeError("No output files found in history response.")

            for file_info in files:
                ext = os.path.splitext(file_info["filename"])[1].lower()
                if args.mode == "video":
                    if args.format == "webm" and ext != ".webm":
                        continue
                    if args.format == "webp" and ext != ".webp":
                        continue
                dest_path = download_file(args.comfy_url, file_info, run_dir)
                all_files.append(dest_path)
                print(f"  saved: {dest_path}")
    finally:
        if port_forward_proc is not None:
            port_forward_proc.terminate()
            port_forward_proc.wait(timeout=5)

    if all_files:
        write_index(run_dir, args.prompt, all_files)
        print(f"\nDone. Open {run_dir / 'index.html'} to view results.")


if __name__ == "__main__":
    main()
