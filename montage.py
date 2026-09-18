#!/usr/bin/env python3
"""montage.py — автомонтаж озвученных ИИ-клипов в готовый вертикальный ролик.

Делает то, что раньше вручную в CapCut:
  1. склейка кадров в заданном порядке (clips.json);
  2. вырезание лишних пауз/тишины (jump-cut по громкости) — речь идёт плотно;
  3. пословные субтитры в стиле CapCut: 1 слово за раз, крупное жёлтое, чёрная обводка, снизу;
  4. прожиг субтитров в видео → out/<name>.mp4.

Тайминг слов берётся forced-alignment'ом через faster-whisper по СЖАТОЙ дорожке
(после удаления пауз), поэтому субтитры всегда совпадают с речью.

Запуск (из папки avtomat montag):
    ./venv/bin/python montage.py

Полезные флаги:
    --noise -30dB     порог тишины (тише этого = пауза)
    --cap 0.30        сколько тишины оставить по КРАЯМ клипа (сек): режем
                      только начало и конец, внутренние паузы не трогаем
    --detect-sil 0.20 порог детекта тишины (паузы короче игнорируются)
    --model medium    модель whisper (small|medium|large-v3)
    --words 1         слов на один субтитр (1 = как на референсе)
    --out out/final.mp4
    --keep-silence    не резать паузы (только склейка + субтитры)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IN = ROOT / "in"
# Базовая папка промежуточных файлов. В main() подменяется на подпапку
# СВОЕГО запуска (work/<имя_ролика>_<pid>): оркестратор монтирует несколько
# роликов параллельно, и общая папка приводила к подмене клипов и субтитров
# между роликами (15.09: v27 получил видео v26, v28 — видео v29).
WORK = ROOT / "work"
OUT = ROOT / "out"

# ---- Стиль субтитров (рисуются как PNG, накладываются overlay). Меняется здесь.
VID_W, VID_H = 720, 1280
# Шрифт субтитров и плашек: Rubik (Google Fonts, OFL), вариативный файл в fonts/,
# вес 700 (решение Кирилла 17.09). Нет файла — откат на Arial Bold.
FONT_RUBIK = ROOT / "fonts" / "Rubik[wght].ttf"
FONT_FALLBACK = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_WEIGHT = 700


def load_font(path, size: int, weight: int = FONT_WEIGHT):
    """TrueType-шрифт нужного кегля; у вариативного выставляем вес."""
    from PIL import ImageFont
    p = Path(path)
    if not p.exists():
        p = Path(FONT_FALLBACK)
    font = ImageFont.truetype(str(p), size)
    try:
        axes = font.get_variation_axes()
    except OSError:          # обычный (не вариативный) шрифт
        axes = []
    if axes:
        vals = [weight if a.get("name") in (b"Weight", "Weight", b"wght", "wght")
                else a.get("default", 0) for a in axes]
        font.set_variation_by_axes(vals)
    return font

SUB = dict(
    font_path=str(FONT_RUBIK),
    size=54,                 # кегль, px — компактно
    active=(255, 255, 255, 255), # активное слово: белый (решение Кирилла 16.09)
    base=(205, 205, 205, 255),   # остальные слова группы: светло-серый
    outline=(0, 0, 0, 255),      # обводка: чёрный
    stroke=5,                # толщина обводки, px
    space=16,                # зазор между словами в группе, px
    y_center=0.82,           # центр строки по высоте (0..1) — ниже
    lowercase=True,          # строчными, как на референсе
    # Сколько символов (с пробелами) помещается в одну плашку при --words 0:
    # слова набираются, пока сумма не превысит лимит; слово длиннее стоит одно.
    # При кегле 54 это ~420 px из 720 — читается с запасом.
    max_chars=14,
)

def _sub_from_env() -> None:
    """Настройки субтитров из приложения KADI (мост webapp/bridge.py выставляет
    переменные окружения перед сборкой — так они доходят через PostFlow, не меняя
    его командную строку). Без переменных действуют дефолты SUB выше."""
    def num(name: str, lo: float, hi: float):
        raw = os.environ.get(name, "")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    size = num("KADI_SUB_SIZE", 24, 96)
    if size:
        SUB["size"] = int(size)
        SUB["stroke"] = max(3, round(int(size) * 0.09))   # обводка за кеглем
        SUB["space"] = max(8, round(int(size) * 0.3))
    y = num("KADI_SUB_Y", 0.1, 0.95)
    if y:
        SUB["y_center"] = y
    color = os.environ.get("KADI_SUB_COLOR", "").strip().lstrip("#")
    if len(color) == 6:
        try:
            SUB["active"] = (*(int(color[i:i + 2], 16) for i in (0, 2, 4)), 255)
        except ValueError:
            pass
    low = os.environ.get("KADI_SUB_LOWER", "")
    if low in ("0", "1"):
        SUB["lowercase"] = low == "1"
    mc = num("KADI_SUB_MAXCHARS", 3, 40)
    if mc:
        SUB["max_chars"] = int(mc)


_sub_from_env()

# Плашка-хук (текст поверх целой сцены, напр. для сцены без реплик).
HOOK = dict(
    font_path=str(FONT_RUBIK),
    size=74, color=(255, 255, 255, 255), outline=(0, 0, 0, 255),
    stroke=7, y_center=0.30, max_w_ratio=0.86, line_gap=10,
)

# Пресеты резки: сколько тишины оставить по КРАЯМ клипа (сек).
# Режем только начало и конец; внутренние паузы не трогаем.
TRIM = {
    "tight":  0.20,
    "normal": 0.30,   # по умолчанию — 0,3 c тишины на входе и выходе клипа
    "loose":  0.50,
    # "none" — не резать вообще (обрабатывается отдельно)
}

# Мелкие правки авто-распознавания (бренд и т.п.). Регистронезависимо, по слову.
FIXUPS = {
    r"^rive?l?line$": "revyline",
    r"^reve?l?line$": "revyline",
    r"^корода$": "щётка",
}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def ffprobe_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)])
    return float(r.stdout.strip())


# ---- 1. Детект тишины и построение интервалов речи -------------------------
def detect_silences(path: Path, noise: str, min_sil: float) -> list[tuple[float, float]]:
    """Возвращает список интервалов ТИШИНЫ [(start,end), ...]."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", f"silencedetect=noise={noise}:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True)
    out = r.stderr
    sils, start = [], None
    for m in re.finditer(r"silence_(start|end):\s*([0-9.]+)", out):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif kind == "end" and start is not None:
            sils.append((start, val))
            start = None
    return sils


