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
    --cap 0.50        целевая макс. пауза между репликами (сек): длиннее —
                      сжать до неё, короче — оставить; речь не режется
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
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IN = ROOT / "in"
WORK = ROOT / "work"
OUT = ROOT / "out"

# ---- Стиль субтитров (рисуются как PNG, накладываются overlay). Меняется здесь.
VID_W, VID_H = 720, 1280
SUB = dict(
    font_path="/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    size=54,                 # кегль, px — компактно
    active=(255, 255, 0, 255),   # активное слово: жёлтый
    base=(255, 255, 255, 255),   # обычный текст: белый
    outline=(0, 0, 0, 255),      # обводка: чёрный
    stroke=5,                # толщина обводки, px
    space=16,                # зазор между словами в группе, px
    y_center=0.82,           # центр строки по высоте (0..1) — ниже
    lowercase=True,          # строчными, как на референсе
)

def _sub_from_env() -> None:
    """Настройки субтитров из приложения KADI.

    Мост выставляет переменные окружения перед запуском сборки — так
    параметры доходят сюда через PostFlow, не меняя его командную строку.
    """
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


_sub_from_env()

# Плашка-хук (текст поверх целой сцены, напр. для сцены без реплик).
HOOK = dict(
    font_path="/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    size=74, color=(255, 255, 255, 255), outline=(0, 0, 0, 255),
    stroke=7, y_center=0.30, max_w_ratio=0.86, line_gap=10,
)

# Пресеты резки пауз: целевая максимальная пауза между репликами (сек).
# Паузу длиннее сжимаем до этого значения, короче — оставляем; речь не режем.
TRIM = {
    "tight":  0.30,   # плотнее
    "normal": 0.50,   # по умолчанию — 0,5 c воздуха между репликами
    "loose":  0.80,   # больше воздуха
    # "none" — не резать вообще (обрабатывается отдельно)
}

# Мелкие правки авто-распознавания (бренд и т.п.). Регистронезависимо, по слову.
# Бренд и линейка на экране пишутся латиницей — так они выглядят на упаковке
# и в карточке товара; кириллица в субтитрах читается как ошибка.
FIXUPS = {
    r"^rive?l?line$": "Revyline",
    r"^reve?l?line$": "Revyline",
    r"^ревиe?лайн[а-яё]*$": "Revyline",
    r"^ревай?лайн[а-яё]*$": "Revyline",
    r"^ревилаин[а-яё]*$": "Revyline",
    r"^кристал+[а-яё]*$": "Crystal",
    r"^crystal$": "Crystal",
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
    """Оставляем ВСЮ речь целиком, а паузы (тишину) между репликами
    укорачиваем до cap секунд — не режем речь, только лишнюю тишину.

    Логика (решение Клейтона): паузу длиннее cap сжимаем до cap, обрезая её
    СИММЕТРИЧНО ИЗ СЕРЕДИНЫ (по cap/2 к каждой соседней реплике), чтобы концы
    фраз не подрезались и оставался «воздух» с обеих сторон. Паузу короче или
    равную cap оставляем целиком — если второй персонаж быстро подхватил
    реплику (короткая пауза), стык не трогаем.

    `cap` — целевая максимальная пауза (сек). Историческое имя параметра было
    `pad`; вызывающий код передаёт сюда значение из пресета TRIM.
    """
    # Речь = дополнение к тишине.
    speech, cur = [], 0.0
    for s, e in silences:
        if s > cur:
            speech.append((cur, s))
        cur = max(cur, e)
    if cur < dur:
        speech.append((cur, dur))
    if not speech:
        return [(0.0, dur)]  # весь клип — тишина (edge): не трогаем

    # Собираем keep: каждая реплика + пауза после неё не длиннее cap.
    result: list[list[float]] = []
    for i, (s, e) in enumerate(speech):
        result.append([s, e])
        if i + 1 < len(speech):
            gap_start, gap_end = e, speech[i + 1][0]
            if gap_end - gap_start <= cap:
                result[-1][1] = gap_end            # короткая пауза — оставить
            else:
                result[-1][1] = gap_start + cap / 2  # половина воздуха здесь
                result.append([gap_end - cap / 2, gap_end])  # половина у следующей
    # склейка смежных
    merged: list[list[float]] = []
    for s, e in result:
        if merged and s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= min_keep]


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
    timing = transcribe_clip(model, path)
    if provided.strip():
        return align_text(provided, timing)
    # нет текста в сценарии — берём распознанное (только времена + слова)
    segs, _ = model.transcribe(str(path), language="ru", word_timestamps=True)
    out = []
    for s in segs:
        for w in (s.words or []):
            if w.word.strip():
                out.append({"start": w.start, "end": w.end, "text": w.word.strip()})
    return out


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
def build_events(words: list[dict], per_group: int) -> list[dict]:
    """Каждое событие = одна показанная плашка с одним активным (жёлтым) словом.
    При per_group>1 в плашке видно несколько слов, активное подсвечивается по очереди."""
    # чистим слова, выкидываем те, что стали пустыми (были только пунктуацией)
    clean = [{**w, "text": fixup(w["text"])} for w in words]
    clean = [w for w in clean if w["text"]]
    # цифры → слова (с учётом соседей: RL 066 остаётся цифрами)
    nums = numberize_seq([w["text"] for w in clean])
    for w, t in zip(clean, nums):
        w["text"] = t
    events = []
    for i in range(0, len(clean), per_group):
        group = clean[i:i + per_group]
        tokens = [w["text"] for w in group]
        for j, w in enumerate(group):
            events.append({"start": w["start"], "end": w["end"],
                           "tokens": tokens, "active": j})
    return events


