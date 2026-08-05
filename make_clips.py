#!/usr/bin/env python3
"""make_clips.py — собирает clips.json из монтажных блоков промт-чата.

Читает in/scenes.txt с блоками:
    [MONTAGE]
    file: brushing
    text: тема урока revyline RL 066 звуковая щётка
    trim: loose
    hook: кого бы ты выбрал      # только для сцен без реплики
    [/MONTAGE]

Сопоставляет каждый блок с файлом в in/: по подстроке из `file:`, иначе по порядку.
Печатает пары клип↔сцена для проверки и пишет clips.json.

Запуск:  ./venv/bin/python make_clips.py [путь_к_scenes.txt]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IN = ROOT / "in"
VIDEO_EXT = {".mp4", ".mov", ".m4v"}


def parse_blocks(text: str) -> list[dict]:
    blocks = []
    for chunk in re.findall(r"\[MONTAGE\](.*?)\[/MONTAGE\]", text, re.S | re.I):
        d = {}
        for line in chunk.strip().splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            d[key.strip().lower()] = val.strip()
        blocks.append(d)
    return blocks


def main() -> None:
    scenes = Path(sys.argv[1]) if len(sys.argv) > 1 else IN / "scenes.txt"
    if not scenes.exists():
        sys.exit(f"Нет файла со сценами: {scenes}\n"
                 f"Сохрани вывод промт-чата (блоки [MONTAGE]) в {scenes}")

    blocks = parse_blocks(scenes.read_text(encoding="utf-8"))
    if not blocks:
        sys.exit("В файле не найдено ни одного блока [MONTAGE].")

    files = sorted(p.name for p in IN.iterdir() if p.suffix.lower() in VIDEO_EXT)
    pool = list(files)

    clips, mapping = [], []
    for i, b in enumerate(blocks):
        hint = b.get("file", "").strip().lower()
        chosen = None
        if hint:
            matches = [f for f in pool if hint in f.lower()]
            if len(matches) == 1:
                chosen = matches[0]
        if chosen is None:                      # по порядку из оставшихся
            chosen = pool[0] if pool else None
        if chosen is None:
            sys.exit(f"Клипов меньше, чем сцен: не хватило файла для блока #{i+1}")
        pool.remove(chosen)

        clip = {"file": chosen, "text": b.get("text", ""),
                "trim": b.get("trim", "normal") or "normal"}
        if b.get("hook"):
            clip["hook"] = b["hook"]
        clips.append(clip)
        mapping.append((chosen, b.get("file", "—"), clip["trim"],
                        "hook" if "hook" in clip else "", clip["text"][:40]))

    out = ROOT / "clips.json"
    out.write_text(json.dumps(
        {"_comment": "Сгенерировано make_clips.py из in/scenes.txt",
         "clips": clips}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✓ {out.name}: {len(clips)} сцен\n")
    print(f"{'#':>2}  {'файл':<48} {'хинт':<10} {'trim':<7} {'':4} текст")
    for i, (f, hint, trim, hk, txt) in enumerate(mapping):
        print(f"{i:>2}  {f:<48} {hint:<10} {trim:<7} {hk:<4} {txt}")
    if pool:
        print(f"\n⚠ Не задействованы файлы: {', '.join(pool)}")


if __name__ == "__main__":
    main()
