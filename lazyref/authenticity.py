"""Відомі позначки генераторів, не універсальний детектор ШІ."""

import json
import re


class GeneratedImage(ValueError):
    """Явна позначка генерації: не пробувати прев'ю або скріншот цього ж зображення."""


def check_generation_metadata(image):
    # Перевіряємо оригінал до перетворення на JPEG, яке видаляє службові поля.
    info = image.info
    parameters = str(info.get("parameters", ""))[:20000]
    if re.search(r"\bSteps:\s*\d+", parameters) and re.search(r"\b(?:Sampler|Model|CFG scale):", parameters):
        raise GeneratedImage("У файлі є параметри дифузійного генератора.")
    for key in ("prompt", "workflow"):
        raw = info.get(key, "")
        if not isinstance(raw, str) or len(raw) > 100000:
            continue
        try:
            graph = json.loads(raw)
        except (ValueError, TypeError):
            continue
        text = json.dumps(graph)
        if '"class_type"' in text and any('"' + node + '"' in text for node in ("KSampler", "KSamplerAdvanced", "FluxGuidance")):
            raise GeneratedImage("У файлі є граф генерації ComfyUI.")
    software = str(image.getexif().get(305, "")) + " " + str(info.get("Software", ""))
    if re.search(r"\b(?:Midjourney|Stable Diffusion|DALL[ -]?E|ComfyUI|InvokeAI)\b", software, re.I):
        raise GeneratedImage("Програмне забезпечення у метаданих позначене як генератор ШІ.")