def speech_intervals(dur: float, silences: list[tuple[float, float]],
                     cap: float, min_keep: float = 0.05) -> list[tuple[float, float]]:
    """Режем ТОЛЬКО тишину по краям клипа (в начале и в конце). Всё, что
    внутри — речь и внутренние паузы — оставляем нетронутым.

    Логика (решение Клейтона, сентябрь): каждый клип из Flow — это отдельная
    сцена длиной 4–10 сек. Часто в начале и в конце есть «мёртвая» пауза
    (персонаж молчит до/после реплики). Её и режем: если пауза с края длиннее
    cap — обрезаем край так, чтобы осталось ровно cap секунд тишины; если
    короче или её нет — не трогаем. Внутренние паузы между словами/репликами
    НЕ трогаем вообще: если внутри диалога есть длинные дыры — это правится в
    сценарии (объём текста под длительность), а не монтажом.

    `cap` — сколько тишины оставить с каждого края (сек).
    Возвращает один keep-интервал [start, end] — обрезанный по краям клип.
    """
    # Границы речи: первый и последний момент, когда есть звук.
    # Тишина в начале = первый silence, начинающийся с ~0.
    lead = 0.0   # сколько тишины в начале
    tail_start = dur  # где начинается финальная тишина (по умолчанию — конец)

    if silences:
        s0, e0 = silences[0]
        if s0 <= 0.02:            # тишина прямо с начала клипа
            lead = e0
        sN, eN = silences[-1]
        if eN >= dur - 0.02:      # тишина до самого конца клипа
            tail_start = sN

    # Обрезаем начало: оставляем не больше cap тишины перед речью.
    start = max(0.0, lead - cap) if lead > cap else 0.0
    # Обрезаем конец: оставляем не больше cap тишины после речи.
    tail_len = dur - tail_start
    end = tail_start + cap if tail_len > cap else dur

    if end - start < min_keep:
        return [(0.0, dur)]  # почти всё — тишина: не трогаем, отдаём как есть
    return [(round(start, 3), round(end, 3))]