def render_event_png(tokens: list[str], active: int, path: Path):
    """Рисует строку слов: активное — жёлтым, остальные — белым, с чёрной обводкой."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(SUB["font_path"], SUB["size"])
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
    font = ImageFont.truetype(HOOK["font_path"], HOOK["size"])
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
    """Ускорить видео и звук одним проходом. Применяется к ГОТОВОМУ ролику
    с прожжёнными субтитрами: они ускоряются вместе с картинкой."""
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
    ap.add_argument("--cap", type=float, default=0.50)
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
                    default=int(os.environ.get("KADI_SUB_WORDS", 1) or 1),
                    help="слов на один субтитр; KADI_SUB_WORDS задаёт умолчание")
    ap.add_argument("--in", dest="in_dir", default=str(IN), help="папка-источник клипов")
    ap.add_argument("--out", default=str(OUT / "final.mp4"))
    ap.add_argument("--keep-silence", action="store_true")
    args = ap.parse_args()

    if args.sub_y is not None:
        SUB["y_center"] = max(0.05, min(0.95, args.sub_y))

    in_dir = Path(args.in_dir)
    WORK.mkdir(exist_ok=True)
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
                  f"(вырезано {dur - kept:.1f}s, пауза≤{cap}s, trim={mode})")
            trim_clip(src, keeps, dst)
        parts.append(dst)
        texts.append(c.get("text", ""))
        durs.append(ffprobe_duration(dst))

    joined = WORK / "joined.mp4"
    concat(parts, joined)
    print(f"→ Склеено: {joined.name} ({ffprobe_duration(joined):.1f}s)")

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
            # сдвиг на позицию клипа в склейке (скорость пока естественная)
            x["start"] += spans[idx][0]
            x["end"] += spans[idx][0]
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
    if args.speed and args.speed != 1.0:
        # Ускорение — В САМОМ КОНЦЕ, одним проходом по готовому ролику
        # (решение Кирилла, 14.09): субтитры уже прожжены и ускоряются вместе
        # с картинкой, whisper слушал речь в естественном темпе.
        burned = WORK / "burned.mp4"
        burn(joined, events, hooks, WORK / "subs", burned)
        print(f"→ Собрано: {ffprobe_duration(burned):.1f}s, ускоряю x{args.speed}…")
        speed_up(burned, out, args.speed)
    else:
        burn(joined, events, hooks, WORK / "subs", out)
    print(f"✓ Готово: {out}  ({ffprobe_duration(out):.1f}s)")


if __name__ == "__main__":
    main()
