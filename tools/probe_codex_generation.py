"""Одноразова перевірка вбудованої генерації Codex, без API-ключів."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lazyref.codex import find_codex, object_schema, TEXT, TEXTS
from lazyref.processes import ProcessTree


def main():
    destination = Path(__file__).resolve().parent.parent / ".lazyref/codex-generation-probe"
    destination.mkdir(parents=True, exist_ok=True)
    schema = object_schema({"generation_available": {"type": "boolean"}, "image_paths": TEXTS, "limitation": TEXT})
    (destination / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    prompt = """Use the built-in image generation tool to create exactly ONE image, not a collage.
This is an AI-generated pose guide for an artist, NOT a retrieved real photograph.
Photorealistic adult man sitting on a bar stool at a bar counter, back fully turned toward the camera,
rear view, hands resting on the counter, enough of the body visible to understand the sitting pose.
Natural proportions, readable silhouette, ordinary clothing and lighting. No text or watermark.
Use only built-in image generation. Do not use shell, scripts, external APIs, browsing, or delegation.
Return generation_available=true and the exact absolute output image path returned by the tool.
If generation is unavailable or fails, return false, empty paths and a concise Ukrainian limitation.
Never invent paths or substitute a written prompt for an image. Return the requested JSON."""
    with tempfile.TemporaryDirectory(prefix="lazyref-generation-probe-") as cwd:
        args = [find_codex(), "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", 'features.shell_tool=false', "-c", 'features.multi_agent=false',
                "-c", 'agents.enabled=false', "--json", "--color", "never",
                "--output-schema", str(destination / "schema.json"), "-o", str(destination / "answer.json"), "-"]
        with ((destination / "events.jsonl").open("wb") as stdout,
              (destination / "stderr.log").open("wb") as stderr, ProcessTree() as tree):
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                cwd=cwd, **tree.popen_options)
            tree.attach(process)
            process.communicate(prompt.encode("utf-8"), timeout=600)
    print("Exit:", process.returncode)
    answer = destination / "answer.json"
    if answer.exists():
        print(json.dumps(json.loads(answer.read_text(encoding="utf-8")), ensure_ascii=True))
    print("Evidence:", destination)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