# ---- 2. Обрезка одного клипа по интервалам речи ----------------------------
def trim_clip(src: Path, keeps: list[tuple[float, float]], dst: Path) -> None:
    """Оставляет только интервалы keeps, склеивает их, перекодирует в единый формат."""
    parts, labels = [], []
    for i, (s, e) in enumerate(keeps):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    fc = ";".join(parts) + ";" + "".join(labels) + \
        f"concat=n={len(keeps)}:v=1:a=1[v][a]"
    run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", fc,
         "-map", "[v]", "-map", "[a]",
         "-r", "30", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-ar", "48000", "-ac", "2",
         "-video_track_timescale", "30000", str(dst)])


def copy_norm(src: Path, dst: Path) -> None:
    """Без резки пауз — просто привести к единому формату для склейки."""
    run(["ffmpeg", "-y", "-i", str(src),
         "-r", "30", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-ar", "48000", "-ac", "2",
         "-video_track_timescale", "30000", str(dst)])


# ---- 3. Склейка ------------------------------------------------------------
def concat(parts: list[Path], dst: Path) -> None:
    lst = WORK / "concat_list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(dst)])


# ---- 4. Тайминг слов: по каждому клипу отдельно (надёжнее, чем по склейке) --
def transcribe_clip(model, path: Path) -> list[dict]:
    """Пословный тайминг ОДНОГО клипа (локальные времена от 0)."""
    segs, _ = model.transcribe(str(path), language="ru", word_timestamps=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            if w.word.strip():
                words.append({"start": w.start, "end": w.end})
    return words


def align_text(provided: str, timing: list[dict]) -> list[dict]:
    """Кладёт слова из сценария (provided) на тайминг из аудио.
    Если число слов совпало — 1:1; иначе равномерно по речевому промежутку."""
    ptoks = [t for t in provided.split() if t]
    if not ptoks:
        return []
    if not timing:
        return []
    span0, span1 = timing[0]["start"], timing[-1]["end"]
    if len(ptoks) == len(timing):
        return [{"start": timing[i]["start"], "end": timing[i]["end"], "text": ptoks[i]}
                for i in range(len(ptoks))]
    # равномерная раскладка по промежутку речи
    step = (span1 - span0) / len(ptoks)
    return [{"start": span0 + i * step, "end": span0 + (i + 1) * step, "text": ptoks[i]}
            for i in range(len(ptoks))]


def clip_words(model, path: Path, provided: str) -> list[dict]:
    """Слова сценария, разложенные по таймингу речи. Нет текста в сценарии —
    нет субтитров: распознанное whisper НЕ показываем (на тишине он выдумывает
    «Редактор субтитров…» — 16.09, v30)."""
    if not provided.strip():
        return []
    timing = transcribe_clip(model, path)
    return align_text(provided, timing)


_PUNCT_RE = re.compile(r"[^\w]", re.UNICODE)
_CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_CODE_RE = re.compile(r"^[A-Za-z]{1,4}$")   # латинский код модели: RL, XR…


def fixup(word: str) -> str:
    """Чистит одно слово: снимает пунктуацию, правит бренд.
    Кириллицу → строчными; латиницу (RL, revyline) оставляет как есть. Цифры — отдельно, в numberize_seq."""
    w = _PUNCT_RE.sub("", word)          # убрать все знаки препинания
    if not w:
        return ""
    low = w.lower()
    for pat, repl in FIXUPS.items():     # бренд и опечатки распознавания
        if re.match(pat, low):
            return repl
    if _CYR_RE.search(w):                # кириллица → строчными
        return low if SUB["lowercase"] else w
    return w                             # латиница/цифры → как в источнике


def numberize_seq(tokens: list[str]) -> list[str]:
    """Цифры → слова (двадцать, тридцать тысяч), НО коды моделей оставляем цифрами:
    цифра после латинского кода (RL 066) или с ведущим нулём (066) — не трогаем."""
    from num2words import num2words
    out = []
    for i, t in enumerate(tokens):
        if t.isdigit():
            prev = tokens[i - 1] if i > 0 else ""
            is_code = _CODE_RE.match(prev) or (len(t) > 1 and t[0] == "0")
            out.append(t if is_code else num2words(int(t), lang="ru"))
        else:
            out.append(t)
    return out


# ---- 5. События субтитров и рендер PNG -------------------------------------
def group_by_chars(clean: list[dict], max_chars: int) -> list[list[dict]]:
    """Группы слов для одной плашки: набираем, пока сумма символов с пробелами
    не превысит max_chars; слово длиннее лимита стоит одно; группа не
    пересекает границу клипа (поле clip)."""
    groups, cur, cur_len = [], [], 0
    for w in clean:
        n = len(w["text"])
        same_clip = not cur or cur[-1].get("clip") == w.get("clip")
        if cur and (not same_clip or cur_len + 1 + n > max_chars):
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(w)
        cur_len = n if cur_len == 0 else cur_len + 1 + n
    if cur:
        groups.append(cur)
    return groups


def build_events(words: list[dict], per_group: int,
                 max_chars: int | None = None) -> list[dict]:
    """Каждое событие = одна показанная плашка с одним активным словом.
    per_group>0 — фиксированное число слов в плашке; per_group<=0 — группировка
    по символам (max_chars, см. SUB["max_chars"]). Активное подсвечивается по очереди."""
    # чистим слова, выкидываем те, что стали пустыми (были только пунктуацией)
    clean = [{**w, "text": fixup(w["text"])} for w in words]
    clean = [w for w in clean if w["text"]]
    # цифры → слова (с учётом соседей: RL 066 остаётся цифрами)
    nums = numberize_seq([w["text"] for w in clean])
    for w, t in zip(clean, nums):
        w["text"] = t
    if per_group and per_group > 0:
        groups = [clean[i:i + per_group] for i in range(0, len(clean), per_group)]
    else:
        groups = group_by_chars(clean, int(max_chars or SUB["max_chars"]))
    events = []
    for group in groups:
        tokens = [w["text"] for w in group]
        for j, w in enumerate(group):
            events.append({"start": w["start"], "end": w["end"],
                           "tokens": tokens, "active": j})
    return events


def render_event_png(tokens: list[str], active: int, path: Path):
    """Рисует строку слов: активное — жёлтым, остальные — белым, с чёрной обводкой."""
    from PIL import Image, ImageDraw, ImageFont
    font = load_font(SUB["font_path"], SUB["size"])
    st, sp = SUB["stroke"], SUB["space"]
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    widths = [probe.textlength(t, font=font) for t in tokens]
    total_w = sum(widths) + sp * (len(tokens) - 1)
    W = int(total_w + st * 2 + 20)
    H = int(SUB["size"] * 1.7 + st * 2)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = (W - total_w) / 2
    y = (H - SUB["size"]) / 2 - SUB["size"] * 0.1
    for k, t in enumerate(tokens):
        color = SUB["active"] if k == active else SUB["base"]
        d.text((x, y), t, font=font, fill=color,
               stroke_width=st, stroke_fill=SUB["outline"])
        x += widths[k] + sp
    img.save(path)
    return W, H


def render_hook_png(text: str, path: Path):
    """Плашка-хук: крупный белый текст с переносом по словам, с обводкой."""
    from PIL import Image, ImageDraw, ImageFont
    font = load_font(HOOK["font_path"], HOOK["size"])
    st = HOOK["stroke"]
    max_w = int(VID_W * HOOK["max_w_ratio"])
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if probe.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    lw = [probe.textlength(ln, font=font) for ln in lines]
    W = int(max(lw) + st * 2 + 20)
    lh = HOOK["size"] + HOOK["line_gap"]
    H = int(lh * len(lines) + st * 2 + 10)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        x = (W - lw[i]) / 2
        y = st + i * lh
        d.text((x, y), ln, font=font, fill=HOOK["color"],
               stroke_width=st, stroke_fill=HOOK["outline"])
    img.save(path)
    return W, H


# ---- 6. Прожиг субтитров через overlay (без libass) ------------------------
def burn(video: Path, events: list[dict], hooks: list[dict],
         subs_dir: Path, dst: Path) -> None:
    subs_dir.mkdir(exist_ok=True)
    inputs = ["-i", str(video)]
    parts, prev = [], "[0:v]"
    n = 1  # 0 — само видео
    # пословные субтитры
    yc = int(VID_H * SUB["y_center"])
    for i, ev in enumerate(events):
        png = subs_dir / f"w{i:03d}.png"
        _, h = render_event_png(ev["tokens"], ev["active"], png)
        inputs += ["-i", str(png)]
        y = yc - h // 2
        lbl = f"[o{n}]"
        parts.append(f"{prev}[{n}:v]overlay=x=(W-w)/2:y={y}:"
                     f"enable='between(t,{ev['start']:.3f},{ev['end']:.3f})'{lbl}")
        prev, n = lbl, n + 1
    # хук-плашки
    yh = int(VID_H * HOOK["y_center"])
    for i, hk in enumerate(hooks):
        png = subs_dir / f"h{i:03d}.png"
        _, h = render_hook_png(hk["text"], png)
        inputs += ["-i", str(png)]
        y = yh - h // 2
        lbl = f"[o{n}]"
        parts.append(f"{prev}[{n}:v]overlay=x=(W-w)/2:y={y}:"
                     f"enable='between(t,{hk['start']:.3f},{hk['end']:.3f})'{lbl}")
        prev, n = lbl, n + 1
    fc = ";".join(parts)
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc,
         "-map", prev, "-map", "0:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "copy", str(dst)])


def speed_up(src: Path, dst: Path, speed: float) -> None:
    """Ускорить видео и звук. Делается ДО тайминга слов, чтобы субтитры
    легли на уже ускоренную дорожку (после — они бы разъехались)."""
    # atempo принимает 0.5..2.0 — большее раскладываем цепочкой.
    filters, s = [], speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    filters.append(f"atempo={s:.4f}")
    run(["ffmpeg", "-y", "-i", str(src),
         "-filter_complex",
         f"[0:v]setpts=PTS/{speed:.4f}[v];[0:a]{','.join(filters)}[a]",
         "-map", "[v]", "-map", "[a]",
         "-r", "30", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dst)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", default=str(ROOT / "clips.json"))
    ap.add_argument("--noise", default="-30dB")
    # Целевая макс. пауза между репликами (сек): длиннее — сжать, короче — оставить.
    ap.add_argument("--cap", type=float, default=0.30)
    # Порог детекта тишины: паузы короче него игнорируются как естественные.
    ap.add_argument("--detect-sil", dest="detect_sil", type=float, default=0.20)
    # Обратная совместимость: PostFlow мог передавать старые флаги — принимаем,
    # но --min-sil теперь маппится на detect_sil, --pad игнорируется.
    ap.add_argument("--min-sil", dest="detect_sil", type=float, default=0.20,
                    help="устар.: порог детекта тишины (= --detect-sil)")
    ap.add_argument("--pad", type=float, default=0.06,
                    help="устар.: больше не используется")
    ap.add_argument("--model", default="medium")
    # Флаги ниже нужны PostFlow (postflow/montage.py передаёт их всегда или
    # по условию) — без них argparse падал и стык был сломан.
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"],
                    help="устройство whisper (auto = пусть выберет сам)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="ускорение готового ролика (1.0 = без)")
    ap.add_argument("--title", default="", help="название серии для заставки")
    ap.add_argument("--episode", default="", help="номер/подпись серии для заставки")
    ap.add_argument("--sub-y", dest="sub_y", type=float, default=None,
                    help="центр строки субтитров по высоте (0..1), дефолт 0.82")
    ap.add_argument("--words", type=int,
                    default=int(os.environ.get("KADI_SUB_WORDS", 0) or 0),
                    help="слов в плашке; 0 = авто по символам (--max-chars); "
                         "KADI_SUB_WORDS задаёт умолчание")
    ap.add_argument("--max-chars", dest="max_chars", type=int, default=None,
                    help=f"лимит символов в плашке при --words 0 (дефолт {SUB['max_chars']})")
    ap.add_argument("--in", dest="in_dir", default=str(IN), help="папка-источник клипов")
    ap.add_argument("--out", default=str(OUT / "final.mp4"))
    ap.add_argument("--keep-silence", action="store_true")
    args = ap.parse_args()

    if args.sub_y is not None:
        SUB["y_center"] = max(0.05, min(0.95, args.sub_y))
    if args.max_chars:
        SUB["max_chars"] = max(3, args.max_chars)

    in_dir = Path(args.in_dir)
    # Своя рабочая папка на запуск — параллельные монтажи не мешают друг другу.
    global WORK
    WORK = ROOT / "work" / f"{Path(args.out).stem}_{os.getpid()}"
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"→ Рабочая папка: {WORK.relative_to(ROOT)}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    spec = json.loads(Path(args.clips).read_text())
    clips = spec["clips"]

    print(f"→ Кадров: {len(clips)}")
    parts, texts, durs = [], [], []
    for idx, c in enumerate(clips):
        fp = Path(c["file"])
        src = fp if fp.is_absolute() else in_dir / c["file"]
        if not src.exists():
            sys.exit(f"Нет файла: {src}")
        dst = WORK / f"{idx:02d}.mp4"
        mode = c.get("trim", "normal")
        if args.keep_silence or mode == "none":
            copy_norm(src, dst)
            print(f"  [{idx}] {c['file']}: без резки")
        else:
            # cap — целевая макс. пауза между репликами (из пресета или --cap).
            cap = TRIM.get(mode, args.cap)
            dur = ffprobe_duration(src)
            # Детект тишины ловит паузы от короткого порога detect_sil, чтобы
            # найти ВСЕ промежутки; сжимаем их до cap в speech_intervals.
            sils = detect_silences(src, args.noise, args.detect_sil)
            keeps = speech_intervals(dur, sils, cap)
            kept = sum(e - s for s, e in keeps)
            print(f"  [{idx}] {c['file']}: {dur:.1f}s → {kept:.1f}s "
                  f"(вырезано {dur - kept:.1f}s, края≤{cap}s, trim={mode})")
            trim_clip(src, keeps, dst)
        parts.append(dst)
        texts.append(c.get("text", ""))
        durs.append(ffprobe_duration(dst))

    joined = WORK / "joined.mp4"
    concat(parts, joined)
    print(f"→ Склеено: {joined.name} ({ffprobe_duration(joined):.1f}s)")

    # Ускорение — ДО тайминга слов: whisper должен слышать финальную дорожку.
    if args.speed and args.speed != 1.0:
        sped = WORK / "joined_speed.mp4"
        speed_up(joined, sped, args.speed)
        joined = sped
        durs = [d / args.speed for d in durs]
        print(f"→ Ускорено x{args.speed}: {ffprobe_duration(joined):.1f}s")

    # позиции клипов на общей таймлинии склейки
    spans, acc = [], 0.0
    for d in durs:
        spans.append((acc, acc + d))
        acc += d

    # Тайминг — ПО КАЖДОМУ клипу отдельно, со сдвигом на его позицию в склейке.
    print(f"→ Тайминг слов по клипам (whisper {args.model})…")
    from faster_whisper import WhisperModel
    device = "cpu" if args.device == "auto" else args.device
    model = WhisperModel(args.model, device=device, compute_type="int8")
    words = []
    for idx, dst in enumerate(parts):
        w = clip_words(model, dst, texts[idx])
        for x in w:
            # Тайминг снят с клипа ДО ускорения — приводим к финальной скорости,
            # потом сдвигаем на позицию клипа в склейке.
            if args.speed and args.speed != 1.0:
                x["start"] /= args.speed
                x["end"] /= args.speed
            x["start"] += spans[idx][0]
            x["end"] += spans[idx][0]
            x["clip"] = idx          # плашка не пересекает границу клипа
        words += w
        print(f"  [{idx}] слов: {len(w)}")

    events = build_events(words, args.words)

    # хук-плашки: показываем на всю длину своей сцены
    hooks = [{"start": spans[i][0], "end": spans[i][1], "text": c["hook"]}
             for i, c in enumerate(clips) if c.get("hook")]
    # Заставка серии (--title/--episode от PostFlow): плашка в первые секунды.
    label = " — ".join(x for x in (args.title.strip(), args.episode.strip()) if x)
    if label:
        hooks.insert(0, {"start": 0.0, "end": min(2.5, sum(durs)), "text": label})
    print(f"→ Плашек субтитров: {len(events)}, хук-плашек: {len(hooks)}")

    out = Path(args.out)
    burn(joined, events, hooks, WORK / "subs", out)
    print(f"✓ Готово: {out}  ({ffprobe_duration(out):.1f}s)")
    # Успех — промежуточные файлы не нужны (при ошибке папка остаётся для разбора).
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
